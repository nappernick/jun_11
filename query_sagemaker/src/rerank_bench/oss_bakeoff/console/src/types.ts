// Shared data contract for the OSS Reranker Bake-off console.
// All JSON artifacts live in /public/data and are fetched at runtime.

export interface QueryScore {
  ranking: string[];                 // node_ids, best-first
  raw: Record<string, number>;       // node_id -> raw model score
  norm: Record<string, number>;      // node_id -> squashed [0,1]
  top_id: string;
  latency_ms: number | null;         // per-query scoring round-trip
}
export interface ModelScored {
  kind: 'logit' | 'margin' | 'unit';
  max_context: number | null;
  device: string;
  queries: Record<string, QueryScore>;
}
export interface Scored {
  meta: { generated_at?: string; n_queries?: number; models?: string[] };
  models: Record<string, ModelScored>;
}

export interface JudgeVerdict {
  id: string; query: string; model_a: string; model_b: string;
  winner: 'a' | 'b' | 'tie'; confidence: number;
  order1: string; order2: string; consistent: boolean; rationale: string;
  top_a?: string; top_b?: string;
}
export interface WinCell { wins: number; losses: number; ties: number; winrate: number }
export interface Judge {
  meta: { judge_model?: string; n_pairs?: number; method?: string };
  verdicts: JudgeVerdict[];
  winrate_matrix: Record<string, Record<string, WinCell>>;
  model_score: Record<string, number>;   // overall Opus judge win-rate 0..1
}

export interface Disagreement {
  id: string; query: string; model_a: string; model_b: string;
  ranking_a: string[]; ranking_b: string[]; top_a: string; top_b: string;
}
export interface Metrics {
  per_model: Record<string, {
    sep_top_minus_restmean_median: number; sep_top_minus_2nd_median: number;
    top1_norm_median: number; latency_ms_p50: number | null; max_context: number;
  }>;
  disagreements: Disagreement[];
  pair_disagree_rate: Record<string, number>;
}

export interface LatencyCell { p50: number; p90: number; p99: number; n: number }
export type Latency = Record<string, Record<string, LatencyCell>>;  // model -> pool -> cell

// RAGAS reference-free metrics (ragas_results.json). Populates once the run finishes.
export interface RagasAgg {
  context_precision: number | null;
  context_relevance: number | null;
  faithfulness: number | null;
  response_relevancy: number | null;
  response_groundedness: number | null;
  ragas_overall: number | null;
  n: number;
}
export interface RagasRow {
  reranker: string; query: string; top_ids: string[]; answer: string;
  metrics: Record<string, number | null>; errors: Record<string, string> | null;
}
export interface Ragas {
  meta: { gen_model?: string; judge_model?: string; embed_model?: string; topk?: number; metrics?: string[] };
  aggregate: Record<string, RagasAgg>;
  rows: RagasRow[];
}

export interface PoolItem { node_id: string; title: string; text: string; char_len: number }
export interface Pools { meta: unknown; pools: Record<string, PoolItem[]> }

// Combo-5 ground-truth top-1 accuracy eval (combo5_results.json). Stratified pool-of-5:
// did the reranker rank the known-correct FAQ #1? random_floor 0.2 = chance baseline.
export type Combo5Tier = 'random' | 'mixed' | 'hard';
export type Combo5TierOrOverall = Combo5Tier | 'overall';
export type Combo5Type =
  | 'single_hop_specific'
  | 'single_hop_abstract'
  | 'multi_hop_specific'
  | 'multi_hop_abstract';

export interface Combo5Cell {
  acc: number;
  ci: [number, number];       // [lo, hi] confidence interval on acc
  mrr: number;
  n: number;
  p50_latency_ms: number | null;
}
export interface Combo5Record {
  model: string;
  tier: string;
  type: string;
  correct: boolean;
  rr: number;
  latency: number | null;
}
export interface Combo5 {
  meta: {
    n_instances: number;
    models: string[];
    random_floor: number;     // 0.2 = chance for pool-of-5
    tiers: Combo5Tier[];
  };
  aggregate: Record<string, Partial<Record<Combo5TierOrOverall, Combo5Cell>>>;
  records: Combo5Record[];
}

export type Family = 'oss' | 'cohere';
export interface ModelMeta { params: string; license: string; deploy: string; fam: Family; ctx: number }

// Static, honest per-model metadata.
export const MODEL_META: Record<string, ModelMeta> = {
  'ettin-1b':       { params: '1B',   license: 'Apache-2.0', deploy: 'GPU g5.2xl', ctx: 7999,   fam: 'oss' },
  'qwen3-0.6b':     { params: '0.6B', license: 'Apache-2.0', deploy: 'GPU g5.2xl', ctx: 131072, fam: 'oss' },
  'qwen3-4b':       { params: '4B',   license: 'Apache-2.0', deploy: 'GPU g5.2xl', ctx: 131072, fam: 'oss' },
  'nemotron-1b-v2': { params: '1B',   license: 'NVIDIA OM',  deploy: 'GPU g5.2xl', ctx: 4096,   fam: 'oss' },
  'cohere-3.5':     { params: 'n/d',  license: 'Commercial', deploy: 'Bedrock',    ctx: 4096,   fam: 'cohere' },
  'cohere-v4-pro':  { params: 'n/d',  license: 'Commercial', deploy: 'SM g5.xl',   ctx: 4096,   fam: 'cohere' },
  'cohere-v4-fast': { params: 'n/d',  license: 'Commercial', deploy: 'SM g5.xl',   ctx: 4096,   fam: 'cohere' },
};

// Stable per-model hue (used across every panel).
export const MODEL_COLOR: Record<string, string> = {
  'ettin-1b': '#58a6ff', 'qwen3-0.6b': '#3fb950', 'qwen3-4b': '#2d8a3e',
  'nemotron-1b-v2': '#d29922', 'cohere-3.5': '#bc8cff',
  'cohere-v4-pro': '#f78166', 'cohere-v4-fast': '#79c0ff',
};
export const colorFor = (m: string) => MODEL_COLOR[m] ?? '#9aa4b2';
