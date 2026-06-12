// Sortable leaderboard table, re-anchored on ground-truth accuracy.
// Primary columns come from combo5 (top-1 acc ±CI, hard acc, MRR, pool-5 p50 latency);
// judge win-rate is kept as a secondary column. Recommended OSS row (highest hard-tier
// accuracy when combo5 is present, else best judge score) lit green.
// Accuracy columns and RAGAS column render "—" / disappear when their data is absent.

import { useMemo, useState } from 'react';
import type { LoadedData } from '../lib/useData';
import { Card, Badge, THEME } from '../lib/ui';
import { MODEL_META, colorFor } from '../types';
import {
  latencyFor,
  singleStreamQps,
  agreementVsCohere35,
  modelsByJudge,
  recommendedOssByHardAcc,
} from '../lib/derive';

interface Row {
  model: string;
  params: string;
  license: string;
  deploy: string;
  fam: 'oss' | 'cohere';
  acc: number | null;          // combo5 overall top-1 acc
  accLo: number | null;        // combo5 overall ci lo
  accHi: number | null;        // combo5 overall ci hi
  hardAcc: number | null;      // combo5 hard-tier acc
  mrr: number | null;          // combo5 overall mrr
  poolLatency: number | null;  // combo5 overall pool-5 p50 latency
  judge: number;
  agree: number;
  p50: number | null;
  p99: number | null;
  qps: number | null;
  maxCtx: number;
  ragas: number | null;
}

type SortKey =
  | 'model' | 'acc' | 'hardAcc' | 'mrr' | 'poolLatency'
  | 'judge' | 'agree' | 'p50' | 'p99' | 'qps' | 'maxCtx' | 'ragas';

const NUM = (value: number | null): string => (value === null ? '—' : `${Math.round(value)}`);
const PCT = (value: number | null): string =>
  value === null || Number.isNaN(value) ? '—' : `${(value * 100).toFixed(1)}%`;
const ACC3 = (value: number | null): string =>
  value === null || Number.isNaN(value) ? '—' : value.toFixed(3);

