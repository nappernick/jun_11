# Implementation Plan: Optimizer ragas + GEPA integration

## Overview

Two tiers, both additive and flag-gated (flags default **off**), built behind adapter seams
with deterministic offline fakes so the offline suite stays green whether or not `ragas` /
`gepa` are installed. Tier 1 first (ragas as a non-deciding cross-check + retrieval
diagnostic); Tier 2 second (the standalone GEPA engine as a gated alternative model-runner).
Nothing existing is removed and no existing decision role changes.

Grounding (verified 2026-06-05): `gepa==0.0.27` (standalone `gepa-ai/gepa`) and `dspy==3.2.1`
are already installed in `.venv`; `ragas` is **not** installed and nothing imports
ragas/gepa today. The live GEPA path binds to `from gepa import optimize, GEPAAdapter,
EvaluationBatch, GEPAResult` + `gepa.utils.stop_condition.MaxMetricCallsStopper` (the
rollout-budget mechanism). All design symbols were read directly from source (see `design.md`).

Sourcing caveat (Req 18): ragas / DSPy / GEPA are external open-source frameworks, not
Amazon-internal guidance; no internal primary source applies to this throwaway local harness;
the ragas Bedrock model ids and GEPA budget numbers are assumptions to confirm; any
judge-derived number must be re-validated before defending a decision upward.

Verify (Python): `.venv/bin/python -m pytest bakeoff/tests/ -q` (offline, network-free; must
stay green throughout, with no dependency on ragas/gepa being importable).
Verify (frontend, only if `bakeoff/ui` is touched): `bun run build` in `bakeoff/ui`.

Commit cadence: one commit per task (subject-line `ragas-gepa(tN): ...`); keep each task under
~500 changed lines / ~10 files. Do not push (separate, explicit step).

---

## Tasks

### Tier 1 — ragas as a non-deciding cross-check + retrieval diagnostic

- [ ] 1. Tier-1 configuration + dependency manifest
  - Add to the `QUALITY_OPT_*` block of `bakeoff/config.py`: `QUALITY_OPT_RAGAS_CROSS_CHECK_ENABLED=False`,
    `QUALITY_OPT_RAGAS_RETRIEVAL_DIAG_ENABLED=False`, `QUALITY_OPT_RAGAS_BACKEND="fake"`,
    `QUALITY_OPT_RAGAS_LLM_MODEL_ID=JUDGE_MODEL_ID`, `QUALITY_OPT_RAGAS_EMBED_MODEL_ID=EMBED_MODEL_ID`,
    each with an "ASSUMPTION — confirm at implementation time" comment on the two model ids.
  - Pin `gepa` and add `ragas` to `requirements.txt` (documents the dependency; do **not**
    `pip install ragas` here — heavy tree, deferred per design Dependencies section).
  - _Requirements: 3.1, 3.2, 4.2, 4.3, 4.4, 16.4, 18.3_
  - _Files: `bakeoff/config.py`, `requirements.txt`_

- [ ] 2. `Ragas_Adapter` seam (`bakeoff/quality/optimizer/ragas_adapter.py`, NEW)
  - `RagasSignals` frozen value object (all `Optional`, incl. `backend`). `RagasAdapter`
    `runtime_checkable` Protocol with `name`, async `cross_check(...)`, async
    `retrieval_diagnostic(...)`. `FakeRagasAdapter` (deterministic content-word overlap,
    gold-presence by set intersection, zero network). `BedrockRagasAdapter` (lazy `import
    ragas`; wraps `QUALITY_OPT_RAGAS_LLM_MODEL_ID`/`EMBED_MODEL_ID`; each metric independently
    `try/except`→`None` with `WARNING`+`exc_info=True`; clear error if ragas absent).
    `build_ragas_adapter(name, *, llm_client=None, embed_client=None)`.
  - No boto3 / ragas import at module load (mirror `retrieval.py` import discipline).
  - _Requirements: 4.1, 4.2, 4.3, 5.1, 5.2, 5.4_
  - _Files: `bakeoff/quality/optimizer/ragas_adapter.py`_

