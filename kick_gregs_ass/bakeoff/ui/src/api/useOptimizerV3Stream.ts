/**
 * useOptimizerV3Stream — the V3 sibling of useOptimizerV2Stream, self-contained
 * so all V3 work stays differentiated from v2.
 *
 * Consumes the v3 dedicated SSE stream (`/api/quality/optimize/v3/stream`) and the
 * v3 status endpoint for durable backfill, accumulating the SAME per-island live
 * state shape as v2 (the IslandLane / IslandRaceChart components are reused) PLUS
 * the v3 containment surface:
 *
 *  * `optimizer_iteration_skipped` — a contained, skipped iteration (per-island
 *    `skippedIterations` counter + the failure list on the feed);
 *  * `optimizer_conversation_failed` — one contained conversation failure (rolling
 *    `failuresFeed`, newest first, capped);
 *  * `optimizer_island_dead` — the island exhausted its consecutive-failure budget
 *    (island `state` becomes `"dead"`);
 *  * `optimizer_phase` — Phase A/B transitions (`phase`).
 *
 * Discipline carried over from the v2 hook: the structural surface reconstructs
 * from the durable status poll, with live SSE deltas layered on top — a page
 * reload never blanks the view.
 */
import { useCallback, useEffect, useState } from "react";
import type {
  OptimizerAuthorToken,
  OptimizerChampionScored,
  OptimizerConversationFailure,
  OptimizerIslandStep,
  OptimizerIterationCompleted,
  OptimizerIterationSkipped,
  OptimizerMigration,
  OptimizerRungEscalated,
  OptimizerScoringProgress,
  OptimizerTournament,
  OptimizerV2TournamentRound,
  OptimizerV3ModelStatus,
  OptimizerV3Status,
} from "./types";
import type {
  IslandIterationOutcome,
  IslandLiveState,
  IslandScoredLite,
  IslandStepPoint,
  V2StreamStatus,
} from "./useOptimizerV2Stream";

/** A live scoring pass's progress for one island ("champion 4/6"). */
export interface ScoringProgress {
  readonly role: string;
  readonly done: number;
  readonly total: number;
  readonly lastItemId: string;
  readonly lastConversationMean: number;
  /** Running mean of all judged conversations in this pass — the provisional
   * headline score while the pass is still in flight. */
  readonly runningMean: number;
}

/** One island's V3 live state: the v2 shape + containment + live pass progress. */
export interface IslandLiveStateV3 extends IslandLiveState {
  readonly skippedIterations: number;
  readonly consecutiveFailures: number;
  /** In-flight pass progress; cleared when the island's step completes. */
  readonly scoringProgress: ScoringProgress | null;
}

/** One live prompt-lineage entry, built the moment an iteration completes (no
 * 3s-poll wait) or when an island announces its seed. iteration -1 = ORIGINAL. */
export interface PromptFeedEntry {
  readonly islandId: number;
  readonly iteration: number;
  readonly accepted: boolean | null;
  readonly challengerTriad: number | null;
  readonly promptDiff: string | null;
  readonly championInstruction: string | null;
}

export interface OptimizerV3StreamState {
  readonly islands: readonly IslandLiveStateV3[];
  readonly tournament_rounds: readonly OptimizerV2TournamentRound[];
  readonly migrations: readonly OptimizerMigration[];
  /** Live prompt lineage (seed + every completed round), newest first, capped. */
  readonly promptFeed: readonly PromptFeedEntry[];
  /** Rolling feed of contained conversation failures (newest first, capped). */
  readonly failuresFeed: readonly OptimizerConversationFailure[];
  /** The latest `optimizer_phase` payload ("A" / "B"). */
  readonly phase: string | null;
  readonly streamStatus: V2StreamStatus;
  readonly received: number;
  /** Durable backfill from the v3 status endpoint (islands + rounds + run_state). */
  readonly backfill: OptimizerV3ModelStatus | null;
}

const FAILURES_FEED_CAP = 50;
const PROMPT_FEED_CAP = 200;

