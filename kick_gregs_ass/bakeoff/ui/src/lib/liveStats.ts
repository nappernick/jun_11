/**
 * Live, judge-agnostic statistics derived from the streamed trial_completed
 * buffer for the Bake-Off race view.
 *
 * Everything here is computed client-side from the in-memory ring buffer
 * (useTrialBuffer) as events arrive, independent of the snapshot poll. The
 * snapshot remains authoritative for planned/done/in_flight/errored counts; this
 * module supplies the live latency and quality approximations the snapshot does
 * not carry (end-to-end p50/p90 and mean composite), plus the thinking-pair
 * grouping the moving frontier draws as a delta vector.
 *
 * Latency note: the SSE payload carries `end_to_end_ms` (time to final token).
 * A separate TTFT (time-to-first-token) field is NOT in the required
 * TrialCompleted shape; we read it defensively as an optional `ttft_ms` so a
 * future backend addition lights up the TTFT lane automatically without a type
 * change here. See readTtftMs.
 */
import type { TrialCompleted } from "../api/types";

/** Suffixes that distinguish how a base model is invoked (the bake-off v2 axis). */
const CONVERSE_SUFFIX = "-converse";
const INLINE_SUFFIX = "-inline";
/** Legacy thinking suffixes (kept so older logs still group sensibly). */
const THINKING_ON_SUFFIX = "-thinking-on";
const THINKING_OFF_SUFFIX = "-thinking-off";

/**
 * Linear-interpolated quantile of an already-sorted ascending array. Matches the
 * convention used by the existing LatencyChart so live p50 reads identically.
 */
export function quantile(sorted: readonly number[], q: number): number {
  if (sorted.length === 0) return 0;
  const pos = (sorted.length - 1) * q;
  const base = Math.floor(pos);
  const rest = pos - base;
  const lo = sorted[base] ?? 0;
  const hi = sorted[base + 1] ?? lo;
  return lo + rest * (hi - lo);
}

/**
 * Defensive optional read of a time-to-first-token field. The required
 * TrialCompleted shape does not include it; if the backend later adds `ttft_ms`
 * to the SSE payload it will flow through untouched (useEventStream validates
 * only the required fields) and this returns it. Returns null when absent or
 * non-finite, so the TTFT lane stays dark until the data exists.
 */
export function readTtftMs(ev: TrialCompleted): number | null {
  const raw = (ev as { ttft_ms?: unknown }).ttft_ms;
  return typeof raw === "number" && Number.isFinite(raw) ? raw : null;
}

/**
 * The base-model stem for method-pair grouping: strip a trailing
 * `-converse` / `-inline` suffix (the bake-off v2 invocation axis), and also any
 * legacy `-thinking-on` / `-thinking-off` suffix. So
 * `claude-sonnet-4.6-thinking-off-converse` and
 * `claude-sonnet-4.6-thinking-off-inline` share the stem
 * `claude-sonnet-4.6-thinking-off`, letting the frontier draw a converse→inline
 * delta vector for the same model+config. Models without a recognized suffix
 * return unchanged and pair with nothing.
 */
export function thinkingStem(model: string): string {
  let s = model;
  if (s.endsWith(CONVERSE_SUFFIX)) s = s.slice(0, -CONVERSE_SUFFIX.length);
  else if (s.endsWith(INLINE_SUFFIX)) s = s.slice(0, -INLINE_SUFFIX.length);
  if (s.endsWith(THINKING_ON_SUFFIX)) s = s.slice(0, -THINKING_ON_SUFFIX.length);
  else if (s.endsWith(THINKING_OFF_SUFFIX)) s = s.slice(0, -THINKING_OFF_SUFFIX.length);
  return s;
}

/** Which invocation method a model uses, if encoded in its name. */
export type ThinkingVariant = "on" | "off" | "none";

export function thinkingVariant(model: string): ThinkingVariant {
  // Reuse the on/off vocabulary for the now-primary converse/inline axis:
  //   "off" = converse (the baseline), "on" = inline (the new method under test).
  if (model.endsWith(CONVERSE_SUFFIX)) return "off";
  if (model.endsWith(INLINE_SUFFIX)) return "on";
  return "none";
}

