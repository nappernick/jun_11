/**
 * Eval2D — the real-time 2D eval view (design C6; Req 11.*, 18.2/18.3, 20.1).
 *
 * Renders the four 2D archetypes — speed × quality, metric-over-instances, the
 * corpus-size sweep curve, and retrieval-vs-ragas — all sharing ONE Control_Panel
 * selection, so any selection change updates whichever chart is showing with no
 * full-page reload (Req 11.5). It is driven by the SAME `useEvalStream` state the
 * 3D view uses (lifted into `App.tsx`).
 *
 * Two display disciplines from the requirements:
 *   - retrieval-quality and generation-quality (ragas) are kept DISTINCT in the
 *     retrieval-vs-ragas chart (the builder never sums across the two families),
 *     and the ragas composite is labeled distinctly from the Authoritative_Judge
 *     verdict (Req 18.2/18.3);
 *   - every metric display carries the external-methodology label (Req 20.1).
 */
import { useMemo, useState } from "react";
import type { JSX } from "react";
import type { EChartsOption } from "echarts";
import { EChart } from "../components/EChart";
import { ControlPanel } from "../eval/ControlPanel";
import {
  deriveChartView,
  defaultSelection,
  type EvalSelection,
} from "../eval/evalSelectors";
import {
  buildSpeedQuality2DOption,
  buildMetricOverInstances2DOption,
  buildCorpusCurve2DOption,
  buildRetrievalVsRagas2DOption,
  buildRetrievalCorrelation2DOption,
} from "../eval/charts2d";
import { methodologyLabel, EXTERNAL_METHODOLOGY_CAVEAT } from "../eval/methodology";
import type { EvalStreamState } from "../api/useEvalStream";
import type { EvalInstance } from "../api/types";

type View2D =
  | "speed-quality"
  | "metric-instances"
  | "corpus-curve"
  | "retrieval-ragas"
  | "retrieval-correlation";

const VIEWS: ReadonlyArray<{ readonly id: View2D; readonly label: string }> = [
  { id: "speed-quality", label: "Speed × Quality" },
  { id: "metric-instances", label: "Metric over instances" },
  { id: "corpus-curve", label: "Corpus-size sweep" },
  { id: "retrieval-ragas", label: "Retrieval vs ragas" },
  { id: "retrieval-correlation", label: "Retrieval correlation" },
];

export interface Eval2DProps {
  readonly stream: EvalStreamState;
}

