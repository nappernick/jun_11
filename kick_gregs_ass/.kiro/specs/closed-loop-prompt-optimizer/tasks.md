# Implementation Plan: Closed-Loop Prompt Optimizer

## Overview

This plan builds the closed-loop prompt optimizer **inner-to-outer**: pure/deterministic
cores first (config, ids, CI math, retrieval), then durable stores and the prompting
guidance, then the per-iteration loop pieces (judge-in-loop scorer, failure selection,
promotion/convergence, author), then the inline persistent-session adapter, then the
orchestration (controller → validate → events → orchestrator), then the CLI and additive
FastAPI routes, then the Quality_Tab Per_Model_View UI, and finally an end-to-end offline
integration test and the migration/fresh-start step. Every step builds on the previous and
ends wired into the loop — no orphaned code.

All new code lives under the new package `bakeoff/quality/optimizer/`. The five-variant
menu (`bakeoff/quality/prompts.py` `MULTI_TURN_BLOCKS` / `variants_for_model`) is retained
**only** as the iteration-0 seed source and is never the selection mechanism for any
iteration ≥ 1.

**Retrieval-always (Req 13–16):** the quality answer path now invokes a pluggable,
held-constant, read-only `RetrievalBackend` on **every turn**; the fragments are rendered
**inline** in the visible prompt and are the model's **only** grounding; the **same**
fragments are threaded into the judge; and **abstention** (declining when unsure /
insufficiently grounded) is a first-class, heavily-weighted scored behavior. This reverses
the prior fragment-free default.

### Test layout and verification (NOT a Brazil workspace)

- Verification command (run after every coding sub-task):
  ```
  .venv/bin/python -m pytest bakeoff/tests/ -q
  ```
- Property-based tests (Hypothesis, ≥ 100 iterations, one per property P1–P29, each tagged
  `Feature: closed-loop-prompt-optimizer, Property {n}: {text}`) → **new file**
  `bakeoff/tests/test_quality_optimizer_pbt.py`.
- Unit / example / edge tests → **new file** `bakeoff/tests/test_quality_optimizer.py`.
- Inline no-noise fidelity test → near `bakeoff/tests/test_inline_agent.py` or a **new
  file** `bakeoff/tests/test_persistent_session_adapter.py`.
- SSE / API tests → **extend** `bakeoff/tests/test_app.py`.
- End-to-end offline mini-loop → **new file** `bakeoff/tests/test_quality_optimizer_e2e.py`.

All offline tests use the existing zero-network doubles: `QualityOfflineAdapter`,
`StubJudge` (`make_stub_judge`), the fake embedder (`_make_fake_embed_fn`), and the new
`OfflineAuthorClient` + `FakeRetrievalBackend`. Live tasks are flagged **[LIVE / MANUAL]**
and are NOT part of the offline `pytest` suite.

### Offline-testable vs live

- **Offline-testable (the vast majority):** everything below except the items marked
  **[LIVE / MANUAL]**. The live backend's streaming/parse/resilience logic, and the
  OpenSearch retrieval path, are exercised offline with injected fake clients (no real
  Bedrock / no real AWS), exactly as the existing adapter/judge tests do.
- **[LIVE / MANUAL]:** the inline persistent-session **live probe** (Task 11.6), the ALPHA
  OpenSearch **smoke** (Task 4.5), and a live backend wiring smoke run (operator action
  behind the bake-off-active quota guard). These are owner-asserted-assumption validations
  against real Bedrock / real AWS and are run manually.

### Methodology sourcing caveat (carried from design)

The judge-as-signal triad, the noise-floor SD ≈ 0.24 / 0.05-threshold CI math, the inline
persistent-session implicit-history behavior, the modern Claude 4.5 Prompting_Guidance, and
the ALPHA OpenSearch endpoint specifics are grounded in external/industry RAG-eval practice,
this repo's own observed Opus verdicts, AWS **public** API docs, an **external/vendor**
prompting source (`modern_system_prompting.pdf`), and **owner-provided** operational facts —
**not** Amazon-internal primary sources (which were unavailable when the design was set).
Re-validate any judge-derived number against internal guidance before defending a decision
upward. The persistent-session history behavior (Task 11.6 probe) and the OpenSearch
endpoint/index/auth (Task 4.5 smoke) are owner-asserted assumptions validated by their
respective live tasks, with documented fallbacks (explicit `conversationHistory`; the
guaranteed-workable `LocalRetrievalBackend`).

## Tasks

- [x] 1. Config constants, new store paths, and the minimal inline template
  - [x] 1.1 Add optimizer configuration to `bakeoff/config.py`
    - Add tuning constants: `QUALITY_OPT_SIGNIFICANCE_THRESHOLD = 0.05`,
      `QUALITY_OPT_STOP_LIMIT = 5`, `QUALITY_OPT_FAILURES_K`, `QUALITY_OPT_PHASE_A_REPS`,
      `QUALITY_OPT_PHASE_B_REPS` (Phase B > Phase A), `QUALITY_OPT_SPLIT_SEED`,
      `QUALITY_OPT_INLINE_HISTORY_MODE = "server"`
    - **Retrieval-always (Req 13):** set `QUALITY_OPT_SEND_FRAGMENTS = True` (the quality
      path is retrieval-always; the inline adapter defaults `send_fragments=True`).
      `send_fragments=False` survives only as a diagnostic escape hatch, never the default
    - **Abstention (Req 14):** add `QUALITY_OPT_ABSTENTION_WEIGHT` (the primary-behavior
      weight by which correct abstention is rewarded and answering-when-unsure penalized in
      the per-turn `overall`)
    - **Retrieval backend selection (Req 16):** add
      `QUALITY_OPT_RETRIEVAL_BACKEND = "opensearch"` (`"opensearch"` | `"local"` | `"fake"`,
      preferring OpenSearch with a local fallback), plus owner-provided ALPHA placeholders
      `QUALITY_OPT_OPENSEARCH_ENDPOINT`, `QUALITY_OPT_OPENSEARCH_INDEX`,
      `QUALITY_OPT_OPENSEARCH_AUTH` (AWS account `948580600005`; left as placeholders to
      confirm at implementation time — the local fallback guarantees the study runs without
      them)
    - Add new append-only store paths under `BAKEOFF_DIR`: `QUALITY_OPT_ITERATIONS_PATH`,
      `QUALITY_OPT_AUDIT_PATH`, `QUALITY_OPT_RESULTS_PATH`, `QUALITY_OPT_ERRORS_PATH`
      (distinct files; never shared with the bake-off or one-shot quality stores)
    - Add `QUALITY_OPT_INLINE_TEMPLATE`: a minimal OVERRIDDEN base template containing only
      `$instruction$` (system), `$question$` (user), and the required empty
      `$agent_scratchpad$` (assistant) — and **no** `$prompt_session_attributes$`
      placeholder (the existing `INLINE_AGENT_PROMPT_TEMPLATE` keeps its placeholder and is
      left untouched for the bake-off adapter)
    - Create `bakeoff/quality/optimizer/__init__.py`
    - _Requirements: 5.2, 5.6, 6.4, 6.5, 3.4, 7.4, 7.6, 11.1, 12.1, 12.2, 13.4, 14.2, 16.1, 16.6_
    - _Design: Data Models (store layout + constants), Inline-agent design, Migration (new config)_
    - _Offline-testable. Verify: `.venv/bin/python -m pytest bakeoff/tests/ -q`_

