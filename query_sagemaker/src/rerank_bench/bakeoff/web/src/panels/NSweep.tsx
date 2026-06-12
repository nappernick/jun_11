import { useState, useMemo } from 'react';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts';
import type { Cell, ModelMeta } from '../types';

export interface NSweepProps {
  cells: Cell[];
  selectedSlice: string;
  models: ModelMeta[];
}

type Metric = 'ndcg10' | 'recall10' | 'p99';
const METRICS: { key: Metric; label: string }[] = [
  { key: 'ndcg10', label: 'nDCG@10' },
  { key: 'recall10', label: 'Recall@10' },
  { key: 'p99', label: 'p99' },
];

const COLORS = ['#58a6ff', '#3fb950', '#f85149', '#bc8cff', '#d29922', '#79c0ff', '#56d364', '#ff7b72'];

export default function NSweep({ cells, selectedSlice, models }: NSweepProps) {
  const [metric, setMetric] = useState<Metric>('ndcg10');

  const { chartData, modelIds } = useMemo(() => {
    const relevant = cells.filter(c => c.by_slice[selectedSlice]);
    const nValues = [...new Set(relevant.map(c => c.N))].sort((a, b) => a - b);
    const ids = [...new Set(relevant.map(c => c.model_id))];
    const data = nValues.map(n => {
      const point: Record<string, number> = { N: n };
      for (const id of ids) {
        const cell = relevant.find(c => c.model_id === id && c.N === n);
        if (cell) point[id] = cell.by_slice[selectedSlice][metric];
      }
      return point;
    });
    return { chartData: data, modelIds: ids };
  }, [cells, selectedSlice, metric]);

  const getName = (id: string) => models.find(m => m.id === id)?.display_name ?? id;

  return (
    <div className="panel">
      <div className="panel-title" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span>N Sweep</span>
        <div className="seg">
          {METRICS.map(m => (
            <button key={m.key} className={metric === m.key ? 'active' : ''} onClick={() => setMetric(m.key)}>{m.label}</button>
          ))}
        </div>
      </div>
      <ResponsiveContainer width="100%" height={260}>
        <LineChart data={chartData} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
          <CartesianGrid stroke="var(--grid)" strokeDasharray="3 3" />
          <XAxis dataKey="N" stroke="var(--text-muted)" tick={{ fontSize: 11 }} label={{ value: 'N (passages)', position: 'insideBottom', offset: -2, fill: 'var(--text-muted)', fontSize: 11 }} />
          <YAxis stroke="var(--text-muted)" tick={{ fontSize: 11 }} />
          <Tooltip contentStyle={{ background: 'var(--bg-1)', border: '1px solid var(--border)', fontSize: 11 }} />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          {modelIds.map((id, i) => (
            <Line key={id} type="monotone" dataKey={id} name={getName(id)} stroke={COLORS[i % COLORS.length]} dot={{ r: 3 }} strokeWidth={2} connectNulls />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
