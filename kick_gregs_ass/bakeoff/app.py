"""
FastAPI backend for the model-bakeoff-harness (Task 12, design Components 8/9).

This module is the **HTTP seam** between the Python harness and the TypeScript
single-page dashboard (design **AD-4**): the runner, the aggregation engine, and
the materialized exec reports are all *derived from* the append-only event log,
and this app exposes them over a JSON + Server-Sent-Events API. The dashboard is
a separate Vite-built client (Tasks 13/14); this backend serves its built bundle
from ``bakeoff/ui/dist/`` when present and otherwise degrades to a small JSON
stub — it never hard-fails because the frontend has not been built yet.

Why SSE and not WebSockets (design AD-4): the live data flow is one-directional
(server → browser: "a trial completed, here are updated aggregates"); SSE is
simpler, rides plain HTTP, and auto-reconnects. The handful of control actions
(pause / resume / abort) are ordinary POST endpoints, not stream traffic.

================================  SECURITY  ================================
**Loopback-only, no-authentication posture — a conscious, documented choice
(Req 15.1 / 15.2; design "Security Considerations").**

This app binds to **localhost (loopback) only** by default
(:data:`bakeoff.config.UI_HOST` == ``127.0.0.1``). It has **no authentication**.
That is acceptable *only* because it is bound to loopback on the operator's own
machine: it is a throwaway local operator tool, not a network service. The
synthetic dataset carries no real PII, no secrets are written to the event log,
and model/judge outputs are treated as data, never executed.

If this app is **ever** bound to a non-loopback interface, **authentication MUST
be added first** — that is a hard precondition, not a nice-to-have. The
:func:`serve` entrypoint enforces it: it refuses to bind to a non-loopback host
unless the caller explicitly asserts that auth has been added
(``allow_non_loopback=True``), so the no-auth posture cannot silently leak onto a
routable interface.
===========================================================================

Route surface (design Component 8/9):

* ``GET  /api/models``            — run status + per-model progress (RunController.snapshot)
* ``GET  /api/aggregate``         — live aggregates (cheap normal-approx CIs; Req 10.4)
* ``GET  /api/bakeoff/diagnostics`` — decision cockpit evidence from the outcomes log
* ``GET  /api/stream``            — SSE: one ``trial_completed`` per appended event (Req 10.3)
* ``POST /api/control/{action}``  — pause / resume / abort the active run (Req 10.5)
* ``POST /api/run/start``         — kick off a flat fixed-rep run from the browser
* ``POST /api/quality/optimize/start`` — start the closed-loop prompt optimizer (Component 12)
* ``GET  /api/quality/optimize/status`` — optimizer run lifecycle + per-model progress
* ``GET  /api/quality/optimize/history?model=...`` — ordered prompt-version history (Req 8.5)
* ``GET  /exec/aggregate``        — materialized exec report (refuses CI-less numbers; P10)
* ``GET  /exec/reports``          — list available materialized plan reports
* ``GET  /healthz``               — harness health (distinct from the retrieval backend's)
* ``GET  /``                      — the built SPA (if ``ui/dist`` exists) else a JSON stub

The real broker↔runner and controller↔control-endpoint plumbing is wired here
and tested; the long-run orchestration (full operator flow) is Task 15.
"""
from __future__ import annotations

import asyncio
import dataclasses
import ipaddress
import json
import math
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional, Sequence

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import bakeoff.config as config
from bakeoff.aggregate import (
    AggregationEngine,
    AnswerabilityBlendError,
    is_accuracy_metric,
    is_latency_metric,
)
from bakeoff.eventlog import read_events
from bakeoff.sessions import BakeOffSessionManager
from bakeoff.runner import (
    CompletionBroker,
    RunController,
    RunStatus,
    schedule_run,
)
from bakeoff.runner import _summarize as _summarize_event
from bakeoff.scoring.pipeline import compute_composite
from bakeoff.stats import (
    extract_metric_value,
    group_rep_values_by_item,
    normal_approx_ci,
    variance_decomp,
)
from bakeoff.types import COHORT_DIMENSIONS, Aggregate, CI, TrialEvent

__all__ = [
    "SSEBroker",
    "Subscription",
    "AppState",
    "create_app",
    "is_loopback_host",
    "live_aggregates",
    "serve",
    "DEV_CORS_ORIGINS",
    "SSE_HEARTBEAT_SECONDS",
]

# Vite dev-server origins permitted by CORS in development only (Req 10.6 / AD-4).
DEV_CORS_ORIGINS: tuple[str, ...] = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)

# How often an idle SSE stream emits a comment keepalive. Keeps proxies from
# closing the connection and lets the generator notice client disconnects
# promptly (so a disconnected subscriber is unregistered, not leaked).
SSE_HEARTBEAT_SECONDS: float = 15.0

# Default location of the built TypeScript SPA bundle (Tasks 13/14 build here).
DEFAULT_DIST_DIR: Path = Path(__file__).resolve().parent / "ui" / "dist"

#: Control actions the FastAPI layer maps onto RunController hooks (Req 10.5).
_CONTROL_ACTIONS = frozenset({"pause", "resume", "abort"})

# ---------------------------------------------------------------------------
# Eval dashboard (ragas-eval-visualization-dashboard) — defaults
# ---------------------------------------------------------------------------
#: Durable Event_Store for the eval dashboard's ``EvalInstance`` records (design
#: Area B). Kept SEPARATE from the bake-off outcomes / optimizer stores so the
#: eval producer never writes into, and is never read from, the decision data.
DEFAULT_EVAL_EVENTS_PATH: Path = config.BAKEOFF_DIR / "eval_instances.jsonl"

#: Durable per-run store of ragas-metric prompt OVERRIDES for the eval dashboard
#: (design Area D / Req 16). Kept SEPARATE from every other store; a JSON file of
#: ``{metric: {instruction, examples, version}}`` overrides, atomically written.
DEFAULT_EVAL_PROMPTS_PATH: Path = config.BAKEOFF_DIR / "eval_prompts.json"

#: The configured Agent_Under_Test set the eval dashboard compares (design
#: "N >= 3 agents (concretely four: A/B/C/D)"). The agent set is *configuration*
#: (Req 5.3): an unknown agent id in a start request is a clean 422, and the
#: default run compares all of these. No fixed count is assumed (Req 5.4) beyond
#: the multi-agent-comparison floor of three enforced by the start route.
EVAL_AGENTS: tuple[str, ...] = ("agent-a", "agent-b", "agent-c", "agent-d")

#: On-demand combinatorial run capability (Area F / Req 22). These are the latent,
#: rarely-exercised knobs for the user-initiated arbitrary run path; the default
#: surface remains visualization of already-recorded runs (Req 22.8).
#:
#: * The **confirmation threshold** (Req 22.12): when a requested combinatorial
#:   pool's ``|agents| x |corpus sizes| x |queries|`` exceeds this, the start
#:   endpoint refuses to launch without an explicit ``confirm: true`` in the body
#:   (so the UI can require the user to confirm an oversized run). Configurable
#:   per :class:`AppState` instance (overridable in tests / by an operator).
#: * The **bounded queue depth** (Req 22.10/22.11): at most one on-demand run is
#:   active at a time; further on-demand requests are enqueued in a bounded queue
#:   and started only after the active run completes. A request that arrives when
#:   the queue is already full is refused (429) rather than silently dropped.
EVAL_ONDEMAND_COMBINATION_THRESHOLD: int = 256
EVAL_ONDEMAND_QUEUE_MAX: int = 32

#: The SSE event type published exactly once per appended ``EvalInstance`` (one
#: event per durable record). The replay-seed endpoint shapes its rows
#: identically to this event's payload (``EvalInstance.to_dict()``).
EVAL_INSTANCE_EVENT: str = "eval_instance_appended"


# ---------------------------------------------------------------------------
# SSE broker — implements the runner's CompletionBroker Protocol (the seam)
# ---------------------------------------------------------------------------
def _format_sse(event_type: str, payload: dict) -> str:
    """Format one event as an SSE wire message (auto-reconnect-friendly framing).

    The framing carries an ``event:`` name, an ``id:`` (the ``trial_id`` when the
    payload has one, so a reconnecting client can hint ``Last-Event-ID``), and a
    single-line JSON ``data:`` field, terminated by the mandatory blank line.
    """
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    lines = [f"event: {event_type}"]
    event_id = payload.get("trial_id") if isinstance(payload, dict) else None
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"data: {data}")
    return "\n".join(lines) + "\n\n"


class Subscription:
    """One connected SSE consumer's mailbox (an unbounded per-subscriber queue).

    Registration is **synchronous** (:meth:`SSEBroker.open` adds the subscription
    to the broker before any publish can race it), which is what lets a test
    subscribe *before* driving a run and then assert it received exactly one
    message per appended event. The queue is unbounded so :meth:`SSEBroker.publish`
    can stay non-blocking and never drop an event for a slow consumer (delivery is
    exactly-once-per-connected-subscriber; an unbounded queue trades memory for
    that guarantee, acceptable for a local throwaway tool).
    """

    __slots__ = ("_broker", "queue")

    def __init__(self, broker: "SSEBroker") -> None:
        self._broker = broker
        self.queue: "asyncio.Queue[tuple[str, dict]]" = asyncio.Queue()

    def close(self) -> None:
        """Unregister this subscription so future publishes skip it (idempotent)."""
        self._broker._unregister(self)

    async def stream(self, *, heartbeat: float = SSE_HEARTBEAT_SECONDS):
        """Yield SSE-formatted strings as events arrive, with comment keepalives.

        Emits an initial ``": connected"`` comment immediately so the HTTP
        response (and its headers) flush right away, then forwards each published
        event. When idle for ``heartbeat`` seconds it emits a ``": keepalive"``
        comment, which also gives the generator a chance to observe a client
        disconnect and run its ``finally`` cleanup.
        """
        try:
            yield ": connected\n\n"
            while True:
                try:
                    event_type, payload = await asyncio.wait_for(
                        self.queue.get(), timeout=heartbeat
                    )
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _format_sse(event_type, payload)
        finally:
            self.close()


class SSEBroker:
    """Fan-out completion broker implementing :class:`bakeoff.runner.CompletionBroker`.

    The runner publishes exactly one ``trial_completed`` per appended
    :class:`~bakeoff.types.TrialEvent` (Req 7.3); this broker fans each published
    event out to **every currently-connected subscriber exactly once** (Req 10.3)
    by enqueuing it onto each subscriber's own queue.

    :meth:`publish` honors the Protocol's contract: it is **synchronous and
    non-blocking** — it does a single ``put_nowait`` per subscriber onto an
    unbounded queue and returns, never awaiting and never blocking the runner's
    event loop. Subscribers connect via :meth:`open` / :meth:`subscribe`; an event
    is delivered to the set of subscribers *connected at publish time*. There is no
    replay buffer — a late joiner does not receive past events — which is the
    intended "live monitoring" semantics (the durable record is the event log).

    Thread model: ``publish`` and ``subscribe`` are expected to run on the same
    asyncio event loop (the runner publishes from within :func:`schedule_run`,
    which the app drives on its own loop); ``asyncio.Queue`` is not thread-safe, so
    this broker is loop-affine by design.
    """

    def __init__(self) -> None:
        self._subscribers: set[Subscription] = set()

    # -- CompletionBroker.publish (sync, non-blocking; the runner's seam) ---
    def publish(self, event_type: str, payload: dict) -> None:
        """Deliver one event to every currently-connected subscriber exactly once."""
        # Snapshot to a list so a subscriber unregistering mid-iteration (its
        # stream generator hit ``finally``) cannot mutate the set under us.
        for sub in list(self._subscribers):
            sub.queue.put_nowait((event_type, payload))

    # -- subscriber lifecycle ----------------------------------------------
    def open(self) -> Subscription:
        """Register and return a new :class:`Subscription` (synchronous)."""
        sub = Subscription(self)
        self._subscribers.add(sub)
        return sub

    def _unregister(self, sub: Subscription) -> None:
        self._subscribers.discard(sub)

    async def subscribe(self, *, heartbeat: float = SSE_HEARTBEAT_SECONDS):
        """Convenience async generator for the ``/api/stream`` route.

        Opens a subscription (registered synchronously the instant this generator
        is first iterated) and streams SSE messages until the client disconnects.
        """
        sub = self.open()
        async for chunk in sub.stream(heartbeat=heartbeat):
            yield chunk

    @property
    def subscriber_count(self) -> int:
        """Number of currently-connected subscribers (for diagnostics/tests)."""
        return len(self._subscribers)


# ---------------------------------------------------------------------------
# Loopback enforcement (Req 15.1 / 15.2)
# ---------------------------------------------------------------------------
def is_loopback_host(host: str) -> bool:
    """True iff ``host`` is a loopback address/name (the no-auth precondition).

    ``localhost`` and any address in a loopback range (``127.0.0.0/8``, ``::1``)
    are loopback. Anything else is routable and therefore requires auth before
    binding (Req 15.2).
    """
    if host in ("localhost", "localhost."):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# App state — the live run wiring (broker <-> runner, controller <-> endpoints)
# ---------------------------------------------------------------------------
class JudgeStatus:
    """Phase-2 judge lifecycle states (JSON-friendly string constants)."""

    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class OptimizerStatus:
    """Closed-loop optimizer run lifecycle states (JSON-friendly string constants).

    The optimizer (design Component 10 ``PerModelOrchestrator``) runs as a background
    task with its own lifecycle, separate from both the generation run and the Phase-2
    judge. The dashboard's Quality_Tab polls ``GET /api/quality/optimize/status`` to see
    which state the loop is in; the durable per-model progress (iterations, champion
    scores, convergence) is reconstructed from the append-only optimizer stores.
    """

    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class EvalStatus:
    """Eval-dashboard run lifecycle states (JSON-friendly string constants).

    The eval run (design Area B ``Experiment_Runner``) executes as a background
    task with its OWN lifecycle, separate from the bake-off run, the Phase-2
    judge, and both optimizer loops. The dashboard's eval tabs poll
    ``GET /api/eval/status`` for this lifecycle; the durable per-view state
    (agents, sessions, corpus sizes, instances, rollups, sweep progress) is
    reconstructed from the append-only :class:`~bakeoff.eval.event_store.EvalEventStore`
    so a reload never blanks the surface.
    """

    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class _PublishingEvalStore:
    """An :class:`EvalEventStore` that also publishes one SSE event per append.

    This is the seam that makes "**exactly one** ``eval_instance_appended`` event
    per appended record" a structural guarantee rather than a convention: every
    record the Metric_Engine / Experiment_Runner produces flows through a single
    :meth:`append`, and that method appends durably *and then* publishes exactly
    one event carrying the record's :meth:`EvalInstance.to_dict` payload — the
    same shape the replay-seed endpoint emits.

    The Experiment_Runner is synchronous and is driven on a worker thread
    (``asyncio.to_thread``) so a gated/slow run never blocks the event loop. The
    broker's queues are :class:`asyncio.Queue` (loop-affine, not thread-safe), so
    the publish is marshalled back onto the loop via ``call_soon_threadsafe``.
    Composition (not subclassing) keeps the durable-store contract untouched.
    """

    def __init__(self, store, broker: "SSEBroker", loop: "asyncio.AbstractEventLoop") -> None:
        self._store = store
        self._broker = broker
        self._loop = loop

    def append(self, instance) -> None:
        # 1) durable append first (the source of truth), then 2) publish exactly
        #    one delta. If the append raised, no event is published.
        self._store.append(instance)
        payload = instance.to_dict()
        self._loop.call_soon_threadsafe(
            self._broker.publish, EVAL_INSTANCE_EVENT, payload
        )

    # -- pass-through reads (the durable-backfill authority) ---------------
    def read_all(self):
        return self._store.read_all()

    def reconstruct(self):
        return self._store.reconstruct()

    def read_recent(self, limit: int):
        return self._store.read_recent(limit)

    @property
    def path(self):
        return self._store.path