- [x] 2. Deterministic identifiers
  - [x] 2.1 Implement `bakeoff/quality/optimizer/ids.py`
    - `iteration_id(model, phase, iteration_index)`, `prompt_version_id(model, iteration_index)`,
      `gen_trial_id(model, item_id, rep, role, phase)` reusing the SHA-256 prefix scheme of
      `bakeoff/ids.py` (`gen_trial_id` delegates to `bakeoff.ids.trial_id` with
      `pass_name=f"opt-{phase}-{role}"`, `plan="quality-opt-v1"`)
    - `iteration_id` is the resume key
    - _Requirements: 10.2_
    - _Design: Deterministic ids_
    - _Offline-testable. Verify with pytest._
  - [x] 2.2 Write property test for deterministic identifiers
    - **Property 16: Deterministic identifiers** — same inputs → same id; any field change
      → different id (collision-free over distinct inputs)
    - **Validates: Requirements 10.2**
    - In `bakeoff/tests/test_quality_optimizer_pbt.py`, tagged
      `Feature: closed-loop-prompt-optimizer, Property 16: ...`, ≥ 100 iterations

- [x] 3. Significance statistics (CI math)
  - [x] 3.1 Implement `bakeoff/quality/optimizer/stats.py`
    - `between_conversation_sd(conv_triads)` (sample SD, ddof=1, 0.0 for n<2),
      `ci_half_width(sd, n, level)` = `z * sd / sqrt(n)` with `_z_for_level` (z≈1.95996 at
      0.95), `is_significant(champion, challenger, threshold)` = `(challenger - champion) >= threshold`,
      `gain_report(prev, new)` returning `{absolute_delta, percent_delta}`
    - _Requirements: 5.1, 5.3, 5.4, 5.5, 5.8_
    - _Design: Significance statistics and CI math_
    - _Offline-testable. Verify with pytest._
  - [x] 3.2 Write property test for CI half-width formula
    - **Property 7: CI half-width formula and its monotonicity** — half-width ==
      `1.96 * s / sqrt(n)` (to tolerance), non-increasing in `n`, `0` when `s == 0`
    - **Validates: Requirements 5.3, 5.8**
    - Tagged `Feature: closed-loop-prompt-optimizer, Property 7: ...`, ≥ 100 iterations
  - [x] 3.3 Write property test for gain reporting
    - **Property 8: Gain is reported as both absolute delta and percentage** — and the
      decision is invariant to the percentage
    - **Validates: Requirements 5.4, 5.5**
    - Tagged `Feature: closed-loop-prompt-optimizer, Property 8: ...`, ≥ 100 iterations
  - [x] 3.4 Write property test for the promotion predicate
    - **Property 1: Promotion iff significant triad gain** — `is_significant` promotes iff
      `(ch - c) >= t`, otherwise retains champion
    - **Validates: Requirements 1.6, 5.1, 5.5, 5.6**
    - Tagged `Feature: closed-loop-prompt-optimizer, Property 1: ...`, ≥ 100 iterations

- [x] 4. RetrievalBackend (pluggable, held-constant, read-only)
  - [x] 4.1 Implement `bakeoff/quality/optimizer/retrieval.py`
    - `RetrievalQuery` dataclass; `RetrievalBackend` Protocol (`name`, read-only
      `retrieve(q) -> Sequence[Fragment]`); `MemoizingRetrievalBackend` wrapping any backend
      with a cache keyed `(item_id, turn, query, frozenset(filters), candidate_n, top_k)` —
      the key **excludes** prompt role and instruction text so Champion and Challenger get
      byte-identical fragments for the same turn (held-constant retrieval)
    - `OpenSearchRetrievalBackend` (PREFERRED, Req 16.1): queries the ALPHA OpenSearch
      service (AWS account `948580600005`); endpoint/index/auth **injected** (owner-provided
      assumptions, not hard-coded); read-only; accepts an injectable client so tests use a
      FAKE OpenSearch client (no real AWS)
    - `LocalRetrievalBackend` (FALLBACK, Req 16.2): the repo's `POST /retrieve` service;
      read-only
    - `FakeRetrievalBackend` (offline test double): deterministic, network-free fixed
      fragments keyed by `(item_id, turn)`
    - `build_retrieval_backend(name)` selection + fallback (Req 16.1/16.2/16.3): prefer
      OpenSearch, fall back to local if onerous/unworkable; `"local"`→local; `"fake"`→fake;
      always wrapped in `MemoizingRetrievalBackend`. All implementations return the same
      `{id, text, metadata, ...}` shape (Req 16.4) and issue read-only queries only (Req 16.5)
    - _Requirements: 12.1, 13.1, 13.2, 13.3, 16.1, 16.2, 16.3, 16.4, 16.5, 16.6_
    - _Design: Component 5b (RetrievalBackend), Retrieval-always data flow_
    - _Offline-testable (fakes; OpenSearch path via injected fake client). Verify with pytest._
  - [x] 4.2 Write unit tests for the retrieval backends
    - Protocol conformance; identical `{id,text,metadata,...}` shape across all three impls
      (via fakes); read-only; memoization returns identical fragments for repeated
      `(turn-query)` regardless of role/instruction; OpenSearch path exercised via injected
      fake client (no real AWS)
    - _Requirements: 16.3, 16.4, 16.5, 13.3_
    - In `bakeoff/tests/test_quality_optimizer.py`
  - [x] 4.5 **[LIVE / MANUAL]** ALPHA OpenSearch smoke
    - A documented manual smoke against the ALPHA OpenSearch account `948580600005`
      confirming the owner-provided endpoint/index/auth resolve and that returned fragments
      match the local-corpus `{id,text,metadata,...}` shape. Behind the bake-off-active quota
      guard; **excluded from the offline `pytest` suite**.
    - _Requirements: 16.1, 16.6_
    - _Design: Component 5b (OpenSearchRetrievalBackend), Methodology caveat_
    - **Manual/live task — not run by the offline suite.**

