"""
Trial runner — parallel, bounded, resumable execution (Task 10, Req 7/12.3/13).

This module is the scheduler that turns a :class:`bakeoff.types.SamplingPlan` into
a durable, append-only log of :class:`bakeoff.types.TrialEvent`s — maximally in
parallel, crash-resumable, and resilient to the credential-expiry burst the user
flagged as load-bearing ("everything's 200, then 400 five minutes later: refresh
creds and redo failed attempts"). It is the convergence point of every upstream
task: the retrieval substrate (Task 4), the model adapters (Task 5), the scoring
pipeline (Tasks 6/7), the planner (Task 9), the event log (Task 2), and the shared
resilience helper (Task 5's :mod:`bakeoff.resilience`).

Four public functions implement the design's load-bearing algorithms (design
"Algorithmic pseudocode": ``run_trial`` / ``schedule_run``, plus their preconditions,
postconditions, and loop invariants):

* :func:`run_trial` — execute ONE ``(model, item, rep)`` trial end-to-end
  (retrieve → generate → score) into a fully-scored :class:`TrialEvent`. It is
  **pure with respect to the log** (it never writes; the scheduler owns the
  append) and **never raises**: on any failure it returns an event with ``error``
  set and best-effort partial fields, so the trial is recorded as *attempted*,
  never silently dropped (Req 7.5, design Error Scenario 1). The merged
  :class:`StageTimings` always satisfies
  ``end_to_end_ms == retrieval_total_ms + generation_total_ms`` (Property 5).

* :func:`planned_trials` — the planned trial set as the product over
  ``(pass, model, item, rep)`` per the plan's per-stratum reps, each carrying a
  deterministic :attr:`TrialSpec.trial_id` (Req 7.1). The per-stratum item
  membership is read from the plan's serialized variance-model blob (the planner
  records ``wide_item_ids`` / ``subsample_item_ids`` per stratum), so this needs
  the plan only — not the loaded items.

* :func:`resume_point` — the set of ``trial_id``s already *durable and successful*
  in the log. Errored events do **not** count as done, so a resumed run retries
  them (Req 7.5, design Error Scenario 1); a fully-successful log makes resume a
  no-op (Property 3).

* :func:`schedule_run` — run every planned trial not already done, maximally in
  parallel via ``asyncio`` with **separate bounded semaphores per downstream
  resource** (model / judge / embed / retrieve; AD-3), appending exactly one
  event per completed trial atomically (Req 7.3) and publishing exactly one
  completion signal to the broker per appended event. CPU-bound scoring is
  offloaded off the event loop via :func:`asyncio.to_thread`. The loop invariant
  ``done ∪ in_flight ∪ pending == planned`` (disjoint) holds throughout.

**Credential-expiry resilience (the user's explicit requirement).** Every
downstream call (retrieve, generate, score) is funnelled through
:func:`bakeoff.resilience.call_with_resilience`, so an ``AUTH_EXPIRED`` burst
triggers an *injectable* credential-refresh callback + retry of the affected call
(no real STS needed for tests). On top of that per-call retry, the scheduler runs
a **run-wide auto-pause**: if the downstream error rate crosses a threshold
mid-run, the run auto-pauses (drains in-flight work and returns) rather than
burning every remaining trial as an error (Req 7.6, Req 13.3). After creds are
refreshed the run resumes by simply re-invoking :func:`schedule_run` — only the
missing (errored + never-run) trials execute.

**Control + state for the UI (Task 12).** :class:`RunController` exposes
async-safe ``pause`` / ``resume`` / ``abort`` hooks the FastAPI layer will call,
and a :meth:`RunController.snapshot` of per-model counts (planned / done /
in-flight / errored) plus run status for the live UI.

**The broker seam (Task 12 plugs in here).** A minimal :class:`CompletionBroker`
``Protocol`` (an object with ``publish(event_type: str, payload: dict)``) is
defined here; :func:`schedule_run` accepts any object implementing it, so Task
12's real SSE broker drops in unchanged and tests can pass a fake recording
broker.
"""
from __future__ import annotations

import asyncio
import inspect
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Awaitable,
    Callable,
    Iterator,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)

from bakeoff import config
from bakeoff.eventlog import append_event, read_events
from bakeoff.ids import SCHEMA_VERSION
from bakeoff.ids import trial_id as _trial_id
from bakeoff.resilience import call_with_resilience
from bakeoff.types import (
    COHORT_DIMENSIONS,
    AccuracyScores,
    CohortKey,
    ErrorClass,
    Item,
    JudgeScores,
    ModelResponse,
    QualityScores,
    RetrievalRecord,
    SamplingPlan,
    StageTimings,
    TrialEvent,
    TrialSpec,
)

__all__ = [
    "CompletionBroker",
    "RetrievalLike",
    "ScoringLike",
    "RunHealthError",
    "ModelCounts",
    "RunController",
    "RunStatus",
    "merge_timings",
    "run_trial",
    "planned_trials",
    "resume_point",
    "schedule_run",
    "DEFAULT_ERROR_RATE_THRESHOLD",
    "DEFAULT_ERROR_RATE_MIN_SAMPLE",
]