class AppState:
    """Mutable app-scoped state holding the current run's controller + broker.

    A single :class:`AppState` lives on ``app.state.bakeoff`` for the app's
    lifetime. It is the rendezvous between the FastAPI layer and a live run: the
    control endpoints call :attr:`controller`'s hooks, the ``/api/stream`` route
    subscribes to :attr:`broker`, and :meth:`start_run` launches a real
    :func:`bakeoff.runner.schedule_run` with :attr:`broker` plugged in as its
    :class:`~bakeoff.runner.CompletionBroker` so the live UI sees real events.

    ``events_path`` / ``reports_dir`` are configurable (defaulting to the
    canonical :mod:`bakeoff.config` paths) so the app is fully testable against a
    temp log without touching real harness output.
    """

    def __init__(
        self,
        *,
        broker: Optional[SSEBroker] = None,
        controller: Optional[RunController] = None,
        events_path: Path = config.TRIAL_EVENTS_PATH,
        reports_dir: Path = config.REPORTS_DIR,
        dist_dir: Path = DEFAULT_DIST_DIR,
        host: str = config.UI_HOST,
        port: int = config.UI_PORT,
        judge_scores_path: Path = config.JUDGE_SCORES_PATH,
        session_manager: Optional[BakeOffSessionManager] = None,
        eval_events_path: Path = DEFAULT_EVAL_EVENTS_PATH,
        eval_prompts_path: Path = DEFAULT_EVAL_PROMPTS_PATH,
    ) -> None:
        self.broker = broker if broker is not None else SSEBroker()
        self.session_manager = session_manager or BakeOffSessionManager(
            legacy_outcomes_path=Path(events_path),
            legacy_reports_dir=Path(reports_dir),
            legacy_judge_scores_path=Path(judge_scores_path),
        )
        active_bakeoff_session = self.session_manager.active()
        self.events_path = active_bakeoff_session.outcomes_path
        self.run_errors_path = active_bakeoff_session.run_errors_path
        self.reports_dir = active_bakeoff_session.reports_dir
        self.dist_dir = Path(dist_dir)
        self.host = host
        self.port = port
        self.judge_scores_path = active_bakeoff_session.judge_scores_path
        self.controller = controller
        self._run_task: Optional[asyncio.Task] = None
        # -- Phase-2 deferred-judge state (separate lifecycle from the run) ----
        # The judge runs AFTER a generation run completes (auto-chained) or on
        # demand (the judge "re-run" button). Its progress/result is tracked here
        # independently of the run controller so the dashboard's judge view can
        # poll it without touching run state.
        self.judge_status: str = JudgeStatus.IDLE
        self.judge_progress: dict = {"judged": 0, "sampled": 0, "skipped_existing": 0}
        self.judge_summary: Optional[dict] = None
        self.judge_error: Optional[str] = None
        self.judge_started_at: Optional[str] = None
        self.judge_finished_at: Optional[str] = None
        self._judge_task: Optional[asyncio.Task] = None
        #: auto-chain Phase-2 judging when a generation run completes cleanly.
        self.auto_judge: bool = True
        # -- Closed-loop optimizer state (separate lifecycle from run + judge) ----
        # The optimizer (design Component 10) runs the champion/challenger loop as
        # its own background task and streams ``optimizer_*`` events over the SAME
        # broker the bake-off uses (Req 9.7 — new event TYPES only; the bake-off's
        # ``trial_completed`` / ``judge_*`` streaming is untouched). Its lifecycle is
        # tracked here independently of the run/judge so the Quality_Tab can poll it.
        # The durable per-model progress (iterations, champion scores, convergence)
        # is reconstructed from the append-only optimizer stores, not held in memory.
        self.optimizer_status: str = OptimizerStatus.IDLE
        self.optimizer_error: Optional[str] = None
        self.optimizer_started_at: Optional[str] = None
        self.optimizer_finished_at: Optional[str] = None
        #: the launch request the operator started the loop with (backend, models,
        #: threshold, stop_limit, reps overrides, retrieval backend, force) — echoed
        #: back on the status snapshot so the view can show what is running.
        self.optimizer_request: Optional[dict] = None
        self._optimizer_task: Optional[asyncio.Task] = None
        # -- Optimizer v2 (island-tournament) state — its OWN broker + lifecycle ----
        # v2 gets a DEDICATED SSEBroker so its island/tournament/migration events
        # never share the bake-off ``/api/stream`` (hard constraint). Lifecycle is
        # independent of both the bake-off run and the v1 optimizer; the existing
        # ``view_registry`` is reused (v2's concurrency gate already consults it).
        self.optimizer_v2_broker = SSEBroker()
        self.optimizer_v2_status: str = OptimizerStatus.IDLE
        self.optimizer_v2_error: Optional[str] = None
        self.optimizer_v2_started_at: Optional[str] = None
        self.optimizer_v2_finished_at: Optional[str] = None
        self.optimizer_v2_request: Optional[dict] = None
        self._optimizer_v2_task: Optional[asyncio.Task] = None
        # -- Optimizer V3 (hardened, LIVE-ONLY; bakeoff/quality/optimizer/v3/) -----
        # Same dedicated-broker discipline as v2 — v3's island/tournament/skip/death
        # events never share any other stream. Durable state lives in its OWN
        # ``QUALITY_OPT_V3_*`` files, so v2 and v3 runs never touch each other's data.
        self.optimizer_v3_broker = SSEBroker()
        self.optimizer_v3_status: str = OptimizerStatus.IDLE
        self.optimizer_v3_error: Optional[str] = None
        self.optimizer_v3_started_at: Optional[str] = None
        self.optimizer_v3_finished_at: Optional[str] = None
        self.optimizer_v3_request: Optional[dict] = None
        self._optimizer_v3_task: Optional[asyncio.Task] = None
        # -- Prompt Bench (fixed A/B/C/D prompt leaderboard) — its OWN broker + ---
        # lifecycle + durable stores, and its OWN Bedrock account (promptbench
        # profile) + semaphores, so it runs fully independently of optimizer v3.
        self.promptbench_broker = SSEBroker()
        self.promptbench_status: str = OptimizerStatus.IDLE
        self.promptbench_error: Optional[str] = None
        self.promptbench_started_at: Optional[str] = None
        self.promptbench_finished_at: Optional[str] = None
        self._promptbench_task: Optional[asyncio.Task] = None
        #: the ViewRegistry the PerModelOrchestrator's concurrency gate consults
        #: (Req 1.11 / 9.8). The SSE layer marks a model viewable while its
        #: Per_Model_View subscription is open and clears it on close. Built lazily
        #: (it lives in the optimizer package) so importing this module stays light.
        self._view_registry = None
        # -- Eval dashboard state — its OWN dedicated broker + lifecycle --------
        # The eval dashboard (ragas-eval-visualization-dashboard) gets a DEDICATED
        # SSEBroker so its ``eval_instance_appended`` / ``eval_status`` events NEVER
        # share the bake-off ``/api/stream`` OR either optimizer stream (hard
        # constraint, mirroring the optimizer-v2 dedicated-broker discipline). Its
        # lifecycle is independent of every other feature; the durable per-view
        # state is reconstructed from the append-only EvalEventStore so a reload
        # never blanks the surface. The run-start path uses an OFFLINE producer by
        # default (offline RagasAdapter + injected offline retrieval/agent
        # providers) so a started run is network-free and needs no AWS.
        self.eval_broker = SSEBroker()
        self.eval_status: str = EvalStatus.IDLE
        self.eval_error: Optional[str] = None
        self.eval_started_at: Optional[str] = None
        self.eval_finished_at: Optional[str] = None
        #: the launch request the run was started with (agents, metrics, corpus
        #: sizes, query count) — echoed back on the status snapshot.
        self.eval_request: Optional[dict] = None
        self.eval_events_path = Path(eval_events_path)
        self._eval_task: Optional[asyncio.Task] = None
        #: the per-run ragas-metric prompt-override store (Req 16). A SINGLE
        #: instance shared by the GET/PUT prompt routes and every eval run, so an
        #: override PUT before/between runs is the prompt the next-scored instances
        #: use, while previously recorded values stay untouched (Req 16.5). Built
        #: lazily so importing this module stays light and the path is overridable
        #: in tests; persisted atomically to ``eval_prompts_path``.
        self.eval_prompts_path = Path(eval_prompts_path)
        self._eval_prompt_store = None
        #: injectable offline producer seams (default offline closures are built in
        #: :meth:`_build_eval_runner` when these are ``None``). Tests override these
        #: to inject a gated/offline producer; production leaves them ``None`` so the
        #: network-free defaults are used. ``(query, corpus_size) -> RetrievalResult``
        #: and ``(agent_id, query, RetrievalResult) -> AgentAnswer`` respectively, plus
        #: an optional ``(corpus_size) -> handle`` preparer for the sweep path.
        self.eval_retrieval_provider = None
        self.eval_agent_provider = None
        self.eval_corpus_preparer = None
        #: On-demand combinatorial run capability (Area F / Req 22). At most one
        #: on-demand run is active at a time (Req 22.10); requests that arrive
        #: while a run is active are enqueued in this BOUNDED FIFO queue and
        #: started only after the active run completes (Req 22.11). Each queued
        #: item is the resolved launch kwargs for :meth:`_spawn_eval_run`. The
        #: depth bound (:attr:`eval_queue_max`) and the combination-confirmation
        #: threshold (:attr:`eval_ondemand_threshold`, Req 22.12) are per-instance
        #: so an operator / test can tune them without a code change.
        self.eval_queue: "deque[dict]" = deque()
        self.eval_queue_max: int = EVAL_ONDEMAND_QUEUE_MAX
        self.eval_ondemand_threshold: int = EVAL_ONDEMAND_COMBINATION_THRESHOLD
        # --- REAL eval run (prompt files × queries.jsonl over the LIVE stack) -----
        # Distinct from the synthetic on-demand path above: this runs real AOSS +
        # model + Opus judge and writes EvalInstance records the 3D/2D tabs read.
        # One real run at a time; cooperative stop via the flag.
        self.eval_real_status: str = EvalStatus.IDLE
        self.eval_real_error: Optional[str] = None
        self.eval_real_summary: Optional[dict] = None
        self.eval_real_progress: Optional[dict] = None
        self._eval_real_task: Optional[asyncio.Task] = None
        self._eval_real_stop: bool = False

    # -- REAL eval run lifecycle ------------------------------------------------
    def real_eval_snapshot(self) -> dict:
        """Status of the real (live-stack) eval run for the Metrics tab."""
        return {
            "status": self.eval_real_status,
            "error": self.eval_real_error,
            "summary": self.eval_real_summary,
            "progress": self.eval_real_progress,
        }

    def start_real_eval_run(self, *, query_count: int, prompt_dir: Optional[str] = None) -> bool:
        """Launch the real eval as a background task. False if one is already running."""
        if self.eval_real_status == EvalStatus.RUNNING:
            return False
        from pathlib import Path as _Path

        from bakeoff.eval.event_store import EvalEventStore
        from bakeoff.eval.real_run import run_real_eval

        self.eval_real_status = EvalStatus.RUNNING
        self.eval_real_error = None
        self.eval_real_summary = None
        self.eval_real_progress = {"done": 0, "total": 0}
        self._eval_real_stop = False
        loop = asyncio.get_event_loop()
        publishing = _PublishingEvalStore(
            EvalEventStore(self.eval_events_path), self.eval_broker, loop
        )

        def _on_progress(p) -> None:
            self.eval_real_progress = {
                "series": p.series, "done": p.done, "total": p.total,
                "last_quality": p.last_quality, "last_latency_ms": p.last_latency_ms,
            }
            loop.call_soon_threadsafe(
                self.eval_broker.publish, "eval_real_progress", dict(self.eval_real_progress)
            )

        async def _run() -> None:
            try:
                summary = await run_real_eval(
                    query_count=query_count,
                    prompt_dir=_Path(prompt_dir) if prompt_dir else config.REPO_ROOT / "data" / "prompts",
                    store=publishing,
                    # Resume reads the SAME file the publishing store writes, so it skips
                    # (prompt, query) pairs already recorded and only runs the remainder.
                    store_path=self.eval_events_path,
                    on_progress=_on_progress,
                    should_stop=lambda: self._eval_real_stop,
                )
                self.eval_real_summary = summary
                self.eval_real_status = EvalStatus.COMPLETED
            except Exception as exc:  # noqa: BLE001 - surface, never crash the loop
                self.eval_real_error = repr(exc)
                self.eval_real_status = EvalStatus.FAILED
            finally:
                self.eval_broker.publish("eval_real_status", self.real_eval_snapshot())

        self._eval_real_task = asyncio.create_task(_run())
        return True

    def stop_real_eval_run(self) -> bool:
        """Cooperatively stop a running real eval. False if nothing is running.

        Sets the stop flag the runner polls before each execution: in-flight executions
        finish, queued ones skip, and the run ends COMPLETED with whatever it recorded —
        so a subsequent Start RESUMES (skips the recorded pairs, runs the remainder).
        """
        if self.eval_real_status != EvalStatus.RUNNING:
            return False
        self._eval_real_stop = True
        return True

    def wipe_eval_data(self) -> int:
        """Truncate the active eval store (the metric data the dashboard reads).

        Returns the number of records discarded. Refuses while a real run is active.
        """
        if self.eval_real_status == EvalStatus.RUNNING:
            raise RuntimeError("an eval run is in progress; stop it before wiping")
        path = self.eval_events_path
        discarded = 0
        try:
            discarded = sum(1 for _ in path.open()) if path.exists() else 0
        except OSError:
            discarded = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        self.eval_real_summary = None
        self.eval_real_progress = None
        self.eval_broker.publish("eval_wiped", {"discarded": discarded})
        return discarded

    def idle_snapshot(self) -> dict:
        """The snapshot shape returned by ``/api/models`` when no run is active."""
        return {
            "status": RunStatus.IDLE,
            "auto_paused": False,
            "auth_refreshes": 0,
            "totals": {"done": 0, "errored": 0},
            "models": {},
        }

    def bakeoff_session_snapshot(self) -> dict:
        """Active Bake-Off session + registry for the session manager UI."""
        return self.session_manager.snapshot()

    def set_active_bakeoff_session(self, session_id: str) -> dict:
        """Switch the active Bake-Off session and reset run/judge lifecycle state."""
        if self.controller is not None and (
            self.controller.status == RunStatus.RUNNING
            or (self.controller.status == RunStatus.PAUSED and not self.controller.auto_paused)
        ):
            raise RuntimeError("a run is already active")
        if self.judge_status == JudgeStatus.RUNNING:
            raise RuntimeError("judging already in progress")

        active_session = self.session_manager.activate(session_id)
        self.events_path = active_session.outcomes_path
        self.run_errors_path = active_session.run_errors_path
        self.reports_dir = active_session.reports_dir
        self.judge_scores_path = active_session.judge_scores_path
        self.controller = None
        self._run_task = None
        self.judge_status = JudgeStatus.IDLE
        self.judge_progress = {"judged": 0, "sampled": 0, "skipped_existing": 0}
        self.judge_summary = None
        self.judge_error = None
        self.judge_started_at = None
        self.judge_finished_at = None
        self._judge_task = None
        self.broker.publish("bakeoff_session_changed", self.bakeoff_session_snapshot())
        return self.bakeoff_session_snapshot()

    def snapshot(self) -> dict:
        """Run status + per-model progress, or the idle snapshot if no run yet."""
        if self.controller is None:
            return self.idle_snapshot()
        return self.controller.snapshot()

    async def start_run(
        self,
        plan,
        models: Sequence,
        *,
        items: Sequence,
        retr,
        scoring,
        errors_path=None,
        judge_scores_path=None,
        auto_judge: Optional[bool] = None,
        **schedule_kwargs,
    ) -> RunController:
        """Start a run as a background task with :attr:`broker` plugged in.

        Thin on purpose (the full operator flow is Task 15): it constructs a fresh
        :class:`~bakeoff.runner.RunController`, stores it on :attr:`controller` so
        the control endpoints can drive it, and launches
        :func:`bakeoff.runner.schedule_run` as an ``asyncio`` task with the live
        :class:`SSEBroker` as the completion broker — so every appended event is
        streamed to connected dashboards in real time.

        ``errors_path`` (when given) is the SEPARATE disposable store errored
        trials append to, so execution failures never pollute the clean outcomes
        store at :attr:`events_path` (defaults to the active session's
        ``run_errors.jsonl``).

        **Auto-chain to Phase 2.** When the generation run finishes *cleanly*
        (status ``completed``, not aborted/auto-paused) and :attr:`auto_judge` is
        on, the deferred LLM-as-judge pass is kicked off automatically over the
        fresh outcomes — so an unattended overnight run produces both the
        candidate data AND the judge verdicts without a human pressing a second
        button. The judge runs on the SAME event loop as a follow-on task; it
        reads the clean outcomes store and writes to ``judge_scores_path``.
        """
        controller = RunController()
        self.controller = controller
        errors = errors_path if errors_path is not None else self.run_errors_path
        judge_path = (
            judge_scores_path if judge_scores_path is not None else self.judge_scores_path
        )
        if auto_judge is not None:
            self.auto_judge = auto_judge

        async def _run_then_maybe_judge() -> None:
            await schedule_run(
                plan,
                models,
                self.events_path,
                self.broker,
                items=items,
                retr=retr,
                scoring=scoring,
                controller=controller,
                errors_path=errors,
                **schedule_kwargs,
            )
            # Auto-chain Phase 2 ONLY on a clean completion: an aborted or
            # auto-paused run is not a finished dataset, so judging it would grade
            # a partial run. The judge never blocks or corrupts the outcomes — it
            # only reads them and writes its own separate store.
            if (
                self.auto_judge
                and controller.status == RunStatus.COMPLETED
                and self.judge_status != JudgeStatus.RUNNING
            ):
                await self._run_judge(judge_scores_path=judge_path)

        self._run_task = asyncio.create_task(_run_then_maybe_judge())
        return controller

    # -- Phase-2 deferred judge (auto-chained or on-demand) ----------------
    def judge_snapshot(self) -> dict:
        """JSON-serializable view of the Phase-2 judge lifecycle for the UI."""
        return {
            "status": self.judge_status,
            "progress": dict(self.judge_progress),
            "error": self.judge_error,
            "started_at": self.judge_started_at,
            "finished_at": self.judge_finished_at,
            "has_summary": self.judge_summary is not None,
        }

    async def start_judge(
        self,
        *,
        items_per_model: Optional[int] = None,
        judge_scores_path=None,
    ) -> bool:
        """Launch the Phase-2 judge as a background task (the re-run button).

        Returns ``True`` if a pass was launched, ``False`` if one is already
        running (the caller maps that to a 409). The judge reads the clean
        outcomes store and appends verdicts to ``judge_scores_path``; it is
        resumable, so a re-run only judges not-yet-judged sampled trials.
        """
        if self.judge_status == JudgeStatus.RUNNING:
            return False
        judge_path = (
            judge_scores_path if judge_scores_path is not None else self.judge_scores_path
        )
        # Flip to RUNNING synchronously (before yielding to the task) so the
        # snapshot the caller returns is accurate immediately and a second start
        # racing in cannot slip past the guard before the task body runs.
        self.judge_status = JudgeStatus.RUNNING
        self._judge_task = asyncio.create_task(
            self._run_judge(items_per_model=items_per_model, judge_scores_path=judge_path)
        )
        return True

    async def _run_judge(
        self,
        *,
        items_per_model: Optional[int] = None,
        judge_scores_path=None,
    ) -> None:
        """Run the deferred judge pass, tracking lifecycle state for the UI.

        Imports :mod:`bakeoff.judge_phase2` lazily (it pulls in the scoring stack)
        so importing :mod:`bakeoff.app` stays light. Never raises: a judge failure
        is recorded on :attr:`judge_error` with ``status == "failed"`` and never
        touches the outcomes store, honoring the "the grader can never sabotage the
        decision data" guarantee.
        """
        from datetime import datetime, timezone

        from bakeoff.judge_phase2 import (
            read_judge_scores,
            run_deferred_judge,
            summarize_judge_scores,
        )

        judge_path = (
            judge_scores_path if judge_scores_path is not None else self.judge_scores_path
        )
        self.judge_status = JudgeStatus.RUNNING
        self.judge_error = None
        self.judge_started_at = datetime.now(timezone.utc).isoformat()
        self.judge_finished_at = None
        self.judge_progress = {"judged": 0, "sampled": 0, "skipped_existing": 0}

        def _on_progress(_record) -> None:
            self.judge_progress["judged"] = self.judge_progress.get("judged", 0) + 1
            self.broker.publish("judge_progress", dict(self.judge_progress))

        try:
            kwargs: dict = {
                "outcomes_path": self.events_path,
                "judge_scores_path": judge_path,
                "data_dir": config.DATASET_DIR,
                "progress": _on_progress,
            }
            if items_per_model is not None:
                kwargs["items_per_model"] = items_per_model
            result = await run_deferred_judge(**kwargs)
            self.judge_progress = {
                "judged": result.judged,
                "sampled": result.sampled,
                "skipped_existing": result.skipped_existing,
            }
            # Roll the full judge store (this pass + any prior) up for the UI.
            self.judge_summary = summarize_judge_scores(read_judge_scores(judge_path))
            self.judge_status = JudgeStatus.COMPLETED
        except Exception as exc:  # noqa: BLE001 - the judge must never crash the app
            self.judge_error = repr(exc)
            self.judge_status = JudgeStatus.FAILED
        finally:
            self.judge_finished_at = datetime.now(timezone.utc).isoformat()
            self.broker.publish("judge_status", self.judge_snapshot())

    # -- Closed-loop optimizer (background champion/challenger loop) --------
    @property
    def view_registry(self):
        """The :class:`ViewRegistry` the optimizer's concurrency gate consults.

        Built lazily (and memoized) so importing :mod:`bakeoff.app` never pulls in
        the optimizer package. The SSE layer marks a model viewable while its
        Per_Model_View subscription is open and clears it on close (Req 1.11 / 9.8),
        and :class:`~bakeoff.quality.optimizer.orchestrator.PerModelOrchestrator`
        reads it to decide concurrent-vs-sequential scheduling.
        """
        if self._view_registry is None:
            from bakeoff.quality.optimizer.orchestrator import ViewRegistry

            self._view_registry = ViewRegistry()
        return self._view_registry

    def optimizer_snapshot(self) -> dict:
        """JSON-serializable view of the optimizer lifecycle + per-model progress.

        The lifecycle (``idle``/``running``/``completed``/``failed``) and the launch
        request are held in memory; the durable per-model progress (latest iteration
        index, phase, champion triad + CI, convergence state) is reconstructed from
        the append-only optimizer stores so a page reload reflects what is actually on
        disk. Empty-but-well-formed before any optimizer run exists.

        v2 additions: per-island progress and per-tournament-round summaries are
        reconstructed durably from the store (so a reload never blanks the surface).
        """
        models: list[str] = []
        if isinstance(self.optimizer_request, dict):
            req_models = self.optimizer_request.get("models")
            if isinstance(req_models, (list, tuple)):
                models = [str(m) for m in req_models]
        if not models:
            models = list(config.QUALITY_MODELS.keys())

        per_model: dict[str, dict] = {}
        try:
            from bakeoff.quality.optimizer.store import OptimizerStore

            store = OptimizerStore()
            for model in models:
                history = store.iteration_history(model)
                viewable = self.view_registry.has_active_view(model)
                if history:
                    last = history[-1]
                    per_model[model] = {
                        "phase": last.phase,
                        "iteration_index": last.iteration_index,
                        "champion_score": last.champion_score,
                        "champion_ci_half_width": last.champion_ci_half_width,
                        "challenger_score": last.challenger_score,
                        "promoted": last.promoted,
                        "consecutive_non_improving": last.consecutive_non_improving,
                        "converged": last.converged,
                        "stop_reason": last.stop_reason,
                        "iterations": len(history),
                        "viewable": viewable,
                    }
                else:
                    per_model[model] = {
                        "phase": None,
                        "iteration_index": None,
                        "iterations": 0,
                        "viewable": viewable,
                    }

                # -- v2 durable backfill: per-island progress ----
                island_groups = store.iteration_history_by_island(model)
                islands: list[dict] = []
                for (_, island_id), recs in sorted(island_groups.items(), key=lambda kv: (kv[0][1] is None, kv[0][1])):
                    if not recs:
                        continue
                    last_rec = recs[-1]
                    islands.append({
                        "island_id": island_id,
                        "rung_index": last_rec.rung_index,
                        "champion_score": last_rec.champion_score,
                        "champion_ci_half_width": last_rec.champion_ci_half_width,
                        "state": "converged" if last_rec.converged else "iterating",
                    })
                per_model[model]["islands"] = islands

                # -- v2 durable backfill: per-tournament-round summaries ----
                tourn_groups = store.iteration_history_by_tournament_round(model)
                rounds: list[dict] = []
                for rnd, recs in sorted(
                    ((k, v) for k, v in tourn_groups.items() if k is not None),
                    key=lambda kv: kv[0],
                ):
                    if not recs:
                        continue
                    # Each tournament round has records for the two island champions.
                    # The winner is the one that was promoted.
                    scores: list[dict] = []
                    winner: Optional[int] = None
                    shared_rung: Optional[int] = None
                    for r in recs:
                        scores.append({
                            "island_id": r.island_id,
                            "champion_score": r.champion_score,
                            "champion_ci_half_width": r.champion_ci_half_width,
                        })
                        if r.promoted:
                            winner = r.island_id
                        if r.rung_index is not None:
                            shared_rung = r.rung_index
                    rounds.append({
                        "round": rnd,
                        "scores": scores,
                        "shared_rung": shared_rung,
                        "winner": winner,
                        "migration": winner is not None,
                    })
                per_model[model]["tournament_rounds"] = rounds

        except Exception as exc:  # noqa: BLE001 - status must never crash the dashboard
            # A malformed/unreadable store should degrade to "no progress yet", not 500.
            per_model = {model: {"iterations": 0, "error": repr(exc)} for model in models}

        return {
            "status": self.optimizer_status,
            "request": self.optimizer_request,
            "error": self.optimizer_error,
            "started_at": self.optimizer_started_at,
            "finished_at": self.optimizer_finished_at,
            "models": per_model,
        }

    async def start_optimizer(
        self,
        *,
        backend: str,
        models: Sequence[str],
        threshold: Optional[float] = None,
        stop_limit: Optional[int] = None,
        phase_a_reps: Optional[int] = None,
        phase_b_reps: Optional[int] = None,
        retrieval_backend: Optional[str] = None,
        force: bool = False,
    ) -> bool:
        """Launch the closed-loop optimizer as a background task (the start button).

        Returns ``True`` if a run was launched, ``False`` if one is already running
        (the caller maps that to a 409). The optimizer streams ``optimizer_*`` events
        over the SAME broker the bake-off uses — new event TYPES only, so the bake-off's
        streaming is untouched (Req 9.7). Its lifecycle is tracked on this state object;
        its durable per-model progress lands in the append-only optimizer stores.

        The heavy optimizer modules (and the dataset load) are imported/performed lazily
        inside the task body so importing :mod:`bakeoff.app` and starting the app stay
        network-free, mirroring :meth:`start_run` / :meth:`_run_judge`.

        Raises:
            AuthorJudgeConflictError: when ``backend == "live"`` and the configured Author
                and Judge resolve to the same model (Req 4.2). Raised synchronously (before
                the task is created) so the route can surface it as a clean 4xx rather than
                a background-task 500.
        """
        if self.optimizer_status == OptimizerStatus.RUNNING:
            return False

        from datetime import datetime, timezone

        from bakeoff.quality.optimizer.backends import (
            build_live_backend,
            build_offline_backend,
        )

        # Build the backend bundle OFF the event loop. ``build_live_backend`` does
        # blocking I/O (boto3 credential resolution for the alpha profile, OpenSearch /
        # Bedrock client construction); calling it directly from this async handler
        # blocks the entire asyncio event loop, which freezes every other request
        # (status polls, SSE, page reload) until it returns — observed as "the Start
        # button does nothing and the whole dashboard hangs". ``asyncio.to_thread``
        # keeps the loop responsive while the bundle is built.
        #
        # The live Author/Judge separation check (AuthorJudgeConflictError) is raised
        # by ``build_live_backend`` BEFORE any network call, so it still propagates out
        # of the awaited thread and the route surfaces it as a clean 4xx (Req 4.2 /
        # design Component 12) rather than a background-task 500.
        if backend == "live":
            opt_backend = (
                await asyncio.to_thread(
                    build_live_backend, retrieval_backend=retrieval_backend
                )
                if retrieval_backend is not None
                else await asyncio.to_thread(build_live_backend)
            )
        else:
            opt_backend = await asyncio.to_thread(build_offline_backend)

        # Flip to RUNNING synchronously (before yielding to the task) so the snapshot
        # the caller returns is accurate immediately and a second start racing in
        # cannot slip past the guard before the task body runs.
        self.optimizer_status = OptimizerStatus.RUNNING
        self.optimizer_error = None
        self.optimizer_started_at = datetime.now(timezone.utc).isoformat()
        self.optimizer_finished_at = None
        self.optimizer_request = {
            "backend": backend,
            "models": [str(m) for m in models],
            "threshold": threshold,
            "stop_limit": stop_limit,
            "phase_a_reps": phase_a_reps,
            "phase_b_reps": phase_b_reps,
            "retrieval_backend": retrieval_backend,
            "force": force,
        }

        self._optimizer_task = asyncio.create_task(
            self._run_optimizer(
                opt_backend=opt_backend,
                models=[str(m) for m in models],
                threshold=threshold,
                stop_limit=stop_limit,
                phase_a_reps=phase_a_reps,
                phase_b_reps=phase_b_reps,
            )
        )
        return True

    async def _run_optimizer(
        self,
        *,
        opt_backend,
        models: Sequence[str],
        threshold: Optional[float],
        stop_limit: Optional[int],
        phase_a_reps: Optional[int],
        phase_b_reps: Optional[int],
    ) -> None:
        """Drive the per-model champion/challenger loop, tracking lifecycle for the UI.

        Mirrors the CLI wiring in :mod:`bakeoff.quality.main` (deterministic seeded
        split, per-model Phase A controllers, gated Phase B) but launched from the
        dashboard and streamed over the live broker. Never raises: any failure is
        recorded on :attr:`optimizer_error` with ``status == "failed"`` and the durable
        decision data is never touched (the controller routes its own failures to the
        disposable errors store).
        """
        from datetime import datetime, timezone

        try:
            from bakeoff.quality.dataset import load_multi_turn_items
            from bakeoff.quality.optimizer.controller import IterationController
            from bakeoff.quality.optimizer.events import OptimizerEventEmitter
            from bakeoff.quality.optimizer.orchestrator import PerModelOrchestrator
            from bakeoff.quality.optimizer.store import OptimizerStore

            # Dataset load is blocking — run it off the event loop.
            items = await asyncio.to_thread(load_multi_turn_items)
            tuning_slice, validation_set = IterationController.phase_a_split(items)

            store = OptimizerStore()
            emitter = OptimizerEventEmitter(self.broker)

            # Per-model Phase A controllers, each scoped to the held-out Tuning_Slice
            # (Req 7.1). Optional overrides default to their config values when None.
            controller_kwargs: dict = {}
            if threshold is not None:
                controller_kwargs["threshold"] = threshold
            if stop_limit is not None:
                controller_kwargs["stop_limit"] = stop_limit
            if phase_a_reps is not None:
                controller_kwargs["reps"] = phase_a_reps

            controllers = {
                model: IterationController(
                    model=model,
                    backend=opt_backend,
                    tuning_items=tuning_slice,
                    store=store,
                    emitter=emitter,
                    **controller_kwargs,
                )
                for model in models
            }

            orchestrator = PerModelOrchestrator(
                models=models,
                backend=opt_backend,
                store=store,
                emitter=emitter,
                view_registry=self.view_registry,
                controllers=controllers,
                validation_items=validation_set,
                phase_b_reps=phase_b_reps,
            )
            if hasattr(orchestrator, "run_v2"):
                # v2 island-tournament entry point. ``run_v2`` requires the model set,
                # backend, emitter, and store explicitly (the CLI passes them the same
                # way in bakeoff/quality/optimizer/main.py::main "islands" branch), and
                # ``all_items`` is the FULL multi-turn universe — run_v2 does its own
                # deterministic Phase-A split internally per model.
                await orchestrator.run_v2(
                    models,
                    opt_backend,
                    emitter=emitter,
                    store=store,
                    all_items=items,
                )
            else:
                await orchestrator.run()
            self.optimizer_status = OptimizerStatus.COMPLETED
        except Exception as exc:  # noqa: BLE001 - the optimizer must never crash the app
            self.optimizer_error = repr(exc)
            self.optimizer_status = OptimizerStatus.FAILED
        finally:
            self.optimizer_finished_at = datetime.now(timezone.utc).isoformat()
            self.broker.publish("optimizer_status", self.optimizer_snapshot())

    # -- Optimizer v2 (island-tournament) launch path + status -------------
    def optimizer_v2_snapshot(self) -> dict:
        """JSON-serializable view of the v2 lifecycle + per-island/per-round backfill.

        Lifecycle (``idle``/``running``/``completed``/``failed``) + launch request are
        held in memory; the per-island and per-tournament-round progress is reconstructed
        durably from the append-only store. Stale-v1 guard (Req 3.4): only records with
        non-null ``island_id`` / ``tournament_round`` are surfaced, so legacy v1-shaped
        rows never appear as v2 islands. Empty-but-well-formed before any v2 run.
        """
        models: list[str] = []
        if isinstance(self.optimizer_v2_request, dict):
            req_models = self.optimizer_v2_request.get("models")
            if isinstance(req_models, (list, tuple)):
                models = [str(m) for m in req_models]
        if not models:
            models = list(config.QUALITY_MODELS.keys())

        per_model: dict[str, dict] = {}
        try:
            from bakeoff.quality.optimizer.store import OptimizerStore

            store = OptimizerStore()
            audits = store.read_audits()
            for model in models:
                # -- per-island progress (skip the null/v1 group) ----
                island_groups = store.iteration_history_by_island(model)
                # Latest audit record per island (full prompt / diff / rationale).
                latest_audit: dict[int, object] = {}
                for a in audits:
                    if a.model != model or a.island_id is None:
                        continue
                    prev = latest_audit.get(a.island_id)
                    if prev is None or a.iteration_index >= prev.iteration_index:  # type: ignore[attr-defined]
                        latest_audit[a.island_id] = a
                islands: list[dict] = []
                for (_, island_id), recs in sorted(
                    ((k, v) for k, v in island_groups.items() if k[1] is not None),
                    key=lambda kv: kv[0][1],
                ):
                    if not recs:
                        continue
                    last_rec = recs[-1]
                    # The champion-score trajectory (the per-island trend curve) — every
                    # scored step in order, so a reload reconstructs the whole sparkline /
                    # race chart, not just the latest point.
                    score_series = [
                        {
                            "champion_score": r.champion_score,
                            "ci_half_width": r.champion_ci_half_width,
                            "rung_index": r.rung_index if r.rung_index is not None else 0,
                        }
                        for r in recs
                    ]
                    stance = (
                        config.QUALITY_OPT_ISLAND_STYLES[island_id]
                        if 0 <= island_id < len(config.QUALITY_OPT_ISLAND_STYLES)
                        else None
                    )
                    audit = latest_audit.get(island_id)
                    islands.append({
                        "island_id": island_id,
                        # The conversation type this island's records were appraised on —
                        # the dashboard splits single-run vs multi-run views on it.
                        "turn_mode": getattr(last_rec, "turn_mode", "multi"),
                        "rung_index": last_rec.rung_index,
                        "champion_score": last_rec.champion_score,
                        "champion_ci_half_width": last_rec.champion_ci_half_width,
                        "state": "converged" if last_rec.converged else "iterating",
                        "iterations": len(recs),
                        "stance": stance,
                        "score_series": score_series,
                        "champion_instruction": (
                            audit.champion_instruction if audit is not None else None  # type: ignore[attr-defined]
                        ),
                        "prompt_diff": (
                            audit.prompt_diff if audit is not None else None  # type: ignore[attr-defined]
                        ),
                        "author_reasoning": (
                            audit.author_rationale if audit is not None else None  # type: ignore[attr-defined]
                        ),
                        "challenger_score": (
                            audit.challenger_triad if audit is not None else None  # type: ignore[attr-defined]
                        ),
                        "challenger_ci_half_width": (
                            audit.challenger_ci_half_width if audit is not None else None  # type: ignore[attr-defined]
                        ),
                        "accepted": (
                            audit.accepted if audit is not None else None  # type: ignore[attr-defined]
                        ),
                    })

                # -- per-tournament-round summaries (skip the null/non-tournament group) ----
                tourn_groups = store.iteration_history_by_tournament_round(model)
                rounds: list[dict] = []
                for rnd, recs in sorted(
                    ((k, v) for k, v in tourn_groups.items() if k is not None),
                    key=lambda kv: kv[0],
                ):
                    if not recs:
                        continue
                    scores: list[dict] = []
                    winner: Optional[int] = None
                    shared_rung: Optional[int] = None
                    for r in recs:
                        scores.append({
                            "island_id": r.island_id,
                            "champion_score": r.champion_score,
                            "champion_ci_half_width": r.champion_ci_half_width,
                        })
                        if r.promoted:
                            winner = r.island_id
                        if r.rung_index is not None:
                            shared_rung = r.rung_index
                    rounds.append({
                        "round": rnd,
                        "scores": scores,
                        "shared_rung": shared_rung,
                        "winner": winner,
                        "migration": winner is not None,
                    })

                per_model[model] = {
                    "islands": islands,
                    "tournament_rounds": rounds,
                    "viewable": self.view_registry.has_active_view(model),
                }
        except Exception as exc:  # noqa: BLE001 - status must never crash the dashboard
            per_model = {model: {"islands": [], "tournament_rounds": [], "error": repr(exc)} for model in models}

        return {
            "status": self.optimizer_v2_status,
            "request": self.optimizer_v2_request,
            "error": self.optimizer_v2_error,
            "started_at": self.optimizer_v2_started_at,
            "finished_at": self.optimizer_v2_finished_at,
            "models": per_model,
        }

    async def start_optimizer_v2(
        self,
        *,
        backend: str,
        models: Sequence[str],
        retrieval_backend: Optional[str] = None,
    ) -> bool:
        """Launch the v2 island-tournament optimizer as a background task.

        Returns ``True`` if launched, ``False`` if one is already running (→ 409).
        Builds the backend bundle OFF the event loop (``build_live_backend`` does
        blocking boto3/OpenSearch I/O — building it inline freezes status polls, the
        v2 stream, and page reloads). The ``AuthorJudgeConflictError`` is raised inside
        ``build_live_backend`` before any network call, so it propagates out of the
        awaited thread and the route maps it to a clean 4xx.
        """
        if self.optimizer_v2_status == OptimizerStatus.RUNNING:
            return False

        from datetime import datetime, timezone

        from bakeoff.quality.optimizer.backends import (
            build_live_backend,
            build_offline_backend,
        )

        if backend == "live":
            opt_backend = (
                await asyncio.to_thread(
                    build_live_backend, retrieval_backend=retrieval_backend
                )
                if retrieval_backend is not None
                else await asyncio.to_thread(build_live_backend)
            )
        else:
            opt_backend = await asyncio.to_thread(build_offline_backend)

        self.optimizer_v2_status = OptimizerStatus.RUNNING
        self.optimizer_v2_error = None
        self.optimizer_v2_started_at = datetime.now(timezone.utc).isoformat()
        self.optimizer_v2_finished_at = None
        self.optimizer_v2_request = {
            "backend": backend,
            "models": [str(m) for m in models],
            "retrieval_backend": retrieval_backend,
        }

        self._optimizer_v2_task = asyncio.create_task(
            self._run_optimizer_v2(
                opt_backend=opt_backend, models=[str(m) for m in models]
            )
        )
        return True

    async def reset_optimizer_v2(self) -> dict:
        """Stop any active v2 run, reset lifecycle state, and clear the v2 stores.

        The one-button "stop + reset + clear" for fast re-runs from the dashboard.
        The v2 run is an ``asyncio.Task`` on this same process/event loop, so stopping
        it is a task cancel (no external process to kill). After cancelling, the
        in-memory lifecycle is reset to ``idle`` and the durable v2 store files
        (iterations / audit / errors SoT + the single-object results JSON) are
        truncated so the next run starts from a clean surface — the per-island and
        per-tournament-round backfill is reconstructed from these, so clearing them
        is what makes the UI show an empty state again.

        Idempotent and safe to call when nothing is running. Returns the post-reset
        snapshot (status ``idle``).
        """
        from datetime import datetime, timezone

        # 1) Cancel the running task (if any) and wait for it to unwind.
        task = self._optimizer_v2_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown only
                pass
        self._optimizer_v2_task = None

        # 2) Reset the in-memory lifecycle.
        self.optimizer_v2_status = OptimizerStatus.IDLE
        self.optimizer_v2_error = None
        self.optimizer_v2_started_at = None
        self.optimizer_v2_finished_at = None
        self.optimizer_v2_request = None

        # 3) Truncate the durable v2 stores so the backfill is empty (clear previous
        #    data). Truncate-in-place rather than delete so the on-disk layout is kept.
        for path in (
            config.QUALITY_OPT_ITERATIONS_PATH,
            config.QUALITY_OPT_AUDIT_PATH,
            config.QUALITY_OPT_ERRORS_PATH,
            config.QUALITY_OPT_RESULTS_PATH,
        ):
            try:
                if path.exists():
                    path.write_text("", encoding="utf-8")
            except OSError:
                pass  # best-effort clear; a locked/missing file must not 500 the reset

        # 4) Tell any open v2 stream the run is gone.
        self.optimizer_v2_finished_at = datetime.now(timezone.utc).isoformat()
        self.optimizer_v2_broker.publish("optimizer_status", self.optimizer_v2_snapshot())
        self.optimizer_v2_finished_at = None
        return self.optimizer_v2_snapshot()

    async def resume_optimizer_v2(self) -> "tuple[bool, dict]":
        """Re-launch a failed v2 run from where it left off — without wiping durable data.

        Distinct from :meth:`reset_optimizer_v2` (which truncates the stores):
        this clears only the in-memory lifecycle state and re-launches the run task
        using the same backend/models from the previous ``optimizer_v2_request``.
        The orchestrator will read the durable store and skip already-completed
        island steps via ``_restore_or_seed_islands``.

        Returns ``(True, snapshot)`` if the resume launched, or
        ``(False, snapshot)`` with the reason why not.
        """
        from datetime import datetime, timezone

        if self.optimizer_v2_status == OptimizerStatus.RUNNING:
            return False, self.optimizer_v2_snapshot()

        prev_request = self.optimizer_v2_request
        if not prev_request:
            return False, self.optimizer_v2_snapshot()

        backend = str(prev_request.get("backend", "offline"))
        models_raw = prev_request.get("models") or list(config.QUALITY_MODELS.keys())
        models = [str(m) for m in models_raw]
        retrieval_backend = prev_request.get("retrieval_backend")

        from bakeoff.quality.optimizer.backends import (
            build_live_backend,
            build_offline_backend,
        )

        if backend == "live":
            opt_backend = (
                await asyncio.to_thread(
                    build_live_backend, retrieval_backend=retrieval_backend
                )
                if retrieval_backend is not None
                else await asyncio.to_thread(build_live_backend)
            )
        else:
            opt_backend = await asyncio.to_thread(build_offline_backend)

        # Clear only the lifecycle fields — stores are intentionally NOT touched.
        self.optimizer_v2_status = OptimizerStatus.RUNNING
        self.optimizer_v2_error = None
        self.optimizer_v2_started_at = datetime.now(timezone.utc).isoformat()
        self.optimizer_v2_finished_at = None
        # Keep optimizer_v2_request intact (it holds the original params).

        self._optimizer_v2_task = asyncio.create_task(
            self._run_optimizer_v2(opt_backend=opt_backend, models=models)
        )
        return True, self.optimizer_v2_snapshot()

    async def _run_optimizer_v2(
        self, *, opt_backend, models: Sequence[str]
    ) -> None:
        """Drive ``PerModelOrchestrator.run_v2`` over the dedicated v2 broker.

        Loads the dataset off-loop, builds the store + a v2-broker-bound emitter +
        the orchestrator, then awaits ``run_v2``. Never raises: a failure is recorded
        on :attr:`optimizer_v2_error` with ``status == "failed"``; a final status event
        is always published on the v2 broker in ``finally``.
        """
        from datetime import datetime, timezone
        import logging

        _log = logging.getLogger("bakeoff.optimizer_v2")
        _log.info("optimizer_v2: ENTER run (models=%s)", list(models))

        try:
            from bakeoff.quality.dataset import load_multi_turn_items
            from bakeoff.quality.optimizer.events import OptimizerEventEmitter
            from bakeoff.quality.optimizer.orchestrator import PerModelOrchestrator
            from bakeoff.quality.optimizer.store import OptimizerStore

            items = await asyncio.to_thread(load_multi_turn_items)
            _log.info("optimizer_v2: dataset loaded (%d items); starting orchestrator.run_v2",
                      len(items))
            store = OptimizerStore()
            emitter = OptimizerEventEmitter(self.optimizer_v2_broker)
            orchestrator = PerModelOrchestrator(
                models=models,
                backend=opt_backend,
                store=store,
                emitter=emitter,
                view_registry=self.view_registry,
            )
            await orchestrator.run_v2(
                models, opt_backend, emitter=emitter, store=store, all_items=items
            )
            self.optimizer_v2_status = OptimizerStatus.COMPLETED
            _log.info("optimizer_v2: run_v2 COMPLETED")
        except Exception as exc:  # noqa: BLE001 - the optimizer must never crash the app
            self.optimizer_v2_error = repr(exc)
            self.optimizer_v2_status = OptimizerStatus.FAILED
            _log.exception("optimizer_v2: run FAILED: %r", exc)
        finally:
            self.optimizer_v2_finished_at = datetime.now(timezone.utc).isoformat()
            self.optimizer_v2_broker.publish(
                "optimizer_status", self.optimizer_v2_snapshot()
            )

    # ======================================================================
    # Optimizer V3 — hardened, LIVE-ONLY (bakeoff/quality/optimizer/v3/).
    # Mirrors the v2 lifecycle surface against v3's own broker, store paths,
    # and run-state sentinel; never reads or writes any v2 file.
    # ======================================================================
    @staticmethod
    def _optimizer_v3_store():
        """The v3-path-bound durable store (v2's schema, v3's files)."""
        from bakeoff.quality.optimizer.store import OptimizerStore

        return OptimizerStore(
            iterations_path=config.QUALITY_OPT_V3_ITERATIONS_PATH,
            audit_path=config.QUALITY_OPT_V3_AUDIT_PATH,
            errors_path=config.QUALITY_OPT_V3_ERRORS_PATH,
            results_path=config.QUALITY_OPT_V3_RESULTS_PATH,
        )

    def optimizer_v3_snapshot(self) -> dict:
        """JSON view of the v3 lifecycle + per-island/per-round durable backfill.

        Identical reconstruction discipline to :meth:`optimizer_v2_snapshot` (the
        UI components are shared), read from the v3 store paths, PLUS the v3
        run-state sentinel (phase progress, dead islands, degraded flag) so the
        V3 tab can render containment state. Never raises.
        """
        models: list[str] = []
        if isinstance(self.optimizer_v3_request, dict):
            req_models = self.optimizer_v3_request.get("models")
            if isinstance(req_models, (list, tuple)):
                models = [str(m) for m in req_models]
        if not models:
            models = list(config.QUALITY_MODELS.keys())

        # Run-state sentinel (phase / death / degradation) — best-effort read.
        run_state: dict = {}
        try:
            run_state = json.loads(config.QUALITY_OPT_V3_STATE_PATH.read_text())
        except (OSError, ValueError):
            run_state = {}

        per_model: dict[str, dict] = {}
        try:
            store = self._optimizer_v3_store()
            audits = store.read_audits()
            for model in models:
                island_groups = store.iteration_history_by_island(model)
                latest_audit: dict[int, object] = {}
                # Deduped per (island, iteration): an escalation used to re-persist
                # the same iteration's audit (see v3 orchestrator note), so keyed
                # storage keeps each round exactly once (last writer wins).
                history_by_key: dict[tuple[int, int], object] = {}
                for a in audits:
                    if a.model != model or a.island_id is None:
                        continue
                    prev = latest_audit.get(a.island_id)
                    if prev is None or a.iteration_index >= prev.iteration_index:  # type: ignore[attr-defined]
                        latest_audit[a.island_id] = a
                    history_by_key[(a.island_id, a.iteration_index)] = a
                model_state = dict(run_state.get(model) or {})
                dead_islands = set(model_state.get("dead_islands") or [])
                islands: list[dict] = []
                for (_, island_id), recs in sorted(
                    ((k, v) for k, v in island_groups.items() if k[1] is not None),
                    key=lambda kv: kv[0][1],
                ):
                    if not recs:
                        continue
                    last_rec = recs[-1]
                    score_series = [
                        {
                            # the belt-holder AFTER each step: a promoted step's new
                            # champion is its challenger_score (same rule as the
                            # headline below), so the curve never dips to the loser.
                            "champion_score": (
                                r.challenger_score
                                if r.promoted and r.challenger_score is not None
                                else r.champion_score
                            ),
                            "ci_half_width": (
                                r.challenger_ci_half_width
                                if r.promoted and r.challenger_ci_half_width is not None
                                else r.champion_ci_half_width
                            ),
                            "rung_index": r.rung_index if r.rung_index is not None else 0,
                        }
                        for r in recs
                    ]
                    stance = (
                        config.QUALITY_OPT_ISLAND_STYLES[island_id]
                        if 0 <= island_id < len(config.QUALITY_OPT_ISLAND_STYLES)
                        else None
                    )
                    audit = latest_audit.get(island_id)
                    # Prompt lineage (owner direction: show current AND previous
                    # prompts): the last few audited iterations, newest first.
                    island_history = sorted(
                        (rec for (isl_id, _), rec in history_by_key.items() if isl_id == island_id),
                        key=lambda rec: rec.iteration_index,  # type: ignore[attr-defined]
                        reverse=True,
                    )[:50]
                    prompt_history = [
                        {
                            "iteration_index": rec.iteration_index,  # type: ignore[attr-defined]
                            "accepted": rec.accepted,  # type: ignore[attr-defined]
                            "challenger_score": rec.challenger_triad,  # type: ignore[attr-defined]
                            "challenger_instruction": rec.challenger_instruction,  # type: ignore[attr-defined]
                            "champion_instruction": rec.champion_instruction,  # type: ignore[attr-defined]
                            "prompt_diff": rec.prompt_diff,  # type: ignore[attr-defined]
                        }
                        for rec in island_history
                    ]
                    # The CURRENT belt-holder's score: a promoted iteration's record
                    # keeps the OLD champion under champion_score (the pass it lost);
                    # the new champion's score is its challenger_score. Headline must
                    # show whoever holds the belt now.
                    if last_rec.promoted and last_rec.challenger_score is not None:
                        effective_champion = last_rec.challenger_score
                        effective_ci = last_rec.challenger_ci_half_width
                    else:
                        effective_champion = last_rec.champion_score
                        effective_ci = last_rec.champion_ci_half_width
                    islands.append({
                        "island_id": island_id,
                        "rung_index": last_rec.rung_index,
                        "champion_score": effective_champion,
                        "champion_ci_half_width": effective_ci,
                        "prompt_history": prompt_history,
                        "state": (
                            "dead" if island_id in dead_islands
                            else ("converged" if last_rec.converged else "iterating")
                        ),
                        "iterations": len(recs),
                        "stance": stance,
                        "score_series": score_series,
                        "champion_instruction": (
                            audit.champion_instruction if audit is not None else None  # type: ignore[attr-defined]
                        ),
                        "prompt_diff": (
                            audit.prompt_diff if audit is not None else None  # type: ignore[attr-defined]
                        ),
                        "author_reasoning": (
                            audit.author_rationale if audit is not None else None  # type: ignore[attr-defined]
                        ),
                        "challenger_score": (
                            audit.challenger_triad if audit is not None else None  # type: ignore[attr-defined]
                        ),
                        "challenger_ci_half_width": (
                            audit.challenger_ci_half_width if audit is not None else None  # type: ignore[attr-defined]
                        ),
                        "accepted": (
                            audit.accepted if audit is not None else None  # type: ignore[attr-defined]
                        ),
                    })

                tourn_groups = store.iteration_history_by_tournament_round(model)
                rounds: list[dict] = []
                for rnd, recs in sorted(
                    ((k, v) for k, v in tourn_groups.items() if k is not None),
                    key=lambda kv: kv[0],
                ):
                    if not recs:
                        continue
                    scores: list[dict] = []
                    winner: Optional[int] = None
                    shared_rung: Optional[int] = None
                    for r in recs:
                        scores.append({
                            "island_id": r.island_id,
                            "champion_score": r.champion_score,
                            "champion_ci_half_width": r.champion_ci_half_width,
                        })
                        if r.promoted:
                            winner = r.island_id
                        if r.rung_index is not None:
                            shared_rung = r.rung_index
                    rounds.append({
                        "round": rnd,
                        "scores": scores,
                        "shared_rung": shared_rung,
                        "winner": winner,
                        "migration": winner is not None,
                    })

                per_model[model] = {
                    "islands": islands,
                    "tournament_rounds": rounds,
                    "run_state": model_state,
                }
        except Exception as exc:  # noqa: BLE001 - status must never crash the dashboard
            per_model = {
                model: {"islands": [], "tournament_rounds": [], "error": repr(exc)}
                for model in models
            }

        return {
            "status": self.optimizer_v3_status,
            "request": self.optimizer_v3_request,
            "error": self.optimizer_v3_error,
            "started_at": self.optimizer_v3_started_at,
            "finished_at": self.optimizer_v3_finished_at,
            "models": per_model,
        }

    async def start_optimizer_v3(
        self,
        *,
        models: Sequence[str],
        retrieval_backend: Optional[str] = None,
        turn_mode: str = "multi",
    ) -> bool:
        """Launch the v3 hardened optimizer (LIVE backend only) as a background task.

        Returns ``True`` if launched, ``False`` if a v3 run is already active (→ 409).
        The live backend bundle is built OFF the event loop (blocking boto3/AOSS I/O),
        exactly like the v2 launch path. There is no offline variant for v3.
        """
        if self.optimizer_v3_status == OptimizerStatus.RUNNING:
            return False

        from datetime import datetime, timezone

        from bakeoff.quality.optimizer.v3.backends import build_v3_backend

        opt_backend = (
            await asyncio.to_thread(build_v3_backend, retrieval_backend=retrieval_backend)
            if retrieval_backend is not None
            else await asyncio.to_thread(build_v3_backend)
        )

        self.optimizer_v3_status = OptimizerStatus.RUNNING
        self.optimizer_v3_error = None
        self.optimizer_v3_started_at = datetime.now(timezone.utc).isoformat()
        self.optimizer_v3_finished_at = None
        turn_mode = turn_mode if turn_mode in ("single", "multi", "both") else "multi"
        self.optimizer_v3_request = {
            "backend": "live",
            "models": [str(m) for m in models],
            "retrieval_backend": retrieval_backend,
            "turn_mode": turn_mode,
        }

        self._optimizer_v3_task = asyncio.create_task(
            self._run_optimizer_v3(
                opt_backend=opt_backend, models=[str(m) for m in models], turn_mode=turn_mode
            )
        )
        return True

    def freeze_v3_champion_to_seed(self, *, model: str, island_id: int) -> dict:
        """Freeze the current champion prompt for (model, island) into its SEED file.

        Writes the latest durable champion instruction for that island to
        ``config.QUALITY_OPT_V3_SEEDS_DIR/<model>_i<island>.txt`` so the next run starts
        from it (the per-(model, island) seed override the orchestrator reads). Raises
        KeyError if no champion has been recorded for that island yet.
        """
        store = self._optimizer_v3_store()
        champions = store.last_champion_per_island(model)
        champion = champions.get(island_id)
        if not champion or not str(champion).strip():
            raise KeyError(f"no champion recorded yet for model={model!r} island={island_id}")
        seed_path = config.QUALITY_OPT_V3_SEEDS_DIR / f"{model}_i{island_id}.txt"
        seed_path.parent.mkdir(parents=True, exist_ok=True)
        seed_path.write_text(str(champion).strip() + "\n", encoding="utf-8")
        return {
            "frozen": True,
            "model": model,
            "island_id": island_id,
            "path": str(seed_path),
            "chars": len(str(champion).strip()),
        }

    async def _run_optimizer_v3(
        self, *, opt_backend, models: Sequence[str], turn_mode: str = "multi"
    ) -> None:
        """Drive ``V3Orchestrator.run_v3`` over the dedicated v3 broker. Never raises.

        Note the contract difference from v2: ``run_v3`` itself CONTAINS per-model
        failures (a failed model yields a structured ``{"status": "failed"}`` result),
        so this wrapper's catch-all only fires on infrastructure failures (dataset
        load, store construction) — and the lifecycle ends ``failed`` only then.
        """
        from datetime import datetime, timezone
        import logging

        _log = logging.getLogger("bakeoff.optimizer_v3")
        _log.info("optimizer_v3: ENTER run (models=%s)", list(models))

        try:
            from bakeoff.quality.dataset import load_multi_turn_items
            from bakeoff.quality.optimizer.events import OptimizerEventEmitter
            from bakeoff.quality.optimizer.v3.orchestrator import V3Orchestrator

            # turn_mode selects the conversation type the optimizer appraises on:
            #   single → single-turn queries only (clean gold, lowest appraisal noise),
            #   multi  → multi-turn conversations only (default; the scripted path),
            #   both   → the full universe.
            def _load_for_mode(mode: str):
                from bakeoff.quality.dataset import DatasetLoader
                if mode == "single":
                    return [it for it in DatasetLoader().load_items() if not it.is_multi_turn]
                if mode == "both":
                    return list(DatasetLoader().load_items())
                return load_multi_turn_items()

            items = await asyncio.to_thread(_load_for_mode, turn_mode)
            _log.info("optimizer_v3: dataset loaded (%d items, turn_mode=%s); starting run_v3",
                      len(items), turn_mode)
            store = self._optimizer_v3_store()
            emitter = OptimizerEventEmitter(self.optimizer_v3_broker)
            orchestrator = V3Orchestrator(
                models=models,
                backend=opt_backend,
                store=store,
                emitter=emitter,
                view_registry=self.view_registry,
                turn_mode=turn_mode,
            )
            results = await orchestrator.run_v3(
                models, opt_backend, emitter=emitter, store=store, all_items=items
            )
            failed_models = [m for m, r in results.items()
                             if isinstance(r, dict) and r.get("status") != "completed"]
            if failed_models:
                self.optimizer_v3_error = (
                    f"model(s) finished non-completed: {sorted(failed_models)}"
                )
            self.optimizer_v3_status = (
                OptimizerStatus.COMPLETED if not failed_models else OptimizerStatus.FAILED
            )
            _log.info("optimizer_v3: run_v3 finished (failed_models=%s)", failed_models)
        except Exception as exc:  # noqa: BLE001 - the optimizer must never crash the app
            self.optimizer_v3_error = repr(exc)
            self.optimizer_v3_status = OptimizerStatus.FAILED
            _log.exception("optimizer_v3: run FAILED: %r", exc)
        finally:
            self.optimizer_v3_finished_at = datetime.now(timezone.utc).isoformat()
            self.optimizer_v3_broker.publish(
                "optimizer_status", self.optimizer_v3_snapshot()
            )

    async def reset_optimizer_v3(self) -> dict:
        """Stop any active v3 run, reset lifecycle, and clear the v3 stores + sentinel."""
        from datetime import datetime, timezone

        task = self._optimizer_v3_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown only
                pass
        self._optimizer_v3_task = None

        self.optimizer_v3_status = OptimizerStatus.IDLE
        self.optimizer_v3_error = None
        self.optimizer_v3_started_at = None
        self.optimizer_v3_finished_at = None
        self.optimizer_v3_request = None

        # ARCHIVE, never destroy: a reset moves the v3 stores into a timestamped
        # archive dir (mirroring the repo's _archive_pre_run convention) so a
        # mis-click can always be recovered. Empty files are simply removed.
        from datetime import datetime as _dt

        archive_dir = (
            config.BAKEOFF_DIR / f"_archive_v3_reset_{_dt.now().strftime('%Y%m%d_%H%M%S')}"
        )
        for path in (
            config.QUALITY_OPT_V3_ITERATIONS_PATH,
            config.QUALITY_OPT_V3_AUDIT_PATH,
            config.QUALITY_OPT_V3_ERRORS_PATH,
            config.QUALITY_OPT_V3_RESULTS_PATH,
            config.QUALITY_OPT_V3_STATE_PATH,
        ):
            try:
                if not path.exists():
                    continue
                if path.stat().st_size > 0:
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    path.rename(archive_dir / path.name)
                else:
                    path.unlink()
            except OSError:
                pass  # best-effort; a locked/missing file must not 500 the reset

        self.optimizer_v3_finished_at = datetime.now(timezone.utc).isoformat()
        self.optimizer_v3_broker.publish("optimizer_status", self.optimizer_v3_snapshot())
        self.optimizer_v3_finished_at = None
        return self.optimizer_v3_snapshot()

    async def resume_optimizer_v3(self) -> "tuple[bool, dict]":
        """Re-launch a v3 run from its durable checkpoints (stores + sentinel kept).

        The v3 orchestrator's restore covers BOTH grains: the run-state sentinel skips
        completed phases entirely, and the durable iteration records fast-forward each
        island inside an incomplete Phase A.
        """
        from datetime import datetime, timezone

        if self.optimizer_v3_status == OptimizerStatus.RUNNING:
            return False, self.optimizer_v3_snapshot()

        prev_request = self.optimizer_v3_request
        if not prev_request:
            return False, self.optimizer_v3_snapshot()

        models = [str(m) for m in (prev_request.get("models") or list(config.QUALITY_MODELS.keys()))]
        retrieval_backend = prev_request.get("retrieval_backend")

        from bakeoff.quality.optimizer.v3.backends import build_v3_backend

        opt_backend = (
            await asyncio.to_thread(build_v3_backend, retrieval_backend=retrieval_backend)
            if retrieval_backend is not None
            else await asyncio.to_thread(build_v3_backend)
        )

        self.optimizer_v3_status = OptimizerStatus.RUNNING
        self.optimizer_v3_error = None
        self.optimizer_v3_started_at = datetime.now(timezone.utc).isoformat()
        self.optimizer_v3_finished_at = None

        self._optimizer_v3_task = asyncio.create_task(
            self._run_optimizer_v3(opt_backend=opt_backend, models=models)
        )
        return True, self.optimizer_v3_snapshot()

    # ======================================================================
    # Prompt Bench — fixed prompt leaderboard (own broker/stores/account)
    # ======================================================================
    @staticmethod
    def _promptbench_store():
        """The Prompt Bench durable store (its own files; never optimizer data)."""
        from bakeoff.promptbench.store import PromptBenchStore

        return PromptBenchStore()

    def promptbench_snapshot(self) -> dict:
        """Lifecycle + durable backfill (per-prompt points + aggregate + winner + texts)."""
        recon: dict = {"points": {}, "results": {}}
        winner = None
        prompts_meta: dict = {}
        try:
            recon = self._promptbench_store().reconstruct()
            from bakeoff.promptbench.runner import compute_winner

            winner = compute_winner(list(recon.get("results", {}).values()))
        except Exception:  # noqa: BLE001 - a snapshot must never raise
            pass
        try:
            from bakeoff.promptbench.prompts import load_prompts

            prompts_meta = {p.key: {"label": p.label, "text": p.text} for p in load_prompts()}
        except Exception:  # noqa: BLE001 - missing/empty prompts dir must not 500 the snapshot
            pass
        return {
            "status": self.promptbench_status,
            "error": self.promptbench_error,
            "started_at": self.promptbench_started_at,
            "finished_at": self.promptbench_finished_at,
            "model": config.PROMPT_BENCH_MODEL,
            "points": recon.get("points", {}),
            "results": recon.get("results", {}),
            "prompts_meta": prompts_meta,
            "winner": winner,
        }

    async def start_promptbench(self) -> bool:
        """Launch the Prompt Bench run as a background task. False if already running."""
        from datetime import datetime, timezone

        if self.promptbench_status == OptimizerStatus.RUNNING:
            return False
        self.promptbench_status = OptimizerStatus.RUNNING
        self.promptbench_error = None
        self.promptbench_started_at = datetime.now(timezone.utc).isoformat()
        self.promptbench_finished_at = None
        self._promptbench_task = asyncio.create_task(self._run_promptbench())
        return True

    async def _run_promptbench(self) -> None:
        """Drive PromptBenchRunner over the dedicated broker. Never raises."""
        import logging
        from datetime import datetime, timezone

        _log = logging.getLogger("bakeoff.promptbench")
        try:
            from bakeoff.promptbench.runner import PromptBenchRunner

            def _emit(event_type: str, payload: dict) -> None:
                self.promptbench_broker.publish(event_type, payload)

            runner = PromptBenchRunner(store=self._promptbench_store(), emit=_emit)
            await runner.run()
            self.promptbench_status = OptimizerStatus.COMPLETED
        except Exception as exc:  # noqa: BLE001 - must never crash the app
            self.promptbench_error = repr(exc)
            self.promptbench_status = OptimizerStatus.FAILED
            _log.exception("promptbench: run FAILED: %r", exc)
        finally:
            self.promptbench_finished_at = datetime.now(timezone.utc).isoformat()
            self.promptbench_broker.publish("promptbench_status", self.promptbench_snapshot())

    async def reset_promptbench(self) -> dict:
        """Stop any active run, reset lifecycle, and ARCHIVE (never destroy) the stores."""
        from datetime import datetime, timezone

        task = self._promptbench_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown only
                pass
        self._promptbench_task = None
        self.promptbench_status = OptimizerStatus.IDLE
        self.promptbench_error = None
        self.promptbench_started_at = None
        self.promptbench_finished_at = None
        try:
            self._promptbench_store().archive()
        except Exception:  # noqa: BLE001 - archive is best-effort
            pass
        self.promptbench_broker.publish("promptbench_status", self.promptbench_snapshot())
        return self.promptbench_snapshot()

    # -- Eval dashboard: durable backfill snapshot + offline run launch ----
    def eval_snapshot(self) -> dict:
        """JSON-serializable durable-backfill view state for the eval dashboard.

        The lifecycle (``idle``/``running``/``completed``/``failed``) + launch
        request are held in memory; everything else is reconstructed from the
        append-only :class:`~bakeoff.eval.event_store.EvalEventStore` so a page
        reload reflects exactly what is durably on disk (Req 8.2/8.3, 15.2):

        * ``agents`` / ``sessions`` / ``corpus_sizes`` — the distinct values seen,
          sorted, so the Control_Panel can populate its selectors.
        * ``instance_count`` — total durable records.
        * ``instances`` — a trailing window of records (newest-first), each in the
          SAME shape as the ``eval_instance_appended`` event payload, so the client
          feeds backfill and live deltas through one code path.
        * ``rollups`` — per-agent counts (total / failed) for a cheap header.
        * ``sweep`` — requested vs. observed corpus sizes (sweep progress).

        Empty-but-well-formed before any run exists, and **never raises**: a read
        against a malformed/partway-written store is caught and degrades to the
        empty shape with an ``error`` marker (mirroring ``optimizer_v2_snapshot``),
        so ``GET /api/eval/status`` never 500s (Req 15.5).
        """
        # Lifecycle + request echo are always available (in-memory).
        base: dict = {
            "status": self.eval_status,
            "request": self.eval_request,
            "error": self.eval_error,
            "started_at": self.eval_started_at,
            "finished_at": self.eval_finished_at,
            "agents": [],
            "sessions": [],
            "corpus_sizes": [],
            "instance_count": 0,
            "instances": [],
            "rollups": {},
            "sweep": {"requested_sizes": [], "completed_sizes": [], "remaining": []},
        }

        # Requested sweep sizes come from the launch request (in-memory).
        requested_sizes: list[int] = []
        if isinstance(self.eval_request, dict):
            raw_sizes = self.eval_request.get("corpus_sizes")
            if isinstance(raw_sizes, (list, tuple)):
                requested_sizes = [int(s) for s in raw_sizes]
            elif self.eval_request.get("corpus_size") is not None:
                requested_sizes = [int(self.eval_request["corpus_size"])]
        base["sweep"]["requested_sizes"] = requested_sizes

        try:
            from bakeoff.eval.event_store import EvalEventStore

            store = EvalEventStore(self.eval_events_path)
            instances = store.read_all()

            agents = sorted({i.agent_id for i in instances})
            sessions = sorted({i.session_id for i in instances})
            corpus_sizes = sorted({int(i.corpus_size) for i in instances})

            rollups: dict[str, dict] = {}
            for inst in instances:
                r = rollups.setdefault(inst.agent_id, {"count": 0, "failed": 0})
                r["count"] += 1
                if inst.status == "failed":
                    r["failed"] += 1

            # Trailing window, newest-first, in the eval_instance_appended shape.
            window = instances[-4000:]
            window_dicts = [inst.to_dict() for inst in reversed(window)]

            completed = [s for s in requested_sizes if s in set(corpus_sizes)]
            remaining = [s for s in requested_sizes if s not in set(corpus_sizes)]

            base.update(
                {
                    "agents": agents,
                    "sessions": sessions,
                    "corpus_sizes": corpus_sizes,
                    "instance_count": len(instances),
                    "instances": window_dicts,
                    "rollups": rollups,
                }
            )
            base["sweep"]["completed_sizes"] = completed
            base["sweep"]["remaining"] = remaining
        except Exception as exc:  # noqa: BLE001 - status must never crash the dashboard
            # Degrade to the empty-but-well-formed shape with an error marker.
            base["error"] = base["error"] or repr(exc)
        return base

    @property
    def eval_prompt_store(self):
        """The shared, lazily-built ragas-metric Prompt_Store (Req 16).

        A single :class:`~bakeoff.eval.prompt_store.PromptStore` instance backs
        both the GET/PUT prompt routes and the eval run's Ragas_Adapter, so an
        override persisted via PUT is the prompt the next-scored instances use
        while previously recorded values are untouched (Req 16.5). Built lazily so
        importing :mod:`bakeoff.app` stays light.
        """
        if self._eval_prompt_store is None:
            from bakeoff.eval.prompt_store import PromptStore

            self._eval_prompt_store = PromptStore(self.eval_prompts_path)
        return self._eval_prompt_store

    def _build_eval_queries(self, num_queries: int):
        """Build a deterministic, synthetic, non-PII query set (Req 21.3).

        The eval run is visualization data: it does not need real corpus queries,
        and generating them in-code keeps a started run fully network-free. The
        ids are stable so retrieval memoization (Req 19.3) is exercised.
        """
        from bakeoff.eval.experiment_runner import Query

        n = max(1, int(num_queries))
        return [
            Query(
                query_id=f"q{i}",
                text=f"synthetic eval query {i}: what does fragment {i} say about topic {i}?",
                reference=f"synthetic reference answer for query {i} about topic {i}",
                prompt_id="eval-default",
                category="synthetic",
            )
            for i in range(n)
        ]

    def _build_eval_queries_from_ids(self, query_ids: Sequence[str]):
        """Build a synthetic, non-PII query subset from explicit ids (Req 22.5).

        The on-demand capability lets the user pick an **arbitrary subset of the
        available queries** (Req 22.5). The available query pool is the harness's
        deterministic synthetic set (see :meth:`_build_eval_queries`); a request
        names the subset by id (``q0``, ``q3``, …) and this builds exactly those
        Query objects, preserving the requested order and de-duplicating ids (the
        runner requires a unique query set). Text is synthetic and derived from
        the id, so an on-demand run stays fully network-free (Req 22.13).
        """
        from bakeoff.eval.experiment_runner import Query

        seen: set[str] = set()
        queries = []
        for raw in query_ids:
            qid = str(raw)
            if qid in seen:
                continue
            seen.add(qid)
            queries.append(
                Query(
                    query_id=qid,
                    text=f"synthetic eval query {qid}: what does fragment {qid} say about its topic?",
                    reference=f"synthetic reference answer for query {qid}",
                    prompt_id="eval-on-demand",
                    category="synthetic",
                )
            )
        return queries

    def _build_eval_runner(self, metrics, loop: "asyncio.AbstractEventLoop"):
        """Construct an OFFLINE, network-free Experiment_Runner for a started run.

        Wires an offline :class:`RagasAdapter` (injected fakes, zero network —
        Req 1.5), a fresh :class:`RetrievalMetricComputer`, the durable
        :class:`EvalEventStore` wrapped in :class:`_PublishingEvalStore` (so every
        appended record publishes exactly one delta on the dedicated eval broker),
        and offline retrieval/agent providers. The providers are injectable via
        :attr:`eval_retrieval_provider` / :attr:`eval_agent_provider` (tests
        override them); when ``None`` the network-free closures below are used.
        """
        from bakeoff.eval.event_store import EvalEventStore
        from bakeoff.eval.experiment_runner import (
            AgentAnswer,
            ExperimentRunner,
            RetrievalResult,
        )
        from bakeoff.eval.metric_engine import MetricEngine
        from bakeoff.eval.ragas_adapter import RagasAdapter

        durable = EvalEventStore(self.eval_events_path)
        publishing = _PublishingEvalStore(durable, self.eval_broker, loop)
        adapter = RagasAdapter.offline(
            enabled_metrics=list(metrics) or None,
            prompt_store=self.eval_prompt_store,
        )
        engine = MetricEngine(publishing, ragas_adapter=adapter)

        def _offline_retrieval(query, corpus_size):
            """Deterministic retrieval for one (query, corpus_size); zero network."""
            ranked = tuple(f"doc-{query.query_id}-{j}" for j in range(5))
            gold = (ranked[0],)  # the top-ranked doc is the resolvable Gold_Link
            fragments = (
                f"context fragment for {query.text} drawn from a corpus of "
                f"{corpus_size} documents",
            )
            return RetrievalResult(
                ranked_ids=ranked,
                gold_ids=gold,
                fragments=fragments,
                retrieval_ms=1.0,
                cached=False,
            )

        def _offline_agent(agent_id, query, retrieval):
            """Deterministic agent answer for one execution; zero network."""
            answer = (
                f"{agent_id} responds to {query.text} grounded in "
                f"{' '.join(retrieval.fragments)}"
            )
            return AgentAnswer(
                answer=answer,
                generation_ms=2.0,
                confidence=0.5,
                volume=float(len(answer)),
                cost=0.001,
            )

        retrieval_provider = self.eval_retrieval_provider or _offline_retrieval
        agent_provider = self.eval_agent_provider or _offline_agent
        return ExperimentRunner(
            engine,
            retrieval_provider,
            agent_provider,
            corpus_preparer=self.eval_corpus_preparer,
        )

    async def start_eval_run(
        self,
        *,
        agents: Sequence[str],
        metrics: Sequence[str],
        corpus_sizes: Optional[Sequence[int]] = None,
        corpus_size: int = 100,
        num_queries: int = 4,
        query_ids: Optional[Sequence[str]] = None,
    ) -> bool:
        """Launch an offline multi-agent run / corpus-size sweep as a background task.

        Returns ``True`` if launched, ``False`` if an eval run is already active
        (→ the route maps that to **409**). The lifecycle flips to ``running``
        **synchronously** before the task is created (so the 202 snapshot and any
        racing second start see the run as active immediately), mirroring the
        optimizer-v2 discipline. Input validation (unknown agent/metric, the
        multi-agent floor) is the route's responsibility (→ 422); this method
        assumes a validated request.

        ``query_ids`` (when supplied) selects an arbitrary subset of the available
        queries for the on-demand path (Req 22.5); otherwise a ``num_queries``-long
        default synthetic set is used.
        """
        if self.eval_status == EvalStatus.RUNNING:
            return False
        self._spawn_eval_run(
            agents=agents,
            metrics=metrics,
            corpus_sizes=corpus_sizes,
            corpus_size=corpus_size,
            num_queries=num_queries,
            query_ids=query_ids,
        )
        return True

    def _spawn_eval_run(
        self,
        *,
        agents: Sequence[str],
        metrics: Sequence[str],
        corpus_sizes: Optional[Sequence[int]] = None,
        corpus_size: int = 100,
        num_queries: int = 4,
        query_ids: Optional[Sequence[str]] = None,
    ) -> None:
        """Flip the lifecycle to ``running`` and create the background run task.

        The synchronous status flip + request echo happens here so both the normal
        :meth:`start_eval_run` entry and the on-demand queue drain
        (:meth:`_drain_eval_queue`, Req 22.11) share one launch path and one
        lifecycle discipline. Assumes no run is currently active (the caller
        guarantees it).
        """
        from datetime import datetime, timezone

        agent_list = [str(a) for a in agents]
        metric_list = [str(m) for m in metrics]
        sizes = [int(s) for s in corpus_sizes] if corpus_sizes else None
        qids = [str(q) for q in query_ids] if query_ids else None

        self.eval_status = EvalStatus.RUNNING
        self.eval_error = None
        self.eval_started_at = datetime.now(timezone.utc).isoformat()
        self.eval_finished_at = None
        self.eval_request = {
            "agents": agent_list,
            "metrics": metric_list,
            "corpus_sizes": sizes,
            "corpus_size": int(corpus_size),
            "num_queries": int(num_queries),
            "query_ids": qids,
        }

        self._eval_task = asyncio.create_task(
            self._run_eval(
                agents=agent_list,
                metrics=metric_list,
                corpus_sizes=sizes,
                corpus_size=int(corpus_size),
                num_queries=int(num_queries),
                query_ids=qids,
            )
        )

    def _drain_eval_queue(self) -> None:
        """Start the next enqueued on-demand run, if any (Req 22.11).

        Called from :meth:`_run_eval`'s ``finally`` once the active run has settled
        (status ``completed``/``failed``). At most one on-demand run is ever active
        (Req 22.10), so the next queued request can only start *after* the active
        one completes — exactly the bounded-queue ordering Req 22.11 requires.
        """
        if not self.eval_queue:
            return
        next_kwargs = self.eval_queue.popleft()
        self._spawn_eval_run(**next_kwargs)

    async def _run_eval(
        self,
        *,
        agents: Sequence[str],
        metrics: Sequence[str],
        corpus_sizes: Optional[Sequence[int]],
        corpus_size: int,
        num_queries: int,
        query_ids: Optional[Sequence[str]] = None,
    ) -> None:
        """Drive the offline Experiment_Runner; never raises.

        The Experiment_Runner is synchronous and is executed on a worker thread
        (``asyncio.to_thread``) so a slow/gated producer never blocks the event
        loop (status polls, the eval stream, and reloads keep serving). A failure
        is recorded on :attr:`eval_error` with ``status == "failed"``; a final
        ``eval_status`` event is always published on the dedicated eval broker.

        ``query_ids`` (when supplied) selects an arbitrary subset of the available
        queries (the on-demand path, Req 22.5); otherwise the default synthetic
        ``num_queries``-long set is used. After the run settles, the next enqueued
        on-demand request (if any) is started (Req 22.11).
        """
        from datetime import datetime, timezone

        try:
            loop = asyncio.get_running_loop()
            runner = self._build_eval_runner(metrics, loop)
            if query_ids:
                queries = self._build_eval_queries_from_ids(query_ids)
            else:
                queries = self._build_eval_queries(num_queries)
            if corpus_sizes:
                await asyncio.to_thread(
                    runner.run_sweep, agents, queries, corpus_sizes=corpus_sizes
                )
            else:
                await asyncio.to_thread(
                    runner.run_multi_agent, agents, queries, corpus_size=corpus_size
                )
            self.eval_status = EvalStatus.COMPLETED
        except Exception as exc:  # noqa: BLE001 - the eval run must never crash the app
            self.eval_error = repr(exc)
            self.eval_status = EvalStatus.FAILED
        finally:
            self.eval_finished_at = datetime.now(timezone.utc).isoformat()
            self.eval_broker.publish("eval_status", self.eval_snapshot())
            # Start the next enqueued on-demand run, if any (Req 22.11). Done last,
            # after the active run has fully settled, so at most one is ever active.
            self._drain_eval_queue()


