// ACCURACY — the discriminating verdict section (ground-truth top-1 accuracy on a
// stratified pool-of-5 eval). Reads combo5_results.json. Lead section, above judge/RAGAS.
//
//   a. DIFFICULTY CURVE — per-model acc at random→mixed→hard with a CI band + 0.20 floor.
//   b. ACCURACY × LATENCY FRONTIER — scatter (x = overall p50 latency [log], y = hard acc),
//      y-error from ci, ideal corner top-left, best-OSS-by-hard-acc badged ★.
//   c. FOREST / SIGNIFICANCE — overall acc ± CI horizontal bars, sorted; non-overlapping CIs = real gap.
//   d. BY-QUERY-TYPE heatmap — model × 4 type values, cell = accuracy from records.
//
// Every panel degrades to "accuracy run in progress…" when combo5 is null.

import { useMemo } from 'react';
import type { EChartsOption } from 'echarts';
import type { CustomSeriesRenderItemAPI, CustomSeriesRenderItemParams } from 'echarts';
import type { LoadedData } from '../lib/useData';
import type { Combo5, Combo5Tier } from '../types';
import { Card, Badge, THEME } from '../lib/ui';
import { MODEL_META, colorFor } from '../types';
import {
  COMBO5_TYPES,
  modelsByHardAcc,
  recommendedOssByHardAcc,
  accuracyByType,
} from '../lib/derive';
import EChart from '../lib/EChart';

const TIERS: Combo5Tier[] = ['random', 'mixed', 'hard'];
const TIER_LABELS = ['random', 'mixed', 'hard'];

// Shared dark-axis styling helpers.
const axisLabelStyle = { color: THEME.dim, fontSize: 9, fontFamily: 'monospace' } as const;
const splitLineStyle = { lineStyle: { color: THEME.panelAlt } } as const;
const axisLineStyle = { lineStyle: { color: THEME.border } } as const;

function InProgress({ title, sub }: { title: string; sub: string }) {
  return (
    <Card title={title} sub={sub}>
      <div
        style={{
          padding: '36px 16px',
          textAlign: 'center',
          color: THEME.amber,
          fontSize: 13,
          border: `1px dashed ${THEME.border}`,
          borderRadius: 6,
          background: THEME.panelAlt,
        }}
      >
        <div style={{ marginBottom: 6 }}>accuracy run in progress…</div>
        <div style={{ fontSize: 11, color: THEME.dim }}>
          combo5_results.json not yet present — this panel populates once the
          ground-truth accuracy run finishes (auto-refreshes every 20s).
        </div>
      </div>
    </Card>
  );
}

