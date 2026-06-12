/**
 * Eval export tests (Task 13.6; Req 17.*, 20.2).
 *
 * Asserts the export is sufficient to reproduce every exported Quality_Score and
 * carries its provenance:
 *
 *  - an exported Quality_Score recomputes EXACTLY from the exported component
 *    values + weights (Req 17.3) — checked both by example and as a property;
 *  - the exported component values are UNCHANGED from what was recorded (Req 17.3);
 *  - the ragas version and Bedrock model id are present (Req 17.4);
 *  - the external/industry-methodology caveat is present (Req 17.5 / 20.2).
 */
import { describe, it, expect } from "vitest";
import fc from "fast-check";
import { buildEvalExport } from "../evalExport";
import {
  compositeQuality,
  DEFAULT_WEIGHT_SET,
  type CompositeWeightSet,
} from "../evalQuality";
import { EXTERNAL_METHODOLOGY_CAVEAT } from "../methodology";
import type { EvalInstance, MetricValue } from "../../api/types";

const RAGAS_VERSION = "0.2.1";
const BEDROCK_ID = "us.anthropic.claude-opus-4";

function ragas(value: number | null, promptId = "faithfulness:default"): MetricValue {
  return value == null
    ? {
        value: null,
        unavailable: true,
        ragas_version: RAGAS_VERSION,
        bedrock_model_id: BEDROCK_ID,
        prompt_config_id: promptId,
      }
    : {
        value,
        unavailable: false,
        ragas_version: RAGAS_VERSION,
        bedrock_model_id: BEDROCK_ID,
        prompt_config_id: promptId,
      };
}

function makeInstance(
  id: string,
  ragasMap: Record<string, MetricValue>,
): EvalInstance {
  return {
    instance_id: id,
    agent_id: "agent-a",
    session_id: "s1",
    instance_index: 0,
    timestamp: "2025-01-01T00:00:00Z",
    latency_ms: 42,
    stage_timings: { retrieval_ms: 12, generation_ms: 30 },
    corpus_size: 1000,
    retrieval_cached: false,
    ragas: ragasMap,
    retrieval: { precision_at_k: { value: 0.5, unavailable: false, k: 5 } },
    confidence: null,
    volume: null,
    cost: null,
    prompt_id: null,
    category: null,
    status: "ok",
    error: null,
  };
}

const ENABLED = ["faithfulness", "answer_relevancy", "context_precision"];

