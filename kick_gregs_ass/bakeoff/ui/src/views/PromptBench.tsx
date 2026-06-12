/**
 * PromptBench — a fixed A/B/C/D prompt leaderboard for one model.
 *
 * Dead-simple visualization (owner spec): one X–Y scatter per prompt (X = conversation
 * index 1..N, Y = overall score 0..1, faint mean line), the full prompt text beneath each,
 * and a header that crowns the winner (highest mean; ties-within-CI flagged). Points fill
 * in live as each conversation scores. Start / Reset controls. Entirely separate stream +
 * stores from the optimizers, so it can run alongside a v3 run.
 */
import { useMemo } from "react";
import type { JSX } from "react";
import type { EChartsOption } from "echarts";
import { EChart } from "../components/EChart";
import {
  usePromptBenchStream,
  type PromptBenchPromptState,
} from "../api/usePromptBenchStream";

function fmt(n: number | null | undefined): string {
  return n == null || Number.isNaN(n) ? "—" : n.toFixed(3);
}

function promptMean(prompt: PromptBenchPromptState): number | null {
  if (prompt.result) return prompt.result.triad;
  if (prompt.points.length === 0) return null;
  return prompt.points.reduce((sum, point) => sum + point.overall, 0) / prompt.points.length;
}

/** Distinct line colors per prompt for the combined overlay. */
const PROMPT_PALETTE: readonly string[] = [
  "#6aa9ff", "#f7a14b", "#7ee787", "#e879f9",
  "#f87171", "#fbbf24", "#34d399", "#a78bfa",
];

/** One combined line chart: each prompt a colored line across the 24 conversations. */
function combinedOption(prompts: readonly PromptBenchPromptState[]): EChartsOption {
  const series = prompts.map((p, i) => ({
    name: p.label,
    type: "line" as const,
    color: PROMPT_PALETTE[i % PROMPT_PALETTE.length] ?? "#6aa9ff",
    showSymbol: true,
    symbolSize: 6,
    smooth: false,
    connectNulls: true,
    emphasis: { focus: "series" as const },
    data: [...p.points]
      .sort((a, b) => a.conversation_index - b.conversation_index)
      .map((pt) => [pt.conversation_index, pt.overall] as [number, number]),
  }));
  return {
    backgroundColor: "transparent",
    grid: { left: 40, right: 16, top: 40, bottom: 30 },
    legend: {
      top: 4,
      type: "scroll",
      textStyle: { color: "#aebfd4", fontSize: 11 },
      data: prompts.map((p) => p.label),
    },
    tooltip: {
      trigger: "axis",
      valueFormatter: (v: unknown) => (v == null ? "—" : Number(v).toFixed(3)),
    },
    xAxis: {
      type: "value",
      name: "conversation",
      nameLocation: "middle",
      nameGap: 20,
      min: 0,
      minInterval: 1,
      axisLabel: { color: "#7488a3", fontSize: 10 },
      nameTextStyle: { color: "#7488a3", fontSize: 10 },
      splitLine: { show: false },
    },
    yAxis: {
      type: "value",
      min: 0,
      max: 1,
      axisLabel: { color: "#7488a3", fontSize: 10, formatter: (v: number) => v.toFixed(1) },
      splitLine: { lineStyle: { color: "rgba(140,165,200,0.10)" } },
    },
    series,
  };
}