// ── a. DIFFICULTY CURVE ────────────────────────────────────────────────────
function DifficultyCurve({ combo5 }: { combo5: Combo5 }) {
  const models = useMemo(() => modelsByHardAcc(combo5), [combo5]);
  const floor = combo5.meta.random_floor;

  const option = useMemo<EChartsOption>(() => {
    // Per model build three stacked series so the CI band fills lo→hi:
    //   1. transparent lower-bound (ci[0]) carrier
    //   2. semi-transparent (ci[1]-ci[0]) band, stacked on the carrier
    //   3. the acc line itself (own stack, drawn over the band)
    // Each model gets a UNIQUE stack id or the bands would sum together.
    const series: NonNullable<EChartsOption['series']> = [];
    for (const model of models) {
      const agg = combo5.aggregate[model];
      const lo = TIERS.map((tier) => agg?.[tier]?.ci?.[0] ?? null);
      const band = TIERS.map((tier) => {
        const cell = agg?.[tier];
        return cell ? cell.ci[1] - cell.ci[0] : null;
      });
      const acc = TIERS.map((tier) => agg?.[tier]?.acc ?? null);
      const hue = colorFor(model);

      series.push({
        name: model,
        type: 'line',
        stack: `band-${model}`,
        symbol: 'none',
        lineStyle: { opacity: 0 },
        areaStyle: { opacity: 0 },
        data: lo,
        silent: true,
        tooltip: { show: false },
        legendHoverLink: false,
        z: 1,
      });
      series.push({
        name: model,
        type: 'line',
        stack: `band-${model}`,
        symbol: 'none',
        lineStyle: { opacity: 0 },
        areaStyle: { color: hue, opacity: 0.1 },
        data: band,
        silent: true,
        tooltip: { show: false },
        legendHoverLink: false,
        z: 1,
      });
      series.push({
        name: model,
        type: 'line',
        symbol: 'circle',
        symbolSize: 7,
        lineStyle: { color: hue, width: 2 },
        itemStyle: { color: hue },
        emphasis: { focus: 'series' },
        data: acc,
        z: 3,
        markLine:
          model === models[0]
            ? {
                silent: true,
                symbol: 'none',
                lineStyle: { color: THEME.dimmer, type: 'dashed', width: 1 },
                label: {
                  formatter: `random floor ${floor.toFixed(2)}`,
                  color: THEME.dimmer,
                  fontSize: 9,
                  fontFamily: 'monospace',
                  position: 'insideEndTop',
                },
                data: [{ yAxis: floor }],
              }
            : undefined,
      });
    }

    return {
      backgroundColor: 'transparent',
      grid: { left: 44, right: 16, top: 40, bottom: 30 },
      legend: {
        type: 'scroll',
        top: 4,
        // Only the acc lines (3rd series per model) carry the legend; band carriers reuse the name.
        data: models,
        textStyle: { color: THEME.dim, fontSize: 10, fontFamily: 'monospace' },
        pageTextStyle: { color: THEME.dim },
        inactiveColor: THEME.dimmer,
      },
      tooltip: {
        trigger: 'axis',
        backgroundColor: THEME.panel,
        borderColor: THEME.border,
        textStyle: { color: THEME.text, fontSize: 11, fontFamily: 'monospace' },
        valueFormatter: (value: unknown) =>
          typeof value === 'number' ? value.toFixed(3) : '—',
      },
      xAxis: {
        type: 'category',
        data: TIER_LABELS,
        boundaryGap: false,
        axisLabel: { ...axisLabelStyle, fontSize: 10 },
        axisLine: axisLineStyle,
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value',
        min: 0,
        max: 1,
        name: 'top-1 acc',
        nameTextStyle: { color: THEME.dimmer, fontSize: 9 },
        axisLabel: axisLabelStyle,
        splitLine: splitLineStyle,
        axisLine: axisLineStyle,
      },
      series,
    };
  }, [combo5, models, floor]);

  return (
    <Card
      title="Difficulty curve"
      sub="Top-1 accuracy as the pool gets harder (random → mixed → hard). Band = CI. Dashed = random floor."
    >
      <EChart option={option} style={{ height: 360 }} />
    </Card>
  );
}

// ── b. ACCURACY × LATENCY FRONTIER ─────────────────────────────────────────
interface FrontierPoint {
  model: string;
  latency: number;
  hardAcc: number;
  lo: number;
  hi: number;
  color: string;
  isBestOss: boolean;
}

