/**
 * Live monitoring view (Task 13): all-models overview + single-model focus.
 *
 * Composes the snapshot poll (authoritative per-model progress) with the SSE
 * trial stream (live feed + live latency distribution). Everything here is
 * judge-agnostic: progress, error counts, credential-refresh count, and latency
 * are unaffected by the pending scoring-rubric rework.
 */
import { useState } from "react";
import type { JSX } from "react";
import type { RunSnapshot, TrialCompleted } from "../api/types";
import { KpiStrip } from "../components/KpiStrip";
import { ModelTable } from "../components/ModelTable";
import { RecentFeed } from "../components/RecentFeed";
import { LatencyChart } from "../components/LatencyChart";

export interface LiveMonitorProps {
  readonly snapshot: RunSnapshot;
  readonly events: readonly TrialCompleted[];
  readonly snapshotError: string | null;
}

export function LiveMonitor({ snapshot, events, snapshotError }: LiveMonitorProps): JSX.Element {
  const [focusModel, setFocusModel] = useState<string | null>(null);

  return (
    <div className="view">
      {snapshotError && (
        <div className="banner">
          Snapshot poll error: {snapshotError}. The backend may not be running.
        </div>
      )}

      <KpiStrip snapshot={snapshot} />

      <div className="shead">
        <h2>{focusModel ? `Focus · ${focusModel}` : "All models"}</h2>
        <span className="sub">
          {focusModel ? "click the row again to return to the fleet view" : "click a model to focus"}
        </span>
        <span className="rule" />
      </div>

      <div className="grid cols-2">
        <div className="panel">
          <div className="panel-head">
            <div>
              <h3>Fleet progress</h3>
              <div className="ph-sub">planned / done / in-flight / errored per model</div>
            </div>
          </div>
          <ModelTable snapshot={snapshot} selected={focusModel} onSelect={setFocusModel} />
        </div>

        <div className="panel">
          <div className="panel-head">
            <div>
              <h3>End-to-end latency</h3>
              <div className="ph-sub">
                {focusModel ? `${focusModel} · ` : "all models · "}live distribution (p50 box, min–max whiskers)
              </div>
            </div>
          </div>
          <LatencyChart events={events} focusModel={focusModel} />
        </div>
      </div>

      <div className="grid" style={{ marginTop: 16 }}>
        <div className="panel">
          <div className="panel-head">
            <div>
              <h3>Recent trials</h3>
              <div className="ph-sub">live stream · newest first{focusModel ? ` · ${focusModel} only` : ""}</div>
            </div>
          </div>
          <RecentFeed events={events} focusModel={focusModel} />
        </div>
      </div>
    </div>
  );
}
