/**
 * 3D chart-option builders for the ragas eval dashboard (design C5).
 *
 * Each exported function is a PURE function of the already-derived `ChartView`
 * (instances filtered/smoothed by `evalSelectors.deriveChartView`, the per-
 * instance composite, the stable agent→color map, and the axis mapping). It
 * returns a typed `EChartsOption` consumed by the existing `EChart.tsx` wrapper,
 * which registers `echarts-gl` (so `grid3D` + `xAxis3D/yAxis3D/zAxis3D` and the
 * `scatter3D` / `line3D` / `surface` series resolve). `echarts-gl` augments the
 * option model at runtime but not the published `EChartsOption` type, so the
 * returned objects are built plainly and cast — the same documented escape hatch
 * `FrontierChart.tsx` uses for its custom series.
 *
 * Correctness outranks latency. The headline discipline is the Property 4 "3D
 * half": the multiset of `instance_id`s carried in the point-bearing series data
 * (scatter, bubble, trajectory) is exactly the multiset of `instance_id`s of the
 * plottable instances — no phantom points, none dropped. Trajectory paths are
 * emitted in strictly ascending `instance_index` order per agent. The latency
 * axis is rendered on a log scale defended by `logSafe` against zero/negative
 * inputs (Property 7), belt-and-braces with the ECharts 3D log-axis guard.
 */
import type { EChartsOption } from "echarts";
import type { EvalInstance } from "../api/types";
import {
  axisValue,
  LOG_FLOOR_MS,
  type AxisBinding,
  type AxisVariable,
} from "./axisMapping";
import type { ChartView } from "./evalSelectors";

/** Which numeric field drives bubble size (Req 10.5). */
export type BubbleSizeSource = "confidence" | "volume" | "cost";

/** User-facing ECharts GL `grid3D.viewControl` knobs exposed by Eval3D. */
export interface Scene3DControls {
  readonly projection: "perspective" | "orthographic" | "isometric";
  readonly alpha: number;
  readonly beta: number;
  readonly distance: number;
  readonly minDistance: number;
  readonly maxDistance: number;
  readonly orthographicSize: number;
  readonly minOrthographicSize: number;
  readonly maxOrthographicSize: number;
  readonly center: readonly [number, number, number];
  readonly damping: number;
  readonly autoRotate: boolean;
  readonly autoRotateDirection: "cw" | "ccw";
  readonly autoRotateSpeed: number;
  readonly autoRotateAfterStill: number;
  readonly minAlpha: number;
  readonly maxAlpha: number;
  readonly minBeta: number;
  readonly maxBeta: number;
  readonly rotateSensitivity: number;
  readonly zoomSensitivity: number;
  readonly panSensitivity: number;
  readonly rotateMouseButton: "left" | "middle" | "right";
  readonly panMouseButton: "left" | "middle" | "right";
  readonly boxWidth: number;
  readonly boxDepth: number;
  readonly boxHeight: number;
  readonly showAxisPointer: boolean;
  readonly showGrid: boolean;
}

/** Surface interpolation lattice: how finely to bucket (X,Z) and how to interpolate. */
export interface SurfaceGridSpec {
  /** Latency (X) buckets, spaced in the axis' own scale (log when X is log). */
  readonly xBuckets: number;
  /** Instance-index / time (Z) buckets. */
  readonly zBuckets: number;
  /** Interpolation of quality (Y) over the lattice. */
  readonly method: "bilinear" | "nearest";
}

/** A point datum: the [x,y,z] coordinate plus the backing record's id + detail. */
interface Point3D {
  readonly value: readonly [number, number, number];
  readonly instance_id: string;
  readonly agent: string;
  readonly session: string;
  readonly index: number;
  readonly latency_ms: number;
  readonly quality: number | null;
  readonly corpus_size: number;
}

const MIN_BUBBLE = 6;
const MAX_BUBBLE = 36;
const DEFAULT_BUBBLE = 10;

// ---------------------------------------------------------------------------
// Axis naming + scale (encode "higher Y better / lower X better / forward Z
// later" per Req 14.1) and the log latency axis (Req 14.4 / P7).
// ---------------------------------------------------------------------------

