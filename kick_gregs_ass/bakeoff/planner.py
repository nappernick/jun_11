"""
Sampling planner + pilot for the model-bakeoff-harness (Task 9, codename GBBO).

This module turns the dataset's cohort structure and a *pilot* run's measured
variance into a :class:`bakeoff.types.SamplingPlan` — the code-external,
data-not-code experiment description (design **AD-6**). The full run reads that
plan; changing the experiment (more multi-turn reps, a different temperature, a
tighter target CI) is editing/regenerating ``data/bakeoff/sampling_plan.json``,
never editing the runner.

Statistical spine (design "Statistical methodology"; sourcing caveat: this is
**general statistical practice, not Amazon-internal guidance**):

* **The dominant power is the number of distinct ITEMS, not reps.** For a cohort
  mean over ``n`` items at ``R`` reps each,
  ``Var(Ybar) ~= sigma_between^2/n + sigma_within^2/(n*R)`` — the first term does
  not depend on ``R`` at all. So the plan is **tiered**:

  - **WIDE pass** — *every* item, small reps. This is the backbone: large ``n``
    drives the dominant between-item term down, giving tight aggregate/cohort
    CIs. ``R_wide >= 2`` (the floor in :data:`bakeoff.config.MIN_REPS_PER_STRATUM`)
    so a within-item signal exists for every item.
  - **DEEP pass** — a *stratified subsample* (every non-empty, possibly-collapsed
    cohort cell represented), more reps. Purpose: a clean per-stratum
    ``sigma_within`` estimate and a qualitative read on tails.

* **Reps are pilot-driven, never hard-coded** ("choose by pilot, not by gut").
  :meth:`SamplingPlanner.required_reps` delegates the rep arithmetic to
  :func:`bakeoff.stats.estimate_required_reps`, which solves the variance
  equation for the smallest ``R`` meeting the target CI half-width, floors at 2,
  bumps multi-turn strata to ``>=`` their single-turn counterparts, clamps to a
  budget, and flags strata whose target is *unreachable* with the available items
  (``sigma_between^2/n`` alone over target — only more items, never more reps,
  can fix it).

* **Temperature ~0.2 is a starting point the pilot confirms or overrides.** The
  pilot pass runs at the starting temperature; the finalized plan records the
  confirmed temperature.

Sparse-cell collapse rule (design "build the stratified subsample"):
    The full cohort key has 7 axes; on the real dataset that is ~1006 cells over
    1300 items, ~804 of them singletons — far too sparse to estimate within-item
    variance per cell. We therefore **collapse** via a fixed finest->coarsest
    hierarchy (:data:`DEFAULT_COLLAPSE_HIERARCHY`) using a *disjoint progressive*
    rule: at each level, any projected-key group of still-unclaimed items with
    ``>= min_items`` members becomes a final stratum and claims those items;
    leftovers fall through to the next (coarser) level; at the coarsest level
    every remaining item is emitted regardless of size (and flagged
    insufficient-data). **Every level retains both ``answerability`` and
    ``turn_type``**, so a stratum can never mix answerability classes (Req 5.4)
    or turn types (Req 6.4) — the multi-turn rep bump and the "never average
    accuracy across answerability" rule are thus structurally guaranteed.

Demo posture: :meth:`SamplingPlanner.demo_plan` produces a tiny, offline-friendly
plan (few items, low reps, hard trial cap) so an end-to-end demo can run against
the mock adapters with no real model calls and no pilot.

Dependencies: stdlib (``dataclasses``/``json``/``math``/``statistics``/
``collections``) + :mod:`bakeoff.stats` (the single estimator) + :mod:`bakeoff.types`
/ :mod:`bakeoff.config`. No network.
"""
from __future__ import annotations

import dataclasses
import json
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence, Union

from bakeoff import config
from bakeoff.stats import Budget, Stratum, estimate_required_reps, variance_decomp
from bakeoff.types import (
    COHORT_DIMENSIONS,
    CohortKey,
    Item,
    SamplingPlan,
    StratumPlan,
    TrialEvent,
)

__all__ = [
    "DEFAULT_COLLAPSE_HIERARCHY",
    "DEFAULT_MIN_ITEMS_PER_STRATUM",
    "DEFAULT_SUBSAMPLE_PER_STRATUM",
    "DEFAULT_PLAN_VERSION",
    "SubsampleStratum",
    "StratifiedSubsample",
    "VarianceModel",
    "SamplingPlanner",
    "plan_to_dict",
    "plan_from_dict",
    "write_plan",
    "read_plan",
]

PathLike = Union[str, "Path"]

# ---------------------------------------------------------------------------
# Collapse hierarchy + planner defaults (statistical parameters, not dataset
# sizes — Req 1.7: nothing here hard-codes how many items the dataset has).
# ---------------------------------------------------------------------------
#: Finest -> coarsest cohort keys for the disjoint progressive collapse. EVERY
#: level MUST include ``answerability`` and ``turn_type`` (enforced in
#: :meth:`SamplingPlanner.__init__`) so no stratum ever mixes answerability
#: classes (Req 5.4) or single/multi turns (Req 6.4). The coarsest level is the
#: protected floor: ``(answerability, turn_type)`` is never collapsed away.
DEFAULT_COLLAPSE_HIERARCHY: tuple[tuple[str, ...], ...] = (
    COHORT_DIMENSIONS,  # full 7-axis key
    ("proficiency", "tone", "answerability", "turn_type"),
    ("proficiency", "answerability", "turn_type"),
    ("answerability", "turn_type"),  # protected floor
)

