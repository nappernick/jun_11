import { useMemo, useState } from 'react';
import { useResults } from './lib/useResults';
import { sliceDimensions, buildSliceKey, recommendedModelId } from './lib/selectors';
import type { SliceMetrics, Cell } from './types';
import CostQuality from './panels/CostQuality';
import AbstentionSweep from './panels/AbstentionSweep';
import Leaderboard from './panels/Leaderboard';
import NSweep from './panels/NSweep';
import LatencyBudget from './panels/LatencyBudget';
import DrillDown from './panels/DrillDown';

export default function App() {
  const { data, error, loading } = useResults();
  const [selections, setSelections] = useState<Record<string, string>>({});

  // Extract all slice keys from all cells
  const allSliceKeys = useMemo(() => {
    if (!data) return [] as string[];
    const keys = new Set<string>();
    for (const cell of data.cells) {
      for (const k of Object.keys(cell.by_slice)) keys.add(k);
    }
    return [...keys];
  }, [data]);

  const dims = useMemo(() => sliceDimensions(allSliceKeys), [allSliceKeys]);

  // Initialize selections to first value of each dimension
  const activeSelections = useMemo(() => {
    const s: Record<string, string> = {};
    for (const [dim, vals] of Object.entries(dims)) {
      s[dim] = selections[dim] ?? vals[0] ?? '';
    }
    return s;
  }, [dims, selections]);

  const activeSlice = useMemo(() => buildSliceKey(activeSelections), [activeSelections]);

  // Compute per-model metrics for active slice (pick highest-N cell per model)
  const sliceMetrics = useMemo(() => {
    if (!data) return [];
    const byModel = new Map<string, { metrics: SliceMetrics; cell: Cell }>();
    for (const cell of data.cells) {
      const m = cell.by_slice[activeSlice];
      if (!m) continue;
      const existing = byModel.get(cell.model_id);
      if (!existing || cell.N > existing.cell.N) {
        byModel.set(cell.model_id, { metrics: m, cell });
      }
    }
    return [...byModel.entries()].map(([modelId, { metrics, cell }]) => ({ modelId, metrics, cell }));
  }, [data, activeSlice]);

  const recId = useMemo(
    () => (data ? recommendedModelId(sliceMetrics, data.gates) : ''),
    [sliceMetrics, data]
  );

  if (loading) return <div style={{ padding: 40, color: 'var(--text-muted)' }}>Loading…</div>;
  if (error || !data) return <div style={{ padding: 40, color: 'var(--red)' }}>Error: {error}</div>;

  return (
    <>
      <header className="header">
        <h1>Reranker Bakeoff</h1>
        <span className="run-id">{data.run_id}</span>
        <div className="gates">
          <span>nDCG≥{data.gates.accuracy_bar}</span>
          <span>p99≤{data.gates.latency_budget_ms}ms</span>
          <span>FAR≤{data.gates.false_answer_ceiling}</span>
        </div>
      </header>

      <nav className="slicer">
        {Object.entries(dims).map(([dim, vals]) => (
          <div className="slicer-group" key={dim}>
            <label>{dim}</label>
            <div className="seg">
              {vals.map((v) => (
                <button
                  key={v}
                  className={activeSelections[dim] === v ? 'active' : ''}
                  onClick={() => setSelections((s) => ({ ...s, [dim]: v }))}
                >
                  {v}
                </button>
              ))}
            </div>
          </div>
        ))}
      </nav>

      <div className="panel-grid">
        <CostQuality sliceMetrics={sliceMetrics} gates={data.gates} recommendedId={recId} models={data.models} />
        <AbstentionSweep sliceMetrics={sliceMetrics} gates={data.gates} models={data.models} />
        <Leaderboard sliceMetrics={sliceMetrics} gates={data.gates} recommendedId={recId} models={data.models} baselineId={data.baseline_model_id} />
        <NSweep cells={data.cells} selectedSlice={activeSlice} models={data.models} />
        <LatencyBudget sliceMetrics={sliceMetrics} gates={data.gates} models={data.models} />
        <DrillDown data={data} selectedSlice={activeSlice} />
      </div>
    </>
  );
}
