/**
 * Per_Model_View — one dedicated, complete copy of the live optimizer
 * visualization for a single Target_Model (design "Live dashboard / Quality-Tab
 * design"; Req 9.8/9.10). The Quality_Tab renders one of these per Target_Model.
 *
 * It renders, all attributed to its own model (Req 9.10):
 *  - champion vs challenger triad scores WITH CIs across iterations, error bars =
 *    `ci_half_width` (Req 9.2) — `OptimizerTriadChart`;
 *  - the Author's reasoning as it streams (`optimizer_author_token` deltas
 *    appended live; Req 9.3);
 *  - the current champion prompt text (Req 9.4);
 *  - the prompt diff vs a prior version with a ≥ 2-version lookback selector
 *    (Req 9.5), driven by `optimizer_iteration_completed.lookback_version_ids`
 *    plus `GET /api/quality/optimize/history?model=...`;
 *  - an accept/reject decision badge that updates the champion state on each
 *    completed iteration (Req 9.6).
 *
 * Data wiring (Req 9.11 / 1.11 / 9.8): all live state comes from
 * `useOptimizerStream(model)`, which subscribes to `/api/stream?model=<model>`
 * (bracketing the backend ViewRegistry subscription that drives the concurrency
 * gate) and filters every payload to this view's own `model_channel`. The two
 * Target_Models' streams therefore never interleave ambiguously.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import type { JSX } from "react";
import { OptimizerTriadChart } from "../components/OptimizerTriadChart";
import { PromptDiff } from "../components/PromptDiff";
import { fetchOptimizeHistory } from "../api/client";
import { useOptimizerStream } from "../api/useOptimizerStream";
import type { IterationScores, ScoredPoint } from "../api/useOptimizerStream";
import type { OptimizerModelProgress, OptimizerPromptVersion } from "../api/types";
import { modelColor, pct, score } from "../lib/format";
import { useElapsed } from "../lib/useElapsed";

export interface PerModelViewProps {
  readonly model: string;
  /** Per-model durable progress from the status poll (viewable flag, phase, etc.). */
  readonly progress?: OptimizerModelProgress | undefined;
  /** Whether the optimizer run is live, so the view can hint "watching". */
  readonly running: boolean;
  /** ISO timestamp the run started, for the live "working for Xs" heartbeat. */
  readonly startedAt?: string | null;
}

/**
 * A live heartbeat shown while the loop is running but BEFORE the first scored
 * iteration has streamed in. The optimizer's first `optimizer_champion_scored`
 * event only fires once the seed champion has been scored across the whole
 * tuning slice, so without this the view looks frozen during that first pass.
 * It surfaces what we *do* know in real time: that the stream is open, how long
 * the run has been working, how many of this model's events have arrived, and
 * the current phase — so the in-progress seed pass is visibly alive.
 */
function LiveActivityBanner({
  status,
  received,
  elapsed,
  phase,
  iterationsSoFar,
}: {
  readonly status: string;
  readonly received: number;
  readonly elapsed: string | null;
  readonly phase: string | null | undefined;
  readonly iterationsSoFar: number;
}): JSX.Element {
  const scoring = iterationsSoFar === 0;
  return (
    <div className="opt-activity" role="status" aria-live="polite">
      <span className="opt-spinner" aria-hidden />
      <span className="opt-activity-main">
        {scoring
          ? "Scoring the seed champion across the tuning slice…"
          : "Working…"}
      </span>
      <span className="opt-activity-meta">
        <span className={`pill state opt-stream ${status}`}>stream {status}</span>
        {elapsed && <span className="muted">· {elapsed}</span>}
        {phase && <span className="muted">· phase {phase}</span>}
        <span className="muted">· {received} event{received === 1 ? "" : "s"}</span>
      </span>
      <span className="opt-activity-hint muted">
        The first champion score appears once the seed prompt finishes scoring; per-turn
        retrieval + judging are running now.
      </span>
    </div>
  );
}

