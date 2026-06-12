/**
 * Per-model **time-to-first-token (TTFT)** distribution, computed live from
 * streamed trial_completed events. TTFT is the responsiveness metric the operator
 * cares most about (how fast does the user see *something*). Latency is
 * right-skewed, so we show a boxplot (min / q1 / median / q3 / max) per model
 * rather than a lone mean — matching the design's "report latency as a
 * distribution" rule. Fully judge-agnostic.
 */
import { useMemo } from "react";
import type { JSX } from "react";
import type { EChartsOption } from "echarts";
import type { TrialCompleted } from "../api/types";
import { EChart } from "./EChart";
import { modelColor } from "../lib/format";

export interface LatencyChartProps {
  readonly events: readonly TrialCompleted[];
  readonly focusModel: string | null;
}

interface Box {
  readonly model: string;
  readonly five: [number, number, number, number, number];
  readonly n: number;
}

function quantile(sorted: readonly number[], q: number): number {
  if (sorted.length === 0) return 0;
  const pos = (sorted.length - 1) * q;
  const base = Math.floor(pos);
  const rest = pos - base;
  const lo = sorted[base] ?? 0;
  const hi = sorted[base + 1] ?? lo;
  return lo + rest * (hi - lo);
}

function buildBoxes(events: readonly TrialCompleted[], focus: string | null): Box[] {
  const byModel = new Map<string, number[]>();
  for (const e of events) {
    if (e.error) continue;
    if (focus && e.model !== focus) continue;
    if (!Number.isFinite(e.ttft_ms)) continue;
    const arr = byModel.get(e.model) ?? [];
    arr.push(e.ttft_ms);
    byModel.set(e.model, arr);
  }
  const boxes: Box[] = [];
  for (const [model, raw] of byModel) {
    const s = [...raw].sort((a, b) => a - b);
    boxes.push({
      model,
      five: [
        s[0] ?? 0,
        quantile(s, 0.25),
        quantile(s, 0.5),
        quantile(s, 0.75),
        s[s.length - 1] ?? 0,
      ],
      n: s.length,
    });
  }
  return boxes.sort((a, b) => a.model.localeCompare(b.model));
}

export function LatencyChart({ events, focusModel }: LatencyChartProps): JSX.Element {
  const boxes = useMemo(() => buildBoxes(events, focusModel), [events, focusModel]);

  const option = useMemo<EChartsOption>(() => {
    const categories = boxes.map((b) => b.model);
    return {
      grid: { left: 8, right: 18, top: 16, bottom: 28, containLabel: true },
      tooltip: {
        trigger: "item",
        backgroundColor: "#1d3552",
        borderColor: "#23415f",
        textStyle: { color: "#eaf1fa" },
        formatter: (p: unknown) => {
          const param = p as { dataIndex: number };
          const b = boxes[param.dataIndex];
          if (!b) return "";
          const [mn, q1, med, q3, mx] = b.five;
          return [
            `<b>${b.model}</b> (n=${b.n})`,
            `ttft p50 ${med.toFixed(0)} ms`,
            `q1 ${q1.toFixed(0)} · q3 ${q3.toFixed(0)} ms`,
            `min ${mn.toFixed(0)} · max ${mx.toFixed(0)} ms`,
          ].join("<br/>");
        },
      },
      xAxis: {
        type: "category",
        data: categories,
        axisLabel: { color: "#7488a3", fontFamily: "var(--mono)", fontSize: 11 },
        axisLine: { lineStyle: { color: "#23415f" } },
      },
      yAxis: {
        type: "value",
        name: "ttft ms",
        nameTextStyle: { color: "#7488a3", fontSize: 11 },
        axisLabel: { color: "#7488a3", fontFamily: "var(--mono)", fontSize: 11 },
        splitLine: { lineStyle: { color: "rgba(140,165,200,0.10)" } },
      },
      series: [
        {
          type: "boxplot",
          data: boxes.map((b) => ({
            value: b.five,
            itemStyle: {
              color: `color-mix(in srgb, ${modelColor(b.model)} 22%, transparent)`,
              borderColor: modelColor(b.model),
            },
          })),
          boxWidth: ["20%", "50%"],
        },
      ],
    };
  }, [boxes]);

  if (boxes.length === 0) {
    return <div className="empty">No TTFT samples yet.</div>;
  }
  return <EChart option={option} height={300} ariaLabel="Per-model time-to-first-token distribution" />;
}