- [x] 5. Store types and append-only JSONL IO
  - [x] 5.1 Implement the record dataclasses in `bakeoff/quality/optimizer/store.py`
    - `DrivingFailure` (incl. `abstention_correct`, `answered_when_unsure`,
      `fragments_sufficient`, `grounding_fragment_ids`), `IterationRecord` (the SoT shape,
      incl. `abstention_reward_mean`, `answered_when_unsure_rate`, `retrieval_backend`),
      `AuditRecord` with frozen dataclasses + `to_jsonl`/`from_jsonl` (one complete JSON
      object per physical line), mirroring `bakeoff/quality/types.py`
    - `prompt_diff` built with `difflib.unified_diff`
    - _Requirements: 8.1, 8.3, 2.6, 4.3, 10.1, 10.6, 13.7, 14.2, 14.4, 16.1_
    - _Design: Data Models (IterationRecord, AuditRecord, DrivingFailure)_
    - _Offline-testable. Verify with pytest._
  - [x] 5.2 Implement `OptimizerStore` append-only IO
    - `append_iteration`, `append_audit`, error-store append, and a `quality_opt_results.json`
      writer; each durable write is a single `flush()` + `os.fsync()`'d JSONL line;
      crash-tolerant readers drop only a truncated trailing line (mirror
      `read_turn_judge_scores`)
    - _Requirements: 10.1, 10.8, 11.1_
    - _Design: Data Models (durability discipline), Error Handling (append-only durable writes)_
    - _Offline-testable. Verify with pytest._
  - [x] 5.3 Implement version-history reconstruction, lookback, and resume helpers
    - Reconstruct ordered prompt-version history per model (filter by `model`, order by
      `iteration_index`); `lookback(model, n)`; `completed_iteration_ids(model)` for resume
    - _Requirements: 8.2, 8.4, 8.5, 10.3_
    - _Design: Data Models (version history), Error Handling (crash resume)_
    - _Offline-testable. Verify with pytest._
  - [x] 5.4 Write property test for store round-trip and history lookback
    - **Property 14: Append-only round-trip and ordered version-history lookback** —
      read-back identity in order, appends never alter earlier lines, ordered version history
      per model, lookback-n returns the correct trailing n versions
    - **Validates: Requirements 8.2, 8.4, 8.5, 10.1**
    - Tagged `Feature: closed-loop-prompt-optimizer, Property 14: ...`, ≥ 100 iterations
  - [x] 5.5 Write property test for truncated-tail tolerance
    - **Property 17: Durable writes tolerate a truncated trailing line** — reading a file
      whose final line is truncated returns the complete prefix without raising; only a
      non-final corrupted line raises
    - **Validates: Requirements 10.8**
    - Tagged `Feature: closed-loop-prompt-optimizer, Property 17: ...`, ≥ 100 iterations

- [x] 6. Prompting_Guidance (repo-baked, Req 15)
  - [x] 6.1 Create the Prompting_Guidance reference
    - `bakeoff/quality/optimizer/prompting_guidance.py` with module constants
      `PROMPTING_GUIDANCE` and `GROUNDING_ABSTENTION_EXCERPT` (or
      `docs/PROMPTING_GUIDANCE.md` loaded once at import) — derived from
      `modern_system_prompting.pdf`, **never parsed from the raw PDF at runtime**. Content:
      Claude 4.5 XML/tagged layered structure, refusal/abstention handling, tone/formatting
      control, knowledge-grounding, steerability, and the 4.x caution that models are highly
      responsive to the system prompt so over-aggressive ALL-CAPS "MUST" language
      over-triggers and should be avoided
    - _Requirements: 15.1, 15.2, 15.3, 15.6_
    - _Design: Prompting_Guidance component (5)_
    - _Offline-testable. Verify with pytest._
  - [x] 6.2 Write SMOKE tests for the Prompting_Guidance
    - Covers the required sections (15.2); is a repo constant / markdown, NOT parsed from the
      PDF at runtime (15.3); flagged external/vendor-sourced (15.6)
    - _Requirements: 15.2, 15.3, 15.6_
    - In `bakeoff/tests/test_quality_optimizer.py`

