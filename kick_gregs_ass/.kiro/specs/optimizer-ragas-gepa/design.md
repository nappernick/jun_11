# Design — Optimizer ragas + GEPA integration

## Overview

This feature integrates two external open-source frameworks into the existing closed-loop
prompt optimizer under `bakeoff/quality/optimizer/`, in two build tiers, **without
removing or changing the behavior of anything that exists today**:

- **Tier 1 (build now, reversible, flag-gated).** ragas `Faithfulness` /
  `FactualCorrectness` are recorded on every `TurnVerdict` as a **non-deciding
  cross-check** (the exact role `closeness` plays today), and ragas `ContextPrecision` /
  `ContextRecall` plus a gold-node-presence flag are recorded per turn as a **retrieval
  diagnostic**. Everything is additive, behind config flags that default **off**, computed
  from the **same** answer + held-constant fragments the Judge already received, and
  offline-testable with a deterministic fake.
- **Tier 2 (build target, flag-gated, additive code path).** A standalone GEPA engine
  (reflective proposer + Pareto frontier + merge) is wired in as an **alternative
  per-model runner**, gated by a flag that defaults off. When on, GEPA replaces the
  hand-rolled author / island / tournament / merge machinery for that run; the Opus judge
  triad becomes GEPA's metric (returning a scalar score **and** natural-language
  feedback), the ragas signals become **named JudgeDimensions** the dashboard can show,
  and the coverage-ladder cadence becomes GEPA's rollout budget. The proven substrate —
  the `InvokeInlineAgent` answer path, OpenSearch retrieval, the SSE dashboard, the
  held-constant memoized retrieval, the seeded Phase-A/Phase-B split — is **kept**.

The whole design follows the repo's own established discipline: a single injectable
backend bundle (`OptimizerBackend`), duck-typed consumption, deterministic offline fakes
with **zero network**, and lazy live imports so importing a module never requires the
external package. The offline test suite stays green whether or not `ragas` and `gepa` are
installed.

### Sourcing & methodology honesty caveat (Req 18)

ragas, DSPy, and GEPA are **external / industry open-source frameworks**, not
Amazon-internal guidance. No Amazon-internal primary source (BuilderHub Golden Path,
internal code search, AWS Prescriptive Guidance) was consulted for this design, and none
applies — this is a deliberately throwaway, local research harness, and the internal-source
tools were not available in this execution environment. The ragas Bedrock endpoint / model
ids and any GEPA rollout-budget numbers are **assumptions to confirm at implementation
time**, surfaced in `config.py` as such. Any judge-derived number MUST be re-validated
before it is used to defend a decision upward. This mirrors the caveat already carried by
`closed-loop-prompt-optimizer`, `optimizer-quality-uplift`, and `bakeoff/README.md`.

---

## Architecture

```
                         ┌───────────────────────── KEEP LIST (unchanged) ─────────────────────────┐
                         │  InvokeInlineAgent answer path · OpenSearch/local/fake RetrievalBackend  │
                         │  (held-constant, memoized) · Opus JudgeScorer · SSE dashboard · Phase A/B │
                         └──────────────────────────────────────────────────────────────────────────┘
                                                          ▲
              build_offline_backend / build_live_backend  │  (OptimizerBackend bundle, frozen)
                                                          │
   OptimizerBackend(name, answer_adapter_factory, judge_scorer, closeness_scorer,
                    retrieval, author,  ── NEW ──►  ragas_adapter: Optional[RagasAdapter] = None)
                                                          │
        ┌─────────────────────────────────────────────────┴───────────────────────────────────────┐
        │                                                                                           │
  TIER 1 (default path: islands+tournament)                         TIER 2 (gated alternative runner)
        │                                                                                           │
  JudgeInLoopScorer._judge_turn                                   PerModelOrchestrator._run_model_v2
   ├─ retrieve()  (held constant, memoized)                         if QUALITY_OPT_TIER2_GEPA_ENABLED:
   ├─ judge.score_detailed → triad  (DECISION; unchanged)              GepaModelRunner.run(model, ...)
   ├─ abstention-weighted `overall`  (DECISION; unchanged)              ├─ Rollout_Budget ← build_rung_ladder
   └─ NEW, gated, non-deciding:                                         ├─ GepaMetric(JudgeInLoopScorer)
        Ragas_Cross_Check     → faithfulness, factual_correctness         → (score, feedback_text)
        Retrieval_Diagnostic  → context_precision/recall, gold_present     named dims = triad + ragas
        via backend.ragas_adapter  (FakeRagasAdapter | BedrockRagasAdapter)├─ GepaEngine.optimize(...)
                                                                          │   (FakeGepaEngine | LiveGepaEngine)
  additive fields on TurnVerdict / SliceScore / records / SSE              └─ Phase B (PhaseBValidator, KEEP)
                                                                          proposer(Sonnet) ≠ judge(Opus) enforced
```