def _get_state(request: Request) -> AppState:
    """Fetch the :class:`AppState` off the request's app (single instance)."""
    return request.app.state.bakeoff


async def _view_scoped_stream(state: "AppState", model: str, *, broker: "Optional[SSEBroker]" = None):
    """Stream SSE for one Per_Model_View, marking ``model`` viewable for its lifetime.

    Wraps :meth:`SSEBroker.subscribe` in the :class:`ViewRegistry` subscription scope
    (Req 1.11 / 9.8): opening this stream marks ``model`` active (so the optimizer's
    concurrency gate sees a live view), and closing it — client disconnect, server
    shutdown, or generator GC — clears it via the registry's balanced
    ``mark_active`` / ``mark_inactive`` pair. The model stays viewable as long as at
    least one of its subscriptions is open (the registry is reference-counted), so two
    browser tabs on the same model behave correctly.

    Reuses the existing broker fan-out unchanged: every optimizer event still reaches
    this subscriber and the Per_Model_View filters to its own ``model_channel`` client
    side. The bake-off's parameterless subscription does not go through here.
    """
    with state.view_registry.subscription(model):
        async for chunk in (broker if broker is not None else state.broker).subscribe():
            yield chunk


# ---------------------------------------------------------------------------
# Live aggregation (cheap normal-approx CIs — Req 10.4)
# ---------------------------------------------------------------------------
def _event_dim_value(event: TrialEvent, dim: str) -> str:
    """Resolve one group/filter dimension to its string value on an event.

    ``model`` / ``pass`` address top-level identity (``pass`` → ``pass_name``);
    everything else is a cohort axis on :class:`~bakeoff.types.CohortKey`. Mirrors
    the aggregation engine's resolution so live and report views slice identically.
    """
    if dim == "model":
        return event.model
    if dim == "pass":
        return event.pass_name
    if hasattr(event.cohort, dim):
        return getattr(event.cohort, dim)
    raise KeyError(f"unknown dimension {dim!r}; expected 'model', 'pass', or a cohort axis")


