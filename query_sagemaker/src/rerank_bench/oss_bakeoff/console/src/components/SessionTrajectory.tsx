// SessionTrajectory — the endorsed 3D mapping: each model is a TRAJECTORY through
//   X = query index (the "session" sequence)
//   Y = per-query latency (ms), warm (first-call model-load spikes replaced w/ the model's warm median)
//   Z = a COMPOSITE RAGAS quality = Σ(weight·metric)/Σweight over the 5 reference-free metrics,
//       with live sliders so you can define "quality" your way and watch the surface re-form.
// Per model: scatter3D points + line3D connecting them in query order (flat = consistent, spiky = outliers).

import { useEffect, useMemo, useRef, useState } from 'react';
import * as echarts from 'echarts';
import 'echarts-gl'; // registers scatter3D / line3D / grid3D (no type decls; side-effect only).
import type { EChartsType, EChartsCoreOption } from 'echarts';
import type { LoadedData } from '../lib/useData';
import { colorFor } from '../types';
import { Card, THEME } from '../lib/ui';

const METRICS = [
  { key: 'context_precision', label: 'ctx precision' },
  { key: 'context_relevance', label: 'ctx relevance' },
  { key: 'faithfulness', label: 'faithfulness' },
  { key: 'response_relevancy', label: 'resp relevancy' },
  { key: 'response_groundedness', label: 'resp grounded' },
] as const;
type MetricKey = (typeof METRICS)[number]['key'];
const COLD_START_MS = 1500;

// [x=session, y=latency, z=quality*100, model, query]
type TrajPoint = [number, number, number, string, string];
interface Traj { model: string; color: string; pts: TrajPoint[]; meanQuality: number }

function median(values: number[]): number {
  if (!values.length) return 0;
  const s = [...values].sort((a, b) => a - b);
  return s[Math.floor(s.length / 2)];
}

// Quality-axis source: composite RAGAS (default) or ground-truth combo5 accuracy.
type ZMode = 'ragas' | 'combo5';

function buildTrajectories(data: LoadedData, weights: Record<MetricKey, number>): Traj[] {
  if (!data.ragas) return [];
  const byModelQuery: Record<string, Record<string, Record<string, number | null>>> = {};
  for (const row of data.ragas.rows) {
    (byModelQuery[row.reranker] ??= {})[row.query] = row.metrics;
  }
  const wsum = METRICS.reduce((s, m) => s + weights[m.key], 0) || 1;
  const firstModel = Object.keys(data.scored.models)[0];
  const queries = firstModel ? Object.keys(data.scored.models[firstModel].queries) : [];

  const out: Traj[] = [];
  for (const model of Object.keys(data.scored.models)) {
    const metricsByQ = byModelQuery[model] ?? {};
    const latByQ = data.scored.models[model].queries;
    const warm = Object.values(latByQ)
      .map((q) => q.latency_ms)
      .filter((x): x is number => x !== null && x > 0 && x <= COLD_START_MS);
    const warmMed = median(warm);

    const pts: TrajPoint[] = [];
    let qsum = 0;
    queries.forEach((q, i) => {
      const m = metricsByQ[q];
      if (!m) return;
      let z = 0;
      for (const mk of METRICS) {
        const v = m[mk.key];
        if (typeof v === 'number') z += weights[mk.key] * v;
      }
      z = z / wsum;
      const rawLat = latByQ[q]?.latency_ms;
      const y = rawLat === null || rawLat === undefined || rawLat > COLD_START_MS ? warmMed : rawLat;
      pts.push([i + 1, y, z * 100, model, q]);
      qsum += z;
    });
    if (pts.length) out.push({ model, color: colorFor(model), pts, meanQuality: qsum / pts.length });
  }
  return out.sort((a, b) => b.meanQuality - a.meanQuality);
}

// combo5 mode: points are built from `scored` alone (no ragas dependency).
// Z is the model's OVERALL ground-truth accuracy (×100), a flat quality plane per
// model — each trajectory rises/falls to where its accuracy actually lands.
function buildCombo5Trajectories(data: LoadedData): Traj[] {
  const combo5 = data.combo5;
  if (!combo5) return [];
  const out: Traj[] = [];
  for (const model of Object.keys(data.scored.models)) {
    const overall = combo5.aggregate[model]?.overall;
    if (!overall) continue;
    const accZ = overall.acc * 100;
    const latByQ = data.scored.models[model].queries;
    const warm = Object.values(latByQ)
      .map((q) => q.latency_ms)
      .filter((x): x is number => x !== null && x > 0 && x <= COLD_START_MS);
    const warmMed = median(warm);

    const pts: TrajPoint[] = [];
    Object.entries(latByQ).forEach(([q, score], index) => {
      const rawLat = score.latency_ms;
      const y = rawLat === null || rawLat === undefined || rawLat > COLD_START_MS ? warmMed : rawLat;
      pts.push([index + 1, y, accZ, model, q]);
    });
    if (pts.length) out.push({ model, color: colorFor(model), pts, meanQuality: overall.acc });
  }
  return out.sort((a, b) => b.meanQuality - a.meanQuality);
}

