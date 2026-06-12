"""
Statistics core for the model-bakeoff-harness (Task 8).

This module is the **scientific spine** of the harness: the variance
decomposition, the item-level cluster bootstrap, the closed-form normal-approx
CI for the live UI, the pilot-driven ``estimate_required_reps`` sizing, and the
paired per-item difference CI for model-vs-model comparisons. Everything here is
a **pure function** (no I/O, no global mutable state) and **deterministic given
a seed** (a single ``numpy`` :class:`~numpy.random.Generator` seeded from the
caller drives every resample), so aggregation is a reproducible function of the
event log (design Property 9 building block).

Design grounding (``.kiro/specs/model-bakeoff-harness/design.md``):

* **The two variances.** For metric ``Y`` on model ``m``, item ``i``, rep ``r``::

      Y[m,i,r] = mu[m] + a[m,i] + e[m,i,r]
                          \\_____/   \\______/
                          between    within

  ``a[m,i] ~ (0, sigma_between^2)`` is real signal about the *population of
  perspectives*; ``e[m,i,r] ~ (0, sigma_within^2)`` is *model stochasticity
  only*. The cardinal rule the whole module encodes: the precision of an
  aggregate/cohort mean is driven by the number of **distinct items**, not the
  number of reps.

* **The variance equation.** For a cohort with ``n`` distinct items and ``R``
  reps each::

      Var(Ybar) ~= sigma_between^2 / n  +  sigma_within^2 / (n*R)

  The first term does not depend on ``R`` at all — this is why the design is
  tiered, and why :func:`normal_approx_halfwidth` shrinks fast in ``n`` and only
  weakly in ``R`` (design Property 6).

* **Cluster bootstrap (design Property 7).** The point estimate weights every
  item equally regardless of its rep count: ``point = mean over items of (mean
  over that item's reps)``.

* **Unreachable detection (design Property 8).** When ``sigma_between^2 / n``
  alone exceeds the target variance ``(target_w/z)^2``, *no number of reps* can
  hit the target — only more items can — so :func:`required_reps_closed_form`
  flags the stratum unreachable rather than returning a finite ``R`` that does
  not meet the target.

**Sourcing caveat.** The CI methodology here (item-level cluster bootstrap,
one-way random-effects ANOVA variance components, normal-approx interval) is
**general statistical practice, not Amazon-internal guidance** — see the design
document's sourcing note. The internal primary sources the global steering rule
prefers were not reachable in this environment.

Dependencies: ``numpy`` (resampling/quantiles) + the stdlib ``statistics``
(normal quantile) + :mod:`bakeoff.types` / :mod:`bakeoff.config`. No network.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

import numpy as np

import bakeoff.config as config
from bakeoff.types import CI, TrialEvent

__all__ = [
    # metric extraction / grouping
    "extract_metric_value",
    "extract_values",
    "group_rep_values_by_item",
    "group_rep_means_by_item",
    "is_judge_metric",
    # confidence intervals
    "cluster_bootstrap_ci",
    "normal_approx_ci",
    "normal_approx_halfwidth",
    "paired_diff_ci",
    # variance decomposition
    "variance_decomp",
    # required-reps sizing
    "Stratum",
    "Budget",
    "RequiredRepsResult",
    "required_reps_closed_form",
    "estimate_required_reps",
    # constants
    "MAX_REPS",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
#: Practical ceiling on reps per stratum. Also the sentinel value returned by
#: :func:`required_reps_closed_form` for an *unreachable* target (the
#: between-item term alone exceeds the target variance). A returned ``R`` equal
#: to ``MAX_REPS`` does NOT by itself mean "unreachable" — a stratum whose
#: required reps merely exceed the ceiling is capped here too — so the
#: unreachable signal is carried *separately* (see :class:`RequiredRepsResult`).
MAX_REPS: int = 50

# Bootstrap iterations are processed in chunks so the (chunk x n_items x R_max)
# scratch tensor used by the two-stage resample stays bounded in memory even for
# the WIDE pass (~1000+ items). The chunk size is a fixed code constant, so the
# RNG is consumed in a fixed order and the result stays deterministic given the
# seed regardless of how many items/iterations are requested.
_BOOT_CHUNK: int = 256


# ---------------------------------------------------------------------------
# Metric field registry
# ---------------------------------------------------------------------------
# A metric is addressed by a bare name (e.g. "composite", "semantic_similarity",
# "ndcg_at_k", "faithfulness", "abstention_correct", "end_to_end_ms"). Dotted
# forms ("quality.accuracy.semantic_similarity", "judge.faithfulness", ...) are
# accepted too: only the final path component is significant, and every field
# name across the four namespaces below is unique, so the last component
# resolves unambiguously.
_COMPOSITE_FIELDS = frozenset({"composite"})
_ACCURACY_FIELDS = frozenset(
    {
        "precision_at_k",
        "recall_at_k",
        "mrr",
        "ndcg_at_k",
        "grounding_precision",
        "grounding_recall",
        "semantic_similarity",
        "abstention_correct",
        "unwarranted_refusal",
    }
)
# The judge rubric dimensions that are real-valued scores (the ones a "judge
# variance" component is defined for). judge_sample_count is an int count, not a
# graded dimension, so it is handled separately and carries no judge component.
_JUDGE_FLOAT_FIELDS = frozenset(
    {
        "faithfulness",
        "correctness",
        "completeness",
    }
)
_JUDGE_INT_FIELDS = frozenset({"judge_sample_count"})
_TIMING_FIELDS = frozenset(
    {
        "embed_query_ms",
        "bm25_vectorize_ms",
        "hybrid_search_ms",
        "rerank_ms",
        "retrieval_total_ms",
        "ttft_ms",
        "generation_total_ms",
        "end_to_end_ms",
    }
)

_ALL_METRIC_FIELDS = (
    _COMPOSITE_FIELDS
    | _ACCURACY_FIELDS
    | _JUDGE_FLOAT_FIELDS
    | _JUDGE_INT_FIELDS
    | _TIMING_FIELDS
)


def _normalize_metric(metric: str) -> str:
    """Reduce a possibly-dotted metric name to its bare, registry-keyed name."""
    return metric.split(".")[-1]


def is_judge_metric(metric: str) -> bool:
    """True iff ``metric`` is one of the graded LLM-as-judge rubric dimensions.

    These are the only metrics for which a separate *judge-sampling* variance
    component is defined (carried in :attr:`JudgeScores.judge_dim_sd`).
    """
    return _normalize_metric(metric) in _JUDGE_FLOAT_FIELDS


def extract_metric_value(event: TrialEvent, metric: str) -> Optional[float]:
    """Return the per-event float value of ``metric``, or ``None`` if absent.

    Resolves the metric against the nested score/timing structure of a
    :class:`TrialEvent`:

    * ``"composite"`` -> ``event.quality.composite``
    * an accuracy field -> ``event.quality.accuracy.<field>``
    * a judge rubric dimension -> ``event.quality.judge.<field>``
    * a timing field -> ``event.timings.<field>``

    ``None`` is returned (not raised) when the underlying value is itself
    ``None`` — most importantly ``abstention_correct`` on an ``answerability ==
    "full"`` item and ``unwarranted_refusal`` on a ``none``/``partial`` item.
    Callers (the grouping helpers below) **skip** such events for that metric, so
    a metric that is undefined for an item simply does not contribute to that
    item's reps. An *unknown* metric name is a programming error and raises.

    Args:
        event: the trial event to read.
        metric: a bare or dotted metric name.

    Returns:
        The value as a ``float``, or ``None`` if the field is unpopulated.

    Raises:
        KeyError: if ``metric`` is not a known metric field.
    """
    name = _normalize_metric(metric)
    if name in _COMPOSITE_FIELDS:
        raw = event.quality.composite
    elif name in _ACCURACY_FIELDS:
        raw = getattr(event.quality.accuracy, name)
    elif name in _JUDGE_FLOAT_FIELDS or name in _JUDGE_INT_FIELDS:
        raw = getattr(event.quality.judge, name)
    elif name in _TIMING_FIELDS:
        raw = getattr(event.timings, name)
    else:
        raise KeyError(
            f"unknown metric {metric!r}; known metrics are "
            f"{sorted(_ALL_METRIC_FIELDS)}"
        )
    return None if raw is None else float(raw)


def extract_values(events: Iterable[TrialEvent], metric: str) -> list[float]:
    """Per-event values of ``metric`` across ``events``, skipping ``None``s.

    Events for which the metric is undefined (e.g. ``abstention_correct`` on a
    ``full`` item) are dropped, never coerced to a number.
    """
    out: list[float] = []
    for ev in events:
        v = extract_metric_value(ev, metric)
        if v is not None:
            out.append(v)
    return out


def group_rep_values_by_item(
    events: Iterable[TrialEvent], metric: str
) -> dict[str, list[float]]:
    """Group the (non-``None``) per-rep values of ``metric`` by ``item_id``.

    An item whose every rep has a ``None`` value for this metric is omitted
    entirely (it contributes no reps), so downstream item-weighted statistics are
    never skewed by a phantom item with no usable observations.
    """
    by_item: dict[str, list[float]] = defaultdict(list)
    for ev in events:
        v = extract_metric_value(ev, metric)
        if v is not None:
            by_item[ev.item_id].append(v)
    return dict(by_item)


def group_rep_means_by_item(
    events: Iterable[TrialEvent], metric: str
) -> dict[str, float]:
    """Map each ``item_id`` to the mean of its (non-``None``) reps for ``metric``.

    This is the item-level summary the cluster bootstrap weights *equally*
    (design Property 7): the per-item rep count drops out, so an item with 12
    reps and an item with 2 reps count the same toward the point estimate.
    """
    return {
        item: float(np.mean(vals))
        for item, vals in group_rep_values_by_item(events, metric).items()
    }


# ---------------------------------------------------------------------------
# Item-level cluster bootstrap (design Property 7 / Req 9.2)
# ---------------------------------------------------------------------------
def cluster_bootstrap_ci(
    events: list[TrialEvent],
    metric: str,
    level: float = config.CONFIDENCE_LEVEL,
    n_boot: int = config.BOOTSTRAP_N,
    seed: int = config.BOOTSTRAP_SEED,
) -> CI:
    """Two-stage item-level cluster bootstrap CI for ``metric``.

    Procedure (design "Confidence intervals and reporting"):

    1. Group observations by item.
    2. For each of ``n_boot`` iterations: **resample items with replacement**,
       then **within each resampled item resample its reps with replacement**;
       the bootstrap statistic is the mean over resampled items of (mean over
       resampled reps).
    3. The reported ``point`` is the mean over items of (mean over that item's
       reps) — **equal weight per item regardless of rep count** (Property 7).
    4. ``low``/``high`` are the ``alpha/2`` and ``1 - alpha/2`` percentiles of
       the bootstrap distribution, where ``alpha = 1 - level``.

    Items (not trials) are the primary resampling unit, so the interval reflects
    between-item variance as the dominant term (the nesting is respected). A
    single :class:`numpy.random.Generator` seeded with ``seed`` drives every draw
    in a fixed order, so the same inputs + seed yield a byte-identical CI
    (design Property 9 building block).

    Args:
        events: all events sharing the grouping this aggregate is over (e.g. one
            model, one cohort cell).
        metric: the metric to summarize (see :func:`extract_metric_value`).
        level: confidence level (default :data:`config.CONFIDENCE_LEVEL`).
        n_boot: bootstrap iterations (default :data:`config.BOOTSTRAP_N`).
        seed: RNG seed (default :data:`config.BOOTSTRAP_SEED`).

    Returns:
        ``CI(point, low, high, method="cluster_bootstrap")``.

    Raises:
        ValueError: if no item has a usable (non-``None``) value for ``metric``.
    """
    by_item = group_rep_values_by_item(events, metric)
    if not by_item:
        raise ValueError(
            f"cluster_bootstrap_ci: no items with a usable value for "
            f"metric {metric!r}"
        )

    # Per-item rep arrays + their lengths, padded into a (k, R_max) matrix so the
    # two-stage resample can be vectorized across iterations.
    rep_arrays = list(by_item.values())
    k = len(rep_arrays)
    counts = np.array([len(a) for a in rep_arrays], dtype=np.int64)
    r_max = int(counts.max())
    padded = np.zeros((k, r_max), dtype=np.float64)
    for i, arr in enumerate(rep_arrays):
        padded[i, : len(arr)] = arr
    # full-item mask (which padded columns are real reps for each item)
    full_mask = np.arange(r_max)[None, :] < counts[:, None]

    # Equal-weight point estimate: mean over items of (mean over that item's reps).
    item_full_means = (padded * full_mask).sum(axis=1) / counts
    point = float(item_full_means.mean())

    alpha = 1.0 - level
    rng = np.random.default_rng(seed)
    boot_stats = np.empty(n_boot, dtype=np.float64)

    filled = 0
    while filled < n_boot:
        b = min(_BOOT_CHUNK, n_boot - filled)
        # Stage 1: resample items with replacement -> (b, k) item indices.
        item_idx = rng.integers(0, k, size=(b, k))
        sel_counts = counts[item_idx]  # (b, k) rep count of each chosen item
        # Stage 2: resample reps within each chosen item with replacement.
        # Draw r_max uniforms per occurrence; only the first ``sel_count`` are
        # used (the rest are masked out), giving a with-replacement rep resample
        # of exactly the chosen item's size.
        u = rng.random(size=(b, k, r_max))
        rep_pos = np.minimum(
            (u * sel_counts[:, :, None]).astype(np.int64), sel_counts[:, :, None] - 1
        )
        gathered = padded[item_idx[:, :, None], rep_pos]  # (b, k, r_max)
        rep_mask = np.arange(r_max)[None, None, :] < sel_counts[:, :, None]
        resampled_item_means = (gathered * rep_mask).sum(axis=2) / sel_counts
        boot_stats[filled : filled + b] = resampled_item_means.mean(axis=1)
        filled += b

    low = float(np.percentile(boot_stats, 100.0 * (alpha / 2.0)))
    high = float(np.percentile(boot_stats, 100.0 * (1.0 - alpha / 2.0)))
    return CI(point=point, low=low, high=high, method="cluster_bootstrap")


# ---------------------------------------------------------------------------
# Variance decomposition (design "the two variances" / Req 9.4)
# ---------------------------------------------------------------------------
def variance_decomp(
    events: Iterable[TrialEvent], metric: str
) -> dict[str, float]:
    """Decompose ``metric``'s variance into between-item / within-item / judge.

    **Estimator.** The between/within split uses the standard **one-way
    random-effects (ANOVA) variance-component estimator** for (generally
    unbalanced) clusters, where the cluster is the *item* and the observations
    are its reps:

        - ``ybar_i``  = mean of item ``i``'s reps; ``ybar`` = overall obs mean.
        - ``MSW`` = SSW / (N - k),  SSW = sum_i sum_r (y_ir - ybar_i)^2
        - ``MSB`` = SSB / (k - 1),  SSB = sum_i R_i (ybar_i - ybar)^2
        - ``sigma_within^2``  = MSW
        - ``sigma_between^2`` = max(0, (MSB - MSW) / R0),
          with the unbalanced-design size constant
          ``R0 = (N - sum_i R_i^2 / N) / (k - 1)``  (R0 == R for balanced data).

    where ``N`` is the total number of (non-``None``) observations and ``k`` the
    number of items. Subtracting the within contribution (``MSW``) before
    dividing is what makes ``between`` an estimate of the *true* item-to-item
    variance rather than the inflated raw spread of item means; it is floored at
    0 (a negative component estimate means "indistinguishable from 0"). When no
    item has more than one rep (``N == k``) the within component is not
    identifiable, so ``within = 0`` and ``between`` falls back to the plug-in
    variance of the item means.

    **Judge component.** For a graded judge dimension (:func:`is_judge_metric`),
    ``judge`` is the mean over events of ``judge_dim_sd[metric] ** 2`` (the
    variance attributable to *judge sampling*, carried separately from model
    within-item variance, per the design). For any non-judge metric, or when no
    event carries a SD for the dimension, ``judge`` is ``0.0``.

    Returns:
        ``{"between": sigma_between^2, "within": sigma_within^2, "judge": ...}``.
        An empty input yields all-zero components.
    """
    events = list(events)
    by_item = group_rep_values_by_item(events, metric)

    between = 0.0
    within = 0.0
    if by_item:
        item_arrays = [np.asarray(v, dtype=np.float64) for v in by_item.values()]
        counts = np.array([a.size for a in item_arrays], dtype=np.float64)
        item_means = np.array([a.mean() for a in item_arrays], dtype=np.float64)
        k = len(item_arrays)
        N = float(counts.sum())
        grand = float((counts * item_means).sum() / N)  # obs-weighted overall mean

        # Within mean square (residual): identifiable only with replication.
        ssw = float(sum(((a - a.mean()) ** 2).sum() for a in item_arrays))
        if N > k:
            within = ssw / (N - k)
        else:
            within = 0.0  # one rep per item everywhere -> within not estimable

        # Between component via the ANOVA estimator (unbalanced-safe).
        if k > 1:
            ssb = float((counts * (item_means - grand) ** 2).sum())
            msb = ssb / (k - 1)
            r0 = (N - float((counts**2).sum()) / N) / (k - 1)
            if r0 > 0:
                between = max(0.0, (msb - within) / r0)
            else:  # pragma: no cover - r0 > 0 whenever k > 1 and counts >= 1
                between = max(0.0, msb)
        else:
            between = 0.0

    # Judge-sampling variance component.
    judge = 0.0
    if is_judge_metric(metric):
        name = _normalize_metric(metric)
        sds = [
            float(ev.quality.judge.judge_dim_sd[name]) ** 2
            for ev in events
            if name in ev.quality.judge.judge_dim_sd
        ]
        if sds:
            judge = float(np.mean(sds))

    return {"between": between, "within": within, "judge": judge}


# ---------------------------------------------------------------------------
# Closed-form normal-approx CI (cheap/incremental, for the live UI; Req 9.2)
# ---------------------------------------------------------------------------
def _z_for_level(level: float) -> float:
    """Two-sided z-multiplier for a confidence ``level`` (e.g. 0.95 -> ~1.96)."""
    alpha = 1.0 - level
    return statistics.NormalDist().inv_cdf(1.0 - alpha / 2.0)


def normal_approx_halfwidth(
    between: float, within: float, n_items: float, reps: float, z: float
) -> float:
    """CI half-width from the variance equation ``z*sqrt(b/n + w/(n*R))``.

    This is the closed-form core of :func:`normal_approx_ci`, exposed as a pure
    function because design **Property 6** is a statement about *this formula*:
    the half-width is monotonically non-increasing in ``n_items`` (both terms
    carry ``1/n``) and only weakly decreasing in ``reps`` (only the second term
    carries ``1/R``). With ``between > 0`` and ``within > 0``, doubling
    ``n_items`` shrinks the half-width strictly more than doubling ``reps``.

    Args:
        between: ``sigma_between^2`` (between-item variance component).
        within: ``sigma_within^2`` (within-item variance component).
        n_items: number of distinct items (``> 0``).
        reps: average reps per item (``> 0``).
        z: z-multiplier for the confidence level.

    Returns:
        The CI half-width (``>= 0``).
    """
    if n_items <= 0 or reps <= 0:
        raise ValueError("n_items and reps must be positive")
    var = between / n_items + within / (n_items * reps)
    return z * math.sqrt(max(0.0, var))


def normal_approx_ci(
    events: list[TrialEvent],
    metric: str,
    level: float = config.CONFIDENCE_LEVEL,
) -> CI:
    """Closed-form normal-approximation CI for ``metric``.

    Uses the variance decomposition (:func:`variance_decomp`) and the variance
    equation: ``point +/- z * sqrt(between/n + within/(n*Rbar))`` where ``n`` is
    the distinct-item count and ``Rbar`` the mean reps per item. This is the
    cheap, incrementally-updatable estimate the live UI uses (Req 9.2); the
    cluster bootstrap is the defensible interval shown in the exec viz.

    The ``point`` matches the bootstrap's: the equal-weight mean over items of
    (mean over that item's reps).

    Raises:
        ValueError: if no item has a usable value for ``metric``.
    """
    by_item = group_rep_means_by_item(events, metric)
    if not by_item:
        raise ValueError(
            f"normal_approx_ci: no items with a usable value for metric {metric!r}"
        )
    item_means = np.array(list(by_item.values()), dtype=np.float64)
    n_items = item_means.size
    point = float(item_means.mean())

    # Mean reps per item (Rbar) from the raw grouping.
    rep_counts = [len(v) for v in group_rep_values_by_item(events, metric).values()]
    rbar = float(np.mean(rep_counts))

    vd = variance_decomp(events, metric)
    z = _z_for_level(level)
    half = normal_approx_halfwidth(vd["between"], vd["within"], n_items, rbar, z)
    return CI(point=point, low=point - half, high=point + half, method="normal_approx")


# ---------------------------------------------------------------------------
# Paired per-item difference CI (model-vs-model; Req 9.3)
# ---------------------------------------------------------------------------
def paired_diff_ci(
    events_a: list[TrialEvent],
    events_b: list[TrialEvent],
    metric: str,
    level: float = config.CONFIDENCE_LEVEL,
    n_boot: int = config.BOOTSTRAP_N,
    seed: int = config.BOOTSTRAP_SEED,
) -> CI:
    """Bootstrap CI on the **paired per-item difference** ``A - B`` for ``metric``.

    **Pairing.** Items are matched by ``item_id``; only items present in *both*
    models contribute. For each matched item the per-item difference is
    ``mean(A's reps) - mean(B's reps)`` (each model summarized to one equal-weight
    value per item, exactly as the cluster bootstrap weights items). Because both
    models saw the same items and the same constant retrieval, the paired design
    removes between-item variance from the comparison — far more powerful than
    differencing two independent means (design "Confidence intervals").

    The bootstrap resamples the *matched items* with replacement (item is the
    primary unit); the statistic is the mean of the per-item differences. The
    same seeded :class:`numpy.random.Generator` makes the CI deterministic.

    Returns:
        ``CI(point, low, high, method="paired_bootstrap")`` where ``point`` is
        the mean paired difference (positive => A scores higher than B).

    Raises:
        ValueError: if the two models share no item with a usable value.
    """
    a_by_item = group_rep_means_by_item(events_a, metric)
    b_by_item = group_rep_means_by_item(events_b, metric)
    common = sorted(set(a_by_item) & set(b_by_item))
    if not common:
        raise ValueError(
            f"paired_diff_ci: models share no common item with a usable value "
            f"for metric {metric!r}"
        )

    diffs = np.array([a_by_item[i] - b_by_item[i] for i in common], dtype=np.float64)
    n = diffs.size
    point = float(diffs.mean())

    alpha = 1.0 - level
    rng = np.random.default_rng(seed)
    boot = np.empty(n_boot, dtype=np.float64)
    filled = 0
    while filled < n_boot:
        b = min(_BOOT_CHUNK, n_boot - filled)
        idx = rng.integers(0, n, size=(b, n))
        boot[filled : filled + b] = diffs[idx].mean(axis=1)
        filled += b

    low = float(np.percentile(boot, 100.0 * (alpha / 2.0)))
    high = float(np.percentile(boot, 100.0 * (1.0 - alpha / 2.0)))
    return CI(point=point, low=low, high=high, method="paired_bootstrap")


# ---------------------------------------------------------------------------
# Pilot -> reps per stratum (design `estimate_required_reps`; Req 6.3/6.4/6.5)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Stratum:
    """A lightweight stratum descriptor consumed by :func:`estimate_required_reps`.

    Self-contained on purpose (the planner in Task 9 adapts to this shape):

    Attributes:
        id: stable stratum identifier (the key in the returned mapping).
        n_items: number of distinct items in the stratum (drives the between
            term ``sigma_between^2 / n``).
        turn_type: ``"single"`` or ``"multi"``; multi-turn strata are bumped to
            at least the single-turn rep level for the same target.
        passes: the pass names this stratum participates in (e.g.
            ``("wide", "deep")``); each gets its own rep count in the result.
        predicate: cohort axis -> required value used to select this stratum's
            pilot events (an event matches iff every axis in the predicate equals
            the event's cohort value). Empty predicate matches all events.
    """

    id: str
    n_items: int
    turn_type: str = "single"
    passes: tuple[str, ...] = ("wide",)
    predicate: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Budget:
    """Trial budget for :func:`estimate_required_reps`.

    ``max_trials`` clamps total (primary-pass) trials; ``max_judge_calls`` is
    carried for the planner's downstream use but not enforced here.
    """

    max_trials: int
    max_judge_calls: Optional[int] = None

    @classmethod
    def coerce(cls, budget: "Budget | Mapping[str, int]") -> "Budget":
        """Accept either a :class:`Budget` or a plain ``{"max_trials": ...}`` dict."""
        if isinstance(budget, Budget):
            return budget
        return cls(
            max_trials=int(budget["max_trials"]),
            max_judge_calls=(
                int(budget["max_judge_calls"])
                if "max_judge_calls" in budget
                else None
            ),
        )


@dataclass(frozen=True)
class RequiredRepsResult:
    """Return of :func:`estimate_required_reps`: reps + unreachable flags.

    Behaves like the design's ``dict[stratum_id, dict[pass_name, reps]]`` mapping
    (``result[stratum_id]`` -> ``{pass: reps}``, plus ``in``/``iter``/``items``/
    ``keys``), while also exposing:

    * :attr:`unreachable` — ``{stratum_id: bool}``; ``True`` iff the target CI
      width is unreachable with the available items for that stratum (the
      between-item term alone exceeds the target variance — design Property 8).
    * :attr:`measured_sigma` — ``{stratum_id: {"within": sw, "between": sb}}``,
      the per-stratum SDs measured from the pilot (for transparency/serialization
      into the sampling plan).
    """

    reps: dict[str, dict[str, int]]
    unreachable: dict[str, bool]
    measured_sigma: dict[str, dict[str, float]] = field(default_factory=dict)

    # dict-like access over ``reps`` so callers can treat the result as the
    # design's mapping shape without reaching into ``.reps``.
    def __getitem__(self, stratum_id: str) -> dict[str, int]:
        return self.reps[stratum_id]

    def __contains__(self, stratum_id: object) -> bool:
        return stratum_id in self.reps

    def __iter__(self):
        return iter(self.reps)

    def keys(self):
        return self.reps.keys()

    def items(self):
        return self.reps.items()

    def values(self):
        return self.reps.values()


def required_reps_closed_form(
    sigma_within: float,
    sigma_between: float,
    n_items: int,
    target_w: float,
    z: float,
) -> tuple[int, bool]:
    """Smallest reps ``R`` meeting the CI-width target, and an unreachable flag.

    Solves the variance equation for the minimal ``R`` such that
    ``z*sqrt(sb^2/n + sw^2/(n*R)) <= target_w`` (design pseudocode):

        need = ceil( sw^2 / (n * ((target_w/z)^2 - sb^2/n)) )

    **Unreachable case (design Property 8).** If ``sb^2 / n`` alone already meets
    or exceeds the target variance ``(target_w/z)^2``, no finite ``R`` can hit the
    target — only more items can — so this returns ``(MAX_REPS, True)`` rather
    than a finite ``R`` that does not actually meet the target. Otherwise the
    required ``R`` is floored at :data:`config.MIN_REPS_PER_STRATUM` (so a
    within-item signal always exists) and capped at :data:`MAX_REPS`; the
    *capped-but-reachable* case still returns ``unreachable=False`` (the ceiling
    is a practical cap, not a methodological impossibility).

    Returns:
        ``(reps, unreachable)``.
    """
    if n_items <= 0:
        raise ValueError("n_items must be positive")
    if target_w <= 0 or z <= 0:
        raise ValueError("target_w and z must be positive")

    target_var = (target_w / z) ** 2
    between_term = sigma_between**2 / n_items
    if target_var > between_term:
        denom = n_items * (target_var - between_term)
        need = math.ceil((sigma_within**2) / denom)
        reps = min(MAX_REPS, max(config.MIN_REPS_PER_STRATUM, need))
        return reps, False
    # between-item term alone exceeds the target variance: unreachable.
    return MAX_REPS, True


def estimate_required_reps(
    pilot_events: list[TrialEvent],
    strata: list[Stratum],
    target_w: float,
    z: float,
    budget: "Budget | Mapping[str, int]",
    metric: str = "composite",
) -> RequiredRepsResult:
    """Compute reps per stratum per pass to hit a target CI half-width.

    Follows the design pseudocode: for each stratum, measure ``sigma_within`` and
    ``sigma_between`` for ``metric`` from that stratum's pilot events (selected by
    the stratum predicate), then take the smallest ``R`` meeting the target via
    :func:`required_reps_closed_form`, floored at
    :data:`config.MIN_REPS_PER_STRATUM`. Post-processing enforces the design's two
    structural postconditions, in this precedence order:

    1. **Floor** — every returned ``R >= MIN_REPS_PER_STRATUM`` (== 2), so a
       within-item signal always exists (design Property 8). This floor is a hard
       constraint that outranks the budget.
    2. **Multi-turn equalization** — for each pass, every multi-turn stratum gets
       ``R >=`` the maximum single-turn ``R`` for that pass (a conservative reading
       of "multi-turn >= its single-turn counterpart": with fewer multi-turn
       items, equal CI width needs at least as many reps).
    3. **Budget clamp** — total primary-pass trials are scaled to fit
       ``budget.max_trials``. Proportional scaling + the floor is monotonic, so it
       *preserves* the multi-turn >= single-turn ordering from step 2. If the floor
       prevents fitting the budget, reps stay at the floor (we never trade away the
       within-item minimum to satisfy a budget).

    The ``metric`` the variance is measured on defaults to ``"composite"`` (the
    headline quality metric); a caller can size on any extractable metric.

    Args:
        pilot_events: events from the pilot run (>= 2 reps per stratum so a
            within-item SD is estimable; strata with no usable pilot data degrade
            to ``sigma = 0``, i.e. the floor).
        strata: the strata to size.
        target_w: target CI half-width (``> 0``).
        z: z-multiplier for the confidence level (``> 0``).
        budget: a :class:`Budget` or ``{"max_trials": int}`` mapping.
        metric: metric to measure variance on (default ``"composite"``).

    Returns:
        A :class:`RequiredRepsResult` (dict-like over ``{stratum: {pass: reps}}``
        plus :attr:`~RequiredRepsResult.unreachable`).
    """
    budget = Budget.coerce(budget)
    primary_pass = "wide"

    reps: dict[str, dict[str, int]] = {}
    unreachable: dict[str, bool] = {}
    measured: dict[str, dict[str, float]] = {}

    for s in strata:
        s_events = _events_in_stratum(pilot_events, s)
        vd = variance_decomp(s_events, metric)
        sw = math.sqrt(max(0.0, vd["within"]))
        sb = math.sqrt(max(0.0, vd["between"]))
        measured[s.id] = {"within": sw, "between": sb}

        need, is_unreachable = required_reps_closed_form(
            sigma_within=sw,
            sigma_between=sb,
            n_items=s.n_items,
            target_w=target_w,
            z=z,
        )
        reps[s.id] = {p: need for p in s.passes}
        unreachable[s.id] = is_unreachable

    # --- step 2: multi-turn strata >= max single-turn R, per pass ----------
    single_max_by_pass: dict[str, int] = {}
    for s in strata:
        if s.turn_type != "multi":
            for p in s.passes:
                single_max_by_pass[p] = max(single_max_by_pass.get(p, 0), reps[s.id][p])
    for s in strata:
        if s.turn_type == "multi":
            for p in s.passes:
                floor_p = single_max_by_pass.get(p, 0)
                if reps[s.id][p] < floor_p:
                    reps[s.id][p] = min(MAX_REPS, floor_p)

    # --- step 3: budget clamp on primary-pass trials -----------------------
    n_items_by_stratum = {s.id: s.n_items for s in strata}
    reps = _budget_clamp(reps, n_items_by_stratum, budget, primary_pass)

    return RequiredRepsResult(reps=reps, unreachable=unreachable, measured_sigma=measured)


def _events_in_stratum(
    events: Iterable[TrialEvent], stratum: Stratum
) -> list[TrialEvent]:
    """Select the events whose cohort matches ``stratum``'s predicate + turn_type."""
    out: list[TrialEvent] = []
    pred = dict(stratum.predicate)
    for ev in events:
        cohort = ev.cohort.to_dict()
        if stratum.turn_type and cohort.get("turn_type") != stratum.turn_type:
            # honor turn_type even when not spelled out in the predicate
            if "turn_type" not in pred:
                continue
        if all(cohort.get(axis) == val for axis, val in pred.items()):
            out.append(ev)
    return out


def _budget_clamp(
    reps: dict[str, dict[str, int]],
    n_items_by_stratum: dict[str, int],
    budget: Budget,
    primary_pass: str,
) -> dict[str, dict[str, int]]:
    """Scale reps to fit ``budget.max_trials`` primary-pass trials (floor at MIN).

    The running total mirrors the design pseudocode: it counts ``R[primary] *
    n_items`` over strata. Scaling every pass by the same factor and flooring at
    :data:`config.MIN_REPS_PER_STRATUM` is monotonic, so the multi-turn >=
    single-turn ordering established before the clamp is preserved.
    """

    def primary_total() -> int:
        return sum(
            reps[sid].get(primary_pass, max(reps[sid].values()))
            * n_items_by_stratum[sid]
            for sid in reps
        )

    total = primary_total()
    if total <= budget.max_trials or total == 0:
        return reps

    scale = budget.max_trials / total
    for sid in reps:
        for p in reps[sid]:
            scaled = int(math.floor(reps[sid][p] * scale))
            reps[sid][p] = max(config.MIN_REPS_PER_STRATUM, scaled)
    return reps
