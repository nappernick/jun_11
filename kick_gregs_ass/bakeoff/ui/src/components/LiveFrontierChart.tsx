/**
 * Live moving frontier for the Bake-Off race: speed (x = live end-to-end p50,
 * lower-left is faster) vs quality (y = live mean composite, higher is better),
 * one point per candidate, recomputed from the trial buffer as it grows.
 *
 * The payoff (bake-off v2): the converse vs inline-agent variants of the same base
 * model are connected by a delta vector so the "method delta" — quality gained/lost
 * vs latency added — is visible as an arrow from converse → inline. Pairing is
 * derived purely by stripping the `-converse` / `-inline` suffix and grouping by
 * stem (lib/liveStats.groupByThinkingStem); models with no pair render as lone points.
 *
 * This mirrors the exec FrontierChart's approach (a custom-rendered points layer
 * over an ECharts cartesian grid) but reads live buffer stats instead of the
 * materialized report, and draws pair connectors instead of a Pareto polyline.
 */
import { useMemo } from "react";
import type { JSX } from "react";
import type {
  EChartsOption,
  LinesSeriesOption,
  ScatterSeriesOption,
} from "echarts";
import type { ModelLiveStats } from "../lib/liveStats";
import { groupByThinkingStem } from "../lib/liveStats";
import { EChart } from "./EChart";
import { modelColor, ms, score } from "../lib/format";

export interface LiveFrontierChartProps {
  readonly stats: ReadonlyMap<string, ModelLiveStats>;
}

interface Plotted {
  readonly model: string;
  readonly x: number;
  readonly y: number;
}

/** A scatter datum for one candidate (a structural ScatterDataItemOption). */
interface ScatterPoint {
  readonly value: [number, number];
  readonly name: string;
  readonly itemStyle: { readonly color: string; readonly borderColor: string; readonly borderWidth: number };
  readonly label: {
    readonly show: boolean;
    readonly position: "right";
    readonly formatter: string;
    readonly color: string;
    readonly fontSize: number;
  };
}

/** A model is plottable once it has at least one non-errored sample. */
function plot(s: ModelLiveStats | null): Plotted | null {
  if (!s || s.endToEndP50 == null || s.meanComposite == null) return null;
  return { model: s.model, x: s.endToEndP50, y: s.meanComposite };
}

export function LiveFrontierChart({ stats }: LiveFrontierChartProps): JSX.Element {
  const groups = useMemo(() => groupByThinkingStem(stats), [stats]);

  const hasAny = useMemo(
    () =>
      [...stats.values()].some((s) => s.endToEndP50 != null && s.meanComposite != null),
    [stats],
  );

  const option = useMemo<EChartsOption>(() => {
    // One scatter datum per plottable model, carrying its own color + label.
    const points: ScatterPoint[] = [];
    // One `lines` series per thinking pair (off → on): a directed delta vector.
    const pairLines: LinesSeriesOption[] = [];

    for (const g of groups) {
      const on = plot(g.on);
      const off = plot(g.off);
      const lone = plot(g.lone);

      for (const p of [on, off, lone]) {
        if (!p) continue;
        const variant = p.model.endsWith("-inline")
          ? " · inline"
          : p.model.endsWith("-converse")
            ? " · converse"
            : "";
        points.push({
          value: [p.x, p.y],
          name: p.model,
          itemStyle: { color: modelColor(p.model), borderColor: "#0b0f14", borderWidth: 1 },
          label: {
            show: true,
            position: "right",
            formatter: `${p.model}${variant}`,
            color: "#aebfd4",
            fontSize: 10,
          },
        });
      }

      // Connect the pair converse → inline so the arrow reads "cost of switching
      // the SAME model from the Converse API to the inline-agent path".
      if (on && off) {
        const dQuality = on.y - off.y;
        const dLatency = on.x - off.x;
        pairLines.push({
          type: "lines",
          name: `${g.stem} method delta`,
          coordinateSystem: "cartesian2d",
          data: [{ coords: [[off.x, off.y], [on.x, on.y]] }],
          symbol: ["none", "arrow"],
          symbolSize: 9,
          lineStyle: { color: modelColor(g.stem), width: 2, type: "dashed", opacity: 0.75, curveness: 0 },
          z: 2,
          silent: true,
          tooltip: {
            show: true,
            formatter: () =>
              [
                `<b>${g.stem}</b> · method delta`,
                `quality ${dQuality >= 0 ? "+" : ""}${score(dQuality)}`,
                `latency ${dLatency >= 0 ? "+" : ""}${ms(Math.abs(dLatency))}`,
              ].join("<br/>"),
          },
        });
      }
    }

    const scatter: ScatterSeriesOption = {
      type: "scatter",
      name: "candidates",
      data: points,
      symbolSize: 16,
      z: 5,
      emphasis: { focus: "series" },
      tooltip: {
        formatter: (p: unknown) => {
          const param = p as { data: { name?: string; value: [number, number] } };
          const name = param.data.name ?? "";
          const [x, y] = param.data.value;
          return `<b>${name}</b><br/>e2e p50 ${ms(x)}<br/>composite ${score(y)}`;
        },
      },
    };

    return {
      backgroundColor: "transparent",
      grid: { left: 56, right: 120, top: 24, bottom: 48 },
      tooltip: { trigger: "item", confine: true },
      xAxis: {
        type: "value",
        name: "end-to-end p50 (ms) — faster ←",
        nameLocation: "middle",
        nameGap: 30,
        axisLabel: { color: "#7488a3", fontFamily: "var(--mono)", fontSize: 11 },
        nameTextStyle: { color: "#7488a3" },
        splitLine: { lineStyle: { color: "rgba(140,165,200,0.10)" } },
      },
      yAxis: {
        type: "value",
        name: "live mean composite — higher is better",
        nameLocation: "middle",
        nameGap: 40,
        min: 0,
        max: 1,
        axisLabel: { color: "#7488a3", fontFamily: "var(--mono)", fontSize: 11 },
        nameTextStyle: { color: "#7488a3" },
        splitLine: { lineStyle: { color: "rgba(140,165,200,0.10)" } },
      },
      series: [...pairLines, scatter],
    };
  }, [groups]);

  if (!hasAny) {
    return <div className="empty">Frontier lights up once the first trials land.</div>;
  }
  return (
    <EChart
      option={option}
      height={360}
      ariaLabel="Live speed versus quality frontier with converse-to-inline method-delta vectors"
    />
  );
}
