/**
 * useOptimizerV2Stream — consumes the v2 island/tournament SSE events for one
 * model and merges them with durable backfill from the status endpoint.
 *
 * Two layers of live data per model:
 *
 *  1. Island/tournament structure (`optimizer_island_step`, `optimizer_rung_escalated`,
 *     `optimizer_tournament`, `optimizer_migration`) — the rung position, the
 *     champion-score-over-time trend (the per-island `sparkline`), and the bracket.
 *  2. Per-iteration detail (`optimizer_champion_scored`, `optimizer_author_token`,
 *     `optimizer_iteration_completed`) — the live champion prompt text, the Author's
 *     streamed reasoning blurb, the prompt diff, and the previous-vs-current scoring.
 *     These three ride the SAME broker as v1 and are filtered to this view's
 *     `model_channel`; in v2 they additionally carry an `island_id` so each one is
 *     routed to the right island lane (both islands of a model share one channel).
 *
 * Follows the same discipline as useOptimizerStream (v1): the chart/state MUST
 * reconstruct from the status poll (durable backfill) and only layers live SSE
 * deltas on top. A page reload never blanks the structural surface (the per-
 * iteration prompt/reasoning detail is live-only and repopulates on the next step).
 */
import { useCallback, useEffect, useState } from "react";
import type {
  OptimizerAuthorToken,
  OptimizerChampionScored,
  OptimizerIslandStep,
  OptimizerIterationCompleted,
  OptimizerMigration,
  OptimizerRungEscalated,
  OptimizerTournament,
  OptimizerV2ModelStatus,
  OptimizerV2Status,
  OptimizerV2TournamentRound,
} from "./types";

export type V2StreamStatus = "connecting" | "open" | "closed";

/** One island step as accumulated state (sparkline point — the trend-curve point). */
export interface IslandStepPoint {
  readonly champion_score: number;
  readonly ci_half_width: number;
  readonly rung_index: number;
  readonly state: string;
}

/** A single scored prompt (champion or challenger) — the live score readout. */
export interface IslandScoredLite {
  readonly triad: number;
  readonly ciHalfWidth: number;
  readonly iterationIndex: number;
}

/** The accept/reject + gain outcome of the most recently completed iteration. */
export interface IslandIterationOutcome {
  readonly iterationIndex: number;
  readonly accepted: boolean;
  readonly challengerTriad: number | null;
  readonly challengerCiHalfWidth: number | null;
  readonly gainAbsolute: number | null;
  readonly gainPercent: number | null;
}

/** Per-island accumulated live state. */
export interface IslandLiveState {
  readonly island_id: number;
  readonly rung_index: number;
  readonly champion_score: number;
  readonly ci_half_width: number;
  readonly state: string;
  /** Champion-score-over-steps — the per-island trend curve (IslandRaceChart). */
  readonly sparkline: readonly IslandStepPoint[];
  // -- live per-iteration detail (from the three rich events) --
  /** The most recent iteration index seen for this island on any rich event. */
  readonly activeIteration: number | null;
  /** Full current champion prompt text (latest iteration_completed). */
  readonly championInstruction: string | null;
  /** Unified diff of the latest challenger vs the prior champion. */
  readonly promptDiff: string | null;
  /** The Author's streamed reasoning for the active iteration (resets per iteration). */
  readonly authorReasoning: string | null;
  /** The champion's score on the current iteration's rung ("last turn"). */
  readonly championScored: IslandScoredLite | null;
  /** The challenger's score on the current iteration's rung ("current turn"). */
  readonly challengerScored: IslandScoredLite | null;
  /** Accept/reject + gain of the most recent completed iteration. */
  readonly lastOutcome: IslandIterationOutcome | null;
}

/** The full accumulated v2 state for one model. */
export interface OptimizerV2ModelState {
  readonly islands: readonly IslandLiveState[];
  readonly tournament_rounds: readonly OptimizerV2TournamentRound[];
  readonly migrations: readonly OptimizerMigration[];
}

export interface OptimizerV2StreamState extends OptimizerV2ModelState {
  readonly streamStatus: V2StreamStatus;
  readonly received: number;
  /** Durable backfill from the status endpoint. */
  readonly backfill: OptimizerV2ModelStatus | null;
}

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}

const EMPTY: OptimizerV2ModelState = { islands: [], tournament_rounds: [], migrations: [] };

/** A fresh island record with all live-detail fields cleared. */
function newIsland(island_id: number): IslandLiveState {
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
  };
}

/**
 * Get-or-create the island with `island_id` and apply `patch` to it. The three
 * rich events can arrive before the island's first `island_step` (the champion is
 * scored at the top of a step, the step event fires at its end), so the island
 * may not exist yet when a champion_scored / author_token lands.
 */
