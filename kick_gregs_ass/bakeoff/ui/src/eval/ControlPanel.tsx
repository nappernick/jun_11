/**
 * Control_Panel — the single selection surface shared by the 2D and 3D eval views
 * (design C11; Req 12.1–12.7).
 *
 * It is a CONTROLLED component: it owns no selection state of its own. It renders
 * the current `EvalSelection` and emits a new one through `onChange` on every
 * edit, so the hosting view re-derives its `ChartView` and the charts update with
 * NO full-page reload (Req 12.1). The available agents / sessions / prompts /
 * categories / metric names are derived from the live instance set passed in, so
 * the panel grows with the data without code changes.
 *
 * Two disciplines are load-bearing:
 *   - Selecting >= 3 agents at once is supported (Req 12.2): agents are independent
 *     checkboxes, empty === all agents.
 *   - Adjusting a weight only changes the `weightSet` the view composes WITH; it
 *     never touches a recorded metric value. The recompute happens downstream in
 *     `deriveChartView` from the unchanged `EvalInstance` records (Req 12.7 / P8).
 */
import { useMemo } from "react";
import type { JSX } from "react";
import type { EvalInstance } from "../api/types";
import type { EvalSelection } from "./evalSelectors";
import { DEFAULT_ENABLED_METRICS } from "./evalSelectors";
import type { AxisBinding, AxisMapping, AxisVariable } from "./axisMapping";
import { methodologyLabel } from "./methodology";

export interface ControlPanelProps {
  readonly selection: EvalSelection;
  readonly onChange: (next: EvalSelection) => void;
  /** Live instances — the source for the available agents/sessions/filters/metrics. */
  readonly instances: readonly EvalInstance[];
}

/** The plottable axis variables the panel can bind, each carrying its full binding
 * (dimension OR raw-metric) so the dropdown can offer faithfulness / the retrieval
 * metrics / answerability directly — the four dimensions the owner cares about. */
const AXIS_VARIABLES: ReadonlyArray<{
  readonly value: string;
  readonly label: string;
  readonly variable: AxisVariable;
  readonly scale: "log" | "linear";
  readonly better: "higher" | "lower";
}> = [
  { value: "latency_ms", label: "latency (ms)", variable: "latency_ms", scale: "log", better: "lower" },
  { value: "judge_faithfulness", label: "faithfulness", variable: { metric: "judge_faithfulness" }, scale: "linear", better: "higher" },
  { value: "judge_correctness", label: "correctness", variable: { metric: "judge_correctness" }, scale: "linear", better: "higher" },
  { value: "judge_completeness", label: "completeness", variable: { metric: "judge_completeness" }, scale: "linear", better: "higher" },
  { value: "ndcg_at_k", label: "retrieval — nDCG@k", variable: { metric: "ndcg_at_k" }, scale: "linear", better: "higher" },
  { value: "precision_at_k", label: "retrieval — precision@k", variable: { metric: "precision_at_k" }, scale: "linear", better: "higher" },
  { value: "recall_at_k", label: "retrieval — recall@k", variable: { metric: "recall_at_k" }, scale: "linear", better: "higher" },
  { value: "answerability", label: "answerability (none→full)", variable: "answerability", scale: "linear", better: "higher" },
  { value: "quality_score", label: "quality (judge triad)", variable: "quality_score", scale: "linear", better: "higher" },
  { value: "instance_index", label: "execution order (time)", variable: "instance_index", scale: "linear", better: "higher" },
];

function sortedUnique(values: Iterable<string>): string[] {
  return [...new Set(values)].sort();
}