- [x] 7. JudgeInLoopScorer (the decision metric)
  - [x] 7.1 Implement `bakeoff/quality/optimizer/judge_loop.py`
    - `TurnVerdict` (incl. `abstention_correct`, `answered_when_unsure`,
      `fragments_sufficient`, `grounding_fragment_ids`, `closeness`), `SliceScore` (incl.
      `abstention_reward_mean`, `answered_when_unsure_rate`, `mean_closeness`), and
      `JudgeInLoopScorer.score_prompt(...)`
    - **Retrieval-always (Req 13):** for every turn, call the held-constant (memoized)
      `RetrievalBackend` to get fragments, generate the answer via the injected adapter with
      fragments rendered **inline**, then judge each turn — threading the **same** fragments
      into the judge as the **faithfulness/grounding** evidence (reconcile the old
      `fragments=[]` vs `wants`: faithfulness now grounds on the actual retrieved fragments;
      correctness/completeness still use gold/wants/abstention ideal)
    - **Abstention (Req 14):** compute an abstention-weighted per-turn `overall` using
      `config.QUALITY_OPT_ABSTENTION_WEIGHT` — correct decline on insufficient/unanswerable
      turns scores near the top; answering-when-unsure is strongly penalized; record
      `abstention_reward_mean` and `answered_when_unsure_rate`. The judge remains the sole
      decision metric (Req 2); the weighting enters the per-turn aggregation, not a separate
      metric
    - Aggregate per-conversation triad → slice mean + CI (via `stats.py`); record
      per-dimension means; record `mean_closeness` via the existing `TurnClosenessScorer` as
      a **secondary cross-check only** (never read by the decision). Take the backend as an
      injected object (duck-typed)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.6, 1.2, 1.5, 13.1, 13.2, 13.5, 13.7, 14.1, 14.2, 14.3, 14.4, 14.5_
    - _Design: Component 2 (JudgeInLoopScorer), Retrieval-always data flow, Abstention weighting_
    - _Offline-testable. Verify with pytest._
  - [x] 7.2 Write unit/example tests for JudgeInLoopScorer
    - Wraps a real `JudgeScorer` (2.2); closeness recorded as secondary (2.3); per-dimension
      recorded (2.6); over-refusal fixture where closeness is high but the triad is
      authoritative (2.5); judge grounds on the SAME fragments the model received (13.7); a
      model that declines on insufficient fragments is NOT penalized (13.8, 14.7);
      abstention-weighting fields recorded (14.2)
    - _Requirements: 2.2, 2.3, 2.5, 2.6, 13.7, 13.8, 14.2, 14.7_
    - In `bakeoff/tests/test_quality_optimizer.py`
  - [x] 7.3 Write property test for retrieval-invoked-every-turn
    - **Property 24: Retrieval is invoked on every turn** — `RetrievalBackend.retrieve` is
      called for every turn of every conversation (after memoization, at least once per
      distinct `(turn-query)`) before the model answers
    - **Validates: Requirements 13.1, 13.2**
    - Tagged `Feature: closed-loop-prompt-optimizer, Property 24: ...`, ≥ 100 iterations
  - [x] 7.4 Write property test for held-constant retrieval across champion/challenger
    - **Property 25: Champion and challenger receive identical fragments for the same turn**
      — memoization key excludes prompt role + instruction, so varying the instruction never
      changes the fragments; the only varied element is the system-instruction text
    - **Validates: Requirements 12.4, 13.3**
    - Tagged `Feature: closed-loop-prompt-optimizer, Property 25: ...`, ≥ 100 iterations
  - [x] 7.5 Write property test for judge grounding parity
    - **Property 26: The judge grounds on the same fragments the model received** — the
      fragment ids/order passed to the judge as faithfulness evidence equal those rendered
      inline to the model for that turn
    - **Validates: Requirements 13.5, 13.7**
    - Tagged `Feature: closed-loop-prompt-optimizer, Property 26: ...`, ≥ 100 iterations
  - [x] 7.6 Write property test for abstention monotonicity
    - **Property 27: Correct abstention scores at least as high as an unsupported answer** —
      on insufficient/unanswerable turns, holding all else equal, a correct-abstention answer
      has per-turn `overall` ≥ an answering-when-unsure answer, and the gap is non-decreasing
      in the abstention weight
    - **Validates: Requirements 14.1, 14.2, 14.3, 14.4, 14.5**
    - Tagged `Feature: closed-loop-prompt-optimizer, Property 27: ...`, ≥ 100 iterations

- [x] 8. FailureSelector
  - [x] 8.1 Implement `bakeoff/quality/optimizer/failures.py`
    - `select_failures(score, *, k)` returning the `min(k, n)` worst `TurnVerdict`s, each
      carrying judge evidence, per-dimension scores, and `grounding_fragment_ids`; `k`
      defaults to `config.QUALITY_OPT_FAILURES_K`. **Ordering surfaces answering-when-unsure
      turns FIRST** (Req 14.4/14.6): `answered_when_unsure == True` sorts ahead of ordinary
      low-triad turns, then ties broken deterministically by `(overall, item_id, rep, turn)`
    - _Requirements: 1.3, 3.1, 3.4, 14.4, 14.6_
    - _Design: Component 4 (FailureSelector)_
    - _Offline-testable. Verify with pytest._
  - [x] 8.2 Write property test for failure selection
    - **Property 4: Failure selection returns the k lowest judged turns with their evidence**
      — and answering-when-unsure turns are surfaced first
    - **Validates: Requirements 1.3, 3.4, 14.4**
    - Tagged `Feature: closed-loop-prompt-optimizer, Property 4: ...`, ≥ 100 iterations

- [x] 9. PromotionDecider and ConvergenceTracker
  - [x] 9.1 Implement `bakeoff/quality/optimizer/convergence.py`
    - `ConvergenceTracker` (consecutive-non-improving counter, reset on promote, stop at
      `stop_limit`, record `converged_iteration` + `stop_reason`) and the `PromotionDecider`
      built on the pure `stats.is_significant` predicate; a non-usable challenger is treated
      as `promoted=False`
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 1.6, 5.1, 3.5_
    - _Design: Component 6 (PromotionDecider + ConvergenceTracker)_
    - _Offline-testable. Verify with pytest._
  - [x] 9.2 Write property test for the convergence counter and stop rule
    - **Property 9: Convergence counter and stop rule** — counter == trailing reject run,
      resets to 0 on promotion, stops exactly at the first iteration the run reaches `L`
    - **Validates: Requirements 6.1, 6.2, 6.3, 6.5**
    - Tagged `Feature: closed-loop-prompt-optimizer, Property 9: ...`, ≥ 100 iterations
  - [x] 9.3 Write property test for champion monotonicity
    - **Property 3: Champion triad is monotonically non-decreasing across accepted promotions**
      — and each promotion strictly increases the champion by ≥ the threshold
    - **Validates: Requirements 1.6, 5.1**
    - Tagged `Feature: closed-loop-prompt-optimizer, Property 3: ...`, ≥ 100 iterations
  - [x] 9.4 Write property test for triad-only decisions
    - **Property 2: The decision depends only on the triad, never on closeness** — holding
      triad scores fixed and varying closeness arbitrarily leaves the decision unchanged
    - **Validates: Requirements 2.1, 2.4, 2.5**
    - Tagged `Feature: closed-loop-prompt-optimizer, Property 2: ...`, ≥ 100 iterations

