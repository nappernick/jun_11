/**
 * Pure view-derivation selectors for the ragas eval dashboard (design C5/C11).
 *
 * This module is the single seam between the raw EvalInstance records (seeded /
 * backfilled / streamed by `useEvalStream`) and the chart-option builders. It is
 * deliberately PURE and DETERMINISTIC: the same (instances, selection) always
 * yields the same ChartView, and the result is independent of the order the
 * records arrive in (the input is deduped by instance_id and sorted before any
 * derivation). That order-independence is what makes the durable-backfill
 * reconstruction equal the stream-built view (Property 6).
 *
 * Correctness outranks latency. The headline discipline here is the Property 4
 * "derivation half": NO in-view record is silently dropped. Every record that
 * passes through `deriveChartView` is accounted for in exactly one bucket —
 * plotted, filtered out by the selection, or non-plottable (missing a value for a
 * bound axis) — and `accounting.plotted` is exactly the set of instance_ids in
 * `instances`. A point never exists without a backing record, and a record is
 * never dropped without being counted.
 */
import type {
  EvalInstance,
  EvalInstanceAppended,
  EvalStatus,
} from "../api/types";
import {
  axisValue,
  DEFAULT_AXIS_MAPPING,
  type AxisMapping,
} from "./axisMapping";
import {
  compositeQuality,
  DEFAULT_WEIGHT_SET,
  type CompositeWeightSet,
} from "./evalQuality";
import { buildAgentColorMap } from "./agentColor";

/** The Control_Panel selection that parameterizes a view (design C11). */
export interface EvalSelection {
  /** Agents to include (>= 3 supported). Empty === no agent filter (all agents). */
  readonly agentIds: readonly string[];
  /** Sessions / time range. "all" === no session filter. */
  readonly sessionIds: readonly string[] | "all";
  /** Which metric names contribute to the composite ("others" expands downstream). */
  readonly enabledMetrics: readonly string[];
  /** The active weight set (recompute is from unchanged recorded values — P8). */
  readonly weightSet: CompositeWeightSet;
  /** Prompt filter; null === no filter (Req 12.4). */
  readonly promptFilter: string | null;
  /** Category filter; null === no filter (Req 12.4). */
  readonly categoryFilter: string | null;
  /** Trailing moving-average window over per-instance quality; <= 1 === no smoothing. */
  readonly smoothingWindow: number;
  /** Configurable axis mapping; defaults to the mockup mapping. */
  readonly axes: AxisMapping;
}

/**
 * Explicit accounting for every input instance considered by `deriveChartView`
 * (Property 4, derivation half). The three id buckets are pairwise disjoint and
 * their union is exactly the deduped set of input instance_ids.
 */
export interface ChartViewAccounting {
  /** Total distinct input instances considered (after dedupe by instance_id). */
  readonly total: number;
  /** instance_ids that are plotted — exactly the ids in `ChartView.instances`. */
  readonly plotted: readonly string[];
  /** instance_ids excluded by the selection's filters. */
  readonly filteredOut: readonly string[];
  /** instance_ids that passed the filters but lack a value for a bound axis. */
  readonly nonPlottable: readonly string[];
}

/** The single derived view model every 2D/3D builder consumes (design C5). */
export interface ChartView {
  /** The plottable, filtered, deterministically ordered instances. */
  readonly instances: readonly EvalInstance[];
  /** Per-instance composite quality (smoothed if a window is set); for all kept ids. */
  readonly qualityByInstanceId: ReadonlyMap<string, number | null>;
  /** Stable, injective agent→color map (built over the kept agent set). */
  readonly agentColors: ReadonlyMap<string, string>;
  /** The axis mapping the view was derived under. */
  readonly axes: AxisMapping;
  /** Whether to draw the Ideal_Region sweet-spot marker (Req 13.1). */
  readonly idealRegion: boolean;
  /** Explicit Property-4 accounting: every input record is in exactly one bucket. */
  readonly accounting: ChartViewAccounting;
}

/** The ragas components of the documented default, excluding the "others" catch-all. */
export const DEFAULT_ENABLED_METRICS: readonly string[] = Object.keys(
  DEFAULT_WEIGHT_SET.weights,
).filter((k) => k !== "others");

/** A sensible default selection: all agents/sessions, default weights, no smoothing. */
export function defaultSelection(): EvalSelection {
  return {
    agentIds: [],
    sessionIds: "all",
    enabledMetrics: DEFAULT_ENABLED_METRICS,
    weightSet: DEFAULT_WEIGHT_SET,
    promptFilter: null,
    categoryFilter: null,
    smoothingWindow: 1,
    axes: DEFAULT_AXIS_MAPPING,
  };
}

