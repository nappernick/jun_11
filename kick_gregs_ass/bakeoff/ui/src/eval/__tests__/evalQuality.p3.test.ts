/**
 * Property 3: Every consumed metric value is clamped/validated to [0,1].
 *
 * After ingestion + clampUnit, every consumed ragas/retrieval metric value is in
 * [0,1]; non-finite or out-of-range inputs are coerced to the range, never
 * propagated. The composite score is likewise always in [0,1] (or null).
 *
 * Validates: Requirements 1.3, 2.1, 3.4
 */
import { describe, it, expect } from "vitest";
import fc from "fast-check";
import { clampUnit, compositeQuality, type CompositeWeightSet } from "../evalQuality";
import type { EvalInstance, MetricValue } from "../../api/types";

describe("Property 3: every consumed metric value clamped to [0,1] (Req 1.3, 2.1, 3.4)", () => {
  it("clampUnit coerces any real or non-finite input into [0,1]", () => {
    fc.assert(
      fc.property(
        fc.oneof(
          fc.double({ noNaN: false }),
          fc.constant(Number.NaN),
          fc.constant(Number.POSITIVE_INFINITY),
          fc.constant(Number.NEGATIVE_INFINITY),
        ),
        (x) => {
          const c = clampUnit(x);
          expect(Number.isFinite(c)).toBe(true);
          expect(c).toBeGreaterThanOrEqual(0);
          expect(c).toBeLessThanOrEqual(1);
        },
      ),
    );
  });

  it("non-finite or out-of-range raw inputs are coerced (never propagated)", () => {
    // non-finite inputs are treated as "no usable value" and coerced to 0
    expect(clampUnit(Number.NaN)).toBe(0);
    expect(clampUnit(Number.POSITIVE_INFINITY)).toBe(0);
    expect(clampUnit(Number.NEGATIVE_INFINITY)).toBe(0);
    expect(clampUnit(-5)).toBe(0);
    expect(clampUnit(42)).toBe(1);
    expect(clampUnit(0.42)).toBeCloseTo(0.42, 12);
  });

  it("composite score is always within [0,1] even with out-of-range recorded values", () => {
    const metricValueArb: fc.Arbitrary<MetricValue> = fc.oneof(
      // deliberately out-of-range / non-finite to prove they are coerced, never propagated
      fc.double({ noNaN: false }).map((value) => ({ value, unavailable: false }) as MetricValue),
      fc.constant({ value: null, unavailable: true } as MetricValue),
    );
    const names = ["faithfulness", "answer_relevancy", "context_precision"] as const;

    fc.assert(
      fc.property(
        fc.record(Object.fromEntries(names.map((n) => [n, metricValueArb]))),
        (ragas) => {
          const inst: EvalInstance = {
            instance_id: "i1",
            agent_id: "A",
            session_id: "s1",
            instance_index: 0,
            timestamp: "2025-01-01T00:00:00Z",
            latency_ms: 100,
            stage_timings: { retrieval_ms: null, generation_ms: null },
            corpus_size: 10,
            retrieval_cached: false,
            ragas: ragas as Record<string, MetricValue>,
            retrieval: {},
            confidence: null,
            volume: null,
            cost: null,
            prompt_id: null,
            category: null,
            status: "ok",
            error: null,
          };
          const ws: CompositeWeightSet = {
            id: "w",
            weights: { faithfulness: 0.5, answer_relevancy: 0.3, context_precision: 0.2 },
          };
          const result = compositeQuality(inst, ws, [...names]);
          if (result.score !== null) {
            expect(result.score).toBeGreaterThanOrEqual(0);
            expect(result.score).toBeLessThanOrEqual(1);
          }
        },
      ),
    );
  });
});