/** A reverse-chronological version list for the lookback selector (newest first). */
function useHistory(model: string, refreshKey: number): {
  versions: readonly OptimizerPromptVersion[];
  error: string | null;
} {
  const [versions, setVersions] = useState<readonly OptimizerPromptVersion[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    fetchOptimizeHistory(model, ctrl.signal)
      .then((h) => {
        setVersions(h.versions);
        setError(null);
      })
      .catch((e: unknown) => {
        if (e instanceof DOMException && e.name === "AbortError") return;
        setError(e instanceof Error ? e.message : String(e));
      });
    return () => ctrl.abort();
  }, [model, refreshKey]);

  return { versions, error };
}

function DecisionBadge({
  accepted,
  gainAbsolute,
  gainPercent,
}: {
  readonly accepted: boolean;
  readonly gainAbsolute: number | null;
  readonly gainPercent: number | null;
}): JSX.Element {
  const cls = accepted ? "accept" : "reject";
  const label = accepted ? "promoted" : "rejected";
  const gain =
    gainAbsolute == null
      ? "no usable challenger"
      : `Δ ${gainAbsolute >= 0 ? "+" : ""}${score(gainAbsolute)}${
          gainPercent == null ? "" : ` (${gainPercent >= 0 ? "+" : ""}${gainPercent.toFixed(1)}%)`
        }`;
  return (
    <span className={`opt-badge ${cls}`} title={`Challenger ${label}`}>
      <i />
      {label} · {gain}
    </span>
  );
}

