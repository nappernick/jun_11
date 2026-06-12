/**
 * Hero Speed/Quality Frontier (Req 11.2).
 *
 * x = end-to-end latency (p50, lower is better) with a p90 whisker so a
 * fast-on-average-but-spiky model reads as a wide horizontal whisker;
 * y = quality (the recomputed composite) with a vertical CI band. Each model is
 * one point carrying a 2-D uncertainty cross (speed whisker × quality band). The
 * Pareto front is highlighted; dominated models are de-emphasized. When two
 * models' quality CIs overlap they are shown as *not yet distinguished* (Req
 * 11.1) — the legend flags the tie rather than implying a confident ranking.
 */
import { useMemo } from "react";
import type { JSX } from "react";
import type { EChartsOption } from "echarts";
import { EChart } from "../components/EChart";
import { modelColor, ms, score } from "../lib/format";
import { ciOverlap, type WeightedQuality } from "./quality";

export interface FrontierDatum {
  readonly model: string;
  readonly quality: WeightedQuality;
  readonly speedP50: number;
  readonly speedP90: number;
  readonly onParetoFront: boolean;
}

export interface FrontierChartProps {
  readonly data: readonly FrontierDatum[];
}

/** Pareto frontier polyline (lower latency + higher quality), left→right. */
function paretoLine(data: readonly FrontierDatum[]): [number, number][] {
  return data
    .filter((d) => d.onParetoFront && !d.quality.insufficient)
    .map((d) => [d.speedP50, d.quality.composite] as [number, number])
    .sort((a, b) => a[0] - b[0]);
}

/** Pairs of models whose quality CIs overlap (not-yet-distinguished, Req 11.1). */
function tiedPairs(data: readonly FrontierDatum[]): string[] {
  const tied: string[] = [];
  const usable = data.filter((d) => !d.quality.insufficient);
  for (let i = 0; i < usable.length; i++) {
    for (let j = i + 1; j < usable.length; j++) {
      const a = usable[i];
      const b = usable[j];
      if (a && b && ciOverlap(a.quality, b.quality)) {
        tied.push(`${a.model} ≈ ${b.model}`);
      }
    }
  }
  return tied;
}

export function FrontierChart({ data }: FrontierChartProps): JSX.Element {
  const ties = useMemo(() => tiedPairs(data), [data]);

  const option = useMemo<EChartsOption>(() => {
    const front = paretoLine(data);

    const pointSeries = data.map((d) => {
      const color = modelColor(d.model);
      const dominated = !d.onParetoFront;
      const q = d.quality;
      // custom renderItem draws the 2-D uncertainty cross + the point. ECharts'
      // CustomSeriesRenderItemReturn is impractical to satisfy with inline
      // graphic literals, so the return is typed `any` (the documented escape
      // hatch); the series array is cast to the option's series type below.
      const renderItem = (_params: unknown, api: any): any => {
        const cx = api.coord([api.value(1), api.value(2)]);
        const lo = api.coord([api.value(1), api.value(3)]);
        const hi = api.coord([api.value(1), api.value(4)]);
        const p90 = api.coord([api.value(5), api.value(2)]);
        const op = dominated ? 0.32 : 1;
        const r = dominated ? 5 : 8;
        return {
          type: "group",
          children: [
            // vertical quality CI band
            {
              type: "line",
              shape: { x1: cx[0], y1: lo[1], x2: cx[0], y2: hi[1] },
              style: { stroke: color, lineWidth: 2, opacity: op * 0.8 },
            },
            // horizontal speed p90 whisker (p50 -> p90)
            {
              type: "line",
              shape: { x1: cx[0], y1: cx[1], x2: p90[0], y2: cx[1] },
              style: { stroke: color, lineWidth: 2, opacity: op * 0.5, lineDash: [4, 3] },
            },
            // the model point
            {
              type: "circle",
              shape: { cx: cx[0], cy: cx[1], r },
              style: { fill: color, opacity: op, stroke: "#0b0f14", lineWidth: dominated ? 1 : 2 },
            },
          ],
        };
      };
      return {
        type: "custom" as const,
        name: d.model,
        // [x, p50, qPoint, qLow, qHigh, p90]
        data: [[d.speedP50, d.speedP50, q.composite, q.low, q.high, d.speedP90]],
        z: dominated ? 2 : 5,
        renderItem,
        tooltip: {
          formatter: () =>
            `<b>${d.model}</b>${d.onParetoFront ? " · Pareto" : " · dominated"}<br/>` +
            `quality ${score(q.composite)} [${score(q.low)}, ${score(q.high)}]` +
            (q.insufficient ? " (insufficient data)" : "") +
            `<br/>speed p50 ${ms(d.speedP50)} · p90 ${ms(d.speedP90)}`,
        },
      };
    });

    const frontSeries = front.length
      ? [
          {
            type: "line" as const,
            name: "Pareto front",
            data: front,
            showSymbol: false,
            lineStyle: { color: "#9aa7b4", width: 1, type: "dashed" as const },
            z: 1,
            silent: true,
          },
        ]
      : [];

    return {
      backgroundColor: "transparent",
      grid: { left: 56, right: 24, top: 24, bottom: 48 },
      tooltip: { trigger: "item", confine: true },
      xAxis: {
        type: "value",
        name: "latency p50 (ms) — lower is better",
        nameLocation: "middle",
        nameGap: 30,
        axisLabel: { color: "#9aa7b4" },
        nameTextStyle: { color: "#9aa7b4" },
        splitLine: { lineStyle: { color: "rgba(255,255,255,0.06)" } },
      },
      yAxis: {
        type: "value",
        name: "quality — higher is better",
        nameLocation: "middle",
        nameGap: 40,
        min: 0,
        max: 1,
        axisLabel: { color: "#9aa7b4" },
        nameTextStyle: { color: "#9aa7b4" },
        splitLine: { lineStyle: { color: "rgba(255,255,255,0.06)" } },
      },
      series: [...frontSeries, ...pointSeries],
    } as EChartsOption;
  }, [data]);

  return (
    <div>
      <EChart option={option} height={360} ariaLabel="Speed versus quality frontier" />
      {ties.length > 0 && (
        <div className="tie-note" role="note">
          <b>Not yet distinguished</b> (quality CIs overlap): {ties.join(" · ")}. Decide these on
          speed, or run more items to separate them.
        </div>
      )}
    </div>
  );
}
