/**
 * GBBO dashboard shell.
 *
 * Wires the typed API hooks: the snapshot poll (run status + per-model progress),
 * the SSE trial stream (feed + live latency), and the control endpoints
 * (pause / resume / abort). Two tabs: the live monitoring view (Task 13) and the
 * executive visualization view (Task 14).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { JSX } from "react";
import { useSnapshot } from "./api/useSnapshot";
import { useEventStream } from "./api/useEventStream";
import { useTrialBuffer } from "./api/useTrialBuffer";
import { postControl, fetchBakeOffSessions, fetchRecentTrials, ApiError } from "./api/client";
import type { BakeOffSessionsResponse, ControlAction, RunStatus } from "./api/types";
import { LiveMonitor } from "./views/LiveMonitor";
import { BakeOff } from "./views/BakeOff";
import { Judge } from "./views/Judge";
import { Quality } from "./views/Quality";
import { QualityOptimizerV2 } from "./views/QualityOptimizerV2";
import { QualityOptimizerV3 } from "./views/QualityOptimizerV3";
import { PromptBench } from "./views/PromptBench";
import { ExecView } from "./exec/ExecView";
import { Eval3D } from "./views/Eval3D";
import { Eval2D } from "./views/Eval2D";
import { EvalMetrics } from "./views/EvalMetrics";
import { useEvalStream } from "./api/useEvalStream";
import { METHODOLOGY_NOT_VALIDATED_NOTICE } from "./eval/methodology";

type Tab =
  | "live"
  | "bakeoff"
  | "judge"
  | "quality"
  | "optimizer-v2"
  | "optimizer-v3"
  | "prompt-bench"
  | "exec"
  | "eval-3d"
  | "eval-2d"
  | "eval-metrics";

const KNOWN_STATUSES: readonly RunStatus[] = [
  "running",
  "paused",
  "aborted",
  "completed",
  "idle",
];

function statusClass(status: string): string {
  return (KNOWN_STATUSES as readonly string[]).includes(status) ? status : "idle";
}

export function App(): JSX.Element {
  const { snapshot, error: snapshotError, refreshNow } = useSnapshot(1000);
  const buffer = useTrialBuffer();
  // Shared eval stream state: lifted here so the eval-3d and eval-2d tabs render
  // from the SAME live state and switching between them never reloads or blanks
  // the surface (Req 9.*).
  const evalStream = useEvalStream();
  const [controlError, setControlError] = useState<string | null>(null);
  const [sessions, setSessions] = useState<BakeOffSessionsResponse | null>(null);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("live");
  const activeSessionIdRef = useRef<string | null>(null);

  const clearTrialBuffer = buffer.clear;
  const seedTrialBuffer = buffer.seed;

  const refreshSessions = useCallback(async () => {
    try {
      const nextSessions = await fetchBakeOffSessions();
      setSessions(nextSessions);
      setSessionError(null);

      const previousActiveSessionId = activeSessionIdRef.current;
      const nextActiveSessionId = nextSessions.active_session_id;
      const activeSessionChanged =
        previousActiveSessionId === null || previousActiveSessionId !== nextActiveSessionId;

      activeSessionIdRef.current = nextActiveSessionId;
      if (activeSessionChanged) {
        clearTrialBuffer();
        try {
          const recentTrials = await fetchRecentTrials(5000);
          seedTrialBuffer(recentTrials.trials);
        } catch {
          /* no durable trials yet / transient fetch issue — live SSE will fill it */
        }
        refreshNow();
      }
    } catch (error) {
      setSessionError(error instanceof Error ? error.message : String(error));
    }
  }, [clearTrialBuffer, refreshNow, seedTrialBuffer]);

  useEffect(() => {
    void refreshSessions();
  }, [refreshSessions]);

  const handleBakeOffSessionChanged = useCallback(() => {
    void refreshSessions();
  }, [refreshSessions]);

  const stream = useEventStream(buffer.push, handleBakeOffSessionChanged);

  const status = snapshot.status;
  const isRunning = status === "running";
  const isPaused = status === "paused";

  const doControl = useCallback(
    async (action: ControlAction) => {
      setControlError(null);
      try {
        await postControl(action);
        refreshNow();
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          setControlError("No active run to control.");
        } else {
          setControlError(e instanceof Error ? e.message : String(e));
        }
      }
    },
    [refreshNow],
  );

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <div className="mark" />
          <div className="wm">
            <b>GBBO Console</b>
            <span>Model Bake-Off</span>
          </div>
        </div>

        <div className="spacer" />

        <nav className="tabs" role="tablist" aria-label="View">
          <button
            role="tab"
            aria-selected={tab === "live"}
            className={`tab ${tab === "live" ? "on" : ""}`}
            onClick={() => setTab("live")}
          >
            Live
          </button>
          <button
            role="tab"
            aria-selected={tab === "bakeoff"}
            className={`tab ${tab === "bakeoff" ? "on" : ""}`}
            onClick={() => setTab("bakeoff")}
          >
            Bake-Off
          </button>
          <button
            role="tab"
            aria-selected={tab === "judge"}
            className={`tab ${tab === "judge" ? "on" : ""}`}
            onClick={() => setTab("judge")}
          >
            Judge
          </button>
          <button
            role="tab"
            aria-selected={tab === "quality"}
            className={`tab ${tab === "quality" ? "on" : ""}`}
            onClick={() => setTab("quality")}
          >
            Quality
          </button>
          <button
            role="tab"
            aria-selected={tab === "optimizer-v2"}
            className={`tab ${tab === "optimizer-v2" ? "on" : ""}`}
            onClick={() => setTab("optimizer-v2")}
          >
            Opt v2
          </button>
          <button
            role="tab"
            aria-selected={tab === "optimizer-v3"}
            className={`tab ${tab === "optimizer-v3" ? "on" : ""}`}
            onClick={() => setTab("optimizer-v3")}
          >
            Opt v3
          </button>
          <button
            role="tab"
            aria-selected={tab === "prompt-bench"}
            className={`tab ${tab === "prompt-bench" ? "on" : ""}`}
            onClick={() => setTab("prompt-bench")}
          >
            Prompt Bench
          </button>
          <button
            role="tab"
            aria-selected={tab === "exec"}
            className={`tab ${tab === "exec" ? "on" : ""}`}
            onClick={() => setTab("exec")}
          >
            Exec
          </button>
          <button
            role="tab"
            aria-selected={tab === "eval-3d"}
            className={`tab ${tab === "eval-3d" ? "on" : ""}`}
            onClick={() => setTab("eval-3d")}
          >
            Eval 3D
          </button>
          <button
            role="tab"
            aria-selected={tab === "eval-2d"}
            className={`tab ${tab === "eval-2d" ? "on" : ""}`}
            onClick={() => setTab("eval-2d")}
          >
            Eval 2D
          </button>
          <button
            role="tab"
            aria-selected={tab === "eval-metrics"}
            className={`tab ${tab === "eval-metrics" ? "on" : ""}`}
            onClick={() => setTab("eval-metrics")}
          >
            Metrics
          </button>
        </nav>

        <div className="statusbadge" title="Run status">
          <i className={statusClass(status)} />
          {status}
          {snapshot.auto_paused ? " (auto)" : ""}
        </div>

        <div className={`streamdot ${stream.status}`} title="Live event stream">
          <i />
          {stream.status} · {stream.received}
        </div>

        <div className="controls">
          <button className="btn" disabled={!isRunning} onClick={() => void doControl("pause")}>
            Pause
          </button>
          <button className="btn" disabled={!isPaused} onClick={() => void doControl("resume")}>
            Resume
          </button>
          <button
            className="btn danger"
            disabled={!isRunning && !isPaused}
            onClick={() => void doControl("abort")}
          >
            Abort
          </button>
        </div>
      </header>

      <main className="stage">
        <div className="methodology-notice" role="note">
          {METHODOLOGY_NOT_VALIDATED_NOTICE}
        </div>
        {controlError && (
          <div className="banner" style={{ margin: "16px 22px 0" }}>
            {controlError}
          </div>
        )}
        {tab === "live" && (
          <>
            <LiveMonitor snapshot={snapshot} events={buffer.events} snapshotError={snapshotError} />
            <div className="view" style={{ paddingTop: 0 }}>
              <div className="foot">
                GBBO · live monitoring · backend loopback only. Live quality numbers use the cheap
                normal-approx CI; the defensible cluster-bootstrap CIs are reserved for the exec
                report.
              </div>
            </div>
          </>
        )}
        {tab === "bakeoff" && (
          <BakeOff
            snapshot={snapshot}
            events={buffer.events}
            snapshotError={snapshotError}
            onControl={(action) => void doControl(action)}
            refreshNow={refreshNow}
            sessions={sessions}
            sessionError={sessionError}
            onRefreshSessions={refreshSessions}
          />
        )}
        {tab === "judge" && <Judge />}
        {tab === "quality" && <Quality />}
        {tab === "optimizer-v2" && <QualityOptimizerV2 />}
        {tab === "optimizer-v3" && <QualityOptimizerV3 />}
        {tab === "prompt-bench" && <PromptBench />}
        {tab === "exec" && <ExecView />}
        {tab === "eval-3d" && <Eval3D stream={evalStream} />}
        {tab === "eval-2d" && <Eval2D stream={evalStream} />}
        {tab === "eval-metrics" && <EvalMetrics stream={evalStream} />}
      </main>
    </div>
  );
}
