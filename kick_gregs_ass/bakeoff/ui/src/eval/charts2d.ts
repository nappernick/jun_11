/**
 * 2D chart-option builders for the ragas eval dashboard (design C6).
 *
 * Each exported function is a PURE function of the already-derived `ChartView`
 * and returns a typed `EChartsOption` consumed by the existing `EChart.tsx`
 * wrapper. The 2D views reuse the same stable agent→color map and the same
 * recomputed composite as the 3D views, so an agent reads identically across
 * every chart and tab (Req 10.7 / P5).
 *
 * Two correctness disciplines are load-bearing here:
 *
 *  - Property 9 (retrieval ≠ ragas): `buildRetrievalVsRagas2DOption` emits the
 *    two metric families as DISTINCT, separately labeled series and never sums a
 *    value across the two. Every series is tagged with the single family and
 *    single metric it draws from; the ragas-family metric names are a subset of
 *    the instance `ragas` keys and the retrieval-family names a subset of the
 *    `retrieval` keys, and the two name sets stay disjoint.
 *  - Property 4 (2D half): `buildSpeedQuality2DOption` emits exactly one scatter
 *    point per plottable instance (non-null composite quality), each datum
 *    carrying its `instance_id`, so the rendered-point ↔ record bijection holds.
 */
import type { EChartsOption } from "echarts";
import type { EvalInstance, MetricValue } from "../api/types";
import { clampUnit } from "./evalQuality";
import { logSafe } from "./axisMapping";
import type { ChartView } from "./evalSelectors";

/** A 2D point datum carrying its backing record id (bijection key, P4). */
interface Point2D {
  readonly value: readonly [number, number];
  readonly instance_id: string;
  readonly agent: string;
}

interface RegressionPoint {
  readonly x: number;
  readonly y: number;
}

/** Distinct agent ids present in the view, in stable sorted (color) order. */
function agentsOf(view: ChartView): string[] {
  return [...new Set(view.instances.map((i) => i.agent_id))].sort();
}

/** Read a recorded metric value (ragas first, then retrieval), clamped to [0,1]. */
function metricValueOf(inst: EvalInstance, name: string): number | null {
  const mv: MetricValue | undefined = inst.ragas[name] ?? inst.retrieval[name];
  return mv && !mv.unavailable && mv.value != null ? clampUnit(mv.value) : null;
}

function displayMetricLabel(metricName: string): string {
  return metricName === "composite" ? "composite quality" : metricName;
}

function regressionLine(points: readonly RegressionPoint[]): readonly [number, number][] {
  if (points.length < 2) return [];
  let xSum = 0;
  let ySum = 0;
  let xySum = 0;
  let xxSum = 0;
  let xMin = Number.POSITIVE_INFINITY;
  let xMax = Number.NEGATIVE_INFINITY;
  for (const point of points) {
    xSum += point.x;
    ySum += point.y;
    xySum += point.x * point.y;
    xxSum += point.x * point.x;
    if (point.x < xMin) xMin = point.x;
    if (point.x > xMax) xMax = point.x;
  }
  const pointCount = points.length;
  const denominator = pointCount * xxSum - xSum * xSum;
  if (denominator === 0) return [];
  const slope = (pointCount * xySum - xSum * ySum) / denominator;
  const intercept = (ySum - slope * xSum) / pointCount;
  return [
    [xMin, intercept + slope * xMin],
    [xMax, intercept + slope * xMax],
  ];
}

const axisLabelStyle = { color: "#9aa7b4" } as const;
const splitLineStyle = { lineStyle: { color: "rgba(255,255,255,0.06)" } } as const;

// ---------------------------------------------------------------------------
// buildSpeedQuality2DOption — latency × quality scatter + Ideal_Region quadrant.
// ---------------------------------------------------------------------------

/**
 * Speed × quality scatter for the selected agents (Req 11.1) with the
 * Ideal_Region quadrant marker (low latency + high quality, Req 13.1). One point
 * per plottable instance (non-null composite quality), grouped per agent and
 * coloured by the stable map; each datum carries `instance_id` so the rendered
 * point set is exactly the plottable record set (Property 4, 2D half).
 */