New modules: `bakeoff/quality/optimizer/ragas_adapter.py` (Tier 1) and
`bakeoff/quality/optimizer/gepa_engine.py` (Tier 2). Everything else is additive edits to
existing modules.

---

## Components and Interfaces

### C1 — `Ragas_Adapter` seam (`ragas_adapter.py`, NEW)

The single seam that runs ragas metrics, mirroring `retrieval.py`'s structure (Protocol +
fake + live + builder, lazy live imports). It provides:

- **`RagasSignals`** — a frozen value object carrying the four ragas scores plus the
  gold-presence flag and the backend name, each `Optional[...]` so a metric that failed or
  was not requested is simply absent (Req 3.5):
  ```python
  @dataclass(frozen=True)
  class RagasSignals:
      faithfulness: Optional[float] = None
      factual_correctness: Optional[float] = None
      context_precision: Optional[float] = None
      context_recall: Optional[float] = None
      gold_node_present: Optional[bool] = None
      backend: Optional[str] = None        # "fake" | "bedrock" (Req 5.4)
  ```
- **`RagasAdapter`** — a `runtime_checkable` Protocol with a stable `name` and two async
  methods, each independently failure-tolerant:
  ```python
  async def cross_check(self, *, answer_text, fragments, reference_texts, question
                        ) -> tuple[Optional[float], Optional[float]]   # (faithfulness, factual_correctness)
  async def retrieval_diagnostic(self, *, fragments, reference_texts, gold_node_ids
                        ) -> tuple[Optional[float], Optional[float], Optional[bool]]
                                                # (context_precision, context_recall, gold_node_present)
  ```
  The diagnostic operates **only** on the fragments handed in (Req 2.2/13.3) — it never
  issues its own retrieval query.
- **`FakeRagasAdapter`** (`name="fake"`) — deterministic, network-free. Derives plausible
  scores from content-word overlap between the answer/fragments and the reference (the same
  technique `StubJudge._grounded_fraction` uses), and computes `gold_node_present` as
  `bool(set(gold_node_ids) & {f["id"] for f in fragments})`. Zero sockets / boto3 / HTTP
  (Req 5.1/5.2).
- **`BedrockRagasAdapter`** (`name="bedrock"`) — the live adapter. Lazily imports `ragas`
  inside the call (never at module load), wraps the Bedrock eval LLM
  (`config.QUALITY_OPT_RAGAS_LLM_MODEL_ID`, defaulting to the harness Judge model) and Embed
  v4 (`config.QUALITY_OPT_RAGAS_EMBED_MODEL_ID`) through ragas' Bedrock adapter, and runs
  `Faithfulness` / `FactualCorrectness` / `ContextPrecision` / `ContextRecall`. Any metric
  that raises (including `ImportError` when ragas is not installed) is caught and returned
  as `None` for that metric (Req 3.5) with a `WARNING` log carrying `exc_info=True` (never a
  bare `except: pass`).
- **`build_ragas_adapter(name=config.QUALITY_OPT_RAGAS_BACKEND, *, llm_client=None,
  embed_client=None) -> RagasAdapter`** — selects `"fake"` or `"bedrock"`; live clients are
  injectable so tests exercise the mapping with fakes.

### C2 — `OptimizerBackend` extension (`backends.py`, additive)

Add one **trailing** field with a default so the frozen dataclass stays backward-compatible
and the duck-typed `JudgeInLoopScorer` contract is unaffected:
```python
ragas_adapter: Optional["RagasAdapter"] = None   # Tier-1 cross-check seam (Req 5.3)
```
- `build_offline_backend(...)` sets `ragas_adapter=build_ragas_adapter("fake")` (always
  network-free; harmless when the flags are off).
