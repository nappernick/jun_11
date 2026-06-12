"""Tests for bakeoff.metrics — hand-checked tiny cases + sample data."""
from __future__ import annotations

import math
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from bakeoff.contract import AbstainPoint, ScoredRow
from bakeoff.metrics import (
    ndcg_at_k, recall_at_k, mrr_at_k,
    bootstrap_ci, paired_bootstrap,
    abstention_curve, operating_point, aggregate,
)


# ---------------------------------------------------------------------------
# nDCG hand-checked cases
# ---------------------------------------------------------------------------

class TestNdcg:
    def test_perfect_ranking(self):
        # [1,1,0,0] — ideal is [1,1,0,0], nDCG=1.0
        assert ndcg_at_k([1, 1, 0, 0], 4) == 1.0

    def test_reversed_ranking(self):
        # [0,0,1,1] vs ideal [1,1,0,0]
        # DCG = 0/log2(2) + 0/log2(3) + 1/log2(4) + 1/log2(5) = 0.5 + 0.4307 = 0.9307
        # IDCG = 1/log2(2) + 1/log2(3) + 0 + 0 = 1.0 + 0.6309 = 1.6309
        val = ndcg_at_k([0, 0, 1, 1], 4)
        expected = (1 / math.log2(4) + 1 / math.log2(5)) / (1 / math.log2(2) + 1 / math.log2(3))
        assert abs(val - expected) < 1e-9

    def test_k_truncation(self):
        # Only consider first 2 positions
        assert ndcg_at_k([1, 0, 1, 1], 2) == ndcg_at_k([1, 0], 2)

    def test_all_irrelevant(self):
        assert ndcg_at_k([0, 0, 0], 3) == 0.0

    def test_single_relevant_at_1(self):
        # [1] at k=1: DCG=1/log2(2)=1, IDCG=1 -> nDCG=1.0
        assert ndcg_at_k([1], 1) == 1.0

    def test_empty_rels(self):
        assert ndcg_at_k([], 5) == 0.0


# ---------------------------------------------------------------------------
# Recall hand-checked cases
# ---------------------------------------------------------------------------

class TestRecall:
    def test_perfect_recall(self):
        # 2 gold retrievable, both in top-4
        assert recall_at_k([1, 1, 0, 0], 4, gold_retrievable=2) == 1.0

    def test_partial_recall(self):
        # 3 gold retrievable, only 1 in top-3
        assert recall_at_k([1, 0, 0], 3, gold_retrievable=3) == 1 / 3

    def test_zero_gold_retrievable(self):
        # HARD RULE (2): conditional on gold_retrievable > 0
        assert recall_at_k([1, 1], 2, gold_retrievable=0) == 0.0

    def test_k_truncation(self):
        assert recall_at_k([0, 0, 1, 1], 2, gold_retrievable=2) == 0.0

    def test_recall_ceiling(self):
        # gold_retrievable=1 but gold_total might be 3 — recall can only reach 1/3 ceiling
        # Here we test recall_at_k which uses gold_retrievable directly
        assert recall_at_k([1, 0], 2, gold_retrievable=1) == 1.0


# ---------------------------------------------------------------------------
# MRR hand-checked cases
# ---------------------------------------------------------------------------

class TestMrr:
    def test_first_position(self):
        assert mrr_at_k([1, 0, 0], 3) == 1.0

    def test_second_position(self):
        assert mrr_at_k([0, 1, 0], 3) == 0.5

    def test_no_relevant(self):
        assert mrr_at_k([0, 0, 0], 3) == 0.0

    def test_k_truncation(self):
        # Relevant at position 3 but k=2
        assert mrr_at_k([0, 0, 1], 2) == 0.0


# ---------------------------------------------------------------------------
# Bootstrap CI
# ---------------------------------------------------------------------------

