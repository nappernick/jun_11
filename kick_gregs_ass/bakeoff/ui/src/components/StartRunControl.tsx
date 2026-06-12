/**
 * Start-Run control for the Bake-Off tab.
 *
 * A "Start Run" button plus small reps/temperature inputs that POST to
 * /api/run/start (client.startRun). The button is disabled while a run is active
 * (status running | paused) so the browser never races the backend's own 409
 * guard, and the 409 ("a run is already active") plus any other error are
 * surfaced inline. After a successful 202 the parent refreshes the snapshot; the
 * existing poll + SSE stream then light everything up.
 *
 * Run-scoped Pause / Resume / Abort live here too so the whole race is driven
 * from one place; they reuse the same control handler the header uses.
 */
import { useState } from "react";
import type { JSX } from "react";
import { ApiError, startRun } from "../api/client";
import type { ControlAction, RunSnapshot, StartRunBody } from "../api/types";

export interface StartRunControlProps {
  readonly snapshot: RunSnapshot;
  /** Called with the 202 snapshot so the parent can refresh immediately. */
  readonly onStarted: (snapshot: RunSnapshot) => void;
  readonly onControl: (action: ControlAction) => void;
}

const REPS_DEFAULT = 3;
const TEMPERATURE_DEFAULT = 0.2;

function detailMessage(detail: unknown): string | null {
  if (detail && typeof detail === "object" && "detail" in detail) {
    const d = (detail as { detail: unknown }).detail;
    if (typeof d === "string") return d;
  }
  return null;
}

export function StartRunControl({
  snapshot,
  onStarted,
  onControl,
}: StartRunControlProps): JSX.Element {
  const [reps, setReps] = useState<number>(REPS_DEFAULT);
  const [temperature, setTemperature] = useState<number>(TEMPERATURE_DEFAULT);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const status = snapshot.status;
  const isRunning = status === "running";
  const isPaused = status === "paused";
  const isActive = isRunning || isPaused;

  const onStart = async (): Promise<void> => {
    setError(null);
    setPending(true);
    try {
      const body: StartRunBody = { reps, temperature };
      const snap = await startRun(body);
      onStarted(snap);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setError(detailMessage(e.detail) ?? "A run is already active.");
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="startrun">
      <div className="startrun-row">
        <div className="startrun-fields">
          <label className="field">
            <span className="field-label">reps</span>
            <input
              className="field-input num"
              type="number"
              min={1}
              max={50}
              step={1}
              value={reps}
              disabled={isActive || pending}
              onChange={(e) => setReps(Math.max(1, Math.round(Number(e.target.value) || REPS_DEFAULT)))}
              aria-label="reps per item"
            />
          </label>
          <label className="field">
            <span className="field-label">temperature</span>
            <input
              className="field-input num"
              type="number"
              min={0}
              max={2}
              step={0.05}
              value={temperature}
              disabled={isActive || pending}
              onChange={(e) => setTemperature(Math.max(0, Number(e.target.value) || 0))}
              aria-label="sampling temperature"
            />
          </label>
        </div>

        <button
          className="btn primary startrun-go"
          disabled={isActive || pending}
          onClick={() => void onStart()}
        >
          {pending ? "Starting…" : isActive ? "Run active" : "Start Run"}
        </button>

        {snapshot.account && (
          <span
            className="startrun-aws"
            title={`Bake-Off target models run on AWS account ${snapshot.account}${snapshot.credential_profile ? ` (profile "${snapshot.credential_profile}")` : ""}`}
          >
            <span className="startrun-aws-label">AWS</span>
            <span className="startrun-aws-acct">{snapshot.account}</span>
            {snapshot.credential_profile && (
              <span className="startrun-aws-profile">{snapshot.credential_profile}</span>
            )}
          </span>
        )}

        <span className="startrun-sep" />

        <div className="controls">
          <button className="btn" disabled={!isRunning} onClick={() => onControl("pause")}>
            Pause
          </button>
          <button className="btn" disabled={!isPaused} onClick={() => onControl("resume")}>
            Resume
          </button>
          <button
            className="btn danger"
            disabled={!isActive}
            onClick={() => onControl("abort")}
          >
            Abort
          </button>
        </div>
      </div>

      {error && <div className="startrun-err">{error}</div>}
      {!error && isActive && (
        <div className="startrun-hint">
          Run {status}
          {snapshot.auto_paused ? " (auto-paused)" : ""} · watch the fleet below light up as trials
          stream in.
        </div>
      )}
    </div>
  );
}
