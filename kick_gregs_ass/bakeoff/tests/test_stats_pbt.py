"""
Property-based tests for :mod:`bakeoff.stats` (Task 8).

Three load-bearing Correctness Properties from the design — the statistical
thesis of the whole project — are exercised here with Hypothesis.

* **P6 — CIs widen as items shrink, not as reps shrink.** The closed-form CI
  half-width :func:`bakeoff.stats.normal_approx_halfwidth` is monotonically
  non-increasing in ``n_items`` and only *weakly* decreasing in ``reps`` (the
  ``1/(n*R)`` term). Concretely: doubling items shrinks the half-width at least
  as much as doubling reps, and the half-width is bounded below as
  ``reps -> inf`` by the irreducible between-item floor. **Breadth beats depth.**
  **Validates: Requirements 9.2, 14.1**

* **P7 — Bootstrap point estimate weights items equally.** The ``point`` of
  :func:`bakeoff.stats.cluster_bootstrap_ci` equals the mean over items of each
  item's rep-mean (== :func:`bakeoff.stats.group_rep_means_by_item` averaged),
  independent of how many reps each item has. Duplicating one item's reps does
  not change the point.
  **Validates: Requirements 9.2, 14.1**

* **P8 — required_reps floors at 2 and refuses the impossible.** Every returned
  rep count is ``>= config.MIN_REPS_PER_STRATUM`` (2), and when the between-item
  term ``sigma_between^2 / n`` alone already meets/exceeds the target variance,
  the function flags the stratum unreachable (the design's "no rep count can hit
  the target — only more items can" signal), never a finite rep count
  masquerading as meeting the target.
  **Validates: Requirements 6.4, 6.5, 14.1**
"""
from __future__ import annotations

import math

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from bakeoff import config
from bakeoff.stats import (
    MAX_REPS,
    Stratum,
    cluster_bootstrap_ci,
    estimate_required_reps,
    group_rep_means_by_item,
    normal_approx_halfwidth,
    required_reps_closed_form,
    _z_for_level,
)
from bakeoff.tests.test_stats import (
    events_from_item_values,
    expected_equal_item_point,
    make_event,
)

# Strategy building blocks ---------------------------------------------------
# Variances (sigma^2) are the inputs to normal_approx_halfwidth.
_var = st.floats(min_value=0.0, max_value=25.0, allow_nan=False, allow_infinity=False)
_pos_var = st.floats(
    min_value=1e-6, max_value=25.0, allow_nan=False, allow_infinity=False
)
_n_items = st.integers(min_value=1, max_value=2000)
_reps = st.integers(min_value=1, max_value=200)
_level = st.sampled_from([0.80, 0.90, 0.95, 0.99])


# ===========================================================================
# P6 — CI half-width: monotone in n_items, only weakly in reps
# **Validates: Requirements 9.2, 14.1**
# ===========================================================================
@settings(max_examples=300)
@given(b=_var, w=_var, n=_n_items, reps=_reps, level=_level)
def test_p6_halfwidth_decreasing_in_n_items(b, w, n, reps, level):
    """More items never widens the CI (strictly narrows it when any variance)."""
    z = _z_for_level(level)
    wider = normal_approx_halfwidth(b, w, n, reps, z)
    narrower = normal_approx_halfwidth(b, w, n + 1, reps, z)
    assert narrower <= wider + 1e-12
    # Strict decrease holds whenever the half-width is numerically nonzero;
    # guard against the subnormal-underflow corner (e.g. w=5e-324) where both
    # sides flush to exactly 0.0 and the strict claim is vacuous.
    if wider > 1e-12:
        assert narrower < wider


@settings(max_examples=300)
@given(b=_var, w=_var, n=_n_items, reps=_reps, level=_level)
def test_p6_halfwidth_nonincreasing_in_reps(b, w, n, reps, level):
    """More reps never widens the CI, but only weakly narrows it: it can never
    cross the irreducible between-item floor ``z*sqrt(b/n)``."""
    z = _z_for_level(level)
    base = normal_approx_halfwidth(b, w, n, reps, z)
    more_reps = normal_approx_halfwidth(b, w, n, reps + 1, z)
    assert more_reps <= base + 1e-12
    floor = z * math.sqrt(b / n)
    assert more_reps >= floor - 1e-9