const METRIC_LABELS: Record<string, string> = {
  judge_faithfulness: "faithfulness",
  judge_correctness: "correctness",
  judge_completeness: "completeness",
  ndcg_at_k: "retrieval nDCG@k",
  precision_at_k: "retrieval precision@k",
  recall_at_k: "retrieval recall@k",
};

function variableLabel(v: AxisVariable): string {
  if (typeof v === "object") return METRIC_LABELS[v.metric] ?? v.metric;
  switch (v) {
    case "latency_ms":
      return "latency (ms)";
    case "quality_score":
      return "quality (0..1)";
    case "instance_index":
      return "execution order";
    case "corpus_size":
      return "corpus size";
    case "answerability":
      return "answerability";
    default:
      return v;
  }
}

function directionPhrase(b: AxisBinding): string {
  // Ordinal/temporal axes are read as forward, not "good/bad".
  if (b.variable === "instance_index") return "forward is later";
  if (b.variable === "answerability") return "none → full";
  return b.betterDirection === "higher" ? "higher is better" : "lower is better";
}

/** The human-readable axis name shown on the 3D axis (Req 14.1). */
export function axisName(b: AxisBinding): string {
  const base = `${variableLabel(b.variable)} — ${directionPhrase(b)}`;
  return b.scale === "log" ? `${base} (log)` : base;
}

function axisSpec(
  b: AxisBinding,
  range?: { min: number; max: number } | null,
): Record<string, unknown> {
  if (b.scale === "log") {
    // ADAPTIVE: fit the log axis to the actual data range (multiplicative padding),
    // so a tight latency cluster (e.g. 4–12 s) isn't squashed into one corner by a
    // fixed floor. Falls back to the floor only when there's no data yet.
    if (range && range.min > 0 && range.max > 0) {
      return {
        type: "log",
        name: axisName(b),
        min: Math.max(LOG_FLOOR_MS, range.min * 0.85),
        max: range.max * 1.15,
      };
    }
    return { type: "log", name: axisName(b), min: LOG_FLOOR_MS };
  }
  if (b.variable === "quality_score") {
    return { type: "value", name: axisName(b), min: 0, max: 1 };
  }
  // Linear value axes (e.g. instance index) auto-fit to data in ECharts; pad lightly
  // when we have a range so points don't sit flush against the wall.
  if (range && range.min !== range.max) {
    const pad = (range.max - range.min) * 0.05;
    return { type: "value", name: axisName(b), min: range.min - pad, max: range.max + pad };
  }
  return { type: "value", name: axisName(b) };
}

/** Min/max of the plotted values on one bound axis — drives adaptive scaling. */
function axisDataRange(
  view: ChartView,
  b: AxisBinding,
): { min: number; max: number } | null {
  let min = Infinity;
  let max = -Infinity;
  for (const inst of view.instances) {
    const q = view.qualityByInstanceId.get(inst.instance_id) ?? null;
    const v = axisValue(inst, b, q);
    if (v == null || !Number.isFinite(v)) continue;
    if (v < min) min = v;
    if (v > max) max = v;
  }
  return min <= max ? { min, max } : null;
}

// ---------------------------------------------------------------------------
// Projection helpers
// ---------------------------------------------------------------------------

/** Project one instance onto the bound (x,y,z); null if any axis has no value. */
function project(view: ChartView, inst: EvalInstance): Point3D | null {
  const q = view.qualityByInstanceId.get(inst.instance_id) ?? null;
  const x = axisValue(inst, view.axes.x, q);
  const y = axisValue(inst, view.axes.y, q);
  const z = axisValue(inst, view.axes.z, q);
  if (x == null || y == null || z == null) return null;
  return {
    value: [x, y, z],
    instance_id: inst.instance_id,
    agent: inst.agent_id,
    session: inst.session_id,
    index: inst.instance_index,
    latency_ms: inst.latency_ms,
    quality: q,
    corpus_size: inst.corpus_size,
  };
}