export function Eval2D({ stream }: Eval2DProps): JSX.Element {
  const [selection, setSelection] = useState<EvalSelection>(() => defaultSelection());
  const [active, setActive] = useState<View2D>("speed-quality");
  const [metric, setMetric] = useState<string>("composite");
  const [retrievalMetric, setRetrievalMetric] = useState<string>("recall_at_k");
  const [qualityMetric, setQualityMetric] = useState<string>("judge_faithfulness");

  const instances = useMemo<readonly EvalInstance[]>(
    () => [...stream.instances.values()],
    [stream.instances],
  );

  const view = useMemo(() => deriveChartView(instances, selection), [instances, selection]);

  // Metric choices for the metric-over-instances chart: the composite + any ragas metric present.
  const metricChoices = useMemo(() => {
    const names = new Set<string>();
    for (const inst of instances) for (const n of Object.keys(inst.ragas)) names.add(n);
    return ["composite", ...[...names].sort()];
  }, [instances]);
  const retrievalChoices = useMemo(() => {
    const names = new Set<string>();
    for (const inst of instances) for (const metricName of Object.keys(inst.retrieval)) names.add(metricName);
    return [...names].sort();
  }, [instances]);
  const qualityChoices = useMemo(() => {
    const names = new Set<string>();
    for (const inst of instances) for (const metricName of Object.keys(inst.ragas)) names.add(metricName);
    return ["composite", ...[...names].sort()];
  }, [instances]);

  const option = useMemo<EChartsOption>(() => {
    switch (active) {
      case "metric-instances":
        return buildMetricOverInstances2DOption(view, metric);
      case "corpus-curve":
        return buildCorpusCurve2DOption(view);
      case "retrieval-ragas":
        return buildRetrievalVsRagas2DOption(view);
      case "retrieval-correlation":
        return buildRetrievalCorrelation2DOption(view, retrievalMetric, qualityMetric);
      case "speed-quality":
      default:
        return buildSpeedQuality2DOption(view);
    }
  }, [active, view, metric, retrievalMetric, qualityMetric]);

  const scoreSummary = useMemo(() => {
    let qualitySum = 0;
    let qualityCount = 0;
    let latencySum = 0;
    let latencyCount = 0;
    let slowLowCount = 0;
    for (const inst of view.instances) {
      const quality = view.qualityByInstanceId.get(inst.instance_id) ?? null;
      if (quality != null) {
        qualitySum += quality;
        qualityCount += 1;
        if (inst.latency_ms >= 4000 && quality < 0.5) slowLowCount += 1;
      }
      if (Number.isFinite(inst.latency_ms)) {
        latencySum += inst.latency_ms;
        latencyCount += 1;
      }
    }
    return {
      meanQuality: qualityCount > 0 ? qualitySum / qualityCount : null,
      meanLatency: latencyCount > 0 ? latencySum / latencyCount : null,
      slowLowCount,
      plottedCount: view.instances.length,
    };
  }, [view]);

  const regressionRows = useMemo(() => {
    return view.instances
      .map((inst) => {
        const quality = view.qualityByInstanceId.get(inst.instance_id) ?? null;
        const recall = inst.retrieval.recall_at_k?.value ?? null;
        const ndcg = inst.retrieval.ndcg_at_k?.value ?? null;
        const faithfulness = inst.ragas.judge_faithfulness?.value ?? null;
        return { inst, quality, recall, ndcg, faithfulness };
      })
      .filter(
        (row) =>
          row.quality != null &&
          ((row.recall != null && row.recall >= 0.75 && row.quality < 0.65) ||
            (row.ndcg != null && row.ndcg >= 0.75 && row.faithfulness != null && row.faithfulness < 0.65)),
      )
      .sort((leftRow, rightRow) => {
        const leftRetrieval = Math.max(leftRow.recall ?? 0, leftRow.ndcg ?? 0);
        const rightRetrieval = Math.max(rightRow.recall ?? 0, rightRow.ndcg ?? 0);
        const leftQuality = leftRow.quality ?? 1;
        const rightQuality = rightRow.quality ?? 1;
        return rightRetrieval - rightQuality - (leftRetrieval - leftQuality);
      })
      .slice(0, 8);
  }, [view]);

  const hasData = view.instances.length > 0;
  const ariaLabel = VIEWS.find((v) => v.id === active)?.label ?? "2D eval chart";

  return (
    <div className="view">
      <div className="shead">
        <h2>Eval · 2D</h2>
        <span className="sub">
          speed × quality · metric trends · corpus sweep · retrieval vs ragas ·{" "}
          {EXTERNAL_METHODOLOGY_CAVEAT}
        </span>
        <span className="rule" />
        <span className={`pill state ${stream.status}`}>{stream.status}</span>
      </div>

      <div className="eval-toolbar">
        <nav className="subtabs" role="tablist" aria-label="2D view">
          {VIEWS.map((v) => (
            <button
              key={v.id}
              role="tab"
              aria-selected={active === v.id}
              className={`subtab ${active === v.id ? "on" : ""}`}
              onClick={() => setActive(v.id)}
            >
              {v.label}
            </button>
          ))}
        </nav>
        {active === "metric-instances" && (
          <label className="cp-field">
            <span>metric</span>
            <select value={metric} onChange={(e) => setMetric(e.target.value)}>
              {metricChoices.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </label>
        )}
        {active === "retrieval-correlation" && (
          <>
            <label className="cp-field">
              <span>retrieval metric</span>
              <select value={retrievalMetric} onChange={(e) => setRetrievalMetric(e.target.value)}>
                {(retrievalChoices.length > 0 ? retrievalChoices : ["recall_at_k"]).map((choice) => (
                  <option key={choice} value={choice}>
                    {choice}
                  </option>
                ))}
              </select>
            </label>
            <label className="cp-field">
              <span>quality metric</span>
              <select value={qualityMetric} onChange={(e) => setQualityMetric(e.target.value)}>
                {qualityChoices.map((choice) => (
                  <option key={choice} value={choice}>
                    {choice}
                  </option>
                ))}
              </select>
            </label>
          </>
        )}
      </div>

      {hasData ? (
        <>
          <div className="eval-insight-strip">
            <div className="panel eval-insight">
              <span className="v2-summary-label">Plotted</span>
              <b>{scoreSummary.plottedCount}</b>
              <span className="muted">records in this lens</span>
            </div>
            <div className="panel eval-insight">
              <span className="v2-summary-label">Mean quality</span>
              <b>{scoreSummary.meanQuality == null ? "—" : scoreSummary.meanQuality.toFixed(3)}</b>
              <span className="muted">judge-triad composite</span>
            </div>
            <div className="panel eval-insight">
              <span className="v2-summary-label">Mean latency</span>
              <b>
                {scoreSummary.meanLatency == null
                  ? "—"
                  : `${Math.round(scoreSummary.meanLatency)}ms`}
              </b>
              <span className="muted">selected instances</span>
            </div>
            <div className="panel eval-insight">
              <span className="v2-summary-label">Watch zone</span>
              <b>{scoreSummary.slowLowCount}</b>
              <span className="muted">slow and low quality</span>
            </div>
          </div>

          <div className="panel">
            <EChart option={option} height={420} ariaLabel={ariaLabel} />
            <div className="cp-hint muted">{methodologyLabel(ariaLabel)}</div>
          </div>

          <details className="panel eval-control-drawer">
            <summary>
              <span>Filters, axes, weights</span>
              <span className="muted">
                {view.accounting.filteredOut.length} filtered ·{" "}
                {view.accounting.nonPlottable.length} non-plottable
              </span>
            </summary>
            <ControlPanel selection={selection} onChange={setSelection} instances={instances} />
          </details>

          <div className="panel eval-regressions">
            <div className="panel-title">Retrieval succeeded, generation struggled</div>
            {regressionRows.length === 0 ? (
              <div className="muted">No retrieval-quality regressions in the current lens.</div>
            ) : (
              <table className="dt">
                <thead>
                  <tr>
                    <th>instance</th>
                    <th>agent</th>
                    <th>recall</th>
                    <th>nDCG</th>
                    <th>faith</th>
                    <th>quality</th>
                    <th>latency</th>
                  </tr>
                </thead>
                <tbody>
                  {regressionRows.map((row) => (
                    <tr key={row.inst.instance_id}>
                      <td>{row.inst.instance_id}</td>
                      <td>{row.inst.agent_id}</td>
                      <td>{row.recall == null ? "—" : row.recall.toFixed(3)}</td>
                      <td>{row.ndcg == null ? "—" : row.ndcg.toFixed(3)}</td>
                      <td>{row.faithfulness == null ? "—" : row.faithfulness.toFixed(3)}</td>
                      <td>{row.quality == null ? "—" : row.quality.toFixed(3)}</td>
                      <td>{Math.round(row.inst.latency_ms)}ms</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {/* ragas vs judge — DISTINCT labeled signals (Req 18.2/18.3) */}
          <div className="panel eval-aux-card" style={{ marginTop: 16 }}>
            <div className="panel-title" title={methodologyLabel()}>
              Signals (distinct &amp; never conflated)
            </div>
            <div className="eval-signals">
              <div className="eval-signal">
                <span className="eval-signal-tag ragas">ragas / retrieval</span>
                <span className="muted">
                  generation-quality (ragas) and retrieval-quality are rendered as distinct,
                  separately labeled series — never summed. {EXTERNAL_METHODOLOGY_CAVEAT}.
                </span>
              </div>
              <div className="eval-signal">
                <span className="eval-signal-tag judge">Authoritative_Judge</span>
                <span className="muted">
                  a separate authoritative verdict, never folded into the ragas composite.
                </span>
              </div>
            </div>
          </div>
        </>
      ) : (
        <>
          <div className="empty" style={{ marginTop: 16 }}>
            No eval instances yet. Start an eval run and these charts fill in live as instances land.
          </div>
          <details className="panel eval-control-drawer">
            <summary>
              <span>Filters, axes, weights</span>
              <span className="muted">Configure the lens before data arrives</span>
            </summary>
            <ControlPanel selection={selection} onChange={setSelection} instances={instances} />
          </details>
        </>
      )}

      <div className="foot">
        GBBO · ragas eval · 2D · {methodologyLabel()}. Each rendered point maps to exactly one
        recorded instance.
      </div>
    </div>
  );
}
