# OSS Reranker Bakeoff — Integration Contract

Single source of truth for every module. Build agents MUST conform to the schemas
and interfaces here so independently-authored pieces integrate without drift.

## Goal
Decide which of three open-weights rerankers — **Ettin-1b**, **Qwen3-Reranker
(0.6B + 4B)**, **Nemotron-1b-v2** — is worthy of further consideration alongside
**Bedrock Cohere Rerank 3.5** and **Cohere Rerank v4** (pro/fast). Verdict =
LLM-judge pairwise win-rate on the real-pool disagreement set (primary) + visual
combo/separation dashboard (secondary) + latency (GPU) + token-window capability.

## Working dir
All paths relative to `query_sagemaker/src/rerank_bench/oss_bakeoff/`.
Python: use `../.venv/bin/python` (torch 2.12, transformers 5.10, sentence-transformers 5.5, cohere 7, boto3 1.43, MPS available).

## Proven facts (from smoke test — do not re-litigate)
| model_id        | repo                                    | kind    | max_context | scoring |
|-----------------|-----------------------------------------|---------|-------------|---------|
| ettin-1b        | cross-encoder/ettin-reranker-1b-v1      | logit   | 7999        | CrossEncoder.predict([(q,doc)]) -> logit |
| qwen3-0.6b      | Qwen/Qwen3-Reranker-0.6B                | margin  | 131072      | causalLM, logit(yes)-logit(no) at last token |
| qwen3-4b        | Qwen/Qwen3-Reranker-4B                  | margin  | 131072      | same as 0.6b |
| nemotron-1b-v2  | nvidia/llama-nemotron-rerank-1b-v2      | logit   | 4096        | trust_remote_code, "question: {q} passage: {d}" -> logits[0] |
| cohere-3.5      | Bedrock cohere.rerank-v3-5:0 (alpha)    | unit    | ~4096 tok   | bedrock-agent-runtime.rerank, relevanceScore in [0,1] |
| cohere-v4-pro   | SageMaker cohere-rerank4-pro-sandbox    | unit    | larger      | cohere.SagemakerClient.rerank (needs endpoint deployed) |
| cohere-v4-fast  | SageMaker cohere-rerank4-fast-sandbox   | unit    | larger      | same |

`kind` -> norm via `normalize.squash`: unit=clamp, logit/margin=sigmoid (per-SCORE, never per-query).

## AWS
- alpha profile -> acct 948580600005, us-west-2: OpenSearch retrieval + Bedrock 3.5 + Bedrock judge (Claude Opus 4.8 = `us.anthropic.claude-opus-4-8`, via `bedrock-runtime` converse/invoke).
- nick-caia profile -> acct 429134228173, us-east-1: SageMaker (Cohere v4 endpoints + the OSS GPU latency endpoint). Exec role `arn:aws:iam::429134228173:role/executor-sage`. g5 quota = 1 endpoint.
- NEVER hardcode keys; use boto3 Session(profile_name=...).

## DATA SCHEMAS (pinned)

### pools.json  (EXISTS — produced by retrieve.py)
```
{ "meta": {...}, "pools": { "<query>": [ {"node_id","title","text","char_len"}, ... ] } }
```

### scored.json  (produced by run_local.py — the execution spine)
```
{
  "meta": {"generated_at","pool_size","n_queries","models":[...]},
  "models": {
    "<model_id>": {
      "kind": "logit|margin|unit",
      "max_context": int,
      "device": "mps|cuda|cpu|bedrock|sagemaker",
      "queries": {
        "<query>": {
          "ranking": ["node_id", ...],        // best-first, full pool
          "raw":  {"node_id": float, ...},     // raw model score per doc
          "norm": {"node_id": float, ...},     // squash(raw,kind) per doc
          "top_id": "node_id",
          "latency_ms": float|null             // per-(query) full-pool score latency, local; null for cohere-v4 until run
        }, ...
      }
    }, ...
  }
}
```

### metrics.json  (produced by metrics.py — BUILD AGENT)
```
{
  "per_model": { "<model_id>": {
      "sep_top_minus_restmean_median": float, "sep_top_minus_2nd_median": float,
      "top1_norm_median": float, "latency_ms_p50": float|null, "max_context": int } },
  "disagreements": [   // every (query, model_a, model_b) where top_id differs — the JUDGE INPUT
     {"id": "d0001", "query": str, "model_a": str, "model_b": str,
      "ranking_a": [node_id...], "ranking_b": [node_id...],
      "top_a": node_id, "top_b": node_id} ],
  "pair_disagree_rate": { "<model_a>__<model_b>": float },   // fraction of queries where top1 differs
  "combo_stability": { "<model_id>": {"top1_winrate_by_doc": {...}, "note": "analytic, secondary"} }
}
```

