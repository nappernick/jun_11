import { useState, useMemo } from 'react';
import type { SliceMetrics, Gates, ModelMeta } from '../types';
import { gatePass } from '../lib/selectors';

export interface LeaderboardProps {
  sliceMetrics: { modelId: string; metrics: SliceMetrics }[];
  gates: Gates;
  recommendedId: string;
  models: ModelMeta[];
  baselineId: string;
}

type SortKey = 'display_name' | 'params' | 'license' | 'deploy_path' | 'ndcg10' | 'recall10' | 'mrr10' | 'p50' | 'p99' | 'throughput_qps' | 'cost_per_1k' | 'far' | 'sig' | 'max_seq_len';

interface RowData {
  modelId: string;
  model: ModelMeta;
  metrics: SliceMetrics;
  passes: boolean;
}

export default function Leaderboard({ sliceMetrics, gates, recommendedId, models, baselineId }: LeaderboardProps) {
  const [sortKey, setSortKey] = useState<SortKey>('ndcg10');
  const [sortAsc, setSortAsc] = useState(false);

  const rows = useMemo<RowData[]>(() => {
    return sliceMetrics
      .map(({ modelId, metrics }) => {
        const model = models.find(m => m.id === modelId);
        if (!model) return null;
        return { modelId, model, metrics, passes: gatePass(metrics, gates) };
      })
      .filter((r): r is RowData => r !== null);
  }, [sliceMetrics, models, gates]);

  const sorted = useMemo(() => {
    const getValue = (r: RowData): number | string => {
      switch (sortKey) {
        case 'display_name': return r.model.display_name;
        case 'params': return r.model.params;
        case 'license': return r.model.license;
        case 'deploy_path': return r.model.deploy_path;
        case 'ndcg10': return r.metrics.ndcg10;
        case 'recall10': return r.metrics.recall10;
        case 'mrr10': return r.metrics.mrr10;
        case 'p50': return r.metrics.p50;
        case 'p99': return r.metrics.p99;
        case 'throughput_qps': return r.metrics.throughput_qps;
        case 'cost_per_1k': return r.metrics.cost_per_1k;
        case 'far': return r.metrics.abstain.false_answer_rate;
        case 'sig': return r.metrics.sig_vs_baseline;
        case 'max_seq_len': return r.model.max_seq_len;
      }
    };
    return [...rows].sort((a, b) => {
      const va = getValue(a), vb = getValue(b);
      const cmp = typeof va === 'string' ? va.localeCompare(vb as string) : (va as number) - (vb as number);
      return sortAsc ? cmp : -cmp;
    });
  }, [rows, sortKey, sortAsc]);

  const handleSort = (key: SortKey) => {
    if (sortKey === key) setSortAsc(!sortAsc);
    else { setSortKey(key); setSortAsc(false); }
  };

  const th = (label: string, key: SortKey) => (
    <th onClick={() => handleSort(key)} style={{ cursor: 'pointer', whiteSpace: 'nowrap' }}>
      {label}{sortKey === key ? (sortAsc ? ' ↑' : ' ↓') : ''}
    </th>
  );

  return (
    <div className="panel" style={{ gridColumn: '1 / -1' }}>
      <div className="panel-title">Leaderboard</div>
      <div style={{ overflowX: 'auto', flex: 1 }}>
        <table>
          <thead>
            <tr>
              {th('Model', 'display_name')}
              {th('Params', 'params')}
              {th('License', 'license')}
              {th('Deploy', 'deploy_path')}
              {th('nDCG@10', 'ndcg10')}
              {th('Recall@10', 'recall10')}
              {th('MRR@10', 'mrr10')}
              {th('p50', 'p50')}
              {th('p99', 'p99')}
              {th('QPS', 'throughput_qps')}
              {th('$/1k', 'cost_per_1k')}
              {th('FAR', 'far')}
              {th('Sig', 'sig')}
              {th('MaxSeq', 'max_seq_len')}
            </tr>
          </thead>
          <tbody>
            {sorted.map(({ modelId, model, metrics, passes }) => {
              const isRec = modelId === recommendedId;
              const isBaseline = modelId === baselineId;
              const ci = metrics.ndcg10_ci;
              const ciHalf = ((ci[1] - ci[0]) / 2).toFixed(3);
              return (
                <tr
                  key={modelId}
                  className={isRec ? 'recommended' : ''}
                  style={{ opacity: passes ? 1 : 0.45 }}
                >
                  <td style={{ whiteSpace: 'nowrap' }}>
                    {model.display_name}
                    {isBaseline && <span style={{ color: 'var(--text-muted)', marginLeft: 4, fontSize: 10 }}>(baseline)</span>}
                  </td>
                  <td>{model.params}</td>
                  <td>{model.license}</td>
                  <td style={{ maxWidth: 120, overflow: 'hidden', textOverflow: 'ellipsis' }}>{model.deploy_path}</td>
                  <td><span className={metrics.ndcg10 >= gates.accuracy_bar ? 'pass' : 'fail'}>{metrics.ndcg10.toFixed(3)}</span> <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>±{ciHalf}</span></td>
                  <td>{metrics.recall10.toFixed(3)}</td>
                  <td>{metrics.mrr10.toFixed(3)}</td>
                  <td>{metrics.p50.toFixed(0)}</td>
                  <td className={metrics.p99 <= gates.latency_budget_ms ? 'pass' : 'fail'}>{metrics.p99.toFixed(0)}</td>
                  <td>{metrics.throughput_qps.toFixed(1)}</td>
                  <td>{metrics.cost_per_1k.toFixed(3)}</td>
                  <td className={metrics.abstain.false_answer_rate <= gates.false_answer_ceiling ? 'pass' : 'fail'}>{metrics.abstain.false_answer_rate.toFixed(3)}</td>
                  <td>{metrics.sig_vs_baseline < 0.05 ? <span style={{ color: 'var(--green)' }}>✓ {metrics.sig_vs_baseline.toFixed(3)}</span> : <span style={{ color: 'var(--text-muted)' }}>{metrics.sig_vs_baseline.toFixed(3)}</span>}</td>
                  <td>{model.max_seq_len}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
