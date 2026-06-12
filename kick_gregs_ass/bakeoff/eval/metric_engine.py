"""
The Metric_Engine (design ``Metric_Engine``): orchestration for one Instance.

The Metric_Engine ties the two quality computers together and persists the
result. For one ``(Agent_Under_Test, query, corpus_size)`` execution it:

1. computes generation-quality metrics via the :class:`RagasAdapter` (Req 1),
2. computes retrieval-quality metrics via the :class:`RetrievalMetricComputer`
   (Req 2),
3. assembles a single :class:`~bakeoff.eval.models.EvalInstance` whose ``ragas``
   and ``retrieval`` maps are **distinct and disjoint** so generation- and
   retrieval-quality are never conflated in storage (Req 2.4 / P9), and
4. appends **exactly one** record to the durable Event_Store (Req 8.1).

Invariants this orchestration upholds:

* **Composite-independent (Req 1.6, 3.3).** The engine computes and stores raw
  component metric values *only*. It holds no Composite_Weight_Set and never
  applies one — the Quality_Score is a downstream, recompute-on-demand concern,
  so a recorded value is never altered by any weighting choice.
* **Judge-neutral (Req 18.1).** The engine does not read, call, or mutate the
  Authoritative_Judge or any promotion decision. Its output is purely additive
  visualization data; nothing here can change a judge verdict.
* **Read-only on retrieval (Req 19.1).** It consumes already-retrieved ids and
  gold ids passed in; it never queries or mutates the retrieval substrate.

Stores nothing but the synthetic, non-PII fields the contract already carries
(Req 21.3).
"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence

from bakeoff.eval.event_store import EvalEventStore
from bakeoff.eval.models import EvalInstance, MetricValue, StageTimings
from bakeoff.eval.ragas_adapter import RagasAdapter, RagasSample
from bakeoff.eval.retrieval_metrics import RetrievalMetricComputer

__all__ = ["MetricEngine"]


class MetricEngine:
    """Orchestrates ragas + retrieval scoring and durable persistence (Req 1.6, 2.4, 8.1).

    Construct with a :class:`RagasAdapter` (default: offline), a
    :class:`RetrievalMetricComputer` (default: a fresh one at the configured
    ``k``), and an :class:`EvalEventStore`. Each call to :meth:`score_instance`
    produces and appends exactly one :class:`EvalInstance`.
    """

    def __init__(
        self,
        store: EvalEventStore,
        ragas_adapter: Optional[RagasAdapter] = None,
        retrieval_computer: Optional[RetrievalMetricComputer] = None,
    ) -> None:
        self.store = store
        self.ragas_adapter = ragas_adapter or RagasAdapter.offline()
        self.retrieval_computer = retrieval_computer or RetrievalMetricComputer()

    def score_instance(
        self,
        *,
        instance_id: str,
        agent_id: str,
        session_id: str,
        instance_index: int,
        timestamp: str,
        latency_ms: float,
        corpus_size: int,
        ragas_sample: Optional[RagasSample] = None,
        ranked_ids: Optional[Sequence[str]] = None,
        gold_ids: Optional[Iterable[str]] = None,
        k: Optional[int] = None,
        stage_timings: Optional[StageTimings] = None,
        retrieval_cached: bool = False,
        confidence: Optional[float] = None,
        volume: Optional[float] = None,
        cost: Optional[float] = None,
        prompt_id: Optional[str] = None,
        category: Optional[str] = None,
        status: str = "ok",
        error: Optional[str] = None,
    ) -> EvalInstance:
        """Score one Instance, append it to the Event_Store, and return it.

        Computes the ragas map (generation quality, Req 1) and the retrieval map
        (retrieval quality, Req 2) **independently**, stores them as the disjoint
        maps :class:`EvalInstance` enforces (Req 2.4 / P9), and appends exactly
        one record (Req 8.1). No Composite_Weight_Set is consulted (Req 1.6); no
        judge decision is read or altered (Req 18.1).

        Args:
            ragas_sample: inputs for generation-quality scoring. ``None`` ⟹ an
                empty ragas map (e.g. a failed execution with no answer).
            ranked_ids / gold_ids: the retrieved ids and resolved Gold_Links for
                retrieval-quality scoring. ``None`` for either ⟹ an empty
                retrieval map.
            k: retrieval cutoff override; defaults to the computer's ``k``.
            Other args populate the :class:`EvalInstance` contract directly.

        Returns:
            The appended :class:`EvalInstance`.
        """
        # 1) Generation-quality metrics (ragas). Independent map (Req 1, 2.4/P9).
        ragas_map: dict[str, MetricValue] = {}
        if ragas_sample is not None:
            ragas_map = self.ragas_adapter.score(ragas_sample)

        # 2) Retrieval-quality metrics (gold-link). Independent map (Req 2, 2.4/P9).
        retrieval_map: dict[str, MetricValue] = {}
        if ranked_ids is not None and gold_ids is not None:
            retrieval_map = self.retrieval_computer.compute(ranked_ids, gold_ids, k=k)

        # 3) Assemble one record. EvalInstance re-asserts the disjointness of the
        #    two maps on construction (Req 2.4 / P9), so a name collision is a
        #    loud failure here, never a silent conflation.
        instance = EvalInstance(
            instance_id=instance_id,
            agent_id=agent_id,
            session_id=session_id,
            instance_index=instance_index,
            timestamp=timestamp,
            latency_ms=latency_ms,
            stage_timings=stage_timings or StageTimings(),
            corpus_size=corpus_size,
            retrieval_cached=retrieval_cached,
            ragas=ragas_map,
            retrieval=retrieval_map,
            confidence=confidence,
            volume=volume,
            cost=cost,
            prompt_id=prompt_id,
            category=category,
            status=status,
            error=error,
        )

        # 4) Append exactly one durable record (Req 8.1).
        self.store.append(instance)
        return instance
