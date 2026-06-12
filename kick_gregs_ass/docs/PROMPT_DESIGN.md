# Per-Family Prompt Design for the Candidate Generator

How the candidate-generation prompt is designed across the Claude model family in
the bake-off, why it is tuned **per model family and per thinking-mode** rather
than locked to one wording, and the exact guarantee that keeps the comparison
fair. The argument is grounded in Amazon-internal primary sources where they
exist, and explicitly falls back to Anthropic's authoritative model-specific docs
(which the internal sources direct builders to consult) for the version-specific
details the internal sources do not cover.

> **Companion docs.** The experiment (what + why) is `docs/BAKEOFF.md`; the
> scoring/judge framing (the RAG triad this prompt is graded against) is
> `docs/Building Short-Lived Calibration Judges for a RAG + QA System...md`. The
> code is `bakeoff/prompts.py` (the per-family instructions + selector) and
> `bakeoff/adapters/base.py` (`build_prompt`, which appends the constant retrieved
> context unchanged).

---

## TL;DR

- **Decision: tune the candidate-generation prompt per model FAMILY and per
  THINKING-MODE, not one locked prompt.** A single prompt written in an older
  model's idiom (3.5-era directive, numbered chain-of-thought scaffolding)
  measurably *handicaps* the newer models, which are tuned to do that reasoning
  themselves. Amazon's own production guidance says to do exactly this: *"Because
  each model is trained differently, even for those from the same model family,
  certain prompt formats may perform well for some models but yield suboptimal
  results for others. For each champion model, consult its specific prompt
  engineering technique..."* — BuilderHub GenAI Golden Path [**INTERNAL,
  PRIMARY**].
- **The load-bearing split is thinking ON vs OFF.** With extended/adaptive
  thinking ON, Anthropic says to *omit* hand-written chain-of-thought (CoT)
  scaffolding — *"Prefer general instructions over prescriptive steps. A prompt
  like 'think thoroughly' often produces better reasoning than a hand-written
  step-by-step plan."* With thinking OFF, the same guidance says to *add* it back
  — *"Manual CoT as a fallback... Ask Claude to self-check."* [**EXTERNAL,
  Anthropic**]. This is corroborated by two internal Bedrock enablement sessions
  [**INTERNAL, supporting**].
- **The fairness guarantee is exact and tested.** Only the *instruction phrasing
  and structure* vary per family/mode. The **task** (ground in the retrieved
  fragments; answerability discipline) and the **information** (the identical
  retrieved fragments) are byte-for-byte identical for every candidate. Retrieval
  is the held constant (`docs/BAKEOFF.md` §2, design AD-2); this design never
  touches it. A unit test asserts the rendered fragments block is identical across
  all families and both thinking modes.
- **Where the internal trail runs out, the doc says so.** The internal *primary*
  sources sanction per-model tuning and the grounding/refusal contract, but they
  do **not** give Claude-version-specific prompt wording (Sonnet 4.6 vs 4.5, Haiku
  4.5 vs 3.5) or the thinking-on-vs-off CoT distinction. Those specifics rest on
  Anthropic's **external** authoritative docs, corroborated by **internal
  supporting** (non-primary) broadcasts and platform wikis. Every such claim is
  labelled below.

---

## 1. The decision: per-family prompts over one locked prompt

### 1.1 What we are choosing between

- **Option A — one locked prompt for all candidates.** Maximizes
  controlled-comparison purity: every model gets a byte-identical system
  instruction, so any difference is "the model." The cost: the single wording is
  necessarily written in *some* model's idiom. Written in the 3.5-era directive
  style, it over-scaffolds the newer Sonnets (which are tuned to reason on their
  own and to follow literal instructions, so redundant CoT and ALL-CAPS imperatives
  can *degrade* them). Written in the 4.6 idiom, it under-specifies for Haiku 3.5,
  which benefits from explicit, numbered structure.
- **Option B — per-family, per-thinking-mode prompts; task + information held
  constant.** Sacrifices a slice of comparison purity (the instruction wording is
  no longer identical) in exchange for each family answering at its own best
  grounded behavior. The task and the retrieved context stay identical, so the
  comparison is still "which model serves this FAQ task best," just with each model
  prompted in its own dialect rather than a foreign one.

### 1.2 The recommendation, and the cited basis for it