export function PerModelView({ model, progress, running, startedAt }: PerModelViewProps): JSX.Element {
  const stream = useOptimizerStream(model);

  // Backfill chart points from DURABLE status so a reload / SSE reconnect never
  // blanks the view. The SSE broker has no replay buffer (a late mount starts
  // from the next event), so the live `stream.iterations` is empty right after a
  // reload even though the run has scored iterations on disk. The status poll
  // carries the latest durable champion/challenger triad + CI per model, so we
  // synthesize a chart point from it and merge the live stream on top (live wins
  // per iteration_index). That makes the graph persistent, not a flicker.
  const chartIterations = useMemo<IterationScores[]>(() => {
    const byIdx = new Map<number, IterationScores>();
    // 1) durable point from the status poll (if the run has scored anything).
    const di = progress?.iteration_index;
    if (di != null && typeof progress?.champion_score === "number") {
      const halfC = progress.champion_ci_half_width ?? 0;
      const champ: ScoredPoint = {
        triad: progress.champion_score,
        ciHalfWidth: halfC,
        ciLow: progress.champion_score - halfC,
        ciHigh: progress.champion_score + halfC,
        perDimension: {},
        abstentionRewardMean: 0,
        answeredWhenUnsureRate: 0,
        meanCloseness: 0,
        retrievalBackend: "",
        phase: progress.phase ?? "",
        nConversations: 0,
      };
      const chall: ScoredPoint | null =
        typeof progress.challenger_score === "number"
          ? { ...champ, triad: progress.challenger_score, ciLow: progress.challenger_score, ciHigh: progress.challenger_score, ciHalfWidth: 0 }
          : null;
      byIdx.set(di, { iterationIndex: di, champion: champ, challenger: chall });
    }
    // 2) live stream points override the durable ones for the same iteration.
    for (const it of stream.iterations) byIdx.set(it.iterationIndex, it);
    return Array.from(byIdx.values()).sort((a, b) => a.iterationIndex - b.iterationIndex);
  }, [stream.iterations, progress]);

  // "Has any data anywhere" — live OR durable. Only when neither has a score do
  // we show the working banner (so it never replaces a graph that already exists).
  const hasAnyScore = chartIterations.length > 0;
  const elapsed = useElapsed(startedAt, running && !hasAnyScore);
  const showActivity = running && !hasAnyScore;

  // Refetch the version history each time an iteration completes so the lookback
  // selector picks up the just-written version (the live diff is also shown).
  const refreshKey = stream.lastCompleted?.iteration_index ?? -1;
  const { versions, error: historyError } = useHistory(model, refreshKey);

  // Lookback selector: newest-first list of prior prompt versions. Default to the
  // most recent prior version (≥ 2-version lookback is supported by the full
  // history list; the diff for any selected version is `PromptVersion.diff`).
  const ordered = useMemo(
    () => versions.slice().sort((a, b) => b.iteration_index - a.iteration_index),
    [versions],
  );
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null);

  // Keep the selection pointed at the newest version as new ones arrive, unless
  // the user has explicitly picked an older one that still exists.
  useEffect(() => {
    if (ordered.length === 0) {
      setSelectedVersionId(null);
      return;
    }
    setSelectedVersionId((cur) => {
      if (cur && ordered.some((v) => v.prompt_version_id === cur)) return cur;
      return ordered[0]?.prompt_version_id ?? null;
    });
  }, [ordered]);

  const selectedVersion = useMemo(
    () => ordered.find((v) => v.prompt_version_id === selectedVersionId) ?? null,
    [ordered, selectedVersionId],
  );

  const onSelectVersion = useCallback((id: string) => setSelectedVersionId(id), []);

  // The diff shown: the selected lookback version's stored diff if a non-latest
  // version is chosen, otherwise the live diff from the latest completed iteration.
  const liveDiff = stream.lastCompleted?.prompt_diff ?? null;
  const isLatestSelected = ordered.length > 0 && selectedVersionId === ordered[0]?.prompt_version_id;
  const diffToShow =
    selectedVersion && !isLatestSelected ? selectedVersion.diff : (liveDiff ?? selectedVersion?.diff ?? "");

  // The current champion prompt text: live value preferred, else the latest
  // version's champion/challenger text from history.
  const championText =
    stream.championInstruction ??
    ordered[0]?.challenger_instruction ??
    ordered[0]?.champion_instruction ??
    null;

  // Streamed Author rationale for the active iteration (Req 9.3).
  const activeIter = stream.activeIteration;
  const rationale =
    activeIter != null ? (stream.rationaleByIteration.get(activeIter) ?? "") : "";

  const lastCompleted = stream.lastCompleted;
  const color = modelColor(model);
  const viewable = progress?.viewable ?? stream.status === "open";

  return (
    <section className="opt-view" aria-label={`Optimizer view for ${model}`}>
      <header className="opt-view-head">
        <span className="opt-dot" style={{ background: color }} />
        <h3 className="opt-model">{model}</h3>
        <span className={`pill state opt-stream ${stream.status}`}>
          stream {stream.status}
        </span>
        <span className="pill state" title="Marked viewable drives the concurrency gate (Req 1.11)">
          {viewable ? "viewable" : "not viewable"}
        </span>
        {progress?.phase && (
          <span className="pill state">phase {progress.phase}</span>
        )}
        {stream.converged && (
          <span className="pill full" title={stream.converged.stop_reason}>
            converged @ {stream.converged.converged_iteration}
          </span>
        )}
        {stream.phaseB && (
          <span className="pill full" title="Final validation triad (Phase B)">
            phase B {score(stream.phaseB.triad)} ±{score(stream.phaseB.ci_half_width)}
          </span>
        )}
      </header>

      {/* Live heartbeat while the loop runs but no score has streamed yet. */}
      {showActivity && (
        <LiveActivityBanner
          status={stream.status}
          received={stream.received}
          elapsed={elapsed}
          phase={progress?.phase}
          iterationsSoFar={stream.iterations.length}
        />
      )}

      {/* Champion vs challenger triad with CIs across iterations (Req 9.2). */}
      <div className="panel">
        <div className="panel-title">
          Champion vs challenger triad — with 95% CIs across iterations
        </div>
        <OptimizerTriadChart
          iterations={chartIterations}
          phaseB={
            stream.phaseB
              ? { triad: stream.phaseB.triad, ciHalfWidth: stream.phaseB.ci_half_width }
              : null
          }
        />
        {!hasAnyScore && (
          <div className="muted opt-chart-empty">
            {running
              ? "Champion vs challenger triad scores light up here as each iteration finishes scoring."
              : "Champion vs challenger triad scores appear here as the loop scores each iteration."}
          </div>
        )}
        {lastCompleted && (
          <div className="opt-decision">
            <span className="muted">iteration {lastCompleted.iteration_index}</span>
            <DecisionBadge
              accepted={lastCompleted.accepted}
              gainAbsolute={lastCompleted.gain_absolute}
              gainPercent={lastCompleted.gain_percent}
            />
            <span className="muted">
              non-improving streak {lastCompleted.consecutive_non_improving}
            </span>
          </div>
        )}
      </div>

      <div className="opt-grid">
        {/* Author reasoning stream (Req 9.3). */}
        <div className="panel">
          <div className="panel-title">
            Author reasoning {activeIter != null ? `· iteration ${activeIter}` : ""}
          </div>
          {rationale ? (
            <pre className="opt-rationale">{rationale}</pre>
          ) : (
            <div className="muted opt-rationale-empty">
              {!running
                ? "The Author's rationale streams here while the loop runs."
                : (progress?.iteration_index ?? 0) < 1 && stream.activeIteration == null
                  ? "Iteration 0 is the seed champion (no authoring yet). The Author starts rewriting at iteration 1, once the seed is scored — its reasoning will stream here then."
                  : "Waiting for the Author to start reasoning…"}
            </div>
          )}
        </div>

        {/* Current champion prompt text (Req 9.4). */}
        <div className="panel">
          <div className="panel-title">Current champion prompt</div>
          {championText ? (
            <pre className="opt-prompt">{championText}</pre>
          ) : (
            <div className="muted opt-prompt-empty">
              The current champion prompt appears here once the first iteration is scored.
            </div>
          )}
        </div>
      </div>

      {/* Prompt diff vs a prior version with ≥ 2-version lookback (Req 9.5). */}
      <div className="panel">
        <div className="opt-diff-head">
          <div className="panel-title" style={{ padding: 0 }}>
            Prompt diff vs prior version
          </div>
          <label className="opt-lookback">
            <span className="field-label">lookback</span>
            <select
              className="opt-select"
              value={selectedVersionId ?? ""}
              disabled={ordered.length === 0}
              onChange={(e) => onSelectVersion(e.target.value)}
              aria-label="Select a prompt version to view its diff"
            >
              {ordered.map((v, i) => (
                <option key={v.prompt_version_id} value={v.prompt_version_id}>
                  {`v${v.iteration_index}`}
                  {i === 0 ? " (latest)" : ""}
                  {v.score != null ? ` · ${score(v.score)}` : ""}
                  {` · ${v.accepted ? "accepted" : "rejected"}`}
                </option>
              ))}
            </select>
          </label>
        </div>
        {historyError && <div className="startrun-err">{historyError}</div>}
        <PromptDiff diff={diffToShow} />
        {ordered.length < 2 && (
          <div className="jctl-hint">
            Lookback compares against earlier versions; it fills in as the loop produces more
            prompt versions (≥ 2 versions enables multi-step lookback).
          </div>
        )}
      </div>

      {/* Compact per-dimension + abstention summary for the active scored point. */}
      <DimensionSummary
        iterations={stream.iterations}
        activeIteration={activeIter}
      />
    </section>
  );
}