function Frontier({ combo5 }: { combo5: Combo5 }) {
  const bestOss = useMemo(() => recommendedOssByHardAcc(combo5), [combo5]);

  const points = useMemo<FrontierPoint[]>(() => {
    const out: FrontierPoint[] = [];
    for (const model of combo5.meta.models) {
      const overall = combo5.aggregate[model]?.overall;
      const hard = combo5.aggregate[model]?.hard;
      const latency = overall?.p50_latency_ms;
      // Log axis cannot take null / non-positive latency — drop those points.
      if (latency === null || latency === undefined || latency <= 0 || !hard) {
        continue;
      }
      out.push({
        model,
        latency,
        hardAcc: hard.acc,
        lo: hard.ci[0],
        hi: hard.ci[1],
        color: colorFor(model),
        isBestOss: model === bestOss,
      });
    }
    return out;
  }, [combo5, bestOss]);

  const option = useMemo<EChartsOption>(() => {
    // echarts has no native error-bar series — render y-error from ci with a custom series.
    const errorData = points.map((point) => [point.latency, point.lo, point.hi, point.color]);
    const scatterData = points.map((point) => ({
      value: [point.latency, point.hardAcc],
      itemStyle: { color: point.color },
      symbol: point.isBestOss ? 'diamond' : 'circle',
      symbolSize: point.isBestOss ? 16 : 11,
    }));

    return {
      backgroundColor: 'transparent',
      grid: { left: 48, right: 18, top: 18, bottom: 44 },
      tooltip: {
        trigger: 'item',
        backgroundColor: THEME.panel,
        borderColor: THEME.border,
        textStyle: { color: THEME.text, fontSize: 11, fontFamily: 'monospace' },
        formatter: (param: unknown) => {
          const typed = param as { dataIndex: number; seriesType?: string };
          const point = points[typed.dataIndex];
          if (!point) {
            return '';
          }
          return [
            `<b>${point.model}</b>`,
            `hard acc&nbsp;&nbsp;${point.hardAcc.toFixed(3)} <span style="color:${THEME.dimmer}">[${point.lo.toFixed(2)}–${point.hi.toFixed(2)}]</span>`,
            `p50 latency&nbsp;&nbsp;${Math.round(point.latency)} ms`,
          ].join('<br/>');
        },
      },
      xAxis: {
        type: 'log',
        name: 'p50 latency ms (log) · overall',
        nameLocation: 'middle',
        nameGap: 28,
        nameTextStyle: { color: THEME.dimmer, fontSize: 10 },
        axisLabel: { ...axisLabelStyle, formatter: (value: number) => `${value}` },
        splitLine: splitLineStyle,
        axisLine: axisLineStyle,
      },
      yAxis: {
        type: 'value',
        min: 0,
        max: 1,
        name: 'hard-tier acc',
        nameTextStyle: { color: THEME.dimmer, fontSize: 9 },
        axisLabel: axisLabelStyle,
        splitLine: splitLineStyle,
        axisLine: axisLineStyle,
      },
      series: [
        {
          // y-error bars (custom): a vertical whisker spanning ci[lo,hi] per point.
          type: 'custom',
          silent: true,
          data: errorData,
          renderItem: (
            _params: CustomSeriesRenderItemParams,
            api: CustomSeriesRenderItemAPI,
          ) => {
            const xValue = api.value(0);
            const loValue = api.value(1);
            const hiValue = api.value(2);
            const color = api.value(3) as unknown as string;
            const top = api.coord([xValue, hiValue]);
            const bottom = api.coord([xValue, loValue]);
            const capWidth = 4;
            const lineStyle = { stroke: color, lineWidth: 1, opacity: 0.7 };
            return {
              type: 'group',
              children: [
                {
                  type: 'line',
                  shape: { x1: top[0], y1: top[1], x2: bottom[0], y2: bottom[1] },
                  style: lineStyle,
                },
                {
                  type: 'line',
                  shape: { x1: top[0] - capWidth, y1: top[1], x2: top[0] + capWidth, y2: top[1] },
                  style: lineStyle,
                },
                {
                  type: 'line',
                  shape: { x1: bottom[0] - capWidth, y1: bottom[1], x2: bottom[0] + capWidth, y2: bottom[1] },
                  style: lineStyle,
                },
              ],
            };
          },
          z: 1,
        },
        {
          type: 'scatter',
          data: scatterData,
          z: 3,
          label: {
            show: true,
            position: 'right',
            color: THEME.dim,
            fontSize: 9,
            fontFamily: 'monospace',
            formatter: (param: unknown) => {
              const typed = param as { dataIndex: number };
              return points[typed.dataIndex]?.model ?? '';
            },
          },
          markPoint: {
            silent: true,
            symbol: 'pin',
            symbolSize: 0,
            label: {
              show: true,
              formatter: '◤ ideal',
              color: THEME.green,
              fontSize: 10,
              fontFamily: 'monospace',
            },
            data: [
              {
                name: 'ideal',
                coord: [
                  points.length ? Math.min(...points.map((point) => point.latency)) : 1,
                  0.98,
                ],
              },
            ],
          },
        },
      ],
    };
  }, [points]);

  return (
    <Card
      title="Accuracy × latency frontier"
      sub="x = overall p50 latency (log) · y = hard-tier acc · whisker = CI. Top-left is ideal."
      right={bestOss ? <Badge kind="pick">★ {bestOss}</Badge> : undefined}
    >
      <EChart option={option} style={{ height: 360 }} />
    </Card>
  );
}

