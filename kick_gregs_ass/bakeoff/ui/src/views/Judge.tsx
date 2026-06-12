/**
 * Judge — the deferred LLM-as-judge view (Phase 2).
 *
 * This is the tab that shows off the grader: Opus 4.x runs AFTER the generation
 * run, on a stratified sample of the outcomes, and produces per-dimension rubric
 * scores, binary pass/fail outcomes, AND written evidence ("why" it scored what
 * it did). The view has three jobs:
 *
 *  1. Control — a "Run / Re-run judging" button (POST /api/judge/start) with the
 *     sample dial (items per model), plus live status/progress while it runs. The
 *     judge auto-chains after a generation run, so this is for re-running or
 *     scaling the sample up on demand.
 *  2. The hard, useful statistics — per model, the mean of each rubric dimension
 *     (continuous) and the binary pass rate (did it clear the bar), side by side,
 *     so an exec sees both "how good" and "how often good".
 *  3. The judge's actual opinions — click a model to expand representative
 *     example verdicts (best / median / worst), each showing the graded answer
 *     excerpt, the judge's quoted evidence, and the per-dimension scores.
 *
 * Data: GET /api/judge/status (polled while running) + GET /api/judge/scores (the
 * per-model rollup). All numbers are normalized to [0, 1] from the 1–5 rubric.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { JSX } from "react";
import { ApiError, fetchJudgeScores, fetchJudgeStatus, startJudge } from "../api/client";
import type { JudgeExample, JudgeModelSummary, JudgeStatus, JudgeSummary } from "../api/types";

const SAMPLE_DEFAULT = 166;

function pct(x: number): string {
  return `${Math.round(x * 100)}%`;
}

function detailMessage(detail: unknown): string | null {
  if (detail && typeof detail === "object" && "detail" in detail) {
    const d = (detail as { detail: unknown }).detail;
    if (typeof d === "string") return d;
  }
  return null;
}

/** A horizontal dual bar: continuous mean (solid) over binary pass rate (ghost). */
function DimensionBar({
  label,
  mean,
  passRate,
}: {
  readonly label: string;
  readonly mean: number;
  readonly passRate: number;
}): JSX.Element {
  return (
    <div className="jdim" title={`${label}: mean ${mean.toFixed(2)} · pass ${pct(passRate)}`}>
      <span className="jdim-name">{label}</span>
      <span className="jdim-track">
        <span className="jdim-pass" style={{ width: pct(passRate) }} />
        <span className="jdim-mean" style={{ width: pct(mean) }} />
      </span>
      <span className="jdim-val">{mean.toFixed(2)}</span>
      <span className="jdim-pct">{pct(passRate)}</span>
    </div>
  );
}

function ExampleCard({
  example,
  dimensions,
}: {
  readonly example: JudgeExample;
  readonly dimensions: readonly string[];
}): JSX.Element {
  const evidenceEntries = Object.entries(example.evidence).filter(([, v]) => v && v.trim());
  return (
    <div className="jex">
      <div className="jex-head">
        <span className="jex-item">{example.item_id}</span>
        <span className={`pill ${example.answerability}`}>{example.answerability}</span>
        <span className="pill state">{example.momentary_state}</span>
        <span className="jex-overall">overall {example.overall.toFixed(2)}</span>
      </div>
      {example.answer_excerpt && (
        <div className="jex-answer">
          <span className="jex-tag">answer</span>
          <p>{example.answer_excerpt}</p>
        </div>
      )}
      {evidenceEntries.length > 0 && (
        <div className="jex-evidence">
          {evidenceEntries.map(([k, v]) => (
            <div key={k} className="jex-ev">
              <span className="jex-tag">{k} evidence</span>
              <blockquote>{v}</blockquote>
            </div>
          ))}
        </div>
      )}
      <div className="jex-dims">
        {dimensions.map((d) => (
          <span key={d} className="jex-dim" title={`${d}: ${(example.dimensions[d] ?? 0).toFixed(2)}`}>
            <i>{d.slice(0, 4)}</i>
            {(example.dimensions[d] ?? 0).toFixed(2)}
          </span>
        ))}
      </div>
    </div>
  );
}

function ModelCard({
  model,
  dimensions,
  expanded,
  onToggle,
}: {
  readonly model: JudgeModelSummary;
  readonly dimensions: readonly string[];
  readonly expanded: boolean;
  readonly onToggle: () => void;
}): JSX.Element {
  const answerability = Object.entries(model.answerability_counts);
  return (
    <div className={`jcard ${expanded ? "open" : ""}`}>
      <button className="jcard-head" onClick={onToggle} aria-expanded={expanded}>
        <span className="jcard-name">{model.model}</span>
        <span className="jcard-overall">{model.overall_mean.toFixed(2)}</span>
        <span className="jcard-n">{model.n_judged} judged</span>
        <span className="jcard-ans">
          {answerability.map(([k, n]) => (
            <span key={k} className={`pill ${k}`}>
              {k} {n}
            </span>
          ))}
        </span>
        <span className="jcard-caret">{expanded ? "▾" : "▸"}</span>
      </button>

      <div className="jcard-dims">
        {dimensions.map((d) => (
          <DimensionBar
            key={d}
            label={d}
            mean={model.dimension_means[d] ?? 0}
            passRate={model.dimension_pass_rates[d] ?? 0}
          />
        ))}
      </div>

      {expanded && (
        <div className="jcard-examples">
          <div className="jcard-examples-head">
            Representative verdicts — best · median · worst (the judge&rsquo;s own words)
          </div>
          {model.examples.map((ex) => (
            <ExampleCard key={ex.trial_id} example={ex} dimensions={dimensions} />
          ))}
          {model.examples.length === 0 && (
            <div className="muted">No example verdicts captured for this model.</div>
          )}
        </div>
      )}
    </div>
  );
}

