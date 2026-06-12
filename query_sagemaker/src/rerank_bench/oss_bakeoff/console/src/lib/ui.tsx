// Dark "console" UI primitives. Monospace, dense, thin-bordered cards.
// Theme color constants are exported so charts and panels share the same palette.
// The THEME export is co-located here per the build contract; eslint.config.js
// allow-lists it for the react-refresh rule.

import type { ReactNode } from 'react';

export const THEME = {
  bg: '#0f1115',
  panel: '#161a22',
  panelAlt: '#12151c',
  border: '#262b36',
  borderBright: '#323a48',
  text: '#e6e6e6',
  dim: '#9aa4b2',
  dimmer: '#6b7484',
  green: '#3fb950',
  greenDim: '#1f6f33',
  blue: '#58a6ff',
  gray: '#6b7484',
  amber: '#d29922',
  red: '#f85149',
  accentBg: '#0d2818',
} as const;

type BadgeKind = 'oss' | 'cohere' | 'pick' | 'promoted' | 'neutral';

const BADGE_STYLES: Record<BadgeKind, { fg: string; bg: string; border: string }> = {
  oss: { fg: '#79c0ff', bg: 'rgba(56,139,253,0.12)', border: 'rgba(56,139,253,0.4)' },
  cohere: { fg: '#9aa4b2', bg: 'rgba(110,118,129,0.12)', border: 'rgba(110,118,129,0.4)' },
  pick: { fg: '#3fb950', bg: 'rgba(63,185,80,0.14)', border: 'rgba(63,185,80,0.45)' },
  promoted: { fg: '#3fb950', bg: 'rgba(63,185,80,0.14)', border: 'rgba(63,185,80,0.45)' },
  neutral: { fg: '#9aa4b2', bg: 'rgba(110,118,129,0.10)', border: 'rgba(110,118,129,0.35)' },
};

export function Badge({ kind, children }: { kind: BadgeKind; children: ReactNode }) {
  const style = BADGE_STYLES[kind];
  return (
    <span
      style={{
        display: 'inline-block',
        fontSize: 10,
        lineHeight: '14px',
        letterSpacing: '0.06em',
        textTransform: 'uppercase',
        padding: '1px 6px',
        borderRadius: 4,
        color: style.fg,
        background: style.bg,
        border: `1px solid ${style.border}`,
        whiteSpace: 'nowrap',
      }}
    >
      {children}
    </span>
  );
}

export function Pill({
  label,
  value,
  tone = 'neutral',
}: {
  label: string;
  value: ReactNode;
  tone?: 'neutral' | 'good' | 'pending';
}) {
  const valueColor =
    tone === 'good' ? THEME.green : tone === 'pending' ? THEME.amber : THEME.text;
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'baseline',
        gap: 6,
        fontSize: 11,
        padding: '2px 8px',
        borderRadius: 5,
        border: `1px solid ${THEME.border}`,
        background: THEME.panelAlt,
        whiteSpace: 'nowrap',
      }}
    >
      <span style={{ color: THEME.dimmer, textTransform: 'uppercase', letterSpacing: '0.06em' }}>
        {label}
      </span>
      <span style={{ color: valueColor }}>{value}</span>
    </span>
  );
}

export function Card({
  title,
  sub,
  right,
  children,
  pad = true,
}: {
  title?: string;
  sub?: string;
  right?: ReactNode;
  children: ReactNode;
  pad?: boolean;
}) {
  return (
    <section
      style={{
        background: THEME.panel,
        border: `1px solid ${THEME.border}`,
        borderRadius: 8,
        overflow: 'hidden',
      }}
    >
      {(title || sub || right) && (
        <header
          style={{
            display: 'flex',
            alignItems: 'flex-start',
            justifyContent: 'space-between',
            gap: 12,
            padding: '10px 14px',
            borderBottom: `1px solid ${THEME.border}`,
            background: THEME.panelAlt,
          }}
        >
          <div style={{ minWidth: 0 }}>
            {title && (
              <div
                style={{
                  fontSize: 12,
                  letterSpacing: '0.08em',
                  textTransform: 'uppercase',
                  color: THEME.text,
                }}
              >
                {title}
              </div>
            )}
            {sub && (
              <div style={{ fontSize: 11, color: THEME.dim, marginTop: 2 }}>{sub}</div>
            )}
          </div>
          {right && <div style={{ flexShrink: 0 }}>{right}</div>}
        </header>
      )}
      <div style={{ padding: pad ? 14 : 0 }}>{children}</div>
    </section>
  );
}

// Anchor + section wrapper used for nav scrolling.
export function Section({
  id,
  children,
  style,
}: {
  id: string;
  children: ReactNode;
  style?: React.CSSProperties;
}) {
  return (
    <section id={id} style={{ scrollMarginTop: 64, ...style }}>
      {children}
    </section>
  );
}

// Small monospace swatch + label for model legends.
export function ModelTag({ model, color }: { model: string; color: string }) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 11 }}>
      <span
        style={{
          width: 9,
          height: 9,
          borderRadius: 2,
          background: color,
          display: 'inline-block',
        }}
      />
      <span style={{ color: THEME.text }}>{model}</span>
    </span>
  );
}