export function buildSpeedQuality2DOption(view: ChartView): EChartsOption {
  const agents = agentsOf(view);
  const frontierPoints: readonly [number, number][] = view.instances
    .map((inst) => ({
      latency: logSafe(inst.latency_ms),
      quality: view.qualityByInstanceId.get(inst.instance_id) ?? null,
    }))
    .filter((point): point is { latency: number; quality: number } => point.quality != null)
    .sort((leftPoint, rightPoint) => leftPoint.latency - rightPoint.latency)
    .reduce<Array<[number, number]>>((frontier, point) => {
      const bestQuality = frontier.length > 0 ? frontier[frontier.length - 1]![1] : -Infinity;
      if (point.quality > bestQuality) frontier.push([point.latency, point.quality]);
      return frontier;
    }, []);
  const series = agents.map((agentId) => {
    const data: Point2D[] = [];
    for (const inst of view.instances) {
      if (inst.agent_id !== agentId) continue;
      const q = view.qualityByInstanceId.get(inst.instance_id) ?? null;
      if (q == null) continue;
      data.push({
        value: [logSafe(inst.latency_ms), q],
        instance_id: inst.instance_id,
        agent: agentId,
      });
    }
    return {
      type: "scatter" as const,
      name: agentId,
      symbolSize: 9,
      itemStyle: { color: view.agentColors.get(agentId) },
      data,
    };
  });

  // Ideal_Region: the high-quality half (y >= 0.75); latency is open to the
  // left (lower is better). markArea is attached to a silent backdrop series.
  const idealSeries =
    view.idealRegion && agents.length > 0
      ? [
          {
            type: "scatter" as const,
            name: "Ideal region",
            data: [] as Point2D[],
            silent: true,
            markArea: {
              itemStyle: { color: "rgba(76,195,138,0.10)" },
              label: { show: true, position: "insideTopLeft", color: "#4cc38a", formatter: "Ideal" },
              data: [[{ yAxis: 0.75 }, { yAxis: 1 }]],
            },
          },
        ]
      : [];

  return {
    backgroundColor: "transparent",
    grid: { left: 56, right: 24, top: 24, bottom: 48 },
    tooltip: {
      trigger: "item",
      confine: true,
      formatter: (params: unknown) => {
        const value = (params as { value?: [number, number]; data?: Point2D }).value;
        const data = (params as { data?: Point2D }).data;
        if (!value) return "";
        return [
          data?.agent ? `<b>${data.agent}</b>` : "<b>frontier</b>",
          `latency ${Math.round(value[0])} ms`,
          `quality ${value[1].toFixed(3)}`,
          data?.instance_id ? `instance ${data.instance_id}` : "",
        ].filter(Boolean).join("<br/>");
      },
    },
    legend: { textStyle: axisLabelStyle },
    xAxis: {
      type: "log",
      name: "latency (ms) — lower is better (log)",
      nameLocation: "middle",
      nameGap: 30,
      min: 1,
      axisLabel: axisLabelStyle,
      nameTextStyle: axisLabelStyle,
      splitLine: splitLineStyle,
    },
    yAxis: {
      type: "value",
      name: "quality — higher is better",
      nameLocation: "middle",
      nameGap: 40,
      min: 0,
      max: 1,
      axisLabel: axisLabelStyle,
      nameTextStyle: axisLabelStyle,
      splitLine: splitLineStyle,
    },
    series: [
      ...idealSeries,
      ...series,
      {
        type: "line",
        name: "Pareto frontier",
        data: frontierPoints,
        symbolSize: 5,
        lineStyle: { color: "#f7d46b", width: 2.5 },
        itemStyle: { color: "#f7d46b" },
        z: 8,
      },
    ],
  } as EChartsOption;
}

// ---------------------------------------------------------------------------
// buildMetricOverInstances2DOption — metric|composite vs instance_index per agent.
// ---------------------------------------------------------------------------

/**
 * The selected metric (or the composite) plotted against `instance_index`, one
 * line per agent (Req 11.2). Points are emitted in ascending `instance_index`
 * order and carry their `instance_id`.
 */
