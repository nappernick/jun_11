"""
Candidate multi-turn system-prompt variants for the quality study's optimizer.

The bake-off's :mod:`bakeoff.prompts` tunes a single per-family/per-thinking
instruction. This study goes further for the two quality-target models (Sonnet
4.6 thinking-off, Haiku 4.5): it generates a SMALL SET of multi-turn-aware
variants per model and lets the offline optimizer
(:mod:`bakeoff.quality.optimize`) rank them on per-turn closeness, then runs the
winner. Genuine ranking needs real model outputs, so this module only *defines*
the variants; the optimizer scores them.

What varies across variants (the multi-turn levers, grounded in current guidance)
--------------------------------------------------------------------------------
The base task (answer the internal Travel/Events/Expenses FAQ strictly from the
retrieved fragments, with answerability discipline) and the retrieved context are
held constant — only the *instruction phrasing/structure* changes, exactly as the
bake-off's per-family trade does (``docs/PROMPT_DESIGN.md``). On top of that base,
each variant turns specific multi-turn knobs on or off:

* **conversation-awareness** — an explicit instruction that this is a multi-turn
  conversation and earlier turns + the model's own earlier answers are context to
  carry forward and stay consistent with. (BuilderHub Golden Path "4 Prompt
  engineering": give the model the context of the task and where it sits in the
  workflow. [INTERNAL, PRIMARY])
* **re-grounding each turn** — an instruction to re-check the retrieved fragments
  on every turn rather than drifting onto its own earlier (possibly wrong)
  answers, since the conversational feed-forward means earlier errors are in
  context. (Golden Path "Mitigate hallucination with firm instruction in system
  prompt and RAG". [INTERNAL, PRIMARY])
* **consistency / self-correction** — permission to correct an earlier answer if
  later context shows it was wrong, rather than doubling down for consistency's
  sake. (Anthropic manual-CoT "ask Claude to self-check" for thinking-off models.
  [EXTERNAL, Anthropic, via the Golden Path's "consult each model's guidance".])
* **answerability persistence** — restating the refuse-don't-fabricate contract
  as applying to EVERY turn, since a later response-dependent turn can become
  unanswerable even when turn-1 was answerable.

These are deliberately small, named deltas so the optimizer's leaderboard is
*interpretable*: the winning variant tells the owner which levers actually moved
per-turn closeness, not just "prompt #3 won".

The variants reuse the bake-off's family instruction as the base when one exists
(so the per-family tuning is inherited, not re-litigated), and append the
multi-turn block. ``build_quality_prompt`` then assembles the full prompt exactly
as the runtime does, so what the optimizer scores is what the run sends.
"""
from __future__ import annotations

from dataclasses import dataclass

from bakeoff.prompts import system_instruction_for

__all__ = [
    "PromptVariant",
    "MULTI_TURN_BLOCKS",
    "variants_for_model",
    "quality_system_instruction",
]


@dataclass(frozen=True)
class PromptVariant:
    """One candidate multi-turn system instruction the optimizer ranks.

    ``variant_id`` is stable + human-readable (e.g. ``"base"``,
    ``"conversation_aware+reground"``) so it is what gets recorded on each
    outcome and shown on the leaderboard. ``multi_turn_block`` is the extra
    instruction text appended to the family base (empty for the ``"base"``
    control). ``levers`` names which multi-turn knobs this variant turns on, for
    the interpretable leaderboard.
    """

    variant_id: str
    multi_turn_block: str
    levers: tuple[str, ...]


# The individual multi-turn instruction blocks, each keyed by its lever name.
# Variants are built by composing subsets of these (plus the always-present
# family base), so the optimizer can attribute a closeness gain to a lever.
MULTI_TURN_BLOCKS: dict[str, str] = {
    "conversation_aware": (
        "<conversation>\n"
        "This is a multi-turn conversation. Earlier turns and your own earlier "
        "replies are shown above as context. Read them so your answer fits the "
        "ongoing conversation and stays consistent with what was already "
        "established, while still answering the user's latest message.\n"
        "</conversation>"
    ),
    "reground": (
        "<grounding_each_turn>\n"
        "On every turn, answer from the reference fragments — not from your own "
        "earlier replies. Re-check the fragments before each answer; if an "
        "earlier reply in this conversation conflicts with the fragments, trust "
        "the fragments.\n"
        "</grounding_each_turn>"
    ),
    "self_correct": (
        "<consistency>\n"
        "Stay consistent with your earlier answers when they were correct, but if "
        "later context or the fragments show an earlier answer was wrong, correct "
        "it plainly rather than repeating the mistake to seem consistent.\n"
        "</consistency>"
    ),
    "answerability_persist": (
        "<answerability_every_turn>\n"
        "The grounding contract applies to every turn independently. A follow-up "
        "can ask for something the fragments do not cover even when an earlier "
        "turn was fully answerable: in that case answer what the fragments "
        "support, flag what is missing, and point the user to the right owner "
        "rather than guessing.\n"
        "</answerability_every_turn>"
    ),
}


# The per-model variant set. The two target models share the SAME variant
# structure (a control + single-lever variants + the full stack) so their
# leaderboards are comparable; the family base they build on differs (selected by
# ``family``), which is the per-family tuning the bake-off established. Keeping
# the variant ladder identical across models is deliberate: it isolates "which
# multi-turn levers help THIS model" from "which model is better overall".
_VARIANT_LADDER: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("base", ()),
    ("conversation_aware", ("conversation_aware",)),
    ("conversation_aware+reground", ("conversation_aware", "reground")),
    (
        "conversation_aware+reground+answerability",
        ("conversation_aware", "reground", "answerability_persist"),
    ),
    (
        "full_stack",
        ("conversation_aware", "reground", "self_correct", "answerability_persist"),
    ),
)


def _build_block(levers: tuple[str, ...]) -> str:
    """Join the multi-turn blocks for ``levers`` (in canonical block order)."""
    ordered = [name for name in MULTI_TURN_BLOCKS if name in levers]
    return "\n\n".join(MULTI_TURN_BLOCKS[name] for name in ordered)


def variants_for_model(model_key: str) -> list[PromptVariant]:
    """Return the candidate prompt variants the optimizer ranks for ``model_key``.

    ``model_key`` is one of :data:`bakeoff.config.QUALITY_MODELS` (e.g.
    ``"sonnet-4.6-thinking-off"``). The same variant ladder is returned for every
    model; the family-specific base is folded in at assembly time by
    :func:`quality_system_instruction`, so this just enumerates the ladder.
    """
    return [
        PromptVariant(variant_id=vid, multi_turn_block=_build_block(levers), levers=levers)
        for vid, levers in _VARIANT_LADDER
    ]


def quality_system_instruction(
    *, family: str, thinking_enabled: bool, variant: PromptVariant
) -> str:
    """Compose the full system instruction for a model+variant (no fragments).

    Takes the bake-off family base (so per-family tuning is inherited) and appends
    the variant's multi-turn block. The retrieved fragments are appended later by
    the runtime's prompt assembly (:func:`bakeoff.adapters.base.build_prompt`),
    exactly as in a normal run — this returns only the *instruction* portion, which
    is the part the optimizer is allowed to vary.
    """
    base = system_instruction_for(family, thinking_enabled)
    if not variant.multi_turn_block:
        return base
    return f"{base}\n\n{variant.multi_turn_block}"
