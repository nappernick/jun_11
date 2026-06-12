/* ============================================================================
   app.js — shell controller: state, tabs, filters, chart lifecycle helpers
   ========================================================================== */
(function () {
  'use strict';
  const D = window.DATA, T = window.MC.theme, fmt = window.MC.fmt;

  const TABS = [
    { id: 'overview',  label: 'Overview' },
    { id: 'latency',   label: 'Latency' },
    { id: 'quality',   label: 'Quality' },
    { id: 'multiturn', label: 'Multi-turn' },
    { id: 'geography', label: 'Geography' },
    { id: 'tradeoff',  label: 'Tradeoff Explorer' },
  ];

  const state = {
    tab: 'overview',
    models: new Set(D.models.map(m => m.id)), // selected models (shared filter)
  };

  // ---- chart registry: dispose on teardown, resize on window resize ------
  const charts = [];
  function makeChart(el, opt, extra) {
    const inst = echarts.init(el, null, { renderer: 'canvas' });
    inst.setOption(Object.assign({ animationDuration: 520, animationEasing: 'cubicOut' }, opt));
    charts.push(inst);
    if (extra && extra.onResize) inst.__onResize = extra.onResize;
    return inst;
  }
  function teardownCharts() { charts.forEach(c => { try { c.dispose(); } catch (e) {} }); charts.length = 0; }
  let rT;
  window.addEventListener('resize', () => {
    clearTimeout(rT);
    rT = setTimeout(() => charts.forEach(c => { try { c.resize(); if (c.__onResize) c.__onResize(c); } catch (e) {} }), 90);
  });

  // ---- selected-models helpers -------------------------------------------
  function selModels() { return D.models.filter(m => state.models.has(m.id)); }
  function isOn(id) { return state.models.has(id); }

  // ---- API exposed to tab modules ----------------------------------------
  const api = {
    D, T, fmt, state,
    makeChart, selModels, isOn,
    el(tag, cls, html) { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; },
    base() { return T.echartsBase(); },
    color(id) { return T.color(id); },
    // build a custom interactive legend bound to the shared model filter
    legend(container) {
      const wrap = api.el('div', 'clegend');
      D.models.forEach(m => {
        const li = api.el('div', 'li' + (isOn(m.id) ? '' : ' off'));
        li.innerHTML = `<span class="dot" style="background:${T.color(m.id)}"></span>${m.short}`;
        li.onclick = () => { toggleModel(m.id); };
        wrap.appendChild(li);
      });
      container.appendChild(wrap);
      return wrap;
    },
  };
  window.MC.api = api;
  window.MC.tabs = window.MC.tabs || {};

  // ---- rendering ----------------------------------------------------------
  const stage = () => document.getElementById('stage');
  function renderTab() {
    teardownCharts();
    const host = stage();
    host.innerHTML = '';
    const mod = window.MC.tabs[state.tab];
    const view = api.el('div', 'view' + (state.tab === 'tradeoff' || state.tab === 'geography' ? ' wide' : ''));
    host.appendChild(view);
    if (mod && mod.render) mod.render(view, api);
    host.scrollTop = 0;
    // re-resize after layout settles
    requestAnimationFrame(() => charts.forEach(c => { try { c.resize(); } catch (e) {} }));
  }

  function setTab(id) {
    state.tab = id;
    document.querySelectorAll('.tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === id));
    // hide filter rail on tabs that don't use the shared model filter the same way
    renderTab();
  }

  function toggleModel(id) {
    if (state.models.has(id)) { if (state.models.size > 1) state.models.delete(id); }
    else state.models.add(id);
    syncChips();
    renderTab();
  }
  function setOnlyModel(id) { state.models = new Set([id]); syncChips(); renderTab(); }
  function allModels() { state.models = new Set(D.models.map(m => m.id)); syncChips(); renderTab(); }

  function syncChips() {
    document.querySelectorAll('.mchip').forEach(c => {
      const on = state.models.has(c.dataset.id);
      c.classList.toggle('on', on); c.classList.toggle('off', !on);
    });
  }

  // ---- build static chrome ------------------------------------------------
  function buildChrome() {
    // tabs
    const tabsEl = document.getElementById('tabs');
    TABS.forEach((t, i) => {
      const b = document.createElement('button');
      b.dataset.tab = t.id;
      b.innerHTML = `<span class="tnum">${String(i + 1).padStart(2, '0')}</span>${t.label}`;
      b.onclick = () => setTab(t.id);
      tabsEl.appendChild(b);
    });
    // model chips
    const chips = document.getElementById('modelchips');
    D.models.forEach(m => {
      const c = document.createElement('div');
      c.className = 'mchip on'; c.dataset.id = m.id;
      c.innerHTML = `<span class="dot" style="background:${T.color(m.id)}"></span>${m.short}`;
      c.onclick = () => toggleModel(m.id);
      chips.appendChild(c);
    });
    document.getElementById('selAll').onclick = allModels;
    document.getElementById('selNone').onclick = () => {
      // "isolate" — keep just Opus + cheapest Sonnet + cheapest Haiku for a clean compare
      state.models = new Set(['opus48', 's46', 'haiku45']); syncChips(); renderTab();
    };
    // window label
    document.getElementById('winlabel').textContent =
      `${fmt.date(D.meta.window.start)} – ${fmt.date(D.meta.window.end)} 2026 · daily`;
    // theme switch
    const ts = document.getElementById('themeswitch');
    Object.entries(T.THEMES).forEach(([k, v]) => {
      const b = document.createElement('button');
      b.dataset.theme = k; b.textContent = v.label;
      b.onclick = () => switchTheme(k);
      ts.appendChild(b);
    });
  }

  function switchTheme(name) {
    T.apply(name);
    document.querySelectorAll('#themeswitch button').forEach(b => b.classList.toggle('on', b.dataset.theme === name));
    // rebuild chip dots & legend colors
    document.querySelectorAll('.mchip').forEach(c => {
      const dot = c.querySelector('.dot'); if (dot) dot.style.background = T.color(c.dataset.id);
    });
    renderTab();
  }

  function boot() {
    T.apply(T.name);
    buildChrome();
    document.querySelectorAll('#themeswitch button').forEach(b => b.classList.toggle('on', b.dataset.theme === T.name));
    setTab('overview');
    window.MC.app = { setTab, toggleModel, setOnlyModel, allModels, switchTheme, renderTab };
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