export function buildMetricOverInstances2DOption(
  view: ChartView,
  metric: string | "composite",
): EChartsOption {
  const agents = agentsOf(view);
  const series = agents.map((agentId) => {
    const rows: Point2D[] = [];
    for (const inst of view.instances) {
      if (inst.agent_id !== agentId) continue;
      const v =
        metric === "composite"
          ? (view.qualityByInstanceId.get(inst.instance_id) ?? null)
          : metricValueOf(inst, metric);
      if (v == null) continue;
      rows.push({
        value: [inst.instance_index, v],
        instance_id: inst.instance_id,
        agent: agentId,
      });
    }
    rows.sort((a, b) => a.value[0] - b.value[0]);
    return {
      type: "line" as const,
      name: agentId,
      showSymbol: true,
      symbolSize: 6,
      lineStyle: { color: view.agentColors.get(agentId), width: 2 },
      itemStyle: { color: view.agentColors.get(agentId) },
      data: rows,
    };
  });

  const label = metric === "composite" ? "composite quality" : metric;
  return {
    backgroundColor: "transparent",
    grid: { left: 56, right: 24, top: 24, bottom: 48 },
    tooltip: { trigger: "item", confine: true },
    legend: { textStyle: axisLabelStyle },
    xAxis: {
      type: "value",
      name: "instance index — forward is later",
      nameLocation: "middle",
      nameGap: 30,
      axisLabel: axisLabelStyle,
      nameTextStyle: axisLabelStyle,
      splitLine: splitLineStyle,
    },
    yAxis: {
      type: "value",
      name: `${label} — higher is better`,
      nameLocation: "middle",
      nameGap: 40,
      min: 0,
      max: 1,
      axisLabel: axisLabelStyle,
      nameTextStyle: axisLabelStyle,
      splitLine: splitLineStyle,
    },
    dataZoom: [
      { type: "inside", xAxisIndex: 0 },
      { type: "slider", xAxisIndex: 0, height: 18, bottom: 5, textStyle: axisLabelStyle },
    ],
    series,
  } as EChartsOption;
}

// ---------------------------------------------------------------------------
// buildCorpusCurve2DOption — latency AND quality vs corpus_size (the sweep curve).
// ---------------------------------------------------------------------------

/**
 * The Corpus_Size_Sweep performance curve (Req 11.3, Req 6): for each agent,
 * BOTH latency (left axis) and composite quality (right axis) are plotted
 * against `corpus_size` as two DISTINCT, separately labeled series — they are
 * never combined into one number. Each agent's series are aggregated to the mean
 * value per corpus size and ordered by corpus size.
 */
export function buildCorpusCurve2DOption(view: ChartView): EChartsOption {
  const agents = agentsOf(view);

  type Acc = { latSum: number; latN: number; qSum: number; qN: number };
  const series: Array<Record<string, unknown>> = [];

  for (const agentId of agents) {
    const bySize = new Map<number, Acc>();
    for (const inst of view.instances) {
      if (inst.agent_id !== agentId) continue;
      const acc =
        bySize.get(inst.corpus_size) ??
        bySize.set(inst.corpus_size, { latSum: 0, latN: 0, qSum: 0, qN: 0 }).get(inst.corpus_size)!;
      if (Number.isFinite(inst.latency_ms)) {
        acc.latSum += inst.latency_ms;
        acc.latN += 1;
      }
      const q = view.qualityByInstanceId.get(inst.instance_id) ?? null;
      if (q != null) {
        acc.qSum += q;
        acc.qN += 1;
      }
    }
    const sizes = [...bySize.keys()].sort((a, b) => a - b);
    const latData = sizes
      .filter((s) => bySize.get(s)!.latN > 0)
      .map((s) => [s, bySize.get(s)!.latSum / bySize.get(s)!.latN] as [number, number]);
    const qData = sizes
      .filter((s) => bySize.get(s)!.qN > 0)
      .map((s) => [s, bySize.get(s)!.qSum / bySize.get(s)!.qN] as [number, number]);

    series.push({
      type: "line",
      name: `${agentId} · latency`,
      metricFamily: "latency",
      yAxisIndex: 0,
      lineStyle: { color: view.agentColors.get(agentId), width: 2, type: "dashed" },
      itemStyle: { color: view.agentColors.get(agentId) },
      data: latData,
    });
    series.push({
      type: "line",
      name: `${agentId} · quality`,
      metricFamily: "quality",
      yAxisIndex: 1,
      lineStyle: { color: view.agentColors.get(agentId), width: 2 },
      itemStyle: { color: view.agentColors.get(agentId) },
      data: qData,
    });
  }

  return {
    backgroundColor: "transparent",
    grid: { left: 64, right: 64, top: 24, bottom: 48 },
    tooltip: { trigger: "item", confine: true },
    legend: { textStyle: axisLabelStyle },
    xAxis: {
      type: "value",
      name: "corpus size",
      nameLocation: "middle",
      nameGap: 30,
      axisLabel: axisLabelStyle,
      nameTextStyle: axisLabelStyle,
      splitLine: splitLineStyle,
    },
    yAxis: [
      {
        type: "value",
        name: "latency (ms) — lower is better",
        position: "left",
        axisLabel: axisLabelStyle,
        nameTextStyle: axisLabelStyle,
        splitLine: splitLineStyle,
      },
      {
        type: "value",
        name: "quality — higher is better",
        position: "right",
        min: 0,
        max: 1,
        axisLabel: axisLabelStyle,
        nameTextStyle: axisLabelStyle,
        splitLine: { show: false },
      },
    ],
    series,
  } as unknown as EChartsOption;
}