- [ ] 3. Inject the adapter through the `OptimizerBackend` bundle
  - Add trailing `ragas_adapter: Optional["RagasAdapter"] = None` to the frozen
    `OptimizerBackend`. `build_offline_backend` → `build_ragas_adapter("fake")`;
    `build_live_backend` → `build_ragas_adapter(config.QUALITY_OPT_RAGAS_BACKEND)` lazily (same
    injectable-factory posture as judge/author/embedder; no AWS at import).
  - _Requirements: 5.3_
  - _Files: `bakeoff/quality/optimizer/backends.py`_

- [ ] 4. Additive ragas fields on `TurnVerdict` + `SliceScore`
  - `TurnVerdict`: trailing `ragas_faithfulness`, `ragas_factual_correctness`,
    `ragas_context_precision`, `ragas_context_recall`, `gold_node_present`, `ragas_backend`
    (all `Optional[...] = None`). `SliceScore`: trailing slice means
    `ragas_*_mean` + `gold_presence_rate`, aggregated in `_aggregate` over verdicts carrying a
    non-`None` value (mirroring `mean_closeness`).
  - _Requirements: 1.1, 2.4, 3.4, 17.1_
  - _Files: `bakeoff/quality/optimizer/judge_loop.py`_

- [ ] 5. `Ragas_Cross_Check` + `Retrieval_Diagnostic` in `JudgeInLoopScorer` (gated)
  - `__init__` gains `ragas_cross_check` / `retrieval_diagnostic` flags (default from config).
    In `_judge_turn`, **after** the unchanged triad/`overall`, an additive guarded block:
    only runs when a flag is on and `getattr(backend,"ragas_adapter",None)` is set; computes
    cross-check from the same `ans`+`frags`+gold reference; computes the diagnostic from the
    same `frags`; `gold_node_present = bool(set(item.gold_node_ids) & set(grounding_fragment_ids))`
    on gold turns, else `None`; whole block `try/except`→signals `None` on failure. `overall`
    and every decision-affecting field unchanged; ragas never enters `overall`.
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4, 2.5, 3.5, 11.1, 11.2, 13.3, 14.1_
  - _Files: `bakeoff/quality/optimizer/judge_loop.py`_

- [ ] 6. Persist + stream the ragas signals (additive)
  - Add the five trailing `ragas_*_mean` / `gold_presence_rate` optionals to `IterationRecord`
    and `AuditRecord` with `to_jsonl`/`from_jsonl` updated (read via `d.get` → `None` for old
    lines; `from_jsonl(to_jsonl(x))==x` holds). Populate them where records are built
    (`orchestrator._persist_iteration`). Add optional ragas keys to the `champion_scored` SSE
    payload in `events.py`.
  - _Requirements: 3.4, 8.3 (Tier-1 portion)_
  - _Files: `bakeoff/quality/optimizer/store.py`, `bakeoff/quality/optimizer/orchestrator.py`, `bakeoff/quality/optimizer/events.py`_

- [ ] 7. Tier-1 tests
  - `bakeoff/tests/test_ragas_adapter.py`: fake signals deterministic; gold-presence exact;
    network-free. `test_quality_optimizer` additions: config-off parity (decision fields +
    `IterationRecord` identical to flags-off; ragas fields `None`); flags-on populated with
    `overall` unchanged and promotion identical; adapter-raises → `None` signals + complete
    verdict; record round-trip with/without ragas fields; pre-feature JSONL loads with new
    fields `None`.
  - _Requirements: 3.2, 3.3, 5.1, 5.2, 17.1, 17.2, 17.3_
  - _Files: `bakeoff/tests/test_ragas_adapter.py`, `bakeoff/tests/test_quality_optimizer.py`_
  - _Gate: full offline suite green._

### Tier 2 — GEPA engine as a gated alternative model-runner

