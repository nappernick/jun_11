"""Repo-baked modern Claude 4.5 system-prompting guidance (Req 15).

This module holds a *curated, version-controlled* summary of modern Claude 4.5
system-prompting practice, used two ways:

* the full :data:`PROMPTING_GUIDANCE` is injected into the Author model's context
  on every Challenger request (Req 15.1), and
* the shorter :data:`GROUNDING_ABSTENTION_EXCERPT` may be handed to the Judge so
  its grounding/abstention evaluation stays consistent with the guidance the
  Author follows (Req 15.4).

Provenance and sourcing honesty (Req 15.3, Req 15.6):

* This text is *derived from* ``docs/modern_system_prompting.pdf`` — a Claude 4.5
  prompt-engineering analysis. The content here is a hand-curated, self-contained
  summary baked in as string literals; the raw PDF is **never** read or parsed at
  runtime (no PDF reader import, no ``open()`` of the PDF). The PDF is the source
  a human consulted while writing these constants, not a runtime dependency.
* This is an **EXTERNAL / VENDOR-sourced** reference (a third-party Claude 4.5
  prompt-engineering analysis), **NOT** an Amazon-internal primary source. It is
  recorded as such per the study's sourcing-and-methodology honesty caveat.
* It is applicable to this study because both Target_Models (``haiku-4.5`` and
  ``sonnet-4.6``) are members of the same Claude 4.x family the source analyzes,
  so its structural and behavioral guidance transfers directly.
"""

