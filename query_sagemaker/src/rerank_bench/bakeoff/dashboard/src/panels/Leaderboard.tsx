import { useState, useMemo } from 'react'
import type { SliceMetrics, Gates, ModelMeta } from '../types.ts'

interface Props {
  data: { modelId: string; metrics: SliceMetrics }[]
  gates: Gates
  recommendedId: string
  models: ModelMeta[]
  baselineId: string
}

type SortKey = 'model' | 'params' | 'license' | 'ndcg10' | 'recall10' | 'mrr10' | 'p50' | 'p99' | 'qps' | 'cost' | 'abstain_f1' | 'far' | 'sig' | 'max_seq'

function abstainF1(recall: number, far: number): number {
  const precision = far < 1 ? (1 - far) : 0
  if (precision + recall === 0) return 0
  return 2 * precision * recall / (precision + recall)
}

export function Leaderboard({ data, gates, recommendedId, models, baselineId: _baselineId }: Props) {
  const [sortKey, setSortKey] = useState<SortKey>('ndcg10')
  const [sortAsc, setSortAsc] = useState(false)
  void _baselineId

  const rows = useMemo(() => {
    const r = data.map(d => {
      const m = models.find(x => x.id === d.modelId)
      const met = d.metrics
      const passesGates = met.ndcg10 >= gates.accuracy_bar && met.p99 <= gates.latency_budget_ms && met.abstain.false_answer_rate <= gates.false_answer_ceiling
      return {
        modelId: d.modelId,
        model: m?.display_name ?? d.modelId,
        params: m?.params ?? '?',
        license: m?.license ?? '?',
        deploy_path: m?.deploy_path ?? '',
        ndcg10: met.ndcg10,
        ci: met.ndcg10_ci,
        recall10: met.recall10,
        mrr10: met.mrr10,
        p50: met.p50,
        p99: met.p99,
        qps: met.throughput_qps,
        cost: met.cost_per_1k,
        abstain_f1: abstainF1(met.abstain.recall, met.abstain.false_answer_rate),
        far: met.abstain.false_answer_rate,
        sig: met.sig_vs_baseline,
        max_seq: m?.max_seq_len ?? 0,
        passesGates,
      }
    })
    r.sort((a, b) => {
      const av = a[sortKey as keyof typeof a]
      const bv = b[sortKey as keyof typeof b]
      if (typeof av === 'number' && typeof bv === 'number') return sortAsc ? av - bv : bv - av
      return sortAsc ? String(av).localeCompare(String(bv)) : String(bv).localeCompare(String(av))
    })
    return r
  }, [data, models, gates, sortKey, sortAsc])

  const handleSort = (k: SortKey) => { if (sortKey === k) setSortAsc(!sortAsc); else { setSortKey(k); setSortAsc(false) } }
  const th = (label: string, key: SortKey) => <th onClick={() => handleSort(key)}>{label}{sortKey === key ? (sortAsc ? ' ↑' : ' ↓') : ''}</th>

  return (
    <>
      <h2>Leaderboard</h2>
      <div style={{ overflowX: 'auto' }}>
        <table>
          <thead><tr>
            {th('Model', 'model')}{th('Params', 'params')}{th('License', 'license')}
            {th('nDCG@10', 'ndcg10')}{th('Recall', 'recall10')}{th('MRR', 'mrr10')}
            {th('p50', 'p50')}{th('p99', 'p99')}{th('QPS', 'qps')}{th('$/1k', 'cost')}
            {th('Abstain F1', 'abstain_f1')}{th('FAR', 'far')}{th('sig', 'sig')}{th('MaxSeq', 'max_seq')}
          </tr></thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.modelId} className={r.modelId === recommendedId ? 'recommended' : (!r.passesGates ? 'gate-fail' : '')}>
                <td>{r.model}{r.modelId === recommendedId ? ' ⭐' : ''}</td>
                <td>{r.params}</td><td>{r.license}</td>
                <td>{r.ndcg10.toFixed(3)} <span style={{ color: '#8b8fa3', fontSize: '0.65rem' }}>[{r.ci[0].toFixed(2)},{r.ci[1].toFixed(2)}]</span></td>
                <td>{r.recall10.toFixed(3)}</td><td>{r.mrr10.toFixed(3)}</td>
                <td>{r.p50.toFixed(0)}</td><td>{r.p99.toFixed(0)}</td><td>{r.qps.toFixed(0)}</td>
                <td>${r.cost.toFixed(2)}</td>
                <td>{r.abstain_f1.toFixed(3)}</td><td>{r.far.toFixed(3)}</td>
                <td>{r.sig.toFixed(3)}</td><td>{r.max_seq}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}
