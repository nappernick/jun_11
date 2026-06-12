"""
ResilientIslandLoop — V3's failure-contained island over v2's hill-climb semantics.

Subclasses :class:`bakeoff.quality.optimizer.island.IslandLoop` so the algorithm —
stance-diverged authoring, rung ladder, promotion via ``PromotionDecider``, escalation
gate, patience — is REUSED unchanged. What V3 changes:

* **Scorer** — :meth:`_scorer_for` builds a
  :class:`~bakeoff.quality.optimizer.v3.scorer.ResilientScorer` (pipelined, guarded,
  collate-survivors) instead of the v2 scorer, and threads every contained
  conversation failure to the emitter as an ``optimizer_conversation_failed`` event.
* **Author guard** — :meth:`_author` wraps the (already internally-resilient) author
  call in a hard timeout, so a hung Sonnet stream cannot stall the island forever.
* **Contained step** — :meth:`step` catches :class:`IterationSkipped` (the scorer's
  too-few-survivors signal) and ANY other exception, converts them into a SKIPPED
  iteration (champion kept, counters advanced, ``optimizer_iteration_skipped``
  emitted) instead of letting them kill the run. A separate
  ``consecutive_failures`` counter (reset on any successful step) lets the
  orchestrator declare the island dead after
  ``config.QUALITY_OPT_V3_ISLAND_MAX_CONSECUTIVE_FAILURES`` — deterministic failures
  (e.g. a malformed item that fails every rung pass) converge to island-death rather
  than an infinite skip-spin.

Event note: skip/failure events ride the existing emitter's public ``emit`` with new
``optimizer_*`` event names — the v3 broker is separate in ``app.py``, so nothing
collides with v2 streams.
"""
from __future__ import annotations

import logging
from typing import Optional

from bakeoff import config
from bakeoff.quality.optimizer.failures import select_failures
from bakeoff.quality.optimizer.island import (
    IslandLoop,
    IslandState,
    StepDetail,
    _augment_with_stance,
    _strip_stance,
)
from bakeoff.quality.optimizer.judge_loop import SliceScore
from bakeoff.quality.optimizer.stats import gain_report
from bakeoff.quality.optimizer.store import make_prompt_diff
from bakeoff.quality.optimizer.v3.guards import guarded_call
from bakeoff.quality.optimizer.v3.scorer import (
    ConversationFailure,
    IterationSkipped,
    ResilientScorer,
)

__all__ = ["ResilientIslandLoop"]

_LOG = logging.getLogger("bakeoff.opt.v3.island")

#: V3-only SSE event names (ride the emitter's public ``emit``; v3 has its own broker).
EVENT_ITERATION_SKIPPED = "optimizer_iteration_skipped"
EVENT_CONVERSATION_FAILED = "optimizer_conversation_failed"
EVENT_SCORING_PROGRESS = "optimizer_scoring_progress"


