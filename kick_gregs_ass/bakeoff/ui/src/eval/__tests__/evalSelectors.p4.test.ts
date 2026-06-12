/**
 * Property 4 (derivation half): no in-view record is silently dropped.
 *
 * For a given (instances, selection), every distinct input record is accounted
 * for in EXACTLY ONE bucket of the derived ChartView's accounting — plotted,
 * filtered out by the selection, or non-plottable (missing a value for a bound
 * axis). The three buckets are pairwise disjoint, their union is exactly the
 * deduped set of input instance_ids, and `accounting.plotted` is exactly the id
 * set of `ChartView.instances`. A point never exists without a backing record,
 * and a record is never dropped without being counted.
 *
 * Validates: Requirements 8.4
 */
import { describe, it, expect } from "vitest";
import fc from "fast-check";
import {
  deriveChartView,
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
  // include 0 / negative / huge latency so some instances exercise the log floor
  latency_ms: fc.oneof(
    fc.double({ min: 1, max: 10000, noNaN: true }),
    fc.constantFrom(0, -5, Number.POSITIVE_INFINITY),
  ),
  corpus_size: fc.nat({ max: 1000 }),
  ragas: ragasMapArb,
  prompt_id: fc.option(fc.constantFrom("p1", "p2"), { nil: null }),
  category: fc.option(fc.constantFrom("c1", "c2"), { nil: null }),
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
      confidence: null,
      volume: null,
      cost: null,
      prompt_id: s.prompt_id,
      category: s.category,
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
  enabledMetrics: fc.constant([...METRIC_POOL]),
  weightSet: fc.constant(DEFAULT_WEIGHT_SET),
  promptFilter: fc.option(fc.constantFrom("p1", "p2"), { nil: null }),
  categoryFilter: fc.option(fc.constantFrom("c1", "c2"), { nil: null }),
  smoothingWindow: fc.integer({ min: 1, max: 5 }),
  axes: fc.constant(DEFAULT_AXIS_MAPPING),
});

describe("Property 4 (derivation half): no in-view record silently dropped (Req 8.4)", () => {
  it("every distinct input id is accounted for in exactly one bucket; union === input ids", () => {
    fc.assert(
      fc.property(recordSetArb, selectionArb, (R, selection) => {
        const view = deriveChartView(R, selection);
        const { plotted, filteredOut, nonPlottable, total } = view.accounting;

        const inputIds = new Set(R.map((i) => i.instance_id)); // unique by construction
        expect(total).toBe(inputIds.size);

        const buckets = [...plotted, ...filteredOut, ...nonPlottable];
        // pairwise disjoint: no id appears in more than one bucket
        expect(new Set(buckets).size).toBe(buckets.length);
        // union equals exactly the input id set (nothing added, nothing dropped)
        expect(new Set(buckets)).toEqual(inputIds);
      }),
    );
  });

  it("accounting.plotted is exactly the id set of ChartView.instances", () => {
    fc.assert(
      fc.property(recordSetArb, selectionArb, (R, selection) => {
        const view = deriveChartView(R, selection);
        const plottedFromInstances = view.instances.map((i) => i.instance_id);
        expect([...view.accounting.plotted].sort()).toEqual([...plottedFromInstances].sort());
      }),
    );
  });

  it("every plotted instance passed the selection filters (no phantom points)", () => {
    fc.assert(
      fc.property(recordSetArb, selectionArb, (R, selection) => {
        const view = deriveChartView(R, selection);
        const agentFilter = new Set(selection.agentIds);
        const sessionFilter =
          selection.sessionIds === "all" ? null : new Set(selection.sessionIds);
        for (const inst of view.instances) {
          if (agentFilter.size > 0) expect(agentFilter.has(inst.agent_id)).toBe(true);
          if (sessionFilter) expect(sessionFilter.has(inst.session_id)).toBe(true);
          if (selection.promptFilter != null)
            expect(inst.prompt_id).toBe(selection.promptFilter);
          if (selection.categoryFilter != null)
            expect(inst.category).toBe(selection.categoryFilter);
        }
      }),
    );
  });
});
