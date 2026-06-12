/**
 * Eval3D — the real-time 3D eval view (design C5/C8; Req 10.6, 13.*, 14.2/14.3,
 * 18.2/18.3, 20.1).
 *
 * Hosts the shared Control_Panel plus a 3D archetype sub-selector
 * (trajectory | scatter | surface | bubble) and renders the chosen builder
 * through the typed `EChart` wrapper. The scene is driven by the SAME
 * `useEvalStream` state the rest of the eval surface uses (lifted into `App.tsx`),
 * so switching tabs never reloads or blanks it (Req 9.*).
 *
 * Around the scene it renders the decision aids the requirements call for:
 *   - the Ideal_Region indicator (low-latency + high-quality sweet spot, Req 13.1),
 *   - the Watch_For cues — a high-latency/low-quality count, plus per-agent drift
 *     and inconsistency flags (Req 13.2/13.3/13.4),
 *   - a quality/latency band legend (Req 13.5),
 *   - rotate/zoom (via `grid3D.viewControl` in the builders, Req 14.2) and a hover
 *     tooltip exposing full instance detail (Req 14.3),
 *   - ragas-derived and Authoritative_Judge signals as DISTINCT labeled values
 *     (Req 18.2/18.3) and the external-methodology label on every metric display
 *     (Req 20.1).
 */
import { useMemo, useState } from "react";
import type { JSX } from "react";
import type { EChartsOption } from "echarts";
import { EChart } from "../components/EChart";
import { ControlPanel } from "../eval/ControlPanel";
import {
  deriveChartView,
  defaultSelection,
  detectDrift,
  detectInconsistency,
  type EvalSelection,
} from "../eval/evalSelectors";
import {
  buildTrajectory3DOption,
  buildScatter3DOption,
  buildSurface3DOption,
  buildBubble3DOption,
  type BubbleSizeSource,
  type Scene3DControls,
} from "../eval/charts3d";
import {
  buildSpeedQuality2DOption,
  buildRetrievalCorrelation2DOption,
} from "../eval/charts2d";
import { ARCHETYPE_AXES } from "../eval/axisMapping";
import type { AxisMapping } from "../eval/axisMapping";
import { methodologyLabel, EXTERNAL_METHODOLOGY_CAVEAT } from "../eval/methodology";
import type { EvalStreamState } from "../api/useEvalStream";
import type { EvalInstance } from "../api/types";

type Archetype = "trajectory" | "scatter" | "surface" | "bubble";

const ARCHETYPES: ReadonlyArray<{ readonly id: Archetype; readonly label: string }> = [
  { id: "trajectory", label: "Trajectory" },
  { id: "scatter", label: "Scatter" },
  { id: "surface", label: "Surface" },
  { id: "bubble", label: "Bubble" },
];

const DEFAULT_SCENE_CONTROLS: Scene3DControls = {
  projection: "perspective",
  alpha: 28,
  beta: 42,
  distance: 200,
  minDistance: 30,
  maxDistance: 480,
  orthographicSize: 150,
  minOrthographicSize: 30,
  maxOrthographicSize: 320,
  center: [0, 0, 0],
  damping: 0.8,
  autoRotate: false,
  autoRotateDirection: "cw",
  autoRotateSpeed: 10,
  autoRotateAfterStill: 3,
  minAlpha: -90,
  maxAlpha: 90,
  minBeta: -180,
  maxBeta: 180,
  rotateSensitivity: 1,
  zoomSensitivity: 1,
  panSensitivity: 1,
  rotateMouseButton: "left",
  panMouseButton: "right",
  boxWidth: 118,
  boxDepth: 118,
  boxHeight: 92,
  showAxisPointer: true,
  showGrid: true,
};

