"""REAL eval runner — prompt files (the series) × queries.jsonl, over the LIVE stack.

This is the producer the Metrics tab triggers. Unlike the synthetic on-demand runner
(invented queries/agents/scores) and unlike :mod:`bakeoff.eval.real_backfill` (which
re-projects an already-collected bake-off), this RUNS the real pipeline now:

    for each prompt file in the prompt folder (one SERIES / colour each):
      for each sampled query from data/synthetic/queries.jsonl (one POINT IN TIME each):
        live AOSS retrieve  ->  real model generate (prompt = system instruction)
        ->  Opus judge (faithfulness / correctness / completeness)
        ->  retrieval metrics (precision/recall/ndcg vs gold_node_ids)
        ->  one EvalInstance appended to the eval store the dashboard reads.

The series is the PROMPT (XML_short, XML_long, …) — the model is held fixed so the
comparison is "which prompt is better", which is what the owner asked for. Each
execution is stamped with a monotonically increasing ``execution_index`` (the 3D
time axis) and ``latency_ms`` = model generation time (the 3D depth axis); the judge
triad rides the ragas map under ``judge_*`` keys (the 3D quality axis).

Reuses the optimizer's live backend (AOSS retrieval on alpha, model + Opus judge),
so it inherits the per-turn-fragment grounding fix and the resilient clients. Writes
to :data:`config.EVAL_REAL_INSTANCES_PATH` (the store the dashboard reads when
``GBBO_EVAL_EVENTS_PATH`` points at it).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional, Sequence

from bakeoff import config
from bakeoff.dataset import DatasetLoader
from bakeoff.eval.event_store import EvalEventStore
from bakeoff.eval.models import EvalInstance, MetricValue, StageTimings
from bakeoff.quality.dataset import ideal_response_text
from bakeoff.quality.optimizer.backends import build_live_backend
from bakeoff.quality.optimizer.retrieval import RetrievalQuery
from bakeoff.scoring.judge import JUDGE_DIMENSIONS
from bakeoff.scoring.retrieval_aligned import ndcg_at_k, precision_at_k, recall_at_k

__all__ = ["run_real_eval", "load_prompt_series", "EvalRunProgress"]

#: The model held fixed across all prompt series (the comparison is prompt-vs-prompt).
DEFAULT_EVAL_MODEL = "sonnet-4.6-thinking-off"
#: Retrieval cutoff for the gold-link metrics (matches the bake-off's k).
_RETRIEVAL_K = 5
_DECLINE_IDEAL = (
    "Correctly decline: state you don't have the information in the reference "
    "material and point the user to the right owner."
)


@dataclass
class PromptSeries:
    """One prompt file = one coloured series in the 3D view."""

    key: str          # the agent_id / series label (filename stem)
    instruction: str  # the system prompt text


@dataclass
class EvalRunProgress:
    """Live progress, passed to the optional callback after each instance."""

    series: str
    done: int
    total: int
    last_quality: Optional[float]
    last_latency_ms: Optional[float]


def load_prompt_series(prompt_dir: Path) -> list[PromptSeries]:
    """Every ``*.txt`` directly in ``prompt_dir`` becomes a series (sorted by name).

    Subfolders and non-.txt files (e.g. images) are ignored — drop a ``.txt`` in to
    add a series.
    """
    series: list[PromptSeries] = []
    for path in sorted(prompt_dir.glob("*.txt")):
        text = path.read_text(encoding="utf-8").strip()
        if text:
            series.append(PromptSeries(key=path.stem, instruction=text))
    return series


def _sample_single_turn_items(query_count: int) -> list:
    """First ``query_count`` single-turn queries from queries.jsonl (deterministic order)."""
    items = [it for it in DatasetLoader().load_items() if it.turn_type == "single"]
    return items[: max(1, query_count)]


def _judge_inputs(item) -> tuple[str, list[str], str]:
    """(ideal_text, gold_texts, answerability) for a single-turn item."""
    answerability = (item.answerability or item.cohort.answerability or "full").lower()
    if answerability == "none" or not item.gold:
        return _DECLINE_IDEAL, [], "none"
    gold_texts = [g.markdown or g.snippet or g.title for g in item.gold
                  if (g.markdown or g.snippet or g.title)]
    ideal = ideal_response_text(item.gold, item.wants)
    return ideal, gold_texts, answerability


def _metric(value: Optional[float], **prov) -> Optional[MetricValue]:
    if value is None:
        return None
    try:
        return MetricValue(value=float(value), **prov)
    except (TypeError, ValueError):
        return None


async def run_real_eval(
    *,
    query_count: int = 100,
    prompt_dir: Path = config.REPO_ROOT / "data" / "prompts",
    model: str = DEFAULT_EVAL_MODEL,
    store_path: Path = config.EVAL_REAL_INSTANCES_PATH,
    store: Optional[object] = None,
    temperature: float = config.DEFAULT_TEMPERATURE,
    max_concurrency: int = 12,
    on_progress: Optional[Callable[[EvalRunProgress], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> dict:
    """Run the real eval and append EvalInstance records to the store.

    ``store`` may be any object with an ``append(instance)`` method (e.g. a
    publishing store that also emits SSE deltas); when ``None`` a durable
    :class:`EvalEventStore` over ``store_path`` is used. ``on_progress`` (if given) is
    called after each instance; ``should_stop`` (if given) is polled for cancellation.
    """
    series = load_prompt_series(prompt_dir)
    if not series:
        raise ValueError(f"no *.txt prompt files found in {prompt_dir}")
    items = _sample_single_turn_items(query_count)
    backend = build_live_backend()
    # ISOLATION: run the eval's model generation + Opus judge on the DEDICATED eval account
    # (config.QUALITY_OPT_EVAL_PROFILE) so they never contend with the optimizer's judge /
    # execution accounts. Retrieval stays on alpha via backend.retrieval (only alpha signs AOSS).
    from bakeoff.quality.optimizer.backends import _bedrock_model_id_for
    from bakeoff.quality.optimizer.inline_session_adapter import PersistentSessionInlineAdapter
    from bakeoff.scoring.judge import JudgeScorer, make_bedrock_judge

    eval_profile = config.QUALITY_OPT_EVAL_PROFILE
    eval_region = config.CREDENTIAL_PROFILES.get(eval_profile, {}).get("region", config.AWS_REGION)
    judge_scorer = JudgeScorer(
        backend=make_bedrock_judge(
            config.JUDGE_MODEL_ID, region=eval_region, credential_profile=eval_profile
        ),
        judge_model=config.JUDGE_MODEL_ID,
    )

    def _eval_adapter_factory(model_key: str, instruction: str, lookup: dict):
        return PersistentSessionInlineAdapter(
            model_key, _bedrock_model_id_for(model_key),
            instruction_override=instruction,
            credential_profile=eval_profile, region=eval_region,
        )

    if store is None:
        store = EvalEventStore(store_path)
    item_lookup = {it.id: it for it in items}

    sem = asyncio.Semaphore(max_concurrency)
    counter = {"n": 0, "ok": 0, "failed": 0}
    per_series: dict[str, int] = {s.key: 0 for s in series}
    total = len(series) * len(items)

    async def one(series_item: PromptSeries, item, execution_index: int) -> None:
        if should_stop and should_stop():
            return
        async with sem:
            query_text = item.query or ""
            ideal, gold_texts, answerability = _judge_inputs(item)
            t0 = time.perf_counter()
            status, error = "ok", None
            ragas_map: dict[str, MetricValue] = {}
            retrieval_map: dict[str, MetricValue] = {}
            generation_ms = retrieval_ms = 0.0
            frags: list = []
            answer = ""
            try:
                # 1) live AOSS retrieve
                _tr = time.perf_counter()
                frags = await backend.retrieval.retrieve(
                    RetrievalQuery(item_id=item.id, turn=1, query=query_text)
                )
                retrieval_ms = (time.perf_counter() - _tr) * 1000.0
                # The gold node id lives in metadata.fragment_id (the AOSS "id" is the
                # internal doc id, which never matches gold_node_ids). Use the former so
                # precision/recall/ndcg are real, not a flat zero.
                ranked_ids = [
                    str((f.get("metadata") or {}).get("fragment_id")
                        or (f.get("metadata") or {}).get("source_id")
                        or f.get("id", ""))
                    for f in frags
                ]

                # 2) real model generate with THIS prompt as the system instruction
                adapter = _eval_adapter_factory(model, series_item.instruction, item_lookup)
                _tg = time.perf_counter()
                resp = await adapter.generate(item, {0: list(frags)}, temperature)
                generation_ms = (time.perf_counter() - _tg) * 1000.0
                answer = (resp.per_turn_answers or [resp.text])[0]

                # 3) Opus judge (off the event loop)
                scores, _ev = await asyncio.to_thread(
                    judge_scorer.score_detailed,
                    answer, ideal_text=ideal, fragments=frags, gold_texts=gold_texts,
                    momentary_state=item.cohort.momentary_state, answerability=answerability,
                    question=query_text,
                )
                jm = getattr(judge_scorer, "judge_model", None)
                for dim in JUDGE_DIMENSIONS:
                    mv = _metric(getattr(scores, dim, None), bedrock_model_id=jm)
                    if mv is not None:
                        ragas_map[f"judge_{dim}"] = mv

                # 4) retrieval metrics vs gold
                gold_ids = list(item.gold_node_ids or [])
                if gold_ids:
                    retrieval_map["precision_at_k"] = MetricValue(
                        value=precision_at_k(ranked_ids, gold_ids, _RETRIEVAL_K), k=_RETRIEVAL_K)
                    retrieval_map["recall_at_k"] = MetricValue(
                        value=recall_at_k(ranked_ids, gold_ids, _RETRIEVAL_K), k=_RETRIEVAL_K)
                    retrieval_map["ndcg_at_k"] = MetricValue(
                        value=ndcg_at_k(ranked_ids, gold_ids, _RETRIEVAL_K), k=_RETRIEVAL_K)
            except Exception as exc:  # noqa: BLE001 - one execution failing is a 'failed' point
                status, error = "failed", repr(exc)

            latency_ms = (time.perf_counter() - t0) * 1000.0
            now_iso = datetime.now(timezone.utc).isoformat()
            # Bubble-size sources: mean retrieved-fragment confidence + answer volume.
            confs = [float(f.get("confidence", 0)) for f in frags
                     if isinstance(f, dict) and f.get("confidence") is not None]
            mean_conf = (sum(confs) / len(confs)) if confs else None
            answer_volume = float(len(answer)) if answer else None
            instance = EvalInstance(
                instance_id=f"real::{series_item.key}::{item.id}",
                agent_id=series_item.key,
                session_id=series_item.key,
                instance_index=execution_index,   # the 3D TIME axis (global execution order)
                timestamp=now_iso,
                latency_ms=generation_ms if status == "ok" else latency_ms,
                stage_timings=StageTimings(retrieval_ms=retrieval_ms, generation_ms=generation_ms),
                corpus_size=len(items),
                retrieval_cached=False,
                ragas=ragas_map,
                retrieval=retrieval_map,
                confidence=mean_conf, volume=answer_volume, cost=None,
                prompt_id=series_item.key,
                category=f"single/{answerability}",
                status=status, error=error,
            )
            store.append(instance)
            counter["n"] += 1
            counter["ok" if status == "ok" else "failed"] += 1
            per_series[series_item.key] += 1
            if on_progress:
                triad = [ragas_map[f"judge_{d}"].value for d in JUDGE_DIMENSIONS
                         if f"judge_{d}" in ragas_map]
                on_progress(EvalRunProgress(
                    series=series_item.key, done=counter["n"], total=total,
                    last_quality=(sum(triad) / len(triad)) if triad else None,
                    last_latency_ms=generation_ms if status == "ok" else None,
                ))

    # RESUME: skip every (prompt, query) pair already recorded OK on disk, and continue
    # the execution-order (time) axis past what's there so resumed points sort as later
    # in time. A stable instance_id (real::<prompt>::<query>) is what makes this idempotent
    # — re-running or resuming overwrites a pair rather than stacking a duplicate.
    existing_ok: set[str] = set()
    existing_count = 0
    try:
        for prior in EvalEventStore(store_path).read_all():
            existing_count += 1
            if getattr(prior, "status", "ok") == "ok":
                existing_ok.add(prior.instance_id)
    except Exception:  # noqa: BLE001 - a missing/partial store just means "nothing to resume"
        pass

    pending = [
        (series_item, item)
        for series_item in series  # series-major: each prompt fills as a band across time
        for item in items
        if f"real::{series_item.key}::{item.id}" not in existing_ok
    ]
    total = len(pending)
    tasks = [
        one(series_item, item, existing_count + offset)
        for offset, (series_item, item) in enumerate(pending)
    ]
    await asyncio.gather(*tasks)

    return {
        "model": model,
        "prompt_dir": str(prompt_dir),
        "series": [s.key for s in series],
        "query_count": len(items),          # the per-prompt target
        "executed": counter["n"],           # how many ran this invocation (the gap)
        "resumed_from": existing_count,      # points already on disk before this run
        "stopped": bool(should_stop and should_stop()),
        "total_instances": counter["n"],
        "ok": counter["ok"],
        "failed": counter["failed"],
        "per_series": per_series,
        "store": str(store_path),
    }
