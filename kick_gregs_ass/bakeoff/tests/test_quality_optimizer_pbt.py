"""
Property-based tests for the closed-loop prompt optimizer's pure cores
(Tasks 2.2 and 3.2).

Two universal Correctness Properties from the design are exercised here with
Hypothesis over randomized, realistic inputs. Both are pure/deterministic with
no I/O, so they can be swept exhaustively.

* **Feature: closed-loop-prompt-optimizer, Property 16: Deterministic
  identifiers** — for every id helper in
  :mod:`bakeoff.quality.optimizer.ids` (:func:`iteration_id`,
  :func:`prompt_version_id`, :func:`gen_trial_id`), identical inputs always
  produce the identical id, and any field change produces a different id
  (collision-free over distinct inputs). This is the resume key that lets a
  re-invoked Phase-A run skip already-durable iterations.
  **Validates: Requirements 10.2**

* **Feature: closed-loop-prompt-optimizer, Property 7: CI half-width formula
  and its monotonicity** — :func:`bakeoff.quality.optimizer.stats.ci_half_width`
  equals ``z * s / sqrt(n)`` (with the module's exact two-sided ``z`` for the
  0.95 level, ``z ~= 1.95996``, the precise form of the ``1.96 * s / sqrt(n)``
  rule of thumb), is non-increasing in ``n``, and is exactly ``0`` when the
  between-conversation spread ``s == 0``. The spread itself is produced by
  :func:`bakeoff.quality.optimizer.stats.between_conversation_sd`.
  **Validates: Requirements 5.3, 5.8**
"""
from __future__ import annotations

import math

from hypothesis import given, settings
from hypothesis import strategies as st

from bakeoff import config
from bakeoff.quality.optimizer.ids import (
    gen_trial_id,
    iteration_id,
    prompt_version_id,
)
from bakeoff.quality.optimizer.stats import (
    between_conversation_sd,
    ci_half_width,
    _z_for_level,
)

# ---------------------------------------------------------------------------
# Strategy building blocks
# ---------------------------------------------------------------------------
# Free-text fields (model names, dataset item ids) drawn from printable ASCII
# (codepoints 33..126): excludes whitespace, control chars and -- crucially --
# the ``\x1f`` Unit-Separator the id scheme joins fields with, so distinct field
# tuples map to distinct canonical pre-hash strings (no delimiter-injection
# collisions). Also excludes surrogate codepoints, which ``str.encode('utf-8')``
# (used inside the hashers) would reject.
_free_text = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=0,
    max_size=16,
)

# Realistic optimizer phase and trial role: the actual closed domains from the
# ids module docstrings. Keeping ``role``/``phase`` to these dash-free literals
# means the ``"opt-{phase}-{role}"`` pass-name that ``gen_trial_id`` builds is an
# injective function of ``(phase, role)`` -- distinct inputs stay distinct.
_phase = st.sampled_from(["A", "B"])
_role = st.sampled_from(["champion", "challenger"])

# Iteration / repetition indices: non-negative (0 = seed iteration).
_iteration_index = st.integers(min_value=0, max_value=10_000)
_rep = st.integers(min_value=0, max_value=200)


@st.composite
def _iteration_args(draw):
    """Args for ``iteration_id(model, phase, iteration_index)``."""
    return (draw(_free_text), draw(_phase), draw(_iteration_index))


@st.composite
def _prompt_version_args(draw):
    """Args for ``prompt_version_id(model, iteration_index)``."""
    return (draw(_free_text), draw(_iteration_index))


@st.composite
def _gen_trial_args(draw):
    """Args for ``gen_trial_id(model, item_id, rep, role, phase)``."""
    return (
        draw(_free_text),
        draw(_free_text),
        draw(_rep),
        draw(_role),
        draw(_phase),
    )


# ===========================================================================
# Property 16 -- deterministic, collision-free identifiers
# Feature: closed-loop-prompt-optimizer, Property 16: Deterministic identifiers
# **Validates: Requirements 10.2**
# ===========================================================================
@settings(max_examples=200)
@given(
    it_a=_iteration_args(),
    it_b=_iteration_args(),
    pv_a=_prompt_version_args(),
    pv_b=_prompt_version_args(),
    gt_a=_gen_trial_args(),
    gt_b=_gen_trial_args(),
)
def test_property16_deterministic_identifiers(it_a, it_b, pv_a, pv_b, gt_a, gt_b):
    """Same inputs -> same id; any field change -> different id.

    Feature: closed-loop-prompt-optimizer, Property 16: Deterministic identifiers.
    Validates Requirements 10.2 (each unit of work gets a deterministic id so
    completed work is identifiable on resume).
    """
    # (1) Determinism: re-evaluating each helper on identical inputs is stable.
    assert iteration_id(*it_a) == iteration_id(*it_a)
    assert prompt_version_id(*pv_a) == prompt_version_id(*pv_a)
    assert gen_trial_id(*gt_a) == gen_trial_id(*gt_a)

    # (2) Collision-free over distinct inputs: any difference in any field
    #     yields a different id. (A genuine 64-bit SHA-256-prefix collision over
    #     distinct inputs is astronomically improbable and unreachable by the
    #     fuzzer, which cannot invert SHA-256.)
    if it_a != it_b:
        assert iteration_id(*it_a) != iteration_id(*it_b)
    if pv_a != pv_b:
        assert prompt_version_id(*pv_a) != prompt_version_id(*pv_b)
    if gt_a != gt_b:
        assert gen_trial_id(*gt_a) != gen_trial_id(*gt_b)


# ===========================================================================
# Property 7 -- CI half-width formula + monotonicity
# Feature: closed-loop-prompt-optimizer, Property 7: CI half-width formula and
#          its monotonicity
# **Validates: Requirements 5.3, 5.8**
# ===========================================================================
# Between-conversation standard deviation (s >= 0) and a slice size (n >= 1).
_sd = st.floats(min_value=0.0, max_value=10.0, allow_nan=False, allow_infinity=False)
_n_conversations = st.integers(min_value=1, max_value=100_000)
# A positive increment for the monotonicity check: a strictly larger slice.
_n_increment = st.integers(min_value=1, max_value=100_000)
# A slice of per-conversation triad scores on the 0..1 scale; >= 2 so the
# sample sd (ddof=1) is defined.
_conv_triads = st.lists(
    st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    min_size=2,
    max_size=50,
)


@settings(max_examples=200)
@given(sd=_sd, n=_n_conversations, n_inc=_n_increment, triads=_conv_triads)
def test_property7_ci_half_width_formula_and_monotonicity(sd, n, n_inc, triads):
    """half_width == z * s / sqrt(n); non-increasing in n; 0 when s == 0.

    Feature: closed-loop-prompt-optimizer, Property 7: CI half-width formula and
    its monotonicity. Validates Requirements 5.3 (95% CI from the
    between-conversation sd and slice size) and 5.8 (a larger slice tightens the
    half-width to resolve smaller gains).
    """
    z = _z_for_level(config.CONFIDENCE_LEVEL)

    # (a) Closed-form formula, using the module's exact z (~1.95996 at 0.95 --
    #     the precise form of the 1.96 * s / sqrt(n) rule of thumb).
    expected = z * sd / math.sqrt(n)
    assert math.isclose(ci_half_width(sd, n), expected, rel_tol=1e-9, abs_tol=1e-12)

    # (b) Non-increasing in n: from a base n, adding a positive increment never
    #     widens the interval (Req 5.8 -- a larger slice resolves smaller gains).
    assert ci_half_width(sd, n + n_inc) <= ci_half_width(sd, n) + 1e-12

    # (c) Exactly zero half-width when the spread is zero (no resolvable noise).
    assert ci_half_width(0.0, n) == 0.0

    # (d) between_conversation_sd produces the s that feeds the same formula
    #     (Req 5.3): the half-width on the measured spread matches z * s / sqrt(n).
    s = between_conversation_sd(triads)
    assert s >= 0.0
    assert math.isclose(
        ci_half_width(s, n), z * s / math.sqrt(n), rel_tol=1e-9, abs_tol=1e-12
    )

    # (e) A zero-spread (constant) slice has sd == 0, which flows through to a
    #     zero half-width -- the boundary of property (c) via the real estimator.
    constant_slice = [triads[0]] * len(triads)
    assert between_conversation_sd(constant_slice) == 0.0
    assert ci_half_width(between_conversation_sd(constant_slice), n) == 0.0


@settings(max_examples=100)
@given(
    triads=_conv_triads,
    singleton=st.lists(
        st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
        min_size=0,
        max_size=1,
    ),
)
def test_property7_between_conversation_sd_sanity(triads, singleton):
    """``between_conversation_sd`` is the ddof=1 sample SD, 0.0 for n < 2.

    Sanity exercise for the estimator behind Property 7 (Req 5.3): it returns
    the Bessel-corrected sample standard deviation for n >= 2 (matching a direct
    hand computation), and degenerates to exactly 0.0 for an empty or
    single-element slice (no measurable between-conversation spread).
    """
    # n < 2 -> exactly 0.0 (empty or singleton slice).
    assert between_conversation_sd(singleton) == 0.0

    # n >= 2 -> matches the ddof=1 sample SD computed directly from the values.
    s = between_conversation_sd(triads)
    assert s >= 0.0
    mean = sum(triads) / len(triads)
    variance = sum((x - mean) ** 2 for x in triads) / (len(triads) - 1)
    assert math.isclose(s, math.sqrt(variance), rel_tol=1e-9, abs_tol=1e-12)


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))


# ===========================================================================
# Tasks 8.2, 9.2, 9.3, 9.4 — four additional Correctness Properties.
#
# These exercise the *decision* half of the loop: failure selection
# (Property 4), the convergence stop rule (Property 9), champion monotonicity
# across accepted promotions (Property 3), and the triad-only promotion
# decision (Property 2). All four targets are pure / deterministic (no I/O), so
# they are swept with Hypothesis at >= 100 examples each.
# ===========================================================================
from bakeoff.quality.optimizer.convergence import (  # noqa: E402
    ConvergenceTracker,
    PromotionDecider,
)
from bakeoff.quality.optimizer.failures import select_failures  # noqa: E402
from bakeoff.quality.optimizer.judge_loop import SliceScore, TurnVerdict  # noqa: E402
from bakeoff.quality.optimizer.stats import is_significant  # noqa: E402
from bakeoff.quality.types import GroundTruthKind  # noqa: E402

# ---------------------------------------------------------------------------
# Shared strategy building blocks for the decision-half properties
# ---------------------------------------------------------------------------
# Triad / threshold scores live on the judge's 0..1 scale. ``_unit_score`` is the
# closed [0, 1] interval the per-conversation triad and the champion/challenger
# scores occupy; ``_pos_threshold`` is a strictly-positive significance threshold
# (so a promotion is a *strict* increase — needed for Property 3's strict-gain
# claim), and ``_any_threshold`` allows the boundary 0.0 for Property 2 where the
# threshold's exact value is irrelevant to the closeness-invariance claim.
_unit_score = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
_pos_threshold = st.floats(
    min_value=1e-3, max_value=1.0, allow_nan=False, allow_infinity=False
)
_any_threshold = st.floats(
    min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
)


def _verdict(*, item_id, rep, turn, overall, answered_when_unsure, evidence, grounding_fragment_ids):
    """Build a :class:`TurnVerdict` varying only the fields Property 4 keys on.

    The remaining (non-ordering) fields are filled with valid, fixed placeholders
    so the verdict is a well-formed judge verdict; ``overall``,
    ``answered_when_unsure`` and the deterministic tie-break key
    ``(item_id, rep, turn)`` are the parts ``select_failures`` actually orders on,
    and ``evidence`` / ``grounding_fragment_ids`` are the carried payload the
    property asserts is preserved through selection.
    """
    return TurnVerdict(
        item_id=item_id,
        rep=rep,
        turn=turn,
        ground_truth_kind=GroundTruthKind.GOLD,
        overall=overall,
        dimensions={"faithfulness": overall, "correctness": overall, "completeness": overall},
        abstention_correct=None,
        answered_when_unsure=answered_when_unsure,
        fragments_sufficient=True,
        grounding_fragment_ids=grounding_fragment_ids,
        evidence=evidence,
        answer_excerpt="answer",
        closeness=0.0,
    )