PROMPTING_GUIDANCE: str = """\
# Modern Claude 4.5 system-prompting guidance (external/vendor-sourced)

This is a curated reference for authoring system prompts (instructions) for the
Claude 4.x family (e.g. Sonnet 4.5/4.6, Haiku 4.5). Write the instruction to read
like a small, layered "constitution": clear named sections, calm declarative
language, and explicit grounding and decline rules. Both target models share the
same Claude 4.x prompt format and respond to it the same way, so a single
well-structured instruction serves both.

## 1. XML / tagged, layered prompt structure
Prefer a layered prompt built from named, tagged sections over one flat blob.
Claude 4.x is trained on a `<behavior_instructions>`-style format whose
subsections each serve one purpose, for example:
- `<role>` / identity and scope: who the assistant is and what it is answering.
- `<grounding_rules>`: where answers must come from (the supplied evidence).
- `<refusal_handling>` / abstention: when and how to decline.
- `<tone_and_formatting>`: how the output should read.
Tagged sections are easier for the model to parse, easier to audit, and easy to
extend or patch one section at a time without rewriting the whole prompt. Keep
each section short and single-topic; let the section boundaries carry the
structure rather than piling everything into one paragraph.

## 2. Refusal / abstention handling (explicit, reliable decline path)
Encode the decline path explicitly rather than hoping the model infers it. State
the conditions under which the model should abstain and the exact shape of the
response. A reliable rule to abstain reads roughly: "If the supplied evidence
does not contain enough information to answer, say so plainly and abstain — that
is, decline to answer rather than guessing; do not fabricate a fragment or a
fact." Keep the refusal tone
calm, brief, and polite (a short "I don't have enough information to answer
that" beats a long apology or a policy dump). Declining when unsupported is the
correct, desired behavior — phrase it as a first-class instruction, not an
afterthought, so the model treats "decline when unsupported" as a normal,
expected outcome rather than a failure.

## 3. Tone and formatting control
Control style explicitly: specify prose vs. lists, heading and markdown use,
emoji, and verbosity. A common, effective default is natural flowing prose,
sparing markdown, and "right-sized" answers — concise for simple asks, thorough
for complex ones. Phrase cosmetic rules as overridable defaults ("use prose
unless the user asks for a list") so genuine user formatting requests still win.
Explicit formatting rules prevent "prompt drift," where long conversations
otherwise decay into messy or inconsistent output.

## 4. Knowledge-grounding (answer strictly from supplied evidence)
For a grounded/RAG task, make the supplied evidence the *only* source of truth.
Instruct the model to answer strictly from the retrieved fragments rendered in
its context, to attribute each claim to the fragment that supports it, and to
NOT use outside or training knowledge to fill gaps. If the fragments are silent
or insufficient on a point, that triggers the abstention path above rather than a
guess. Grounding and abstention are two halves of one rule: use the evidence when
it is there; decline when it is not.

## 5. Steerability
Claude 4.x is highly steerable through the system layer. Put authoritative,
non-negotiable rules (grounding, abstention, safety) in clear declarative
instructions, and phrase cosmetic preferences as user-overridable defaults. Small
wording changes in the instruction produce noticeable behavior changes, so steer
deliberately: say exactly what you want, in plain language, in the right section.
Structured, sectioned instructions are the lever — use them to shape behavior
precisely instead of repeating or shouting a single rule.

## 6. The Claude 4.x responsiveness caution (avoid ALL-CAPS over-triggering)
Because Claude 4.x models are *highly* responsive to the system prompt,
over-aggressive language back-fires. You do NOT need ALL-CAPS "MUST" / "CRITICAL"
/ "ALWAYS" phrasing or repeated emphatic commands — with 4.x they over-trigger,
making the model rigid, over-cautious, or prone to unwanted behaviors (e.g.
over-refusing, or dumping a thinking trace when a prompt over-stresses words like
"think"). A normal, calm instruction is enough and is more reliable. Replace
shouted imperatives with clear, calm, well-structured statements placed in the
right section. Be explicit and specific; do not be loud.

## 7. Be clear and direct, and motivate the rules (explain the "why")
Write each rule the way you would brief a capable new colleague who lacks your
context: say specifically what you want, and give the reason behind it. Claude
generalizes well from a short explanation — "answer only from the fragments,
because a confident answer that isn't supported by the evidence is worse here
than admitting the gap" steers behavior more reliably than the bare rule alone,
because the model can apply the intent to cases the rule did not spell out.
Motivated rules also resist edge-case drift better than a long list of flat
prohibitions. Golden test: if a colleague reading the instruction cold would be
unsure what to do, the model will be too — so tighten it.

## 8. Show the behavior with a few concrete examples (few-shot)
Examples are one of the most reliable ways to lock in format, tone, and the
decline behavior — often more reliable than describing the behavior in the
abstract. Include a small number (about two to four) of short, concrete examples
of the desired behavior, each wrapped in an `<example>` tag, and make them
diverse: at least one fully-answerable turn, one partially-supported turn (answer
the supported part, flag the rest), and one unanswerable turn that shows the exact
decline phrasing you want. Keep each example tight — a representative case, not a
transcript — and vary them so the model learns the principle rather than copying
one surface pattern. A single example of a good, calm decline is worth several
sentences telling the model to decline.

## 9. Say what to do, not what only what not to do
Prefer positive, do-this instructions over a pile of don'ts. "Compose the answer
in clear, flowing prose, citing the fragment each claim comes from" steers better
than "do not use markdown, do not cite outside sources, do not ...". State the
prohibition where it genuinely matters (e.g. "do not invent fragments or facts
not in the evidence"), but lead with the behavior you want — the model follows a
clear positive target more reliably than it avoids a long list of negatives.
"""

GROUNDING_ABSTENTION_EXCERPT: str = """\
# Grounding and abstention guidance (for consistent evaluation)

A well-grounded, abstention-aware answer on a retrieval task should satisfy both
of the following, and should be judged accordingly:

## Knowledge-grounding
- Answer strictly from the supplied/retrieved evidence fragments that appear in
  context. The fragments are the only source of truth for the answer.
- Attribute each substantive claim to the fragment that supports it.
- Do NOT use outside or training knowledge to fill gaps, and do NOT invent
  fragments, citations, or facts not present in the evidence.

## Abstention (declining when unsupported)
- When the supplied fragments do not contain enough information to answer, the
  correct behavior is to abstain — say so plainly and decline, rather than
  guessing.
- A correct, explicit decline on an insufficient or unanswerable turn is the
  desired outcome and should score at least as high as — not be penalized
  relative to — an unsupported answer that guesses.
- Conversely, answering confidently when the evidence is insufficient
  (answering-when-unsure) is the failure to penalize.
- Retrieval-always does not mean answer-always: the model may receive fragments
  on every turn and still correctly decline when they are insufficient.
- Keep the decline calm and brief; a short "the evidence doesn't cover that"
  is preferred over a long apology.
"""
