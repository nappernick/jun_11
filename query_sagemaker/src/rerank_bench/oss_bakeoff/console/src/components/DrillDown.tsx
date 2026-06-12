// Opus rationale cards from judge.verdicts. Filter by model pair, expandable rationale,
// winner side green. Top doc titles resolved via scored.top_id -> pools[query].

import { useMemo, useState } from 'react';
import type { LoadedData } from '../lib/useData';
import type { JudgeVerdict } from '../types';
import { Card, Badge, THEME } from '../lib/ui';
import { colorFor } from '../types';
import { topDocTitle } from '../lib/derive';

function pairKey(model_a: string, model_b: string): string {
  return [model_a, model_b].sort().join('  vs  ');
}

// One side (model A or B) of a verdict: model name, win/lose tint, its top doc title.
function Side({
  model,
  topDoc,
  isWinner,
  isTie,
}: {
  model: string;
  topDoc: string | null;
  isWinner: boolean;
  isTie: boolean;
}) {
  const borderColor = isTie ? THEME.border : isWinner ? THEME.greenDim : THEME.border;
  const bg = isWinner && !isTie ? 'rgba(63,185,80,0.07)' : THEME.panelAlt;
  return (
    <div
      style={{
        flex: 1,
        minWidth: 0,
        border: `1px solid ${borderColor}`,
        background: bg,
        borderRadius: 5,
        padding: '8px 10px',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <span style={{ width: 8, height: 8, borderRadius: 2, background: colorFor(model) }} />
        <span style={{ color: THEME.text, fontSize: 11.5 }}>{model}</span>
        {isWinner && !isTie && <Badge kind="pick">winner</Badge>}
      </div>
      <div
        style={{
          fontSize: 10.5,
          color: THEME.dim,
          marginTop: 6,
          lineHeight: 1.4,
        }}
        title={topDoc ?? undefined}
      >
        <span style={{ color: THEME.dimmer }}>top doc: </span>
        {topDoc ?? '—'}
      </div>
    </div>
  );
}

function VerdictCard({ verdict, data }: { verdict: JudgeVerdict; data: LoadedData }) {
  const [open, setOpen] = useState(false);
  const topA = topDocTitle(data.scored, data.pools, verdict.model_a, verdict.query);
  const topB = topDocTitle(data.scored, data.pools, verdict.model_b, verdict.query);
  const isTie = verdict.winner === 'tie';

  return (
    <div
      style={{
        border: `1px solid ${THEME.border}`,
        borderRadius: 6,
        background: THEME.panel,
        overflow: 'hidden',
      }}
    >
      <button
        onClick={() => setOpen((prev) => !prev)}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          width: '100%',
          padding: '8px 12px',
          background: 'transparent',
          border: 'none',
          cursor: 'pointer',
          textAlign: 'left',
          color: THEME.text,
          fontFamily: 'inherit',
        }}
      >
        <span style={{ color: THEME.dim, fontSize: 11, width: 12 }}>{open ? '▾' : '▸'}</span>
        <span style={{ color: THEME.dimmer, fontSize: 10 }}>{verdict.id}</span>
        <span
          style={{
            flex: 1,
            minWidth: 0,
            fontSize: 11.5,
            color: THEME.text,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {verdict.query}
        </span>
        <span style={{ fontSize: 10, color: THEME.dim }}>conf {verdict.confidence.toFixed(2)}</span>
        {verdict.consistent ? (
          <Badge kind="neutral">consistent</Badge>
        ) : (
          <Badge kind="cohere">flipped</Badge>
        )}
      </button>

      {open && (
        <div style={{ padding: '0 12px 12px', borderTop: `1px solid ${THEME.panelAlt}` }}>
          <div style={{ display: 'flex', gap: 10, marginTop: 10 }}>
            <Side model={verdict.model_a} topDoc={topA} isWinner={verdict.winner === 'a'} isTie={isTie} />
            <div
              style={{
                alignSelf: 'center',
                color: THEME.dimmer,
                fontSize: 10,
                textTransform: 'uppercase',
              }}
            >
              {isTie ? 'tie' : 'vs'}
            </div>
            <Side model={verdict.model_b} topDoc={topB} isWinner={verdict.winner === 'b'} isTie={isTie} />
          </div>
          <div
            style={{
              fontSize: 11.5,
              color: THEME.text,
              lineHeight: 1.55,
              marginTop: 10,
              padding: '8px 10px',
              background: THEME.panelAlt,
              borderRadius: 5,
              border: `1px solid ${THEME.border}`,
            }}
          >
            {verdict.rationale}
          </div>
        </div>
      )}
    </div>
  );
}

export default function DrillDown({ data }: { data: LoadedData }) {
  const { judge } = data;

  // Distinct model pairs present in the verdicts.
  const pairs = useMemo(() => {
    const set = new Set<string>();
    for (const verdict of judge.verdicts) {
      set.add(pairKey(verdict.model_a, verdict.model_b));
    }
    return Array.from(set).sort();
  }, [judge.verdicts]);

  const [filter, setFilter] = useState<string>('all');

  const visible = useMemo(() => {
    if (filter === 'all') {
      return judge.verdicts;
    }
    return judge.verdicts.filter((verdict) => pairKey(verdict.model_a, verdict.model_b) === filter);
  }, [judge.verdicts, filter]);

  return (
    <Card
      title="Rationale drill-down"
      sub={`${judge.meta.judge_model ?? 'Opus'} pairwise verdicts · expand for reasoning + top docs`}
      right={
        <select
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          style={{
            background: THEME.panelAlt,
            color: THEME.text,
            border: `1px solid ${THEME.border}`,
            borderRadius: 5,
            fontSize: 11,
            fontFamily: 'inherit',
            padding: '4px 8px',
            maxWidth: 280,
          }}
        >
          <option value="all">all pairs ({judge.verdicts.length})</option>
          {pairs.map((pair) => (
            <option key={pair} value={pair}>
              {pair}
            </option>
          ))}
        </select>
      }
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 560, overflowY: 'auto' }}>
        {visible.map((verdict) => (
          <VerdictCard key={verdict.id} verdict={verdict} data={data} />
        ))}
        {visible.length === 0 && (
          <div style={{ color: THEME.dim, fontSize: 11, padding: 12 }}>No verdicts for this pair.</div>
        )}
      </div>
    </Card>
  );
}
