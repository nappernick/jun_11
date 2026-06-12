"""
Answerability scorer — the first-class abstention dimension (Task 7, Req 5).

Answerability is scored **separately and never blended into accuracy** (design
"Answerability handling", Req 5). The cardinal real-world failure for an FAQ bot is
*confidently fabricating an answer to a question the corpus cannot answer*, so this
module turns the model's answer text + the item's ``answerability`` label into the
two 0/1 signals the event schema carries:

* ``answerability == "none"``  → ``abstention_correct ∈ {0,1}`` — 1 iff the model
  refused/escalated **without fabricating**; 0 is a fabrication, which feeds the
  per-model **fabrication-on-unanswerable rate** (``1 - mean(abstention_correct)``
  over the ``none`` stratum). This is the "don't ship the confident liar" guardrail.
* ``answerability == "partial"`` → ``abstention_correct ∈ {0,1}`` — 1 iff the model
  **answered the answerable part *and* flagged the unanswerable gap**; both
  over-claiming (answered, no gap flag) and over-refusing (pure refusal) score 0.
* ``answerability == "full"`` → ``unwarranted_refusal ∈ {0,1}`` — 1 iff the model
  refused an answerable question (a model that refuses answerable questions is
  useless).

The detector is **deterministic, pure, and network-free** (no judge call needed),
so it is trivially unit-testable for each answerability class against fixtures —
including the two :class:`bakeoff.adapters.mock.MockAdapter` misbehavior profiles
(``fabricate_on_unanswerable`` → ``abstention_correct == 0``; ``refuse_on_answerable``
→ ``unwarranted_refusal == 1``). It reuses the mock's published refusal/gap markers
(:data:`bakeoff.adapters.mock.REFUSAL_MARKERS` / ``GAP_FLAG_MARKER``) as the contract
so the adapter and the scorer cannot drift apart, and augments them with the common
natural-language refusal/escalation phrasings a real model uses.

Import-light: pure stdlib + the mock's marker constants. No network, no Bedrock.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from bakeoff.adapters.mock import GAP_FLAG_MARKER, REFUSAL_MARKERS

__all__ = [
    "ANSWERABILITY_CLASSES",
    "REFUSAL_SIGNATURES",
    "GAP_FLAG_SIGNATURES",
    "is_refusal",
    "flags_gap",
    "has_substantive_answer",
    "score_answerability",
]

#: The three answerability classes (the precondition domain of the scorer).
ANSWERABILITY_CLASSES: tuple[str, ...] = ("full", "partial", "none")

# Case-insensitive substrings that mark a refusal / escalation. Seeded from the
# mock adapter's published markers (so the adapter and scorer share one contract)
# and extended with the common ways a real model declines or escalates. Kept as a
# module constant (not inlined) so the pipeline/tests can tune or inspect it.
REFUSAL_SIGNATURES: tuple[str, ...] = tuple(
    dict.fromkeys(  # dedupe while preserving order
        [m.lower() for m in REFUSAL_MARKERS]
        + [
            "i don't have that information",
            "i do not have that information",
            "i don't have information",
            "i do not have information",
            "i'm not able to find",
            "i am not able to find",
            "i couldn't find",
            "i could not find",
            "i'm unable to",
            "i am unable to",
            "i don't have access",
            "i do not have access",
            "isn't covered in the reference",
            "is not covered in the reference",
            "not in the reference material",
            "i can't answer",
            "i cannot answer",
            "please contact",
            "please reach out",
            "reach out to",
            "contact your support",
            "contact support",
            "escalate this",
        ]
    )
)

# Case-insensitive substrings that mark "I answered what I can, but flagged a gap"
# — the partial-class reward signal. Seeded from the mock's gap-flag marker.
GAP_FLAG_SIGNATURES: tuple[str, ...] = tuple(
    dict.fromkeys(
        [GAP_FLAG_MARKER.lower()]
        + [
            "i don't have information about the rest",
            "i do not have information about the rest",
            "for the rest",
            "the remaining part",
            "the other part",
            "i'm not able to find the rest",
            "however, i don't have",
            "however, i do not have",
            "but i don't have",
            "but i do not have",
            "for the part i can't",
            "for the part i cannot",
            "please contact",
        ]
    )
)

# Minimum number of word characters for an answer to count as "substantive"
# (used for the partial class: answering the answerable part). A bare refusal or
# empty string is not substantive.
_MIN_SUBSTANTIVE_WORDS: int = 4
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'-]*")


def _normalize(answer: Optional[str]) -> str:
    """Lower-cased, whitespace-collapsed answer text (``""`` for ``None``)."""
    if not answer:
        return ""
    return re.sub(r"\s+", " ", answer).strip().lower()


def is_refusal(answer: Optional[str]) -> bool:
    """True iff the answer reads as a refusal / escalation rather than an attempt.

    Deterministic substring match against :data:`REFUSAL_SIGNATURES` over the
    normalized text. Empty/whitespace answers are treated as a (degenerate)
    refusal — the model produced nothing usable.
    """
    text = _normalize(answer)
    if not text:
        return True
    return any(sig in text for sig in REFUSAL_SIGNATURES)


def flags_gap(answer: Optional[str]) -> bool:
    """True iff the answer explicitly flags an unanswerable gap (partial reward)."""
    text = _normalize(answer)
    if not text:
        return False
    return any(sig in text for sig in GAP_FLAG_SIGNATURES)


def has_substantive_answer(answer: Optional[str]) -> bool:
    """True iff the answer contains a substantive attempt (not just a refusal).

    Used by the partial class to check the model actually answered the answerable
    portion. An answer counts as substantive when it has at least
    :data:`_MIN_SUBSTANTIVE_WORDS` words **beyond** any refusal/gap-flag boilerplate
    — i.e. it said something concrete, not only "I don't have the rest".
    """
    text = _normalize(answer)
    if not text:
        return False
    # Strip out the refusal/gap boilerplate, then see if real content remains.
    stripped = text
    for sig in (*REFUSAL_SIGNATURES, *GAP_FLAG_SIGNATURES):
        stripped = stripped.replace(sig, " ")
    return len(_WORD.findall(stripped)) >= _MIN_SUBSTANTIVE_WORDS


def score_answerability(
    answer: Optional[str],
    answerability: str,
    *,
    refusal_detector: Callable[[Optional[str]], bool] = is_refusal,
    gap_detector: Callable[[Optional[str]], bool] = flags_gap,
    substantive_detector: Callable[[Optional[str]], bool] = has_substantive_answer,
) -> dict[str, int]:
    """Score behavior on the answerability dimension; never blend classes.

    The detectors are injectable so a caller can swap in a judge-backed or stricter
    detector without changing the scoring logic; the defaults are the deterministic
    heuristics in this module.

    Args:
        answer: the model's answer text.
        answerability: one of :data:`ANSWERABILITY_CLASSES`.
        refusal_detector: predicate "is this answer a refusal/escalation?".
        gap_detector: predicate "does this answer flag an unanswerable gap?".
        substantive_detector: predicate "did this answer say something concrete?".

    Returns:
        ``{"abstention_correct": 0|1}`` for ``none``/``partial``; or
        ``{"unwarranted_refusal": 0|1}`` for ``full``. Exactly one key, matching
        which :class:`bakeoff.types.AccuracyScores` field is populated for the class
        (and the :class:`~bakeoff.types.TrialEvent` validation rule).

    Raises:
        ValueError: if ``answerability`` is not one of :data:`ANSWERABILITY_CLASSES`.
    """
    if answerability not in ANSWERABILITY_CLASSES:
        raise ValueError(
            f"answerability must be one of {ANSWERABILITY_CLASSES}, got {answerability!r}"
        )

    refused = refusal_detector(answer)

    if answerability == "none":
        # Correct = refused/escalated WITHOUT fabricating. A non-refusal on an
        # unanswerable item is a fabrication (abstention_correct == 0), which the
        # aggregation surfaces as the fabrication-on-unanswerable rate.
        return {"abstention_correct": 1 if refused else 0}

    if answerability == "partial":
        # Correct = answered the answerable part AND flagged the gap. A pure
        # refusal over-refuses (``answered`` is False after boilerplate is
        # stripped); a substantive answer with no gap flag over-claims; only
        # answer-and-flag scores 1.
        answered = substantive_detector(answer)
        flagged = gap_detector(answer)
        return {"abstention_correct": 1 if (answered and flagged) else 0}

    # answerability == "full": standard accuracy elsewhere; here we only flag an
    # UNWARRANTED refusal of an answerable question.
    return {"unwarranted_refusal": 1 if refused else 0}