// ── c. FOREST / SIGNIFICANCE PLOT ──────────────────────────────────────────
function ForestPlot({ combo5 }: { combo5: Combo5 }) {
  // Sort by overall acc, best at top. Reverse for echarts (category axis draws bottom-up).
  const ranked = useMemo(() => {
    const withOverall = combo5.meta.models
      .map((model) => ({ model, cell: combo5.aggregate[model]?.overall }))
      .filter((entry): entry is { model: string; cell: NonNullable<typeof entry.cell> } => Boolean(entry.cell))
      .sort((left, right) => left.cell.acc - right.cell.acc); // ascending so best ends at top
    return withOverall;
  }, [combo5]);

  const option = useMemo<EChartsOption>(() => {
    const labels = ranked.map((entry) => entry.model);
    // Bars draw the point estimate; a custom series draws the horizontal CI whisker.
    const accData = ranked.map((entry) => ({
      value: entry.cell.acc,
      itemStyle: { color: colorFor(entry.model) },
    }));
    const errorData = ranked.map((entry, index) => [
      index,
      entry.cell.ci[0],
      entry.cell.ci[1],
      colorFor(entry.model),
    ]);

    return {
      backgroundColor: 'transparent',
      grid: { left: 110, right: 28, top: 16, bottom: 36 },
      tooltip: {
        trigger: 'item',
        backgroundColor: THEME.panel,
        borderColor: THEME.border,
        textStyle: { color: THEME.text, fontSize: 11, fontFamily: 'monospace' },
        formatter: (param: unknown) => {
          const typed = param as { dataIndex: number };
          const entry = ranked[typed.dataIndex];
          if (!entry) {
            return '';
          }
          return [
            `<b>${entry.model}</b>`,
            `overall acc&nbsp;&nbsp;${entry.cell.acc.toFixed(3)}`,
            `CI&nbsp;&nbsp;[${entry.cell.ci[0].toFixed(3)} – ${entry.cell.ci[1].toFixed(3)}]`,
            `n=${entry.cell.n} · mrr ${entry.cell.mrr.toFixed(3)}`,
          ].join('<br/>');
        },
      },
      xAxis: {
        type: 'value',
        min: 0,
        max: 1,
        name: 'overall top-1 acc',
        nameLocation: 'middle',
        nameGap: 24,
        nameTextStyle: { color: THEME.dimmer, fontSize: 10 },
        axisLabel: axisLabelStyle,
        splitLine: splitLineStyle,
        axisLine: axisLineStyle,
      },
      yAxis: {
        type: 'category',
        data: labels,
        axisLabel: { ...axisLabelStyle, fontSize: 10 },
        axisLine: axisLineStyle,
        axisTick: { show: false },
      },
      series: [
        {
          type: 'bar',
          data: accData,
          barWidth: '46%',
          itemStyle: { opacity: 0.85 },
          z: 1,
          markLine: {
            silent: true,
            symbol: 'none',
            lineStyle: { color: THEME.dimmer, type: 'dashed', width: 1 },
            label: {
              formatter: `floor ${combo5.meta.random_floor.toFixed(2)}`,
              color: THEME.dimmer,
              fontSize: 9,
              fontFamily: 'monospace',
              position: 'end',
            },
            data: [{ xAxis: combo5.meta.random_floor }],
          },
        },
        {
          // Horizontal CI whiskers, drawn over the bars.
          type: 'custom',
          silent: true,
          data: errorData,
          renderItem: (
            _params: CustomSeriesRenderItemParams,
            api: CustomSeriesRenderItemAPI,
          ) => {
            const categoryIndex = api.value(0);
            const loValue = api.value(1);
            const hiValue = api.value(2);
            const color = api.value(3) as unknown as string;
            const left = api.coord([loValue, categoryIndex]);
            const right = api.coord([hiValue, categoryIndex]);
            const capHeight = 4;
            const lineStyle = { stroke: THEME.text, lineWidth: 1.2, opacity: 0.9 };
            const capStyle = { stroke: color, lineWidth: 1.2, opacity: 0.9 };
            return {
              type: 'group',
              children: [
                {
                  type: 'line',
                  shape: { x1: left[0], y1: left[1], x2: right[0], y2: right[1] },
                  style: lineStyle,
                },
                {
                  type: 'line',
                  shape: { x1: left[0], y1: left[1] - capHeight, x2: left[0], y2: left[1] + capHeight },
                  style: capStyle,
                },
                {
                  type: 'line',
                  shape: { x1: right[0], y1: right[1] - capHeight, x2: right[0], y2: right[1] + capHeight },
                  style: capStyle,
                },
              ],
            };
          },
          z: 3,
        },
      ],
    };
  }, [ranked, combo5.meta.random_floor]);

  return (
    <Card
      title="Forest / significance"
      sub="Overall top-1 acc ± CI per model, sorted. Non-overlapping CIs = a statistically real gap."
    >
      <EChart option={option} style={{ height: 340 }} />
      <div style={{ fontSize: 10, color: THEME.dimmer, marginTop: 8, lineHeight: 1.6 }}>
        Where two models&rsquo; CI whiskers do not overlap, the difference in top-1
        accuracy is statistically real — not eval noise.
      </div>
    </Card>
  );
}