def _latency_quantiles(group_events: Sequence[TrialEvent], metric: str) -> Optional[dict]:
    """p50/p90/p95 for a latency metric over all reps, else ``None`` (Req 9.5)."""
    if not is_latency_metric(metric):
        return None
    vals = [
        v for ev in group_events if (v := extract_metric_value(ev, metric)) is not None
    ]
    if not vals:
        return None
    import numpy as np

    arr = np.asarray(vals, dtype=np.float64)
    return {
        "p50": float(np.percentile(arr, 50)),
        "p90": float(np.percentile(arr, 90)),
        "p95": float(np.percentile(arr, 95)),
    }


def live_aggregates(
    events: Sequence[TrialEvent],
    group_by: Sequence[str],
    metric: str = "composite",
    *,
    min_items_for_ci: int = config.MIN_ITEMS_FOR_CI,
) -> list[Aggregate]:
    """Group ``events`` and summarize ``metric`` with the **cheap normal-approx CI**.

    This is the live-UI aggregator (Req 10.4): unlike the exec/report layer (which
    uses the expensive item-level cluster bootstrap), every CI here is the
    closed-form :func:`bakeoff.stats.normal_approx_ci` so running averages update
    cheaply as trials land.

    It preserves the two load-bearing invariants of the report engine:

    * **P4 (no answerability blend).** If ``metric`` is an accuracy metric and any
      produced group spans more than one ``answerability`` value, the whole call
      raises :class:`~bakeoff.aggregate.AnswerabilityBlendError` — the caller must
      slice by ``answerability`` first.
    * **P10 (no number without a CI).** A group with fewer than
      ``min_items_for_ci`` distinct items is marked ``insufficient_data`` with
      ``mean_ci=None``; otherwise it carries a populated normal-approx CI. The
      exclusive-or ``(mean_ci is None) == insufficient_data`` always holds.

    Groups are emitted in sorted-key order for stable output.
    """
    if not group_by:
        raise ValueError("group_by must contain at least one dimension")
    group_by = list(group_by)

    buckets: dict[tuple[str, ...], list[TrialEvent]] = {}
    for ev in events:
        key = tuple(_event_dim_value(ev, dim) for dim in group_by)
        buckets.setdefault(key, []).append(ev)

    out: list[Aggregate] = []
    for key in sorted(buckets):
        group_events = buckets[key]
        group = {dim: val for dim, val in zip(group_by, key)}

        # P4 guard: an accuracy metric must not be blended across answerability.
        if is_accuracy_metric(metric):
            classes = {ev.answerability for ev in group_events}
            if len(classes) > 1:
                raise AnswerabilityBlendError(
                    f"refusing to average accuracy metric {metric!r} across "
                    f"answerability classes {sorted(classes)} for group {group!r}: "
                    f"slice by 'answerability' first (Req 5.4/5.5)"
                )

        by_item = group_rep_values_by_item(group_events, metric)
        n_items = len(by_item)
        n_trials = sum(len(v) for v in by_item.values())
        vdecomp = variance_decomp(group_events, metric)
        latency = _latency_quantiles(group_events, metric)

        if n_items < min_items_for_ci:
            out.append(
                Aggregate(
                    group=group,
                    metric=metric,
                    n_items=n_items,
                    n_trials=n_trials,
                    mean_ci=None,
                    variance_decomp=vdecomp,
                    latency_quantiles=latency,
                    insufficient_data=True,
                )
            )
            continue

        ci: CI = normal_approx_ci(list(group_events), metric, level=config.CONFIDENCE_LEVEL)
        out.append(
            Aggregate(
                group=group,
                metric=metric,
                n_items=n_items,
                n_trials=n_trials,
                mean_ci=ci,
                variance_decomp=vdecomp,
                latency_quantiles=latency,
                insufficient_data=False,
            )
        )
    return out