# ---------------------------------------------------------------------------
# Run-wide auto-pause defaults (Req 7.6 / 13.3). A run that starts erroring
# systemically (credentials rolled over, retrieval backend fell over) should
# auto-pause rather than consume every remaining trial as an error. These are
# defaults; schedule_run accepts overrides.
# ---------------------------------------------------------------------------
#: Fraction of *completed* trials that may be errors before the run auto-pauses.
DEFAULT_ERROR_RATE_THRESHOLD: float = 0.5
#: Minimum completed-trial sample before the error-rate gate can fire (so a
#: single early failure on a tiny run never trips it).
DEFAULT_ERROR_RATE_MIN_SAMPLE: int = 8

# Pass-name → which serialized stratum item-list a trial of that pass draws from.
# Mirrors the planner: WIDE runs every item in the stratum; DEEP/PILOT run the
# stratified subsample. TARGETED (flagged high-variance items) defaults to the
# full list. Kept here so planned_trials needs the plan only, not the planner.
_PASS_ITEM_KEY: dict[str, str] = {
    "wide": "wide_item_ids",
    "deep": "subsample_item_ids",
    "pilot": "subsample_item_ids",
    "targeted": "wide_item_ids",
}


# ---------------------------------------------------------------------------
# Seams: the broker (Task 12) + the duck-typed downstream clients
# ---------------------------------------------------------------------------
@runtime_checkable
class CompletionBroker(Protocol):
    """The minimal completion-signal sink the scheduler publishes to.

    Task 12 owns the real Server-Sent-Events broker; this Protocol is the only
    surface the runner needs, so the real broker plugs in unchanged and tests can
    pass a fake recording broker. ``publish`` is synchronous and MUST be
    non-blocking (the real broker enqueues onto per-subscriber queues); the
    scheduler calls it exactly once per appended event (Req 7.3).
    """

    def publish(self, event_type: str, payload: dict) -> None:
        """Publish one ``event_type`` with a JSON-serializable ``payload``."""
        ...


@runtime_checkable
class RetrievalLike(Protocol):
    """The slice of :class:`bakeoff.retrieval_client.RetrievalClient` the runner uses."""

    async def retrieve(self, query: str, filters: Optional[dict] = ...) -> object: ...

    async def healthz(self) -> bool: ...


@runtime_checkable
class ScoringLike(Protocol):
    """The slice of :class:`bakeoff.scoring.pipeline.ScoringPipeline` the runner uses.

    ``score_trial`` may be sync (the real CPU-bound pipeline) or async (the
    scheduler's gated/offloaded wrapper); :func:`run_trial` awaits it iff it
    returns an awaitable.
    """

    def score_trial(self, item: Item, gold: Sequence, fragments: Sequence[dict],
                    response: ModelResponse) -> object: ...


class RunHealthError(RuntimeError):
    """Raised when the retrieval substrate is unhealthy at run start (Req 2.4).

    The run is gated on ``retr.healthz()``; an unhealthy backend fails the run
    fast with a clear message (design Error Scenario 2) rather than letting every
    trial error one by one.
    """


# ---------------------------------------------------------------------------
# now_iso — provenance timestamps
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (provenance stamp)."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Stratum id (canonical, matches bakeoff.planner._stratum_id) — kept local so
# the runner depends only on the plan object, not on the planner module.
# ---------------------------------------------------------------------------
def _stratum_id(predicate: Mapping[str, str]) -> str:
    """Stable, readable id for a stratum from its predicate (cohort-axis order)."""
    return "|".join(
        f"{axis}={predicate[axis]}" for axis in COHORT_DIMENSIONS if axis in predicate
    )


def _retrieval_query(item: Item) -> str:
    """The query used for retrieval + stamped on the event (focal/turn-1 query).

    Retrieval is the held constant (design AD-2, Property 1): the query must be a
    deterministic function of the item so every rep and every model retrieves the
    identical fragments. Single-turn uses the focal ``query``; multi-turn uses the
    first turn's utterance.
    """
    if item.query:
        return item.query
    if item.turns:
        return item.turns[0].user_utterance
    return ""


def _abstention_fields(answerability: str) -> tuple[Optional[int], Optional[int]]:
    """Return ``(abstention_correct, unwarranted_refusal)`` honoring the schema rule.

    The :class:`TrialEvent` validation rule couples these to ``answerability``
    (``abstention_correct`` iff ``answerability in {none, partial}``;
    ``unwarranted_refusal`` iff ``answerability == full``). Used to build a
    *valid* placeholder quality for an error event so even a failed trial's
    best-effort fields obey the coupling.
    """
    if answerability in ("none", "partial"):
        return 0, None
    if answerability == "full":
        return None, 0
    return None, None


