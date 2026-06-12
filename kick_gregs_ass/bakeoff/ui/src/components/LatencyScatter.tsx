/**
 * Streaming scatter of recent trials for the Bake-Off race: x = arrival order
 * (newest at the right), y = end_to_end_ms (time to final token), one point per
 * trial colored by model. This shows *spread*, not just the per-model mean — a
 * fast-on-average model that occasionally spikes reads as scattered high points.
 *
 * Errored trials carry no meaningful latency, so they are excluded here (they are
 * still surfaced in the fleet error counts and the recent-trials ticker). One
 * scatter series per model so the legend doubles as a color key. Fully
 * judge-agnostic — this is pure latency.
 */
import { useMemo } from "react";
import type { JSX } from "react";
import type { EChartsOption, ScatterSeriesOption } from "echarts";
import type { TrialCompleted } from "../api/types";
import { EChart } from "./EChart";
import { modelColor, ms } from "../lib/format";

export interface LatencyScatterProps {
  readonly events: readonly TrialCompleted[];
  /** Most-recent N trials to plot (the buffer is newest-first). */
  readonly window?: number;
}

export function LatencyScatter({ events, window = 240 }: LatencyScatterProps): JSX.Element {
  const option = useMemo<EChartsOption>(() => {
    // Buffer is newest-first; take the recent window and order oldest→newest so
    // arrival order increases left→right.
    const recent = events.slice(0, window).filter((e) => !e.error && Number.isFinite(e.end_to_end_ms));
    const ordered = [...recent].reverse();

    const byModel = new Map<string, [number, number][]>();
    ordered.forEach((e, i) => {
      const arr = byModel.get(e.model) ?? [];
      arr.push([i, e.end_to_end_ms]);
      byModel.set(e.model, arr);
    });

    const series: ScatterSeriesOption[] = [...byModel.entries()]
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([model, data]) => ({
        type: "scatter",
        name: model,
        data,
        symbolSize: 7,
        itemStyle: { color: modelColor(model), opacity: 0.8 },
      }));

    return {
      backgroundColor: "transparent",
      grid: { left: 56, right: 18, top: 16, bottom: 30, containLabel: true },
      legend: {
        type: "scroll",
        bottom: 0,
        textStyle: { color: "#7488a3", fontFamily: "var(--mono)", fontSize: 10 },
        inactiveColor: "#3a4f6a",
      },
      tooltip: {
        trigger: "item",
        formatter: (p: unknown) => {
          const param = p as { seriesName: string; value: [number, number] };
          return `<b>${param.seriesName}</b><br/>e2e ${ms(param.value[1])}`;
        },
      },
      xAxis: {
        type: "value",
        name: "arrival order →",
        nameLocation: "middle",
        nameGap: 22,
        axisLabel: { color: "#7488a3", fontFamily: "var(--mono)", fontSize: 11 },
        nameTextStyle: { color: "#7488a3", fontSize: 11 },
        splitLine: { lineStyle: { color: "rgba(140,165,200,0.08)" } },
      },
      yAxis: {
        type: "value",
        name: "end-to-end ms",
        nameTextStyle: { color: "#7488a3", fontSize: 11 },
        axisLabel: { color: "#7488a3", fontFamily: "var(--mono)", fontSize: 11 },
        splitLine: { lineStyle: { color: "rgba(140,165,200,0.10)" } },
      },
      series,
    };
  }, [events, window]);

  const hasPoints = events.some((e) => !e.error && Number.isFinite(e.end_to_end_ms));
  if (!hasPoints) {
    return <div className="empty">No latency samples yet.</div>;
  }
  return <EChart option={option} height={300} ariaLabel="Streaming per-trial end-to-end latency scatter" />;
}
