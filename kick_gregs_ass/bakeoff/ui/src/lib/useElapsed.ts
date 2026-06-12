/**
 * useElapsed — a tiny ticking "time since T" hook for live activity displays.
 *
 * The optimizer's first `optimizer_champion_scored` event only fires after the
 * seed champion has been scored across the whole tuning slice, which can take a
 * while. Until then the SSE stream is open but silent, so the Per_Model_View has
 * no per-iteration data to show yet. This hook lets the views render a live
 * "working for Xs" heartbeat from `started_at` alone, so an in-progress seed pass
 * looks alive instead of frozen. Dependency-free; ticks once a second only while
 * `active` is true (so it stops churning re-renders once the run ends).
 */
import { useEffect, useState } from "react";

/** Format a millisecond span as a compact clock-ish string (e.g. 8s, 1m 04s, 1h 02m). */
export function formatElapsed(msSpan: number): string {
  if (!Number.isFinite(msSpan) || msSpan < 0) return "—";
  const totalSec = Math.floor(msSpan / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m > 0) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}

/**
 * Return the elapsed wall-clock time since `sinceIso`, re-rendering ~1×/second
 * while `active`. Returns `null` when `sinceIso` is missing/unparseable.
 */
export function useElapsed(sinceIso: string | null | undefined, active: boolean): string | null {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [active]);

  if (!sinceIso) return null;
  const started = Date.parse(sinceIso);
  if (!Number.isFinite(started)) return null;
  return formatElapsed(now - started);
}