def _placeholder_quality(answerability: str) -> QualityScores:
    """A zeroed-but-schema-valid :class:`QualityScores` for an error event."""
    abstention_correct, unwarranted_refusal = _abstention_fields(answerability)
    accuracy = AccuracyScores(
        precision_at_k=0.0,
        recall_at_k=0.0,
        mrr=0.0,
        ndcg_at_k=0.0,
        grounding_precision=0.0,
        grounding_recall=0.0,
        semantic_similarity=0.0,
        abstention_correct=abstention_correct,
        unwarranted_refusal=unwarranted_refusal,
    )
    judge = JudgeScores(
        faithfulness=0.0,
        correctness=0.0,
        completeness=0.0,
        judge_sample_count=0,
        judge_model=config.JUDGE_MODEL_ID,
        judge_dim_sd={},
    )
    return QualityScores(
        accuracy=accuracy,
        judge=judge,
        composite=0.0,
        composite_weights_version=config.COMPOSITE_WEIGHTS_VERSION,
    )


def merge_timings(
    retrieval_timings: Optional[Mapping[str, float]], response: ModelResponse
) -> StageTimings:
    """Merge the constant retrieval timings with the model's generation timings.

    The retrieval stage figures come verbatim from the ``/retrieve`` ``timings``
    block; the generation figures (TTFT, total) are owned by the candidate. The
    end-to-end figure is defined as the **sum** of the two stage totals, so the
    Property-5 invariant ``end_to_end_ms == retrieval_total_ms +
    generation_total_ms`` holds by construction for every non-error event.

    Robust to a partial/missing retrieval-timings dict: missing stage fields
    default to 0.0, and ``retrieval_total_ms`` falls back to the sum of the four
    stages when no explicit total is present.
    """
    rt = dict(retrieval_timings or {})
    embed = float(rt.get("embed_query_ms", 0.0) or 0.0)
    bm25 = float(rt.get("bm25_vectorize_ms", 0.0) or 0.0)
    hybrid = float(rt.get("hybrid_search_ms", 0.0) or 0.0)
    rerank = float(rt.get("rerank_ms", 0.0) or 0.0)
    if "total_ms" in rt:
        retrieval_total = float(rt["total_ms"] or 0.0)
    elif "retrieval_total_ms" in rt:
        retrieval_total = float(rt["retrieval_total_ms"] or 0.0)
    else:
        retrieval_total = embed + bm25 + hybrid + rerank

    generation_total = float(response.generation_total_ms)
    return StageTimings(
        embed_query_ms=embed,
        bm25_vectorize_ms=bm25,
        hybrid_search_ms=hybrid,
        rerank_ms=rerank,
        retrieval_total_ms=retrieval_total,
        ttft_ms=float(response.ttft_ms),
        generation_total_ms=generation_total,
        end_to_end_ms=retrieval_total + generation_total,  # Property 5
    )


# ---------------------------------------------------------------------------
# run_trial — one trial end-to-end (pure w.r.t. the log; never raises)
# ---------------------------------------------------------------------------
async def run_trial(
    model,
    item: Item,
    rep: int,
    pass_name: str,
    temperature: float,
    retr: RetrievalLike,
    scoring: ScoringLike,
    plan_version: str,
) -> TrialEvent:
    """Run one ``(model, item, rep)`` trial and return a fully-scored event.

    Pipeline: retrieve the constant fragments → generate (capturing TTFT) →
    score → assemble a :class:`TrialEvent`. The function is **pure with respect
    to the log** (it does not write; the scheduler owns the append) and **never
    raises**: on any exception it returns an event with ``error`` set and
    best-effort partial fields (the trial is recorded as attempted, Req 7.5).

    ``scoring.score_trial`` may be sync (the CPU-bound pipeline) or async (the
    scheduler's gated, thread-offloaded wrapper); it is awaited iff it returns an
    awaitable.

    Postconditions (design ``run_trial`` spec):
    * ``error is None`` on success; otherwise ``error`` set + partial fields.
    * ``retrieval.fragment_ids`` is the substrate's result for this item's query
      (identical across all reps/models — Property 1).
    * ``timings.end_to_end_ms == retrieval_total_ms + generation_total_ms``
      (Property 5) for the success path.
    """
    started_at = _now_iso()
    query = _retrieval_query(item)
    answerability = item.answerability or item.cohort.answerability
    filters = item.retrieval_filters or None
    tid = _trial_id(getattr(model, "name", str(model)), item.item_id, rep, pass_name, plan_version)

    # Best-effort partial captures (filled progressively; used iff we error).
    fragment_ids: list[str] = []
    confidence: list[float] = []
    cache_hit = False
    retrieval_timings: dict[str, float] = {}
    answer_text = ""
    token_usage: dict[str, int] = {}
    response: Optional[ModelResponse] = None

    try:
        result = await retr.retrieve(query, filters)
        fragment_ids = list(result.fragment_ids)
        confidence = list(result.confidence)
        cache_hit = bool(result.cache_hit)
        retrieval_timings = dict(result.timings)

        response = await model.generate(item, result.fragments, temperature)
        answer_text = response.text
        token_usage = dict(response.token_usage)

        scored = scoring.score_trial(item, item.gold, result.fragments, response)
        quality: QualityScores = await scored if inspect.isawaitable(scored) else scored

        return TrialEvent(
            trial_id=tid,
            schema_version=SCHEMA_VERSION,
            plan_version=plan_version,
            model=getattr(model, "name", str(model)),
            item_id=item.item_id,
            turn_type=item.turn_type,
            pass_name=pass_name,
            rep=rep,
            temperature=temperature,
            cohort=item.cohort,
            query=query,
            gold_node_ids=list(item.gold_node_ids),
            answerability=answerability,
            retrieval=RetrievalRecord(
                fragment_ids=fragment_ids, confidence=confidence, cache_hit=cache_hit
            ),
            answer_text=answer_text,
            token_usage=token_usage,
            timings=merge_timings(retrieval_timings, response),
            quality=quality,
            started_at=started_at,
            completed_at=_now_iso(),
            error=None,
        )
    except Exception as exc:  # noqa: BLE001 - the trial is recorded as attempted
        # Best-effort partial timings: whatever stages completed before failure.
        # Error events are exempt from the timing identity (validate_event), so
        # this is informational, not load-bearing.
        gen_total = float(response.generation_total_ms) if response is not None else 0.0
        ttft = float(response.ttft_ms) if response is not None else 0.0
        rt = dict(retrieval_timings)
        retrieval_total = float(
            rt.get("total_ms", rt.get("retrieval_total_ms", 0.0)) or 0.0
        )
        partial_timings = StageTimings(
            embed_query_ms=float(rt.get("embed_query_ms", 0.0) or 0.0),
            bm25_vectorize_ms=float(rt.get("bm25_vectorize_ms", 0.0) or 0.0),
            hybrid_search_ms=float(rt.get("hybrid_search_ms", 0.0) or 0.0),
            rerank_ms=float(rt.get("rerank_ms", 0.0) or 0.0),
            retrieval_total_ms=retrieval_total,
            ttft_ms=ttft,
            generation_total_ms=gen_total,
            end_to_end_ms=retrieval_total + gen_total,
        )
        return TrialEvent(
            trial_id=tid,
            schema_version=SCHEMA_VERSION,
            plan_version=plan_version,
            model=getattr(model, "name", str(model)),
            item_id=item.item_id,
            turn_type=item.turn_type,
            pass_name=pass_name,
            rep=rep,
            temperature=temperature,
            cohort=item.cohort,
            query=query,
            gold_node_ids=list(item.gold_node_ids),
            answerability=answerability,
            retrieval=RetrievalRecord(
                fragment_ids=fragment_ids, confidence=confidence, cache_hit=cache_hit
            ),
            answer_text=answer_text,
            token_usage=token_usage,
            timings=partial_timings,
            quality=_placeholder_quality(answerability),
            started_at=started_at,
            completed_at=_now_iso(),
            error=repr(exc),
        )