@st.composite
def _turn_verdict_lists(draw):
    """Generate a list of ``TurnVerdict``\\ s with varied ordering + payload fields.

    ``item_id`` is drawn from a tiny alphabet and ``overall`` from the unit
    interval so genuine ties on the leading sort-key components arise (exercising
    the deterministic ``(overall, item_id, rep, turn)`` tie-break), and each
    verdict carries a distinct ``evidence`` payload + ``grounding_fragment_ids``
    so the "evidence is carried through selection" assertion is meaningful.
    """
    n = draw(st.integers(min_value=0, max_value=12))
    verdicts = []
    for i in range(n):
        overall = draw(_unit_score)
        verdicts.append(
            _verdict(
                item_id=draw(st.sampled_from(["a", "b", "c"])),
                rep=draw(st.integers(min_value=0, max_value=3)),
                turn=draw(st.integers(min_value=1, max_value=4)),
                overall=overall,
                answered_when_unsure=draw(st.booleans()),
                # Distinct, non-empty evidence per verdict so we can assert it
                # survives selection verbatim.
                evidence={"span": f"evidence-{i}", "grounding": draw(_free_text)},
                grounding_fragment_ids=tuple(
                    draw(st.lists(_free_text, min_size=0, max_size=3))
                ),
            )
        )
    return verdicts


def _make_slice_score(verdicts):
    """Wrap ``verdicts`` in a minimal, well-formed :class:`SliceScore`.

    ``select_failures`` reads only ``score.verdicts``; the remaining aggregate
    fields are filled with valid placeholders so the dataclass is complete
    without affecting selection.
    """
    return SliceScore(
        model="m",
        prompt_role="champion",
        triad_score=0.0,
        ci_half_width=0.0,
        ci_low=0.0,
        ci_high=0.0,
        n_conversations=0,
        between_conv_sd=0.0,
        per_dimension_mean={},
        abstention_reward_mean=0.0,
        answered_when_unsure_rate=0.0,
        mean_closeness=0.0,
        verdicts=tuple(verdicts),
    )


def _selection_key(v: TurnVerdict):
    """The documented total order ``select_failures`` sorts by (design Property 4).

    Answering-when-unsure turns first (leading 0/1 group flag), then ascending
    ``overall`` (worst-first), then the deterministic ``(item_id, rep, turn)``
    tie-break.
    """
    return (0 if v.answered_when_unsure else 1, v.overall, v.item_id, v.rep, v.turn)


# ===========================================================================
# Property 4 -- failure selection: k lowest judged turns, abstention-first,
#               evidence carried.
# Feature: closed-loop-prompt-optimizer, Property 4: Failure selection returns
#          the k lowest judged turns with their evidence, and
#          answering-when-unsure turns are surfaced first.
# **Validates: Requirements 1.3, 3.4, 14.4**
# ===========================================================================
@settings(max_examples=100)
@given(verdicts=_turn_verdict_lists(), k=st.integers(min_value=-2, max_value=20))
def test_property4_failure_selection_lowest_k_abstention_first_with_evidence(verdicts, k):
    """``select_failures`` returns the ``min(k, n)`` lowest turns by the documented
    order, surfaces answering-when-unsure turns first, and carries their evidence.

    Feature: closed-loop-prompt-optimizer, Property 4: Failure selection returns
    the k lowest judged turns with their evidence, and answering-when-unsure turns
    are surfaced first. Validates Requirements 1.3 (the Champion's worst turns
    drive the rewrite), 3.4 (k is operator-tunable), and 14.4 (over-claiming /
    answering-when-unsure failures are surfaced prominently).
    """
    n = len(verdicts)
    score = _make_slice_score(verdicts)
    result = select_failures(score, k=k)

    # (1) Cardinality: exactly min(k, n) for a positive k; empty for k <= 0.
    expected_len = min(k, n) if k > 0 else 0
    assert len(result) == expected_len

    # (2) Lowest-by-ordering: the multiset of selection keys returned equals the
    #     expected_len smallest keys over all verdicts (robust to ties: equal
    #     keys compare equal even if the concrete object at the boundary differs).
    all_keys_sorted = sorted(_selection_key(v) for v in verdicts)
    assert sorted(_selection_key(v) for v in result) == all_keys_sorted[:expected_len]

    # (3) Returned worst-first: the result itself is in the documented order.
    assert [_selection_key(v) for v in result] == sorted(_selection_key(v) for v in result)

    # (4) Answering-when-unsure surfaced first: because every awu turn (group flag
    #     0) sorts ahead of every non-awu turn (flag 1), the selection takes ALL
    #     awu turns before any other -- so the count of awu turns selected is
    #     exactly min(expected_len, total awu turns), and they lead the result.
    leading_flags = [0 if v.answered_when_unsure else 1 for v in result]
    assert leading_flags == sorted(leading_flags)
    n_awu = sum(1 for v in verdicts if v.answered_when_unsure)
    assert sum(1 for v in result if v.answered_when_unsure) == min(expected_len, n_awu)

    # (5) Evidence carried: every returned verdict is one of the input verdict
    #     objects (so its evidence, per-dimension scores and grounding fragment
    #     ids are carried verbatim), and the evidence payload is non-empty.
    for v in result:
        assert any(v is original for original in verdicts)
        assert v.evidence  # the distinct, non-empty evidence dict survived

    # (6) The default k is the operator-tunable config value (Req 3.4).
    default_result = select_failures(score)
    assert len(default_result) == min(config.QUALITY_OPT_FAILURES_K, n)


# ===========================================================================
# Property 9 -- convergence counter and stop rule.
# Feature: closed-loop-prompt-optimizer, Property 9: Convergence counter and stop
#          rule -- counter == trailing reject run, resets to 0 on promotion, stops
#          exactly at the first iteration the run reaches L.
# **Validates: Requirements 6.1, 6.2, 6.3, 6.5**
# ===========================================================================
@settings(max_examples=100)
@given(
    outcomes=st.lists(st.booleans(), min_size=0, max_size=40),
    stop_limit=st.integers(min_value=1, max_value=6),
)
def test_property9_convergence_counter_and_stop_rule(outcomes, stop_limit):
    """The tracker's counter equals the trailing reject run, resets on promotion,
    and converges exactly at the first iteration the run reaches ``stop_limit``.

    Feature: closed-loop-prompt-optimizer, Property 9: Convergence counter and stop
    rule. Validates Requirements 6.1 (count consecutive non-improving iterations),
    6.2 (reset on promotion), 6.3 (stop at the limit), and 6.5 (configurable limit).
    ``outcomes[i] == True`` is a promotion at iteration ``i``; ``False`` is a
    non-improving iteration.
    """
    tracker = ConvergenceTracker(stop_limit=stop_limit)

    expected_counter = 0
    expected_converged_iteration = None
    for i, promoted in enumerate(outcomes):
        tracker.record(promoted=promoted, iteration_index=i)

        # Reference counter: reset on promotion, else increment (Req 6.1/6.2).
        if promoted:
            expected_counter = 0
        else:
            expected_counter += 1
        # First crossing of the limit is sticky thereafter (Req 6.3).
        if expected_counter >= stop_limit and expected_converged_iteration is None:
            expected_converged_iteration = i

        # Independent check that the counter equals the trailing run of rejects in
        # the outcomes seen so far (counter == trailing reject run).
        trailing_rejects = 0
        for o in reversed(outcomes[: i + 1]):
            if o:
                break
            trailing_rejects += 1
        assert tracker.consecutive_non_improving == trailing_rejects == expected_counter

        # Convergence point is recorded exactly once, at the first crossing, and is
        # never moved or cleared by later calls.
        assert tracker.converged_iteration == expected_converged_iteration
        assert tracker.should_stop == (expected_converged_iteration is not None)
        if expected_converged_iteration is not None:
            assert (
                tracker.stop_reason
                == f"{stop_limit} consecutive non-improving iterations"
            )
        else:
            assert tracker.stop_reason is None


# ===========================================================================
# Property 3 -- champion monotonicity across accepted promotions.
# Feature: closed-loop-prompt-optimizer, Property 3: Champion triad is
#          monotonically non-decreasing across accepted promotions, and each
#          promotion strictly increases the champion by >= the threshold.
# **Validates: Requirements 1.6, 5.1**
# ===========================================================================
@settings(max_examples=100)
@given(
    initial=_unit_score,
    challengers=st.lists(_unit_score, min_size=0, max_size=30),
    threshold=_pos_threshold,
)
def test_property3_champion_monotonic_across_accepted_promotions(initial, challengers, threshold):
    """Replaying challengers through the ``PromotionDecider`` yields a
    non-decreasing champion, and every accepted promotion gains >= threshold.

    Feature: closed-loop-prompt-optimizer, Property 3: Champion triad is
    monotonically non-decreasing across accepted promotions, and each promotion
    strictly increases the champion by >= the threshold. Validates Requirements 1.6
    (promote on a significant triad gain) and 5.1 (the absolute-delta significance
    test). ``threshold`` is strictly positive, so an accepted promotion is a
    *strict* increase.
    """
    decider = PromotionDecider()
    champion = initial
    champions = [champion]  # champion value after each accepted promotion

    for challenger in challengers:
        promoted = decider.decide(champion, challenger, threshold)
        # The decision is exactly the absolute-delta significance test (Req 1.6/5.1).
        assert promoted == is_significant(champion, challenger, threshold)
        if promoted:
            # Each accepted promotion gains at least the threshold (>= threshold),
            # and since threshold > 0 it strictly raises the champion.
            assert challenger - champion >= threshold
            assert challenger > champion
            champion = challenger
            champions.append(champion)

    # The champion is monotonically non-decreasing across accepted promotions...
    assert all(champions[i] <= champions[i + 1] for i in range(len(champions) - 1))
    # ...and every recorded step is a >= threshold increase.
    for i in range(len(champions) - 1):
        assert champions[i + 1] - champions[i] >= threshold


