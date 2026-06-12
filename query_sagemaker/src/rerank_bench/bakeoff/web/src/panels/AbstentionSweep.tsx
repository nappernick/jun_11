import type { SliceMetrics, Gates, ModelMeta } from '../types';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  ReferenceLine, ResponsiveContainer,
} from 'recharts';
import { useMemo } from 'react';

export interface AbstentionSweepProps {
  sliceMetrics: { modelId: string; metrics: SliceMetrics }[];
  gates: Gates;
  models: ModelMeta[];
}

const COLORS = ['#58a6ff', '#3fb950', '#d29922', '#f778ba', '#79c0ff', '#a5d6ff'];

export default function AbstentionSweep({ sliceMetrics, gates, models }: AbstentionSweepProps) {
  const modelLookup = useMemo(() => new Map(models.map(m => [m.id, m])), [models]);

  // Merge all curves into a unified dataset keyed by t
  const { mergedData, modelKeys } = useMemo(() => {
    const tMap = new Map<number, Record<string, number>>();
    const keys: { id: string; meta: ModelMeta }[] = [];

    for (const { modelId, metrics } of sliceMetrics) {
      const meta = modelLookup.get(modelId);
      if (!meta) continue;
      keys.push({ id: modelId, meta });
      for (const pt of metrics.abstain_curve) {
        const row = tMap.get(pt.t) ?? { t: pt.t };
        row[`${modelId}_far`] = pt.false_answer_rate;
        row[`${modelId}_recall`] = pt.abstain_recall;
        tMap.set(pt.t, row);
      }
    }

    const sorted = [...tMap.values()].sort((a, b) => a.t - b.t);
    return { mergedData: sorted, modelKeys: keys };
  }, [sliceMetrics, modelLookup]);

  const operatingPoints = useMemo(() =>
    sliceMetrics.map(({ modelId, metrics }) => ({
      modelId,
      t: metrics.abstain.operating_t,
    })),
  [sliceMetrics]);

  return (
    <div className="panel">
      <div className="panel-title">Abstention Sweep — Threshold vs. FAR &amp; Recall</div>
      <div style={{ flex: 1, minHeight: 280 }}>
        <ResponsiveContainer width="100%" height={280}>
          <LineChart data={mergedData} margin={{ top: 8, right: 12, bottom: 24, left: 4 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--grid)" />
            <XAxis
              dataKey="t"
              type="number"
              domain={['dataMin', 'dataMax']}
              label={{ value: 'Threshold (t)', position: 'insideBottom', offset: -12, fill: 'var(--text-muted)', fontSize: 11 }}
              tick={{ fill: 'var(--text-muted)', fontSize: 10 }}
              stroke="var(--border)"
            />
            <YAxis
              domain={[0, 1]}
              tick={{ fill: 'var(--text-muted)', fontSize: 10 }}
              stroke="var(--border)"
              label={{ value: 'Rate', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 11 }}
            />
            <Tooltip
              contentStyle={{ background: 'var(--bg-1)', border: '1px solid var(--border)', fontSize: 11, fontFamily: 'var(--font-mono)' }}
              labelStyle={{ color: 'var(--text-muted)' }}
              labelFormatter={(v) => `t = ${Number(v).toFixed(3)}`}
            />
            {/* Gates ceiling */}
            <ReferenceLine
              y={gates.false_answer_ceiling}
              stroke="var(--red)"
              strokeDasharray="6 3"
              strokeWidth={2}
              label={{ value: `FAR ceiling ${gates.false_answer_ceiling}`, position: 'right', fill: 'var(--red)', fontSize: 10 }}
            />
            {/* Lines per model */}
            {modelKeys.flatMap(({ id, meta }, i) => {
              const color = COLORS[i % COLORS.length];
              const calibrated = meta.calibrated_scores;
              return [
                <Line
                  key={`${id}_far`}
                  dataKey={`${id}_far`}
                  name={`${meta.display_name} FAR`}
                  stroke="var(--red)"
                  strokeWidth={calibrated ? 2.5 : 1.5}
                  strokeDasharray={calibrated ? undefined : '6 3'}
                  dot={false}
                  connectNulls
                  opacity={calibrated ? 1 : 0.7}
                />,
                <Line
                  key={`${id}_recall`}
                  dataKey={`${id}_recall`}
                  name={`${meta.display_name} Recall`}
                  stroke={color}
                  strokeWidth={calibrated ? 2.5 : 1.5}
                  strokeDasharray={calibrated ? undefined : '6 3'}
                  dot={false}
                  connectNulls
                  opacity={calibrated ? 1 : 0.7}
                />,
              ];
            })}
            {/* Operating point markers */}
            {operatingPoints.map(({ modelId, t }, i) => (
              <ReferenceLine
                key={`op_${modelId}`}
                x={t}
                stroke={COLORS[i % COLORS.length]}
                strokeDasharray="2 2"
                strokeWidth={1}
                label={{ value: '●', position: 'top', fill: COLORS[i % COLORS.length], fontSize: 14 }}
                ifOverflow="extendDomain"
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
      {/* Per-model color key + calibrated/operating-point key */}
      <div className="chart-legend">
        {modelKeys.map(({ id, meta }, i) => (
          <span className="lg" key={id}>
            <span className="sw" style={{ borderTopColor: COLORS[i % COLORS.length] }} />
            {meta.display_name} recall
          </span>
        ))}
        <span className="lg"><span className="sw" style={{ borderTopColor: 'var(--bad)' }} /> false-answer rate (all models)</span>
      </div>
      <div className="chart-legend" style={{ marginTop: 4 }}>
        <span className="lg"><span className="sw" style={{ borderTopColor: 'var(--ink2)' }} /> calibrated (solid)</span>
        <span className="lg"><span className="sw" style={{ borderTopColor: 'var(--ink2)', borderTopStyle: 'dashed' }} /> raw-logit (dashed)</span>
        <span className="lg" style={{ color: 'var(--bad)', fontWeight: 600 }}>● operating point</span>
        <span className="lg" style={{ color: 'var(--bad)' }}>— — FAR ceiling @ {gates.false_answer_ceiling}</span>
      </div>
    </div>
  );
}