- [ ] 8. Tier-2 configuration
  - Add `QUALITY_OPT_TIER2_GEPA_ENABLED=False`, `QUALITY_OPT_GEPA_BACKEND="fake"`,
    `QUALITY_OPT_GEPA_PROPOSER_MODEL_KEY=QUALITY_OPT_V2_AUTHOR_MODEL_KEY`,
    `QUALITY_OPT_GEPA_ROLLOUT_BUDGET=0` (0 = derive from ladder), `QUALITY_OPT_GEPA_MAX_MERGE_INVOCATIONS=5`,
    `QUALITY_OPT_GEPA_NAMED_RAGAS_DIMENSIONS=("ragas_faithfulness","ragas_factual_correctness")`,
    budget/merge marked ASSUMPTION.
  - _Requirements: 6.4, 9.2, 9.3, 12.1_
  - _Files: `bakeoff/config.py`_

- [ ] 9. `GEPA_Engine` seam (`bakeoff/quality/optimizer/gepa_engine.py`, NEW) + `FakeGepaEngine`
  - `GepaEngine` Protocol (`name`, async `optimize(*, seed_instruction, metric, budget,
    proposer, merge_max) -> GepaResult`). `GepaResult` (best_instruction, best_score,
    per_dimension, history). `FakeGepaEngine` = deterministic propose→evaluate→Pareto→merge
    loop over the injected metric (reuses `OfflineAuthorClient` to author candidates); zero
    network. `build_gepa_engine(name=config.QUALITY_OPT_GEPA_BACKEND, ...)`.
  - _Requirements: 6.1, 6.2, 6.3, 6.4_
  - _Files: `bakeoff/quality/optimizer/gepa_engine.py`_

- [ ] 10. `GEPA_Metric` (Opus judge → score + feedback; named dimensions)
  - `GepaMetric` scores a candidate on the current rung via the existing
    `JudgeInLoopScorer.score_prompt`; returns `(score=SliceScore.triad_score (abstention-weighted),
    feedback_text)` assembled from worst-verdict judge `evidence` + abstention/ragas signals;
    exposes `per_dimension_mean` + named ragas dimensions; triad stays the sole decider.
  - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 8.1, 8.2, 8.4, 11.1, 11.2, 14.2, 14.3_
  - _Files: `bakeoff/quality/optimizer/gepa_engine.py`_

- [ ] 11. `Rollout_Budget` from the coverage ladder
  - Derive the GEPA budget from `build_rung_ladder(tuning)` (rung sizes/reps → rollout
    schedule; total metric-call budget from `QUALITY_OPT_GEPA_ROLLOUT_BUDGET` or ladder sum),
    expressed via `MaxMetricCallsStopper` for the live engine.
  - _Requirements: 9.1, 9.2, 9.3_
  - _Files: `bakeoff/quality/optimizer/gepa_engine.py`_

- [ ] 12. `LiveGepaEngine` (lazy, graceful)
  - Lazy `import gepa`; adapt `GepaMetric` to `gepa.core.adapter.GEPAAdapter` (`evaluate` +
    `make_reflective_dataset`); run `gepa.optimize` with the proposer model + budget stopper;
    raise a clear `RuntimeError` naming the package + alternative if `gepa` is absent (read
    `gepa/core/adapter.py` for exact method signatures at this point).
  - _Requirements: 6.4_
  - _Files: `bakeoff/quality/optimizer/gepa_engine.py`_

- [ ] 13. Orchestrator Tier-2 gate (KEEP list + proposer≠judge)
  - At the top of `PerModelOrchestrator._run_model_v2`: `if config.QUALITY_OPT_TIER2_GEPA_ENABLED:
    return await self._run_model_gepa(model, **opts)`. `_run_model_gepa` reuses
    `phase_a_split` (seeded), builds the rung ladder as budget, constructs `GepaMetric` over the
    backend's `JudgeInLoopScorer`, runs `GepaEngine.optimize`, validates via the existing
    `PhaseBValidator`. Enforce proposer≠judge (reuse `AuthorJudgeConflictError`; proposer =
    Sonnet, judge = Opus). Flag-off ⇒ the island/tournament path is byte-for-byte unchanged.
  - _Requirements: 6.1, 6.2, 6.3, 6.5, 10.1, 10.2, 10.3, 10.4, 10.5, 12.1, 12.2, 12.3, 15.1, 15.2, 15.3, 15.4_
  - _Files: `bakeoff/quality/optimizer/orchestrator.py`_

