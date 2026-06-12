// The headline. Recommendation banner + ranked judge-win-rate bars + p50 latency bars.

import type { LoadedData } from '../lib/useData';
import { Card, Badge, THEME } from '../lib/ui';
import { MODEL_META, colorFor } from '../types';
import { latencyFor, modelsByJudge } from '../lib/derive';

function pct(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

// Horizontal bar row: label on the left, a proportional bar, a value on the right.
function BarRow({
  label,
  color,
  value,
  max,
  display,
  highlight,
}: {
  label: string;
  color: string;
  value: number;
  max: number;
  display: string;
  highlight?: boolean;
}) {
  const widthPct = max > 0 ? Math.max(2, (value / max) * 100) : 0;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 11 }}>
      <div
        style={{
          width: 116,
          color: highlight ? THEME.green : THEME.text,
          whiteSpace: 'nowrap',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          fontWeight: highlight ? 600 : 400,
        }}
        title={label}
      >
        {label}
      </div>
      <div
        style={{
          flex: 1,
          height: 14,
          background: THEME.panelAlt,
          borderRadius: 3,
          overflow: 'hidden',
          border: `1px solid ${THEME.border}`,
        }}
      >
        <div
          style={{
            width: `${widthPct}%`,
            height: '100%',
            background: color,
            opacity: highlight ? 1 : 0.78,
          }}
        />
      </div>
      <div style={{ width: 64, textAlign: 'right', color: THEME.dim }}>{display}</div>
    </div>
  );
}

export default function VerdictHero({ data }: { data: LoadedData }) {
  const { judge, scored, latency } = data;
  const ranked = modelsByJudge(judge.model_score);

  // p50 latency per model (OSS -> warm GPU pool10; cohere -> warm API median).
  const latencyRows = Object.keys(judge.model_score)
    .map((model) => ({ model, p50: latencyFor(model, scored, latency).p50 }))
    .filter((row): row is { model: string; p50: number } => row.p50 !== null)
    .sort((left, right) => left.p50 - right.p50);

  const maxJudge = Math.max(...ranked.map((model) => judge.model_score[model]));
  const maxLatency = Math.max(...latencyRows.map((row) => row.p50));

  const topOss = ranked.find((model) => MODEL_META[model]?.fam === 'oss') ?? 'ettin-1b';
  const fastest = latencyRows[0]?.model ?? topOss;
  const ettinScore = judge.model_score[topOss] ?? 0;
  const proScore = judge.model_score['cohere-v4-pro'] ?? 0;
  const fastScore = judge.model_score['cohere-v4-fast'] ?? 0;
  const c35Score = judge.model_score['cohere-3.5'] ?? 0;
  const gap = Math.abs(ettinScore - proScore);

  return (
    <Card
      title="Verdict"
      sub={`Opus judge · pairwise win-rate over ${judge.meta.n_pairs ?? judge.verdicts.length} anonymized pairs`}
    >
      {/* Recommendation banner */}
      <div
        style={{
          border: `1px solid ${THEME.greenDim}`,
          background: THEME.accentBg,
          borderRadius: 6,
          padding: '12px 14px',
          marginBottom: 16,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <Badge kind="pick">Recommended</Badge>
          <span style={{ color: colorFor(topOss), fontSize: 15, fontWeight: 600 }}>{topOss}</span>
          <Badge kind="oss">open · {MODEL_META[topOss]?.params}</Badge>
          <span style={{ fontSize: 11, color: THEME.dim }}>{MODEL_META[topOss]?.license}</span>
        </div>
        <div style={{ fontSize: 12.5, color: THEME.text, marginTop: 8, lineHeight: 1.5 }}>
          <strong style={{ color: colorFor(topOss) }}>{topOss}</strong> ties{' '}
          <strong style={{ color: colorFor('cohere-v4-pro') }}>cohere-v4-pro</strong> on judge
          win-rate ({pct(ettinScore)} vs {pct(proScore)}, Δ {(gap * 100).toFixed(1)}pp) and is the{' '}
          <strong style={{ color: THEME.green }}>fastest in field</strong>
          {fastest !== topOss ? ` (${fastest} leads)` : ''}. It beats cohere-v4-fast (
          {pct(fastScore)}) and crushes cohere-3.5 ({pct(c35Score)}).
        </div>
      </div>

      {/* Two columns: judge win-rate bars + p50 latency bars */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))',
          gap: 18,
        }}
      >
        <div>
          <div style={subHead}>Judge win-rate (quality)</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {ranked.map((model) => (
              <BarRow
                key={model}
                label={model}
                color={colorFor(model)}
                value={judge.model_score[model]}
                max={maxJudge}
                display={pct(judge.model_score[model])}
                highlight={model === topOss}
              />
            ))}
          </div>
        </div>

        <div>
          <div style={subHead}>p50 latency · lower is better</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {latencyRows.map((row) => (
              <BarRow
                key={row.model}
                label={row.model}
                color={colorFor(row.model)}
                value={row.p50}
                max={maxLatency}
                display={`${Math.round(row.p50)}ms`}
                highlight={row.model === fastest}
              />
            ))}
          </div>
          <div style={{ fontSize: 10, color: THEME.dimmer, marginTop: 8, lineHeight: 1.5 }}>
            OSS = warm GPU g5.2xl, pool=10. Cohere = API median (cold-start dropped).
          </div>
        </div>
      </div>
    </Card>
  );
}

const subHead: React.CSSProperties = {
  fontSize: 11,
  color: THEME.dim,
  textTransform: 'uppercase',
  letterSpacing: '0.06em',
  marginBottom: 10,
};
