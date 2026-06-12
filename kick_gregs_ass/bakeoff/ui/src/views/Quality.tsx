/**
 * Quality — the multi-turn quality study view (a SEPARATE study from the bake-off).
 *
 * This study takes the multi-turn dataset and measures, per turn, how CLOSE each
 * model's answer is to the correct answer — turn-1 against the gold-derived ideal
 * (or abstention-correctness when turn-1 is unanswerable), each later turn against
 * that turn's `wants`. The headline is the **turn-drift curve**: closeness by turn
 * position, which shows whether (and how fast) a model drifts from the correct
 * answer as the conversation deepens (the conversational feed-forward compounding).
 *
 * Only two models are under test here (Sonnet 4.6 thinking-off, Haiku 4.5). The
 * view shows, per model: the drift curve, the gold-anchored turn-1 vs wants-
 * anchored later-turn split (kept separate on purpose — they measure against
 * different ground truth), the Phase-2 judged fraction, and example conversations
 * (best / median / worst) with each turn's answer, reference, and closeness.
 *
 * Data: GET /api/quality/summary. Empty-but-graceful until the quality run + judge
 * have produced outcomes.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import type { JSX } from "react";
import type { EChartsOption } from "echarts";
import { EChart } from "../components/EChart";
import { fetchQualitySummary } from "../api/client";
import type {
  QualityExample,
  QualityModelSummary,
  QualitySummary,
} from "../api/types";
import { QualityOptimizer } from "./QualityOptimizer";

/** Quality_Tab sub-sections: the existing closeness study, and the closed-loop optimizer. */
type QualitySection = "closeness" | "optimizer";

const SERIES_COLORS = ["#7c5cff", "#22b8a6", "#e0a458", "#d9596b"] as const;

function seriesColor(i: number): string {
  return SERIES_COLORS[i % SERIES_COLORS.length] ?? "#7c5cff";
}

function pct(x: number): string {
  return `${Math.round(x * 100)}%`;
}

function kindPill(kind: string): string {
  if (kind === "gold") return "full";
  if (kind === "abstention") return "none";
  return "partial"; // "wants"
}

/** The turn-drift line chart: one series per model, x = turn position. */
function DriftChart({ models }: { readonly models: readonly QualityModelSummary[] }): JSX.Element {
  const option = useMemo<EChartsOption>(() => {
    const maxTurn = models.reduce(
      (mx, m) => Math.max(mx, ...m.turn_closeness.map((t) => t.turn)),
      0,
    );
    const turns = Array.from({ length: maxTurn }, (_, i) => i + 1);
    return {
      grid: { left: 44, right: 16, top: 28, bottom: 32 },
      tooltip: { trigger: "axis", valueFormatter: (v) => (v == null ? "—" : Number(v).toFixed(3)) },
      legend: { top: 0, textStyle: { color: "#b9c0d4" } },
      xAxis: {
        type: "category",
        data: turns.map((t) => `turn ${t}`),
        axisLabel: { color: "#9aa3bd" },
      },
      yAxis: {
        type: "value",
        min: 0,
        max: 1,
        axisLabel: { color: "#9aa3bd" },
        splitLine: { lineStyle: { color: "rgba(255,255,255,0.06)" } },
      },
      series: models.map((m, i) => ({
        name: m.model,
        type: "line",
        smooth: true,
        symbolSize: 7,
        lineStyle: { width: 2 },
        color: seriesColor(i),
        data: turns.map((t) => {
          const pt = m.turn_closeness.find((p) => p.turn === t);
          return pt && !pt.insufficient_data ? pt.mean : null;
        }),
      })),
    };
  }, [models]);

  return <EChart option={option} height={300} ariaLabel="Per-turn closeness drift by model" />;
}