- [ ] 14. Dashboard: ragas as named dimensions on accepted candidates
  - Ensure the ragas dimensions ride inside the existing `per_dimension` dict on
    `champion_scored` / the v2 snapshot so the existing per-dimension renderer shows them
    with no UI contract change; add a thin label if the UI needs the new keys named.
  - _Requirements: 8.2, 8.3_
  - _Files: `bakeoff/quality/optimizer/events.py`, `bakeoff/ui/src/...` (only if a label map is needed)_

- [ ] 15. Tier-2 tests + caveat doc + full green
  - `bakeoff/tests/test_gepa_engine.py`: `FakeGepaEngine` runs propose→evaluate→Pareto→merge;
    `GepaMetric` returns `(score, feedback_text)` with abstention-weighted triad + named ragas
    dims; proposer≠judge enforced. Orchestrator test: Tier-2-off leaves island/tournament
    tests untouched; Tier-2-on (offline fakes) runs a GEPA model end-to-end through Phase B.
    Add the Req-18 external-source caveat to the optimizer README/docs note. Full offline suite
    green.
  - _Requirements: 6.x, 7.x, 8.x, 10.x, 12.x, 18.1, 18.2, 18.3, 18.4_
  - _Files: `bakeoff/tests/test_gepa_engine.py`, `bakeoff/tests/test_orchestrator_v2.py`, optimizer docs note_
  - _Gate: full offline suite green._

---

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1"], "rationale": "Config flags + dependency manifest unblock everything; flags default off." },
    { "wave": 2, "tasks": ["2", "4"], "rationale": "Ragas adapter seam and the additive verdict/slice fields are independent." },
    { "wave": 3, "tasks": ["3", "5"], "rationale": "Bundle injection + the gated scorer block depend on the seam and fields." },
    { "wave": 4, "tasks": ["6"], "rationale": "Persistence + SSE depend on the slice-level ragas means." },
    { "wave": 5, "tasks": ["7"], "rationale": "Tier-1 tests; hard gate before Tier 2." },
    { "wave": 6, "tasks": ["8", "9"], "rationale": "Tier-2 config + engine seam/fake are independent." },
    { "wave": 7, "tasks": ["10", "11"], "rationale": "Metric + budget build on the seam." },
    { "wave": 8, "tasks": ["12"], "rationale": "Live engine adapts to gepa once the metric/budget exist." },
    { "wave": 9, "tasks": ["13", "14"], "rationale": "Orchestrator gate + dashboard wiring depend on metric/engine." },
    { "wave": 10, "tasks": ["15"], "rationale": "Tier-2 tests + full green + caveat doc." }
  ]
}
```

## Notes

- **Offline-green invariant.** The offline suite must never import `ragas` or `gepa`; the
  fakes (`FakeRagasAdapter`, `FakeGepaEngine`) carry every offline test. Live adapters import
  lazily and degrade with a clear error.
- **Config-off parity is the load-bearing safety property.** Tasks 5/7/13/15 must prove that
  with every new flag off, behavior, records, and decisions are byte-identical to today.
- **Decision metric is untouched.** ragas never enters `overall` or any promotion path in
  either tier; the Opus triad stays the sole decider (Req 11).
- **No-interference.** A dashboard / optimizer may be running against this venv; tests are
  hermetic (tmp stores, offline backend) and do not restart shared processes. Do not
  `pip install ragas` mid-session.

## Out of scope

Tier 3 (full DSPy-program migration) — no build requirements here; see `design.md` "Out of
Scope". v2 visual redesign and v1 work are separately scoped.
