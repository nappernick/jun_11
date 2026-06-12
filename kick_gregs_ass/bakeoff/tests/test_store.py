"""Tests for store.py v2 island-partitioned fields and reconstruction methods."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bakeoff.quality.optimizer.store import (
    AuditRecord,
    DrivingFailure,
    IterationRecord,
    OptimizerStore,
)


# -- Fixtures / helpers ----------------------------------------------------


def _iter_rec(
    *,
    model: str = "sonnet",
    iteration_index: int = 1,
    island_id: int | None = None,
    rung_index: int | None = None,
    tournament_round: int | None = None,
    iteration_id: str | None = None,
) -> IterationRecord:
    """Build a minimal valid IterationRecord with optional v2 fields."""
    return IterationRecord(
        iteration_id=iteration_id or f"{model}-{iteration_index}",
        model=model,
        phase="A",
        iteration_index=iteration_index,
        backend="fake",
        author_model="sonnet",
        judge_model="opus",
        champion_score=0.8,
        champion_ci_half_width=0.03,
        challenger_score=0.82,
        challenger_ci_half_width=0.03,
        significance_threshold=0.05,
        promoted=True,
        gain_absolute=0.02,
        gain_percent=2.5,
        slice_n_conversations=20,
        between_conversation_sd=0.22,
        consecutive_non_improving=0,
        converged=False,
        stop_reason=None,
        mean_closeness=0.7,
        abstention_reward_mean=0.1,
        answered_when_unsure_rate=0.05,
        retrieval_backend="opensearch",
        created_at="2026-06-03T00:00:00+00:00",
        island_id=island_id,
        rung_index=rung_index,
        tournament_round=tournament_round,
    )


def _audit_rec(
    *,
    model: str = "sonnet",
    iteration_index: int = 1,
    island_id: int | None = None,
    rung_index: int | None = None,
    tournament_round: int | None = None,
) -> AuditRecord:
    """Build a minimal valid AuditRecord with optional v2 fields."""
    return AuditRecord(
        iteration_id=f"{model}-{iteration_index}",
        prompt_version_id=f"pv-{model}-{iteration_index}",
        model=model,
        iteration_index=iteration_index,
        backend="fake",
        author_model="sonnet",
        judge_model="opus",
        champion_instruction="do the thing",
        challenger_instruction="do it better",
        prompt_diff="--- a\n+++ b\n@@ -1 +1 @@\n-do the thing\n+do it better",
        author_rationale="improved clarity",
        driving_failures=(),
        challenger_triad=0.82,
        challenger_ci_half_width=0.03,
        challenger_per_dimension={"accuracy": 0.9},
        accepted=True,
        created_at="2026-06-03T00:00:00+00:00",
        island_id=island_id,
        rung_index=rung_index,
        tournament_round=tournament_round,
    )


# -- Round-trip tests: new fields ------------------------------------------


class TestIterationRecordV2Fields:
    """IterationRecord round-trips the three new optional fields."""

    def test_round_trip_with_all_fields(self):
        rec = _iter_rec(island_id=1, rung_index=2, tournament_round=3)
        restored = IterationRecord.from_jsonl(rec.to_jsonl())
        assert restored == rec
        assert restored.island_id == 1
        assert restored.rung_index == 2
        assert restored.tournament_round == 3

    def test_round_trip_with_none_fields(self):
        rec = _iter_rec()
        restored = IterationRecord.from_jsonl(rec.to_jsonl())
        assert restored == rec
        assert restored.island_id is None
        assert restored.rung_index is None
        assert restored.tournament_round is None

    def test_old_record_without_fields_loads_as_none(self):
        """v1 records that lack the new keys still deserialize (backward compat)."""
        rec = _iter_rec(island_id=0, rung_index=0, tournament_round=0)
        d = json.loads(rec.to_jsonl())
        # Simulate a v1 record by removing the new keys
        del d["island_id"]
        del d["rung_index"]
        del d["tournament_round"]
        line = json.dumps(d)
        restored = IterationRecord.from_jsonl(line)
        assert restored.island_id is None
        assert restored.rung_index is None
        assert restored.tournament_round is None


class TestAuditRecordV2Fields:
    """AuditRecord round-trips the three new optional fields."""

    def test_round_trip_with_all_fields(self):
        rec = _audit_rec(island_id=0, rung_index=1, tournament_round=2)
        restored = AuditRecord.from_jsonl(rec.to_jsonl())
        assert restored == rec
        assert restored.island_id == 0
        assert restored.rung_index == 1
        assert restored.tournament_round == 2

    def test_round_trip_with_none_fields(self):
        rec = _audit_rec()
        restored = AuditRecord.from_jsonl(rec.to_jsonl())
        assert restored == rec
        assert restored.island_id is None
        assert restored.rung_index is None
        assert restored.tournament_round is None

    def test_old_record_without_fields_loads_as_none(self):
        """v1 records that lack the new keys still deserialize (backward compat)."""
        rec = _audit_rec(island_id=1, rung_index=1, tournament_round=1)
        d = json.loads(rec.to_jsonl())
        del d["island_id"]
        del d["rung_index"]
        del d["tournament_round"]
        line = json.dumps(d)
        restored = AuditRecord.from_jsonl(line)
        assert restored.island_id is None
        assert restored.rung_index is None
        assert restored.tournament_round is None


# -- Island-partitioned reconstruction tests -------------------------------


class TestIslandPartitionedReconstruction:
    """OptimizerStore reconstruction groups by (model, island_id) and tournament_round."""

    def test_groups_by_model_and_island(self, tmp_path: Path):
        store = OptimizerStore(
            iterations_path=tmp_path / "iter.jsonl",
            audit_path=tmp_path / "audit.jsonl",
            errors_path=tmp_path / "err.jsonl",
            results_path=tmp_path / "res.json",
        )
        # Two islands under one model
        store.append_iteration(_iter_rec(model="s", iteration_index=0, island_id=0))
        store.append_iteration(_iter_rec(model="s", iteration_index=1, island_id=1))
        store.append_iteration(_iter_rec(model="s", iteration_index=2, island_id=0))
        store.append_iteration(_iter_rec(model="s", iteration_index=3, island_id=1))
        # Different model — should not appear
        store.append_iteration(_iter_rec(model="h", iteration_index=0, island_id=0))

        groups = store.iteration_history_by_island("s")
        assert ("s", 0) in groups
        assert ("s", 1) in groups
        assert ("h", 0) not in groups
        assert [r.iteration_index for r in groups[("s", 0)]] == [0, 2]
        assert [r.iteration_index for r in groups[("s", 1)]] == [1, 3]

    def test_legacy_records_group_under_none(self, tmp_path: Path):
        store = OptimizerStore(
            iterations_path=tmp_path / "iter.jsonl",
            audit_path=tmp_path / "audit.jsonl",
            errors_path=tmp_path / "err.jsonl",
            results_path=tmp_path / "res.json",
        )
        store.append_iteration(_iter_rec(model="s", iteration_index=0))
        store.append_iteration(_iter_rec(model="s", iteration_index=1))

        groups = store.iteration_history_by_island("s")
        assert ("s", None) in groups
        assert len(groups[("s", None)]) == 2

    def test_groups_by_tournament_round(self, tmp_path: Path):
        store = OptimizerStore(
            iterations_path=tmp_path / "iter.jsonl",
            audit_path=tmp_path / "audit.jsonl",
            errors_path=tmp_path / "err.jsonl",
            results_path=tmp_path / "res.json",
        )
        # Non-tournament iterations
        store.append_iteration(_iter_rec(model="s", iteration_index=0, island_id=0))
        store.append_iteration(_iter_rec(model="s", iteration_index=1, island_id=1))
        # Tournament round 1
        store.append_iteration(
            _iter_rec(model="s", iteration_index=2, island_id=0, tournament_round=1)
        )
        store.append_iteration(
            _iter_rec(model="s", iteration_index=3, island_id=1, tournament_round=1)
        )
        # Tournament round 2
        store.append_iteration(
            _iter_rec(model="s", iteration_index=4, island_id=0, tournament_round=2)
        )

        groups = store.iteration_history_by_tournament_round("s")
        assert None in groups
        assert 1 in groups
        assert 2 in groups
        assert [r.iteration_index for r in groups[None]] == [0, 1]
        assert [r.iteration_index for r in groups[1]] == [2, 3]
        assert [r.iteration_index for r in groups[2]] == [4]

    def test_mixed_islands_and_rounds(self, tmp_path: Path):
        """Two islands across multiple tournament rounds reconstruct correctly."""
        store = OptimizerStore(
            iterations_path=tmp_path / "iter.jsonl",
            audit_path=tmp_path / "audit.jsonl",
            errors_path=tmp_path / "err.jsonl",
            results_path=tmp_path / "res.json",
        )
        # Island 0: rungs 0,1 no tournament; then tournament round 1
        store.append_iteration(
            _iter_rec(model="m", iteration_index=0, island_id=0, rung_index=0)
        )
        store.append_iteration(
            _iter_rec(model="m", iteration_index=1, island_id=0, rung_index=1)
        )
        store.append_iteration(
            _iter_rec(
                model="m", iteration_index=4, island_id=0, rung_index=2, tournament_round=1
            )
        )
        # Island 1: rungs 0,1 no tournament; then tournament round 1
        store.append_iteration(
            _iter_rec(model="m", iteration_index=2, island_id=1, rung_index=0)
        )
        store.append_iteration(
            _iter_rec(model="m", iteration_index=3, island_id=1, rung_index=1)
        )
        store.append_iteration(
            _iter_rec(
                model="m", iteration_index=5, island_id=1, rung_index=2, tournament_round=1
            )
        )

        by_island = store.iteration_history_by_island("m")
        assert [r.iteration_index for r in by_island[("m", 0)]] == [0, 1, 4]
        assert [r.iteration_index for r in by_island[("m", 1)]] == [2, 3, 5]

        by_round = store.iteration_history_by_tournament_round("m")
        assert [r.iteration_index for r in by_round[None]] == [0, 1, 2, 3]
        assert [r.iteration_index for r in by_round[1]] == [4, 5]
