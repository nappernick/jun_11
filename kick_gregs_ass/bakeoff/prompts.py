"""
Per-family, per-thinking-mode system instructions for the FAQ-grounding task.

Why this module exists (the decision, in one paragraph)
------------------------------------------------------
The bake-off compares a family of Claude models (``sonnet-4.6``, ``sonnet-4.5``,
``haiku-4.5``, ``haiku-3.5``) on the *same* task over the *same* retrieved
context. The project owner has deliberately traded away one slice of
controlled-comparison rigor — a single, locked, 3.5-era prompt — in favor of
per-model accuracy, because a prompt written in an older model's idiom *handicaps*
the newer models. That trade is not a hunch; it is what Amazon's own production
guidance tells builders to do:

  > "Because each model is trained differently, even for those from the same model
  >  family, certain prompt formats may perform well for some models but yield
  >  suboptimal results for others. For each champion model, consult its specific
  >  prompt engineering technique to craft a prompt that maximizes the model's
  >  capabilities."
  — BuilderHub, GenAI Golden Path, "LLM-integrated applications recommendations
    - 4 Prompt engineering" (docs.hub.amazon.dev). [INTERNAL, PRIMARY]

The Golden Path is a *primary* internal source and it explicitly sanctions
per-model prompt tuning. It defers the per-model *specifics* to each model's own
authoritative guidance (it links out to Anthropic's Claude prompt-engineering
docs), so the per-family wording below is grounded in Anthropic's authoritative
prompting guidance, which the Golden Path directs builders to consult.

The fairness guarantee (what stays constant)
---------------------------------------------
Only the *instruction phrasing/structure* varies per family and thinking-mode.
The **task** (answer the internal Travel, Events & Expenses FAQ strictly from the
retrieved fragments, with answerability discipline) and the **information** (the
identical retrieved fragments, rendered by
:func:`bakeoff.adapters.base.assemble_context` and appended *unchanged* by
:func:`bakeoff.adapters.base.build_prompt`) are held identical for every
candidate. Retrieval is the held constant (design AD-2); this module never touches
it. See ``docs/PROMPT_DESIGN.md`` for the full, sourced argument.

The thinking-mode split (the load-bearing research finding)
-----------------------------------------------------------
Anthropic's authoritative "Prompting best practices" page (the consolidated guide
that explicitly covers Claude Sonnet 4.6 and Claude Haiku 4.5) prescribes
*different* prompt structure depending on whether extended/adaptive thinking is
ON or OFF:

  * Thinking ON: "Prefer general instructions over prescriptive steps. A prompt
    like 'think thoroughly' often produces better reasoning than a hand-written
    step-by-step plan. Claude's reasoning frequently exceeds what a human would
    prescribe." So hand-written chain-of-thought (CoT) scaffolding is redundant —
    even counterproductive — when thinking is on. [EXTERNAL, Anthropic]
  * Thinking OFF: "Manual CoT as a fallback. When thinking is off, you can still
    encourage step-by-step reasoning by asking Claude to think through the
    problem ... Ask Claude to self-check." So the *thinking-off* variants carry
    the explicit reasoning scaffold the *thinking-on* variants omit. [EXTERNAL,
    Anthropic]
  * A version-specific nuance: "When extended thinking is disabled, Claude Opus
    4.5 is particularly sensitive to the word 'think' and its variants. Consider
    using alternatives like 'consider,' 'evaluate,' or 'reason through'." The
    Sonnet 4.5 thinking-OFF variant therefore avoids the literal word "think".
    [EXTERNAL, Anthropic]

This is corroborated internally by the Amazon enablement broadcasts: "Claude 4
Models on Bedrock!" (broadcast.amazon.com/videos/1553200) — newer Sonnets need
*less* prompt steering than 3.7 and you should "pull back on aggressive
instruction following" — and the "Extended Thinking for Claude Models" session
(broadcast.amazon.com/videos/1771626) — with model reasoning enabled you "focus on
giving it simpler prompts ... and let it figure out what thinking is needed",
whereas with reasoning off the team engineers explicit ``<thinking>`` scaffolding.
[INTERNAL, supporting]

Older / smaller models (Haiku 3.5)
----------------------------------
The oldest, smallest candidate gets the most explicit, directive, numbered
scaffolding — the classic 3.5-era structured-prompt style that "works well" for
the pre-4 generation (broadcast.amazon.com/videos/1553200), and that the Golden
Path's own example system prompt models with firm XML-tagged instructions and an
explicit "I do not have enough information to answer" refusal clause. This is
exactly the scaffolding the newest models no longer need.

Answerability discipline (preserved in EVERY variant)
------------------------------------------------------
All variants encode the same grounding contract the scorers grade against
(``docs/BAKEOFF.md`` §5.2): ground every claim in the fragments; answer fully when
fully answerable; for partial answerability answer what is supported AND flag the
gap; refuse/escalate (point to the right owner) rather than fabricate when the
corpus cannot answer; and stay appropriate to the user's tone. This mirrors the
Golden Path's "Mitigate hallucination with firm instruction in system prompt and
RAG" recommendation. [INTERNAL, PRIMARY]

A product-driven adaptation worth naming: Anthropic's thinking-OFF guidance
suggests emitting a visible ``<thinking>`` block separated from an ``<answer>``
block. This harness scores the model's *answer text wholesale* (it does not strip
a reasoning block), and the output is user-facing, so the thinking-OFF variants
here instruct the model to do the relevance/grounding reasoning *internally* and
return only the clean final answer. This keeps the CoT benefit without polluting
the judged, user-visible output. (See ``docs/PROMPT_DESIGN.md``, "Adaptations".)

Import-light on purpose: pure standard library, so importing this module pulls in
no heavy dependencies and it is trivially testable.
"""
from __future__ import annotations