- `build_live_backend(...)` sets `ragas_adapter=build_ragas_adapter(config.QUALITY_OPT_RAGAS_BACKEND)`
  lazily, with the same injectable-factory posture as the judge/author/embedder so importing
  the module needs no boto3 and tests pass fakes.

The adapter is injected through the **same** one-bundle seam as judge / closeness /
retrieval / author, so the whole outside world swaps in one move (Req 5.3).

### C3 — `Ragas_Cross_Check` + `Retrieval_Diagnostic` (`judge_loop.py`, additive, gated)

`JudgeInLoopScorer.__init__` gains two flags (defaulting from config, injectable for tests),
exactly as `abstention_weight` already does:
```python
ragas_cross_check: bool = config.QUALITY_OPT_RAGAS_CROSS_CHECK_ENABLED       # default False
retrieval_diagnostic: bool = config.QUALITY_OPT_RAGAS_RETRIEVAL_DIAG_ENABLED  # default False
```
In `_judge_turn`, **after** the existing triad/`overall` computation is complete and
unchanged, an additive, fully-guarded block runs:
1. It only does work when a flag is on **and** `getattr(self._backend, "ragas_adapter", None)`
   is not `None`; otherwise the new `TurnVerdict` fields stay `None` and the verdict is
   byte-identical in every decision-affecting field to today's (Req 3.3/3.5/17.3).
2. `cross_check` is computed from the **same** `ans` and `frags` the Judge received and the
   same gold/ideal reference (`gold_texts`, the turn reference) — never a re-retrieval
   (Req 1.2/2.2/13.3).
3. `gold_node_present` is `bool(set(item.gold_node_ids) & set(grounding_fragment_ids))` on
   turns that carry gold (turn-1 / gold turns); `None` on later `wants`-only turns that have
   no gold node id (Req 2.3). This is the mechanized form of the
   `optimizer-quality-uplift` Effort A/A1 "is the gold node in the retrieved fragments?"
   diagnosis.
4. The whole block is wrapped so any failure records the affected signal as `None` and the
   iteration continues on the Judge triad (Req 3.5).

`overall`, `dimensions`, `abstention_correct`, `answered_when_unsure`, and every other
decision-affecting field are untouched: ragas never enters `overall` or any promotion path
(Req 1.3/1.4/2.4/2.5/11.1/11.2).

### C4 — Data-model deltas (additive, round-trip-preserving)

All new fields are trailing `Optional[...] = None` so existing positional/keyword
construction and existing JSONL on disk keep working; readers use `d.get(...)` so old
records (missing the keys) deserialize to `None`.

- **`TurnVerdict`** (`judge_loop.py`): `ragas_faithfulness`, `ragas_factual_correctness`,
  `ragas_context_precision`, `ragas_context_recall`, `gold_node_present`, `ragas_backend`.
- **`SliceScore`** (`judge_loop.py`): slice means
  `ragas_faithfulness_mean` / `ragas_factual_correctness_mean` /
  `ragas_context_precision_mean` / `ragas_context_recall_mean` and `gold_presence_rate`,
  aggregated over verdicts that carry a non-`None` value (mirroring `mean_closeness`).
- **`IterationRecord`** + **`AuditRecord`** (`store.py`): the same slice-level ragas
  optionals, with `to_jsonl`/`from_jsonl` updated so `from_jsonl(to_jsonl(x)) == x` holds
  including the new fields and old lines round-trip to `None`.
- **SSE** (`events.py`): `champion_scored` gains optional `ragas` block keys; in Tier 2 the
  ragas dimensions ride inside the existing `per_dimension` dict so the dashboard's existing
  per-dimension renderer shows them with no UI contract change (Req 8.3).

### C5 — Tier-1 configuration (`config.py`, additive in the `QUALITY_OPT_*` block)