def _parse_group_by(request: Request) -> list[str]:
    """Parse ``group_by`` from the query string (repeatable AND/or comma-separated)."""
    raw = request.query_params.getlist("group_by")
    dims: list[str] = []
    for chunk in raw:
        dims.extend(part.strip() for part in chunk.split(",") if part.strip())
    return dims or ["model"]


def _apply_cohort_filters(request: Request, events: list[TrialEvent]) -> list[TrialEvent]:
    """Filter events by any cohort/identity equality params in the query string."""
    filterable = set(COHORT_DIMENSIONS) | {"model", "pass"}
    active = {
        k: request.query_params[k]
        for k in request.query_params.keys()
        if k in filterable
    }
    if not active:
        return events
    return [
        ev
        for ev in events
        if all(_event_dim_value(ev, dim) == val for dim, val in active.items())
    ]


# ---------------------------------------------------------------------------
# Bake-Off diagnostics — decision cockpit payload over the clean outcomes log
# ---------------------------------------------------------------------------
BAKEOFF_COMPONENT_METRICS: tuple[str, ...] = (
    "composite",
    "grounding_precision",
    "grounding_recall",
    "recall_at_k",
    "precision_at_k",
    "ndcg_at_k",
    "mrr",
    "semantic_similarity",
    "abstention_correct",
    "unwarranted_refusal",
    "faithfulness",
    "correctness",
    "completeness",
)

BAKEOFF_TIMING_FIELDS: tuple[str, ...] = (
    "embed_query_ms",
    "bm25_vectorize_ms",
    "hybrid_search_ms",
    "rerank_ms",
    "retrieval_total_ms",
    "ttft_ms",
    "generation_total_ms",
    "end_to_end_ms",
)

BAKEOFF_COHORT_DIMENSIONS: tuple[str, ...] = (
    "answerability",
    "turn_type",
    "momentary_state",
    "entry_route",
    "tone",
    "proficiency",
)


def _mean(values: Sequence[float]) -> Optional[float]:
    """Mean of finite values, or ``None`` when empty."""
    finite_values = [value for value in values if math.isfinite(value)]
    if not finite_values:
        return None
    return sum(finite_values) / len(finite_values)


def _quantile(sorted_values: Sequence[float], fraction: float) -> Optional[float]:
    """Linear-interpolated quantile of an ascending finite list."""
    if not sorted_values:
        return None
    raw_position = (len(sorted_values) - 1) * fraction
    lower_index = math.floor(raw_position)
    upper_index = math.ceil(raw_position)
    weight = raw_position - lower_index
    lower_value = sorted_values[lower_index]
    upper_value = sorted_values[upper_index]
    return lower_value + (upper_value - lower_value) * weight


def _distribution(values: Sequence[float]) -> dict[str, Optional[float]]:
    """Compact distribution for latency/tokens: p50/p90/p95 + mean."""
    finite_values = sorted(value for value in values if math.isfinite(value))
    return {
        "mean": _mean(finite_values),
        "p50": _quantile(finite_values, 0.5),
        "p90": _quantile(finite_values, 0.9),
        "p95": _quantile(finite_values, 0.95),
    }


def _metric_mean(events: Sequence[TrialEvent], metric: str) -> Optional[float]:
    """Mean for a metric over events, skipping undefined metric values."""
    values: list[float] = []
    for event in events:
        value = extract_metric_value(event, metric)
        if value is not None:
            values.append(value)
    return _mean(values)


def _token_mean(events: Sequence[TrialEvent], token_key: str) -> Optional[float]:
    """Mean token count for a token_usage key."""
    values = [
        float(event.token_usage[token_key])
        for event in events
        if token_key in event.token_usage and isinstance(event.token_usage[token_key], int)
    ]
    return _mean(values)


def _counts(values: Sequence[str]) -> dict[str, int]:
    """Sorted frequency map for stable JSON output."""
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return {key: counts[key] for key in sorted(counts)}


def _aggregate_dict(aggregate: Aggregate) -> dict[str, object]:
    """JSON-ready aggregate using the same shape as the exec report."""
    return AggregationEngine._aggregate_to_dict(aggregate)


def _normal_ci_from_values(values: Sequence[float], method: str) -> Optional[dict[str, object]]:
    """Small normal-approx CI over already-derived values, used for paired deltas."""
    finite_values = [value for value in values if math.isfinite(value)]
    if not finite_values:
        return None
    point = sum(finite_values) / len(finite_values)
    if len(finite_values) == 1:
        return {"point": point, "low": point, "high": point, "method": method}
    variance = sum((value - point) ** 2 for value in finite_values) / (len(finite_values) - 1)
    standard_error = math.sqrt(variance / len(finite_values))
    half_width = 1.96 * standard_error
    return {
        "point": point,
        "low": point - half_width,
        "high": point + half_width,
        "method": method,
    }


def _model_item_metric_means(
    events: Sequence[TrialEvent],
    metric: str,
) -> dict[str, dict[str, float]]:
    """Per-model per-item means for paired model deltas."""
    buckets: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for event in events:
        value = extract_metric_value(event, metric)
        if value is None:
            continue
        buckets[event.model][event.item_id].append(value)
    out: dict[str, dict[str, float]] = {}
    for model, item_values in buckets.items():
        out[model] = {}
        for item_id, values in item_values.items():
            mean_value = _mean(values)
            if mean_value is not None:
                out[model][item_id] = mean_value
    return out


def _paired_deltas(events: Sequence[TrialEvent], metric: str = "composite") -> list[dict[str, object]]:
    """Pairwise model deltas over shared item means."""
    by_model = _model_item_metric_means(events, metric)
    models = sorted(by_model)
    out: list[dict[str, object]] = []
    for left_index, model_a in enumerate(models):
        for model_b in models[left_index + 1:]:
            shared_items = sorted(set(by_model[model_a]) & set(by_model[model_b]))
            deltas = [by_model[model_a][item_id] - by_model[model_b][item_id] for item_id in shared_items]
            ci = _normal_ci_from_values(deltas, "paired_normal_approx")
            if ci is None:
                continue
            out.append(
                {
                    "model_a": model_a,
                    "model_b": model_b,
                    "metric": metric,
                    "shared_items": len(shared_items),
                    "delta_ci": ci,
                    "winner": model_a if ci["point"] > 0 else model_b if ci["point"] < 0 else None,
                }
            )
    return out


def _retrieval_regressions(events: Sequence[TrialEvent], limit: int = 12) -> list[dict[str, object]]:
    """Examples where retrieval appears strong but final quality is poor."""
    rows: list[dict[str, object]] = []
    for event in events:
        recall = extract_metric_value(event, "recall_at_k")
        ndcg = extract_metric_value(event, "ndcg_at_k")
        composite = extract_metric_value(event, "composite")
        if recall is None or ndcg is None or composite is None:
            continue
        retrieval_strength = max(recall, ndcg)
        if retrieval_strength < 0.75 or composite >= 0.35:
            continue
        rows.append(
            {
                "trial_id": event.trial_id,
                "model": event.model,
                "item_id": event.item_id,
                "answerability": event.answerability,
                "composite": composite,
                "recall_at_k": recall,
                "ndcg_at_k": ndcg,
                "latency_ms": event.timings.end_to_end_ms,
                "query": event.query[:260],
                "answer_excerpt": event.answer_text[:360],
            }
        )
    rows.sort(
        key=lambda row: (
            -(float(row["recall_at_k"]) + float(row["ndcg_at_k"])),
            float(row["composite"]),
            str(row["model"]),
            str(row["item_id"]),
        )
    )
    return rows[:limit]


def _quality_latency_points(events: Sequence[TrialEvent]) -> list[dict[str, object]]:
    """Per-trial (latency, quality) points for the decision-surface scatter cloud.

    Each judged trial that has BOTH a composite quality and an end-to-end latency becomes
    one point — so the decision surface plots the real distribution (every judged trial),
    not just the two aggregate per-model centroids. The view overlays the centroids on top.
    """
    points: list[dict[str, object]] = []
    for event in events:
        composite = extract_metric_value(event, "composite")
        latency = getattr(getattr(event, "timings", None), "end_to_end_ms", None)
        if composite is None or latency is None:
            continue
        points.append(
            {
                "model": event.model,
                "item_id": event.item_id,
                "answerability": event.answerability,
                "composite": float(composite),
                "latency_ms": float(latency),
            }
        )
    return points


def _events_with_phase2_judge(
    events: Sequence[TrialEvent],
    judge_records: Sequence[object],
) -> list[TrialEvent]:
    """Return outcome events enriched with Phase-2 judge scores by trial_id."""
    judge_by_trial = {
        getattr(record, "trial_id"): record
        for record in judge_records
        if getattr(record, "trial_id", None)
    }
    enriched_events: list[TrialEvent] = []
    for event in events:
        judge_record = judge_by_trial.get(event.trial_id)
        if judge_record is None:
            continue
        judge = getattr(judge_record, "judge")
        quality = dataclasses.replace(
            event.quality,
            judge=judge,
            composite=compute_composite(event.quality.accuracy, judge, config.COMPOSITE_WEIGHTS),
            composite_weights_version=config.COMPOSITE_WEIGHTS_VERSION,
        )
        enriched_events.append(dataclasses.replace(event, quality=quality))
    return enriched_events