class ResilientIslandLoop(IslandLoop):
    """v2's island semantics inside V3's failure envelope."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        #: trailing run of FAILED (skipped) steps; reset by any successful step. The
        #: orchestrator reads this to declare the island dead — distinct from
        #: ``consecutive_non_improving``, which counts honest non-promotions.
        self.consecutive_failures: int = 0
        #: the most recent skip's failure detail (for the orchestrator's error store).
        self.last_skip: Optional[IterationSkipped] = None

    # -- V3 scorer + failure event threading ---------------------------------------------
    def _scorer_for(self, reps: int) -> ResilientScorer:
        """Build (cached) the V3 resilient scorer for ``reps``, wired to failure events."""
        key = max(1, int(reps))
        scorer = self._scorers.get(key)
        if scorer is None:
            scorer = ResilientScorer(
                self._backend,
                reps=key,
                on_conversation_failure=self._emit_conversation_failure,
                on_conversation_scored=self._emit_scoring_progress,
            )
            self._scorers[key] = scorer
        return scorer

    def _emit_scoring_progress(self, **progress) -> None:
        """Stream one per-conversation scoring tick to the v3 UI (best-effort).

        Fires after EVERY judged conversation (owner direction: report each cycle's
        progress live), carrying ``role`` / ``done`` / ``total`` so the tab renders
        "champion 4/6" while a pass is in flight instead of going dark.
        """
        try:
            self._emitter.emit(
                EVENT_SCORING_PROGRESS,
                self._model,
                {
                    "island_id": self._island_id,
                    "rung_index": self._rung_index,
                    **progress,
                },
            )
        except Exception:  # noqa: BLE001 — observability must never affect the loop
            _LOG.warning("scoring-progress emit failed; ignoring", exc_info=True)

    def _emit_conversation_failure(self, failure: ConversationFailure) -> None:
        """Stream one contained conversation failure to the v3 UI (best-effort)."""
        try:
            self._emitter.emit(
                EVENT_CONVERSATION_FAILED,
                self._model,
                {
                    "island_id": self._island_id,
                    "rung_index": self._rung_index,
                    **failure.to_dict(),
                },
            )
        except Exception:  # noqa: BLE001 — observability must never affect the loop
            _LOG.warning("conversation-failure emit failed; ignoring", exc_info=True)

    # -- head-to-head promotion (LEARNING OVER TIME) -------------------------------------
    def _challenger_wins(self, champ_score: SliceScore, chall_score: SliceScore) -> bool:
        """Promote on a real improvement that also never lowers the belt.

        Both prompts are scored on the SAME items the SAME round, so their triad scores
        are a fair, apples-to-apples head-to-head. But the champion is RE-SCORED fresh
        every round and that re-score is noisy: a regression-to-the-mean low roll let a
        challenger that is actually WORSE than the champion's established score sneak in,
        ratcheting the champion DOWN (observed: 0.411 -> 0.400). So the bar the challenger
        must clear is the HIGHER of (a) the champion's fresh same-round score and (b) the
        champion's established (carried, monotonic) score. The challenger wins iff it
        strictly beats that bar — so a promotion can only ever move the champion UP and a
        losing challenger is never promoted.

        This still promotes genuine gains (the original fix: a real +0.05 win is kept,
        not rejected as noise) while refusing to trade an established champion for a
        lower-scoring challenger just because this round's champion re-roll dipped.
        """
        bar = champ_score.triad_score
        if self._champion_score is not None:
            bar = max(bar, self._champion_score)
        return chall_score.triad_score > bar

    async def _step_single(self) -> IslandState:
        """V3 ``author → score`` iteration: monotonic champion + significance gate.

        Same shape as v2's :meth:`IslandLoop._step_single` (scores the champion and a
        stance-authored challenger on the current rung, emits the same events, stashes a
        :class:`StepDetail`), with the two LEARNING-OVER-TIME corrections:

        1. Promotion uses :meth:`_challenger_wins` (a straight same-round head-to-head:
           the challenger wins iff it scored higher than the champion this round).
        2. A KEPT champion keeps its established score; it is NEVER re-rolled downward by
           a fresh noisy re-measurement. The champion's carried (monotonic) score is what
           the events and durable record report, so the trend line only ever holds or
           climbs. The fresh champion measurement is used ONLY for the within-round
           head-to-head decision, never to overwrite the carried score.
        """
        rung = self._ladder[self._rung_index]
        iteration_index = self._total_iterations
        champion_before = self._champion_instruction

        # 1) Score the current champion on this rung (fresh — for the head-to-head only).
        champ_score = await self._score(
            champion_before, role="champion", rung=rung, iteration_index=iteration_index
        )

        # 2) Worst judged turns drive the author (answering-when-unsure first).
        failures = select_failures(champ_score, k=self._failures_k)

        # 3) Author a challenger FROM the current champion (the winning prompt perpetuates
        #    as the base for the next attempt), stance threaded in then stripped back out.
        authored = await self._author(
            champion_before, failures, iteration_index=iteration_index
        )
        challenger_instruction = _strip_stance(authored.instruction)
        usable = bool(challenger_instruction.strip()) and (
            challenger_instruction != champion_before
        )

        # 4) Score the challenger on the SAME rung (only when usable).
        chall_score: Optional[SliceScore] = None
        if usable:
            chall_score = await self._score(
                challenger_instruction,
                role="challenger",
                rung=rung,
                iteration_index=iteration_index,
            )

        # 5) Promote iff the challenger beats the champion on this round's head-to-head.
        promoted = (
            usable and chall_score is not None and self._challenger_wins(champ_score, chall_score)
        )

        if usable and chall_score is not None:
            gains = gain_report(champ_score.triad_score, chall_score.triad_score)
            gain_absolute: Optional[float] = gains["absolute_delta"]
            gain_percent: Optional[float] = gains["percent_delta"]
        else:
            gain_absolute = None
            gain_percent = None

        # 6) Update champion + counters — MONOTONIC.
        self._total_iterations += 1
        self._iterations_at_rung += 1
        if promoted and chall_score is not None:
            self._champion_instruction = challenger_instruction
            self._champion_score = chall_score.triad_score
            self._champion_ci_half_width = chall_score.ci_half_width
            self._improved_at_rung = True
            self._consecutive_non_improving = 0
        else:
            # Kept champion: hold the established score. ONLY seed it if the champion
            # has never been measured at this rung yet (first round / post-escalation
            # re-baseline already set it). Never lower it on a noisy re-measurement —
            # this is the fix for the observed downward drift.
            if self._champion_score is None:
                self._champion_score = champ_score.triad_score
                self._champion_ci_half_width = champ_score.ci_half_width
            self._consecutive_non_improving += 1

        # The carried (monotonic) champion score is what every surface reports.
        carried_score = self._champion_score if self._champion_score is not None else champ_score.triad_score
        carried_ci = (
            self._champion_ci_half_width
            if self._champion_ci_half_width is not None
            else champ_score.ci_half_width
        )

        prompt_diff = (
            make_prompt_diff(champion_before, challenger_instruction) if usable else ""
        )
        self._emitter.iteration_completed(
            model=self._model,
            iteration_index=iteration_index,
            challenger_triad=(chall_score.triad_score if chall_score is not None else None),
            challenger_ci_half_width=(
                chall_score.ci_half_width if chall_score is not None else None
            ),
            gain_absolute=gain_absolute,
            gain_percent=gain_percent,
            accepted=promoted,
            consecutive_non_improving=self._consecutive_non_improving,
            champion_instruction=self._champion_instruction,
            prompt_diff=prompt_diff,
            lookback_version_ids=[],
            island_id=self._island_id,
        )

        # Durable detail: champion_score is the CARRIED (monotonic) value so the chart
        # and the per-iteration record both reflect the belt-holder, not a noisy re-roll.
        self._last_step_detail = StepDetail(
            iteration_index=iteration_index,
            champion_instruction_before=champion_before,
            champion_score=carried_score,
            champion_ci_half_width=carried_ci,
            challenger_instruction=(challenger_instruction if usable else None),
            challenger_score=(chall_score.triad_score if chall_score is not None else None),
            challenger_ci_half_width=(
                chall_score.ci_half_width if chall_score is not None else None
            ),
            challenger_per_dimension=(
                dict(chall_score.per_dimension_mean) if chall_score is not None else {}
            ),
            author_rationale=authored.rationale,
            prompt_diff=prompt_diff,
            promoted=promoted,
            gain_absolute=gain_absolute,
            gain_percent=gain_percent,
            slice_n_conversations=champ_score.n_conversations,
            between_conversation_sd=getattr(champ_score, "between_conv_sd", 0.0),
            mean_closeness=champ_score.mean_closeness,
            abstention_reward_mean=champ_score.abstention_reward_mean,
            answered_when_unsure_rate=champ_score.answered_when_unsure_rate,
            champion_instruction_after=self._champion_instruction,
        )

        return self._snapshot()

    # -- author guard + kickoff-only stance -----------------------------------------------
    async def _author(self, champion_instruction, failures, *, iteration_index: int):
        """Author a challenger under a hard timeout; stance steers the KICKOFF only.

        The island's concise/verbose ``style`` is prepended to the champion the author
        sees ONLY on the very first authoring round (``iteration_index == 0``), to push
        the two islands into maximally different starting shapes. On every subsequent
        round the stance is dropped, so each island optimizes FREELY — it writes its
        best prompt however it sees fit, never handcuffed to a style (owner direction
        2026-06-10; aligns with learning-over-time). The stance, when applied, is
        stripped back out of the author's result by the caller before scoring/storing.
        """
        style = self._style if iteration_index == 0 else ""
        styled_champion = _augment_with_stance(champion_instruction, style)

        def _stream(delta: str) -> None:
            self._emitter.author_token(
                model=self._model,
                iteration_index=iteration_index,
                delta=delta,
                island_id=self._island_id,
            )

        async def _attempt():
            return await self._backend.author.author(
                target_model=self._model,
                champion_instruction=styled_champion,
                failures=failures,
                stream=_stream,
            )

        return await guarded_call(
            f"author:{self._model}:i{self._island_id}:iter{iteration_index}",
            _attempt,
            timeout_s=config.QUALITY_OPT_V3_TIMEOUT_AUTHOR_S,
        )

    # -- contained step ---------------------------------------------------------------------
    async def step(self) -> IslandState:
        """One v2 iteration, contained: ANY failure becomes a skipped iteration.

        On success: reset ``consecutive_failures`` and return v2's state unchanged.
        On :class:`IterationSkipped` or any other exception: keep the champion, advance
        the iteration counters (so patience/tournament cadence still move), bump both
        ``consecutive_non_improving`` and ``consecutive_failures``, clear the step
        detail (the orchestrator persists a position-only record), emit
        ``optimizer_iteration_skipped``, and return the post-skip state. Cooperative
        cancellation is never swallowed.
        """
        try:
            state = await super().step()
        except IterationSkipped as skip:
            return self._record_skip(reason="too_few_survivors", skip=skip, error=None)
        except Exception as exc:  # noqa: BLE001 — containment boundary (island level)
            return self._record_skip(reason="step_error", skip=None, error=exc)
        self.consecutive_failures = 0
        self.last_skip = None
        return state

    def _record_skip(
        self,
        *,
        reason: str,
        skip: Optional[IterationSkipped],
        error: Optional[BaseException],
    ) -> IslandState:
        """Bookkeep + emit one skipped iteration; return the post-skip snapshot."""
        iteration_index = self._total_iterations
        self._total_iterations += 1
        self._iterations_at_rung += 1
        self._consecutive_non_improving += 1
        self.consecutive_failures += 1
        self.last_skip = skip
        self._last_step_detail = None  # position-only durable record for this iteration

        failure_dicts = [f.to_dict() for f in skip.failures] if skip is not None else []
        _LOG.warning(
            "island %s/%d: iteration %d SKIPPED (%s; consecutive_failures=%d)%s",
            self._model, self._island_id, iteration_index, reason, self.consecutive_failures,
            f" error={error!r}" if error is not None else "",
        )
        try:
            self._emitter.emit(
                EVENT_ITERATION_SKIPPED,
                self._model,
                {
                    "island_id": self._island_id,
                    "iteration_index": iteration_index,
                    "rung_index": self._rung_index,
                    "reason": reason,
                    "error": repr(error) if error is not None else None,
                    "survivors": skip.survivors if skip is not None else None,
                    "total": skip.total if skip is not None else None,
                    "failures": failure_dicts,
                    "consecutive_failures": self.consecutive_failures,
                },
            )
        except Exception:  # noqa: BLE001 — observability must never affect the loop
            _LOG.warning("iteration-skipped emit failed; ignoring", exc_info=True)
        return self._snapshot()
