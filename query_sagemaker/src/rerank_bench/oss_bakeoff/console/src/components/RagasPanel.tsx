// RAGAS reference-free metrics. Grouped bars over the 5 metrics + overall, per reranker.
// Degrades to a "running…" placeholder when ragas_results.json is absent.

import { useMemo } from 'react';
import type { EChartsOption } from 'echarts';
import type { LoadedData } from '../lib/useData';
import type { RagasAgg } from '../types';
import { Card, THEME } from '../lib/ui';
import { colorFor } from '../types';
import { modelsByJudge } from '../lib/derive';
import EChart from '../lib/EChart';

const METRICS: { key: keyof RagasAgg; label: string }[] = [
  { key: 'context_precision', label: 'ctx precision' },
  { key: 'context_relevance', label: 'ctx relevance' },
  { key: 'faithfulness', label: 'faithfulness' },
  { key: 'response_relevancy', label: 'resp relevancy' },
  { key: 'response_groundedness', label: 'resp grounded' },
  { key: 'ragas_overall', label: 'overall' },
];

export default function RagasPanel({ data }: { data: LoadedData }) {
  const ragas = data.ragas;

  const models = useMemo(() => {
    if (!ragas) {
      return [];
    }
    const judged = modelsByJudge(data.judge.model_score);
    // Keep judge order, but only models RAGAS actually scored.
    return judged.filter((model) => ragas.aggregate[model]);
  }, [ragas, data.judge.model_score]);

  const option = useMemo<EChartsOption | null>(() => {
    if (!ragas || models.length === 0) {
      return null;
    }
    return {
      backgroundColor: 'transparent',
      grid: { left: 44, right: 16, top: 48, bottom: 36 },
      legend: {
        type: 'scroll',
        top: 4,
        textStyle: { color: THEME.dim, fontSize: 10 },
        pageTextStyle: { color: THEME.dim },
      },
      tooltip: {
        trigger: 'axis',
        backgroundColor: THEME.panel,
        borderColor: THEME.border,
        textStyle: { color: THEME.text, fontSize: 11 },
      },
      xAxis: {
        type: 'category',
        data: METRICS.map((metric) => metric.label),
        axisLabel: { color: THEME.dim, fontSize: 9, rotate: 25, fontFamily: 'monospace' },
        axisLine: { lineStyle: { color: THEME.border } },
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value',
        min: 0,
        max: 1,
        axisLabel: { color: THEME.dim, fontSize: 9, fontFamily: 'monospace' },
        splitLine: { lineStyle: { color: THEME.panelAlt } },
        axisLine: { lineStyle: { color: THEME.border } },
      },
      series: models.map((model) => {
        const agg = ragas.aggregate[model];
        return {
          name: model,
          type: 'bar',
          itemStyle: { color: colorFor(model) },
          data: METRICS.map((metric) => {
            const value = agg[metric.key];
            return value === null ? null : value;
          }),
        };
      }),
    };
  }, [ragas, models]);

  if (!ragas) {
    return (
      <Card title="RAGAS" sub="Reference-free answer-quality metrics">
        <div
          style={{
            padding: '36px 16px',
            textAlign: 'center',
            color: THEME.amber,
            fontSize: 13,
            border: `1px dashed ${THEME.border}`,
            borderRadius: 6,
            background: THEME.panelAlt,
          }}
        >
          <div style={{ marginBottom: 6 }}>running…</div>
          <div style={{ fontSize: 11, color: THEME.dim }}>
            ragas_results.json not yet present — this panel populates once the RAGAS run finishes.
          </div>
        </div>
      </Card>
    );
  }

  const sampleN = models.length > 0 ? ragas.aggregate[models[0]]?.n : undefined;

  return (
    <Card
      title="RAGAS"
      sub={`secondary / saturated — accuracy section is the verdict · ${ragas.meta.gen_model ?? 'generator'} answers · ${ragas.meta.judge_model ?? 'judge'} · top-${ragas.meta.topk ?? '?'}${sampleN ? ` · n=${sampleN}` : ''}`}
    >
      {option ? (
        <EChart option={option} style={{ height: 360 }} />
      ) : (
        <div style={{ color: THEME.dim, fontSize: 11, padding: 12 }}>
          RAGAS present but no per-model aggregates to chart.
        </div>
      )}
      <div style={{ fontSize: 10, color: THEME.dimmer, marginTop: 8, lineHeight: 1.6 }}>
        Higher is better on all five metrics; overall is the RAGAS composite. Each reranker feeds its
        top-k retrieved context to the same generator, so differences reflect retrieval quality.
      </div>
    </Card>
  );
}
