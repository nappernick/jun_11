/**
 * Watch_For cue detection: drift and inconsistency (design C11; Req 13.3, 13.4).
 *
 * `detectDrift` fires iff an agent's per-session mean quality trends strictly
 * downward across consecutive sessions (>= 2 sessions, ordered by first
 * appearance). `detectInconsistency` fires iff the agent's cross-instance quality
 * variance exceeds the inconsistency threshold. These are unit examples, not
 * property tests: each case pins a specific, known input to its expected verdict.
 *
 * Quality is the recomputed composite, so to control it directly each instance
 * carries exactly one present ragas component (`faithfulness`); with that single
 * present component the weights renormalize to 1.0 and the composite equals the
 * faithfulness value. The selection therefore lets us set each instance's quality
 * precisely.
 *
 * Validates: Requirements 13.3, 13.4
 */
import { describe, it, expect } from "vitest";
import {
  deriveChartView,
  defaultSelection,
  detectDrift,
  detectInconsistency,
  INCONSISTENCY_VARIANCE_THRESHOLD,
  type EvalSelection,
} from "../evalSelectors";
import type { EvalInstance } from "../../api/types";

/** Build a well-formed plottable instance whose composite quality === `quality`. */
function inst(opts: {
  id: string;
  agent: string;
  session: string;
  index: number;
  quality: number;
}): EvalInstance {
  return {
    instance_id: opts.id,
    agent_id: opts.agent,
    session_id: opts.session,
    instance_index: opts.index,
    timestamp: "2025-01-01T00:00:00Z",
    latency_ms: 100,
    stage_timings: { retrieval_ms: null, generation_ms: null },
    corpus_size: 100,
    retrieval_cached: false,
    // single present component -> composite renormalizes to exactly this value
    ragas: { faithfulness: { value: opts.quality, unavailable: false } },
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

/** A selection whose composite is driven solely by the single `faithfulness` value. */
const selection: EvalSelection = {
  ...defaultSelection(),
  enabledMetrics: ["faithfulness"],
  smoothingWindow: 1,
};

const view = (instances: readonly EvalInstance[]) => deriveChartView(instances, selection);

describe("detectDrift (Req 13.3): downward quality across consecutive sessions", () => {
  it("fires on a monotone downward per-session trend", () => {
    // s1 (idx 0,1) mean 0.9, s2 (idx 10,11) mean 0.6, s3 (idx 20,21) mean 0.3
    const v = view([
      inst({ id: "a0", agent: "A", session: "s1", index: 0, quality: 0.9 }),
      inst({ id: "a1", agent: "A", session: "s1", index: 1, quality: 0.9 }),
      inst({ id: "a2", agent: "A", session: "s2", index: 10, quality: 0.6 }),
      inst({ id: "a3", agent: "A", session: "s2", index: 11, quality: 0.6 }),
      inst({ id: "a4", agent: "A", session: "s3", index: 20, quality: 0.3 }),
      inst({ id: "a5", agent: "A", session: "s3", index: 21, quality: 0.3 }),
    ]);
    expect(detectDrift(v, "A")).toBe(true);
  });

  it("fires using SESSION means (a single noisy point inside a session does not break the trend)", () => {
    // s1 mean = (0.95+0.85)/2 = 0.90, s2 mean = (0.50+0.70)/2 = 0.60 -> still downward
    const v = view([
      inst({ id: "a0", agent: "A", session: "s1", index: 0, quality: 0.95 }),
      inst({ id: "a1", agent: "A", session: "s1", index: 1, quality: 0.85 }),
      inst({ id: "a2", agent: "A", session: "s2", index: 10, quality: 0.5 }),
      inst({ id: "a3", agent: "A", session: "s2", index: 11, quality: 0.7 }),
    ]);
    expect(detectDrift(v, "A")).toBe(true);
  });

  it("does NOT fire on an upward trend", () => {
    const v = view([
      inst({ id: "a0", agent: "A", session: "s1", index: 0, quality: 0.3 }),
      inst({ id: "a1", agent: "A", session: "s2", index: 10, quality: 0.6 }),
      inst({ id: "a2", agent: "A", session: "s3", index: 20, quality: 0.9 }),
    ]);
    expect(detectDrift(v, "A")).toBe(false);
  });

  it("does NOT fire on a flat trend (equal session means are not strictly decreasing)", () => {
    const v = view([
      inst({ id: "a0", agent: "A", session: "s1", index: 0, quality: 0.5 }),
      inst({ id: "a1", agent: "A", session: "s2", index: 10, quality: 0.5 }),
    ]);
    expect(detectDrift(v, "A")).toBe(false);
  });

  it("does NOT fire on a non-monotone (down-then-up) trend", () => {
    const v = view([
      inst({ id: "a0", agent: "A", session: "s1", index: 0, quality: 0.8 }),
      inst({ id: "a1", agent: "A", session: "s2", index: 10, quality: 0.4 }),
      inst({ id: "a2", agent: "A", session: "s3", index: 20, quality: 0.6 }),
    ]);
    expect(detectDrift(v, "A")).toBe(false);
  });

  it("does NOT fire with fewer than two sessions", () => {
    const v = view([
      inst({ id: "a0", agent: "A", session: "s1", index: 0, quality: 0.9 }),
      inst({ id: "a1", agent: "A", session: "s1", index: 1, quality: 0.2 }),
    ]);
    expect(detectDrift(v, "A")).toBe(false);
  });
});

describe("detectInconsistency (Req 13.4): high cross-instance quality variance", () => {
  it("fires when variance exceeds the threshold", () => {
    // alternating 0.1 / 0.9: mean 0.5, population variance 0.16 > 0.05
    const v = view([
      inst({ id: "a0", agent: "A", session: "s1", index: 0, quality: 0.1 }),
      inst({ id: "a1", agent: "A", session: "s1", index: 1, quality: 0.9 }),
      inst({ id: "a2", agent: "A", session: "s1", index: 2, quality: 0.1 }),
      inst({ id: "a3", agent: "A", session: "s1", index: 3, quality: 0.9 }),
    ]);
    expect(detectInconsistency(v, "A")).toBe(true);
  });

  it("does NOT fire when quality is tightly clustered (low variance)", () => {
    // 0.50 / 0.51 / 0.49 / 0.50: variance well under the threshold
    const v = view([
      inst({ id: "a0", agent: "A", session: "s1", index: 0, quality: 0.5 }),
      inst({ id: "a1", agent: "A", session: "s1", index: 1, quality: 0.51 }),
      inst({ id: "a2", agent: "A", session: "s1", index: 2, quality: 0.49 }),
      inst({ id: "a3", agent: "A", session: "s1", index: 3, quality: 0.5 }),
    ]);
    expect(detectInconsistency(v, "A")).toBe(false);
  });

  it("does NOT fire with fewer than two quality values", () => {
    const v = view([inst({ id: "a0", agent: "A", session: "s1", index: 0, quality: 0.5 })]);
    expect(detectInconsistency(v, "A")).toBe(false);
  });

  it("is scoped per-agent: another agent's spread does not flag a stable agent", () => {
    const v = view([
      // stable agent A
      inst({ id: "a0", agent: "A", session: "s1", index: 0, quality: 0.5 }),
      inst({ id: "a1", agent: "A", session: "s1", index: 1, quality: 0.5 }),
      // volatile agent B
      inst({ id: "b0", agent: "B", session: "s1", index: 0, quality: 0.0 }),
      inst({ id: "b1", agent: "B", session: "s1", index: 1, quality: 1.0 }),
    ]);
    expect(detectInconsistency(v, "A")).toBe(false);
    expect(detectInconsistency(v, "B")).toBe(true);
  });

  it("threshold constant is the documented value", () => {
    expect(INCONSISTENCY_VARIANCE_THRESHOLD).toBeCloseTo(0.05, 10);
  });
});
