import { useState, useMemo } from 'react'
import { ScatterChart, Scatter, XAxis, YAxis, CartesianGrid, Tooltip, ReferenceLine, ResponsiveContainer, Cell } from 'recharts'
import type { SliceMetrics, Gates, ModelMeta } from '../types.ts'

interface Props {
  data: { modelId: string; metrics: SliceMetrics }[]
  gates: Gates
  recommendedId: string
  models: ModelMeta[]
}

const COLORS = ['#6366f1', '#22c55e', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4']

function computePareto(points: { x: number; y: number; idx: number }[]): Set<number> {
  // Pareto: minimize x (cost), maximize y (quality)
  const sorted = [...points].sort((a, b) => a.x - b.x)
  const frontier = new Set<number>()
  let maxY = -Infinity
  for (const p of sorted) {
    if (p.y >= maxY) { frontier.add(p.idx); maxY = p.y }
  }
  return frontier
}

export function CostQuality({ data, gates, recommendedId, models }: Props) {
  const [useP99] = useState(false)

  const points = useMemo(() =>
    data.map((d, i) => ({
      x: useP99 ? d.metrics.p99 : d.metrics.cost_per_1k,
      y: d.metrics.ndcg10,
      name: models.find(m => m.id === d.modelId)?.display_name ?? d.modelId,
      modelId: d.modelId,
      idx: i,
    })), [data, useP99, models])

  const pareto = useMemo(() => computePareto(points), [points])

  return (
    <>
      <h2>Cost / Quality Frontier</h2>
      <ResponsiveContainer width="100%" height={280}>
        <ScatterChart margin={{ top: 10, right: 20, bottom: 30, left: 10 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#2a2d3a" />
          <XAxis type="number" dataKey="x" name={useP99 ? 'p99 (ms)' : '$/1k'} stroke="#8b8fa3"
            label={{ value: useP99 ? 'p99 (ms)' : '$/1k queries', position: 'bottom', fill: '#8b8fa3', fontSize: 11 }} />
          <YAxis type="number" dataKey="y" name="nDCG@10" stroke="#8b8fa3"
            label={{ value: 'nDCG@10', angle: -90, position: 'insideLeft', fill: '#8b8fa3', fontSize: 11 }} />
          <Tooltip content={({ payload }) => {
            if (!payload?.[0]) return null
            const d = payload[0].payload as typeof points[0]
            return <div style={{ background: '#1a1d27', border: '1px solid #2a2d3a', padding: '0.5rem', borderRadius: 4, fontSize: '0.75rem' }}>
              <strong>{d.name}</strong>{d.modelId === recommendedId ? ' ⭐' : ''}<br/>
              {useP99 ? 'p99' : '$/1k'}: {d.x.toFixed(2)}<br/>nDCG@10: {d.y.toFixed(3)}
              {pareto.has(d.idx) ? <><br/><em>Pareto optimal</em></> : null}
            </div>
          }} />
          <ReferenceLine y={gates.accuracy_bar} stroke="#eab308" strokeDasharray="5 5" label={{ value: 'accuracy bar', fill: '#eab308', fontSize: 10 }} />
          <Scatter data={points}>
            {points.map((p, i) => (
              <Cell key={i} fill={pareto.has(i) ? COLORS[i % COLORS.length] : '#555'} r={p.modelId === recommendedId ? 8 : 5}
                stroke={p.modelId === recommendedId ? '#fff' : undefined} strokeWidth={p.modelId === recommendedId ? 2 : 0} />
            ))}
          </Scatter>
        </ScatterChart>
      </ResponsiveContainer>
    </>
  )
}
