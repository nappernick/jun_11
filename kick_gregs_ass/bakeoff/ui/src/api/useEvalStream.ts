/**
 * useEvalStream — consumes the eval feature's dedicated SSE deltas and merges
 * them with durable backfill from the status endpoint.
 *
 * A direct analog of `useOptimizerV2Stream.ts`, following the same hard-won
 * discipline proven there and in `useSnapshot` / `App.tsx`:
 *
 *  1. Seed ONCE from `/api/eval/instances/recent` (the SSE stream has no replay
 *     buffer, so a reload would otherwise start blank even though records are on
 *     disk).
 *  2. Poll `/api/eval/status` every 3s for DURABLE BACKFILL — the authoritative
 *     full reconstruction of the view state from the Event_Store.
 *  3. Open `/api/eval/stream` (EventSource) for LIVE DELTAS only.
 *
 * All three sources merge into a single `Map` keyed by `instance_id`, so a record
 * that arrives via seed AND backfill AND the live stream is counted exactly once
 * (the dedupe that makes seed/backfill/stream idempotent — Property 6). The map is
 * never cleared after mount, so a reload or a stream reconnect reconstructs from
 * the status poll and NEVER blanks the surface: on reconnect the next 3s poll (and
 * the EventSource's own auto-reconnect) re-establish state before deltas resume.
 *
 * Correctness outranks latency: the merge only ever adds/replaces by id; it never
 * drops a previously seen instance, so the displayed set is monotonic in the
 * underlying record set.
 */
import { useCallback, useEffect, useState } from "react";
import { fetchEvalStatus, fetchRecentEvalInstances } from "./client";
import type { EvalInstance, EvalStatus } from "./types";

export type EvalStreamConnState = "connecting" | "open" | "closed";

export interface EvalStreamState {
  /** All known instances, keyed by instance_id (dedupe across seed/backfill/stream). */
  readonly instances: ReadonlyMap<string, EvalInstance>;
  /** Run lifecycle, last known from the status poll or an `eval_run_status` event. */
  readonly status: "idle" | "running" | "completed" | "failed";
  readonly streamStatus: EvalStreamConnState;
  /** Count of live delta events merged (diagnostic). */
  readonly received: number;
  /** The last durable status poll (the backfill authority). */
  readonly backfill: EvalStatus | null;
}

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}

/**
 * Merge a batch of instances into the accumulated map by `instance_id`. Returns
 * the SAME map reference when nothing changes (so React can skip a re-render), and
 * a new map otherwise. Last-writer-wins per id keeps seed/backfill/stream
 * idempotent: re-seeing an id with the same record is a no-op, and a corrected
 * record replaces the stale one.
 */
function mergeInstances(
  prev: ReadonlyMap<string, EvalInstance>,
  incoming: readonly EvalInstance[],
): ReadonlyMap<string, EvalInstance> {
  if (incoming.length === 0) return prev;
  let next: Map<string, EvalInstance> | null = null;
  for (const inst of incoming) {
    if (!inst || typeof inst.instance_id !== "string") continue;
    const existing = prev.get(inst.instance_id);
    // Skip when the record is byte-for-byte the one we already hold (idempotent).
    if (existing && shallowSameInstance(existing, inst)) continue;
    if (!next) next = new Map(prev);
    next.set(inst.instance_id, inst);
  }
  return next ?? prev;
}

/** Cheap identity check to avoid needless re-renders on a re-sent identical record. */
function shallowSameInstance(a: EvalInstance, b: EvalInstance): boolean {
  return (
    a === b ||
    (a.instance_id === b.instance_id &&
      a.agent_id === b.agent_id &&
      a.session_id === b.session_id &&
      a.instance_index === b.instance_index &&
      a.latency_ms === b.latency_ms &&
      a.status === b.status &&
      a.timestamp === b.timestamp)
  );
}

