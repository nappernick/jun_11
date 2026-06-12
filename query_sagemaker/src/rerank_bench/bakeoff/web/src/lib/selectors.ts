import type { SliceMetrics, Gates } from '../types';

/** Parse slice keys like "channel=typed&english=clean" into {dimension: unique_values[]} */
export function sliceDimensions(keys: string[]): Record<string, string[]> {
  const dims: Record<string, Set<string>> = {};
  for (const key of keys) {
    for (const part of key.split('&')) {
      const eq = part.indexOf('=');
      if (eq < 0) continue;
      const dim = part.slice(0, eq);
      const val = part.slice(eq + 1);
      (dims[dim] ??= new Set()).add(val);
    }
  }
  const out: Record<string, string[]> = {};
  for (const [dim, vals] of Object.entries(dims)) {
    out[dim] = [...vals].sort();
  }
  return out;
}

/** Does this model pass all 3 gates for a given slice? */
export function gatePass(metrics: SliceMetrics, gates: Gates): boolean {
  return (
    metrics.ndcg10 >= gates.accuracy_bar &&
    metrics.p99 <= gates.latency_budget_ms &&
    metrics.abstain.false_answer_rate <= gates.false_answer_ceiling
  );
}

/** Return model_id of the highest-ndcg model that passes all gates, or '' if none pass. */
export function recommendedModelId(
  sliceMetrics: { modelId: string; metrics: SliceMetrics }[],
  gates: Gates
): string {
  let best = '';
  let bestNdcg = -1;
  for (const { modelId, metrics } of sliceMetrics) {
    if (gatePass(metrics, gates) && metrics.ndcg10 > bestNdcg) {
      bestNdcg = metrics.ndcg10;
      best = modelId;
    }
  }
  return best;
}

/** Pareto frontier: minimize cost_per_1k, maximize ndcg10. Returns subset of input points on the frontier. */
export function paretoFrontier<T extends { cost_per_1k: number; ndcg10: number }>(
  pts: T[]
): T[] {
  const sorted = [...pts].sort((a, b) => a.cost_per_1k - b.cost_per_1k);
  const frontier: T[] = [];
  let maxNdcg = -Infinity;
  for (const pt of sorted) {
    if (pt.ndcg10 > maxNdcg) {
      frontier.push(pt);
      maxNdcg = pt.ndcg10;
    }
  }
  return frontier;
}

/** Build a slice key from dimension selections: {channel:"typed", english:"clean"} -> "channel=typed&english=clean" */
export function buildSliceKey(selections: Record<string, string>): string {
  return Object.entries(selections)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([k, v]) => `${k}=${v}`)
    .join('&');
}