/** Deterministic total order so derivation is independent of input arrival order. */
function compareInstances(a: EvalInstance, b: EvalInstance): number {
  if (a.agent_id !== b.agent_id) return a.agent_id < b.agent_id ? -1 : 1;
  if (a.session_id !== b.session_id) return a.session_id < b.session_id ? -1 : 1;
  if (a.instance_index !== b.instance_index) return a.instance_index - b.instance_index;
  return a.instance_id < b.instance_id ? -1 : a.instance_id > b.instance_id ? 1 : 0;
}

/** Dedupe by instance_id (last-writer-wins) then impose the deterministic order. */
function dedupeSorted(list: readonly EvalInstance[]): EvalInstance[] {
  const m = new Map<string, EvalInstance>();
  for (const i of list) if (i && typeof i.instance_id === "string") m.set(i.instance_id, i);
  return [...m.values()].sort(compareInstances);
}

/**
 * Trailing moving-average smoothing of per-instance quality within each
 * (agent_id, session_id) group, ordered by instance_index. A null quality stays
 * null (an unavailable composite is not smoothed into existence); the average is
 * taken over the non-null values within the trailing window including self.
 * window <= 1 returns the raw quality unchanged.
 */
function applySmoothing(
  ordered: readonly EvalInstance[],
  raw: ReadonlyMap<string, number | null>,
  window: number,
): Map<string, number | null> {
  const out = new Map<string, number | null>(raw);
  if (!Number.isFinite(window) || window <= 1) return out;
  const w = Math.floor(window);

  const groups = new Map<string, EvalInstance[]>();
  for (const inst of ordered) {
    const key = `${inst.agent_id}\u0000${inst.session_id}`;
    (groups.get(key) ?? groups.set(key, []).get(key)!).push(inst);
  }
  for (const group of groups.values()) {
    // `ordered` is already sorted by (agent, session, instance_index), so each
    // group is in instance_index order.
    for (let j = 0; j < group.length; j++) {
      const self = raw.get(group[j]!.instance_id) ?? null;
      if (self == null) {
        out.set(group[j]!.instance_id, null);
        continue;
      }
      let sum = 0;
      let n = 0;
      for (let k = Math.max(0, j - w + 1); k <= j; k++) {
        const v = raw.get(group[k]!.instance_id) ?? null;
        if (v != null) {
          sum += v;
          n += 1;
        }
      }
      out.set(group[j]!.instance_id, n > 0 ? sum / n : null);
    }
  }
  return out;
}

/**
 * Filter → smooth → compose → project the raw records into the single ChartView
 * every builder consumes. Pure and order-independent.
 *
 * Property 4 (derivation half): every distinct input instance ends up in exactly
 * one of `accounting.plotted` / `filteredOut` / `nonPlottable`, and `plotted` is
 * exactly the id set of `instances`. Nothing is silently dropped.
 */
export function deriveChartView(
  instances: readonly EvalInstance[],
  selection: EvalSelection,
): ChartView {
  const all = dedupeSorted(instances);

  const agentFilter = new Set(selection.agentIds);
  const sessionFilter =
    selection.sessionIds === "all" ? null : new Set(selection.sessionIds);

  const kept: EvalInstance[] = [];
  const filteredOut: string[] = [];
  for (const inst of all) {
    let pass = true;
    if (agentFilter.size > 0 && !agentFilter.has(inst.agent_id)) pass = false;
    else if (sessionFilter && !sessionFilter.has(inst.session_id)) pass = false;
    else if (selection.promptFilter != null && inst.prompt_id !== selection.promptFilter)
      pass = false;
    else if (selection.categoryFilter != null && inst.category !== selection.categoryFilter)
      pass = false;
    if (pass) kept.push(inst);
    else filteredOut.push(inst.instance_id);
  }

  // Per-instance composite from the UNCHANGED recorded values (P8), then smooth.
  const rawQuality = new Map<string, number | null>();
  for (const inst of kept) {
    rawQuality.set(
      inst.instance_id,
      compositeQuality(inst, selection.weightSet, selection.enabledMetrics).score,
    );
  }
  const quality = applySmoothing(kept, rawQuality, selection.smoothingWindow);

  // Project onto the bound axes; an instance missing ANY bound axis value is
  // explicitly non-plottable rather than silently dropped.
  const plotted: EvalInstance[] = [];
  const nonPlottable: string[] = [];
  for (const inst of kept) {
    const q = quality.get(inst.instance_id) ?? null;
    const x = axisValue(inst, selection.axes.x, q);
    const y = axisValue(inst, selection.axes.y, q);
    const z = axisValue(inst, selection.axes.z, q);
    if (x == null || y == null || z == null) nonPlottable.push(inst.instance_id);
    else plotted.push(inst);
  }

  const agentColors = buildAgentColorMap(kept.map((i) => i.agent_id));

  return {
    instances: plotted,
    qualityByInstanceId: quality,
    agentColors,
    axes: selection.axes,
    idealRegion: true,
    accounting: {
      total: all.length,
      plotted: plotted.map((i) => i.instance_id),
      filteredOut: filteredOut.sort(),
      nonPlottable: nonPlottable.sort(),
    },
  };
}

