/**
 * Tests for the on-demand combinatorial run logic (Area F / Req 22).
 *
 * Exercises the PURE request-building + confirmation-gate module that backs the
 * `OnDemandRunControl` component (the component is a thin shell over it), so the
 * behaviour is verified without a DOM:
 *
 *  - The control builds a VALID run request from arbitrary selections (Req 22.1–22.6).
 *  - The over-threshold confirmation gate BLOCKS launch until confirmed (Req 22.12).
 *  - The default surface stays recorded-run visualization (Req 22.8): the control
 *    is not open by default.
 */
import { describe, it, expect } from "vitest";
import {
  ON_DEMAND_DEFAULT_OPEN,
  DEFAULT_ONDEMAND_THRESHOLD,
  buildOnDemandRequest,
  canLaunch,
  combinationCount,
  requiresConfirmation,
  type OnDemandSelection,
} from "../onDemandRun";

const sel = (over: Partial<OnDemandSelection> = {}): OnDemandSelection => ({
  agents: ["agent-a"],
  metrics: ["faithfulness"],
  corpusSizes: [],
  queryIds: ["q0"],
  ...over,
});

describe("combinationCount — cartesian sizing (Req 22.6)", () => {
  it("is |agents| x |sizes| x |queries|, with empty sizes counting as one", () => {
    expect(combinationCount(sel({ agents: ["a", "b"], corpusSizes: [10, 20, 30], queryIds: ["q0", "q1"] }))).toBe(
      2 * 3 * 2,
    );
    // empty corpus-size selection => a single default size (factor 1).
    expect(combinationCount(sel({ agents: ["a", "b"], corpusSizes: [], queryIds: ["q0"] }))).toBe(2);
  });

  it("collapses duplicate selections", () => {
    expect(combinationCount(sel({ agents: ["a", "a", "b"], corpusSizes: [10, 10], queryIds: ["q0", "q0"] }))).toBe(
      2 * 1 * 1,
    );
  });
});

describe("buildOnDemandRequest — a valid request from arbitrary selections (Req 22.1–22.6)", () => {
  it("marks on_demand and carries de-duplicated agents/metrics/queries", () => {
    const body = buildOnDemandRequest(
      sel({
        agents: ["agent-a", "agent-a", "agent-b"],
        metrics: ["faithfulness", "precision_at_k", "faithfulness"],
        queryIds: ["q0", "q1", "q1"],
      }),
    );
    expect(body.on_demand).toBe(true);
    expect(body.agents).toEqual(["agent-a", "agent-b"]);
    // an arbitrary subset INCLUDING a retrieval-metric name (Req 22.3).
    expect(body.metrics).toEqual(["faithfulness", "precision_at_k"]);
    expect(body.query_ids).toEqual(["q0", "q1"]);
    // no confirm flag unless explicitly requested.
    expect(body.confirm).toBeUndefined();
  });

  it("includes corpus_sizes only when a sweep series is selected (Req 22.4)", () => {
    expect(buildOnDemandRequest(sel({ corpusSizes: [] })).corpus_sizes).toBeUndefined();
    expect(buildOnDemandRequest(sel({ corpusSizes: [50, 100, 100] })).corpus_sizes).toEqual([50, 100]);
  });

  it("accepts an arbitrary pool of one or more agents (not bound to >= 3) (Req 22.2)", () => {
    const body = buildOnDemandRequest(sel({ agents: ["agent-c"] }));
    expect(body.agents).toEqual(["agent-c"]);
    expect(body.on_demand).toBe(true);
  });

  it("sets confirm=true when explicitly confirmed (Req 22.12)", () => {
    expect(buildOnDemandRequest(sel(), { confirm: true }).confirm).toBe(true);
  });
});

describe("canLaunch — over-threshold confirmation gate (Req 22.12)", () => {
  it("blocks launch over threshold until confirmed, then allows it", () => {
    // 4 agents x 1 size x 4 queries = 16 combinations, threshold 8.
    const big = sel({ agents: ["a", "b", "c", "d"], queryIds: ["q0", "q1", "q2", "q3"] });
    expect(combinationCount(big)).toBe(16);
    expect(requiresConfirmation(big, 8)).toBe(true);

    const blocked = canLaunch(big, { confirmed: false, threshold: 8 });
    expect(blocked.ok).toBe(false);
    expect(blocked.requiresConfirmation).toBe(true);
    expect(blocked.count).toBe(16);

    const confirmed = canLaunch(big, { confirmed: true, threshold: 8 });
    expect(confirmed.ok).toBe(true);
    expect(confirmed.requiresConfirmation).toBe(true);
  });

  it("needs no confirmation under the threshold", () => {
    const small = sel({ agents: ["a", "b"], queryIds: ["q0", "q1"] });
    const d = canLaunch(small, { confirmed: false, threshold: 8 });
    expect(d.ok).toBe(true);
    expect(d.requiresConfirmation).toBe(false);
  });

  it("blocks an empty agent or query selection regardless of confirmation", () => {
    expect(canLaunch(sel({ agents: [] }), { confirmed: true }).ok).toBe(false);
    expect(canLaunch(sel({ queryIds: [] }), { confirmed: true }).ok).toBe(false);
  });
});

describe("default surface posture (Req 22.8)", () => {
  it("the on-demand control is NOT open by default", () => {
    expect(ON_DEMAND_DEFAULT_OPEN).toBe(false);
  });

  it("exposes a default threshold mirroring the backend", () => {
    expect(DEFAULT_ONDEMAND_THRESHOLD).toBe(256);
  });
});