# ---------------------------------------------------------------------------
# planned_trials — expand the plan into the (pass, model, item, rep) product
# ---------------------------------------------------------------------------
def _items_for_pass(meta: Mapping[str, object], pass_name: str) -> list[str]:
    """Item ids a trial of ``pass_name`` draws from this stratum's serialized meta."""
    key = _PASS_ITEM_KEY.get(pass_name, "wide_item_ids")
    ids = meta.get(key)
    if ids is None:
        ids = meta.get("wide_item_ids") or meta.get("subsample_item_ids") or []
    return [str(i) for i in ids]


def planned_trials(plan: SamplingPlan, models: Sequence) -> Iterator[TrialSpec]:
    """Yield every planned :class:`TrialSpec`: the ``(pass, model, item, rep)`` product.

    Per the plan's per-stratum reps (``StratumPlan.passes``) and the per-stratum
    item membership recorded by the planner in
    ``plan.pilot_variance_model["strata"][stratum_id]`` (``wide_item_ids`` for the
    WIDE pass, ``subsample_item_ids`` for DEEP/PILOT). Each spec carries a
    deterministic :attr:`TrialSpec.trial_id` (Req 7.1); trial ids are unique
    across the plan because strata are a disjoint item cover and ``(pass, rep)``
    is unique within a stratum.

    Deterministic iteration order: strata in plan order, then passes in plan
    order, then models, then items, then reps.
    """
    variance_model = plan.pilot_variance_model or {}
    strata_meta = variance_model.get("strata", {}) if isinstance(variance_model, Mapping) else {}

    for stratum in plan.strata:
        predicate = stratum.cohort_predicate
        sid = _stratum_id(predicate)
        meta = strata_meta.get(sid, {}) if isinstance(strata_meta, Mapping) else {}
        turn_type = predicate.get("turn_type") if isinstance(predicate, Mapping) else None
        for pass_name, reps in stratum.passes.items():
            item_ids = _items_for_pass(meta, pass_name)
            if not item_ids:
                continue
            for model in models:
                model_name = getattr(model, "name", str(model))
                for item_id in item_ids:
                    for rep in range(int(reps)):
                        yield TrialSpec(
                            model=model_name,
                            item_id=item_id,
                            rep=rep,
                            pass_name=pass_name,
                            plan_version=plan.plan_version,
                            temperature=plan.temperature,
                            turn_type=str(turn_type) if turn_type is not None else None,
                        )


