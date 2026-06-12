/**
 * TournamentBracket — shows the tournament timeline: per round the two island
 * champions that fought, the shared rung, head-to-head scores + CI, the winner,
 * and the migration event. Rounds stack to show lineage.
 *
 * Consumes the STATUS-endpoint round shape (`scores[]` keyed by island_id), which
 * is what the v2 status poll returns (bakeoff/app.py optimizer_v2_snapshot).
 */
import type { JSX } from "react";
import { score } from "../lib/format";
import type { OptimizerV2StatusRound } from "../api/types";

export interface TournamentBracketProps {
  readonly rounds: readonly OptimizerV2StatusRound[];
}

export function TournamentBracket({ rounds }: TournamentBracketProps): JSX.Element {
  if (rounds.length === 0) {
    return (
      <div className="v2-bracket-empty muted">
        No tournament rounds yet — islands are still iterating independently.
      </div>
    );
  }

  return (
    <div className="v2-bracket" role="list" aria-label="Tournament bracket">
      <div className="v2-bracket-title">Tournament Bracket</div>
      {rounds.map((r) => {
        const a = r.scores.find((s) => s.island_id === 0) ?? null;
        const b = r.scores.find((s) => s.island_id === 1) ?? null;
        return (
          <div key={r.round} className="v2-bracket-round" role="listitem">
            <div className="v2-bracket-round-head">
              <span className="v2-bracket-rnd">Round {r.round}</span>
              <span className="v2-bracket-rung">@ rung {r.shared_rung ?? "—"}</span>
            </div>
            <div className="v2-bracket-matchup">
              <div className={`v2-bracket-island ${r.winner === 0 ? "winner" : ""}`}>
                <span className="v2-bracket-id">Island 0</span>
                <span className="v2-bracket-score">{score(a?.champion_score ?? null)}</span>
                <span className="v2-bracket-ci">±{score(a?.champion_ci_half_width ?? null)}</span>
              </div>
              <span className="v2-bracket-vs">vs</span>
              <div className={`v2-bracket-island ${r.winner === 1 ? "winner" : ""}`}>
                <span className="v2-bracket-id">Island 1</span>
                <span className="v2-bracket-score">{score(b?.champion_score ?? null)}</span>
                <span className="v2-bracket-ci">±{score(b?.champion_ci_half_width ?? null)}</span>
              </div>
            </div>
            <div className="v2-bracket-result">
              <span className="v2-bracket-winner">
                {r.winner != null ? `Winner: Island ${r.winner}` : "Undecided"}
              </span>
              {r.migration && <span className="v2-bracket-migration">→ migrated</span>}
            </div>
          </div>
        );
      })}
    </div>
  );
}
