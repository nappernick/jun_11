/**
 * React hook wrapping the SSE stream at GET /api/stream.
 *
 * The backend (bakeoff/app.py SSEBroker) emits one `trial_completed` event per
 * appended TrialEvent, plus session-change notifications and periodic keepalive
 * comments. EventSource auto-reconnects on transient drops, which suits a long
 * live run. Trial payloads are validated against the TrialCompleted shape before
 * being handed to the consumer.
 */
import { useEffect, useRef, useState } from "react";
import type { TrialCompleted } from "./types";

export type StreamStatus = "connecting" | "open" | "closed";

function isTrialCompleted(v: unknown): v is TrialCompleted {
  if (typeof v !== "object" || v === null) return false;
  const o = v as Record<string, unknown>;
  return (
    typeof o["trial_id"] === "string" &&
    typeof o["model"] === "string" &&
    typeof o["item_id"] === "string" &&
    typeof o["error"] === "boolean"
  );
}

export interface EventStreamState {
  readonly status: StreamStatus;
  readonly received: number;
  readonly last: TrialCompleted | null;
}

export function useEventStream(
  onTrial?: (ev: TrialCompleted) => void,
  onBakeOffSessionChanged?: () => void,
): EventStreamState {
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const [received, setReceived] = useState(0);
  const [last, setLast] = useState<TrialCompleted | null>(null);
  const onTrialRef = useRef(onTrial);
  onTrialRef.current = onTrial;
  const onBakeOffSessionChangedRef = useRef(onBakeOffSessionChanged);
  onBakeOffSessionChangedRef.current = onBakeOffSessionChanged;

  useEffect(() => {
    const es = new EventSource("/api/stream");

    es.onopen = () => setStatus("open");
    es.onerror = () => {
      setStatus(es.readyState === EventSource.CLOSED ? "closed" : "connecting");
    };

    const handleTrial = (msg: MessageEvent<string>) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(msg.data);
      } catch {
        return;
      }
      if (!isTrialCompleted(parsed)) return;
      setReceived((n) => n + 1);
      setLast(parsed);
      onTrialRef.current?.(parsed);
    };
    const handleBakeOffSessionChanged = () => {
      onBakeOffSessionChangedRef.current?.();
    };

    es.addEventListener("trial_completed", handleTrial as EventListener);
    es.addEventListener("bakeoff_session_changed", handleBakeOffSessionChanged);
    es.onmessage = handleTrial;

    return () => {
      es.removeEventListener("trial_completed", handleTrial as EventListener);
      es.removeEventListener("bakeoff_session_changed", handleBakeOffSessionChanged);
      es.close();
      setStatus("closed");
    };
  }, []);

  return { status, received, last };
}