- [x] 10. AuthorClient (offline first, then Bedrock) and backend wiring
  - [x] 10.1 Implement `AuthoredChallenger`, the `AuthorClient` protocol, and `OfflineAuthorClient`
    - In `bakeoff/quality/optimizer/author.py`: `AuthoredChallenger` (instruction, rationale,
      author_model, `usable`, raw); the `AuthorClient` Protocol with the structured author
      prompt contract (role/task, the repo-baked **Prompting_Guidance** included every
      invocation (Req 15.1), verbatim champion, driving failures with evidence treated as
      data, strict JSON `{instruction, rationale}`, held-constant preservation). The contract
      steers **fragments-only grounding** (no outside/training knowledge, Req 13.6) and
      **explicit/reliable abstention** (decline when unsure/insufficiently grounded,
      Req 14.6). Deterministic network-free `OfflineAuthorClient` that edits lever blocks
      from the failure mix (incl. a grounding/abstention lever when failures show
      answering-when-unsure) so the offline loop has a real improving signal; `usable=False`
      when empty/whitespace or byte-identical to the champion
    - _Requirements: 3.1, 3.2, 3.3, 3.5, 3.6, 1.4, 13.6, 14.6, 15.1_
    - _Design: Component 5 (AuthorClient), Author prompt design, Fidelity invariant preservation_
    - _Offline-testable. Verify with pytest._
  - [x] 10.2 Write unit/example tests for OfflineAuthorClient
    - Author receives champion + selected failures with evidence (1.4, 3.1); rationale present
      (3.3); produces authored text, not a menu pick (3.2); includes the Prompting_Guidance
      every invocation (15.1); contract steers fragments-only grounding (13.6) and explicit
      abstention (14.6)
    - _Requirements: 1.4, 3.1, 3.2, 3.3, 13.6, 14.6, 15.1_
    - In `bakeoff/tests/test_quality_optimizer.py`
  - [x] 10.3 Write property test for non-usable challengers
    - **Property 5: A non-usable challenger is a non-improving iteration** — empty / whitespace
      / identical-to-champion → no usable challenger, not promoted, counter increments
    - **Validates: Requirements 3.5**
    - Tagged `Feature: closed-loop-prompt-optimizer, Property 5: ...`, ≥ 100 iterations
  - [x] 10.4 Write property test for authored-not-menu challengers
    - **Property 6: Challengers are authored text, never menu selections** — every iteration
      ≥ 1 challenger is authored and not equal to any `variants_for_model` member; the menu is
      used at most for the iteration-0 seed
    - **Validates: Requirements 1.8, 3.2, 11.4**
    - Tagged `Feature: closed-loop-prompt-optimizer, Property 6: ...`, ≥ 100 iterations
  - [x] 10.5 Implement `BedrockAuthorClient` (live author = Sonnet 4.6)
    - Converse streaming with a `stream(delta)` token callback (Req 9.3), wrapped in
      `call_with_resilience`; loads the Prompting_Guidance at construction and includes it
      every invocation (Req 15.1); parses the strict JSON contract; built lazily so import
      needs no boto3
    - _Requirements: 3.2, 3.3, 4.4, 9.3, 15.1_
    - _Design: Component 5 (BedrockAuthorClient), Author prompt design_
    - _Offline-testable via injected fake boto3 client factory (no real Bedrock). Verify with pytest._
  - [x] 10.6 Write unit tests for BedrockAuthorClient with a fake client factory
    - Streaming token callback fires, JSON parse, resilience refresh path, guidance included —
      all with an injected fake client (zero network)
    - _Requirements: 3.3, 4.4, 9.3, 15.1_
    - In `bakeoff/tests/test_quality_optimizer.py`
  - [x] 10.7 Implement `bakeoff/quality/optimizer/backends.py` offline bundle
    - `OptimizerBackend` dataclass (name, answer_adapter_factory, judge_scorer,
      closeness_scorer, **retrieval: RetrievalBackend**, author), `AuthorJudgeConflictError`,
      and `build_offline_backend(...)` wiring `QualityOfflineAdapter` factory +
      `StubJudge`-backed `JudgeScorer` + fake-embed `TurnClosenessScorer` +
      **`FakeRetrievalBackend`** + `OfflineAuthorClient`; mirrors `build_offline_scorers` in
      `bakeoff/quality/main.py`. The Judge MAY be supplied the
      `GROUNDING_ABSTENTION_EXCERPT` (Req 15.4)
    - _Requirements: 10.4, 10.6, 4.1, 4.5, 13.1, 15.4, 16.3_
    - _Design: Component 1 (Backend wiring)_
    - _Offline-testable. Verify with pytest._

- [x] 11. PersistentSessionInlineAdapter and live backend
  - [x] 11.1 Implement `bakeoff/quality/optimizer/inline_session_adapter.py`
    - `PersistentSessionInlineAdapter`: `promptOverrideConfiguration` with
      `promptType=ORCHESTRATION`, `promptCreationMode=OVERRIDDEN`, base template =
      `config.QUALITY_OPT_INLINE_TEMPLATE` (no `$prompt_session_attributes$`); **no**
      `actionGroups`, **no** `knowledgeBases`; one stable `sessionId` per conversation
      (`session_id_for(item, rep)`, default `opt-{name}-{item_id}-{rep}`); **one**
      `invoke_inline_agent` per turn sending that turn's utterance as `inputText`/`$question$`
    - **Retrieval-always (Req 13.4/13.9):** `send_fragments` defaults `True`; the per-turn
      fragments (from the held-constant memoized `RetrievalBackend`) are concatenated
      **inline** into the visible `$question$` via `assemble_context` — **never** via
      `promptSessionAttributes` and **never** via `sessionAttributes`. The model MAY still
      decline to use them (retrieval-always ≠ answer-always, Req 13.8)
    - `history_mode="server"` (rely on server-side session history) with documented
      `history_mode="explicit"` fallback via `inlineSessionState.conversationHistory`;
      `thinking_honored=False`, `history_mode`, and `grounding_fragment_ids` recorded on
      `raw`; wrapped in `call_with_resilience`; `per_turn_answers` in order
    - _Requirements: 3.6, 12.1, 12.4, 13.4, 13.8, 13.9_
    - _Design: Component 9 (PersistentSessionInlineAdapter), Inline-agent + prompt-override design_
    - _Offline-testable via injected fake client factory (no real Bedrock). Verify with pytest._
  - [x] 11.2 Write the inline no-noise fidelity unit test (mandatory)
    - Render the request the adapter builds and assert: `promptCreationMode == "OVERRIDDEN"`
      and the base template is the minimal one with **no** `$prompt_session_attributes$`;
      `actionGroups` and `knowledgeBases` absent/empty; **neither** `promptSessionAttributes`
      **nor** `sessionAttributes` set; **one** `invoke_inline_agent` per turn under a single
      stable `sessionId`; the rendered prompt contains our instruction + the turn question
      **with the turn's retrieved fragments rendered inline**; **grounding parity** (the
      fragment ids rendered to the model equal those the judge received for that turn); and
      the **absence** of every orchestration marker (`"actionGroup"`, `"action group"`,
      `"function_call"`, `"<tools>"`, `"Thought:"`, `"Action:"`, `"Observation:"`,
      `"you have access to"`)
    - _Requirements: 3.6, 13.4, 13.7, 13.9 (and the new fidelity invariant)_
    - _Design: No-noise unit test (mandatory)_
    - In `bakeoff/tests/test_persistent_session_adapter.py` (new) or near
      `bakeoff/tests/test_inline_agent.py`. **Mandatory (not optional).**
  - [x] 11.3 Write property test for the inline fidelity invariant
    - **Property 23: Inline orchestration prompt contains only our instruction/question/inline-fragments**
      — over arbitrary items + instructions: OVERRIDDEN minimal template (no
      `$prompt_session_attributes$`), no action groups/knowledge bases, one invoke per turn
      under one stable sessionId, **neither** attribute channel set, the turn's fragments
      rendered **inline** in the question, grounding parity, and no orchestration markers
    - **Validates: Requirements 3.6, 13.4, 13.9**
    - In `bakeoff/tests/test_quality_optimizer_pbt.py`, tagged
      `Feature: closed-loop-prompt-optimizer, Property 23: ...`, ≥ 100 iterations
  - [x] 11.4 Implement `build_live_backend(...)` in `backends.py`
    - Wire `PersistentSessionInlineAdapter` factory + real Opus `JudgeScorer` + Embed v4
      `TurnClosenessScorer` + **`build_retrieval_backend()`** (OpenSearch preferred / local
      fallback) + `BedrockAuthorClient` (default author = Sonnet 4.6); raise
      `AuthorJudgeConflictError` if `author_model == config.JUDGE_MODEL_ID`
    - _Requirements: 10.5, 10.6, 4.1, 4.2, 4.4, 4.5, 16.1, 16.2_
    - _Design: Component 1 (build_live_backend)_
    - _Offline-testable for construction via fake client factory. Verify with pytest._
  - [x] 11.5 Write unit test for live backend construction with a fake client factory
    - Live backend builds with injected fake client factories (Bedrock + OpenSearch); refuses
      when author == judge; retrieval defaults to OpenSearch-preferred / local-fallback
    - _Requirements: 10.5, 4.2, 16.1, 16.2_
    - In `bakeoff/tests/test_quality_optimizer.py`

