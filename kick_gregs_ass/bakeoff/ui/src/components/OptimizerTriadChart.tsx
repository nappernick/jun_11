/**
 * Champion-vs-challenger triad scores WITH confidence intervals across iterations
 * for one Target_Model (design "Live dashboard / Quality-Tab design"; Req 9.2).
 *
 * x = `iteration_index`; two line series (champion, challenger) of the triad
 * decision metric, each overlaid with 95%-CI **error bars** whose half-extent is
 * the event's `ci_half_width`. The error bars are drawn with an ECharts `custom`
 * series (the standard echarts idiom for error bars: a renderItem that draws a
 * whiskered vertical line from `ci_low` to `ci_high`). Reuses the shared `EChart`
 * wrapper unchanged — all data shaping stays here.
 */
import { useMemo } from "react";
import type { JSX } from "react";
import type {
  CustomSeriesOption,
  CustomSeriesRenderItem,
  CustomSeriesRenderItemAPI,
  CustomSeriesRenderItemParams,
  EChartsOption,
  LineSeriesOption,
} from "echarts";
import { EChart } from "./EChart";
import type { IterationScores, ScoredPoint } from "../api/useOptimizerStream";

const CHAMPION_COLOR = "#7c5cff";
const CHALLENGER_COLOR = "#22b8a6";

export interface OptimizerTriadChartProps {
  readonly iterations: readonly IterationScores[];
  /** Phase-B final triad + CI, rendered as a marked reference band when present. */
  readonly phaseB?: { readonly triad: number; readonly ciHalfWidth: number } | null;
}

/** One CI whisker datum: [iterationIndex, low, high] for the custom series. */
type WhiskerDatum = readonly [number, number, number];

function whiskers(
  iterations: readonly IterationScores[],
  pick: (it: IterationScores) => ScoredPoint | null,
): WhiskerDatum[] {
  const out: WhiskerDatum[] = [];
  for (const it of iterations) {
    const p = pick(it);
    if (!p) continue;
    out.push([it.iterationIndex, p.ciLow, p.ciHigh]);
  }
  return out;
}

/** renderItem for the CI error bars: a vertical line with top/bottom caps. */
function makeErrorBarRenderer(color: string): CustomSeriesRenderItem {
  return (
    params: CustomSeriesRenderItemParams,
    api: CustomSeriesRenderItemAPI,
  ) => {
    void params;
    const xValue = api.value(0) as number;
    const lowPt = api.coord([xValue, api.value(1) as number]);
    const highPt = api.coord([xValue, api.value(2) as number]);
    const lowX = lowPt[0] ?? 0;
    const lowY = lowPt[1] ?? 0;
    const highX = highPt[0] ?? 0;
    const highY = highPt[1] ?? 0;
    const halfCap = 5;
    const style = { stroke: color, lineWidth: 1.5 };
    return {
      type: "group" as const,
      children: [
        {
          type: "line" as const,
          shape: { x1: highX, y1: highY, x2: lowX, y2: lowY },
          style,
        },
        {
          type: "line" as const,
          shape: { x1: highX - halfCap, y1: highY, x2: highX + halfCap, y2: highY },
          style,
        },
        {
          type: "line" as const,
          shape: { x1: lowX - halfCap, y1: lowY, x2: lowX + halfCap, y2: lowY },
          style,
        },
      ],
    };
  };
}