const ANALYSIS_PRESETS: ReadonlyArray<{
  readonly id: string;
  readonly label: string;
  readonly description: string;
  readonly archetype: Archetype;
  readonly axes: AxisMapping;
}> = [
  {
    id: "frontier",
    label: "Speed quality frontier",
    description: "Latency against judge-triad quality over execution order.",
    archetype: "scatter",
    axes: ARCHETYPE_AXES.scatter!,
  },
  {
    id: "retrieval-faith",
    label: "Retrieval vs faithfulness",
    description: "Find cases where retrieval succeeds but generation drifts.",
    archetype: "bubble",
    axes: {
      x: { variable: { metric: "recall_at_k" }, scale: "linear", betterDirection: "higher" },
      y: { variable: { metric: "judge_faithfulness" }, scale: "linear", betterDirection: "higher" },
      z: { variable: "latency_ms", scale: "log", betterDirection: "lower" },
    },
  },
  {
    id: "judge-triad",
    label: "Judge triad",
    description: "Correctness, faithfulness, and completeness as the 3D space.",
    archetype: "scatter",
    axes: {
      x: { variable: { metric: "judge_correctness" }, scale: "linear", betterDirection: "higher" },
      y: { variable: { metric: "judge_faithfulness" }, scale: "linear", betterDirection: "higher" },
      z: { variable: { metric: "judge_completeness" }, scale: "linear", betterDirection: "higher" },
    },
  },
  {
    id: "landscape",
    label: "Quality landscape",
    description: "Interpolated quality surface across latency and execution order.",
    archetype: "surface",
    axes: ARCHETYPE_AXES.surface!,
  },
];

/** Watch_For thresholds (Req 13.2): the high-latency / low-quality danger zone. */
const HIGH_LATENCY_MS = 4000;
const LOW_QUALITY = 0.5;

export interface Eval3DProps {
  readonly stream: EvalStreamState;
}

