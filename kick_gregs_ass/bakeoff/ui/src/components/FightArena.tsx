/**
 * FightArena — the gamified champion-vs-challenger ring for one V3 island.
 *
 * Esports-broadcast treatment of the live optimizer duel:
 *
 *  * GOLD corner: the reigning CHAMPION prompt and its official slice score.
 *  * NEON corner: the CHALLENGER — while its pass runs, its score is the live
 *    running mean ticking conversation by conversation.
 *  * Tug-of-war meter between them, with the PROMOTE LINE tick (champion score +
 *    significance threshold) the challenger must clear to take the belt.
 *  * CONTENDER state: the moment the live challenger mean crosses the promote
 *    line mid-pass, the ring ignites (animated glow + label).
 *  * Round stamps: each completed iteration slams a PROMOTED! (belt changes
 *    hands) or DEFENDED stamp across the ring.
 *  * Round counter, win tallies, and a KO'd treatment for dead islands.
 *
 * Pure presentation: everything derives from the live stream state + durable
 * backfill the panel already holds. No new data dependencies.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import type { JSX } from "react";
import type { IslandLiveStateV3 } from "../api/useOptimizerV3Stream";
import type { OptimizerV3IslandProgress } from "../api/types";
import { score } from "../lib/format";
import { playCue } from "../lib/sounds";

/** Promotion significance threshold (config.QUALITY_OPT_SIGNIFICANCE_THRESHOLD). */
const PROMOTE_THRESHOLD = 0.01;
/** How long a round stamp stays slammed across the ring (ms). */
const STAMP_MS = 5000;

export interface FightArenaProps {
  readonly live: IslandLiveStateV3 | null;
  readonly backfill: OptimizerV3IslandProgress | null;
  readonly stance?: string | null;
}

interface RoundStamp {
  readonly kind: "promoted" | "defended";
  readonly iteration: number;
}

function clamp01(value: number): number {
  return Math.max(0, Math.min(1, value));
}

