"""
Unit tests for :mod:`bakeoff.planner` (Task 9).

Coverage maps to the task's required checks and Requirements 6.1-6.6 / 12.1:

* **subsample covers every non-empty (possibly collapsed) cohort cell** — every
  item lands in exactly one stratum and every stratum is represented by >= 1
  subsample item; against the *real* dataset, too (Req 6.1).
* **sparse-cell collapse on singleton-heavy input** — a synthetic dataset that is
  almost entirely singletons still collapses to strata each with >= min_items,
  and every level retains answerability + turn_type (Req 6.1, Req 5.4).
* **plan round-trips to/from JSON** — ``plan_from_dict(plan_to_dict(p)) == p`` and
  a file write/read round-trip (Req 6.6, Req 12.1).
* **multi-turn strata get reps >= single-turn for the same target** (Req 6.4).
* **estimate_variances recovers planted sigmas on synthetic pilot events**
  (Req 6.3).
* plus: unreachable surfaced honestly (Req 6.5), WIDE backbone vs DEEP reps,
  pilot plan shape (Req 6.2), demo plan caps trials (offline demo), and the
  collapse-level guard rejects dropping answerability/turn_type.
"""
from __future__ import annotations

from collections import Counter

import numpy as np
import pytest

from bakeoff import config
from bakeoff.dataset import DatasetLoader
from bakeoff.planner import (
    DEFAULT_MIN_ITEMS_PER_STRATUM,
    SamplingPlanner,
    StratifiedSubsample,
    plan_from_dict,
    plan_to_dict,
    read_plan,
    write_plan,
)
from bakeoff.stats import Budget, MAX_REPS
from bakeoff.types import CohortKey, Item, SamplingPlan

# Reuse the validated TrialEvent builder from the stats tests.
from bakeoff.tests.test_stats import make_event


# ---------------------------------------------------------------------------
# Synthetic Item / cohort builders
# ---------------------------------------------------------------------------
def make_item(
    item_id: str,
    *,
    geography: str = "g",
    proficiency: str = "fluent",
    tone: str = "terse",
    entry_route: str = "slack",
    momentary_state: str = "neutral",
    answerability: str = "full",
    turn_type: str = "single",
) -> Item:
    cohort = CohortKey(
        geography=geography,
        proficiency=proficiency,
        tone=tone,
        entry_route=entry_route,
        momentary_state=momentary_state,
        answerability=answerability,
        turn_type=turn_type,
    )
    return Item(id=item_id, turn_type=turn_type, cohort=cohort, query="q",
                answerability=answerability, gold_node_ids=["n1"])


def singleton_heavy_items(n: int = 200) -> list[Item]:
    """A dataset where the full 7-axis cohort key is almost entirely singletons.

    Geography and momentary_state are made nearly unique per item, so the full
    cell of any item is a singleton — forcing the collapse to engage. The coarse
    axes (proficiency/tone/answerability/turn_type) repeat enough that a coarser
    key has plenty of members.
    """
    rng = np.random.default_rng(123)
    profs = ["broken", "functional", "fluent", "near-native"]
    tones = ["terse", "chatty", "formal"]
    ans = ["full", "partial", "none"]
    turns = ["single", "multi"]
    items: list[Item] = []
    for i in range(n):
        items.append(
            make_item(
                f"it-{i:04d}",
                geography=f"geo-{i}",            # unique -> full cell is a singleton
                momentary_state=f"state-{i}",    # unique
                proficiency=profs[int(rng.integers(len(profs)))],
                tone=tones[int(rng.integers(len(tones)))],
                answerability=ans[int(rng.integers(len(ans)))],
                turn_type=turns[int(rng.integers(len(turns)))],
            )
        )
    return items


