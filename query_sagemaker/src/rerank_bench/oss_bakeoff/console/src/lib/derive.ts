// Shared derivations so panels never disagree on the same number.
//
// LATENCY SPLIT (load-bearing):
//   - OSS models  -> latency_gpu.json[model]["10"]  (warm GPU, pool size 10 = headline 204/206/399/525 ms)
//   - Cohere      -> median / p99 of scored per-query latency_ms (API; no GPU pool exists for cohere),
//                    dropping the first query of each model (cold-start RTT ~5x the rest).
// Never use scored per-query latency for OSS: that is the cold laptop-RTT number, not the warm GPU one.

import type { Scored, Latency, Metrics, Combo5, Combo5Type } from '../types';
import { MODEL_META } from '../types';

const GPU_POOL = '10'; // pool size used for the warm OSS p50/p99 headline.

function isOss(model: string): boolean {
  return MODEL_META[model]?.fam === 'oss';
}

function percentile(sortedAscending: number[], fraction: number): number | null {
  if (sortedAscending.length === 0) {
    return null;
  }
  const position = (sortedAscending.length - 1) * fraction;
  const lowerIndex = Math.floor(position);
  const upperIndex = Math.ceil(position);
  if (lowerIndex === upperIndex) {
    return sortedAscending[lowerIndex];
  }
  const weight = position - lowerIndex;
  return sortedAscending[lowerIndex] * (1 - weight) + sortedAscending[upperIndex] * weight;
}

// Cohere per-query latencies with the cold-start first query dropped, sorted ascending.
function cohereWarmLatencies(scored: Scored, model: string): number[] {
  const modelScored = scored.models[model];
  if (!modelScored) {
    return [];
  }
  const all = Object.values(modelScored.queries)
    .map((query) => query.latency_ms)
    .filter((value): value is number => value !== null);
  // Drop the single largest cold-start sample (first request pays connection + warmup).
  const sortedDescending = [...all].sort((left, right) => right - left);
  const withoutColdStart = sortedDescending.slice(1);
  return withoutColdStart.sort((left, right) => left - right);
}

export interface LatencyStat {
  p50: number | null;
  p99: number | null;
  source: 'gpu-pool10' | 'api-scored';
}

export function latencyFor(model: string, scored: Scored, latency: Latency): LatencyStat {
  if (isOss(model)) {
    const cell = latency[model]?.[GPU_POOL];
    return { p50: cell?.p50 ?? null, p99: cell?.p99 ?? null, source: 'gpu-pool10' };
  }
  const warm = cohereWarmLatencies(scored, model);
  return {
    p50: percentile(warm, 0.5),
    p99: percentile(warm, 0.99),
    source: 'api-scored',
  };
}

export function p50For(model: string, scored: Scored, latency: Latency): number | null {
  return latencyFor(model, scored, latency).p50;
}

// Single-stream throughput estimate. There is no measured concurrent-QPS artifact,
// so this is derived from p50 and must be labelled single-stream wherever shown.
export function singleStreamQps(p50Ms: number | null): number | null {
  if (p50Ms === null || p50Ms <= 0) {
    return null;
  }
  return 1000 / p50Ms;
}

// Agreement of a model's rankings with cohere-3.5, from pairwise disagreement rate.
// Key is always "cohere-3.5__<model>" because cohere-3.5 sorts first alphabetically.
export function agreementVsCohere35(model: string, metrics: Metrics): number {
  if (model === 'cohere-3.5') {
    return 1.0;
  }
  const rate = metrics.pair_disagree_rate[`cohere-3.5__${model}`];
  if (rate === undefined) {
    return Number.NaN;
  }
  return 1 - rate;
}