export default function Leaderboard({ data }: { data: LoadedData }) {
  const { judge, scored, latency, metrics, ragas, combo5 } = data;
  const hasRagas = ragas !== null;
  const hasCombo5 = combo5 !== null;

  // Recommended OSS: highest hard-tier accuracy when combo5 is present, else
  // fall back to the existing best-judge-score OSS pick.
  const recommendedOss = useMemo(() => {
    if (combo5) {
      const byHard = recommendedOssByHardAcc(combo5);
      if (byHard) {
        return byHard;
      }
    }
    const ranked = modelsByJudge(judge.model_score);
    return ranked.find((model) => MODEL_META[model]?.fam === 'oss') ?? null;
  }, [combo5, judge.model_score]);

  const rows = useMemo<Row[]>(() => {
    return Object.keys(judge.model_score).map((model) => {
      const meta = MODEL_META[model];
      const stat = latencyFor(model, scored, latency);
      const overall = combo5?.aggregate[model]?.overall ?? null;
      const hard = combo5?.aggregate[model]?.hard ?? null;
      return {
        model,
        params: meta?.params ?? 'n/d',
        license: meta?.license ?? 'n/d',
        deploy: meta?.deploy ?? 'n/d',
        fam: meta?.fam ?? 'oss',
        acc: overall?.acc ?? null,
        accLo: overall?.ci?.[0] ?? null,
        accHi: overall?.ci?.[1] ?? null,
        hardAcc: hard?.acc ?? null,
        mrr: overall?.mrr ?? null,
        poolLatency: overall?.p50_latency_ms ?? null,
        judge: judge.model_score[model],
        agree: agreementVsCohere35(model, metrics),
        p50: stat.p50,
        p99: stat.p99,
        qps: singleStreamQps(stat.p50),
        maxCtx: meta?.ctx ?? scored.models[model]?.max_context ?? 0,
        ragas: hasRagas ? ragas.aggregate[model]?.ragas_overall ?? null : null,
      };
    });
  }, [judge.model_score, scored, latency, metrics, ragas, hasRagas, combo5]);

  // Default sort: hard accuracy when combo5 present (the verdict basis), else judge.
  const [sortKey, setSortKey] = useState<SortKey>(hasCombo5 ? 'hardAcc' : 'judge');
  const [ascending, setAscending] = useState<boolean>(false);

  const sorted = useMemo(() => {
    const copy = [...rows];
    copy.sort((left, right) => {
      let comparison: number;
      if (sortKey === 'model') {
        comparison = left.model.localeCompare(right.model);
      } else {
        const leftValue = left[sortKey];
        const rightValue = right[sortKey];
        const leftNum = leftValue === null || Number.isNaN(leftValue as number) ? -Infinity : (leftValue as number);
        const rightNum = rightValue === null || Number.isNaN(rightValue as number) ? -Infinity : (rightValue as number);
        comparison = leftNum - rightNum;
      }
      return ascending ? comparison : -comparison;
    });
    return copy;
  }, [rows, sortKey, ascending]);

  function onSort(key: SortKey) {
    if (key === sortKey) {
      setAscending((prev) => !prev);
    } else {
      setSortKey(key);
      // Default direction: ascending for latency (lower better), descending otherwise.
      setAscending(key === 'p50' || key === 'p99' || key === 'poolLatency' || key === 'model');
    }
  }

  const columns: { key: SortKey; label: string; align: 'left' | 'right' }[] = [
    { key: 'model', label: 'model', align: 'left' },
  ];
  if (hasCombo5) {
    columns.push(
      { key: 'acc', label: 'top-1 acc', align: 'right' },
      { key: 'hardAcc', label: 'hard acc', align: 'right' },
      { key: 'mrr', label: 'mrr', align: 'right' },
      { key: 'poolLatency', label: 'pool-5 p50', align: 'right' },
    );
  }
  columns.push(
    { key: 'judge', label: 'judge win%', align: 'right' },
    { key: 'agree', label: 'agree·3.5', align: 'right' },
    { key: 'p50', label: 'p50 ms', align: 'right' },
    { key: 'p99', label: 'p99 ms', align: 'right' },
    { key: 'qps', label: 'qps¹', align: 'right' },
    { key: 'maxCtx', label: 'max ctx', align: 'right' },
  );
  if (hasRagas) {
    columns.push({ key: 'ragas', label: 'ragas', align: 'right' });
  }

  const sub = hasCombo5
    ? 'Re-anchored on ground-truth top-1 accuracy. Recommended OSS (best hard-tier acc) lit green.'
    : 'Click a column to sort. Recommended OSS row lit green. (Accuracy columns appear once combo5 lands.)';

  return (
    <Card title="Leaderboard" sub={sub} pad={false}>
      <div style={{ overflowX: 'auto' }}>
        <table
          style={{
            width: '100%',
            borderCollapse: 'collapse',
            fontSize: 11.5,
            minWidth: hasCombo5 ? 960 : 760,
          }}
        >
          <thead>
            <tr>
              {columns.map((col) => (
                <th
                  key={col.key}
                  onClick={() => onSort(col.key)}
                  style={{
                    textAlign: col.align,
                    padding: '8px 12px',
                    color: sortKey === col.key ? THEME.text : THEME.dim,
                    textTransform: 'uppercase',
                    letterSpacing: '0.05em',
                    fontSize: 10,
                    fontWeight: 500,
                    cursor: 'pointer',
                    userSelect: 'none',
                    borderBottom: `1px solid ${THEME.border}`,
                    whiteSpace: 'nowrap',
                  }}
                >
                  {col.label}
                  {sortKey === col.key ? (ascending ? ' ▲' : ' ▼') : ''}
                </th>
              ))}
              <th
                style={{
                  textAlign: 'left',
                  padding: '8px 12px',
                  borderBottom: `1px solid ${THEME.border}`,
                }}
              />
            </tr>
          </thead>
          <tbody>
            {sorted.map((row) => {
              const isPick = row.model === recommendedOss;
              return (
                <tr
                  key={row.model}
                  style={{
                    background: isPick ? 'rgba(63,185,80,0.07)' : 'transparent',
                    borderLeft: isPick ? `2px solid ${THEME.green}` : '2px solid transparent',
                  }}
                >
                  <td style={cell('left')}>
                    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 7 }}>
                      <span
                        style={{
                          width: 8,
                          height: 8,
                          borderRadius: 2,
                          background: colorFor(row.model),
                        }}
                      />
                      <span style={{ color: isPick ? THEME.green : THEME.text, fontWeight: isPick ? 600 : 400 }}>
                        {row.model}
                      </span>
                      <span style={{ color: THEME.dimmer }}>{row.params}</span>
                      <Badge kind={row.fam === 'oss' ? 'oss' : 'cohere'}>{row.fam}</Badge>
                    </span>
                    <div style={{ fontSize: 10, color: THEME.dimmer, marginTop: 2 }}>
                      {row.license} · {row.deploy}
                    </div>
                  </td>
                  {hasCombo5 && (
                    <>
                      <td style={cell('right', isPick ? THEME.green : THEME.text)}>
                        {ACC3(row.acc)}
                        {row.accLo !== null && row.accHi !== null && (
                          <span style={{ color: THEME.dimmer, fontSize: 10 }}>
                            {' '}±[{row.accLo.toFixed(2)}–{row.accHi.toFixed(2)}]
                          </span>
                        )}
                      </td>
                      <td style={cell('right', isPick ? THEME.green : THEME.text)}>{ACC3(row.hardAcc)}</td>
                      <td style={cell('right')}>{ACC3(row.mrr)}</td>
                      <td style={cell('right')}>{NUM(row.poolLatency)}</td>
                    </>
                  )}
                  <td style={cell('right')}>{PCT(row.judge)}</td>
                  <td style={cell('right')}>{PCT(row.agree)}</td>
                  <td style={cell('right')}>{NUM(row.p50)}</td>
                  <td style={cell('right')}>{NUM(row.p99)}</td>
                  <td style={cell('right')}>{row.qps === null ? '—' : row.qps.toFixed(1)}</td>
                  <td style={cell('right')}>{row.maxCtx.toLocaleString()}</td>
                  {hasRagas && (
                    <td style={cell('right')}>
                      {row.ragas === null ? '—' : row.ragas.toFixed(3)}
                    </td>
                  )}
                  <td style={cell('left')}>{isPick && <Badge kind="pick">pick</Badge>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div style={{ padding: '8px 14px', fontSize: 10, color: THEME.dimmer, lineHeight: 1.6 }}>
        {hasCombo5
          ? 'top-1 acc / hard acc / mrr / pool-5 p50 = ground-truth pool-of-5 eval (combo5). '
          : ''}
        ¹ qps = single-stream estimate (1000 / p50); no concurrent-throughput benchmark was run.
        Latency: OSS = warm GPU pool=10; cohere = API median (cold-start dropped). agree·3.5 = 1 − pairwise disagreement vs cohere-3.5.
      </div>
    </Card>
  );
}

function cell(align: 'left' | 'right', color: string = THEME.dim): React.CSSProperties {
  return {
    textAlign: align,
    padding: '9px 12px',
    color,
    borderBottom: `1px solid ${THEME.panelAlt}`,
    whiteSpace: 'nowrap',
  };
}