/** Distinct agent ids present in the view, in stable sorted order (color order). */
function agentsOf(view: ChartView): string[] {
  return [...new Set(view.instances.map((i) => i.agent_id))].sort();
}

/** Plottable points for one agent (already-filtered instances only). */
function pointsForAgent(view: ChartView, agentId: string): Point3D[] {
  const out: Point3D[] = [];
  for (const inst of view.instances) {
    if (inst.agent_id !== agentId) continue;
    const p = project(view, inst);
    if (p) out.push(p);
  }
  return out;
}

/** A tooltip formatter exposing agent/latency/quality/session/index/corpus (Req 14.3). */
function pointTooltip(): Record<string, unknown> {
  return {
    formatter: (params: { data?: Point3D }): string => {
      const d = params?.data;
      if (!d) return "";
      const q = d.quality == null ? "n/a" : d.quality.toFixed(3);
      return [
        `<b>${d.agent}</b>`,
        `latency ${Math.round(d.latency_ms)} ms`,
        `quality ${q}`,
        `session ${d.session}`,
        `index ${d.index}`,
        `corpus ${d.corpus_size}`,
      ].join("<br/>");
    },
  };
}

// ---------------------------------------------------------------------------
// build3DBase — shared grid3D + 3 axes (log on whichever axis is bound latency).
// ---------------------------------------------------------------------------

/**
 * The shared scene scaffold: a `grid3D` with rotate/zoom view-control (Req 14.2)
 * plus the three axes derived from the configurable mapping (Req 14.1). The axis
 * bound to latency renders on a log scale floored at `LOG_FLOOR_MS` (P7).
 */
export function build3DBase(view: ChartView): Record<string, unknown> {
  return build3DBaseWithControls(view);
}

export function build3DBaseWithControls(
  view: ChartView,
  controls?: Scene3DControls,
): Record<string, unknown> {
  const viewControl = controls
    ? {
        projection: controls.projection,
        alpha: controls.alpha,
        beta: controls.beta,
        distance: controls.distance,
        minDistance: controls.minDistance,
        maxDistance: controls.maxDistance,
        orthographicSize: controls.orthographicSize,
        minOrthographicSize: controls.minOrthographicSize,
        maxOrthographicSize: controls.maxOrthographicSize,
        center: controls.center,
        damping: controls.damping,
        autoRotate: controls.autoRotate,
        autoRotateDirection: controls.autoRotateDirection,
        autoRotateSpeed: controls.autoRotateSpeed,
        autoRotateAfterStill: controls.autoRotateAfterStill,
        minAlpha: controls.minAlpha,
        maxAlpha: controls.maxAlpha,
        minBeta: controls.minBeta,
        maxBeta: controls.maxBeta,
        rotateSensitivity: controls.rotateSensitivity,
        zoomSensitivity: controls.zoomSensitivity,
        panSensitivity: controls.panSensitivity,
        rotateMouseButton: controls.rotateMouseButton,
        panMouseButton: controls.panMouseButton,
      }
    : { autoRotate: false, distance: 200 };
  const axisLineStyle = controls?.showGrid === false ? { color: "rgba(154,167,180,0.30)" } : { color: "#9aa7b4" };
  const splitLineStyle =
    controls?.showGrid === false
      ? { show: false }
      : { lineStyle: { color: "rgba(255,255,255,0.08)" } };
  return {
    backgroundColor: "transparent",
    tooltip: { trigger: "item", confine: true },
    grid3D: {
      boxWidth: controls?.boxWidth ?? 100,
      boxDepth: controls?.boxDepth ?? 100,
      boxHeight: controls?.boxHeight ?? 100,
      viewControl,
      axisPointer: { show: controls?.showAxisPointer ?? true },
      axisLine: { lineStyle: axisLineStyle },
      axisLabel: { textStyle: { color: "#9aa7b4" } },
      splitLine: splitLineStyle,
    },
    xAxis3D: axisSpec(view.axes.x, axisDataRange(view, view.axes.x)),
    yAxis3D: axisSpec(view.axes.y, axisDataRange(view, view.axes.y)),
    zAxis3D: axisSpec(view.axes.z, axisDataRange(view, view.axes.z)),
  };
}

