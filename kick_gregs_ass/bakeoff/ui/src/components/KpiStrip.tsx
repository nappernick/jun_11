/**
 * Run-level KPIs derived from the snapshot: completed, in-flight, errored, and
 * credential refreshes (the auth-expiry resilience signal). All judge-agnostic.
 */
import type { JSX } from "react";
import type { RunSnapshot } from "../api/types";
import { count, pct } from "../lib/format";

export interface KpiStripProps {
  readonly snapshot: RunSnapshot;
}

export function KpiStrip({ snapshot }: KpiStripProps): JSX.Element {
  const models = Object.values(snapshot.models);
  const planned = models.reduce((s, m) => s + m.planned, 0);
  const inFlight = models.reduce((s, m) => s + m.in_flight, 0);
  const done = snapshot.totals.done;
  const errored = snapshot.totals.errored;
  const completed = done + errored;
  const errRate = completed > 0 ? errored / completed : 0;
  const progress = planned > 0 ? completed / planned : 0;

  return (
    <div className="kpis">
      <div className="kpi">
        <div className="k-label">Completed</div>
        <div className="k-val">{count(completed)}</div>
        <div className="k-foot">{pct(progress)} of {count(planned)} planned</div>
      </div>
      <div className="kpi">
        <div className="k-label">In flight</div>
        <div className="k-val">{count(inFlight)}</div>
        <div className="k-foot">running now</div>
      </div>
      <div className="kpi">
        <div className="k-label">Errored</div>
        <div className="k-val" style={errored > 0 ? { color: "var(--bad)" } : undefined}>
          {count(errored)}
        </div>
        <div className="k-foot">{pct(errRate, 1)} error rate</div>
      </div>
      <div className="kpi">
        <div className="k-label">Cred refreshes</div>
        <div className="k-val" style={snapshot.auth_refreshes > 0 ? { color: "var(--warn)" } : undefined}>
          {count(snapshot.auth_refreshes)}
        </div>
        <div className="k-foot">{snapshot.auto_paused ? "auto-paused" : "auth recoveries"}</div>
      </div>
    </div>
  );
}