/** Live, buffer-derived stats for a single model. */
export interface ModelLiveStats {
  readonly model: string;
  /** Non-errored samples that contributed to latency/composite. */
  readonly n: number;
  /** Errored events seen in the buffer for this model. */
  readonly errored: number;
  /** End-to-end (time-to-final-token) latency quantiles, ms. null until n>0. */
  readonly endToEndP50: number | null;
  readonly endToEndP90: number | null;
  /** Mean live composite over non-errored samples. null until n>0. */
  readonly meanComposite: number | null;
  /** TTFT p50 if the optional field is present on any sample; else null. */
  readonly ttftP50: number | null;
}

/**
 * Fold the trial buffer into per-model live stats. Only non-errored events feed
 * latency and composite (an errored trial has no meaningful end_to_end/composite);
 * errors are counted separately so the fleet can surface them. Models present in
 * `seedModels` but with no samples yet still get a zeroed entry, so the fleet
 * shows every candidate from the snapshot immediately, not only those that have
 * already produced a trial.
 */
export function computeModelLiveStats(
  events: readonly TrialCompleted[],
  seedModels: readonly string[] = [],
): Map<string, ModelLiveStats> {
  const latency = new Map<string, number[]>();
  const ttft = new Map<string, number[]>();
  const composite = new Map<string, number[]>();
  const errored = new Map<string, number>();
  const seen = new Set<string>(seedModels);

  for (const e of events) {
    seen.add(e.model);
    if (e.error) {
      errored.set(e.model, (errored.get(e.model) ?? 0) + 1);
      continue;
    }
    if (Number.isFinite(e.end_to_end_ms)) {
      const arr = latency.get(e.model) ?? [];
      arr.push(e.end_to_end_ms);
      latency.set(e.model, arr);
    }
    if (Number.isFinite(e.composite)) {
      const arr = composite.get(e.model) ?? [];
      arr.push(e.composite);
      composite.set(e.model, arr);
    }
    const t = readTtftMs(e);
    if (t != null) {
      const arr = ttft.get(e.model) ?? [];
      arr.push(t);
      ttft.set(e.model, arr);
    }
  }

  const out = new Map<string, ModelLiveStats>();
  for (const model of seen) {
    const lat = (latency.get(model) ?? []).slice().sort((a, b) => a - b);
    const comp = composite.get(model) ?? [];
    const tt = (ttft.get(model) ?? []).slice().sort((a, b) => a - b);
    const meanComposite =
      comp.length > 0 ? comp.reduce((s, v) => s + v, 0) / comp.length : null;
    out.set(model, {
      model,
      n: lat.length,
      errored: errored.get(model) ?? 0,
      endToEndP50: lat.length > 0 ? quantile(lat, 0.5) : null,
      endToEndP90: lat.length > 0 ? quantile(lat, 0.9) : null,
      meanComposite,
      ttftP50: tt.length > 0 ? quantile(tt, 0.5) : null,
    });
  }
  return out;
}

/** A thinking-on/off pair (or a lone model) for the moving-frontier delta. */
export interface ThinkingGroup {
  readonly stem: string;
  readonly on: ModelLiveStats | null;
  readonly off: ModelLiveStats | null;
  /** Models with neither suffix render as lone points. */
  readonly lone: ModelLiveStats | null;
}

/**
 * Group per-model live stats by their base-model stem so the frontier can draw a
 * delta vector between the thinking-on and thinking-off variants of the same base
 * model. A model with no thinking suffix becomes a lone point. Groups are sorted
 * stably by stem so the chart does not reshuffle as the buffer grows.
 */
export function groupByThinkingStem(
  stats: ReadonlyMap<string, ModelLiveStats>,
): ThinkingGroup[] {
  const byStem = new Map<string, { on: ModelLiveStats | null; off: ModelLiveStats | null; lone: ModelLiveStats | null }>();
  for (const s of stats.values()) {
    const stem = thinkingStem(s.model);
    const entry = byStem.get(stem) ?? { on: null, off: null, lone: null };
    const variant = thinkingVariant(s.model);
    if (variant === "on") entry.on = s;
    else if (variant === "off") entry.off = s;
    else entry.lone = s;
    byStem.set(stem, entry);
  }
  return [...byStem.entries()]
    .map(([stem, e]) => ({ stem, on: e.on, off: e.off, lone: e.lone }))
    .sort((a, b) => a.stem.localeCompare(b.stem));
}
