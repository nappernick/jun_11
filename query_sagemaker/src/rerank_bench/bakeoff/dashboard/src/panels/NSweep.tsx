import { useMemo } from 'react'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts'
import type { Cell } from '../types.ts'

interface Props {
  cells: Cell[]
  selectedSlice: string
}

const COLORS = ['#6366f1', '#22c55e', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4']

export function NSweep({ cells, selectedSlice }: Props) {
  // Group cells by N, plot ndcg10 per model at each N
  const chartData = useMemo(() => {
    const byN = new Map<number, Record<string, number>>()
    for (const c of cells) {
      const sl = c.by_slice[selectedSlice]
      if (!sl) continue
      if (!byN.has(c.N)) byN.set(c.N, { N: c.N })
      byN.get(c.N)![c.model_id] = sl.ndcg10
    }
    return [...byN.values()].sort((a, b) => a['N'] - b['N'])
  }, [cells, selectedSlice])

  const modelIds = useMemo(() => [...new Set(cells.map(c => c.model_id))], [cells])

  if (chartData.length < 2) {
    return <><h2>N-Sweep</h2><p style={{ color: '#8b8fa3', fontSize: '0.8rem' }}>Need multiple N values in data to show sweep.</p></>
  }

  return (
    <>
      <h2>N-Sweep (nDCG@10 vs N)</h2>
      <ResponsiveContainer width="100%" height={260}>
        <LineChart data={chartData} margin={{ top: 5, right: 10, bottom: 30, left: 10 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#2a2d3a" />
          <XAxis dataKey="N" stroke="#8b8fa3" label={{ value: 'N (corpus size)', position: 'bottom', fill: '#8b8fa3', fontSize: 11 }} />
          <YAxis stroke="#8b8fa3" domain={[0, 1]} />
          <Tooltip contentStyle={{ background: '#1a1d27', border: '1px solid #2a2d3a', fontSize: '0.7rem' }} />
          <Legend wrapperStyle={{ fontSize: '0.7rem' }} />
          {modelIds.map((id, i) => (
            <Line key={id} dataKey={id} stroke={COLORS[i % COLORS.length]} dot connectNulls name={id} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </>
  )
}
