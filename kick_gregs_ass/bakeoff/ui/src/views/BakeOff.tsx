/**
 * Bake-Off — full-stack decision cockpit.
 *
 * This tab now separates two jobs that were previously blurred:
 *   1. operating the live run, via the snapshot/SSE fleet surfaces; and
 *   2. deciding what the run says, via /api/bakeoff/diagnostics over the clean
 *      outcomes log.
 *
 * The diagnostics payload exposes the evidence the runner already records:
 * paired model deltas, normal-approx quality CIs, timing-stage distributions,
 * token usage, answerability/cohort slices, high-variance items, and
 * retrieval-good/quality-bad examples.
 */
import { useEffect, useMemo, useState } from "react";
import type { JSX } from "react";
import type { EChartsOption } from "echarts";
import type {
  Aggregate,
  BakeOffDiagnostics,
  BakeOffModelCard,
  BakeOffQualityLatencyPoint,
  BakeOffSessionsResponse,
  BakeOffTimingStage,
  ControlAction,
  RunSnapshot,
  TrialCompleted,
} from "../api/types";
import { fetchBakeOffDiagnostics } from "../api/client";
import { computeModelLiveStats } from "../lib/liveStats";
import { ms, score, count, modelColor } from "../lib/format";
import { BakeOffSessionManager } from "../components/BakeOffSessionManager";
import { StartRunControl } from "../components/StartRunControl";
import { FleetLane } from "../components/FleetLane";
import { LatencyChart } from "../components/LatencyChart";
import { LatencyScatter } from "../components/LatencyScatter";
import { RecentFeed } from "../components/RecentFeed";
import { EChart } from "../components/EChart";

export interface BakeOffProps {
  readonly snapshot: RunSnapshot;
  readonly events: readonly TrialCompleted[];
  readonly snapshotError: string | null;
  readonly onControl: (action: ControlAction) => void;
  /** Force an immediate snapshot re-fetch (e.g. right after a successful start). */
  readonly refreshNow: () => void;
  readonly sessions: BakeOffSessionsResponse | null;
  readonly sessionError: string | null;
  readonly onRefreshSessions: () => Promise<void> | void;
}

function qualityPoint(card: BakeOffModelCard): number | null {
  return card.quality?.mean_ci?.point ?? null;
}

function qualityLabel(card: BakeOffModelCard): string {
  const quality = card.quality?.mean_ci;
  if (!quality) return "insufficient";
  return `${score(quality.point)} [${score(quality.low)}, ${score(quality.high)}]`;
}

function timingValue(card: BakeOffModelCard, field: string, statistic: keyof BakeOffTimingStage = "end_to_end_ms"): number | null {
  void statistic;
  return card.timing[field]?.p50 ?? null;
}

function fmtNumber(value: number | null | undefined, digits = 3): string {
  return value == null || !Number.isFinite(value) ? "-" : value.toFixed(digits);
}

function countMapText(values: Readonly<Record<string, number>>): string {
  const entries = Object.entries(values);
  if (entries.length === 0) return "-";
  return entries.map(([label, value]) => `${label} ${value}`).join(" · ");
}

function qualitySourceLabel(diagnostics: BakeOffDiagnostics | null): string {
  if (!diagnostics) return "waiting for diagnostics";
  if (diagnostics.source.quality_source === "phase2_judge_scores") {
    return `Phase-2 judge composite · ${count(diagnostics.source.judge_scores_joined)} judged`;
  }
  return "outcome composite fallback";
}

