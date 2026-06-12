"""
Tournament decision logic for the v2 island-tournament optimizer.

Pure functions and frozen serializable dataclasses — ZERO I/O, no network, no store
writes, no event emission. The orchestrator consumes these to schedule and resolve
island-vs-island head-to-head rounds and to produce migration plans.

Sourcing honesty: tournament selection with migration is an EXTERNAL/industry technique,
not Amazon-internal guidance — same posture as the rest of this spec's methodology.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Optional, Sequence

from bakeoff import config
from bakeoff.quality.optimizer.island import IslandState
from bakeoff.quality.optimizer.stats import is_significant

__all__ = [
    "TournamentDecision",
    "MigrationPlan",
    "TournamentBudget",
    "escalation_gate",
    "should_run_tournament",
    "choose_shared_rung",
    "decide_winner",
    "migration_plan",
]


# ---------------------------------------------------------------------------
# Escalation policy (hybrid gate, pure)
# ---------------------------------------------------------------------------


def escalation_gate(
    champion_score: float,
    prior_rung_score: Optional[float],
    ci_half_width: float,
    improved_at_rung: bool,
    *,
    ci_slack: float = config.QUALITY_OPT_ESCALATION_CI_SLACK,
) -> bool:
    """Hybrid escalation gate: statistical elimination AND model-judgment proxy.

    Statistical half: champion is not significantly worse than the prior-rung baseline
    within ``ci_slack`` CI half-widths. Model-judgment half: the island has produced at
    least one promotion at the current rung (``improved_at_rung``).

    On rung 0 (no prior baseline) the statistical half is vacuously satisfied.
    """
    if not improved_at_rung:
        return False
    if prior_rung_score is None:
        return True
    return champion_score >= (prior_rung_score - ci_slack * ci_half_width)


# ---------------------------------------------------------------------------
# Tournament scheduling
# ---------------------------------------------------------------------------


def should_run_tournament(
    states: Sequence[IslandState],
    *,
    min_rung: int = config.QUALITY_OPT_TOURNAMENT_MIN_RUNG,
    every_iters: int = config.QUALITY_OPT_TOURNAMENT_EVERY_ITERS,
    total_iters: int,
) -> bool:
    """True when BOTH islands reached ``min_rung`` OR ``total_iters >= every_iters``."""
    if total_iters >= every_iters:
        return True
    return all(s.rung_index >= min_rung for s in states)


def choose_shared_rung(
    states: Sequence[IslandState],
    *,
    min_rung: int = config.QUALITY_OPT_TOURNAMENT_MIN_RUNG,
) -> int:
    """Shared rung >= min_rung: the larger of the two islands' rungs, clamped to top."""
    max_rung = max(s.rung_index for s in states)
    top_rung = min(s.n_rungs - 1 for s in states)
    return min(max(max_rung, min_rung), top_rung)


# ---------------------------------------------------------------------------
# Winner decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TournamentDecision:
    """Result of a head-to-head round."""

    winner_island_id: int
    loser_island_id: int
    tie: bool

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def decide_winner(
    score_a: float,
    ci_a: float,
    score_b: float,
    ci_b: float,
    *,
    threshold: float = config.QUALITY_OPT_SIGNIFICANCE_THRESHOLD,
) -> TournamentDecision:
    """Pick the winner via 0.05 significance; deterministic tie-break on ties.

    Significance: ``is_significant(loser, winner, threshold)``. When neither beats
    the other significantly, tie-break: higher score > lower CI half-width > island 0.
    """
    a_beats_b = is_significant(score_b, score_a, threshold)
    b_beats_a = is_significant(score_a, score_b, threshold)

    if a_beats_b and not b_beats_a:
        return TournamentDecision(winner_island_id=0, loser_island_id=1, tie=False)
    if b_beats_a and not a_beats_b:
        return TournamentDecision(winner_island_id=1, loser_island_id=0, tie=False)

    # Tie-break: higher score, then lower CI, then island 0.
    if score_a > score_b:
        return TournamentDecision(winner_island_id=0, loser_island_id=1, tie=True)
    if score_b > score_a:
        return TournamentDecision(winner_island_id=1, loser_island_id=0, tie=True)
    if ci_a < ci_b:
        return TournamentDecision(winner_island_id=0, loser_island_id=1, tie=True)
    if ci_b < ci_a:
        return TournamentDecision(winner_island_id=1, loser_island_id=0, tie=True)
    # All equal: island 0 wins.
    return TournamentDecision(winner_island_id=0, loser_island_id=1, tie=True)


# ---------------------------------------------------------------------------
# Migration plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MigrationPlan:
    """Plan: winning prompt becomes baseline for BOTH islands; styles preserved."""

    winner_island_id: int
    winning_instruction: str
    island_styles: tuple[str, ...]

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def migration_plan(
    winner_island_id: int,
    winning_instruction: str,
    *,
    island_styles: tuple[str, ...] = config.QUALITY_OPT_ISLAND_STYLES,
) -> MigrationPlan:
    """Winning prompt → baseline for BOTH islands; each retains its own style index."""
    return MigrationPlan(
        winner_island_id=winner_island_id,
        winning_instruction=winning_instruction,
        island_styles=island_styles,
    )


# ---------------------------------------------------------------------------
# Tournament budget
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TournamentBudget:
    """Track round progression toward the freeze point."""

    current_round: int
    max_rounds: int = config.QUALITY_OPT_TOURNAMENT_ROUNDS

    @property
    def is_final_round(self) -> bool:
        return self.current_round >= self.max_rounds

    @property
    def should_freeze_to_phase_b(self) -> bool:
        return self.current_round >= self.max_rounds

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)