```python
QUALITY_OPT_RAGAS_CROSS_CHECK_ENABLED: bool = False      # Req 3.1/3.2
QUALITY_OPT_RAGAS_RETRIEVAL_DIAG_ENABLED: bool = False    # Req 3.1/3.2
QUALITY_OPT_RAGAS_BACKEND: str = "fake"                    # "fake" | "bedrock"
QUALITY_OPT_RAGAS_LLM_MODEL_ID: str = JUDGE_MODEL_ID       # Req 4.2/4.3 — ASSUMPTION, confirm
QUALITY_OPT_RAGAS_EMBED_MODEL_ID: str = EMBED_MODEL_ID     # Req 4.2/4.3 — ASSUMPTION, confirm
```
The two model-id lines carry an explicit "assumption to confirm at implementation time"
comment (Req 4.4).

### C6 — `GEPA_Engine` seam (`gepa_engine.py`, NEW)

Mirrors C1's structure (Protocol + fake + live + builder, lazy live import):
- **`GepaEngine`** Protocol — `name: str`; `async def optimize(*, seed_instruction,
  metric, budget, proposer_model, merge_max) -> GepaResult` where `GepaResult` carries the
  best instruction, its score, the per-dimension breakdown, and the candidate/Pareto
  history.
- **`FakeGepaEngine`** (`name="fake"`) — a deterministic, network-free reflective
  proposer + Pareto-frontier + merge loop driven by the injected `metric` and a fake
  proposer (reuses `OfflineAuthorClient` to author candidates). It exercises the full
  contract (propose → evaluate → retain-on-Pareto → merge) so the offline suite covers the
  Tier-2 wiring with zero external dependency.
- **`LiveGepaEngine`** (`name="live"`) — lazily imports `gepa`, adapts the harness metric
  to gepa's `GEPAAdapter` (`evaluate` + `make_reflective_dataset`), and runs `gepa.optimize`
  with the proposer model and rollout budget. Lazy import → module import needs no `gepa`.
- **`build_gepa_engine(name=config.QUALITY_OPT_GEPA_BACKEND, ...)`** selector.

### C7 — `GEPA_Metric` (`gepa_engine.py`)

`GepaMetric` presents the existing Opus Judge to the engine (Req 7):
- For a candidate instruction it scores it on the current rung **via the existing
  `JudgeInLoopScorer.score_prompt`** (the same Judge implementation the rest of the study
  uses, Req 7.3) and returns:
  - **scalar score** = `SliceScore.triad_score`, the abstention-weighted per-conversation
    mean (Req 7.2);
  - **`feedback_text`** = natural-language critique assembled from the worst verdicts'
    judge `evidence` plus the abstention/ragas signals (Req 7.1/7.5), so the reflective
    proposer is conditioned on **why** a turn scored as it did.
- **Named JudgeDimensions** (Req 8.1/8.2): the metric exposes `per_dimension_mean`
  (faithfulness/correctness/completeness) **plus** the ragas means as named dimensions, and
  attributes a candidate's score movement across them, but all of them feed the **single**
  triad decision — they are never independent competing deciders (Req 8.4/11.2).
- The Judge triad remains the sole promotion-decision metric inside the engine (Req 7.4).

### C8 — `Rollout_Budget` from the coverage ladder

The GEPA budget is derived from `build_rung_ladder(tuning)` (the existing `Rung` ladder):
the rung sizes/reps define the rollout schedule (cheap early, broader as a candidate earns
it), and the total metric-call budget is read from config (Req 9.1/9.2). The numbers are
flagged as assumptions to confirm (Req 9.3).

### C9 — Orchestrator Tier-2 gate (`orchestrator.py`, additive)

`PerModelOrchestrator._run_model_v2` gains a single branch at its top:
```python
if config.QUALITY_OPT_TIER2_GEPA_ENABLED:
    return await self._run_model_gepa(model, **opts)   # NEW gated path
# ... existing island + tournament loop unchanged ...
```
`_run_model_gepa` reuses the **identical** `phase_a_split` (seeded tuning/validation,
Req 15), builds the rung ladder as the budget (C8), constructs the `GepaMetric` over the
existing backend's `JudgeInLoopScorer`, runs `GepaEngine.optimize`, then validates the
winning instruction through the **existing** `PhaseBValidator` (KEEP, Req 10). When the flag
is off (default), this branch is never taken and the island/tournament path is **byte-for-byte
unchanged** (Req 6.1/6.2/6.3/17). Proposer/judge separation (Req 12) is enforced by reusing
`build_live_backend`'s existing `AuthorJudgeConflictError` check and by configuring the GEPA
reflective proposer to use the Sonnet author model while the judge stays Opus.