class TestBootstrapCi:
    def test_constant_values(self):
        # All same value -> CI collapses
        lo, hi = bootstrap_ci([0.5, 0.5, 0.5, 0.5], iters=1000, seed=42)
        assert lo == 0.5
        assert hi == 0.5

    def test_ci_contains_mean(self):
        vals = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
        lo, hi = bootstrap_ci(vals, iters=2000, seed=0)
        mean = sum(vals) / len(vals)
        assert lo <= mean <= hi

    def test_empty_values(self):
        assert bootstrap_ci([]) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# Paired bootstrap
# ---------------------------------------------------------------------------

class TestPairedBootstrap:
    def test_identical_returns_high_p(self):
        vals = [0.5, 0.6, 0.7, 0.8, 0.9]
        p = paired_bootstrap(vals, vals, iters=1000, seed=0)
        # Identical -> obs_diff=0, every resample >= 0, p=1.0
        assert p == 1.0

    def test_very_different_returns_low_p(self):
        a = [0.9, 0.95, 0.92, 0.88, 0.91, 0.93, 0.90, 0.89]
        b = [0.1, 0.05, 0.08, 0.12, 0.09, 0.07, 0.10, 0.11]
        p = paired_bootstrap(a, b, iters=2000, seed=0)
        assert p < 0.05

    def test_empty_returns_1(self):
        assert paired_bootstrap([], []) == 1.0


# ---------------------------------------------------------------------------
# Abstention curve — three-way semantics
# ---------------------------------------------------------------------------

def _make_row(query_id: str, abstain_class: str, top_norm: float) -> ScoredRow:
    return ScoredRow(
        model_id="test", query_id=query_id,
        slice={"s": "a"}, latency_ms=10.0,
        rels=[1], gold_total=1, gold_retrievable=1,
        abstain_class=abstain_class,
        expect_abstain=(abstain_class == "unanswerable"),
        top_norm=top_norm,
    )


class TestAbstentionCurve:
    def test_basic_curve(self):
        rows = [
            _make_row("q1", "unanswerable", 0.2),        # should abstain, score low
            _make_row("q2", "unanswerable", 0.8),        # should abstain, score high
            _make_row("q3", "answerable_retrievable", 0.9),  # should answer, score high
            _make_row("q4", "answerable_retrievable", 0.1),  # should answer, score low
        ]
        curve = abstention_curve(rows, [0.5])
        assert len(curve) == 1
        pt = curve[0]
        # t=0.5: q1 (norm=0.2 < 0.5) abstains (TP), q2 (norm=0.8 >= 0.5) answers (FN)
        # q3 (0.9 >= 0.5) answers (TN), q4 (0.1 < 0.5) abstains (FP)
        assert pt.abstain_recall == 0.5  # 1 TP / (1 TP + 1 FN)
        assert pt.false_answer_rate == 0.5  # 1 FN / (1 FN + 1 TP)
        assert pt.false_abstain_rate == 0.5  # 1 FP / (1 FP + 1 TN)

    def test_answerable_not_retrieved_excluded_from_fp(self):
        """HARD RULE (3): answerable_not_retrieved is NOT in the FP pool."""
        rows = [
            _make_row("q1", "unanswerable", 0.2),              # TP at t=0.5
            _make_row("q2", "answerable_retrievable", 0.9),    # TN at t=0.5
            _make_row("q3", "answerable_not_retrieved", 0.1),  # abstains but NOT FP
        ]
        curve = abstention_curve(rows, [0.5])
        pt = curve[0]
        # Only 1 unanswerable: TP=1, FN=0 -> recall=1.0, false_answer_rate=0.0
        assert pt.abstain_recall == 1.0
        assert pt.false_answer_rate == 0.0
        # Only 1 answerable_retrievable (q2): FP=0, TN=1 -> false_abstain_rate=0.0
        assert pt.false_abstain_rate == 0.0

    def test_sorted_output(self):
        rows = [_make_row("q1", "unanswerable", 0.5)]
        curve = abstention_curve(rows, [0.9, 0.1, 0.5])
        assert [p.t for p in curve] == [0.1, 0.5, 0.9]


# ---------------------------------------------------------------------------
# Operating point
# ---------------------------------------------------------------------------

