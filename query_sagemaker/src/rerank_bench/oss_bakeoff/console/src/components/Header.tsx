// Sticky console header: title, nav tabs (anchor links to sections), status pills.

import type { LoadedData } from '../lib/useData';
import { Pill, THEME } from '../lib/ui';

const NAV = [
  { id: 'accuracy', label: 'Accuracy' },
  { id: 'verdict', label: 'Verdict' },
  { id: 'leaderboard', label: 'Board' },
  { id: 'threed', label: '3D' },
  { id: 'distributions', label: 'Latency' },
  { id: 'headtohead', label: 'Judge' },
  { id: 'drilldown', label: 'Rationale' },
  { id: 'ragas', label: 'RAGAS' },
];

export default function Header({ data }: { data: LoadedData }) {
  const modelCount = Object.keys(data.judge.model_score).length;
  const judgedPairs = data.judge.meta.n_pairs ?? data.judge.verdicts.length;
  const ragasDone = data.ragas !== null;

  return (
    <header
      style={{
        position: 'sticky',
        top: 0,
        zIndex: 50,
        background: 'rgba(15,17,21,0.92)',
        backdropFilter: 'blur(6px)',
        borderBottom: `1px solid ${THEME.border}`,
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 16,
          flexWrap: 'wrap',
          padding: '10px 18px',
          maxWidth: 1320,
          margin: '0 auto',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
          <span
            style={{
              fontSize: 13,
              letterSpacing: '0.14em',
              textTransform: 'uppercase',
              color: THEME.text,
              fontWeight: 600,
            }}
          >
            OSS Reranker
          </span>
          <span
            style={{
              fontSize: 13,
              letterSpacing: '0.14em',
              textTransform: 'uppercase',
              color: THEME.dim,
            }}
          >
            · Bake-off
          </span>
        </div>

        <nav style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
          {NAV.map((tab) => (
            <a
              key={tab.id}
              href={`#${tab.id}`}
              style={{
                fontSize: 11,
                letterSpacing: '0.05em',
                textTransform: 'uppercase',
                color: THEME.dim,
                textDecoration: 'none',
                padding: '4px 8px',
                borderRadius: 5,
                border: `1px solid transparent`,
              }}
              onMouseEnter={(event) => {
                event.currentTarget.style.color = THEME.text;
                event.currentTarget.style.borderColor = THEME.border;
              }}
              onMouseLeave={(event) => {
                event.currentTarget.style.color = THEME.dim;
                event.currentTarget.style.borderColor = 'transparent';
              }}
            >
              {tab.label}
            </a>
          ))}
        </nav>

        <div style={{ display: 'flex', gap: 6, marginLeft: 'auto', flexWrap: 'wrap' }}>
          <Pill label="models" value={modelCount} />
          <Pill label="judged" value={judgedPairs} />
          <Pill
            label="ragas"
            value={ragasDone ? 'done' : 'pending'}
            tone={ragasDone ? 'good' : 'pending'}
          />
        </div>
      </div>
    </header>
  );
}
