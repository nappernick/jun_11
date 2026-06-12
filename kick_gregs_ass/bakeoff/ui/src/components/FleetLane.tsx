/**
 * One lane/card per candidate in the Bake-Off fleet — the "watch them all go"
 * centerpiece. Each lane fuses the authoritative snapshot counts
 * (planned / done / in_flight / errored + progress bar) with the live,
 * buffer-derived approximations the snapshot does not carry: end-to-end
 * (time-to-final-token) latency p50 and mean live composite. An optional TTFT p50
 * lights up only if the SSE payload starts carrying `ttft_ms`.
 *
 * Latency and progress are judge-agnostic; the composite is the live cheap
 * approximation (the defensible CIs live in the exec report), labelled as such.
 */
import type { JSX } from "react";
import type { ModelCounts } from "../api/types";
import type { ModelLiveStats } from "../lib/liveStats";
import { count, ms, score, modelColor } from "../lib/format";

export interface FleetLaneProps {
  readonly model: string;
  readonly counts: ModelCounts;
  readonly live: ModelLiveStats | null;
}

export function FleetLane({ model, counts, live }: FleetLaneProps): JSX.Element {
  const planned = counts.planned || 1;
  const donePct = Math.min(100, (counts.done / planned) * 100);
  const errPct = Math.min(100, (counts.errored / planned) * 100);
  const inFlight = counts.in_flight > 0;
  const color = modelColor(model);

  return (
    <div className={`lane${inFlight ? " active" : ""}`}>
      <div className="lane-head">
        <span className="mtag">
          <span className="dot" style={{ background: color }} />
          <span className="lane-model">{model}</span>
        </span>
        {inFlight && (
          <span className="lane-flight" title={`${counts.in_flight} in flight`}>
            <i />
            {count(counts.in_flight)} in&nbsp;flight
          </span>
        )}
      </div>

      <div className="lane-prog">
        <div className="pbar-track" title={`${donePct.toFixed(0)}% done`}>
          <div className="pbar-done" style={{ width: `${donePct}%` }} />
          <div className="pbar-err" style={{ width: `${errPct}%`, left: `${donePct}%` }} />
        </div>
        <div className="lane-counts num">
          <span>
            {count(counts.done)}
            <span className="faint"> / {count(counts.planned)}</span>
          </span>
          <span className={`errcount${counts.errored === 0 ? " zero" : ""}`}>
            {count(counts.errored)} err
          </span>
        </div>
      </div>

      <div className="lane-metrics">
        <div className="lane-metric">
          <div className="lm-label">ttft p50</div>
          <div className="lm-val num">{ms(live?.ttftP50 ?? null)}</div>
          <div className="lm-foot">time to first token</div>
        </div>
        <div className="lane-metric">
          <div className="lm-label">e2e p50</div>
          <div className="lm-val num">{ms(live?.endToEndP50 ?? null)}</div>
          <div className="lm-foot">time to final token</div>
        </div>
        <div className="lane-metric">
          <div className="lm-label">composite</div>
          <div className="lm-val num">{score(live?.meanComposite ?? null)}</div>
          <div className="lm-foot">live mean (n={count(live?.n ?? 0)})</div>
        </div>
      </div>
    </div>
  );
}
