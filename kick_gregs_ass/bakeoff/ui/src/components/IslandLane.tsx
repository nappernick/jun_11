/**
 * IslandLane — one island's live optimizer state for the v2 view.
 *
 * Shows: current rung + size/CI, champion score-over-iterations sparkline,
 * stance/style label, the live previous-vs-current prompt scoring, the live
 * author reasoning blurb, the current champion prompt + last diff, and a
 * stuck/iterating/escalating state chip.
 *
 * Live SSE detail (champion/challenger scores, author reasoning, champion prompt,
 * diff) comes from the three rich per-iteration events routed by island_id; the
 * durable status backfill is the fallback so a reload keeps the structural
 * surface populated until the next live step refreshes the detail.
 */
import type { JSX } from "react";
import { EChart } from "./EChart";
import { PromptDiff } from "./PromptDiff";
import { score } from "../lib/format";
import type { IslandStepPoint, IslandLiveState } from "../api/useOptimizerV2Stream";
import type { OptimizerV2IslandProgress } from "../api/types";
import type { EChartsOption } from "echarts";

export interface IslandLaneProps {
  /** Live SSE-accumulated state (sparkline, current rung/score, prompt, reasoning). */
  readonly live: IslandLiveState | null;
  /** Durable backfill from the status poll (fallback for prompt/diff/score). */
  readonly backfill: OptimizerV2IslandProgress | null;
}

function stateChipClass(state: string): string {
  if (state === "stuck") return "v2-chip stuck";
  if (state === "escalating") return "v2-chip escalating";
  return "v2-chip iterating";
}

/** Coverage ladder rung sizes (config QUALITY_OPT_RUNG_SIZES: 6→12→24→40→60). */
const RUNG_SIZES = [6, 12, 24, 40, 60] as const;

function rungLabel(rungIndex: number): string {
  if (rungIndex < RUNG_SIZES.length) return `n=${RUNG_SIZES[rungIndex]}`;
  return "full";
}

function buildSparkOption(points: readonly IslandStepPoint[]): EChartsOption {
  return {
    grid: { left: 4, right: 4, top: 4, bottom: 4 },
    xAxis: { type: "category", show: false, data: points.map((_, i) => i) },
    yAxis: { type: "value", show: false, min: "dataMin", max: "dataMax" },
    series: [
      {
        type: "line",
        data: points.map((p) => p.champion_score),
        showSymbol: false,
        lineStyle: { width: 1.5, color: "#6aa9ff" },
        areaStyle: { color: "rgba(106,169,255,0.08)" },
        smooth: true,
      },
    ],
    tooltip: { show: false },
    animation: false,
  };
}

export function IslandLane({ live, backfill }: IslandLaneProps): JSX.Element {
  const island_id = live?.island_id ?? backfill?.island_id ?? 0;
  const rung = live?.rung_index ?? backfill?.rung_index ?? 0;
  const champScore = live?.champion_score ?? backfill?.champion_score ?? null;
  const ciHalf = live?.ci_half_width ?? backfill?.champion_ci_half_width ?? null;
  const state = live?.state ?? backfill?.state ?? "iterating";
  const stance = backfill?.stance ?? (island_id === 0 ? "concise" : "explicit");
  const sparkline = live?.sparkline ?? [];

  // Prefer live per-iteration detail; fall back to durable backfill.
  const reasoning = live?.authorReasoning ?? backfill?.author_reasoning ?? null;
  const prompt = live?.championInstruction ?? backfill?.champion_instruction ?? null;
  const diff = live?.promptDiff ?? backfill?.prompt_diff ?? null;

  // Previous-vs-current prompt scoring (the "last turn vs current turn" readout).
  // Prefer live; fall back to the durable backfill so a reload keeps the readout.
  const championScored = live?.championScored
    ?? (backfill?.champion_score != null
      ? { triad: backfill.champion_score, ciHalfWidth: backfill.champion_ci_half_width ?? 0, iterationIndex: -1 }
      : null);
  const challengerScored = live?.challengerScored
    ?? (backfill?.challenger_score != null
      ? { triad: backfill.challenger_score, ciHalfWidth: backfill.challenger_ci_half_width ?? 0, iterationIndex: -1 }
      : null);
  const outcome = live?.lastOutcome
    ?? (backfill?.accepted != null
      ? {
          iterationIndex: -1,
          accepted: backfill.accepted,
          challengerTriad: backfill.challenger_score ?? null,
          challengerCiHalfWidth: backfill.challenger_ci_half_width ?? null,
          gainAbsolute: null,
          gainPercent: null,
        }
      : null);

  return (
    <div className="v2-island-lane">
      <div className="v2-island-head">
        <span className="v2-island-title">Island {island_id}</span>
        <span className={stateChipClass(state)}>{state}</span>
        <span className="v2-stance">{stance}</span>
      </div>

      <div className="v2-island-rung">
        <span className="v2-rung-label">Rung {rung}</span>
        <span className="v2-rung-size">{rungLabel(rung)}</span>
        {ciHalf != null && <span className="v2-rung-ci">±{score(ciHalf)}</span>}
      </div>

      {champScore != null && (
        <div className="v2-island-score">
          <span className="v2-score-val">{score(champScore)}</span>
          {ciHalf != null && <span className="v2-score-ci">±{score(ciHalf)}</span>}
        </div>
      )}

      {/* Previous-vs-current prompt scoring for the active iteration. */}
      {(championScored || challengerScored) && (
        <div className="v2-score-compare">
          <span className="v2-score-cell prev">
            <span className="v2-score-cell-label">champion</span>
            <span className="v2-score-cell-val">
              {championScored ? score(championScored.triad) : "—"}
              {championScored ? ` ±${score(championScored.ciHalfWidth)}` : ""}
            </span>
          </span>
          <span className="v2-score-arrow">→</span>
          <span className={`v2-score-cell cur${outcome?.accepted ? " accepted" : ""}`}>
            <span className="v2-score-cell-label">challenger</span>
            <span className="v2-score-cell-val">
              {challengerScored ? score(challengerScored.triad) : "—"}
              {challengerScored ? ` ±${score(challengerScored.ciHalfWidth)}` : ""}
            </span>
          </span>
          {outcome && (
            <span className={`v2-score-verdict ${outcome.accepted ? "accepted" : "rejected"}`}>
              {outcome.accepted ? "promoted" : "kept"}
              {outcome.gainAbsolute != null ? ` (${outcome.gainAbsolute >= 0 ? "+" : ""}${score(outcome.gainAbsolute)})` : ""}
            </span>
          )}
        </div>
      )}

      {sparkline.length > 1 && (
        <div className="v2-sparkline">
          <EChart option={buildSparkOption(sparkline)} height={48} ariaLabel={`Island ${island_id} score sparkline`} />
        </div>
      )}

      {reasoning && (
        <details className="v2-reasoning" open>
          <summary>Author reasoning</summary>
          <pre className="v2-reasoning-text">{reasoning}</pre>
        </details>
      )}

      {prompt && (
        <details className="v2-prompt">
          <summary>Champion prompt</summary>
          <pre className="v2-prompt-text">{prompt}</pre>
        </details>
      )}

      {diff && (
        <details className="v2-diff">
          <summary>Last diff</summary>
          <PromptDiff diff={diff} />
        </details>
      )}
    </div>
  );
}
