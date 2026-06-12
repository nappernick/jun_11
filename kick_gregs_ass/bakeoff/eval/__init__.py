"""
``bakeoff.eval`` — the Python producer half of the ragas eval visualization
dashboard (requirements Areas A–B).

This package computes and durably records :class:`~bakeoff.eval.models.EvalInstance`
records — the single data contract that the TypeScript visualization app
(``bakeoff/ui/src/eval/``) derives every view from. It reuses, rather than
reinvents, the existing harness discipline: the append-only-log pattern from
:mod:`bakeoff.eventlog`, the frozen-dataclass value-object style from
:mod:`bakeoff.types`, and (in later tasks) the retrieval metrics in
``bakeoff.scoring``.

Foundation surface (Task 1):

* :mod:`bakeoff.eval.models` — the ``EvalInstance`` data contract + JSON seam.
* :mod:`bakeoff.eval.event_store` — the durable append-only Event_Store.

Metric computation layer (Task 2, Area A):

* :mod:`bakeoff.eval.catalog` — the ragas metric catalog as data (Req 4).
* :mod:`bakeoff.eval.retrieval_metrics` — the Retrieval_Metric_Computer (Req 2),
  delegating to :mod:`bakeoff.scoring.retrieval_aligned`.
* :mod:`bakeoff.eval.ragas_adapter` — the Ragas_Adapter (Req 1), offline-first
  with a guarded optional ragas import.
* :mod:`bakeoff.eval.metric_engine` — the Metric_Engine orchestration (Req 1.6,
  2.4, 8.1).
"""
from __future__ import annotations

from bakeoff.eval.event_store import EvalEventStore, EvalEventStoreError
from bakeoff.eval.metric_engine import MetricEngine
from bakeoff.eval.models import EvalInstance, MetricValue, StageTimings, clamp_unit
from bakeoff.eval.ragas_adapter import (
    RagasAdapter,
    RagasSample,
    RAGAS_AVAILABLE,
)
from bakeoff.eval.retrieval_metrics import (
    RetrievalMetricComputer,
    compute_retrieval_metrics,
)

__all__ = [
    "clamp_unit",
    "MetricValue",
    "StageTimings",
    "EvalInstance",
    "EvalEventStore",
    "EvalEventStoreError",
    "RetrievalMetricComputer",
    "compute_retrieval_metrics",
    "RagasAdapter",
    "RagasSample",
    "RAGAS_AVAILABLE",
    "MetricEngine",
]
