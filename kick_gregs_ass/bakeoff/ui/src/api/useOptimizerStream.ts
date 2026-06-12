/**
 * React hook wrapping the optimizer SSE stream for ONE Per_Model_View.
 *
 * This is the data seam for the closed-loop prompt optimizer's live view (design
 * "Live dashboard / Quality-Tab design"; Req 9). It does two jobs at once:
 *
 *  1. **Drives the concurrency gate (Req 1.11 / 9.8).** It subscribes to
 *     `GET /api/stream?model=<target_model>`. The backend's `?model=` param
 *     brackets a `ViewRegistry` subscription for the lifetime of the connection
 *     (`bakeoff/app.py::_view_scoped_stream` → `view_registry.subscription(model)`):
 *     opening the stream marks the model "viewable", closing it (unmount / model
 *     change / disconnect) clears it. The `PerModelOrchestrator` only runs the two
 *     models concurrently when both are viewable, so simply mounting a
 *     Per_Model_View is what makes its model eligible for concurrent optimization.
 *
 *  2. **Consumes the optimizer events filtered to its own `model_channel`
 *     (Req 9.10 / 9.11).** The broker fans EVERY `optimizer_*` event to every
 *     subscriber (the `?model=` only scopes the registry, not the fan-out), so the
 *     hook defensively drops any payload whose `model_channel` is not this view's
 *     model. That keeps the two Target_Models' streams from interleaving
 *     ambiguously even though they ride the same broker.
 *
 * The accumulated state (champion/challenger scores with CIs per iteration, the
 * streamed Author rationale, the current champion prompt, the latest diff +
 * lookback ids, convergence, and the Phase-B number) is exactly what the
 * Per_Model_View renders. There is no replay buffer on the broker, so a late
 * mount starts from the next event; the version lookback is backfilled from
 * `GET /api/quality/optimize/history` by the view.
 */
import { useEffect, useState } from "react";
import type {
  OptimizerAuthorToken,
  OptimizerChampionScored,
  OptimizerConverged,
  OptimizerIterationCompleted,
  OptimizerPhaseB,
} from "./types";

export type OptimizerStreamStatus = "connecting" | "open" | "closed";

/** One scored prompt (champion or challenger) on a slice — triad + 95% CI. */
export interface ScoredPoint {
  readonly triad: number;
  readonly ciHalfWidth: number;
  readonly ciLow: number;
  readonly ciHigh: number;
  readonly perDimension: Readonly<Record<string, number>>;
  readonly abstentionRewardMean: number;
  readonly answeredWhenUnsureRate: number;
  readonly meanCloseness: number;
  readonly retrievalBackend: string;
  readonly phase: string;
  readonly nConversations: number;
}

/** The champion and (optional) challenger scores for one iteration index. */
export interface IterationScores {
  readonly iterationIndex: number;
  readonly champion: ScoredPoint | null;
  readonly challenger: ScoredPoint | null;
}

/** The full accumulated live state for one model's Per_Model_View. */
export interface OptimizerModelState {
  /** Ordered by `iteration_index` — the champion-vs-challenger CI chart series. */
  readonly iterations: readonly IterationScores[];
  /** Streamed Author rationale text, keyed by the iteration it belongs to. */
  readonly rationaleByIteration: ReadonlyMap<number, string>;
  /** The most recent iteration index seen on any event (the "live" iteration). */
  readonly activeIteration: number | null;
  /** Full current champion prompt text (from the latest iteration_completed). */
  readonly championInstruction: string | null;
  /** The latest completed iteration — carries the diff, lookback ids, decision. */
  readonly lastCompleted: OptimizerIterationCompleted | null;
  /** Phase A convergence, once it fires. */
  readonly converged: OptimizerConverged | null;
  /** The final Phase-B validation number, once it lands. */
  readonly phaseB: OptimizerPhaseB | null;
}

export interface OptimizerStreamState extends OptimizerModelState {
  readonly status: OptimizerStreamStatus;
  /** Count of this model's own events consumed (after model_channel filtering). */
  readonly received: number;
}

