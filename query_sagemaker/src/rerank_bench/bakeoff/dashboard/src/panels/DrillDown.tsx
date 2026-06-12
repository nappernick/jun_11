import { useState, useMemo } from 'react'
import type { ResultsFile } from '../types.ts'

interface Props {
  data: ResultsFile
  selectedSlice: string
}

export function DrillDown({ data, selectedSlice }: Props) {
  const modelIds = useMemo(() => data.cells.map(c => c.model_id), [data])
  const [selectedModel, setSelectedModel] = useState(modelIds[0] ?? '')

  const cell = useMemo(() => data.cells.find(c => c.model_id === selectedModel), [data, selectedModel])
  const sliceMetrics = cell?.by_slice[selectedSlice]

  const failures = useMemo(() => {
    if (!cell || !sliceMetrics) return { falseAnswers: [] as typeof data.cells[0]['rows'], answerableMisses: [] as typeof data.cells[0]['rows'] }
    const operatingT = sliceMetrics.abstain.operating_t
    // Filter rows matching current slice
    const sliceKey = selectedSlice.split('=')[0]
    const sliceVal = selectedSlice.split('=')[1]
    const sliceRows = cell.rows.filter(r => r.slice[sliceKey] === sliceVal)

    // False answers: expect_abstain but top_norm cleared t (model should have abstained but didn't)
    // In our rows, we don't have expect_abstain flag directly - approximate: if all rels are 0 (no gold in results), it's unanswerable
    const falseAnswers = sliceRows.filter(r => {
      const allIrrelevant = r.rels.every(x => x === 0)
      return allIrrelevant && r.top_norm >= operatingT
    })

    // Answerable misses: gold not in top-k (has relevant docs but didn't surface them well)
    const answerableMisses = sliceRows.filter(r => {
      const hasRelevant = r.rels.some(x => x > 0)
      const topHit = r.rels[0] === 1
      return hasRelevant && !topHit
    })

    return { falseAnswers, answerableMisses }
  }, [cell, sliceMetrics, selectedSlice])

  return (
    <>
      <h2>Drill-Down</h2>
      <div style={{ display: 'flex', gap: '0.5rem', marginBottom: '0.5rem' }}>
        <select value={selectedModel} onChange={e => setSelectedModel(e.target.value)}>
          {modelIds.map(id => <option key={id} value={id}>{data.models.find(m => m.id === id)?.display_name ?? id}</option>)}
        </select>
        <span style={{ fontSize: '0.75rem', color: '#8b8fa3' }}>Slice: {selectedSlice} | Operating t: {sliceMetrics?.abstain.operating_t.toFixed(2) ?? '—'}</span>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
        <div>
          <h2 style={{ color: '#ef4444' }}>False Answers ({failures.falseAnswers.length})</h2>
          <p style={{ fontSize: '0.7rem', color: '#8b8fa3', marginBottom: '0.3rem' }}>expect_abstain but top_norm ≥ t</p>
          {failures.falseAnswers.length === 0 ? <p style={{ fontSize: '0.75rem' }}>None</p> :
            <table><thead><tr><th>ID</th><th>top_norm</th><th>rels</th></tr></thead><tbody>
              {failures.falseAnswers.map(r => <tr key={r.id}><td>{r.id}</td><td>{r.top_norm.toFixed(3)}</td><td>[{r.rels.join(',')}]</td></tr>)}
            </tbody></table>}
        </div>
        <div>
          <h2 style={{ color: '#eab308' }}>Answerable Misses ({failures.answerableMisses.length})</h2>
          <p style={{ fontSize: '0.7rem', color: '#8b8fa3', marginBottom: '0.3rem' }}>gold not at rank 0</p>
          {failures.answerableMisses.length === 0 ? <p style={{ fontSize: '0.75rem' }}>None</p> :
            <table><thead><tr><th>ID</th><th>top_norm</th><th>rels</th></tr></thead><tbody>
              {failures.answerableMisses.map(r => <tr key={r.id}><td>{r.id}</td><td>{r.top_norm.toFixed(3)}</td><td>[{r.rels.join(',')}]</td></tr>)}
            </tbody></table>}
        </div>
      </div>
    </>
  )
}
