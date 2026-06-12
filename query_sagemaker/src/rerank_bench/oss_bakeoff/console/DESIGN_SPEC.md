# OSS Reranker Bake-off Console — Design Spec (build contract)

A Vite + React + TS app (Bun). Charting: **echarts** + **echarts-gl** (installed).
Data: fetched at runtime from `/data/*.json` (already copied to `public/data/`).
Types: `src/types.ts` (the data contract — import from there, do not redefine).

## Aesthetic (match the user's "GBBO Console")
Dark, **monospace**, information-dense, calm. NOT generic/rounded/AI-pastel.
- bg `#0f1115`; panels `#161a22`; borders `#262b36`; text `#e6e6e6` / dim `#9aa4b2`.
- Header bar (sticky): left = title `OSS RERANKER · BAKE-OFF` (mono, small caps), center/right = nav tabs (Verdict · 3D · Latency · RAGAS · Judge) that scroll/route to sections, plus status pills (`models · 7`, `judged · 100`, `ragas · pending|done`).
- Cards: thin border, 8px radius, dense padding, mono section titles in dim caps + a one-line sub.
- Badges: green `promoted/pick`, blue `OSS`, gray `cohere`. CI/±shown in dim mono.
- Expandable sections (▸/▾) for reasoning/answers/diffs, like the reference console.
- Use `colorFor(model)` from types for every model hue, consistently across panels.

## Sections / components (one file each under src/components/)
1. **VerdictHero** — the headline. Recommendation banner: "Ettin-1b (open, 1B) ties Cohere v4-Pro on judge win-rate and is the fastest" (read from judge.model_score + latency). Show the ranked judge-win-rate bars (accuracy) + p50 latency bars side by side.
2. **ThreeDView** — echarts-gl `scatter3D`. Axes: X = latency (ms), Y = judge win-rate (quality, per model), Z = per-query confidence (norm top − 2nd). Each model = a cloud of its per-query points (color = model) + a larger centroid marker. Orbit/auto-rotate; legend toggles models; the ideal corner (high-Y, low-X) annotated. Clip per-query latency display at ~1500ms (cold-start outliers) with a note.
3. **Leaderboard** — sortable table: model · params · license · deploy · judge win-rate · agree-vs-3.5 · p50/p99 · qps · max ctx · RAGAS overall (when present). Recommended OSS row lit green; OSS/cohere badges.
4. **Distributions** — two echarts boxplots: per-query LATENCY (log y) and per-query confidence (top−2nd raw, note scales differ across families).
5. **HeadToHead** — echarts heatmap of judge winrate_matrix (row beats column %).
6. **DrillDown** — Opus-4.8 rationale cards from judge.verdicts; filter by model pair; winner side green; expandable rationale + the two top doc titles (look up via pools.json).
7. **RagasPanel** — when ragas_results.json present: per-reranker radar or grouped bars over the 5 metrics + ragas_overall; degrade to "running…" if absent.

## Data facts (already final, 42-query judge eval)
judge.model_score: cohere-v4-pro 0.674, **ettin-1b 0.667**, cohere-v4-fast 0.643, qwen3-4b 0.548, nemotron-1b-v2 0.419, cohere-3.5 0.286, qwen3-0.6b 0.271.
GPU p50 latency: ettin 204, nemotron 206, qwen3-0.6b 399, qwen3-4b 525 ms. Cohere via API p50: 3.5 368, v4-fast 364, v4-pro 1362 ms.
Headline: **Ettin-1b (open-source, 1B, Apache-2.0) ≈ Cohere v4-Pro on quality, fastest in field, beats v4-fast and crushes 3.5.**

## Rules
- Every panel degrades gracefully if its JSON is missing (RAGAS may be mid-run).
- Strict TS (the scaffold has strict mode). No `any` in component props; use types.ts.
- Keep it a single SPA page with anchored sections (no router needed) unless trivial.
- `bun run build` MUST pass (tsc + vite). Validate before claiming done.