export function OptimizerTriadChart({
  iterations,
  phaseB,
}: OptimizerTriadChartProps): JSX.Element {
  const option = useMemo<EChartsOption>(() => {
    const xs = iterations.map((it) => it.iterationIndex);
    const championLine: number[][] = [];
    const challengerLine: number[][] = [];
    for (const it of iterations) {
      if (it.champion) championLine.push([it.iterationIndex, it.champion.triad]);
      if (it.challenger) challengerLine.push([it.iterationIndex, it.challenger.triad]);
    }

    const championSeries: LineSeriesOption = {
      name: "champion",
      type: "line",
      color: CHAMPION_COLOR,
      smooth: false,
      symbolSize: 8,
      connectNulls: true,
      lineStyle: { width: 2 },
      data: championLine,
    };
    // Always define markLine so a merge update (notMerge:false) can also CLEAR the
    // phase-B band if it ever goes away — empty data when there is no phase-B result.
    championSeries.markLine = {
      silent: true,
      symbol: "none",
      lineStyle: { color: "#e0a458", type: "dashed", width: 1.5 },
      label: {
        formatter: phaseB
          ? `phase B ${phaseB.triad.toFixed(3)} ±${phaseB.ciHalfWidth.toFixed(3)}`
          : "",
        color: "#e0a458",
        position: "insideEndTop",
      },
      data: phaseB ? [{ yAxis: phaseB.triad }] : [],
    };

    const challengerSeries: LineSeriesOption = {
      name: "challenger",
      type: "line",
      color: CHALLENGER_COLOR,
      smooth: false,
      symbolSize: 8,
      symbol: "diamond",
      connectNulls: true,
      lineStyle: { width: 2, type: "dashed" },
      data: challengerLine,
    };

    const championCi: CustomSeriesOption = {
      name: "champion CI",
      type: "custom",
      renderItem: makeErrorBarRenderer(CHAMPION_COLOR),
      itemStyle: { color: CHAMPION_COLOR },
      encode: { x: 0, y: [1, 2] },
      data: whiskers(iterations, (it) => it.champion).map((w) => [w[0], w[1], w[2]]),
      z: 3,
      silent: true,
      tooltip: { show: false },
      legendHoverLink: false,
    };

    const challengerCi: CustomSeriesOption = {
      name: "challenger CI",
      type: "custom",
      renderItem: makeErrorBarRenderer(CHALLENGER_COLOR),
      itemStyle: { color: CHALLENGER_COLOR },
      encode: { x: 0, y: [1, 2] },
      data: whiskers(iterations, (it) => it.challenger).map((w) => [w[0], w[1], w[2]]),
      z: 3,
      silent: true,
      tooltip: { show: false },
      legendHoverLink: false,
    };

    return {
      backgroundColor: "transparent",
      grid: { left: 48, right: 18, top: 34, bottom: 72 },
      legend: {
        top: 0,
        data: ["champion", "challenger"],
        textStyle: { color: "#b9c0d4" },
      },
      toolbox: {
        right: 12,
        top: 2,
        itemSize: 14,
        iconStyle: { borderColor: "#9aa3bd" },
        emphasis: { iconStyle: { borderColor: CHAMPION_COLOR } },
        feature: {
          // Box-select rectangle zoom (drag a region) + one-click restore.
          dataZoom: { title: { zoom: "box zoom", back: "undo zoom" } },
          restore: { title: "reset" },
        },
      },
      tooltip: {
        trigger: "axis",
        // Keep the tooltip up while the pointer is on the chart, let the pointer
        // move INTO the tooltip, and never auto-dismiss it out from under a hover.
        enterable: true,
        confine: true,
        hideDelay: 0,
        transitionDuration: 0,
        axisPointer: { type: "line", snap: true },
        valueFormatter: (v) => (v == null ? "—" : Number(v).toFixed(3)),
      },
      // Zoomable + manipulable: wheel-zoom and drag-pan on the iteration axis via the
      // inside zoom, plus a draggable slider. filterMode "none" keeps the y-range
      // (0..1 triad) fixed while zooming x, and omitting start/end means a merge
      // update never resets the user's current zoom window.
      dataZoom: [
        {
          type: "inside",
          xAxisIndex: 0,
          filterMode: "none",
          zoomOnMouseWheel: true,
          moveOnMouseMove: true,
          moveOnMouseWheel: false,
        },
        {
          type: "slider",
          xAxisIndex: 0,
          filterMode: "none",
          bottom: 8,
          height: 16,
          borderColor: "transparent",
          backgroundColor: "rgba(255,255,255,0.04)",
          fillerColor: "rgba(124,92,255,0.18)",
          handleStyle: { color: CHAMPION_COLOR },
          moveHandleStyle: { color: CHAMPION_COLOR },
          dataBackground: { lineStyle: { color: "#7488a3" }, areaStyle: { color: "rgba(124,92,255,0.10)" } },
          textStyle: { color: "#9aa3bd" },
        },
      ],
      xAxis: {
        type: "value",
        name: "iteration",
        nameLocation: "middle",
        nameGap: 24,
        minInterval: 1,
        min: xs.length ? Math.min(...xs) : 0,
        max: xs.length ? Math.max(...xs) : 1,
        axisLabel: { color: "#9aa3bd", formatter: (v: number) => `${v}` },
        nameTextStyle: { color: "#7488a3" },
        splitLine: { lineStyle: { color: "rgba(255,255,255,0.06)" } },
      },
      yAxis: {
        type: "value",
        name: "triad",
        min: 0,
        max: 1,
        axisLabel: { color: "#9aa3bd" },
        nameTextStyle: { color: "#7488a3" },
        splitLine: { lineStyle: { color: "rgba(255,255,255,0.06)" } },
      },
      series: [championSeries, challengerSeries, championCi, challengerCi],
    };
  }, [iterations, phaseB]);

  if (iterations.length === 0) {
    return (
      <div className="empty">
        Champion vs challenger triad scores light up here as the loop scores each iteration.
      </div>
    );
  }
  return (
    <EChart
      option={option}
      height={340}
      notMerge={false}
      ariaLabel="Champion versus challenger triad scores with confidence intervals across iterations"
    />
  );
}
