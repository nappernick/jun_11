/* ============================================================================
   charts.js — reusable ECharts option builders shared across tabs
   ========================================================================== */
(function () {
  'use strict';
  const MC = window.MC;
  MC.tabs = MC.tabs || {};
  const D = window.DATA, T = MC.theme, fmt = MC.fmt;

  function css(v) { return T.css(v); }

  // alpha helper for oklch strings -> use color-mix via canvas? Simpler: rgba fallback.
  function fade(color, a) { return `color-mix(in srgb, ${color} ${Math.round(a * 100)}%, transparent)`; }

  // Multi-model daily time series with optional confidence band + dataZoom.
  // accessor(modelId) -> { values:[], lo?:[], hi?:[] }
  function timeSeries(opts) {
    const { accessor, yName, valueFmt, band, yMin, yMax, dataZoom = true, smooth = true } = opts;
    const models = MC.api.selModels();
    const base = T.echartsBase();
    const series = [];

    models.forEach(m => {
      const a = accessor(m.id);
      const col = T.color(m.id);
      if (band && a.lo && a.hi) {
        // lower (transparent) + stacked diff (faint fill) to form a band
        series.push({
          name: m.id + '__lo', type: 'line', data: a.lo, stack: 'band-' + m.id,
          lineStyle: { opacity: 0 }, symbol: 'none', silent: true, z: 1,
          areaStyle: { opacity: 0 }, tooltip: { show: false },
        });
        series.push({
          name: m.id + '__band', type: 'line',
          data: a.hi.map((h, i) => +(h - a.lo[i]).toFixed(3)), stack: 'band-' + m.id,
          lineStyle: { opacity: 0 }, symbol: 'none', silent: true, z: 1,
          areaStyle: { color: fade(col, 0.12) }, tooltip: { show: false },
        });
      }
      series.push({
        name: m.short, type: 'line', data: a.values, modelId: m.id,
        smooth, symbol: 'circle', symbolSize: 1, showSymbol: false,
        sampling: 'lttb', z: 5,
        lineStyle: { width: 2, color: col },
        itemStyle: { color: col },
        emphasis: { focus: 'series', lineStyle: { width: 3 } },
        areaStyle: opts.area ? { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [{ offset: 0, color: fade(col, 0.22) }, { offset: 1, color: fade(col, 0) }] } } : undefined,
      });
    });

    const opt = Object.assign({}, base, {
      grid: { left: 8, right: 18, top: 18, bottom: dataZoom ? 58 : 30, containLabel: true },
      tooltip: Object.assign({}, base.tooltip, {
        trigger: 'axis',
        formatter(ps) {
          ps = ps.filter(p => p.seriesName && !/__lo|__band/.test(p.seriesName));
          if (!ps.length) return '';
          let h = `<div style="font:600 11px var(--mono);color:${css('--ink3')};margin-bottom:6px">${fmt.date(ps[0].axisValue)}</div>`;
          ps.sort((a, b) => b.value - a.value);
          ps.forEach(p => {
            h += `<div style="display:flex;align-items:center;gap:8px;margin:2px 0;font-size:12px">
              <span style="width:8px;height:8px;border-radius:50%;background:${p.color}"></span>
              <span style="color:${css('--ink2')};min-width:84px">${p.seriesName}</span>
              <b style="font-family:var(--mono);color:${css('--ink')}">${valueFmt ? valueFmt(p.value) : p.value}</b></div>`;
          });
          return h;
        },
      }),
      xAxis: Object.assign({}, base.categoryAxis, {
        type: 'category', data: D.dates, boundaryGap: false,
        axisLabel: Object.assign({}, base.categoryAxis.axisLabel, {
          formatter: v => fmt.date(v), interval: Math.floor(D.dates.length / 8), hideOverlap: true }),
      }),
      yAxis: Object.assign({}, base.valueAxis, {
        type: 'value', name: yName, min: yMin, max: yMax, scale: !yMin && yMin !== 0,
        axisLabel: Object.assign({}, base.valueAxis.axisLabel, { formatter: valueFmt || (v => v) }),
      }),
      series,
    });
    if (dataZoom) {
      opt.dataZoom = [
        { type: 'inside', start: 55, end: 100, zoomOnMouseWheel: true, moveOnMouseMove: true },
        { type: 'slider', start: 55, end: 100, height: 22, bottom: 14,
          borderColor: css('--line'), backgroundColor: 'transparent',
          fillerColor: fade(css('--accent'), 0.10),
          handleStyle: { color: css('--panel-hi'), borderColor: css('--accent') },
          moveHandleStyle: { color: css('--line') },
          dataBackground: { lineStyle: { color: css('--ink3') }, areaStyle: { color: fade(css('--ink3'), 0.18) } },
          selectedDataBackground: { lineStyle: { color: css('--accent') }, areaStyle: { color: fade(css('--accent'), 0.2) } },
          textStyle: { color: css('--ink3'), fontFamily: 'var(--mono)', fontSize: 10 },
          labelFormatter: v => fmt.date(D.dates[Math.max(0, Math.min(D.dates.length - 1, v))]) },
      ];
    }
    return opt;
  }

  MC.charts = { timeSeries, fade, css };
})();