@settings(max_examples=400)
@given(b=_pos_var, w=_pos_var, n=_n_items, reps=_reps, level=_level)
def test_p6_breadth_beats_depth(b, w, n, reps, level):
    """THE thesis: doubling items shrinks the half-width at least as much as
    doubling reps. halfwidth(2n, R) <= halfwidth(n, 2R) for b, w > 0.

    This holds universally because the two within-item terms are *equal* (both
    ``w/(2nR)``) while the between term is strictly smaller when items are
    doubled (``b/(2n) < b/n``). The per-increment magnitude comparison is NOT
    universally true (in the within-dominated, very-low-rep regime a single rep
    can help more than a single item), so the honest universal statement of
    breadth-beats-depth is exactly this doubling form plus the between-item floor.
    """
    z = _z_for_level(level)
    double_items = normal_approx_halfwidth(b, w, 2 * n, reps, z)
    double_reps = normal_approx_halfwidth(b, w, n, 2 * reps, z)
    assert double_items <= double_reps + 1e-12


# ===========================================================================
# P7 — bootstrap point weights items equally regardless of per-item rep count
# **Validates: Requirements 9.2, 14.1**
# ===========================================================================
@st.composite
def item_value_maps(draw):
    """Generate ``{item_id: [rep values]}`` with >= 1 item and varying rep counts."""
    n_items = draw(st.integers(min_value=1, max_value=8))
    out: dict[str, list[float]] = {}
    val = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
    for i in range(n_items):
        reps = draw(st.lists(val, min_size=1, max_size=6))
        out[f"i{i}"] = reps
    return out


@settings(max_examples=200)
@given(item_values=item_value_maps())
def test_p7_group_rep_means_then_average_is_equal_item_point(item_values):
    events = events_from_item_values(item_values)
    means = group_rep_means_by_item(events, "composite")
    point = float(np.mean(list(means.values())))
    assert abs(point - expected_equal_item_point(item_values)) < 1e-9


@settings(max_examples=120)
@given(item_values=item_value_maps())
def test_p7_bootstrap_point_is_equal_item_mean(item_values):
    events = events_from_item_values(item_values)
    ci = cluster_bootstrap_ci(events, "composite", n_boot=64, seed=config.BOOTSTRAP_SEED)
    assert abs(ci.point - expected_equal_item_point(item_values)) < 1e-9


@settings(max_examples=150)
@given(item_values=item_value_maps(), extra=st.integers(min_value=1, max_value=5))
def test_p7_point_invariant_to_rep_duplication(item_values, extra):
    """Duplicating every rep of ONE item ``extra`` times changes its rep count but
    NOT its rep-mean, so the equal-item-weight point is unchanged (Property 7)."""
    base = cluster_bootstrap_ci(
        events_from_item_values(item_values), "composite", n_boot=32,
        seed=config.BOOTSTRAP_SEED,
    ).point

    target = next(iter(item_values))  # duplicate the first item's reps
    inflated = dict(item_values)
    inflated[target] = item_values[target] * (1 + extra)  # same values, more reps
    inflated_point = cluster_bootstrap_ci(
        events_from_item_values(inflated), "composite", n_boot=32,
        seed=config.BOOTSTRAP_SEED,
    ).point

    assert abs(inflated_point - base) < 1e-9