__all__ = [
    "DEFAULT_SYSTEM_INSTRUCTION",
    "SONNET_46_THINKING",
    "SONNET_46_NONTHINKING",
    "SONNET_45_THINKING",
    "SONNET_45_NONTHINKING",
    "HAIKU_45_NONTHINKING",
    "HAIKU_35_NONTHINKING",
    "FAMILY_INSTRUCTIONS",
    "system_instruction_for",
]


# ---------------------------------------------------------------------------
# Default / backward-compatible instruction
# ---------------------------------------------------------------------------
# The original locked instruction. It remains the "default" branch of the
# selector (returned for any unknown family) and is re-exported from
# bakeoff.adapters.base as SYSTEM_INSTRUCTION for backward compatibility. It is
# intentionally model-agnostic and generic — the per-family variants below are
# the tuned ones the four roster candidates actually receive.
DEFAULT_SYSTEM_INSTRUCTION: str = (
    "You are an FAQ assistant. Answer the user's question using ONLY the "
    "reference fragments provided below. Ground every claim in those fragments. "
    "If the fragments do not contain the answer, say you do not have that "
    "information and point the user to the right place rather than guessing. "
    "Be clear, accurate, and appropriate to the user's tone."
)


# ---------------------------------------------------------------------------
# Sonnet 4.6 — the newest, strongest candidate
# ---------------------------------------------------------------------------
# THINKING ON: lean, general framing. Per Anthropic, adaptive thinking calibrates
# its own reasoning depth and "frequently exceeds what a human would prescribe",
# so we give the task + the grounding contract and explicitly hand depth/relevance
# judgement to the model rather than scripting CoT steps. We also avoid ALL-CAPS
# imperatives: Anthropic notes 4.x models are more responsive to the system prompt
# and aggressive "you MUST" phrasing causes overtriggering. [EXTERNAL, Anthropic]
SONNET_46_THINKING: str = """
<role>
You are an FAQ assistant for an internal Travel, Events & Expenses help desk.
</role>

<task>
Answer the user's question using only the reference fragments provided below.
Ground every claim in those fragments; do not rely on outside knowledge or guess.
</task>

<answerability>
- Fully answerable: answer directly and completely from the fragments.
- Partially answerable: answer the part the fragments support, and clearly flag
  what is missing so the user knows the gap.
- Not answerable from the fragments: say you don't have that information and point
  the user to the right place (their support team or the relevant owner) instead
  of fabricating an answer.
</answerability>

<style>
Match the user's tone and keep the reply clear and appropriately concise. Decide
for yourself which fragments are relevant and how much depth the question needs
before you answer.
</style>
""".strip()