/** A small breakdown of the latest scored champion's per-dimension + abstention numbers. */
function DimensionSummary({
  iterations,
  activeIteration,
}: {
  readonly iterations: readonly IterationScores[];
  readonly activeIteration: number | null;
}): JSX.Element | null {
  const point = useMemo<ScoredPoint | null>(() => {
    if (activeIteration == null) return null;
    const it = iterations.find((x) => x.iterationIndex === activeIteration);
    return it?.challenger ?? it?.champion ?? null;
  }, [iterations, activeIteration]);

  if (!point) return null;
  const dims = Object.entries(point.perDimension);
  return (
    <div className="panel">
      <div className="panel-title">Latest scored breakdown</div>
      <div className="opt-dims">
        {dims.map(([k, v]) => (
          <span key={k} className="jex-dim" title={`${k}: ${score(v)}`}>
            <i>{k.slice(0, 4)}</i>
            {score(v, 2)}
          </span>
        ))}
        <span className="jex-dim" title="mean abstention-correctness contribution">
          <i>abst</i>
          {score(point.abstentionRewardMean, 2)}
        </span>
        <span className="jex-dim" title="fraction of turns that answered when unsure (penalized)">
          <i>unsure</i>
          {pct(point.answeredWhenUnsureRate)}
        </span>
        <span className="jex-dim" title="secondary semantic-closeness cross-check (never decides)">
          <i>close</i>
          {score(point.meanCloseness, 2)}
        </span>
        {point.retrievalBackend && (
          <span className="pill state" title="held-constant retrieval backend">
            {point.retrievalBackend}
          </span>
        )}
      </div>
    </div>
  );
}