def _model_cards(
    events: Sequence[TrialEvent],
    quality_events: Sequence[TrialEvent],
) -> list[dict[str, object]]:
    """Per-model decision cards with quality, timing, token, and component evidence."""
    by_model: dict[str, list[TrialEvent]] = defaultdict(list)
    for event in events:
        by_model[event.model].append(event)
    quality_by_model: dict[str, list[TrialEvent]] = defaultdict(list)
    for event in quality_events:
        quality_by_model[event.model].append(event)
    aggregate_by_model = {
        aggregate.group["model"]: _aggregate_dict(aggregate)
        for aggregate in live_aggregates(quality_events, ["model"], "composite")
    } if quality_events else {}
    cards: list[dict[str, object]] = []
    for model in sorted(by_model):
        model_events = by_model[model]
        model_quality_events = quality_by_model.get(model, [])
        component_events = model_quality_events if model_quality_events else model_events
        timing = {
            timing_field: _distribution(
                [float(getattr(event.timings, timing_field)) for event in model_events]
            )
            for timing_field in BAKEOFF_TIMING_FIELDS
        }
        token_usage = {
            "prompt": _token_mean(model_events, "prompt"),
            "completion": _token_mean(model_events, "completion"),
            "total": _token_mean(model_events, "total"),
        }
        component_means = {
            metric: _metric_mean(component_events, metric)
            for metric in BAKEOFF_COMPONENT_METRICS
        }
        cards.append(
            {
                "model": model,
                "n_trials": len(model_events),
                "n_items": len({event.item_id for event in model_events}),
                "n_quality_trials": len(model_quality_events),
                "n_quality_items": len({event.item_id for event in model_quality_events}),
                "quality": aggregate_by_model.get(model),
                "timing": timing,
                "token_usage_mean": token_usage,
                "component_means": component_means,
                "answerability_counts": _counts([event.answerability for event in model_events]),
                "turn_type_counts": _counts([event.turn_type for event in model_events]),
            }
        )
    return cards


def build_bakeoff_diagnostics(
    events: Sequence[TrialEvent],
    judge_records: Sequence[object] = (),
) -> dict[str, object]:
    """Build the Bake-Off decision payload from the clean outcomes log."""
    events = list(events)
    judge_records = list(judge_records)
    judged_events = _events_with_phase2_judge(events, judge_records)
    quality_events = judged_events if judged_events else events
    quality_source = "phase2_judge_scores" if judged_events else "outcomes_composite"
    engine = AggregationEngine()
    cohort_slices: dict[str, list[dict[str, object]]] = {}
    for dimension in BAKEOFF_COHORT_DIMENSIONS:
        cohort_slices[dimension] = [
            _aggregate_dict(aggregate)
            for aggregate in live_aggregates(quality_events, ["model", dimension], "composite")
        ] if quality_events else []

    model_cards = _model_cards(events, quality_events)
    timing_stages = []
    for card in model_cards:
        model = str(card["model"])
        timing = card["timing"]
        timing_stages.append(
            {
                "model": model,
                **{
                    timing_field: (timing[timing_field] or {}).get("mean")
                    for timing_field in BAKEOFF_TIMING_FIELDS
                },
            }
        )

    return {
        "source": {
            "success_store_only": True,
            "total_trials": len(events),
            "total_items": len({event.item_id for event in events}),
            "quality_source": quality_source,
            "quality_trials": len(quality_events),
            "quality_items": len({event.item_id for event in quality_events}),
            "judge_scores_total": len(judge_records),
            "judge_scores_joined": len(judged_events),
            "composite_weights_version": config.COMPOSITE_WEIGHTS_VERSION,
            "models": sorted({event.model for event in events}),
            "passes": _counts([event.pass_name for event in events]),
            "answerability": _counts([event.answerability for event in events]),
            "turn_type": _counts([event.turn_type for event in events]),
            "schema_version": sorted({event.schema_version for event in events}),
        },
        "model_cards": model_cards,
        "paired_deltas": _paired_deltas(quality_events),
        "cohort_slices": cohort_slices,
        "timing_stages": timing_stages,
        "high_variance": [flag.to_dict() for flag in engine.flag_high_variance(quality_events)[:20]],
        "retrieval_regressions": _retrieval_regressions(quality_events),
        "quality_latency": _quality_latency_points(quality_events),
    }


# ---------------------------------------------------------------------------
# Exec report serving (Property 10: refuse a CI-less number)
# ---------------------------------------------------------------------------
def _iter_report_aggregates(report: dict):
    """Yield every aggregate-shaped dict in a materialized report (for P10 checks)."""
    yield from report.get("by_model", []) or []
    yield from report.get("safety", []) or []
    for cells in (report.get("cohort_heatmaps", {}) or {}).values():
        yield from cells or []


def _report_ci_violations(report: dict) -> list[str]:
    """Return P10 violations: any aggregate lacking a CI without being marked thin.

    Property 10 / Req 11.1: every number reaching the exec viz carries a CI or is
    explicitly marked ``insufficient_data``. The invariant is the exclusive-or
    ``(mean_ci is None) == insufficient_data``. A violation is either a *bare
    number* (``mean_ci`` null but not marked insufficient) or a contradictory cell
    (a CI present yet flagged insufficient). Every ``frontier`` point must also
    carry a non-null quality CI.
    """
    violations: list[str] = []
    for agg in _iter_report_aggregates(report):
        has_ci = agg.get("mean_ci") is not None
        insufficient = bool(agg.get("insufficient_data"))
        if has_ci == insufficient:  # must be exclusive-or
            violations.append(
                f"aggregate group={agg.get('group')!r} metric={agg.get('metric')!r}: "
                f"mean_ci present={has_ci}, insufficient_data={insufficient} "
                f"(P10 requires exactly one)"
            )
    for fp in report.get("frontier", []) or []:
        if fp.get("quality") is None:
            violations.append(
                f"frontier point model={fp.get('model')!r} lacks a quality CI (P10)"
            )
    return violations