// ---------------------------------------------------------------------------
// buildRetrievalCorrelation2DOption — retrieval metric vs quality metric.
// ---------------------------------------------------------------------------

export function buildRetrievalCorrelation2DOption(
  view: ChartView,
  retrievalMetric: string,
  qualityMetric: string | "composite",
): EChartsOption {
  const agents = agentsOf(view);
  const allPoints: RegressionPoint[] = [];
  const series = agents.map((agentId) => {
    const data: Point2D[] = [];
    for (const inst of view.instances) {
      if (inst.agent_id !== agentId) continue;
      const retrievalValue = metricValueOf(inst, retrievalMetric);
      const qualityValue =
        qualityMetric === "composite"
          ? (view.qualityByInstanceId.get(inst.instance_id) ?? null)
          : metricValueOf(inst, qualityMetric);
      if (retrievalValue == null || qualityValue == null) continue;
      data.push({
        value: [retrievalValue, qualityValue],
        instance_id: inst.instance_id,
        agent: agentId,
      });
      allPoints.push({ x: retrievalValue, y: qualityValue });
    }
    return {
      type: "scatter" as const,
      name: agentId,
      symbolSize: 9,
      itemStyle: { color: view.agentColors.get(agentId), opacity: 0.82 },
      data,
    };
  });
  const regression = regressionLine(allPoints);
  return {
    backgroundColor: "transparent",
    grid: { left: 58, right: 24, top: 30, bottom: 50 },
    tooltip: {
      trigger: "item",
      confine: true,
      formatter: (params: unknown) => {
        const value = (params as { value?: [number, number]; data?: Point2D }).value;
        const data = (params as { data?: Point2D }).data;
        if (!value) return "";
        return [
          data?.agent ? `<b>${data.agent}</b>` : "<b>trend</b>",
          `${retrievalMetric} ${value[0].toFixed(3)}`,
          `${displayMetricLabel(qualityMetric)} ${value[1].toFixed(3)}`,
          data?.instance_id ? `instance ${data.instance_id}` : "",
        ].filter(Boolean).join("<br/>");
      },
    },
    legend: { textStyle: axisLabelStyle },
    xAxis: {
      type: "value",
      name: `${retrievalMetric} — higher is better`,
      nameLocation: "middle",
      nameGap: 32,
      min: 0,
      max: 1,
      axisLabel: axisLabelStyle,
      nameTextStyle: axisLabelStyle,
      splitLine: splitLineStyle,
    },
    yAxis: {
      type: "value",
      name: `${displayMetricLabel(qualityMetric)} — higher is better`,
      nameLocation: "middle",
      nameGap: 42,
      min: 0,
      max: 1,
      axisLabel: axisLabelStyle,
      nameTextStyle: axisLabelStyle,
      splitLine: splitLineStyle,
    },
    series: [
      ...series,
      {
        type: "line",
        name: "regression",
        data: regression,
        showSymbol: false,
        lineStyle: { color: "#f7d46b", width: 2, type: "dashed" },
        silent: true,
      },
    ],
  } as EChartsOption;
}

// ---------------------------------------------------------------------------
// buildRetrievalVsRagas2DOption — retrieval vs ragas as DISTINCT series (P9).
// ---------------------------------------------------------------------------

/** A series tagged with the single metric family + name it draws from (P9). */
export interface FamilyTaggedSeries {
  readonly type: "line";
  readonly name: string;
  readonly metricFamily: "ragas" | "retrieval";
  readonly metricName: string;
  readonly data: ReadonlyArray<readonly [number, number]>;
  readonly lineStyle: Record<string, unknown>;
  readonly itemStyle: Record<string, unknown>;
}

