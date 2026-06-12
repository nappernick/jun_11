"""Tests for bakeoff.quality.optimizer.tournament — pure decision logic."""
from __future__ import annotations

import pytest

from bakeoff import config
from bakeoff.quality.optimizer.island import IslandState
from bakeoff.quality.optimizer.tournament import (
    TournamentBudget,
    TournamentDecision,
    MigrationPlan,
    choose_shared_rung,
    decide_winner,
    escalation_gate,
    migration_plan,
    should_run_tournament,
)


def _island(island_id: int, rung_index: int = 0, n_rungs: int = 4, **kw) -> IslandState:
    """Build a minimal IslandState for testing."""
    defaults = dict(
        island_id=island_id,
        model="test-model",
        style=config.QUALITY_OPT_ISLAND_STYLES[island_id],
        rung_index=rung_index,
        n_rungs=n_rungs,
        rung_n_items=12,
        rung_n_conversations=12,
        at_top_rung=(rung_index >= n_rungs - 1),
        champion_instruction="test",
        champion_score=0.7,
        champion_ci_half_width=0.05,
        prior_rung_score=None,
        iterations_at_rung=2,
        total_iterations=4,
        consecutive_non_improving=1,
        improved_at_rung=True,
        stuck=False,
    )
    defaults.update(kw)
    return IslandState(**defaults)


# --- escalation_gate -------------------------------------------------------

class TestEscalationGate:
    def test_passes_when_improved_and_within_slack(self):
        assert escalation_gate(0.70, 0.72, 0.05, True, ci_slack=1.0) is True

    def test_fails_when_not_improved(self):
        assert escalation_gate(0.70, 0.72, 0.05, False, ci_slack=1.0) is False

    def test_fails_when_too_far_below_baseline(self):
        # 0.60 < 0.72 - 1.0*0.05 = 0.67
        assert escalation_gate(0.60, 0.72, 0.05, True, ci_slack=1.0) is False

    def test_passes_on_rung0_no_prior(self):
        assert escalation_gate(0.50, None, 0.10, True) is True


# --- should_run_tournament --------------------------------------------------

class TestShouldRunTournament:
    def test_both_reached_min_rung(self):
        states = [_island(0, rung_index=2), _island(1, rung_index=3)]
        assert should_run_tournament(states, min_rung=2, every_iters=99, total_iters=0) is True

    def test_one_below_min_rung(self):
        states = [_island(0, rung_index=1), _island(1, rung_index=3)]
        assert should_run_tournament(states, min_rung=2, every_iters=99, total_iters=0) is False

    def test_iters_threshold(self):
        states = [_island(0, rung_index=0), _island(1, rung_index=0)]
        assert should_run_tournament(states, min_rung=2, every_iters=6, total_iters=6) is True

    def test_below_both_thresholds(self):
        states = [_island(0, rung_index=0), _island(1, rung_index=1)]
        assert should_run_tournament(states, min_rung=2, every_iters=10, total_iters=3) is False


# --- choose_shared_rung -----------------------------------------------------

class TestChooseSharedRung:
    def test_takes_larger_rung(self):
        states = [_island(0, rung_index=1, n_rungs=4), _island(1, rung_index=2, n_rungs=4)]
        assert choose_shared_rung(states, min_rung=2) == 2

    def test_clamps_to_min_rung(self):
        states = [_island(0, rung_index=0, n_rungs=4), _island(1, rung_index=1, n_rungs=4)]
        assert choose_shared_rung(states, min_rung=2) == 2

    def test_clamps_to_top_rung(self):
        states = [_island(0, rung_index=3, n_rungs=4), _island(1, rung_index=3, n_rungs=4)]
        # min_rung=2, max of islands=3, top=3 → 3
        assert choose_shared_rung(states, min_rung=2) == 3

    def test_min_rung_above_islands_still_returns_min(self):
        states = [_island(0, rung_index=0, n_rungs=4), _island(1, rung_index=0, n_rungs=4)]
        assert choose_shared_rung(states, min_rung=3) == 3


# --- decide_winner ----------------------------------------------------------

class TestDecideWinner:
    def test_a_significantly_better(self):
        # a=0.80, b=0.70, threshold=0.05 → a wins, not a tie
        d = decide_winner(0.80, 0.03, 0.70, 0.03, threshold=0.05)
        assert d.winner_island_id == 0
        assert d.tie is False

    def test_b_significantly_better(self):
        d = decide_winner(0.70, 0.03, 0.80, 0.03, threshold=0.05)
        assert d.winner_island_id == 1
        assert d.tie is False

    def test_tie_higher_score_wins(self):
        # Neither is significant (diff=0.03 < 0.05) but a has higher score
        d = decide_winner(0.73, 0.05, 0.70, 0.05, threshold=0.05)
        assert d.winner_island_id == 0
        assert d.tie is True

    def test_tie_lower_ci_wins(self):
        # Same score, a has tighter CI
        d = decide_winner(0.70, 0.03, 0.70, 0.06, threshold=0.05)
        assert d.winner_island_id == 0
        assert d.tie is True

    def test_tie_all_equal_island0_wins(self):
        d = decide_winner(0.70, 0.05, 0.70, 0.05, threshold=0.05)
        assert d.winner_island_id == 0
        assert d.tie is True

    def test_deterministic(self):
        """Same inputs always produce the same result."""
        results = [decide_winner(0.72, 0.04, 0.70, 0.04, threshold=0.05) for _ in range(10)]
        assert all(r == results[0] for r in results)


# --- migration_plan ---------------------------------------------------------

class TestMigrationPlan:
    def test_preserves_distinct_styles(self):
        plan = migration_plan(0, "winning prompt")
        # Both islands get the same winning instruction
        assert plan.winning_instruction == "winning prompt"
        # But each island retains its OWN style
        assert len(plan.island_styles) == config.QUALITY_OPT_ISLANDS_PER_MODEL
        assert plan.island_styles[0] != plan.island_styles[1]
        # Style indices match the config
        assert plan.island_styles[0] == config.QUALITY_OPT_ISLAND_STYLES[0]
        assert plan.island_styles[1] == config.QUALITY_OPT_ISLAND_STYLES[1]

    def test_winner_id_preserved(self):
        plan = migration_plan(1, "instruction")
        assert plan.winner_island_id == 1

    def test_to_dict_roundtrip(self):
        plan = migration_plan(0, "test")
        d = plan.to_dict()
        assert d["winner_island_id"] == 0
        assert d["winning_instruction"] == "test"
        assert len(d["island_styles"]) == 2


# --- TournamentBudget -------------------------------------------------------

class TestTournamentBudget:
    def test_not_final_before_max(self):
        b = TournamentBudget(current_round=1, max_rounds=3)
        assert b.is_final_round is False
        assert b.should_freeze_to_phase_b is False

    def test_final_at_max(self):
        b = TournamentBudget(current_round=3, max_rounds=3)
        assert b.is_final_round is True
        assert b.should_freeze_to_phase_b is True

    def test_to_dict(self):
        b = TournamentBudget(current_round=2, max_rounds=3)
        assert b.to_dict() == {"current_round": 2, "max_rounds": 3}
