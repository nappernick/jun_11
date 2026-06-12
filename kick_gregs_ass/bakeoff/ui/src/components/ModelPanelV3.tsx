/**
 * ModelPanelV3 — one model's V3 optimizer panel: summary header + 2 island lanes
 * + the containment strip (phase, skipped iterations, dead islands, failure feed).
 *
 * Reuses the v2 IslandLane / IslandRaceChart components (the per-island live shape
 * is shared); only the data source (useOptimizerV3Stream) and the containment
 * surface are V3-specific.
 */
import { useCallback, useMemo, useState } from "react";
import type { JSX } from "react";
import { freezeV3Champion } from "../api/client";
import { FightArena } from "./FightArena";
import { IslandLane } from "./IslandLane";
import { IslandRaceChart } from "./IslandRaceChart";
import { useOptimizerV3Stream } from "../api/useOptimizerV3Stream";
import type { IslandLiveStateV3 } from "../api/useOptimizerV3Stream";
import type { IslandLiveState, IslandStepPoint } from "../api/useOptimizerV2Stream";
import type { OptimizerV2IslandProgress, OptimizerV3PromptHistoryEntry } from "../api/types";
import { score } from "../lib/format";
import { modelColor } from "../lib/format";

export interface ModelPanelV3Props {
  readonly model: string;
  /** Show only this run type's data (single|multi|both). Durable islands are filtered
   * by their tag; live islands show only when the current run is in this mode. */
  readonly turnMode?: string;
}

