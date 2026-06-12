# Requirements Document

## Introduction

The closed-loop prompt optimizer at `bakeoff/quality/optimizer/` improves system prompts for
two fixed Claude Target_Models (`config.QUALITY_MODELS`: `sonnet-4.6-thinking-off`,
`haiku-4.5`). Today the Author (prompt rewriter, resolved from
`config.QUALITY_OPT_V2_AUTHOR_MODEL_KEY = "sonnet-4.6-thinking-on"`) is Claude, and the Judge
(`config.JUDGE_MODEL_ID = us.anthropic.claude-opus-4-8`) is Claude (Opus). The whole loop is
therefore same-family, which couples the optimization signal to Claude "house style" and to
Opus self-preference.

This feature makes three coupled changes, and only these three:

1. **Correct the loop cadence.** Today `JudgeInLoopScorer.score_prompt` runs Opus on every
   champion and challenger scoring, every iteration — Opus is in the hot loop. The intended
   design has the Author self-iterate across a Round's steps using a cheap in-loop signal that
   is not Opus, with the Opus Judge adjudicating only at the conclusion of a Round.
2. **Make the Author non-Anthropic** (roughly Sonnet-4.6 size), configured separately from the
   deploy-target roster, with a family-aware Author≠Judge guard and a provider-general
   `BedrockAuthorClient` / author contract.
3. **Add a cross-family audit seam**: a periodic non-Claude Audit_Judge that re-scores the
   current winner on a sample, light authorship/style obfuscation before judging, and a
   proxy-vs-audit divergence check (the self-preference / Goodhart detector). This addresses the
   Judge-to-target self-preference coupling that the Author swap alone does not fix, because the
   Target_Models stay fixed Claude and are graded by Opus.

This spec is a distinct concern. It does not replace `optimizer-quality-uplift`,
`optimizer-ragas-gepa`, or `optimizer-v2-end-to-end`. It strengthens `optimizer-ragas-gepa`
Req 12 (which enforces Author≠Judge by identity only) to Family-level separation. GEPA,
Bradley-Terry ranking, and a sequestered holdout vault are explicitly out of scope and live in
other specs.

## Sourcing and Honesty

- `docs/solo-model-prompt-iteration.md`, and the GEPA / RAGAS / self-preference literature it
  cites, are external / industry sources. They are not Amazon-internal guidance. No
  Amazon-internal primary source was consulted, and none applies to this throwaway local
  harness.
- Bedrock model availability could not be verified this session: a live
  `aws bedrock list-inference-profiles` against the `alpha` profile failed with
  `ExpiredTokenException`. Every specific non-Anthropic model id below is therefore an
  assumption to confirm at implementation time.
- Any judge-derived number must be re-validated before it is used to defend a decision upward.

## Assumptions to Confirm

- **A1 — Per-Round step count.** The intended Author-iterations-per-Round count (the user
  referred to "six terms") is configurable. The current knob `QUALITY_OPT_ISLAND_RUNG_PATIENCE`
  is `2`. Confirm the intended value and whether it reuses this knob or a new one.
- **A2 — Author model id.** A non-Anthropic Bedrock model of ~Sonnet-4.6 size. Candidates
  (general knowledge, unverified): Amazon Nova Pro, Meta Llama 4 Maverick, Mistral Large 2.
  Confirm the exact Bedrock id and its temperature-parameter behavior at implementation time.
- **A3 — Audit_Judge model id.** Strong non-Claude judges named in the doc (GPT-5 / Gemini
  class) are not on Bedrock. In-Bedrock options (unverified): Nova Premier, Llama 4 Maverick,
  Mistral Large, DeepSeek-R1. Confirm availability and the exact id at implementation time.

## Glossary

- **Optimizer**: the closed-loop prompt optimizer in `bakeoff/quality/optimizer/`, including its
  backend wiring (`backends.py`) and in-loop scorer (`judge_loop.py`).
- **Author**: the model that rewrites the system prompt each iteration (`BedrockAuthorClient`).
- **Judge**: the Opus model (`config.JUDGE_MODEL_ID`) that adjudicates prompt quality.
- **Audit_Judge**: a separate, non-Claude model that periodically re-scores the current winner
  to detect self-preference; no such seam exists today.
- **Target_Model**: one of the two fixed Claude models whose prompt is being optimized
  (`config.QUALITY_MODELS`).
- **Round**: one cadence cycle of in-loop Author self-iteration that ends with a single Judge
  adjudication step.
- **In_Loop_Signal**: the cheaper per-iteration scoring signal used during a Round that does not
  invoke the Judge.