/** Upsert a prompt-feed entry keyed by (island, iteration); newest first. */
function mergePromptFeed(
  feed: readonly PromptFeedEntry[],
  entry: PromptFeedEntry,
): PromptFeedEntry[] {
  const next = feed.filter(
    (e) => !(e.islandId === entry.islandId && e.iteration === entry.iteration),
  );
  next.unshift(entry);
  next.sort((a, b) => b.iteration - a.iteration || a.islandId - b.islandId);
  return next.slice(0, PROMPT_FEED_CAP);
}

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}

interface MutableState {
  readonly islands: readonly IslandLiveStateV3[];
  readonly tournament_rounds: readonly OptimizerV2TournamentRound[];
  readonly migrations: readonly OptimizerMigration[];
  readonly promptFeed: readonly PromptFeedEntry[];
  readonly failuresFeed: readonly OptimizerConversationFailure[];
  readonly phase: string | null;
}

const EMPTY: MutableState = {
  islands: [],
  tournament_rounds: [],
  migrations: [],
  promptFeed: [],
  failuresFeed: [],
  phase: null,
};

function newIsland(island_id: number): IslandLiveStateV3 {
  return {
    island_id,
    rung_index: 0,
    champion_score: 0,
    ci_half_width: 0,
    state: "iterating",
    sparkline: [],
    activeIteration: null,
    championInstruction: null,
    promptDiff: null,
    authorReasoning: null,
    championScored: null,
    challengerScored: null,
    lastOutcome: null,
    skippedIterations: 0,
    consecutiveFailures: 0,
    scoringProgress: null,
  };
}

function upsertIsland(
  islands: readonly IslandLiveStateV3[],
  island_id: number,
  patch: (cur: IslandLiveStateV3) => IslandLiveStateV3,
): IslandLiveStateV3[] {
  const next = islands.slice();
  const idx = next.findIndex((i) => i.island_id === island_id);
  if (idx < 0) {
    next.push(patch(newIsland(island_id)));
    next.sort((a, b) => a.island_id - b.island_id);
  } else {
    next[idx] = patch(next[idx]!);
  }
  return next;
}

async function fetchV3Status(signal?: AbortSignal): Promise<OptimizerV3Status | null> {
  try {
    const init: RequestInit = {};
    if (signal) init.signal = signal;
    const res = await fetch("/api/quality/optimize/v3/status", init);
    if (!res.ok) return null;
    return (await res.json()) as OptimizerV3Status;
  } catch {
    return null;
  }
}