export function useEvalStream(): EvalStreamState {
  const [instances, setInstances] = useState<ReadonlyMap<string, EvalInstance>>(
    () => new Map(),
  );
  const [status, setStatus] = useState<EvalStreamState["status"]>("idle");
  const [streamStatus, setStreamStatus] = useState<EvalStreamConnState>("connecting");
  const [received, setReceived] = useState(0);
  const [backfill, setBackfill] = useState<EvalStatus | null>(null);

  // Durable backfill poll every 3s — the authoritative reconstruction. Merges
  // (never replaces) into the map, so a transient empty/partial status can never
  // blank an already-populated surface.
  const loadBackfill = useCallback(async (signal?: AbortSignal) => {
    try {
      const s = await fetchEvalStatus(signal);
      setBackfill(s);
      setStatus(s.status);
      if (s.instances && s.instances.length > 0) {
        setInstances((prev) => mergeInstances(prev, s.instances!));
      }
    } catch {
      // Defensive: a failed/aborted poll leaves the existing surface untouched.
    }
  }, []);

  // Seed once from the durable recent-instances replay, then start polling.
  useEffect(() => {
    const ctrl = new AbortController();
    void (async () => {
      try {
        const seed = await fetchRecentEvalInstances(4000, ctrl.signal);
        if (seed.instances.length > 0) {
          setInstances((prev) => mergeInstances(prev, seed.instances));
        }
      } catch {
        // Seed is best-effort; the status poll below reconstructs regardless.
      }
      void loadBackfill(ctrl.signal);
    })();
    const id = window.setInterval(() => void loadBackfill(), 3000);
    return () => {
      ctrl.abort();
      window.clearInterval(id);
    };
  }, [loadBackfill]);

  // Live SSE deltas. The map is NOT reset here — a reconnect layers fresh deltas
  // on top of the backfilled surface rather than blanking it.
  useEffect(() => {
    setStreamStatus("connecting");
    const es = new EventSource("/api/eval/stream");
    es.onopen = () => setStreamStatus("open");
    es.onerror = () => {
      setStreamStatus(es.readyState === EventSource.CLOSED ? "closed" : "connecting");
    };

    const onInstanceAppended = (msg: MessageEvent<string>) => {
      let d: unknown;
      try {
        d = JSON.parse(msg.data);
      } catch {
        return;
      }
      if (!isObj(d)) return;
      // Tolerate BOTH the wrapped {instance:{...}} payload and the bare
      // EvalInstance.to_dict() the publishing store actually emits — otherwise live
      // deltas are silently dropped and points only appear via the 3s backfill poll.
      const wrapped = (d as Record<string, unknown>).instance;
      const inst = (isObj(wrapped) ? wrapped : d) as unknown as EvalInstance;
      if (typeof inst.instance_id !== "string") return;
      setReceived((n) => n + 1);
      setInstances((prev) => mergeInstances(prev, [inst]));
    };

    // A wipe truncates the durable store; clear the in-memory surface to match so
    // every view (3D/2D + the Metrics rollup) live-clears instead of showing stale
    // points until reload. The next 3s backfill poll reads the now-empty store.
    const onWiped = () => {
      setInstances(new Map());
      setReceived(0);
    };

    const onRunStatus = (msg: MessageEvent<string>) => {
      let d: unknown;
      try {
        d = JSON.parse(msg.data);
      } catch {
        return;
      }
      if (!isObj(d) || typeof d["status"] !== "string") return;
      setStatus(d["status"] as EvalStreamState["status"]);
    };

    es.addEventListener("eval_instance_appended", onInstanceAppended as EventListener);
    es.addEventListener("eval_run_status", onRunStatus as EventListener);
    es.addEventListener("eval_wiped", onWiped as EventListener);

    return () => {
      es.removeEventListener("eval_instance_appended", onInstanceAppended as EventListener);
      es.removeEventListener("eval_run_status", onRunStatus as EventListener);
      es.removeEventListener("eval_wiped", onWiped as EventListener);
      es.close();
      setStreamStatus("closed");
    };
  }, []);

  return { instances, status, streamStatus, received, backfill };
}
