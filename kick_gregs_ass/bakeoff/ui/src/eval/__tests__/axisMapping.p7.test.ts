/**
 * Property 7: Log-scale latency axis handles zero/negative defensively.
 *
 * For every real input, logSafe(ms) >= LOG_FLOOR_MS > 0, so a 0, negative, or
 * non-finite latency can never feed log(<=0). The same guard applies when a
 * log-scaled binding projects an instance via axisValue.
 *
 * Validates: Requirements 10.1, 14.4
 */
import { describe, it, expect } from "vitest";
import fc from "fast-check";
import {
  logSafe,
  axisValue,
  LOG_FLOOR_MS,
  DEFAULT_AXIS_MAPPING,
  type AxisBinding,
} from "../axisMapping";
import type { EvalInstance } from "../../api/types";

function instanceWithLatency(latency_ms: number, instance_index = 0): EvalInstance {
  return {
    instance_id: "i",
    agent_id: "A",
    session_id: "s",
    instance_index,
    timestamp: "2025-01-01T00:00:00Z",
    latency_ms,
    stage_timings: { retrieval_ms: null, generation_ms: null },
    corpus_size: 0,
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
  };
}

describe("Property 7: log-scale latency axis is defensive (Req 10.1, 14.4)", () => {
  it("logSafe(ms) >= LOG_FLOOR_MS > 0 for every real input", () => {
    expect(LOG_FLOOR_MS).toBeGreaterThan(0);
    fc.assert(
      fc.property(
        fc.double({ noNaN: false, noDefaultInfinity: false }),
        (ms) => {
          const out = logSafe(ms);
          expect(out).toBeGreaterThanOrEqual(LOG_FLOOR_MS);
          expect(out).toBeGreaterThan(0);
          expect(Number.isFinite(out)).toBe(true);
        },
      ),
    );
  });

  it("zero / negative / non-finite latency is floored to LOG_FLOOR_MS", () => {
    for (const bad of [0, -1, -1e9, Number.NEGATIVE_INFINITY, Number.NaN]) {
      expect(logSafe(bad)).toBe(LOG_FLOOR_MS);
    }
  });

  it("a positive latency above the floor is preserved unchanged", () => {
    fc.assert(
      fc.property(fc.double({ min: LOG_FLOOR_MS + 1e-6, max: 1e9, noNaN: true }), (ms) => {
        expect(logSafe(ms)).toBe(ms);
      }),
    );
  });

  it("axisValue on the default log latency binding floors finite 0/negative inputs", () => {
    const xBinding: AxisBinding = DEFAULT_AXIS_MAPPING.x;
    expect(xBinding.scale).toBe("log");
    // Real instances carry a finite latency; feed the full finite range (incl. 0/neg).
    fc.assert(
      fc.property(
        fc.double({ min: -1e9, max: 1e9, noNaN: true }),
        (ms) => {
          const v = axisValue(instanceWithLatency(ms), xBinding, null);
          expect(v).not.toBeNull();
          expect(v!).toBeGreaterThanOrEqual(LOG_FLOOR_MS);
        },
      ),
    );
  });
});
