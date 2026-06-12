import { useMemo } from 'react'
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ReferenceLine, ResponsiveContainer } from 'recharts'
import type { SliceMetrics, Gates, ModelMeta } from '../types.ts'

interface Props {
  data: { modelId: string; metrics: SliceMetrics }[]
  gates: Gates
  models: ModelMeta[]
}

export function LatencyBudget({ data, gates, models }: Props) {
  const chartData = useMemo(() =>
    data.map(d => ({
      name: models.find(m => m.id === d.modelId)?.display_name ?? d.modelId,
      p50: d.metrics.p50,
      p95: d.metrics.p95,
      p99: d.metrics.p99,
    })), [data, models])

  return (
    <>
      <h2>Latency vs Budget</h2>
      <ResponsiveContainer width="100%" height={260}>
        <BarChart data={chartData} margin={{ top: 5, right: 10, bottom: 30, left: 10 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#2a2d3a" />
          <XAxis dataKey="name" stroke="#8b8fa3" fontSize={10} />
          <YAxis stroke="#8b8fa3" label={{ value: 'ms', angle: -90, position: 'insideLeft', fill: '#8b8fa3', fontSize: 11 }} />
          <Tooltip contentStyle={{ background: '#1a1d27', border: '1px solid #2a2d3a', fontSize: '0.7rem' }} />
          <Legend wrapperStyle={{ fontSize: '0.7rem' }} />
          <ReferenceLine y={gates.latency_budget_ms} stroke="#ef4444" strokeDasharray="5 5" label={{ value: 'budget', fill: '#ef4444', fontSize: 10 }} />
          <Bar dataKey="p50" fill="#6366f1" name="p50" />
          <Bar dataKey="p95" fill="#22c55e" name="p95" />
          <Bar dataKey="p99" fill="#f59e0b" name="p99" />
        </BarChart>
      </ResponsiveContainer>
    </>
  )
}