- [x] 13. IterationController (Phase A)
  - [x] 13.1 Implement `bakeoff/quality/optimizer/controller.py` loop
    - `IterationController.run_phase_a`: seed iteration-0 champion (a permitted
      `variants_for_model` variant or explicit `seed_instruction`, default `full_stack`),
      persist its baseline audit record, then iterate: score champion (retrieval-always via
      `JudgeInLoopScorer`) → select failures (answering-when-unsure first) → author challenger
      (streaming author tokens to the emitter, guidance included) → score challenger →
      promote iff significant → persist `IterationRecord` + `AuditRecord` (durable) → emit
      events → advance `ConvergenceTracker`; **sequential within the model**; resume-aware
      (skip iterations whose records are durable via `completed_iteration_ids`); failures
      written to the disposable errors store, never the SoT
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 1.10, 3.5, 8.1, 8.6, 6.6, 10.3, 11.4_
    - _Design: Component 7 (IterationController), Error Handling (crash resume, per-model state)_
    - _Offline-testable. Verify with pytest._
  - [x] 13.2 Implement Phase A tuning-slice scoping
    - Reuse `bakeoff/quality/dataset.py::split_items` seeded by `config.QUALITY_OPT_SPLIT_SEED`;
      Phase A iterates **only** on the held-out Tuning_Slice; the Author only ever receives
      failures drawn from the Tuning_Slice
    - _Requirements: 7.1, 7.2, 7.6, 7.7_
    - _Design: Two-phase train/test_
    - _Offline-testable. Verify with pytest._
    

- [x] 14. PhaseBValidator (Phase B)
  - [x] 14.1 Implement `bakeoff/quality/optimizer/validate.py`
    - `PhaseBResult` + `PhaseBValidator.validate`: score the converged champion on the
      Validation_Set **only** (the split complement) at `config.QUALITY_OPT_PHASE_B_REPS`
      (> Phase A) reusing `JudgeInLoopScorer` (retrieval-always), reporting triad + CI; the
      Author is never invoked; the final reported number is always the Phase B value
    - _Requirements: 7.3, 7.4, 7.5, 7.7_
    - _Design: Component 8 (PhaseBValidator)_
    - _Offline-testable. Verify with pytest._
  - [x] 14.2 Write unit/example tests for Phase B
    - Phase B reps > Phase A and CI present (7.4); final reported number is the Phase B value
      (7.5); the Author is never invoked in Phase B (7.7)
    - _Requirements: 7.4, 7.5, 7.7_
    - In `bakeoff/tests/test_quality_optimizer.py`

- [x] 15. OptimizerEventEmitter (SSE, per Model_Channel)
  - [x] 15.1 Implement `bakeoff/quality/optimizer/events.py`
    - `OptimizerEventEmitter.emit(event_type, model, payload)` publishing over the **existing**
      `SSEBroker` unchanged; every payload stamped `payload["model_channel"] = model`; new
      event **types only** (`optimizer_champion_scored` carrying
      `abstention_reward_mean`/`answered_when_unsure_rate`/`retrieval_backend`,
      `optimizer_author_token`, `optimizer_iteration_completed`, `optimizer_converged`,
      `optimizer_phase_b`)
    - _Requirements: 9.1, 9.7, 9.10, 9.11_
    - _Design: Component 11 (OptimizerEventEmitter), Per-iteration SSE event shape_
    - _Offline-testable. Verify with pytest._

- [x] 16. PerModelOrchestrator and ViewRegistry (concurrency gate)
  - [x] 16.1 Implement `ViewRegistry` in `bakeoff/quality/optimizer/orchestrator.py`
    - `has_active_view(model)`, mark-active on Per_Model_View subscription open, clear on close
    - _Requirements: 1.11, 9.8_
    - _Design: Component 10 (ViewRegistry), Concurrency gate_
    - _Offline-testable. Verify with pytest._
  - [x] 16.2 Implement `PerModelOrchestrator.run`
    - Run the two per-model `IterationController` + `PhaseBValidator` loops **concurrently iff
      every running model has an active Per_Model_View** (`asyncio.gather`); otherwise run
      sequentially; **always sequential within a model**; record the concurrent-vs-sequential
      decision
    - _Requirements: 1.9, 1.10, 1.11_
    - _Design: Component 10 (PerModelOrchestrator)_
    - _Offline-testable. Verify with pytest._

