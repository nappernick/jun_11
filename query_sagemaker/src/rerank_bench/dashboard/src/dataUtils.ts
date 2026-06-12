import type { BenchRow, Dataset, RunFile } from "./types";

/** Label applied to rows from older runs that predate multi-model support. */
export const DEFAULT_MODEL = "cohere-3.5";

/** Ensure every row carries a model label (older files omit it). */
function withModel(rows: BenchRow[]): BenchRow[] {
  return rows.map((r) => (r.model ? r : { ...r, model: DEFAULT_MODEL }));
}

/** Parse raw JSON — handles both the new envelope format and the old flat array */
export function parseFile(filename: string, raw: unknown): Dataset {
  // New format: { meta, rows }
  if (raw && typeof raw === "object" && !Array.isArray(raw) && "rows" in raw) {
    const rf = raw as RunFile;
    const rows = withModel(rf.rows);
    const meta = rf.meta
      ? { ...rf.meta, models: rf.meta.models ?? [...new Set(rows.map((r) => r.model))] }
      : rf.meta;
    return { filename, meta, rows };
  }
  // Old format: flat array
  if (Array.isArray(raw)) {
    const rows = withModel(raw as BenchRow[]);
    const isCombo = rows.some((r) => Array.isArray(r.ranking));
    return {
      filename,
      meta: {
        run_mode: isCombo ? "combo" : "pool_sweep",
        queries: [...new Set(rows.map((r) => r.query))],
        pools: isCombo ? null : [...new Set(rows.map((r) => r.pool))].sort((a, b) => a - b),
        combo_k: isCombo ? (rows[0]?.pool ?? null) : null,
        combo_base: null,
        combo_cap: null,
        max_doc_chars: 32000,
        dropped_chunks: [],
        models: [...new Set(rows.map((r) => r.model))],
        models_errored: {},
        record_count: rows.length,
        generated_at: "",
      },
      rows,
    };
  }
  throw new Error("Unrecognized file format");
}

/** Distinct model labels present in a dataset, in a stable order (3.5 first). */
export function modelsInDataset(ds: Dataset): string[] {
  const set = [...new Set(ds.rows.map((r) => r.model || DEFAULT_MODEL))];
  return set.sort((a, b) => (a === DEFAULT_MODEL ? -1 : b === DEFAULT_MODEL ? 1 : a.localeCompare(b)));
}

export function p50(vals: number[]): number {
  if (!vals.length) return 0;
  const s = [...vals].sort((a, b) => a - b);
  return s[Math.floor(s.length / 2)];
}

export function percentile(vals: number[], p: number): number {
  if (!vals.length) return 0;
  const s = [...vals].sort((a, b) => a - b);
  const idx = Math.ceil((p / 100) * s.length) - 1;
  return s[Math.max(0, idx)];
}

export function mean(vals: number[]): number {
  if (!vals.length) return 0;
  return vals.reduce((a, b) => a + b, 0) / vals.length;
}

export function isComboDataset(ds: Dataset): boolean {
  return ds.rows.some((r) => Array.isArray(r.ranking));
}

/**
 * Latency outlier threshold: median + 3 * (p90 - p50).
 * Combos that exceed this are flagged as potential context-window stress.
 */
export function latencyOutlierThreshold(rows: BenchRow[]): number {
  const lats = rows.map((r) => r.latency_ms);
  const m = p50(lats);
  const spread = percentile(lats, 90) - m;
  return m + 3 * Math.max(spread, 20); // floor at +60ms over median
}

/**
 * Score compression: if the spread between rank-1 and rank-k score is < 0.05,
 * the model may be confused / context is noisy.
 */
export function isScoreCompressed(scores: number[]): boolean {
  if (scores.length < 2) return false;
  return Math.max(...scores) - Math.min(...scores) < 0.05;
}