class TestOperatingPoint:
    def test_picks_max_recall_under_ceiling(self):
        curve = [
            AbstainPoint(t=0.2, abstain_recall=0.4, false_answer_rate=0.08, false_abstain_rate=0.02),
            AbstainPoint(t=0.4, abstain_recall=0.7, false_answer_rate=0.04, false_abstain_rate=0.10),
            AbstainPoint(t=0.6, abstain_recall=0.9, false_answer_rate=0.02, false_abstain_rate=0.20),
        ]
        # ceiling=0.05: t=0.4 (far=0.04) and t=0.6 (far=0.02) qualify; pick t=0.6 (higher recall)
        assert operating_point(curve, 0.05) == 0.6

    def test_nothing_qualifies(self):
        curve = [
            AbstainPoint(t=0.2, abstain_recall=0.4, false_answer_rate=0.10, false_abstain_rate=0.02),
        ]
        # ceiling=0.05: nothing qualifies -> t=0.0
        assert operating_point(curve, 0.05) == 0.0


# ---------------------------------------------------------------------------
# Aggregate (integration test with sample data)
# ---------------------------------------------------------------------------

class TestAggregate:
    def _sample_rows(self) -> list[ScoredRow]:
        """Build scored rows from sample fixtures as if a model returned perfect ranking."""
        return [
            ScoredRow(model_id="m1", query_id="q01", slice={"english": "clean"},
                      latency_ms=100, rels=[1, 1, 0, 0], gold_total=2, gold_retrievable=2,
                      abstain_class="answerable_retrievable", expect_abstain=False, top_norm=0.9),
            ScoredRow(model_id="m1", query_id="q04", slice={"english": "clean"},
                      latency_ms=80, rels=[0, 0, 0], gold_total=0, gold_retrievable=0,
                      abstain_class="unanswerable", expect_abstain=True, top_norm=0.2),
            ScoredRow(model_id="m1", query_id="q06", slice={"english": "clean"},
                      latency_ms=90, rels=[0, 0, 0], gold_total=1, gold_retrievable=0,
                      abstain_class="answerable_not_retrieved", expect_abstain=False, top_norm=0.3),
        ]

    def test_ndcg_only_answerable(self):
        """HARD RULE (1): nDCG only over answerable queries."""
        rows = self._sample_rows()
        result = aggregate(rows, rows, k=4)
        # All in slice "english=clean"
        cell = result["english=clean"]
        # answerable = q01 and q06 (expect_abstain=False)
        # q01: [1,1,0,0] perfect -> nDCG=1.0; q06: [0,0,0] -> nDCG=0.0
        assert abs(cell["ndcg10"] - 0.5) < 1e-9

    def test_recall_conditional_on_retrievable(self):
        """HARD RULE (2): recall only for gold_retrievable > 0."""
        rows = self._sample_rows()
        result = aggregate(rows, rows, k=4)
        cell = result["english=clean"]
        # answerable with gold_retrievable > 0: only q01 (gold_retrievable=2, rels=[1,1,0,0])
        # q06 has gold_retrievable=0 so excluded
        assert cell["recall10"] == 1.0

    def test_three_way_counts(self):
        """HARD RULE (3): three-way counts exposed."""
        rows = self._sample_rows()
        result = aggregate(rows, rows, k=4)
        cell = result["english=clean"]
        assert cell["three_way_counts"]["unanswerable"] == 1
        assert cell["three_way_counts"]["answerable_retrievable"] == 1
        assert cell["three_way_counts"]["answerable_not_retrieved"] == 1

    def test_significance_identical_is_1(self):
        """HARD RULE (4): paired bootstrap, identical -> p=1.0."""
        rows = self._sample_rows()
        result = aggregate(rows, rows, k=4)
        cell = result["english=clean"]
        assert cell["sig_vs_baseline"] == 1.0

    def test_abstain_curve_present(self):
        """HARD RULE (5): full abstain curve in output."""
        rows = self._sample_rows()
        result = aggregate(rows, rows, k=4)
        cell = result["english=clean"]
        assert len(cell["abstain_curve"]) > 0
        assert all(isinstance(p, AbstainPoint) for p in cell["abstain_curve"])