/**
 * Build the instance set from the durable status backfill (the reconstruction
 * authority). Deduped by instance_id so it is idempotent with the seed/stream.
 */
export function fromStatus(status: EvalStatus): readonly EvalInstance[] {
  return dedupeSorted(status.instances ?? []);
}

/**
 * Build the instance set from a sequence of live stream deltas. Deduped by
 * instance_id (last-writer-wins) so replays / re-sends collapse — this is the
 * idempotence that makes `deriveChartView(fromStatus(R)) ≡ deriveChartView(fromStream(R))`
 * for the same underlying record set R (Property 6).
 */
export function fromStream(
  deltas: readonly EvalInstanceAppended[],
): readonly EvalInstance[] {
  return dedupeSorted(deltas.map((d) => d.instance));
}

/** Variance threshold above which an agent's quality is flagged inconsistent. */
export const INCONSISTENCY_VARIANCE_THRESHOLD = 0.05;

/** Ordered (by first appearance / instance_index) per-session mean quality for an agent. */
function sessionMeanQualities(view: ChartView, agentId: string): number[] {
  // session -> { firstIndex, sum, n } over plotted instances with a non-null quality.
  const acc = new Map<string, { firstIndex: number; sum: number; n: number }>();
  for (const inst of view.instances) {
    if (inst.agent_id !== agentId) continue;
    const q = view.qualityByInstanceId.get(inst.instance_id) ?? null;
    if (q == null) continue;
    const cur = acc.get(inst.session_id);
    if (cur) {
      cur.sum += q;
      cur.n += 1;
      cur.firstIndex = Math.min(cur.firstIndex, inst.instance_index);
    } else {
      acc.set(inst.session_id, { firstIndex: inst.instance_index, sum: q, n: 1 });
    }
  }
  return [...acc.entries()]
    .sort((a, b) =>
      a[1].firstIndex !== b[1].firstIndex
        ? a[1].firstIndex - b[1].firstIndex
        : a[0] < b[0]
          ? -1
          : 1,
    )
    .map(([, v]) => v.sum / v.n);
}

/**
 * Drift cue (Req 13.3): a downward quality trend across consecutive sessions.
 * Fires iff the agent has >= 2 sessions (with quality) whose per-session mean
 * strictly decreases at every consecutive step (a monotone downward trend).
 */
export function detectDrift(view: ChartView, agentId: string): boolean {
  const means = sessionMeanQualities(view, agentId);
  if (means.length < 2) return false;
  const EPS = 1e-9;
  for (let i = 1; i < means.length; i++) {
    if (!(means[i]! < means[i - 1]! - EPS)) return false;
  }
  return true;
}

/**
 * Inconsistency cue (Req 13.4): high quality variance across an agent's
 * instances. Fires iff the population variance of the agent's (non-null) quality
 * values exceeds INCONSISTENCY_VARIANCE_THRESHOLD.
 */
export function detectInconsistency(view: ChartView, agentId: string): boolean {
  const qs: number[] = [];
  for (const inst of view.instances) {
    if (inst.agent_id !== agentId) continue;
    const q = view.qualityByInstanceId.get(inst.instance_id) ?? null;
    if (q != null) qs.push(q);
  }
  if (qs.length < 2) return false;
  const mean = qs.reduce((a, b) => a + b, 0) / qs.length;
  const variance = qs.reduce((a, b) => a + (b - mean) * (b - mean), 0) / qs.length;
  return variance > INCONSISTENCY_VARIANCE_THRESHOLD;
}