function buildOption(series: Traj[], zLabel: string, qualityLabel: string): EChartsCoreOption {
  const axisLabel = { color: THEME.dim, fontFamily: 'monospace', fontSize: 10 };
  const nameTextStyle = { color: THEME.dim, fontFamily: 'monospace', fontSize: 11 };
  const axisLine = { lineStyle: { color: THEME.border } };
  const splitLine = { lineStyle: { color: 'rgba(38,43,54,0.55)' } };

  const lines = series.map((s) => ({
    name: s.model, type: 'line3D', coordinateSystem: 'cartesian3D',
    lineStyle: { color: s.color, width: 2, opacity: 0.45 }, data: s.pts,
  }));
  const dots = series.map((s) => ({
    name: s.model, type: 'scatter3D', coordinateSystem: 'cartesian3D',
    symbolSize: 6, itemStyle: { color: s.color, opacity: 0.9 },
    emphasis: { itemStyle: { opacity: 1 } }, data: s.pts,
  }));

  return {
    backgroundColor: 'transparent',
    textStyle: { fontFamily: 'monospace', color: THEME.text },
    tooltip: {
      backgroundColor: THEME.panel, borderColor: THEME.border,
      textStyle: { color: THEME.text, fontFamily: 'monospace', fontSize: 11 },
      formatter: (p: { value?: unknown }): string => {
        const v = p.value as unknown[] | undefined;
        if (!Array.isArray(v) || v.length < 5) return '';
        return [
          `<b>${String(v[3])}</b>`,
          `session&nbsp;&nbsp;#${Number(v[0])}`,
          `latency&nbsp;&nbsp;${Number(v[1]).toFixed(0)} ms`,
          `${qualityLabel}&nbsp;&nbsp;${Number(v[2]).toFixed(1)}`,
          `<span style="color:${THEME.dim}">${String(v[4]).slice(0, 46)}</span>`,
        ].join('<br/>');
      },
    },
    legend: {
      data: series.map((s) => s.model), top: 8, type: 'scroll',
      textStyle: { color: THEME.dim, fontFamily: 'monospace', fontSize: 11 },
      inactiveColor: THEME.dimmer,
    },
    grid3D: {
      boxWidth: 110, boxDepth: 90, boxHeight: 70,
      viewControl: { projection: 'perspective', autoRotate: true, autoRotateSpeed: 6, autoRotateAfterStill: 3, distance: 230, alpha: 16, beta: 28 },
      axisPointer: { lineStyle: { color: THEME.dim } },
      environment: 'transparent',
      light: { main: { intensity: 1.1, shadow: false }, ambient: { intensity: 0.5 } },
    },
    xAxis3D: { type: 'value', name: 'query # (session)', min: 0, nameTextStyle, axisLine, axisLabel, splitLine },
    yAxis3D: { type: 'value', name: 'latency ms (warm)', min: 0, nameTextStyle, axisLine, axisLabel, splitLine },
    zAxis3D: { type: 'value', name: zLabel, nameTextStyle, axisLine, axisLabel, splitLine },
    series: [...lines, ...dots],
  };
}

