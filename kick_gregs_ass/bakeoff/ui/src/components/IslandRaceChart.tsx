/**
 * IslandRaceChart — the hero of a model's v2 panel: both islands' champion-triad
 * trajectories on ONE shared axis so you can literally watch them diverge, climb,
 * and (after a migration) snap back to a common baseline and diverge again.
 *
 * x = iteration step (each island's own sequence of scored steps), y = triad
 * (0..1). Two lines, one per island, colored by stance. The most recent point of
 * each line carries a 95%-CI whisker (the band the current rung can resolve).
 * Migration steps — where the tournament winner became both islands' baseline —
 * are drawn as faint vertical guide lines so the lineage is legible.
 *
 * Why one shared chart (not two sparklines): the v2 story is a RACE between two
 * divergent strategies. Seeing both on the same y-scale is the whole point; two
 * separate mini-charts hide exactly the comparison that matters.
 */
import { useMemo } from "react";
import type { JSX } from "react";
import type {
  CustomSeriesRenderItem,
  CustomSeriesRenderItemAPI,
  CustomSeriesRenderItemParams,
  EChartsOption,
  LineSeriesOption,
} from "echarts";
import { EChart } from "./EChart";
import type { IslandLiveState } from "../api/useOptimizerV2Stream";

/** Per-island brand colors (match the stance chips / lane accents). */
export const ISLAND_COLORS: readonly string[] = ["#6aa9ff", "#f7a14b"];

export interface IslandRaceChartProps {
  readonly islands: readonly IslandLiveState[];
  /** Iteration-step indices (per island) at which a migration reset the baseline. */
  readonly migrationSteps?: Readonly<Record<number, readonly number[]>>;
  readonly height?: number;
}

function islandColor(islandId: number): string {
  return ISLAND_COLORS[islandId % ISLAND_COLORS.length] ?? "#6aa9ff";
}

/** renderItem for a single trailing CI whisker on the last point of a line. */
function makeWhisker(color: string): CustomSeriesRenderItem {
  return (_p: CustomSeriesRenderItemParams, api: CustomSeriesRenderItemAPI) => {
    const x = api.value(0) as number;
    const lo = api.coord([x, api.value(1) as number]);
    const hi = api.coord([x, api.value(2) as number]);
    const hx = hi[0] ?? 0;
    const hy = hi[1] ?? 0;
    const lx = lo[0] ?? 0;
    const ly = lo[1] ?? 0;
    const cap = 4;
    const style = { stroke: color, lineWidth: 1.5, opacity: 0.9 };
    return {
      type: "group" as const,
      children: [
        { type: "line" as const, shape: { x1: hx, y1: hy, x2: lx, y2: ly }, style },
        { type: "line" as const, shape: { x1: hx - cap, y1: hy, x2: hx + cap, y2: hy }, style },
        { type: "line" as const, shape: { x1: lx - cap, y1: ly, x2: lx + cap, y2: ly }, style },
      ],
    };
  };
}

export function IslandRaceChart({
  islands,
  migrationSteps,
  height = 230,
}: IslandRaceChartProps): JSX.Element {
  const hasData = islands.some((i) => i.sparkline.length > 0);

  const option = useMemo<EChartsOption>(() => {
    const lineSeries: LineSeriesOption[] = [];
    const whiskerSeries: Record<string, unknown>[] = [];

    for (const isl of islands) {
      const color = islandColor(isl.island_id);
      const pts = isl.sparkline.map((p, i) => [i, p.champion_score] as [number, number]);
      const migrations = migrationSteps?.[isl.island_id] ?? [];

      const series: LineSeriesOption = {
        name: `Island ${isl.island_id}`,
        type: "line",
        color,
        smooth: false,
        symbol: "circle",
        symbolSize: 6,
        showSymbol: pts.length < 40,
        lineStyle: { width: 2 },
        data: pts,
        emphasis: { focus: "series" },
      };
      // Faint vertical guides at migration steps for this island.
      if (migrations.length > 0) {
        series.markLine = {
          silent: true,
          symbol: "none",
          lineStyle: { color, type: "dotted", width: 1, opacity: 0.5 },
          label: { show: false },
          data: migrations.map((step) => ({ xAxis: step })),
        };
      }
      lineSeries.push(series);

      // Trailing CI whisker on the latest point only (keeps the chart readable).
      const last = isl.sparkline[isl.sparkline.length - 1];
      if (last) {
        const x = isl.sparkline.length - 1;
        whiskerSeries.push({
          name: `Island ${isl.island_id} CI`,
          type: "custom",
          renderItem: makeWhisker(color),
          encode: { x: 0, y: [1, 2] },
          data: [[x, last.champion_score - last.ci_half_width, last.champion_score + last.ci_half_width]],
          z: 4,
          silent: true,
          tooltip: { show: false },
        });
      }
    }

    return {
      backgroundColor: "transparent",
      grid: { left: 40, right: 14, top: 24, bottom: 26 },
      legend: {
        top: 0,
        right: 0,
        itemWidth: 14,
        itemHeight: 8,
        textStyle: { color: "#aebfd4", fontSize: 11 },
        data: islands.map((i) => `Island ${i.island_id}`),
      },
      tooltip: {
        trigger: "axis",
        valueFormatter: (v) => (v == null ? "—" : Number(v).toFixed(3)),
      },
      xAxis: {
        type: "value",
        name: "step",
        nameLocation: "middle",
        nameGap: 20,
        minInterval: 1,
        axisLabel: { color: "#7488a3", fontSize: 10 },
        nameTextStyle: { color: "#7488a3", fontSize: 10 },
        splitLine: { show: false },
      },
      yAxis: {
        type: "value",
        min: 0,
        max: 1,
        axisLabel: { color: "#7488a3", fontSize: 10, formatter: (v: number) => v.toFixed(1) },
        splitLine: { lineStyle: { color: "rgba(140,165,200,0.10)" } },
      },
      series: [...lineSeries, ...(whiskerSeries as LineSeriesOption[])],
    };
  }, [islands, migrationSteps]);

  if (!hasData) {
    return (
      <div className="v2-race-empty">
        <span className="v2-race-empty-dot" />
        Islands are warming up — the first scored step lights up both trajectories here.
      </div>
    );
  }

  return (
    <EChart
      option={option}
      height={height}
      ariaLabel="Both islands' champion triad trajectories on a shared axis"
    />
  );
}