export function Eval3D({ stream }: Eval3DProps): JSX.Element {
  const [selection, setSelection] = useState<EvalSelection>(() => defaultSelection());
  const [archetype, setArchetype] = useState<Archetype>("scatter");
  const [bubbleSizeBy, setBubbleSizeBy] = useState<BubbleSizeSource>("confidence");
  const [sceneControls, setSceneControls] = useState<Scene3DControls>(DEFAULT_SCENE_CONTROLS);

  const instances = useMemo<readonly EvalInstance[]>(
    () => [...stream.instances.values()],
    [stream.instances],
  );

  const view = useMemo(() => deriveChartView(instances, selection), [instances, selection]);

  const option = useMemo<EChartsOption>(() => {
    switch (archetype) {
      case "trajectory":
        return buildTrajectory3DOption(view, sceneControls);
      case "surface":
        return buildSurface3DOption(
          view,
          { xBuckets: 12, zBuckets: 12, method: "bilinear" },
          sceneControls,
        );
      case "bubble":
        return buildBubble3DOption(view, bubbleSizeBy, sceneControls);
      case "scatter":
      default:
        return buildScatter3DOption(view, sceneControls);
    }
  }, [archetype, view, bubbleSizeBy, sceneControls]);

  const projectionOptions = useMemo(
    () => ({
      speedQuality: buildSpeedQuality2DOption(view),
      retrievalFaithfulness: buildRetrievalCorrelation2DOption(
        view,
        "recall_at_k",
        "judge_faithfulness",
      ),
    }),
    [view],
  );

  // Per-agent Watch_For flags + the danger-zone count over plottable instances.
  const agents = useMemo(
    () => [...new Set(view.instances.map((i) => i.agent_id))].sort(),
    [view.instances],
  );
  const cues = useMemo(
    () =>
      agents.map((id) => ({
        id,
        color: view.agentColors.get(id) ?? "#9aa7b4",
        drift: detectDrift(view, id),
        inconsistent: detectInconsistency(view, id),
        meanQuality: meanComposite(view.instances, view.qualityByInstanceId, id),
      })),
    [agents, view],
  );
  const dangerCount = useMemo(() => {
    let count = 0;
    for (const inst of view.instances) {
      const quality = view.qualityByInstanceId.get(inst.instance_id) ?? null;
      if (inst.latency_ms >= HIGH_LATENCY_MS && quality != null && quality < LOW_QUALITY) count += 1;
    }
    return count;
  }, [view]);
  const outliers = useMemo(() => {
    return view.instances
      .map((inst) => {
        const quality = view.qualityByInstanceId.get(inst.instance_id) ?? null;
        const recall = inst.retrieval.recall_at_k?.value ?? null;
        const faithfulness = inst.ragas.judge_faithfulness?.value ?? null;
        return { inst, quality, recall, faithfulness };
      })
      .filter(
        (row) =>
          row.quality != null &&
          ((row.recall != null && row.recall >= 0.75 && row.quality < 0.6) ||
            (row.faithfulness != null && row.faithfulness < 0.55) ||
            row.inst.latency_ms >= HIGH_LATENCY_MS),
      )
      .sort((leftRow, rightRow) => {
        const leftPenalty =
          (leftRow.recall != null && leftRow.recall >= 0.75 ? 1 : 0) +
          (leftRow.quality != null ? 1 - leftRow.quality : 0) +
          leftRow.inst.latency_ms / 10000;
        const rightPenalty =
          (rightRow.recall != null && rightRow.recall >= 0.75 ? 1 : 0) +
          (rightRow.quality != null ? 1 - rightRow.quality : 0) +
          rightRow.inst.latency_ms / 10000;
        return rightPenalty - leftPenalty;
      })
      .slice(0, 6);
  }, [view]);

  const hasData = view.instances.length > 0;
  const updateScene = (partial: Partial<Scene3DControls>): void =>
    setSceneControls((currentControls) => ({ ...currentControls, ...partial }));
  const updateSceneCenter = (centerIndex: 0 | 1 | 2, value: number): void =>
    setSceneControls((currentControls) => {
      const nextCenter: [number, number, number] = [
        currentControls.center[0],
        currentControls.center[1],
        currentControls.center[2],
      ];
      nextCenter[centerIndex] = value;
      return { ...currentControls, center: nextCenter };
    });

  return (
    <div className="view">
      <div className="shead">
        <h2>Eval · 3D</h2>
        <span className="sub">
          prompts × execution-time × judge-quality × latency · live · {EXTERNAL_METHODOLOGY_CAVEAT}
        </span>
        <span className="rule" />
        <span className={`pill state ${stream.status}`}>{stream.status}</span>
      </div>

      <div className="eval-toolbar">
        <nav className="subtabs" role="tablist" aria-label="3D analysis presets">
          {ANALYSIS_PRESETS.map((preset) => (
            <button
              key={preset.id}
              role="tab"
              className="subtab eval-preset"
              title={preset.description}
              onClick={() => {
                setArchetype(preset.archetype);
                setSelection((currentSelection) => ({ ...currentSelection, axes: preset.axes }));
                if (preset.archetype === "bubble") setBubbleSizeBy("confidence");
              }}
            >
              {preset.label}
            </button>
          ))}
        </nav>
        <nav className="subtabs" role="tablist" aria-label="3D archetype">
          {ARCHETYPES.map((a) => (
            <button
              key={a.id}
              role="tab"
              aria-selected={archetype === a.id}
              className={`subtab ${archetype === a.id ? "on" : ""}`}
              onClick={() => {
                setArchetype(a.id);
                // Seed this archetype's recommended axis combo (Control Panel can still override).
                const axes = ARCHETYPE_AXES[a.id];
                if (axes) setSelection((s) => ({ ...s, axes }));
              }}
            >
              {a.label}
            </button>
          ))}
        </nav>
        {archetype === "bubble" && (
          <label className="cp-field">
            <span>bubble size</span>
            <select
              value={bubbleSizeBy}
              onChange={(e) => setBubbleSizeBy(e.target.value as BubbleSizeSource)}
            >
              <option value="confidence">confidence</option>
              <option value="volume">volume</option>
              <option value="cost">cost</option>
            </select>
          </label>
        )}
      </div>

      {hasData ? (
        <>
          <div className="eval-hero-grid">
            <div className="panel eval-chart-panel">
              <div className="eval-chart-head">
                <div>
                  <div className="panel-title">3D scene</div>
                  <div className="muted">
                    Drag, zoom, pan, or use camera presets. Hover a point for instance detail.
                  </div>
                </div>
                <div className="eval-camera-presets" aria-label="camera presets">
                  <button className="btn" onClick={() => updateScene(DEFAULT_SCENE_CONTROLS)}>
                    Reset
                  </button>
                  <button className="btn" onClick={() => updateScene({ alpha: 90, beta: 0 })}>
                    Top
                  </button>
                  <button className="btn" onClick={() => updateScene({ alpha: 0, beta: 0 })}>
                    Front
                  </button>
                  <button className="btn" onClick={() => updateScene({ alpha: 28, beta: 42 })}>
                    Iso
                  </button>
                </div>
              </div>
              <EChart
                option={option}
                height={720}
                ariaLabel="3D eval scene: latency × quality × instance index"
              />
            </div>
            <aside className="panel eval-scene-controls" aria-label="3D graph controls">
              <div className="panel-title">3D controls</div>
              <SceneControls controls={sceneControls} onChange={updateScene} onCenterChange={updateSceneCenter} />
            </aside>
          </div>

          <details className="panel eval-control-drawer">
            <summary>
              <span>Filters, axes, weights</span>
              <span className="muted">
                {view.instances.length} plotted · {view.accounting.filteredOut.length} filtered ·{" "}
                {view.accounting.nonPlottable.length} non-plottable
              </span>
            </summary>
            <ControlPanel selection={selection} onChange={setSelection} instances={instances} />
          </details>

          <div className="eval-projection-grid">
            <div className="panel">
              <div className="panel-title">Projection · speed quality frontier</div>
              <EChart option={projectionOptions.speedQuality} height={260} ariaLabel="2D speed quality projection" />
            </div>
            <div className="panel">
              <div className="panel-title">Projection · retrieval vs faithfulness</div>
              <EChart
                option={projectionOptions.retrievalFaithfulness}
                height={260}
                ariaLabel="2D retrieval faithfulness projection"
              />
            </div>
            <div className="panel eval-outliers">
              <div className="panel-title">Watch list</div>
              {outliers.length === 0 ? (
                <div className="muted">No high-priority outliers in the current lens.</div>
              ) : (
                outliers.map((row) => (
                  <div key={row.inst.instance_id} className="eval-outlier-row">
                    <span className="dot" style={{ background: view.agentColors.get(row.inst.agent_id) }} />
                    <div>
                      <b>{row.inst.agent_id}</b>
                      <span className="muted"> · {row.inst.instance_id}</span>
                      <div className="muted">
                        q {row.quality?.toFixed(3) ?? "n/a"} · recall{" "}
                        {row.recall?.toFixed(3) ?? "n/a"} · faith{" "}
                        {row.faithfulness?.toFixed(3) ?? "n/a"} · {Math.round(row.inst.latency_ms)}ms
                      </div>
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>

          {/* Ideal_Region + Watch_For cues + legend */}
          <div className="eval-aux">
            <div className="panel eval-aux-card">
              <div className="panel-title">Ideal region &amp; Watch-For cues</div>
              <div className="eval-cue ideal">
                <span className="swatch-ideal" />
                Ideal region — low latency &amp; high quality (the sweet spot, Req 13.1)
              </div>
              <div className={`eval-cue ${dangerCount > 0 ? "warn" : ""}`}>
                <span className="swatch-danger" />
                High-latency / low-quality zone: <b>{dangerCount}</b> instance
                {dangerCount === 1 ? "" : "s"}
                {" "}(≥{HIGH_LATENCY_MS}ms &amp; &lt;{LOW_QUALITY} quality)
              </div>
              <div className="eval-cue-list">
                {cues.map((c) => (
                  <div key={c.id} className="eval-cue-agent">
                    <span className="dot" style={{ background: c.color }} />
                    <span className="eval-cue-name">{c.id}</span>
                    {c.drift && <span className="pill partial">drift ↓</span>}
                    {c.inconsistent && <span className="pill none">inconsistent</span>}
                    {!c.drift && !c.inconsistent && <span className="pill full">stable</span>}
                  </div>
                ))}
              </div>
            </div>

            <div className="panel eval-aux-card">
              <div className="panel-title">Quality &amp; latency bands</div>
              <div className="eval-bands">
                <div className="eval-band">
                  <b>Quality</b>
                  <span className="pill none">low &lt;0.5</span>
                  <span className="pill partial">mid 0.5–0.75</span>
                  <span className="pill full">high ≥0.75</span>
                </div>
                <div className="eval-band">
                  <b>Latency</b>
                  <span className="pill full">fast &lt;1s</span>
                  <span className="pill partial">mid 1–4s</span>
                  <span className="pill none">slow ≥4s</span>
                </div>
              </div>
            </div>

            {/* ragas-derived vs Authoritative_Judge — DISTINCT labeled signals (Req 18.2/18.3) */}
            <div className="panel eval-aux-card">
              <div className="panel-title" title={methodologyLabel()}>
                Signals (distinct &amp; never conflated)
              </div>
              <div className="eval-signals">
                <div className="eval-signal">
                  <span className="eval-signal-tag ragas">ragas-derived</span>
                  <span className="muted">{EXTERNAL_METHODOLOGY_CAVEAT}</span>
                  <div className="eval-signal-rows">
                    {cues.map((c) => (
                      <div key={c.id} className="eval-signal-row">
                        <span className="dot" style={{ background: c.color }} />
                        {c.id}: composite{" "}
                        <b>{c.meanQuality == null ? "n/a" : c.meanQuality.toFixed(3)}</b>
                      </div>
                    ))}
                  </div>
                </div>
                <div className="eval-signal">
                  <span className="eval-signal-tag judge">Authoritative_Judge</span>
                  <span className="muted">
                    a separate authoritative verdict — never summed into the ragas composite
                    (sourced from the judge study, not this record)
                  </span>
                </div>
              </div>
            </div>
          </div>
        </>
      ) : (
        <>
          <div className="panel eval-empty-hero">
            <div>
              <div className="panel-title">3D scene is ready</div>
              <div className="muted">
                Start a real run from Metrics. The first eval instances will populate the 3D
                scene, projections, watch list, and cue cards here.
              </div>
            </div>
            <SceneControls controls={sceneControls} onChange={updateScene} onCenterChange={updateSceneCenter} />
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
        GBBO · ragas eval · 3D · {methodologyLabel()}. Correctness outranks latency: every rendered
        point maps to exactly one recorded instance.
      </div>
    </div>
  );
}

function SceneControls({
  controls,
  onChange,
  onCenterChange,
}: {
  readonly controls: Scene3DControls;
  readonly onChange: (partial: Partial<Scene3DControls>) => void;
  readonly onCenterChange: (centerIndex: 0 | 1 | 2, value: number) => void;
}): JSX.Element {
  return (
    <div className="scene-control-grid">
      <label className="cp-field">
        <span>projection</span>
        <select
          value={controls.projection}
          onChange={(event) =>
            onChange({ projection: event.target.value as Scene3DControls["projection"] })
          }
        >
          <option value="perspective">perspective</option>
          <option value="orthographic">orthographic</option>
          <option value="isometric">isometric</option>
        </select>
      </label>
      <ToggleField
        label="auto rotate"
        checked={controls.autoRotate}
        onChange={(checked) => onChange({ autoRotate: checked })}
      />
      <ToggleField
        label="axis pointer"
        checked={controls.showAxisPointer}
        onChange={(checked) => onChange({ showAxisPointer: checked })}
      />
      <ToggleField
        label="grid"
        checked={controls.showGrid}
        onChange={(checked) => onChange({ showGrid: checked })}
      />
      <RangeField label="alpha" value={controls.alpha} min={-90} max={90} step={1} onChange={(value) => onChange({ alpha: value })} />
      <RangeField label="beta" value={controls.beta} min={-180} max={180} step={1} onChange={(value) => onChange({ beta: value })} />
      <RangeField label="distance" value={controls.distance} min={controls.minDistance} max={controls.maxDistance} step={5} onChange={(value) => onChange({ distance: value })} />
      <RangeField label="ortho size" value={controls.orthographicSize} min={controls.minOrthographicSize} max={controls.maxOrthographicSize} step={5} onChange={(value) => onChange({ orthographicSize: value })} />
      <RangeField label="damping" value={controls.damping} min={0} max={1} step={0.05} onChange={(value) => onChange({ damping: value })} />
      <RangeField label="rotate sens." value={controls.rotateSensitivity} min={0} max={4} step={0.1} onChange={(value) => onChange({ rotateSensitivity: value })} />
      <RangeField label="zoom sens." value={controls.zoomSensitivity} min={0} max={4} step={0.1} onChange={(value) => onChange({ zoomSensitivity: value })} />
      <RangeField label="pan sens." value={controls.panSensitivity} min={0} max={4} step={0.1} onChange={(value) => onChange({ panSensitivity: value })} />
      <RangeField label="auto speed" value={controls.autoRotateSpeed} min={1} max={60} step={1} onChange={(value) => onChange({ autoRotateSpeed: value })} />
      <RangeField label="auto wait" value={controls.autoRotateAfterStill} min={0} max={12} step={0.5} onChange={(value) => onChange({ autoRotateAfterStill: value })} />
      <RangeField label="min alpha" value={controls.minAlpha} min={-90} max={90} step={1} onChange={(value) => onChange({ minAlpha: value })} />
      <RangeField label="max alpha" value={controls.maxAlpha} min={-90} max={90} step={1} onChange={(value) => onChange({ maxAlpha: value })} />
      <RangeField label="min beta" value={controls.minBeta} min={-360} max={360} step={5} onChange={(value) => onChange({ minBeta: value })} />
      <RangeField label="max beta" value={controls.maxBeta} min={-360} max={360} step={5} onChange={(value) => onChange({ maxBeta: value })} />
      <RangeField label="min distance" value={controls.minDistance} min={5} max={controls.maxDistance} step={5} onChange={(value) => onChange({ minDistance: value })} />
      <RangeField label="max distance" value={controls.maxDistance} min={controls.minDistance} max={800} step={10} onChange={(value) => onChange({ maxDistance: value })} />
      <RangeField label="min ortho" value={controls.minOrthographicSize} min={5} max={controls.maxOrthographicSize} step={5} onChange={(value) => onChange({ minOrthographicSize: value })} />
      <RangeField label="max ortho" value={controls.maxOrthographicSize} min={controls.minOrthographicSize} max={600} step={10} onChange={(value) => onChange({ maxOrthographicSize: value })} />
      <RangeField label="box width" value={controls.boxWidth} min={40} max={220} step={2} onChange={(value) => onChange({ boxWidth: value })} />
      <RangeField label="box depth" value={controls.boxDepth} min={40} max={220} step={2} onChange={(value) => onChange({ boxDepth: value })} />
      <RangeField label="box height" value={controls.boxHeight} min={40} max={220} step={2} onChange={(value) => onChange({ boxHeight: value })} />
      {(["x", "y", "z"] as const).map((axisName, centerIndex) => (
        <RangeField
          key={axisName}
          label={`center ${axisName}`}
          value={controls.center[centerIndex] ?? 0}
          min={-120}
          max={120}
          step={2}
          onChange={(value) => onCenterChange(centerIndex as 0 | 1 | 2, value)}
        />
      ))}
      <label className="cp-field">
        <span>auto direction</span>
        <select
          value={controls.autoRotateDirection}
          onChange={(event) =>
            onChange({ autoRotateDirection: event.target.value as "cw" | "ccw" })
          }
        >
          <option value="cw">clockwise</option>
          <option value="ccw">counter-clockwise</option>
        </select>
      </label>
      <label className="cp-field">
        <span>rotate mouse</span>
        <select
          value={controls.rotateMouseButton}
          onChange={(event) =>
            onChange({ rotateMouseButton: event.target.value as "left" | "middle" | "right" })
          }
        >
          <option value="left">left</option>
          <option value="middle">middle</option>
          <option value="right">right</option>
        </select>
      </label>
      <label className="cp-field">
        <span>pan mouse</span>
        <select
          value={controls.panMouseButton}
          onChange={(event) =>
            onChange({ panMouseButton: event.target.value as "left" | "middle" | "right" })
          }
        >
          <option value="left">left</option>
          <option value="middle">middle</option>
          <option value="right">right</option>
        </select>
      </label>
    </div>
  );
}

function RangeField({
  label,
  value,
  min,
  max,
  step,
  onChange,
}: {
  readonly label: string;
  readonly value: number;
  readonly min: number;
  readonly max: number;
  readonly step: number;
  readonly onChange: (value: number) => void;
}): JSX.Element {
  return (
    <label className="cp-field scene-range">
      <span>
        {label} <code>{Number.isInteger(value) ? value : value.toFixed(2)}</code>
      </span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
      />
    </label>
  );
}

function ToggleField({
  label,
  checked,
  onChange,
}: {
  readonly label: string;
  readonly checked: boolean;
  readonly onChange: (checked: boolean) => void;
}): JSX.Element {
  return (
    <label className="cp-check scene-toggle">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
      />
      {label}
    </label>
  );
}

/** Mean composite quality for one agent over the plottable, non-null values. */
function meanComposite(
  instances: readonly EvalInstance[],
  qualityById: ReadonlyMap<string, number | null>,
  agentId: string,
): number | null {
  let sum = 0;
  let n = 0;
  for (const inst of instances) {
    if (inst.agent_id !== agentId) continue;
    const q = qualityById.get(inst.instance_id) ?? null;
    if (q != null) {
      sum += q;
      n += 1;
    }
  }
  return n > 0 ? sum / n : null;
}
