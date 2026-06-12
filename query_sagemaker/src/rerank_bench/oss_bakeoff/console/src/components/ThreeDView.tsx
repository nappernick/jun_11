// ThreeDView — echarts-gl scatter3D of the bake-off field.
//
// Each model is a CLOUD of its ~42 per-query points plus one larger CENTROID marker:
//   X = per-query latency (ms), clipped to 1500 for display (cold-start/warmup outliers).
//   Y = Opus judge win-rate for that model (quality altitude; identical for all of a model's points).
//   Z = per-query confidence = norm[ranking[0]] - norm[ranking[1]] (top vs runner-up margin).
//
// LATENCY SOURCE (deliberate): per-query latency comes from scored.json latency_ms for EVERY model
// (cohere and OSS alike). scored.json is the only per-query latency artifact in existence —
// latency_gpu.json holds aggregate p50/p90/p99 cells, not per-query values, so a 42-point cloud
// can only be drawn from scored. These raw per-query numbers INCLUDE cold-start / warmup overhead,
// so OSS clouds sit far right of the warm steady-state GPU p50 headline (~204ms for ettin); the
// clip note and caption make that explicit so a right-shifted cloud is read as "cold-start spread",
// not "this model is slow". The Leaderboard shows the warm GPU p50 headline number.

import { useEffect, useRef } from 'react';
import * as echarts from 'echarts';
import 'echarts-gl'; // side-effect: registers scatter3D / grid3D / *Axis3D (no type decls; fine).
import type { EChartsType, EChartsCoreOption } from 'echarts';
import type { LoadedData } from '../lib/useData';
import { colorFor } from '../types';
import { perQueryLatencies } from '../lib/derive';
import { Card, THEME } from '../lib/ui';

// Cold-start / warmup outliers are clipped to this ceiling for display only.
const LATENCY_CLIP_MS = 1500;

// scatter3D point payload: [x, y, z, modelName] — name rides in slot 3 for tooltips.
type Point3D = [number, number, number, string];

interface ModelSeriesData {
  model: string;
  color: string;
  quality: number; // Y altitude (judge win-rate), constant per model
  cloud: Point3D[]; // per-query points
  centroid: Point3D; // [p50 latency (clipped), quality, mean confidence]
}

function median(sortedAscending: number[]): number {
  if (sortedAscending.length === 0) {
    return 0;
  }
  const mid = Math.floor(sortedAscending.length / 2);
  if (sortedAscending.length % 2 === 1) {
    return sortedAscending[mid];
  }
  return (sortedAscending[mid - 1] + sortedAscending[mid]) / 2;
}