describe("evalExport — reproducibility + provenance (Req 17.*, 20.2)", () => {
  const instances: EvalInstance[] = [
    makeInstance("i1", {
      faithfulness: ragas(0.9),
      answer_relevancy: ragas(0.8, "answer_relevancy:v1:abcd1234"),
      context_precision: ragas(0.7),
    }),
    makeInstance("i2", {
      faithfulness: ragas(0.6),
      answer_relevancy: ragas(null),
      context_precision: ragas(0.5),
    }),
  ];

  it("an exported Quality_Score recomputes exactly from exported components + weights", () => {
    const exp = buildEvalExport(instances, DEFAULT_WEIGHT_SET, ENABLED);
    for (const ei of exp.instances) {
      const original = instances.find((i) => i.instance_id === ei.instance_id)!;
      // Recompute from the EXPORTED components (ei.ragas/retrieval) + weights.
      const recomputed = compositeQuality(
        { ...original, ragas: ei.ragas, retrieval: ei.retrieval },
        { id: exp.weight_set.id, weights: exp.weight_set.weights },
        exp.enabled_components,
      );
      expect(recomputed.score).toEqual(ei.quality_score);
    }
  });

  it("exports the component values UNCHANGED from what was recorded", () => {
    const exp = buildEvalExport(instances, DEFAULT_WEIGHT_SET, ENABLED);
    for (const ei of exp.instances) {
      const original = instances.find((i) => i.instance_id === ei.instance_id)!;
      expect(ei.ragas).toEqual(original.ragas);
      expect(ei.retrieval).toEqual(original.retrieval);
    }
  });

  it("includes the ragas version and Bedrock model id (Req 17.4)", () => {
    const exp = buildEvalExport(instances, DEFAULT_WEIGHT_SET, ENABLED);
    expect(exp.ragas_version).toBe(RAGAS_VERSION);
    expect(exp.bedrock_model_id).toBe(BEDROCK_ID);
  });

  it("carries the external-methodology caveat (Req 17.5 / 20.2)", () => {
    const exp = buildEvalExport(instances, DEFAULT_WEIGHT_SET, ENABLED);
    expect(exp.methodology_caveat).toContain(EXTERNAL_METHODOLOGY_CAVEAT);
    expect(exp.methodology_notice.length).toBeGreaterThan(0);
  });

  it("carries the weight set id + weights and the union of prompt-config ids (Req 17.2)", () => {
    const exp = buildEvalExport(instances, DEFAULT_WEIGHT_SET, ENABLED);
    expect(exp.weight_set.id).toBe(DEFAULT_WEIGHT_SET.id);
    expect(exp.weight_set.weights).toEqual(DEFAULT_WEIGHT_SET.weights);
    expect(exp.prompt_config_ids).toContain("faithfulness:default");
    expect(exp.prompt_config_ids).toContain("answer_relevancy:v1:abcd1234");
  });

  it("includes per-instance identity, latency, and stage timings (Req 17.1)", () => {
    const exp = buildEvalExport(instances, DEFAULT_WEIGHT_SET, ENABLED);
    const ei = exp.instances[0]!;
    expect(ei.agent_id).toBe("agent-a");
    expect(ei.session_id).toBe("s1");
    expect(ei.instance_index).toBe(0);
    expect(ei.corpus_size).toBe(1000);
    expect(ei.latency_ms).toBe(42);
    expect(ei.stage_timings).toEqual({ retrieval_ms: 12, generation_ms: 30 });
  });

  // --- property: recompute holds for arbitrary instances + weight sets ---
  const metricValueArb: fc.Arbitrary<MetricValue> = fc.oneof(
    fc.double({ min: 0, max: 1, noNaN: true }).map((v) => ragas(v)),
    fc.constant(ragas(null)),
  );

  const instanceArb: fc.Arbitrary<EvalInstance> = fc
    .record({
      id: fc.uuid(),
      faithfulness: metricValueArb,
      answer_relevancy: metricValueArb,
      context_precision: metricValueArb,
    })
    .map((r) =>
      makeInstance(r.id, {
        faithfulness: r.faithfulness,
        answer_relevancy: r.answer_relevancy,
        context_precision: r.context_precision,
      }),
    );

  const weightSetArb: fc.Arbitrary<CompositeWeightSet> = fc
    .dictionary(
      fc.constantFrom(...ENABLED, "others"),
      fc.double({ min: 0, max: 1, noNaN: true }),
    )
    .map((weights) => ({ id: "arb", weights }));

  it("property: every exported score equals a fresh recompute from exported components", () => {
    fc.assert(
      fc.property(
        fc.array(instanceArb, { minLength: 1, maxLength: 8 }),
        weightSetArb,
        (insts, ws) => {
          const exp = buildEvalExport(insts, ws, ENABLED);
          for (const ei of exp.instances) {
            const original = insts.find((i) => i.instance_id === ei.instance_id)!;
            const recomputed = compositeQuality(
              { ...original, ragas: ei.ragas, retrieval: ei.retrieval },
              { id: exp.weight_set.id, weights: exp.weight_set.weights },
              exp.enabled_components,
            );
            expect(recomputed.score).toEqual(ei.quality_score);
            // components unchanged
            expect(ei.ragas).toEqual(original.ragas);
          }
          // caveat + provenance always present
          expect(exp.methodology_caveat).toContain(EXTERNAL_METHODOLOGY_CAVEAT);
          expect(exp.ragas_version).toBe(RAGAS_VERSION);
          expect(exp.bedrock_model_id).toBe(BEDROCK_ID);
        },
      ),
    );
  });
});