#: Minimum distinct items for a cohort group to stand alone as a stratum. Groups
#: smaller than this collapse to a coarser key so each stratum has enough items
#: to estimate between-item spread (and, with reps, within-item variance).
DEFAULT_MIN_ITEMS_PER_STRATUM: int = 8

#: How many distinct items per stratum to draw into the DEEP/pilot subsample.
#: Multiple items per stratum give a per-stratum between-item read; reps (not
#: items) supply the within-item estimate.
DEFAULT_SUBSAMPLE_PER_STRATUM: int = 8

#: Default plan-version stamp. A run may override it to keep plan versions from
#: mixing in one aggregate (Req 12.3).
DEFAULT_PLAN_VERSION: str = "plan-v1"

# Pass names (kept as constants so the runner and planner agree on spelling).
PASS_WIDE = "wide"
PASS_DEEP = "deep"
PASS_PILOT = "pilot"


def _z_for_level(level: float) -> float:
    """Two-sided z-multiplier for a confidence ``level`` (e.g. 0.95 -> ~1.96)."""
    alpha = 1.0 - level
    return statistics.NormalDist().inv_cdf(1.0 - alpha / 2.0)


# ---------------------------------------------------------------------------
# Subsample / stratum data structures
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SubsampleStratum:
    """One collapsed cohort stratum and its sampled representatives.

    Attributes:
        id: stable, readable stratum id (``"axis=val|axis=val|..."`` in cohort
            axis order).
        key_axes: the collapse level used for this stratum (a tuple of axis
            names from :data:`DEFAULT_COLLAPSE_HIERARCHY`).
        predicate: ``axis -> value`` for this stratum's projected cohort key.
            Always contains ``answerability`` and ``turn_type``.
        turn_type: ``"single"`` or ``"multi"`` (read from the predicate).
        n_items: number of distinct WIDE-universe items in this stratum (the
            between-item power that drives the WIDE cohort CI).
        item_ids: every WIDE-universe item id assigned to this stratum.
        subsample_item_ids: the deterministically-chosen DEEP/pilot subset
            (always non-empty; at most ``per_stratum`` ids).
        collapsed: ``True`` iff this stratum is coarser than the full cohort key.
        sufficient: ``True`` iff ``n_items >= min_items`` (a thin stratum at the
            protected floor is kept but flagged ``False`` — insufficient-data).
    """

    id: str
    key_axes: tuple[str, ...]
    predicate: dict[str, str]
    turn_type: str
    n_items: int
    item_ids: tuple[str, ...]
    subsample_item_ids: tuple[str, ...]
    collapsed: bool
    sufficient: bool


@dataclass(frozen=True)
class StratifiedSubsample:
    """The stratified subsample: collapsed strata + the chosen representatives.

    Guarantees (asserted by :meth:`SamplingPlanner.build_subsample`):

    * Every WIDE-universe item belongs to exactly one stratum (disjoint cover).
    * Every non-empty (possibly-collapsed) cohort cell is represented by at least
      one subsample item — :attr:`subsample_items` covers every stratum.
    """

    strata: tuple[SubsampleStratum, ...]
    min_items: int
    per_stratum: int
    items_by_id: dict[str, Item] = field(default_factory=dict)

    @property
    def subsample_item_ids(self) -> list[str]:
        """All chosen subsample item ids (sorted, deduped, deterministic)."""
        out: set[str] = set()
        for s in self.strata:
            out.update(s.subsample_item_ids)
        return sorted(out)

    @property
    def subsample_items(self) -> list[Item]:
        """The chosen subsample :class:`Item` objects, in subsample-id order."""
        return [self.items_by_id[i] for i in self.subsample_item_ids if i in self.items_by_id]

    @property
    def thin_strata(self) -> list[str]:
        """Ids of strata still below ``min_items`` after collapsing (Req 13.4)."""
        return [s.id for s in self.strata if not s.sufficient]


@dataclass(frozen=True)
class VarianceModel:
    """Per-stratum within/between SDs measured from the pilot (design step 3).

    ``per_stratum[stratum_id] = {"within": sigma_within, "between": sigma_between}``.
    Produced by :meth:`SamplingPlanner.estimate_variances` and embedded into the
    serialized plan for transparency (so the sizing is auditable).
    """

    metric: str
    per_stratum: dict[str, dict[str, float]]


# ---------------------------------------------------------------------------
# Collapse engine (disjoint progressive) — shared by items and pilot events
# ---------------------------------------------------------------------------
@dataclass
class _StratumBuild:
    """Mutable scratch stratum used while collapsing (internal)."""

    key_axes: tuple[str, ...]
    predicate: dict[str, str]
    item_ids: list[str]
    sufficient: bool


