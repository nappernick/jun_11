# bakeoff/ — Reranker Eval Harness Contract

Model-agnostic reranker evaluation framework. This package defines the **shared contract** (types, protocols, sample data) that all downstream agents build against.

## Pipeline Stages

```
FREEZE → RERANK → SCORE → AGGREGATE → DECIDE → RENDER
```

| Stage | Responsibility | Implementer |
|-------|---------------|-------------|
| FREEZE | Lock fixtures from sample/live retrieval | harness agent |
| RERANK | Run each Reranker adapter on frozen fixtures | harness agent |
| SCORE | Compute per-query metrics (nDCG, recall, MRR, latency) | metrics agent |
| AGGREGATE | Roll up by slice, compute CI, abstain curves | metrics agent |
| DECIDE | Apply Gates, pick winner, explain | decide agent |
| RENDER | Dashboard visualization | dashboard agent |

## The Contract (`contract.py`)

All types are pure-stdlib dataclasses with `to_dict()`/`from_dict()` round-trip. No numpy/pandas dependency.

### Core Types

- **`Candidate`** — a document presented to a reranker (node_id, text, source_metadata).
- **`Fixture`** — a frozen eval query with gold answers, slice tags, and answerability class.
- **`RankedDoc`** — one document in a reranker's output (rank 0 = best, raw_score model-native, norm_score ∈ [0,1]).
- **`Reranker`** (Protocol) — the adapter interface every model must implement.

### Reranker Protocol Contract

```python
class Reranker(Protocol):
    @property
    def id(self) -> str: ...
    def rerank(self, query: str, candidates: list[Candidate], top_k: int) -> list[RankedDoc]: ...
```

Rules:
- `rank` 0 = best document.
- `raw_score` = model-native (logit, probability, etc.).
- `norm_score` = comparable [0,1] via `normalize.squash()` or `PlattCalibrator`.
- **NEVER throw on a bad/corrupt document** — score it low (0.0 norm).
- Transport failures (network, timeout, 5xx) **MAY** throw.

### Results Types

- **`ScoredRow`** — one model × one query evaluation.
- **`AbstainPoint`** — one point on the abstention operating-characteristic curve.
- **`ModelMeta`** — model metadata (params, seq_len, license, etc.).
- **`Gates`** — pass/fail thresholds (accuracy_bar, latency_budget_ms, false_answer_ceiling).
- **`ResultsFile`** — the full output shape; see `results_to_json` / `results_from_json`.

## Three-Way Abstain Classification

Every fixture has an `answerability` field with one of three values:

| Class | Meaning | `expect_abstain` | Gold |
|-------|---------|-------------------|------|
| `unanswerable` | No correct answer exists anywhere | `True` | `gold_node_ids = {}` |
| `answerable_retrievable` | Answer exists AND is in the candidate set | `False` | gold ∩ candidates ≠ ∅ |
| `answerable_not_retrieved` | Answer exists but is NOT in the candidate set | `True` | gold ≠ {} but gold ∩ candidates = ∅ |

### Critical Scoring Rule

A correct abstain on `answerable_not_retrieved` must **NOT** be charged as a false-abstain. The system correctly identified that no good answer was available in the candidate set — the retrieval system failed, not the reranker.

- **False abstain** = model abstains on `answerable_retrievable` (answer was there, model missed it).
- **False answer** = model answers on `unanswerable` or `answerable_not_retrieved` (no good answer available, model answered anyway).

## Matched Operating-Point Rule

Cross-model abstention comparison is done at a **fixed `false_answer_rate`**, never a shared raw threshold.

Each model has its own raw score distribution. A threshold of 0.3 means completely different things for Cohere (unit [0,1] scores) vs. Qwen3 (yes/no logit margins). To compare abstention quality:

1. For each model, sweep thresholds and build the `abstain_curve` (FAR → abstain_recall).
2. Pick the operating point where `false_answer_rate ≤ gates.false_answer_ceiling`.
3. Compare `abstain_recall` at that matched FAR.

This ensures a model with better calibration isn't penalized by an arbitrary shared threshold.

## Named Seams (Stubs)

These integration points are intentionally left as stubs. They must never be hardcoded:

| Seam | Purpose | Stub behavior |
|------|---------|---------------|
| **OpenSearch retrieval** | Fetch live candidates for a query | Returns fixture candidates directly |
| **Auth** | Verify caller identity/scope | Always permits |
| **Abstention gate** | Runtime "should I answer?" decision | Not invoked during eval |
| **Persona → scope mapping** | Map user attributes to scope filter | Identity (pass-through) |

## Sample Data

- `sample/sample_fixtures.jsonl` — 12 synthetic fixtures (SYNTHETIC, dev/test only).
- `sample/sample_results.json` — valid ResultsFile with 3 fake models, 2 slices (SYNTHETIC).

## Score Normalization

Reuses `normalize.py` (copied from parent `rerank_bench/`):
- **Tier A** `squash(raw, kind)` — label-free, fixed function (sigmoid for logit/margin, clamp for unit).
- **Tier B** `PlattCalibrator` — per-model fit on labeled (raw, rel) pairs.

See `normalize.py` docstring for the three rules that prevent per-query normalization.
