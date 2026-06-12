/**
 * Property 9: retrieval and ragas metrics are never conflated — AND
 * Property 4 (2D half): rendered-point ↔ record bijection.
 *
 * Property 9 — `buildRetrievalVsRagas2DOption` emits the two metric families as
 * DISTINCT, separately labeled series. Every series draws from exactly one metric
 * of exactly one family: a ragas-family series' `metricName` is a key of some
 * instance's `ragas` map, a retrieval-family series' `metricName` is a key of
 * some instance's `retrieval` map, and the two name sets are disjoint. No series
 * value is ever summed across the two families (each series value is a
 * within-metric mean, bounded by [0,1]).
 *
 * Property 4 (2D half) — `buildSpeedQuality2DOption` emits exactly one scatter
 * point per plottable instance (non-null composite quality), each carrying its
 * `instance_id`; the rendered-point multiset equals the plottable record set.
 *
 * Validates: Requirements 2.4, 2.6, 11.4, 8.4
 */
import { describe, it, expect } from "vitest";
import fc from "fast-check";
import {
  buildRetrievalVsRagas2DOption,
  buildSpeedQuality2DOption,
  type FamilyTaggedSeries,
} from "../charts2d";
import {
  deriveChartView,
  DEFAULT_ENABLED_METRICS,
  type EvalSelection,
} from "../evalSelectors";
import { DEFAULT_AXIS_MAPPING } from "../axisMapping";
import { DEFAULT_WEIGHT_SET } from "../evalQuality";
import type { EvalInstance, MetricValue } from "../../api/types";

const RAGAS_POOL = [
  "faithfulness",
  "answer_relevancy",
  "context_precision",
  "context_recall",
] as const;
const RETRIEVAL_POOL = ["precision_at_k", "recall_at_k", "ndcg_at_k"] as const;

const metricValueArb: fc.Arbitrary<MetricValue> = fc.oneof(
  fc.double({ min: 0, max: 1, noNaN: true }).map((value) => ({ value, unavailable: false })),
  fc.constant({ value: null, unavailable: true } as MetricValue),
);

const ragasMapArb: fc.Arbitrary<Record<string, MetricValue>> = fc
  .subarray([...RAGAS_POOL], { minLength: 0 })
  .chain((names) => fc.record(Object.fromEntries(names.map((n) => [n, metricValueArb]))));

const retrievalMapArb: fc.Arbitrary<Record<string, MetricValue>> = fc
  .subarray([...RETRIEVAL_POOL], { minLength: 0 })
  .chain((names) => fc.record(Object.fromEntries(names.map((n) => [n, metricValueArb]))));

const seedArb = fc.record({
  agent_id: fc.constantFrom("A", "B", "C", "D"),
  session_id: fc.constantFrom("s1", "s2", "s3"),
  instance_index: fc.nat({ max: 50 }),
  latency_ms: fc.oneof(
    fc.double({ min: 1, max: 10000, noNaN: true }),
    fc.constantFrom(0, -5, Number.POSITIVE_INFINITY),
  ),
  corpus_size: fc.nat({ max: 1000 }),
  ragas: ragasMapArb,
  retrieval: retrievalMapArb,
});

const recordSetArb: fc.Arbitrary<readonly EvalInstance[]> = fc
  .array(seedArb, { maxLength: 30 })
  .map((seeds) =>
    seeds.map((s, i) => ({
      instance_id: `inst-${i}`,
      agent_id: s.agent_id,
      session_id: s.session_id,
      instance_index: s.instance_index,
      timestamp: "2025-01-01T00:00:00Z",
      latency_ms: s.latency_ms,
      stage_timings: { retrieval_ms: null, generation_ms: null },
      corpus_size: s.corpus_size,
      retrieval_cached: false,
      ragas: s.ragas,
      retrieval: s.retrieval,
      confidence: null,
      volume: null,
      cost: null,
      prompt_id: null,
      category: null,
      status: "ok" as const,
      error: null,
    })),
  );

const selectionArb: fc.Arbitrary<EvalSelection> = fc.record({
  agentIds: fc.subarray(["A", "B", "C", "D"], { minLength: 0 }),
  sessionIds: fc.constant("all" as const),
  enabledMetrics: fc.constant([...DEFAULT_ENABLED_METRICS]),
  weightSet: fc.constant(DEFAULT_WEIGHT_SET),
  promptFilter: fc.constant(null),
  categoryFilter: fc.constant(null),
  smoothingWindow: fc.integer({ min: 1, max: 3 }),
  axes: fc.constant(DEFAULT_AXIS_MAPPING),
});

describe("Property 9: retrieval and ragas metrics never conflated (Req 2.4, 2.6, 11.4)", () => {
  it("every series is single-family, single-metric, drawn from the matching map; families disjoint", () => {
    fc.assert(
      fc.property(recordSetArb, selectionArb, (R, selection) => {
        const view = deriveChartView(R, selection);
        const option = buildRetrievalVsRagas2DOption(view) as { series?: FamilyTaggedSeries[] };
        const series = option.series ?? [];

        // The legal key sets observed across the plotted instances.
        const ragasKeys = new Set<string>();
        const retrievalKeys = new Set<string>();
        for (const inst of view.instances) {
          for (const k of Object.keys(inst.ragas)) ragasKeys.add(k);
          for (const k of Object.keys(inst.retrieval)) retrievalKeys.add(k);
        }

        const ragasUsed = new Set<string>();
        const retrievalUsed = new Set<string>();
        for (const s of series) {
          expect(s.metricFamily === "ragas" || s.metricFamily === "retrieval").toBe(true);
          if (s.metricFamily === "ragas") {
            expect(ragasKeys.has(s.metricName)).toBe(true);
            ragasUsed.add(s.metricName);
          } else {
            expect(retrievalKeys.has(s.metricName)).toBe(true);
            retrievalUsed.add(s.metricName);
          }
          // Every value is a within-metric aggregate bounded to [0,1] — never a
          // cross-family sum (which could exceed 1).
          for (const [, v] of s.data) {
            expect(v).toBeGreaterThanOrEqual(0);
            expect(v).toBeLessThanOrEqual(1);
          }
        }
        // The two families never share a metric name (no conflation).
        for (const n of ragasUsed) expect(retrievalUsed.has(n)).toBe(false);
      }),
    );
  });
});

/** Pull instance_ids out of every scatter series' data. */
function idsInSeries(option: { series?: unknown }): string[] {
  const series = (option.series ?? []) as Array<{ data?: unknown }>;
  const ids: string[] = [];
  for (const s of series) {
    for (const d of (s.data ?? []) as Array<{ instance_id?: string }>) {
      if (d && typeof d.instance_id === "string") ids.push(d.instance_id);
    }
  }
  return ids;
}

describe("Property 4 (2D half): rendered-point ↔ record bijection (Req 8.4)", () => {
  it("speed×quality scatter point ids equal the plottable (non-null quality) instance ids", () => {
    fc.assert(
      fc.property(recordSetArb, selectionArb, (R, selection) => {
        const view = deriveChartView(R, selection);
        const expected = view.instances
          .filter((i) => (view.qualityByInstanceId.get(i.instance_id) ?? null) != null)
          .map((i) => i.instance_id)
          .sort();
        const got = idsInSeries(buildSpeedQuality2DOption(view) as { series?: unknown }).sort();
        expect(got).toEqual(expected);
      }),
    );
  });
});