function frontierOption(
  cards: readonly BakeOffModelCard[],
  points: readonly BakeOffQualityLatencyPoint[],
): EChartsOption {
  // The CLOUD: one scatter point per judged trial (latency, quality), grouped by model.
  // This is what makes the surface a real distribution instead of two lonely dots.
  const byModel = new Map<string, [number, number][]>();
  for (const point of points) {
    if (!Number.isFinite(point.latency_ms) || !Number.isFinite(point.composite)) continue;
    const bucket = byModel.get(point.model) ?? [];
    bucket.push([point.latency_ms, point.composite]);
    byModel.set(point.model, bucket);
  }
  const cloudSeries = [...byModel.entries()].map(([model, data]) => ({
    type: "scatter" as const,
    name: model,
    data,
    symbolSize: 5,
    large: true,
    largeThreshold: 400,
    itemStyle: { color: modelColor(model), opacity: 0.3 },
    emphasis: { focus: "series" as const, itemStyle: { opacity: 0.9 } },
  }));

  // The CENTROIDS: each model's official aggregate (quality CI point × e2e p50), drawn
  // large on top so the headline comparison is unmistakable over the cloud.
  const centroidData = cards
    .map((card) => {
      const quality = qualityPoint(card);
      const latency = timingValue(card, "end_to_end_ms");
      if (quality == null || latency == null) return null;
      return {
        value: [latency, quality],
        name: card.model,
        itemStyle: { color: modelColor(card.model), borderColor: "#0b0f14", borderWidth: 2 },
        label: { show: true, position: "right" as const, formatter: card.model, color: "#e8edf5", fontSize: 11, fontWeight: 600 },
      };
    })
    .filter((point): point is NonNullable<typeof point> => point !== null);

  return {
    backgroundColor: "transparent",
    grid: { left: 58, right: 130, top: 28, bottom: 46 },
    legend: { top: 2, type: "scroll", textStyle: { color: "#aebfd4", fontSize: 10 } },
    tooltip: {
      trigger: "item",
      confine: true,
      formatter: (params: unknown) => {
        const point = params as { seriesName?: string; data?: { name?: string; value?: [number, number] }; value?: [number, number] };
        const value = Array.isArray(point.value) ? point.value : point.data?.value;
        if (!value) return "";
        const isMean = point.seriesName === "model mean";
        const name = isMean ? point.data?.name ?? "" : point.seriesName ?? "";
        return `<b>${name}</b>${isMean ? " · model mean" : " · one trial"}<br/>quality ${score(value[1])}<br/>e2e ${ms(value[0])}`;
      },
    },
    xAxis: {
      type: "value",
      name: "end-to-end latency (ms) - lower is better",
      nameLocation: "middle",
      nameGap: 30,
      axisLabel: { color: "#7488a3", fontFamily: "var(--mono)", fontSize: 11 },
      nameTextStyle: { color: "#7488a3" },
      splitLine: { lineStyle: { color: "rgba(140,165,200,0.10)" } },
    },
    yAxis: {
      type: "value",
      name: "judged quality - higher is better",
      nameLocation: "middle",
      nameGap: 40,
      min: 0,
      max: 1,
      axisLabel: { color: "#7488a3", fontFamily: "var(--mono)", fontSize: 11 },
      nameTextStyle: { color: "#7488a3" },
      splitLine: { lineStyle: { color: "rgba(140,165,200,0.10)" } },
    },
    series: [
      ...cloudSeries,
      {
        type: "scatter",
        name: "model mean",
        data: centroidData,
        symbol: "diamond",
        symbolSize: 22,
        z: 10,
      },
    ],
  } as EChartsOption;
}

function timingStagesOption(stages: readonly BakeOffTimingStage[]): EChartsOption {
  const models = stages.map((stage) => stage.model);
  const series = [
    { key: "retrieval_total_ms", label: "retrieval", color: "#6aa9ff" },
    { key: "ttft_ms", label: "TTFT", color: "#f7a14b" },
    { key: "generation_total_ms", label: "generation", color: "#58c08a" },
  ].map((definition) => ({
    type: "bar" as const,
    name: definition.label,
    itemStyle: { color: definition.color },
    data: stages.map((stage) => Number(stage[definition.key as keyof BakeOffTimingStage] ?? 0)),
  }));
  return {
    backgroundColor: "transparent",
    grid: { left: 52, right: 18, top: 32, bottom: 64 },
    legend: { top: 0, textStyle: { color: "#aebfd4", fontSize: 11 } },
    tooltip: {
      trigger: "axis",
      valueFormatter: (value: unknown) => ms(Number(value)),
    },
    xAxis: {
      type: "category",
      data: models,
      axisLabel: { color: "#7488a3", fontSize: 10, interval: 0, rotate: 15 },
    },
    yAxis: {
      type: "value",
      name: "mean ms",
      nameTextStyle: { color: "#7488a3" },
      axisLabel: { color: "#7488a3", fontFamily: "var(--mono)", fontSize: 11 },
      splitLine: { lineStyle: { color: "rgba(140,165,200,0.10)" } },
    },
    series,
  } as EChartsOption;
}

function lowestCohortCells(diagnostics: BakeOffDiagnostics | null): Aggregate[] {
  if (!diagnostics) return [];
  return Object.entries(diagnostics.cohort_slices)
    .flatMap(([dimension, cells]) =>
      cells.map((cell) => ({
        ...cell,
        group: { ...cell.group, dimension },
      })),
    )
    .filter((cell) => cell.mean_ci !== null)
    .sort((leftCell, rightCell) => (leftCell.mean_ci?.point ?? 1) - (rightCell.mean_ci?.point ?? 1))
    .slice(0, 12);
}

