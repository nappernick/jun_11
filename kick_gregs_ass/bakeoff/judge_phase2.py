"""
Phase-2 deferred LLM-as-judge pass (the judge runs AFTER generation, on a sample).

The judge (Opus 4.x) is **slow and TPM-limited** and must never run inside the
generation hot loop: doing so would couple candidate-data collection to grader
fragility and risk not finishing the full ~23k-trial run. So generation (Phase 1)
records every candidate's answer + latency + the local, pure-CPU quality
components into the clean :data:`bakeoff.config.OUTCOMES_PATH` store with the
judge dimensions left *neutral* (``judge_model == "(deferred)"``;
:data:`bakeoff.scoring.pipeline.DEFERRED_JUDGE_MODEL`). This module is **Phase 2**:
it reads those outcomes, draws a stratified SAMPLE, runs the real judge on just
that subset, and writes the verdicts to :data:`bakeoff.config.JUDGE_SCORES_PATH`,
keyed by ``trial_id`` — a SEPARATE store from the outcomes, never mixed in.

Why a sample, and how big (the owner's inline Bake-Off target):

    judge_attempts ≈ items_per_model × n_models × JUDGE_SAMPLES_K

With the default :data:`bakeoff.config.JUDGE_SAMPLE_ITEMS_PER_MODEL` (300) × 2
candidate models × ``k`` = 3 judge samples per answer ≈ ~1,800 Opus calls. The
sample size is a single dial (``items_per_model=``) so the operator can scale the
judge's coverage up or down on demand without touching anything else.

Reconstructing the judge's inputs (the load-bearing detail). A stored
:class:`~bakeoff.types.TrialEvent` carries the candidate's ``answer_text``,
``answerability``, ``cohort.momentary_state``, ``item_id``, ``gold_node_ids``, and
the retrieved ``fragment_ids`` — but NOT the gold fragment *text* nor the
retrieved fragment *text* the anchored judge prompt grades against. Phase 2
rebuilds those deterministically from the same two sources Phase 1 used:

* the **dataset** (``item_id`` → :class:`~bakeoff.types.Item`) yields the resolved
  gold fragments and the item's ``wants``, from which
  :func:`bakeoff.scoring.semantic.ideal_response_text` rebuilds the ideal text and
  the per-gold ``gold_texts``;
* the **held-constant retrieval substrate** is re-queried with the event's own
  ``query`` to recover the retrieved fragment objects. After a completed Phase-1
  run the retrieval result cache on disk is fully populated, so this re-retrieval
  is served entirely from cache — **zero** backend calls — which is exactly the
  "retrieval is a held constant" guarantee paying off.

Because the judge's content-hash cache key is
``(answer, ideal, fragments, momentary_state, answerability)`` and every one of
those is rebuilt from the same deterministic sources, a Phase-2 score is the
identical score Phase 1 would have produced inline — just deferred.

Durability + resumability. Verdicts are appended one JSON line per judged trial
to :data:`JUDGE_SCORES_PATH`, fsync'd. A re-invocation reads what is already
there and **skips** those ``trial_id``s, so an interrupted Phase-2 pass resumes
and runs only the not-yet-judged trials — the same append-only / diff-the-log
discipline the generation runner uses.

Everything is injectable (the dataset/items, the judge scorer, the
fragment provider, every path) so the whole pass runs fully offline in tests with
a deterministic stub judge and zero network.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional, Sequence, Union

from bakeoff import config
from bakeoff.eventlog import read_events
from bakeoff.scoring.judge import JUDGE_DIMENSIONS, JudgeScorer
from bakeoff.scoring.pipeline import DEFERRED_JUDGE_MODEL
from bakeoff.scoring.semantic import ideal_response_text
from bakeoff.types import GoldFragment, Item, JudgeScores, TrialEvent

__all__ = [
    "JudgeScoreRecord",
    "Phase2Result",
    "read_judge_scores",
    "append_judge_score",
    "select_sample",
    "run_deferred_judge",
    "summarize_judge_scores",
    "DEFAULT_SAMPLE_SEED",
]

PathLike = Union[str, "os.PathLike[str]"]

#: Default seed for the deterministic, reproducible stratified sample. Changing
#: it draws a different (still representative) subset; holding it fixed makes the
#: sampled trial set a pure function of the outcomes log.
DEFAULT_SAMPLE_SEED: int = 1729

#: A fragment provider rebuilds the retrieved fragment objects the judge grades
#: against, for one outcome event + its dataset item. The default re-queries the
#: held-constant retrieval substrate (cache-served after a completed run); tests
#: inject a deterministic provider so the pass is fully offline.
FragmentProvider = Callable[[TrialEvent, Optional[Item]], Awaitable[Sequence[dict]]]


# ---------------------------------------------------------------------------
# The Phase-2 verdict record + its (de)serialization
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class JudgeScoreRecord:
    """One Phase-2 judge verdict, keyed to the outcome trial it grades.

    Stored as one JSON line in :data:`JUDGE_SCORES_PATH`. It is intentionally a
    SEPARATE record type from :class:`~bakeoff.types.TrialEvent`: the outcome is
    the decision data (the model's answer + latency + local quality), and this is
    the deferred grader's enrichment of it, joined back by ``trial_id`` only when
    an aggregation actually wants the judge dimensions. ``model`` / ``item_id`` /
    ``answerability`` are denormalized on so the judge store can be sliced without
    re-joining the outcomes log.

    ``evidence`` carries the judge's quoted supporting span(s) (the written
    "opinion" — e.g. the faithfulness grounding quote), and ``answer_excerpt`` a
    trimmed copy of the graded answer, so the dashboard's judge view can show
    *what* was graded and *why* it scored as it did without re-joining the
    outcomes log. ``momentary_state`` is the cohort state the interaction
    dimensions were judged against.
    """

    trial_id: str
    model: str
    item_id: str
    answerability: str
    judge: JudgeScores
    judged_at: str
    evidence: dict[str, str] = dataclasses.field(default_factory=dict)
    answer_excerpt: str = ""
    momentary_state: str = "neutral"

    def to_jsonl(self) -> str:
        """Serialize to a single-line JSON string (no embedded newline)."""
        payload = {
            "trial_id": self.trial_id,
            "model": self.model,
            "item_id": self.item_id,
            "answerability": self.answerability,
            "judge": dataclasses.asdict(self.judge),
            "judged_at": self.judged_at,
            "evidence": dict(self.evidence),
            "answer_excerpt": self.answer_excerpt,
            "momentary_state": self.momentary_state,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_jsonl(cls, line: str) -> "JudgeScoreRecord":
        """Parse one JSON line back into a fully-typed record."""
        d = json.loads(line)
        j = d["judge"]
        judge = JudgeScores(
            faithfulness=float(j["faithfulness"]),
            correctness=float(j["correctness"]),
            completeness=float(j["completeness"]),
            judge_sample_count=int(j["judge_sample_count"]),
            judge_model=str(j["judge_model"]),
            judge_dim_sd={k: float(v) for k, v in (j.get("judge_dim_sd") or {}).items()},
        )
        return cls(
            trial_id=d["trial_id"],
            model=d["model"],
            item_id=d["item_id"],
            answerability=d["answerability"],
            judge=judge,
            judged_at=d["judged_at"],
            evidence={k: str(v) for k, v in (d.get("evidence") or {}).items()},
            answer_excerpt=str(d.get("answer_excerpt", "")),
            momentary_state=str(d.get("momentary_state", "neutral")),
        )


@dataclasses.dataclass(frozen=True)
class Phase2Result:
    """Summary of one Phase-2 pass, for the CLI / caller / tests.

    ``judged`` counts verdicts written this invocation; ``skipped_existing``
    counts sampled trials already present in the judge store (resume); ``sampled``
    is the full selected set size; ``models`` maps model → number of judged trials.
    """

    outcomes_seen: int
    sampled: int
    judged: int
    skipped_existing: int
    judge_scores_path: Path
    models: dict[str, int]


# ---------------------------------------------------------------------------
# The judge store: durable append-only JSONL keyed by trial_id
# ---------------------------------------------------------------------------
def read_judge_scores(path: PathLike) -> list[JudgeScoreRecord]:
    """Read all judge verdicts from ``path`` (``[]`` if absent).

    Tolerates a crash-truncated trailing line (the signature of a process killed
    mid-write): the final unparseable line is discarded and the complete prefix is
    returned, mirroring :func:`bakeoff.eventlog.read_events`. A malformed line that
    is NOT the final line is real corruption and raises.
    """
    p = Path(path)
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8") as f:
        raw_lines = f.readlines()
    out: list[JudgeScoreRecord] = []
    last = len(raw_lines) - 1
    for i, raw in enumerate(raw_lines):
        line = raw.rstrip("\n")
        if not line:
            continue
        try:
            out.append(JudgeScoreRecord.from_jsonl(line))
        except Exception:  # noqa: BLE001 - tolerate only a truncated final line
            if i == last:
                break
            raise
    return out


def append_judge_score(path: PathLike, record: JudgeScoreRecord) -> None:
    """Append exactly one verdict line to ``path``, durably (fsync'd).

    Single write of a complete line in append mode, then flush + ``os.fsync`` so
    the verdict is on disk before returning — the same durability discipline as
    :func:`bakeoff.eventlog.append_event`, so a crash leaves at most one truncated
    trailing line (which :func:`read_judge_scores` tolerates).
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = record.to_jsonl() + "\n"
    with open(p, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


# ---------------------------------------------------------------------------
# Sample selection — one representative trial per (model, item), stratified by
# answerability, deterministic and seeded
# ---------------------------------------------------------------------------
def _representative_by_item(
    events: Sequence[TrialEvent],
) -> dict[tuple[str, str], TrialEvent]:
    """Reduce a model's events to one representative trial per ``item_id``.

    The representative is the lowest ``(rep, trial_id)`` for the item, so the
    choice is deterministic. Keyed by ``(model, item_id)``.
    """
    best: dict[tuple[str, str], TrialEvent] = {}
    for ev in events:
        key = (ev.model, ev.item_id)
        cur = best.get(key)
        if cur is None or (ev.rep, ev.trial_id) < (cur.rep, cur.trial_id):
            best[key] = ev
    return best


def _stratified_allocation(strata_sizes: dict[str, int], total: int) -> dict[str, int]:
    """Allocate ``total`` picks across strata proportional to size (largest-remainder).

    Proportional (Hamilton) apportionment: each stratum gets ``floor`` of its
    proportional share, then the leftover picks go to the largest fractional
    remainders (ties broken by stratum name for determinism). Never allocates a
    stratum more than it has. When ``total`` meets or exceeds the available count,
    every stratum is allocated its full size.
    """
    available = sum(strata_sizes.values())
    if total >= available:
        return dict(strata_sizes)
    if total <= 0 or available == 0:
        return {s: 0 for s in strata_sizes}

    exact = {s: total * n / available for s, n in strata_sizes.items()}
    alloc = {s: int(v) for s, v in exact.items()}
    # Cap floors at the stratum size (proportional share can't exceed it, but be safe).
    alloc = {s: min(alloc[s], strata_sizes[s]) for s in strata_sizes}
    remaining = total - sum(alloc.values())
    # Distribute leftovers by largest fractional remainder, skipping full strata.
    remainders = sorted(
        strata_sizes,
        key=lambda s: (-(exact[s] - int(exact[s])), s),
    )
    i = 0
    while remaining > 0 and any(alloc[s] < strata_sizes[s] for s in strata_sizes):
        s = remainders[i % len(remainders)]
        if alloc[s] < strata_sizes[s]:
            alloc[s] += 1
            remaining -= 1
        i += 1
    return alloc


def _sample_rank(seed: int, model: str, item_id: str) -> str:
    """A stable, seeded ordering key for an item within a stratum.

    Hashing ``(seed, model, item_id)`` spreads the selection across the item space
    (rather than biasing toward low item ids) while staying fully reproducible for
    a fixed ``seed``.
    """
    return hashlib.sha256(f"{seed}\x1f{model}\x1f{item_id}".encode("utf-8")).hexdigest()


def select_sample(
    outcomes: Sequence[TrialEvent],
    *,
    items_per_model: int = config.JUDGE_SAMPLE_ITEMS_PER_MODEL,
    seed: int = DEFAULT_SAMPLE_SEED,
    models: Optional[Sequence[str]] = None,
) -> list[TrialEvent]:
    """Pick the trials to judge: ~``items_per_model`` per model, answerability-stratified.

    For each model, the outcomes are reduced to one representative trial per item
    (so the judge grades one answer per (model, item) — matching the ~3k-attempts
    sizing where ``k`` internal judge samples happen inside each score), then the
    per-item trials are stratified by ``answerability`` and ``items_per_model``
    picks are allocated across the strata proportionally (largest-remainder).
    Within each stratum the picks are the items with the smallest seeded rank, so
    the sample is representative of the answerability mix and fully reproducible.

    Returns the selected trials sorted by ``(model, item_id)`` for stable output.
    A model with fewer than ``items_per_model`` items contributes all of them.
    """
    by_item = _representative_by_item(outcomes)

    # Group the representative trials per model.
    per_model: dict[str, list[TrialEvent]] = defaultdict(list)
    for ev in by_item.values():
        per_model[ev.model].append(ev)

    wanted_models = set(models) if models is not None else set(per_model)
    selected: list[TrialEvent] = []

    for model in sorted(per_model):
        if model not in wanted_models:
            continue
        model_events = per_model[model]
        # Stratify this model's items by answerability.
        strata: dict[str, list[TrialEvent]] = defaultdict(list)
        for ev in model_events:
            strata[ev.answerability].append(ev)
        strata_sizes = {s: len(evs) for s, evs in strata.items()}
        alloc = _stratified_allocation(strata_sizes, items_per_model)
        for s, evs in strata.items():
            take = alloc.get(s, 0)
            if take <= 0:
                continue
            ranked = sorted(evs, key=lambda e: _sample_rank(seed, e.model, e.item_id))
            selected.extend(ranked[:take])

    selected.sort(key=lambda e: (e.model, e.item_id))
    return selected


# ---------------------------------------------------------------------------
# Judge-input reconstruction (from the dataset + the held-constant retrieval)
# ---------------------------------------------------------------------------
def _gold_texts(gold: Sequence[GoldFragment]) -> list[str]:
    """The per-gold body texts (markdown → snippet → title), skipping empties."""
    out: list[str] = []
    for g in gold:
        body = g.markdown or g.snippet or g.title
        if body:
            out.append(body)
    return out


def _build_items_index(
    items: Optional[Sequence[Item]],
    loader,
    data_dir: Optional[PathLike],
) -> dict[str, Item]:
    """Resolve ``item_id`` → :class:`Item` from exactly one of items / loader / data_dir."""
    if items is None:
        if loader is None:
            from bakeoff.dataset import DatasetLoader

            loader = DatasetLoader(Path(data_dir) if data_dir is not None else None)
        items = loader.load_items()
    return {it.item_id: it for it in items}


def _default_fragment_provider(retr) -> FragmentProvider:
    """A :data:`FragmentProvider` that re-queries the held-constant retrieval substrate.

    Uses the event's own stored ``query`` so the retrieval cache key matches Phase
    1 exactly (after a completed run the cache is fully populated → no backend
    call). The dataset has no per-item retrieval filters, so ``filters`` is
    ``None`` — identical to the generation-time call.
    """

    async def provide(event: TrialEvent, item: Optional[Item]) -> Sequence[dict]:
        result = await retr.retrieve(event.query, None)
        return list(result.fragments)

    return provide


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# The Phase-2 pass
# ---------------------------------------------------------------------------
async def run_deferred_judge(
    *,
    # -- stores ------------------------------------------------------------
    outcomes_path: PathLike = config.OUTCOMES_PATH,
    judge_scores_path: PathLike = config.JUDGE_SCORES_PATH,
    # -- dataset (provide exactly one of items / loader / data_dir) --------
    items: Optional[Sequence[Item]] = None,
    loader=None,
    data_dir: Optional[PathLike] = None,
    # -- judge + retrieval -------------------------------------------------
    judge_scorer: Optional[JudgeScorer] = None,
    retr=None,
    fragment_provider: Optional[FragmentProvider] = None,
    # -- sampling ----------------------------------------------------------
    items_per_model: int = config.JUDGE_SAMPLE_ITEMS_PER_MODEL,
    seed: int = DEFAULT_SAMPLE_SEED,
    models: Optional[Sequence[str]] = None,
    # -- concurrency -------------------------------------------------------
    max_concurrency: Optional[int] = None,
    progress: Optional[Callable[[JudgeScoreRecord], None]] = None,
) -> Phase2Result:
    """Run the deferred judge over a stratified sample of the outcomes.

    Reads :data:`OUTCOMES_PATH`, selects ~``items_per_model`` answerability-stratified
    trials per model (see :func:`select_sample`), reconstructs each trial's judge
    inputs from the dataset (ideal text + gold texts) and the held-constant
    retrieval substrate (fragment text, cache-served), runs ``judge_scorer`` on
    each, and appends one :class:`JudgeScoreRecord` per trial to
    :data:`JUDGE_SCORES_PATH`, keyed by ``trial_id``.

    Idempotent / resumable: trials already present in the judge store are skipped,
    so a re-invocation judges only the not-yet-judged sampled trials.

    Defaults wire the real components — the real :class:`JudgeScorer` (resilient
    Bedrock Opus judge, ``k = config.JUDGE_SAMPLES_K``) and a
    :class:`~bakeoff.retrieval_client.RetrievalClient` for fragment reconstruction
    — so a bare call runs the real Phase-2 pass, while tests inject a stub judge
    and a fragment provider to stay fully offline.

    Concurrency: judge ``score`` calls (each blocking on ``k`` Bedrock calls) are
    run off the event loop via :func:`asyncio.to_thread` under a bounded semaphore
    (default :data:`config.CONCURRENCY_CAPS`\\ ``["judge"]``), so the slow,
    TPM-limited judge is rate-bounded without blocking the loop.
    """
    outcomes_path = Path(outcomes_path)
    judge_scores_path = Path(judge_scores_path)

    outcomes = read_events(outcomes_path)
    items_index = _build_items_index(items, loader, data_dir)

    # Resume: which trials are already judged (durable store is the source).
    already = {r.trial_id for r in read_judge_scores(judge_scores_path)}

    sample = select_sample(
        outcomes, items_per_model=items_per_model, seed=seed, models=models
    )
    to_judge = [ev for ev in sample if ev.trial_id not in already]
    skipped = len(sample) - len(to_judge)

    # Resolve the judge + fragment provider (defaults = the real components).
    if judge_scorer is None:
        judge_scorer = JudgeScorer()  # resilient Bedrock Opus judge, k from config
    if fragment_provider is None:
        if retr is None:
            from bakeoff.retrieval_client import RetrievalClient

            retr = RetrievalClient()
        fragment_provider = _default_fragment_provider(retr)

    cap = max_concurrency if max_concurrency is not None else config.CONCURRENCY_CAPS["judge"]
    sem = asyncio.Semaphore(max(1, cap))
    # A single writer lock so the durable append stays one-complete-line-at-a-time
    # even though scoring fans out concurrently.
    write_lock = asyncio.Lock()
    judged_by_model: dict[str, int] = defaultdict(int)
    judged = 0

    async def judge_one(event: TrialEvent) -> None:
        nonlocal judged
        item = items_index.get(event.item_id)
        gold = item.gold if item is not None else []
        wants = item.wants if item is not None else None
        ideal_text = ideal_response_text(gold, wants)
        gold_texts = _gold_texts(gold)
        fragments = list(await fragment_provider(event, item))

        async with sem:
            scores, evidence = await asyncio.to_thread(
                judge_scorer.score_detailed,
                event.answer_text,
                ideal_text=ideal_text,
                fragments=fragments,
                gold_texts=gold_texts,
                momentary_state=event.cohort.momentary_state,
                answerability=event.answerability,
                question=event.query,
            )

        record = JudgeScoreRecord(
            trial_id=event.trial_id,
            model=event.model,
            item_id=event.item_id,
            answerability=event.answerability,
            judge=scores,
            judged_at=_now_iso(),
            evidence=evidence,
            answer_excerpt=(event.answer_text or "")[:600],
            momentary_state=event.cohort.momentary_state,
        )
        async with write_lock:
            append_judge_score(judge_scores_path, record)
            judged += 1
            judged_by_model[event.model] += 1
            if progress is not None:
                progress(record)

    if to_judge:
        await asyncio.gather(*(judge_one(ev) for ev in to_judge))

    return Phase2Result(
        outcomes_seen=len(outcomes),
        sampled=len(sample),
        judged=judged,
        skipped_existing=skipped,
        judge_scores_path=judge_scores_path,
        models=dict(judged_by_model),
    )


# ---------------------------------------------------------------------------
# Summarization for the dashboard's judge view (per-model rollups + examples)
# ---------------------------------------------------------------------------
#: A judge dimension is treated as a "pass" (binary win) at or above this
#: normalized score. The dimensions are already normalized to [0, 1] from the
#: 1-5 rubric ((s-1)/4), so 0.6 corresponds to a rubric score of ~3.4/5 — i.e.
#: "clearly better than the neutral middle". Used only for the binary-outcome
#: rollup the exec view shows alongside the continuous means; the continuous
#: means remain the primary signal.
JUDGE_PASS_THRESHOLD: float = 0.6


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarize_judge_scores(
    records: Sequence[JudgeScoreRecord],
    *,
    examples_per_model: int = 3,
    pass_threshold: float = JUDGE_PASS_THRESHOLD,
) -> dict:
    """Roll the raw judge verdicts up into the shape the dashboard's judge view reads.

    Produces, per model: the count of judged trials, the mean of each rubric
    dimension across its verdicts (the continuous signal), the mean overall judge
    score, a per-dimension **binary pass rate** (fraction of verdicts at or above
    ``pass_threshold`` — the "did it clear the bar" view execs like), an
    answerability breakdown, and a few representative **example verdicts** (with
    the judge's quoted evidence + the graded answer excerpt) so the view can show
    the judge's actual opinions, not just numbers.

    Returns a plain JSON-serializable dict (no dataclasses) so it drops straight
    into a FastAPI response.
    """
    by_model: dict[str, list[JudgeScoreRecord]] = defaultdict(list)
    for r in records:
        by_model[r.model].append(r)

    models_out: list[dict] = []
    for model in sorted(by_model):
        recs = by_model[model]
        dim_means: dict[str, float] = {}
        dim_pass_rates: dict[str, float] = {}
        for dim in JUDGE_DIMENSIONS:
            vals = [getattr(r.judge, dim) for r in recs]
            dim_means[dim] = _mean(vals)
            passes = sum(1 for v in vals if v >= pass_threshold)
            dim_pass_rates[dim] = passes / len(vals) if vals else 0.0
        overall = _mean([dim_means[d] for d in JUDGE_DIMENSIONS])

        answerability_counts: dict[str, int] = defaultdict(int)
        for r in recs:
            answerability_counts[r.answerability] += 1

        # Representative examples: the best and worst by overall judge mean plus a
        # median, so the view can show the judge's range of opinions for the model.
        def _overall(rec: JudgeScoreRecord) -> float:
            return _mean([getattr(rec.judge, d) for d in JUDGE_DIMENSIONS])

        ranked = sorted(recs, key=_overall)
        picks: list[JudgeScoreRecord] = []
        if ranked:
            idxs = {0, len(ranked) - 1, len(ranked) // 2}
            picks = [ranked[i] for i in sorted(idxs)][:examples_per_model]
        examples = [
            {
                "trial_id": r.trial_id,
                "item_id": r.item_id,
                "answerability": r.answerability,
                "momentary_state": r.momentary_state,
                "overall": _overall(r),
                "dimensions": {d: getattr(r.judge, d) for d in JUDGE_DIMENSIONS},
                "dim_sd": dict(r.judge.judge_dim_sd),
                "evidence": dict(r.evidence),
                "answer_excerpt": r.answer_excerpt,
                "judge_model": r.judge.judge_model,
            }
            for r in picks
        ]

        models_out.append(
            {
                "model": model,
                "n_judged": len(recs),
                "overall_mean": overall,
                "dimension_means": dim_means,
                "dimension_pass_rates": dim_pass_rates,
                "answerability_counts": dict(answerability_counts),
                "examples": examples,
            }
        )

    judge_models = sorted({r.judge.judge_model for r in records})
    return {
        "dimensions": list(JUDGE_DIMENSIONS),
        "pass_threshold": pass_threshold,
        "judge_models": judge_models,
        "n_records": len(records),
        "models": models_out,
    }


# ---------------------------------------------------------------------------
# CLI — defaults wire the real components (the operator's Phase-2 command)
# ---------------------------------------------------------------------------
def _build_arg_parser():
    import argparse

    p = argparse.ArgumentParser(
        prog="python -m bakeoff.judge_phase2",
        description=(
            "Phase 2: run the deferred LLM-as-judge over a stratified sample of the "
            "generation outcomes and write verdicts (keyed by trial_id) to the "
            "judge-scores store. Re-invoking resumes (skips already-judged trials)."
        ),
    )
    p.add_argument(
        "--items-per-model", type=int, default=config.JUDGE_SAMPLE_ITEMS_PER_MODEL,
        help=(
            "judged items per model (the sample dial; default "
            f"{config.JUDGE_SAMPLE_ITEMS_PER_MODEL} → ~"
            f"{config.JUDGE_SAMPLE_ITEMS_PER_MODEL * len(config.CANDIDATE_MODELS) * config.JUDGE_SAMPLES_K} "
            "Opus attempts across the roster)"
        ),
    )
    p.add_argument(
        "--seed", type=int, default=DEFAULT_SAMPLE_SEED,
        help=f"seed for the reproducible stratified sample (default {DEFAULT_SAMPLE_SEED})",
    )
    p.add_argument(
        "--data-dir", default=None,
        help="dataset directory (default: config.DATASET_DIR = data/synthetic/)",
    )
    p.add_argument(
        "--max-concurrency", type=int, default=None,
        help=(
            "max concurrent judge score() calls (default "
            f"config.CONCURRENCY_CAPS['judge'] = {config.CONCURRENCY_CAPS['judge']})"
        ),
    )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entrypoint: run the real Phase-2 judge pass and print a short summary."""
    args = _build_arg_parser().parse_args(argv)
    config.ensure_dirs()

    def _progress(record: JudgeScoreRecord) -> None:
        print(f"  judged {record.model:<32} {record.item_id}", flush=True)

    result = asyncio.run(
        run_deferred_judge(
            data_dir=args.data_dir,
            items_per_model=args.items_per_model,
            seed=args.seed,
            max_concurrency=args.max_concurrency,
            progress=_progress,
        )
    )

    print(f"outcomes seen:     {result.outcomes_seen}")
    print(f"sampled trials:    {result.sampled}")
    print(f"judged this run:   {result.judged}")
    print(f"skipped (existing):{result.skipped_existing}")
    print(f"judge scores ->    {result.judge_scores_path}")
    print("per-model judged:")
    for model in sorted(result.models):
        print(f"  {model:<34} {result.models[model]}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