const EMPTY_STATE: OptimizerModelState = {
  iterations: [],
  rationaleByIteration: new Map(),
  activeIteration: null,
  championInstruction: null,
  lastCompleted: null,
  converged: null,
  phaseB: null,
};

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}

function num(o: Record<string, unknown>, k: string): number | null {
  const v = o[k];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function isChampionScored(v: unknown): v is OptimizerChampionScored {
  if (!isObject(v)) return false;
  return (
    typeof v["model_channel"] === "string" &&
    typeof v["role"] === "string" &&
    typeof v["iteration_index"] === "number" &&
    typeof v["triad"] === "number" &&
    typeof v["ci_half_width"] === "number" &&
    isObject(v["per_dimension"])
  );
}

function isAuthorToken(v: unknown): v is OptimizerAuthorToken {
  if (!isObject(v)) return false;
  return (
    typeof v["model_channel"] === "string" &&
    typeof v["iteration_index"] === "number" &&
    typeof v["delta"] === "string"
  );
}

function isIterationCompleted(v: unknown): v is OptimizerIterationCompleted {
  if (!isObject(v)) return false;
  return (
    typeof v["model_channel"] === "string" &&
    typeof v["iteration_index"] === "number" &&
    typeof v["accepted"] === "boolean" &&
    typeof v["champion_instruction"] === "string" &&
    typeof v["prompt_diff"] === "string" &&
    Array.isArray(v["lookback_version_ids"])
  );
}

function isConverged(v: unknown): v is OptimizerConverged {
  if (!isObject(v)) return false;
  return (
    typeof v["model_channel"] === "string" &&
    typeof v["converged_iteration"] === "number" &&
    typeof v["stop_reason"] === "string"
  );
}

function isPhaseB(v: unknown): v is OptimizerPhaseB {
  if (!isObject(v)) return false;
  return (
    typeof v["model_channel"] === "string" &&
    typeof v["triad"] === "number" &&
    typeof v["ci_half_width"] === "number"
  );
}

function toScoredPoint(ev: OptimizerChampionScored): ScoredPoint {
  return {
    triad: ev.triad,
    ciHalfWidth: ev.ci_half_width,
    ciLow: typeof ev.ci_low === "number" ? ev.ci_low : ev.triad - ev.ci_half_width,
    ciHigh: typeof ev.ci_high === "number" ? ev.ci_high : ev.triad + ev.ci_half_width,
    perDimension: ev.per_dimension ?? {},
    abstentionRewardMean: num(ev as unknown as Record<string, unknown>, "abstention_reward_mean") ?? 0,
    answeredWhenUnsureRate: num(ev as unknown as Record<string, unknown>, "answered_when_unsure_rate") ?? 0,
    meanCloseness: num(ev as unknown as Record<string, unknown>, "mean_closeness") ?? 0,
    retrievalBackend: typeof ev.retrieval_backend === "string" ? ev.retrieval_backend : "",
    phase: typeof ev.phase === "string" ? ev.phase : "",
    nConversations: num(ev as unknown as Record<string, unknown>, "n_conversations") ?? 0,
  };
}

/** Merge a scored point into the ordered iteration list, keyed by index + role. */
function mergeScore(
  iterations: readonly IterationScores[],
  iterationIndex: number,
  role: string,
  point: ScoredPoint,
): IterationScores[] {
  const next = iterations.slice();
  let idx = next.findIndex((it) => it.iterationIndex === iterationIndex);
  if (idx < 0) {
    next.push({ iterationIndex, champion: null, challenger: null });
    next.sort((a, b) => a.iterationIndex - b.iterationIndex);
    idx = next.findIndex((it) => it.iterationIndex === iterationIndex);
  }
  const cur = next[idx];
  if (!cur) return next;
  next[idx] =
    role === "challenger"
      ? { ...cur, challenger: point }
      : { ...cur, champion: point };
  return next;
}

export function useOptimizerStream(model: string): OptimizerStreamState {
  const [status, setStatus] = useState<OptimizerStreamStatus>("connecting");
  const [received, setReceived] = useState(0);
  const [state, setState] = useState<OptimizerModelState>(EMPTY_STATE);

  useEffect(() => {
    // A fresh subscription per model: reset the accumulated state so switching
    // (or remounting) a Per_Model_View never shows another model's residue.
    setState(EMPTY_STATE);
    setReceived(0);
    setStatus("connecting");

    // The `?model=` param brackets the backend ViewRegistry subscription for the
    // life of this connection — opening marks the model viewable (concurrency
    // gate), closing clears it (Req 1.11 / 9.8).
    const es = new EventSource(`/api/stream?model=${encodeURIComponent(model)}`);

    es.onopen = () => setStatus("open");
    es.onerror = () => {
      setStatus(es.readyState === EventSource.CLOSED ? "closed" : "connecting");
    };

    const bump = () => setReceived((n) => n + 1);

    const onChampionScored = (msg: MessageEvent<string>) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(msg.data);
      } catch {
        return;
      }
      if (!isChampionScored(parsed) || parsed.model_channel !== model) return;
      bump();
      const point = toScoredPoint(parsed);
      const { iteration_index: ii, role } = parsed;
      setState((s) => ({
        ...s,
        iterations: mergeScore(s.iterations, ii, role, point),
        activeIteration: Math.max(s.activeIteration ?? ii, ii),
      }));
    };

    const onAuthorToken = (msg: MessageEvent<string>) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(msg.data);
      } catch {
        return;
      }
      if (!isAuthorToken(parsed) || parsed.model_channel !== model) return;
      bump();
      const { iteration_index: ii, delta } = parsed;
      setState((s) => {
        const map = new Map(s.rationaleByIteration);
        map.set(ii, (map.get(ii) ?? "") + delta);
        return { ...s, rationaleByIteration: map, activeIteration: Math.max(s.activeIteration ?? ii, ii) };
      });
    };

    const onIterationCompleted = (msg: MessageEvent<string>) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(msg.data);
      } catch {
        return;
      }
      if (!isIterationCompleted(parsed) || parsed.model_channel !== model) return;
      bump();
      setState((s) => ({
        ...s,
        lastCompleted: parsed,
        championInstruction: parsed.champion_instruction,
        activeIteration: Math.max(s.activeIteration ?? parsed.iteration_index, parsed.iteration_index),
      }));
    };

    const onConverged = (msg: MessageEvent<string>) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(msg.data);
      } catch {
        return;
      }
      if (!isConverged(parsed) || parsed.model_channel !== model) return;
      bump();
      setState((s) => ({ ...s, converged: parsed }));
    };

    const onPhaseB = (msg: MessageEvent<string>) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(msg.data);
      } catch {
        return;
      }
      if (!isPhaseB(parsed) || parsed.model_channel !== model) return;
      bump();
      setState((s) => ({ ...s, phaseB: parsed }));
    };

    es.addEventListener("optimizer_champion_scored", onChampionScored as EventListener);
    es.addEventListener("optimizer_author_token", onAuthorToken as EventListener);
    es.addEventListener("optimizer_iteration_completed", onIterationCompleted as EventListener);
    es.addEventListener("optimizer_converged", onConverged as EventListener);
    es.addEventListener("optimizer_phase_b", onPhaseB as EventListener);

    return () => {
      es.removeEventListener("optimizer_champion_scored", onChampionScored as EventListener);
      es.removeEventListener("optimizer_author_token", onAuthorToken as EventListener);
      es.removeEventListener("optimizer_iteration_completed", onIterationCompleted as EventListener);
      es.removeEventListener("optimizer_converged", onConverged as EventListener);
      es.removeEventListener("optimizer_phase_b", onPhaseB as EventListener);
      // Closing the EventSource ends the backend ViewRegistry subscription scope,
      // clearing this model's "viewable" flag for the concurrency gate.
      es.close();
      setStatus("closed");
    };
  }, [model]);

  return { ...state, status, received };
}
