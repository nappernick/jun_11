"""
Tests for :mod:`bakeoff.calibration` — judge↔human agreement (Task 15, Req 14.4/13.2).

All OFFLINE: the judge is a :class:`bakeoff.scoring.judge.JudgeScorer` wrapping the
deterministic :class:`~bakeoff.scoring.judge.StubJudge`, so calibration runs with
**zero Bedrock calls**. The fixture
``bakeoff/tests/fixtures/calibration_set.jsonl`` is hand-built so the stub judge's
content-derived scores have a *known* agreement structure against the human labels:

* **faithfulness / correctness** — the answers are a descending ladder of how much
  gold-fragment text they contain, and the human labels descend in lockstep, so the
  stub (whose faithfulness/correctness scale with grounded fraction) tracks the
  humans monotonically → **high** Spearman ρ → NOT flagged.
* **tone** — the human labels are arranged *opposite* to the stub's tone behavior
  (the stub rewards grounded answers; the humans rate the ungrounded answer's tone
  highest) → strongly **negative** ρ → flagged low-agreement.
* **clarity** — every human clarity label is identical (0.7) → ρ is **undefined**
  (a constant rater has zero rank variance) → reported as ``None`` and flagged
  low-agreement (we cannot establish that the judge tracks humans there).

The suite asserts agreement is computed and surfaced, low-agreement dimensions are
flagged (and can soften/exclude composite weights), and the whole thing is
**reporting-only** — poor/undefined agreement never raises and never gates.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from bakeoff.calibration import (
    AGREEMENT_METRIC,
    DEFAULT_AGREEMENT_THRESHOLD,
    CalibrationRecord,
    CalibrationReport,
    load_calibration_set,
    score_calibration_set,
    spearman_rho,
)
from bakeoff.scoring.judge import JUDGE_DIMENSIONS, JudgeScorer, make_stub_judge

FIXTURE = Path(__file__).parent / "fixtures" / "calibration_set.jsonl"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def _stub_judge() -> JudgeScorer:
    """A fully-offline judge (StubJudge backend, no disk cache, no network)."""
    return JudgeScorer(backend=make_stub_judge(), disk_cache=False)


@pytest.fixture
def report() -> CalibrationReport:
    records = load_calibration_set(FIXTURE)
    return score_calibration_set(records, _stub_judge())


# ===========================================================================
# spearman_rho — the documented agreement metric
# ===========================================================================
def test_spearman_perfect_monotonic_is_one():
    assert spearman_rho([0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]) == pytest.approx(1.0)


def test_spearman_reversed_is_minus_one():
    assert spearman_rho([0.1, 0.2, 0.3, 0.4], [0.9, 0.8, 0.7, 0.6]) == pytest.approx(-1.0)


def test_spearman_is_rank_based_not_linear():
    # A monotonic-but-nonlinear judge still scores perfect agreement (the point of
    # choosing Spearman over Pearson for graded judge↔human scores).
    assert spearman_rho([1.0, 2.0, 3.0, 4.0], [1.0, 4.0, 9.0, 16.0]) == pytest.approx(1.0)


def test_spearman_undefined_when_constant_or_too_short():
    assert spearman_rho([0.5, 0.5, 0.5], [0.1, 0.2, 0.3]) is None  # constant rater
    assert spearman_rho([0.5], [0.5]) is None                       # < 2 pairs


def test_spearman_length_mismatch_raises():
    with pytest.raises(ValueError):
        spearman_rho([0.1, 0.2], [0.3])


# ===========================================================================
# Loading the calibration set
# ===========================================================================
def test_load_calibration_set_parses_fixture():
    records = load_calibration_set(FIXTURE)
    assert len(records) == 5
    assert all(isinstance(r, CalibrationRecord) for r in records)
    # human scores already in [0, 1] under the default "unit" scale.
    assert all(0.0 <= v <= 1.0 for r in records for v in r.human_scores.values())
    assert records[0].item_id == "cal-A"


def test_load_calibration_set_normalizes_1_to_5_scale():
    rec = CalibrationRecord  # noqa: F841 - keep import obvious
    import json
    import tempfile

    line = json.dumps(
        {"answer": "x", "human_scores": {"faithfulness": 5, "correctness": 1}}
    )
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
        fh.write(line + "\n")
        path = fh.name
    records = load_calibration_set(path, human_scale="1-5")
    # (5-1)/4 == 1.0 ; (1-1)/4 == 0.0
    assert records[0].human_scores["faithfulness"] == pytest.approx(1.0)
    assert records[0].human_scores["correctness"] == pytest.approx(0.0)


def test_load_calibration_set_rejects_unknown_scale():
    with pytest.raises(ValueError):
        load_calibration_set(FIXTURE, human_scale="bananas")


# ===========================================================================
# score_calibration_set — agreement computed AND surfaced (Req 14.4)
# ===========================================================================
def test_agreement_is_computed_and_surfaced(report):
    assert report.agreement_metric == AGREEMENT_METRIC == "spearman_rho"
    assert report.n_items == 5
    assert report.threshold == DEFAULT_AGREEMENT_THRESHOLD
    # Only the dimensions a human actually graded are reported.
    assert set(report.per_dimension) == {"faithfulness", "correctness", "completeness"}
    # Every reported dimension carries the structured agreement fields.
    for dim, agreement in report.per_dimension.items():
        assert agreement.dimension == dim
        assert agreement.n == 5
        assert 0.0 <= agreement.mae <= 1.0


def test_high_agreement_dimensions_track_humans(report):
    # faithfulness: the stub grades by grounded-fraction and the human ladder
    # descends in lockstep -> high Spearman ρ, NOT flagged.
    assert report.per_dimension["faithfulness"].agreement == pytest.approx(1.0)
    assert "faithfulness" not in report.low_agreement_dimensions
    assert "faithfulness" in report.defensible_dimensions


def test_low_agreement_dimensions_are_flagged(report):
    # correctness is arranged OPPOSITE to the stub (human labels ascend while the
    # stub descends with grounded fraction) -> negative ρ -> flagged.
    assert report.per_dimension["correctness"].agreement < 0
    assert "correctness" in report.low_agreement_dimensions
    # completeness has a constant human label -> undefined ρ -> reported None + flagged.
    assert report.per_dimension["completeness"].agreement is None
    assert report.per_dimension["completeness"].low_agreement is True
    assert "completeness" in report.low_agreement_dimensions


def test_reporting_only_never_raises_on_poor_agreement():
    # A calibration set where the judge disagrees with humans on EVERY dimension
    # still produces a report (no exception, no gate) — Req 14.4.
    records = load_calibration_set(FIXTURE)
    report = score_calibration_set(records, _stub_judge(), threshold=0.99)
    assert isinstance(report, CalibrationReport)
    # With a near-perfect threshold even the strong dimensions may flag, but the
    # call still returns a report rather than raising.
    assert report.n_items == 5


def test_footer_omits_undefined_dimensions(report):
    footer = report.agreement_for_footer()
    # Only dimensions with a DEFINED ρ ride in the provenance footer (Req 11.7);
    # completeness (undefined) is honestly omitted rather than fabricated.
    assert "completeness" not in footer
    assert footer["faithfulness"] == pytest.approx(1.0)
    assert all(isinstance(v, float) for v in footer.values())


# ===========================================================================
# adjusted_weights — soften/exclude low-agreement dims (Req 13.2)
# ===========================================================================
def test_adjusted_weights_soften_halves_low_agreement_dims(report):
    weights = {"faithfulness": 0.4, "correctness": 0.2, "grounding": 0.4}
    adjusted = report.adjusted_weights(weights, mode="soften", soften_factor=0.5)
    # correctness is low-agreement -> softened; faithfulness defensible -> untouched;
    # grounding is not a judge dimension -> untouched.
    assert adjusted["correctness"] == pytest.approx(0.1)
    assert adjusted["faithfulness"] == pytest.approx(0.4)
    assert adjusted["grounding"] == pytest.approx(0.4)


def test_adjusted_weights_exclude_zeroes_low_agreement_dims(report):
    weights = {"faithfulness": 0.4, "correctness": 0.2, "completeness": 0.1}
    adjusted = report.adjusted_weights(weights, mode="exclude")
    assert adjusted["correctness"] == 0.0    # flagged low -> excluded
    assert adjusted["completeness"] == 0.0   # undefined -> excluded
    assert adjusted["faithfulness"] == pytest.approx(0.4)


def test_adjusted_weights_rejects_unknown_mode(report):
    with pytest.raises(ValueError):
        report.adjusted_weights({"correctness": 0.2}, mode="obliterate")


# ===========================================================================
# Judge model id is surfaced on the report (provenance)
# ===========================================================================
def test_report_carries_judge_model(report):
    # The stub-backed JudgeScorer still reports the fixed judge model id.
    assert isinstance(report.judge_model, str) and report.judge_model


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
