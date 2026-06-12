// Single-page console. Loads all data once, composes the header + anchored sections.
// ThreeDView is owned by another agent and imported from ./components/ThreeDView.

import type { ReactNode } from 'react';
import { useData } from './lib/useData';
import { THEME, Section } from './lib/ui';
import Header from './components/Header';
import AccuracySection from './components/AccuracySection';
import VerdictHero from './components/VerdictHero';
import Leaderboard from './components/Leaderboard';
import SessionTrajectory from './components/SessionTrajectory';
import ThreeDView from './components/ThreeDView';
import Distributions from './components/Distributions';
import HeadToHead from './components/HeadToHead';
import DrillDown from './components/DrillDown';
import RagasPanel from './components/RagasPanel';

function Centered({ children }: { children: ReactNode }) {
  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        color: THEME.dim,
        fontSize: 13,
        letterSpacing: '0.06em',
      }}
    >
      {children}
    </div>
  );
}

export default function App() {
  const { data, loading, error } = useData();

  if (loading) {
    return <Centered>loading bake-off artifacts…</Centered>;
  }
  if (error || !data) {
    return (
      <Centered>
        <span style={{ color: THEME.red }}>failed to load data: {error ?? 'unknown error'}</span>
      </Centered>
    );
  }

  return (
    <div style={{ minHeight: '100vh', background: THEME.bg, color: THEME.text }}>
      <Header data={data} />
      <main
        style={{
          maxWidth: 1320,
          margin: '0 auto',
          padding: '18px',
          display: 'flex',
          flexDirection: 'column',
          gap: 18,
        }}
      >
        <Section id="accuracy">
          <AccuracySection data={data} />
        </Section>
        <Section id="verdict">
          <VerdictHero data={data} />
        </Section>
        <Section id="leaderboard">
          <Leaderboard data={data} />
        </Section>
        <Section id="threed">
          <SessionTrajectory data={data} />
        </Section>
        <Section id="fieldmap">
          <ThreeDView data={data} />
        </Section>
        <Section id="distributions">
          <Distributions data={data} />
        </Section>
        <Section id="headtohead">
          <HeadToHead data={data} />
        </Section>
        <Section id="drilldown">
          <DrillDown data={data} />
        </Section>
        <Section id="ragas">
          <RagasPanel data={data} />
        </Section>
        <footer
          style={{
            color: THEME.dimmer,
            fontSize: 10,
            textAlign: 'center',
            padding: '12px 0 24px',
            letterSpacing: '0.04em',
          }}
        >
          OSS Reranker Bake-off · {data.judge.meta.n_pairs ?? data.judge.verdicts.length} judged
          pairs · {Object.keys(data.judge.model_score).length} models
        </footer>
      </main>
    </div>
  );
}