### judge.json  (produced by judge.py — BUILD AGENT)
```
{
  "meta": {"judge_model":"us.anthropic.claude-opus-4-8","n_pairs":int,
           "method":"pairwise preference, anonymized, BOTH orderings, tie-default"},
  "verdicts": [ {"id":"d0001","query":str,"model_a":str,"model_b":str,
                 "winner":"a|b|tie","confidence":float,
                 "order1":"a|b|tie","order2":"a|b|tie","consistent":bool,"rationale":str} ],
  "winrate_matrix": { "<model>": {"<other>": {"wins":int,"losses":int,"ties":int,"winrate":float} } },
  "model_score": { "<model_id>": float }   // overall judge-derived quality, 0..1
}
```

## MODULE DELIVERABLES (one build agent each; write the file + return a summary)

- **metrics.py**: `compute(scored_path='scored.json') -> writes metrics.json`. Pure stdlib + json. Extract disagreement set across ALL model pairs (OSS vs OSS, OSS vs Cohere). CLI: `python metrics.py`.
- **judge.py**: `judge(metrics_path='metrics.json', limit=None) -> writes judge.json`. Bedrock `bedrock-runtime` on ALPHA profile, model `us.anthropic.claude-opus-4-8`, `converse` API. For each disagreement: show query + the two FULL rankings rendered as doc titles+snippets, labelled "Ranking 1"/"Ranking 2" (NEVER reveal model names). Ask which better answers the query; allow "tie". Run BOTH orderings (swap which model is Ranking 1) to cancel position bias; winner only if consistent, else tie. Parse strict JSON from the model. Resilient: on parse/transport error, record tie + note, never crash the batch. CLI: `python judge.py [--limit N]`.
- **dashboard.html**: standalone single-file HTML + Plotly (CDN) that loads scored.json + metrics.json + judge.json (via fetch on a local file:// or a tiny `python -m http.server`) and renders: (1) per-model separation box/bars, (2) latency bars, (3) cross-model top-1 disagreement table, (4) judge win-rate matrix heatmap + per-model overall score, (5) max-context capability bar. Match the dark aesthetic of `../dashboard/src` if practical. Must degrade gracefully if a file is missing.
- **sagemaker_infer.py**: a SageMaker HF-DLC custom inference handler (`model_fn`, `input_fn`, `predict_fn`, `output_fn`) that serves ALL THREE oss models in ONE container, lazy-loading per request `{"model":"ettin-1b|qwen3-0.6b|qwen3-4b|nemotron-1b-v2","query":str,"docs":[str]}` -> `{"scores":[float],"kind":str}`. Reuse the EXACT scoring math from models.py (below). fp16, cuda. Must keep loaded models cached. This is for the LATENCY-ONLY GPU run.
- **deploy_bench.py**: ONE atomic script: package sagemaker_infer.py + model code as model.tar.gz -> S3 (nick-caia), create Model (HF DLC GPU image us-east-1) + endpoint-config (ml.g5.2xlarge) + endpoint, wait InService, run a warm latency benchmark (discard cold start; N reps at realistic pool sizes 5/10/20) for each oss model, write `latency_gpu.json`, then **ALWAYS teardown in a `finally` block** (delete endpoint, config, model) AND register an independent watchdog (a forked `subprocess` / `atexit` / background thread that force-deletes after a hard cap of 45 min no matter what). Print explicit teardown confirmation. CLI: `python deploy_bench.py`. DO NOT RUN — author only; the human reviews + runs it serially.

## models.py interface (SPINE — authored by orchestrator, build agents import/mirror it)
```
class OSSReranker:
    id: str; kind: str; max_context: int; device: str
    def score_pairs(self, query: str, docs: list[str]) -> list[float]   # raw scores, doc order preserved
def load(model_id: str, device=None, dtype='float16') -> OSSReranker   # ids: ettin-1b qwen3-0.6b qwen3-4b nemotron-1b-v2
```

## Hard rules
- Pointwise: every reranker scores each (query,doc) independently. A combo's ranking is the restriction of the full-pool ordering. So combos add NO new ranking signal — they are PRESENTATIONAL. Lead all conclusions with judge win-rate on full real pools.
- Never generate queries/labels from gold docs (saturation failure mode).
- Reranker is a RANKER — no abstention metric anywhere.
- Latency: GPU number is "unoptimized eager-PyTorch serving" — label it as a ceiling, not OSS's best.