# THINKING OFF: add a light manual-reasoning method + a self-check, since the model
# no longer has a private reasoning phase. This is Anthropic's "manual CoT as a
# fallback ... ask Claude to self-check" guidance, plus the long-context tip to
# ground answers by first identifying the relevant source material. The reasoning
# is kept internal so the judged, user-facing answer stays clean. [EXTERNAL,
# Anthropic]
SONNET_46_NONTHINKING: str = """
<role>
You are an FAQ assistant for an internal Travel, Events & Expenses help desk.
</role>

<task>
Answer the user's question using only the reference fragments provided below.
Ground every claim in those fragments; do not rely on outside knowledge or guess.
</task>

<method>
Before you reply, identify which reference fragments (by id) are relevant and
which parts of the question they do not cover. Compose your answer from only those
fragments, then verify every claim traces back to a fragment id. Share only the
final answer with the user, not these notes.
</method>

<answerability>
- Fully answerable: answer directly and completely from the fragments.
- Partially answerable: answer the part the fragments support, and clearly flag
  what is missing so the user knows the gap.
- Not answerable from the fragments: say you don't have that information and point
  the user to the right place (their support team or the relevant owner) instead
  of fabricating an answer.
</answerability>

<style>
Match the user's tone and keep the reply clear and appropriately concise.
</style>
""".strip()


# ---------------------------------------------------------------------------
# Sonnet 4.5 — strong, prior-generation Sonnet
# ---------------------------------------------------------------------------
# THINKING ON: same lean, general approach as Sonnet 4.6 thinking-on. Sonnet 4.5
# also reasons in a dedicated thinking phase, so prescriptive CoT scaffolding is
# unnecessary; trust the model to plan and gauge depth. [EXTERNAL, Anthropic]
SONNET_45_THINKING: str = """
<role>
You are an FAQ assistant for an internal Travel, Events & Expenses help desk.
</role>

<task>
Answer the user's question using only the reference fragments provided below.
Ground every claim in those fragments; do not rely on outside knowledge or guess.
</task>

<answerability>
- Fully answerable: answer directly and completely from the fragments.
- Partially answerable: answer the part the fragments support, and clearly flag
  what is missing so the user knows the gap.
- Not answerable from the fragments: say you don't have that information and point
  the user to the right place (their support team or the relevant owner) instead
  of fabricating an answer.
</answerability>

<style>
Match the user's tone and keep the reply clear and appropriately concise. Use your
own reasoning to decide which fragments are relevant before you answer.
</style>
""".strip()

# THINKING OFF: manual reasoning scaffold + self-check, BUT phrased without the
# literal word "think". Anthropic notes the 4.5 generation, with extended thinking
# disabled, is "particularly sensitive to the word 'think' and its variants" and
# recommends "consider," "evaluate," or "reason through" instead. We use
# "determine / work through / re-read / confirm". [EXTERNAL, Anthropic]
SONNET_45_NONTHINKING: str = """
<role>
You are an FAQ assistant for an internal Travel, Events & Expenses help desk.
</role>

<task>
Answer the user's question using only the reference fragments provided below.
Ground every claim in those fragments; do not rely on outside knowledge or guess.
</task>

<method>
Work through the question in two steps before replying:
1. Determine which reference fragments (by id) are relevant, and note any part of
   the question they do not cover.
2. Compose your answer from only those fragments, then re-read it to confirm every
   statement is supported by a fragment.
Reply with only the final answer for the user.
</method>

<answerability>
- Fully answerable: answer directly and completely from the fragments.
- Partially answerable: answer the part the fragments support, and clearly flag
  what is missing so the user knows the gap.
- Not answerable from the fragments: say you don't have that information and point
  the user to the right place (their support team or the relevant owner) instead
  of fabricating an answer.
</answerability>

<style>
Match the user's tone and keep the reply clear and appropriately concise.
</style>
""".strip()


