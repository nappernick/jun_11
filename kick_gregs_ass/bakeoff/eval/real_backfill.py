"""
Real-data backfill for the eval dashboard — REAL bake-off records, never synthetic.

The Eval 3D / Eval 2D views render :class:`~bakeoff.eval.models.EvalInstance`
records from an :class:`~bakeoff.eval.event_store.EvalEventStore`. Until now the
only producer was the offline synthetic runner, so the views had nothing real to
display. This module maps the REAL bake-off run's durable records into that shape:

* ``data/bakeoff/outcomes.jsonl`` — one record per executed trial (latency stages,
  retrieval confidence/cache, token usage, accuracy metrics, answer text);
* ``data/bakeoff/judge_scores.jsonl`` — the Phase-2 Opus judge verdicts, joined by
  ``trial_id``.

Models under test ONLY (owner direction 2026-06-10): ``sonnet-4.6-thinking-off``
and ``haiku-4.5``. ``claude-sonnet-4.6-thinking-on-converse`` records exist in the
source data but are NOT tested here and are skipped.

Outputs — three NEW files (clear lineage; the synthetic producer's default store
is never touched):

* ``config.EVAL_REAL_INSTANCES_PATH`` — the EvalInstance store the dashboard reads
  (point the app at it via the ``GBBO_EVAL_EVENTS_PATH`` env var);
* ``config.EVAL_REAL_RUN_DETAILS_PATH`` — per-instance provenance: the trial_id,
  source file, pass, item, rep and timestamps each instance came from;
* ``config.EVAL_REAL_JUDGE_PATH`` — the joined judge outputs per trial, kept as
  their own artifact (judge signals stay DISTINCT from ragas-style metrics,
  Req 18.2/18.3 — inside each instance they ride under ``judge_*`` keys).

Metric mapping (all real, recorded values — nothing is recomputed):

* ragas map (generation quality): ``semantic_similarity``, ``grounding_precision``,
  ``grounding_recall`` from ``quality.accuracy``; the joined Opus triad as
  ``judge_faithfulness`` / ``judge_correctness`` / ``judge_completeness`` (clearly
  judge-labeled, with the judge model recorded as provenance).
* retrieval map (gold-link quality): ``precision_at_k`` / ``recall_at_k`` / ``mrr``
  / ``ndcg_at_k`` from ``quality.accuracy`` at the run's k=5.

Idempotent: the output files are rewritten from scratch on every invocation (the
source of truth is the bake-off log, not the backfill output).

Usage::

    PYTHONPATH=. .venv/bin/python -m bakeoff.eval.real_backfill
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Optional

from bakeoff import config
from bakeoff.eval.event_store import EvalEventStore
from bakeoff.eval.models import EvalInstance, MetricValue, StageTimings

__all__ = ["backfill_real_eval_data", "MODELS_UNDER_TEST"]

#: Converse-id → dashboard agent key, models under test ONLY (owner direction:
#: thinking-on appears in the source data but is NOT tested here).
MODELS_UNDER_TEST: dict[str, str] = {
    "claude-sonnet-4.6-thinking-off-converse": "sonnet-4.6-thinking-off",
    "claude-haiku-4.5-converse": "haiku-4.5",
}

#: quality.accuracy keys that map into the ragas (generation-quality) map.
_RAGAS_ACCURACY_KEYS = ("semantic_similarity", "grounding_precision", "grounding_recall")
#: quality.accuracy keys that map into the retrieval (gold-link) map.
_RETRIEVAL_ACCURACY_KEYS = ("precision_at_k", "recall_at_k", "mrr", "ndcg_at_k")
#: joined judge dimensions carried into the ragas map under judge_* keys.
_JUDGE_DIMENSIONS = ("faithfulness", "correctness", "completeness")
#: the retrieval cutoff the bake-off ran with (provenance on retrieval metrics).
_RETRIEVAL_K = 5


def _unit_metric(raw_value: Any, **provenance: Any) -> Optional[MetricValue]:
    """Build an available MetricValue from a recorded score; None when unusable."""
    if raw_value is None or isinstance(raw_value, bool):
        return None
    try:
        as_float = float(raw_value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(as_float):
        return None
    return MetricValue(value=as_float, **provenance)


def _mean_confidence(confidences: Any) -> Optional[float]:
    """Mean of the recorded per-fragment reranker confidences (the bubble proxy)."""
    if not isinstance(confidences, (list, tuple)) or not confidences:
        return None
    numeric = [c for c in confidences if isinstance(c, (int, float)) and math.isfinite(c)]
    return (sum(numeric) / len(numeric)) if numeric else None


def _corpus_size(corpus_csv: Path) -> int:
    """The real corpus size (rows minus header); 0 when the CSV is absent."""
    try:
        with corpus_csv.open() as handle:
            return max(0, sum(1 for _ in handle) - 1)
    except OSError:
        return 0


def _load_judge_by_trial(judge_scores_path: Path) -> dict[str, dict]:
    """Index the Phase-2 judge records by trial_id (last record wins per trial)."""
    judge_by_trial: dict[str, dict] = {}
    try:
        with judge_scores_path.open() as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                trial_id = record.get("trial_id")
                if isinstance(trial_id, str):
                    judge_by_trial[trial_id] = record
    except OSError:
        pass
    return judge_by_trial


def backfill_real_eval_data(
    *,
    outcomes_path: Path = config.BAKEOFF_DIR / "outcomes.jsonl",
    judge_scores_path: Path = config.BAKEOFF_DIR / "judge_scores.jsonl",
    corpus_csv_path: Path = Path("data/faq_corpus.csv"),
    instances_path: Path = config.EVAL_REAL_INSTANCES_PATH,
    run_details_path: Path = config.EVAL_REAL_RUN_DETAILS_PATH,
    judge_out_path: Path = config.EVAL_REAL_JUDGE_PATH,
) -> dict:
    """Map the real bake-off records into the three eval backfill files.

    Returns a summary dict (counts per agent, joins, skips) for logging/tests.
    """
    judge_by_trial = _load_judge_by_trial(judge_scores_path)
    corpus_size = _corpus_size(corpus_csv_path)

    # Fresh outputs each run — the bake-off log is the source of truth.
    for path in (instances_path, run_details_path, judge_out_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    store = EvalEventStore(instances_path)

    instance_counts: dict[str, int] = defaultdict(int)
    session_indexes: dict[str, int] = defaultdict(int)
    skipped_models: dict[str, int] = defaultdict(int)
    judge_joined = 0
    malformed = 0

    with run_details_path.open("a") as details_handle, judge_out_path.open("a") as judge_handle:
        with outcomes_path.open() as outcomes_handle:
            for line in outcomes_handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    outcome: Mapping[str, Any] = json.loads(line)
                except ValueError:
                    malformed += 1
                    continue

                source_model = str(outcome.get("model") or "")
                agent_id = MODELS_UNDER_TEST.get(source_model)
                if agent_id is None:
                    skipped_models[source_model] += 1
                    continue

                trial_id = str(outcome.get("trial_id") or "")
                if not trial_id:
                    malformed += 1
                    continue

                timings = dict(outcome.get("timings") or {})
                quality = dict(outcome.get("quality") or {})
                accuracy = dict(quality.get("accuracy") or {})
                retrieval_block = dict(outcome.get("retrieval") or {})
                token_usage = dict(outcome.get("token_usage") or {})
                pass_name = str(outcome.get("pass_name") or "run")

                # --- ragas (generation-quality) map: recorded accuracy + joined judge ---
                ragas_map: dict[str, MetricValue] = {}
                for key in _RAGAS_ACCURACY_KEYS:
                    metric = _unit_metric(accuracy.get(key))
                    if metric is not None:
                        ragas_map[key] = metric
                judge_record = judge_by_trial.get(trial_id)
                if judge_record is not None:
                    judge_block = dict(judge_record.get("judge") or {})
                    judge_model = judge_block.get("judge_model")
                    for dimension in _JUDGE_DIMENSIONS:
                        metric = _unit_metric(
                            judge_block.get(dimension),
                            bedrock_model_id=(
                                str(judge_model) if judge_model is not None else None
                            ),
                        )
                        if metric is not None:
                            ragas_map[f"judge_{dimension}"] = metric
                    judge_joined += 1
                    judge_handle.write(
                        json.dumps(
                            {
                                "trial_id": trial_id,
                                "agent_id": agent_id,
                                "item_id": outcome.get("item_id"),
                                "judged_at": judge_record.get("judged_at"),
                                "judge": judge_block,
                            }
                        )
                        + "\n"
                    )

                # --- retrieval (gold-link) map ---
                retrieval_map: dict[str, MetricValue] = {}
                for key in _RETRIEVAL_ACCURACY_KEYS:
                    metric = _unit_metric(accuracy.get(key), k=_RETRIEVAL_K)
                    if metric is not None:
                        retrieval_map[key] = metric

                # --- per-stage timings (recorded, never derived) ---
                stage_timings = StageTimings(
                    retrieval_ms=timings.get("retrieval_total_ms"),
                    generation_ms=timings.get("generation_total_ms"),
                    extra_ms={
                        stage: value
                        for stage, value in timings.items()
                        if stage
                        not in ("retrieval_total_ms", "generation_total_ms", "end_to_end_ms")
                        and isinstance(value, (int, float))
                    },
                )

                session_key = f"{agent_id}:{pass_name}"
                instance_index = session_indexes[session_key]
                session_indexes[session_key] += 1

                error_text = outcome.get("error")
                cohort = outcome.get("cohort") or {}
                category = (
                    f"{outcome.get('turn_type') or 'single'}/"
                    f"{outcome.get('answerability') or (cohort.get('answerability') if isinstance(cohort, dict) else None) or 'unknown'}"
                )

                instance = EvalInstance(
                    instance_id=f"real-{trial_id}",
                    agent_id=agent_id,
                    session_id=session_key,
                    instance_index=instance_index,
                    timestamp=str(outcome.get("completed_at") or outcome.get("started_at") or ""),
                    latency_ms=float(timings.get("end_to_end_ms") or 0.0),
                    stage_timings=stage_timings,
                    corpus_size=corpus_size,
                    retrieval_cached=bool(retrieval_block.get("cache_hit", False)),
                    ragas=ragas_map,
                    retrieval=retrieval_map,
                    confidence=_mean_confidence(retrieval_block.get("confidence")),
                    volume=(
                        float(token_usage["total"])
                        if isinstance(token_usage.get("total"), (int, float))
                        else None
                    ),
                    cost=None,  # no recorded $ value; never invent one
                    prompt_id=pass_name,
                    category=category,
                    status="failed" if error_text else "ok",
                    error=str(error_text) if error_text else None,
                )
                store.append(instance)
                instance_counts[agent_id] += 1

                details_handle.write(
                    json.dumps(
                        {
                            "instance_id": instance.instance_id,
                            "trial_id": trial_id,
                            "source": str(outcomes_path),
                            "agent_id": agent_id,
                            "source_model": source_model,
                            "pass_name": pass_name,
                            "item_id": outcome.get("item_id"),
                            "rep": outcome.get("rep"),
                            "started_at": outcome.get("started_at"),
                            "completed_at": outcome.get("completed_at"),
                            "judge_joined": judge_record is not None,
                        }
                    )
                    + "\n"
                )

    summary = {
        "instances": dict(instance_counts),
        "total_instances": sum(instance_counts.values()),
        "judge_joined": judge_joined,
        "skipped_models": dict(skipped_models),
        "malformed_lines": malformed,
        "corpus_size": corpus_size,
        "outputs": {
            "instances": str(instances_path),
            "run_details": str(run_details_path),
            "judge": str(judge_out_path),
        },
    }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outcomes", type=Path, default=config.BAKEOFF_DIR / "outcomes.jsonl")
    parser.add_argument(
        "--judge-scores", type=Path, default=config.BAKEOFF_DIR / "judge_scores.jsonl"
    )
    parser.add_argument("--corpus-csv", type=Path, default=Path("data/faq_corpus.csv"))
    args = parser.parse_args()

    summary = backfill_real_eval_data(
        outcomes_path=args.outcomes,
        judge_scores_path=args.judge_scores,
        corpus_csv_path=args.corpus_csv,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
