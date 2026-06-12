/**
 * EvalMetrics — the Metrics tab, rebuilt as the REAL eval run surface.
 *
 * The old synthetic catalog / weights / ragas-prompt-manager / on-demand controls
 * are gone. This tab now does ONE thing: launch a real evaluation that runs each
 * prompt file in data/prompts (a SERIES) over N queries from queries.jsonl through
 * the LIVE stack (AOSS retrieve → real model → Opus judge), and shows it land. The
 * resulting points are visualized on the Eval 3D / Eval 2D tabs (time × quality ×
 * latency, one colour per prompt).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { JSX } from "react";
import {
  fetchRealEvalPrompts,
  fetchRealEvalStatus,
  startRealEval,
  stopRealEval,
  wipeEvalData,
  ApiError,
  type RealEvalStatus,
  type RealEvalSeries,
} from "../api/client";
import type { EvalInstance } from "../api/types";
import type { EvalStreamState } from "../api/useEvalStream";

export interface EvalMetricsProps {
  readonly stream: EvalStreamState;
}

const QUERY_COUNTS = [100, 200, 500, 1000] as const;
type QueryCount = (typeof QUERY_COUNTS)[number];
const JUDGE_DIMS = ["judge_faithfulness", "judge_correctness", "judge_completeness"];

/** Mean of this instance's judge triad (the quality axis), or null if unjudged. */
function instanceQuality(inst: EvalInstance): number | null {
  const vals: number[] = [];
  for (const dim of JUDGE_DIMS) {
    const v = inst.ragas?.[dim]?.value;
    if (typeof v === "number") vals.push(v);
  }
  return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
}

interface SeriesRollup {
  readonly key: string;
  readonly n: number;
  readonly meanQuality: number | null;
  readonly meanLatencyMs: number | null;
}

