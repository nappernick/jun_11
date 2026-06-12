// Two echarts boxplots:
//   1. per-query LATENCY (log y) — all points, no clipping; cold-starts visible as upper whiskers.
//   2. per-query CONFIDENCE = RAW top-minus-2nd margin (note: scales differ across families).

import { useMemo } from 'react';
import type { EChartsOption } from 'echarts';
import type { LoadedData } from '../lib/useData';
import { Card, THEME } from '../lib/ui';
import { colorFor } from '../types';
import { perQueryLatencies, rawTopMinus2nd, boxplotSummary, modelsByJudge } from '../lib/derive';
import EChart from '../lib/EChart';

interface BoxData {
  models: string[];
  boxes: [number, number, number, number, number][];
}

function buildBoxData(
  models: string[],
  series: (model: string) => number[],
): BoxData {
  const keptModels: string[] = [];
  const boxes: [number, number, number, number, number][] = [];
  for (const model of models) {
    const summary = boxplotSummary(series(model));
    if (summary) {
      keptModels.push(model);
      boxes.push(summary);
    }
  }
  return { models: keptModels, boxes };
}

function boxOption(
  title: string,
  data: BoxData,
  options: { logY: boolean; valueFormat: (value: number) => string; yName: string },
): EChartsOption {
  return {
    backgroundColor: 'transparent',
    grid: { left: 56, right: 16, top: 28, bottom: 70 },
    title: {
      text: title,
      left: 0,
      top: 0,
      textStyle: { color: THEME.dim, fontSize: 11, fontWeight: 'normal' },
    },
    tooltip: {
      trigger: 'item',
      backgroundColor: THEME.panel,
      borderColor: THEME.border,
      textStyle: { color: THEME.text, fontSize: 11 },
      formatter: (param: unknown) => {
        // echarts normalizes each boxplot item in place and prepends the category base index,
        // so at tooltip time param.data.value is [base, min, q1, median, q3, max] (6 elements).
        const typed = param as { name?: string; data?: { value?: number[] } };
        const values = typed.data?.value ?? [];
        if (values.length < 6) {
          return typed.name ?? '';
        }
        const [, low, q1, median, q3, high] = values;
        return [
          `<b>${typed.name}</b>`,
          `max: ${options.valueFormat(high)}`,
          `Q3: ${options.valueFormat(q3)}`,
          `median: ${options.valueFormat(median)}`,
          `Q1: ${options.valueFormat(q1)}`,
          `min: ${options.valueFormat(low)}`,
        ].join('<br/>');
      },
    },
    xAxis: {
      type: 'category',
      data: data.models,
      axisLabel: { color: THEME.dim, fontSize: 9, rotate: 35, fontFamily: 'monospace' },
      axisLine: { lineStyle: { color: THEME.border } },
      axisTick: { show: false },
    },
    yAxis: {
      type: options.logY ? 'log' : 'value',
      name: options.yName,
      nameTextStyle: { color: THEME.dimmer, fontSize: 10 },
      axisLabel: {
        color: THEME.dim,
        fontSize: 9,
        fontFamily: 'monospace',
        formatter: (value: number) => options.valueFormat(value),
      },
      splitLine: { lineStyle: { color: THEME.panelAlt } },
      axisLine: { lineStyle: { color: THEME.border } },
    },
    series: [
      {
        type: 'boxplot',
        data: data.boxes.map((box, index) => ({
          value: box,
          itemStyle: {
            color: 'transparent',
            borderColor: colorFor(data.models[index]),
            borderWidth: 1.5,
          },
        })),
        boxWidth: ['25%', '55%'],
      },
    ],
  };
}

export default function Distributions({ data }: { data: LoadedData }) {
  const models = useMemo(() => modelsByJudge(data.judge.model_score), [data.judge.model_score]);

  const latencyData = useMemo(
    () => buildBoxData(models, (model) => perQueryLatencies(data.scored, model)),
    [models, data.scored],
  );
  const confidenceData = useMemo(
    () => buildBoxData(models, (model) => rawTopMinus2nd(data.scored, model)),
    [models, data.scored],
  );

  const latencyOption = useMemo(
    () =>
      boxOption('per-query latency (log scale)', latencyData, {
        logY: true,
        valueFormat: (value) => `${Math.round(value)}`,
        yName: 'ms',
      }),
    [latencyData],
  );
  const confidenceOption = useMemo(
    () =>
      boxOption('per-query top − 2nd (raw margin)', confidenceData, {
        logY: false,
        valueFormat: (value) => value.toFixed(2),
        yName: 'raw',
      }),
    [confidenceData],
  );

  return (
    <Card
      title="Distributions"
      sub="Per-query spread. Latency is log-scaled (cold-starts kept). Confidence margins use raw scores."
    >
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))',
          gap: 16,
        }}
      >
        <EChart option={latencyOption} style={{ height: 320 }} />
        <EChart option={confidenceOption} style={{ height: 320 }} />
      </div>
      <div style={{ fontSize: 10, color: THEME.dimmer, marginTop: 8, lineHeight: 1.6 }}>
        Raw top−2nd separation is NOT comparable across families: ettin/nemotron are logit-margins,
        qwen3 are unit-margins, cohere are squashed unit scores. Read each family against itself.
      </div>
    </Card>
  );
}