def _stratum_id(predicate: Mapping[str, str]) -> str:
    """Stable, readable id for a stratum from its predicate (cohort-axis order)."""
    return "|".join(
        f"{axis}={predicate[axis]}" for axis in COHORT_DIMENSIONS if axis in predicate
    )


def _collapse(
    records: Iterable[tuple[str, Mapping[str, str]]],
    hierarchy: Sequence[Sequence[str]],
    min_items: int,
) -> list[_StratumBuild]:
    """Disjoint progressive collapse of ``(item_id, full_cohort)`` records.

    At each level (finest -> coarsest), group the still-unclaimed items by their
    projected key; any group with ``>= min_items`` members becomes a final
    stratum and claims those items. Remaining items fall through to the next
    level. At the coarsest level every remaining item is emitted regardless of
    size (flagged ``sufficient=False`` if still below ``min_items``).

    Each item lands in exactly one stratum, and membership counts are exact (the
    sufficiency test is over *unclaimed* members, not the raw projection count).

    Returns the strata in a deterministic order (level, then sorted predicate).
    """
    # Dedup to distinct items (a multi-rep event list collapses to one cohort
    # per item; cohorts are identical across an item's reps).
    cohort_by_item: dict[str, dict[str, str]] = {}
    for item_id, cohort in records:
        cohort_by_item[item_id] = dict(cohort)

    unclaimed: set[str] = set(cohort_by_item)
    n_levels = len(hierarchy)
    builds: list[_StratumBuild] = []

    for level_idx, axes in enumerate(hierarchy):
        axes = tuple(axes)
        is_coarsest = level_idx == n_levels - 1
        groups: dict[tuple[str, ...], list[str]] = defaultdict(list)
        for item_id in sorted(unclaimed):
            cohort = cohort_by_item[item_id]
            key = tuple(cohort[a] for a in axes)
            groups[key].append(item_id)
        for key in sorted(groups):
            members = groups[key]
            big_enough = len(members) >= min_items
            if big_enough or is_coarsest:
                predicate = {a: v for a, v in zip(axes, key)}
                builds.append(
                    _StratumBuild(
                        key_axes=axes,
                        predicate=predicate,
                        item_ids=list(members),
                        sufficient=big_enough,
                    )
                )
                for m in members:
                    unclaimed.discard(m)

    builds.sort(key=lambda b: _stratum_id(b.predicate))
    return builds


