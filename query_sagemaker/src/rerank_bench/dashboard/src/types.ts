export interface BenchRow {
  query: string;
  pool: number;
  /** Reranker label, e.g. "cohere-3.5" or "cohere-4-pro". Older single-model
   *  files omit this; dataUtils defaults it to DEFAULT_MODEL on load. */
  model: string;
  latency_ms: number;
  top_score: number;
  top_id: string;
  scores: number[];
  // combo-mode only
  combo?: string[];
  ranking?: string[];
}

export interface DroppedChunk {
  query: string;
  source_id: string;
  char_count: number;
  limit: number;
  reason: string;
}

export interface RunMeta {
  run_mode: "combo" | "pool_sweep";
  queries: string[];
  pools: number[] | null;
  combo_k: number | null;
  combo_base: number | null;
  combo_cap: number | null;
  max_doc_chars: number;
  dropped_chunks: DroppedChunk[];
  /** Models present in this run (e.g. ["cohere-3.5","cohere-4-pro"]). */
  models?: string[];
  /** Models requested but skipped because their backend was unreachable. */
  models_errored?: Record<string, string>;
  record_count: number;
  generated_at: string;
}

/** New envelope format from the updated bench script */
export interface RunFile {
  meta: RunMeta;
  rows: BenchRow[];
}

/** Loaded dataset — either from the envelope or from the old flat array */
export interface Dataset {
  filename: string;
  meta: RunMeta | null;
  rows: BenchRow[];
}