export function ControlPanel({ selection, onChange, instances }: ControlPanelProps): JSX.Element {
  // Derive the available option sets from the live data (grows without code changes).
  const opts = useMemo(() => {
    const agents = new Set<string>();
    const sessions = new Set<string>();
    const prompts = new Set<string>();
    const categories = new Set<string>();
    const metrics = new Set<string>(DEFAULT_ENABLED_METRICS);
    for (const inst of instances) {
      agents.add(inst.agent_id);
      sessions.add(inst.session_id);
      if (inst.prompt_id) prompts.add(inst.prompt_id);
      if (inst.category) categories.add(inst.category);
      for (const name of Object.keys(inst.ragas)) metrics.add(name);
    }
    return {
      agents: sortedUnique(agents),
      sessions: sortedUnique(sessions),
      prompts: sortedUnique(prompts),
      categories: sortedUnique(categories),
      metrics: sortedUnique(metrics),
    };
  }, [instances]);

  const update = (partial: Partial<EvalSelection>): void =>
    onChange({ ...selection, ...partial });

  // --- agents (>= 3 at once; empty === all) ---
  const agentSelected = (id: string): boolean =>
    selection.agentIds.length === 0 || selection.agentIds.includes(id);
  const toggleAgent = (id: string): void => {
    // Materialize "all" into an explicit set the first time the user narrows it.
    const base = selection.agentIds.length === 0 ? opts.agents : selection.agentIds;
    const next = base.includes(id) ? base.filter((a) => a !== id) : [...base, id];
    // If the user re-selects every agent, collapse back to the "all" sentinel.
    update({ agentIds: next.length === opts.agents.length ? [] : next });
  };

  // --- sessions ("all" sentinel or explicit set) ---
  const sessionSelected = (id: string): boolean =>
    selection.sessionIds === "all" || selection.sessionIds.includes(id);
  const toggleSession = (id: string): void => {
    const base = selection.sessionIds === "all" ? opts.sessions : selection.sessionIds;
    const next = base.includes(id) ? base.filter((s) => s !== id) : [...base, id];
    update({ sessionIds: next.length === opts.sessions.length ? "all" : next });
  };

  // --- composite metric weights (which contribute + each weight) ---
  const metricEnabled = (name: string): boolean => selection.enabledMetrics.includes(name);
  const toggleMetric = (name: string): void => {
    const next = metricEnabled(name)
      ? selection.enabledMetrics.filter((m) => m !== name)
      : [...selection.enabledMetrics, name];
    update({ enabledMetrics: next });
  };
  const setWeight = (name: string, weight: number): void => {
    const w = Number.isFinite(weight) ? Math.max(0, weight) : 0;
    update({
      weightSet: {
        id: "custom",
        weights: { ...selection.weightSet.weights, [name]: w },
      },
    });
  };
  const weightOf = (name: string): number => selection.weightSet.weights[name] ?? 0;

  // --- axis mapping ---
  const setAxisVariable = (axis: keyof AxisMapping, value: string): void => {
    const entry = AXIS_VARIABLES.find((v) => v.value === value);
    if (!entry) return;
    const next: AxisBinding = {
      variable: entry.variable, // dimension string OR { metric } — bound correctly
      scale: entry.scale,
      betterDirection: entry.better,
    };
    update({ axes: { ...selection.axes, [axis]: next } });
  };
  const setAxisScale = (axis: keyof AxisMapping, scale: "log" | "linear"): void => {
    update({
      axes: { ...selection.axes, [axis]: { ...selection.axes[axis], scale } },
    });
  };

  const axisVarString = (b: AxisBinding): string =>
    typeof b.variable === "object" ? b.variable.metric : b.variable;

  return (
    <div className="panel cp" aria-label="Eval control panel">
      <div className="cp-grid">
        {/* Agents */}
        <section className="cp-sec">
          <div className="cp-label">Agents (≥3 supported · none = all)</div>
          <div className="cp-checks">
            {opts.agents.length === 0 && <span className="muted">no agents yet</span>}
            {opts.agents.map((id) => (
              <label key={id} className="cp-check">
                <input
                  type="checkbox"
                  checked={agentSelected(id)}
                  onChange={() => toggleAgent(id)}
                />
                {id}
              </label>
            ))}
          </div>
        </section>

        {/* Sessions / time range */}
        <section className="cp-sec">
          <div className="cp-label">Sessions / time range (none = all)</div>
          <div className="cp-checks">
            {opts.sessions.length === 0 && <span className="muted">no sessions yet</span>}
            {opts.sessions.map((id) => (
              <label key={id} className="cp-check">
                <input
                  type="checkbox"
                  checked={sessionSelected(id)}
                  onChange={() => toggleSession(id)}
                />
                {id}
              </label>
            ))}
          </div>
        </section>

        {/* Prompt + category filters */}
        <section className="cp-sec">
          <div className="cp-label">Filters</div>
          <div className="cp-row">
            <label className="cp-field">
              <span>prompt</span>
              <select
                value={selection.promptFilter ?? ""}
                onChange={(e) => update({ promptFilter: e.target.value || null })}
              >
                <option value="">all</option>
                {opts.prompts.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
            </label>
            <label className="cp-field">
              <span>category</span>
              <select
                value={selection.categoryFilter ?? ""}
                onChange={(e) => update({ categoryFilter: e.target.value || null })}
              >
                <option value="">all</option>
                {opts.categories.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </label>
            <label className="cp-field">
              <span>smoothing window</span>
              <input
                type="number"
                min={1}
                step={1}
                value={selection.smoothingWindow}
                onChange={(e) =>
                  update({ smoothingWindow: Math.max(1, Math.floor(Number(e.target.value) || 1)) })
                }
              />
            </label>
          </div>
        </section>

        {/* Axis mapping */}
        <section className="cp-sec">
          <div className="cp-label">Axis mapping (X / Y / Z)</div>
          <div className="cp-row">
            {(["x", "y", "z"] as const).map((axis) => (
              <div key={axis} className="cp-axis">
                <span className="cp-axis-name">{axis.toUpperCase()}</span>
                <select
                  value={axisVarString(selection.axes[axis])}
                  onChange={(e) => setAxisVariable(axis, e.target.value)}
                  aria-label={`${axis} axis variable`}
                >
                  {AXIS_VARIABLES.map((v) => (
                    <option key={v.value} value={v.value}>
                      {v.label}
                    </option>
                  ))}
                </select>
                <select
                  value={selection.axes[axis].scale}
                  onChange={(e) => setAxisScale(axis, e.target.value as "log" | "linear")}
                  aria-label={`${axis} axis scale`}
                >
                  <option value="linear">linear</option>
                  <option value="log">log</option>
                </select>
              </div>
            ))}
          </div>
        </section>

        {/* Composite weights */}
        <section className="cp-sec cp-sec-wide">
          <div className="cp-label" title={methodologyLabel("composite quality")}>
            Composite components &amp; weights — {methodologyLabel()}
          </div>
          <div className="cp-weights">
            {opts.metrics.map((name) => (
              <div key={name} className={`cp-weight ${metricEnabled(name) ? "on" : ""}`}>
                <label className="cp-check">
                  <input
                    type="checkbox"
                    checked={metricEnabled(name)}
                    onChange={() => toggleMetric(name)}
                  />
                  {name}
                </label>
                <input
                  type="number"
                  min={0}
                  step={0.05}
                  value={weightOf(name)}
                  disabled={!metricEnabled(name)}
                  onChange={(e) => setWeight(name, Number(e.target.value))}
                  aria-label={`${name} weight`}
                />
              </div>
            ))}
            <div className="cp-weight">
              <span className="muted">others (catch-all)</span>
              <input
                type="number"
                min={0}
                step={0.05}
                value={weightOf("others")}
                onChange={(e) => setWeight("others", Number(e.target.value))}
                aria-label="others weight"
              />
            </div>
          </div>
          <div className="cp-hint muted">
            Weights are renormalized over the present components; adjusting a weight recomputes the
            displayed score from the unchanged recorded values (id: {selection.weightSet.id}).
          </div>
        </section>
      </div>
    </div>
  );
}