**Recommended: Option B (per-family, per-thinking-mode), with the fairness
guarantee in §3.** The project owner has explicitly chosen to trade a slice of
controlled-comparison rigor for per-model accuracy, and this is not merely a
preference — it is what Amazon's production guidance prescribes:

> *"Consult prompt engineering guidance from each model. Because each model is
> trained differently, even for those from the same model family, certain prompt
> formats may perform well for some models but yield suboptimal results for
> others. For each champion model, consult its specific prompt engineering
> technique to craft a prompt that maximizes the model's capabilities."*
> — BuilderHub, GenAI Golden Path, "LLM-integrated applications recommendations -
> 4 Prompt engineering" [**INTERNAL, PRIMARY**]
> (`docs.hub.amazon.dev/docs/golden-path/llm-integrated-applications/recommendation/design/4-prompt-engineering/`)

The Golden Path is a primary internal source (BuilderHub), and it explicitly (a)
sanctions per-model prompt tuning even *within* a family, and (b) defers the
per-model specifics to each model's own authoritative technique, linking out to
*"Claude family model prompt engineering technique"* (Anthropic's docs). So the
chain of authority is: an internal primary source authorizes per-model prompts
and points to Anthropic's model-specific guidance for the wording. That is exactly
the structure of this design.

The internal Bedrock enablement session for the Claude 4 launch makes the
handicap concrete:

> Paraphrased: where on 3.7 you used "direct and structured prompts," those still
> work on Claude 4, but because of the investment in agentic and extended-thinking
> capabilities you should "lean in" differently; many recommendations are to *pull
> back on aggressive instruction following and see what these models can do out of
> the box.* — "Claude 4 Models on Bedrock!" enablement broadcast [**INTERNAL,
> supporting**] (`broadcast.amazon.com/videos/1553200`). *Content rephrased for
> compliance with licensing restrictions.*

In other words: a prompt that aggressively steers (the older idiom) leaves
performance on the table on the newer models. That is the precise failure mode a
single locked, older-idiom prompt would bake into the bake-off, and the reason the
per-family path is the correct one here.

---

## 2. Per-family / per-thinking-mode strategy

The roster is fixed: `sonnet-4.6`, `sonnet-4.5`, `haiku-4.5`, `haiku-3.5`.
Thinking-mode variants exist for `sonnet-4.6` and `sonnet-4.5` (ON and OFF);
`haiku-4.5` and `haiku-3.5` are thinking-OFF only. The implementation is
`bakeoff/prompts.py`; the selector is `system_instruction_for(family,
thinking_enabled)`.

### 2.1 The thinking ON vs OFF split (the central research finding)

Anthropic's consolidated "Prompting best practices" page — which explicitly names
**Claude Sonnet 4.6** and **Claude Haiku 4.5** as in-scope — prescribes *different
prompt structure by thinking mode* [**EXTERNAL, Anthropic**]
(`docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/claude-4-best-practices`):

- **Thinking ON → omit prescriptive CoT.** *"Prefer general instructions over
  prescriptive steps. A prompt like 'think thoroughly' often produces better
  reasoning than a hand-written step-by-step plan. Claude's reasoning frequently
  exceeds what a human would prescribe."* So when a model has a private reasoning
  phase, hand-written "Step 1… Step 2…" scaffolding is at best redundant and at
  worst constrains a reasoner that would otherwise plan better on its own.
- **Thinking OFF → add CoT back as a fallback.** *"Manual CoT as a fallback. When
  thinking is off, you can still encourage step-by-step reasoning by asking Claude
  to think through the problem... Ask Claude to self-check."* So the thinking-OFF
  variants carry the explicit reasoning method and self-check that the thinking-ON
  variants drop.
- **A 4.5-generation nuance.** *"When extended thinking is disabled, Claude Opus
  4.5 is particularly sensitive to the word 'think' and its variants. Consider
  using alternatives like 'consider,' 'evaluate,' or 'reason through.'"* We apply
  this to the **Sonnet 4.5 thinking-OFF** variant: it prescribes a reasoning method
  worded with "determine / work through / re-read / confirm" and avoids the literal
  word "think."

This is independently corroborated inside Amazon. From the Bedrock extended-thinking
enablement session:

> Paraphrased: with model reasoning turned ON you can "focus on giving it simpler
> prompts with instructions on business logic and let it figure out what thinking
> is needed," which means less prompt-engineering maintenance; with reasoning OFF
> the team instead engineers an explicit `<thinking>` block in the prompt.
> Enabling reasoning "helps it hallucinate less" and produce more aligned answers.
> — "Reasoning / Extended Thinking for Claude Models" enablement session
> [**INTERNAL, supporting**] (`broadcast.amazon.com/videos/1771626`). *Content
> rephrased for compliance with licensing restrictions.*

And from the Claude 4 launch session: extended-thinking models want *less* prompt
scaffolding and more general framing, while non-thinking use of the same models
keeps the explicit `<thinking>`-block engineering [**INTERNAL, supporting**,
`broadcast.amazon.com/videos/1553200`].

### 2.2 Newer Sonnet vs older/smaller Haiku

- **Newer Sonnets (4.6, 4.5) are tuned to follow instructions literally and to
  need less steering.** Anthropic: 4.x models are *"more responsive to the system
  prompt"* and aggressive language ("CRITICAL: You MUST...") can cause
  *overtriggering*; the fix is *"more normal prompting"* [**EXTERNAL, Anthropic**].
  So the Sonnet variants are lean: a role, the task, the answerability contract,
  and a light style note — no ALL-CAPS imperatives, no redundant scaffolding.
- **Haiku 3.5 (oldest, smallest) gets the classic pre-4 structured idiom.** Direct,
  numbered, explicit step-by-step instructions with firm "ONLY" grounding language
  and explicit refusal/partial templates — the style that *"works well"* for the
  3.5/3.7 generation [**INTERNAL, supporting**, `broadcast.amazon.com/videos/1553200`],
  and that mirrors the Golden Path's own example system prompt (XML-tagged, with an
  explicit *"I do not have enough information to answer"* clause) [**INTERNAL,
  PRIMARY**]. This is precisely the scaffolding the newest models no longer need —
  which is the whole reason a single locked prompt cannot serve both ends of the
  roster well.
