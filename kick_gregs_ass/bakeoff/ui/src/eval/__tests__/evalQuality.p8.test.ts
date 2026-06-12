/**
 * Property 8: Recorded metric values are never altered by weight changes.
 *
 * Recomputing the composite with a different weight set leaves
 * EvalInstance.ragas / EvalInstance.retrieval byte-for-byte unchanged. The
 * composite reads recorded values and re-derives a score; it never writes back.
 *
 * Validates: Requirements 3.3, 12.7
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
const RETRIEVAL_POOL = ["precision_at_k", "recall_at_k", "ndcg_at_k"] as const;

const metricValueArb: fc.Arbitrary<MetricValue> = fc.oneof(
  fc
    .double({ min: 0, max: 1, noNaN: true })
    .map((value) => ({ value, unavailable: false }) as MetricValue),
  fc.constant({ value: null, unavailable: true } as MetricValue),
);

function mapArb(pool: readonly string[]): fc.Arbitrary<Record<string, MetricValue>> {
  return fc
    .subarray([...pool], { minLength: 0 })
    .chain((names) => fc.record(Object.fromEntries(names.map((n) => [n, metricValueArb]))));
}

const weightSetArb: fc.Arbitrary<CompositeWeightSet> = fc
  .dictionary(
    fc.constantFrom(...METRIC_POOL, ...RETRIEVAL_POOL, "others"),
    fc.double({ min: 0, max: 1, noNaN: true }),
  )
  .map((weights) => ({ id: "w", weights }));

describe("Property 8: recorded metric values never altered by weight changes (Req 3.3, 12.7)", () => {
  it("recomputing with a different weight set leaves ragas/retrieval unchanged", () => {
    fc.assert(
      fc.property(
        mapArb(METRIC_POOL),
        mapArb(RETRIEVAL_POOL),
        weightSetArb,
        weightSetArb,
        (ragas, retrieval, wsA, wsB) => {
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
            ragas,
            retrieval,
            confidence: null,
            volume: null,
            cost: null,
            prompt_id: null,
            category: null,
            status: "ok",
            error: null,
          };
          // Independent deep snapshots taken BEFORE any computation.
          const ragasSnapshot = structuredClone(ragas);
          const retrievalSnapshot = structuredClone(retrieval);

          const enabled = [...METRIC_POOL, ...RETRIEVAL_POOL];
          compositeQuality(inst, wsA, enabled);
          compositeQuality(inst, wsB, enabled);

          expect(inst.ragas).toEqual(ragasSnapshot);
          expect(inst.retrieval).toEqual(retrievalSnapshot);
        },
      ),
    );
  });
});