def pilot_events_for_subsample(
    subsample: StratifiedSubsample,
    *,
    within_sd_by_stratum=None,
    between_sd: float = 0.15,
    reps: int = 10,
    seed: int = 0,
):
    """Synthesize pilot events for every subsample item with cohort-correct fields.

    Each item gets a between-item mean draw (SD ``between_sd``) and per-rep noise
    (SD from ``within_sd_by_stratum[stratum_id]`` or a default). Answerability and
    turn_type are taken from the item's own cohort so validation passes.
    """
    within_sd_by_stratum = within_sd_by_stratum or {}
    rng = np.random.default_rng(seed)
    events = []
    for s in subsample.strata:
        wsd = within_sd_by_stratum.get(s.id, 0.10)
        for iid in s.subsample_item_ids:
            it = subsample.items_by_id[iid]
            c = it.cohort
            base = float(rng.normal(0.5, between_sd))
            for rep in range(reps):
                v = base + (float(rng.normal(0.0, wsd)) if wsd > 0 else 0.0)
                events.append(
                    make_event(
                        composite=v,
                        item_id=iid,
                        rep=rep,
                        answerability=c.answerability,
                        turn_type=c.turn_type,
                        geography=c.geography,
                    )
                )
    return events


# ---------------------------------------------------------------------------
# build_subsample: full coverage + every cell represented
# ---------------------------------------------------------------------------
def test_subsample_covers_every_cohort_cell_disjointly():
    items = singleton_heavy_items(200)
    planner = SamplingPlanner()
    ss = planner.build_subsample(items)

    # disjoint cover: every item in exactly one stratum, counts add up
    assigned = [iid for s in ss.strata for iid in s.item_ids]
    assert len(assigned) == len(items)
    assert set(assigned) == {it.item_id for it in items}
    assert len(assigned) == len(set(assigned)), "an item appears in two strata"

    # every stratum represented by >= 1 subsample item
    assert all(s.subsample_item_ids for s in ss.strata)

    # every original non-empty full cohort cell maps into a stratum that contains it
    cell_to_stratum = {iid: s.id for s in ss.strata for iid in s.item_ids}
    for it in items:
        assert it.item_id in cell_to_stratum


def test_subsample_real_dataset_full_coverage_and_no_thin_strata():
    """Against the real 1300-item / ~1006-cell dataset (Req 6.1, Req 1.7)."""
    items = DatasetLoader().load_items()
    planner = SamplingPlanner()
    ss = planner.build_subsample(items)

    covered = sum(len(s.item_ids) for s in ss.strata)
    assert covered == len(items)
    assert all(s.subsample_item_ids for s in ss.strata)
    # the default hierarchy bottoms out at (answerability, turn_type), whose
    # smallest real block has 9 items >= min_items -> no insufficient strata.
    assert ss.thin_strata == []
    # both turn types are present as strata
    assert any(s.turn_type == "single" for s in ss.strata)
    assert any(s.turn_type == "multi" for s in ss.strata)


# ---------------------------------------------------------------------------
# sparse-cell collapse on singleton-heavy input
# ---------------------------------------------------------------------------
def test_sparse_cells_collapse_to_min_items():
    items = singleton_heavy_items(200)
    planner = SamplingPlanner(min_items=DEFAULT_MIN_ITEMS_PER_STRATUM)
    ss = planner.build_subsample(items)

    # Every singleton full cell must have collapsed: no stratum may be a full
    # 7-axis key with a single item (that is exactly the case collapse exists to
    # remove). Sufficient strata all clear min_items.
    for s in ss.strata:
        if s.sufficient:
            assert s.n_items >= planner.min_items
        # no stratum mixes answerability or turn_type, at any collapse level
        assert "answerability" in s.predicate
        assert "turn_type" in s.predicate

    # At least some strata are collapsed (coarser than the full key).
    assert any(s.collapsed for s in ss.strata)


def test_collapse_level_must_retain_answerability_and_turn_type():
    with pytest.raises(ValueError):
        SamplingPlanner(collapse_hierarchy=(("proficiency", "turn_type"),))  # no answerability
    with pytest.raises(ValueError):
        SamplingPlanner(collapse_hierarchy=(("answerability",),))  # no turn_type


