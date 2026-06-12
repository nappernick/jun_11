/**
 * CoverageLadderRail — the stepped coverage indicator showing rungs
 * (6→12→24→40→60) with each island's marker at its current rung
 * and the CI it can resolve there.
 *
 * This is the "fast early, slower as confidence grows" story made visible.
 */
import type { JSX } from "react";
import { score } from "../lib/format";

export interface LadderIslandMarker {
  readonly island_id: number;
  readonly rung_index: number;
  readonly ci_half_width: number | null;
  readonly model: string;
}

export interface CoverageLadderRailProps {
  readonly markers: readonly LadderIslandMarker[];
}

/**
 * The rung definitions: index → { size, CI resolvable at SD≈0.228 }.
 * Mirrors config QUALITY_OPT_RUNG_SIZES (6,12,24,40,60) x QUALITY_OPT_RUNG_REPS
 * (3,2,1,1,1); `ci` is 1.96·SD/sqrt(size·reps) i.e. the CI at each rung's actual
 * scored-conversation count (18, 24, 24, 40, 60).
 */
const RUNGS: readonly { size: number; ci: number }[] = [
  { size: 6, ci: 0.105 },
  { size: 12, ci: 0.091 },
  { size: 24, ci: 0.091 },
  { size: 40, ci: 0.071 },
  { size: 60, ci: 0.058 },
];

export function CoverageLadderRail({ markers }: CoverageLadderRailProps): JSX.Element {
  return (
    <div className="v2-ladder" role="img" aria-label="Coverage ladder rail">
      <div className="v2-ladder-title">Coverage Ladder</div>
      <div className="v2-ladder-rungs">
        {RUNGS.map((rung, idx) => {
          const onThisRung = markers.filter((m) => m.rung_index === idx);
          return (
            <div key={idx} className="v2-ladder-rung">
              <div className="v2-ladder-step">
                <span className="v2-ladder-size">n={rung.size}</span>
                <span className="v2-ladder-ci">±{score(rung.ci)}</span>
              </div>
              <div className="v2-ladder-markers">
                {onThisRung.map((m) => (
                  <span
                    key={`${m.model}-${m.island_id}`}
                    className="v2-ladder-marker"
                    title={`${m.model} island ${m.island_id}${m.ci_half_width != null ? ` (±${score(m.ci_half_width)})` : ""}`}
                  >
                    {m.island_id}
                  </span>
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
