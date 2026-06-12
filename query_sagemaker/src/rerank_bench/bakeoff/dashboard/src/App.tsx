import { useState, useEffect, useMemo } from 'react'
import type { ResultsFile, SliceMetrics, Cell } from './types.ts'
import { CostQuality } from './panels/CostQuality.tsx'
import { AbstentionSweep } from './panels/AbstentionSweep.tsx'
import { Leaderboard } from './panels/Leaderboard.tsx'
import { NSweep } from './panels/NSweep.tsx'
import { LatencyBudget } from './panels/LatencyBudget.tsx'
import { DrillDown } from './panels/DrillDown.tsx'

export default function App() {
  const [data, setData] = useState<ResultsFile | null>(null)
  const [error, setError] = useState('')
  const [selectedSlice, setSelectedSlice] = useState<string>('')

  useEffect(() => {
    fetch('/results/sample_results.json')
      .then(r => { if (!r.ok) throw new Error(`${r.status}`); return r.json() })
      .then((d: ResultsFile) => { setData(d); setSelectedSlice(Object.keys(d.cells[0]?.by_slice ?? {})[0] ?? '') })
      .catch(e => setError(String(e)))
  }, [])

  const sliceKeys = useMemo(() => {
    if (!data) return [] as string[]
    const keys = new Set<string>()
    for (const cell of data.cells) for (const k of Object.keys(cell.by_slice)) keys.add(k)
    return [...keys].sort()
  }, [data])

  // Group slices by dimension for segmented controls
  const sliceDimensions = useMemo(() => {
    const dims: Record<string, string[]> = {}
    for (const k of sliceKeys) {
      const [dim] = k.split('=')
      if (!dims[dim]) dims[dim] = []
      dims[dim].push(k)
    }
    return dims
  }, [sliceKeys])

  const sliceMetrics = useMemo(() => {
    if (!data || !selectedSlice) return [] as { modelId: string; metrics: SliceMetrics; cell: Cell }[]
    return data.cells
      .filter(c => c.by_slice[selectedSlice])
      .map(c => ({ modelId: c.model_id, metrics: c.by_slice[selectedSlice], cell: c }))
  }, [data, selectedSlice])

  // Determine recommended model: highest nDCG that passes all gates
  const recommendedId = useMemo(() => {
    if (!data || sliceMetrics.length === 0) return ''
    const passing = sliceMetrics.filter(s =>
      s.metrics.ndcg10 >= data.gates.accuracy_bar &&
      s.metrics.p99 <= data.gates.latency_budget_ms &&
      s.metrics.abstain.false_answer_rate <= data.gates.false_answer_ceiling
    )
    if (passing.length === 0) return ''
    passing.sort((a, b) => b.metrics.ndcg10 - a.metrics.ndcg10)
    return passing[0].modelId
  }, [data, sliceMetrics])

  if (error) return <div style={{ padding: '2rem', color: 'var(--red)' }}>Load error: {error}</div>
  if (!data) return <div style={{ padding: '2rem' }}>Loading…</div>

  return (
    <>
      <h1>Reranker Bakeoff — {data.run_id}</h1>
      <div className="slicer">
        <label>Slice:</label>
        {Object.entries(sliceDimensions).map(([dim, vals]) => (
          <div key={dim} className="seg-group">
            {vals.map(v => (
              <button key={v} className={`seg-btn ${v === selectedSlice ? 'active' : ''}`}
                onClick={() => setSelectedSlice(v)}>
                {v.split('=')[1]}
              </button>
            ))}
          </div>
        ))}
      </div>
      <div className="panels">
        <div className="panel"><CostQuality data={sliceMetrics} gates={data.gates} recommendedId={recommendedId} models={data.models} /></div>
        <div className="panel"><AbstentionSweep data={sliceMetrics} gates={data.gates} /></div>
        <div className="panel full"><Leaderboard data={sliceMetrics} gates={data.gates} recommendedId={recommendedId} models={data.models} baselineId={data.baseline_model_id} /></div>
        <div className="panel"><NSweep cells={data.cells} selectedSlice={selectedSlice} /></div>
        <div className="panel"><LatencyBudget data={sliceMetrics} gates={data.gates} models={data.models} /></div>
        <div className="panel full"><DrillDown data={data} selectedSlice={selectedSlice} /></div>
      </div>
    </>
  )
}