# ===========================================================================
# Property 2 -- the decision depends ONLY on the triad, never on closeness.
# Feature: closed-loop-prompt-optimizer, Property 2: The decision depends only on
#          the triad, never on closeness -- holding triad scores fixed and varying
#          closeness arbitrarily leaves the promotion decision unchanged.
# **Validates: Requirements 2.1, 2.4, 2.5**
# ===========================================================================
@settings(max_examples=100)
@given(
    champion=_unit_score,
    challenger=_unit_score,
    threshold=_any_threshold,
    usable=st.booleans(),
    closeness_values=st.lists(
        st.floats(min_value=-1.0, max_value=1.0, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=10,
    ),
)
def test_property2_decision_depends_only_on_triad_not_closeness(
    champion, challenger, threshold, usable, closeness_values
):
    """Varying closeness arbitrarily never changes the promotion decision, which
    is governed solely by the triad scores (gated only by usability).

    Feature: closed-loop-prompt-optimizer, Property 2: The decision depends only on
    the triad, never on closeness. Validates Requirements 2.1 (the triad is the
    sole decision metric), 2.4 (closeness is a non-deciding secondary cross-check),
    and 2.5 (the judge triad is authoritative even when closeness disagrees).
    Closeness is not even an input to ``PromotionDecider.decide`` -- the strongest
    possible form of "never depends on closeness" -- so for any sequence of
    arbitrary closeness values the decision is constant.
    """
    decider = PromotionDecider()
    decision = decider.decide(champion, challenger, threshold, usable=usable)

    # The decision is exactly the triad predicate, gated by usability (Req 2.1):
    # closeness contributes nothing.
    expected = usable and is_significant(champion, challenger, threshold)
    assert decision == expected

    # Holding the triad scores fixed while "closeness" ranges over arbitrary values
    # (high, low, even contradicting the triad) leaves the decision unchanged
    # (Req 2.4/2.5): closeness is never consulted.
    for _closeness in closeness_values:
        assert decider.decide(champion, challenger, threshold, usable=usable) == decision


# ===========================================================================
# Tasks 3.3, 3.4, 5.4, 5.5 — four further Correctness Properties.
#
# Property 8  (3.3) — gain is reported as BOTH an absolute delta and a
#                     percentage, and the promotion DECISION is invariant to the
#                     percentage (keys only on the absolute delta vs threshold).
# Property 1  (3.4) — promotion iff a significant triad gain: ``is_significant``
#                     promotes iff ``(challenger - champion) >= threshold``,
#                     otherwise the champion is retained.
# Property 14 (5.4) — append-only round-trip + ordered per-model version-history
#                     lookback over a temp-path ``OptimizerStore``.
# Property 17 (5.5) — durable writes tolerate a truncated trailing line: a
#                     truncated FINAL line is dropped (the complete prefix is
#                     recovered without raising); a corrupted NON-final line
#                     raises.
#
# Properties 8 and 1 are pure/deterministic; Properties 14 and 17 exercise the
# durable store against fresh per-example temp files (never the real data store).
# All four are swept at >= 100 Hypothesis examples.
# ===========================================================================
import json  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from hypothesis import HealthCheck, assume  # noqa: E402

from bakeoff.quality.optimizer.stats import gain_report  # noqa: E402
from bakeoff.quality.optimizer.store import (  # noqa: E402
    AuditRecord,
    DrivingFailure,
    IterationRecord,
    OptimizerStore,
    PromptVersion,
)


# ===========================================================================
# Property 8 -- gain reported as absolute delta + percentage; decision invariant
#               to the percentage.
# Feature: closed-loop-prompt-optimizer, Property 8: Gain is reported as both an
#          absolute delta and a percentage, and the decision is invariant to the
#          percentage.
# **Validates: Requirements 5.4, 5.5**
# ===========================================================================
@settings(max_examples=100)
@given(
    champion=_unit_score,
    challenger=_unit_score,
    threshold=_any_threshold,
    alt_champions=st.lists(
        st.floats(min_value=1e-6, max_value=1.0, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=6,
    ),
)
def test_property8_gain_report_absolute_and_percent_decision_invariant(
    champion, challenger, threshold, alt_champions
):
    """``gain_report`` returns both the absolute delta and the percentage, and the
    promotion decision keys only on the absolute delta -- never on the percentage.

    Feature: closed-loop-prompt-optimizer, Property 8: Gain is reported as both an
    absolute delta and a percentage, and the decision is invariant to the
    percentage. Validates Requirements 5.4 (report each gain both as an absolute
    Judge_Triad_Score delta and as a percentage relative to the previous champion)
    and 5.5 (the decision keys on the absolute delta tied to the CI, not on the
    percentage figure).
    """
    # (A) Both representations are present and correct.
    report = gain_report(champion, challenger)
    assert set(report) == {"absolute_delta", "percent_delta"}
    # The absolute delta is exactly ``new - prev`` (same float op the predicate uses).
    assert report["absolute_delta"] == challenger - champion
    # The percentage is ``delta / prev * 100`` when prev > 0, else the inf sentinel.
    if champion > 0:
        assert math.isclose(
            report["percent_delta"],
            (challenger - champion) / champion * 100.0,
            rel_tol=1e-9,
            abs_tol=1e-12,
        )
    else:
        assert report["percent_delta"] == float("inf")

    # (B) The decision keys ONLY on the absolute delta vs the threshold. This is
    #     exact -- it is the same subtraction/inequality the predicate computes --
    #     and the percentage is never an input.
    gain = challenger - champion
    decision = is_significant(champion, challenger, threshold)
    assert decision == (gain >= threshold)

    # (C) Invariance to the percentage: hold the absolute delta fixed at ``gain``
    #     while varying the previous score (and therefore the percentage) across
    #     ``alt_champions``; the decision is unchanged even as the percentage moves.
    #     ``assume`` keeps the fixed gain off the float knife-edge at the threshold
    #     so the tiny rounding in ``(c2 + gain) - c2`` cannot flip the inequality.
    assume(abs(gain - threshold) > 1e-9)
    percents = [report["percent_delta"]] if champion > 0 else []
    denominators = [champion] if champion > 0 else []
    for c2 in alt_champions:
        ch2 = c2 + gain
        rep2 = gain_report(c2, ch2)
        # Same absolute-delta side of the threshold -> identical decision, whatever
        # the percentage is.
        assert is_significant(c2, ch2, threshold) == decision
        percents.append(rep2["percent_delta"])
        denominators.append(c2)

    # The decision stayed constant while the percentage genuinely varied: distinct
    # previous scores give distinct percentages whenever the (fixed) gain is large
    # enough to be observable. A denormal/near-zero gain (e.g. ~1e-308) is excluded
    # because ``c2 + gain == c2`` in float there, collapsing every percentage to 0.0 —
    # that is a float-resolution artifact, not a percentage the decision could ride on.
    if abs(gain) > 1e-12 and len(set(denominators)) >= 2:
        assert len(set(percents)) >= 2


# ===========================================================================
# Property 1 -- promotion iff a significant triad gain.
# Feature: closed-loop-prompt-optimizer, Property 1: Promotion iff significant
#          triad gain -- ``is_significant`` promotes iff ``(challenger - champion)
#          >= threshold``, otherwise retains the champion.
# **Validates: Requirements 1.6, 5.1, 5.5, 5.6**
# ===========================================================================
@settings(max_examples=100)
@given(
    champion=_unit_score,
    challenger=_unit_score,
    threshold=_any_threshold,
    thresholds=st.lists(_any_threshold, min_size=0, max_size=5),
)
def test_property1_promotion_iff_significant_triad_gain(
    champion, challenger, threshold, thresholds
):
    """The challenger is promoted iff ``(challenger - champion) >= threshold``;
    otherwise the champion is retained -- the inclusive absolute-delta test.

    Feature: closed-loop-prompt-optimizer, Property 1: Promotion iff significant
    triad gain. Validates Requirements 1.6 (promote only if the challenger beats
    the champion by at least the Significance_Threshold; otherwise retain), 5.1
    (the significance test is the absolute triad delta vs the threshold), 5.5 (the
    decision keys on the absolute delta, never a percentage) and 5.6 (the threshold
    is configurable).
    """
    gain = challenger - champion
    promoted = is_significant(champion, challenger, threshold)

    # Promote iff a significant gain; otherwise retain the champion. Exact: the
    # assertion recomputes the predicate's own subtraction/inequality.
    assert promoted == (gain >= threshold)
    if promoted:
        assert gain >= threshold  # a real (>= threshold) gain drove the promotion
    else:
        assert gain < threshold  # below threshold -> the champion is retained

    # Req 5.6: the threshold is just a parameter -- the predicate honours the
    # configured default and any other threshold value identically.
    assert is_significant(
        champion, challenger, config.QUALITY_OPT_SIGNIFICANCE_THRESHOLD
    ) == ((challenger - champion) >= config.QUALITY_OPT_SIGNIFICANCE_THRESHOLD)
    for t in thresholds:
        assert is_significant(champion, challenger, t) == (gain >= t)

    # Boundary (Req 5.1/1.6): promotion is inclusive at exactly ``gain == threshold``
    # and does NOT fire just below it. ``champion == 0.0`` makes ``challenger - 0.0``
    # float-exact, so these boundary checks are not subject to rounding.
    assert is_significant(0.0, threshold, threshold) is True
    if threshold > 0:
        just_below = math.nextafter(threshold, float("-inf"))
        assert is_significant(0.0, just_below, threshold) is False


# ---------------------------------------------------------------------------
# Strategy building blocks for the durable-store properties (14, 17)
# ---------------------------------------------------------------------------
# At least two models so the per-model partitioning of history/lookback/resume is
# actually exercised. ``iteration_index`` is drawn from a small range so genuine
# index ties arise (exercising the stable per-model ordering).
_STORE_MODELS = ["model-a", "model-b", "model-c"]
_rt_model = st.sampled_from(_STORE_MODELS)
# Text that round-trips byte-exactly through ``json.dumps(ensure_ascii=False)`` /
# ``json.loads``: any unicode except lone surrogates (which utf-8 encoding rejects).
# Real control characters (newlines/tabs) are fine -- json escapes them, so a record
# is always one physical JSONL line.
_rt_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)), min_size=0, max_size=12
)
# Finite floats round-trip exactly through JSON (Python emits round-trippable reprs).
_rt_float = st.floats(allow_nan=False, allow_infinity=False, min_value=-1e6, max_value=1e6)
_rt_index = st.integers(min_value=0, max_value=6)


@st.composite
def _driving_failures(draw):
    """A short (0..2) tuple of well-formed :class:`DrivingFailure`\\ s."""
    out = []
    for _ in range(draw(st.integers(min_value=0, max_value=2))):
        out.append(
            DrivingFailure(
                item_id=draw(_rt_text),
                rep=draw(st.integers(min_value=0, max_value=20)),
                turn=draw(st.integers(min_value=0, max_value=20)),
                overall=draw(_rt_float),
                dimensions=draw(st.dictionaries(_rt_text, _rt_float, max_size=3)),
                abstention_correct=draw(st.one_of(st.none(), st.booleans())),
                answered_when_unsure=draw(st.booleans()),
                fragments_sufficient=draw(st.booleans()),
                grounding_fragment_ids=tuple(draw(st.lists(_rt_text, max_size=3))),
                evidence=draw(st.dictionaries(_rt_text, _rt_text, max_size=3)),
                answer_excerpt=draw(_rt_text),
            )
        )
    return tuple(out)


@st.composite
def _iteration_record(draw):
    """A valid :class:`IterationRecord` with all fields populated (round-trip exact)."""
    return IterationRecord(
        iteration_id=draw(_rt_text),
        model=draw(_rt_model),
        phase=draw(st.sampled_from(["A", "B"])),
        iteration_index=draw(_rt_index),
        backend=draw(_rt_text),
        author_model=draw(_rt_text),
        judge_model=draw(_rt_text),
        champion_score=draw(_rt_float),
        champion_ci_half_width=draw(_rt_float),
        challenger_score=draw(st.one_of(st.none(), _rt_float)),
        challenger_ci_half_width=draw(st.one_of(st.none(), _rt_float)),
        significance_threshold=draw(_rt_float),
        promoted=draw(st.booleans()),
        gain_absolute=draw(st.one_of(st.none(), _rt_float)),
        gain_percent=draw(st.one_of(st.none(), _rt_float)),
        slice_n_conversations=draw(st.integers(min_value=0, max_value=1000)),
        between_conversation_sd=draw(_rt_float),
        consecutive_non_improving=draw(st.integers(min_value=0, max_value=50)),
        converged=draw(st.booleans()),
        stop_reason=draw(st.one_of(st.none(), _rt_text)),
        mean_closeness=draw(_rt_float),
        abstention_reward_mean=draw(_rt_float),
        answered_when_unsure_rate=draw(_rt_float),
        retrieval_backend=draw(_rt_text),
        created_at=draw(_rt_text),
    )


@st.composite
def _audit_record(draw):
    """A valid :class:`AuditRecord` (incl. nested failures + dicts; round-trip exact)."""
    return AuditRecord(
        iteration_id=draw(_rt_text),
        prompt_version_id=draw(_rt_text),
        model=draw(_rt_model),
        iteration_index=draw(_rt_index),
        backend=draw(_rt_text),
        author_model=draw(_rt_text),
        judge_model=draw(_rt_text),
        champion_instruction=draw(_rt_text),
        challenger_instruction=draw(st.one_of(st.none(), _rt_text)),
        prompt_diff=draw(_rt_text),
        author_rationale=draw(_rt_text),
        driving_failures=draw(_driving_failures()),
        challenger_triad=draw(st.one_of(st.none(), _rt_float)),
        challenger_ci_half_width=draw(st.one_of(st.none(), _rt_float)),
        challenger_per_dimension=draw(st.dictionaries(_rt_text, _rt_float, max_size=3)),
        accepted=draw(st.booleans()),
        created_at=draw(_rt_text),
    )


