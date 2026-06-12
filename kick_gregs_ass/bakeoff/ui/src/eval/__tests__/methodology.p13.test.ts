/**
 * Cross-cutting invariant suite — Property 13 (Task 14.1).
 *
 * **Property 13: External-methodology labeling is present wherever a metric is
 * shown or exported**, and ragas-derived vs Authoritative_Judge signals are
 * representable as DISTINCT labeled signals that are never summed into one
 * composite.
 *
 * This is the frontend half of the suite; the Python half
 * (`bakeoff/tests/test_app_eval_posture.py`) covers Req 21.2 (loopback/no-auth).
 *
 * The assertions are made at the data/helper level — exactly where the
 * obligation lives:
 *
 *  - the metric-display path labels every value through the `methodology.ts`
 *    helpers, so the caveat string is always emitted next to a number;
 *  - `buildEvalExport` carries the same caveat (and the longer not-validated
 *    notice) on every export document;
 *  - the composite (`compositeQuality`) only ever consumes ragas/retrieval
 *    component names — an Authoritative_Judge signal lives outside those two
 *    maps and therefore can never be folded into the composite number, so the
 *    two families stay distinct, separately labeled signals.
 *
 * **Validates: Requirements 18.2, 18.3, 20.1, 20.2**
 */
import { describe, it, expect } from "vitest";
import fc from "fast-check";
import {
  EXTERNAL_METHODOLOGY_CAVEAT,
  METHODOLOGY_NOT_VALIDATED_NOTICE,
  methodologyLabel,
} from "../methodology";
import { buildEvalExport } from "../evalExport";
import {
  compositeQuality,
  DEFAULT_WEIGHT_SET,
  type CompositeWeightSet,
} from "../evalQuality";
import type { EvalInstance, MetricValue } from "../../api/types";

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------
function ragas(value: number | null): MetricValue {
  return value == null
    ? { value: null, unavailable: true, ragas_version: "0.2.1", bedrock_model_id: "m1" }
    : { value, unavailable: false, ragas_version: "0.2.1", bedrock_model_id: "m1" };
}

function retrieval(value: number | null, k = 5): MetricValue {
  return value == null
    ? { value: null, unavailable: true, k }
    : { value, unavailable: false, k };
}

