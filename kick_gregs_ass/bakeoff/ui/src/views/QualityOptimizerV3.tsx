/**
 * QualityOptimizerV3 — the top-level V3 optimizer view (hardened, LIVE-ONLY).
 *
 * Same layout as the v2 view (two model panels, shared coverage-ladder rail,
 * tournament bracket) over the /api/quality/optimize/v3/* surface, plus the V3
 * containment readout each ModelPanelV3 renders (phase, skipped iterations,
 * dead islands, contained-failure feed). There is no backend selector: v3 has
 * no offline mode — every run is live.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import type { JSX } from "react";
import type { EChartsOption } from "echarts";
import { ModelPanelV3 } from "../components/ModelPanelV3";
import { CoverageLadderRail } from "../components/CoverageLadderRail";
import type { LadderIslandMarker } from "../components/CoverageLadderRail";
import { TournamentBracket } from "../components/TournamentBracket";
import { EChart } from "../components/EChart";
import {
  ApiError,
  fetchOptimizeV3Status,
  resetOptimizeV3,
  resumeOptimizeV3,
  startOptimizeV3,
} from "../api/client";
import type { OptimizerV3Status, OptimizerV2StatusRound } from "../api/types";
import { modelColor, score } from "../lib/format";

const TARGET_MODELS = ["sonnet-4.6-thinking-off", "haiku-4.5"] as const;

function errorText(e: unknown): string {
  if (e instanceof ApiError) {
    return typeof e.detail === "string" ? e.detail : e.message;
  }
  return e instanceof Error ? e.message : String(e);
}

function trajectoryOption(status: OptimizerV3Status | null, turnMode: string): EChartsOption {
  const series: Record<string, unknown>[] = [];
  for (const [model, modelStatus] of Object.entries(status?.models ?? {})) {
    for (const island of modelStatus.islands ?? []) {
      if ((island.turn_mode ?? "multi") !== turnMode) continue;
      const color = modelColor(`${model}-${island.island_id}`);
      const data = (island.score_series ?? []).map(
        (point, stepIndex) => [stepIndex, point.champion_score] as [number, number],
      );
      if (data.length === 0 && typeof island.champion_score === "number") {
        data.push([0, island.champion_score]);
      }
      series.push({
        type: "line",
        name: `${model} · i${island.island_id}`,
        data,
        showSymbol: data.length < 40,
        symbolSize: 5,
        lineStyle: { color, width: 2 },
        itemStyle: { color },
        emphasis: { focus: "series" },
      });
    }
  }
  return {
    backgroundColor: "transparent",
    grid: { left: 44, right: 20, top: 42, bottom: 34 },
    legend: {
      top: 4,
      type: "scroll",
      textStyle: { color: "#aebfd4", fontSize: 10 },
    },
    tooltip: {
      trigger: "axis",
      valueFormatter: (value: unknown) => (value == null ? "—" : Number(value).toFixed(3)),
    },
    xAxis: {
      type: "value",
      name: "optimizer step",
      nameLocation: "middle",
      nameGap: 22,
      minInterval: 1,
      axisLabel: { color: "#7488a3", fontSize: 10 },
      nameTextStyle: { color: "#7488a3", fontSize: 10 },
      splitLine: { show: false },
    },
    yAxis: {
      type: "value",
      min: 0,
      max: 1,
      name: "champion score",
      nameLocation: "middle",
      nameGap: 34,
      axisLabel: { color: "#7488a3", fontSize: 10 },
      nameTextStyle: { color: "#7488a3", fontSize: 10 },
      splitLine: { lineStyle: { color: "rgba(140,165,200,0.10)" } },
    },
    series,
  } as unknown as EChartsOption;
}

export function QualityOptimizerV3(): JSX.Element {
  const [status, setStatus] = useState<OptimizerV3Status | null>(null);
  const [pending, setPending] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [turnMode, setTurnMode] = useState<"single" | "multi" | "both">("multi");
  const [viewMode, setViewMode] = useState<string | null>(null);  // which run-type section is shown

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setStatus(await fetchOptimizeV3Status(signal));
    } catch {
      // Leave the last-known status; the 3s poll retries.
    }
  }, []);

  const onStart = useCallback(async () => {
    setError(null);
    setPending(true);
    try {
      await startOptimizeV3({ models: [...TARGET_MODELS], turn_mode: turnMode });
      await load();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setPending(false);
    }
  }, [load, turnMode]);

  const onReset = useCallback(async () => {
    // Destructive-action gate: this clears the live v3 view (the data itself is
    // archived server-side, but the run restarts from scratch). Require an
    // explicit confirmation so a stressed mis-click can't wipe a run.
    const confirmed = window.confirm(
      "Stop & Reset clears the current v3 run from the dashboard and starts fresh.\n" +
        "(The durable data is archived under data/bakeoff/_archive_v3_reset_*, not deleted.)\n\n" +
        "Reset now?",
    );
    if (!confirmed) return;
    setError(null);
    setResetting(true);
    try {
      setStatus(await resetOptimizeV3());
    } catch (e) {
      setError(errorText(e));
    } finally {
      setResetting(false);
    }
  }, []);

  const onResume = useCallback(async () => {
    setError(null);
    setResuming(true);
    try {
      setStatus(await resumeOptimizeV3());
    } catch (e) {
      setError(errorText(e));
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

  const running = status?.status === "running";

  // GRAND CHAMPION: when the run completes, crown the model whose frozen
  // champion scored highest (per-model winners come from the run-state sentinel).
  const winners = Object.entries(status?.models ?? {})
    .map(([model, ms]) => ({
      model,
      done: Boolean(ms.run_state?.phase_b_done),
      scoreValue:
        typeof ms.run_state?.champion_score === "number" ? ms.run_state.champion_score : null,
      degraded: Boolean(ms.run_state?.degraded),
      prompt: ms.run_state?.champion_instruction ?? null,
    }))
    .filter((w) => w.done && w.scoreValue !== null)
    .sort((a, b) => (b.scoreValue ?? 0) - (a.scoreValue ?? 0));
  const grandChampion = status?.status === "completed" && winners.length > 0 ? winners[0] : null;

  // Single/multi split: which run-types have data (the views that "pop up"). Derived from
  // the durable islands' turn_mode plus the currently-running run's mode.
  const _modes = new Set<string>();
  let runningMode: string | null = null;
  for (const ms of Object.values(status?.models ?? {})) {
    for (const isl of ms.islands ?? []) _modes.add(isl.turn_mode ?? "multi");
    // The per-model run_state never carries status="running" (the v3 orchestrator only
    // writes TERMINAL statuses — failed/completed — into the sentinel), so we must key the
    // running section off the TOP-LEVEL run status. Without this, a live "both"/"single"
    // run with no durable islands yet collapsed activeMode to "multi", so showLive went
    // false and the FightArena live duel (challenger, animations, audio) stayed blank.
    const runTurnMode = ms.run_state?.turn_mode;
    if (runTurnMode && (running || ms.run_state?.status === "running")) {
      _modes.add(runTurnMode);
      runningMode = runTurnMode;
    }
  }
  const presentModes: string[] = ["single", "multi", "both"].filter((m) => _modes.has(m));
  const activeMode =
    viewMode && presentModes.includes(viewMode)
      ? viewMode
      : runningMode && presentModes.includes(runningMode)
        ? runningMode
        : presentModes[0] ?? "multi";

  // Build ladder markers from the active run type's islands.
  const ladderMarkers: LadderIslandMarker[] = [];
  if (status?.models) {
    for (const [model, modelStatus] of Object.entries(status.models)) {
      for (const island of modelStatus.islands ?? []) {
        if ((island.turn_mode ?? "multi") !== activeMode) continue;
        ladderMarkers.push({
          island_id: island.island_id,
          rung_index: island.rung_index,
          ci_half_width: island.champion_ci_half_width ?? null,
          model,
        });
      }
    }
  }

  // Aggregate tournament rounds from all models (deduped by round number).
  const allRounds: OptimizerV2StatusRound[] = status?.models
    ? Object.values(status.models).flatMap((modelStatus) => modelStatus.tournament_rounds ?? [])
    : [];
  const uniqueRounds = Array.from(
    new Map(allRounds.map((round) => [round.round, round])).values(),
  ).sort((leftRound, rightRound) => leftRound.round - rightRound.round);

  const sharedTrajectory = useMemo(() => trajectoryOption(status, activeMode), [status, activeMode]);
  const modelSummaries = useMemo(
    () =>
      TARGET_MODELS.map((model) => {
        const modelStatus = status?.models?.[model];
        const islands = (modelStatus?.islands ?? []).filter(
          (island) => (island.turn_mode ?? "multi") === activeMode,
        );
        const bestIsland = [...islands].sort(
          (leftIsland, rightIsland) => rightIsland.champion_score - leftIsland.champion_score,
        )[0];
        const runState = modelStatus?.run_state ?? {};
        return {
          model,
          phase: runState.phase_b_done ? "B done" : runState.phase_a_complete ? "B" : "A",
          scoreValue: runState.champion_score ?? bestIsland?.champion_score ?? null,
          ciHalfWidth: bestIsland?.champion_ci_half_width ?? null,
          bestIslandId: bestIsland?.island_id ?? null,
          deadCount: (runState.dead_islands ?? []).length,
          statusText: runState.status ?? status?.status ?? "idle",
          updatedAt: runState.updated_at ?? null,
        };
      }),
    [activeMode, status],
  );

  return (
    <div className="v2-root view">
      <div className="shead">
        <h2>Optimizer v3</h2>
        <span className="sub">
          Hardened · live-only · contained failures · concurrent islands &amp; items
        </span>
        {status && (
          <span className={`pill state ${status.status}`}>
            {status.status}
            {running ? " ●" : ""}
          </span>
        )}
        <div className="seg" role="group" aria-label="conversation type" title="Which conversation type to appraise on. Single-turn = clean gold, lowest appraisal noise.">
          {(["single", "multi", "both"] as const).map((m) => (
            <button
              key={m}
              className={`seg-btn ${turnMode === m ? "on" : ""}`}
              disabled={running || pending}
              onClick={() => setTurnMode(m)}
            >
              {m}
            </button>
          ))}
        </div>
        <button
          className="btn"
          disabled={running || pending}
          onClick={() => void onStart()}
          title="Launch a LIVE v3 run (alpha AOSS retrieval + rerank v4 + Opus judge)"
        >
          {pending ? "starting…" : running ? "running…" : "Start v3 run (live)"}
        </button>
        <button
          className="btn danger"
          disabled={pending || resetting}
          title="Stop the active run, reset state, and clear previous v3 data + sentinel"
          onClick={() => void onReset()}
        >
          {resetting ? "resetting…" : "Stop & Reset"}
        </button>
        {(status?.status === "failed" || status?.status === "completed") && (
          <button
            className="btn"
            disabled={resuming || pending}
            title="Resume from durable checkpoints — completed phases are skipped via the sentinel"
            onClick={() => void onResume()}
          >
            {resuming ? "resuming…" : "Resume run"}
          </button>
        )}
        {error && <span className="startrun-err">{error}</span>}
        {status?.error && <span className="startrun-err">{status.error}</span>}
        <span className="rule" />
      </div>

      {/* GRAND CHAMPION victory banner — the run's crowned winner. */}
      {grandChampion && (
        <div className="v3f-victory">
          <div className="v3f-victory-burst" aria-hidden />
          <div className="v3f-victory-title">VICTORY</div>
          <div className="v3f-victory-model">
            🏆 {grandChampion.model}
            {grandChampion.degraded ? " · degraded run" : ""}
          </div>
          <div className="v3f-victory-score">
            frozen champion {grandChampion.scoreValue?.toFixed(3)}
          </div>
          {grandChampion.prompt && (
            <code className="v3f-victory-prompt">{grandChampion.prompt.slice(0, 280)}…</code>
          )}
        </div>
      )}

      {/* Separate single-run / multi-run views — a tab pops up only once that run type
          has data. Switching filters the panels (durable by tag, live by the run's mode). */}
      {presentModes.length > 1 && (
        <div className="seg" role="tablist" aria-label="run type" style={{ marginBottom: 10 }}>
          {presentModes.map((m) => (
            <button
              key={m}
              role="tab"
              aria-selected={activeMode === m}
              className={`seg-btn ${activeMode === m ? "on" : ""}`}
              onClick={() => setViewMode(m)}
            >
              {m}-turn
            </button>
          ))}
        </div>
      )}

      <div className="v3-summary-grid">
        {modelSummaries.map((summary) => (
          <div key={summary.model} className="panel v3-summary-card">
            <span className="v2-model-dot" style={{ background: modelColor(summary.model) }} />
            <div>
              <div className="v3-summary-model">{summary.model}</div>
              <div className="muted">
                phase {summary.phase} · {summary.statusText}
                {summary.bestIslandId != null ? ` · best island ${summary.bestIslandId}` : ""}
              </div>
            </div>
            <div className="v3-summary-score">
              <b>{score(summary.scoreValue)}</b>
              <span className="muted">
                {summary.ciHalfWidth == null ? "CI —" : `± ${score(summary.ciHalfWidth)}`}
              </span>
            </div>
            {summary.deadCount > 0 && <span className="pill none">{summary.deadCount} dead</span>}
          </div>
        ))}
      </div>

      <div className="panel v3-shared-trajectory">
        <div className="panel-title">Shared champion trajectory · {activeMode}-turn</div>
        <EChart
          option={sharedTrajectory}
          height={270}
          ariaLabel="Optimizer v3 shared champion trajectories across models and islands"
        />
      </div>

      {/* Two model panels side-by-side, filtered to the active run type. The key is the
          MODEL ALONE (not model+mode): switching the run-type tab must NOT remount the panel,
          or its useOptimizerV3Stream state (sparklines, prompt feed, the live duel) would be
          wiped and the SSE reconnected on every tab click. ModelPanelV3 re-filters reactively
          off the turnMode prop, so a prop change is all that's needed to switch modes. */}
      <div className="v2-models-grid">
        {TARGET_MODELS.map((m) => (
          <ModelPanelV3 key={m} model={m} turnMode={activeMode} />
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
