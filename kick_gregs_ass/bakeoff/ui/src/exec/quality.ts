/**
 * Client-side composite re-weighting (Req 11.3).
 *
 * The exec can ask "what if tone matters more than completeness?" and watch the
 * frontier re-rank live. The composite is a transparent weighted sum of the
 * component quality metrics; this module recomputes it per model from the
 * per-component aggregates the API already returns, so re-weighting needs no
 * backend round-trip.
 *
 * Judge-rework insulation: the component set is data, not hard-coded UI. We read
 * whatever component metrics the API reports and weight those; a rubric change
 * changes the available components, not this code.
 */
import type { Aggregate, CI } from "../api/types";

/** The quality components an exec can re-weight, with default weights. */
export interface QualityWeights {
  readonly [component: string]: number;
}

/** A model's recomputed composite quality + the CI band carried for display. */
export interface WeightedQuality {
  readonly model: string;
  readonly composite: number;
  readonly low: number;
  readonly high: number;
  readonly insufficient: boolean;
}

/**
 * The "softer-confidence" interaction components (Req 11.1/11.7): these are the
 * squishy judge dimensions rendered with a lighter visual treatment so no one
 * mistakes a subjective rubric score for a hard measurement.
 */
export const SQUISHY_COMPONENTS: readonly string[] = [
  "tone",
  "empathy",
  "clarity",
  "actionability",
];

/** Default composite weights (mirrors the backend's transparent default set).
 *  Keys are the backend's real per-trial metric field names (bakeoff/stats.py).
 *  `grounding` is represented by `grounding_recall` (did the answer use the gold
 *  fragment) — the single most decision-relevant grounding signal. */
export const DEFAULT_WEIGHTS: QualityWeights = {
  grounding_recall: 0.25,
  semantic_similarity: 0.15,
  faithfulness: 0.2,
  correctness: 0.15,
  completeness: 0.1,
  tone: 0.04,
  empathy: 0.04,
  clarity: 0.04,
  actionability: 0.03,
};

/** Components the backend treats as accuracy metrics (P4-guarded: fetched sliced
 *  by answerability and collapsed client-side, never blended at the API). Mirrors
 *  bakeoff/aggregate.py `_ACCURACY_METRIC_NAMES` = `_ACCURACY_FIELDS` ∪
 *  {faithfulness, correctness, completeness}. The interaction dims (tone/empathy/
 *  clarity/actionability) and `composite` are NOT guarded and fetch by model. */
export const ACCURACY_FIELD_COMPONENTS: readonly string[] = [
  "grounding_recall",
  "grounding_precision",
  "semantic_similarity",
  "precision_at_k",
  "recall_at_k",
  "mrr",
  "ndcg_at_k",
  "faithfulness",
  "correctness",
  "completeness",
];

/** Metric presets the quality-axis toggle exposes (Req 11.3). */
export type MetricMode = "composite" | "accuracy" | "interaction";

export const METRIC_MODE_LABELS: Record<MetricMode, string> = {
  composite: "Composite",
  accuracy: "Accuracy only",
  interaction: "Interaction only",
};

const ACCURACY_COMPONENTS: readonly string[] = [
  "grounding_recall",
  "semantic_similarity",
  "faithfulness",
  "correctness",
  "completeness",
];

/** The component weights active for a given metric mode + slider weights. */
export function effectiveWeights(mode: MetricMode, weights: QualityWeights): QualityWeights {
  if (mode === "composite") return weights;
  const keep = mode === "accuracy" ? ACCURACY_COMPONENTS : SQUISHY_COMPONENTS;
  const out: Record<string, number> = {};
  for (const k of keep) {
    const w = weights[k];
    if (w !== undefined) out[k] = w;
  }
  return out;
}

function meanOf(ci: Aggregate["mean_ci"]): number | null {
  return ci == null ? null : ci.point;
}