function distributionOption(prompts: readonly PromptBenchPromptState[]): EChartsOption {
  const boxData = prompts.map((prompt) => {
    const values = prompt.points.map((point) => point.overall).sort((left, right) => left - right);
    if (values.length === 0) return [0, 0, 0, 0, 0];
    const quantileAt = (fraction: number): number => {
      const rawIndex = (values.length - 1) * fraction;
      const lowerIndex = Math.floor(rawIndex);
      const upperIndex = Math.ceil(rawIndex);
      const weight = rawIndex - lowerIndex;
      const lowerValue = values[lowerIndex] ?? 0;
      const upperValue = values[upperIndex] ?? lowerValue;
      return lowerValue + (upperValue - lowerValue) * weight;
    };
    return [
      values[0] ?? 0,
      quantileAt(0.25),
      quantileAt(0.5),
      quantileAt(0.75),
      values[values.length - 1] ?? 0,
    ];
  });
  const jitterData = prompts.flatMap((prompt, promptIndex) =>
    prompt.points.map((point) => [promptIndex, point.overall, prompt.label] as [number, number, string]),
  );
  return {
    backgroundColor: "transparent",
    grid: { left: 42, right: 18, top: 24, bottom: 58 },
    tooltip: {
      trigger: "item",
      confine: true,
      formatter: (params: unknown) => {
        const value = (params as { value?: unknown[] }).value;
        if (!Array.isArray(value)) return "";
        if (value.length >= 5) {
          return `min ${fmt(Number(value[1]))}<br/>median ${fmt(Number(value[3]))}<br/>max ${fmt(Number(value[5]))}`;
        }
        return `${value[2] ?? "prompt"} · ${fmt(Number(value[1]))}`;
      },
    },
    xAxis: {
      type: "category",
      data: prompts.map((prompt) => prompt.label),
      axisLabel: { color: "#7488a3", fontSize: 10, interval: 0, rotate: 18 },
    },
    yAxis: {
      type: "value",
      min: 0,
      max: 1,
      axisLabel: { color: "#7488a3", fontSize: 10 },
      splitLine: { lineStyle: { color: "rgba(140,165,200,0.10)" } },
    },
    series: [
      {
        type: "boxplot",
        name: "distribution",
        itemStyle: { color: "rgba(106,169,255,0.20)", borderColor: "#6aa9ff" },
        data: boxData,
      },
      {
        type: "scatter",
        name: "conversation",
        symbolSize: 6,
        itemStyle: { color: "#f7a14b", opacity: 0.55 },
        data: jitterData,
      },
    ],
  } as unknown as EChartsOption;
}

function pairedDifferenceOption(prompts: readonly PromptBenchPromptState[]): EChartsOption {
  const ranked = [...prompts]
    .filter((prompt) => prompt.points.length > 0)
    .sort((leftPrompt, rightPrompt) => (promptMean(rightPrompt) ?? -1) - (promptMean(leftPrompt) ?? -1));
  const contender = ranked[0];
  const baseline = ranked[1] ?? ranked[0];
  const baselinePoints = new Map(
    (baseline?.points ?? []).map((point) => [point.conversation_index, point.overall]),
  );
  const data =
    contender && baseline && contender.key !== baseline.key
      ? contender.points
          .filter((point) => baselinePoints.has(point.conversation_index))
          .map((point) => [
            point.conversation_index,
            point.overall - (baselinePoints.get(point.conversation_index) ?? point.overall),
          ] as [number, number])
          .sort((leftPoint, rightPoint) => leftPoint[0] - rightPoint[0])
      : [];
  return {
    backgroundColor: "transparent",
    grid: { left: 46, right: 18, top: 36, bottom: 36 },
    title: {
      text:
        contender && baseline && contender.key !== baseline.key
          ? `${contender.label} minus ${baseline.label}`
          : "Need two prompts with scored conversations",
      left: 4,
      top: 0,
      textStyle: { color: "#aebfd4", fontSize: 12, fontWeight: 500 },
    },
    tooltip: {
      trigger: "item",
      confine: true,
      formatter: (params: unknown) => {
        const value = (params as { value?: [number, number] }).value;
        return value ? `conversation ${value[0]} · delta ${fmt(value[1])}` : "";
      },
    },
    xAxis: {
      type: "value",
      name: "conversation",
      nameLocation: "middle",
      nameGap: 20,
      minInterval: 1,
      axisLabel: { color: "#7488a3", fontSize: 10 },
      nameTextStyle: { color: "#7488a3", fontSize: 10 },
      splitLine: { show: false },
    },
    yAxis: {
      type: "value",
      name: "score delta",
      nameLocation: "middle",
      nameGap: 34,
      axisLabel: { color: "#7488a3", fontSize: 10 },
      nameTextStyle: { color: "#7488a3", fontSize: 10 },
      splitLine: { lineStyle: { color: "rgba(140,165,200,0.10)" } },
    },
    series: [
      {
        type: "bar",
        name: "paired delta",
        data,
        itemStyle: {
          color: (params: { value?: [number, number] }) =>
            (params.value?.[1] ?? 0) >= 0 ? "#58c08a" : "#ef6a6a",
        },
        markLine: {
          silent: true,
          symbol: "none",
          lineStyle: { color: "#aebfd4", opacity: 0.55 },
          data: [{ yAxis: 0 }],
        },
      },
    ],
  } as unknown as EChartsOption;
}

