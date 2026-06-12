/**
 * QualityOptimizer — the closed-loop prompt optimizer surface inside the
 * Quality_Tab (design "Live dashboard / Quality-Tab design"; Req 9).
 *
 * It renders ONE `PerModelView` per Target_Model, laid out **side-by-side** so
 * both views keep their `/api/stream?model=<model>` subscriptions open at once.
 * That layout choice is load-bearing for the concurrency gate (Req 1.11 / 9.8):
 * each open subscription marks its model "viewable" in the backend ViewRegistry,
 * and the `PerModelOrchestrator` only runs the two per-model loops concurrently
 * when BOTH models are viewable. Keeping both views mounted is therefore what
 * makes concurrent optimization eligible; if a view is closed its model falls
 * back to sequential.
 *
 * A small start control (POST /api/quality/optimize/start) and a status poll
 * (GET /api/quality/optimize/status) sit above the views, mirroring the Judge
 * view's control-strip + poll pattern. The live champion/challenger scores,
 * Author reasoning, prompt, diff, and decisions all stream straight into each
 * Per_Model_View over SSE.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import type { JSX } from "react";
import { ApiError, fetchOptimizeStatus, startOptimize } from "../api/client";
import type { OptimizeStartBody } from "../api/client";
import type { OptimizerStatus } from "../api/types";
import { PerModelView } from "./PerModelView";
import { useElapsed } from "../lib/useElapsed";

/** The two fixed Target_Models (bakeoff/config.py::QUALITY_MODELS, in order). */
const TARGET_MODELS: readonly string[] = ["sonnet-4.6-thinking-off", "haiku-4.5"];

function detailMessage(detail: unknown): string | null {
  if (detail && typeof detail === "object" && "detail" in detail) {
    const d = (detail as { detail: unknown }).detail;
    if (typeof d === "string") return d;
  }
  return null;
}

export function QualityOptimizer(): JSX.Element {
  const [status, setStatus] = useState<OptimizerStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [backend, setBackend] = useState<"offline" | "live">("offline");

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const s = await fetchOptimizeStatus(signal);
      setStatus(s);
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      /* transient poll error: keep the last good status */
    }
  }, []);

  // Poll status every 2s so the lifecycle + per-model progress stay fresh.
  useEffect(() => {
    const ctrl = new AbortController();
    void load(ctrl.signal);
    const id = window.setInterval(() => void load(), 2000);
    return () => {
      ctrl.abort();
      window.clearInterval(id);
    };
  }, [load]);

  const running = status?.status === "running";
  const elapsed = useElapsed(status?.started_at ?? null, running);

  const onStart = useCallback(async () => {
    setError(null);
    setPending(true);
    try {
      const body: OptimizeStartBody = { backend, models: TARGET_MODELS };
      const s = await startOptimize(body);
      setStatus(s);
    } catch (e) {
      if (e instanceof ApiError) {
        setError(detailMessage(e.detail) ?? e.message);
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setPending(false);
    }
  }, [backend]);

  const models = useMemo(() => {
    // Prefer the models the active run was launched with; fall back to the fixed two.
    const req = status?.request;
    if (req && Array.isArray((req as { models?: unknown }).models)) {
      const m = (req as { models: unknown[] }).models.map((x) => String(x));
      if (m.length > 0) return m;
    }
    return TARGET_MODELS;
  }, [status]);

  return (
    <div className="opt-root">
      {/* Control strip + lifecycle. */}
      <div className="panel">
        <div className="jctl">
          <label className="field">
            <span className="field-label">backend</span>
            <select
              className="opt-select"
              value={backend}
              disabled={running || pending}
              onChange={(e) => setBackend(e.target.value === "live" ? "live" : "offline")}
              aria-label="optimizer backend"
            >
              <option value="offline">offline (zero network)</option>
              <option value="live">live (Bedrock)</option>
            </select>
          </label>
          <button
            className="btn primary"
            disabled={running || pending}
            onClick={() => void onStart()}
          >
            {pending ? "Starting…" : running ? "Optimizing…" : "Start optimizer"}
          </button>
          <span className="jctl-status">
            <i className={`jdot ${status?.status ?? "idle"}`} />
            {status?.status ?? "idle"}
            {status?.started_at ? ` · since ${new Date(status.started_at).toLocaleTimeString()}` : ""}
            {running && elapsed ? ` · ${elapsed} elapsed` : ""}
          </span>
        </div>
        <div className="jctl-hint">
          The closed-loop optimizer runs a champion/challenger loop per Target_Model, scored by the
          Opus judge triad. Both Per_Model_Views below stay subscribed so both models are marked
          viewable — which is what lets the orchestrator run the two loops concurrently (Req 1.11).
        </div>
        {error && <div className="startrun-err">{error}</div>}
        {status?.status === "failed" && status.error && (
          <div className="startrun-err">Optimizer failed: {status.error}</div>
        )}
      </div>

      {/* Side-by-side Per_Model_Views (Req 9.8/9.9): both subscriptions open. */}
      <div className="opt-models">
        {models.map((m) => (
          <PerModelView
            key={m}
            model={m}
            progress={status?.models?.[m]}
            running={running}
            startedAt={status?.started_at ?? null}
          />
        ))}
      </div>
    </div>
  );
}
