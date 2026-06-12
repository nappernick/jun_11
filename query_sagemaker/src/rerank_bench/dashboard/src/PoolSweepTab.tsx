/**
 * Pool Sweep Tab — for runs produced by:
 *   python rerank_bench.py
 *   python rerank_bench.py --pools 5 10 20 40
 *
 * Shows:
 *   1. Efficiency frontier — latency vs quality per query as pool grows
 *   2. Latency p50/p90 bar chart by pool size
 *   3. Relevance score distribution (box) by pool size
 *   4. Top-1 score per query at max pool
 *   5. Top-1 stability table
 *   6. Variable correlation heatmap
 */
import { useMemo } from "react";
import type { Data as PlotlyData } from "plotly.js";
import PlotlyChart from "./PlotlyChart";
import type { Dataset } from "./types";
import { mean, p50, percentile } from "./dataUtils";
import { ax } from "./plotUtils";

const COLORS = [
  "#58a6ff", "#3fb950", "#d29922", "#f78166",
  "#bc8cff", "#39d353", "#ffa657", "#79c0ff",
];

interface Props {
  dataset: Dataset;
}

export default function PoolSweepTab({ dataset }: Props) {
  const { rows, meta } = dataset;

  const queries = useMemo(() => [...new Set(rows.map((r) => r.query))], [rows]);
  const pools = useMemo(
    () => [...new Set(rows.map((r) => r.pool))].sort((a, b) => a - b),
    [rows]
  );

  // 1. Efficiency frontier: latency (x) vs top_score (y), one trace per query
  const frontierData = useMemo((): PlotlyData[] =>
    queries.map((q, i) => {
      const qrows = rows.filter((r) => r.query === q).sort((a, b) => a.pool - b.pool);
      return {
        x: qrows.map((r) => r.latency_ms),
        y: qrows.map((r) => r.top_score),
        text: qrows.map((r) => `pool=${r.pool}`),
        mode: "lines+markers" as const,
        type: "scatter" as const,
        name: q.slice(0, 28),
        marker: { size: 10, color: COLORS[i % COLORS.length] },
        line: { color: COLORS[i % COLORS.length] },
      };
    }), [rows, queries]);

  // 2. Latency p50/p90 bars
  const latencyData = useMemo((): PlotlyData[] => {
    const p50s = pools.map((p) => {
      const lats = rows.filter((r) => r.pool === p).map((r) => r.latency_ms);
      return p50(lats);
    });
    const p90s = pools.map((p) => {
      const lats = rows.filter((r) => r.pool === p).map((r) => r.latency_ms);
      return percentile(lats, 90);
    });
    return [
      { x: pools.map(String), y: p50s, type: "bar" as const, name: "p50", marker: { color: "#58a6ff" } },
      { x: pools.map(String), y: p90s, type: "bar" as const, name: "p90", marker: { color: "#d29922" } },
    ];
  }, [rows, pools]);

  // 3. Score distribution boxes
  const scoreDistData = useMemo((): PlotlyData[] =>
    pools.map((p, i) => ({
      y: rows.filter((r) => r.pool === p).flatMap((r) => r.scores),
      type: "box" as const,
      name: `pool ${p}`,
      boxpoints: "all" as const,
      jitter: 0.4,
      marker: { size: 3, color: COLORS[i % COLORS.length] },
      line: { color: COLORS[i % COLORS.length] },
    })), [rows, pools]);

  // 4. Top-1 score per query at max pool
  const maxPool = pools[pools.length - 1] ?? 0;
  const topScoreData = useMemo((): PlotlyData[] => {
    const qScores = queries.map((q) => {
      const r = rows.find((r) => r.query === q && r.pool === maxPool);
      return r?.top_score ?? 0;
    });
    return [{
      x: queries.map((q) => q.slice(0, 22)),
      y: qScores,
      type: "bar" as const,
      marker: { color: qScores.map((s) => s > 0.7 ? "#3fb950" : s > 0.5 ? "#d29922" : "#f78166") },
    }];
  }, [rows, queries, maxPool]);

  // 5. Stability data for table
  const stabilityRows = useMemo(() =>
    queries.map((q) => {
      const tops = rows.filter((r) => r.query === q).map((r) => r.top_id);
      const unique = new Set(tops);
      return { query: q, stable: unique.size === 1, topIds: [...unique] };
    }), [rows, queries]);

  // 6. Correlation heatmap
  const NUMERIC_FIELDS = ["pool", "latency_ms", "top_score", "mean_score", "score_spread"] as const;
  type NF = typeof NUMERIC_FIELDS[number];
  const enriched = useMemo(() =>
    rows.map((r) => ({
      ...r,
      mean_score: mean(r.scores),
      score_spread: r.scores.length > 1 ? Math.max(...r.scores) - Math.min(...r.scores) : 0,
    })), [rows]);

  const corrData = useMemo((): PlotlyData[] => {
    function pearson(a: NF, b: NF) {
      const xs = enriched.map((r) => r[a] as number);
      const ys = enriched.map((r) => r[b] as number);
      const n = xs.length;
      if (!n) return 0;
      const mx = xs.reduce((p, c) => p + c, 0) / n;
      const my = ys.reduce((p, c) => p + c, 0) / n;
      let num = 0, dx = 0, dy = 0;
      for (let i = 0; i < n; i++) {
        const da = xs[i] - mx, db = ys[i] - my;
        num += da * db; dx += da * da; dy += db * db;
      }
      return dx && dy ? num / Math.sqrt(dx * dy) : 0;
    }
    const z = NUMERIC_FIELDS.map((a) =>
      NUMERIC_FIELDS.map((b) => +pearson(a, b).toFixed(2))
    );
    return [{
      z,
      x: [...NUMERIC_FIELDS],
      y: [...NUMERIC_FIELDS],
      type: "heatmap" as const,
      zmin: -1, zmax: 1,
      colorscale: "RdBu" as const,
      reversescale: true,
      texttemplate: "%{z}",
      textfont: { size: 10, color: "#fff" },
    }];
  }, [enriched]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {/* Efficiency frontier */}
      <div className="card">
        <div className="card-title">Efficiency frontier — latency vs quality as pool grows</div>
        <div className="card-sub">Each line is a query. Hover to see pool size. Upper-left = better (lower latency, higher quality).</div>
        <PlotlyChart
          data={frontierData}
          layout={{
            xaxis: ax("latency (ms)"),
            yaxis: ax("top-1 relevance score"),
          }}
        />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        {/* Latency bars */}
        <div className="card">
          <div className="card-title">Latency by pool size</div>
          <PlotlyChart
            data={latencyData}
            layout={{
              barmode: "group",
              xaxis: ax("pool size"),
              yaxis: ax("ms"),
            }}
          />
        </div>

        {/* Score distribution */}
        <div className="card">
          <div className="card-title">Relevance score distribution by pool size</div>
          <div className="card-sub">All per-position scores across every call at that pool size.</div>
          <PlotlyChart
            data={scoreDistData}
            layout={{ yaxis: ax("relevanceScore") }}
          />
        </div>

        {/* Top-1 per query */}
        <div className="card">
          <div className="card-title">Top-1 relevance score per query (pool={maxPool})</div>
          <div className="card-sub">Green &gt;0.70 · yellow &gt;0.50 · red below.</div>
          <PlotlyChart
            data={topScoreData}
            layout={{
              xaxis: { tickangle: -35, color: "#9aa4b2" },
              yaxis: ax("relevanceScore", { range: [0, 1] }),
            }}
          />
        </div>

        {/* Correlation */}
        <div className="card">
          <div className="card-title">Variable correlation (Pearson r)</div>
          <div className="card-sub">Does latency rise with pool? Does quality follow latency?</div>
          <PlotlyChart
            data={corrData}
            layout={{
              margin: { t: 20, r: 16, b: 80, l: 100 },
              xaxis: { tickangle: -30, color: "#9aa4b2" },
              yaxis: { color: "#9aa4b2" },
            }}
          />
        </div>
      </div>

      {/* Stability table */}
      <div className="card">
        <div className="card-title">Top-1 stability across pool sizes</div>
        <table className="data-table">
          <thead>
            <tr><th>Query</th><th>Stable?</th><th>Unique top-1 docs</th></tr>
          </thead>
          <tbody>
            {stabilityRows.map((s) => (
              <tr key={s.query}>
                <td>{s.query}</td>
                <td>
                  <span className={s.stable ? "badge green" : "badge yellow"}>
                    {s.stable ? "STABLE" : "SHIFTS"}
                  </span>
                </td>
                <td className="mono small">{s.topIds.join(", ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Run meta */}
      {meta && (
        <div className="card">
          <div className="card-title">Run metadata</div>
          <div className="meta-grid">
            <span className="meta-key">Generated</span>
            <span>{meta.generated_at || "—"}</span>
            <span className="meta-key">Queries</span>
            <span>{meta.queries.length}</span>
            <span className="meta-key">Pools</span>
            <span>{meta.pools?.join(", ") ?? "—"}</span>
            <span className="meta-key">Records</span>
            <span>{meta.record_count}</span>
            {meta.dropped_chunks.length > 0 && (
              <>
                <span className="meta-key" style={{ color: "#f78166" }}>Dropped chunks</span>
                <span style={{ color: "#f78166" }}>{meta.dropped_chunks.length} chunk(s) exceeded {meta.max_doc_chars.toLocaleString()} char limit</span>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