### C10 — Tier-2 configuration (`config.py`, additive)

```python
QUALITY_OPT_TIER2_GEPA_ENABLED: bool = False                 # Req 6 (default off)
QUALITY_OPT_GEPA_BACKEND: str = "fake"                        # "fake" | "live"
QUALITY_OPT_GEPA_PROPOSER_MODEL_KEY: str = QUALITY_OPT_V2_AUTHOR_MODEL_KEY  # Sonnet (≠ Judge, Req 12)
QUALITY_OPT_GEPA_ROLLOUT_BUDGET: int = 0                      # 0 = derive from rung ladder; Req 9 — ASSUMPTION
QUALITY_OPT_GEPA_MAX_MERGE_INVOCATIONS: int = 5               # Req 6.3 — ASSUMPTION
QUALITY_OPT_GEPA_NAMED_RAGAS_DIMENSIONS: tuple[str, ...] = (
    "ragas_faithfulness", "ragas_factual_correctness",
)                                                             # Req 8.1
```

---

## Data Models

The complete additive field set (all `Optional`, all defaulting `None`, all
round-trip-preserving). No existing field is renamed, removed, retyped, or reordered.

| Type | New fields |
|------|-----------|
| `TurnVerdict` | `ragas_faithfulness`, `ragas_factual_correctness`, `ragas_context_precision`, `ragas_context_recall`, `gold_node_present`, `ragas_backend` |
| `SliceScore` | `ragas_faithfulness_mean`, `ragas_factual_correctness_mean`, `ragas_context_precision_mean`, `ragas_context_recall_mean`, `gold_presence_rate` |
| `IterationRecord` | `ragas_faithfulness_mean`, `ragas_factual_correctness_mean`, `ragas_context_precision_mean`, `ragas_context_recall_mean`, `gold_presence_rate` |
| `AuditRecord` | same five as `IterationRecord` |
| `RagasSignals` (new) | `faithfulness`, `factual_correctness`, `context_precision`, `context_recall`, `gold_node_present`, `backend` |
| `GepaResult` (new) | `best_instruction`, `best_score`, `per_dimension`, `history` |

Persistence invariant (design-critical): `from_jsonl(to_jsonl(x)) == x` continues to hold,
and a JSONL line written **before** this feature deserializes with every new field `None`
(read via `d.get(...)`), so existing `data/bakeoff/quality_opt_*.jsonl` stores load unchanged.

---

## Correctness Properties

### Property 1: ragas is recorded but never decides (Tier 1 + Tier 2)
For every turn, `overall` and the promotion decision are computed exactly as before; the
ragas fields are written but read by no decision path.
**Validates: Requirements 1.3, 1.4, 2.4, 2.5, 11.1, 11.2, 11.3**

### Property 2: same-input cross-check
The ragas faithfulness/factual-correctness for a turn are computed from the identical
`answer_text` and `fragments` the Judge scored, and the diagnostic reads the same memoized
fragments without re-querying retrieval.
**Validates: Requirements 1.2, 2.1, 2.2, 13.2, 13.3**

### Property 3: gold-presence is exact
`gold_node_present` for a gold turn equals `set(item.gold_node_ids) & set(grounding_fragment_ids) ≠ ∅`,
and is `None` for turns with no gold node id.
**Validates: Requirement 2.3**

### Property 4: config-off parity
With both Tier-1 flags off, every `TurnVerdict`/`SliceScore`/`IterationRecord`/`AuditRecord`
decision-affecting field, the promotion outcome, the convergence stop, and the Phase A/B
boundary are identical to pre-feature behavior; the new ragas fields are `None`.
**Validates: Requirements 3.2, 3.3, 3.4, 17.2, 17.3**

### Property 5: failure tolerance
A ragas computation that raises records the affected signal as `None` and the iteration
continues on the Judge triad; no exception propagates into the loop.
**Validates: Requirement 3.5**