export function EvalMetrics({ stream }: EvalMetricsProps): JSX.Element {
  const [series, setSeries] = useState<readonly RealEvalSeries[]>([]);
  const [promptDir, setPromptDir] = useState<string>("");
  const [queryCount, setQueryCount] = useState<QueryCount>(100);
  const [status, setStatus] = useState<RealEvalStatus>({ status: "idle" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const pollRef = useRef<number | null>(null);

  const running = status.status === "running";

  // Load the prompt series once.
  useEffect(() => {
    void (async () => {
      try {
        const res = await fetchRealEvalPrompts();
        setSeries(res.series);
        setPromptDir(res.prompt_dir);
        if (res.error) setError(res.error);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    })();
  }, []);

  // Poll the real-run status while a run is active (and once on mount).
  const poll = useCallback(async () => {
    try {
      setStatus(await fetchRealEvalStatus());
    } catch {
      /* transient; keep last status */
    }
  }, []);
  useEffect(() => {
    void poll();
    pollRef.current = window.setInterval(() => void poll(), 1500);
    return () => {
      if (pollRef.current != null) window.clearInterval(pollRef.current);
    };
  }, [poll]);

  // Per-series rollup from the live instance stream (what's actually recorded).
  const rollups = useMemo<SeriesRollup[]>(() => {
    const byKey = new Map<string, { q: number[]; lat: number[]; n: number }>();
    for (const inst of stream.instances.values()) {
      const k = inst.agent_id;
      const bucket = byKey.get(k) ?? { q: [], lat: [], n: 0 };
      bucket.n += 1;
      const q = instanceQuality(inst);
      if (q != null) bucket.q.push(q);
      if (typeof inst.latency_ms === "number") bucket.lat.push(inst.latency_ms);
      byKey.set(k, bucket);
    }
    const mean = (xs: number[]): number | null =>
      xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
    return [...byKey.entries()]
      .map(([key, b]) => ({ key, n: b.n, meanQuality: mean(b.q), meanLatencyMs: mean(b.lat) }))
      .sort((a, b) => a.key.localeCompare(b.key));
  }, [stream.instances]);

  const totalRecorded = useMemo(() => stream.instances.size, [stream.instances]);

  const onStart = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      setStatus(await startRealEval(queryCount));
      void poll();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setError("A real eval run is already running.");
      else setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [queryCount, poll]);

  const onStop = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      setStatus(await stopRealEval());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, []);

  const onWipe = useCallback(async () => {
    if (!window.confirm(`Wipe all ${totalRecorded} recorded metric points? This cannot be undone.`))
      return;
    setBusy(true);
    setError(null);
    try {
      await wipeEvalData();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setError("Stop the running eval before wiping.");
      else setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [totalRecorded]);

  const progress = useMemo(() => status.progress ?? null, [status.progress]);
  const pct = useMemo(
    () => (progress && progress.total ? Math.round((progress.done / progress.total) * 100) : 0),
    [progress],
  );
  const totalToRun = useMemo(() => series.length * queryCount, [series.length, queryCount]);

  return (
    <div className="view">
      <div className="shead">
        <h2>Eval · Real Run</h2>
        <span className="sub">
          each prompt file = a series · {queryCount} queries · live AOSS → model → Opus judge
        </span>
        <span className="rule" />
        <span className={`pill state ${status.status}`}>{status.status}</span>
      </div>

      {error && (
        <div className="banner" style={{ marginBottom: 12 }}>
          {error}
        </div>
      )}

      {/* Run configuration + launch */}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-title">Configure &amp; run</div>

        <div className="cp-field" style={{ marginBottom: 12 }}>
          <span>queries (sampled from queries.jsonl)</span>
          <div className="seg">
            {QUERY_COUNTS.map((qc) => (
              <button
                key={qc}
                className={`seg-btn ${queryCount === qc ? "on" : ""}`}
                disabled={running}
                onClick={() => setQueryCount(qc)}
              >
                {qc}
              </button>
            ))}
          </div>
        </div>

        <div className="cp-field" style={{ marginBottom: 12 }}>
          <span>
            prompt series — {series.length} file(s) in <code>{promptDir || "data/prompts"}</code>
          </span>
          <div className="chips">
            {series.length === 0 && <span className="muted">no .txt prompt files found</span>}
            {series.map((s) => (
              <span key={s.key} className="chip" title={`${s.chars} chars`}>
                {s.key}
              </span>
            ))}
          </div>
        </div>

        <div className="cp-row" style={{ alignItems: "center", gap: 12 }}>
          <button
            className="btn primary"
            disabled={busy || running || series.length === 0}
            onClick={() => void onStart()}
            title={
              totalRecorded > 0
                ? "Resume: skips pairs already recorded and runs the remainder up to the target"
                : "Run every prompt over the selected queries"
            }
          >
            {running
              ? "Running…"
              : totalRecorded > 0
                ? `Resume · up to ${totalToRun}`
                : `Start real run · ${totalToRun} executions`}
          </button>
          <button className="btn" disabled={!running || busy} onClick={() => void onStop()}>
            Stop
          </button>
          <button className="btn danger" disabled={busy || running} onClick={() => void onWipe()}>
            Wipe metric data
          </button>
          <span className="muted" style={{ marginLeft: "auto" }}>
            {totalRecorded} point(s) recorded
          </span>
        </div>

        {running && (
          <div style={{ marginTop: 14 }}>
            <div className="progress-track">
              <div className="progress-fill" style={{ width: `${pct}%` }} />
            </div>
            <div className="cp-hint muted" style={{ marginTop: 6 }}>
              {progress?.done ?? 0} / {progress?.total ?? totalToRun} ({pct}%)
              {progress?.series ? ` · ${progress.series}` : ""}
              {progress?.last_quality != null
                ? ` · last quality ${progress.last_quality.toFixed(2)}`
                : ""}
              {progress?.last_latency_ms != null
                ? ` · ${Math.round(progress.last_latency_ms)} ms`
                : ""}
            </div>
          </div>
        )}

        {status.status === "completed" && status.summary && (
          <div className="cp-hint muted" style={{ marginTop: 10 }}>
            Last run complete — {String((status.summary as Record<string, unknown>).total_instances ?? "")}{" "}
            executions, {String((status.summary as Record<string, unknown>).failed ?? 0)} failed.
            View the points on the <b>Eval 3D</b> / <b>Eval 2D</b> tabs.
          </div>
        )}
      </div>

      {/* What's recorded right now, per series */}
      <div className="panel">
        <div className="panel-title">Recorded points by series</div>
        {rollups.length === 0 ? (
          <div className="empty">
            No points yet. Start a run above — results stream in and appear here and on the 3D/2D tabs.
          </div>
        ) : (
          <table className="eval-catalog-table">
            <thead>
              <tr>
                <th>series (prompt)</th>
                <th>points</th>
                <th>mean quality</th>
                <th>mean latency</th>
              </tr>
            </thead>
            <tbody>
              {rollups.map((r) => (
                <tr key={r.key}>
                  <td>
                    <span className="chip">{r.key}</span>
                  </td>
                  <td>{r.n}</td>
                  <td>{r.meanQuality == null ? "—" : r.meanQuality.toFixed(3)}</td>
                  <td>{r.meanLatencyMs == null ? "—" : `${Math.round(r.meanLatencyMs)} ms`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="foot">
        GBBO · real eval — prompts × queries over the live stack. Quality = Opus judge triad
        (faithfulness / correctness / completeness); latency = model generation time; the time
        axis is execution order.
      </div>
    </div>
  );
}