def test_no_stratum_mixes_answerability_or_turn_type_real_data():
    items = DatasetLoader().load_items()
    ss = SamplingPlanner().build_subsample(items)
    for s in ss.strata:
        ans = {ss.items_by_id[i].cohort.answerability for i in s.item_ids}
        tt = {ss.items_by_id[i].cohort.turn_type for i in s.item_ids}
        assert len(ans) == 1, f"stratum {s.id} mixes answerability {ans}"
        assert len(tt) == 1, f"stratum {s.id} mixes turn_type {tt}"


# ---------------------------------------------------------------------------
# estimate_variances recovers planted sigmas
# ---------------------------------------------------------------------------
def test_estimate_variances_recovers_planted_sigmas():
    # Two homogeneous cohorts so each maps to one stratum with many items.
    # full/single and none/single -> distinct (answerability, turn_type) blocks.
    items = (
        [make_item(f"f-{i}", answerability="full", turn_type="single",
                   geography="g", momentary_state="neutral") for i in range(40)]
        + [make_item(f"n-{i}", answerability="none", turn_type="single",
                     geography="g", momentary_state="neutral") for i in range(40)]
    )
    planner = SamplingPlanner(min_items=4, subsample_per_stratum=40)
    ss = planner.build_subsample(items)

    planted_within = 0.12
    planted_between = 0.22
    events = pilot_events_for_subsample(
        ss,
        within_sd_by_stratum={s.id: planted_within for s in ss.strata},
        between_sd=planted_between,
        reps=12,
        seed=7,
    )
    vm = planner.estimate_variances(events)
    assert set(vm.per_stratum) == {s.id for s in ss.strata}
    for sid, sig in vm.per_stratum.items():
        assert sig["within"] == pytest.approx(planted_within, abs=0.03)
        assert sig["between"] == pytest.approx(planted_between, abs=0.06)


def test_estimate_variances_empty_stratum_is_zero():
    items = [make_item(f"f-{i}", answerability="full") for i in range(10)]
    planner = SamplingPlanner(min_items=4, subsample_per_stratum=10)
    ss = planner.build_subsample(items)
    vm = planner.estimate_variances([])  # no pilot events at all
    for sig in vm.per_stratum.values():
        assert sig["within"] == 0.0
        assert sig["between"] == 0.0


# ---------------------------------------------------------------------------
# multi-turn strata get reps >= single-turn for the same target
# ---------------------------------------------------------------------------
def test_multi_turn_reps_ge_single_turn_same_target():
    # single-turn stratum: genuinely noisy (needs > floor reps).
    # multi-turn stratum: negligible within noise (would otherwise floor).
    single_items = [make_item(f"s-{i}", answerability="full", turn_type="single",
                              geography="g", momentary_state="neutral")
                    for i in range(30)]
    multi_items = [make_item(f"m-{i}", answerability="full", turn_type="multi",
                             geography="g", momentary_state="neutral")
                   for i in range(30)]
    planner = SamplingPlanner(min_items=4, subsample_per_stratum=15)
    ss = planner.build_subsample(single_items + multi_items)

    single_ids = [s.id for s in ss.strata if s.turn_type == "single"]
    multi_ids = [s.id for s in ss.strata if s.turn_type == "multi"]
    within = {sid: 0.40 for sid in single_ids}
    within.update({sid: 0.0 for sid in multi_ids})  # multi would floor on its own

    events = pilot_events_for_subsample(
        ss, within_sd_by_stratum=within, between_sd=0.05, reps=10, seed=11
    )
    plan = planner.required_reps(
        events, target_ci_halfwidth=0.05, budget=Budget(max_trials=10**9),
        temperature=0.2,
    )

    reps_by_pred = {tuple(sorted(sp.cohort_predicate.items())): sp.passes for sp in plan.strata}

    def reps_for(turn_type, pass_name):
        out = []
        for sp in plan.strata:
            if sp.cohort_predicate.get("turn_type") == turn_type:
                out.append(sp.passes[pass_name])
        return out

    single_wide = reps_for("single", "wide")
    multi_wide = reps_for("multi", "wide")
    assert single_wide and multi_wide
    assert max(single_wide) > config.MIN_REPS_PER_STRATUM  # single genuinely needs more
    # every multi-turn wide rep >= max single-turn wide rep (equalized up)
    assert min(multi_wide) >= max(single_wide)