- [x] 17. Optimizer CLI phases
  - [x] 17.1 Implement `bakeoff/quality/optimizer/main.py` phases
    - `iterate` (Phase A via the orchestrator), `validate` (Phase B), `all`; offline/live
      backend factory selection wiring `build_offline_backend` / `build_live_backend`
      (incl. the retrieval backend selection); writes `quality_opt_results.json`
    - _Requirements: 1.9, 7.3, 7.4, 7.5, 10.4, 10.5, 10.6, 16.1_
    - _Design: Architecture (CLI), Component 10_
    - _Offline-testable. Verify with pytest._
  - [x] 17.2 Implement the live quota guard and author/judge refusal surfacing
    - Reuse `bakeoff/quality/main.py::_bakeoff_run_looks_active`: `--backend live` is refused
      while a bake-off run looks active unless `--force`; surface `AuthorJudgeConflictError`
      as a clean refusal-to-start
    - _Requirements: 10.7, 4.2_
    - _Design: Error Handling (quota guard)_
    - _Offline-testable. Verify with pytest._

- [x] 18. Additive FastAPI routes
  - [x] 18.1 Add optimizer routes to `bakeoff/app.py`
    - `POST /api/quality/optimize/start` (body: backend, models, threshold, stop_limit, reps
      overrides, retrieval backend, `--force`; loopback-only; refuses live while a bake-off
      run looks active unless forced), `GET /api/quality/optimize/status`,
      `GET /api/quality/optimize/history?model=...` (ordered prompt-version history with
      diffs, scores, accept/reject); wire the `ViewRegistry` to the SSE subscription lifecycle
      (open Per_Model_View subscription → model viewable; close → cleared); optimizer events
      ride the existing `/api/stream`; inherit the existing `is_loopback_host` enforcement;
      bake-off streaming untouched
    - _Requirements: 9.1, 9.7, 8.5, 10.7, 12.5, 12.6_
    - _Design: Component 12 (FastAPI surface), Live dashboard design_
    - _Offline-testable. Verify with pytest._

- [x] 19. Quality_Tab Per_Model_View UI
  - [x] 19.1 Implement the `Per_Model_View` component(s) in the TS/Vite SPA (`bakeoff/ui/`)
    - One dedicated view per Target_Model (sub-tabs or side-by-side): champion vs challenger
      triad scores **with CIs** across iterations (error bars = `ci_half_width`); the Author's
      reasoning streamed live (`optimizer_author_token`); the current champion prompt text; the
      prompt diff vs the prior version with a **≥ 2-version lookback** selector; accept/reject
      decision badge; each view filters `/api/stream` to its own `model_channel` and attributes
      every score/prompt/diff/rationale to its model
    - _Requirements: 9.2, 9.3, 9.4, 9.5, 9.6, 9.8, 9.9, 9.10, 9.11_
    - _Design: Live dashboard / Quality-Tab design_
    - _Offline-testable (component build). Verify with pytest for the API side; UI build per the SPA toolchain._
  - [x] 19.2 Wire the Per_Model_View subscription to the `ViewRegistry` and data sources
    - Opening a Per_Model_View subscription marks the model viewable (drives the concurrency
      gate); consume `/api/stream` (filtered by `model_channel`) and
      `GET /api/quality/optimize/history?model=...` for the lookback
    - _Requirements: 9.8, 1.11, 8.5_
    - _Design: Concurrency gate, Live dashboard design_

- [x] 20. Checkpoint — full pipeline wired
  - Ensure all tests pass, ask the user if questions arise.

- [x] 21. End-to-end offline integration
  - [x] 21.1 Write the end-to-end offline mini-loop integration test
    - Drive a full mini-loop with the offline backend (`QualityOfflineAdapter` +
      `StubJudge`-backed `JudgeScorer` + fake-embed `TurnClosenessScorer` +
      `FakeRetrievalBackend` + `OfflineAuthorClient`) over a small slice: seed → author →
      judge → promote/reject → converge → Phase B. Assert the loop converges, the
      iteration/audit/result stores are complete and consistent, Phase B is evaluated only on
      the validation complement, **retrieval is invoked on every turn with the same fragments
      reaching the model and the judge**, abstention is scored with its weight, and **zero
      network calls** occur
    - _Requirements: 1.1, 6.3, 7.3, 7.5, 8.1, 10.1, 10.4, 13.1, 13.7, 14.2_
    - _Design: Testing Strategy (End-to-end offline integration test)_
    - In `bakeoff/tests/test_quality_optimizer_e2e.py` (new). Verify with pytest.

- [x] 22. Migration / fresh-start
  - [x] 22.1 Implement the `reset` CLI subcommand
    - Add `reset` to `bakeoff/quality/optimizer/main.py` that empties the old one-shot
      artifacts (`data/bakeoff/quality_outcomes.jsonl`, `quality_prompts.json`,
      `quality_optimizer_report.json`, and the old judge/errors stores
      `quality_judge_scores.jsonl`, `quality_run_errors.jsonl`); the Optimizer reads none of
      these for any decision. Note the fragment-free default is removed (retrieval-always) and
      the new retrieval/guidance/abstention config is in effect. Keep `MULTI_TURN_BLOCKS` /
      `variants_for_model` in `prompts.py` **only** as the iteration-0 seed source
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 13.2_
    - _Design: Migration / Fresh Start_
    - _Offline-testable. Verify with pytest._

- [x] 23. Final checkpoint
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test sub-tasks and may be skipped for a faster MVP —
  **except Task 11.2 (the inline no-noise fidelity test), which the design designates
  mandatory** and is therefore left non-optional.
- **[LIVE / MANUAL]** tasks are Task 4.5 (ALPHA OpenSearch smoke against account
  `948580600005`) and Task 11.6 (inline persistent-session live probe). Both require real
  AWS / real Bedrock behind the bake-off-active quota guard and are **not** part of the
  offline `pytest` suite. A live backend wiring smoke run is likewise a manual operator
  action; all live logic is otherwise covered offline with injected fake client factories.
- Each task references specific requirement sub-clauses and the design component it
  implements, for traceability.
- Every property test (P1–P29) is tagged `Feature: closed-loop-prompt-optimizer, Property
  {n}: {text}` and runs ≥ 100 Hypothesis iterations. A failing property test records the
  Hypothesis falsifying example via the PBT status tool.
- Verification after every coding sub-task: `.venv/bin/python -m pytest bakeoff/tests/ -q`
  (this is **not** a Brazil workspace).
- Out of scope (no tasks): changing the speed/quality bake-off study, latency measurement,
  and any auth / non-loopback exposure. The retrieval substrate is now *invoked* by the
  quality path on every turn but is **held constant and read-only** — never tuned or
  mutated (Req 12).

### Coverage of the 29 correctness properties