function mean(values: number[]): number {
  if (values.length === 0) {
    return 0;
  }
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

// Per-query confidence = squashed top score minus squashed runner-up score, keyed by query id.
function confidenceByQuery(data: LoadedData, model: string): Record<string, number> {
  const modelScored = data.scored.models[model];
  const out: Record<string, number> = {};
  if (!modelScored) {
    return out;
  }
  for (const [queryId, query] of Object.entries(modelScored.queries)) {
    if (query.ranking.length < 2) {
      continue;
    }
    const top = query.norm[query.ranking[0]];
    const second = query.norm[query.ranking[1]];
    if (top === undefined || second === undefined) {
      continue;
    }
    out[queryId] = top - second;
  }
  return out;
}

function buildSeriesData(data: LoadedData): ModelSeriesData[] {
  const series: ModelSeriesData[] = [];
  for (const model of Object.keys(data.scored.models)) {
    const quality = data.judge.model_score[model];
    if (quality === undefined) {
      continue; // no quality altitude -> cannot place this model on Y
    }
    const modelScored = data.scored.models[model];
    const confidence = confidenceByQuery(data, model);

    const cloud: Point3D[] = [];
    const latencies: number[] = [];
    const confidences: number[] = [];
    for (const [queryId, query] of Object.entries(modelScored.queries)) {
      const rawLatency = query.latency_ms;
      const conf = confidence[queryId];
      if (rawLatency === null || rawLatency <= 0 || conf === undefined) {
        continue;
      }
      const clipped = Math.min(rawLatency, LATENCY_CLIP_MS);
      cloud.push([clipped, quality, conf, model]);
      latencies.push(clipped);
      confidences.push(conf);
    }

    // Centroid rides the SAME clipped scored per-query axis as its cloud (not the warm GPU p50),
    // so the centroid is the literal center of the cloud and X means one thing everywhere.
    const sortedLatencies = [...latencies].sort((left, right) => left - right);
    const centroid: Point3D = [
      median(sortedLatencies),
      quality,
      mean(confidences),
      model,
    ];

    series.push({ model, color: colorFor(model), quality, cloud, centroid });
  }
  // Order does not affect rendering; keep models highest-quality first for a stable legend.
  return series.sort((left, right) => right.quality - left.quality);
}

// Whether any per-query latency was clipped (drives the honesty note copy).
function clipCount(data: LoadedData): { clipped: number; total: number } {
  let clipped = 0;
  let total = 0;
  for (const model of Object.keys(data.scored.models)) {
    for (const latency of perQueryLatencies(data.scored, model)) {
      total += 1;
      if (latency > LATENCY_CLIP_MS) {
        clipped += 1;
      }
    }
  }
  return { clipped, total };
}

function buildOption(series: ModelSeriesData[]): EChartsCoreOption {
  const axisLine = { lineStyle: { color: THEME.border } };
  const axisLabel = { color: THEME.dim, fontFamily: 'monospace', fontSize: 10 };
  const nameTextStyle = { color: THEME.dim, fontFamily: 'monospace', fontSize: 11 };
  const splitLine = { lineStyle: { color: 'rgba(38,43,54,0.6)' } };

  // Two series per model that share a NAME so the legend toggles cloud + centroid together:
  //   - the cloud of per-query points (small symbols)
  //   - the centroid (one large symbol)
  const cloudSeries = series.map((entry) => ({
    name: entry.model,
    type: 'scatter3D',
    coordinateSystem: 'cartesian3D',
    symbolSize: 7,
    itemStyle: {
      color: entry.color,
      opacity: 0.55,
      borderColor: 'rgba(0,0,0,0.35)',
      borderWidth: 0.5,
    },
    emphasis: { itemStyle: { opacity: 0.95 } },
    data: entry.cloud,
  }));

  const centroidSeries = series.map((entry) => ({
    name: entry.model, // same name -> legend toggle hides cloud + centroid together
    type: 'scatter3D',
    coordinateSystem: 'cartesian3D',
    symbol: 'diamond',
    symbolSize: 22,
    itemStyle: {
      color: entry.color,
      opacity: 1,
      borderColor: THEME.text,
      borderWidth: 1.5,
    },
    label: {
      show: true,
      formatter: entry.model,
      color: THEME.text,
      fontFamily: 'monospace',
      fontSize: 11,
      backgroundColor: 'rgba(15,17,21,0.7)',
      padding: [2, 4],
      borderRadius: 3,
    },
    data: [entry.centroid],
  }));

  // In-scene annotation of the ideal corner: high-Y (quality 1.0) + low-X (latency 0) = best.
  // Kept OUT of legend.data so it renders permanently and is not a toggleable model entry, and
  // styled as a ghost/label marker (not a solid diamond) so it cannot be mistaken for a centroid.
  const maxConfidence = series.reduce(
    (acc, entry) => Math.max(acc, entry.centroid[2]),
    0,
  );
  const idealCornerSeries = {
    name: '__ideal_corner__', // intentionally excluded from legend.data
    type: 'scatter3D',
    coordinateSystem: 'cartesian3D',
    silent: true,
    symbol: 'pin',
    symbolSize: 14,
    itemStyle: { color: THEME.green, opacity: 0.85 },
    label: {
      show: true,
      formatter: 'ideal · fast + accurate',
      color: THEME.green,
      fontFamily: 'monospace',
      fontSize: 11,
      backgroundColor: 'rgba(13,40,24,0.85)',
      borderColor: THEME.green,
      borderWidth: 1,
      padding: [3, 6],
      borderRadius: 4,
    },
    data: [[0, 1, maxConfidence / 2]],
  };

  return {
    backgroundColor: 'transparent',
    textStyle: { fontFamily: 'monospace', color: THEME.text },
    tooltip: {
      backgroundColor: THEME.panel,
      borderColor: THEME.border,
      textStyle: { color: THEME.text, fontFamily: 'monospace', fontSize: 11 },
      formatter: (params: {
        value?: number[] | string[];
        seriesName?: string;
      }): string => {
        const value = params.value;
        if (!Array.isArray(value) || value.length < 3) {
          return '';
        }
        const latency = Number(value[0]);
        const quality = Number(value[1]);
        const confidence = Number(value[2]);
        const model = String(value[3] ?? params.seriesName ?? '');
        const latencyLabel =
          latency >= LATENCY_CLIP_MS ? `${LATENCY_CLIP_MS}+ (clipped)` : `${latency.toFixed(0)}`;
        return [
          `<b>${model}</b>`,
          `latency&nbsp;&nbsp;${latencyLabel} ms`,
          `quality&nbsp;&nbsp;${(quality * 100).toFixed(1)}% win-rate`,
          `conf&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;${confidence.toFixed(3)} (top − 2nd)`,
        ].join('<br/>');
      },
    },
    legend: {
      data: series.map((entry) => entry.model),
      textStyle: { color: THEME.dim, fontFamily: 'monospace', fontSize: 11 },
      inactiveColor: THEME.dimmer,
      top: 8,
      type: 'scroll',
    },
    grid3D: {
      boxWidth: 100,
      boxDepth: 100,
      boxHeight: 80,
      viewControl: {
        projection: 'perspective',
        autoRotate: true,
        autoRotateSpeed: 6, // gentle
        autoRotateAfterStill: 3,
        distance: 215,
        alpha: 18,
        beta: 35,
      },
      axisPointer: { lineStyle: { color: THEME.dim } },
      environment: 'transparent',
      light: {
        main: { intensity: 1.1, shadow: false },
        ambient: { intensity: 0.45 },
      },
    },
    xAxis3D: {
      type: 'value',
      name: 'latency ms (clip 1500)',
      min: 0,
      max: LATENCY_CLIP_MS,
      nameTextStyle,
      axisLine,
      axisLabel,
      splitLine,
    },
    yAxis3D: {
      type: 'value',
      name: 'judge win-rate',
      min: 0,
      max: 1,
      nameTextStyle,
      axisLine,
      axisLabel: { ...axisLabel, formatter: (value: number) => `${(value * 100).toFixed(0)}%` },
      splitLine,
    },
    zAxis3D: {
      type: 'value',
      name: 'confidence (top − 2nd)',
      nameTextStyle,
      axisLine,
      axisLabel,
      splitLine,
    },
    series: [...cloudSeries, ...centroidSeries, idealCornerSeries],
  };
}

export default function ThreeDView({ data }: { data: LoadedData }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<EChartsType | null>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) {
      return;
    }

    const chart = echarts.init(container, undefined, { renderer: 'canvas' });
    chartRef.current = chart;

    const series = buildSeriesData(data);
    chart.setOption(buildOption(series));

    const resizeObserver = new ResizeObserver(() => chart.resize());
    resizeObserver.observe(container);

    return () => {
      resizeObserver.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, [data]);

  const { clipped, total } = clipCount(data);

  return (
    <Card
      title="3D · field map"
      sub="per-query cloud per model — latency × quality × confidence; orbiting, auto-rotate"
    >
      <div
        ref={containerRef}
        style={{ width: '100%', height: 520, minHeight: 360 }}
        role="img"
        aria-label="3D scatter of models by latency, judge win-rate, and per-query confidence"
      />
      <div style={{ marginTop: 10, fontSize: 11, color: THEME.dim, lineHeight: 1.55 }}>
        <div>
          <span style={{ color: THEME.text }}>X</span> = per-query latency (ms) ·{' '}
          <span style={{ color: THEME.text }}>Y</span> = Opus judge win-rate (quality, constant per
          model) · <span style={{ color: THEME.text }}>Z</span> = per-query confidence (norm top − 2nd).
        </div>
        <div style={{ marginTop: 4 }}>
          The ideal corner is <span style={{ color: THEME.green }}>high-Y / low-X</span> (top-left:
          accurate and fast). A model&rsquo;s diamond is its centroid; the surrounding cloud spread is
          its <span style={{ color: THEME.text }}>consistency</span> — tight = predictable,
          wide = variable across queries.
        </div>
        <div style={{ marginTop: 4, color: THEME.dimmer }}>
          Latency = raw scored.json per-query round-trips (cold-start / warmup included), clipped at{' '}
          {LATENCY_CLIP_MS} ms for display
          {total > 0 ? ` (${clipped}/${total} points clipped)` : ''}. These are not the warm
          steady-state GPU p50 numbers (ettin ≈ 204 ms) shown in the Leaderboard — a right-shifted
          cloud reflects per-query cold-start spread, not slow steady-state serving.
        </div>
      </div>
    </Card>
  );
}