// Raw top-minus-2nd separation per query (score margin between rank 1 and rank 2).
// Distributions uses RAW (not norm); scales differ across logit/margin/unit families.
export function rawTopMinus2nd(scored: Scored, model: string): number[] {
  const modelScored = scored.models[model];
  if (!modelScored) {
    return [];
  }
  const out: number[] = [];
  for (const query of Object.values(modelScored.queries)) {
    if (query.ranking.length < 2) {
      continue;
    }
    const first = query.raw[query.ranking[0]];
    const second = query.raw[query.ranking[1]];
    if (first === undefined || second === undefined) {
      continue;
    }
    out.push(first - second);
  }
  return out;
}

// Per-query latency series used by the latency distribution boxplot (log-y, no clipping).
export function perQueryLatencies(scored: Scored, model: string): number[] {
  const modelScored = scored.models[model];
  if (!modelScored) {
    return [];
  }
  return Object.values(modelScored.queries)
    .map((query) => query.latency_ms)
    .filter((value): value is number => value !== null && value > 0);
}

// Top document title for (model, query): scored gives the chosen node_id, pools gives the title.
export function topDocTitle(
  scored: Scored,
  pools: { pools: Record<string, { node_id: string; title: string }[]> },
  model: string,
  query: string,
): string | null {
  const topId = scored.models[model]?.queries[query]?.top_id;
  if (!topId) {
    return null;
  }
  const pool = pools.pools[query];
  if (!pool) {
    return null;
  }
  return pool.find((item) => item.node_id === topId)?.title ?? null;
}

// Models ranked by Opus judge win-rate, highest first.
export function modelsByJudge(modelScore: Record<string, number>): string[] {
  return Object.keys(modelScore).sort((left, right) => modelScore[right] - modelScore[left]);
}

// ── Combo-5 ground-truth accuracy derivations ──────────────────────────────
// Discipline: difficulty curve / frontier / forest read combo5.aggregate; the
// by-query-type heatmap is computed from combo5.records (never mix the two).

// The four query types, in a fixed display order (specific → abstract).
export const COMBO5_TYPES: { key: Combo5Type; label: string }[] = [
  { key: 'single_hop_specific', label: 'single·specific' },
  { key: 'single_hop_abstract', label: 'single·abstract' },
  { key: 'multi_hop_specific', label: 'multi·specific' },
  { key: 'multi_hop_abstract', label: 'multi·abstract' },
];

// Models present in combo5.aggregate, ranked by hard-tier accuracy (best first).
// The recommended OSS pick = highest hard-tier accuracy among OSS models.
export function modelsByHardAcc(combo5: Combo5): string[] {
  return [...combo5.meta.models].sort((left, right) => {
    const leftAcc = combo5.aggregate[left]?.hard?.acc ?? -Infinity;
    const rightAcc = combo5.aggregate[right]?.hard?.acc ?? -Infinity;
    return rightAcc - leftAcc;
  });
}

// Recommended OSS = OSS model with the highest hard-tier accuracy.
export function recommendedOssByHardAcc(combo5: Combo5): string | null {
  return modelsByHardAcc(combo5).find((model) => MODEL_META[model]?.fam === 'oss') ?? null;
}

// Mean ground-truth accuracy for a (model, type) cell, computed from records.
// Returns null when no record matches that pairing.
export function accuracyByType(
  combo5: Combo5,
  model: string,
  type: Combo5Type,
): number | null {
  let matched = 0;
  let correct = 0;
  for (const record of combo5.records) {
    if (record.model === model && record.type === type) {
      matched += 1;
      if (record.correct) {
        correct += 1;
      }
    }
  }
  if (matched === 0) {
    return null;
  }
  return correct / matched;
}

// Boxplot five-number summary [min, Q1, median, Q3, max] for an echarts boxplot series.
export function boxplotSummary(values: number[]): [number, number, number, number, number] | null {
  if (values.length === 0) {
    return null;
  }
  const sorted = [...values].sort((left, right) => left - right);
  const min = sorted[0];
  const max = sorted[sorted.length - 1];
  const q1 = percentile(sorted, 0.25) as number;
  const median = percentile(sorted, 0.5) as number;
  const q3 = percentile(sorted, 0.75) as number;
  return [min, q1, median, q3, max];
}
