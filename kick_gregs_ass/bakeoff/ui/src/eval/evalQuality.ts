/**
 * Configurable composite quality for the ragas eval dashboard (design C2).
 *
 * This BUILDS ON `exec/quality.ts`: it reuses that module's load-bearing
 * discipline — a transparent weighted sum over whatever component metrics are
 * present, explicit missing-component handling, and "insufficient" (null) rather
 * than a confident bare number — but keys on the ragas/retrieval metric names and
 * adds weight-set identity, weight normalization, range-clamping, and an "others"
 * catch-all, as required by the Correctness Properties.
 *
 * Correctness outranks latency here. Every exported function is PURE and
 * DETERMINISTIC, and `compositeQuality` NEVER mutates the recorded metric values
 * it reads (P8) — it only re-derives a score from them.
 */
import type { EvalInstance } from "../api/types";

/** A named, persisted weight set (Req 3.2, 3.6). Keys are metric names, values weights. */
export interface CompositeWeightSet {
  readonly id: string; // recorded alongside each score (Req 3.6)
  readonly weights: Readonly<Record<string, number>>;
}

/** The documented default (mockup), summing to 1.0 (Req 3.7). Reproduced verbatim. */
export const DEFAULT_WEIGHT_SET: CompositeWeightSet = {
  // The dashboard's quality is the REAL Opus-judge triad (the live eval's only quality
  // signal): faithfulness / correctness / completeness, equally weighted. These are the
  // keys the real-eval producer emits (ragas map, judge_* prefix). An instance carrying
  // them renders a meaningful quality on every 3D/2D view.
  id: "judge-triad-v1",
  weights: {
    judge_faithfulness: 0.34,
    judge_correctness: 0.33,
    judge_completeness: 0.33,
  },
};

const QUALITY_METRIC_ALIASES: Readonly<Record<string, readonly string[]>> = {
  faithfulness: ["judge_faithfulness"],
  correctness: ["judge_correctness"],
  completeness: ["judge_completeness"],
  judge_faithfulness: ["faithfulness"],
  judge_correctness: ["correctness"],
  judge_completeness: ["completeness"],
};

/** Outcome of composing one instance — score plus exactly what produced it (Req 3.5, 3.6). */
export interface CompositeResult {
  readonly score: number | null; // 0..1, null === no usable component
  readonly weightSetId: string; // which weights produced it (Req 3.6)
  readonly missingComponents: readonly string[]; // components weighted but unavailable (Req 3.5)
  readonly usedComponents: readonly string[];
}

/** Clamp any metric to its declared range. ragas/retrieval metrics are 0..1 (P3). */
export function clampUnit(x: number): number {
  if (!Number.isFinite(x)) return 0;
  return x < 0 ? 0 : x > 1 ? 1 : x;
}

/**
 * Normalize weights to sum to 1.0 over the components actually present, or reject.
 *
 * Determinism + the sum-to-1 invariant are Property 1. Only positive weights for
 * present components are kept; a weight set whose positive weights over the present
 * components sum to <= 0 is rejected (returns null), never silently treated as
 * uniform.
 */
export function normalizeWeights(
  weights: Readonly<Record<string, number>>,
  presentComponents: readonly string[],
): Readonly<Record<string, number>> | null {
  let sum = 0;
  const kept: Record<string, number> = {};
  for (const c of presentComponents) {
    const w = weights[c];
    if (w === undefined || w <= 0) continue;
    kept[c] = w;
    sum += w;
  }
  if (sum <= 0) return null;
  for (const c of Object.keys(kept)) kept[c] = kept[c]! / sum; // renormalize to 1.0
  return kept;
}

/**
 * Expand an "others" catch-all weight by splitting it EVENLY and deterministically
 * across the enabled metrics that are not otherwise explicitly named.
 *
 * The split is deterministic regardless of input ordering: the recipient set is
 * de-duplicated and sorted before the even division, so "Others 0.05" has a
 * precise, reproducible meaning (Data Models). If there is no positive "others"
 * weight, or no enabled-but-unlisted metric to receive it, the named weights are
 * returned unchanged (minus the synthetic "others" key).
 */
