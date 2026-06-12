import { useState, useMemo } from 'react';
import type { ResultsFile, Row, Cell } from '../types';

export interface DrillDownProps {
  data: ResultsFile;
  selectedSlice: string;
}

export default function DrillDown({ data, selectedSlice }: DrillDownProps) {
  const modelIds = useMemo(
    () => [...new Set(data.cells.map(c => c.model_id))],
    [data.cells]
  );
  const [modelId, setModelId] = useState(modelIds[0] ?? '');

  // Pick highest-N cell for the selected model
  const cell: Cell | undefined = useMemo(
    () =>
      data.cells
        .filter(c => c.model_id === modelId)
        .sort((a, b) => b.N - a.N)[0],
    [data.cells, modelId]
  );

  // Filter rows to selected slice
  const sliceRows: Row[] = useMemo(() => {
    if (!cell) return [];
    const dims = selectedSlice.split('&').map(p => {
      const eq = p.indexOf('=');
      return [p.slice(0, eq), p.slice(eq + 1)] as [string, string];
    });
    return cell.rows.filter(r =>
      dims.every(([k, v]) => r.slice[k] === v)
    );
  }, [cell, selectedSlice]);

  // Operating threshold for this model/slice
  const operatingT: number = useMemo(() => {
    if (!cell) return 0.5;
    const metrics = cell.by_slice[selectedSlice];
    return metrics?.abstain.operating_t ?? 0.5;
  }, [cell, selectedSlice]);

  // FALSE-ANSWERS: all rels are 0 AND top_norm >= operating_t (model confident on unanswerable query)
  const falseAnswers = useMemo(
    () =>
      sliceRows
        .filter(r => r.rels.every(v => v === 0) && r.top_norm >= operatingT)
        .sort((a, b) => b.top_norm - a.top_norm),
    [sliceRows, operatingT]
  );

  // ANSWERABLE-MISSES: has at least one relevant doc in rels, but none in top positions (no 1 in first 3)
  const answerableMisses = useMemo(
    () =>
      sliceRows
        .filter(r => r.rels.some(v => v === 1) && r.rels.slice(0, 3).every(v => v === 0))
        .sort((a, b) => b.latency - a.latency),
    [sliceRows]
  );

  const modelMeta = data.models.find(m => m.id === modelId);

  return (
    <div className="panel" style={{ gridColumn: '1 / -1' }}>
      <div className="panel-title">
        Failure Drill-Down
        <select
          value={modelId}
          onChange={e => setModelId(e.target.value)}
          style={{
            marginLeft: 12,
            background: 'var(--bg-2)',
            color: 'var(--text)',
            border: '1px solid var(--border)',
            borderRadius: 'var(--radius)',
            padding: '2px 8px',
            fontSize: 11,
            fontFamily: 'var(--font-sans)',
          }}
        >
          {modelIds.map(id => {
            const m = data.models.find(x => x.id === id);
            return <option key={id} value={id}>{m?.display_name ?? id}</option>;
          })}
        </select>
        {modelMeta?.calibrated_scores && (
          <span style={{ marginLeft: 8, color: 'var(--purple)', fontSize: 11 }}>● calibrated</span>
        )}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, flex: 1, overflow: 'auto' }}>
        {/* FALSE ANSWERS */}
        <div>
          <h3 style={{ color: 'var(--red)', fontSize: 12, marginBottom: 4 }}>
            False Answers ({falseAnswers.length})
          </h3>
          <p style={{ color: 'var(--text-muted)', fontSize: 10, marginBottom: 8 }}>
            Heuristic: all rels=0 (unanswerable) AND top_norm ≥ {operatingT.toFixed(3)} (model would answer). Sorted by top_norm desc.
          </p>
          <div style={{ maxHeight: 280, overflow: 'auto' }}>
            <table>
              <thead>
                <tr><th>id</th><th>top_norm</th><th>rels</th><th>lat ms</th></tr>
              </thead>
              <tbody>
                {falseAnswers.slice(0, 50).map(r => (
                  <tr key={r.id} className="danger-zone">
                    <td style={{ maxWidth: 140, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.id}</td>
                    <td className="fail">{r.top_norm.toFixed(4)}</td>
                    <td><RelsStrip rels={r.rels} /></td>
                    <td>{r.latency.toFixed(0)}</td>
                  </tr>
                ))}
                {falseAnswers.length === 0 && (
                  <tr><td colSpan={4} style={{ color: 'var(--text-muted)', textAlign: 'center' }}>None detected</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        {/* ANSWERABLE MISSES */}
        <div>
          <h3 style={{ color: 'var(--yellow)', fontSize: 12, marginBottom: 4 }}>
            Answerable Misses ({answerableMisses.length})
          </h3>
          <p style={{ color: 'var(--text-muted)', fontSize: 10, marginBottom: 8 }}>
            Heuristic: rels has ≥1 relevant doc but none in top-3 positions. Sorted by latency desc.
          </p>
          <div style={{ maxHeight: 280, overflow: 'auto' }}>
            <table>
              <thead>
                <tr><th>id</th><th>top_norm</th><th>rels</th><th>lat ms</th></tr>
              </thead>
              <tbody>
                {answerableMisses.slice(0, 50).map(r => (
                  <tr key={r.id}>
                    <td style={{ maxWidth: 140, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.id}</td>
                    <td>{r.top_norm.toFixed(4)}</td>
                    <td><RelsStrip rels={r.rels} /></td>
                    <td>{r.latency.toFixed(0)}</td>
                  </tr>
                ))}
                {answerableMisses.length === 0 && (
                  <tr><td colSpan={4} style={{ color: 'var(--text-muted)', textAlign: 'center' }}>None detected</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <p style={{ color: 'var(--text-muted)', fontSize: 10, marginTop: 8, fontStyle: 'italic' }}>
        Full gold labels are in the fixtures (sample_results.json), not in Row objects. These heuristics approximate failure modes from available signals.
      </p>
    </div>
  );
}

/** Renders rels as a compact colored 0/1 strip */
function RelsStrip({ rels }: { rels: number[] }) {
  return (
    <span style={{ display: 'inline-flex', gap: 1 }}>
      {rels.map((v, i) => (
        <span
          key={i}
          style={{
            width: 6,
            height: 12,
            borderRadius: 1,
            background: v === 1 ? 'var(--green)' : 'var(--border)',
          }}
        />
      ))}
    </span>
  );
}