function scatterOption(prompt: PromptBenchPromptState): EChartsOption {
  const data = prompt.points.map((p) => [p.conversation_index, p.overall] as [number, number]);
  const mean =
    prompt.result?.triad ??
    (prompt.points.length
      ? prompt.points.reduce((a, p) => a + p.overall, 0) / prompt.points.length
      : null);
  return {
    backgroundColor: "transparent",
    grid: { left: 40, right: 16, top: 16, bottom: 30 },
    tooltip: {
      trigger: "item",
      formatter: (params: unknown) => {
        const v = (params as { value: [number, number] }).value;
        return `conv ${v[0]} · ${v[1].toFixed(3)}`;
      },
    },
    xAxis: {
      type: "value",
      name: "conversation",
      nameLocation: "middle",
      nameGap: 20,
      min: 0,
      minInterval: 1,
      axisLabel: { color: "#7488a3", fontSize: 10 },
      nameTextStyle: { color: "#7488a3", fontSize: 10 },
      splitLine: { show: false },
    },
    yAxis: {
      type: "value",
      min: 0,
      max: 1,
      axisLabel: { color: "#7488a3", fontSize: 10, formatter: (v: number) => v.toFixed(1) },
      splitLine: { lineStyle: { color: "rgba(140,165,200,0.10)" } },
    },
    series: [
      {
        type: "scatter",
        symbolSize: 9,
        itemStyle: { color: "#6aa9ff", opacity: 0.85 },
        data,
        ...(mean != null
          ? {
              markLine: {
                silent: true,
                symbol: "none",
                lineStyle: { color: "#f7a14b", type: "dashed", width: 1.5, opacity: 0.8 },
                label: { show: true, formatter: `mean ${mean.toFixed(3)}`, color: "#f7a14b", fontSize: 10 },
                data: [{ yAxis: mean }],
              },
            }
          : {}),
      },
    ],
  };
}

function PromptPanel({ prompt, isWinner }: { prompt: PromptBenchPromptState; isWinner: boolean }): JSX.Element {
  const option = useMemo(() => scatterOption(prompt), [prompt]);
  const r = prompt.result;
  return (
    <div className={`pb-panel panel ${isWinner ? "winner" : ""}`} style={{ marginBottom: 18 }}>
      <div className="pb-panel-head" style={{ display: "flex", alignItems: "baseline", gap: 12 }}>
        <b style={{ fontSize: 15 }}>
          {prompt.label}
        </b>
        <span style={{ color: "#aebfd4", fontSize: 13 }}>
          {r ? `triad ${fmt(r.triad)} ± ${fmt(r.ci_half_width)}` : `${prompt.points.length} scored…`}
        </span>
        {r && (
          <span style={{ color: "#7488a3", fontSize: 12 }}>
            faith {fmt(r.per_dimension_mean?.faithfulness)} · corr{" "}
            {fmt(r.per_dimension_mean?.correctness)} · compl {fmt(r.per_dimension_mean?.completeness)} ·
            answered-when-unsure {fmt(r.answered_when_unsure_rate)} · confident-wrong{" "}
            {r.confident_wrong_count}
          </span>
        )}
        {prompt.failed && <span style={{ color: "#ff6b6b", fontSize: 12 }}>failed: {prompt.failed}</span>}
      </div>
      <EChart option={option} height={220} ariaLabel={`${prompt.label} per-conversation scores`} />
      <details style={{ marginTop: 8 }}>
        <summary style={{ cursor: "pointer", color: "#aebfd4", fontSize: 12 }}>
          prompt text ({prompt.key})
        </summary>
        <pre
          style={{
            whiteSpace: "pre-wrap",
            fontSize: 11.5,
            lineHeight: 1.45,
            color: "#cdd9ea",
            marginTop: 8,
            maxHeight: 360,
            overflow: "auto",
          }}
        >
          {prompt.text}
        </pre>
      </details>
    </div>
  );
}

