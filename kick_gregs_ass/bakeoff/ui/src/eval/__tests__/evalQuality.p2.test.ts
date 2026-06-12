/**
 * Property 2: Composite monotonic in each non-negative-weighted metric.
 *
 * Raising one available component's value while holding all others fixed never
 * decreases the Quality_Score (weights are non-negative).
 *
 * Validates: Requirements 3.1
 */
import { describe, it, expect } from "vitest";
import fc from "fast-check";
import { compositeQuality, type CompositeWeightSet } from "../evalQuality";
import type { EvalInstance, MetricValue } from "../../api/types";

const METRIC_POOL = [
  "faithfulness",
  "answer_relevancy",
  "context_precision",
  "context_recall",
] as const;

function makeInstance(values: Record<string, number>): EvalInstance {
  const ragas: Record<string, MetricValue> = {};
  for (const [name, value] of Object.entries(values)) {
    ragas[name] = { value, unavailable: false };
  }
  return {
    instance_id: "i1",
    agent_id: "A",
    session_id: "s1",
    instance_index: 0,
    timestamp: "2025-01-01T00:00:00Z",
    latency_ms: 100,
    stage_timings: { retrieval_ms: null, generation_ms: null },
    corpus_size: 10,
    retrieval_cached: false,
    ragas,
    retrieval: {},
    confidence: null,
    volume: null,
    cost: null,
    prompt_id: null,
    category: null,
    status: "ok",
    error: null,
  };
}

describe("Property 2: composite monotonic in each non-negative-weighted metric (Req 3.1)", () => {
  it("raising one available component (others fixed) never lowers the score", () => {
    fc.assert(
      fc.property(
        // values for a fixed set of present metrics
        fc.record({
          faithfulness: fc.double({ min: 0, max: 1, noNaN: true }),
          answer_relevancy: fc.double({ min: 0, max: 1, noNaN: true }),
          context_precision: fc.double({ min: 0, max: 1, noNaN: true }),
          context_recall: fc.double({ min: 0, max: 1, noNaN: true }),
        }),
        // strictly positive weights so each component is used
        fc.record({
          faithfulness: fc.double({ min: 0.01, max: 1, noNaN: true }),
          answer_relevancy: fc.double({ min: 0.01, max: 1, noNaN: true }),
          context_precision: fc.double({ min: 0.01, max: 1, noNaN: true }),
          context_recall: fc.double({ min: 0.01, max: 1, noNaN: true }),
        }),
        // which component to raise, and by how much
        fc.constantFrom(...METRIC_POOL),
        fc.double({ min: 0, max: 1, noNaN: true }),
        (values, weights, target, bump) => {
          const enabled = [...METRIC_POOL];
          const ws: CompositeWeightSet = { id: "w", weights };

          const base = compositeQuality(makeInstance(values), ws, enabled);

          const raised = { ...values, [target]: Math.min(1, values[target] + bump) };
          const after = compositeQuality(makeInstance(raised), ws, enabled);

          expect(base.score).not.toBeNull();
          expect(after.score).not.toBeNull();
          // allow a tiny epsilon for floating-point noise
          expect(after.score!).toBeGreaterThanOrEqual(base.score! - 1e-9);
        },
      ),
    );
  });
});