export function expandOthers(
  weights: Readonly<Record<string, number>>,
  enabled: readonly string[],
): Readonly<Record<string, number>> {
  // Start from the explicitly named weights, dropping the synthetic "others" key.
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(weights)) {
    if (k === "others") continue;
    out[k] = v;
  }
  const othersWeight = weights["others"];
  if (othersWeight === undefined || othersWeight <= 0) return out;

  // Recipients: enabled metrics that are neither explicitly named nor "others".
  const recipients = [
    ...new Set(enabled.filter((m) => m !== "others" && weights[m] === undefined)),
  ].sort(); // deterministic even-split order independent of arrival order
  if (recipients.length === 0) return out;

  const each = othersWeight / recipients.length;
  for (const m of recipients) out[m] = (out[m] ?? 0) + each;
  return out;
}

/**
 * Compose one instance's quality from its recorded metric values + a weight set.
 *
 * Pure and deterministic (Property 1): identical (instance, weightSet, enabled)
 * always yields an identical CompositeResult. NEVER mutates the recorded metric
 * values — it only reads them and recomputes (Req 3.3, 12.7 / P8). Missing
 * weighted components are recorded, and the score is computed from the available
 * weighted components renormalized to sum 1.0 (Req 3.5). Every consumed value is
 * clamped to [0,1] (P3).
 */
export function compositeQuality(
  instance: EvalInstance,
  weightSet: CompositeWeightSet,
  /** Which metric names are eligible; "others" expands to enabled-but-unlisted metrics. */
  enabledComponents: readonly string[],
): CompositeResult {
  const present: string[] = [];
  const missing: string[] = [];
  const hasOthers = weightSet.weights["others"] !== undefined;
  const aliasedNames = (metricName: string): readonly string[] => [
    metricName,
    ...(QUALITY_METRIC_ALIASES[metricName] ?? []),
  ];
  const expandedWeights = expandOthers(weightSet.weights, enabledComponents);
  const effectiveWeights: Record<string, number> = {};
  for (const metricName of enabledComponents) {
    for (const aliasedName of aliasedNames(metricName)) {
      const aliasedWeight = expandedWeights[aliasedName];
      if (aliasedWeight !== undefined) {
        effectiveWeights[metricName] = aliasedWeight;
        break;
      }
    }
  }
  const hasNamedWeight = (metricName: string): boolean => {
    return aliasedNames(metricName).some((aliasedName) => expandedWeights[aliasedName] !== undefined);
  };
  const valueOf = (metricName: string): number | null => {
    for (const aliasedName of aliasedNames(metricName)) {
      const metricValue = instance.ragas[aliasedName] ?? instance.retrieval[aliasedName];
      if (metricValue && !metricValue.unavailable && metricValue.value != null) {
        return clampUnit(metricValue.value);
      }
    }
    return null;
  };
  for (const componentName of enabledComponents) {
    // A component is weighted iff it is named OR an "others" catch-all exists.
    if (!hasNamedWeight(componentName) && !hasOthers) continue;
    (valueOf(componentName) == null ? missing : present).push(componentName);
  }
  const norm = normalizeWeights(effectiveWeights, present);
  if (!norm) {
    return {
      score: null,
      weightSetId: weightSet.id,
      missingComponents: missing,
      usedComponents: [],
    };
  }
  let score = 0;
  for (const componentName of present) {
    const componentWeight = norm[componentName];
    if (componentWeight === undefined) continue; // present but zero/absent effective weight
    score += componentWeight * (valueOf(componentName) as number);
  }
  return {
    score: clampUnit(score),
    weightSetId: weightSet.id,
    missingComponents: missing,
    usedComponents: present.filter((componentName) => norm[componentName] !== undefined),
  };
}
