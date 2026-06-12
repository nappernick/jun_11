/**
 * Property 10: Composite records its weight-set id and missing components.
 *
 * Every CompositeResult carries the weightSetId that produced it and exactly the
 * weighted-but-unavailable components in missingComponents (whether unavailable
 * because the value is flagged unavailable, or because the metric is absent from
 * the recorded maps). This holds even when the score is null.
 *
 * Validates: Requirements 3.5, 3.6
 */
import { describe, it, expect } from "vitest";
import { compositeQuality, type CompositeWeightSet } from "../evalQuality";
import type { EvalInstance, MetricValue } from "../../api/types";

function instance(
  ragas: Record<string, MetricValue>,
  retrieval: Record<string, MetricValue> = {},
): EvalInstance {
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
    retrieval,
    confidence: null,
    volume: null,
    cost: null,
    prompt_id: null,
    category: null,
    status: "ok",
    error: null,
  };
}

describe("Property 10: composite records weight-set id and missing components (Req 3.5, 3.6)", () => {
  it("records the weightSetId and exactly the weighted-but-unavailable components", () => {
    const inst = instance({
      faithfulness: { value: 0.8, unavailable: false },
      answer_relevancy: { value: null, unavailable: true }, // present-but-unavailable
      // context_precision intentionally absent from the recorded map
    });
    const ws: CompositeWeightSet = {
      id: "mockup-default-v1",
      weights: { faithfulness: 0.5, answer_relevancy: 0.3, context_precision: 0.2 },
    };
    const enabled = ["faithfulness", "answer_relevancy", "context_precision", "context_recall"];

    const result = compositeQuality(inst, ws, enabled);

    expect(result.weightSetId).toBe("mockup-default-v1");
    // answer_relevancy: unavailable flag; context_precision: absent map entry.
    // context_recall is NOT weighted (not named, no "others") ⟹ not "missing".
    expect([...result.missingComponents].sort()).toEqual(
      ["answer_relevancy", "context_precision"].sort(),
    );
    expect(result.usedComponents).toEqual(["faithfulness"]);
    expect(result.score).not.toBeNull();
  });

  it("counts an enabled-but-unlisted metric as missing when an 'others' catch-all weights it", () => {
    const inst = instance({
      faithfulness: { value: 0.9, unavailable: false },
      // noise_sensitivity is enabled + covered by "others" but unavailable here
    });
    const ws: CompositeWeightSet = {
      id: "ws-others",
      weights: { faithfulness: 0.6, others: 0.4 },
    };
    const enabled = ["faithfulness", "noise_sensitivity"];

    const result = compositeQuality(inst, ws, enabled);

    expect(result.weightSetId).toBe("ws-others");
    expect(result.missingComponents).toEqual(["noise_sensitivity"]);
  });

  it("carries the weightSetId even when the score is null (no usable component)", () => {
    const inst = instance({
      faithfulness: { value: null, unavailable: true },
    });
    const ws: CompositeWeightSet = { id: "ws-null", weights: { faithfulness: 1.0 } };

    const result = compositeQuality(inst, ws, ["faithfulness"]);

    expect(result.score).toBeNull();
    expect(result.weightSetId).toBe("ws-null");
    expect(result.missingComponents).toEqual(["faithfulness"]);
    expect(result.usedComponents).toEqual([]);
  });
});