- **Haiku 4.5 sits in between.** It is a 4.x model (so no ALL-CAPS over-steering),
  but it is the smaller, speed-oriented candidate, so its (thinking-OFF) reasoning
  method is explicit but compact: identify relevant fragments, then apply the
  answerability contract.

That Amazon platforms model both *per-model selection* and *per-model thinking
mode* as first-class is itself corroboration that this is standard internal
practice: an internal agent platform seeds "Claude Haiku 4.5" and "Claude Sonnet
4.6" as distinct models each carrying a `thinkingMode` of `off | budget | adaptive`
[**INTERNAL, supporting**, WorkcellAI wiki, `w.amazon.com/bin/view/OIS/WorkcellAI/`].

### 2.3 The per-family decision table

| Family | Thinking | Structure | Reasoning scaffold | Key sourced reason |
|---|---|---|---|---|
| `sonnet-4.6` | ON | lean: role / task / answerability / style (XML-tagged) | **none** (model reasons on its own) | omit prescriptive CoT when thinking on [EXT, Anthropic]; pull back steering on 4.x [INT, supporting] |
| `sonnet-4.6` | OFF | lean + a `<method>` block | internal "identify relevant fragments → compose → verify" + self-check | manual CoT as fallback when thinking off [EXT, Anthropic] |
| `sonnet-4.5` | ON | lean (same as 4.6 ON) | **none** | same as 4.6 ON [EXT, Anthropic] |
| `sonnet-4.5` | OFF | lean + a `<method>` block worded **without "think"** | "determine / work through / re-read / confirm" + self-check | 4.5-gen is sensitive to the word "think" when thinking is off [EXT, Anthropic] |
| `haiku-4.5` | OFF only | compact numbered `<method>` | explicit but lean (identify → answerability → reply) | 4.x model, smaller/faster: scaffold but don't over-steer [EXT + INT supporting] |
| `haiku-3.5` | OFF only | most prescriptive: numbered `<steps>`, firm "ONLY", explicit refusal/partial templates | full directive step-by-step | pre-4 "direct and structured" idiom works well for 3.5 [INT, supporting]; mirrors Golden Path example prompt [INT, PRIMARY] |
| unknown | either | the generic backward-compatible default instruction | n/a | safe fallback; preserves old behavior |