# ---------------------------------------------------------------------------
# WIDE backbone vs DEEP reps; unreachable honesty
# ---------------------------------------------------------------------------
def test_wide_is_backbone_deep_gets_more_reps_for_within_variance():
    # One homogeneous noisy cohort: large WIDE n -> floor; small DEEP n -> more.
    items = [make_item(f"f-{i}", answerability="full", turn_type="single",
                       geography="g", momentary_state="neutral") for i in range(60)]
    planner = SamplingPlanner(min_items=4, subsample_per_stratum=6)
    ss = planner.build_subsample(items)
    # within noise modest enough that the large-n WIDE pass floors, but the
    # small-n DEEP pass (only 6 items) still needs reps to shrink within/(n*R).
    events = pilot_events_for_subsample(
        ss, within_sd_by_stratum={s.id: 0.20 for s in ss.strata},
        between_sd=0.03, reps=10, seed=3,
    )
    plan = planner.required_reps(events, target_ci_halfwidth=0.05,
                                 budget=Budget(max_trials=10**9))
    sp = plan.strata[0]
    # WIDE over the full 60 items lands at the floor (the 1/n term dominates,
    # big n); DEEP over 6 subsample items needs more reps to hit the same target.
    assert sp.passes["wide"] == config.MIN_REPS_PER_STRATUM
    assert sp.passes["deep"] > sp.passes["wide"]


def test_unreachable_surfaced_when_between_exceeds_target():
    # Big item-to-item spread + few items per stratum + a tiny target => the
    # between term alone exceeds target variance => unreachable signal (Req 6.5).
    items = [make_item(f"f-{i}", answerability="full", turn_type="single",
                       geography="g", momentary_state="neutral") for i in range(6)]
    planner = SamplingPlanner(min_items=2, subsample_per_stratum=6)
    ss = planner.build_subsample(items)
    events = pilot_events_for_subsample(
        ss, within_sd_by_stratum={s.id: 0.02 for s in ss.strata},
        between_sd=0.40, reps=6, seed=9,
    )
    plan = planner.required_reps(events, target_ci_halfwidth=0.01,
                                 budget=Budget(max_trials=10**9))
    vm = plan.pilot_variance_model
    assert vm["unreachable_strata"], "expected at least one unreachable stratum"
    # the unreachable stratum's WIDE reps are capped at the ceiling, not infinite
    unreachable_id = vm["unreachable_strata"][0]
    assert vm["strata"][unreachable_id]["unreachable"] is True
    sp = next(s for s in plan.strata
              if _stratum_pred_id(s.cohort_predicate) == unreachable_id)
    assert sp.passes["wide"] == MAX_REPS


def _stratum_pred_id(pred):
    from bakeoff.types import COHORT_DIMENSIONS
    return "|".join(f"{a}={pred[a]}" for a in COHORT_DIMENSIONS if a in pred)


# ---------------------------------------------------------------------------
# pilot_plan shape
# ---------------------------------------------------------------------------
def test_pilot_plan_runs_subsample_at_starting_temperature():
    items = singleton_heavy_items(120)
    planner = SamplingPlanner()
    ss = planner.build_subsample(items)
    plan = planner.pilot_plan(temperature=0.2, reps=config.PILOT_REPS, subsample=ss)
    assert plan.temperature == 0.2
    assert all(sp.passes == {"pilot": config.PILOT_REPS} for sp in plan.strata)
    # budget == total pilot trials/model = sum over strata of (subsample items * reps)
    expected = sum(len(s.subsample_item_ids) for s in ss.strata) * config.PILOT_REPS
    assert plan.budget["max_trials"] == expected