/**
 * Retrieval-quality and generation-quality (ragas) metrics rendered as DISTINCT,
 * separately labeled series — never conflated or summed (Req 11.4 / Property 9).
 *
 * Each emitted series draws from exactly ONE metric of exactly ONE family: a
 * ragas-family series' `metricName` is a key of some instance's `ragas` map, a
 * retrieval-family series' `metricName` is a key of some instance's `retrieval`
 * map, and the two name sets are disjoint (the maps are disjoint by contract).
 * No series value aggregates a ragas value together with a retrieval value.
 * Each series plots mean(value) per `instance_index` over the plottable
 * instances, so the value is always a within-metric aggregate.
 */
export function buildRetrievalVsRagas2DOption(view: ChartView): EChartsOption {
  // Collect the metric names present per family across plottable instances.
  const ragasNames = new Set<string>();
  const retrievalNames = new Set<string>();
  for (const inst of view.instances) {
    for (const [name, mv] of Object.entries(inst.ragas)) {
      if (mv && !mv.unavailable && mv.value != null) ragasNames.add(name);
    }
    for (const [name, mv] of Object.entries(inst.retrieval)) {
      if (mv && !mv.unavailable && mv.value != null) retrievalNames.add(name);
    }
  }

  /** Mean (within a single metric) per instance_index — never crosses families. */
  const seriesFor = (
    name: string,
    family: "ragas" | "retrieval",
    pick: (inst: EvalInstance) => MetricValue | undefined,
    color: string,
    dashed: boolean,
  ): FamilyTaggedSeries => {
    const byIndex = new Map<number, { sum: number; n: number }>();
    for (const inst of view.instances) {
      const mv = pick(inst);
      if (!mv || mv.unavailable || mv.value == null) continue;
      const acc = byIndex.get(inst.instance_index) ?? { sum: 0, n: 0 };
      acc.sum += clampUnit(mv.value);
      acc.n += 1;
      byIndex.set(inst.instance_index, acc);
    }
    const data = [...byIndex.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([idx, acc]) => [idx, acc.sum / acc.n] as [number, number]);
    return {
      type: "line",
      name: `${family}: ${name}`,
      metricFamily: family,
      metricName: name,
      data,
      lineStyle: { color, width: 2, type: dashed ? "dashed" : "solid" },
      itemStyle: { color },
    };
  };

  // Distinct palettes per family so the eye never reads them as the same scale.
  const RAGAS_COLORS = ["#5aa9f7", "#c08bf0", "#5fd0c8", "#86a8ff", "#a0d6ff"];
  const RETRIEVAL_COLORS = ["#f7a14b", "#e5688b", "#ffc684", "#ff9a76", "#ffd0a0"];

  const series: FamilyTaggedSeries[] = [];
  [...ragasNames].sort().forEach((name, i) =>
    series.push(
      seriesFor(name, "ragas", (inst) => inst.ragas[name], RAGAS_COLORS[i % RAGAS_COLORS.length]!, false),
    ),
  );
  [...retrievalNames].sort().forEach((name, i) =>
    series.push(
      seriesFor(
        name,
        "retrieval",
        (inst) => inst.retrieval[name],
        RETRIEVAL_COLORS[i % RETRIEVAL_COLORS.length]!,
        true,
      ),
    ),
  );

  return {
    backgroundColor: "transparent",
    grid: { left: 56, right: 24, top: 24, bottom: 48 },
    tooltip: { trigger: "item", confine: true },
    legend: { textStyle: axisLabelStyle },
    xAxis: {
      type: "value",
      name: "instance index — forward is later",
      nameLocation: "middle",
      nameGap: 30,
      axisLabel: axisLabelStyle,
      nameTextStyle: axisLabelStyle,
      splitLine: splitLineStyle,
    },
    yAxis: {
      type: "value",
      name: "metric value (0..1) — distinct families, never summed",
      nameLocation: "middle",
      nameGap: 40,
      min: 0,
      max: 1,
      axisLabel: axisLabelStyle,
      nameTextStyle: axisLabelStyle,
      splitLine: splitLineStyle,
    },
    series,
  } as unknown as EChartsOption;
}
