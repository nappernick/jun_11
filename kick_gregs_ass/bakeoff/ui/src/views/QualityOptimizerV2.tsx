/**
 * QualityOptimizerV2 — the top-level v2 optimizer view.
 *
 * Layout: Two model panels (sonnet-4.6-thinking-off, haiku-4.5) side-by-side,
 * a shared coverage-ladder rail, and the tournament bracket/timeline below.
 * Both model panels keep their SSE subscriptions open for the concurrency gate.
 */
import { useCallback, useEffect, useState } from "react";
import type { JSX } from "react";
import { ModelPanelV2 } from "../components/ModelPanelV2";
import { CoverageLadderRail } from "../components/CoverageLadderRail";
import type { LadderIslandMarker } from "../components/CoverageLadderRail";
import { TournamentBracket } from "../components/TournamentBracket";
import { ApiError, fetchOptimizeV2Status, resetOptimizeV2, resumeOptimizeV2, startOptimizeV2 } from "../api/client";
import type { OptimizeStartBody } from "../api/client";
import type { OptimizerV2Status, OptimizerV2StatusRound } from "../api/types";

const TARGET_MODELS = ["sonnet-4.6-thinking-off", "haiku-4.5"] as const;

export function QualityOptimizerV2(): JSX.Element {
  const [status, setStatus] = useState<OptimizerV2Status | null>(null);
  const [backend, setBackend] = useState<"offline" | "live">("offline");
  const [pending, setPending] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setStatus(await fetchOptimizeV2Status(signal));
    } catch {
      // Leave the last-known status; the 3s poll retries.
    }
  }, []);

  const onStart = useCallback(async () => {
    setError(null);
    setPending(true);
    try {
      const body: OptimizeStartBody = { backend, models: [...TARGET_MODELS] };
      await startOptimizeV2(body);
      await load();
    } catch (e) {
      setError(
        e instanceof ApiError
          ? typeof e.detail === "string"
            ? e.detail
            : e.message
          : e instanceof Error
            ? e.message
            : String(e),
      );
    } finally {
      setPending(false);
    }
  }, [backend, load]);

  const onReset = useCallback(async () => {
    setError(null);
    setResetting(true);
    try {
      setStatus(await resetOptimizeV2());
    } catch (e) {
      setError(
        e instanceof ApiError
          ? typeof e.detail === "string"
            ? e.detail
            : e.message
          : e instanceof Error
            ? e.message
            : String(e),
      );
    } finally {
      setResetting(false);
    }
  }, []);

  const onResume = useCallback(async () => {
    setError(null);
    setResuming(true);
    try {
      setStatus(await resumeOptimizeV2());
    } catch (e) {
      setError(
        e instanceof ApiError
          ? typeof e.detail === "string"
            ? e.detail
            : e.message
          : e instanceof Error
            ? e.message
            : String(e),
      );
    } finally {
      setResuming(false);
    }
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    void load(ctrl.signal);
    const id = window.setInterval(() => void load(), 3000);
    return () => {
      ctrl.abort();
      window.clearInterval(id);
    };
  }, [load]);

  // Build ladder markers from all models' islands (backend emits champion_ci_half_width).
  const ladderMarkers: LadderIslandMarker[] = [];
  if (status?.models) {
    for (const [model, ms] of Object.entries(status.models)) {
      for (const island of ms.islands ?? []) {
        ladderMarkers.push({
          island_id: island.island_id,
          rung_index: island.rung_index,
          ci_half_width: island.champion_ci_half_width ?? null,
          model,
        });
      }
    }
  }

  // Aggregate tournament rounds from all models (they share the same rounds).
  const allRounds: OptimizerV2StatusRound[] = status?.models
    ? Object.values(status.models).flatMap((ms) => ms.tournament_rounds ?? [])
    : [];
  // Deduplicate by round number (both models see the same tournament).
  const uniqueRounds = Array.from(
    new Map(allRounds.map((t) => [t.round, t])).values(),
  ).sort((a, b) => a.round - b.round);

  const running = status?.status === "running";

  return (
    <div className="v2-root view">
      <div className="shead">
        <h2>Optimizer v2</h2>
        <span className="sub">Island coevolution · coverage ladder · tournament</span>
        {status && (
          <span className={`pill state ${status.status}`}>
            {status.status}{running ? " ●" : ""}
          </span>
        )}
        <label className="field">
          <span className="field-label">backend</span>
          <select
            className="opt-select"
            value={backend}
            disabled={running || pending}
            onChange={(e) => setBackend(e.target.value as "offline" | "live")}
          >
            <option value="offline">offline</option>
            <option value="live">live</option>
          </select>
        </label>
        <button
          className="btn"
          disabled={running || pending}
          onClick={() => void onStart()}
        >
          {pending ? "starting…" : running ? "running…" : "Start v2 run"}
        </button>
        <button
          className="btn danger"
          disabled={pending || resetting}
          title="Stop the active run, reset state, and clear previous v2 data"
          onClick={() => void onReset()}
        >
          {resetting ? "resetting…" : "Stop & Reset"}
        </button>
        {status?.status === "failed" && (
          <button
            className="btn"
            disabled={resuming || pending}
            title="Resume the failed run from its last durable checkpoint — no data is lost"
            onClick={() => void onResume()}
          >
            {resuming ? "resuming…" : "Resume run"}
          </button>
        )}
        {error && <span className="startrun-err">{error}</span>}
        <span className="rule" />
      </div>

      {/* Two model panels side-by-side. */}
      <div className="v2-models-grid">
        {TARGET_MODELS.map((m) => (
          <ModelPanelV2 key={m} model={m} />
        ))}
      </div>

      {/* Shared lateral widgets: ladder rail + tournament bracket. */}
      <div className="v2-bottom-row">
        <CoverageLadderRail markers={ladderMarkers} />
        <TournamentBracket rounds={uniqueRounds} />
      </div>
    </div>
  );
}