# ---------------------------------------------------------------------------
# resume_point — trial_ids already durable AND successful in the log
# ---------------------------------------------------------------------------
def resume_point(events_path) -> set[str]:
    """Return the set of ``trial_id``s already durable **and successful** in the log.

    Only events with ``error is None`` count as done, so an errored trial is
    retried on resume (Req 7.5, design Error Scenario 1) and a fully-successful
    log makes a re-invoked run a no-op (Property 3). A missing log yields the
    empty set (nothing done yet); a crash-truncated final line is tolerated by
    :func:`bakeoff.eventlog.read_events`.
    """
    return {ev.trial_id for ev in read_events(events_path) if ev.error is None}


# ---------------------------------------------------------------------------
# Run state + control (the UI seam — Task 12)
# ---------------------------------------------------------------------------
class RunStatus:
    """Run lifecycle states (string constants for JSON-friendly reporting)."""

    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    ABORTED = "aborted"
    COMPLETED = "completed"


class ModelCounts:
    """Mutable per-model trial counters surfaced to the live UI (Req 10.1)."""

    __slots__ = ("planned", "done", "in_flight", "errored")

    def __init__(self, planned: int = 0, done: int = 0) -> None:
        self.planned = planned
        self.done = done
        self.in_flight = 0
        self.errored = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "planned": self.planned,
            "done": self.done,
            "in_flight": self.in_flight,
            "errored": self.errored,
        }


class RunController:
    """Async-safe control + live state for one run (the FastAPI seam, Task 12).

    Control hooks (``pause`` / ``resume`` / ``abort``) are ordinary methods the
    FastAPI control endpoints call; they toggle ``asyncio`` primitives the worker
    pool observes. ``snapshot`` exposes status + per-model counts for the live UI.

    Two distinct "not running" modes are modeled deliberately:

    * **Manual pause (hold).** :meth:`pause` clears an internal resume gate;
      workers block at :meth:`wait_if_paused` until :meth:`resume` releases them.
      The run stays alive in-process.
    * **Stop (drain-and-exit).** :meth:`abort` and the run-wide auto-pause set a
      stop flag; workers finish in-flight trials and return, so
      :func:`schedule_run` returns promptly with pending trials left unconsumed.
      The actual *resume* of an auto-paused run is re-invoking
      :func:`schedule_run` after the systemic problem (e.g. expired creds) is
      fixed — only the missing trials run (Property 3).

    Counter mutations are safe without a lock: the event loop is single-threaded
    and the worker mutates counters only between ``await`` points.
    """

    def __init__(
        self,
        *,
        auto_pause: bool = True,
        error_rate_threshold: float = DEFAULT_ERROR_RATE_THRESHOLD,
        error_rate_min_sample: int = DEFAULT_ERROR_RATE_MIN_SAMPLE,
    ) -> None:
        self.status: str = RunStatus.IDLE
        self.counts: dict[str, ModelCounts] = {}
        self.auth_refreshes: int = 0
        self.total_done: int = 0
        self.total_errored: int = 0
        self.auto_paused: bool = False

        self.auto_pause = auto_pause
        self.error_rate_threshold = error_rate_threshold
        self.error_rate_min_sample = error_rate_min_sample

        self._resume_event = asyncio.Event()
        self._resume_event.set()  # not paused at start
        self._aborted = False
        self._stopped = False

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        self.status = RunStatus.RUNNING

    @property
    def stopped(self) -> bool:
        """True iff workers should stop pulling new trials (abort or auto-pause)."""
        return self._stopped or self._aborted

    @property
    def aborted(self) -> bool:
        return self._aborted

    # -- control hooks (called by the FastAPI layer) ----------------------
    def pause(self) -> None:
        """Manually pause: workers hold at the gate until :meth:`resume` (Req 10.5)."""
        if not self._aborted:
            self.status = RunStatus.PAUSED
            self._resume_event.clear()

    def resume(self) -> None:
        """Resume from a manual pause / clear a prior auto-pause stop (Req 10.5)."""
        if self._aborted:
            return
        self._stopped = False
        self.auto_paused = False
        self.status = RunStatus.RUNNING
        self._resume_event.set()

    def abort(self) -> None:
        """Abort: stop pulling new trials and unblock any held workers (Req 10.5)."""
        self._aborted = True
        self._stopped = True
        self.status = RunStatus.ABORTED
        self._resume_event.set()  # release any paused waiters so they exit

    # -- worker-facing gate ----------------------------------------------
    async def wait_if_paused(self) -> None:
        """Block while manually paused (returns immediately when running/stopped)."""
        await self._resume_event.wait()

    # -- accounting -------------------------------------------------------
    def ensure_model(self, model: str, *, planned: int = 0, done: int = 0) -> ModelCounts:
        mc = self.counts.get(model)
        if mc is None:
            mc = ModelCounts(planned=planned, done=done)
            self.counts[model] = mc
        return mc

    def note_completion(self, model: str, *, errored: bool) -> None:
        """Record one durable completion and trip the run-wide auto-pause if due."""
        mc = self.ensure_model(model)
        if errored:
            mc.errored += 1
            self.total_errored += 1
        else:
            mc.done += 1
            self.total_done += 1

        if self.auto_pause and not self.stopped:
            completed = self.total_done + self.total_errored
            if (
                completed >= self.error_rate_min_sample
                and self.total_errored / completed > self.error_rate_threshold
            ):
                self._auto_pause()

    def _auto_pause(self) -> None:
        """Run-wide auto-pause: drain in-flight work and exit (Req 7.6 / 13.3)."""
        self._stopped = True
        self.auto_paused = True
        self.status = RunStatus.PAUSED

    def note_auth_refresh(self) -> None:
        """Increment the credential-refresh counter (surfaced for the UI/metrics)."""
        self.auth_refreshes += 1

    def snapshot(self) -> dict:
        """A JSON-serializable view of run status + per-model counts (live UI)."""
        return {
            "status": self.status,
            "auto_paused": self.auto_paused,
            "auth_refreshes": self.auth_refreshes,
            "totals": {
                "done": self.total_done,
                "errored": self.total_errored,
            },
            "models": {m: c.to_dict() for m, c in self.counts.items()},
        }