# ---------------------------------------------------------------------------
# plan round-trips to/from JSON
# ---------------------------------------------------------------------------
def test_plan_round_trips_in_memory():
    items = singleton_heavy_items(150)
    planner = SamplingPlanner()
    ss = planner.build_subsample(items)
    events = pilot_events_for_subsample(ss, reps=8, seed=5)
    plan = planner.required_reps(events, target_ci_halfwidth=0.05,
                                 budget=Budget(max_trials=50_000, max_judge_calls=10_000))
    rebuilt = plan_from_dict(plan_to_dict(plan))
    assert rebuilt == plan
    assert isinstance(rebuilt, SamplingPlan)


def test_plan_round_trips_through_file(tmp_path):
    items = singleton_heavy_items(150)
    planner = SamplingPlanner()
    ss = planner.build_subsample(items)
    events = pilot_events_for_subsample(ss, reps=8, seed=6)
    plan = planner.required_reps(events, target_ci_halfwidth=0.05,
                                 budget=Budget(max_trials=50_000))
    path = tmp_path / "sampling_plan.json"
    written = write_plan(plan, path)
    assert written == path and path.exists()
    assert read_plan(path) == plan


def test_build_full_plan_matches_manual_pipeline():
    items = singleton_heavy_items(150)
    planner = SamplingPlanner()
    ss = planner.build_subsample(items)
    events = pilot_events_for_subsample(ss, reps=8, seed=8)
    manual = planner.required_reps(events, target_ci_halfwidth=0.05,
                                   budget=Budget(max_trials=10**12), temperature=0.2)
    # build_full_plan rebuilds the subsample internally; default budget is "no clamp".
    auto = SamplingPlanner().build_full_plan(items, events, target_ci_halfwidth=0.05,
                                             temperature=0.2)
    assert {(_pred_key(sp), tuple(sorted(sp.passes.items()))) for sp in auto.strata} == \
           {(_pred_key(sp), tuple(sorted(sp.passes.items()))) for sp in manual.strata}


def _pred_key(sp):
    return tuple(sorted(sp.cohort_predicate.items()))


# ---------------------------------------------------------------------------
# demo_plan: tiny, offline, capped
# ---------------------------------------------------------------------------
def test_demo_plan_caps_trials_and_needs_no_pilot():
    items = DatasetLoader().load_items()
    planner = SamplingPlanner()
    demo = planner.demo_plan(items, max_items=12, reps_wide=2, reps_deep=3)
    assert demo.plan_version == "demo-v1"
    # total WIDE items across strata <= max_items
    total_wide_items = sum(
        demo.pilot_variance_model["strata"][_pred_key_str(sp.cohort_predicate)]["n_items_wide"]
        for sp in demo.strata
    )
    assert total_wide_items <= 12
    # per-model trial count is bounded and small (offline-demo-able)
    assert demo.budget["max_trials"] <= 12 * (2 + 3)
    assert demo.budget["max_trials"] > 0
    assert demo.pilot_variance_model["demo"] is True


def _pred_key_str(pred):
    from bakeoff.types import COHORT_DIMENSIONS
    return "|".join(f"{a}={pred[a]}" for a in COHORT_DIMENSIONS if a in pred)


def test_demo_plan_multi_turn_reps_ge_single_turn():
    items = DatasetLoader().load_items()
    demo = SamplingPlanner().demo_plan(items, max_items=20, reps_wide=2, reps_deep=3)
    single = [sp.passes["wide"] for sp in demo.strata
              if sp.cohort_predicate.get("turn_type") == "single"]
    multi = [sp.passes["wide"] for sp in demo.strata
             if sp.cohort_predicate.get("turn_type") == "multi"]
    if single and multi:
        assert min(multi) >= max(single)


