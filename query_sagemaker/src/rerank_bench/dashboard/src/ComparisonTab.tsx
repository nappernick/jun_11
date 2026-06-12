/**
 * Comparison Tab — side-by-side Cohere Rerank 3.5 vs 4 Pro.
 *
 * Scores the SAME retrieved candidates through both models, so every panel is
 * apples-to-apples. Focused on the four things that matter here:
 *   • how fast      — latency p50/p90 per pool, and the latency trend
 *   • how confident — relevance-score distribution per pool
 *   • how well it handles the doc set — score separation (rank-1 minus rank-last):
 *                     a confident reranker pushes the best doc well above the rest
 *   • how it trends — latency and median top-1 score vs pool size, both models overlaid
 *   • agreement     — do the two models pick the same top-1 doc? (label-free accuracy proxy)
 *
 * Deliberately NOT included (dropped per request): rank-position spread,
 * pairwise dominance, document-personality win-rate.
 */
import { useMemo } from "react";
import type { Data as PlotlyData } from "plotly.js";
import PlotlyChart from "./PlotlyChart";
import type { BenchRow, Dataset } from "./types";
import { modelsInDataset, p50, percentile } from "./dataUtils";
import { ax } from "./plotUtils";

const MODEL_COLOR: Record<string, string> = {
  "cohere-3.5": "#58a6ff",   // blue
  "cohere-4-pro": "#3fb950", // green
};
const FALLBACK = ["#d29922", "#bc8cff", "#f78166", "#79c0ff"];
const colorFor = (m: string, i: number) => MODEL_COLOR[m] ?? FALLBACK[i % FALLBACK.length];

/** Rank-1 minus rank-last for a single call: how far the model spreads the set. */
const separation = (scores: number[]) =>
  scores.length > 1 ? scores[0] - scores[scores.length - 1] : 0;

interface Props {
  dataset: Dataset;
}

