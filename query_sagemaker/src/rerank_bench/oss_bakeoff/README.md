# OSS Reranker Bakeoff

Decide which of three open-weights rerankers is worthy of further consideration
alongside **Cohere Rerank 3.5** (Bedrock) and **Cohere Rerank v4** (SageMaker):

| model | repo | scoring | max context | license |
|-------|------|---------|-------------|---------|
| Ettin-1b | `cross-encoder/ettin-reranker-1b-v1` | ModernBERT cross-encoder logit | **7,999** | Apache-2.0 |
| Qwen3-Reranker 0.6B / 4B | `Qwen/Qwen3-Reranker-{0.6B,4B}` | causal-LM yes/no margin | **131,072** | Apache-2.0 |
| Nemotron-1b-v2 | `nvidia/llama-nemotron-rerank-1b-v2` | bidirectional Llama logit (trust_remote_code) | **4,096** | NVIDIA OM |

## Why this design (vs the prior saturated eval)
- **Real BM25 pools, never gold-from-query.** Queries (`queries.json`, 42 natural
  employee questions) are scored against live `faq_evidence_a` retrievals
  (`pools.json`). The earlier labeled eval saturated (gold@rank0 ≈99%, nDCG ≈0.93)
  precisely because each query was written from its own gold doc; we don't do that.
- **Pointwise truth.** Every reranker — OSS *and* Cohere — scores each (query, doc)
  independently. A combo's ranking is just the restriction of the full-pool order,
  so combos add NO new ranking signal. The verdict therefore rests on **LLM-judge
  pairwise win-rate on the full real pools** (primary); combo/separation views are
  presentational (the visual the prior runs liked).
- **No abstention.** The reranker is a ranker; abstention is a downstream concern
  and is measured nowhere here.
- **Quality is hardware-independent; latency is not.** Reranker logits are identical
  on any FP backend, so quality is gathered once on the GPU endpoint and latency is
  measured there too — nothing heavy runs on the laptop.

## Files
| file | role |
|------|------|
| `queries.json` | 42 realistic queries (topic-spanning, not gold-derived) |
| `retrieve.py` → `pools.json` | freeze identical real BM25 pools (20/query) all models score |
| `models.py` | OSS adapter spine (Ettin / Qwen3 / Nemotron), shared by local + GPU |
| `cohere_adapters.py` | Cohere 3.5 (Bedrock) + v4 (SageMaker) baselines, same interface |
| `run_local.py` | score one model into `scored.json` (used for cohere-3.5; OSS run on GPU) |
| `sagemaker_infer.py` | HF-DLC custom handler serving all 3 OSS models from one container |
| `deploy_bench.py` | **atomic** deploy → GPU quality scoring + latency → **guaranteed teardown** (finally + 75-min watchdog) |
| `cohere_v4_bench.py` | atomic deploy → score v4 pro/fast → teardown |
| `metrics.py` → `metrics.json` | separation, top-1 disagreement set (judge input), pair-disagree rates |
| `judge.py` → `judge.json` | pairwise preference judge (Sonnet 4.6, anonymized, both orderings, tie-default) → win-rate |
| `analyze.py` → `final_verdict.json` | fuse judge + separation + latency + window into the verdict table |
| `dashboard.html` | standalone Plotly view: separation, latency, disagreement table, win-rate heatmap, context bars |

## Run order
```
python retrieve.py                          # -> pools.json (done)
python run_local.py cohere-3.5              # Bedrock baseline (done)
python deploy_bench.py --watchdog-min 75    # OSS quality+latency on g5.2xlarge, auto-teardown
python cohere_v4_bench.py fast              # v4 baselines (sequential, never 2 endpoints at once)
python cohere_v4_bench.py pro
python metrics.py && python judge.py && python analyze.py
python -m http.server  # then open dashboard.html
```

## Cost safety
Every GPU script tears down in a `finally:` that fires on success, error, AND
Ctrl-C, plus an independent watchdog (daemon thread + atexit) that force-deletes
the endpoint after a hard cap regardless of main-thread state, and a
`--teardown-only` mode + `prove_clean` that lists and confirms zero resources
remain. Quota is 1 g5.2xlarge endpoint; runs are sequential.

## Smoke / capability findings (pre-eval)
All three load+score on transformers 5.x / torch 2.x. Token-window capability:
**Qwen3 128K ≫ Ettin 8K > Nemotron 4K = Cohere 3.5** — but the FAQ corpus is almost
all <900-token docs, so window is a model *capability* differentiator, not a
workload advantage on this corpus. (See `smoke_findings.json`.)
