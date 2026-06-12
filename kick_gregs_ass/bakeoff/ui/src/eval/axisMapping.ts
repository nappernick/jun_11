/**
 * Configurable axis → variable binding for the ragas eval dashboard (design C3).
 *
 * The early prose mapped X=session, Y=latency, Z=quality; the authoritative
 * refined mockup maps X=Latency (ms, log), Y=Quality (0..1), Z=Instance Index.
 * This module resolves that discrepancy by making the axis→variable assignment
 * RUNTIME CONFIGURATION and defaulting to the mockup mapping (Req 10.1, 14.1).
 *
 * Correctness outranks latency: the log latency axis is defended against
 * zero/negative/non-finite inputs so a bad value can never produce log(<=0)
 * (Property 7). Every exported function is pure and deterministic.
 */
import type { EvalInstance } from "../api/types";

/** A plottable axis variable. Either a known dimension or a raw metric binding. */
export type AxisVariable =
  | "latency_ms"
  | "quality_score"
  | "instance_index"
  | "corpus_size"
  | "answerability" // categorical from `category` ("single/<a>"): none=0, partial=1, full=2
  | { readonly metric: string }; // bind a raw ragas/retrieval metric to an axis

// Reusable axis bindings for the four meaningful eval dimensions (faithfulness/
// retrieval bind through the {metric} variant; latency/answerability are dimensions).
export const FAITHFULNESS_AXIS: AxisBinding = {
  variable: { metric: "judge_faithfulness" },
  scale: "linear",
  betterDirection: "higher",
};
export const RETRIEVAL_AXIS: AxisBinding = {
  variable: { metric: "recall_at_k" },
  scale: "linear",
  betterDirection: "higher",
};
export const LATENCY_AXIS: AxisBinding = {
  variable: "latency_ms",
  scale: "log",
  betterDirection: "lower",
};
export const QUALITY_AXIS: AxisBinding = {
  variable: "quality_score",
  scale: "linear",
  betterDirection: "higher",
};
export const INSTANCE_INDEX_AXIS: AxisBinding = {
  variable: "instance_index",
  scale: "linear",
  betterDirection: "higher",
};
export const ANSWERABILITY_AXIS: AxisBinding = {
  variable: "answerability",
  scale: "linear",
  betterDirection: "higher",
};

export type AxisScale = "log" | "linear";

export interface AxisBinding {
  readonly variable: AxisVariable;
  readonly scale: AxisScale;
  /** Drives the "how to read" axis labels (Req 14.1). */
  readonly betterDirection: "higher" | "lower";
}

export interface AxisMapping {
  readonly x: AxisBinding;
  readonly y: AxisBinding;
  readonly z: AxisBinding;
}

/**
 * Default = the authoritative mockup mapping:
 *   X = latency_ms     (log,    lower is better)
 *   Y = quality_score  (linear, higher is better)
 *   Z = instance_index (linear, forward/up = later)
 * (Req 10.1, 14.1)
 */
export const DEFAULT_AXIS_MAPPING: AxisMapping = {
  x: LATENCY_AXIS,
  y: QUALITY_AXIS,
  z: INSTANCE_INDEX_AXIS,
};

/**
 * Per-archetype axis combos over the four meaningful dimensions (answerability,
 * faithfulness, retrieval, latency) — chosen so each archetype reads well:
 *   - scatter:    retrieval × faithfulness × latency (3 continuous — a clean cloud)
 *   - bubble:     latency × faithfulness × answerability (bubble size adds retrieval)
 *   - surface:    latency × faithfulness(height) × retrieval (a faithfulness landscape)
 *   - trajectory: latency × faithfulness × answerability (banded by answerability)
 * Switching archetype seeds these; the Control Panel can still override per axis.
 */
export const ARCHETYPE_AXES: Record<string, AxisMapping> = {
  scatter: { x: RETRIEVAL_AXIS, y: FAITHFULNESS_AXIS, z: LATENCY_AXIS },
  bubble: { x: LATENCY_AXIS, y: FAITHFULNESS_AXIS, z: ANSWERABILITY_AXIS },
  surface: { x: LATENCY_AXIS, y: FAITHFULNESS_AXIS, z: RETRIEVAL_AXIS },
  trajectory: { x: LATENCY_AXIS, y: FAITHFULNESS_AXIS, z: ANSWERABILITY_AXIS },
};

/**
 * Positive floor for the log latency axis (Property 7). A latency of 0, a
 * negative value, or a non-finite value is floored to this epsilon so log(value)
 * is always defined; such values are also flagged at ingestion validation.
 */
export const LOG_FLOOR_MS = 1;

/** Defensive log handling (P7): never returns a value < LOG_FLOOR_MS. */
export function logSafe(ms: number): number {
  return Number.isFinite(ms) && ms > LOG_FLOOR_MS ? ms : LOG_FLOOR_MS;
}

/**
 * Project an instance onto an axis variable. Quality is not stored on the
 * instance — it is the recomputed composite — so it is passed in explicitly.
 *
 * Returns null when the bound variable has no plottable value for this instance
 * (e.g. quality could not be composed, or a bound raw metric is unavailable) so
 * the caller can account for it explicitly rather than silently plotting a hole.
 * A log-scaled value is floored through logSafe (belt-and-braces alongside the
 * ECharts 3D log axis guard).
 */
export function axisValue(
  inst: EvalInstance,
  b: AxisBinding,
  quality: number | null,
): number | null {
  let raw: number | null;
  if (b.variable === "latency_ms") {
    raw = inst.latency_ms;
  } else if (b.variable === "quality_score") {
    raw = quality;
  } else if (b.variable === "instance_index") {
    raw = inst.instance_index;
  } else if (b.variable === "corpus_size") {
    raw = inst.corpus_size;
  } else if (b.variable === "answerability") {
    // `category` is "single/<answerability>" — order none < partial < full.
    const a = (inst.category ?? "").split("/").pop();
    raw = a === "none" ? 0 : a === "partial" ? 1 : a === "full" ? 2 : null;
  } else {
    const mv = inst.ragas[b.variable.metric] ?? inst.retrieval[b.variable.metric];
    raw = mv && !mv.unavailable ? mv.value : null;
  }
  if (raw == null || !Number.isFinite(raw)) return null;
  return b.scale === "log" ? logSafe(raw) : raw;
}
