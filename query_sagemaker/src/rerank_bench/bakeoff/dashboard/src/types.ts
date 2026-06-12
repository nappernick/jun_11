// Mirrors bakeoff/contract.py ResultsFile shape

export interface AbstainPoint {
  t: number;
  abstain_recall: number;
  false_answer_rate: number;
  false_abstain_rate: number;
}

export interface AbstainOp {
  operating_t: number;
  recall: number;
  false_answer_rate: number;
  false_abstain_rate: number;
}

export interface SliceMetrics {
  ndcg10: number;
  ndcg10_ci: [number, number];
  recall10: number;
  mrr10: number;
  p50: number;
  p95: number;
  p99: number;
  throughput_qps: number;
  cost_per_1k: number;
  sig_vs_baseline: number;
  abstain: AbstainOp;
  abstain_curve: AbstainPoint[];
}

export interface Row {
  id: string;
  slice: Record<string, string>;
  rels: number[];
  top_norm: number;
  latency: number;
}

export interface Cell {
  model_id: string;
  N: number;
  by_slice: Record<string, SliceMetrics>;
  rows: Row[];
}

export interface ModelMeta {
  id: string;
  display_name: string;
  params: string;
  max_seq_len: number;
  deploy_path: string;
  license: string;
  instruction_following: boolean;
  calibrated_scores: boolean;
}

export interface Gates {
  accuracy_bar: number;
  latency_budget_ms: number;
  false_answer_ceiling: number;
}

export interface ResultsFile {
  run_id: string;
  gates: Gates;
  baseline_model_id: string;
  models: ModelMeta[];
  cells: Cell[];
}
