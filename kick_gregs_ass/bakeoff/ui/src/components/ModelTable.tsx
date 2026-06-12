/**
 * All-models overview: one row per model with status-derived progress
 * (planned / done / in_flight / errored) and a progress bar. Clicking a row
 * focuses that model. Judge-agnostic — progress and error counts are unaffected
 * by the scoring rework.
 */
import type { JSX } from "react";
import type { RunSnapshot } from "../api/types";
import { count, modelColor } from "../lib/format";

export interface ModelTableProps {
  readonly snapshot: RunSnapshot;
  readonly selected: string | null;
  readonly onSelect: (model: string | null) => void;
}

export function ModelTable({ snapshot, selected, onSelect }: ModelTableProps): JSX.Element {
  const rows = Object.entries(snapshot.models).sort(([a], [b]) => a.localeCompare(b));

  if (rows.length === 0) {
    return <div className="empty">No models registered yet. Start a run to populate the fleet.</div>;
  }

  return (
    <table className="dt">
      <thead>
        <tr>
          <th>Model</th>
          <th>Progress</th>
          <th>Done</th>
          <th>In&nbsp;flight</th>
          <th>Errored</th>
          <th>Planned</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([model, c]) => {
          const planned = c.planned || 1;
          const donePct = (c.done / planned) * 100;
          const errPct = (c.errored / planned) * 100;
          const isSel = selected === model;
          return (
            <tr
              key={model}
              className={isSel ? "sel" : undefined}
              onClick={() => onSelect(isSel ? null : model)}
            >
              <td>
                <span className="mtag">
                  <span className="dot" style={{ background: modelColor(model) }} />
                  {model}
                </span>
              </td>
              <td>
                <div className="pbar-track" title={`${donePct.toFixed(0)}% done`}>
                  <div className="pbar-done" style={{ width: `${Math.min(100, donePct)}%` }} />
                  <div
                    className="pbar-err"
                    style={{ width: `${Math.min(100, errPct)}%`, left: `${Math.min(100, donePct)}%` }}
                  />
                </div>
              </td>
              <td className="num">{count(c.done)}</td>
              <td className="num">{count(c.in_flight)}</td>
              <td className={`num errcount${c.errored === 0 ? " zero" : ""}`}>{count(c.errored)}</td>
              <td className="num faint">{count(c.planned)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