# ---------------------------------------------------------------------------
# Per-resource gating proxies — apply a bounded semaphore + credential
# resilience around each downstream call WITHOUT run_trial knowing about it
# (run_trial stays pure). This realizes AD-3's "separate semaphore per resource"
# at true per-phase granularity: a trial holds the retrieve slot only while
# retrieving, the model slot only while generating, the judge/embed slots only
# while scoring.
# ---------------------------------------------------------------------------
class _Resilience:
    """Bundle of the credential-refresh + backoff knobs shared by every proxy."""

    def __init__(
        self,
        *,
        refresh_credentials: Optional[Callable[[], object]],
        controller: RunController,
        sleep: Optional[Callable[[float], Awaitable[None]]],
    ) -> None:
        self.refresh_credentials = refresh_credentials
        self.controller = controller
        self.sleep = sleep or asyncio.sleep

    def _on_retry(self, error_class: ErrorClass, exc: BaseException, attempt: int) -> None:
        if error_class == ErrorClass.AUTH_EXPIRED:
            self.controller.note_auth_refresh()

    async def run(self, fn: Callable[[], Awaitable]):
        """Invoke ``fn`` under :func:`call_with_resilience` with the shared knobs."""
        return await call_with_resilience(
            fn,
            refresh_credentials=self.refresh_credentials,
            sleep=self.sleep,
            on_retry=self._on_retry,
        )


class _GatedRetrieval:
    """Gate retrieval behind ``sem['retrieve']`` + credential resilience."""

    def __init__(self, inner: RetrievalLike, sem: asyncio.Semaphore, res: _Resilience) -> None:
        self._inner = inner
        self._sem = sem
        self._res = res

    async def retrieve(self, query: str, filters: Optional[dict] = None):
        async def attempt():
            async with self._sem:
                return await self._inner.retrieve(query, filters)

        return await self._res.run(attempt)


class _GatedModel:
    """Gate one model adapter behind ``sem['model']`` + credential resilience."""

    def __init__(self, inner, sem: asyncio.Semaphore, res: _Resilience) -> None:
        self._inner = inner
        self.name = getattr(inner, "name", str(inner))
        self._sem = sem
        self._res = res

    async def generate(self, item: Item, fragments: Sequence[dict], temperature: float):
        async def attempt():
            async with self._sem:
                return await self._inner.generate(item, fragments, temperature)

        return await self._res.run(attempt)


class _GatedScoring:
    """Gate scoring behind ``sem['judge']`` + ``sem['embed']``, offloaded to a thread.

    CPU-bound scoring (and the real judge's synchronous Bedrock calls) runs off
    the event loop via :func:`asyncio.to_thread` (AD-3); the whole scoring unit is
    wrapped in credential resilience so an ``AUTH_EXPIRED`` raised by the judge
    inside the thread triggers a refresh + retry like any other downstream call.
    """

    def __init__(
        self,
        inner: ScoringLike,
        judge_sem: asyncio.Semaphore,
        embed_sem: asyncio.Semaphore,
        res: _Resilience,
    ) -> None:
        self._inner = inner
        self._judge_sem = judge_sem
        self._embed_sem = embed_sem
        self._res = res

    async def score_trial(self, item, gold, fragments, response, **kwargs):
        async def attempt():
            async with self._judge_sem, self._embed_sem:
                return await asyncio.to_thread(
                    self._inner.score_trial, item, gold, fragments, response, **kwargs
                )

        return await self._res.run(attempt)


