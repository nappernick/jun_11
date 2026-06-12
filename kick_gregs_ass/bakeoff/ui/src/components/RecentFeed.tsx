/**
 * Live feed of the most recent trial_completed events from the SSE stream.
 * Newest first, capped. Shows model, item, pass/rep, composite, and end-to-end
 * latency — with errored trials visually flagged. Optionally filtered to one
 * focused model.
 */
import type { JSX } from "react";
import type { TrialCompleted } from "../api/types";
import { ms, score, modelColor } from "../lib/format";

export interface RecentFeedProps {
  readonly events: readonly TrialCompleted[];
  readonly focusModel: string | null;
}

export function RecentFeed({ events, focusModel }: RecentFeedProps): JSX.Element {
  const shown = (focusModel ? events.filter((e) => e.model === focusModel) : events).slice(0, 80);

  if (shown.length === 0) {
    return <div className="empty">Awaiting trials…</div>;
  }

  return (
    <div className="feed">
      {shown.map((e, i) => (
        <div className={`feedrow${e.error ? " err" : ""}`} key={`${e.trial_id}-${i}`}>
          <span className="fdot" style={{ background: e.error ? "var(--bad)" : modelColor(e.model) }} />
          <span>
            <span className="fmodel">{e.model}</span>{" "}
            <span className="fitem">
              {e.item_id} · {e.pass}#{e.rep} · {e.answerability}
            </span>
          </span>
          <span className="fnum">{e.error ? "ERR" : score(e.composite)}</span>
          <span className="fnum">{ms(e.end_to_end_ms)}</span>
        </div>
      ))}
    </div>
  );
}