# ---------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------
class SamplingPlanner:
    """Build the stratified subsample, run the pilot, and size the full plan.

    The planner is lightly stateful: :meth:`build_subsample` caches the last
    built :class:`StratifiedSubsample` on the instance so :meth:`pilot_plan`,
    :meth:`estimate_variances`, and :meth:`required_reps` can reuse it. Every
    method also accepts the subsample explicitly for stateless use/testing.

    Args:
        collapse_hierarchy: finest->coarsest cohort keys for collapsing. Every
            level MUST include ``answerability`` and ``turn_type``.
        min_items: minimum distinct items for a group to stand alone as a
            stratum (sparser groups collapse to a coarser key).
        subsample_per_stratum: how many items per stratum to draw into the
            DEEP/pilot subsample.
    """

    def __init__(
        self,
        *,
        collapse_hierarchy: Sequence[Sequence[str]] = DEFAULT_COLLAPSE_HIERARCHY,
        min_items: int = DEFAULT_MIN_ITEMS_PER_STRATUM,
        subsample_per_stratum: int = DEFAULT_SUBSAMPLE_PER_STRATUM,
    ):
        hierarchy = tuple(tuple(level) for level in collapse_hierarchy)
        if not hierarchy:
            raise ValueError("collapse_hierarchy must have at least one level")
        for level in hierarchy:
            missing = {"answerability", "turn_type"} - set(level)
            if missing:
                raise ValueError(
                    f"every collapse level must retain {{'answerability','turn_type'}} "
                    f"(level {level} is missing {sorted(missing)}); collapsing those "
                    f"axes away would let a stratum mix answerability classes or turns"
                )
        if min_items < 1:
            raise ValueError("min_items must be >= 1")
        if subsample_per_stratum < 1:
            raise ValueError("subsample_per_stratum must be >= 1")

        self.collapse_hierarchy = hierarchy
        self.min_items = min_items
        self.subsample_per_stratum = subsample_per_stratum
        self._subsample: Optional[StratifiedSubsample] = None

    # ------------------------------------------------------------------
    # build_subsample
    # ------------------------------------------------------------------
    def build_subsample(self, items: list[Item]) -> StratifiedSubsample:
        """Build the stratified subsample covering every non-empty cohort cell.

        Collapses sparse full cells per the disjoint progressive rule (see
        :func:`_collapse`), then draws up to ``subsample_per_stratum`` items from
        each stratum (sorted by id for determinism; always at least one). Caches
        the result on the instance.

        The returned subsample is guaranteed to represent every (possibly
        collapsed) non-empty cohort cell: every item is assigned to exactly one
        stratum, and every stratum contributes at least one subsample item.
        """
        if not items:
            raise ValueError("build_subsample: no items provided")

        records = [(it.item_id, it.cohort.to_dict()) for it in items]
        builds = _collapse(records, self.collapse_hierarchy, self.min_items)

        items_by_id = {it.item_id: it for it in items}
        strata: list[SubsampleStratum] = []
        full_len = len(self.collapse_hierarchy[0])
        for b in builds:
            member_ids = sorted(b.item_ids)
            chosen = tuple(member_ids[: self.subsample_per_stratum])
            strata.append(
                SubsampleStratum(
                    id=_stratum_id(b.predicate),
                    key_axes=b.key_axes,
                    predicate=dict(b.predicate),
                    turn_type=b.predicate["turn_type"],
                    n_items=len(member_ids),
                    item_ids=tuple(member_ids),
                    subsample_item_ids=chosen,
                    collapsed=len(b.key_axes) < full_len,
                    sufficient=b.sufficient,
                )
            )

        subsample = StratifiedSubsample(
            strata=tuple(strata),
            min_items=self.min_items,
            per_stratum=self.subsample_per_stratum,
            items_by_id=items_by_id,
        )

        # Invariants: disjoint full cover + every stratum represented.
        covered = sum(len(s.item_ids) for s in strata)
        assert covered == len(items_by_id), (
            f"collapse did not cover every item exactly once "
            f"(covered={covered}, items={len(items_by_id)})"
        )
        assert all(s.subsample_item_ids for s in strata), "a stratum has no subsample item"

        self._subsample = subsample
        return subsample

    # ------------------------------------------------------------------
    # pilot_plan
    # ------------------------------------------------------------------
    def pilot_plan(
        self,
        temperature: float,
        reps: int,
        subsample: Optional[StratifiedSubsample] = None,
        *,
        plan_version: str = DEFAULT_PLAN_VERSION,
        confidence_level: float = config.CONFIDENCE_LEVEL,
        target_ci_halfwidth: float = config.TARGET_CI_HALFWIDTH,
        composite_weights: Optional[Mapping[str, float]] = None,
    ) -> SamplingPlan:
        """A :class:`SamplingPlan` describing the PILOT pass.

        The pilot runs each candidate on the *subsample only* at the starting
        ``temperature`` for ``reps`` repetitions (design pilot step 1-2). Every
        stratum gets the same pilot reps (the pilot's job is to *measure*
        variance, not to hit a target CI). The plan's variance model is empty —
        it is filled in after the pilot by :meth:`estimate_variances`.
        """
        subsample = self._require_subsample(subsample)
        if reps < 1:
            raise ValueError("pilot reps must be >= 1")
        weights = dict(composite_weights or config.COMPOSITE_WEIGHTS)

        strata_plans: list[StratumPlan] = []
        strata_meta: dict[str, dict[str, object]] = {}
        total = 0
        for s in subsample.strata:
            strata_plans.append(
                StratumPlan(
                    cohort_predicate=dict(s.predicate),
                    passes={PASS_PILOT: int(reps)},
                    rationale=(
                        f"pilot: {reps} reps on {len(s.subsample_item_ids)} subsample "
                        f"item(s) at temperature {temperature} to measure variance"
                        + ("" if s.sufficient else " [thin stratum: insufficient-data]")
                    ),
                )
            )
            strata_meta[s.id] = self._stratum_meta(s, within=0.0, between=0.0,
                                                   unreachable=False, deep_n=len(s.subsample_item_ids))
            total += len(s.subsample_item_ids) * int(reps)

        variance_model = self._variance_model_blob(
            metric="composite",
            z=_z_for_level(confidence_level),
            confidence_level=confidence_level,
            strata_meta=strata_meta,
            unreachable_strata=[],
        )
        return SamplingPlan(
            plan_version=plan_version,
            temperature=float(temperature),
            target_ci_halfwidth=float(target_ci_halfwidth),
            confidence_level=float(confidence_level),
            strata=strata_plans,
            budget={"max_trials": int(total)},
            pilot_variance_model=variance_model,
            composite_weights=weights,
        )

    # ------------------------------------------------------------------
    # estimate_variances
    # ------------------------------------------------------------------
    def estimate_variances(
        self,
        pilot_events: list[TrialEvent],
        *,
        strata: Optional[Sequence[SubsampleStratum]] = None,
        metric: str = "composite",
    ) -> VarianceModel:
        """Per-stratum ``sigma_within`` / ``sigma_between`` from pilot events.

        Uses :func:`bakeoff.stats.variance_decomp` on the events matching each
        stratum's predicate, taking the square roots of the between/within
        variance components. Strata with no usable pilot data report ``0.0`` for
        both (which sizes their reps to the floor downstream).

        Strata default to the last :meth:`build_subsample` result; if none was
        built and none is passed, strata are derived directly from the events'
        own cohorts via the same collapse rule (handy for standalone testing).
        """
        if strata is None:
            if self._subsample is not None:
                strata = self._subsample.strata
            else:
                strata = self._strata_from_events(pilot_events)

        per_stratum: dict[str, dict[str, float]] = {}
        for s in strata:
            ev = _events_matching(pilot_events, s.predicate)
            vd = variance_decomp(ev, metric)
            per_stratum[s.id] = {
                "within": _sqrt0(vd["within"]),
                "between": _sqrt0(vd["between"]),
            }
        return VarianceModel(metric=metric, per_stratum=per_stratum)

    # ------------------------------------------------------------------
    # required_reps  (the core sizer)
    # ------------------------------------------------------------------
    def required_reps(
        self,
        pilot_events: list[TrialEvent],
        target_ci_halfwidth: float,
        budget: Union[Budget, Mapping[str, int]],
        *,
        subsample: Optional[StratifiedSubsample] = None,
        temperature: Optional[float] = None,
        plan_version: str = DEFAULT_PLAN_VERSION,
        confidence_level: float = config.CONFIDENCE_LEVEL,
        composite_weights: Optional[Mapping[str, float]] = None,
        metric: str = "composite",
    ) -> SamplingPlan:
        """Size the full WIDE+DEEP plan from pilot events (reps chosen, not guessed).

        Pipeline (all rep arithmetic delegated to
        :func:`bakeoff.stats.estimate_required_reps`, the single estimator):

        1. **WIDE pass** — one :class:`bakeoff.stats.Stratum` per collapsed
           stratum with ``n_items`` = the *full-universe* item count. Sizing
           against the large ``n`` typically lands near the floor (the between
           term dominates), which is exactly why WIDE is the backbone.
        2. **DEEP pass** — the same strata but ``n_items`` = the *subsample*
           count, so reps rise to characterize within-item variance on the
           subsample.
        3. Both passes inherit the stats module's guarantees: floor at
           :data:`config.MIN_REPS_PER_STRATUM`, multi-turn strata bumped to
           ``>=`` the max single-turn reps for that pass, budget clamp, and an
           unreachable flag when ``sigma_between^2/n`` alone exceeds the target.

        The WIDE pass's unreachable flags are the honest cohort-CI signal
        surfaced in the plan; the measured per-stratum sigmas are embedded for
        auditability. ``temperature`` defaults to
        :data:`config.DEFAULT_TEMPERATURE` (the pilot-confirmed value the caller
        passes through).
        """
        subsample = self._require_subsample(subsample)
        z = _z_for_level(confidence_level)
        temp = config.DEFAULT_TEMPERATURE if temperature is None else float(temperature)
        weights = dict(composite_weights or config.COMPOSITE_WEIGHTS)

        wide_strata = [
            Stratum(id=s.id, n_items=s.n_items, turn_type=s.turn_type,
                    passes=(PASS_WIDE,), predicate=dict(s.predicate))
            for s in subsample.strata
        ]
        deep_strata = [
            Stratum(id=s.id, n_items=len(s.subsample_item_ids), turn_type=s.turn_type,
                    passes=(PASS_DEEP,), predicate=dict(s.predicate))
            for s in subsample.strata
        ]

        budget = Budget.coerce(budget)
        wide = estimate_required_reps(pilot_events, wide_strata, target_ci_halfwidth,
                                      z, budget, metric=metric)
        deep = estimate_required_reps(pilot_events, deep_strata, target_ci_halfwidth,
                                      z, budget, metric=metric)

        single_max_wide = max(
            (wide[s.id][PASS_WIDE] for s in subsample.strata if s.turn_type != "multi"),
            default=0,
        )

        strata_plans: list[StratumPlan] = []
        strata_meta: dict[str, dict[str, object]] = {}
        unreachable_strata: list[str] = []
        for s in subsample.strata:
            r_wide = wide[s.id][PASS_WIDE]
            r_deep = deep[s.id][PASS_DEEP]
            sigma = wide.measured_sigma.get(s.id, {"within": 0.0, "between": 0.0})
            is_unreachable = bool(wide.unreachable.get(s.id, False))
            if is_unreachable:
                unreachable_strata.append(s.id)

            rationale = self._rationale(s, r_wide, r_deep, single_max_wide, is_unreachable)
            strata_plans.append(
                StratumPlan(
                    cohort_predicate=dict(s.predicate),
                    passes={PASS_WIDE: int(r_wide), PASS_DEEP: int(r_deep)},
                    rationale=rationale,
                )
            )
            strata_meta[s.id] = self._stratum_meta(
                s, within=sigma["within"], between=sigma["between"],
                unreachable=is_unreachable, deep_n=len(s.subsample_item_ids),
            )

        variance_model = self._variance_model_blob(
            metric=metric, z=z, confidence_level=confidence_level,
            strata_meta=strata_meta, unreachable_strata=unreachable_strata,
        )
        budget_blob: dict[str, int] = {"max_trials": int(budget.max_trials)}
        if budget.max_judge_calls is not None:
            budget_blob["max_judge_calls"] = int(budget.max_judge_calls)

        return SamplingPlan(
            plan_version=plan_version,
            temperature=temp,
            target_ci_halfwidth=float(target_ci_halfwidth),
            confidence_level=float(confidence_level),
            strata=strata_plans,
            budget=budget_blob,
            pilot_variance_model=variance_model,
            composite_weights=weights,
        )

    # ------------------------------------------------------------------
    # build_full_plan  (end-to-end convenience)
    # ------------------------------------------------------------------
    def build_full_plan(
        self,
        items: list[Item],
        pilot_events: list[TrialEvent],
        *,
        target_ci_halfwidth: float = config.TARGET_CI_HALFWIDTH,
        budget: Union[Budget, Mapping[str, int], None] = None,
        temperature: Optional[float] = None,
        plan_version: str = DEFAULT_PLAN_VERSION,
        confidence_level: float = config.CONFIDENCE_LEVEL,
        composite_weights: Optional[Mapping[str, float]] = None,
        metric: str = "composite",
    ) -> SamplingPlan:
        """Build the subsample, then size the full plan from pilot events.

        A thin wrapper over :meth:`build_subsample` + :meth:`required_reps`.
        When ``budget`` is omitted it defaults to "no clamp" (a very large
        ``max_trials``) so the sizing reflects the pure statistical need; pass a
        :class:`bakeoff.stats.Budget` to clamp to a real trial cap.
        """
        subsample = self.build_subsample(items)
        if budget is None:
            budget = Budget(max_trials=10**12)
        return self.required_reps(
            pilot_events,
            target_ci_halfwidth,
            budget,
            subsample=subsample,
            temperature=temperature,
            plan_version=plan_version,
            confidence_level=confidence_level,
            composite_weights=composite_weights,
            metric=metric,
        )

    # ------------------------------------------------------------------
    # demo_plan  (tiny, offline, no pilot / no model calls)
    # ------------------------------------------------------------------
    def demo_plan(
        self,
        items: list[Item],
        *,
        max_items: int = 12,
        reps_wide: int = config.MIN_REPS_PER_STRATUM,
        reps_deep: int = 3,
        temperature: float = config.DEFAULT_TEMPERATURE,
        plan_version: str = "demo-v1",
        confidence_level: float = config.CONFIDENCE_LEVEL,
        target_ci_halfwidth: float = config.TARGET_CI_HALFWIDTH,
        composite_weights: Optional[Mapping[str, float]] = None,
    ) -> SamplingPlan:
        """A tiny, offline-friendly plan for a fast end-to-end demo.

        Produces a plan with a hard cap of ``max_items`` WIDE items and low fixed
        reps, requiring **no pilot and no real model calls** (the mock adapters
        can satisfy it). The WIDE universe is the capped, stratified set of items
        (one per stratum first, round-robin, up to ``max_items``) so the demo
        still touches a spread of cohorts. Multi-turn reps are held ``>=``
        single-turn (``reps_deep`` applies to both, satisfying Req 6.4 trivially).

        The total trial count for a run with ``M`` models is bounded by
        ``M * max_items * (reps_wide + reps_deep)``.
        """
        if max_items < 1:
            raise ValueError("max_items must be >= 1")
        # Use a small subsample so even singleton-heavy data yields a few strata.
        demo_per_stratum = max(1, min(self.subsample_per_stratum, max_items))
        demo_planner = SamplingPlanner(
            collapse_hierarchy=self.collapse_hierarchy,
            min_items=self.min_items,
            subsample_per_stratum=demo_per_stratum,
        )
        subsample = demo_planner.build_subsample(items)

        # Cap the WIDE universe to <= max_items, round-robin across strata so the
        # cohort spread is preserved rather than taking the first max_items ids.
        wide_ids = _round_robin_cap(
            [list(s.subsample_item_ids) for s in subsample.strata], max_items
        )
        wide_set = set(wide_ids)
        weights = dict(composite_weights or config.COMPOSITE_WEIGHTS)

        strata_plans: list[StratumPlan] = []
        strata_meta: dict[str, dict[str, object]] = {}
        total = 0
        for s in subsample.strata:
            stratum_wide = [i for i in s.item_ids if i in wide_set]
            stratum_deep = [i for i in s.subsample_item_ids if i in wide_set]
            if not stratum_wide:
                continue  # this stratum contributed no item to the capped demo set
            strata_plans.append(
                StratumPlan(
                    cohort_predicate=dict(s.predicate),
                    passes={PASS_WIDE: int(reps_wide), PASS_DEEP: int(reps_deep)},
                    rationale=(
                        f"demo: {reps_wide} WIDE / {reps_deep} DEEP reps on "
                        f"{len(stratum_wide)} capped item(s); offline, no pilot"
                    ),
                )
            )
            meta = self._stratum_meta(s, within=0.0, between=0.0, unreachable=False,
                                      deep_n=len(stratum_deep))
            # override membership with the capped demo sets
            meta["wide_item_ids"] = list(stratum_wide)
            meta["subsample_item_ids"] = list(stratum_deep)
            meta["n_items_wide"] = len(stratum_wide)
            strata_meta[s.id] = meta
            total += len(stratum_wide) * int(reps_wide) + len(stratum_deep) * int(reps_deep)

        variance_model = self._variance_model_blob(
            metric="composite", z=_z_for_level(confidence_level),
            confidence_level=confidence_level, strata_meta=strata_meta,
            unreachable_strata=[], extra={"demo": True, "max_items": int(max_items)},
        )
        return SamplingPlan(
            plan_version=plan_version,
            temperature=float(temperature),
            target_ci_halfwidth=float(target_ci_halfwidth),
            confidence_level=float(confidence_level),
            strata=strata_plans,
            budget={"max_trials": int(total)},
            pilot_variance_model=variance_model,
            composite_weights=weights,
        )

    # ------------------------------------------------------------------
    # flat_plan  (flat fixed-rep run: every item x R WIDE reps, no pilot/DEEP)
    # ------------------------------------------------------------------
    def flat_plan(
        self,
        items: list[Item],
        *,
        reps: int = 3,
        temperature: float = config.DEFAULT_TEMPERATURE,
        plan_version: str = "flat-r3-v1",
        confidence_level: float = config.CONFIDENCE_LEVEL,
        target_ci_halfwidth: float = config.TARGET_CI_HALFWIDTH,
        composite_weights: Optional[Mapping[str, float]] = None,
    ) -> SamplingPlan:
        """A flat fixed-rep plan: **every** item in a single WIDE pass at ``reps`` reps.

        This deliberately abandons the tiered WIDE/DEEP/pilot design for the real
        run in favor of the operator's explicit ask — *every scenario, each model,
        R times* (flat). There is **no pilot, no DEEP pass, and no item cap**: each
        collapsed stratum simply runs every one of its items at ``reps`` WIDE reps.

        The strata are still built via :meth:`build_subsample` so the cohort
        predicates and thin-stratum metadata stay consistent with the rest of the
        system; only the rep design changes. Crucially, :meth:`_stratum_meta`
        records ``wide_item_ids = list(s.item_ids)`` — the *full* stratum
        membership — which is exactly what :func:`bakeoff.runner.planned_trials`
        reads for the WIDE pass (``_PASS_ITEM_KEY["wide"] == "wide_item_ids"``), so
        every item is scheduled.

        The total trial count per model is ``sum(len(s.item_ids) * reps)`` over all
        strata = ``n_items * reps`` (the strata are a disjoint cover of every item).

        Args:
            items: the loaded :class:`Item`s to run (every one is scheduled).
            reps: WIDE reps per item (``>= 1``; default 3).
            temperature: the run temperature stamped on the plan.
            plan_version: the plan-version stamp (default ``"flat-r3-v1"``).
            confidence_level / target_ci_halfwidth: recorded for reporting (the CI
                a flat run achieves is observed, not targeted by rep sizing).
            composite_weights: optional override of :data:`config.COMPOSITE_WEIGHTS`.

        Raises:
            ValueError: if ``reps < 1``.
        """
        if reps < 1:
            raise ValueError("flat reps must be >= 1")
        subsample = self.build_subsample(items)
        weights = dict(composite_weights or config.COMPOSITE_WEIGHTS)

        strata_plans: list[StratumPlan] = []
        strata_meta: dict[str, dict[str, object]] = {}
        total = 0
        for s in subsample.strata:
            strata_plans.append(
                StratumPlan(
                    cohort_predicate=dict(s.predicate),
                    passes={PASS_WIDE: int(reps)},
                    rationale=(
                        f"flat R={reps}: every item x {reps} WIDE reps "
                        f"(no pilot, no DEEP)"
                        + ("" if s.sufficient else " [thin stratum: insufficient-data]")
                    ),
                )
            )
            strata_meta[s.id] = self._stratum_meta(
                s, within=0.0, between=0.0, unreachable=False,
                deep_n=len(s.item_ids),
            )
            total += len(s.item_ids) * int(reps)

        variance_model = self._variance_model_blob(
            metric="composite",
            z=_z_for_level(confidence_level),
            confidence_level=confidence_level,
            strata_meta=strata_meta,
            unreachable_strata=[],
            extra={"flat": True, "reps": int(reps)},
        )
        return SamplingPlan(
            plan_version=plan_version,
            temperature=float(temperature),
            target_ci_halfwidth=float(target_ci_halfwidth),
            confidence_level=float(confidence_level),
            strata=strata_plans,
            budget={"max_trials": int(total)},
            pilot_variance_model=variance_model,
            composite_weights=weights,
        )

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    def _require_subsample(
        self, subsample: Optional[StratifiedSubsample]
    ) -> StratifiedSubsample:
        if subsample is not None:
            return subsample
        if self._subsample is None:
            raise ValueError(
                "no subsample available: call build_subsample(items) first or pass "
                "subsample=..."
            )
        return self._subsample

    def _strata_from_events(self, events: list[TrialEvent]) -> list[SubsampleStratum]:
        """Derive strata directly from events' cohorts (standalone/testing path)."""
        records = [(ev.item_id, ev.cohort.to_dict()) for ev in events]
        builds = _collapse(records, self.collapse_hierarchy, self.min_items)
        full_len = len(self.collapse_hierarchy[0])
        out: list[SubsampleStratum] = []
        for b in builds:
            member_ids = tuple(sorted(b.item_ids))
            out.append(
                SubsampleStratum(
                    id=_stratum_id(b.predicate),
                    key_axes=b.key_axes,
                    predicate=dict(b.predicate),
                    turn_type=b.predicate["turn_type"],
                    n_items=len(member_ids),
                    item_ids=member_ids,
                    subsample_item_ids=member_ids,
                    collapsed=len(b.key_axes) < full_len,
                    sufficient=b.sufficient,
                )
            )
        return out

    @staticmethod
    def _rationale(
        s: SubsampleStratum, r_wide: int, r_deep: int, single_max_wide: int,
        unreachable: bool,
    ) -> str:
        parts = [
            f"WIDE {r_wide} reps x {s.n_items} items (cohort-CI backbone); "
            f"DEEP {r_deep} reps x {len(s.subsample_item_ids)} subsample items "
            f"(within-item variance)"
        ]
        if s.turn_type == "multi" and r_wide >= single_max_wide and single_max_wide:
            parts.append(
                f"multi-turn: WIDE reps held >= max single-turn ({single_max_wide}) "
                "to equalize CI width given fewer multi-turn items"
            )
        if not s.sufficient:
            parts.append("thin stratum (collapsed to floor): insufficient-data")
        if unreachable:
            parts.append(
                "target CI unreachable with available items (sigma_between^2/n over "
                "target) — only more items, not more reps, can tighten it"
            )
        return "; ".join(parts)

    @staticmethod
    def _stratum_meta(
        s: SubsampleStratum, *, within: float, between: float, unreachable: bool,
        deep_n: int,
    ) -> dict[str, object]:
        return {
            "within": float(within),
            "between": float(between),
            "turn_type": s.turn_type,
            "n_items_wide": int(s.n_items),
            "n_items_deep": int(deep_n),
            "collapsed": bool(s.collapsed),
            "sufficient": bool(s.sufficient),
            "unreachable": bool(unreachable),
            "key_axes": list(s.key_axes),
            "wide_item_ids": list(s.item_ids),
            "subsample_item_ids": list(s.subsample_item_ids),
        }

    @staticmethod
    def _variance_model_blob(
        *, metric: str, z: float, confidence_level: float,
        strata_meta: dict[str, dict[str, object]], unreachable_strata: list[str],
        extra: Optional[dict[str, object]] = None,
    ) -> dict[str, object]:
        blob: dict[str, object] = {
            "metric": metric,
            "z": float(z),
            "confidence_level": float(confidence_level),
            "strata": strata_meta,
            "unreachable_strata": list(unreachable_strata),
        }
        if extra:
            blob.update(extra)
        return blob