// ── d. BY-QUERY-TYPE HEATMAP ───────────────────────────────────────────────
function ByTypeHeatmap({ combo5 }: { combo5: Combo5 }) {
  const models = useMemo(() => modelsByHardAcc(combo5), [combo5]);

  const option = useMemo<EChartsOption>(() => {
    // cell = mean(correct) over records matching (model, type). [colIndex, rowIndex, acc].
    const cells: { value: [number, number, number] }[] = [];
    models.forEach((model, rowIndex) => {
      COMBO5_TYPES.forEach((typeEntry, colIndex) => {
        const acc = accuracyByType(combo5, model, typeEntry.key);
        if (acc === null) {
          return;
        }
        cells.push({ value: [colIndex, rowIndex, acc] });
      });
    });

    return {
      backgroundColor: 'transparent',
      grid: { left: 120, right: 20, top: 18, bottom: 70 },
      tooltip: {
        backgroundColor: THEME.panel,
        borderColor: THEME.border,
        textStyle: { color: THEME.text, fontSize: 11, fontFamily: 'monospace' },
        formatter: (param: unknown) => {
          const typed = param as { data?: { value: [number, number, number] } };
          const datum = typed.data;
          if (!datum) {
            return '';
          }
          const [colIndex, rowIndex, acc] = datum.value;
          return [
            `<b>${models[rowIndex]}</b>`,
            `${COMBO5_TYPES[colIndex].label}`,
            `acc ${acc.toFixed(3)}`,
          ].join('<br/>');
        },
      },
      xAxis: {
        type: 'category',
        data: COMBO5_TYPES.map((entry) => entry.label),
        splitArea: { show: true },
        axisLabel: { ...axisLabelStyle, rotate: 30 },
        axisLine: axisLineStyle,
        axisTick: { show: false },
      },
      yAxis: {
        type: 'category',
        data: models,
        inverse: true,
        splitArea: { show: true },
        axisLabel: { ...axisLabelStyle, fontSize: 10 },
        axisLine: axisLineStyle,
        axisTick: { show: false },
      },
      visualMap: {
        min: 0,
        max: 1,
        calculable: false,
        orient: 'horizontal',
        left: 'center',
        bottom: 4,
        itemWidth: 12,
        itemHeight: 110,
        text: ['1.0', '0'],
        textStyle: { color: THEME.dim, fontSize: 9 },
        inRange: { color: ['#161a22', '#15351f', '#1f6f33', '#3fb950'] },
      },
      series: [
        {
          type: 'heatmap',
          data: cells,
          label: {
            show: true,
            color: THEME.text,
            fontSize: 9.5,
            fontFamily: 'monospace',
            formatter: (param: unknown) => {
              const typed = param as { value: [number, number, number] };
              return typed.value[2].toFixed(2);
            },
          },
          itemStyle: { borderColor: THEME.bg, borderWidth: 2 },
          emphasis: { itemStyle: { borderColor: THEME.text, borderWidth: 1 } },
        },
      ],
    };
  }, [combo5, models]);

  return (
    <Card
      title="Accuracy by query type"
      sub="Ground-truth top-1 acc per (model × query type), computed from per-record outcomes."
    >
      <EChart option={option} style={{ height: 380 }} />
    </Card>
  );
}