# A sequence of store operations: each is an ("iter", IterationRecord) or
# ("audit", AuditRecord) append, drawn so a run interleaves both stores and >= 2
# models in a single example.
_store_ops = st.lists(
    st.one_of(
        _iteration_record().map(lambda r: ("iter", r)),
        _audit_record().map(lambda r: ("audit", r)),
    ),
    min_size=0,
    max_size=10,
)


def _fresh_store(tmpdir):
    """An :class:`OptimizerStore` whose four paths all live under ``tmpdir``.

    Every path is overridden so the test never reads or writes the real
    ``data/bakeoff`` optimizer stores.
    """
    base = Path(tmpdir)
    return OptimizerStore(
        iterations_path=base / "iterations.jsonl",
        audit_path=base / "audit.jsonl",
        errors_path=base / "errors.jsonl",
        results_path=base / "results.json",
    )


# ===========================================================================
# Property 14 -- append-only round-trip + ordered version-history lookback.
# Feature: closed-loop-prompt-optimizer, Property 14: Append-only round-trip and
#          ordered version-history lookback.
# **Validates: Requirements 8.2, 8.4, 8.5, 10.1**
# ===========================================================================
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(ops=_store_ops)
def test_property14_append_only_roundtrip_and_ordered_version_lookback(ops):
    """Records read back identically and in order; appends never alter earlier
    lines; per-model version history is ordered by iteration index; lookback-n
    returns the correct trailing n versions.

    Feature: closed-loop-prompt-optimizer, Property 14: Append-only round-trip and
    ordered version-history lookback. Validates Requirements 8.2 (audit records are
    persisted to an append-only store), 8.4 (all prior prompt versions are retained
    and retrievable), 8.5 (the ordered per-model version history supports lookback
    of several versions) and 10.1 (iteration state is persisted to append-only JSONL
    stores). A fresh temp-path store is used per example -- never the real data store.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _fresh_store(tmpdir)

        appended_iters: list[IterationRecord] = []
        appended_audits: list[AuditRecord] = []
        for kind, rec in ops:
            if kind == "iter":
                # Per-record read-back identity (round-trip exactness).
                assert IterationRecord.from_jsonl(rec.to_jsonl()) == rec
                store.append_iteration(rec)
                appended_iters.append(rec)
            else:
                assert AuditRecord.from_jsonl(rec.to_jsonl()) == rec
                store.append_audit(rec)
                appended_audits.append(rec)

            # Append-only: each new append leaves every earlier line untouched, so
            # the full read-back always equals the running append-ordered list.
            assert store.read_iterations() == appended_iters
            assert store.read_audits() == appended_audits

        # Final read-back identity, in append order, for both stores (Req 10.1/8.2).
        assert store.read_iterations() == appended_iters
        assert store.read_audits() == appended_audits

        for model in _STORE_MODELS:
            # Ordered per-model version history: filter to the model (in append
            # order) then stable-sort by iteration_index, projected to PromptVersion
            # -- exactly what the store reconstructs (Req 8.4/8.5).
            model_audits = [a for a in appended_audits if a.model == model]
            expected_versions = [
                PromptVersion.from_audit(a)
                for a in sorted(model_audits, key=lambda a: a.iteration_index)
            ]
            history = store.prompt_version_history(model)
            assert history == expected_versions
            # History is non-decreasing in iteration_index (ordered).
            assert [v.iteration_index for v in history] == sorted(
                v.iteration_index for v in history
            )

            # lookback-n returns the correct trailing n versions, in order.
            for n in (-1, 0, 1, 2, len(expected_versions), len(expected_versions) + 3):
                expected_tail = expected_versions[-n:] if n > 0 else []
                assert store.lookback(model, n) == expected_tail

            # Resume key set: the durable iteration ids for this model (Req 10.1).
            expected_ids = {
                r.iteration_id for r in appended_iters if r.model == model
            }
            assert store.completed_iteration_ids(model) == expected_ids


# ===========================================================================
# Property 17 -- durable writes tolerate a truncated trailing line.
# Feature: closed-loop-prompt-optimizer, Property 17: Durable writes tolerate a
#          truncated trailing line.
# **Validates: Requirements 10.8**
# ===========================================================================
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    records=st.lists(_iteration_record(), min_size=1, max_size=6),
    cut=st.integers(min_value=1, max_value=4096),
)
def test_property17_truncated_trailing_line_tolerated_interior_corruption_raises(
    records, cut
):
    """A truncated FINAL line is dropped and the complete prefix is recovered
    without raising; a corrupted NON-final line raises.

    Feature: closed-loop-prompt-optimizer, Property 17: Durable writes tolerate a
    truncated trailing line. Validates Requirement 10.8 (each durable record is one
    flushed JSONL line, so an interruption never leaves a half-written record that
    breaks resume -- a reader recovers the complete prefix). A fresh temp file is
    used per example.
    """
    valid_lines = [r.to_jsonl() for r in records]

    # Build a genuinely partial/unparseable final line from a real serialized
    # record, truncated to a strict, non-empty prefix.
    base_line = valid_lines[-1]
    cut_at = min(cut, len(base_line) - 1) if len(base_line) > 1 else 1
    truncated = base_line[:cut_at]
    # Only proceed when the prefix is genuinely corrupt (does not parse as JSON);
    # a strict prefix of a JSON object effectively always is, but assume defensively.
    try:
        json.loads(truncated)
        assume(False)  # extraordinarily rare: the prefix happened to be valid JSON
    except json.JSONDecodeError:
        pass

    with tempfile.TemporaryDirectory() as tmpdir:
        store = _fresh_store(tmpdir)

        # (1) Truncated FINAL line: complete prefix written as whole fsync'd lines,
        #     then a truncated partial line with NO trailing newline.
        store.iterations_path.write_text(
            "".join(line + "\n" for line in valid_lines) + truncated,
            encoding="utf-8",
        )
        # The reader recovers the full valid prefix and does not raise.
        recovered = store.read_iterations()
        assert recovered == records

        # (2) Corrupted NON-final line: a broken line followed by a valid line is
        #     genuine interior corruption and must raise (not be silently dropped).
        corrupt_line = '{"corrupt":'
        store.iterations_path.write_text(
            valid_lines[0] + "\n" + corrupt_line + "\n" + valid_lines[-1] + "\n",
            encoding="utf-8",
        )
        with pytest.raises(Exception):
            store.read_iterations()


# ===========================================================================
# Tasks 15.3, 16.4, 16.5 — three further Correctness Properties covering the
# streaming/visualization seam and the per-model orchestration layer.
#
# Property 19 (15.3) — per-model event streams never interleave ambiguously:
#                      filtering the shared broker stream by ``model_channel``
#                      partitions it cleanly per model, in order, with none
#                      misattributed and the total conserved.
# Property 21 (16.4) — concurrency is gated on per-model visualization: the
#                      orchestrator runs concurrently iff every running model has
#                      an active Per_Model_View, else sequentially.
# Property 20 (16.5) — per-model loop structure + state isolation: each model is
#                      run exactly once, produces exactly one outcome (a
#                      challenger or a recorded "no usable challenger"), results
#                      map 1:1 to models, and a model's outcome never depends on
#                      another's.
#
# Property 19 drives the *real* :class:`bakeoff.app.SSEBroker` (unchanged) via the
# real :class:`OptimizerEventEmitter`; Properties 20/21 drive the real
# :class:`PerModelOrchestrator` / :class:`ViewRegistry`. Property 20 drives the
# async ``orchestrator.run()`` via ``asyncio.run``. No network; tiny per-example
# work; each swept at >= 100 Hypothesis examples.
# ===========================================================================
import asyncio  # noqa: E402
from collections import Counter  # noqa: E402

from bakeoff.app import SSEBroker  # noqa: E402
from bakeoff.quality.optimizer.events import (  # noqa: E402
    EVENT_AUDIT_FLAG,
    EVENT_AUTHOR_TOKEN,
    EVENT_CHAMPION_SCORED,
    EVENT_CONVERGED,
    EVENT_ISLAND_STEP,
    EVENT_ITERATION_COMPLETED,
    EVENT_MIGRATION,
    EVENT_PHASE_B,
    EVENT_RUNG_ESCALATED,
    EVENT_TOURNAMENT,
    MODEL_CHANNEL,
    OPTIMIZER_EVENT_TYPES,
    OptimizerEventEmitter,
)
from bakeoff.quality.optimizer.orchestrator import (  # noqa: E402
    ConcurrencyDecision,
    PerModelOrchestrator,
    ViewRegistry,
)

# A small, fixed pool of distinct, surrogate-free model names. Drawing a unique
# subset guarantees the orchestrator's order-preserving de-duplication is a no-op,
# so ``ConcurrencyDecision.models`` equals the drawn list verbatim.
_MODEL_POOL = ["model-a", "model-b", "model-c", "model-d", "model-e"]
# Ghost models that are NEVER in any running set: used to mark *non-running* views
# active and prove the gate keys only on the running models (Req 1.11).
_GHOST_POOL = ["ghost-1", "ghost-2", "ghost-3"]

# For Property 19 read-back: the int field each typed emit helper carries that we
# overload as a unique, monotonically-increasing emission marker, so a received
# frame can be matched back to the exact emission it came from (per-model order is
# then verifiable, not just counts).
_MARKER_FIELD = {
    EVENT_CHAMPION_SCORED: "iteration_index",
    EVENT_AUTHOR_TOKEN: "iteration_index",
    EVENT_ITERATION_COMPLETED: "iteration_index",
    EVENT_CONVERGED: "converged_iteration",
    EVENT_PHASE_B: "n_conversations",
    EVENT_ISLAND_STEP: "rung_index",
    EVENT_RUNG_ESCALATED: "to_rung",
    EVENT_TOURNAMENT: "round",
    EVENT_MIGRATION: "round",
    EVENT_AUDIT_FLAG: "round",
}


def _emit_marked(emitter: OptimizerEventEmitter, event_type: str, model: str, marker: int):
    """Emit one event of ``event_type`` for ``model`` through the *typed* helper.

    Every helper funnels through :meth:`OptimizerEventEmitter.emit`, which is the
    sole path that stamps ``model_channel`` (design Property 19 holds by
    construction). ``marker`` is embedded into the helper's int field named in
    :data:`_MARKER_FIELD` so the frame is recoverable on the other side. All other
    kwargs are valid, fixed placeholders that match the design's payload shapes.
    """
    if event_type == EVENT_CHAMPION_SCORED:
        emitter.champion_scored(
            model=model,
            phase="A",
            iteration_index=marker,
            role="champion",
            triad=0.5,
            ci_half_width=0.01,
            ci_low=0.49,
            ci_high=0.51,
            per_dimension={"faithfulness": 0.5},
            abstention_reward_mean=0.0,
            answered_when_unsure_rate=0.0,
            retrieval_backend="frozen",
            mean_closeness=0.0,
            n_conversations=10,
        )
    elif event_type == EVENT_AUTHOR_TOKEN:
        emitter.author_token(model=model, iteration_index=marker, delta="tok")
    elif event_type == EVENT_ITERATION_COMPLETED:
        emitter.iteration_completed(
            model=model,
            iteration_index=marker,
            challenger_triad=0.5,
            challenger_ci_half_width=0.01,
            gain_absolute=0.0,
            gain_percent=0.0,
            accepted=False,
            consecutive_non_improving=1,
            champion_instruction="instr",
            prompt_diff="diff",
            lookback_version_ids=["v0", "v1"],
        )
    elif event_type == EVENT_CONVERGED:
        emitter.converged(model=model, converged_iteration=marker, stop_reason="limit")
    elif event_type == EVENT_PHASE_B:
        emitter.phase_b(model=model, triad=0.5, ci_half_width=0.01, n_conversations=marker)
    elif event_type == EVENT_ISLAND_STEP:
        emitter.island_step(
            model=model, island_id=0, rung_index=marker,
            champion_score=0.5, ci_half_width=0.01, state="iterating",
        )
    elif event_type == EVENT_RUNG_ESCALATED:
        emitter.rung_escalated(model=model, island_id=0, from_rung=0, to_rung=marker)
    elif event_type == EVENT_TOURNAMENT:
        emitter.tournament(
            model=model, round=marker,
            island_a={"champion_score": 0.5, "ci_half_width": 0.01},
            island_b={"champion_score": 0.5, "ci_half_width": 0.01},
            shared_rung=2, winner=0,
        )
    elif event_type == EVENT_MIGRATION:
        emitter.migration(model=model, round=marker, winning_prompt_version_id="v0")
    elif event_type == EVENT_AUDIT_FLAG:
        emitter.audit_flag(
            model=model,
            round=marker,
            report={"divergence": 0.5, "threshold": 0.3, "flagged": True},
        )
    else:  # pragma: no cover - guards against a future event type slipping in
        raise AssertionError(f"unhandled event type {event_type!r}")


@st.composite
def _interleaved_emissions(draw):
    """Generate >= 2 models and an interleaved (model, event_type) emission list.

    Returns ``(models, emissions)`` where ``models`` is a unique list of >= 2 model
    names and ``emissions`` is a list of ``(model, event_type)`` pairs, each model
    drawn from ``models`` and each event_type from :data:`OPTIMIZER_EVENT_TYPES`.
    The list deliberately interleaves the models so their frames are physically
    intermixed on the single shared broker.
    """
    models = draw(
        st.lists(
            st.sampled_from(_MODEL_POOL),
            min_size=2,
            max_size=len(_MODEL_POOL),
            unique=True,
        )
    )
    event_types = sorted(OPTIMIZER_EVENT_TYPES)
    emissions = draw(
        st.lists(
            st.tuples(st.sampled_from(models), st.sampled_from(event_types)),
            min_size=0,
            max_size=40,
        )
    )
    return models, emissions


# ===========================================================================
# Property 19 -- per-model event streams never interleave ambiguously.
# Feature: closed-loop-prompt-optimizer, Property 19: Per-model event streams
#          never interleave ambiguously -- filtering the shared stream by
#          ``model_channel`` partitions it cleanly per model, in order, with none
#          misattributed and the total conserved.
# **Validates: Requirements 9.10, 9.11**
# ===========================================================================
@settings(max_examples=100)
@given(payload=_interleaved_emissions())
def test_property19_per_model_streams_partition_cleanly_by_channel(payload):
    """Emissions for >= 2 models, physically interleaved on ONE broker, are
    recovered exactly per model by filtering on ``model_channel``: each model's
    subsequence comes back in order, none misattributed, and the total is
    conserved.

    Feature: closed-loop-prompt-optimizer, Property 19: Per-model event streams
    never interleave ambiguously. Validates Requirements 9.10 (every optimizer
    event is stamped with the model_channel it describes) and 9.11 (a Per_Model_View
    filtering on model_channel sees only -- and all of -- its own model's events).
    Drives the real :class:`bakeoff.app.SSEBroker` (unchanged, Req 9.7) via the real
    :class:`OptimizerEventEmitter`.
    """
    models, emissions = payload
    broker = SSEBroker()
    emitter = OptimizerEventEmitter(broker)

    # Subscribe BEFORE emitting (registration is synchronous, like a live
    # Per_Model_View opening before the loops publish).
    sub = broker.open()

    # Emit the interleaved sequence; each emission gets a globally-unique marker
    # so a received frame maps back to exactly one emission (order, not just count).
    marker_to_model: dict[int, str] = {}
    intended_per_model: dict[str, list[tuple[str, int]]] = {m: [] for m in models}
    for marker, (model, event_type) in enumerate(emissions):
        marker_to_model[marker] = model
        intended_per_model[model].append((event_type, marker))
        _emit_marked(emitter, event_type, model, marker)

    # Drain everything the single subscriber received, in arrival order.
    frames: list[tuple[str, dict]] = []
    while not sub.queue.empty():
        frames.append(sub.queue.get_nowait())

    # (1) Total conserved: one frame received per emission, none lost or invented.
    assert len(frames) == len(emissions)

    # (2) Every frame carries a model_channel, and it is one of our models -- and
    #     it matches the model that emission was *intended* for (no misattribution).
    received_per_model: dict[str, list[tuple[str, int]]] = {m: [] for m in models}
    seen_markers: set[int] = set()
    for event_type, p in frames:
        assert MODEL_CHANNEL in p
        channel = p[MODEL_CHANNEL]
        assert channel in models
        assert event_type in OPTIMIZER_EVENT_TYPES
        marker = p[_MARKER_FIELD[event_type]]
        # The channel the frame arrived on is exactly the model it was emitted for.
        assert channel == marker_to_model[marker]
        # champion_scored also carries an explicit "model" key; it must agree with
        # the channel stamp (a second, independent misattribution guard).
        if event_type == EVENT_CHAMPION_SCORED:
            assert p["model"] == channel
        seen_markers.add(marker)
        received_per_model[channel].append((event_type, marker))

    # (3) Markers are conserved as a set: exactly the emitted markers came back.
    assert seen_markers == set(marker_to_model)

    # (4) Clean partition, in order: filtering by model_channel recovers exactly
    #     each model's emission subsequence, in the order it was emitted. Because
    #     the broker preserves publish order and each model's frames are a
    #     subsequence of the shared stream, the per-channel projection equals the
    #     intended per-model list element-for-element.
    for m in models:
        assert received_per_model[m] == intended_per_model[m]

    # (5) The partition is a true partition: summed per-model counts == total, so
    #     no frame landed in two channels or none.
    assert sum(len(v) for v in received_per_model.values()) == len(frames)


# ---------------------------------------------------------------------------
# Strategy for Properties 20/21: a running model set + per-model view activity.
# ---------------------------------------------------------------------------
@st.composite
def _models_and_activity(draw, *, min_models=1):
    """Draw ``(models, active, active_reps, ghosts)``.

    * ``models`` — a unique list of running models (>= ``min_models``).
    * ``active`` — ``model -> bool``: whether that running model has an active view.
    * ``active_reps`` — ``model -> int``: how many times an active model's view was
      opened (>= 1), exercising the registry's reference counting.
    * ``ghosts`` — non-running models marked active, to prove the gate ignores
      views for models it is not running (Req 1.11).
    """
    models = draw(
        st.lists(
            st.sampled_from(_MODEL_POOL),
            min_size=min_models,
            max_size=len(_MODEL_POOL),
            unique=True,
        )
    )
    active = {m: draw(st.booleans()) for m in models}
    active_reps = {m: draw(st.integers(min_value=1, max_value=3)) for m in models}
    ghosts = draw(st.lists(st.sampled_from(_GHOST_POOL), unique=True, max_size=3))
    return models, active, active_reps, ghosts


def _build_registry(models, active, active_reps, ghosts) -> ViewRegistry:
    """Build a :class:`ViewRegistry` reflecting the drawn activity.

    Active running models are opened ``active_reps`` times (reference counting);
    ghost models are opened once. Models with ``active is False`` are left with no
    open subscriptions (not viewable).
    """
    registry = ViewRegistry()
    for m in models:
        if active.get(m):
            for _ in range(active_reps.get(m, 1)):
                registry.mark_active(m)
    for g in ghosts:
        registry.mark_active(g)
    return registry


# ===========================================================================
# Property 21 -- concurrency gated on per-model visualization.
# Feature: closed-loop-prompt-optimizer, Property 21: Concurrency gated on
#          per-model visualization -- concurrent iff every running model has an
#          active Per_Model_View, else sequential.
# **Validates: Requirements 1.11**
# ===========================================================================
@settings(max_examples=100)
@given(spec=_models_and_activity(min_models=1))
def test_property21_concurrency_gated_on_per_model_visualization(spec):
    """``decide_concurrency()`` returns ``"concurrent"`` iff every running model
    has an active Per_Model_View, else ``"sequential"``; ``.viewable`` /
    ``.all_viewable`` mirror the registry, and views for non-running (ghost) models
    never affect the decision.

    Feature: closed-loop-prompt-optimizer, Property 21: Concurrency gated on
    per-model visualization. Validates Requirement 1.11 (the two per-model loops run
    concurrently iff every running model has its own active Per_Model_View; otherwise
    they run sequentially). Drives the real :class:`PerModelOrchestrator` gate over a
    real :class:`ViewRegistry`.
    """
    models, active, active_reps, ghosts = spec
    registry = _build_registry(models, active, active_reps, ghosts)

    orch = PerModelOrchestrator(
        models=models,
        backend=None,
        store=None,
        emitter=None,
        view_registry=registry,
    )

    decision = orch.decide_concurrency()
    assert isinstance(decision, ConcurrencyDecision)

    # Reference truth derived directly from the drawn activity over RUNNING models
    # (ghost activity is deliberately excluded -- it must not move the gate).
    expected_viewable = tuple(m for m in models if active.get(m))
    expected_all = len(expected_viewable) == len(models)

    # (1) The gate predicate and its recorded fields mirror the registry state.
    assert decision.models == tuple(models)
    assert decision.viewable == expected_viewable
    assert decision.all_viewable == expected_all

    # (2) Mode is concurrent iff every running model is viewable, else sequential
    #     -- and all_viewable is exactly equivalent to mode == "concurrent".
    assert decision.mode == ("concurrent" if expected_all else "sequential")
    assert decision.all_viewable == (decision.mode == "concurrent")

    # (3) The decision agrees with the registry's own predicate, model-by-model
    #     (the gate consults has_active_view for each running model, Req 1.11).
    assert decision.viewable == tuple(
        m for m in models if registry.has_active_view(m)
    )

    # (4) Purity: decide_concurrency only reads the registry, so re-evaluating it
    #     yields an equal decision (no hidden state mutated by the gate check).
    assert orch.decide_concurrency() == decision


# ---------------------------------------------------------------------------
# Strategy + helpers for Property 20 (per-model loop structure + isolation).
# ---------------------------------------------------------------------------
@st.composite
def _loop_inputs(draw):
    """Draw ``(models, outcomes, active, active_reps, ghosts)`` for the loop test.

    ``outcomes`` is ``model -> ("challenger", score)`` or ``("none", None)``: the
    per-model result the injected ``model_runner`` will produce. The activity
    fields (re-used from :func:`_models_and_activity`) randomize the visualization
    gate so the isolation/1:1 invariants are exercised under BOTH concurrent and
    sequential scheduling.
    """
    models, active, active_reps, ghosts = draw(_models_and_activity(min_models=1))
    outcomes: dict[str, tuple[str, float | None]] = {}
    for m in models:
        if draw(st.booleans()):
            outcomes[m] = (
                "challenger",
                draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)),
            )
        else:
            outcomes[m] = ("none", None)
    return models, outcomes, active, active_reps, ghosts


def _make_runner(outcomes):
    """Build an injected ``model_runner`` plus the list it records invocations into.

    The runner depends ONLY on its own ``model`` argument and that model's entry in
    ``outcomes`` -- never on any other model -- which is exactly the isolation the
    orchestrator must preserve. It awaits ``asyncio.sleep(0)`` so that, in
    concurrent mode, the per-model coroutines genuinely interleave at an await
    point (Req 1.11) while recording stays correct on the single event loop.
    """
    calls: list[str] = []

    async def runner(model: str):
        await asyncio.sleep(0)
        calls.append(model)
        kind, score = outcomes[model]
        if kind == "challenger":
            return {"model": model, "usable": True, "challenger_triad": score}
        return {"model": model, "usable": False, "no_usable_challenger": True}

    return runner, calls


def _run_models(models, outcomes, active, active_reps, ghosts):
    """Run a fresh orchestrator over ``models`` and return ``(results, calls, decision)``.

    A fresh :class:`ViewRegistry`, runner, and orchestrator are built each call so
    runs are independent (no shared recorder). Activity is restricted to the models
    actually present in this run.
    """
    present_active = {m: active.get(m, False) for m in models}
    present_reps = {m: active_reps.get(m, 1) for m in models}
    registry = _build_registry(models, present_active, present_reps, ghosts)
    runner, calls = _make_runner(outcomes)
    orch = PerModelOrchestrator(
        models=models,
        backend=None,
        store=None,
        emitter=None,
        view_registry=registry,
        model_runner=runner,
    )
    results = asyncio.run(orch.run())
    return results, calls, orch.last_decision


def _assert_one_outcome(result, model):
    """Assert ``result`` is exactly one outcome for ``model``: a challenger XOR a
    recorded 'no usable challenger' (never both, never neither)."""
    assert result["model"] == model
    is_challenger = result.get("usable") is True and "challenger_triad" in result
    is_none = result.get("usable") is False and result.get("no_usable_challenger") is True
    assert is_challenger != is_none  # exactly one of the two outcome shapes


# ===========================================================================
# Property 20 -- per-model loop structure and state isolation.
# Feature: closed-loop-prompt-optimizer, Property 20: Per-model loop structure and
#          state isolation -- each iteration produces exactly one challenger (or one
#          recorded "no usable challenger") for one model, and a model's
#          champion/convergence state depends only on its own outcomes.
# **Validates: Requirements 1.1, 1.9**
# ===========================================================================
@settings(max_examples=100, deadline=None)
@given(inp=_loop_inputs(), data=st.data())
def test_property20_per_model_loop_structure_and_state_isolation(inp, data):
    """The orchestrator runs each model exactly once, maps results 1:1 to models
    with exactly one outcome each, and a model's result is unchanged when other
    models are permuted or removed -- regardless of concurrent/sequential mode.

    Feature: closed-loop-prompt-optimizer, Property 20: Per-model loop structure and
    state isolation. Validates Requirements 1.1 (each model runs its own
    champion/challenger study) and 1.9 (a model's champion/convergence state depends
    only on its own outcomes, never another model's). Drives the real async
    ``PerModelOrchestrator.run()`` via ``asyncio.run`` with an injected
    ``model_runner`` that records per-model invocations.
    """
    models, outcomes, active, active_reps, ghosts = inp

    # --- Baseline run over the full model set -----------------------------------
    results_full, calls_full, decision = _run_models(
        models, outcomes, active, active_reps, ghosts
    )

    # (1) Each model is run exactly once -- no model skipped, none run twice.
    assert Counter(calls_full) == Counter(models)
    assert len(calls_full) == len(models)

    # (2) Results map 1:1 to models, in running order, each a single well-formed
    #     outcome (one challenger XOR one recorded "no usable challenger").
    assert list(results_full.keys()) == list(models)
    assert set(results_full) == set(models)
    for m in models:
        _assert_one_outcome(results_full[m], m)
        # The recorded outcome reflects this model's own assigned outcome only.
        kind, score = outcomes[m]
        if kind == "challenger":
            assert results_full[m] == {"model": m, "usable": True, "challenger_triad": score}
        else:
            assert results_full[m] == {"model": m, "usable": False, "no_usable_challenger": True}

    # (3) Mode is whatever the gate decided; the invariants below must hold in BOTH
    #     concurrent and sequential schedulings (decision is recorded on run).
    assert decision is not None
    assert decision.mode in ("concurrent", "sequential")

    # --- Isolation under permutation -------------------------------------------
    # Permuting the model order leaves every model's own result byte-for-byte
    # identical: one model's outcome never depends on another's (Req 1.9).
    permuted = list(data.draw(st.permutations(models)))
    results_perm, calls_perm, _ = _run_models(
        permuted, outcomes, active, active_reps, ghosts
    )
    assert Counter(calls_perm) == Counter(models)
    for m in models:
        assert results_perm[m] == results_full[m]

    # --- Isolation under removal -----------------------------------------------
    # Keep a random subset that still includes a chosen focus model, dropping the
    # others. The focus model's result is unchanged by the removal of any other
    # model (its champion/convergence state depends only on its own outcomes).
    focus = data.draw(st.sampled_from(models))
    keep = sorted(
        set(data.draw(st.lists(st.sampled_from(models), unique=True))) | {focus},
        key=models.index,
    )
    results_sub, calls_sub, _ = _run_models(
        keep, outcomes, active, active_reps, ghosts
    )
    assert Counter(calls_sub) == Counter(keep)
    for m in keep:
        assert results_sub[m] == results_full[m]


# ===========================================================================
# Tasks 7.3, 7.4, 7.5, 7.6, 10.3, 10.4, 11.3 — the remaining seven properties.
#
# These exercise the retrieval-always data flow (Properties 24/25/26), abstention
# weighting (Property 27), the author/promotion edges (Properties 5/6), and the
# inline-agent fidelity invariant (Property 23). They drive the real
# JudgeInLoopScorer / OfflineAuthorClient / PersistentSessionInlineAdapter with
# in-memory synthesized Items (zero network, no dataset file), each swept at
# >= 100 Hypothesis examples.
# ===========================================================================
import asyncio  # noqa: E402
import re as _re  # noqa: E402
from dataclasses import replace as _dc_replace  # noqa: E402

from hypothesis import HealthCheck  # noqa: E402

from bakeoff.quality.optimizer.author import (  # noqa: E402
    AuthoredChallenger,
    OfflineAuthorClient,
)
from bakeoff.quality.optimizer.backends import build_offline_backend  # noqa: E402
from bakeoff.quality.optimizer.convergence import (  # noqa: E402
    ConvergenceTracker,
    PromotionDecider,
)
from bakeoff.quality.optimizer.inline_session_adapter import (  # noqa: E402
    PersistentSessionInlineAdapter,
)
from bakeoff.quality.optimizer.judge_loop import (  # noqa: E402
    REFUSAL,
    JudgeInLoopScorer,
)
from bakeoff.quality.optimizer.retrieval import (  # noqa: E402
    MemoizingRetrievalBackend,
    RetrievalQuery,
)
from bakeoff.quality.prompts import (  # noqa: E402
    MULTI_TURN_BLOCKS,
    quality_system_instruction,
    variants_for_model,
)
from bakeoff.scoring.judge import (  # noqa: E402
    JUDGE_DIMENSIONS,
    JudgeScorer,
    make_stub_judge,
)
from bakeoff.types import (  # noqa: E402
    CohortKey,
    GoldFragment,
    Item,
    ModelResponse,
    Turn,
)

# A heavier-test settings profile for the score_prompt-driven properties: >=100
# examples, no per-example deadline (the async scorer + stub judge can take a beat),
# and tolerate the "too slow" health check since each example runs a small loop.
_LOOP_SETTINGS = settings(
    max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)

_MODEL = "haiku-4.5"  # a real config.QUALITY_MODELS key (the Haiku target)


def _run(coro):
    """Drive an awaitable to completion (no pytest-asyncio dependency)."""
    return asyncio.run(coro)


def _cohort(answerability: str) -> CohortKey:
    """A minimal valid multi-turn cohort with the given answerability."""
    return CohortKey(
        geography="g",
        proficiency="fluent",
        tone="terse",
        entry_route="slack",
        momentary_state="neutral",
        answerability=answerability,
        turn_type="multi",
    )


def _answerable_item(item_id: str, n_turns: int) -> Item:
    """An answerable multi-turn item: turn-1 GOLD, later turns carry ``wants``.

    Built entirely in memory (no dataset file); ``turn_reference`` yields a usable
    reference for every turn so the judge-input derivation never raises.
    """
    turns = [
        Turn(
            turn=1,
            user_utterance=f"{item_id} how do i request a corporate card",
            momentary_state="neutral",
            answerability="full",
        )
    ]
    for t in range(2, n_turns + 1):
        turns.append(
            Turn(
                turn=t,
                user_utterance=f"{item_id} follow up question number {t}",
                momentary_state="neutral",
                wants="Submit the limit-increase request to your manager for approval.",
            )
        )
    return Item(
        id=item_id,
        turn_type="multi",
        cohort=_cohort("full"),
        wants="how to request a corporate card",
        answerability="full",
        gold=[
            GoldFragment(
                node_id="g1",
                title="Corporate Card",
                markdown="Request a corporate card in the expense portal; it arrives in five business days.",
            )
        ],
        turns=tuple(turns),
    )


def _unanswerable_item(item_id: str) -> Item:
    """A turn-1 unanswerable item (answerability ``none``) → ABSTENTION regime."""
    return Item(
        id=item_id,
        turn_type="multi",
        cohort=_cohort("none"),
        answerability="none",
        turns=(
            Turn(
                turn=1,
                user_utterance=f"{item_id} can i expense my neighbor's dental surgery",
                momentary_state="neutral",
                answerability="none",
            ),
        ),
    )


class _ScriptedAdapter:
    """A deterministic answer adapter returning one scripted answer per turn.

    Mirrors the ``ModelAdapter`` contract the scorer uses (``name`` + async
    ``generate``); ignores the instruction (so the same scripted answers are
    produced for Champion and Challenger — the held-constant discipline). The
    answer text is chosen by an injected callable so a test can place a decline or
    a fabrication on a turn.
    """

    def __init__(self, name: str, answer_for):
        self.name = name
        self._answer_for = answer_for

    async def generate(self, item, fragments, temperature):
        answers = [self._answer_for(item, ti) for ti in range(len(item.turns))]
        return ModelResponse(
            text=answers[-1] if answers else "",
            ttft_ms=1.0,
            generation_total_ms=float(len(answers)),
            token_usage={"prompt": 0, "completion": 0, "total": 0},
            per_turn_answers=answers,
            finish_reason="stop",
            model=self.name,
        )


def _scripted_factory(answer_for):
    """Build an ``answer_adapter_factory`` returning a scripted adapter."""

    def factory(model, instruction, item_lookup):
        return _ScriptedAdapter(model, answer_for)

    return factory


class _RecordingRetrieval:
    """Wrap a retrieval backend, recording every ``retrieve`` call + returned ids.

    Records the ``(item_id, turn)`` of every call (proving retrieval is invoked per
    turn, Property 24) and the returned fragment-id tuple per ``(item_id, turn)``
    (so it can be compared to what the judge saw, Property 26). Read-only —
    delegates verbatim to the wrapped backend.
    """

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[tuple[str, int]] = []
        self.returned_ids: dict[tuple[str, int], tuple[str, ...]] = {}

    @property
    def name(self) -> str:
        return self._inner.name

    async def retrieve(self, q: RetrievalQuery):
        self.calls.append((q.item_id, q.turn))
        frags = await self._inner.retrieve(q)
        self.returned_ids[(q.item_id, q.turn)] = tuple(str(f.get("id", "")) for f in frags)
        return frags


class _VaryingRetrieval:
    """An inner backend whose output changes on EVERY call (zero network).

    Wrapped in :class:`MemoizingRetrievalBackend`, it proves the held-constant
    guarantee (Property 25): if the memo layer ever re-invoked the inner backend
    for the same ``(turn-query)``, the fragments would differ. Identical fragments
    for Champion and Challenger therefore prove the result was served from cache
    (the instruction never enters the key).
    """

    name = "varying"

    def __init__(self):
        self.calls = 0

    async def retrieve(self, q: RetrievalQuery):
        self.calls += 1
        n = self.calls
        return [
            {
                "id": f"frag-{q.item_id}-t{q.turn}-call{n}",
                "text": f"call {n} for {q.item_id} turn {q.turn}",
                "metadata": {"call": n},
                "confidence": round(1.0 / (n + 1), 4),
            }
        ]


class _RecordingJudge:
    """Wrap a real ``JudgeScorer`` and record the fragment ids each call grounded on.

    Proves Property 26: the fragments threaded into the judge as faithfulness
    evidence are the SAME ones retrieval produced. Delegates ``score_detailed`` to
    the wrapped judge so the full aggregation path still runs.
    """

    def __init__(self, inner: JudgeScorer):
        self._inner = inner
        self.fragment_ids_seen: list[tuple[str, ...]] = []

    def score_detailed(self, answer, **kwargs):
        frags = kwargs.get("fragments") or []
        self.fragment_ids_seen.append(tuple(str(f.get("id", "")) for f in frags))
        return self._inner.score_detailed(answer, **kwargs)


# ===========================================================================
# Property 24 — retrieval is invoked on every turn (Task 7.3).
# Feature: closed-loop-prompt-optimizer, Property 24: Retrieval is invoked on
#          every turn — RetrievalBackend.retrieve is called for every turn of
#          every conversation before the model answers.
# **Validates: Requirements 13.1, 13.2**
# ===========================================================================
@_LOOP_SETTINGS
@given(
    turn_counts=st.lists(st.integers(min_value=1, max_value=3), min_size=1, max_size=3),
    reps=st.integers(min_value=1, max_value=2),
)
def test_property24_retrieval_invoked_on_every_turn(turn_counts, reps):
    """``JudgeInLoopScorer`` calls retrieval for every ``(item, turn)`` of every rep.

    Feature: closed-loop-prompt-optimizer, Property 24: Retrieval is invoked on
    every turn. Validates Requirements 13.1/13.2 (the quality path is
    retrieval-always: every turn of every conversation retrieves before the model
    answers). A recording shim over the held-constant fake backend captures every
    retrieve call; the recorded ``(item_id, turn)`` set must cover every turn of
    every item — twice per rep, once on the model pass (the model is fed the turn's
    fragments) and once on the judge pass (the judge grounds on them), the memo
    keeping the two byte-identical.
    """
    items = [_answerable_item(f"it-{i}", n) for i, n in enumerate(turn_counts)]
    backend = build_offline_backend()
    recorder = _RecordingRetrieval(backend.retrieval)
    backend = _dc_replace(backend, retrieval=recorder)

    scorer = JudgeInLoopScorer(backend, reps=reps)
    score = _run(
        scorer.score_prompt(
            model=_MODEL, instruction="sys", items=items, prompt_role="champion",
            max_concurrency=1,
        )
    )

    expected_pairs = {(it.item_id, t) for it in items for t in range(1, len(it.turns) + 1)}
    # Every (item, turn) was retrieved (retrieval-always). Retrieval is now invoked TWICE per
    # (item, turn, rep): once on the MODEL pass (``_conversation_fragments`` feeds the model
    # the turn's fragments) and once on the JUDGE pass (``_judge_turn`` grounds the judge on
    # them). Both hit the held-constant memo, so they return byte-identical fragments (the
    # verdict-parity check below proves the model and judge saw the same set).
    assert set(recorder.calls) == expected_pairs
    n_turns_total = sum(len(it.turns) for it in items)
    assert len(recorder.calls) == 2 * reps * n_turns_total
    # The split is exactly even — every (item, turn) pair is retrieved twice per rep.
    from collections import Counter as _Counter

    assert all(count == 2 * reps for count in _Counter(recorder.calls).values())
    # The scorer produced a verdict for every (item, turn, rep), each carrying the
    # grounding ids retrieval returned for that turn — so retrieval fed the answer path.
    assert len(score.verdicts) == reps * sum(len(it.turns) for it in items)
    for v in score.verdicts:
        assert v.grounding_fragment_ids == recorder.returned_ids[(v.item_id, v.turn)]


# ===========================================================================
# Property 25 — champion & challenger receive identical fragments per turn (Task 7.4).
# Feature: closed-loop-prompt-optimizer, Property 25: Champion and challenger
#          receive identical fragments for the same turn — the memo key excludes
#          prompt role + instruction, so varying the instruction never changes
#          the fragments.
# **Validates: Requirements 12.4, 13.3**
# ===========================================================================
@_LOOP_SETTINGS
@given(turn_counts=st.lists(st.integers(min_value=1, max_value=3), min_size=1, max_size=3))
def test_property25_champion_and_challenger_get_identical_fragments(turn_counts):
    """Scoring two different instructions over the same items yields byte-identical
    per-turn fragments — the only varied element is the system instruction.

    Feature: closed-loop-prompt-optimizer, Property 25: Champion and challenger
    receive identical fragments for the same turn. Validates Requirements 12.4/13.3
    (retrieval is held constant per ``(turn-query)``). An inner backend that returns
    DIFFERENT fragments on every inner call is wrapped in
    :class:`MemoizingRetrievalBackend`; if the instruction ever leaked into the key
    the Challenger would see different fragments, so identical per-turn grounding
    ids prove the held-constant guarantee.
    """
    items = [_answerable_item(f"it-{i}", n) for i, n in enumerate(turn_counts)]
    inner = _VaryingRetrieval()
    backend = build_offline_backend()
    backend = _dc_replace(backend, retrieval=MemoizingRetrievalBackend(inner))
    scorer = JudgeInLoopScorer(backend, reps=1)

    champ = _run(
        scorer.score_prompt(
            model=_MODEL, instruction="CHAMPION instruction text",
            items=items, prompt_role="champion", max_concurrency=1,
        )
    )
    chall = _run(
        scorer.score_prompt(
            model=_MODEL, instruction="CHALLENGER instruction — different text",
            items=items, prompt_role="challenger", max_concurrency=1,
        )
    )

    champ_by_turn = {(v.item_id, v.turn): v.grounding_fragment_ids for v in champ.verdicts}
    chall_by_turn = {(v.item_id, v.turn): v.grounding_fragment_ids for v in chall.verdicts}
    assert champ_by_turn.keys() == chall_by_turn.keys()
    for key, ids in champ_by_turn.items():
        assert chall_by_turn[key] == ids  # identical fragments for the same turn
    # The inner varying backend was hit exactly once per distinct (turn-query) —
    # the Challenger's scoring was served entirely from the held-constant cache.
    assert inner.calls == sum(len(it.turns) for it in items)


# ===========================================================================
# Property 26 — the judge grounds on the same fragments the model received (Task 7.5).
# Feature: closed-loop-prompt-optimizer, Property 26: The judge grounds on the
#          same fragments the model received.
# **Validates: Requirements 13.5, 13.7**
# ===========================================================================
@_LOOP_SETTINGS
@given(turn_counts=st.lists(st.integers(min_value=1, max_value=3), min_size=1, max_size=2))
def test_property26_judge_grounds_on_same_fragments_as_model(turn_counts):
    """The fragment ids handed to the judge as faithfulness evidence equal those
    retrieval produced for the turn — and the verdict records that same set.

    Feature: closed-loop-prompt-optimizer, Property 26: The judge grounds on the
    same fragments the model received. Validates Requirements 13.5/13.7 (the same
    held-constant fragments reach both the model and the judge). A recording
    retrieval shim and a recording judge wrapper capture, respectively, what
    retrieval returned per turn and what the judge was grounded on; the two
    multisets must match, and each verdict's ``grounding_fragment_ids`` equals the
    retrieval set for its turn.
    """
    items = [_answerable_item(f"it-{i}", n) for i, n in enumerate(turn_counts)]
    backend = build_offline_backend()
    recorder = _RecordingRetrieval(backend.retrieval)
    rec_judge = _RecordingJudge(JudgeScorer(backend=make_stub_judge(), k=1, disk_cache=False))
    backend = _dc_replace(backend, retrieval=recorder, judge_scorer=rec_judge)

    scorer = JudgeInLoopScorer(backend, reps=1)
    score = _run(
        scorer.score_prompt(
            model=_MODEL, instruction="sys", items=items, prompt_role="champion",
            max_concurrency=1,
        )
    )

    # Each verdict grounds on exactly the fragments retrieval produced for its turn.
    for v in score.verdicts:
        assert v.grounding_fragment_ids == recorder.returned_ids[(v.item_id, v.turn)]
    # The judge saw exactly the fragment-id sets retrieval produced (k=1 → one judge
    # call per turn), as a multiset — no turn was grounded on a different set.
    from collections import Counter

    assert Counter(rec_judge.fragment_ids_seen) == Counter(recorder.returned_ids.values())


# ===========================================================================
# Property 27 — abstention monotonicity (Task 7.6).
# Feature: closed-loop-prompt-optimizer, Property 27: Correct abstention scores at
#          least as high as an unsupported answer, and the gap is non-decreasing
#          in the abstention weight.
# **Validates: Requirements 14.1, 14.2, 14.3, 14.4, 14.5**
# ===========================================================================
class _FixedTriadJudge:
    """A judge that returns the SAME triad regardless of the answer (network-free).

    Isolates the abstention weighting: with the triad held fixed, the only thing
    that moves the per-turn ``overall`` between a correct decline and an
    answer-when-unsure is the abstention weight, so the gap is exactly ``w``.
    """

    def __init__(self, faithfulness, correctness, completeness):
        import types as _types

        self._scores = _types.SimpleNamespace(
            faithfulness=faithfulness, correctness=correctness, completeness=completeness
        )

    def score_detailed(self, answer, **kwargs):
        return self._scores, {}


# A grounded decline (matches REFUSAL) vs a confident fabrication (does not).
_DECLINE = "I don't have that information in the reference material."
_FABRICATION = "Yes, you can do that; here is the full process to follow."


# Faithfulness is held AT/ABOVE the confident-wrong floor here on purpose. The
# confident-wrong hammer (judge_loop step 3b) is a SEPARATE gate: when faithfulness
# < QUALITY_OPT_FAITHFULNESS_FLOOR it caps the turn's ``overall`` at the faithfulness
# value, which would confound the clean abstention-weighting identity this property
# isolates (gap == w). A genuine grounded decline asserts nothing, so its faithfulness is
# high and the gate never fires — exactly the regime this property lives in; the gate's own
# behavior is exercised elsewhere. So we draw faithfulness from [FLOOR, 1].
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    faithfulness=st.floats(
        min_value=config.QUALITY_OPT_FAITHFULNESS_FLOOR, max_value=1.0,
        allow_nan=False, allow_infinity=False,
    ),
    correctness=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    completeness=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    weights=st.lists(
        st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
        min_size=2, max_size=5,
    ),
)
def test_property27_correct_abstention_scores_at_least_as_high(
    faithfulness, correctness, completeness, weights
):
    """On an unanswerable turn, a correct decline scores ``>=`` answering-when-unsure,
    and the gap is non-decreasing in the abstention weight.

    Feature: closed-loop-prompt-optimizer, Property 27: Correct abstention scores at
    least as high as an unsupported answer. Validates Requirements 14.1–14.5
    (abstention is a first-class, heavily-weighted behavior; correct declines are
    rewarded, answering-when-unsure is penalized). With the judge triad held fixed,
    the per-turn ``overall`` gap between a decline and a fabrication equals the
    abstention weight ``w`` — non-negative and non-decreasing in ``w``.
    """
    item = _unanswerable_item("unans-0")
    triad_mean = (faithfulness + correctness + completeness) / 3.0
    fixed_judge = _FixedTriadJudge(faithfulness, correctness, completeness)

    def overall_for(answer_text, w):
        backend = build_offline_backend()
        backend = _dc_replace(
            backend,
            judge_scorer=fixed_judge,
            answer_adapter_factory=_scripted_factory(lambda it, ti: answer_text),
        )
        scorer = JudgeInLoopScorer(backend, reps=1, abstention_weight=w)
        score = _run(
            scorer.score_prompt(
                model=_MODEL, instruction="sys", items=[item], prompt_role="champion",
                max_concurrency=1,
            )
        )
        return score.verdicts[0]

    gaps = []
    for w in weights:
        decline = overall_for(_DECLINE, w)
        unsure = overall_for(_FABRICATION, w)
        # Sanity: the detectors classified the two answers as intended.
        assert bool(REFUSAL.search(_DECLINE)) is True
        assert decline.abstention_correct is True
        assert unsure.answered_when_unsure is True
        # Correct decline scores at least as high as answering-when-unsure (Req 14.1/14.5)...
        assert decline.overall + 1e-9 >= unsure.overall
        # ...and the gap is exactly the abstention weight w (T + w(1-T) - (1-w)T == w).
        gap = decline.overall - unsure.overall
        assert math.isclose(gap, w, rel_tol=1e-6, abs_tol=1e-6)
        gaps.append((w, gap))

    # The gap is non-decreasing in the weight (sort by w, gaps follow).
    gaps.sort(key=lambda pair: pair[0])
    for earlier, later in zip(gaps, gaps[1:]):
        assert later[1] + 1e-9 >= earlier[1]


# ===========================================================================
# Property 5 — a non-usable challenger is a non-improving iteration (Task 10.3).
# Feature: closed-loop-prompt-optimizer, Property 5: A non-usable challenger is a
#          non-improving iteration — empty / whitespace / identical-to-champion →
#          not usable, not promoted, counter increments.
# **Validates: Requirements 3.5**
# ===========================================================================
@settings(max_examples=100)
@given(
    champion=_free_text,
    champion_score=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    challenger_score=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    threshold=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    degenerate=st.sampled_from(["empty", "whitespace", "identical"]),
    stop_limit=st.integers(min_value=1, max_value=5),
)
def test_property5_non_usable_challenger_is_non_improving(
    champion, champion_score, challenger_score, threshold, degenerate, stop_limit
):
    """An empty/whitespace/identical challenger is ``usable=False``, is never promoted
    regardless of scores, and increments the convergence counter.

    Feature: closed-loop-prompt-optimizer, Property 5: A non-usable challenger is a
    non-improving iteration. Validates Requirement 3.5 (a degenerate Author output
    is a normal non-improving iteration, not an error or a promotion).
    """
    if degenerate == "empty":
        text = ""
    elif degenerate == "whitespace":
        text = "   \n\t  "
    else:
        text = champion

    authored = AuthoredChallenger.build(
        instruction=text, rationale="r", author_model="m", raw={}, champion_instruction=champion
    )
    assert authored.usable is False

    decider = PromotionDecider()
    # Never promoted, regardless of the scores or threshold (the usability gate).
    assert decider.decide(champion_score, challenger_score, threshold, usable=authored.usable) is False

    # Recording the non-promotion increments the trailing-reject run, and after
    # stop_limit consecutive non-improving iterations the tracker converges.
    tracker = ConvergenceTracker(stop_limit=stop_limit)
    for i in range(stop_limit):
        assert tracker.consecutive_non_improving == i
        tracker.record(promoted=False, iteration_index=i)
    assert tracker.consecutive_non_improving == stop_limit
    assert tracker.should_stop is True


@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(seed_tail=_free_text)
def test_property5_offline_author_terminal_iteration_is_non_usable(seed_tail):
    """When every lever marker is already present, ``OfflineAuthorClient`` returns the
    champion unchanged → ``usable=False`` (the terminal non-improving iteration).

    Feature: closed-loop-prompt-optimizer, Property 5 (real-author edge): a champion
    that already carries every lever can produce no usable challenger, which is how
    the offline loop converges (Req 3.5).
    """
    champion = (
        f"Seed instruction {seed_tail}.\n\n" + "\n\n".join(MULTI_TURN_BLOCKS.values())
    )
    authored = _run(
        OfflineAuthorClient().author(
            target_model="sonnet-4.6", champion_instruction=champion, failures=[]
        )
    )
    assert authored.instruction == champion
    assert authored.usable is False


# ===========================================================================
# Property 6 — challengers are authored text, never menu selections (Task 10.4).
# Feature: closed-loop-prompt-optimizer, Property 6: Challengers are authored
#          text, never menu selections — every iteration >= 1 challenger is
#          authored and != any variants_for_model member.
# **Validates: Requirements 1.8, 3.2, 11.4**
# ===========================================================================
def _menu_instructions() -> set[str]:
    """Every instruction reachable from the fixed five-variant menu (both models)."""
    menu: set[str] = set()
    for model_key, spec in config.QUALITY_MODELS.items():
        for variant in variants_for_model(model_key):
            menu.add(
                quality_system_instruction(
                    family=str(spec["family"]),
                    thinking_enabled=bool(spec["thinking"]),
                    variant=variant,
                )
            )
            if variant.multi_turn_block:
                menu.add(variant.multi_turn_block)
    return menu


# A seed alphabet with no XML '<' so a generated seed carries no lever marker, and
# is distinct from any family-base menu instruction.
_seed_text = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=8, max_size=24
)


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(seed_tail=_seed_text, iterations=st.integers(min_value=1, max_value=4))
def test_property6_challengers_are_authored_not_menu_selections(seed_tail, iterations):
    """Iterating the offline author from a custom seed produces authored instructions
    that are never equal to any fixed-menu member.

    Feature: closed-loop-prompt-optimizer, Property 6: Challengers are authored text,
    never menu selections. Validates Requirements 1.8/3.2/11.4 (the Author authors
    new instruction text; the fixed five-variant menu is used at most for the
    iteration-0 seed, never as the iteration mechanism).
    """
    author = OfflineAuthorClient()
    menu = _menu_instructions()
    champion = f"custom seed instruction {seed_tail} for the faq assistant"
    # The seed itself is a custom instruction, not a menu pick.
    assert champion not in menu

    for _ in range(iterations):
        # An answering-when-unsure failure mix so the author keeps adding levers.
        failures = [
            _verdict(
                item_id="c0",
                rep=0,
                turn=1,
                overall=0.1,
                answered_when_unsure=True,
                evidence={"span": "e"},
                grounding_fragment_ids=("f1",),
            )
        ]
        authored = _run(
            author.author(
                target_model="sonnet-4.6", champion_instruction=champion, failures=failures
            )
        )
        if not authored.usable:
            break  # terminal iteration (all levers present) — no usable challenger
        # The authored challenger is new text built from the champion, never a menu member.
        assert authored.instruction != champion
        assert authored.instruction.startswith(champion)
        assert authored.instruction not in menu
        champion = authored.instruction


# ===========================================================================
# Property 23 — inline orchestration fidelity invariant (Task 11.3).
# Feature: closed-loop-prompt-optimizer, Property 23: Inline orchestration prompt
#          contains only our instruction/question/inline-fragments.
# **Validates: Requirements 3.6, 13.4, 13.9**
# ===========================================================================
_INLINE_NAME = "claude-haiku-4.5-opt"
_INLINE_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
_ORCHESTRATION_MARKERS = (
    "actionGroup",
    "action group",
    "function_call",
    "<tools>",
    "Thought:",
    "Action:",
    "Observation:",
    "you have access to",
)


class _RecordingAgentClient:
    """Network-free ``bedrock-agent-runtime`` stand-in (records every invoke call)."""

    def __init__(self):
        import copy as _copy

        self._copy = _copy
        self.calls: list[dict] = []

    def invoke_inline_agent(self, **kwargs):
        self.calls.append(self._copy.deepcopy(kwargs))

        def _gen():
            yield {"chunk": {"bytes": b"per the reference material, here is the answer."}}

        return {"completion": _gen()}


async def _inline_instant_sleep(_s):
    return None


# Marker-free generation: pure [a-z0-9], no spaces / '<' / '_' / ':' / capitals, so
# no orchestration marker can ever appear in the generated parts.
_alnum = st.text(
    alphabet=st.characters(
        whitelist_categories=(), whitelist_characters="abcdefghijklmnopqrstuvwxyz0123456789"
    ),
    min_size=3,
    max_size=12,
)
_alnum_long = st.text(
    alphabet=st.characters(
        whitelist_categories=(), whitelist_characters="abcdefghijklmnopqrstuvwxyz0123456789"
    ),
    min_size=40,
    max_size=80,
)


@st.composite
def _inline_case(draw):
    """Generate a marker-free (instruction, item, fragments) inline-adapter case."""
    n_turns = draw(st.integers(min_value=1, max_value=3))
    instruction = draw(_alnum_long)
    turns = tuple(
        Turn(turn=i + 1, user_utterance=draw(_alnum), momentary_state="neutral")
        for i in range(n_turns)
    )
    item = Item(
        id=draw(_alnum),
        turn_type="multi",
        cohort=_cohort("full"),
        query=turns[0].user_utterance,
        answerability="full",
        turns=turns,
    )
    n_frags = draw(st.integers(min_value=1, max_value=3))
    fragments = [
        {"id": draw(_alnum), "text": draw(_alnum), "metadata": {}} for _ in range(n_frags)
    ]
    return instruction, item, fragments


def _inline_rendered_ids(context_text: str) -> tuple[str, ...]:
    return tuple(_re.findall(r"\(id=([^)]+)\)", context_text))


def _inline_context(call: dict) -> str:
    """The assembled-context value the model sees via the per-turn promptSessionAttributes."""
    state = call.get("inlineSessionState") or {}
    return (state.get("promptSessionAttributes") or {}).get("retrieved_context", "")


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(case=_inline_case())
def test_property23_inline_orchestration_fidelity_invariant(case):
    """Over arbitrary items/instructions/fragments, the inline request is OVERRIDDEN
    minimal-template, one-invoke-per-turn under one stable sessionId, with fragments on
    the per-turn promptSessionAttributes channel (never the question), grounding parity,
    and no orchestration markers.

    Feature: closed-loop-prompt-optimizer, Property 23: Inline orchestration prompt
    contains only our instruction/bare-question plus fragments via the per-turn attribute
    channel. Validates Requirements 3.6/13.4/13.9 (the no-noise fidelity invariant holds
    for every input, not just the one fixture).
    """
    instruction, item, fragments = case
    client = _RecordingAgentClient()
    adapter = PersistentSessionInlineAdapter(
        _INLINE_NAME,
        _INLINE_MODEL_ID,
        instruction_override=instruction,
        send_fragments=True,
        history_mode="server",
        client_factory=lambda: client,
        sleep=_inline_instant_sleep,
    )
    _run(adapter.generate(item, fragments, 0.2))

    # One invoke per turn under one stable sessionId matching the default scheme.
    assert len(client.calls) == len(item.turns)
    session_ids = {c["sessionId"] for c in client.calls}
    assert session_ids == {f"opt-{_INLINE_NAME}-{item.item_id}-0"}

    grounding_ids = tuple(str(f.get("id", "")) for f in fragments)
    for turn_index, call in enumerate(client.calls):
        poc = call["promptOverrideConfiguration"]["promptConfigurations"][0]
        assert poc["promptType"] == "ORCHESTRATION"
        assert poc["promptCreationMode"] == "OVERRIDDEN"
        assert poc["basePromptTemplate"] == config.QUALITY_OPT_INLINE_TEMPLATE
        assert "$prompt_session_attributes$" in poc["basePromptTemplate"]
        # No action groups / knowledge bases; session-scoped attribute channel never set.
        assert not call.get("actionGroups")
        assert not call.get("knowledgeBases")
        assert "sessionAttributes" not in call
        # Our instruction is the only system text; the question is the BARE utterance (so
        # nothing fragment-sized is persisted into the conversation history). Asserted as
        # exact equality rather than "id not in inputText" — the generator draws ids and
        # utterances from the same alphabet, so a coincidental string collision is not a
        # fragment leak; equality is the true bare-question invariant.
        assert call["instruction"].strip() == instruction
        assert call["inputText"] == item.turns[turn_index].user_utterance
        # Fragments ride the per-turn promptSessionAttributes channel instead.
        context_text = _inline_context(call)
        for frag in fragments:
            assert frag["id"] in context_text
        # Grounding parity: ids handed to the model == the judge's grounding set.
        assert _inline_rendered_ids(context_text) == grounding_ids
        # No orchestration marker anywhere in the serialized request.
        blob = json.dumps(call, ensure_ascii=False)
        for marker in _ORCHESTRATION_MARKERS:
            assert marker not in blob
