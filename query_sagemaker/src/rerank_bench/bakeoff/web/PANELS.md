# Panel Agent Spec

This document defines the exact prop interfaces, shared selectors, and theme API for implementing the six dashboard panels.

## Project Layout

```
web/
├── public/results.json        # Dev fixture (copy of sample_results.json)
├── src/
│   ├── types.ts               # Contract types (DO NOT MODIFY)
│   ├── theme.css              # Dark analytics design system
│   ├── lib/
│   │   ├── selectors.ts       # Pure helpers
│   │   └── useResults.ts      # Data-fetching hook
│   ├── panels/
│   │   ├── CostQuality.tsx
│   │   ├── AbstentionSweep.tsx
│   │   ├── Leaderboard.tsx
│   │   ├── NSweep.tsx
│   │   ├── LatencyBudget.tsx
│   │   └── DrillDown.tsx
│   ├── App.tsx                # Orchestrator
│   └── main.tsx               # Entry
```

## Charting Library

**recharts** (v3) is installed. Import from `'recharts'`.

## Panel Props (exact signatures — keep these, replace the stub body)

### CostQuality
```tsx
import type { SliceMetrics, Gates, Cell, ModelMeta } from '../types';
export interface CostQualityProps {
  sliceMetrics: { modelId: string; metrics: SliceMetrics; cell: Cell }[];
  gates: Gates;
  recommendedId: string;
  models: ModelMeta[];
}
```
**Purpose:** Scatter plot of cost_per_1k (x) vs ndcg10 (y). Draw the Pareto frontier line. Highlight recommended model. Show accuracy_bar gate as horizontal line. Use `paretoFrontier()` from selectors.

### AbstentionSweep
```tsx
import type { SliceMetrics, Gates, ModelMeta } from '../types';
export interface AbstentionSweepProps {
  sliceMetrics: { modelId: string; metrics: SliceMetrics }[];
  gates: Gates;
  models: ModelMeta[];
}
```
**Purpose:** Line chart of abstain_curve per model: x=t (threshold), y=false_answer_rate (PRIMARY, scary metric — use red), secondary y or separate line for abstain_recall. Draw horizontal line at gates.false_answer_ceiling. Visually distinguish `calibrated_scores` models (solid) vs raw-logit (dashed). Mark operating_t for each model.

### Leaderboard
```tsx
import type { SliceMetrics, Gates, ModelMeta } from '../types';
export interface LeaderboardProps {
  sliceMetrics: { modelId: string; metrics: SliceMetrics }[];
  gates: Gates;
  recommendedId: string;
  models: ModelMeta[];
  baselineId: string;
}
```
**Purpose:** Dense table sorted by ndcg10 desc. Columns: rank, model name, nDCG@10 (with CI), recall@10, MRR@10, p99, FAR, cost, gate pass/fail. Highlight recommended row with `.recommended` class. Show sig_vs_baseline indicator.

### NSweep
```tsx
import type { Cell, ModelMeta } from '../types';
export interface NSweepProps {
  cells: Cell[];
  selectedSlice: string;
  models: ModelMeta[];
}
```
**Purpose:** Line chart showing ndcg10 as a function of N (number of passages reranked) per model for the active slice. Group cells by model_id, x=N, y=ndcg10.

### LatencyBudget
```tsx
import type { SliceMetrics, Gates, ModelMeta } from '../types';
export interface LatencyBudgetProps {
  sliceMetrics: { modelId: string; metrics: SliceMetrics }[];
  gates: Gates;
  models: ModelMeta[];
}
```
**Purpose:** Bar/dot chart showing p50, p95, p99 per model. Draw vertical line at gates.latency_budget_ms. Color bars red if p99 exceeds budget.

### DrillDown
```tsx
import type { ResultsFile } from '../types';
export interface DrillDownProps {
  data: ResultsFile;
  selectedSlice: string;
}
```
**Purpose:** Per-query table or distribution view. Show Row-level data for the selected slice — rels, top_norm score, latency. Allow sorting/filtering. Show query-level nDCG distribution.

---

## Shared Selectors (`src/lib/selectors.ts`)

```ts
sliceDimensions(keys: string[]): Record<string, string[]>
gatePass(metrics: SliceMetrics, gates: Gates): boolean
recommendedModelId(sliceMetrics: {modelId:string; metrics:SliceMetrics}[], gates: Gates): string
paretoFrontier<T extends {cost_per_1k:number; ndcg10:number}>(pts: T[]): T[]
buildSliceKey(selections: Record<string,string>): string
```

## Theme CSS Variables

| Variable | Usage |
|----------|-------|
| `--bg-0` | Page background `#0d1117` |
| `--bg-1` | Header/slicer/hover `#161b22` |
| `--bg-2` | Controls/inputs `#1c2128` |
| `--bg-card` | Panel cards `#1c2128` |
| `--border` | Borders `#30363d` |
| `--grid` | Table grid lines `#21262d` |
| `--text` | Primary text `#e6edf3` |
| `--text-muted` | Secondary text `#8b949e` |
| `--accent` | Links, highlights `#58a6ff` |
| `--accent-dim` | Active bg tint `#1f6feb33` |
| `--green` | Pass/good `#3fb950` |
| `--green-dim` | Recommended row bg `#23863633` |
| `--red` | Fail/danger/FAR `#f85149` |
| `--red-dim` | Danger zone bg `#da363333` |
| `--yellow` | Warning `#d29922` |
| `--purple` | Calibrated models `#bc8cff` |
| `--font-mono` | Data, tables |
| `--font-sans` | UI text |
| `--radius` | Border radius `6px` |

## Theme CSS Classes

| Class | Usage |
|-------|-------|
| `.panel` | Outer panel card container |
| `.panel-title` | Panel heading bar |
| `.pass` | Green text for gate-passing values |
| `.fail` | Red text for gate-failing values |
| `.recommended` | Highlighted row (green left border + dim bg) |
| `.danger-zone` | Red dim background |
| `.seg` | Segmented control container |
| `.seg button` | Segment button |
| `.seg button.active` | Active segment |

## Methodology Reminders for Panel Authors

- Relevance is BINARY (rels are 0/1)
- nDCG is computed over answerable queries only
- Recall is conditional on retrieval
- Abstention is a CO-EQUAL hero metric (threshold sweep over abstain_curve)
- **false_answer_rate** is the scary metric — always use `--red`
- Calibrated-score models (`model.calibrated_scores === true`) should be visually distinct from raw-logit models on abstention curves (e.g., solid vs dashed, or use `--purple` for calibrated)
- Gate pass: ndcg10 >= accuracy_bar AND p99 <= latency_budget_ms AND abstain.false_answer_rate <= false_answer_ceiling
- Recommended model = highest-ndcg model passing all 3 gates

## How to Find Model Metadata

```ts
const model = props.models.find(m => m.id === modelId);
model.display_name  // human-readable name for labels
model.calibrated_scores  // distinguish on abstention chart
model.instruction_following  // may want to annotate
```

## Build Verification

```bash
cd web && bunx tsc -b && bun run build
```

Both must pass green after your changes.