### Property 6: offline is network-free
With the offline backend / fake adapters selected, no socket, boto3 client, or HTTP call is
made by the ragas or GEPA paths.
**Validates: Requirements 5.1, 5.2, 16.1**

### Property 7: one-move backend swap + provenance
The ragas adapter is injected through the same `OptimizerBackend` bundle as judge / closeness
/ retrieval / author, and each ragas signal records which backend produced it.
**Validates: Requirements 5.3, 5.4**

### Property 8: live ragas reads config, not literals
The live ragas adapter reads its Bedrock endpoint / model ids from config, which marks them
as assumptions to confirm.
**Validates: Requirements 4.1, 4.2, 4.3, 4.4**

### Property 9: GEPA replaces the search machinery only when enabled
With Tier-2 on, candidate proposal / selection / merge are performed by the GEPA engine and
the island/tournament code is not run; with Tier-2 off the island/tournament path is
unchanged.
**Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.5**

### Property 10: judge-as-metric returns score + feedback, abstention-weighted, sole decider
The GEPA metric derives its scalar from the abstention-weighted triad of the same Opus Judge
and returns feedback text to the proposer; the triad is the sole promotion decision inside
GEPA.
**Validates: Requirements 7.1, 7.2, 7.3, 7.4, 7.5**

### Property 11: ragas as named dimensions feeding one decision
With Tier-2 on, ragas metrics appear as named JudgeDimensions in the per-dimension breakdown
and on the dashboard for an accepted candidate, but feed the single triad decision rather
than competing as independent deciders.
**Validates: Requirements 8.1, 8.2, 8.3, 8.4**

### Property 12: budget from the ladder
The GEPA rollout budget is configured from the coverage-ladder cadence, read from config,
which marks the numbers as assumptions.
**Validates: Requirements 9.1, 9.2, 9.3**

### Property 13: KEEP list intact under Tier 2
With Tier-2 on, the inline-agent answer path, OpenSearch retrieval, the SSE stream, and the
held-constant memoized retrieval behave exactly as before; GEPA is confined to proposal /
selection / merge.
**Validates: Requirements 10.1, 10.2, 10.3, 10.4, 10.5**

### Property 14: proposer ≠ judge across both tiers
The proposer model (AuthorClient in Tier 1, GEPA reflective proposer in Tier 2) is a
different model from the Judge; configuring them equal makes the run refuse to start; Opus is
reserved for the Judge.
**Validates: Requirements 12.1, 12.2, 12.3**

### Property 15: retrieval held constant and read-only
Retrieval is invoked read-only every turn, memoized per `(turn-query)` so champion and
challenger get byte-identical fragments, and the diagnostic reads those same fragments.
**Validates: Requirements 13.1, 13.2, 13.3, 13.4, 14.1**

### Property 16: abstention stays first-class in both tiers
Correct decline is rewarded and answering-when-unsure penalized in the metric, in Tier 1 and
Tier 2.
**Validates: Requirements 14.2, 14.3**

### Property 17: seeded two-phase boundary preserved
Tier-1 and Tier-2 search run only on the seeded ~20% tuning slice; the converged champion is
validated on the reserved ~80%; the validation set is never seen by the proposer; the split
is deterministic.
**Validates: Requirements 15.1, 15.2, 15.3, 15.4**

### Property 18: local throwaway posture preserved
The harness stays loopback-only, no-auth, no-PII, on the local venv with bun for JS, adding
ragas/gepa to the local environment using the existing Bedrock credential chain (no new
secrets).
**Validates: Requirements 16.1, 16.2, 16.3, 16.4**

### Property 19: external-source caveat carried
The feature's docs state ragas/DSPy/GEPA are external (not Amazon-internal), that no internal
source was consulted, that the Bedrock/model/budget values are assumptions, and that
judge-derived numbers must be re-validated upward.
**Validates: Requirements 18.1, 18.2, 18.3, 18.4**

---

## Error Handling

- **ragas metric failure / `ragas` not installed.** Caught inside the adapter; the affected
  signal returns `None`, logged at `WARNING` with `exc_info=True`; the turn's verdict is
  complete on the Judge triad (Req 3.5). Never a bare `except: pass`.