### 2.4 Answerability discipline — preserved identically in *every* variant

Every variant encodes the same grounding contract the scorers grade against
(`docs/BAKEOFF.md` §5.2), regardless of family or mode:

1. **Ground every claim in the retrieved fragments** — no outside knowledge, no
   guessing.
2. **Fully answerable → answer completely** from the fragments.
3. **Partially answerable → answer the supported part AND flag the gap** so the
   user knows what is missing (rewards "answer-the-answerable, flag-the-rest"
   rather than over-claiming).
4. **Not answerable → refuse/escalate, don't fabricate**: say you don't have the
   information and point the user to the right owner/support.
5. **Match the user's tone** and keep it clear.

This mirrors the Golden Path's *"Mitigate hallucination with firm instruction in
system prompt and RAG"* recommendation, which prescribes a system-prompt clause
that makes the model respond with "I don't know" when it lacks relevant data
[**INTERNAL, PRIMARY**], and it is what the harness's answerability/refusal scoring
keys on (correct abstention on `none` items; gap-flagging vs over-claiming on
`partial`). A unit test asserts the discipline (grounding, refuse/escalate,
partial-gap, tone) is present in every variant.

---

## 3. The fairness guarantee (what stays constant)

**Retrieval is the held constant, and so is the task.** The only thing that varies
across candidates is the *phrasing/structure of the instruction*.

Concretely, `bakeoff/adapters/base.build_prompt` composes the system message as:

```
<family/thinking-aware instruction>      ← the ONLY part that varies
\n\nReference fragments:\n
<assemble_context(fragments)>            ← IDENTICAL for every candidate
```

- `assemble_context(fragments)` is **unchanged** by this work — same numbered,
  id-tagged fragment block for everyone. Every candidate sees the identical
  retrieved fragments in the identical rendering (`docs/BAKEOFF.md` §2; design
  AD-2). No model is given more, less, or differently-ordered context.
- The multi-turn assembly (prior user turns + the model's own prior answers, in
  order) is **unchanged** — family/thinking only swap the instruction text.
- The **task** is identical: answer the internal Travel, Events & Expenses FAQ
  strictly from the provided fragments, with the answerability discipline of §2.4.
  Only *how that identical task is phrased* differs by family.

This is enforced by tests in `bakeoff/tests/test_prompts.py`:

- `test_fragments_context_identical_across_all_families` — the rendered
  "Reference fragments:" block is byte-identical across all four families plus the
  default, in both thinking modes, and equal to `assemble_context`'s output.
- `test_fragments_context_identical_across_thinking_modes` — thinking ON vs OFF
  produce the identical fragments block.
- `test_build_prompt_multi_turn_threads_family_but_keeps_turns_and_context` — the
  conversational messages and the fragments block are identical across families;
  only the system instruction differs.

So the experiment remains a clean comparison of "which model best serves this
grounded FAQ task," with each model prompted in its own dialect rather than a
foreign one — the deliberate, sourced trade described in §1.

---

## 4. RAG-triad alignment (what the prompt is optimizing for)

The prompt is designed against the same RAG-triad the calibration judges grade
(`docs/Building Short-Lived Calibration Judges...md`; TruLens "RAG triad"; Jason
Liu's "6 RAG evals"). Because retrieval is constant, the prompt can only move the
two *generation-side* legs of the triad:

| Triad leg | What it measures | Prompt lever (every variant) |
|---|---|---|
| **Faithfulness / Groundedness (A\|C)** | every claim supported by the retrieved context | "ground every claim in the fragments; no outside knowledge; no guessing"; thinking-OFF variants add an explicit "verify every claim traces to a fragment" self-check |
| **Answer Relevance (A\|Q)** | the answer actually addresses the question | "answer the user's question"; partial-gap handling; tone-matching |
| **Context Relevance (C\|Q)** | retriever quality | **not a prompt lever** — fixed by the shared substrate (held constant) |

The abstention/refusal behavior the prompt encodes maps onto the Golden Path's own
RAG metrics — **Faithfulness**, **Answer Relevance**, and **Refusal Rate**
("appropriate 'I don't know' frequency") — and onto Bedrock's **contextual
grounding checks** for RAG [**INTERNAL, PRIMARY**, Golden Path guardrail page,
`docs.hub.amazon.dev/docs/golden-path/llm-integrated-applications/recommendation/design/6-model-consumption/guardrail/`].
The practitioner guide's emphasis on measuring True Negative Rate separately (the
documented low-TNR / agreeableness problem) is exactly why the prompt makes the
refuse-don't-fabricate and flag-the-partial-gap behaviors explicit in every variant
rather than leaving them implicit.

