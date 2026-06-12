/**
 * Bounded in-memory ring buffer of streamed trial_completed events. Newest first,
 * capacity-capped so a long run does not grow the tab's memory without bound. The
 * authoritative totals come from the snapshot poll; this buffer feeds the live
 * feed and the live latency distribution.
 */
import { useCallback, useRef, useState } from "react";
import type { TrialCompleted } from "./types";

export interface TrialBuffer {
  readonly events: readonly TrialCompleted[];
  readonly push: (ev: TrialCompleted) => void;
  readonly seed: (events: readonly TrialCompleted[]) => void;
  readonly clear: () => void;
}

export function useTrialBuffer(capacity = 5000): TrialBuffer {
  const [events, setEvents] = useState<readonly TrialCompleted[]>([]);
  const capRef = useRef(capacity);
  const seenRef = useRef<Set<string>>(new Set());

  const push = useCallback((ev: TrialCompleted) => {
    // Dedupe by trial_id so an event that arrives via both the disk seed and the
    // SSE stream is counted once (otherwise reload-then-live would double-count).
    if (seenRef.current.has(ev.trial_id)) return;
    seenRef.current.add(ev.trial_id);
    setEvents((prev) => {
      const next = [ev, ...prev];
      return next.length > capRef.current ? next.slice(0, capRef.current) : next;
    });
  }, []);

  const seed = useCallback((seedEvents: readonly TrialCompleted[]) => {
    // Replace the buffer with the disk replay (newest-first from the API),
    // resetting the dedupe set to exactly the seeded ids so subsequent SSE
    // events for already-seeded trials are skipped and new ones appended.
    const seen = new Set<string>();
    const deduped: TrialCompleted[] = [];
    for (const ev of seedEvents) {
      if (seen.has(ev.trial_id)) continue;
      seen.add(ev.trial_id);
      deduped.push(ev);
    }
    seenRef.current = seen;
    setEvents(deduped.slice(0, capRef.current));
  }, []);

  const clear = useCallback(() => {
    seenRef.current = new Set();
    setEvents([]);
  }, []);

  return { events, push, seed, clear };
}