# ---------------------------------------------------------------------------
# schedule_run — bounded-concurrency, resumable, resilient, auto-pausing
# ---------------------------------------------------------------------------
async def schedule_run(
    plan: SamplingPlan,
    models: Sequence,
    events_path,
    broker: CompletionBroker,
    *,
    items: Sequence[Item],
    retr: RetrievalLike,
    scoring: ScoringLike,
    controller: Optional[RunController] = None,
    errors_path=None,
    refresh_credentials: Optional[Callable[[], object]] = None,
    concurrency_caps: Optional[Mapping[str, int]] = None,
    max_concurrency: Optional[int] = None,
    auto_pause: bool = True,
    error_rate_threshold: float = DEFAULT_ERROR_RATE_THRESHOLD,
    error_rate_min_sample: int = DEFAULT_ERROR_RATE_MIN_SAMPLE,
    resilience_sleep: Optional[Callable[[float], Awaitable[None]]] = None,
    gate_healthz: bool = True,
) -> RunController:
    """Run every planned trial not already done, maximally in parallel.

    See the module docstring for the full contract. Resolution:

    1. **Gate on health (Req 2.4 / 13.3).** Unless ``gate_healthz`` is False, the
       run is gated on ``retr.healthz()`` and fails fast with :class:`RunHealthError`
       if the substrate is unhealthy.
    2. **Diff against the log (Property 3).** ``pending`` = planned trials whose
       ``trial_id`` is not already durable+successful (``resume_point``). A
       complete log yields no pending trials → zero new work, zero new publishes.
    3. **Bounded parallel execution (AD-3).** A fixed worker pool pulls specs from
       a queue; each downstream call is gated by its own semaphore
       (model/judge/embed/retrieve) and wrapped in credential resilience.
    4. **Atomic append + exactly-once publish (Req 7.3).** Each completed trial
       appends exactly one event (serialized by a lock, fsync'd) and publishes one
       ``trial_completed`` signal to ``broker``.
    5. **Run-wide auto-pause (Req 7.6 / 13.3).** If the error rate crosses the
       threshold, the run drains and returns with ``status == "paused"``.

    Args:
        items: the loaded :class:`Item`s for this run; the scheduler maps
            ``spec.item_id`` → ``Item`` to execute each trial. (The plan records
            only item *ids*; the items themselves are supplied here.)
        retr / scoring: the (real or stub) downstream clients.
        controller: an existing :class:`RunController` (so the FastAPI layer can
            hold a reference and call pause/resume/abort); one is created if None.
        errors_path: optional SEPARATE store for errored trials. When given,
            successful trials append to ``events_path`` (the clean outcomes store)
            and errored trials append to ``errors_path`` (a disposable execution
            log) — so execution failures never pollute the decision data. When
            ``None``, errors fall back to ``events_path`` (legacy single-log
            behavior) so nothing is dropped.
        refresh_credentials: injectable credential-refresh callback fired on an
            ``AUTH_EXPIRED`` retry (no real STS needed in tests).
        concurrency_caps / max_concurrency: per-resource caps (default
            ``config.CONCURRENCY_CAPS``) and an optional hard cap on concurrent
            in-flight trials (default: saturate the resource caps).
        resilience_sleep: injectable async sleep for backoff (tests pass an
            instant sleep).

    Returns:
        The :class:`RunController` (its ``snapshot`` carries the final state).
    """
    caps = dict(concurrency_caps or config.CONCURRENCY_CAPS)
    if controller is None:
        controller = RunController(
            auto_pause=auto_pause,
            error_rate_threshold=error_rate_threshold,
            error_rate_min_sample=error_rate_min_sample,
        )
    else:
        controller.auto_pause = auto_pause
        controller.error_rate_threshold = error_rate_threshold
        controller.error_rate_min_sample = error_rate_min_sample

    # 1. Health gate (fail fast — design Error Scenario 2).
    if gate_healthz:
        healthy = await retr.healthz()
        if not healthy:
            raise RunHealthError(
                "retrieval substrate is unhealthy at run start (GET /healthz did "
                "not report ok); bring the backend up and retry. No trials were run."
            )

    items_by_id: dict[str, Item] = {it.item_id: it for it in items}
    model_by_name = {getattr(m, "name", str(m)): m for m in models}

    # 2. Plan expansion + resume diff (loop invariant: done ∪ pending == planned).
    all_specs = list(planned_trials(plan, models))
    spec_by_id = {s.trial_id: s for s in all_specs}
    done_ids = resume_point(events_path)
    pending = [s for s in all_specs if s.trial_id not in done_ids]

    # Initialize per-model counts: planned (full), done (already durable).
    planned_by_model = Counter(s.model for s in all_specs)
    done_by_model: Counter = Counter(
        spec_by_id[tid].model for tid in done_ids if tid in spec_by_id
    )
    for model_name in planned_by_model:
        controller.ensure_model(
            model_name,
            planned=planned_by_model[model_name],
            done=done_by_model.get(model_name, 0),
        )

    controller.start()

    # Nothing to do → resume is a no-op (Property 3).
    if not pending:
        controller.status = RunStatus.COMPLETED
        return controller

    # 3. Per-resource bounded semaphores (AD-3) + the gating proxies.
    sem = {res: asyncio.Semaphore(max(1, int(cap))) for res, cap in caps.items()}
    for res in ("model", "judge", "embed", "retrieve"):
        sem.setdefault(res, asyncio.Semaphore(1))

    resilience = _Resilience(
        refresh_credentials=refresh_credentials,
        controller=controller,
        sleep=resilience_sleep,
    )
    gated_retr = _GatedRetrieval(retr, sem["retrieve"], resilience)
    gated_scoring = _GatedScoring(scoring, sem["judge"], sem["embed"], resilience)
    gated_models = {
        name: _GatedModel(m, sem["model"], resilience) for name, m in model_by_name.items()
    }

    queue: "asyncio.Queue[TrialSpec]" = asyncio.Queue()
    for spec in pending:
        queue.put_nowait(spec)

    append_lock = asyncio.Lock()

    async def _run_one(spec: TrialSpec) -> None:
        item = items_by_id.get(spec.item_id)
        mc = controller.ensure_model(spec.model)
        if item is None:
            # An unknown item id is a planning/loader bug; record an error event
            # so the trial is not silently dropped (Req 7.5) and the run continues.
            ev = _missing_item_event(spec)
        else:
            mc.in_flight += 1
            try:
                ev = await run_trial(
                    gated_models[spec.model],
                    item,
                    spec.rep,
                    spec.pass_name,
                    spec.temperature,
                    gated_retr,
                    gated_scoring,
                    plan.plan_version,
                )
            finally:
                mc.in_flight -= 1

        # 4. Atomic append (serialized + offloaded) then exactly-once publish.
        # TWO STORES, by design: a SUCCESSFUL trial is decision data → outcomes
        # (events_path); an ERRORED trial is a disposable execution record →
        # errors_path. They must never share a store, so an execution failure can
        # never pollute the numbers we choose a model on. When errors_path is None
        # (legacy/back-compat callers) errors fall back to events_path so nothing
        # is silently dropped. Resume keys only on successful outcomes, so an
        # errored trial is naturally retried on the next run.
        async with append_lock:
            if ev.error is not None and errors_path is not None:
                await asyncio.to_thread(append_event, errors_path, ev)
            else:
                await asyncio.to_thread(append_event, events_path, ev)
        broker.publish("trial_completed", _summarize(ev))
        controller.note_completion(spec.model, errored=ev.error is not None)

    async def _worker() -> None:
        while True:
            if controller.stopped:
                return
            await controller.wait_if_paused()
            if controller.stopped:
                return
            try:
                spec = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await _run_one(spec)
            finally:
                queue.task_done()

    pool_size = _pool_size(max_concurrency, caps, len(pending))
    workers = [asyncio.create_task(_worker()) for _ in range(pool_size)]
    try:
        await asyncio.gather(*workers)
    finally:
        for w in workers:
            if not w.done():
                w.cancel()

    # 5. Final status.
    if controller.aborted:
        controller.status = RunStatus.ABORTED
    elif controller.stopped:
        controller.status = RunStatus.PAUSED  # auto-paused
    else:
        controller.status = RunStatus.COMPLETED
    return controller


