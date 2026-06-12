"""
Failure selection for the closed-loop prompt optimizer (design "Component 4:
FailureSelector"; Req 1.3, 3.1, 3.4, 14.4, 14.6).

Once :class:`bakeoff.quality.optimizer.judge_loop.JudgeInLoopScorer` has scored the
current Champion on the Tuning_Slice, the loop needs the Champion's *worst* judged turns
to hand to the Author as the concrete, evidence-backed failures that should drive the next
rewrite (Req 1.3 / 3.1). This module is that one pure step: :func:`select_failures` takes a
:class:`~bakeoff.quality.optimizer.judge_loop.SliceScore` and returns the ``min(k, n)``
worst :class:`~bakeoff.quality.optimizer.judge_loop.TurnVerdict`\\ s, deterministically
ordered. ``k`` defaults to ``config.QUALITY_OPT_FAILURES_K`` so the Author's view of the
Champion's failures is operator-tunable (Req 3.4).

Ordering — abstention failures first (Req 14.4 / 14.6). Each returned verdict already
carries the judge's per-dimension scores, quoted ``evidence``, and the
``grounding_fragment_ids`` of the fragments the model received, so no extra work is needed
to make a failure actionable for the Author. What this module adds is the *priority*:
turns where the model **answered when it should have abstained**
(``answered_when_unsure == True``) sort ahead of ordinary low-triad turns, so the
hallucination / over-claim / unsupported-answer failures Req 14 cares about are surfaced
prominently to the Author rather than being buried under generically low scores. Within
each of those two groups, and to break every tie, verdicts sort by ascending ``overall``
(the abstention-weighted triad — the worst turns first) and then by the deterministic
``(item_id, rep, turn)`` key, so the selection is a total order: identical inputs always
yield identical output (design Property 4).

Concretely the sort key is::

    (0 if v.answered_when_unsure else 1, v.overall, v.item_id, v.rep, v.turn)

— a leading 0/1 group flag puts answering-when-unsure turns first, then ascending
``overall`` orders the remaining (and within-group) turns worst-first, with
``(item_id, rep, turn)`` as the final deterministic tie-break.

This module is pure (no I/O, no network, no global state); the only configuration it reads
is the default ``k`` from :mod:`bakeoff.config`.
"""
from __future__ import annotations

from typing import List

from bakeoff import config
from bakeoff.quality.optimizer.judge_loop import SliceScore, TurnVerdict

__all__ = ["select_failures"]


def select_failures(score: SliceScore, *, k: int = config.QUALITY_OPT_FAILURES_K) -> List[TurnVerdict]:
    """Return the ``min(k, n)`` worst judged turns of ``score``, abstention failures first.

    ``score`` is a :class:`~bakeoff.quality.optimizer.judge_loop.SliceScore`; its
    ``verdicts`` are every per-turn :class:`~bakeoff.quality.optimizer.judge_loop.TurnVerdict`
    produced when the Champion was scored on the Tuning_Slice. The returned verdicts are the
    ones handed to the Author as the driving failures (Req 1.3 / 3.1); each already carries
    the judge's per-dimension scores, quoted ``evidence``, and ``grounding_fragment_ids``,
    so the caller needs no further enrichment.

    Ordering (Req 14.4 / 14.6, design Property 4): answering-when-unsure turns
    (``answered_when_unsure == True``) sort ahead of all other turns, then by ascending
    ``overall`` (worst abstention-weighted triad first), with ``(overall, item_id, rep,
    turn)`` breaking ties deterministically. The slice is then truncated to the first
    ``min(k, n)`` verdicts.

    ``k`` defaults to ``config.QUALITY_OPT_FAILURES_K`` (Req 3.4). A non-positive ``k``
    yields an empty list, and a ``k`` larger than the number of verdicts simply returns all
    of them (the ``min(k, n)`` guarantee).
    """
    if k <= 0:
        return []
    ordered = sorted(
        score.verdicts,
        key=lambda v: (
            0 if v.answered_when_unsure else 1,
            v.overall,
            v.item_id,
            v.rep,
            v.turn,
        ),
    )
    return ordered[:k]
