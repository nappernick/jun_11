"""
Aggregation engine for the model-bakeoff-harness (Task 11) — codename **GBBO**.

This module derives **every reported statistic** from the append-only event log
(``data/bakeoff/trial_events.jsonl``). It is the layer that turns a pile of
:class:`~bakeoff.types.TrialEvent` lines into the numbers the live UI and the
executive speed/quality frontier render. Per design **Component 7**, it is a
**pure, deterministic function of the events** — no hidden state, no I/O except
the explicit ``materialize`` report write — so re-aggregating the same log with
the same fixed :data:`bakeoff.config.BOOTSTRAP_SEED` yields byte-identical
output (design **Property 9**). The heavy statistics (item-level cluster
bootstrap, variance decomposition, paired-difference CI) are reused verbatim
from :mod:`bakeoff.stats`; this module composes them into grouped aggregates, a
Pareto frontier, high-variance flagging, and the materialized report.

What this engine guarantees (the load-bearing invariants):

* **P4 — accuracy is never averaged across answerability classes.**
  :meth:`AggregationEngine.aggregate` *rejects* (raises
  :class:`AnswerabilityBlendError`) any group of an **accuracy** metric whose
  events span more than one ``answerability`` value. Answerable-accuracy and
  unanswerable-abstention are separate axes (Req 5.4/5.5): the engine forces the
  caller to slice by ``answerability`` first. Non-accuracy metrics (latency,
  composite, interaction dims) are unaffected.

* **P9 — aggregation is a pure deterministic function of the log.** Given the
  same events and seed, two runs produce identical :class:`Aggregate`s and
  :class:`FrontierPoint`s. The engine holds no mutable state; the seed flows into
  every bootstrap.

* **P10 — no number escapes without a CI.** Every :class:`Aggregate` either
  carries a populated :class:`CI` *or* is explicitly marked
  ``insufficient_data`` (with ``mean_ci is None``); the exclusive-or
  ``(mean_ci is None) == insufficient_data`` always holds. Every
  :class:`FrontierPoint` carries a populated quality :class:`CI` (models too thin
  for a CI are omitted from the frontier rather than plotted as a bare point).

* **Thin-cell honesty (Req 9.8, Req 13.4).** A cohort cell with fewer than
  :data:`config.MIN_ITEMS_FOR_CI` distinct items renders as insufficient-data
  rather than a confident value (a 1-item cell would bootstrap to a zero-width
  interval that lies about its certainty).

* **Latency as a distribution (Req 9.5).** Latency metrics carry
  ``{"p50","p90","p95"}`` quantiles, never a lone mean; the frontier's speed axis
  uses the median with a p90 whisker.

* **High-variance flagging (design tiered design).** :meth:`flag_high_variance`
  surfaces items whose per-item rep SD exceeds
  :data:`config.HIGH_VARIANCE_REP_SD_THRESHOLD` so the TARGETED pass can give
  them extra reps.

**Sourcing caveat.** The CI methodology this engine composes (item-level cluster
bootstrap, ANOVA variance components, paired-difference CI) is **general
statistical practice, not Amazon-internal guidance** — see the design document's
sourcing note; the internal primary sources the global steering rule prefers were
not reachable in this environment.

Dependencies: :mod:`bakeoff.stats` / :mod:`bakeoff.types` / :mod:`bakeoff.config`
+ ``numpy`` (quantiles) + stdlib ``json``. No network.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence, Union

import numpy as np

import bakeoff.config as config
from bakeoff.stats import (
    cluster_bootstrap_ci,
    extract_metric_value,
    group_rep_values_by_item,
    is_judge_metric,
    paired_diff_ci as _paired_diff_ci,
    variance_decomp,
)
from bakeoff.stats import _ACCURACY_FIELDS, _normalize_metric, _TIMING_FIELDS
from bakeoff.types import CI, Aggregate, FrontierPoint, TrialEvent

__all__ = [
    "AnswerabilityBlendError",
    "HighVarianceItem",
    "AggregationEngine",
    "COMPOSITE_METRIC",
    "SPEED_METRIC",
    "is_accuracy_metric",
    "is_latency_metric",
]

PathLike = Union[str, "Path"]

# ---------------------------------------------------------------------------
# Metric classification
# ---------------------------------------------------------------------------
#: The default quality metric driving the frontier's y-axis.
COMPOSITE_METRIC: str = "composite"
#: The default speed metric driving the frontier's x-axis (end-to-end wall clock
#: as the user feels it; design "Speed" / Req 9.5).
SPEED_METRIC: str = "end_to_end_ms"

#: Accuracy metrics that MUST NOT be averaged across answerability classes
#: (design Property 4 / Req 5.4). These are the retrieval-aligned + grounding +
#: semantic + answerability-behavior fields plus the judge's *accuracy* rubric
#: dimensions (faithfulness/correctness/completeness). The interaction dims
#: (tone/empathy/clarity/actionability) and the ``composite`` are intentionally
#: NOT here: the design's rule is specifically about *accuracy* metrics, and the
#: answerability split is the accuracy axis (Req 5.5).
_JUDGE_ACCURACY_FIELDS = frozenset({"faithfulness", "correctness", "completeness"})
_ACCURACY_METRIC_NAMES = frozenset(_ACCURACY_FIELDS) | _JUDGE_ACCURACY_FIELDS


def is_accuracy_metric(metric: str) -> bool:
    """True iff ``metric`` is an accuracy metric subject to the P4 no-blend rule.

    Accuracy metrics are the retrieval-aligned/grounding/semantic/answerability
    fields and the judge's accuracy rubric dimensions
    (faithfulness/correctness/completeness). The squishy interaction dimensions
    and the ``composite`` are not accuracy metrics for the purpose of the
    answerability-blend guard (design Req 5.4/5.5).
    """
    return _normalize_metric(metric) in _ACCURACY_METRIC_NAMES


def is_latency_metric(metric: str) -> bool:
    """True iff ``metric`` is a timing/latency metric (reported as a distribution)."""
    return _normalize_metric(metric) in _TIMING_FIELDS


# ---------------------------------------------------------------------------
# Errors + small value types
# ---------------------------------------------------------------------------
class AnswerabilityBlendError(ValueError):
    """Raised when an accuracy metric would be averaged across answerability.

    The engine refuses a group spanning more than one ``answerability`` value for
    an accuracy metric (design Property 4 / Req 5.4): answerable-accuracy and
    unanswerable-abstention are separate axes and must be sliced first. Subclasses
    :class:`ValueError` so existing broad ``ValueError`` handling still catches
    it.
    """


class HighVarianceItem:
    """One item flagged as high-variance for the TARGETED pass.

    Plain class (not a dataclass) to stay import-light and JSON-trivial. Carries
    the ``item_id``, the measured per-item rep SD on the target metric, the rep
    ``count`` the SD was computed from, and the ``model`` the flag belongs to
    (high variance is a per-(model, item) property).
    """

    __slots__ = ("item_id", "model", "rep_sd", "count", "metric")

    def __init__(
        self, item_id: str, model: str, rep_sd: float, count: int, metric: str
    ):
        self.item_id = item_id
        self.model = model
        self.rep_sd = rep_sd
        self.count = count
        self.metric = metric

    def to_dict(self) -> dict[str, object]:
        return {
            "item_id": self.item_id,
            "model": self.model,
            "rep_sd": self.rep_sd,
            "count": self.count,
            "metric": self.metric,
        }

    def __eq__(self, other: object) -> bool:  # value equality for tests
        if not isinstance(other, HighVarianceItem):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"HighVarianceItem(item_id={self.item_id!r}, model={self.model!r}, "
            f"rep_sd={self.rep_sd:.4f}, count={self.count}, metric={self.metric!r})"
        )


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------
class AggregationEngine:
    """Pure, deterministic aggregation over the trial event log (design Component 7).

    Construction takes only the bootstrap parameters (seed/level/iterations) so a
    single engine instance is stateless w.r.t. the data — calling any method twice
    with the same events yields identical results (Property 9). All defaults come
    from :mod:`bakeoff.config`, so the harness's reproducibility knobs live in one
    place.

    Args:
        level: confidence level for every CI (default
            :data:`config.CONFIDENCE_LEVEL`).
        n_boot: bootstrap iterations (default :data:`config.BOOTSTRAP_N`).
        seed: fixed RNG seed so aggregation is reproducible (default
            :data:`config.BOOTSTRAP_SEED`) — the spine of Property 9.
        min_items_for_ci: distinct-item floor below which a cell is marked
            insufficient-data (default :data:`config.MIN_ITEMS_FOR_CI`).
        high_variance_rep_sd: per-item rep-SD threshold for TARGETED-pass flagging
            (default :data:`config.HIGH_VARIANCE_REP_SD_THRESHOLD`).
    """

    def __init__(
        self,
        *,
        level: float = config.CONFIDENCE_LEVEL,
        n_boot: int = config.BOOTSTRAP_N,
        seed: int = config.BOOTSTRAP_SEED,
        min_items_for_ci: int = config.MIN_ITEMS_FOR_CI,
        high_variance_rep_sd: float = config.HIGH_VARIANCE_REP_SD_THRESHOLD,
    ):
        self.level = level
        self.n_boot = n_boot
        self.seed = seed
        self.min_items_for_ci = min_items_for_ci
        self.high_variance_rep_sd = high_variance_rep_sd

    # -- canonicalization (order-invariance, the spine of Property 9) -------
    @staticmethod
    def _canonical(events: Iterable[TrialEvent]) -> list[TrialEvent]:
        """Return events in a content-derived canonical order.

        The underlying item-level cluster bootstrap (:mod:`bakeoff.stats`) indexes
        items and reps by their *position* in the event list, so a seeded RNG only
        yields identical draws if the events arrive in an identical order. The
        engine therefore sorts by :attr:`TrialEvent.trial_id` — a deterministic
        hash of ``(model, item_id, rep, pass_name, plan_version)`` and unique per
        trial — so the bootstrap sees a fixed order that depends only on *which*
        trials are present, not on the order they were read from / appended to the
        log. This upgrades Property 9 from "same list -> same output" to the
        stronger, genuinely-defensible "same set of trials -> same output",
        regardless of input ordering, without mutating the reusable stats core.
        """
        return sorted(events, key=lambda e: e.trial_id)

    # -- grouping -----------------------------------------------------------
    @staticmethod
    def _group_value(event: TrialEvent, dim: str) -> str:
        """Resolve one ``group_by`` dimension to its string value on an event.

        ``"model"`` and ``"pass"`` address top-level identity; everything else is
        a cohort axis on :class:`~bakeoff.types.CohortKey`. An unknown dimension is
        a programming error and raises :class:`KeyError`.
        """
        if dim == "model":
            return event.model
        if dim == "pass":
            return event.pass_name
        # cohort axis (geography/proficiency/tone/entry_route/momentary_state/
        # answerability/turn_type) — validated against the CohortKey fields.
        if hasattr(event.cohort, dim):
            return getattr(event.cohort, dim)
        raise KeyError(
            f"unknown group_by dimension {dim!r}; expected 'model', 'pass', or a "
            f"cohort axis"
        )

    def _group_events(
        self, events: Sequence[TrialEvent], group_by: Sequence[str]
    ) -> "dict[tuple[str, ...], list[TrialEvent]]":
        """Partition events into groups keyed by the ``group_by`` tuple of values.

        Deterministic: groups are returned in **sorted key order** so downstream
        list output is stable run-to-run (a building block of Property 9).
        """
        buckets: dict[tuple[str, ...], list[TrialEvent]] = defaultdict(list)
        for ev in events:
            key = tuple(self._group_value(ev, dim) for dim in group_by)
            buckets[key].append(ev)
        # canonicalize within-group order too, so the bootstrap (which indexes by
        # position) sees a fixed order regardless of input ordering (Property 9).
        return {k: self._canonical(buckets[k]) for k in sorted(buckets)}

    # -- the P4 guard -------------------------------------------------------
    def _reject_answerability_blend(
        self, group_events: Sequence[TrialEvent], metric: str, group: dict[str, str]
    ) -> None:
        """Enforce Property 4: refuse an accuracy metric blended across answerability.

        Raises :class:`AnswerabilityBlendError` when ``metric`` is an accuracy
        metric and the group's events carry more than one distinct
        ``answerability`` value. The check reads the *event's* ``answerability``
        field (the authoritative per-trial label), so it fires whether or not
        ``answerability`` is one of the ``group_by`` dimensions — slicing by
        ``answerability`` is exactly what makes a group safe.
        """
        if not is_accuracy_metric(metric):
            return
        classes = {ev.answerability for ev in group_events}
        if len(classes) > 1:
            raise AnswerabilityBlendError(
                f"refusing to average accuracy metric {metric!r} across "
                f"answerability classes {sorted(classes)} for group {group!r}: "
                f"answerable-accuracy and unanswerable-abstention are separate "
                f"axes (Req 5.4/5.5) — slice by 'answerability' first"
            )

    # -- latency quantiles --------------------------------------------------
    @staticmethod
    def _latency_quantiles(
        group_events: Sequence[TrialEvent], metric: str
    ) -> Optional[dict[str, float]]:
        """Return ``{"p50","p90","p95"}`` for a latency metric, else ``None``.

        Quantiles are computed over **all reps** (the per-trial latency
        distribution the user feels), using linear interpolation
        (``numpy.percentile`` default), which is deterministic given the inputs.
        """
        if not is_latency_metric(metric):
            return None
        vals = [
            v
            for ev in group_events
            if (v := extract_metric_value(ev, metric)) is not None
        ]
        if not vals:
            return None
        arr = np.asarray(vals, dtype=np.float64)
        return {
            "p50": float(np.percentile(arr, 50)),
            "p90": float(np.percentile(arr, 90)),
            "p95": float(np.percentile(arr, 95)),
        }

    # -- one aggregate ------------------------------------------------------
    def _aggregate_one(
        self, group: dict[str, str], group_events: Sequence[TrialEvent], metric: str
    ) -> Aggregate:
        """Build a single :class:`Aggregate` for one group + metric.

        Encodes the thin-cell + P10 contract: if the group has fewer than
        :attr:`min_items_for_ci` distinct items with a usable value, the result is
        marked ``insufficient_data`` with ``mean_ci=None`` (no fabricated number);
        otherwise it carries a populated cluster-bootstrap CI. The
        exclusive-or ``(mean_ci is None) == insufficient_data`` always holds.
        """
        # P4 guard first — a blended accuracy group is an error, not a thin cell.
        self._reject_answerability_blend(group_events, metric, group)

        by_item = group_rep_values_by_item(group_events, metric)
        n_items = len(by_item)
        n_trials = sum(len(v) for v in by_item.values())
        vdecomp = variance_decomp(group_events, metric)
        latency = self._latency_quantiles(group_events, metric)

        if n_items < self.min_items_for_ci:
            # Thin cell: explicitly marked, never a confident value (Req 9.8/13.4).
            return Aggregate(
                group=dict(group),
                metric=metric,
                n_items=n_items,
                n_trials=n_trials,
                mean_ci=None,
                variance_decomp=vdecomp,
                latency_quantiles=latency,
                insufficient_data=True,
            )

        ci = cluster_bootstrap_ci(
            list(group_events),
            metric,
            level=self.level,
            n_boot=self.n_boot,
            seed=self.seed,
        )
        return Aggregate(
            group=dict(group),
            metric=metric,
            n_items=n_items,
            n_trials=n_trials,
            mean_ci=ci,
            variance_decomp=vdecomp,
            latency_quantiles=latency,
            insufficient_data=False,
        )

    def aggregate(
        self,
        events: Sequence[TrialEvent],
        group_by: Sequence[str],
        metric: str = COMPOSITE_METRIC,
    ) -> list[Aggregate]:
        """Aggregate ``metric`` over ``events`` grouped by ``group_by``.

        ``group_by`` is a list of cohort dimensions and/or ``"model"`` / ``"pass"``
        (e.g. ``["model"]``, ``["model", "answerability"]``, ``["momentary_state"]``
        for a heatmap column). Each resulting group becomes one :class:`Aggregate`
        carrying a mean CI (item-level cluster bootstrap), the between/within/judge
        variance decomposition, and — for latency metrics — p50/p90/p95 quantiles.

        **Property 4 (no answerability blend).** If ``metric`` is an accuracy
        metric and *any* produced group would span more than one ``answerability``
        value, the whole call raises :class:`AnswerabilityBlendError`. The caller
        must add ``"answerability"`` to ``group_by`` (or pre-filter) so accuracy is
        sliced, never blended. Latency/composite/interaction metrics are exempt.

        **Property 10 (no number without a CI).** Every returned aggregate either
        has a populated ``mean_ci`` or ``insufficient_data is True`` with
        ``mean_ci is None`` — never neither, never both.

        **Property 9 (purity/determinism).** Groups are emitted in sorted-key
        order and every CI uses the fixed seed, so the returned list is identical
        across runs on the same events.

        Args:
            events: the trial events to aggregate (the log, or a subset).
            group_by: dimensions to group by; must be non-empty.
            metric: the metric to summarize (default ``"composite"``).

        Returns:
            One :class:`Aggregate` per non-empty group, in sorted-key order.

        Raises:
            ValueError: if ``group_by`` is empty.
            AnswerabilityBlendError: if an accuracy metric would be blended across
                answerability classes (Property 4).
            KeyError: if a ``group_by`` dimension or ``metric`` is unknown.
        """
        if not group_by:
            raise ValueError("group_by must contain at least one dimension")
        group_by = list(group_by)

        grouped = self._group_events(events, group_by)
        out: list[Aggregate] = []
        for key, group_events in grouped.items():
            group = {dim: val for dim, val in zip(group_by, key)}
            out.append(self._aggregate_one(group, group_events, metric))
        return out

    # -- paired model-vs-model ---------------------------------------------
    def paired_diff_ci(
        self,
        events: Sequence[TrialEvent],
        model_a: str,
        model_b: str,
        metric: str = COMPOSITE_METRIC,
    ) -> CI:
        """CI on the paired per-item difference ``model_a - model_b`` for ``metric``.

        Splits ``events`` by model, then defers to
        :func:`bakeoff.stats.paired_diff_ci`, which matches items seen by *both*
        models and bootstraps the per-item differences (the same items + same
        constant retrieval make this far more powerful than differencing two
        independent means — design "Confidence intervals", Req 9.3).

        A positive ``point`` means ``model_a`` scores higher. Deterministic given
        the engine's seed. Raises :class:`ValueError` if the two models share no
        item with a usable value for ``metric``.
        """
        a = [ev for ev in events if ev.model == model_a]
        b = [ev for ev in events if ev.model == model_b]
        return _paired_diff_ci(
            self._canonical(a),
            self._canonical(b),
            metric,
            level=self.level,
            n_boot=self.n_boot,
            seed=self.seed,
        )

    # -- frontier -----------------------------------------------------------
    def frontier(
        self,
        events: Sequence[TrialEvent],
        quality_metric: str = COMPOSITE_METRIC,
        speed_metric: str = SPEED_METRIC,
    ) -> list[FrontierPoint]:
        """Build the speed/quality Pareto frontier, one point per model.

        For each model the engine computes:

        * **quality** — an item-level cluster-bootstrap :class:`CI` on
          ``quality_metric`` (the y-axis with its CI band); and
        * **speed** — the p50 and p90 of ``speed_metric`` over all the model's
          trials (the x-axis median + p90 whisker; design "Primary view").

        Pareto dominance is computed on **(speed_p50 lower-is-better, quality.point
        higher-is-better)**: model ``X`` is dominated iff some other model is at
        least as good on both axes and strictly better on at least one.
        Non-dominated models get ``on_pareto_front=True``; dominated ones are
        flagged ``False`` (the exec viz de-emphasizes them).

        **Property 10.** Every emitted :class:`FrontierPoint` carries a populated
        quality CI. A model too thin for a CI (fewer than
        :attr:`min_items_for_ci` distinct items) is **omitted** from the frontier
        rather than plotted as a bare point — no number reaches the viz without a
        CI. (Such models still surface as ``insufficient_data`` aggregates via
        :meth:`aggregate`.)

        **Property 9.** Output is sorted by model name and every CI uses the fixed
        seed, so the frontier is identical across runs on the same events.

        Args:
            events: the trial events (typically the whole log).
            quality_metric: metric for the quality axis (default ``"composite"``).
            speed_metric: latency metric for the speed axis (default
                ``"end_to_end_ms"``).

        Returns:
            A list of :class:`FrontierPoint`, sorted by model name.
        """
        by_model: dict[str, list[TrialEvent]] = defaultdict(list)
        for ev in events:
            by_model[ev.model].append(ev)

        # First pass: compute quality CI + speed quantiles per model, skipping
        # models too thin for a quality CI (Property 10) or with no speed data.
        raw: list[tuple[str, CI, float, float]] = []
        for model in sorted(by_model):
            model_events = self._canonical(by_model[model])
            q_by_item = group_rep_values_by_item(model_events, quality_metric)
            if len(q_by_item) < self.min_items_for_ci:
                continue  # too thin for a defensible CI -> not on the frontier
            speed_vals = [
                v
                for ev in model_events
                if (v := extract_metric_value(ev, speed_metric)) is not None
            ]
            if not speed_vals:
                continue
            quality_ci = cluster_bootstrap_ci(
                model_events,
                quality_metric,
                level=self.level,
                n_boot=self.n_boot,
                seed=self.seed,
            )
            speed_arr = np.asarray(speed_vals, dtype=np.float64)
            p50 = float(np.percentile(speed_arr, 50))
            p90 = float(np.percentile(speed_arr, 90))
            raw.append((model, quality_ci, p50, p90))

        # Second pass: Pareto flag on (speed_p50 lower-better, quality higher-better).
        points: list[FrontierPoint] = []
        for i, (model, quality_ci, p50, p90) in enumerate(raw):
            dominated = False
            for j, (other_m, other_q, other_p50, _) in enumerate(raw):
                if i == j:
                    continue
                # other dominates self iff at least as good on both axes and
                # strictly better on at least one (speed: lower; quality: higher).
                at_least_as_good = (
                    other_p50 <= p50 and other_q.point >= quality_ci.point
                )
                strictly_better = (
                    other_p50 < p50 or other_q.point > quality_ci.point
                )
                if at_least_as_good and strictly_better:
                    dominated = True
                    break
            points.append(
                FrontierPoint(
                    model=model,
                    quality=quality_ci,
                    speed_p50_ms=p50,
                    speed_p90_ms=p90,
                    on_pareto_front=not dominated,
                )
            )
        return points

    # -- high-variance flagging (TARGETED pass) ----------------------------
    def flag_high_variance(
        self,
        events: Sequence[TrialEvent],
        metric: str = COMPOSITE_METRIC,
        pass_name: str = "wide",
    ) -> list[HighVarianceItem]:
        """Flag high-variance (model, item) pairs for the TARGETED pass.

        Per the design's tiered design, items whose **per-item rep SD** on
        ``metric`` exceeds :attr:`high_variance_rep_sd` are individually unstable
        and most likely to flip a decision, so they earn extra reps in the
        TARGETED pass. Variance is a per-(model, item) property: the same item may
        be stable for one model and unstable for another, so flags are keyed by
        both.

        Only events from ``pass_name`` are considered (the WIDE pass is what flags
        items *for* the TARGETED pass). An item needs >= 2 reps for a rep SD to be
        defined; items with a single rep cannot be flagged. The sample SD
        (``ddof=1``) is used.

        Args:
            events: trial events to scan.
            metric: the metric whose rep SD is measured (default ``"composite"``).
            pass_name: only events from this pass are considered (default
                ``"wide"``).

        Returns:
            High-variance items sorted by descending rep SD (then model, item_id
            for a deterministic tie-break) — Property 9 holds for this output too.
        """
        relevant = [ev for ev in events if ev.pass_name == pass_name]
        by_pair: dict[tuple[str, str], list[float]] = defaultdict(list)
        for ev in relevant:
            v = extract_metric_value(ev, metric)
            if v is not None:
                by_pair[(ev.model, ev.item_id)].append(v)

        flagged: list[HighVarianceItem] = []
        for (model, item_id), vals in by_pair.items():
            if len(vals) < 2:
                continue  # rep SD undefined with a single rep
            rep_sd = float(np.std(vals, ddof=1))
            if rep_sd > self.high_variance_rep_sd:
                flagged.append(
                    HighVarianceItem(
                        item_id=item_id,
                        model=model,
                        rep_sd=rep_sd,
                        count=len(vals),
                        metric=metric,
                    )
                )
        flagged.sort(key=lambda f: (-f.rep_sd, f.model, f.item_id))
        return flagged

    # -- report materialization --------------------------------------------
    def materialize(
        self,
        events: Sequence[TrialEvent],
        plan_version: str,
        *,
        reports_dir: PathLike = config.REPORTS_DIR,
        cohort_dimensions: Sequence[str] = ("momentary_state", "geography"),
        judge_human_agreement: Optional[dict[str, float]] = None,
        generated_at: Optional[str] = None,
    ) -> Path:
        """Materialize the aggregate report the exec viz reads, to ``reports_dir``.

        Writes ``reports_dir/aggregate_<plan_version>.json`` — the file the
        executive visualization layer (Task 14) loads via its ``/exec/...`` routes.
        The report bundles, all derived purely from ``events`` with the fixed seed:

        * ``frontier`` — the speed/quality Pareto points (each with a quality CI);
        * ``by_model`` — per-model composite aggregates;
        * ``safety`` — per-(model, answerability) abstention/accuracy aggregates,
          sliced by answerability so accuracy is **never blended** (Property 4);
        * ``cohort_heatmaps`` — per (model × cohort-dimension) composite aggregates
          for each dimension in ``cohort_dimensions`` (the heatmap cells, faded
          when ``insufficient_data``);
        * ``high_variance`` — items flagged for the TARGETED pass;
        * ``provenance`` — plan_version, n_items, total trials, judge model,
          judge↔human agreement, CI method, and date (the footer every exec chart
          carries, Req 11.7).

        Every number in the report carries a CI or is marked ``insufficient_data``
        (Property 10). The write is the engine's *only* side effect; the computed
        content is a pure function of the inputs (``generated_at`` included).

        Args:
            events: the trial events (the log).
            plan_version: the sampling-plan version (names the output file and the
                provenance footer; mixing plan versions is the caller's concern).
            reports_dir: output directory (default :data:`config.REPORTS_DIR`).
            cohort_dimensions: cohort axes to build heatmaps for.
            judge_human_agreement: optional judge↔human agreement per dimension,
                surfaced in the provenance footer (Req 11.7, reported not gated).
            generated_at: ISO-8601 generation timestamp for the provenance footer.
                Defaults to the current UTC time at write — the report file always
                carries a real wall-clock stamp. The *statistical content* stays a
                pure deterministic function of the log + seed (Property 9); only
                this provenance field reflects when the file was written. Pass an
                explicit value to obtain a byte-identical file across runs.

        Returns:
            The path of the written report file.
        """
        events = list(events)
        if generated_at is None:
            generated_at = datetime.now(timezone.utc).isoformat()
        report = self.build_report(
            events,
            plan_version,
            cohort_dimensions=cohort_dimensions,
            judge_human_agreement=judge_human_agreement,
            generated_at=generated_at,
        )
        out_dir = Path(reports_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"aggregate_{plan_version}.json"
        out_path.write_text(
            json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        return out_path

    def build_report(
        self,
        events: Sequence[TrialEvent],
        plan_version: str,
        *,
        cohort_dimensions: Sequence[str] = ("momentary_state", "geography"),
        judge_human_agreement: Optional[dict[str, float]] = None,
        generated_at: Optional[str] = None,
    ) -> dict[str, object]:
        """Build the report payload (pure; no I/O) — see :meth:`materialize`.

        Exposed separately so tests and the exec data route can assert on the
        structure without touching the filesystem. The payload is JSON-ready
        (only dicts/lists/str/number/bool/None) and is a **pure deterministic
        function of its arguments** — including ``generated_at`` — so two calls
        with identical arguments produce identical output (Property 9). The
        timestamp is passed in rather than read from the clock here precisely to
        keep this function pure; :meth:`materialize` owns the clock read.
        """
        events = list(events)

        frontier = [self._frontier_point_to_dict(fp) for fp in self.frontier(events)]

        by_model = [
            self._aggregate_to_dict(agg)
            for agg in self.aggregate(events, ["model"], COMPOSITE_METRIC)
        ]

        # Safety panel: accuracy sliced by answerability (never blended — P4).
        safety = [
            self._aggregate_to_dict(agg)
            for agg in self.aggregate(
                events, ["model", "answerability"], "abstention_correct"
            )
        ]

        cohort_heatmaps: dict[str, list[dict]] = {}
        for dim in cohort_dimensions:
            cohort_heatmaps[dim] = [
                self._aggregate_to_dict(agg)
                for agg in self.aggregate(events, ["model", dim], COMPOSITE_METRIC)
            ]

        high_variance = [hv.to_dict() for hv in self.flag_high_variance(events)]

        distinct_items = {ev.item_id for ev in events}
        judge_models = sorted({ev.quality.judge.judge_model for ev in events})
        provenance = {
            "plan_version": plan_version,
            "generated_at": generated_at,
            "n_items": len(distinct_items),
            "n_trials": len(events),
            "judge_model": judge_models[0] if len(judge_models) == 1 else judge_models,
            "judge_human_agreement": dict(judge_human_agreement or {}),
            "ci_method": "cluster_bootstrap",
            "ci_level": self.level,
            "bootstrap_n": self.n_boot,
            "bootstrap_seed": self.seed,
            "schema_version": sorted({ev.schema_version for ev in events}),
        }

        return {
            "frontier": frontier,
            "by_model": by_model,
            "safety": safety,
            "cohort_heatmaps": cohort_heatmaps,
            "high_variance": high_variance,
            "provenance": provenance,
        }

    # -- JSON helpers -------------------------------------------------------
    @staticmethod
    def _ci_to_dict(ci: Optional[CI]) -> Optional[dict[str, object]]:
        return None if ci is None else asdict(ci)

    @classmethod
    def _aggregate_to_dict(cls, agg: Aggregate) -> dict[str, object]:
        """JSON-ready dict for an :class:`Aggregate`, preserving the P10 contract.

        ``mean_ci`` serializes to ``null`` exactly when ``insufficient_data`` is
        ``True`` — the exec data route can therefore detect (and refuse to render
        as a confident value) any cell lacking a CI directly from the JSON.
        """
        return {
            "group": dict(agg.group),
            "metric": agg.metric,
            "n_items": agg.n_items,
            "n_trials": agg.n_trials,
            "mean_ci": cls._ci_to_dict(agg.mean_ci),
            "variance_decomp": dict(agg.variance_decomp),
            "latency_quantiles": (
                None if agg.latency_quantiles is None else dict(agg.latency_quantiles)
            ),
            "insufficient_data": agg.insufficient_data,
        }

    @classmethod
    def _frontier_point_to_dict(cls, fp: FrontierPoint) -> dict[str, object]:
        return {
            "model": fp.model,
            "quality": cls._ci_to_dict(fp.quality),
            "speed_p50_ms": fp.speed_p50_ms,
            "speed_p90_ms": fp.speed_p90_ms,
            "on_pareto_front": fp.on_pareto_front,
        }
