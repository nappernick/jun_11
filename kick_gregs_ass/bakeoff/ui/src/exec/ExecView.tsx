/**
 * Executive visualization view (Task 14, Req 11).
 *
 * The decision surface for an Amazon exec / just-below-exec: the speed/quality
 * frontier (hero), live composite re-weighting, the cohort heatmap, the
 * answerability/safety panel, and the provenance footer — all reading the
 * materialized report from GET /exec/aggregate, with per-component aggregates
 * fetched from /api/aggregate so re-weighting recomputes client-side without a
 * backend round-trip.
 *
 * Design principles enforced: uncertainty is always on screen (every number has a
 * CI or is marked insufficient); decision-first (the frontier leads); speed and
 * quality are never collapsed by default; the squishy interaction metrics are
 * visibly softer (lighter sliders + a "softer confidence" note).
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import type { JSX } from "react";
import { ApiError, fetchAggregate, fetchExecAggregate } from "../api/client";
import type { Aggregate, ExecReport } from "../api/types";
import { FrontierChart, type FrontierDatum } from "./FrontierChart";
import { CohortHeatmap } from "./CohortHeatmap";
import { SafetyPanel } from "./SafetyPanel";
import { ProvenanceFooter } from "./ProvenanceFooter";
import { downloadSnapshot } from "./exportSnapshot";
import {
  ACCURACY_FIELD_COMPONENTS,
  DEFAULT_WEIGHTS,
  METRIC_MODE_LABELS,
  SQUISHY_COMPONENTS,
  collapseAcrossAnswerability,
  effectiveWeights,
  recomputeComposite,
  type MetricMode,
  type QualityWeights,
} from "./quality";

/** Component metrics we fetch per-model so re-weighting can recompute live. */
const COMPONENT_METRICS: readonly string[] = Object.keys(DEFAULT_WEIGHTS);