export function BakeOff({
  snapshot,
  events,
  snapshotError,
  onControl,
  refreshNow,
  sessions,
  sessionError,
  onRefreshSessions,
}: BakeOffProps): JSX.Element {
  const [diagnostics, setDiagnostics] = useState<BakeOffDiagnostics | null>(null);
  const [diagnosticsError, setDiagnosticsError] = useState<string | null>(null);
  // The Bake-Off sessions block is large and rarely touched mid-run, so it
  // collapses (default closed) to keep the run controls front-and-center.
  const [sessionsOpen, setSessionsOpen] = useState(false);

  const models = useMemo(
    () => Object.keys(snapshot.models).sort((leftModel, rightModel) => leftModel.localeCompare(rightModel)),
    [snapshot.models],
  );
  const liveStats = useMemo(() => computeModelLiveStats(events, models), [events, models]);

  useEffect(() => {
    const controller = new AbortController();
    const load = async (): Promise<void> => {
      try {
        const nextDiagnostics = await fetchBakeOffDiagnostics(controller.signal);
        setDiagnostics(nextDiagnostics);
        setDiagnosticsError(null);
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setDiagnosticsError(error instanceof Error ? error.message : String(error));
      }
    };
    void load();
    const intervalId = window.setInterval(() => void load(), 4000);
    return () => {
      controller.abort();
      window.clearInterval(intervalId);
    };
  }, []);

  const diagnosticModels = diagnostics?.model_cards ?? [];
  const leader = [...diagnosticModels].sort(
    (leftCard, rightCard) => (qualityPoint(rightCard) ?? -1) - (qualityPoint(leftCard) ?? -1),
  )[0] ?? null;
  const fastest = [...diagnosticModels].sort(
    (leftCard, rightCard) =>
      (timingValue(leftCard, "end_to_end_ms") ?? Number.POSITIVE_INFINITY) -
      (timingValue(rightCard, "end_to_end_ms") ?? Number.POSITIVE_INFINITY),
  )[0] ?? null;
  const frontier = useMemo(
    () => frontierOption(diagnosticModels, diagnostics?.quality_latency ?? []),
    [diagnosticModels, diagnostics?.quality_latency],
  );
  const timingChart = useMemo(
    () => timingStagesOption(diagnostics?.timing_stages ?? []),
    [diagnostics],
  );
  const cohortRows = useMemo(() => lowestCohortCells(diagnostics), [diagnostics]);
  const hasFleet = models.length > 0;

  return (
    <div className="view bakeoff-view">
      {snapshotError && (
        <div className="banner">
          Snapshot poll error: {snapshotError}. The backend may not be running.
        </div>
      )}
      {diagnosticsError && (
        <div className="banner">
          Bake-Off diagnostics error: {diagnosticsError}
        </div>
      )}

      <div className="panel bakeoff-sessions-collapsible">
        <button
          type="button"
          className="bakeoff-sessions-toggle"
          aria-expanded={sessionsOpen}
          onClick={() => setSessionsOpen((open) => !open)}
        >
          <span className="bakeoff-sessions-caret">{sessionsOpen ? "▾" : "▸"}</span>
          <span className="bakeoff-sessions-title">Bake-Off sessions</span>
          <span className="muted">
            {sessionsOpen ? "click to collapse" : "click to expand · active session & legacy data"}
          </span>
        </button>
        {sessionsOpen && (
          <div className="bakeoff-sessions-body">
            <BakeOffSessionManager
              snapshot={snapshot}
              sessions={sessions}
              sessionError={sessionError}
              onRefreshSessions={onRefreshSessions}
            />
          </div>
        )}
      </div>

      <StartRunControl snapshot={snapshot} onStarted={() => refreshNow()} onControl={onControl} />

      <div className="bakeoff-hero">
        <section className="panel bakeoff-decision">
          <div className="panel-head">
            <div>
              <h3>Decision surface</h3>
              <div className="ph-sub">
                every judged trial (quality × latency) · ◆ = model mean ·{" "}
                {qualitySourceLabel(diagnostics)}
              </div>
            </div>
          </div>
          {diagnosticModels.length > 0 ? (
            <EChart option={frontier} height={360} ariaLabel="Bake-Off decision frontier" />
          ) : (
            <div className="empty">No completed outcome records yet.</div>
          )}
        </section>

        <aside className="bakeoff-summary">
          <div className="panel bakeoff-summary-card">
            <span className="v2-summary-label">quality leader</span>
            <b>{leader?.model ?? "-"}</b>
            <span className="muted">{leader ? qualityLabel(leader) : "waiting for diagnostics"}</span>
          </div>
          <div className="panel bakeoff-summary-card">
            <span className="v2-summary-label">fastest p50</span>
            <b>{fastest?.model ?? "-"}</b>
            <span className="muted">{fastest ? ms(timingValue(fastest, "end_to_end_ms")) : "-"}</span>
          </div>
          <div className="panel bakeoff-summary-card">
            <span className="v2-summary-label">evidence</span>
            <b>{count(diagnostics?.source.quality_trials ?? 0)}</b>
            <span className="muted">
              {count(diagnostics?.source.quality_items ?? 0)} quality items · {count(diagnostics?.source.total_trials ?? 0)} outcomes
            </span>
          </div>
        </aside>
      </div>

      <div className="panel" style={{ marginTop: 18 }}>
        <div className="panel-head">
          <div>
            <h3>Model decision table</h3>
            <div className="ph-sub">CI-backed quality, latency stages, tokens, answerability mix</div>
          </div>
        </div>
        {diagnosticModels.length === 0 ? (
          <div className="empty">No model diagnostics yet.</div>
        ) : (
          <table className="dt bakeoff-decision-table">
            <thead>
              <tr>
                <th>model</th>
                <th>quality CI</th>
                <th>quality n</th>
                <th>e2e p50</th>
                <th>TTFT p50</th>
                <th>retrieval p50</th>
                <th>tokens</th>
                <th>answerability</th>
              </tr>
            </thead>
            <tbody>
              {diagnosticModels.map((card) => (
                <tr key={card.model}>
                  <td>
                    <span className="mtag">
                      <span className="dot" style={{ background: modelColor(card.model) }} />
                      {card.model}
                    </span>
                  </td>
                  <td>{qualityLabel(card)}</td>
                  <td>{count(card.n_quality_trials)}</td>
                  <td>{ms(timingValue(card, "end_to_end_ms"))}</td>
                  <td>{ms(timingValue(card, "ttft_ms"))}</td>
                  <td>{ms(timingValue(card, "retrieval_total_ms"))}</td>
                  <td>{fmtNumber(card.token_usage_mean.total, 0)}</td>
                  <td>{countMapText(card.answerability_counts)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="grid cols-2" style={{ marginTop: 18 }}>
        <div className="panel">
          <div className="panel-head">
            <div>
              <h3>Timing stages</h3>
              <div className="ph-sub">mean retrieval, responsiveness, and generation time</div>
            </div>
          </div>
          <EChart option={timingChart} height={320} ariaLabel="Bake-Off timing stage comparison" />
        </div>
        <div className="panel">
          <div className="panel-head">
            <div>
              <h3>Paired deltas</h3>
              <div className="ph-sub">shared judged-item composite deltas; positive favors model A</div>
            </div>
          </div>
          {(diagnostics?.paired_deltas.length ?? 0) === 0 ? (
            <div className="empty">Need at least two models with shared items.</div>
          ) : (
            <table className="dt">
              <thead>
                <tr>
                  <th>model A</th>
                  <th>model B</th>
                  <th>delta CI</th>
                  <th>items</th>
                  <th>winner</th>
                </tr>
              </thead>
              <tbody>
                {diagnostics?.paired_deltas.map((delta) => (
                  <tr key={`${delta.model_a}-${delta.model_b}`}>
                    <td>{delta.model_a}</td>
                    <td>{delta.model_b}</td>
                    <td>
                      {score(delta.delta_ci.point)} [{score(delta.delta_ci.low)}, {score(delta.delta_ci.high)}]
                    </td>
                    <td>{delta.shared_items}</td>
                    <td>{delta.winner ?? "tie"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <div className="panel" style={{ marginTop: 18 }}>
        <div className="panel-head">
          <div>
            <h3>Live fleet</h3>
            <div className="ph-sub">run operation state from snapshot and SSE</div>
          </div>
        </div>
        {hasFleet ? (
          <div className="fleet">
            {models.map((model) => {
              const countsForModel = snapshot.models[model];
              if (!countsForModel) return null;
              return (
                <FleetLane
                  key={model}
                  model={model}
                  counts={countsForModel}
                  live={liveStats.get(model) ?? null}
                />
              );
            })}
          </div>
        ) : (
          <div className="empty">No models registered yet. Hit Start Run above to fill the fleet.</div>
        )}
      </div>

      <div className="grid cols-2" style={{ marginTop: 18 }}>
        <div className="panel">
          <div className="panel-head">
            <div>
              <h3>TTFT distribution</h3>
              <div className="ph-sub">responsiveness by model from streamed trials</div>
            </div>
          </div>
          <LatencyChart events={events} focusModel={null} />
        </div>
        <div className="panel">
          <div className="panel-head">
            <div>
              <h3>Per-trial latency stream</h3>
              <div className="ph-sub">arrival order x end-to-end ms, colored by model</div>
            </div>
          </div>
          <LatencyScatter events={events} />
        </div>
      </div>

      <div className="grid cols-2" style={{ marginTop: 18 }}>
        <div className="panel">
          <div className="panel-head">
            <div>
              <h3>Cohort risk</h3>
              <div className="ph-sub">lowest judged-composite cohort cells across answerability and user context</div>
            </div>
          </div>
          {cohortRows.length === 0 ? (
            <div className="empty">No cohort cells with CIs yet.</div>
          ) : (
            <table className="dt">
              <thead>
                <tr>
                  <th>model</th>
                  <th>dimension</th>
                  <th>slice</th>
                  <th>quality</th>
                  <th>n</th>
                </tr>
              </thead>
              <tbody>
                {cohortRows.map((cell) => {
                  const dimension = cell.group.dimension ?? "";
                  const sliceValue = cell.group[dimension] ?? "";
                  return (
                    <tr key={`${cell.group.model}-${dimension}-${sliceValue}`}>
                      <td>{cell.group.model}</td>
                      <td>{dimension}</td>
                      <td>{sliceValue}</td>
                      <td>{cell.mean_ci ? score(cell.mean_ci.point) : "-"}</td>
                      <td>{cell.n_trials}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
        <div className="panel">
          <div className="panel-head">
            <div>
              <h3>High-variance items</h3>
              <div className="ph-sub">items likely to flip with extra reps</div>
            </div>
          </div>
          {(diagnostics?.high_variance.length ?? 0) === 0 ? (
            <div className="empty">No high-variance items above threshold.</div>
          ) : (
            <table className="dt">
              <thead>
                <tr>
                  <th>item</th>
                  <th>model</th>
                  <th>metric</th>
                  <th>rep SD</th>
                  <th>count</th>
                </tr>
              </thead>
              <tbody>
                {diagnostics?.high_variance.map((row, rowIndex) => (
                  <tr key={`${String(row.item_id)}-${String(row.model)}-${rowIndex}`}>
                    <td>{String(row.item_id ?? "")}</td>
                    <td>{String(row.model ?? "")}</td>
                    <td>{String(row.metric ?? "")}</td>
                    <td>{fmtNumber(Number(row.rep_sd), 3)}</td>
                    <td>{String(row.count ?? "")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <div className="panel" style={{ marginTop: 18 }}>
        <div className="panel-head">
          <div>
              <h3>Retrieval succeeded, answer quality failed</h3>
            <div className="ph-sub">high recall or nDCG with low judged composite</div>
          </div>
        </div>
        {(diagnostics?.retrieval_regressions.length ?? 0) === 0 ? (
          <div className="empty">No retrieval-quality regressions in the current log.</div>
        ) : (
          <div className="bakeoff-regression-list">
            {diagnostics?.retrieval_regressions.map((regression) => (
              <div key={regression.trial_id} className="bakeoff-regression">
                <div>
                  <b>{regression.model}</b>
                  <span className="muted"> · {regression.item_id} · {regression.answerability}</span>
                </div>
                <div className="muted">
                  quality {score(regression.composite)} · recall {score(regression.recall_at_k)} · nDCG{" "}
                  {score(regression.ndcg_at_k)} · {ms(regression.latency_ms)}
                </div>
                <div className="bakeoff-regression-text">{regression.query}</div>
                <div className="bakeoff-regression-text muted">{regression.answer_excerpt}</div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="panel" style={{ marginTop: 18 }}>
        <div className="panel-head">
          <div>
            <h3>Recent trials</h3>
            <div className="ph-sub">live stream, newest first</div>
          </div>
        </div>
        <RecentFeed events={events} focusModel={null} />
      </div>

      <div className="foot">
        GBBO · Bake-Off cockpit · quality source: {qualitySourceLabel(diagnostics)}. Execution
        failures stay in the separate run-errors store and do not enter model-selection numbers.
      </div>
    </div>
  );
}
