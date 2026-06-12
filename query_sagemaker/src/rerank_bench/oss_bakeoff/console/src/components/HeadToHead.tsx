// echarts heatmap of judge.winrate_matrix. Cell[row][col].winrate = row beats column %.

import { useMemo } from 'react';
import type { EChartsOption } from 'echarts';
import type { LoadedData } from '../lib/useData';
import { Card, THEME } from '../lib/ui';
import { modelsByJudge } from '../lib/derive';
import EChart from '../lib/EChart';

export default function HeadToHead({ data }: { data: LoadedData }) {
  const { judge } = data;

  // Order rows/cols by overall judge strength (best at top-left for a readable gradient).
  const models = useMemo(() => modelsByJudge(judge.model_score), [judge.model_score]);

  const option = useMemo<EChartsOption>(() => {
    // Heatmap data: [colIndex, rowIndex, winrate%]. Diagonal (self vs self) left empty.
    const cells: { value: [number, number, number]; cell: { wins: number; losses: number; ties: number } | null }[] = [];
    for (let rowIndex = 0; rowIndex < models.length; rowIndex++) {
      for (let colIndex = 0; colIndex < models.length; colIndex++) {
        const rowModel = models[rowIndex];
        const colModel = models[colIndex];
        if (rowModel === colModel) {
          continue;
        }
        const cell = judge.winrate_matrix[rowModel]?.[colModel];
        if (!cell) {
          continue;
        }
        cells.push({
          value: [colIndex, rowIndex, Math.round(cell.winrate * 100)],
          cell,
        });
      }
    }

    return {
      backgroundColor: 'transparent',
      grid: { left: 110, right: 24, top: 36, bottom: 96 },
      tooltip: {
        backgroundColor: THEME.panel,
        borderColor: THEME.border,
        textStyle: { color: THEME.text, fontSize: 11 },
        formatter: (param: unknown) => {
          const typed = param as { data?: { value: [number, number, number]; cell: { wins: number; losses: number; ties: number } | null } };
          const datum = typed.data;
          if (!datum) {
            return '';
          }
          const [colIndex, rowIndex, winrate] = datum.value;
          const cell = datum.cell;
          return [
            `<b>${models[rowIndex]}</b> vs ${models[colIndex]}`,
            `win-rate: ${winrate}%`,
            cell ? `${cell.wins}W · ${cell.losses}L · ${cell.ties}T` : '',
          ].join('<br/>');
        },
      },
      xAxis: {
        type: 'category',
        data: models,
        position: 'bottom',
        splitArea: { show: true },
        axisLabel: { color: THEME.dim, fontSize: 9, rotate: 40, fontFamily: 'monospace' },
        axisLine: { lineStyle: { color: THEME.border } },
        axisTick: { show: false },
        name: 'opponent (column)',
        nameLocation: 'middle',
        nameGap: 78,
        nameTextStyle: { color: THEME.dimmer, fontSize: 10 },
      },
      yAxis: {
        type: 'category',
        data: models,
        inverse: true,
        splitArea: { show: true },
        axisLabel: { color: THEME.dim, fontSize: 9, fontFamily: 'monospace' },
        axisLine: { lineStyle: { color: THEME.border } },
        axisTick: { show: false },
      },
      visualMap: {
        min: 0,
        max: 100,
        calculable: false,
        orient: 'horizontal',
        left: 'center',
        bottom: 4,
        itemWidth: 12,
        itemHeight: 90,
        text: ['row wins', 'row loses'],
        textStyle: { color: THEME.dim, fontSize: 9 },
        inRange: { color: ['#3a1d1d', '#161a22', '#15351f', '#3fb950'] },
      },
      series: [
        {
          type: 'heatmap',
          data: cells,
          label: {
            show: true,
            color: THEME.text,
            fontSize: 9.5,
            fontFamily: 'monospace',
            formatter: (param: unknown) => {
              const typed = param as { value: [number, number, number] };
              return `${typed.value[2]}`;
            },
          },
          itemStyle: { borderColor: THEME.bg, borderWidth: 2 },
          emphasis: { itemStyle: { borderColor: THEME.text, borderWidth: 1 } },
        },
      ],
    };
  }, [judge.winrate_matrix, models]);

  return (
    <Card
      title="Head-to-head"
      sub="Row model's win-rate (%) vs column model across both judge orderings. Green = row wins."
    >
      <EChart option={option} style={{ height: 460 }} />
    </Card>
  );
}