def _resolve_report_path(reports_dir: Path, plan_version: Optional[str]) -> Optional[Path]:
    """Resolve which ``aggregate_<plan_version>.json`` to serve (newest if unspecified)."""
    if plan_version:
        candidate = reports_dir / f"aggregate_{plan_version}.json"
        return candidate if candidate.exists() else None
    if not reports_dir.exists():
        return None
    candidates = sorted(
        reports_dir.glob("aggregate_*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
def create_app(
    *,
    broker: Optional[SSEBroker] = None,
    controller: Optional[RunController] = None,
    events_path: Path = config.TRIAL_EVENTS_PATH,
    reports_dir: Path = config.REPORTS_DIR,
    dist_dir: Path = DEFAULT_DIST_DIR,
    dev_cors: bool = True,
    host: str = config.UI_HOST,
    port: int = config.UI_PORT,
    judge_scores_path: Path = config.JUDGE_SCORES_PATH,
    start_cred_refresh: bool = False,
    eval_events_path: Path = DEFAULT_EVAL_EVENTS_PATH,
    eval_prompts_path: Path = DEFAULT_EVAL_PROMPTS_PATH,
) -> FastAPI:
    """Construct the harness FastAPI app, bound (by config) to loopback only.

    All collaborators are injectable so the app is testable against a temp event
    log / reports dir and a pre-seeded :class:`~bakeoff.runner.RunController`
    without starting a server or touching real harness output.

    Args:
        broker: the SSE broker (a fresh :class:`SSEBroker` if ``None``).
        controller: an active run controller to expose (``None`` → idle).
        events_path: the trial-event log the live ``/api/*`` routes read.
        reports_dir: where materialized exec reports live (``/exec/*``).
        dist_dir: the built SPA bundle; served at ``/`` when it exists.
        dev_cors: enable permissive CORS for the Vite dev origins (dev only).
        host / port: the intended bind target (recorded on ``app.state`` and used
            by :func:`serve`); the default is loopback per Req 15.1.
        judge_scores_path: the Phase-2 judge verdict store joined into Bake-Off
            diagnostics by ``trial_id``.
        start_cred_refresh: when ``True``, spawn the background credential-refresh
            loop on startup. Defaults to ``False`` so building the app in tests (or
            any non-server context) never spawns a real ``ada`` subprocess — that
            blocking, uninterruptible call would otherwise hang TestClient lifespan
            teardown. The real server entrypoint (:func:`serve`) passes ``True``.
    """
    app = FastAPI(
        title="model-bakeoff-harness",
        version="1.0.0",
        summary="Local, loopback-only live-monitoring + exec API for the bakeoff.",
    )
    state = AppState(
        broker=broker,
        controller=controller,
        events_path=events_path,
        reports_dir=reports_dir,
        dist_dir=dist_dir,
        host=host,
        port=port,
        judge_scores_path=judge_scores_path,
        eval_events_path=eval_events_path,
        eval_prompts_path=eval_prompts_path,
    )
    app.state.bakeoff = state
    # Mirror the bind target onto app.state for tests / introspection (Req 15.1).
    app.state.host = host
    app.state.port = port

    # ---- Background credential refresh loop (credential-expiry resilience) ----
    # Proactively keeps every broker-known profile fresh so a long optimizer run
    # never hits ExpiredTokenException. Delegates to the centralized credential
    # broker (bakeoff.credentials), which re-runs `ada` under a cross-process lock
    # (so this loop, a CLI run, and any sibling agent coalesce to one real refresh)
    # and binds every session to an explicit named profile rather than ambient env.
    # This is belt-and-suspenders alongside the per-client auth-expiry refresh hook,
    # which now also goes through the broker.
    _CRED_REFRESH_INTERVAL_S: int = 1200  # 20 min — well inside the ~1h token lifetime

    async def _background_cred_refresh() -> None:
        """Proactively refresh every broker-known profile every 20 minutes."""
        import asyncio as _asyncio
        import logging

        from bakeoff.credentials import CredentialRefreshError, get_broker

        log = logging.getLogger("bakeoff.cred_refresh")
        broker = get_broker()
        profiles = sorted(config.CREDENTIAL_PROFILES)
        if not profiles:
            log.warning("no credential profiles configured — background refresh disabled")
            return

        async def _do_refresh() -> None:
            for profile in profiles:
                try:
                    # force=True asks for a real mint, but the broker's cross-process
                    # min-interval still coalesces concurrent refreshers to one ada call.
                    ran = await _asyncio.to_thread(broker.refresh, profile, force=True)
                    log.info("background cred refresh (%s): %s", profile,
                             "minted" if ran else "coalesced (already fresh)")
                except CredentialRefreshError as exc:
                    # A lapsed Midway session is the one case automation can't fix —
                    # surface it loudly and actionably rather than silently.
                    if getattr(exc, "needs_mwinit", False):
                        log.error("background cred refresh (%s): Midway lapsed — run `mwinit`. %s",
                                  profile, exc)
                    else:
                        log.warning("background cred refresh (%s) failed: %s", profile, exc)
                except Exception as exc:  # noqa: BLE001
                    log.warning("background cred refresh (%s) error (non-fatal): %s", profile, exc)

        # Fire immediately on startup so the first run always has fresh creds,
        # then repeat every 20 minutes.
        await _do_refresh()
        while True:
            await _asyncio.sleep(_CRED_REFRESH_INTERVAL_S)
            await _do_refresh()

    @app.on_event("startup")
    async def _install_blocking_io_executor() -> None:
        # Enlarge the event loop's DEFAULT executor so every blocking boto3 call
        # (target generation, Opus judge, Embed, AOSS retrieve — all via
        # asyncio.to_thread) can actually run at its semaphore cap. Python's default
        # (~cpu_count+4 = 18 on this box) bottlenecked generation far below model_cap=24
        # because the judge's blocking calls shared the same ~18 threads. These calls are
        # I/O-bound, so a larger pool is the right fix. Idempotent + best-effort.
        import asyncio as _asyncio
        import logging
        from concurrent.futures import ThreadPoolExecutor

        try:
            loop = _asyncio.get_running_loop()
            loop.set_default_executor(
                ThreadPoolExecutor(
                    max_workers=config.BLOCKING_IO_MAX_WORKERS,
                    thread_name_prefix="gbbo-io",
                )
            )
            logging.getLogger("bakeoff.app").info(
                "blocking-IO executor sized to %d workers", config.BLOCKING_IO_MAX_WORKERS
            )
        except Exception:  # noqa: BLE001 - never block startup on this
            logging.getLogger("bakeoff.app").warning(
                "could not enlarge default executor; using asyncio default", exc_info=True
            )

    @app.on_event("startup")
    async def _start_cred_refresh() -> None:
        # Only the real server (serve()) enables this; building the app in tests
        # leaves it off so no blocking `ada` subprocess is spawned on startup (which
        # would hang TestClient lifespan teardown — the portal thread join waits on
        # the uninterruptible subprocess).
        if not start_cred_refresh:
            return
        import asyncio as _asyncio

        _asyncio.create_task(_background_cred_refresh())

    if dev_cors:
        from fastapi.middleware.cors import CORSMiddleware

        # Dev-only: the Vite dev server (Task 13/14) runs on :5173 and proxies the
        # API. No credentials are allowed (there is no auth/cookie surface).
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(DEV_CORS_ORIGINS),
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # ---- health (harness, distinct from the retrieval backend's /healthz) ----
    @app.get("/healthz")
    def healthz() -> dict:
        """Harness liveness. Distinct from the retrieval backend's own /healthz."""
        return {
            "status": "ok",
            "service": "model-bakeoff-harness",
            "run_status": state.snapshot()["status"],
            "subscribers": state.broker.subscriber_count,
        }

    # ---- live: model list + status + progress (Req 10.1) ----
    @app.get("/api/models")
    def api_models() -> dict:
        """Run status + per-model planned/done/in_flight/errored (idle if no run).

        Also surfaces the AWS account the bake-off's target models run on — the
        broker DEFAULT profile (bake-off candidate adapters bind to it), so the UI
        can show the account without hardcoding it.
        """
        snap = dict(state.snapshot())
        profile = config.CREDENTIAL_DEFAULT_PROFILE
        snap["credential_profile"] = profile
        snap["account"] = (config.CREDENTIAL_PROFILES.get(profile) or {}).get("account")
        return snap

    # ---- live: aggregates with cheap normal-approx CIs (Req 10.4) ----
    @app.get("/api/aggregate")
    def api_aggregate(request: Request, metric: str = "composite") -> dict:
        """Live aggregates for ``metric`` grouped by ``group_by`` (normal-approx CIs).

        Query params: ``group_by`` (repeatable and/or comma-separated; default
        ``model``), ``metric`` (default ``composite``), plus optional cohort /
        identity equality filters (``model``, ``pass``, or any cohort axis).
        """
        group_by = _parse_group_by(request)
        events = read_events(state.events_path)
        events = _apply_cohort_filters(request, events)
        try:
            aggs = live_aggregates(events, group_by, metric)
        except AnswerabilityBlendError as exc:
            # P4: refuse to blend accuracy across answerability — 422, not a number.
            raise HTTPException(status_code=422, detail=str(exc))
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {
            "group_by": group_by,
            "metric": metric,
            "ci_method": "normal_approx",
            "aggregates": [dataclasses.asdict(a) for a in aggs],
        }

    @app.get("/api/bakeoff/diagnostics")
    def api_bakeoff_diagnostics(request: Request) -> dict:
        """Bake-Off decision cockpit data derived from the clean outcomes log.

        This route intentionally reads only successful outcomes plus their
        separate Phase-2 judge enrichments. Execution failures are stored
        separately in ``run_errors.jsonl`` so they cannot pollute
        model-selection numbers; the response's ``source`` block calls that out.
        """
        from bakeoff.judge_phase2 import read_judge_scores

        events = read_events(state.events_path)
        events = _apply_cohort_filters(request, events)
        judge_records = read_judge_scores(state.judge_scores_path)
        return build_bakeoff_diagnostics(events, judge_records)

    # ---- live: SSE stream of trial_completed events (Req 10.3) ----
    @app.get("/api/stream")
    async def api_stream(model: Optional[str] = None) -> StreamingResponse:
        """Stream SSE events as they land (text/event-stream).

        The bake-off uses this with no parameters and sees the unchanged behavior: a
        live fan-out of ``trial_completed`` / ``judge_*`` events with no replay buffer
        (Req 10.3). It ALSO carries the optimizer's ``optimizer_*`` events, which ride
        the same broker (design Component 12 / Req 9.7) — every consumer receives them
        and a Per_Model_View filters to its own model by the ``model_channel`` stamp.

        When a Per_Model_View opens a subscription it passes ``?model=<target_model>``;
        for the lifetime of that subscription the model is marked viewable in the
        :class:`~bakeoff.quality.optimizer.orchestrator.ViewRegistry` (and cleared when
        the connection closes), which is what gates the optimizer's per-model
        concurrency (Req 1.11 / 9.8). The bake-off's parameterless subscription never
        touches the registry, so its streaming is untouched.
        """
        if model is None:
            stream = state.broker.subscribe()
        else:
            stream = _view_scoped_stream(state, model)
        return StreamingResponse(
            stream,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                # Disable proxy buffering so events flush immediately (nginx hint).
                "X-Accel-Buffering": "no",
            },
        )

    # ---- live: replay recent trials from disk so a page reload isn't blank ----
    @app.get("/api/trials/recent")
    def api_trials_recent(limit: int = 2000) -> dict:
        """Return the most recent completed trials from the durable outcomes log.

        The SSE stream (``/api/stream``) has no replay buffer — a browser that
        connects (or reloads) only sees trials that land *after* it connected, so
        the live fleet/latency views would start blank on every reload even though
        thousands of trials are durably on disk. This endpoint lets the dashboard
        **seed its in-memory buffer from disk on load**, so the live view
        reconstructs immediately and then continues from the SSE stream.

        It returns up to ``limit`` of the newest events in the SAME compact shape
        the SSE ``trial_completed`` payload uses (``_summarize``), so the client
        can feed them through the identical code path. Reads the clean outcomes
        store only (successes/decision data); errored trials live in a separate
        store and are not replayed here.
        """
        events = read_events(state.events_path)
        # Newest-first to match the client buffer's ordering; cap to ``limit`` so a
        # very long run doesn't ship the whole log on every reload.
        tail = events[-max(0, limit):] if limit else events
        summaries = [_summarize_event(ev) for ev in reversed(tail)]
        return {"trials": summaries, "total": len(events)}

    @app.get("/api/bakeoff/sessions")
    def api_bakeoff_sessions() -> dict:
        """List Bake-Off sessions with the current active session first."""
        return state.bakeoff_session_snapshot()

    @app.post("/api/bakeoff/sessions")
    async def api_bakeoff_session_create(request: Request) -> JSONResponse:
        """Create a new Bake-Off session and make it active."""
        ctrl = state.controller
        if ctrl is not None and (
            ctrl.status == RunStatus.RUNNING
            or (ctrl.status == RunStatus.PAUSED and not ctrl.auto_paused)
        ):
            raise HTTPException(status_code=409, detail="a run is already active")
        if state.judge_status == JudgeStatus.RUNNING:
            raise HTTPException(status_code=409, detail="judging already in progress")

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            body = {}

        label = body.get("label")
        notes = body.get("notes")
        if label is not None and not isinstance(label, str):
            raise HTTPException(status_code=422, detail="'label' must be a string")
        if notes is not None and not isinstance(notes, str):
            raise HTTPException(status_code=422, detail="'notes' must be a string")

        created_session = state.session_manager.create(label, notes)
        snapshot = state.set_active_bakeoff_session(created_session.id)
        return JSONResponse(snapshot, status_code=201)

    @app.post("/api/bakeoff/sessions/{session_id}/activate")
    def api_bakeoff_session_activate(session_id: str) -> JSONResponse:
        """Switch the active Bake-Off session."""
        ctrl = state.controller
        if ctrl is not None and (
            ctrl.status == RunStatus.RUNNING
            or (ctrl.status == RunStatus.PAUSED and not ctrl.auto_paused)
        ):
            raise HTTPException(status_code=409, detail="a run is already active")
        if state.judge_status == JudgeStatus.RUNNING:
            raise HTTPException(status_code=409, detail="judging already in progress")

        try:
            snapshot = state.set_active_bakeoff_session(session_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown session {session_id!r}")
        except ValueError:
            raise HTTPException(status_code=409, detail=f"session {session_id!r} is archived")
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return JSONResponse(snapshot, status_code=200)

    @app.patch("/api/bakeoff/sessions/{session_id}")
    async def api_bakeoff_session_update(session_id: str, request: Request) -> JSONResponse:
        """Update a Bake-Off session's label, notes, or archived state."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            body = {}

        label = body.get("label") if "label" in body else None
        notes = body.get("notes") if "notes" in body else None
        archived = body.get("archived") if "archived" in body else None
        if label is not None and not isinstance(label, str):
            raise HTTPException(status_code=422, detail="'label' must be a string")
        if notes is not None and not isinstance(notes, str):
            raise HTTPException(status_code=422, detail="'notes' must be a string")
        if archived is not None and not isinstance(archived, bool):
            raise HTTPException(status_code=422, detail="'archived' must be a boolean")

        try:
            state.session_manager.update(
                session_id,
                label=label,
                notes=notes,
                archived=archived,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown session {session_id!r}")
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

        return JSONResponse(state.bakeoff_session_snapshot(), status_code=200)

    # ---- control: pause / resume / abort the active run (Req 10.5) ----
    @app.post("/api/control/{action}")
    def api_control(action: str) -> dict:
        """Drive the active run's pause/resume/abort hooks; return the new status."""
        if action not in _CONTROL_ACTIONS:
            raise HTTPException(
                status_code=404,
                detail=f"unknown control action {action!r}; expected one of "
                f"{sorted(_CONTROL_ACTIONS)}",
            )
        if state.controller is None:
            # No active run — a clear, non-failing signal the UI can render.
            raise HTTPException(status_code=409, detail="no active run to control")
        getattr(state.controller, action)()
        return {"action": action, "status": state.controller.status, **state.controller.snapshot()}

    # ---- run: kick off a flat fixed-rep run from the browser (no script) ----
    @app.post("/api/run/start")
    async def api_run_start(request: Request) -> JSONResponse:
        """Start a flat fixed-rep run (every item × every model × ``reps`` WIDE reps).

        Request body (JSON; all optional):
        ``{"reps": 3, "temperature": 0.2, "max_trials": null}``. Defaults: ``reps``
        = 3, ``temperature`` = :data:`config.DEFAULT_TEMPERATURE`, ``max_trials`` =
        ``None`` (no clamp).

        Returns **202** with the run snapshot (same shape as ``GET /api/models``)
        once the run is launched as a background task. If a run is already active
        (``running`` or ``paused``), returns **409** with
        ``{"detail": "a run is already active"}`` and starts nothing.

        All boto3-touching collaborators (the candidate adapters, the retrieval
        client, the scoring pipeline, the dataset loader) and the planner are
        imported lazily *inside* this handler so importing :mod:`bakeoff.app` stays
        network-free and free of a hard backend dependency (mirrors
        :mod:`bakeoff.main`'s lazy ``_default_*`` wiring).
        """
        # Reject only a genuinely LIVE run. A run that auto-paused (the error-rate
        # gate tripped, e.g. a credential blip) has DRAINED — its worker pool exited
        # and schedule_run returned — so it is resumable: re-launching diffs the
        # append-only outcomes log and runs only the missing/errored trials. A
        # MANUAL pause (paused by a human, auto_paused=False) keeps workers parked
        # and the run task alive, so it must NOT be clobbered here (resume it via
        # /api/control/resume instead). So: reject RUNNING, and reject a manual
        # pause; allow a (re)start when idle, completed, aborted, or auto-paused.
        ctrl = state.controller
        if ctrl is not None and (
            ctrl.status == RunStatus.RUNNING
            or (ctrl.status == RunStatus.PAUSED and not ctrl.auto_paused)
        ):
            return JSONResponse(
                {"detail": "a run is already active"}, status_code=409
            )

        # Parse the optional JSON body (tolerate an empty / missing body).
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        reps = int(body.get("reps", 3))
        temperature = float(body.get("temperature", config.DEFAULT_TEMPERATURE))
        max_trials = body.get("max_trials", None)
        if max_trials is not None:
            max_trials = int(max_trials)
        # Optional PHASE filter: restrict this run to candidates of the given
        # invocation method(s) so the two methods run in SEPARATE phases and their
        # latency is measured without cross-method worker-pool contention. Accepts
        # a string or list, e.g. {"methods": "inline_agent"} or
        # {"methods": ["converse"]}. None/absent => all enabled candidates.
        methods = body.get("methods", None)
        if isinstance(methods, str):
            methods = [methods]
        if methods is not None:
            methods = [str(m) for m in methods]
        # Optional hard cap on concurrent in-flight trials for this phase (lets the
        # operator throttle a buffered method like inline so it doesn't oversubscribe).
        max_concurrency = body.get("max_concurrency", None)
        if max_concurrency is not None:
            max_concurrency = int(max_concurrency)

        # Lazy, boto3-touching imports (kept inside the handler — see docstring).
        from bakeoff.adapters.bedrock import build_candidate_adapters
        from bakeoff.dataset import DatasetLoader
        from bakeoff.planner import SamplingPlanner
        from bakeoff.retrieval_client import RetrievalClient
        from bakeoff.scoring.pipeline import ScoringPipeline

        # Dataset load can be slow + blocking — run it off the event loop.
        items = await asyncio.to_thread(DatasetLoader(config.DATASET_DIR).load_items)

        # A flat plan: every item × reps WIDE reps (no pilot, no DEEP, no cap).
        plan = SamplingPlanner().flat_plan(items, reps=reps, temperature=temperature)
        if max_trials is not None:
            # SamplingPlan is frozen but budget is a mutable dict — clamp in place.
            plan.budget["max_trials"] = min(
                plan.budget.get("max_trials", max_trials), max_trials
            )

        models = build_candidate_adapters(methods=methods)
        retr = RetrievalClient()
        # PHASE 1 scoring only: local, pure-CPU scorers (retrieval-aligned ranking
        # + answerability). NO judge (Opus) and NO semantic (Embed-v4) in the
        # generation hot loop, so the only Bedrock surface is the candidates + the
        # held-constant retrieval. The deferred Phase-2 judge scores a sampled
        # subset later, keyed by trial_id — see scripts/judge.sh.
        scoring = ScoringPipeline.generation_phase()

        # Two-store split: successes → outcomes (state.events_path), errored trials
        # → the disposable run-errors store, so execution failures never pollute
        # the decision data.
        #
        # PHASED RUNS: auto-chaining Phase-2 judging on completion is suppressed
        # when this run is a single method phase (a ``methods`` filter is set), so
        # judging waits until the filtered phase has populated the outcomes store.
        # A full run (no methods filter) keeps the auto-chain.
        start_kwargs: dict = {"errors_path": state.run_errors_path}
        if max_concurrency is not None:
            start_kwargs["max_concurrency"] = max_concurrency
        auto_judge = bool(body.get("auto_judge", methods is None))
        await state.start_run(
            plan, models, items=items, retr=retr, scoring=scoring,
            auto_judge=auto_judge, **start_kwargs,
        )
        return JSONResponse(state.snapshot(), status_code=202)

    # ---- judge (Phase 2): status / start (re-run) / scores ----
    @app.get("/api/judge/status")
    def api_judge_status() -> dict:
        """The Phase-2 judge lifecycle (idle/running/completed/failed) + progress."""
        return state.judge_snapshot()

    @app.post("/api/judge/start")
    async def api_judge_start(request: Request) -> JSONResponse:
        """Kick off (or re-run) the deferred Phase-2 judge over the outcomes.

        Request body (JSON; all optional): ``{"items_per_model": 166}`` — the
        single sample dial (how many items per model the judge grades; the run
        sizes to ~3k Opus attempts by default). Returns **202** with the judge
        snapshot once launched, or **409** ``{"detail": "judging already in
        progress"}`` if a pass is already running.

        The judge reads only the clean outcomes store and writes its own separate
        judge-scores store, so re-running it never touches the candidate decision
        data. It is resumable: a re-run judges only not-yet-judged sampled trials,
        unless the sample dial is raised (then it judges the newly-included ones).
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        items_per_model = body.get("items_per_model", None)
        if items_per_model is not None:
            items_per_model = int(items_per_model)

        launched = await state.start_judge(items_per_model=items_per_model)
        if not launched:
            return JSONResponse(
                {"detail": "judging already in progress"}, status_code=409
            )
        return JSONResponse(state.judge_snapshot(), status_code=202)

    @app.get("/api/judge/scores")
    def api_judge_scores(refresh: bool = False) -> dict:
        """Per-model judge rollups + example verdicts for the dashboard's judge view.

        Returns the cached summary built when the last pass finished. Pass
        ``?refresh=true`` to recompute it from the judge-scores store on disk
        (useful right after a pass, or to pick up an out-of-band judge run). When
        no verdicts exist yet, returns an empty-but-well-formed summary.
        """
        if refresh or state.judge_summary is None:
            from bakeoff.judge_phase2 import read_judge_scores, summarize_judge_scores

            records = read_judge_scores(state.judge_scores_path)
            state.judge_summary = summarize_judge_scores(records)
        return state.judge_summary

    # ---- quality study (separate, self-contained): per-turn closeness summary ----
    @app.get("/api/quality/summary")
    def api_quality_summary() -> dict:
        """Per-model, per-turn closeness rollup for the dashboard's Quality tab.

        Reads the SEPARATE quality outcomes store (never the bake-off's) and
        returns the turn-drift curve + gold/wants/abstention split + example
        conversations. Recomputed from disk each call (the store is small and the
        tab is viewed on demand). Empty-but-well-formed when no quality run exists.
        """
        from bakeoff.quality.summary import summarize_quality

        return summarize_quality()

    # ---- quality study: closed-loop prompt optimizer (additive; design Component 12) ----
    @app.post("/api/quality/optimize/start")
    async def api_optimize_start(request: Request) -> JSONResponse:
        """Start the closed-loop prompt optimizer (champion/challenger loop).

        Request body (JSON; all optional):
        ``{"backend": "offline"|"live", "models": [...], "threshold": 0.05,
        "stop_limit": 5, "phase_a_reps": 3, "phase_b_reps": 5,
        "retrieval_backend": "opensearch"|"local"|"fake", "force": false}``.
        Defaults: ``backend`` = ``offline``, ``models`` = the two fixed Target_Models
        (:data:`config.QUALITY_MODELS`), every override = its ``config`` default, and
        ``force`` = ``false``.

        Returns **202** with the optimizer snapshot once the loop is launched as a
        background task. If an optimizer run is already active, returns **409**
        ``{"detail": "optimizer already running"}`` and starts nothing.

        **Loopback / no-auth posture (Req 12.5/12.6).** This is a loopback-only, no-auth
        research endpoint, identical in posture to the rest of the harness: the app binds
        to loopback only and :func:`serve` refuses a non-loopback bind without an explicit
        auth-added override (Req 15.2), so this additive route inherits that enforcement
        and widens no exposure.

        **Live safety (Req 10.7).** When ``backend == "live"`` and a bake-off run looks
        active (its outcomes file was written recently), the route refuses with **409**
        unless ``force`` is set, so the Quality_Study never silently contends with the
        bake-off for the shared Opus judge quota — mirroring the CLI's ``--force`` guard
        (``bakeoff.quality.main._bakeoff_run_looks_active``).

        **Author/Judge conflict (Req 4.2).** A live run whose configured Author and Judge
        resolve to the same model is rejected with **409** (a clean client error), not a
        500 — the conflict is surfaced from ``build_live_backend`` synchronously before the
        background task is created.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            body = {}

        backend = str(body.get("backend", "offline")).strip().lower()
        if backend not in ("offline", "live"):
            raise HTTPException(
                status_code=422,
                detail=f"unknown backend {backend!r}; expected 'offline' or 'live'",
            )

        models = body.get("models", None)
        if models is None:
            models = list(config.QUALITY_MODELS.keys())
        elif isinstance(models, str):
            models = [models]
        else:
            models = [str(m) for m in models]
        # Only the two fixed Target_Models are valid (Req 12.3) — reject anything else
        # with a clean 422 rather than launching a loop over an unknown model.
        unknown = [m for m in models if m not in config.QUALITY_MODELS]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"unknown target model(s) {unknown}; the quality optimizer runs only "
                    f"on {sorted(config.QUALITY_MODELS)} (Req 12.3)"
                ),
            )

        force = bool(body.get("force", False))
        threshold = body.get("threshold", None)
        if threshold is not None:
            threshold = float(threshold)
        stop_limit = body.get("stop_limit", None)
        if stop_limit is not None:
            stop_limit = int(stop_limit)
        phase_a_reps = body.get("phase_a_reps", None)
        if phase_a_reps is not None:
            phase_a_reps = int(phase_a_reps)
        phase_b_reps = body.get("phase_b_reps", None)
        if phase_b_reps is not None:
            phase_b_reps = int(phase_b_reps)
        retrieval_backend = body.get("retrieval_backend", None)
        if retrieval_backend is not None:
            retrieval_backend = str(retrieval_backend).strip().lower()

        # Live safety: refuse while a bake-off run looks active unless forced (Req 10.7).
        # Reuse the CLI's exact heuristic so the dashboard and CLI guard identically.
        if backend == "live" and not force:
            from bakeoff.quality.main import _bakeoff_run_looks_active

            if _bakeoff_run_looks_active():
                return JSONResponse(
                    {
                        "detail": (
                            "a bake-off run looks active (outcomes written in the last "
                            "2 min); refusing --backend live to avoid contending for the "
                            "shared Opus judge quota. Retry with force=true once it is done."
                        )
                    },
                    status_code=409,
                )

        # Author/Judge separation is enforced inside build_live_backend; surface the
        # conflict as a clean 4xx instead of a background-task 500 (Req 4.2).
        from bakeoff.quality.optimizer.backends import AuthorJudgeConflictError

        try:
            launched = await state.start_optimizer(
                backend=backend,
                models=models,
                threshold=threshold,
                stop_limit=stop_limit,
                phase_a_reps=phase_a_reps,
                phase_b_reps=phase_b_reps,
                retrieval_backend=retrieval_backend,
                force=force,
            )
        except AuthorJudgeConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

        if not launched:
            return JSONResponse(
                {"detail": "optimizer already running"}, status_code=409
            )
        return JSONResponse(state.optimizer_snapshot(), status_code=202)

    @app.get("/api/quality/optimize/status")
    def api_optimize_status() -> dict:
        """The optimizer run lifecycle + per-model phase/iteration/champion progress.

        Returns the in-memory lifecycle (``idle``/``running``/``completed``/``failed``)
        and launch request, plus the durable per-model progress reconstructed from the
        append-only optimizer stores (latest iteration index, phase, champion triad + CI,
        convergence). Empty-but-well-formed before any optimizer run exists.
        """
        return state.optimizer_snapshot()

    @app.get("/api/quality/optimize/history")
    def api_optimize_history(model: str) -> dict:
        """Ordered prompt-version history for ``model`` (diffs, scores, accept/reject).

        Reads the append-only audit store via
        :meth:`~bakeoff.quality.optimizer.store.OptimizerStore.prompt_version_history`
        and returns the ordered sequence of prompt versions — each with its diff against
        the prior version, the challenger's triad score + CI, and the accept/reject
        decision — supporting a lookback of at least several versions (Req 8.5). ``model``
        is required and must be one of the two fixed Target_Models (Req 12.3); an unknown
        model is rejected with **422**.
        """
        if model not in config.QUALITY_MODELS:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"unknown target model {model!r}; the quality optimizer runs only on "
                    f"{sorted(config.QUALITY_MODELS)} (Req 12.3)"
                ),
            )
        from bakeoff.quality.optimizer.store import OptimizerStore

        store = OptimizerStore()
        versions = store.prompt_version_history(model)
        return {
            "model": model,
            "versions": [dataclasses.asdict(v) for v in versions],
        }

    # ---- optimizer v2 (island-tournament): own start / status / stream ----
    @app.post("/api/quality/optimize/v2/start")
    async def api_optimize_v2_start(request: Request) -> JSONResponse:
        """Start the v2 island-tournament optimizer (its own launch path + broker).

        Request body (JSON; all optional): ``{"backend": "offline"|"live",
        "models": [...], "retrieval_backend": "opensearch"|"local"|"fake"}``.
        Defaults: ``backend`` = ``offline``, ``models`` = the two ``config.QUALITY_MODELS``.
        Returns **202** + the v2 snapshot once launched; **409** if a v2 run is already
        active or the Author/Judge resolve to the same model; **422** for an unknown
        model or backend.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            body = {}

        backend = str(body.get("backend", "offline")).strip().lower()
        if backend not in ("offline", "live"):
            raise HTTPException(
                status_code=422,
                detail=f"unknown backend {backend!r}; expected 'offline' or 'live'",
            )

        models = body.get("models", None)
        if models is None:
            models = list(config.QUALITY_MODELS.keys())
        elif isinstance(models, str):
            models = [models]
        else:
            models = [str(m) for m in models]
        unknown = [m for m in models if m not in config.QUALITY_MODELS]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"unknown target model(s) {unknown}; the quality optimizer runs only "
                    f"on {sorted(config.QUALITY_MODELS)} (Req 12.3)"
                ),
            )

        retrieval_backend = body.get("retrieval_backend", None)
        if retrieval_backend is not None:
            retrieval_backend = str(retrieval_backend).strip().lower()

        from bakeoff.quality.optimizer.backends import AuthorJudgeConflictError

        try:
            launched = await state.start_optimizer_v2(
                backend=backend, models=models, retrieval_backend=retrieval_backend
            )
        except AuthorJudgeConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

        if not launched:
            return JSONResponse(
                {"detail": "optimizer v2 already running"}, status_code=409
            )
        return JSONResponse(state.optimizer_v2_snapshot(), status_code=202)

    @app.get("/api/quality/optimize/v2/status")
    def api_optimize_v2_status() -> dict:
        """The v2 lifecycle + per-island/per-tournament-round progress from the store."""
        return state.optimizer_v2_snapshot()

    @app.get("/api/quality/optimize/v2/stream")
    async def api_optimize_v2_stream(model: Optional[str] = None) -> StreamingResponse:
        """SSE stream of the v2 island/tournament/migration events (dedicated broker).

        Separate from the bake-off ``/api/stream``: it drains ``optimizer_v2_broker``.
        When a ``model`` param is supplied it rides the existing ``_view_scoped_stream``
        pattern so the model is marked viewable for the subscription's lifetime (the
        concurrency gate keeps working); otherwise a plain subscribe.
        """
        if model is None:
            stream = state.optimizer_v2_broker.subscribe()
        else:
            stream = _view_scoped_stream(state, model, broker=state.optimizer_v2_broker)
        return StreamingResponse(
            stream,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/quality/optimize/v2/reset")
    async def api_optimize_v2_reset() -> JSONResponse:
        """Stop the active v2 run (if any), reset lifecycle, and clear the v2 stores.

        The dashboard's one-button "stop + reset + clear" for re-running. Idempotent:
        returns **200** with the post-reset (idle, empty) snapshot whether or not a run
        was active.
        """
        snapshot = await state.reset_optimizer_v2()
        return JSONResponse(snapshot, status_code=200)

    @app.post("/api/quality/optimize/v2/resume")
    async def api_optimize_v2_resume() -> JSONResponse:
        """Resume a failed or stopped v2 run from its last durable checkpoint.

        Unlike ``/reset`` (which truncates the stores), this preserves all durable
        iteration + audit records and re-launches the run task so the orchestrator
        picks up where it left off via ``_restore_or_seed_islands``. The same
        backend/models from the previous request are reused.

        Returns **200** with the new snapshot if the resume launched, or **409** if
        the run is already active or there is no previous request to resume from.
        """
        launched, snapshot = await state.resume_optimizer_v2()
        if not launched:
            if state.optimizer_v2_status == "running":
                return JSONResponse(
                    {"detail": "optimizer v2 already running"}, status_code=409
                )
            return JSONResponse(
                {"detail": "no previous v2 run to resume (no request recorded)"},
                status_code=409,
            )
        return JSONResponse(snapshot, status_code=200)

    # ======================================================================
    # Optimizer V3 routes — hardened, LIVE-ONLY (bakeoff/quality/optimizer/v3/).
    # Same surface shape as /v2 so the UI components are reusable; no offline
    # backend exists for v3 (a request for one is a 422).
    # ======================================================================
    @app.post("/api/quality/optimize/v3/start")
    async def api_optimize_v3_start(request: Request) -> JSONResponse:
        """Start the v3 hardened optimizer (live backend only; its own broker/stores).

        Request body (JSON; all optional): ``{"models": [...],
        "retrieval_backend": "opensearch"|"local"|"fake"}``. ``backend`` is accepted
        but must be ``"live"`` when present — v3 has NO offline mode (422 otherwise).
        Returns **202** + the v3 snapshot once launched; **409** if already running;
        **422** for an unknown model or a non-live backend.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            body = {}

        requested_backend = str(body.get("backend", "live")).strip().lower()
        if requested_backend != "live":
            raise HTTPException(
                status_code=422,
                detail=(
                    f"optimizer v3 is live-only; got backend {requested_backend!r} "
                    "(no offline functionality exists for v3)"
                ),
            )

        models = body.get("models", None)
        if models is None:
            models = list(config.QUALITY_MODELS.keys())
        elif isinstance(models, str):
            models = [models]
        else:
            models = [str(m) for m in models]
        unknown = [m for m in models if m not in config.QUALITY_MODELS]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"unknown target model(s) {unknown}; the v3 optimizer runs only "
                    f"on {sorted(config.QUALITY_MODELS)}"
                ),
            )

        retrieval_backend = body.get("retrieval_backend", None)
        if retrieval_backend is not None:
            retrieval_backend = str(retrieval_backend).strip().lower()

        turn_mode = str(body.get("turn_mode", "multi")).strip().lower()
        if turn_mode not in ("single", "multi", "both"):
            raise HTTPException(
                status_code=422,
                detail=f"turn_mode must be one of single|multi|both; got {turn_mode!r}",
            )

        from bakeoff.quality.optimizer.backends import AuthorJudgeConflictError

        try:
            launched = await state.start_optimizer_v3(
                models=models, retrieval_backend=retrieval_backend, turn_mode=turn_mode
            )
        except AuthorJudgeConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

        if not launched:
            return JSONResponse(
                {"detail": "optimizer v3 already running"}, status_code=409
            )
        return JSONResponse(state.optimizer_v3_snapshot(), status_code=202)

    @app.get("/api/quality/optimize/v3/status")
    def api_optimize_v3_status() -> dict:
        """The v3 lifecycle + durable per-island/per-round backfill snapshot."""
        return state.optimizer_v3_snapshot()

    @app.post("/api/quality/optimize/v3/freeze")
    async def api_optimize_v3_freeze(request: Request) -> JSONResponse:
        """Freeze the current champion for (model, island) into its seed file.

        Body: ``{"model": <key>, "island_id": <int>}``. 422 on a bad model/island,
        404 if no champion is recorded for that island yet.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        model = str(body.get("model", "")).strip()
        if model not in config.QUALITY_MODELS:
            raise HTTPException(status_code=422, detail=f"unknown model {model!r}")
        try:
            island_id = int(body.get("island_id"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="island_id must be an integer")
        if not (0 <= island_id < config.QUALITY_OPT_ISLANDS_PER_MODEL):
            raise HTTPException(
                status_code=422,
                detail=f"island_id out of range (0..{config.QUALITY_OPT_ISLANDS_PER_MODEL - 1})",
            )
        try:
            result = state.freeze_v3_champion_to_seed(model=model, island_id=island_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return JSONResponse(result)

    @app.get("/api/quality/optimize/v3/stream")
    async def api_optimize_v3_stream(model: Optional[str] = None) -> StreamingResponse:
        """SSE stream of the v3 events (dedicated broker; view-scoped when ``model``)."""
        if model is None:
            stream = state.optimizer_v3_broker.subscribe()
        else:
            stream = _view_scoped_stream(state, model, broker=state.optimizer_v3_broker)
        return StreamingResponse(
            stream,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/quality/optimize/v3/reset")
    async def api_optimize_v3_reset() -> JSONResponse:
        """Stop the active v3 run (if any), reset lifecycle, clear v3 stores + sentinel."""
        snapshot = await state.reset_optimizer_v3()
        return JSONResponse(snapshot, status_code=200)

    @app.post("/api/quality/optimize/v3/resume")
    async def api_optimize_v3_resume() -> JSONResponse:
        """Resume a v3 run from its durable checkpoints (sentinel skips done phases).

        **200** with the new snapshot when launched; **409** when already running or
        when there is no previous v3 request to resume from.
        """
        launched, snapshot = await state.resume_optimizer_v3()
        if not launched:
            if state.optimizer_v3_status == "running":
                return JSONResponse(
                    {"detail": "optimizer v3 already running"}, status_code=409
                )
            return JSONResponse(
                {"detail": "no previous v3 run to resume (no request recorded)"},
                status_code=409,
            )
        return JSONResponse(snapshot, status_code=200)

    # ======================================================================
    # Prompt Bench — fixed prompt leaderboard (own broker/stores/account).
    # Independent of every optimizer stream; safe to run alongside a v3 run.
    # ======================================================================
    @app.post("/api/promptbench/start")
    async def api_promptbench_start() -> JSONResponse:
        """Start the Prompt Bench run (202) or 409 if one is already running."""
        launched = await state.start_promptbench()
        if not launched:
            return JSONResponse(
                {"detail": "prompt bench already running"}, status_code=409
            )
        return JSONResponse(state.promptbench_snapshot(), status_code=202)

    @app.get("/api/promptbench/status")
    def api_promptbench_status() -> dict:
        """Prompt Bench lifecycle + durable backfill (points + results + winner)."""
        return state.promptbench_snapshot()

    @app.get("/api/promptbench/stream")
    async def api_promptbench_stream() -> StreamingResponse:
        """SSE stream of the Prompt Bench events (dedicated broker)."""
        return StreamingResponse(
            state.promptbench_broker.subscribe(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/promptbench/reset")
    async def api_promptbench_reset() -> JSONResponse:
        """Stop the active run (if any), reset lifecycle, archive the stores."""
        snapshot = await state.reset_promptbench()
        return JSONResponse(snapshot, status_code=200)

    # ---- eval dashboard (ragas-eval-visualization-dashboard): own start / status / stream / recent ----
    @app.post("/api/eval/runs/start")
    async def api_eval_run_start(request: Request) -> JSONResponse:
        """Start an OFFLINE multi-agent eval run / corpus-size sweep (its own broker).

        Request body (JSON; all optional):
        ``{"agents": ["agent-a","agent-b","agent-c"], "metrics": ["faithfulness", ...],
        "corpus_sizes": [100, 500], "corpus_size": 100, "num_queries": 4}``.
        Defaults: ``agents`` = the configured :data:`EVAL_AGENTS` set, ``metrics`` =
        the catalog's default-enabled set, a single ``corpus_size`` multi-agent run
        (supply ``corpus_sizes`` for a sweep). The run uses an offline producer
        (offline ragas + injected offline retrieval/agent providers), so a started
        run is **network-free** and needs no AWS.

        Returns **202** + the eval snapshot once launched; **409** if an eval run is
        already active; **422** for an unknown agent or metric, or fewer than three
        agents (the multi-agent-comparison floor, Req 5.4).

        **Loopback / no-auth posture (Req 21.2 / 15.1/15.2).** This additive route
        inherits the harness's loopback-only, no-auth posture unchanged: the app
        binds to loopback only and :func:`serve` refuses a non-loopback bind without
        an explicit auth-added override. It widens no exposure.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            body = {}

        # On-demand combinatorial mode (Area F / Req 22): an additive, latent path
        # that RELAXES the >= 3 multi-agent floor to "one or more agents" (Req 22.2)
        # and accepts an arbitrary metric subset (ragas + retrieval), corpus
        # series, and query subset. Absent the flag, this route behaves EXACTLY as
        # before (the recorded-run multi-agent / sweep path is untouched).
        on_demand = bool(body.get("on_demand", False))

        # -- agents: default to the configured set; reject unknown / too-few (422) --
        agents = body.get("agents", None)
        if agents is None:
            agents = list(EVAL_AGENTS)
        elif isinstance(agents, str):
            agents = [agents]
        else:
            agents = [str(a) for a in agents]
        unknown_agents = [a for a in agents if a not in EVAL_AGENTS]
        if unknown_agents:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"unknown agent(s) {unknown_agents}; the eval dashboard compares "
                    f"the configured agent set {sorted(EVAL_AGENTS)} (Req 5.3)"
                ),
            )
        if on_demand:
            # On-demand accepts an arbitrary pool of ONE OR MORE agents — it is NOT
            # bound to the >= 3 comparison primitive (Req 22.2).
            if len(set(agents)) < 1:
                raise HTTPException(
                    status_code=422,
                    detail="an on-demand run needs at least one agent (Req 22.2)",
                )
        elif len(set(agents)) < 3:
            raise HTTPException(
                status_code=422,
                detail=(
                    "a multi-agent eval run compares at least 3 agents (N >= 3, Req 5.4); "
                    f"got {sorted(set(agents))}"
                ),
            )

        # -- metrics: default to the catalog's enabled set; reject unknown (422) --
        from bakeoff.eval import catalog
        from bakeoff.eval.retrieval_metrics import RETRIEVAL_METRIC_NAMES

        known_ragas = {e.name for e in catalog.CATALOG}
        known_retrieval = set(RETRIEVAL_METRIC_NAMES)
        metrics = body.get("metrics", None)
        if metrics is None:
            metrics = catalog.default_enabled_names()
        elif isinstance(metrics, str):
            metrics = [metrics]
        else:
            metrics = [str(m) for m in metrics]
        # On-demand accepts an arbitrary subset of the enabled in-scope metrics
        # INCLUDING retrieval-metric entries (Req 22.3); the default/recorded path
        # validates ragas catalog names only (unchanged). Retrieval metrics are
        # always computed by the Retrieval_Metric_Computer and stored as distinct
        # signals, so an accepted retrieval name narrows the *selection* the run
        # echoes; only ragas names are handed to the Ragas_Adapter's enabled set.
        allowed_metrics = (known_ragas | known_retrieval) if on_demand else known_ragas
        unknown_metrics = [m for m in metrics if m not in allowed_metrics]
        if unknown_metrics:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"unknown metric(s) {unknown_metrics}; choose from the ragas metric "
                    f"catalog {sorted(known_ragas)}"
                    + (
                        f" or the retrieval metrics {sorted(known_retrieval)} (Req 22.3)"
                        if on_demand
                        else " (Req 4)"
                    )
                ),
            )
        ragas_metrics = [m for m in metrics if m in known_ragas]

        # -- run shape: a single-size multi-agent run, or a corpus-size sweep --
        corpus_sizes = body.get("corpus_sizes", None)
        if corpus_sizes is not None:
            if isinstance(corpus_sizes, (int, float)):
                corpus_sizes = [int(corpus_sizes)]
            else:
                try:
                    corpus_sizes = [int(s) for s in corpus_sizes]
                except (TypeError, ValueError):
                    raise HTTPException(
                        status_code=422,
                        detail=f"corpus_sizes must be a list of integers, got {corpus_sizes!r}",
                    )
            if not corpus_sizes:
                corpus_sizes = None
        corpus_size = int(body.get("corpus_size", 100))
        num_queries = int(body.get("num_queries", 4))

        # -- on-demand combinatorial path (Area F / Req 22) -----------------
        if on_demand:
            # Arbitrary query subset (Req 22.5): named by id (q0, q3, …). When
            # omitted, fall back to the num_queries-long default synthetic set so
            # an on-demand run still works with no explicit query selection.
            query_ids = None
            raw_qids = body.get("query_ids")
            if isinstance(raw_qids, (list, tuple)) and raw_qids:
                query_ids = [str(q) for q in raw_qids]

            # Combination count = |agents| x |corpus sizes| x |queries| (Req 22.6).
            sizes_for_count = corpus_sizes if corpus_sizes else [corpus_size]
            n_queries = len(set(query_ids)) if query_ids is not None else num_queries
            from bakeoff.eval.experiment_runner import combination_count

            combos = combination_count(agents, sizes_for_count, n_queries)

            # Over-threshold confirmation gate (Req 22.12): refuse to launch an
            # oversized combinatorial pool without an explicit ``confirm: true``.
            # The confirmation requirement is signalled to the UI as a structured
            # 409 carrying ``confirmation_required`` + the count and threshold.
            threshold = state.eval_ondemand_threshold
            if combos > threshold and not bool(body.get("confirm", False)):
                return JSONResponse(
                    {
                        "detail": (
                            f"combination count {combos} exceeds the configured "
                            f"threshold {threshold}; resend with confirm=true to launch"
                        ),
                        "confirmation_required": True,
                        "combination_count": combos,
                        "threshold": threshold,
                    },
                    status_code=409,
                )

            run_kwargs = dict(
                agents=agents,
                metrics=ragas_metrics,
                corpus_sizes=corpus_sizes,
                corpus_size=corpus_size,
                num_queries=num_queries,
                query_ids=query_ids,
            )

            # At most one on-demand run active at a time (Req 22.10); a request that
            # arrives while a run is active is enqueued in the BOUNDED queue and
            # started only after the active run completes (Req 22.11). A full queue
            # is refused (429) rather than silently dropping the request.
            if state.eval_status == EvalStatus.RUNNING:
                if len(state.eval_queue) >= state.eval_queue_max:
                    return JSONResponse(
                        {
                            "detail": (
                                f"on-demand run queue is full (max {state.eval_queue_max}); "
                                "retry after the active run completes"
                            ),
                            "queue_depth": len(state.eval_queue),
                        },
                        status_code=429,
                    )
                state.eval_queue.append(run_kwargs)
                snap = state.eval_snapshot()
                snap["enqueued"] = True
                snap["queue_depth"] = len(state.eval_queue)
                snap["combination_count"] = combos
                return JSONResponse(snap, status_code=202)

            await state.start_eval_run(**run_kwargs)
            snap = state.eval_snapshot()
            snap["enqueued"] = False
            snap["queue_depth"] = len(state.eval_queue)
            snap["combination_count"] = combos
            return JSONResponse(snap, status_code=202)

        # -- recorded-run multi-agent / sweep path (unchanged) --------------
        launched = await state.start_eval_run(
            agents=agents,
            metrics=metrics,
            corpus_sizes=corpus_sizes,
            corpus_size=corpus_size,
            num_queries=num_queries,
        )
        if not launched:
            return JSONResponse(
                {"detail": "an eval run is already active"}, status_code=409
            )
        return JSONResponse(state.eval_snapshot(), status_code=202)

    @app.get("/api/eval/status")
    def api_eval_status() -> dict:
        """The eval run lifecycle + durable-backfill view state from the Event_Store.

        Returns the in-memory lifecycle (``idle``/``running``/``completed``/``failed``)
        and launch request, plus the per-view state (agents, sessions, corpus sizes,
        instance count, a trailing window of instances, per-agent rollups, sweep
        progress) reconstructed from the append-only EvalEventStore (Req 8.2/8.3,
        15.2). Empty-but-well-formed before any run, and never 500s on a malformed
        store (Req 15.5).
        """
        return state.eval_snapshot()

    @app.get("/api/eval/stream")
    async def api_eval_stream() -> StreamingResponse:
        """SSE stream of the eval ``eval_instance_appended`` / ``eval_status`` deltas.

        Drains the DEDICATED :attr:`AppState.eval_broker`, never the bake-off
        ``/api/stream`` broker or either optimizer broker (hard isolation
        constraint). Delta-only with **no replay buffer**: a late joiner only sees
        records that land after it connects — the durable backfill authority is
        ``GET /api/eval/status`` and the replay seed is ``/api/eval/instances/recent``.
        """
        return StreamingResponse(
            state.eval_broker.subscribe(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/eval/instances/recent")
    def api_eval_instances_recent(limit: int = 4000) -> dict:
        """Replay the most recent durable ``EvalInstance`` records (the stream seed).

        The eval SSE stream has no replay buffer, so a browser that connects (or
        reloads) only sees instances that land after it connected. This endpoint
        lets the dashboard seed its in-memory buffer from disk on load, then continue
        from the live stream. Each row is shaped **identically** to the
        ``eval_instance_appended`` event payload (``EvalInstance.to_dict()``) so the
        client feeds seed + live deltas through one code path. Newest-first, capped
        to ``limit``. Never raises on a malformed store (degrades to an empty seed).
        """
        try:
            from bakeoff.eval.event_store import EvalEventStore

            store = EvalEventStore(state.eval_events_path)
            recent = store.read_recent(max(0, limit)) if limit else store.read_all()
            total = len(store.read_all())
            rows = [inst.to_dict() for inst in reversed(recent)]
            return {"instances": rows, "total": total}
        except Exception:  # noqa: BLE001 - a malformed store must not 500 the seed
            return {"instances": [], "total": 0}

    # -- REAL eval run: prompt files × queries.jsonl over the LIVE stack ---------
    @app.get("/api/eval/real/prompts")
    def api_eval_real_prompts() -> dict:
        """List the prompt files (each a series) available to the real eval run."""
        from bakeoff.eval.real_run import load_prompt_series

        prompt_dir = config.REPO_ROOT / "data" / "prompts"
        try:
            series = load_prompt_series(prompt_dir)
            return {"prompt_dir": str(prompt_dir),
                    "series": [{"key": s.key, "chars": len(s.instruction)} for s in series]}
        except Exception as exc:  # noqa: BLE001
            return {"prompt_dir": str(prompt_dir), "series": [], "error": repr(exc)}

    @app.get("/api/eval/real/status")
    def api_eval_real_status() -> dict:
        """Status of the real (live-stack) eval run."""
        return state.real_eval_snapshot()

    @app.post("/api/eval/real/start")
    async def api_eval_real_start(request: Request) -> JSONResponse:
        """Launch a real eval run over ``query_count`` queries × the prompt files.

        Body: ``{"query_count": 100|200|500|1000, "prompt_dir": <optional path>}``.
        409 if a real run is already active.
        """
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        allowed = {100, 200, 500, 1000}
        try:
            query_count = int(body.get("query_count", 100))
        except (TypeError, ValueError):
            return JSONResponse({"error": "query_count must be an integer"}, status_code=422)
        if query_count not in allowed:
            return JSONResponse(
                {"error": f"query_count must be one of {sorted(allowed)}"}, status_code=422)
        prompt_dir = body.get("prompt_dir")
        started = state.start_real_eval_run(query_count=query_count, prompt_dir=prompt_dir)
        if not started:
            return JSONResponse(
                {"error": "a real eval run is already running", **state.real_eval_snapshot()},
                status_code=409)
        return JSONResponse(state.real_eval_snapshot(), status_code=202)

    @app.post("/api/eval/real/stop")
    def api_eval_real_stop() -> JSONResponse:
        """Cooperatively stop the running real eval (a later Start resumes the remainder)."""
        stopping = state.stop_real_eval_run()
        return JSONResponse({"stopping": stopping, **state.real_eval_snapshot()})

    @app.post("/api/eval/real/wipe")
    def api_eval_real_wipe() -> JSONResponse:
        """Wipe the metric data the dashboard reads (truncate the eval store)."""
        try:
            discarded = state.wipe_eval_data()
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse({"wiped": True, "discarded": discarded})

    @app.get("/api/eval/prompts")
    def api_eval_prompts() -> dict:
        """The ragas-metric prompt catalog + each metric's active prompt config.

        Returns one row per catalog metric (priority order), carrying its
        scope/family/customizable/external marking and the active prompt
        configuration (instruction + few-shot examples + config id + version +
        is_override) plus the ragas default for reset-preview (Req 16.1, 16.7).
        Customizable metrics expose an editable prompt; non-customizable ones are
        flagged ``customizable: false`` so the Prompt_Manager renders them as not
        editable (Req 16.7). Never raises; degrades to an empty list on error.
        """
        try:
            return {"prompts": state.eval_prompt_store.list_configs()}
        except Exception as exc:  # noqa: BLE001 - the prompt view must never 500
            return {"prompts": [], "error": repr(exc)}

    @app.put("/api/eval/prompts/{metric}")
    async def api_eval_prompt_put(metric: str, request: Request) -> JSONResponse:
        """Persist a prompt OVERRIDE for ``metric`` (or reset it to default).

        Request body (JSON):
        ``{"instruction": "...", "examples": [{"input": "...", "output": "..."}]}``
        to set an override (Req 16.3), or ``{"reset": true}`` to reset to the
        ragas default (Req 16.4). The override is named + versioned and scoped to
        the run; the new config id is what the Metric_Engine records alongside
        each value produced after the change (Req 16.5/16.6) — previously recorded
        values are untouched, and retrieval is never affected.

        Returns **200** + the updated prompt row; **404** for an unknown metric;
        **422** for a metric that does not support prompt customization (Req
        16.7) or a malformed body. Inherits the loopback-only, no-auth posture.
        """
        from bakeoff.eval.prompt_store import (
            PromptNotCustomizableError,
            UnknownMetricError,
        )

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            body = {}

        store = state.eval_prompt_store
        try:
            if body.get("reset") is True:
                store.reset(metric)
            else:
                instruction = body.get("instruction")
                if not isinstance(instruction, str) or not instruction.strip():
                    raise HTTPException(
                        status_code=422,
                        detail="an override requires a non-empty 'instruction' string",
                    )
                examples = body.get("examples")
                if examples is not None and not isinstance(examples, (list, tuple)):
                    raise HTTPException(
                        status_code=422,
                        detail="'examples' must be a list of {input, output} objects",
                    )
                store.set_override(
                    metric, instruction=instruction, examples=examples
                )
        except UnknownMetricError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except PromptNotCustomizableError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        return JSONResponse(store.config_row(metric), status_code=200)

    @app.get("/exec/reports")
    def exec_reports() -> dict:
        """List the materialized exec reports available under the reports dir."""
        rd = state.reports_dir
        if not rd.exists():
            return {"reports": []}
        versions = sorted(
            p.name[len("aggregate_") : -len(".json")]
            for p in rd.glob("aggregate_*.json")
        )
        return {"reports": versions}

    @app.get("/exec/aggregate")
    def exec_aggregate(plan_version: Optional[str] = None) -> dict:
        """Serve a materialized ``aggregate_<plan_version>.json`` for the exec viz.

        Enforces design **Property 10**: the exec layer refuses to emit an
        ``Aggregate`` lacking a CI. If the report contains any bare number (a
        ``mean_ci`` of ``null`` not marked ``insufficient_data``) or a frontier
        point without a quality CI, the route returns **422** rather than serving a
        CI-less number to the executive audience.
        """
        path = _resolve_report_path(state.reports_dir, plan_version)
        if path is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"no materialized exec report found"
                    + (f" for plan_version {plan_version!r}" if plan_version else "")
                ),
            )
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=f"failed to read report: {exc}")

        violations = _report_ci_violations(report)
        if violations:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "report contains numbers without a CI (Property 10)",
                    "violations": violations,
                },
            )
        return report

    # ---- static SPA at / (graceful stub when the bundle isn't built) ----
    _mount_frontend(app, state.dist_dir)
    return app


