"""
The Retrieval_Metric_Computer (Req 2): retrieval-quality metrics from gold links.

This component computes the gold-link retrieval-quality signals — precision@k,
recall@k, and NDCG@k — for one Instance by **comparing the retrieved fragment
ids against the resolved gold node ids** for that query (Req 2.1). It is the
retrieval-quality half of the dashboard's two quality dimensions; the
generation-quality half is the :mod:`bakeoff.eval.ragas_adapter`.

Design posture (owner guidance: reuse, do not reinvent):

* **Delegates to the existing scorer.** The ranking math already lives in
  :mod:`bakeoff.scoring.retrieval_aligned` (the Layer-A scorer), which is pure,
  deterministic, CPU-only, and unit-tested against hand-computed rankings. This
  computer is a thin adapter that calls
  :func:`~bakeoff.scoring.retrieval_aligned.precision_at_k`,
  :func:`~bakeoff.scoring.retrieval_aligned.recall_at_k`, and
  :func:`~bakeoff.scoring.retrieval_aligned.ndcg_at_k` and wraps each result in a
  provenance-bearing :class:`~bakeoff.eval.models.MetricValue`. It never
  re-implements the formulas.
* **Records k (Req 2.2).** Every produced :class:`MetricValue` carries the ``k``
  it was computed at — including the unavailable ones, so a no-gold instance
  still records the k that *would* have been used.
* **No-gold ⟹ unavailable (Req 2.3).** When a query has no resolvable Gold_Link
  (an empty gold set), each retrieval metric is recorded ``unavailable`` rather
  than as a misleading 0.0.
* **Precision and recall are stored independently (Req 2.6).** Both are computed
  by their own delegated function; neither is derived from the other.
* **Read-only (Req 2.5, 19.1).** This is pure computation over ids that were
  *already* retrieved and passed in. It issues no retrieval query and holds no
  reference to the substrate, its index, or its corpus, so it cannot mutate any
  of them. There is no I/O on any code path here.

Metric names match the design's ``RetrievalMetricName`` (``precision_at_k`` /
``recall_at_k`` / ``ndcg_at_k``) and are deliberately disjoint from every ragas
metric name, so the two signals are never conflated in storage (Req 2.4 / P9,
enforced by :class:`~bakeoff.eval.models.EvalInstance`).
"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence

from bakeoff import config
from bakeoff.eval.models import MetricValue
from bakeoff.scoring.retrieval_aligned import (
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)

__all__ = [
    "RETRIEVAL_METRIC_NAMES",
    "compute_retrieval_metrics",
    "RetrievalMetricComputer",
]

#: The retrieval-quality metric names this computer produces (design
#: ``RetrievalMetricName``). Disjoint from every ragas metric name (P9).
RETRIEVAL_METRIC_NAMES: tuple[str, ...] = (
    "precision_at_k",
    "recall_at_k",
    "ndcg_at_k",
)


def compute_retrieval_metrics(
    ranked_ids: Sequence[str],
    gold_ids: Iterable[str],
    k: Optional[int] = None,
) -> dict[str, MetricValue]:
    """Compute the three retrieval-quality metrics for one Instance.

    Delegates the ranking math to :mod:`bakeoff.scoring.retrieval_aligned`
    (reuse, not reinvention) and wraps each result in a provenance-bearing
    :class:`MetricValue` that records the ``k`` used (Req 2.2).

    Args:
        ranked_ids: the retrieved fragment ids in ranked order (the constant
            ``/retrieve`` ranking). Read only.
        gold_ids: the resolved gold node ids for the query (the Gold_Links).
        k: the cutoff for precision@k / recall@k / NDCG@k. Defaults to
            :data:`bakeoff.config.SCORING_K`.

    Returns:
        A dict keyed by :data:`RETRIEVAL_METRIC_NAMES`. When the query has at
        least one resolvable Gold_Link, every value is an available
        :class:`MetricValue` carrying ``k`` (Req 2.1, 2.2). When the gold set is
        empty (no resolvable Gold_Link), every value is ``unavailable`` — still
        carrying the ``k`` that would have been used (Req 2.3). Precision and
        recall are produced by their own delegated calls, never derived from one
        another (Req 2.6).
    """
    k_used = config.SCORING_K if k is None else k

    # Materialize the gold set once. An empty gold set is the no-Gold_Link case.
    gold = list(gold_ids)
    if not gold:
        # No resolvable Gold_Link: each retrieval metric is unavailable, but we
        # still record the k that would have been used (Req 2.2, 2.3).
        return {
            name: MetricValue.missing(k=k_used) for name in RETRIEVAL_METRIC_NAMES
        }

    # Delegate every formula to the existing Layer-A scorer (no reinvention).
    # precision and recall are computed by SEPARATE calls so neither is derived
    # from the other (Req 2.6).
    precision = precision_at_k(ranked_ids, gold, k_used)
    recall = recall_at_k(ranked_ids, gold, k_used)
    ndcg = ndcg_at_k(ranked_ids, gold, k_used)

    return {
        "precision_at_k": MetricValue.available(precision, k=k_used),
        "recall_at_k": MetricValue.available(recall, k=k_used),
        "ndcg_at_k": MetricValue.available(ndcg, k=k_used),
    }


class RetrievalMetricComputer:
    """Stateless Retrieval_Metric_Computer (design ``Retrieval_Metric_Computer``).

    A thin, reusable wrapper over :func:`compute_retrieval_metrics` that pins a
    default ``k`` for a run. Pure and read-only: it operates only on ids passed
    in and never touches the retrieval substrate, its index, or its corpus
    (Req 2.5, 19.1).
    """

    name = "retrieval_metric_computer"

    def __init__(self, k: Optional[int] = None) -> None:
        self.k = config.SCORING_K if k is None else k

    def compute(
        self,
        ranked_ids: Sequence[str],
        gold_ids: Iterable[str],
        k: Optional[int] = None,
    ) -> dict[str, MetricValue]:
        """Compute the retrieval metrics for one Instance.

        ``k`` overrides the instance default for this call only; otherwise the
        computer's configured ``k`` (or :data:`bakeoff.config.SCORING_K`) is
        used. See :func:`compute_retrieval_metrics` for the full contract.
        """
        return compute_retrieval_metrics(
            ranked_ids, gold_ids, k if k is not None else self.k
        )
