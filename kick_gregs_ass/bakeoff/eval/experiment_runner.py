"""
The Experiment_Runner (design ``Experiment_Runner``; Req 5, 6, 7, 19.2, 19.3).

The Experiment_Runner orchestrates the two experiment shapes the dashboard's
data layer needs and turns them into durable :class:`EvalInstance` records via
the :class:`~bakeoff.eval.metric_engine.MetricEngine`:

* **Multi-agent comparison runs (Req 5).** Given a set of Agent_Under_Test
  entities (N ≥ 3, *no fixed count assumed* — Req 5.4), run every agent against
  the **same** query set under the **same** read-only retrieval conditions, and
  produce exactly one Instance per ``(agent, query, corpus_size)`` (Req 5.1,
  5.2). The identical retrieval result for a given ``(query, corpus_size)`` is
  **reused** across all compared agents, so observed differences are attributable
  to the agents and not to differing retrieval (Req 19.3). A single agent/query
  failure is recorded as a ``status="failed"`` Instance and the run continues
  (Req 5.5).

* **Corpus-size sweep (Req 6).** Given an ordered series of corpus sizes, run the
  same constant query set against each size (Req 6.1, 6.4), labelling every
  Instance with its corpus size (Req 6.3). Each sized corpus is treated as a
  prepared, **read-only** experiment input (the canonical substrate is never
  mutated — Req 19.2); if a size cannot be prepared it is recorded *unavailable*
  and the sweep continues with the remaining sizes (Req 6.5).

For every Instance the runner records the Agent_Under_Test id, the Session id, a
strictly-increasing Instance_Index within that Session (Req 7.4), the corpus
size, the end-to-end Latency, the per-stage timings kept separate from that
end-to-end Latency (Req 7.3), and whether retrieval was served from cache so
cold and cached timings are never conflated (Req 7.5).

Stability / testability posture (owner guidance):

The runner is built entirely around **injected callables**, so it is fully
exercisable offline with **zero network** and no live Bedrock/OpenSearch
dependency:

* a :data:`RetrievalProvider` — ``(query, corpus_size) -> RetrievalResult`` —
  which the runner **memoizes per ``(query_id, corpus_size)``** so the retrieval
  ids/fragments handed to every compared agent are byte-identical (Req 19.3);
* an :data:`AgentProvider` — ``(agent_id, query, RetrievalResult) -> AgentAnswer``
  — the Agent_Under_Test's answer (raising signals an agent/query failure);
* an optional :data:`CorpusPreparer` — ``(corpus_size) -> handle`` — a read-only
  preparation gate that may raise to mark a size unavailable (Req 6.5);
* the :class:`~bakeoff.eval.metric_engine.MetricEngine` (default offline ragas),
  which computes the metrics and appends exactly one record (Req 8.1).

Pure standard library plus the existing :mod:`bakeoff.eval` package; no
third-party deps, no I/O beyond what the injected MetricEngine's store performs.
Operates only on the harness's synthetic, non-PII fields (Req 21.3).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from bakeoff.eval.metric_engine import MetricEngine
from bakeoff.eval.models import EvalInstance, StageTimings
from bakeoff.eval.ragas_adapter import RagasSample

__all__ = [
    "Query",
    "RetrievalResult",
    "AgentAnswer",
    "RetrievalProvider",
    "AgentProvider",
    "CorpusPreparer",
    "RunResult",
    "ExperimentRunnerError",
    "CorpusUnavailableError",
    "ExperimentRunner",
    "combination_count",
]


# ---------------------------------------------------------------------------
# On-demand combinatorial sizing (Area F / Req 22.6, 22.12)
# ---------------------------------------------------------------------------
def combination_count(
    agents: Sequence[str],
    corpus_sizes: Sequence[int],
    num_queries: int,
) -> int:
    """The number of Instances an on-demand combinatorial run would produce.

    A run produces exactly one Instance per element of the cartesian combination
    of the selected agents, the selected corpus sizes, and the selected query
    subset (Req 22.6), so the count is simply
    ``|distinct agents| × |distinct corpus sizes| × |queries|``. This is the
    quantity the start endpoint compares against the configurable confirmation
    threshold (Req 22.12) **before** launching anything, so an oversized request
    is gated without first materializing the combination.

    Duplicate agent ids / corpus sizes are collapsed (they would be rejected by
    :meth:`ExperimentRunner._validate` anyway) so the count reflects the actual
    distinct combination the runner would execute. ``num_queries`` is taken as
    given (the caller knows the size of its chosen query subset).
    """
    n_agents = len({str(a) for a in agents})
    n_sizes = len({int(s) for s in corpus_sizes})
    n_queries = max(0, int(num_queries))
    return n_agents * n_sizes * n_queries


# ---------------------------------------------------------------------------
# Value objects crossing the injected-callable seam
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Query:
    """One item in the constant query set run against every agent / corpus size.

    ``query_id`` is the stable identity used to **memoize retrieval** per
    ``(query_id, corpus_size)`` (Req 19.3) and to assert the query set is held
    constant across a sweep (Req 6.4). All text is the harness's synthetic,
    non-PII data (Req 21.3).
    """

    query_id: str
    text: str
    reference: Optional[str] = None
    prompt_id: Optional[str] = None
    category: Optional[str] = None


@dataclass(frozen=True)
class RetrievalResult:
    """A read-only retrieval result for one ``(query, corpus_size)``.

    The runner memoizes this per ``(query_id, corpus_size)`` and reuses the same
    object for every compared agent, so the ranked ids, gold ids, and fragments
    fed into each agent's scoring are byte-identical (Req 19.3). It carries the
    retrieved ranking, the resolved Gold_Links, the context fragments handed to
    the generation step, the retrieval stage time, and whether the substrate
    served it from cache.
    """

    ranked_ids: tuple[str, ...]
    gold_ids: tuple[str, ...]
    fragments: tuple[str, ...] = ()
    retrieval_ms: Optional[float] = None
    cached: bool = False


@dataclass(frozen=True)
class AgentAnswer:
    """One Agent_Under_Test's answer for one ``(query, RetrievalResult)``.

    ``generation_ms`` is the generation stage time (recorded separately from the
    end-to-end Latency, Req 7.3); ``confidence`` / ``volume`` / ``cost`` are the
    optional bubble-size source candidates carried onto the Instance (Req 10.5).
    Raising from the :data:`AgentProvider` instead of returning one of these
    signals an agent/query failure (Req 5.5).
    """

    answer: str
    generation_ms: Optional[float] = None
    confidence: Optional[float] = None
    volume: Optional[float] = None
    cost: Optional[float] = None


#: ``(query, corpus_size) -> RetrievalResult``. Memoized per ``(query_id,
#: corpus_size)`` by the runner so it is invoked at most once per distinct
#: ``(query, corpus_size)`` and reused across agents (Req 19.3).
RetrievalProvider = Callable[[Query, int], RetrievalResult]
#: ``(agent_id, query, RetrievalResult) -> AgentAnswer``. Raising signals a
#: per-execution failure that becomes a ``status="failed"`` Instance (Req 5.5).
AgentProvider = Callable[[str, Query, RetrievalResult], AgentAnswer]
#: ``(corpus_size) -> handle``. A read-only preparation gate; raising marks the
#: size unavailable for the sweep (Req 6.5). MUST NOT mutate the substrate.
CorpusPreparer = Callable[[int], object]


# ---------------------------------------------------------------------------
# Result + errors
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RunResult:
    """The outcome of one experiment run.

    ``instances`` are every produced :class:`EvalInstance` in production order
    (each already appended to the MetricEngine's store, Req 8.1).
    ``unavailable_corpus_sizes`` are the requested sizes that could not be
    prepared and were skipped (Req 6.5). ``failed_count`` is the number of
    ``status="failed"`` Instances (Req 5.5).
    """

    run_id: str
    instances: tuple[EvalInstance, ...]
    agents: tuple[str, ...]
    corpus_sizes: tuple[int, ...]
    unavailable_corpus_sizes: tuple[int, ...] = ()
    failed_count: int = 0


class ExperimentRunnerError(RuntimeError):
    """Base error for the Experiment_Runner."""


class CorpusUnavailableError(ExperimentRunnerError):
    """A :data:`CorpusPreparer` may raise this to mark a corpus size unavailable.

    Any exception raised by the preparer is treated as "this size could not be
    prepared" (Req 6.5); this named type makes the intent explicit at call sites.
    """


def _default_clock() -> float:
    """Monotonic wall-clock in milliseconds (the production timing source)."""
    return time.perf_counter() * 1000.0


def _default_now() -> str:
    """ISO-8601 UTC capture time for an Instance's ``timestamp``."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------
class ExperimentRunner:
    """Orchestrates multi-agent runs and corpus-size sweeps into Instances.

    Construct with the injected seams (see module docstring). Both public
    entry points — :meth:`run_multi_agent` and :meth:`run_sweep` — delegate to a
    single private ``_execute`` so the multi-agent run is exactly the sweep over
    a one-element corpus-size series, keeping the two requirement areas on one
    code path.

    The agent set is taken as configuration (Req 5.3): adding or removing an
    agent is a call-site change, never a code change, and no fixed agent count is
    assumed (Req 5.4).
    """

    def __init__(
        self,
        engine: MetricEngine,
        retrieval_provider: RetrievalProvider,
        agent_provider: AgentProvider,
        *,
        corpus_preparer: Optional[CorpusPreparer] = None,
        k: Optional[int] = None,
        clock: Callable[[], float] = _default_clock,
        now: Callable[[], str] = _default_now,
        session_id_provider: Optional[Callable[[str, str], str]] = None,
        run_id_provider: Optional[Callable[[], str]] = None,
    ) -> None:
        self.engine = engine
        self.retrieval_provider = retrieval_provider
        self.agent_provider = agent_provider
        self.corpus_preparer = corpus_preparer
        self.k = k
        self._clock = clock
        self._now = now
        # A Session is an ordered group of Instances for one Agent_Under_Test
        # (glossary); default to one session per (agent, run). Injectable so a
        # caller can group differently (e.g. per (agent, corpus_size)).
        self._session_id_provider = session_id_provider or (
            lambda agent_id, run_id: f"{run_id}:{agent_id}"
        )
        self._run_id_provider = run_id_provider or (lambda: uuid.uuid4().hex)

    # --- public: multi-agent comparison run (Req 5) ---------------------
    def run_multi_agent(
        self,
        agents: Sequence[str],
        queries: Sequence[Query],
        *,
        corpus_size: int,
        run_id: Optional[str] = None,
    ) -> RunResult:
        """Run every agent against the same query set at one corpus size (Req 5).

        Produces one Instance per ``(agent, query)`` (all at ``corpus_size``),
        reusing the identical retrieval result for each ``(query, corpus_size)``
        across the compared agents (Req 19.3). A single agent/query failure is
        recorded as a failed Instance and the run continues (Req 5.5).
        """
        return self._execute(agents, queries, [corpus_size], run_id=run_id)

    # --- public: corpus-size sweep (Req 6) ------------------------------
    def run_sweep(
        self,
        agents: Sequence[str],
        queries: Sequence[Query],
        *,
        corpus_sizes: Sequence[int],
        run_id: Optional[str] = None,
    ) -> RunResult:
        """Run the same constant query set against each corpus size (Req 6).

        The query set is held constant across every size (Req 6.4); each Instance
        is labelled with the corpus size it ran against (Req 6.3). A size that
        cannot be prepared is recorded unavailable and the sweep continues with
        the remaining sizes (Req 6.5).
        """
        if not corpus_sizes:
            raise ExperimentRunnerError("corpus_sizes must be a non-empty series")
        return self._execute(agents, queries, list(corpus_sizes), run_id=run_id)

    # --- public: on-demand arbitrary/combinatorial run (Req 22) ---------
    def run_on_demand(
        self,
        agents: Sequence[str],
        queries: Sequence[Query],
        *,
        corpus_sizes: Sequence[int],
        run_id: Optional[str] = None,
    ) -> RunResult:
        """Run an arbitrary, user-initiated combinatorial pool (Area F / Req 22).

        This is the latent, on-demand entry point: it accepts an **arbitrary pool
        of one or more agents** (it does *not* assume the N ≥ 3 comparison
        primitive — Req 22.2), an **arbitrary corpus-size series** (Req 22.4), and
        an **arbitrary query subset** (Req 22.5), and produces exactly one Instance
        for each element of the cartesian combination of agents × corpus sizes ×
        queries (Req 22.6). Every produced Instance is appended via the *same*
        MetricEngine → Event_Store path as every other run (Req 22.9), so on-demand
        records are indistinguishable downstream from recorded-run records.

        It shares the single :meth:`_execute` core with :meth:`run_multi_agent`
        and :meth:`run_sweep` — the only thing that makes a run "on demand" is the
        relaxed agent floor and the fact the caller assembled the pool
        interactively; the cartesian production, retrieval reuse, per-Session
        Instance_Index, and failure isolation are identical (Req 22.6/22.9).
        """
        if not corpus_sizes:
            raise ExperimentRunnerError("corpus_sizes must be a non-empty series")
        return self._execute(agents, queries, list(corpus_sizes), run_id=run_id)

    # --- the shared execution core --------------------------------------
    def _execute(
        self,
        agents: Sequence[str],
        queries: Sequence[Query],
        corpus_sizes: Sequence[int],
        *,
        run_id: Optional[str],
    ) -> RunResult:
        agent_list = list(agents)
        self._validate(agent_list, queries)
        run_id = run_id or self._run_id_provider()

        # Retrieval memo persists for the WHOLE run, keyed by (query_id,
        # corpus_size), so identical retrieval is reused byte-for-byte across
        # every compared agent (Req 19.3). The first agent to reach a key pays
        # the cold retrieval; every later reuse is flagged cached (Req 7.5).
        memo: dict[tuple[str, int], RetrievalResult] = {}
        # Strictly-increasing Instance_Index per Session (Req 7.4).
        session_next_index: dict[str, int] = {}

        instances: list[EvalInstance] = []
        unavailable: list[int] = []
        failed = 0

        for size in corpus_sizes:
            # Read-only preparation gate for this sized corpus (Req 6.5, 19.2).
            if self.corpus_preparer is not None:
                try:
                    self.corpus_preparer(size)
                except Exception:  # noqa: BLE001 - any failure ⟹ size unavailable
                    unavailable.append(size)
                    continue

            for agent_id in agent_list:
                session_id = self._session_id_provider(agent_id, run_id)
                for query in queries:
                    index = session_next_index.get(session_id, 0)
                    session_next_index[session_id] = index + 1
                    inst = self._run_one_instance(
                        run_id=run_id,
                        agent_id=agent_id,
                        session_id=session_id,
                        instance_index=index,
                        query=query,
                        corpus_size=size,
                        memo=memo,
                    )
                    if inst.status == "failed":
                        failed += 1
                    instances.append(inst)

        return RunResult(
            run_id=run_id,
            instances=tuple(instances),
            agents=tuple(agent_list),
            corpus_sizes=tuple(corpus_sizes),
            unavailable_corpus_sizes=tuple(unavailable),
            failed_count=failed,
        )

    # --- one (agent, query, corpus_size) execution ----------------------
    def _run_one_instance(
        self,
        *,
        run_id: str,
        agent_id: str,
        session_id: str,
        instance_index: int,
        query: Query,
        corpus_size: int,
        memo: dict[tuple[str, int], RetrievalResult],
    ) -> EvalInstance:
        """Execute one ``(agent, query, corpus_size)`` and append one Instance.

        Reuses the memoized retrieval for ``(query, corpus_size)`` (cold the
        first time, cached on reuse — Req 7.5/19.3), times the end-to-end Latency
        independently of the per-stage timings (Req 7.2/7.3), and appends exactly
        one record via the MetricEngine (Req 8.1). On any failure executing this
        agent/query, records a ``status="failed"`` Instance instead and lets the
        caller continue (Req 5.5).
        """
        instance_id = f"{run_id}:{agent_id}:cs{corpus_size}:{query.query_id}"
        key = (query.query_id, corpus_size)

        start = self._clock()
        retrieval: Optional[RetrievalResult] = None
        cached = False
        try:
            # Retrieval: served from the run-wide memo when present (reuse across
            # agents, Req 19.3), otherwise computed once and memoized.
            if key in memo:
                retrieval = memo[key]
                cached = True
            else:
                retrieval = self.retrieval_provider(query, corpus_size)
                memo[key] = retrieval
                cached = bool(retrieval.cached)

            mid = self._clock()
            answer = self.agent_provider(agent_id, query, retrieval)
            end = self._clock()

            # End-to-end Latency is the runner's own measurement; the per-stage
            # timings are recorded separately and never summed into it (Req 7.3).
            # A cached retrieval contributes only the (near-zero) memo-fetch time,
            # so cold and cached are not conflated in the retrieval stage (Req 7.5).
            if cached:
                retrieval_ms: Optional[float] = mid - start
            elif retrieval.retrieval_ms is not None:
                retrieval_ms = retrieval.retrieval_ms
            else:
                retrieval_ms = mid - start
            generation_ms = (
                answer.generation_ms
                if answer.generation_ms is not None
                else end - mid
            )

            sample = RagasSample(
                question=query.text,
                answer=answer.answer,
                contexts=tuple(retrieval.fragments),
                reference=query.reference,
            )
            return self.engine.score_instance(
                instance_id=instance_id,
                agent_id=agent_id,
                session_id=session_id,
                instance_index=instance_index,
                timestamp=self._now(),
                latency_ms=end - start,
                corpus_size=corpus_size,
                ragas_sample=sample,
                ranked_ids=retrieval.ranked_ids,
                gold_ids=retrieval.gold_ids,
                k=self.k,
                stage_timings=StageTimings(
                    retrieval_ms=retrieval_ms, generation_ms=generation_ms
                ),
                retrieval_cached=cached,
                confidence=answer.confidence,
                volume=answer.volume,
                cost=answer.cost,
                prompt_id=query.prompt_id,
                category=query.category,
                status="ok",
            )
        except Exception as exc:  # noqa: BLE001 - isolate per-execution failure (Req 5.5)
            end = self._clock()
            # Record a failed Instance and let the run continue. Retrieval that
            # already succeeded is still recorded (generation is what failed);
            # there is no answer to score, so the ragas map is empty.
            ranked = retrieval.ranked_ids if retrieval is not None else None
            gold = retrieval.gold_ids if retrieval is not None else None
            return self.engine.score_instance(
                instance_id=instance_id,
                agent_id=agent_id,
                session_id=session_id,
                instance_index=instance_index,
                timestamp=self._now(),
                latency_ms=end - start,
                corpus_size=corpus_size,
                ragas_sample=None,
                ranked_ids=ranked,
                gold_ids=gold,
                k=self.k,
                retrieval_cached=cached,
                prompt_id=query.prompt_id,
                category=query.category,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    # --- validation -----------------------------------------------------
    @staticmethod
    def _validate(agents: Sequence[str], queries: Sequence[Query]) -> None:
        """Validate the run configuration without assuming a fixed agent count.

        Enforces only what correctness needs — a non-empty, unique agent set
        (Req 5.3, 5.4) and a non-empty, unique query set — and deliberately does
        *not* hardcode a minimum/maximum agent count, so any N ≥ 1 is accepted
        and the N ≥ 3 comparison primitive is supported without being assumed
        (Req 5.4).
        """
        if not agents:
            raise ExperimentRunnerError("agents must be a non-empty set")
        if len(set(agents)) != len(agents):
            raise ExperimentRunnerError(f"agent ids must be unique, got {list(agents)}")
        if not queries:
            raise ExperimentRunnerError("queries must be a non-empty set")
        ids = [q.query_id for q in queries]
        if len(set(ids)) != len(ids):
            raise ExperimentRunnerError(f"query ids must be unique, got {ids}")