function makeInstance(
  ragasMap: Record<string, MetricValue>,
  retrievalMap: Record<string, MetricValue> = {},
): EvalInstance {
  return {
    instance_id: "i1",
    agent_id: "agent-a",
    session_id: "s1",
    instance_index: 0,
    timestamp: "2025-01-01T00:00:00Z",
    latency_ms: 42,
    stage_timings: { retrieval_ms: 12, generation_ms: 30 },
    corpus_size: 1000,
    retrieval_cached: false,
    ragas: ragasMap,
    retrieval: retrievalMap,
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

// ===========================================================================
// (A) The metric-display path always emits the external-methodology caveat.
// ===========================================================================
describe("Property 13 — metric-display path carries the external-methodology caveat (Req 20.1)", () => {
  it("the bare caveat is the canonical external-methodology string", () => {
    expect(methodologyLabel()).toBe(EXTERNAL_METHODOLOGY_CAVEAT);
    expect(EXTERNAL_METHODOLOGY_CAVEAT).toMatch(/not Amazon-internal guidance/i);
  });

  it("labeling any metric value keeps the caveat inline next to the metric name", () => {
    fc.assert(
      fc.property(
        fc.string({ minLength: 1, maxLength: 40 }).filter((s) => s.trim().length > 0),
        (metricName) => {
          const label = methodologyLabel(metricName);
          // The displayed label names the metric AND carries the caveat — the
          // number is never shown without the external-methodology qualifier.
          expect(label).toContain(metricName.trim());
          expect(label).toContain(EXTERNAL_METHODOLOGY_CAVEAT);
        },
      ),
    );
  });

  it("the shell notice states the metrics are not validated against Amazon-internal sources (Req 20.3)", () => {
    expect(METHODOLOGY_NOT_VALIDATED_NOTICE).toMatch(/not been validated/i);
    expect(METHODOLOGY_NOT_VALIDATED_NOTICE).toMatch(/Amazon-internal/i);
  });
});

// ===========================================================================
// (B) The export carries the same caveat (Req 20.2).
// ===========================================================================
describe("Property 13 — export carries the external-methodology caveat (Req 20.2)", () => {
  const instances = [
    makeInstance({
      faithfulness: ragas(0.9),
      answer_relevancy: ragas(0.8),
      context_precision: ragas(0.7),
    }),
  ];

  it("buildEvalExport emits the inline caveat and the longer not-validated notice", () => {
    const exp = buildEvalExport(instances, DEFAULT_WEIGHT_SET, ENABLED);
    expect(exp.methodology_caveat).toBe(EXTERNAL_METHODOLOGY_CAVEAT);
    expect(exp.methodology_notice).toBe(METHODOLOGY_NOT_VALIDATED_NOTICE);
  });

  it("the caveat is present on every export, for any instance set", () => {
    const metricArb: fc.Arbitrary<MetricValue> = fc.oneof(
      fc.double({ min: 0, max: 1, noNaN: true }).map((v) => ragas(v)),
      fc.constant(ragas(null)),
    );
    fc.assert(
      fc.property(
        fc.array(
          fc.record({
            faithfulness: metricArb,
            answer_relevancy: metricArb,
            context_precision: metricArb,
          }),
          { minLength: 0, maxLength: 6 },
        ),
        (rows) => {
          const insts = rows.map((r) => makeInstance(r));
          const exp = buildEvalExport(insts, DEFAULT_WEIGHT_SET, ENABLED);
          expect(exp.methodology_caveat).toContain(EXTERNAL_METHODOLOGY_CAVEAT);
          expect(exp.methodology_notice.length).toBeGreaterThan(0);
        },
      ),
    );
  });
});

// ===========================================================================
// (C) ragas-derived and Authoritative_Judge are DISTINCT signals — the
//     composite never sums one into the other (Req 18.2, 18.3).
// ===========================================================================
describe("Property 13 — ragas vs Authoritative_Judge are distinct, never summed into one composite (Req 18.2, 18.3)", () => {
  it("the composite only ever consumes ragas/retrieval component names", () => {
    const metricArb: fc.Arbitrary<MetricValue> = fc.oneof(
      fc.double({ min: 0, max: 1, noNaN: true }).map((v) => ragas(v)),
      fc.constant(ragas(null)),
    );
    const retrievalArb: fc.Arbitrary<MetricValue> = fc.oneof(
      fc.double({ min: 0, max: 1, noNaN: true }).map((v) => retrieval(v)),
      fc.constant(retrieval(null)),
    );
    const weightSetArb: fc.Arbitrary<CompositeWeightSet> = fc
      .dictionary(
        fc.constantFrom("faithfulness", "answer_relevancy", "precision_at_k", "others"),
        fc.double({ min: 0, max: 1, noNaN: true }),
      )
      .map((weights) => ({ id: "arb", weights }));

    fc.assert(
      fc.property(
        metricArb,
        metricArb,
        retrievalArb,
        weightSetArb,
        (faith, ans, prec, ws) => {
          const inst = makeInstance(
            { faithfulness: faith, answer_relevancy: ans },
            { precision_at_k: prec },
          );
          const enabled = ["faithfulness", "answer_relevancy", "precision_at_k"];
          const result = compositeQuality(inst, ws, enabled);
          // Every component that actually contributed is a key of the
          // ragas∪retrieval maps — the composite cannot draw from anywhere else
          // (e.g. an Authoritative_Judge field), so the families stay distinct.
          const ragasRetrievalKeys = new Set([
            ...Object.keys(inst.ragas),
            ...Object.keys(inst.retrieval),
          ]);
          for (const used of result.usedComponents) {
            expect(ragasRetrievalKeys.has(used)).toBe(true);
          }
        },
      ),
    );
  });

  it("an Authoritative_Judge signal (outside ragas/retrieval) never changes the composite", () => {
    // The judge decision is modeled as a separate, enabled component name that is
    // NOT a key of ragas or retrieval. Because the composite only reads from those
    // two maps, the judge value can never be summed into the composite number — it
    // is reported as a missing component, never used, regardless of its value.
    const base = makeInstance({
      faithfulness: ragas(0.9),
      answer_relevancy: ragas(0.6),
    });

    const enabledWithJudge = ["faithfulness", "answer_relevancy", "authoritative_judge"];
    const enabledWithoutJudge = ["faithfulness", "answer_relevancy"];
    const ws: CompositeWeightSet = {
      id: "judge-isolation",
      weights: { faithfulness: 0.5, answer_relevancy: 0.5, authoritative_judge: 0.9 },
    };

    const withJudge = compositeQuality(base, ws, enabledWithJudge);
    const withoutJudge = compositeQuality(base, ws, enabledWithoutJudge);

    // The judge signal is accounted for as missing (it has no ragas/retrieval
    // value), never used, and the recomputed score is identical with or without
    // it — i.e. it is never folded into the composite.
    expect(withJudge.missingComponents).toContain("authoritative_judge");
    expect(withJudge.usedComponents).not.toContain("authoritative_judge");
    expect(withJudge.score).toEqual(withoutJudge.score);
  });

  it("ragas-derived and Authoritative_Judge render as distinct labeled signals (neither overrides the other)", () => {
    // Each signal is independently labeled for display. The ragas-derived signal
    // carries the external-methodology caveat; the judge signal is its own,
    // distinct label. They are different strings — one never masquerades as or
    // overrides the other.
    const ragasSignalLabel = methodologyLabel("ragas: faithfulness");
    const judgeSignalLabel = "Authoritative_Judge: pass";

    expect(ragasSignalLabel).toContain(EXTERNAL_METHODOLOGY_CAVEAT);
    expect(ragasSignalLabel).not.toEqual(judgeSignalLabel);
    expect(judgeSignalLabel).not.toContain(EXTERNAL_METHODOLOGY_CAVEAT);
  });
});