export function ExecView(): JSX.Element {
  const [report, setReport] = useState<ExecReport | null>(null);
  const [components, setComponents] = useState<Record<string, readonly Aggregate[]>>({});
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<MetricMode>("composite");
  const [weights, setWeights] = useState<QualityWeights>(DEFAULT_WEIGHTS);

  // Load the materialized report + per-component aggregates once.
  useEffect(() => {
    const ctrl = new AbortController();
    (async () => {
      try {
        const rep = await fetchExecAggregate(undefined, ctrl.signal);
        setReport(rep);
        const comp: Record<string, readonly Aggregate[]> = {};
        await Promise.all(
          COMPONENT_METRICS.map(async (metric) => {
            // Accuracy components are P4-guarded: the API refuses to blend them
            // across answerability when grouped by model alone (422). Fetch them
            // sliced by answerability and collapse client-side (never blended at
            // the API). Judge/interaction components are not accuracy-guarded, so
            // a plain by-model grouping is served directly.
            const isAccuracy = ACCURACY_FIELD_COMPONENTS.includes(metric);
            try {
              if (isAccuracy) {
                const res = await fetchAggregate(
                  { metric, groupBy: ["model", "answerability"] },
                  ctrl.signal,
                );
                comp[metric] = collapseAcrossAnswerability(res.aggregates);
              } else {
                const res = await fetchAggregate(
                  { metric, groupBy: ["model"] },
                  ctrl.signal,
                );
                comp[metric] = res.aggregates;
              }
            } catch {
              /* a missing/unservable component metric just drops out of weighting */
            }
          }),
        );
        setComponents(comp);
        setError(null);
      } catch (e) {
        if (ctrl.signal.aborted) return;
        if (e instanceof ApiError && e.status === 404) {
          setError("No materialized exec report yet. Run the bake-off to produce one.");
        } else if (e instanceof ApiError && e.status === 422) {
          setError("The exec report contains a number without a CI and was refused (Property 10).");
        } else {
          setError(e instanceof Error ? e.message : String(e));
        }
      }
    })();
    return () => ctrl.abort();
  }, []);

  const models = useMemo(
    () => (report ? report.frontier.map((f) => f.model) : []),
    [report],
  );

  // Recompute the composite client-side from the per-component aggregates under
  // the active metric mode + slider weights (Req 11.3).
  const frontierData = useMemo<FrontierDatum[]>(() => {
    if (!report) return [];
    const active = effectiveWeights(mode, weights);
    const weighted = recomputeComposite(models, components, active);
    const byModel = new Map(weighted.map((w) => [w.model, w]));
    return report.frontier.map((fp) => {
      const q = byModel.get(fp.model) ?? {
        model: fp.model,
        composite: fp.quality?.point ?? 0,
        low: fp.quality?.low ?? 0,
        high: fp.quality?.high ?? 0,
        insufficient: fp.quality == null,
      };
      return {
        model: fp.model,
        quality: q,
        speedP50: fp.speed_p50_ms,
        speedP90: fp.speed_p90_ms,
        onParetoFront: fp.on_pareto_front,
      };
    });
  }, [report, components, models, mode, weights]);

  const setWeight = useCallback((component: string, value: number) => {
    setWeights((w) => ({ ...w, [component]: value }));
  }, []);

  if (error) {
    return (
      <div className="view">
        <div className="banner">{error}</div>
      </div>
    );
  }
  if (!report) {
    return (
      <div className="view">
        <div className="muted" style={{ padding: 24 }}>
          Loading exec report…
        </div>
      </div>
    );
  }

  const heatmapDims = Object.keys(report.cohort_heatmaps);

  return (
    <div className="view">
      <div className="shead">
        <h2>Executive view</h2>
        <span className="sub">speed vs quality · every number carries its uncertainty</span>
        <span className="rule" />
        <button className="btn" onClick={() => downloadSnapshot(report)}>
          Export snapshot
        </button>
      </div>

      {/* Hero frontier + controls */}
      <div className="panel">
        <div className="panel-head">
          <div>
            <h3>Speed / Quality frontier</h3>
            <div className="ph-sub">
              point = model · vertical band = quality CI · dashed whisker = latency p90 · faded =
              dominated
            </div>
          </div>
          <div className="metric-toggle" role="group" aria-label="Quality metric">
            {(Object.keys(METRIC_MODE_LABELS) as MetricMode[]).map((m) => (
              <button
                key={m}
                className={`chip ${mode === m ? "on" : ""}`}
                onClick={() => setMode(m)}
              >
                {METRIC_MODE_LABELS[m]}
              </button>
            ))}
          </div>
        </div>

        <FrontierChart data={frontierData} />

        {mode === "composite" && (
          <div className="weights">
            <div className="weights-head">
              Composite weights — drag to ask &ldquo;what if X mattered more?&rdquo; The frontier
              re-ranks live.
            </div>
            <div className="weights-grid">
              {COMPONENT_METRICS.map((c) => {
                const squishy = SQUISHY_COMPONENTS.includes(c);
                return (
                  <label key={c} className={`weight ${squishy ? "squishy" : ""}`}>
                    <span className="weight-name">
                      {c}
                      {squishy && (
                        <span className="soft-badge" title="Subjective rubric — softer confidence">
                          soft
                        </span>
                      )}
                    </span>
                    <input
                      type="range"
                      min={0}
                      max={0.5}
                      step={0.01}
                      value={weights[c] ?? 0}
                      onChange={(e) => setWeight(c, Number(e.target.value))}
                      aria-label={`${c} weight`}
                    />
                    <span className="weight-val">{(weights[c] ?? 0).toFixed(2)}</span>
                  </label>
                );
              })}
            </div>
          </div>
        )}
      </div>

      {/* Safety panel */}
      <div className="panel" style={{ marginTop: 16 }}>
        <div className="panel-head">
          <div>
            <h3>Answerability &amp; safety</h3>
            <div className="ph-sub">abstention on out-of-domain questions, shown separately</div>
          </div>
        </div>
        <SafetyPanel safety={report.safety} />
      </div>

      {/* Cohort heatmaps */}
      {heatmapDims.map((dim) => (
        <div className="panel" style={{ marginTop: 16 }} key={dim}>
          <div className="panel-head">
            <div>
              <h3>Quality by {dim}</h3>
              <div className="ph-sub">cell opacity encodes CI width — faded = thin data</div>
            </div>
          </div>
          <CohortHeatmap dimension={dim} cells={report.cohort_heatmaps[dim] ?? []} />
        </div>
      ))}

      <ProvenanceFooter provenance={report.provenance} />
    </div>
  );
}