function ExampleConversation({ ex }: { readonly ex: QualityExample }): JSX.Element {
  return (
    <div className="qex">
      <div className="qex-head">
        <span className="jex-item">{ex.item_id}</span>
        <span className="pill state">{ex.prompt_variant_id}</span>
        <span className="jex-overall">mean {ex.mean_closeness.toFixed(2)}</span>
      </div>
      {ex.turns.map((t) => (
        <div key={t.turn} className="qex-turn">
          <div className="qex-turn-head">
            <b>turn {t.turn}</b>
            <span className={`pill ${kindPill(t.ground_truth_kind)}`}>{t.ground_truth_kind}</span>
            {t.response_dependent && <span className="pill state">response-dependent</span>}
            <span className="qex-close">close {t.composite.toFixed(2)}</span>
            <span className="muted">
              sem {t.semantic.toFixed(2)}
              {t.judge != null ? ` · judge ${t.judge.toFixed(2)}` : " · judge —"}
            </span>
          </div>
          <div className="qex-answer">
            <span className="jex-tag">answer</span>
            <p>{t.answer_excerpt || "—"}</p>
          </div>
          {t.reference_excerpt && (
            <div className="qex-ref">
              <span className="jex-tag">target</span>
              <blockquote>{t.reference_excerpt}</blockquote>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function ModelCard({
  model,
  expanded,
  onToggle,
}: {
  readonly model: QualityModelSummary;
  readonly expanded: boolean;
  readonly onToggle: () => void;
}): JSX.Element {
  const gt = Object.entries(model.ground_truth_counts);
  return (
    <div className={`jcard ${expanded ? "open" : ""}`}>
      <button className="jcard-head" onClick={onToggle} aria-expanded={expanded}>
        <span className="jcard-name">{model.model}</span>
        <span className="jcard-overall">{model.overall_mean.toFixed(2)}</span>
        <span className="jcard-n">{model.n_outcomes} runs</span>
        <span className="jcard-ans">
          <span className="pill full" title="turn-1 vs gold ideal">
            t1 {model.turn1_mean.toFixed(2)}
          </span>
          <span className="pill partial" title="later turns vs wants">
            later {model.later_mean.toFixed(2)}
          </span>
          <span className="pill state" title="fraction of scoreable turns with a judge verdict">
            judged {pct(model.judged_fraction)}
          </span>
        </span>
        <span className="jcard-caret">{expanded ? "▾" : "▸"}</span>
      </button>

      <div className="qcard-gt">
        {gt.map(([k, n]) => (
          <span key={k} className={`pill ${kindPill(k)}`}>
            {k} {n}
          </span>
        ))}
      </div>

      {expanded && (
        <div className="jcard-examples">
          <div className="jcard-examples-head">
            Example conversations — best · median · worst (per-turn answer vs target)
          </div>
          {model.examples.map((ex) => (
            <ExampleConversation key={ex.trial_id} ex={ex} />
          ))}
          {model.examples.length === 0 && (
            <div className="muted">No example conversations captured yet.</div>
          )}
        </div>
      )}
    </div>
  );
}

export function Quality(): JSX.Element {
  const [summary, setSummary] = useState<QualitySummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [section, setSection] = useState<QualitySection>("optimizer");

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const s = await fetchQualitySummary(signal);
      setSummary(s);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  // Poll every 3s so a running quality/judge pass fills the view live. Only the
  // closeness section needs this poll; the optimizer section streams over SSE.
  useEffect(() => {
    if (section !== "closeness") return;
    const ctrl = new AbortController();
    void load(ctrl.signal);
    const id = window.setInterval(() => void load(), 3000);
    return () => {
      ctrl.abort();
      window.clearInterval(id);
    };
  }, [load, section]);

  const models = useMemo(() => summary?.models ?? [], [summary]);
  const hasData = models.length > 0 && (summary?.n_outcomes ?? 0) > 0;

  return (
    <div className="view">
      <div className="shead">
        <h2>Multi-Turn Quality</h2>
        <span className="sub">
          A separate study · closed-loop prompt optimizer plus the per-turn closeness drift
        </span>
        <span className="rule" />
      </div>

      <nav className="subtabs" role="tablist" aria-label="Quality section">
        <button
          role="tab"
          aria-selected={section === "optimizer"}
          className={`subtab ${section === "optimizer" ? "on" : ""}`}
          onClick={() => setSection("optimizer")}
        >
          Prompt Optimizer
        </button>
        <button
          role="tab"
          aria-selected={section === "closeness"}
          className={`subtab ${section === "closeness" ? "on" : ""}`}
          onClick={() => setSection("closeness")}
        >
          Closeness Drift
        </button>
      </nav>

      {section === "optimizer" ? (
        <QualityOptimizer />
      ) : (
        <ClosenessStudy
          hasData={hasData}
          models={models}
          error={error}
          expanded={expanded}
          onToggle={(m) => setExpanded(expanded === m ? null : m)}
        />
      )}

      <div className="foot">
        GBBO · multi-turn quality study · separate stores from the bake-off · two models under test
        (Sonnet 4.6 thinking-off, Haiku 4.5). The optimizer streams its champion/challenger loop
        live; closeness blends a semantic cross-check with the deferred per-turn judge.
      </div>
    </div>
  );
}

/** The original per-turn closeness drift study (unchanged), now a Quality sub-section. */
function ClosenessStudy({
  hasData,
  models,
  error,
  expanded,
  onToggle,
}: {
  readonly hasData: boolean;
  readonly models: readonly QualityModelSummary[];
  readonly error: string | null;
  readonly expanded: string | null;
  readonly onToggle: (model: string) => void;
}): JSX.Element {
  return (
    <>
      {error && <div className="startrun-err">{error}</div>}

      {hasData ? (
        <>
          <div className="panel">
            <div className="panel-title">Closeness by turn position (the drift curve)</div>
            <DriftChart models={models} />
            <div className="jctl-hint">
              Each turn is generated conversationally — the model&rsquo;s own earlier answers are fed
              forward, so errors compound exactly as they would in production. Turn-1 closeness is
              measured against the gold ideal (or abstention-correctness when unanswerable); later
              turns are measured against each turn&rsquo;s <code>wants</code>. The two are reported
              separately because they use different ground truth.
            </div>
          </div>

          <div className="jcards" style={{ marginTop: 16 }}>
            <div className="jcards-legend">
              <span>
                <i className="swatch mean" /> turn-1 vs gold ideal
              </span>
              <span>
                <i className="swatch pass" /> later turns vs wants
              </span>
              <span className="muted">click a model for example conversations</span>
            </div>
            {models.map((m) => (
              <ModelCard
                key={m.model}
                model={m}
                expanded={expanded === m.model}
                onToggle={() => onToggle(m.model)}
              />
            ))}
          </div>
        </>
      ) : (
        <div className="empty" style={{ marginTop: 16 }}>
          No quality outcomes yet. Run the multi-turn quality study
          (<code>python -m bakeoff.quality.main all --backend live</code>) once the bake-off run has
          finished; this tab fills in as outcomes and per-turn judge verdicts land.
        </div>
      )}
    </>
  );
}
