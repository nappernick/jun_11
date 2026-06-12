/**
 * ModelPanelV2 — one model's v2 optimizer panel: summary header + 2 island lanes.
 *
 * Summary header shows: best prompt, best triad+CI, rounds done, Phase B number.
 * Below it, the two island lanes render side-by-side.
 */
import type { JSX } from "react";
import { IslandLane } from "./IslandLane";
import { IslandRaceChart } from "./IslandRaceChart";
import { useOptimizerV2Stream } from "../api/useOptimizerV2Stream";
import type { IslandLiveState, IslandStepPoint } from "../api/useOptimizerV2Stream";
import type { OptimizerV2IslandProgress } from "../api/types";
import { score } from "../lib/format";
import { modelColor } from "../lib/format";

export interface ModelPanelV2Props {
  readonly model: string;
}

/**
 * Build the island list for the trend chart. Prefer the live SSE sparkline (it grows
 * point-by-point during a run); on a fresh page load the live state is empty, so
 * synthesize the trajectory from the durable `score_series` backfill so the curve
 * repaints exactly as it was. Returns `null` slots collapsed out.
 */
function chartIslands(
  live: readonly IslandLiveState[],
  backfill: OptimizerV2IslandProgress[] | undefined,
): IslandLiveState[] {
  const ids = new Set<number>();
  for (const i of live) ids.add(i.island_id);
  for (const b of backfill ?? []) ids.add(b.island_id);

  const out: IslandLiveState[] = [];
  for (const id of Array.from(ids).sort((a, b) => a - b)) {
    const liveIsl = live.find((i) => i.island_id === id);
    if (liveIsl && liveIsl.sparkline.length > 0) {
      out.push(liveIsl);
      continue;
    }
    const bf = backfill?.find((b) => b.island_id === id);
    const series = bf?.score_series ?? [];
    if (series.length === 0 && !liveIsl) continue;
    const sparkline: IslandStepPoint[] = series.map((p) => ({
      champion_score: p.champion_score,
      ci_half_width: p.ci_half_width,
      rung_index: p.rung_index,
      state: "iterating",
    }));
    out.push({
      island_id: id,
      rung_index: bf?.rung_index ?? 0,
      champion_score: bf?.champion_score ?? 0,
      ci_half_width: bf?.champion_ci_half_width ?? 0,
      state: bf?.state ?? "iterating",
      sparkline,
      activeIteration: null,
      championInstruction: null,
      promptDiff: null,
      authorReasoning: null,
      championScored: null,
      challengerScored: null,
      lastOutcome: null,
    });
  }
  return out;
}

export function ModelPanelV2({ model }: ModelPanelV2Props): JSX.Element {
  const stream = useOptimizerV2Stream(model);
  const bf = stream.backfill;

  // Merge live and backfill to get the two islands.
  const island0Live = stream.islands.find((i) => i.island_id === 0) ?? null;
  const island1Live = stream.islands.find((i) => i.island_id === 1) ?? null;
  const island0Bf = bf?.islands.find((i) => i.island_id === 0) ?? null;
  const island1Bf = bf?.islands.find((i) => i.island_id === 1) ?? null;

  const raceIslands = chartIslands(stream.islands, bf?.islands as OptimizerV2IslandProgress[] | undefined);

  const bestTriad = bf?.best_triad ?? null;
  const bestCi = bf?.best_ci_half_width ?? null;
  const roundsDone = bf?.tournament_rounds_done ?? stream.tournament_rounds.length;
  const phaseBTriad = bf?.phase_b_triad ?? null;
  const phaseBCi = bf?.phase_b_ci_half_width ?? null;

  return (
    <div className="v2-model-panel panel">
      <div className="v2-model-head">
        <span className="v2-model-dot" style={{ background: modelColor(model) }} />
        <span className="v2-model-name">{model}</span>
        <span className={`v2-stream-badge ${stream.streamStatus}`}>
          {stream.streamStatus} · {stream.received}
        </span>
      </div>

      <div className="v2-model-summary">
        {bestTriad != null && (
          <span className="v2-summary-item">
            <span className="v2-summary-label">Best</span>
            <span className="v2-summary-val">{score(bestTriad)}{bestCi != null ? ` ±${score(bestCi)}` : ""}</span>
          </span>
        )}
        <span className="v2-summary-item">
          <span className="v2-summary-label">Rounds</span>
          <span className="v2-summary-val">{roundsDone}</span>
        </span>
        {phaseBTriad != null && (
          <span className="v2-summary-item">
            <span className="v2-summary-label">Phase B</span>
            <span className="v2-summary-val">{score(phaseBTriad)}{phaseBCi != null ? ` ±${score(phaseBCi)}` : ""}</span>
          </span>
        )}
      </div>

      {/* Trend curve: both islands' champion-triad trajectories on a shared axis. */}
      <div className="v2-model-race">
        <IslandRaceChart islands={raceIslands} height={200} />
      </div>

      <div className="v2-islands-row">
        <IslandLane live={island0Live} backfill={island0Bf} />
        <IslandLane live={island1Live} backfill={island1Bf} />
      </div>
    </div>
  );
}