export default function ComparisonTab({ dataset }: Props) {
  const { rows, meta } = dataset;

  const models = useMemo(() => modelsInDataset(dataset), [dataset]);
  const pools = useMemo(
    () => [...new Set(rows.map((r) => r.pool))].sort((a, b) => a - b),
    [rows]
  );
  const queries = useMemo(
    () => meta?.queries ?? [...new Set(rows.map((r) => r.query))],
    [rows, meta]
  );
  const maxPool = pools[pools.length - 1] ?? 0;
  const byModel = useMemo(() => {
    const m: Record<string, BenchRow[]> = {};
    models.forEach((label) => (m[label] = rows.filter((r) => r.model === label)));
    return m;
  }, [rows, models]);

  // --- trend overlays (both models on one chart) ---------------------------
  const latencyTrend = useMemo((): PlotlyData[] =>
    models.map((label, i) => ({
      x: pools.map(String),
      y: pools.map((p) => p50(byModel[label].filter((r) => r.pool === p).map((r) => r.latency_ms))),
      mode: "lines+markers" as const,
      type: "scatter" as const,
      name: label,
      marker: { size: 9, color: colorFor(label, i) },
      line: { color: colorFor(label, i), width: 2 },
    })), [models, pools, byModel]);

  const scoreTrend = useMemo((): PlotlyData[] =>
    models.map((label, i) => ({
      x: pools.map(String),
      y: pools.map((p) => {
        const v = byModel[label].filter((r) => r.pool === p).map((r) => r.top_score);
        return v.length ? p50(v) : null;
      }),
      mode: "lines+markers" as const,
      type: "scatter" as const,
      name: label,
      marker: { size: 9, color: colorFor(label, i) },
      line: { color: colorFor(label, i), width: 2 },
    })), [models, pools, byModel]);

  // --- per-model panels (latency / score dist / separation) ----------------
  const latencyBars = (label: string, i: number): PlotlyData[] => [
    {
      x: pools.map(String),
      y: pools.map((p) => p50(byModel[label].filter((r) => r.pool === p).map((r) => r.latency_ms))),
      type: "bar" as const, name: "p50", marker: { color: colorFor(label, i) },
    },
    {
      x: pools.map(String),
      y: pools.map((p) => percentile(byModel[label].filter((r) => r.pool === p).map((r) => r.latency_ms), 90)),
      type: "bar" as const, name: "p90", marker: { color: "#6a737d" },
    },
  ];

  const scoreBoxes = (label: string, i: number): PlotlyData[] =>
    pools.map((p) => ({
      y: byModel[label].filter((r) => r.pool === p).flatMap((r) => r.scores),
      type: "box" as const, name: `pool ${p}`, boxpoints: "outliers" as const,
      marker: { size: 3, color: colorFor(label, i) }, line: { color: colorFor(label, i) },
    }));

  const separationBoxes = (label: string, i: number): PlotlyData[] =>
    pools.map((p) => ({
      y: byModel[label].filter((r) => r.pool === p).map((r) => separation(r.scores)),
      type: "box" as const, name: `pool ${p}`, boxpoints: "outliers" as const,
      marker: { size: 3, color: colorFor(label, i) }, line: { color: colorFor(label, i) },
    }));

  // --- per-query head-to-head deltas (only meaningful for exactly 2 models) -
  const twoModels = models.length === 2;
  const [mA, mB] = models;
  const deltaRows = useMemo(() => {
    if (!twoModels) return [];
    return queries.map((q) => {
      const a = rows.find((r) => r.query === q && r.model === mA && r.pool === maxPool);
      const b = rows.find((r) => r.query === q && r.model === mB && r.pool === maxPool);
      return {
        query: q,
        aScore: a?.top_score ?? null, bScore: b?.top_score ?? null,
        aLat: a?.latency_ms ?? null, bLat: b?.latency_ms ?? null,
        agree: a && b ? a.top_id === b.top_id : null,
      };
    });
  }, [twoModels, queries, rows, mA, mB, maxPool]);

  const agreement = useMemo(() => {
    const pairs = deltaRows.filter((d) => d.agree !== null);
    const agreed = pairs.filter((d) => d.agree).length;
    return pairs.length ? { agreed, total: pairs.length, pct: agreed / pairs.length } : null;
  }, [deltaRows]);

  const num = (v: number | null, d = 3) => (v == null ? "—" : v.toFixed(d));
  const deltaCell = (a: number | null, b: number | null, lowerIsBetter = false) => {
    if (a == null || b == null) return <span className="mono small">—</span>;
    const d = b - a;
    const good = lowerIsBetter ? d < 0 : d > 0;
    const cls = Math.abs(d) < 1e-9 ? "" : good ? "green" : "red";
    return <span className={`mono small ${cls ? "badge " + cls : ""}`}>{d >= 0 ? "+" : ""}{d.toFixed(lowerIsBetter ? 0 : 3)}</span>;
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {/* Model legend + run summary */}
      <div className="controls-bar">
        {models.map((m, i) => (
          <span key={m} className="stat-pill" style={{ borderColor: colorFor(m, i), color: colorFor(m, i) }}>
            ● {m} — {byModel[m].length.toLocaleString()} calls
          </span>
        ))}
        <span className="stat-pill">{pools.length} pool size{pools.length !== 1 ? "s" : ""}</span>
        {agreement && (
          <span className="stat-pill">
            top-1 agreement {agreement.agreed}/{agreement.total} ({(agreement.pct * 100).toFixed(0)}%)
          </span>
        )}
      </div>

      {/* Missing / errored model notice */}
      {(!twoModels || (meta?.models_errored && Object.keys(meta.models_errored).length > 0)) && (
        <div className="card alert-card">
          <div className="card-title" style={{ color: "#d29922" }}>⚠ Single-model or partial run</div>
          <div className="card-sub">
            {models.length < 2
              ? "Only one model is present. Run `python rerank_bench.py --models 3.5 4pro` (with the 4 Pro endpoint deployed) for a full side-by-side."
              : "Both models present."}
          </div>
          {meta?.models_errored && Object.entries(meta.models_errored).map(([m, msg]) => (
            <div key={m} className="limit-row">
              <span className="badge red">{m} skipped</span>
              <span className="mono small">{msg}</span>
            </div>
          ))}
        </div>
      )}

      {/* Trend overlays — both models, how it trends as the pool grows */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        <div className="card">
          <div className="card-title">Latency trend (p50) vs pool size</div>
          <div className="card-sub">Lower is faster. Diverging lines = the models scale differently with more candidates.</div>
          <PlotlyChart data={latencyTrend} layout={{ xaxis: ax("pool size"), yaxis: ax("p50 latency (ms)") }} />
        </div>
        <div className="card">
          <div className="card-title">Top-1 score trend (median) vs pool size</div>
          <div className="card-sub">Median confidence in the best doc as the candidate set grows.</div>
          <PlotlyChart data={scoreTrend} layout={{ xaxis: ax("pool size"), yaxis: ax("median top-1 score", { range: [0, 1] }) }} />
        </div>
      </div>

      {/* Per-metric left/right columns: one column per model */}
      <SectionRow title="Latency by pool size (p50 / p90)"
        sub="How fast each model is at each candidate-pool size.">
        {models.map((m, i) => (
          <ModelPanel key={m} label={m} color={colorFor(m, i)}>
            <PlotlyChart data={latencyBars(m, i)} layout={{ barmode: "group", xaxis: ax("pool size"), yaxis: ax("ms") }} />
          </ModelPanel>
        ))}
      </SectionRow>

      <SectionRow title="Relevance-score distribution by pool size"
        sub="All per-position scores across every call. Higher + tighter near the top = more confident.">
        {models.map((m, i) => (
          <ModelPanel key={m} label={m} color={colorFor(m, i)}>
            <PlotlyChart data={scoreBoxes(m, i)} layout={{ showlegend: false, yaxis: ax("relevanceScore", { range: [0, 1] }) }} />
          </ModelPanel>
        ))}
      </SectionRow>

      <SectionRow title="Score separation (rank-1 − rank-last) by pool size"
        sub="How decisively the model pushes the best doc above the rest. Higher = handles the doc set with more discrimination; near-zero = score compression / confusion.">
        {models.map((m, i) => (
          <ModelPanel key={m} label={m} color={colorFor(m, i)}>
            <PlotlyChart data={separationBoxes(m, i)} layout={{ showlegend: false, yaxis: ax("rank-1 − rank-last") }} />
          </ModelPanel>
        ))}
      </SectionRow>

      {/* Per-query head-to-head deltas */}
      {twoModels && (
        <div className="card">
          <div className="card-title">Per-query head-to-head (pool = {maxPool})</div>
          <div className="card-sub">
            Δ columns are {mB} minus {mA}. Score Δ green = {mB} more confident; latency Δ green = {mB} faster.
            "Agree" = both models chose the same top-1 document.
          </div>
          <table className="data-table" style={{ marginTop: 8 }}>
            <thead>
              <tr>
                <th>Query</th>
                <th>{mA} score</th><th>{mB} score</th><th>Δ score</th>
                <th>{mA} ms</th><th>{mB} ms</th><th>Δ ms</th>
                <th>Top-1</th>
              </tr>
            </thead>
            <tbody>
              {deltaRows.map((d) => (
                <tr key={d.query}>
                  <td>{d.query.slice(0, 46)}</td>
                  <td className="mono small">{num(d.aScore)}</td>
                  <td className="mono small">{num(d.bScore)}</td>
                  <td>{deltaCell(d.aScore, d.bScore)}</td>
                  <td className="mono small">{num(d.aLat, 0)}</td>
                  <td className="mono small">{num(d.bLat, 0)}</td>
                  <td>{deltaCell(d.aLat, d.bLat, true)}</td>
                  <td>
                    {d.agree == null
                      ? <span className="mono small">—</span>
                      : <span className={`badge ${d.agree ? "green" : "yellow"}`}>{d.agree ? "agree" : "differ"}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Run meta */}
      {meta && (
        <div className="card">
          <div className="card-title">Run metadata</div>
          <div className="meta-grid">
            <span className="meta-key">Generated</span><span>{meta.generated_at || "—"}</span>
            <span className="meta-key">Models</span><span>{(meta.models ?? models).join(" · ")}</span>
            <span className="meta-key">Pools</span><span>{meta.pools?.join(", ") ?? `k=${meta.combo_k}`}</span>
            <span className="meta-key">Queries</span><span>{meta.queries.length}</span>
            <span className="meta-key">Records</span><span>{meta.record_count.toLocaleString()}</span>
            {meta.dropped_chunks.length > 0 && (
              <>
                <span className="meta-key" style={{ color: "#f78166" }}>Dropped chunks</span>
                <span style={{ color: "#f78166" }}>{meta.dropped_chunks.length} exceeded {meta.max_doc_chars.toLocaleString()} chars</span>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/** A labelled metric section whose children are laid out as equal columns. */
function SectionRow({ title, sub, children }: { title: string; sub: string; children: React.ReactNode }) {
  const cols = Array.isArray(children) ? children.length : 1;
  return (
    <div className="card">
      <div className="card-title">{title}</div>
      <div className="card-sub">{sub}</div>
      <div style={{ display: "grid", gridTemplateColumns: `repeat(${cols}, 1fr)`, gap: 8 }}>
        {children}
      </div>
    </div>
  );
}

function ModelPanel({ label, color, children }: { label: string; color: string; children: React.ReactNode }) {
  return (
    <div>
      <div style={{ fontSize: 12, fontWeight: 600, color, marginBottom: 4 }}>● {label}</div>
      {children}
    </div>
  );
}