# ---------------------------------------------------------------------------
# small functional helpers
# ---------------------------------------------------------------------------
def _sqrt0(variance: float) -> float:
    """``sqrt`` of a variance component, flooring tiny-negative noise at 0."""
    return float(max(0.0, variance)) ** 0.5


def _events_matching(
    events: Iterable[TrialEvent], predicate: Mapping[str, str]
) -> list[TrialEvent]:
    """Events whose cohort matches every axis in ``predicate``."""
    out: list[TrialEvent] = []
    for ev in events:
        cohort = ev.cohort.to_dict()
        if all(cohort.get(axis) == val for axis, val in predicate.items()):
            out.append(ev)
    return out


def _round_robin_cap(groups: list[list[str]], cap: int) -> list[str]:
    """Take up to ``cap`` ids, round-robin across ``groups`` (preserve spread)."""
    out: list[str] = []
    idx = 0
    exhausted = False
    while len(out) < cap and not exhausted:
        exhausted = True
        for g in groups:
            if idx < len(g):
                exhausted = False
                out.append(g[idx])
                if len(out) >= cap:
                    break
        idx += 1
    return out


# ---------------------------------------------------------------------------
# Serialization (round-trips losslessly to/from sampling_plan.json — Req 6.6)
# ---------------------------------------------------------------------------
def plan_to_dict(plan: SamplingPlan) -> dict:
    """Convert a :class:`SamplingPlan` to a plain JSON-ready dict."""
    return dataclasses.asdict(plan)


