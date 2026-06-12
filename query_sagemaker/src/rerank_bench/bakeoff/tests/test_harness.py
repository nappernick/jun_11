"""Tests for bakeoff.harness — stages 0-2 and MockReranker."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from bakeoff.contract import Candidate, Fixture, RankedDoc
from bakeoff.harness import (
    MockReranker,
    freeze_candidates,
    load_fixtures,
    run_model,
    score_one,
)

SAMPLE_PATH = Path(__file__).resolve().parent.parent / "sample" / "sample_fixtures.jsonl"


# ---------------------------------------------------------------------------
# Stage 0: load_fixtures
# ---------------------------------------------------------------------------

class TestLoadFixtures:
    def test_loads_all_fixtures(self):
        fixtures = load_fixtures(SAMPLE_PATH)
        assert len(fixtures) == 12

    def test_fixture_types(self):
        fixtures = load_fixtures(SAMPLE_PATH)
        for f in fixtures:
            assert isinstance(f, Fixture)
            assert isinstance(f.gold_node_ids, set)


# ---------------------------------------------------------------------------
# Stage 0: freeze_candidates SEAM
# ---------------------------------------------------------------------------

class TestFreezeCandidates:
    """The freeze seam is now wired to AossAccess. Verify the wiring offline by
    faking the accessor — no live AWS, no boto3 import at test time."""

    def test_builds_sentinel_aware_filter_and_delegates(self, monkeypatch):
        import bakeoff.harness as h

        captured = {}

        class FakeAccess:
            def search(self, query, size, scope_filter=None):
                captured["query"] = query
                captured["size"] = size
                captured["scope_filter"] = scope_filter
                return [Candidate(node_id="n1", text="frozen doc")]

        monkeypatch.setattr(h, "_aoss_access", lambda: FakeAccess())
        out = freeze_candidates("book a flight", {"country": "India"}, pool_size=7)

        assert [c.node_id for c in out] == ["n1"]
        assert captured["query"] == "book a flight"
        assert captured["size"] == 7
        # sentinel-aware: India + the "Global" country sentinel
        assert captured["scope_filter"] == [{"terms": {"country": ["India", "Global"]}}]

    def test_empty_scope_passes_no_filter(self, monkeypatch):
        import bakeoff.harness as h

        captured = {}

        class FakeAccess:
            def search(self, query, size, scope_filter=None):
                captured["scope_filter"] = scope_filter
                return []

        monkeypatch.setattr(h, "_aoss_access", lambda: FakeAccess())
        freeze_candidates("q", {})
        assert captured["scope_filter"] is None


# ---------------------------------------------------------------------------
# Stage 2: score_one — abstain class labeling
# ---------------------------------------------------------------------------

class TestScoreOneAbstainClasses:
    """Assert score_one labels the three abstain classes correctly."""

    def test_unanswerable(self):
        """gold_total==0 -> unanswerable, expect_abstain=True."""
        fixture = Fixture(
            query_id="u1", query="nonsense",
            gold_node_ids=set(),
            candidates=[Candidate(node_id="c1", text="irrelevant")],
            slice={"english": "clean"}, answerability="unanswerable",
        )
        ranked = [RankedDoc(node_id="c1", rank=0, raw_score=1.0, norm_score=0.7)]
        row = score_one(fixture, ranked, 5.0, "test")
        assert row.abstain_class == "unanswerable"
        assert row.expect_abstain is True
        assert row.gold_total == 0
        assert row.gold_retrievable == 0

    def test_answerable_not_retrieved(self):
        """gold_total>0, gold ∩ candidates = ∅ -> answerable_not_retrieved."""
        fixture = Fixture(
            query_id="anr1", query="transfer",
            gold_node_ids={"n_missing"},
            candidates=[Candidate(node_id="c1", text="unrelated")],
            slice={"english": "clean"}, answerability="answerable_not_retrieved",
        )
        ranked = [RankedDoc(node_id="c1", rank=0, raw_score=0.5, norm_score=0.6)]
        row = score_one(fixture, ranked, 3.0, "test")
        assert row.abstain_class == "answerable_not_retrieved"
        assert row.expect_abstain is False
        assert row.gold_total == 1
        assert row.gold_retrievable == 0

    def test_answerable_retrievable(self):
        """gold_total>0, gold ∩ candidates ≠ ∅ -> answerable_retrievable."""
        fixture = Fixture(
            query_id="ar1", query="holidays",
            gold_node_ids={"c1", "c2"},
            candidates=[
                Candidate(node_id="c1", text="holidays info"),
                Candidate(node_id="c3", text="other"),
            ],
            slice={"english": "clean"}, answerability="answerable_retrievable",
        )
        ranked = [
            RankedDoc(node_id="c1", rank=0, raw_score=2.0, norm_score=0.88),
            RankedDoc(node_id="c3", rank=1, raw_score=0.1, norm_score=0.52),
        ]
        row = score_one(fixture, ranked, 2.0, "test")
        assert row.abstain_class == "answerable_retrievable"
        assert row.expect_abstain is False
        assert row.gold_total == 2
        assert row.gold_retrievable == 1
        assert row.rels == [1, 0]

    def test_top_norm_empty_ranked(self):
        """Empty ranked list -> top_norm=0.0."""
        fixture = Fixture(
            query_id="e1", query="x",
            gold_node_ids=set(),
            candidates=[Candidate(node_id="c1", text="y")],
            slice={}, answerability="unanswerable",
        )
        row = score_one(fixture, [], 1.0, "test")
        assert row.top_norm == 0.0

    def test_all_sample_fixtures_labeled_correctly(self):
        """End-to-end: load sample, score with mock, verify abstain labels match."""
        fixtures = load_fixtures(SAMPLE_PATH)
        reranker = MockReranker()
        for f in fixtures:
            ranked = reranker.rerank(f.query, f.candidates, 10)
            row = score_one(f, ranked, 1.0, "mock")
            assert row.abstain_class == f.answerability, (
                f"Fixture {f.query_id}: expected {f.answerability}, got {row.abstain_class}"
            )


# ---------------------------------------------------------------------------
# Stage 1: run_model — timer wraps only rerank
# ---------------------------------------------------------------------------

class TestRunModelTiming:
    def test_timer_wraps_only_rerank(self):
        """Latency reflects only the rerank() call duration."""
        fixtures = load_fixtures(SAMPLE_PATH)[:1]

        class SlowReranker:
            @property
            def id(self) -> str:
                return "slow"

            def rerank(self, query, candidates, top_k):
                time.sleep(0.05)  # 50ms
                return [RankedDoc(node_id=c.node_id, rank=i,
                                  raw_score=0.0, norm_score=0.5)
                        for i, c in enumerate(candidates[:top_k])]

        rows = run_model(SlowReranker(), fixtures, 10)
        assert len(rows) == 1
        # Should be at least 50ms (the sleep) but not wildly more
        assert rows[0].latency_ms >= 45.0
        assert rows[0].latency_ms < 200.0


# ---------------------------------------------------------------------------
# MockReranker — never throws
# ---------------------------------------------------------------------------

class TestMockReranker:
    def test_deterministic(self):
        """Same inputs -> same outputs."""
        reranker = MockReranker()
        cands = [Candidate(node_id="a", text="book a flight"),
                 Candidate(node_id="b", text="parking lot")]
        r1 = reranker.rerank("book flight", cands, 2)
        r2 = reranker.rerank("book flight", cands, 2)
        assert [d.node_id for d in r1] == [d.node_id for d in r2]
        assert [d.raw_score for d in r1] == [d.raw_score for d in r2]

    def test_never_throws_on_empty_text(self):
        """Empty text candidate -> low score, no exception."""
        reranker = MockReranker()
        cands = [Candidate(node_id="empty", text=""),
                 Candidate(node_id="good", text="travel booking info")]
        result = reranker.rerank("travel", cands, 2)
        assert len(result) == 2
        # "good" should rank higher
        assert result[0].node_id == "good"

    def test_never_throws_on_none_like_text(self):
        """Candidate with bizarre text -> still returns, no exception."""
        reranker = MockReranker()
        cands = [Candidate(node_id="bad", text="", source_metadata={})]
        result = reranker.rerank("anything", cands, 5)
        assert len(result) == 1
        assert result[0].norm_score > 0.0  # sigmoid(-5) ≈ 0.0067

    def test_id_is_mock(self):
        assert MockReranker().id == "mock"

    def test_norm_scores_in_unit_range(self):
        """All norm_scores are in [0, 1]."""
        reranker = MockReranker()
        fixtures = load_fixtures(SAMPLE_PATH)
        for f in fixtures:
            ranked = reranker.rerank(f.query, f.candidates, 10)
            for doc in ranked:
                assert 0.0 <= doc.norm_score <= 1.0