def _mount_frontend(app: FastAPI, dist_dir: Path) -> None:
    """Serve the built SPA from ``dist_dir`` at ``/`` if present, else a JSON stub.

    The backend MUST NOT hard-fail when ``dist_dir`` is absent (the frontend is
    Tasks 13/14): in that case ``/`` returns a small, friendly JSON stub
    explaining the dashboard is not built yet and pointing at the live API. When
    the bundle exists, it is mounted last (so it never shadows the ``/api`` and
    ``/exec`` routes registered above) with ``html=True`` for SPA fallback.
    """
    if dist_dir.is_dir() and (dist_dir / "index.html").exists():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=str(dist_dir), html=True), name="spa")
        return

    @app.get("/")
    def spa_not_built() -> JSONResponse:
        """Graceful fallback when the TypeScript dashboard bundle is absent."""
        return JSONResponse(
            {
                "service": "model-bakeoff-harness",
                "ui": "not-built",
                "message": (
                    "The TypeScript dashboard has not been built yet. Build it with "
                    "`npm run build` in bakeoff/ui/ (Tasks 13/14), or use the JSON/SSE "
                    "API directly."
                ),
                "api": [
                    "/api/models",
                    "/api/aggregate",
                    "/api/stream",
                    "/api/control/{pause|resume|abort}",
                    "/api/run/start",
                    "/api/judge/status",
                    "/api/judge/start",
                    "/api/judge/scores",
                    "/api/quality/summary",
                    "/api/quality/optimize/start",
                    "/api/quality/optimize/status",
                    "/api/quality/optimize/history",
                    "/api/eval/runs/start",
                    "/api/eval/status",
                    "/api/eval/stream",
                    "/api/eval/instances/recent",
                    "/api/eval/prompts",
                    "/exec/aggregate",
                    "/exec/reports",
                    "/healthz",
                ],
            }
        )


# ---------------------------------------------------------------------------
# Server entrypoint — enforces the loopback/no-auth precondition (Req 15.1/15.2)
# ---------------------------------------------------------------------------
def serve(
    host: str = config.UI_HOST,
    port: int = config.UI_PORT,
    *,
    allow_non_loopback: bool = False,
) -> None:  # pragma: no cover - real server start is out of scope for tests
    """Run the app under uvicorn, refusing a non-loopback bind without auth.

    The no-authentication posture is valid **only** for a loopback bind (Req
    15.1). This entrypoint therefore refuses to bind to a non-loopback host unless
    the caller explicitly asserts that authentication has been added first
    (``allow_non_loopback=True``) — encoding Req 15.2 as a runtime precondition so
    the no-auth API cannot silently land on a routable interface.
    """
    if not is_loopback_host(host) and not allow_non_loopback:
        raise RuntimeError(
            f"refusing to bind the bakeoff harness to non-loopback host {host!r} "
            "with no authentication: add auth first, then pass allow_non_loopback=True "
            "(Req 15.2)."
        )
    import uvicorn

    # Give the whole `bakeoff.*` logger tree its own INFO handler. Without this, uvicorn
    # only configures its OWN loggers and every bakeoff.* INFO log (cred refresh,
    # optimizer heartbeat, the swallowed-error traceback) falls through to the WARNING-only
    # last-resort handler and is silently dropped — which is why the optimizer's failures
    # were invisible. propagate=False keeps this independent of uvicorn's own handlers.
    import logging
    import sys

    _bakeoff_log = logging.getLogger("bakeoff")
    _bakeoff_log.setLevel(logging.INFO)
    if not _bakeoff_log.handlers:
        _handler = logging.StreamHandler(sys.stderr)
        _handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        _bakeoff_log.addHandler(_handler)
    _bakeoff_log.propagate = False

    # Eval surface data source override (real-data backfill): when
    # GBBO_EVAL_EVENTS_PATH is set (scripts/dashboard.sh points it at
    # config.EVAL_REAL_INSTANCES_PATH), the eval dashboard reads THAT EvalInstance
    # store — keeping the real-backfill lineage separate from the synthetic
    # producer's default store file.
    import os

    eval_events_override = os.environ.get("GBBO_EVAL_EVENTS_PATH")
    eval_events_path = (
        Path(eval_events_override) if eval_events_override else DEFAULT_EVAL_EVENTS_PATH
    )

    uvicorn.run(
        create_app(
            host=host,
            port=port,
            start_cred_refresh=True,
            eval_events_path=eval_events_path,
        ),
        host=host,
        port=port,
    )


if __name__ == "__main__":  # pragma: no cover
    serve()