def plan_from_dict(d: Mapping[str, object]) -> SamplingPlan:
    """Rebuild a :class:`SamplingPlan` from :func:`plan_to_dict` output.

    ``plan_from_dict(plan_to_dict(p)) == p`` for any plan this module produces.
    """
    strata = [
        StratumPlan(
            cohort_predicate=dict(sp["cohort_predicate"]),
            passes={k: int(v) for k, v in sp["passes"].items()},
            rationale=sp["rationale"],
        )
        for sp in d["strata"]
    ]
    return SamplingPlan(
        plan_version=d["plan_version"],
        temperature=float(d["temperature"]),
        target_ci_halfwidth=float(d["target_ci_halfwidth"]),
        confidence_level=float(d["confidence_level"]),
        strata=strata,
        budget={k: int(v) for k, v in d["budget"].items()},
        pilot_variance_model=dict(d["pilot_variance_model"]),
        composite_weights={k: float(v) for k, v in d["composite_weights"].items()},
    )


def write_plan(plan: SamplingPlan, path: Optional[PathLike] = None) -> Path:
    """Serialize ``plan`` to ``sampling_plan.json`` (defaults to the config path).

    Creates ``data/bakeoff/`` if needed. Returns the path written.
    """
    if path is None:
        config.ensure_dirs()
        path = config.SAMPLING_PLAN_PATH
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(plan_to_dict(plan), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return p


def read_plan(path: Optional[PathLike] = None) -> SamplingPlan:
    """Load a :class:`SamplingPlan` from JSON (defaults to the config path)."""
    if path is None:
        path = config.SAMPLING_PLAN_PATH
    p = Path(path)
    return plan_from_dict(json.loads(p.read_text(encoding="utf-8")))
