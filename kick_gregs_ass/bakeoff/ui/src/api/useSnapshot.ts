/**
 * Polls GET /api/models for the run snapshot (status + per-model progress).
 *
 * The SSE stream tells us *that* trials land; this poll gives the authoritative
 * per-model planned/done/in_flight/errored counts and run status from the
 * RunController. `refreshNow` forces an immediate re-fetch (e.g. right after a
 * pause/resume/abort control action).
 */
import { useCallback, useEffect, useState } from "react";
import { fetchModels } from "./client";
import type { RunSnapshot } from "./types";

const IDLE_SNAPSHOT: RunSnapshot = {
  status: "idle",
  auto_paused: false,
  auth_refreshes: 0,
  totals: { done: 0, errored: 0 },
  models: {},
};

export interface SnapshotState {
  readonly snapshot: RunSnapshot;
  readonly error: string | null;
  readonly refreshNow: () => void;
}

export function useSnapshot(intervalMs = 1000): SnapshotState {
  const [snapshot, setSnapshot] = useState<RunSnapshot>(IDLE_SNAPSHOT);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const snap = await fetchModels(signal);
      setSnapshot(snap);
      setError(null);
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const refreshNow = useCallback(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const ctrl = new AbortController();
    void load(ctrl.signal);
    const id = window.setInterval(() => {
      void load();
    }, intervalMs);
    return () => {
      ctrl.abort();
      window.clearInterval(id);
    };
  }, [load, intervalMs]);

  return { snapshot, error, refreshNow };
}