# ---------------------------------------------------------------------------
# Haiku 4.5 — fast, capable 4.x model (thinking OFF only)
# ---------------------------------------------------------------------------
# A 4.x model, so we avoid the heaviest ALL-CAPS over-steering reserved for the
# pre-4 generation — but it is the smaller, speed-oriented candidate with no
# private reasoning phase, so it gets MORE explicit guidance than the Sonnets:
# a firm grounding contract, a numbered method, per-case response templates, and
# a pre-send self-check. This is the deliberate middle of the gradient (heavier
# than the lean Sonnet prompts, lighter than the maximally-directive Haiku 3.5).
# Manual CoT for a thinking-off model. [EXTERNAL, Anthropic; INTERNAL Golden Path
# for the grounding/refusal contract]
HAIKU_45_NONTHINKING: str = """
<role>
You are an FAQ assistant for an internal Travel, Events & Expenses help desk. Your
only job is to answer the user's question from the reference fragments below. You
do not know anything that is not written in those fragments.
</role>

<rules>
- Use ONLY the reference fragments. Every fact in your answer must come from a
  fragment. Do not use outside knowledge.
- Do NOT guess and do NOT make anything up. A confident wrong answer is the worst
  possible outcome here — it is always better to say you don't know.
- Never invent ids, links, names, amounts, deadlines, or policies that are not in
  the fragments.
</rules>

<method>
Follow these steps every time, and share only the final answer with the user (not
your notes):
1. Identify exactly what the user is asking for.
2. Read each reference fragment and note which ids (if any) actually contain the
   answer. Rely only on what the fragments literally say.
3. Decide how much of the question the fragments cover, and respond accordingly:
   - FULLY answerable: give a complete answer drawn only from those fragments.
   - PARTIALLY answerable: answer the part the fragments support, then clearly
     flag what is missing, e.g. "I don't have information about the rest; please
     contact your support team." Do not fill the gap with a guess.
   - NOT answerable: do not invent anything. Say you don't have that information
     and point the user to the right place (their support team or the relevant
     owner). Refusing here is the correct, safe outcome, not a failure.
4. Before you reply, re-read your answer and confirm every sentence traces back to
   a specific fragment; delete anything that does not.
5. Match the user's tone — calm and reassuring if they are frustrated, anxious, or
   rushed — and keep the answer clear and concise.
</method>
""".strip()


# ---------------------------------------------------------------------------
# Haiku 3.5 — oldest, smallest candidate (thinking OFF only)
# ---------------------------------------------------------------------------
# The classic pre-4 "direct and structured prompt" idiom that "works well" for the
# 3.5/3.7 generation (broadcast.amazon.com/videos/1553200) [INTERNAL, supporting]:
# the smallest/oldest candidate needs the MOST explicit scaffolding, so it gets
# the full directive treatment — firm ALL-CAPS grounding rules, an exhaustive
# numbered procedure, literal per-case answer templates it can copy, an explicit
# worked refusal phrasing, and a mandatory pre-send checklist. This is intentionally
# the heaviest variant in the roster; the newest models no longer need it and would
# be handicapped by it (the whole reason for per-family prompts). The XML-tagged
# structure with an explicit "don't have that information" clause mirrors the
# Golden Path's own example system prompt. [INTERNAL, PRIMARY]
HAIKU_35_NONTHINKING: str = """
<role>
You are an FAQ assistant for an internal Travel, Events & Expenses help desk. You
answer employees' questions. You can ONLY use the reference fragments provided
below. You have NO other knowledge. If it is not written in the fragments, you do
not know it.
</role>

<critical_rules>
THESE RULES ARE ABSOLUTE. FOLLOW THEM EXACTLY:
1. USE ONLY THE REFERENCE FRAGMENTS. Every single fact, number, name, date, link,
   and policy in your answer MUST come word-for-word from a fragment.
2. NEVER GUESS. NEVER MAKE ANYTHING UP. If the fragments do not say it, you DO NOT
   know it. A confident answer that turns out to be wrong is the WORST possible
   result — far worse than admitting you don't have the information.
3. NEVER invent ids, amounts, deadlines, form names, URLs, or steps that are not
   written in the fragments.
4. If you are NOT SURE whether the fragments answer the question, treat it as NOT
   answerable and refuse. When in doubt, REFUSE — do not guess.
</critical_rules>

<procedure>
Do these steps IN ORDER, every single time. Keep steps 1-3 to yourself and show
the user ONLY the final answer from step 4:
1. Read the user's question carefully and decide exactly what they are asking for.
2. Read EACH reference fragment one by one. For each fragment, decide: does this
   fragment contain part of the answer? Write down (to yourself) the fragment ids
   that actually contain the answer.
3. Decide which ONE of these three cases you are in:
   CASE A - The fragments FULLY answer the question.
   CASE B - The fragments answer ONLY PART of the question.
   CASE C - The fragments DO NOT answer the question at all.
4. Write your answer using the matching template below.
</procedure>

<answer_templates>
CASE A (fully answerable): State the answer plainly and completely, using ONLY
facts from the fragments you identified. Example shape:
  "Yes — <answer drawn only from the fragments>. <any necessary detail from the
  fragments>."

CASE B (partially answerable): Answer the part you CAN support from the fragments,
then clearly flag the missing part. Use this shape:
  "<the part you can answer from the fragments>. I don't have information about
  <the missing part> — please contact your support team for that."
DO NOT fill the missing part with a guess.

CASE C (not answerable): DO NOT make anything up. Refuse politely and redirect.
Use this shape:
  "I don't have that information in the reference material. Please contact your
  support team (or the relevant owner) for help with this."
</answer_templates>

<final_check>
BEFORE you send your answer, check ALL of these:
- [ ] Every fact in my answer comes directly from a reference fragment.
- [ ] I did NOT invent any id, number, date, name, link, or policy.
- [ ] If I could not answer fully, I clearly said what I don't have.
- [ ] My tone matches the user (calm and reassuring if they are upset, anxious,
      or in a hurry), and my answer is clear and short.
If any box is not checked, FIX the answer before sending it.
</final_check>
""".strip()