export function Judge(): JSX.Element {
  const [status, setStatus] = useState<JudgeStatus | null>(null);
  const [summary, setSummary] = useState<JudgeSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sample, setSample] = useState<number>(SAMPLE_DEFAULT);
  const [pending, setPending] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const wasRunning = useRef(false);

  const loadScores = useCallback(async (refresh: boolean) => {
    try {
      const s = await fetchJudgeScores(refresh);
      setSummary(s);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  // Poll status every 1.5s; refresh the scores rollup when a pass finishes.
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const st = await fetchJudgeStatus();
        if (!alive) return;
        setStatus(st);
        const running = st.status === "running";
        if (wasRunning.current && !running) {
          // a pass just finished -> recompute the rollup from disk.
          void loadScores(true);
        }
        wasRunning.current = running;
      } catch {
        /* transient poll error; keep the last good status */
      }
    };
    void tick();
    const id = window.setInterval(tick, 1500);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [loadScores]);

  // Initial scores load.
  useEffect(() => {
    void loadScores(false);
  }, [loadScores]);

  const onRun = useCallback(async () => {
    setError(null);
    setPending(true);
    try {
      const st = await startJudge({ items_per_model: sample });
      setStatus(st);
      wasRunning.current = true;
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setError(detailMessage(e.detail) ?? "Judging already in progress.");
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setPending(false);
    }
  }, [sample]);

  const isRunning = status?.status === "running";
  const dimensions = summary?.dimensions ?? [];
  const models = useMemo(() => summary?.models ?? [], [summary]);
  const progress = status?.progress;

  return (
    <div className="view">
      <div className="shead">
        <h2>The Judge</h2>
        <span className="sub">
          Opus grades a sampled subset after the run · rubric scores, binary outcomes, and its own
          written verdicts
        </span>
        <span className="rule" />
      </div>

      {/* Control strip */}
      <div className="panel">
        <div className="jctl">
          <label className="field">
            <span className="field-label">items / model</span>
            <input
              className="field-input num"
              type="number"
              min={1}
              max={2000}
              step={1}
              value={sample}
              disabled={isRunning || pending}
              onChange={(e) => setSample(Math.max(1, Math.round(Number(e.target.value) || SAMPLE_DEFAULT)))}
              aria-label="items per model to judge"
            />
          </label>
          <button
            className="btn primary"
            disabled={isRunning || pending}
            onClick={() => void onRun()}
          >
            {pending ? "Starting…" : isRunning ? "Judging…" : "Run / Re-run judging"}
          </button>

          <span className="jctl-status">
            <i className={`jdot ${status?.status ?? "idle"}`} />
            {status?.status ?? "idle"}
            {isRunning && progress ? ` · ${progress.judged} judged` : ""}
            {!isRunning && progress && progress.skipped_existing > 0
              ? ` · ${progress.skipped_existing} already judged`
              : ""}
          </span>
        </div>

        <div className="jctl-hint">
          The judge runs out of the generation loop and auto-starts when a run completes. Re-running
          is safe and resumable — it only grades not-yet-judged trials unless you raise the sample.
          {summary && summary.judge_models.length > 0 && (
            <> Judge model: <b>{summary.judge_models.join(", ")}</b>.</>
          )}
        </div>

        {error && <div className="startrun-err">{error}</div>}
        {status?.status === "failed" && status.error && (
          <div className="startrun-err">Judge failed: {status.error}</div>
        )}
      </div>

      {/* Per-model rollups */}
      {models.length > 0 ? (
        <div className="jcards" style={{ marginTop: 16 }}>
          <div className="jcards-legend">
            <span>
              <i className="swatch mean" /> mean rubric score (0–1)
            </span>
            <span>
              <i className="swatch pass" /> pass rate ≥ {summary ? pct(summary.pass_threshold) : "60%"}
            </span>
            <span className="muted">click a model for the judge&rsquo;s example verdicts</span>
          </div>
          {models.map((m) => (
            <ModelCard
              key={m.model}
              model={m}
              dimensions={dimensions}
              expanded={expanded === m.model}
              onToggle={() => setExpanded(expanded === m.model ? null : m.model)}
            />
          ))}
        </div>
      ) : (
        <div className="empty" style={{ marginTop: 16 }}>
          No judge verdicts yet. They appear automatically after a run completes, or hit{" "}
          <b>Run / Re-run judging</b> above once the outcomes store has trials.
        </div>
      )}

      <div className="foot">
        GBBO · deferred judge · the grader runs after generation on a stratified sample (~3k Opus
        attempts by default), so it never stalls or sabotages the candidate data. Means are the
        continuous signal; pass rates are the binary &ldquo;cleared the bar&rdquo; view.
      </div>
    </div>
  );
}