/** Live sparkline preferred; durable score_series backfill on a fresh load. */
function chartIslands(
  live: readonly IslandLiveStateV3[],
  backfill: readonly OptimizerV2IslandProgress[] | undefined,
): IslandLiveState[] {
  const ids = new Set<number>();
  for (const island of live) ids.add(island.island_id);
  for (const progress of backfill ?? []) ids.add(progress.island_id);

  const out: IslandLiveState[] = [];
  for (const id of Array.from(ids).sort((a, b) => a - b)) {
    const liveIsland = live.find((i) => i.island_id === id);
    if (liveIsland && liveIsland.sparkline.length > 0) {
      out.push(liveIsland);
      continue;
    }
    const progress = backfill?.find((b) => b.island_id === id);
    const series = progress?.score_series ?? [];
    if (series.length === 0 && !liveIsland) continue;
    const sparkline: IslandStepPoint[] = series.map((p) => ({
      champion_score: p.champion_score,
      ci_half_width: p.ci_half_width,
      rung_index: p.rung_index,
      state: "iterating",
    }));
    out.push({
      island_id: id,
      rung_index: progress?.rung_index ?? 0,
      champion_score: progress?.champion_score ?? 0,
      ci_half_width: progress?.champion_ci_half_width ?? 0,
      state: progress?.state ?? "iterating",
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

/** Headline = SETTLED champion scores only (an island_step or a completed
 * champion pass). In-flight running means live in the arena's pass strip, never
 * in the headline — mixing a 4/12 provisional mean with settled numbers next to
 * it read as data corruption (observed 2026-06-10). */
function withLiveHeadline(island: IslandLiveStateV3 | null): IslandLiveStateV3 | null {
  if (island === null) return island;
  if (island.champion_score > 0 || island.sparkline.length > 0) return island;
  if (island.championScored !== null) {
    return {
      ...island,
      champion_score: island.championScored.triad,
      ci_half_width: island.championScored.ciHalfWidth,
    };
  }
  return island;
}

/** What the v2 IslandLane receives in v3: rung/headline/sparkline/reasoning/
 * prompt/diff. Its champion→challenger score row is SUPPRESSED — it mixed the
 * latest champion_scored event (e.g. a rung re-baseline) with the previous
 * round's challenger, producing incoherent pairs like "0.423 → 0.406 promoted".
 * The FightArena is the single, stage-labeled source for the duel readout. */
function sanitizedForLane(island: IslandLiveStateV3 | null): IslandLiveStateV3 | null {
  if (island === null) return island;
  return { ...island, championScored: null, challengerScored: null, lastOutcome: null };
}

export function ModelPanelV3({ model, turnMode = "multi" }: ModelPanelV3Props): JSX.Element {
  const stream = useOptimizerV3Stream(model);
  const bf = stream.backfill;
  // Single/multi split: durable islands are filtered to this section's mode; live islands
  // belong to the CURRENT run, so they show only when that run is in this section's mode.
  const showLive = (bf?.run_state?.turn_mode ?? "multi") === turnMode;
  const liveIslands = useMemo(
    () => (showLive ? stream.islands : []),
    [stream.islands, showLive],
  );
  const modeBfIslands = useMemo(
    () => (bf?.islands ?? []).filter((i) => (i.turn_mode ?? "multi") === turnMode),
    [bf, turnMode],
  );
  const runState = bf?.run_state ?? {};

  // Freeze an island's current champion prompt into its seed file (next run starts there).
  const [frozen, setFrozen] = useState<Record<number, string>>({});
  const onFreeze = useCallback(
    async (islandId: number) => {
      setFrozen((f) => ({ ...f, [islandId]: "freezing…" }));
      try {
        const r = await freezeV3Champion(model, islandId);
        setFrozen((f) => ({ ...f, [islandId]: `frozen → seed (${r.chars} chars)` }));
      } catch (e) {
        setFrozen((f) => ({
          ...f,
          [islandId]: e instanceof Error ? `failed: ${e.message}` : "failed",
        }));
      }
    },
    [model],
  );

  // Every derived value below memoizes on its actual inputs — this component
  // re-renders on every SSE tick, so unmemoized maps/sorts would re-run dozens
  // of times a minute for nothing.
  const island0Live = useMemo(
    () => withLiveHeadline(liveIslands.find((i) => i.island_id === 0) ?? null),
    [liveIslands],
  );
  const island1Live = useMemo(
    () => withLiveHeadline(liveIslands.find((i) => i.island_id === 1) ?? null),
    [liveIslands],
  );
  const lane0Live = useMemo(() => sanitizedForLane(island0Live), [island0Live]);
  const lane1Live = useMemo(() => sanitizedForLane(island1Live), [island1Live]);
  const island0Bf = useMemo(
    () => modeBfIslands.find((i) => i.island_id === 0) ?? null,
    [modeBfIslands],
  );
  const island1Bf = useMemo(
    () => modeBfIslands.find((i) => i.island_id === 1) ?? null,
    [modeBfIslands],
  );

  const raceIslands = useMemo(
    () => chartIslands(liveIslands, modeBfIslands),
    [liveIslands, modeBfIslands],
  );

  const skippedTotal = useMemo(
    () => liveIslands.reduce((acc, i) => acc + i.skippedIterations, 0),
    [liveIslands],
  );
  // Live pass progress per island ("champion 4/6") — the every-cycle visibility.
  const liveProgress = useMemo(
    () =>
      liveIslands
        .filter((i) => i.scoringProgress !== null)
        .map((i) => ({ island_id: i.island_id, progress: i.scoringProgress! })),
    [liveIslands],
  );

  // Prompt lineage: LIVE feed (instant, from iteration_completed/seed events)
  // merged with the durable backfill (richer: full challenger text), keyed by
  // (island, iteration) with the durable record preferred when both exist.
  // ORIGINAL: the seed event when present, else derived from the OLDEST audited
  // round's before-prompt (iteration 0's champion IS the seed).
  type LineageRow = {
    readonly island_id: number;
    readonly iteration_index: number;
    readonly accepted: boolean | null;
    readonly challenger_score: number | null;
    readonly text: string | null;
    readonly promptDiff: string | null;
    readonly live: boolean;
  };
  const promptHistory = useMemo<LineageRow[]>(() => {
    const lineage = new Map<string, LineageRow>();
    for (const entry of showLive ? stream.promptFeed : []) {
      lineage.set(`${entry.islandId}:${entry.iteration}`, {
        island_id: entry.islandId,
        iteration_index: entry.iteration,
        accepted: entry.accepted,
        challenger_score: entry.challengerTriad,
        text: entry.championInstruction,
        promptDiff: entry.promptDiff,
        live: true,
      });
    }
    for (const island of modeBfIslands) {
      const history = (island.prompt_history ?? []) as readonly OptimizerV3PromptHistoryEntry[];
      for (const entry of history) {
        lineage.set(`${island.island_id}:${entry.iteration_index}`, {
          island_id: island.island_id,
          iteration_index: entry.iteration_index,
          accepted: entry.accepted,
          challenger_score: entry.challenger_score,
          text: entry.challenger_instruction ?? entry.champion_instruction,
          promptDiff: entry.prompt_diff,
          live: false,
        });
      }
      // Derive the ORIGINAL from the oldest audited round when no seed event exists.
      if (history.length > 0 && !lineage.has(`${island.island_id}:-1`)) {
        const oldest = [...history].sort((a, b) => a.iteration_index - b.iteration_index)[0]!;
        if (oldest.champion_instruction) {
          lineage.set(`${island.island_id}:-1`, {
            island_id: island.island_id,
            iteration_index: -1,
            accepted: null,
            challenger_score: null,
            text: oldest.champion_instruction,
            promptDiff: null,
            live: false,
          });
        }
      }
    }
    return [...lineage.values()].sort(
      (a, b) => b.iteration_index - a.iteration_index || a.island_id - b.island_id,
    );
  }, [stream.promptFeed, modeBfIslands, showLive]);

  const deadIslands = useMemo(() => {
    const dead = new Set<number>(showLive ? runState.dead_islands ?? [] : []);
    for (const island of liveIslands) {
      if (island.state === "dead") dead.add(island.island_id);
    }
    return dead;
    // runState is derived from bf; depend on bf so identity churn doesn't thrash.
  }, [bf, liveIslands, showLive]);  // eslint-disable-line react-hooks/exhaustive-deps
  const phase = stream.phase ?? (runState.phase_b_done ? "B" : runState.phase_a_complete ? "B" : "A");

  return (
    <div className="v2-model-panel panel">
      <div className="v2-model-head">
        <span className="v2-model-dot" style={{ background: modelColor(model) }} />
        <span className="v2-model-name">{model}</span>
        <span className={`v2-stream-badge ${stream.streamStatus}`}>
          {stream.streamStatus} · {stream.received}
        </span>
      </div>

      {/* V3 containment strip: phase / degraded / skips / deaths. */}
      <div className="v3-runstate">
        <span className="v2-summary-item">
          <span className="v2-summary-label">Phase</span>
          <span className="v2-summary-val">{phase}</span>
        </span>
        {runState.champion_score != null && (
          <span className="v2-summary-item">
            <span className="v2-summary-label">Frozen</span>
            <span className="v2-summary-val">{score(runState.champion_score)}</span>
          </span>
        )}
        {skippedTotal > 0 && (
          <span className="v2-summary-item v3-warn">
            <span className="v2-summary-label">Skipped</span>
            <span className="v2-summary-val">{skippedTotal}</span>
          </span>
        )}
        {deadIslands.size > 0 && (
          <span className="v2-summary-item v3-dead">
            <span className="v2-summary-label">Dead islands</span>
            <span className="v2-summary-val">{Array.from(deadIslands).sort().join(", ")}</span>
          </span>
        )}
        {runState.degraded && (
          <span className="v2-summary-item v3-dead">
            <span className="v2-summary-val">DEGRADED</span>
          </span>
        )}
        {runState.status && (
          <span className="v2-summary-item">
            <span className="v2-summary-label">Model</span>
            <span className="v2-summary-val">{runState.status}</span>
          </span>
        )}
        {liveProgress.map(({ island_id, progress }) => (
          <span
            key={island_id}
            className="v2-summary-item v3-progress"
            title="Live pass: conversations judged so far. 'conv' is the LAST single conversation's score; 'mean' is the pass's running mean across judged conversations."
          >
            <span className="v2-summary-label">i{island_id} {progress.role}</span>
            <span className="v2-summary-val">
              {progress.done}/{progress.total}
              {" · conv "}
              {progress.lastItemId} {score(progress.lastConversationMean)}
              {" · mean "}
              {score(progress.runningMean)}
            </span>
          </span>
        ))}
      </div>

      {/* THE RINGS: one live champion-vs-challenger duel per island. */}
      <div className="v3f-rings">
        <FightArena
          live={island0Live}
          backfill={island0Bf}
          stance={island0Bf?.stance ?? "CONCISE"}
        />
        <FightArena
          live={island1Live}
          backfill={island1Bf}
          stance={island1Bf?.stance ?? "EXPLICIT"}
        />
      </div>

      {/* Freeze each island's current champion prompt into its seed (next run starts there). */}
      <div className="v3f-freeze-row">
        {[0, 1].map((iid) => (
          <div key={iid} className="v3f-freeze">
            <button
              className="btn"
              onClick={() => void onFreeze(iid)}
              title={`Freeze island ${iid}'s current champion prompt into ${model}_i${iid}.txt so the next run seeds from it`}
            >
              ❄ Freeze island {iid} champion → seed
            </button>
            {frozen[iid] && <span className="muted v3f-freeze-msg">{frozen[iid]}</span>}
          </div>
        ))}
      </div>

      {/* Trend curve: both islands' champion-triad trajectories on a shared axis. */}
      <div className="v2-model-race">
        <IslandRaceChart islands={raceIslands} height={200} />
      </div>

      <div className="v2-islands-row">
        <IslandLane live={lane0Live} backfill={island0Bf} />
        <IslandLane live={lane1Live} backfill={island1Bf} />
      </div>

      {/* Prompt lineage: ORIGINAL (seed) + every round's prompt, live as rounds
          complete (event-fed) and enriched from the durable audit on the poll. */}
      <div className="v3-prompt-history">
        <div className="v3-prompt-history-head">
          prompt history {promptHistory.length > 0 ? `(${promptHistory.length})` : ""}
          {liveProgress.length > 0 && (
            <span className="v3-ph-cooking">
              {" · next up: "}
              {liveProgress
                .map(
                  ({ island_id, progress }) =>
                    `i${island_id} ${progress.role} ${progress.done}/${progress.total}`,
                )
                .join(" · ")}
            </span>
          )}
        </div>
        {promptHistory.length === 0 ? (
          <div className="v3-ph-empty">
            round 1 in flight — the original and each round's challenger land here
            the moment a round completes
          </div>
        ) : (
          <ul className="v3-prompt-history-list">
            {promptHistory.map((entry) => (
              <li key={`${entry.island_id}-${entry.iteration_index}`}>
                {entry.iteration_index < 0 ? (
                  <span className="v3-ph-flag origin">◈ ORIGINAL</span>
                ) : (
                  <span className={`v3-ph-flag ${entry.accepted ? "ok" : "no"}`}>
                    {entry.accepted ? "✓ promoted" : "✗ kept champion"}
                  </span>
                )}
                <span className="v3-ph-meta">
                  i{entry.island_id}
                  {entry.iteration_index >= 0 ? ` · round ${entry.iteration_index + 1}` : " · seed"}
                  {entry.challenger_score != null ? ` · ${score(entry.challenger_score)}` : ""}
                  {entry.live ? " · live" : ""}
                </span>
                {entry.promptDiff ? (
                  <code className="v3-ph-diff">{entry.promptDiff.slice(0, 260)}…</code>
                ) : null}
                <details className="v3-ph-prompt-detail">
                  <summary>prompt text</summary>
                  <code className="v3-ph-text">{(entry.text ?? "").slice(0, 520)}…</code>
                </details>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Rolling contained-failure feed (newest first, capped at 50). */}
      {stream.failuresFeed.length > 0 && (
        <div className="v3-failures">
          <div className="v3-failures-head">
            contained failures ({stream.failuresFeed.length})
          </div>
          <ul className="v3-failures-list">
            {stream.failuresFeed.slice(0, 8).map((f, feedIndex) => (
              <li key={`${f.item_id}-${f.rep}-${feedIndex}`}>
                <code>{f.item_id}#{f.rep}</code> [{f.stage}
                {f.island_id != null ? ` · i${f.island_id}` : ""}] {f.error}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