/**
 * Recompute a per-model weighted composite from per-component aggregates.
 *
 * `componentAggregates` maps component-metric name -> the `by_model` aggregate
 * list for that metric. The weighted mean uses only components present for a
 * model; a model with no usable component CI is marked insufficient (P10: never
 * a confident bare number). The CI band is propagated as the weighted mean of the
 * component CIs' half-widths around the recomputed point — a transparent display
 * band, not a re-derived bootstrap (the defensible CI stays the backend's).
 */
export function recomputeComposite(
  models: readonly string[],
  componentAggregates: Readonly<Record<string, readonly Aggregate[]>>,
  weights: QualityWeights,
): WeightedQuality[] {
  const out: WeightedQuality[] = [];
  for (const model of models) {
    let wSum = 0;
    let pSum = 0;
    let halfSum = 0;
    let any = false;
    for (const [component, w] of Object.entries(weights)) {
      if (w <= 0) continue;
      const aggs = componentAggregates[component];
      if (!aggs) continue;
      const agg = aggs.find((a) => a.group["model"] === model);
      if (!agg) continue;
      const point = meanOf(agg.mean_ci);
      if (point == null || agg.mean_ci == null) continue;
      any = true;
      wSum += w;
      pSum += w * point;
      halfSum += w * ((agg.mean_ci.high - agg.mean_ci.low) / 2);
    }
    if (!any || wSum <= 0) {
      out.push({ model, composite: 0, low: 0, high: 0, insufficient: true });
      continue;
    }
    const point = pSum / wSum;
    const half = halfSum / wSum;
    out.push({
      model,
      composite: point,
      low: Math.max(0, point - half),
      high: Math.min(1, point + half),
      insufficient: false,
    });
  }
  return out;
}

/** Two CIs "overlap" (models not yet distinguished on quality — Req 11.1). */
export function ciOverlap(
  a: { low: number; high: number },
  b: { low: number; high: number },
): boolean {
  return a.low <= b.high && b.low <= a.high;
}

/**
 * Collapse a `["model", "answerability"]` aggregate list into per-model
 * equal-weight aggregates, so a P4-guarded accuracy component (which the API
 * refuses to blend across answerability) can still participate in client-side
 * re-weighting without ever blending at the API layer. Each answerability slice
 * contributes equally to the per-model mean; the display CI band is the
 * equal-weight mean of the slice CIs' half-widths. Slices marked
 * insufficient_data (mean_ci null) are skipped; a model with no usable slice
 * collapses to an insufficient aggregate (mean_ci null) — never a bare number.
 */
export function collapseAcrossAnswerability(
  aggregates: readonly Aggregate[],
): Aggregate[] {
  const byModel = new Map<string, Aggregate[]>();
  for (const agg of aggregates) {
    const model = agg.group["model"] ?? "?";
    const list = byModel.get(model) ?? [];
    list.push(agg);
    byModel.set(model, list);
  }
  const out: Aggregate[] = [];
  for (const [model, slices] of byModel) {
    const usable = slices.filter((s) => s.mean_ci != null);
    const nItems = slices.reduce((n, s) => n + s.n_items, 0);
    const nTrials = slices.reduce((n, s) => n + s.n_trials, 0);
    if (usable.length === 0) {
      out.push({
        group: { model },
        metric: slices[0]?.metric ?? "",
        n_items: nItems,
        n_trials: nTrials,
        mean_ci: null,
        variance_decomp: {},
        latency_quantiles: null,
        insufficient_data: true,
      });
      continue;
    }
    let point = 0;
    let half = 0;
    for (const s of usable) {
      const ci = s.mean_ci as CI;
      point += ci.point;
      half += (ci.high - ci.low) / 2;
    }
    point /= usable.length;
    half /= usable.length;
    out.push({
      group: { model },
      metric: usable[0]?.metric ?? "",
      n_items: nItems,
      n_trials: nTrials,
      mean_ci: { point, low: Math.max(0, point - half), high: Math.min(1, point + half), method: "normal_approx" },
      variance_decomp: {},
      latency_quantiles: null,
      insufficient_data: false,
    });
  }
  return out;
}
