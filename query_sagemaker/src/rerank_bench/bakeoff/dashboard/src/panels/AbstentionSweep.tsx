import { useMemo, useState } from 'react'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ReferenceLine, ResponsiveContainer } from 'recharts'
import type { SliceMetrics, Gates } from '../types.ts'

interface Props {
  data: { modelId: string; metrics: SliceMetrics }[]
  gates: Gates
}

const COLORS = ['#6366f1', '#22c55e', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4']

export function AbstentionSweep({ data, gates }: Props) {
  const [matchFAR, setMatchFAR] = useState(false)
  const [targetFAR, setTargetFAR] = useState(gates.false_answer_ceiling)

  // Merge all curve points into a single dataset keyed by t
  const chartData = useMemo(() => {
    if (matchFAR) {
      // At matched FAR: interpolate to find t where FAR = targetFAR for each model
      return data.map(d => {
        const curve = d.metrics.abstain_curve
        // Find two points bracketing targetFAR (FAR decreases with t)
        let matchedRecall = NaN
        for (let i = 0; i < curve.length - 1; i++) {
          const a = curve[i], b = curve[i + 1]
          if ((a.false_answer_rate >= targetFAR && b.false_answer_rate <= targetFAR) ||
              (a.false_answer_rate <= targetFAR && b.false_answer_rate >= targetFAR)) {
            const frac = (targetFAR - a.false_answer_rate) / (b.false_answer_rate - a.false_answer_rate)
            matchedRecall = a.abstain_recall + frac * (b.abstain_recall - a.abstain_recall)
            break
          }
        }
        // Fallback: closest point
        if (isNaN(matchedRecall)) {
          const closest = [...curve].sort((a, b) => Math.abs(a.false_answer_rate - targetFAR) - Math.abs(b.false_answer_rate - targetFAR))[0]
          matchedRecall = closest?.abstain_recall ?? 0
        }
        return { modelId: d.modelId, recall: matchedRecall, far: targetFAR }
      })
    }
    // Standard: all t-points merged
    const tSet = new Set<number>()
    for (const d of data) for (const p of d.metrics.abstain_curve) tSet.add(p.t)
    const ts = [...tSet].sort((a, b) => a - b)
    return ts.map(t => {
      const row: Record<string, number> = { t }
      for (const d of data) {
        const pt = d.metrics.abstain_curve.find(p => p.t === t)
        if (pt) {
          row[`${d.modelId}_recall`] = pt.abstain_recall
          row[`${d.modelId}_far`] = pt.false_answer_rate
        }
      }
      return row
    })
  }, [data, matchFAR, targetFAR])

  if (matchFAR) {
    // Bar-style comparison at matched FAR
    const matchedData = chartData as { modelId: string; recall: number; far: number }[]
    return (
      <>
        <h2>Abstention @ Matched FAR={targetFAR.toFixed(2)}</h2>
        <div style={{ display: 'flex', gap: '0.5rem', marginBottom: '0.5rem', alignItems: 'center' }}>
          <button onClick={() => setMatchFAR(false)}>← Sweep view</button>
          <label style={{ fontSize: '0.7rem' }}>Target FAR:
            <input type="range" min={0.01} max={0.2} step={0.01} value={targetFAR}
              onChange={e => setTargetFAR(+e.target.value)} style={{ width: 80, marginLeft: 4 }} />
            {targetFAR.toFixed(2)}
          </label>
        </div>
        <table>
          <thead><tr><th>Model</th><th>Abstain Recall</th></tr></thead>
          <tbody>
            {matchedData.map((d, i) => (
              <tr key={d.modelId}><td style={{ color: COLORS[i % COLORS.length] }}>{d.modelId}</td><td>{d.recall.toFixed(3)}</td></tr>
            ))}
          </tbody>
        </table>
      </>
    )
  }

  const lineData = chartData as Record<string, number>[]
  return (
    <>
      <h2>Abstention Sweep</h2>
      <button onClick={() => setMatchFAR(true)} style={{ marginBottom: '0.5rem', fontSize: '0.7rem' }}>Match FAR →</button>
      <ResponsiveContainer width="100%" height={260}>
        <LineChart data={lineData} margin={{ top: 5, right: 10, bottom: 30, left: 10 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#2a2d3a" />
          <XAxis dataKey="t" stroke="#8b8fa3" label={{ value: 'threshold t', position: 'bottom', fill: '#8b8fa3', fontSize: 11 }} />
          <YAxis stroke="#8b8fa3" domain={[0, 1]} />
          <Tooltip contentStyle={{ background: '#1a1d27', border: '1px solid #2a2d3a', fontSize: '0.7rem' }} />
          <Legend wrapperStyle={{ fontSize: '0.7rem' }} />
          <ReferenceLine y={gates.false_answer_ceiling} stroke="#ef4444" strokeDasharray="3 3" />
          {data.map((d, i) => (
            <Line key={`${d.modelId}_recall`} dataKey={`${d.modelId}_recall`} stroke={COLORS[i % COLORS.length]}
              name={`${d.modelId} recall`} dot={false} strokeWidth={2} connectNulls />
          ))}
          {data.map((d, i) => (
            <Line key={`${d.modelId}_far`} dataKey={`${d.modelId}_far`} stroke={COLORS[i % COLORS.length]}
              name={`${d.modelId} FAR`} dot={false} strokeDasharray="4 2" connectNulls />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </>
  )
}
