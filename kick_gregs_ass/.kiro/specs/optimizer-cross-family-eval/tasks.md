# Implementation Plan

All changes are **additive** and **config-gated**: with every new flag at its default
(`False` / `None`) the optimizer behaves exactly as today (Req 4.1, 4.2). Tasks are ordered so
the shared config contract lands first, then each requirement's code, then tests.

- [ ] 1. Add the gated configuration contract (shared dependency)
  - Add the Round-cadence, cross-family-Author, and audit knobs to `bakeoff/config.py`, all
    gates defaulting off so a default run is byte-for-byte today's behavior.
  - Knobs: `QUALITY_OPT_ROUND_CADENCE_ENABLED`, `QUALITY_OPT_ROUND_STEPS`;
    `QUALITY_OPT_CROSS_FAMILY_AUTHOR_ENABLED`, `QUALITY_OPT_AUTHOR_MODEL_ID`,
    `QUALITY_OPT_AUTHOR_FAMILY`, `QUALITY_OPT_AUTHOR_ACCEPTS_TEMPERATURE`,
    `QUALITY_OPT_AUTHOR_TEMPERATURE`, `QUALITY_OPT_JUDGE_FAMILY`;
    `QUALITY_OPT_AUDIT_ENABLED`, `QUALITY_OPT_AUDIT_JUDGE_MODEL_ID`,
    `QUALITY_OPT_AUDIT_JUDGE_FAMILY`, `QUALITY_OPT_AUDIT_INTERVAL`,
    `QUALITY_OPT_AUDIT_SAMPLE_SIZE`, `QUALITY_OPT_AUDIT_DIVERGENCE_THRESHOLD`.
  - _Requirements: 1.5, 2.1, 2.2, 2.3, 3.1, 3.2, 4.2; Assumptions A1, A2, A3_

- [ ] 2. Req 1 — In_Loop_Signal scorer (no Judge)
  - [ ] 2.1 Add `JudgeInLoopScorer.score_in_loop` + `_score_turn_in_loop` to
    `bakeoff/quality/optimizer/judge_loop.py`: mirror `score_prompt`'s generate +
    held-constant retrieval + closeness path but **never** call `judge_scorer.score_detailed`.
    Derive each turn's `overall` from the closeness composite blended with the existing
    abstention-reward branch; reuse `_generate_conversation` and `_aggregate`. Returns a
    `SliceScore` so `select_failures`/`PromotionDecider`/aggregation are reused unchanged.
    - _Requirements: 1.1, 1.2, 4.3_

- [ ] 3. Req 1 — Round cadence in the island loop
  - [ ] 3.1 In `bakeoff/quality/optimizer/island.py`, make `IslandLoop.step()` a dispatcher:
    when `QUALITY_OPT_ROUND_CADENCE_ENABLED` run `_step_round()`, else the unchanged legacy
    body (renamed `_step_single()`).
  - [ ] 3.2 Implement `_step_round()`: baseline + `QUALITY_OPT_ROUND_STEPS` in-round Author
    iterations scored only by `score_in_loop` (no Opus), keeping the best in-round candidate;
    then **one** Opus adjudication at the Round's conclusion via `_score`; promotion decided by
    `PromotionDecider.decide` on the Opus scores; reuse `_author`/`_strip_stance`/`gain_report`/
    `make_prompt_diff`/`StepDetail` and emit the same `iteration_completed` event.
    - _Requirements: 1.1, 1.3, 1.4, 1.5, 4.1_

- [ ] 4. Req 2 — non-Anthropic, family-aware Author
  - [ ] 4.1 Genericize `build_author_prompt` in `bakeoff/quality/optimizer/author.py`: drop the
    "the Author is itself a Claude 4.x model" assertion from the docstring and frame the
    embedded guidance explicitly as guidance about the Target_Model's family.
    - _Requirements: 2.8, 2.9_
  - [ ] 4.2 In `bakeoff/quality/optimizer/backends.py` add `model_family(model_id, declared=None)`
    and `AuthorJudgeFamilyConflictError(AuthorJudgeConflictError)`.
    - _Requirements: 2.3, 2.4_
  - [ ] 4.3 In `build_live_backend`, gate on `QUALITY_OPT_CROSS_FAMILY_AUTHOR_ENABLED`: resolve
    the Author from the separate slot `QUALITY_OPT_AUTHOR_MODEL_ID` (explicit arg still wins),
    raise a clear config error when the gate is on but the id is unset, and apply the
    family-aware Author≠Judge guard; keep today's identity-only guard when the gate is off.
    Pass `accepts_temperature`/`temperature` from config into `BedrockAuthorClient` (defaults
    preserve today's behavior).
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7_

- [ ] 5. Req 3 — cross-family audit seam
  - [ ] 5.1 New module `bakeoff/quality/optimizer/audit.py`: `AuditItem`, `AuditSample`,
    `DivergenceReport`; pure `obfuscate` (idempotent), `ranking_divergence` (normalized
    Kendall-tau in [0,1], symmetric, identity-zero, 1.0 reversed), `evaluate_self_preference`;
    `AuditJudge` (lazy injectable `client_factory`, credential-resilience posture, no new
    secret); `AuditSeam.maybe_run(round_index, samples)` honoring the interval, obfuscating
    before scoring, and flagging above threshold.
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 4.5_
  - [ ] 5.2 In `backends.py` add optional `OptimizerBackend.audit_judge` (default `None`),
    built only when `QUALITY_OPT_AUDIT_ENABLED`.
    - _Requirements: 3.1, 4.2_
  - [ ] 5.3 In `events.py` add `EVENT_AUDIT_FLAG` + `audit_flag(...)` and include it in
    `OPTIMIZER_EVENT_TYPES`.
    - _Requirements: 3.5_
  - [ ] 5.4 Wire the gated, defensive audit hook into `orchestrator._run_model_v2`: build an
    `AuditSeam` from `backend.audit_judge`, draw a sample from the Phase A tuning slice, call
    `maybe_run` at the configured interval, and emit/log the flag. No-op when disabled; never
    aborts the run on audit failure; samples only the tuning slice.
    - _Requirements: 3.2, 3.3, 3.4, 3.5, 4.4, 4.6_

- [ ] 6. Tests
  - [ ] 6.1 New `bakeoff/tests/test_cross_family_eval_pbt.py` with the 8 design properties
    (Hypothesis, ≥100 examples, tagged): Round cadence keeps the Judge out of the in-round loop
    (P1); promotion follows the Round-conclusion Judge (P2); family-aware guard (P3);
    provider-aware temperature (P4); audit interval (P5); obfuscation before audit (P6);
    well-formed divergence (P7); flag iff divergence > threshold (P8).
  - [ ] 6.2 Example/non-regression tests: config-slot wiring (Req 2.1/3.1), genericized contract
    (Req 2.8/2.9), gates-off legacy path + `audit_judge is None` (Req 4.1/4.2), held-constant
    retrieval on the in-loop path (Req 4.3).
  - _Requirements: 1.1–1.5, 2.1–2.9, 3.1–3.5, 4.1–4.4_

- [ ] 7. Verify
  - Run the new suites plus the existing quality-optimizer suites; confirm gates-off
    non-regression and fix any failures.
  - _Requirements: all_