function upsertIsland(
  islands: readonly IslandLiveState[],
  island_id: number,
  patch: (cur: IslandLiveState) => IslandLiveState,
): IslandLiveState[] {
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

function mergeIslandStep(
  islands: readonly IslandLiveState[],
  ev: OptimizerIslandStep,
): IslandLiveState[] {
  const point: IslandStepPoint = {
    champion_score: ev.champion_score,
    ci_half_width: ev.ci_half_width,
    rung_index: ev.rung_index,
    state: ev.state,
  };
  return upsertIsland(islands, ev.island_id, (cur) => ({
    ...cur,
    rung_index: ev.rung_index,
    champion_score: ev.champion_score,
    ci_half_width: ev.ci_half_width,
    state: ev.state,
    sparkline: [...cur.sparkline, point],
  }));
}

function mergeRungEscalated(
  islands: readonly IslandLiveState[],
  ev: OptimizerRungEscalated,
): IslandLiveState[] {
  return upsertIsland(islands, ev.island_id, (cur) => ({
    ...cur,
    rung_index: ev.to_rung,
    state: "escalating",
  }));
}

function mergeChampionScored(
  islands: readonly IslandLiveState[],
  ev: OptimizerChampionScored,
  island_id: number,
): IslandLiveState[] {
  const lite: IslandScoredLite = {
    triad: ev.triad,
    ciHalfWidth: ev.ci_half_width,
    iterationIndex: ev.iteration_index,
  };
  return upsertIsland(islands, island_id, (cur) => {
    const active = Math.max(cur.activeIteration ?? ev.iteration_index, ev.iteration_index);
    if (ev.role === "challenger") {
      return { ...cur, challengerScored: lite, activeIteration: active };
    }
    // Champion role fires first each step; a new iteration's champion clears the
    // stale challenger so "last vs current" never mixes two iterations.
    const newIter = cur.championScored?.iterationIndex !== ev.iteration_index;
    return {
      ...cur,
      championScored: lite,
      challengerScored: newIter ? null : cur.challengerScored,
      activeIteration: active,
    };
  });
}

function mergeAuthorToken(
  islands: readonly IslandLiveState[],
  ev: OptimizerAuthorToken,
  island_id: number,
): IslandLiveState[] {
  return upsertIsland(islands, island_id, (cur) => {
    const sameIter = cur.activeIteration === ev.iteration_index;
    return {
      ...cur,
      activeIteration: ev.iteration_index,
      authorReasoning: (sameIter ? (cur.authorReasoning ?? "") : "") + ev.delta,
    };
  });
}

function mergeIterationCompleted(
  islands: readonly IslandLiveState[],
  ev: OptimizerIterationCompleted,
  island_id: number,
): IslandLiveState[] {
  const outcome: IslandIterationOutcome = {
    iterationIndex: ev.iteration_index,
    accepted: ev.accepted,
    challengerTriad: ev.challenger_triad,
    challengerCiHalfWidth: ev.challenger_ci_half_width,
    gainAbsolute: ev.gain_absolute,
    gainPercent: ev.gain_percent,
  };
  return upsertIsland(islands, island_id, (cur) => ({
    ...cur,
    championInstruction: ev.champion_instruction,
    promptDiff: ev.prompt_diff || cur.promptDiff,
    lastOutcome: outcome,
    activeIteration: Math.max(cur.activeIteration ?? ev.iteration_index, ev.iteration_index),
  }));
}

function mergeTournament(
  tournaments: readonly OptimizerV2TournamentRound[],
  ev: OptimizerTournament,
): OptimizerV2TournamentRound[] {
  const next = tournaments.slice();
  const existing = next.findIndex((t) => t.round === ev.round);
  const rec: OptimizerV2TournamentRound = {
    round: ev.round,
    island_a: ev.island_a,
    island_b: ev.island_b,
    shared_rung: ev.shared_rung,
    winner: ev.winner,
  };
  if (existing >= 0) {
    next[existing] = rec;
  } else {
    next.push(rec);
    next.sort((a, b) => a.round - b.round);
  }
  return next;
}

/**
 * Fetches the v2 optimizer status for durable backfill. On the v2 endpoint.
 * Falls back gracefully if the endpoint doesn't exist yet.
 */
async function fetchV2Status(signal?: AbortSignal): Promise<OptimizerV2Status | null> {
  try {
    const init: RequestInit = {};
    if (signal) init.signal = signal;
    const res = await fetch("/api/quality/optimize/v2/status", init);
    if (!res.ok) return null;
    return (await res.json()) as OptimizerV2Status;
  } catch {
    return null;
  }
}

export function useOptimizerV2Stream(model: string): OptimizerV2StreamState {
  const [streamStatus, setStreamStatus] = useState<V2StreamStatus>("connecting");
  const [received, setReceived] = useState(0);
  const [state, setState] = useState<OptimizerV2ModelState>(EMPTY);
  const [backfill, setBackfill] = useState<OptimizerV2ModelStatus | null>(null);

  // Durable backfill poll every 3s.
  const loadBackfill = useCallback(
    async (signal?: AbortSignal) => {
      const s = await fetchV2Status(signal);
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

  // SSE subscription for live events.
  useEffect(() => {
    setState(EMPTY);
    setReceived(0);
    setStreamStatus("connecting");

    const es = new EventSource(`/api/quality/optimize/v2/stream?model=${encodeURIComponent(model)}`);
    es.onopen = () => setStreamStatus("open");
    es.onerror = () => {
      setStreamStatus(es.readyState === EventSource.CLOSED ? "closed" : "connecting");
    };

    const bump = () => setReceived((n) => n + 1);

    // -- structural events (island_id-stamped natively) --
    const onIslandStep = (msg: MessageEvent<string>) => {
      let d: unknown;
      try { d = JSON.parse(msg.data); } catch { return; }
      if (!isObj(d) || typeof d["island_id"] !== "number") return;
      bump();
      const ev = d as unknown as OptimizerIslandStep;
      setState((s) => ({ ...s, islands: mergeIslandStep(s.islands, ev) }));
    };

    const onRungEscalated = (msg: MessageEvent<string>) => {
      let d: unknown;
      try { d = JSON.parse(msg.data); } catch { return; }
      if (!isObj(d) || typeof d["island_id"] !== "number") return;
      bump();
      const ev = d as unknown as OptimizerRungEscalated;
      setState((s) => ({ ...s, islands: mergeRungEscalated(s.islands, ev) }));
    };

    const onTournament = (msg: MessageEvent<string>) => {
      let d: unknown;
      try { d = JSON.parse(msg.data); } catch { return; }
      if (!isObj(d) || typeof d["round"] !== "number") return;
      bump();
      const ev = d as unknown as OptimizerTournament;
      setState((s) => ({ ...s, tournament_rounds: mergeTournament(s.tournament_rounds, ev) }));
    };

    const onMigration = (msg: MessageEvent<string>) => {
      let d: unknown;
      try { d = JSON.parse(msg.data); } catch { return; }
      if (!isObj(d) || typeof d["round"] !== "number") return;
      bump();
      const ev = d as unknown as OptimizerMigration;
      setState((s) => ({ ...s, migrations: [...s.migrations, ev] }));
    };

    // -- per-iteration detail events (shared broker; filter by model_channel,
    //    route by island_id). These carry the prompt text / reasoning / scores. --
    const onChampionScored = (msg: MessageEvent<string>) => {
      let d: unknown;
      try { d = JSON.parse(msg.data); } catch { return; }
      if (!isObj(d) || d["model_channel"] !== model) return;
      if (typeof d["island_id"] !== "number") return; // v2-only: must be island-stamped
      bump();
      const ev = d as unknown as OptimizerChampionScored;
      setState((s) => ({ ...s, islands: mergeChampionScored(s.islands, ev, ev.island_id as number) }));
    };

    const onAuthorToken = (msg: MessageEvent<string>) => {
      let d: unknown;
      try { d = JSON.parse(msg.data); } catch { return; }
      if (!isObj(d) || d["model_channel"] !== model) return;
      if (typeof d["island_id"] !== "number") return;
      bump();
      const ev = d as unknown as OptimizerAuthorToken;
      setState((s) => ({ ...s, islands: mergeAuthorToken(s.islands, ev, ev.island_id as number) }));
    };

    const onIterationCompleted = (msg: MessageEvent<string>) => {
      let d: unknown;
      try { d = JSON.parse(msg.data); } catch { return; }
      if (!isObj(d) || d["model_channel"] !== model) return;
      if (typeof d["island_id"] !== "number") return;
      bump();
      const ev = d as unknown as OptimizerIterationCompleted;
      setState((s) => ({ ...s, islands: mergeIterationCompleted(s.islands, ev, ev.island_id as number) }));
    };

    es.addEventListener("optimizer_island_step", onIslandStep as EventListener);
    es.addEventListener("optimizer_rung_escalated", onRungEscalated as EventListener);
    es.addEventListener("optimizer_tournament", onTournament as EventListener);
    es.addEventListener("optimizer_migration", onMigration as EventListener);
    es.addEventListener("optimizer_champion_scored", onChampionScored as EventListener);
    es.addEventListener("optimizer_author_token", onAuthorToken as EventListener);
    es.addEventListener("optimizer_iteration_completed", onIterationCompleted as EventListener);

    return () => {
      es.removeEventListener("optimizer_island_step", onIslandStep as EventListener);
      es.removeEventListener("optimizer_rung_escalated", onRungEscalated as EventListener);
      es.removeEventListener("optimizer_tournament", onTournament as EventListener);
      es.removeEventListener("optimizer_migration", onMigration as EventListener);
      es.removeEventListener("optimizer_champion_scored", onChampionScored as EventListener);
      es.removeEventListener("optimizer_author_token", onAuthorToken as EventListener);
      es.removeEventListener("optimizer_iteration_completed", onIterationCompleted as EventListener);
      es.close();
      setStreamStatus("closed");
    };
  }, [model]);

  return { ...state, streamStatus, received, backfill };
}