// ── Section composition ────────────────────────────────────────────────────
export default function AccuracySection({ data }: { data: LoadedData }) {
  const combo5 = data.combo5;

  if (!combo5) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        <SectionBanner combo5={null} />
        <div style={gridTwoCol}>
          <InProgress title="Difficulty curve" sub="random → mixed → hard top-1 accuracy" />
          <InProgress title="Accuracy × latency frontier" sub="hard-tier acc vs overall p50 latency" />
        </div>
        <InProgress title="Forest / significance" sub="overall acc ± CI per model" />
        <InProgress title="Accuracy by query type" sub="model × query-type accuracy heatmap" />
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <SectionBanner combo5={combo5} />
      <div style={gridTwoCol}>
        <DifficultyCurve combo5={combo5} />
        <Frontier combo5={combo5} />
      </div>
      <ForestPlot combo5={combo5} />
      <ByTypeHeatmap combo5={combo5} />
    </div>
  );
}

const gridTwoCol: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'repeat(auto-fit, minmax(420px, 1fr))',
  gap: 14,
};

// A short headline banner naming the verdict basis: ground-truth top-1 accuracy.
function SectionBanner({ combo5 }: { combo5: Combo5 | null }) {
  const bestOss = combo5 ? recommendedOssByHardAcc(combo5) : null;
  const bestOssHard =
    bestOss && combo5 ? combo5.aggregate[bestOss]?.hard?.acc ?? null : null;
  const ossLabel = bestOss && MODEL_META[bestOss] ? `${bestOss} (${MODEL_META[bestOss].params})` : bestOss;

  return (
    <div
      style={{
        border: `1px solid ${THEME.border}`,
        borderLeft: `3px solid ${THEME.green}`,
        borderRadius: 8,
        background: THEME.accentBg,
        padding: '12px 16px',
      }}
    >
      <div
        style={{
          fontSize: 12,
          letterSpacing: '0.08em',
          textTransform: 'uppercase',
          color: THEME.text,
          marginBottom: 4,
        }}
      >
        Ground-truth accuracy — the discriminating verdict
      </div>
      <div style={{ fontSize: 12, color: THEME.dim, lineHeight: 1.6 }}>
        {combo5 ? (
          <>
            Did the reranker rank the known-correct FAQ #1 over a stratified pool-of-5
            (n={combo5.meta.n_instances}, random floor {combo5.meta.random_floor.toFixed(2)} = chance).
            {bestOss && bestOssHard !== null ? (
              <>
                {' '}Recommended OSS by hardest-tier accuracy:{' '}
                <span style={{ color: THEME.green }}>★ {ossLabel}</span> at{' '}
                {(bestOssHard * 100).toFixed(1)}% on the hard pool.
              </>
            ) : null}
          </>
        ) : (
          <>
            Ground-truth top-1 accuracy over a stratified pool-of-5 (did the reranker rank the
            known-correct FAQ #1). Awaiting combo5_results.json — auto-refreshes every 20s.
          </>
        )}
      </div>
    </div>
  );
}