- **Family**: a model's provider/lineage (for example Anthropic, Amazon, Meta, Mistral,
  DeepSeek), used to compare Author, Judge, and Audit_Judge for same-family separation.

## Requirements

### Requirement 1: Corrected loop cadence

**User Story:** As a researcher running the optimizer, I want the Author to self-iterate within
a Round using a cheap in-loop signal while the Opus Judge adjudicates only at the Round's
conclusion, so that Opus is removed from the per-iteration hot loop and the cadence matches the
intended design.

#### Acceptance Criteria

1. WHILE a Round is in progress, THE Optimizer SHALL score each in-round Author iteration using
   only the In_Loop_Signal.
2. THE Optimizer SHALL derive the In_Loop_Signal without invoking the Judge.
3. WHEN a Round reaches its concluding step, THE Optimizer SHALL invoke the Judge to adjudicate
   the Round's candidate prompt.
4. WHEN the Judge has adjudicated at a Round's conclusion, THE Optimizer SHALL decide prompt
   promotion using that Judge adjudication.
5. THE Optimizer SHALL read the number of Author iterations per Round from a configurable value
   (see Assumption A1).

### Requirement 2: Non-Anthropic Author with family-aware separation

**User Story:** As a researcher, I want the prompt Author to be a non-Anthropic Bedrock model of
roughly Sonnet-4.6 size, configured separately from the deploy targets, so that the rewriter is
a different Family from the Judge and same-family bias in authoring is reduced.

#### Acceptance Criteria

1. THE Optimizer SHALL resolve the Author model id from a configuration slot that is separate
   from `config.QUALITY_MODELS`.
2. THE configured Author SHALL be a non-Anthropic Bedrock model (see Assumption A2).
3. WHEN the live backend is built, THE Optimizer SHALL determine the Family of the resolved
   Author and the Family of the Judge.
4. IF the Author Family equals the Judge Family, THEN THE Optimizer SHALL halt startup and raise
   a Family-conflict error.
5. WHERE the configured Author provider accepts a temperature parameter, THE Author SHALL send
   the temperature in the Bedrock request.
6. WHERE the configured Author provider does not accept a temperature parameter, THE Author
   SHALL omit the temperature from the Bedrock request.
7. THE Author SHALL determine temperature handling from the configured Author provider rather
   than from a fixed Claude assumption.
8. THE Author contract produced by `build_author_prompt` SHALL state the authoring task without
   asserting that the Author is a Claude model.
9. THE Author contract SHALL present the embedded prompting guidance as guidance about the
   Target_Model Family rather than as guidance describing the Author.

### Requirement 3: Cross-family audit judge, obfuscation, and Goodhart check

**User Story:** As a researcher, I want a periodic non-Claude Audit_Judge that re-scores the
current winner on a sample with light authorship/style obfuscation, plus a divergence check
between the Judge ranking and the Audit_Judge ranking, so that self-preference between the fixed
Claude Target_Models and the Opus Judge is detectable.

#### Acceptance Criteria

1. THE Optimizer SHALL provide a seam for an Audit_Judge that is a non-Claude Bedrock model (see
   Assumption A3).
2. WHERE the Audit_Judge is enabled, THE Optimizer SHALL re-score the current winning prompt on
   a sample of conversations using the Audit_Judge at a configurable interval.
3. WHEN material is submitted to the Audit_Judge, THE Optimizer SHALL apply authorship and style
   obfuscation to that material before scoring.
4. WHEN both a Judge ranking and an Audit_Judge ranking are available, THE Optimizer SHALL
   compute a divergence measure between the two rankings.
5. IF the divergence measure exceeds a configurable threshold, THEN THE Optimizer SHALL flag a
   potential self-preference condition.

### Requirement 4: Posture and non-regression

**User Story:** As a researcher maintaining a throwaway local harness, I want these changes to
be additive, config-gated, and to preserve the existing held-constant retrieval and phase
boundaries, so that the new seams do not disturb the established study.

#### Acceptance Criteria

1. THE Optimizer SHALL reserve the Opus model for the Judge role.
2. WHERE a behavior introduced by this feature can be gated, THE Optimizer SHALL expose a
   configuration flag that controls that behavior.
3. THE Optimizer SHALL preserve the held-constant, memoized, read-only retrieval substrate
   unchanged.
4. THE Optimizer SHALL preserve the Phase A tuning-slice and Phase B validation-set boundary
   unchanged.
5. THE Optimizer SHALL reuse the existing Bedrock credential chain and introduce no new secrets.
6. WHERE a network listener is started, THE Optimizer SHALL bind it to the loopback interface.
7. WHERE JavaScript tooling is required, THE Optimizer SHALL use bun.
