import { useState, useMemo } from 'react';
import {
  Scatter, XAxis, YAxis, CartesianGrid, Tooltip,
  ReferenceLine, Line, ComposedChart, ErrorBar, ResponsiveContainer,
  Label,
} from 'recharts';
import type { SliceMetrics, Gates, Cell, ModelMeta } from '../types';
import { paretoFrontier, gatePass } from '../lib/selectors';

export interface CostQualityProps {
  sliceMetrics: { modelId: string; metrics: SliceMetrics; cell: Cell }[];
  gates: Gates;
  recommendedId: string;
  models: ModelMeta[];
}

type XMode = 'cost' | 'latency';

export default function CostQuality({ sliceMetrics, gates, recommendedId, models }: CostQualityProps) {
  const [xMode, setXMode] = useState<XMode>('cost');

  // Auto-switch to latency if all costs are 0
  const effectiveXMode = useMemo(() => {
    if (xMode === 'cost' && sliceMetrics.every(s => s.metrics.cost_per_1k === 0)) return 'latency';
    return xMode;
  }, [xMode, sliceMetrics]);

  const chartPoints = useMemo(() => {
    return sliceMetrics.map(({ modelId, metrics }) => {
      const model = models.find(m => m.id === modelId);
      const pass = gatePass(metrics, gates);
      return {
        modelId,
        name: model?.display_name ?? modelId,
        x: effectiveXMode === 'cost' ? metrics.cost_per_1k : metrics.p99,
        ndcg10: metrics.ndcg10,
        ciLow: metrics.ndcg10 - metrics.ndcg10_ci[0],
        ciHigh: metrics.ndcg10_ci[1] - metrics.ndcg10,
        cost_per_1k: metrics.cost_per_1k,
        p99: metrics.p99,
        pass,
        isRecommended: modelId === recommendedId,
      };
    });
  }, [sliceMetrics, models, gates, recommendedId, effectiveXMode]);

  const frontier = useMemo(() => {
    if (effectiveXMode === 'latency') return [];
    const pts = chartPoints.map(p => ({ cost_per_1k: p.cost_per_1k, ndcg10: p.ndcg10, x: p.x }));
    return paretoFrontier(pts).map(fp => ({ x: fp.x, ndcg10: fp.ndcg10 }));
  }, [chartPoints, effectiveXMode]);

  const xLabel = effectiveXMode === 'cost' ? 'Cost per 1K queries ($)' : 'p99 Latency (ms)';
  const allZeroCost = sliceMetrics.every(s => s.metrics.cost_per_1k === 0);

  return (
    <div className="panel">
      <div className="panel-title" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span>Cost vs Quality (Pareto)</span>
        <div className="seg">
          <button className={effectiveXMode === 'cost' ? 'active' : ''} onClick={() => setXMode('cost')} disabled={allZeroCost}>
            Cost
          </button>
          <button className={effectiveXMode === 'latency' ? 'active' : ''} onClick={() => setXMode('latency')}>
            p99 Latency
          </button>
        </div>
      </div>
      {allZeroCost && effectiveXMode === 'latency' && (
        <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 6, fontStyle: 'italic' }}>
          All models are self-hosted (cost = $0) — showing p99 latency as x-axis.
        </div>
      )}
      <div style={{ flex: 1, minHeight: 280 }}>
        <ResponsiveContainer width="100%" height={280}>
          <ComposedChart margin={{ top: 20, right: 20, bottom: 30, left: 20 }}>
            <CartesianGrid stroke="var(--grid)" strokeDasharray="3 3" />
            <XAxis
              dataKey="x"
              type="number"
              name={xLabel}
              domain={['auto', 'auto']}
              tick={{ fill: 'var(--text-muted)', fontSize: 11 }}
              stroke="var(--border)"
            >
              <Label value={xLabel} position="bottom" offset={10} style={{ fill: 'var(--text-muted)', fontSize: 11 }} />
            </XAxis>
            <YAxis
              dataKey="ndcg10"
              type="number"
              name="nDCG@10"
              domain={['auto', 'auto']}
              tick={{ fill: 'var(--text-muted)', fontSize: 11 }}
              stroke="var(--border)"
            >
              <Label value="nDCG@10" angle={-90} position="left" offset={5} style={{ fill: 'var(--text-muted)', fontSize: 11 }} />
            </YAxis>
            <Tooltip
              contentStyle={{ background: 'var(--bg-1)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', fontSize: 12 }}
              labelStyle={{ color: 'var(--text)' }}
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              formatter={(value: any, name: any) => {
                if (name === 'ndcg10') return [Number(value).toFixed(4), 'nDCG@10'];
                return [value, name];
              }}
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              labelFormatter={(_: any, payload: any) => payload?.[0]?.payload?.name ?? ''}
            />
            {/* Accuracy gate line */}
            <ReferenceLine y={gates.accuracy_bar} stroke="var(--yellow)" strokeDasharray="6 3" strokeWidth={1.5}>
              <Label value={`Gate: nDCG ≥ ${gates.accuracy_bar}`} position="right" style={{ fill: 'var(--yellow)', fontSize: 10 }} />
            </ReferenceLine>
            {/* Pareto frontier line */}
            {frontier.length > 1 && (
              <Line
                data={frontier}
                dataKey="ndcg10"
                stroke="var(--accent)"
                strokeWidth={2}
                dot={false}
                strokeDasharray="5 3"
                name="Pareto Frontier"
                legendType="line"
              />
            )}
            {/* Passing models */}
            <Scatter
              data={chartPoints.filter(p => p.pass && !p.isRecommended)}
              fill="var(--green)"
              name="Pass Gates"
              shape="circle"
            >
              <ErrorBar dataKey="ciHigh" direction="y" stroke="var(--green)" strokeWidth={1} width={4} />
              <ErrorBar dataKey="ciLow" direction="y" stroke="var(--green)" strokeWidth={1} width={4} />
            </Scatter>
            {/* Failing models */}
            <Scatter
              data={chartPoints.filter(p => !p.pass && !p.isRecommended)}
              fill="var(--red)"
              name="Fail Gates"
              shape="diamond"
            >
              <ErrorBar dataKey="ciHigh" direction="y" stroke="var(--red)" strokeWidth={1} width={4} />
              <ErrorBar dataKey="ciLow" direction="y" stroke="var(--red)" strokeWidth={1} width={4} />
            </Scatter>
            {/* Recommended model */}
            {chartPoints.filter(p => p.isRecommended).length > 0 && (
              <Scatter
                data={chartPoints.filter(p => p.isRecommended)}
                fill="var(--accent)"
                name="★ Recommended"
                shape="star"
              >
                <ErrorBar dataKey="ciHigh" direction="y" stroke="var(--accent)" strokeWidth={1} width={4} />
                <ErrorBar dataKey="ciLow" direction="y" stroke="var(--accent)" strokeWidth={1} width={4} />
              </Scatter>
            )}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      {/* Model labels below chart */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 12px', marginTop: 8, fontSize: 11, fontFamily: 'var(--font-mono)' }}>
        {chartPoints.map(p => (
          <span key={p.modelId} style={{ color: p.isRecommended ? 'var(--accent)' : p.pass ? 'var(--green)' : 'var(--red)' }}>
            {p.isRecommended ? '★ ' : ''}{p.name}
            <span style={{ color: 'var(--text-muted)', marginLeft: 4 }}>
              ({effectiveXMode === 'cost' ? `$${p.cost_per_1k.toFixed(2)}` : `${p.p99.toFixed(0)}ms`}, {p.ndcg10.toFixed(3)})
            </span>
          </span>
        ))}
      </div>
    </div>
  );
}