// ---------------------------------------------------------------------------
// buildTrajectory3DOption — line3D, one path per agent, ordered by instance_index.
// ---------------------------------------------------------------------------

/**
 * One connected `line3D` path per agent, with the path points emitted in
 * strictly ascending `instance_index` order (Req 10.2). The union of the series'
 * point ids is exactly the plottable instance ids (Property 4, 3D half).
 */
export function buildTrajectory3DOption(
  view: ChartView,
  controls?: Scene3DControls,
): EChartsOption {
  const base = build3DBaseWithControls(view, controls);
  const series = agentsOf(view).map((agentId) => {
    const pts = pointsForAgent(view, agentId).sort((a, b) =>
      a.index !== b.index
        ? a.index - b.index
        : a.instance_id < b.instance_id
          ? -1
          : a.instance_id > b.instance_id
            ? 1
            : 0,
    );
    return {
      type: "line3D",
      name: agentId,
      lineStyle: { color: view.agentColors.get(agentId), width: 3 },
      data: pts,
      tooltip: pointTooltip(),
    };
  });
  return { ...base, series } as unknown as EChartsOption;
}

// ---------------------------------------------------------------------------
// buildScatter3DOption — scatter3D, one point per Instance carrying instance_id.
// ---------------------------------------------------------------------------

/**
 * One `scatter3D` point per plottable Instance, grouped into a per-agent series
 * coloured by the stable map (Req 10.3 / P5). The multiset of point ids equals
 * the plottable instance ids exactly (Property 4, 3D half).
 */
export function buildScatter3DOption(
  view: ChartView,
  controls?: Scene3DControls,
): EChartsOption {
  const base = build3DBaseWithControls(view, controls);
  const series = agentsOf(view).map((agentId) => ({
    type: "scatter3D",
    name: agentId,
    symbolSize: 8,
    itemStyle: { color: view.agentColors.get(agentId) },
    data: pointsForAgent(view, agentId),
    tooltip: pointTooltip(),
  }));
  return { ...base, series } as unknown as EChartsOption;
}

// ---------------------------------------------------------------------------
// buildBubble3DOption — scatter3D + symbolSize by confidence|volume|cost.
// ---------------------------------------------------------------------------

function sizeSourceValue(inst: EvalInstance, sizeBy: BubbleSizeSource): number | null {
  const raw = inst[sizeBy];
  return raw != null && Number.isFinite(raw) ? raw : null;
}

/**
 * One `scatter3D` point per plottable Instance whose `symbolSize` encodes the
 * chosen source (confidence|volume|cost), normalized across the plotted set into
 * [MIN_BUBBLE, MAX_BUBBLE]; a missing/non-finite source value falls back to a
 * neutral default size (Req 10.5). Point-id bijection is preserved (Property 4).
 */
