/**
 * Property 1: Composite determinism + weights sum to 1.0.
 *
 * For all instances i and weight sets w: compositeQuality(i, w, e) is pure and
 * deterministic (equal inputs ⟹ equal output), and the effective weights it
 * applies over the present components sum to exactly 1.0 (after normalization).
 * A weight set whose positive weights over present components sum to ≤ 0 is
 * rejected (score === null), never silently uniform.
 *
 * Validates: Requirements 3.1, 3.2, 3.4, 3.7
 */
import { describe, it, expect } from "vitest";
import fc from "fast-check";
import {
  compositeQuality,
  normalizeWeights,
  DEFAULT_WEIGHT_SET,
  type CompositeWeightSet,
} from "../evalQuality";
import type { EvalInstance, MetricValue } from "../../api/types";

const METRIC_POOL = [
  "faithfulness",
  "answer_relevancy",
  "context_precision",
  "context_recall",
  "context_entities_recall",
  "noise_sensitivity",
] as const;

const metricValueArb: fc.Arbitrary<MetricValue> = fc.oneof(
  fc
    .double({ min: 0, max: 1, noNaN: true })
    .map((value) => ({ value, unavailable: false }) as MetricValue),
  fc.constant({ value: null, unavailable: true } as MetricValue),
);

const ragasMapArb: fc.Arbitrary<Record<string, MetricValue>> = fc
  .subarray([...METRIC_POOL], { minLength: 0 })
  .chain((names) =>
    fc.record(Object.fromEntries(names.map((n) => [n, metricValueArb]))),
  );

const instanceArb: fc.Arbitrary<EvalInstance> = fc
  .record({
    instance_id: fc.uuid(),
    agent_id: fc.constantFrom("A", "B", "C", "D"),
    session_id: fc.constantFrom("s1", "s2"),
    instance_index: fc.nat({ max: 100 }),
    latency_ms: fc.double({ min: 1, max: 10000, noNaN: true }),
    corpus_size: fc.nat({ max: 1000 }),
    ragas: ragasMapArb,
  })
  .map((r) => ({
    instance_id: r.instance_id,
    agent_id: r.agent_id,
    session_id: r.session_id,
    instance_index: r.instance_index,
    timestamp: "2025-01-01T00:00:00Z",
    latency_ms: r.latency_ms,
    stage_timings: { retrieval_ms: null, generation_ms: null },
    corpus_size: r.corpus_size,
    retrieval_cached: false,
    ragas: r.ragas,
    retrieval: {},
    confidence: null,
    volume: null,
    cost: null,
    prompt_id: null,
    category: null,
    status: "ok" as const,
    error: null,
  }));

const weightSetArb: fc.Arbitrary<CompositeWeightSet> = fc
  .dictionary(
    fc.constantFrom(...METRIC_POOL, "others"),
    fc.double({ min: 0, max: 1, noNaN: true }),
  )
  .map((weights) => ({ id: "arb", weights }));

const enabledArb = fc.subarray([...METRIC_POOL], { minLength: 0 });

describe("Property 1: composite determinism + weights sum to 1.0 (Req 3.1, 3.2, 3.4, 3.7)", () => {
  it("is pure/deterministic: identical (instance, weightSet, enabled) ⟹ identical result", () => {
    fc.assert(
      fc.property(instanceArb, weightSetArb, enabledArb, (inst, ws, enabled) => {
        const a = compositeQuality(inst, ws, enabled);
        const b = compositeQuality(inst, ws, enabled);
        expect(a).toEqual(b);
      }),
    );
  });

  it("normalized effective weights over a non-empty present set sum to 1.0 (±epsilon)", () => {
    fc.assert(
      fc.property(
        fc
          .array(
            fc.tuple(
              fc.constantFrom(...METRIC_POOL),
              fc.double({ min: 0.001, max: 1, noNaN: true }),
            ),
            { minLength: 1, maxLength: 6 },
          )
          .map((pairs) => Object.fromEntries(pairs)),
        (weights) => {
          const present = Object.keys(weights);
          const norm = normalizeWeights(weights, present);
          expect(norm).not.toBeNull();
          const sum = Object.values(norm!).reduce((a, b) => a + b, 0);
          expect(sum).toBeCloseTo(1.0, 9);
        },
      ),
    );
  });

  it("a weight set whose positive present weights sum to ≤ 0 ⟹ score === null", () => {
    fc.assert(
      fc.property(
        instanceArb,
        fc.array(fc.constantFrom(...METRIC_POOL), { minLength: 1 }),
        (inst, enabledRaw) => {
          const enabled = [...new Set(enabledRaw)];
          // All enabled components get a non-positive weight; no positive "others".
          const weights: Record<string, number> = { others: 0 };
          for (const c of enabled) weights[c] = 0;
          const result = compositeQuality(inst, { id: "zero", weights }, enabled);
          expect(result.score).toBeNull();
        },
      ),
    );
  });

  it("the documented default weight set's raw weights sum to 1.0 (Req 3.7)", () => {
    const sum = Object.values(DEFAULT_WEIGHT_SET.weights).reduce((a, b) => a + b, 0);
    expect(sum).toBeCloseTo(1.0, 9);
  });
});