---

## 5. Adaptations (where we deviate, and why)

Two places where the design intentionally diverges from a literal reading of a
source, with the reasoning recorded:

1. **Thinking-OFF reasoning is kept internal, not emitted as a visible
   `<thinking>` block.** Anthropic's thinking-OFF guidance suggests emitting a
   `<thinking>` block separated from an `<answer>` block. But this harness scores
   the model's *answer text wholesale* — it does not strip a reasoning block — and
   the output is user-facing (a Slack/QuickSuite FAQ reply). So the thinking-OFF
   variants instruct the model to do the relevance/grounding reasoning *internally*
   and return only the clean final answer ("Share only the final answer, not these
   notes."). This preserves the manual-CoT accuracy benefit Anthropic describes
   while keeping the judged, user-visible output clean. (If a future harness chose
   to strip a delimited reasoning block before scoring, the variants could switch
   to the explicit visible-`<thinking>` form.)
2. **Haiku models accept `thinking_enabled=True` but degrade to their single
   variant.** The roster defines Haiku 4.5 and 3.5 as thinking-OFF only. The
   selector still accepts the flag for them and returns their one variant either
   way, so a caller passing the flag uniformly never breaks. (`prompts.py`
   `FAMILY_INSTRUCTIONS` stores the same string in both slots for these families.)

---

## 6. Sourcing

Claims are labelled **INTERNAL, PRIMARY** (BuilderHub / AWS Prescriptive
Guidance / internal code search — authoritative), **INTERNAL, supporting**
(internal wikis and enablement broadcasts — used as supporting context, not relied
on alone), or **EXTERNAL** (Anthropic's authoritative model-specific docs, which
the internal primary source explicitly directs builders to consult).

### 6.1 Internal — primary (authoritative)

- **BuilderHub GenAI Golden Path — "LLM-integrated applications recommendations - 4
  Prompt engineering."** Sanctions per-model prompt tuning *within a family*
  ("Consult prompt engineering guidance from each model"); prescribes XML-tagged
  prompt structure with a worked example system prompt; prescribes hallucination
  mitigation via firm system-prompt "I don't know" instruction + RAG; recommends
  the Bedrock Converse API abstraction.
  `docs.hub.amazon.dev/docs/golden-path/llm-integrated-applications/recommendation/design/4-prompt-engineering/`
- **BuilderHub GenAI Golden Path — Guardrail / RAG metrics.** Contextual grounding
  checks for RAG; Faithfulness, Answer Relevance, Citation Accuracy, Refusal Rate
  as the RAG metric set.
  `docs.hub.amazon.dev/docs/golden-path/llm-integrated-applications/recommendation/design/6-model-consumption/guardrail/`

### 6.2 Internal — supporting (corroborating, not relied on alone)

- **Enablement broadcast, "Claude 4 Models on Bedrock!"** Newer Claude needs *less*
  steering than 3.7; "pull back on aggressive instruction following"; extended
  thinking wants more general framing; Sonnet 4 is the recommended 3.7 replacement.
  `broadcast.amazon.com/videos/1553200`
- **Enablement session, "Reasoning / Extended Thinking for Claude Models."** With
  model reasoning ON, give simpler prompts and let the model decide how to think;
  with reasoning OFF, engineer an explicit `<thinking>` block; reasoning reduces
  hallucination.
  `broadcast.amazon.com/videos/1771626`
- **WorkcellAI platform wiki.** Internal agent platform models per-model selection
  *and* a per-model `thinkingMode` (`off | budget | adaptive`), seeding Claude
  Haiku 4.5 and Claude Sonnet 4.6 — evidence that per-model + per-thinking config
  is standard internal practice. `w.amazon.com/bin/view/OIS/WorkcellAI/`
- **Internal wiki "Prompt Engineering Research Report" (NA_EF_BIE).** CRISP
  framework, XML-tag standard pattern, model-selection guidance, and the note that
  *newer reasoning models have built-in CoT so explicit CoT is most valuable for
  non-reasoning models*. NOTE: this is an **agent-generated wiki page**, treated as
  supporting context only; its own primary citations are the Golden Path and
  Anthropic docs used directly above.
  `w.amazon.com/bin/view/NA_EF_BIE/agents/jim/prompt_engineering_report/`

### 6.3 External — Anthropic (authoritative model-specific guidance)

The Golden Path explicitly directs builders to "Claude family model prompt
engineering technique" for per-model specifics; that is this source.

- **Anthropic, "Prompting best practices"** (consolidated guide; explicitly covers
  Claude Sonnet 4.6 and Claude Haiku 4.5). Thinking ON → prefer general
  instructions over prescriptive steps; thinking OFF → manual CoT fallback + self
  check; 4.5-gen sensitivity to the word "think" when thinking is disabled; 4.x
  models more responsive to the system prompt (dial back aggressive language);
  clarity/role/XML-tag/ground-in-quotes fundamentals; Sonnet 4.5→4.6 thinking-off
  behavior.
  `docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/claude-4-best-practices`
- **Anthropic, "Extended thinking tips" / "Let Claude think (CoT)."** Standard mode
  (thinking off) is where traditional CoT with `<thinking>` XML tags applies; with
  extended thinking on, hand-written CoT is unnecessary.
  `docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/extended-thinking-tips`

### 6.4 What internal search did NOT turn up (stated explicitly)

Per the rigor rule, here is what was searched and what was *not* found
authoritatively internal:

- **Searched** (via `InternalSearch` over `ALL` / `BUILDER_HUB` / `AWS_DOCS`, and
  `ReadInternalWebsites` over `docs.hub.amazon.dev` / `broadcast.amazon.com` /
  `w.amazon.com`): "prompt engineering Claude Sonnet 4.5 Bedrock best practices";
  "Claude extended thinking reasoning prompt patterns Bedrock"; "Golden Path
  production LLM RAG hallucination mitigation grounding prompt XML tags."
- **Found internally (primary):** the *general* prescriptions — per-model tuning is
  sanctioned; XML structure; firm grounding/refusal instruction for RAG. These
  carry the §1 decision and the §2.4 answerability contract.
- **NOT found in any internal *primary* source:** (a) Claude-**version-specific**
  prompt wording differences (Sonnet 4.6 vs 4.5; Haiku 4.5 vs 3.5), and (b) the
  specific **thinking-ON-vs-OFF CoT distinction** (omit prescriptive CoT when
  thinking is on; add it when off; avoid the literal word "think" for the 4.5
  generation when thinking is off). These specifics were found only in **internal
  supporting** material (the two enablement broadcasts) and in **Anthropic's
  external** authoritative docs. The §2 per-family wording therefore rests on
  EXTERNAL Anthropic guidance + INTERNAL *supporting* corroboration — **not** on an
  internal primary source. This is flagged so the reader can weight it
  accordingly; it is general-industry/vendor best practice that the internal
  primary source points to, not Amazon-blessed wording.

---

## 7. Caveats

- **The per-family wording is a hypothesis, not a measured optimum.** It is
  grounded in current guidance, but the only way to know it helps each family is to
  run the bake-off and compare. The harness's own design (per-model quality/latency
  with CIs) is the instrument that would confirm or refute it; treat these prompts
  as the starting, sourced baseline.
- **Vendor guidance shifts.** Anthropic's prompting page is versioned to current
  models and explicitly evolves (e.g., `budget_tokens` → adaptive thinking + an
  `effort` parameter). The thinking-mode split here is the durable principle; the
  exact API knobs are owned by the adapter/config workstream, not this module.
- **The thinking-mode flag is an input, not a measurement.** `prompts.py` selects
  wording from `thinking_enabled`; whether a given candidate *actually* runs with
  extended thinking on is set by the adapter/config layer (a separate workstream).
  The selector degrades safely (Haiku families ignore the flag; unknown families
  get the default).
- **Fairness is about context, not wording.** This design deliberately makes the
  *wording* differ. The guarantee it protects is that the *task and the retrieved
  information* do not. Anyone auditing the comparison should read §3 and the
  `test_prompts.py` invariants as the definition of "fair" here.