export function useOptimizerV3Stream(model: string): OptimizerV3StreamState {
  const [streamStatus, setStreamStatus] = useState<V2StreamStatus>("connecting");
  const [received, setReceived] = useState(0);
  const [state, setState] = useState<MutableState>(EMPTY);
  const [backfill, setBackfill] = useState<OptimizerV3ModelStatus | null>(null);

  // Durable backfill poll every 3s.
  const loadBackfill = useCallback(
    async (signal?: AbortSignal) => {
      const s = await fetchV3Status(signal);
      if (s?.models?.[model]) {
        setBackfill(s.models[model]!);
      }
    },
    [model],
  );

  useEffect(() => {
    const ctrl = new AbortController();
    void loadBackfill(ctrl.signal);
    const id = window.setInterval(() => void loadBackfill(), 3000);
    return () => {
      ctrl.abort();
      window.clearInterval(id);
    };
  }, [loadBackfill]);

  // SSE subscription for live v3 events.
  useEffect(() => {
    setState(EMPTY);
    setReceived(0);
    setStreamStatus("connecting");

    const es = new EventSource(
      `/api/quality/optimize/v3/stream?model=${encodeURIComponent(model)}`,
    );
    es.onopen = () => setStreamStatus("open");
    es.onerror = () => {
      setStreamStatus(es.readyState === EventSource.CLOSED ? "closed" : "connecting");
    };

    const bump = () => setReceived((n) => n + 1);

    /** Parse + model-channel/island gate shared by every handler. */
    function parsed(msg: MessageEvent<string>, needIsland: boolean): Record<string, unknown> | null {
      let d: unknown;
      try {
        d = JSON.parse(msg.data);
      } catch {
        return null;
      }
      if (!isObj(d)) return null;
      if (d["model_channel"] !== undefined && d["model_channel"] !== model) return null;
      if (needIsland && typeof d["island_id"] !== "number") return null;
      return d;
    }

    const onIslandStep = (msg: MessageEvent<string>) => {
      const d = parsed(msg, true);
      if (!d) return;
      bump();
      const ev = d as unknown as OptimizerIslandStep;
      const point: IslandStepPoint = {
        champion_score: ev.champion_score,
        ci_half_width: ev.ci_half_width,
        rung_index: ev.rung_index,
        state: ev.state,
      };
      setState((s) => ({
        ...s,
        islands: upsertIsland(s.islands, ev.island_id, (cur) => ({
          ...cur,
          rung_index: ev.rung_index,
          champion_score: ev.champion_score,
          ci_half_width: ev.ci_half_width,
          state: cur.state === "dead" ? "dead" : ev.state,
          sparkline: [...cur.sparkline, point],
          scoringProgress: null, // the step completed; the pass is over
        })),
      }));
    };

    const onScoringProgress = (msg: MessageEvent<string>) => {
      const d = parsed(msg, true);
      if (!d || typeof d["done"] !== "number") return;
      bump();
      const ev = d as unknown as OptimizerScoringProgress;
      setState((s) => ({
        ...s,
        islands: upsertIsland(s.islands, ev.island_id, (cur) => {
          const prev = cur.scoringProgress;
          // Restart the running mean when a new pass begins (role change or
          // a done counter that went backwards).
          const isSamePass = prev !== null && prev.role === ev.role && ev.done > prev.done;
          const judgedBefore = isSamePass ? prev.done : 0;
          const meanBefore = isSamePass ? prev.runningMean : 0;
          const runningMean =
            (meanBefore * judgedBefore + ev.conversation_mean) / (judgedBefore + 1);
          return {
            ...cur,
            scoringProgress: {
              role: ev.role,
              done: ev.done,
              total: ev.total,
              lastItemId: ev.item_id,
              lastConversationMean: ev.conversation_mean,
              runningMean,
            },
          };
        }),
      }));
    };

    const onRungEscalated = (msg: MessageEvent<string>) => {
      const d = parsed(msg, true);
      if (!d) return;
      bump();
      const ev = d as unknown as OptimizerRungEscalated;
      setState((s) => ({
        ...s,
        islands: upsertIsland(s.islands, ev.island_id, (cur) => ({
          ...cur,
          rung_index: ev.to_rung,
          state: cur.state === "dead" ? "dead" : "escalating",
        })),
      }));
    };

    const onTournament = (msg: MessageEvent<string>) => {
      const d = parsed(msg, false);
      if (!d || typeof d["round"] !== "number") return;
      bump();
      const ev = d as unknown as OptimizerTournament;
      setState((s) => {
        const next = s.tournament_rounds.slice();
        const rec: OptimizerV2TournamentRound = {
          round: ev.round,
          island_a: ev.island_a,
          island_b: ev.island_b,
          shared_rung: ev.shared_rung,
          winner: ev.winner,
        };
        const existing = next.findIndex((t) => t.round === ev.round);
        if (existing >= 0) next[existing] = rec;
        else {
          next.push(rec);
          next.sort((a, b) => a.round - b.round);
        }
        return { ...s, tournament_rounds: next };
      });
    };

    const onMigration = (msg: MessageEvent<string>) => {
      const d = parsed(msg, false);
      if (!d || typeof d["round"] !== "number") return;
      bump();
      const ev = d as unknown as OptimizerMigration;
      setState((s) => ({ ...s, migrations: [...s.migrations, ev] }));
    };

    const onChampionScored = (msg: MessageEvent<string>) => {
      const d = parsed(msg, true);
      if (!d) return;
      bump();
      const ev = d as unknown as OptimizerChampionScored;
      const lite: IslandScoredLite = {
        triad: ev.triad,
        ciHalfWidth: ev.ci_half_width,
        iterationIndex: ev.iteration_index,
      };
      setState((s) => ({
        ...s,
        islands: upsertIsland(s.islands, ev.island_id as number, (cur) => {
          const active = Math.max(cur.activeIteration ?? ev.iteration_index, ev.iteration_index);
          if (ev.role === "challenger") {
            return { ...cur, challengerScored: lite, activeIteration: active };
          }
          const newIter = cur.championScored?.iterationIndex !== ev.iteration_index;
          return {
            ...cur,
            championScored: lite,
            challengerScored: newIter ? null : cur.challengerScored,
            activeIteration: active,
          };
        }),
      }));
    };

    const onAuthorToken = (msg: MessageEvent<string>) => {
      const d = parsed(msg, true);
      if (!d) return;
      bump();
      const ev = d as unknown as OptimizerAuthorToken;
      setState((s) => ({
        ...s,
        islands: upsertIsland(s.islands, ev.island_id as number, (cur) => {
          const sameIter = cur.activeIteration === ev.iteration_index;
          return {
            ...cur,
            activeIteration: ev.iteration_index,
            authorReasoning: (sameIter ? (cur.authorReasoning ?? "") : "") + ev.delta,
          };
        }),
      }));
    };

    const onIterationCompleted = (msg: MessageEvent<string>) => {
      const d = parsed(msg, true);
      if (!d) return;
      bump();
      const ev = d as unknown as OptimizerIterationCompleted;
      const outcome: IslandIterationOutcome = {
        iterationIndex: ev.iteration_index,
        accepted: ev.accepted,
        challengerTriad: ev.challenger_triad,
        challengerCiHalfWidth: ev.challenger_ci_half_width,
        gainAbsolute: ev.gain_absolute,
        gainPercent: ev.gain_percent,
      };
      setState((s) => ({
        ...s,
        islands: upsertIsland(s.islands, ev.island_id as number, (cur) => ({
          ...cur,
          championInstruction: ev.champion_instruction,
          promptDiff: ev.prompt_diff || cur.promptDiff,
          lastOutcome: outcome,
          activeIteration: Math.max(cur.activeIteration ?? ev.iteration_index, ev.iteration_index),
        })),
        // Live prompt lineage: the round's verdict lands here the instant the
        // iteration completes — no waiting on the durable poll.
        promptFeed: mergePromptFeed(s.promptFeed, {
          islandId: ev.island_id as number,
          iteration: ev.iteration_index,
          accepted: ev.accepted,
          challengerTriad: ev.challenger_triad,
          promptDiff: ev.prompt_diff || null,
          championInstruction: ev.champion_instruction,
        }),
      }));
    };

    // Future runs: the orchestrator announces each island's seed prompt at start;
    // captured as the pinned ORIGINAL entry (iteration -1).
    const onIslandSeeded = (msg: MessageEvent<string>) => {
      const d = parsed(msg, true);
      if (!d || typeof d["champion_instruction"] !== "string") return;
      bump();
      const islandId = d["island_id"] as number;
      const seedText = d["champion_instruction"] as string;
      setState((s) => ({
        ...s,
        islands: upsertIsland(s.islands, islandId, (cur) => ({
          ...cur,
          championInstruction: cur.championInstruction ?? seedText,
        })),
        promptFeed: mergePromptFeed(s.promptFeed, {
          islandId,
          iteration: -1,
          accepted: null,
          challengerTriad: null,
          promptDiff: null,
          championInstruction: seedText,
        }),
      }));
    };

    // -- V3 containment events ------------------------------------------------
    const onIterationSkipped = (msg: MessageEvent<string>) => {
      const d = parsed(msg, true);
      if (!d) return;
      bump();
      const ev = d as unknown as OptimizerIterationSkipped;
      setState((s) => ({
        ...s,
        islands: upsertIsland(s.islands, ev.island_id, (cur) => ({
          ...cur,
          skippedIterations: cur.skippedIterations + 1,
          consecutiveFailures: ev.consecutive_failures,
        })),
        failuresFeed: [
          ...(ev.failures ?? []).map((f) => ({ ...f, island_id: ev.island_id })),
          ...s.failuresFeed,
        ].slice(0, FAILURES_FEED_CAP),
      }));
    };

    const onConversationFailed = (msg: MessageEvent<string>) => {
      const d = parsed(msg, false);
      if (!d || typeof d["item_id"] !== "string") return;
      bump();
      const ev = d as unknown as OptimizerConversationFailure;
      setState((s) => ({
        ...s,
        failuresFeed: [ev, ...s.failuresFeed].slice(0, FAILURES_FEED_CAP),
      }));
    };

    const onIslandDead = (msg: MessageEvent<string>) => {
      const d = parsed(msg, true);
      if (!d) return;
      bump();
      const ev = d as unknown as { island_id: number; consecutive_failures: number };
      setState((s) => ({
        ...s,
        islands: upsertIsland(s.islands, ev.island_id, (cur) => ({
          ...cur,
          state: "dead",
          consecutiveFailures: ev.consecutive_failures,
        })),
      }));
    };

    const onPhase = (msg: MessageEvent<string>) => {
      const d = parsed(msg, false);
      if (!d || typeof d["phase"] !== "string") return;
      bump();
      setState((s) => ({ ...s, phase: d["phase"] as string }));
    };

    es.addEventListener("optimizer_island_step", onIslandStep as EventListener);
    es.addEventListener("optimizer_scoring_progress", onScoringProgress as EventListener);
    es.addEventListener("optimizer_rung_escalated", onRungEscalated as EventListener);
    es.addEventListener("optimizer_tournament", onTournament as EventListener);
    es.addEventListener("optimizer_migration", onMigration as EventListener);
    es.addEventListener("optimizer_champion_scored", onChampionScored as EventListener);
    es.addEventListener("optimizer_author_token", onAuthorToken as EventListener);
    es.addEventListener("optimizer_iteration_completed", onIterationCompleted as EventListener);
    es.addEventListener("optimizer_island_seeded", onIslandSeeded as EventListener);
    es.addEventListener("optimizer_iteration_skipped", onIterationSkipped as EventListener);
    es.addEventListener("optimizer_conversation_failed", onConversationFailed as EventListener);
    es.addEventListener("optimizer_island_dead", onIslandDead as EventListener);
    es.addEventListener("optimizer_phase", onPhase as EventListener);

    return () => {
      es.removeEventListener("optimizer_island_step", onIslandStep as EventListener);
      es.removeEventListener("optimizer_scoring_progress", onScoringProgress as EventListener);
      es.removeEventListener("optimizer_rung_escalated", onRungEscalated as EventListener);
      es.removeEventListener("optimizer_tournament", onTournament as EventListener);
      es.removeEventListener("optimizer_migration", onMigration as EventListener);
      es.removeEventListener("optimizer_champion_scored", onChampionScored as EventListener);
      es.removeEventListener("optimizer_author_token", onAuthorToken as EventListener);
      es.removeEventListener("optimizer_iteration_completed", onIterationCompleted as EventListener);
      es.removeEventListener("optimizer_island_seeded", onIslandSeeded as EventListener);
      es.removeEventListener("optimizer_iteration_skipped", onIterationSkipped as EventListener);
      es.removeEventListener("optimizer_conversation_failed", onConversationFailed as EventListener);
      es.removeEventListener("optimizer_island_dead", onIslandDead as EventListener);
      es.removeEventListener("optimizer_phase", onPhase as EventListener);
      es.close();
      setStreamStatus("closed");
    };
  }, [model]);

  return { ...state, streamStatus, received, backfill };
}