| Property | Task | Lives near |
|---|---|---|
| P1 Promotion iff significant | 3.4 | stats |
| P2 Decision depends only on triad | 9.4 | convergence |
| P3 Champion monotonic non-decreasing | 9.3 | convergence |
| P4 Failure selection k-lowest + evidence (unsure-first) | 8.2 | failures |
| P5 Non-usable challenger = non-improving | 10.3 | author |
| P6 Challengers authored, never menu | 10.4 | author / controller |
| P7 CI half-width formula + monotonicity | 3.2 | stats |
| P8 Gain absolute + percentage | 3.3 | stats |
| P9 Convergence counter + stop rule | 9.2 | convergence |
| P10 Author never sees validation conv | 13.7 | controller (split scope) |
| P11 Phase scoping (A tuning / B validation) | 14.3 | controller + validate |
| P12 Deterministic split | 13.6 | controller (split) |
| P13 Complete audit record per iteration | 13.4 | controller + store |
| P14 Append-only round-trip + history lookback | 5.4 | store |
| P15 Resume skips exactly durable iterations | 13.5 | controller + store |
| P16 Deterministic ids | 2.2 | ids |
| P17 Truncated-tail tolerance | 5.5 | store |
| P18 Offline backend zero network | 21.2 | end-to-end |
| P19 Per-model stream isolation | 15.3 | events |
| P20 Per-model loop isolation | 16.5 | orchestrator |
| P21 Concurrency gated on visualization | 16.4 | orchestrator |
| P22 Author/judge distinct | 10.8 | backends / author |
| P23 Inline orchestration fidelity (inline fragments + grounding parity) | 11.3 | inline adapter |
| P24 Retrieval invoked every turn | 7.3 | judge_loop |
| P25 Champion & challenger identical fragments per turn | 7.4 | judge_loop / retrieval |
| P26 Judge grounds on same fragments as model | 7.5 | judge_loop |
| P27 Abstention monotonicity | 7.6 | judge_loop |
| P28 Offline fake retrieval zero network | 4.3 | retrieval |
| P29 Backend selection prefers OpenSearch / falls back local | 4.4 | retrieval |

## Task Dependency

Build order is strictly inner-to-outer (config → ids/stats/retrieval → store/guidance →
judge_loop → failures → convergence → author/backends → inline adapter → controller →
validate → events → orchestrator → CLI → routes → UI → e2e → migration). Where parallelism
is possible across **distinct source files**, independent implementation modules can be
built in parallel once `config.py` exists: `ids.py`, `stats.py`, `retrieval.py`, and
`events.py` have no cross-dependencies; `prompting_guidance.py` is standalone;
`failures.py` only needs `judge_loop.py`; `convergence.py` only needs `stats.py`;
`judge_loop.py` needs `retrieval.py` + `stats.py`.

Two scheduling caveats:

1. **Shared test files serialize.** All `*` property tests append to the single file
   `bakeoff/tests/test_quality_optimizer_pbt.py`, all unit tests to
   `bakeoff/tests/test_quality_optimizer.py`, and the SSE/API tests to
   `bakeoff/tests/test_app.py`. Tasks that append to the **same** test file are logically
   independent (each adds a new test function) but must be executed **sequentially** to avoid
   write conflicts — they are placed in separate waves below for that reason.
2. **Per-model RUNTIME concurrency is NOT a build-task parallelism note.** The orchestrator
   running the two Target_Models' loops concurrently is a runtime behavior gated on each
   model having an active Per_Model_View (Req 1.11); it does not affect how these build tasks
   are scheduled.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1", "3.1", "4.1", "6.1", "15.1"] },
    { "id": 2, "tasks": ["5.1", "7.1", "9.1", "2.2"] },
    { "id": 3, "tasks": ["5.2", "8.1", "10.1", "3.2"] },
    { "id": 4, "tasks": ["5.3", "10.5", "10.7", "11.1", "4.2", "6.2"] },
    { "id": 5, "tasks": ["11.2", "11.4", "13.1", "14.1", "7.2", "3.3"] },
    { "id": 6, "tasks": ["13.2", "16.1", "11.6", "4.5", "10.2", "5.4"] },
    { "id": 7, "tasks": ["16.2", "11.5", "10.6", "3.4"] },
    { "id": 8, "tasks": ["17.1", "13.3", "8.2"] },
    { "id": 9, "tasks": ["17.2", "18.1", "14.2", "9.2"] },
    { "id": 10, "tasks": ["19.1", "22.1", "16.3", "9.3"] },
    { "id": 11, "tasks": ["19.2", "17.3", "9.4"] },
    { "id": 12, "tasks": ["21.1", "18.2", "15.2", "10.3"] },
    { "id": 13, "tasks": ["22.2", "19.3", "10.4"] },
    { "id": 14, "tasks": ["10.8"] },
    { "id": 15, "tasks": ["11.3"] },
    { "id": 16, "tasks": ["13.4"] },
    { "id": 17, "tasks": ["13.5"] },
    { "id": 18, "tasks": ["13.6"] },
    { "id": 19, "tasks": ["13.7"] },
    { "id": 20, "tasks": ["14.3"] },
    { "id": 21, "tasks": ["15.3"] },
    { "id": 22, "tasks": ["16.4"] },
    { "id": 23, "tasks": ["16.5"] },
    { "id": 24, "tasks": ["4.3"] },
    { "id": 25, "tasks": ["4.4"] },
    { "id": 26, "tasks": ["7.3"] },
    { "id": 27, "tasks": ["7.4"] },
    { "id": 28, "tasks": ["7.5"] },
    { "id": 29, "tasks": ["7.6"] }
  ]
}
```

Notes on the graph: each leaf sub-task appears in exactly one wave; the PBT tasks all write
`test_quality_optimizer_pbt.py`, so they occupy distinct waves (one PBT per wave); the unit
tests share `test_quality_optimizer.py` and likewise occupy distinct waves; implementation
tasks that edit the same source file (e.g. `store.py` 5.1/5.2/5.3, `retrieval.py` 4.1 then
its tests, `backends.py` 10.7/11.4, `controller.py` 13.1/13.2, the optimizer CLI
17.1/17.2/22.1) are placed in different waves. `retrieval.py` (4.1) lands in wave 1 so it is
available before `judge_loop.py` (7.1, wave 2), `backends.py` (10.7/11.4), the inline adapter
(11.1), and the controller (13.1). The `[LIVE/MANUAL]` tasks 4.5 and 11.6 are scheduled like
any build task (they author the probe/smoke scripts) but are excluded from the offline suite.
Wave IDs are contiguous from 0.