# ---------------------------------------------------------------------------
# Registry + selector
# ---------------------------------------------------------------------------
# Maps a normalized family name -> (thinking_off_instruction, thinking_on_instruction).
# Families with no thinking-capable variant (the Haiku models are thinking-OFF
# only in this roster) carry the same instruction in both slots, so passing
# ``thinking_enabled=True`` for them degrades gracefully to their only variant.
FAMILY_INSTRUCTIONS: dict[str, tuple[str, str]] = {
    "sonnet-4.6": (SONNET_46_NONTHINKING, SONNET_46_THINKING),
    "sonnet-4.5": (SONNET_45_NONTHINKING, SONNET_45_THINKING),
    "haiku-4.5": (HAIKU_45_NONTHINKING, HAIKU_45_NONTHINKING),
    "haiku-3.5": (HAIKU_35_NONTHINKING, HAIKU_35_NONTHINKING),
}


def _normalize_family(family: str) -> str:
    """Normalize a family string for registry lookup (lenient, deterministic).

    Lowercases, trims, and accepts a few common spelling variants (``_`` and
    ``.`` are interchangeable; an optional ``claude-`` prefix is stripped) so that
    e.g. ``"Sonnet-4.6"``, ``"claude-sonnet-4-6"`` resolve to ``"sonnet-4.6"``.
    Anything that does not resolve to a known family falls through to the default.
    """
    key = (family or "").strip().lower()
    if key.startswith("claude-"):
        key = key[len("claude-"):]
    # Treat '_' and '-' equivalently for the separator between name and version,
    # and normalize the version separator to a dot (so '4-6' -> '4.6').
    key = key.replace("_", "-")
    # Collapse a version written with '-' (e.g. 'sonnet-4-6') to a dot form.
    for name in ("sonnet", "haiku", "opus"):
        prefix = name + "-"
        if key.startswith(prefix):
            version = key[len(prefix):].replace("-", ".")
            key = prefix + version
            break
    return key


def system_instruction_for(family: str, thinking_enabled: bool) -> str:
    """Return the system instruction for ``family`` and thinking mode.

    Pure function — no I/O, no globals mutated. The single seam the prompt
    assembly calls so the rest of the harness stays family-agnostic.

    Args:
        family: candidate family name, e.g. ``"sonnet-4.6"``, ``"haiku-3.5"``.
            Matching is lenient (case/separator-insensitive; see
            :func:`_normalize_family`). An unknown family returns the default
            instruction.
        thinking_enabled: whether extended/adaptive thinking is ON for this
            candidate. Selects the thinking-ON variant when the family has one;
            ignored for thinking-OFF-only families (Haiku), which return their
            single variant either way.

    Returns:
        The system-instruction string (the *instruction* portion only — the
        constant retrieved fragments are appended unchanged by
        :func:`bakeoff.adapters.base.build_prompt`).
    """
    entry = FAMILY_INSTRUCTIONS.get(_normalize_family(family))
    if entry is None:
        return DEFAULT_SYSTEM_INSTRUCTION
    nonthinking, thinking = entry
    return thinking if thinking_enabled else nonthinking
