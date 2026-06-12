"""
Promotion decision and convergence tracking for the closed-loop prompt optimizer.

This module holds the two small, dependency-light pieces that together decide, per
iteration, whether Phase A keeps going (design "Component 6: PromotionDecider +
ConvergenceTracker"):

* :class:`PromotionDecider` — a stateless, pure wrapper over the promotion predicate
  :func:`bakeoff.quality.optimizer.stats.is_significant`. It answers a single question:
  "given the champion's and challenger's triad scores and the significance threshold,
  should the challenger be promoted?" A non-usable challenger (empty / whitespace-only /
  byte-identical to the champion — surfaced by the caller as ``usable=False``) is never
  promoted (Req 3.5), so a degenerate Author output becomes a normal non-improving
  iteration rather than an error.

* :class:`ConvergenceTracker` — the mutable per-model counter behind the stop rule
  (Req 6). It maintains the length of the current trailing run of non-improving
  iterations, resets that run to zero on every promotion (Req 6.2), and — the first time
  the run reaches the configured ``stop_limit`` — records the iteration at which
  convergence was reached and the human-readable reason Phase A stopped (Req 6.3, 6.6).
  Once converged it stays converged: a later call never clears or moves the recorded
  convergence point.

Together these satisfy design Property 9 (the counter equals the trailing reject run,
resets on promotion, and stops *exactly* at the first iteration the run reaches the limit)
and the convergence half of design Property 5 (a non-usable challenger counts as a
non-improving iteration). The controller (Task 11) wires them together: it calls
``decider.decide(...)`` to get the accept/reject outcome, then feeds that outcome to
``tracker.record(promoted=...)``.

The promotion threshold and the noise-floor reasoning behind the default ``stop_limit``
are owned by :mod:`bakeoff.config` (``QUALITY_OPT_STOP_LIMIT = 5``, Req 6.4) and
:mod:`bakeoff.quality.optimizer.stats`; they are never redefined here. This module is
pure (no I/O, no network, no global state) so it can be exercised exhaustively by the
property-based tests in Tasks 7.2–7.4.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from bakeoff import config
from bakeoff.quality.optimizer import stats

__all__ = [
    "PromotionDecider",
    "ConvergenceTracker",
]


class PromotionDecider:
    """Decide whether a challenger should be promoted over the current champion.

    A thin, stateless adapter over the pure promotion predicate
    :func:`bakeoff.quality.optimizer.stats.is_significant` (design Component 6). It holds
    no state of its own and performs no I/O, so a single instance can be shared freely and
    every call is a pure function of its arguments.

    The only behavior it adds on top of the bare predicate is the usability gate: a
    challenger the caller has already classified as non-usable (empty, whitespace-only, or
    byte-identical to the champion) is never promoted (Req 3.5), independent of the
    scores. This keeps the "a degenerate Author output is just a non-improving iteration"
    rule (design Property 5) in one obvious place rather than scattered through the
    controller.
    """

    def decide(
        self,
        champion_score: float,
        challenger_score: float,
        threshold: float = config.QUALITY_OPT_SIGNIFICANCE_THRESHOLD,
        *,
        usable: bool = True,
    ) -> bool:
        """Return whether the challenger is promoted to champion.

        The decision is the pure significance test on the absolute triad delta — the
        challenger is promoted **iff** ``(challenger_score - champion_score) >= threshold``
        (Req 1.6 / 5.1, via :func:`stats.is_significant`) — gated by usability: a
        non-usable challenger (``usable=False``) is **never** promoted regardless of the
        scores (Req 3.5). The caller (the IterationController) is responsible for
        classifying usability before calling, and for recording the resulting outcome on a
        :class:`ConvergenceTracker`.

        Args:
            champion_score: the current champion's triad score (0..1).
            challenger_score: the candidate challenger's triad score (0..1).
            threshold: the minimum absolute triad gain that counts as significant;
                defaults to :data:`bakeoff.config.QUALITY_OPT_SIGNIFICANCE_THRESHOLD`
                (0.05).
            usable: whether the challenger is a usable prompt at all. ``False`` for an
                empty / whitespace-only / identical-to-champion Author output, which is
                never promoted (Req 3.5).

        Returns:
            ``True`` iff the challenger should be promoted to champion; ``False`` to
            retain the current champion (including every non-usable challenger).
        """
        if not usable:
            return False
        return stats.is_significant(champion_score, challenger_score, threshold)


@dataclass
class ConvergenceTracker:
    """Track the consecutive-non-improving run and fire the Phase-A stop rule.

    Mutable per-model state (one tracker per Target_Model) implementing Requirement 6's
    stop rule and design Property 9. It counts the current trailing run of non-improving
    iterations, resets that run to zero whenever a challenger is promoted (Req 6.2), and
    the first time the run reaches :attr:`stop_limit` it records the iteration at which
    convergence was reached and a human-readable stop reason (Req 6.3, 6.6). Convergence
    is sticky: once recorded it is never cleared or moved by a later call.

    Attributes:
        stop_limit: the number of consecutive non-improving iterations that triggers
            convergence; defaults to :data:`bakeoff.config.QUALITY_OPT_STOP_LIMIT` (5,
            Req 6.4) and is configurable per construction (Req 6.5).
        consecutive_non_improving: the length of the current trailing run of non-improving
            iterations (reset to 0 on every promotion, Req 6.2).
        converged_iteration: the ``iteration_index`` at which the run first reached
            ``stop_limit``, or ``None`` while Phase A should keep running (Req 6.6).
        stop_reason: the human-readable reason Phase A stopped, set together with
            ``converged_iteration`` (Req 6.6), or ``None`` until convergence.
    """

    stop_limit: int = config.QUALITY_OPT_STOP_LIMIT
    consecutive_non_improving: int = 0
    converged_iteration: Optional[int] = None
    stop_reason: Optional[str] = None

    def record(self, *, promoted: bool, iteration_index: int) -> None:
        """Record one iteration's outcome and update the stop-rule state.

        Updates the trailing-run counter, then — the first time the run reaches
        :attr:`stop_limit` — records the convergence point. Specifically:

        * On a **promotion** (``promoted=True``) the consecutive-non-improving run resets
          to ``0`` (Req 6.2). A non-usable challenger is reported by the caller as
          ``promoted=False`` (Req 3.5 / design Property 5), so it increments the run like
          any other non-improving iteration.
        * On a **non-improving** iteration (``promoted=False``) the run increments by one
          (Req 6.1).
        * When the run reaches ``stop_limit`` for the **first** time, ``converged_iteration``
          is set to ``iteration_index`` and ``stop_reason`` to the documented message
          (Req 6.3, 6.6). The guard ``converged_iteration is None`` makes convergence
          sticky: a later call never overwrites the originally recorded convergence point
          or reason, so Phase A stops *exactly* at the first crossing — never later
          (design Property 9). The counter itself keeps tracking the trailing run for
          auditability even after convergence.

        Args:
            promoted: whether this iteration's challenger was promoted to champion. The
                caller passes ``False`` for both a rejected usable challenger and a
                non-usable one.
            iteration_index: the index of the iteration whose outcome is being recorded;
                used as ``converged_iteration`` when this call trips the stop rule.
        """
        if promoted:
            self.consecutive_non_improving = 0
        else:
            self.consecutive_non_improving += 1
        # Fire the stop rule exactly once, the first time the trailing reject run reaches
        # the limit; the guard keeps convergence sticky (never un-converge / re-stamp).
        if (
            self.consecutive_non_improving >= self.stop_limit
            and self.converged_iteration is None
        ):
            self.converged_iteration = iteration_index
            self.stop_reason = f"{self.stop_limit} consecutive non-improving iterations"

    @property
    def should_stop(self) -> bool:
        """Whether Phase A should stop for this model (convergence has been reached).

        ``True`` once :meth:`record` has tripped the stop rule (``converged_iteration`` is
        set) and remains ``True`` thereafter, since convergence is sticky.
        """
        return self.converged_iteration is not None