export function FightArena({ live, backfill, stance }: FightArenaProps): JSX.Element {
  // --- resolve the two corners from the best available signal -------------
  // Priority: the island_step state (authoritative CURRENT belt-holder after a
  // completed step — post-promotion this is the NEW champion) > the live pass
  // score of the prompt being measured > durable backfill.
  const championScore =
    (live && live.champion_score > 0 ? live.champion_score : null) ??
    live?.championScored?.triad ??
    backfill?.champion_score ??
    null;

  const progress = live?.scoringProgress ?? null;
  const challengerOfficial = live?.challengerScored?.triad ?? null;
  const challengerLive =
    progress !== null && progress.role === "challenger" && progress.runningMean > 0
      ? progress.runningMean
      : null;
  // Durable fallback: the most recent audited round's challenger score, so the corner shows
  // the last challenger between rounds and on a fresh reload instead of going blank.
  const challengerBackfill = useMemo(() => {
    const history = backfill?.prompt_history ?? [];
    let best: { iteration_index: number; score: number } | null = null;
    for (const entry of history) {
      if (entry.challenger_score != null && (best === null || entry.iteration_index > best.iteration_index)) {
        best = { iteration_index: entry.iteration_index, score: entry.challenger_score };
      }
    }
    return best?.score ?? null;
  }, [backfill?.prompt_history]);
  const challengerScore = challengerOfficial ?? challengerLive ?? challengerBackfill;
  const challengerIsLive = challengerOfficial === null && challengerLive !== null;

  const round =
    (live?.lastOutcome?.iterationIndex ?? live?.activeIteration ?? null) ??
    (backfill?.iterations != null ? backfill.iterations - 1 : null);

  const promoteLine = championScore !== null ? championScore + PROMOTE_THRESHOLD : null;
  const contender =
    championScore !== null &&
    challengerScore !== null &&
    challengerScore >= (promoteLine ?? Infinity);

  const dead = (live?.state ?? backfill?.state) === "dead";

  // Blue comparison readout above the challenger corner: where the challenger stands versus
  // the champion and the promote line. Only meaningful once both scores exist.
  const compare = useMemo(() => {
    if (championScore === null || challengerScore === null) return null;
    const delta = challengerScore - championScore;
    const deltaText = `${delta >= 0 ? "+" : "−"}${score(Math.abs(delta))} vs champion`;
    if (promoteLine !== null && challengerScore >= promoteLine) {
      return `${deltaText} · clears the promote line`;
    }
    if (promoteLine !== null) {
      return `${deltaText} · needs +${score(promoteLine - challengerScore)} to take the belt`;
    }
    return deltaText;
  }, [championScore, challengerScore, promoteLine]);

  // Win tallies from the durable prompt lineage (promotions vs defenses).
  const tallies = useMemo(() => {
    const history = backfill?.prompt_history ?? [];
    let promoted = 0;
    let defended = 0;
    for (const entry of history) {
      if (entry.accepted) promoted += 1;
      else defended += 1;
    }
    return { promoted, defended };
  }, [backfill?.prompt_history]);

  // --- round stamp: slams in when a new iteration outcome lands -----------
  const [stamp, setStamp] = useState<RoundStamp | null>(null);
  const lastSeenIteration = useRef<number | null>(null);
  useEffect(() => {
    const outcome = live?.lastOutcome ?? null;
    if (outcome === null) return;
    if (lastSeenIteration.current === outcome.iterationIndex) return;
    lastSeenIteration.current = outcome.iterationIndex;
    setStamp({
      kind: outcome.accepted ? "promoted" : "defended",
      iteration: outcome.iterationIndex,
    });
    playCue(outcome.accepted ? "promoted" : "defended");
    const timer = window.setTimeout(() => setStamp(null), STAMP_MS);
    return () => window.clearTimeout(timer);
  }, [live?.lastOutcome]);

  // --- contender ignite: fire the cue on the rising edge (the moment the live
  // challenger mean first crosses the promote line), not on every render it
  // stays crossed.
  const wasContender = useRef(false);
  useEffect(() => {
    if (contender && !wasContender.current) {
      playCue("contender");
    }
    wasContender.current = contender;
  }, [contender]);

  // --- tug-of-war meter geometry (scores normalized to 0..1) --------------
  const champFill = championScore !== null ? clamp01(championScore) : 0;
  const chalFill = challengerScore !== null ? clamp01(challengerScore) : 0;

  const passLabel =
    progress !== null
      ? `${progress.role} pass · ${progress.done}/${progress.total} · last ${progress.lastItemId} ${score(progress.lastConversationMean)}`
      : null;

  return (
    <div
      className={`v3f-ring ${contender ? "contender" : ""} ${dead ? "dead" : ""}`}
      data-island={live?.island_id ?? backfill?.island_id ?? 0}
    >
      <div className="v3f-marquee">
        <span className="v3f-round">
          {dead ? "KO" : round !== null && round >= 0 ? `ROUND ${round + 1}` : "WARM-UP"}
        </span>
        <span className="v3f-stance">{stance ?? ""}</span>
        <span className="v3f-tally" title="promotions · defenses (from the audit lineage)">
          <em className="v3f-tally-p">{tallies.promoted}▲</em>
          <em className="v3f-tally-d">{tallies.defended}▣</em>
        </span>
      </div>

      <div className="v3f-corners">
        <div className="v3f-corner champ">
          <span className="v3f-belt" aria-hidden>
            ★
          </span>
          <span className="v3f-corner-label">CHAMPION</span>
          <span className="v3f-corner-score">
            {championScore !== null ? score(championScore) : "—"}
          </span>
        </div>
        <div className="v3f-vs" aria-hidden>
          VS
        </div>
        <div className={`v3f-corner chal ${challengerIsLive ? "live" : ""}`}>
          {compare && (
            <span className="v3f-compare" title="challenger vs champion and the promote line">
              {compare}
            </span>
          )}
          <span className="v3f-corner-label">
            CHALLENGER{challengerIsLive ? " · LIVE" : ""}
          </span>
          <span className="v3f-corner-score">
            {challengerScore !== null ? score(challengerScore) : "—"}
          </span>
          {contender && <span className="v3f-contender-tag">CONTENDER!</span>}
        </div>
      </div>

      {/* Tug-of-war meter: both corners pull toward the center; the promote
          line is the tick the challenger must clear. */}
      <div className="v3f-meter" role="img" aria-label="champion vs challenger score meter">
        <div className="v3f-meter-half champ">
          <div className="v3f-meter-fill" style={{ width: `${champFill * 100}%` }} />
        </div>
        <div className="v3f-meter-half chal">
          <div
            className={`v3f-meter-fill ${challengerIsLive ? "live" : ""}`}
            style={{ width: `${chalFill * 100}%` }}
          />
          {promoteLine !== null && (
            <div
              className="v3f-promote-line"
              style={{ left: `${clamp01(promoteLine) * 100}%` }}
              title={`promote line: ${score(promoteLine)} (champion + ${PROMOTE_THRESHOLD})`}
            />
          )}
        </div>
      </div>

      {passLabel && <div className="v3f-pass">{passLabel}</div>}

      {/* Last completed round's verdict — explicitly labeled history, so old
          numbers never read as the current state. */}
      {live?.lastOutcome && (
        <div className="v3f-lastround">
          LAST ROUND {live.lastOutcome.iterationIndex + 1}:{" "}
          {live.lastOutcome.challengerTriad !== null
            ? `challenger ${score(live.lastOutcome.challengerTriad)} — `
            : ""}
          <em className={live.lastOutcome.accepted ? "won" : "held"}>
            {live.lastOutcome.accepted ? "TOOK THE BELT" : "CHAMPION HELD"}
          </em>
        </div>
      )}

      {stamp && (
        <div key={stamp.iteration} className={`v3f-stamp ${stamp.kind}`}>
          {stamp.kind === "promoted" ? "PROMOTED!" : "DEFENDED"}
        </div>
      )}
      {dead && <div className="v3f-stamp ko">KO</div>}
    </div>
  );
}
