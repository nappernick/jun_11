/**
 * Property 11: Axis mapping is configurable and defaults to the mockup mapping.
 *
 * DEFAULT_AXIS_MAPPING binds X→latency (log, lower-better), Y→quality
 * (linear, higher-better), Z→instance_index (linear); and the AxisMapping is
 * reconfigurable — any axis can be rebound to another variable/scale/direction.
 *
 * Validates: Requirements 10.1, 14.1
 */
import { describe, it, expect } from "vitest";
import {
  DEFAULT_AXIS_MAPPING,
  axisValue,
  type AxisMapping,
  type AxisBinding,
} from "../axisMapping";
import type { EvalInstance } from "../../api/types";

function instance(overrides: Partial<EvalInstance> = {}): EvalInstance {
  return {
    instance_id: "i",
    agent_id: "A",
    session_id: "s",
    instance_index: 7,
    timestamp: "2025-01-01T00:00:00Z",
    latency_ms: 420,
    stage_timings: { retrieval_ms: null, generation_ms: null },
    corpus_size: 1000,
    retrieval_cached: false,
    ragas: {},
    retrieval: {},
    confidence: null,
    volume: null,
    cost: null,
    prompt_id: null,
    category: null,
    status: "ok",
    error: null,
    ...overrides,
  };
}

describe("Property 11: default axis mapping is the mockup mapping (Req 10.1, 14.1)", () => {
  it("X binds latency (log, lower-better)", () => {
    expect(DEFAULT_AXIS_MAPPING.x.variable).toBe("latency_ms");
    expect(DEFAULT_AXIS_MAPPING.x.scale).toBe("log");
    expect(DEFAULT_AXIS_MAPPING.x.betterDirection).toBe("lower");
  });

  it("Y binds quality (linear, higher-better)", () => {
    expect(DEFAULT_AXIS_MAPPING.y.variable).toBe("quality_score");
    expect(DEFAULT_AXIS_MAPPING.y.scale).toBe("linear");
    expect(DEFAULT_AXIS_MAPPING.y.betterDirection).toBe("higher");
  });

  it("Z binds instance_index (linear, higher = later)", () => {
    expect(DEFAULT_AXIS_MAPPING.z.variable).toBe("instance_index");
    expect(DEFAULT_AXIS_MAPPING.z.scale).toBe("linear");
    expect(DEFAULT_AXIS_MAPPING.z.betterDirection).toBe("higher");
  });

  it("default bindings project an instance + composite onto the expected values", () => {
    const inst = instance({ latency_ms: 500, instance_index: 3 });
    const quality = 0.8;
    expect(axisValue(inst, DEFAULT_AXIS_MAPPING.x, quality)).toBe(500); // log floor not hit
    expect(axisValue(inst, DEFAULT_AXIS_MAPPING.y, quality)).toBe(0.8);
    expect(axisValue(inst, DEFAULT_AXIS_MAPPING.z, quality)).toBe(3);
  });

  it("the mapping is reconfigurable — any axis can be rebound", () => {
    const remapped: AxisMapping = {
      x: { variable: "corpus_size", scale: "linear", betterDirection: "higher" },
      y: { variable: { metric: "faithfulness" }, scale: "linear", betterDirection: "higher" },
      z: { variable: "latency_ms", scale: "log", betterDirection: "lower" },
    };
    const inst = instance({
      corpus_size: 2048,
      latency_ms: 0, // floored on the now-log Z axis
      ragas: { faithfulness: { value: 0.6, unavailable: false } },
    });
    expect(axisValue(inst, remapped.x, null)).toBe(2048);
    expect(axisValue(inst, remapped.y, null)).toBe(0.6);
    // latency 0 bound to a log axis is floored, proving the binding (not a fixed
    // axis) drives both variable selection and scale.
    const z: AxisBinding = remapped.z;
    expect(axisValue(inst, z, null)).toBeGreaterThan(0);
  });
});
