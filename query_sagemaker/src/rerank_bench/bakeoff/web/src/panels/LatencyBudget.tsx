import { useMemo } from 'react';
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ReferenceLine, ResponsiveContainer, Cell as RCell } from 'recharts';
import type { SliceMetrics, Gates, ModelMeta } from '../types';

export interface LatencyBudgetProps {
  sliceMetrics: { modelId: string; metrics: SliceMetrics }[];
  gates: Gates;
  models: ModelMeta[];
}

export default function LatencyBudget({ sliceMetrics, gates, models }: LatencyBudgetProps) {
  const chartData = useMemo(() => {
    return sliceMetrics.map(({ modelId, metrics }) => {
      const model = models.find(m => m.id === modelId);
      return {
        name: model?.display_name ?? modelId,
        p50: metrics.p50,
        p95: metrics.p95,
        p99: metrics.p99,
        exceeds: metrics.p99 > gates.latency_budget_ms,
      };
    });
  }, [sliceMetrics, models, gates]);

  return (
    <div className="panel">
      <div className="panel-title">Latency Budget</div>
      <ResponsiveContainer width="100%" height={280}>
        <BarChart data={chartData} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
          <CartesianGrid stroke="var(--grid)" strokeDasharray="3 3" />
          <XAxis dataKey="name" stroke="var(--text-muted)" tick={{ fontSize: 10 }} angle={-20} textAnchor="end" height={50} />
          <YAxis stroke="var(--text-muted)" tick={{ fontSize: 11 }} label={{ value: 'ms', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 11 }} />
          <Tooltip contentStyle={{ background: 'var(--bg-1)', border: '1px solid var(--border)', fontSize: 11 }} />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          <ReferenceLine y={gates.latency_budget_ms} stroke="var(--red)" strokeDasharray="6 3" strokeWidth={2} label={{ value: `budget ${gates.latency_budget_ms}ms`, fill: 'var(--red)', fontSize: 10, position: 'right' }} />
          <Bar dataKey="p50" fill="var(--accent)" name="p50">
            {chartData.map((_, i) => <RCell key={i} />)}
          </Bar>
          <Bar dataKey="p95" fill="var(--yellow)" name="p95">
            {chartData.map((_, i) => <RCell key={i} />)}
          </Bar>
          <Bar dataKey="p99" name="p99">
            {chartData.map((entry, i) => <RCell key={i} fill={entry.exceeds ? 'var(--red)' : 'var(--green)'} />)}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
