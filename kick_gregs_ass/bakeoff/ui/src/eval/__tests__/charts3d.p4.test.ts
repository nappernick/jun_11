/**
 * Property 4 (3D half): rendered-point ↔ record bijection.
 *
 * For the point-bearing 3D builders (scatter, bubble, trajectory) the multiset of
 * `instance_id`s carried in the series `data` is EXACTLY the multiset of the
 * plottable instances' `instance_id`s (the ids of `ChartView.instances`). No
 * phantom point exists without a backing record, and no plottable record is
 * dropped. Additionally, each trajectory series' points are emitted in ascending
 * `instance_index` order (Req 10.2).
 *
 * Validates: Requirements 10.2, 10.3, 8.4
 */
import { describe, it, expect } from "vitest";
import fc from "fast-check";
import {
  buildScatter3DOption,
  buildBubble3DOption,
  buildTrajectory3DOption,
} from "../charts3d";
import {
  deriveChartView,
  DEFAULT_ENABLED_METRICS,
  type EvalSelection,
} from "../evalSelectors";
import { DEFAULT_AXIS_MAPPING } from "../axisMapping";
import { DEFAULT_WEIGHT_SET } from "../evalQuality";
import type { EvalInstance, MetricValue } from "../../api/types";

const METRIC_POOL = [
  "faithfulness",
  "answer_relevancy",
  "context_precision",
  "context_recall",
] as const;

const metricValueArb: fc.Arbitrary<MetricValue> = fc.oneof(
  fc.double({ min: 0, max: 1, noNaN: true }).map((value) => ({ value, unavailable: false })),
  fc.constant({ value: null, unavailable: true } as MetricValue),
);

const ragasMapArb: fc.Arbitrary<Record<string, MetricValue>> = fc
  .subarray([...METRIC_POOL], { minLength: 0 })
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
  confidence: fc.option(fc.double({ min: 0, max: 1, noNaN: true }), { nil: null }),
  volume: fc.option(fc.double({ min: 0, max: 5000, noNaN: true }), { nil: null }),
  cost: fc.option(fc.double({ min: 0, max: 10, noNaN: true }), { nil: null }),
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
      retrieval: {},
      confidence: s.confidence,
      volume: s.volume,
      cost: s.cost,
      prompt_id: null,
      category: null,
      status: "ok" as const,
      error: null,
    })),
  );

const selectionArb: fc.Arbitrary<EvalSelection> = fc.record({
  agentIds: fc.subarray(["A", "B", "C", "D"], { minLength: 0 }),
  sessionIds: fc.oneof(
    fc.constant("all" as const),
    fc.subarray(["s1", "s2", "s3"], { minLength: 0 }),
  ),
  enabledMetrics: fc.constant([...DEFAULT_ENABLED_METRICS]),
  weightSet: fc.constant(DEFAULT_WEIGHT_SET),
  promptFilter: fc.constant(null),
  categoryFilter: fc.constant(null),
  smoothingWindow: fc.integer({ min: 1, max: 4 }),
  axes: fc.constant(DEFAULT_AXIS_MAPPING),
});

/** Pull instance_ids out of every series' data (point-bearing builders only). */
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

describe("Property 4 (3D half): rendered-point ↔ record bijection", () => {
  it("scatter3D point ids are exactly the plottable instance ids (Req 10.3, 8.4)", () => {
    fc.assert(
      fc.property(recordSetArb, selectionArb, (R, selection) => {
        const view = deriveChartView(R, selection);
        const expected = view.instances.map((i) => i.instance_id).sort();
        const got = idsInSeries(buildScatter3DOption(view) as { series?: unknown }).sort();
        expect(got).toEqual(expected);
      }),
    );
  });

  it("bubble3D point ids are exactly the plottable instance ids (Req 10.3, 8.4)", () => {
    fc.assert(
      fc.property(
        recordSetArb,
        selectionArb,
        fc.constantFrom("confidence", "volume", "cost") as fc.Arbitrary<
          "confidence" | "volume" | "cost"
        >,
        (R, selection, sizeBy) => {
          const view = deriveChartView(R, selection);
          const expected = view.instances.map((i) => i.instance_id).sort();
          const got = idsInSeries(
            buildBubble3DOption(view, sizeBy) as { series?: unknown },
          ).sort();
          expect(got).toEqual(expected);
        },
      ),
    );
  });

  it("trajectory ids union to the plottable set and each path is instance_index-ordered (Req 10.2)", () => {
    fc.assert(
      fc.property(recordSetArb, selectionArb, (R, selection) => {
        const view = deriveChartView(R, selection);
        const option = buildTrajectory3DOption(view) as {
          series?: Array<{ data?: Array<{ instance_id?: string; index?: number }> }>;
        };

        // Bijection across the union of per-agent paths.
        const expected = view.instances.map((i) => i.instance_id).sort();
        const got = idsInSeries(option as { series?: unknown }).sort();
        expect(got).toEqual(expected);

        // Each path is in non-decreasing instance_index order.
        for (const s of option.series ?? []) {
          const idxs = (s.data ?? []).map((d) => d.index ?? Number.NaN);
          for (let i = 1; i < idxs.length; i++) {
            expect(idxs[i]! >= idxs[i - 1]!).toBe(true);
          }
        }
      }),
    );
  });
});