- **`gepa` not installed while Tier-2 live is requested.** `LiveGepaEngine` raises a clear
  `RuntimeError` naming the missing package and the supported alternative (the fake engine /
  the island path), exactly as `OpenSearchRetrievalBackend._ensure_client` does today.
- **Author == Judge under Tier 2.** `build_live_backend` raises `AuthorJudgeConflictError`
  before any network call, so the orchestrator/route maps it to a clean 4xx and the run never
  starts (Req 12.2).
- **Malformed persisted record.** The store's existing crash-tolerant reader drops only a
  truncated trailing line; the new optional fields default to `None` on any record missing
  them, so old stores load unchanged.

---

## Testing Strategy

Runner: `.venv/bin/python -m pytest bakeoff/tests/ -q` (offline, network-free). Frontend (if
touched): `bun run build` in `bakeoff/ui`. The full suite must stay green throughout, with
**no** dependency on `ragas` or `gepa` being importable.

- **Ragas adapter (offline fake):** deterministic signals; `gold_node_present` is exact set
  intersection; zero network.
- **Config-off parity:** with both Tier-1 flags off, a scored slice produces verdicts whose
  decision-affecting fields and the resulting `IterationRecord` equal the pre-feature run
  (golden comparison), and the new ragas fields are `None`.
- **Cross-check / diagnostic on:** with the fake adapter and flags on, the ragas fields are
  populated from the same fragments/answer, `overall` is unchanged versus flags-off, and the
  promotion decision is identical (Property 1/2/4).
- **Failure tolerance:** an adapter stubbed to raise yields `None` signals and a complete
  verdict (Property 5).
- **Round-trip:** `from_jsonl(to_jsonl(rec)) == rec` for records with and without ragas
  fields; a pre-feature JSONL line loads with the new fields `None`.
- **GEPA (offline fake):** `FakeGepaEngine` runs propose→evaluate→Pareto→merge over the
  `GepaMetric`; the metric returns `(score, feedback_text)` with the abstention-weighted
  triad as the score and ragas as named dimensions; proposer≠judge enforced; Tier-2-off
  leaves the island/tournament tests untouched.
- **Reused green suites:** `test_orchestrator_v2`, `test_tournament`, `test_events_v2`,
  `test_quality_optimizer` continue to pass unchanged.

Live verification (out of the offline gate, recorded in hand-off only): a single flag-on
rung-0 step against alpha OpenSearch to confirm the live ragas adapter and (optionally) the
live GEPA engine produce real signals — never required for the suite to be green.

---

## Dependencies & environment

**Verified state (2026-06-05):** `gepa` (0.0.27, MIT — the standalone `gepa-ai/gepa` engine)
and `dspy` (3.2.1, MIT, which depends on `gepa`) are **already installed** in the `.venv`
(py3.12); `ragas` is **not** installed and no source file imports it today. This feature pins
`gepa` and adds `ragas` to the repo-root `requirements.txt` (Req 16.4 — no new secrets, the
existing Bedrock credential chain is reused). Both `ragas` and `gepa` are imported **lazily**
only on their live paths, so importing any module and the entire offline test suite work
whether or not they are installed: the offline fakes (`FakeRagasAdapter`, `FakeGepaEngine`)
carry the suite, the live `BedrockRagasAdapter` raises a clear error when `ragas` is absent,
and the live `LiveGepaEngine` binds to the already-present standalone `gepa` engine.
Installing `ragas`'s heavy transitive tree (langchain / pydantic / datasets) is deferred to a
deliberate, isolated step — it is **not** required for the offline suite to be green and is
kept out of band to avoid perturbing the installed `dspy`/`gepa` stack. JS tooling stays on
bun.

---

## Out of Scope — Tier 3 (future platform decision; no build here)

The full DSPy-program migration (the previously-deferred "Effort C"): the answer path,
retrieval, and judge re-expressed as DSPy modules; a custom `dspy.LM` Bedrock
`InvokeInlineAgent` adapter; MIPROv2 / BootstrapFewShot demo optimization; ragas as the full
eval harness with synthetic test-data generation and align-LLM-as-judge calibration. Tier 3
carries **no** build requirements in this spec and would be scoped as its own platform-decision
spec. Until then, the GEPA-engine integration (Tier 2) is the build target and the Tier-2
KEEP list stays in place.
