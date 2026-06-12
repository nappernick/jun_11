"""
PhaseBValidator — the validate phase of the closed-loop prompt optimizer (design
Component 8 "PhaseBValidator" / "Two-phase train/test"; Req 7.3, 7.4, 7.5, 7.7).

Phase A iterates the champion/challenger loop on the held-out ~20% Tuning_Slice until the
stop rule fires, producing a *converged Champion* prompt per Target_Model. Phase B — this
module — takes that converged Champion and scores it **once** on the reserved ~80%
Validation_Set (the split complement) to produce the **final reported** triad score and
its confidence interval. The strict train/test boundary the design mandates means Phase A
touches **only** the ``heldout`` slice and Phase B touches **only** the ``remainder``
complement; the two never overlap (design Property 11). The caller (the orchestrator)
owns the deterministic seeded ``split_items`` and hands this validator the ``remainder``
as ``validation_items`` — this module scores exactly the items it is given and never
re-derives or widens the split.

What Phase B changes relative to Phase A:

- **Higher reps (Req 7.4).** Phase B re-uses :class:`JudgeInLoopScorer` exactly as Phase A
  does, but at ``config.QUALITY_OPT_PHASE_B_REPS`` repetitions per item — strictly greater
  than ``config.QUALITY_OPT_PHASE_A_REPS`` (an invariant asserted at config import time).
  More conversations means a tighter between-conversation CI on the one number that gets
  reported.
- **No Author, ever (Req 7.7).** Phase B is pure scoring. The Author is never invoked here
  and no Validation_Set conversation is ever exposed to it. :class:`JudgeInLoopScorer`
  only needs the answer adapter / judge / closeness / retrieval seam of the backend, so
  this validator never even references the backend's ``author``.
- **Final reported value (Req 7.5).** The number this module returns is *always* the
  reported performance for the Target_Model. The Tuning_Slice (Phase A) score is never
  reported as final; it is only the in-loop decision signal.

Retrieval-always is unchanged (Req 13): :class:`JudgeInLoopScorer` invokes the
held-constant, read-only :class:`RetrievalBackend` on every turn, renders the fragments
inline into the visible prompt, and threads the same fragments into the judge as the
grounding evidence — Phase B inherits all of that by reusing the scorer rather than
re-implementing scoring.

Backend injection. The backend is taken as an **injected, duck-typed** bundle (the same
object :class:`JudgeInLoopScorer` consumes — it needs ``answer_adapter_factory``,
``judge_scorer``, ``closeness_scorer`` and ``retrieval``). This module never hard-imports
``backends.py`` so there is no import cycle and it works identically with the offline
bundle (zero network) or the live bundle. The backend's ``name`` (``"offline"`` |
``"live"``) is recorded on every :class:`PhaseBResult` so a reader can tell which backend
produced the final number (Req 10.6).

Sourcing caveat (carried from requirements.md / design.md): the judge triad as the signal
and the between-conversation-SD / CI reasoning behind the reported confidence interval are
grounded in external/industry RAG-evaluation practice and this repo's own observed Opus
verdicts, **not** in Amazon-internal primary sources; re-validate any judge-derived number
against internal guidance before using it to defend a decision upward.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from bakeoff import config
from bakeoff.quality.optimizer.judge_loop import JudgeInLoopScorer
from bakeoff.types import Item

__all__ = [
    "PhaseBResult",
    "PhaseBValidator",
]


@dataclass(frozen=True)
class PhaseBResult:
    """The final, reported Phase B outcome for one Target_Model (design Component 8).

    This is the number the Quality_Study reports for ``model``: the converged Champion's
    abstention-weighted triad mean on the Validation_Set, with its 95% confidence interval
    (``ci_half_width`` / ``ci_low`` / ``ci_high``) computed from the between-conversation
    SD over ``n_conversations`` at ``reps`` repetitions per item. Because the train/test
    boundary is strict, this value is always measured on the reserved complement the prompt
    was *not* tuned on, and it is **always** the final reported value (Req 7.5); the
    Tuning_Slice (Phase A) score is never reported as final. ``backend`` records the backend
    identity (``"offline"`` | ``"live"``) that produced the number (Req 10.6).
    """

    model: str
    champion_instruction: str
    triad_score: float
    ci_half_width: float
    ci_low: float
    ci_high: float
    n_conversations: int
    reps: int
    backend: str


class PhaseBValidator:
    """Score a converged Champion on the Validation_Set — the final reported number.

    The backend is **duck-typed** (the same bundle :class:`JudgeInLoopScorer` consumes); it
    is injected so this module never hard-imports the backend bundle and works identically
    offline or live. ``abstention_weight`` is threaded into the reused scorer so Phase B
    applies the same abstention-weighted aggregation (Req 14) as Phase A — only the rep
    count and the slice differ between the two phases.
    """

    def __init__(
        self,
        backend,
        *,
        abstention_weight: float = config.QUALITY_OPT_ABSTENTION_WEIGHT,
    ) -> None:
        self._backend = backend
        self._abstention_weight = float(abstention_weight)

    async def validate(
        self,
        *,
        model: str,
        champion_instruction: str,
        validation_items: Sequence[Item],
        reps: int = config.QUALITY_OPT_PHASE_B_REPS,
        max_concurrency: Optional[int] = None,
    ) -> PhaseBResult:
        """Score the converged Champion on the Validation_Set ONLY and return a :class:`PhaseBResult`.

        ``validation_items`` MUST be the reserved ~80% complement (the ``remainder`` from
        the deterministic seeded ``split_items`` — Req 7.3); this validator scores exactly
        those items and never re-derives the split, so the strict Phase A (tuning) / Phase B
        (validation) boundary is preserved (design Property 11). Scoring reuses
        :class:`JudgeInLoopScorer` at ``reps`` repetitions per item — defaulting to
        ``config.QUALITY_OPT_PHASE_B_REPS``, which is strictly greater than the Phase A rep
        count (Req 7.4) so the reported CI is tighter than the in-loop one.

        The Author is **never** invoked here (Req 7.7): Phase B is pure scoring of the
        Champion (``prompt_role="champion"``), and the returned triad is **always** the
        final reported value for ``model`` (Req 7.5). The backend identity is recorded on
        the result so a reader can tell which backend produced the number (Req 10.6).
        """
        scorer = JudgeInLoopScorer(
            self._backend,
            reps=reps,
            abstention_weight=self._abstention_weight,
        )
        score = await scorer.score_prompt(
            model=model,
            instruction=champion_instruction,
            items=validation_items,
            prompt_role="champion",
            max_concurrency=max_concurrency,
        )
        return PhaseBResult(
            model=model,
            champion_instruction=champion_instruction,
            triad_score=score.triad_score,
            ci_half_width=score.ci_half_width,
            ci_low=score.ci_low,
            ci_high=score.ci_high,
            n_conversations=score.n_conversations,
            reps=reps,
            backend=str(getattr(self._backend, "name", "unknown")),
        )