export function buildBubble3DOption(
  view: ChartView,
  sizeBy: BubbleSizeSource,
  controls?: Scene3DControls,
): EChartsOption {
  const base = build3DBaseWithControls(view, controls);

  // Normalization range over all plottable instances with a present value.
  let lo = Number.POSITIVE_INFINITY;
  let hi = Number.NEGATIVE_INFINITY;
  for (const inst of view.instances) {
    const v = sizeSourceValue(inst, sizeBy);
    if (v == null) continue;
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  const span = hi - lo;
  const sizeOf = (inst: EvalInstance): number => {
    const v = sizeSourceValue(inst, sizeBy);
    if (v == null) return DEFAULT_BUBBLE;
    if (!(span > 0)) return (MIN_BUBBLE + MAX_BUBBLE) / 2; // all equal → mid size
    const t = (v - lo) / span;
    return MIN_BUBBLE + t * (MAX_BUBBLE - MIN_BUBBLE);
  };

  const byId = new Map(view.instances.map((i) => [i.instance_id, i]));
  const series = agentsOf(view).map((agentId) => {
    const pts = pointsForAgent(view, agentId);
    return {
      type: "scatter3D",
      name: agentId,
      itemStyle: { color: view.agentColors.get(agentId), opacity: 0.85 },
      data: pts.map((p) => ({
        ...p,
        symbolSize: sizeOf(byId.get(p.instance_id)!),
      })),
      tooltip: pointTooltip(),
    };
  });
  return { ...base, series } as unknown as EChartsOption;
}

// ---------------------------------------------------------------------------
// buildSurface3DOption — per-agent downsampled lattice interpolation of quality.
// ---------------------------------------------------------------------------

/** Build the lattice edges over [min,max] with `buckets` cells; log-spaced if log. */
function latticeAxis(
  min: number,
  max: number,
  buckets: number,
  log: boolean,
): number[] {
  const n = Math.max(1, Math.floor(buckets));
  if (!(max > min)) return [min];
  const out: number[] = [];
  if (log) {
    const lmin = Math.log(Math.max(min, LOG_FLOOR_MS));
    const lmax = Math.log(Math.max(max, LOG_FLOOR_MS));
    for (let i = 0; i <= n; i++) out.push(Math.exp(lmin + ((lmax - lmin) * i) / n));
  } else {
    for (let i = 0; i <= n; i++) out.push(min + ((max - min) * i) / n);
  }
  return out;
}

/**
 * Per-agent interpolated quality landscape over the (X=latency, Z=instance) plane
 * (Req 10.4). The raw points are downsampled onto a lattice and the bound Y
 * (quality) is interpolated by nearest-neighbour or inverse-distance ("bilinear")
 * over the agent's own points. Surface series are an aggregate landscape, not a
 * per-record rendering, so they do not participate in the Property-4 point
 * bijection (that property targets the scatter/bubble/trajectory builders).
 */
export function buildSurface3DOption(
  view: ChartView,
  grid: SurfaceGridSpec,
  controls?: Scene3DControls,
): EChartsOption {
  const base = build3DBaseWithControls(view, controls);
  const xLog = view.axes.x.scale === "log";

  const series = agentsOf(view).map((agentId) => {
    const pts = pointsForAgent(view, agentId);
    let xMin = Number.POSITIVE_INFINITY;
    let xMax = Number.NEGATIVE_INFINITY;
    let zMin = Number.POSITIVE_INFINITY;
    let zMax = Number.NEGATIVE_INFINITY;
    for (const p of pts) {
      const [x, , z] = p.value;
      if (x < xMin) xMin = x;
      if (x > xMax) xMax = x;
      if (z < zMin) zMin = z;
      if (z > zMax) zMax = z;
    }

    const xs = pts.length ? latticeAxis(xMin, xMax, grid.xBuckets, xLog) : [];
    const zs = pts.length ? latticeAxis(zMin, zMax, grid.zBuckets, false) : [];

    const interp = (gx: number, gz: number): number => {
      if (grid.method === "nearest") {
        let best = 0;
        let bestD = Number.POSITIVE_INFINITY;
        for (const p of pts) {
          const dx = p.value[0] - gx;
          const dz = p.value[2] - gz;
          const d = dx * dx + dz * dz;
          if (d < bestD) {
            bestD = d;
            best = p.value[1];
          }
        }
        return best;
      }
      // inverse-distance weighting ("bilinear" lattice fill).
      let wsum = 0;
      let vsum = 0;
      for (const p of pts) {
        const dx = p.value[0] - gx;
        const dz = p.value[2] - gz;
        const d2 = dx * dx + dz * dz;
        if (d2 === 0) return p.value[1];
        const w = 1 / d2;
        wsum += w;
        vsum += w * p.value[1];
      }
      return wsum > 0 ? vsum / wsum : 0;
    };

    const data: Array<[number, number, number]> = [];
    for (const gx of xs) {
      for (const gz of zs) data.push([gx, interp(gx, gz), gz]);
    }
    return {
      type: "surface",
      name: agentId,
      itemStyle: { color: view.agentColors.get(agentId), opacity: 0.6 },
      data,
    };
  });

  return { ...base, series } as unknown as EChartsOption;
}