def _pool_size(max_concurrency: Optional[int], caps: Mapping[str, int], n_pending: int) -> int:
    """Worker-pool size: enough workers to saturate the resource caps, capped at need.

    The per-resource semaphores enforce the real concurrency limits; the pool just
    needs enough workers that every resource slot can be filled. Bounded by the
    number of pending trials so we never spawn idle workers.
    """
    if max_concurrency is not None:
        return max(1, min(int(max_concurrency), n_pending))
    cap_sum = sum(max(1, int(c)) for c in caps.values()) if caps else 1
    return max(1, min(cap_sum, n_pending))


def _summarize(ev: TrialEvent) -> dict:
    """Compact, JSON-serializable completion payload for the SSE broker (Req 10)."""
    return {
        "trial_id": ev.trial_id,
        "model": ev.model,
        "item_id": ev.item_id,
        "pass": ev.pass_name,
        "rep": ev.rep,
        "answerability": ev.answerability,
        "error": ev.error is not None,
        "composite": ev.quality.composite,
        # Both latency signals stream live: TTFT (time to FIRST token — the
        # responsiveness metric the operator cares most about) and end-to-end
        # (time to FINAL token). The dashboard's fleet lanes + latency views read
        # both off this payload.
        "ttft_ms": ev.timings.ttft_ms,
        "end_to_end_ms": ev.timings.end_to_end_ms,
        "cohort": ev.cohort.to_dict(),
    }


def _missing_item_event(spec: TrialSpec) -> TrialEvent:
    """Build an error event for a planned trial whose item could not be resolved."""
    # answerability "unknown" leaves both abstention_correct and unwarranted_refusal
    # None, which is schema-valid (the coupling only constrains none/partial/full).
    answerability = "unknown"
    cohort = CohortKey(
        geography="unknown",
        proficiency="unknown",
        tone="unknown",
        entry_route="unknown",
        momentary_state="neutral",
        answerability=answerability,
        turn_type=spec.turn_type or "single",
    )
    response = ModelResponse(text="", ttft_ms=0.0, generation_total_ms=0.0)
    return TrialEvent(
        trial_id=spec.trial_id,
        schema_version=SCHEMA_VERSION,
        plan_version=spec.plan_version,
        model=spec.model,
        item_id=spec.item_id,
        turn_type=spec.turn_type or "single",
        pass_name=spec.pass_name,
        rep=spec.rep,
        temperature=spec.temperature,
        cohort=cohort,
        query="",
        gold_node_ids=[],
        answerability=answerability,
        retrieval=RetrievalRecord(fragment_ids=[], confidence=[], cache_hit=False),
        answer_text="",
        token_usage={},
        timings=merge_timings({}, response),
        quality=_placeholder_quality(answerability),
        started_at=_now_iso(),
        completed_at=_now_iso(),
        error=f"item not found for spec {spec.item_id!r} (planning/loader mismatch)",
    )