export default function SessionTrajectory({ data }: { data: LoadedData }) {
  const [weights, setWeights] = useState<Record<MetricKey, number>>(
    () => Object.fromEntries(METRICS.map((m) => [m.key, 1])) as Record<MetricKey, number>,
  );
  // Default to RAGAS; the combo5 toggle only appears once combo5 data is present.
  const [mode, setMode] = useState<ZMode>('ragas');
  const hasCombo5 = data.combo5 !== null;
  const effectiveMode: ZMode = mode === 'combo5' && hasCombo5 ? 'combo5' : 'ragas';
  const containerRef = useRef<HTMLDivElement | null>(null);

  const series = useMemo(
    () => (effectiveMode === 'combo5' ? buildCombo5Trajectories(data) : buildTrajectories(data, weights)),
    [data, weights, effectiveMode],
  );

  const zLabel = effectiveMode === 'combo5' ? 'top-1 accuracy %' : 'composite quality';
  const qualityLabel = effectiveMode === 'combo5' ? 'acc %' : 'quality';

  useEffect(() => {
    const c = containerRef.current;
    if (!c || !series.length) return;
    const chart: EChartsType = echarts.init(c, undefined, { renderer: 'canvas' });
    chart.setOption(buildOption(series, zLabel, qualityLabel));
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(c);
    return () => { ro.disconnect(); chart.dispose(); };
  }, [series, zLabel, qualityLabel]);

  if (!data.ragas) {
    return (
      <Card title="3D · session trajectories" sub="composite RAGAS quality × latency × session">
        <div style={{ color: THEME.dim, fontSize: 12, fontFamily: 'monospace' }}>
          Waiting on <code>ragas_results.json</code> — the composite quality axis comes from the RAGAS metrics.
        </div>
      </Card>
    );
  }

  return (
    <Card
      title="3D · session trajectories"
      sub={
        effectiveMode === 'combo5'
          ? 'X = query (session) · Y = latency · Z = ground-truth top-1 accuracy (combo5, per-model overall).'
          : 'X = query (session) · Y = latency · Z = composite RAGAS quality. Each model is a trajectory; tune the quality weights live and watch it re-form.'
      }
    >
      {hasCombo5 && (
        <div style={{ display: 'flex', gap: 6, marginBottom: 12, alignItems: 'center' }}>
          <span style={{ fontSize: 11, color: THEME.dimmer, fontFamily: 'monospace' }}>Z axis:</span>
          {(['ragas', 'combo5'] as const).map((option) => {
            const active = effectiveMode === option;
            return (
              <button
                key={option}
                onClick={() => setMode(option)}
                style={{
                  background: active ? THEME.accentBg : THEME.panel,
                  color: active ? THEME.green : THEME.dim,
                  border: `1px solid ${active ? THEME.greenDim : THEME.border}`,
                  borderRadius: 6,
                  padding: '4px 10px',
                  fontFamily: 'monospace',
                  fontSize: 11,
                  cursor: 'pointer',
                }}
              >
                {option === 'ragas' ? 'composite RAGAS' : 'combo5 accuracy'}
              </button>
            );
          })}
        </div>
      )}
      {effectiveMode === 'ragas' && (
      <div style={{ display: 'flex', gap: 18, flexWrap: 'wrap', marginBottom: 10 }}>
        {METRICS.map((m) => (
          <label key={m.key} style={{ fontSize: 11, color: THEME.dim, fontFamily: 'monospace' }}>
            {m.label}{' '}
            <span style={{ color: THEME.text }}>×{weights[m.key].toFixed(1)}</span>
            <br />
            <input
              type="range" min={0} max={2} step={0.1} value={weights[m.key]}
              onChange={(e) => setWeights((w) => ({ ...w, [m.key]: Number(e.target.value) }))}
              style={{ width: 120, accentColor: THEME.green }}
            />
          </label>
        ))}
        <button
          onClick={() => setWeights(Object.fromEntries(METRICS.map((m) => [m.key, 1])) as Record<MetricKey, number>)}
          style={{ alignSelf: 'flex-end', background: THEME.panel, color: THEME.dim, border: `1px solid ${THEME.border}`, borderRadius: 6, padding: '4px 10px', fontFamily: 'monospace', fontSize: 11, cursor: 'pointer' }}
        >
          reset
        </button>
      </div>
      )}
      <div ref={containerRef} style={{ width: '100%', height: 540, minHeight: 380 }} role="img"
           aria-label="3D session trajectories of models by latency and quality" />
      <div style={{ marginTop: 8, fontSize: 11, color: THEME.dimmer, lineHeight: 1.55 }}>
        {effectiveMode === 'combo5' ? (
          <>
            Z = each model&rsquo;s OVERALL ground-truth top-1 accuracy (combo5) — a flat quality plane per
            model, so the trajectory sits at the accuracy it actually earned.
          </>
        ) : (
          <>
            Z = Σ(weight·metric) / Σweight over the 5 reference-free RAGAS metrics — slide to define &ldquo;quality&rdquo; your way.
          </>
        )}{' '}
        Y = per-query latency; first-call model-load spikes (&gt;{COLD_START_MS}ms) are replaced with each model&rsquo;s warm
        median (production sessions are warm). Each line connects a model&rsquo;s queries in order — flat = consistent, spiky = outliers.
      </div>
    </Card>
  );
}