# ---------------------------------------------------------------------------
# guard rails
# ---------------------------------------------------------------------------
def test_build_subsample_empty_raises():
    with pytest.raises(ValueError):
        SamplingPlanner().build_subsample([])


def test_required_reps_without_subsample_raises():
    with pytest.raises(ValueError):
        SamplingPlanner().required_reps([], target_ci_halfwidth=0.05,
                                        budget=Budget(max_trials=10))


# ---------------------------------------------------------------------------
# flat_plan: every item x R WIDE reps (no pilot, no DEEP, no item cap)
# ---------------------------------------------------------------------------
def _flat_fixture_items() -> list[Item]:
    """A small item set spanning a couple of cohorts (answerability x turn_type)."""
    return (
        [make_item(f"full-s-{i}", answerability="full", turn_type="single",
                   geography="g", momentary_state="neutral") for i in range(5)]
        + [make_item(f"none-s-{i}", answerability="none", turn_type="single",
                     geography="g", momentary_state="neutral") for i in range(4)]
        + [make_item(f"full-m-{i}", answerability="full", turn_type="multi",
                     geography="g", momentary_state="neutral") for i in range(3)]
    )


def test_flat_plan_yields_exactly_n_items_times_reps_per_model():
    from bakeoff.adapters.mock import MockAdapter
    from bakeoff.runner import planned_trials

    items = _flat_fixture_items()
    n_items = len(items)
    reps = 4
    planner = SamplingPlanner(min_items=2, subsample_per_stratum=2)
    plan = planner.flat_plan(items, reps=reps)

    models = [MockAdapter(name="A"), MockAdapter(name="B")]
    specs = list(planned_trials(plan, models))

    # exactly n_items * reps trials per model, 2 models -> 2 * n_items * reps total.
    assert len(specs) == 2 * n_items * reps
    per_model = Counter(s.model for s in specs)
    assert per_model == {"A": n_items * reps, "B": n_items * reps}
    # flat run is a single WIDE pass at `reps` reps for every stratum.
    assert all(sp.passes == {"wide": reps} for sp in plan.strata)
    # budget mirrors the per-model planned count (n_items * reps).
    assert plan.budget["max_trials"] == n_items * reps
    assert plan.pilot_variance_model["flat"] is True
    assert plan.pilot_variance_model["reps"] == reps


def test_flat_plan_schedules_every_item_id():
    from bakeoff.adapters.mock import MockAdapter
    from bakeoff.runner import planned_trials

    items = _flat_fixture_items()
    plan = SamplingPlanner(min_items=2, subsample_per_stratum=2).flat_plan(items, reps=2)
    specs = list(planned_trials(plan, [MockAdapter(name="A")]))
    scheduled_ids = {s.item_id for s in specs}
    assert scheduled_ids == {it.item_id for it in items}


def test_flat_plan_reps_default_is_three():
    from bakeoff.adapters.mock import MockAdapter
    from bakeoff.runner import planned_trials

    items = _flat_fixture_items()
    n_items = len(items)
    plan = SamplingPlanner(min_items=2, subsample_per_stratum=2).flat_plan(items)
    specs = list(planned_trials(plan, [MockAdapter(name="A")]))
    assert len(specs) == n_items * 3
    assert all(sp.passes == {"wide": 3} for sp in plan.strata)
    assert plan.plan_version == "flat-r3-v1"


def test_flat_plan_round_trips_through_file(tmp_path):
    items = _flat_fixture_items()
    plan = SamplingPlanner(min_items=2, subsample_per_stratum=2).flat_plan(items, reps=3)
    path = tmp_path / "flat_plan.json"
    written = write_plan(plan, path)
    assert written == path and path.exists()
    assert read_plan(path) == plan


def test_flat_plan_reps_zero_raises():
    items = _flat_fixture_items()
    with pytest.raises(ValueError):
        SamplingPlanner(min_items=2, subsample_per_stratum=2).flat_plan(items, reps=0)