export function PromptBench(): JSX.Element {
  const bench = usePromptBenchStream();
  const running = bench.status === "running";
  const combined = useMemo(() => combinedOption(bench.prompts), [bench.prompts]);
  const distribution = useMemo(() => distributionOption(bench.prompts), [bench.prompts]);
  const pairedDifference = useMemo(() => pairedDifferenceOption(bench.prompts), [bench.prompts]);
  const hasPoints = bench.prompts.some((p) => p.points.length > 0);
  const rankedPrompts = useMemo(
    () =>
      [...bench.prompts].sort(
        (leftPrompt, rightPrompt) =>
          (promptMean(rightPrompt) ?? -1) - (promptMean(leftPrompt) ?? -1),
      ),
    [bench.prompts],
  );

  return (
    <div className="view" style={{ padding: 22 }}>
      <div
        className="pb-header"
        style={{ display: "flex", alignItems: "center", gap: 16, marginBottom: 18 }}
      >
        <b style={{ fontSize: 18 }}>Prompt Bench</b>
        <span style={{ color: "#7488a3", fontSize: 13 }}>
          {bench.model || "sonnet-4.6-thinking-off"} · 24 held-out conversations
        </span>
        <span className={`v2-stream-badge ${bench.streamStatus}`}>
          {bench.streamStatus} · {bench.received}
        </span>
        <div style={{ flex: 1 }} />
        <button
          className="btn"
          disabled={running}
          onClick={() => void bench.start()}
          title="Resumes by default — any prompt that already completed (has a durable result) is reused, not re-scored. Use Reset first for a clean-slate run."
        >
          {running ? "Running…" : "Start / Resume"}
        </button>
        <button
          className="btn danger"
          onClick={() => void bench.reset()}
          title="Archive the current results & points and start fresh — the next Start re-scores every prompt."
        >
          Reset
        </button>
      </div>

      <div className="pb-scoreboard">
        <div className="panel pb-winner-card">
          <span className="v2-summary-label">leader</span>
          <b>{bench.winner?.label ?? rankedPrompts[0]?.label ?? "—"}</b>
          <span className="muted">
            {bench.winner
              ? `mean triad ${fmt(bench.winner.triad)}${
                  bench.winner.tie_within_ci ? " · tie within CI" : ""
                }`
              : "waiting for completed prompt results"}
          </span>
        </div>
        <div className="panel pb-winner-card">
          <span className="v2-summary-label">scored</span>
          <b>{bench.prompts.reduce((total, prompt) => total + prompt.points.length, 0)}</b>
          <span className="muted">conversation-level points</span>
        </div>
        <div className="panel pb-winner-card">
          <span className="v2-summary-label">prompts</span>
          <b>{bench.prompts.length}</b>
          <span className="muted">loaded from prompt bench</span>
        </div>
      </div>

      {rankedPrompts.length > 0 && (
        <div className="panel pb-rank-table">
          <div className="panel-title">Leaderboard</div>
          <table className="dt">
            <thead>
              <tr>
                <th>prompt</th>
                <th>mean</th>
                <th>CI</th>
                <th>n</th>
                <th>faith</th>
                <th>corr</th>
                <th>compl</th>
                <th>risk</th>
              </tr>
            </thead>
            <tbody>
              {rankedPrompts.map((prompt) => {
                const result = prompt.result;
                return (
                  <tr key={prompt.key} className={bench.winner?.prompt_key === prompt.key ? "sel" : ""}>
                    <td>{prompt.label}</td>
                    <td>{fmt(promptMean(prompt))}</td>
                    <td>{result ? `± ${fmt(result.ci_half_width)}` : "—"}</td>
                    <td>{result?.n_conversations ?? prompt.points.length}</td>
                    <td>{fmt(result?.per_dimension_mean?.faithfulness)}</td>
                    <td>{fmt(result?.per_dimension_mean?.correctness)}</td>
                    <td>{fmt(result?.per_dimension_mean?.completeness)}</td>
                    <td>
                      {result
                        ? `unsure ${fmt(result.answered_when_unsure_rate)} · wrong ${result.confident_wrong_count}`
                        : prompt.failed ?? "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {hasPoints && (
        <div className="pb-chart-grid">
          <div className="panel">
            <div style={{ color: "#aebfd4", fontSize: 13, marginBottom: 6 }}>
              All prompts · per-conversation overall
            </div>
            <EChart
              option={combined}
              height={300}
              ariaLabel="All prompts' per-conversation scores on one chart"
            />
          </div>
          <div className="panel">
            <div style={{ color: "#aebfd4", fontSize: 13, marginBottom: 6 }}>
              Paired delta · leader versus runner-up
            </div>
            <EChart
              option={pairedDifference}
              height={300}
              ariaLabel="Paired per-conversation prompt difference"
            />
          </div>
          <div className="panel pb-chart-wide">
            <div style={{ color: "#aebfd4", fontSize: 13, marginBottom: 6 }}>
              Distribution by prompt
            </div>
            <EChart
              option={distribution}
              height={280}
              ariaLabel="Prompt score distribution boxplots"
            />
          </div>
        </div>
      )}

      {bench.prompts.length === 0 ? (
        <div className="panel" style={{ padding: 18, color: "#7488a3" }}>
          No results yet. Press Start to score the prompts in bakeoff/promptbench/prompts/ on the
          24-conversation sample.
        </div>
      ) : (
        bench.prompts.map((p) => (
          <PromptPanel
            key={p.key}
            prompt={p}
            isWinner={bench.winner?.prompt_key === p.key}
          />
        ))
      )}
    </div>
  );
}
