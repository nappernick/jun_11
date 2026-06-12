/* ============================================================================
   theme.js — design tokens, 3 directions, model colors, ECharts base options
   ========================================================================== */
(function () {
  'use strict';

  // 3 comparable visual directions. Switchable live.
  const THEMES = {
    squid: {
      label: 'Squid Ink',
      blurb: 'Dark enterprise console',
      vars: {
        '--bg':        '#0c1623',
        '--bg2':       '#0f1c2e',
        '--panel':     '#13243a',
        '--panel2':    '#172a43',
        '--panel-hi':  '#1d3552',
        '--line':      '#23415f',
        '--line-soft': '#1a3048',
        '--ink':       '#eaf1fa',
        '--ink2':      '#aebfd4',
        '--ink3':      '#7488a3',
        '--accent':    '#f7a14b',
        '--accent2':   '#ffc684',
        '--good':      '#4cc38a',
        '--warn':      '#f7a14b',
        '--bad':       '#e5688b',
        '--grid':      'rgba(140,165,200,0.10)',
        '--grid-strong':'rgba(140,165,200,0.18)',
        '--shadow':    '0 1px 0 rgba(255,255,255,0.03), 0 18px 40px -24px rgba(0,0,0,0.8)',
        '--chip':      'rgba(255,255,255,0.05)',
        '--scheme':    'dark',
      },
    },
    paper: {
      label: 'Paper',
      blurb: 'Light analytical brief',
      vars: {
        '--bg':        '#eef1f5',
        '--bg2':       '#e7ebf1',
        '--panel':     '#ffffff',
        '--panel2':    '#f6f8fb',
        '--panel-hi':  '#eef2f7',
        '--line':      '#dde3ec',
        '--line-soft': '#e8edf3',
        '--ink':       '#16202e',
        '--ink2':      '#475569',
        '--ink3':      '#8493a6',
        '--accent':    '#c2591a',
        '--accent2':   '#e07b34',
        '--good':      '#1d8a5e',
        '--warn':      '#c2591a',
        '--bad':       '#c43c63',
        '--grid':      'rgba(40,60,90,0.07)',
        '--grid-strong':'rgba(40,60,90,0.14)',
        '--shadow':    '0 1px 2px rgba(20,32,46,0.04), 0 14px 30px -22px rgba(20,32,46,0.28)',
        '--chip':      'rgba(20,32,46,0.045)',
        '--scheme':    'light',
      },
    },
    aurora: {
      label: 'Aurora',
      blurb: 'Cool slate telemetry',
      vars: {
        '--bg':        '#081316',
        '--bg2':       '#0a181c',
        '--panel':     '#0e2127',
        '--panel2':    '#112a31',
        '--panel-hi':  '#163740',
        '--line':      '#1d424c',
        '--line-soft': '#16323a',
        '--ink':       '#e6f2f1',
        '--ink2':      '#a3c4c4',
        '--ink3':      '#6d9494',
        '--accent':    '#36d6c0',
        '--accent2':   '#7df0df',
        '--good':      '#46cf9a',
        '--warn':      '#f2b755',
        '--bad':       '#ef6f8e',
        '--grid':      'rgba(120,200,195,0.09)',
        '--grid-strong':'rgba(120,200,195,0.17)',
        '--shadow':    '0 1px 0 rgba(255,255,255,0.03), 0 18px 40px -24px rgba(0,0,0,0.85)',
        '--chip':      'rgba(255,255,255,0.05)',
        '--scheme':    'dark',
      },
    },
  };

  // Model categorical colors — harmonious oklch, hue per model from data.
  // Slightly different lightness/chroma per scheme for legibility.
  function modelColor(hue, scheme) {
    if (scheme === 'light') return `oklch(0.58 0.15 ${hue})`;
    return `oklch(0.74 0.142 ${hue})`;
  }
  function buildModelColors(scheme) {
    const out = {};
    (window.DATA ? window.DATA.models : []).forEach(m => { out[m.id] = modelColor(m.hue, scheme); });
    return out;
  }

  const state = {
    name: localStorage.getItem('mc-theme') || 'squid',
    colors: {},
    scheme: 'dark',
  };

  function apply(name) {
    const t = THEMES[name] || THEMES.squid;
    state.name = name;
    state.scheme = t.vars['--scheme'];
    const root = document.documentElement;
    Object.entries(t.vars).forEach(([k, v]) => root.style.setProperty(k, v));
    root.setAttribute('data-scheme', t.vars['--scheme']);
    root.setAttribute('data-theme', name);
    state.colors = buildModelColors(state.scheme);
    localStorage.setItem('mc-theme', name);
  }

  // ---- ECharts base option (merged into every chart) ---------------------
  function css(v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }

  function echartsBase() {
    const ink = css('--ink'), ink2 = css('--ink2'), ink3 = css('--ink3');
    const grid = css('--grid'), gridStrong = css('--grid-strong');
    const panel = css('--panel'), line = css('--line');
    return {
      textStyle: { fontFamily: 'inherit', color: ink2,
        fontFamily: '"IBM Plex Sans","Helvetica Neue",Helvetica,Arial,sans-serif' },
      grid: { left: 54, right: 22, top: 28, bottom: 44, containLabel: true },
      tooltip: {
        backgroundColor: css('--panel-hi'),
        borderColor: line, borderWidth: 1,
        padding: [10, 12],
        textStyle: { color: ink, fontSize: 12,
          fontFamily: '"IBM Plex Sans",Helvetica,Arial,sans-serif' },
        extraCssText: 'border-radius:10px; box-shadow:0 12px 32px -16px rgba(0,0,0,.6); backdrop-filter:saturate(1.2);',
        axisPointer: { lineStyle: { color: ink3, type: 'dashed', width: 1 },
          crossStyle: { color: ink3 }, label: { backgroundColor: css('--panel-hi'), color: ink, borderColor: line } },
      },
      categoryAxis: {
        axisLine: { lineStyle: { color: line } },
        axisTick: { show: false },
        axisLabel: { color: ink3, fontSize: 11.5,
          fontFamily: '"IBM Plex Mono",ui-monospace,monospace' },
        splitLine: { show: false },
      },
      valueAxis: {
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: ink3, fontSize: 11.5,
          fontFamily: '"IBM Plex Mono",ui-monospace,monospace' },
        splitLine: { lineStyle: { color: grid, type: 'solid' } },
        nameTextStyle: { color: ink3, fontSize: 11 },
      },
      legend: { textStyle: { color: ink2, fontSize: 12 }, inactiveColor: ink3, icon: 'roundRect',
        itemWidth: 12, itemHeight: 4, itemGap: 16 },
    };
  }

  window.MC = window.MC || {};
  window.MC.theme = {
    THEMES, apply,
    get name() { return state.name; },
    get scheme() { return state.scheme; },
    get colors() { return state.colors; },
    color(id) { return state.colors[id] || css('--ink2'); },
    echartsBase, css,
    refreshColors() { state.colors = buildModelColors(state.scheme); },
  };
})();