# ===========================================================================
# P8 — required_reps floors at 2 and detects unreachability
# **Validates: Requirements 6.4, 6.5, 14.1**
# ===========================================================================
@settings(max_examples=400)
@given(
    sw=st.floats(min_value=0.0, max_value=5.0),
    sb=st.floats(min_value=0.0, max_value=5.0),
    n=st.integers(min_value=1, max_value=2000),
    target_w=st.floats(min_value=0.005, max_value=0.5),
    level=_level,
)
def test_p8_closed_form_floor_and_unreachable(sw, sb, n, target_w, level):
    z = _z_for_level(level)
    reps, unreachable = required_reps_closed_form(sw, sb, n, target_w, z)

    # Floor + ceiling always hold.
    assert reps >= config.MIN_REPS_PER_STRATUM
    assert reps <= MAX_REPS

    target_var = (target_w / z) ** 2
    between_term = sb**2 / n
    if between_term >= target_var:
        # impossible branch: unreachable signal fires, sentinel reps returned.
        assert unreachable is True
        assert reps == MAX_REPS
    else:
        assert unreachable is False


def _stratum_pilot_events(geography, n_pilot, reps, spread, within_sd, seed, turn_type="single"):
    """Pilot events whose item means span ``[0, spread]`` (controls sigma_between)
    with run-to-run noise ``within_sd`` (controls sigma_within)."""
    rng = np.random.default_rng(seed)
    item_values: dict[str, list[float]] = {}
    for j in range(n_pilot):
        mean = (spread * j / max(1, n_pilot - 1)) if n_pilot > 1 else 0.5
        vals = [
            mean + (float(rng.normal(0.0, within_sd)) if within_sd > 0 else 0.0)
            for _ in range(reps)
        ]
        item_values[f"{geography}-{j}"] = vals
    return events_from_item_values(
        item_values, geography=geography, turn_type=turn_type
    )


@settings(max_examples=120, deadline=None)
@given(
    spread=st.floats(min_value=0.0, max_value=1.0),
    within_sd=st.floats(min_value=0.0, max_value=0.5),
    n_full_items=st.integers(min_value=1, max_value=500),
    target_w=st.floats(min_value=0.005, max_value=0.3),
    seed=st.integers(min_value=0, max_value=10_000),
)
def test_p8_estimate_required_reps_floor_and_unreachable(
    spread, within_sd, n_full_items, target_w, seed
):
    z = _z_for_level(0.95)
    events = _stratum_pilot_events("S", n_pilot=8, reps=4, spread=spread,
                                   within_sd=within_sd, seed=seed)
    strata = [Stratum(id="S", n_items=n_full_items, turn_type="single",
                      passes=("wide",), predicate={"geography": "S"})]
    out = estimate_required_reps(events, strata, target_w, z,
                                 budget={"max_trials": 10**12})

    # Floor: every returned rep count is >= 2.
    assert all(r >= config.MIN_REPS_PER_STRATUM for r in out["S"].values())
    # Unreachable detection is exposed as a per-stratum flag.
    assert isinstance(out.unreachable["S"], bool)


@settings(max_examples=60, deadline=None)
@given(
    n_single=st.integers(min_value=1, max_value=200),
    n_multi=st.integers(min_value=1, max_value=200),
    seed=st.integers(min_value=0, max_value=10_000),
)
def test_p8_multi_turn_reps_ge_single_turn(n_single, n_multi, seed):
    """Multi-turn strata always receive reps >= the single-turn maximum for the
    same target/pass (Req 6.4 equalization), regardless of their own variance."""
    z = _z_for_level(0.95)
    single = _stratum_pilot_events("S", n_pilot=8, reps=6, spread=0.0,
                                   within_sd=0.3, seed=seed, turn_type="single")
    multi = _stratum_pilot_events("M", n_pilot=8, reps=6, spread=0.0,
                                  within_sd=0.0, seed=seed + 1, turn_type="multi")
    strata = [
        Stratum(id="S", n_items=n_single, turn_type="single", passes=("wide",),
                predicate={"geography": "S"}),
        Stratum(id="M", n_items=n_multi, turn_type="multi", passes=("wide",),
                predicate={"geography": "M"}),
    ]
    out = estimate_required_reps(single + multi, strata, 0.05, z,
                                 budget={"max_trials": 10**12})
    assert out["M"]["wide"] >= out["S"]["wide"]
