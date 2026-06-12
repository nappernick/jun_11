/**
 * Property 6: View fully reconstructs from durable status backfill (no blanking).
 *
 * The view built purely from the Status_Endpoint backfill equals, in displayed
 * content, the view built by applying the live Stream_Channel deltas, for the same
 * underlying set of instance records R. Dedupe by instance_id makes seed/backfill/
 * stream idempotent, so even a shuffled + re-sent (duplicated) delta sequence
 * reconstructs the identical ChartView. A reload or reconnect therefore never
 * blanks the surface.
 *
 *   ∀ record set R:  deriveChartView(fromStatus(R)) ≡ deriveChartView(fromStream(R))
 *
 * Validates: Requirements 8.5, 15.2, 15.3, 15.4, 15.5
 */
import { describe, it, expect } from "vitest";
import fc from "fast-check";
import {
  deriveChartView,
  fromStatus,
  fromStream,
  type EvalSelection,
} from "../evalSelectors";
import { DEFAULT_AXIS_MAPPING } from "../axisMapping";
import { DEFAULT_WEIGHT_SET } from "../evalQuality";
import type {
  EvalInstance,
  EvalInstanceAppended,
  EvalStatus,
  MetricValue,
} from "../../api/types";

const METRIC_POOL = [
  "faithfulness",
  "answer_relevancy",
  "context_precision",
  "context_recall",
] as const;

const metricValueArb: fc.Arbitrary<MetricValue> = fc.oneof(
  fc.double({ min: 0, max: 1, noNaN: true }).map((value) => ({ value, unavailable: false })),
  fc.constant({ value: null, unavailable: true } as MetricValue),
);

const ragasMapArb: fc.Arbitrary<Record<string, MetricValue>> = fc
  .subarray([...METRIC_POOL], { minLength: 0 })
  .chain((names) => fc.record(Object.fromEntries(names.map((n) => [n, metricValueArb]))));

/** Build a well-formed EvalInstance from a seed record, stamping a unique id. */
function makeInstance(
  seed: {
    agent_id: string;
    session_id: string;
    instance_index: number;
    latency_ms: number;
    corpus_size: number;
    ragas: Record<string, MetricValue>;
    prompt_id: string | null;
    category: string | null;
  },
  index: number,
): EvalInstance {
  return {
    instance_id: `inst-${index}`,
    agent_id: seed.agent_id,
    session_id: seed.session_id,
    instance_index: seed.instance_index,
    timestamp: "2025-01-01T00:00:00Z",
    latency_ms: seed.latency_ms,
    stage_timings: { retrieval_ms: null, generation_ms: null },
    corpus_size: seed.corpus_size,
    retrieval_cached: false,
    ragas: seed.ragas,
    retrieval: {},
    confidence: null,
    volume: null,
    cost: null,
    prompt_id: seed.prompt_id,
    category: seed.category,
    status: "ok",
    error: null,
  };
}

const seedArb = fc.record({
  agent_id: fc.constantFrom("A", "B", "C", "D"),
  session_id: fc.constantFrom("s1", "s2", "s3"),
  instance_index: fc.nat({ max: 50 }),
  latency_ms: fc.double({ min: 1, max: 10000, noNaN: true }),
  corpus_size: fc.nat({ max: 1000 }),
  ragas: ragasMapArb,
  prompt_id: fc.option(fc.constantFrom("p1", "p2"), { nil: null }),
  category: fc.option(fc.constantFrom("c1", "c2"), { nil: null }),
});

/** A record set R with guaranteed-unique instance_ids. */
const recordSetArb: fc.Arbitrary<readonly EvalInstance[]> = fc
  .array(seedArb, { maxLength: 30 })
  .map((seeds) => seeds.map((s, i) => makeInstance(s, i)));

const selectionArb: fc.Arbitrary<EvalSelection> = fc.record({
  agentIds: fc.subarray(["A", "B", "C", "D"], { minLength: 0 }),
  sessionIds: fc.oneof(
    fc.constant("all" as const),
    fc.subarray(["s1", "s2", "s3"], { minLength: 0 }),
  ),
  enabledMetrics: fc.constant([...METRIC_POOL]),
  weightSet: fc.constant(DEFAULT_WEIGHT_SET),
  promptFilter: fc.option(fc.constantFrom("p1", "p2"), { nil: null }),
  categoryFilter: fc.option(fc.constantFrom("c1", "c2"), { nil: null }),
  smoothingWindow: fc.integer({ min: 1, max: 5 }),
  axes: fc.constant(DEFAULT_AXIS_MAPPING),
});

const baseStatus: Omit<EvalStatus, "instances"> = {
  status: "running",
  error: null,
  started_at: null,
  finished_at: null,
  agents: [],
  sessions: [],
  corpus_sizes: [],
  instance_count: 0,
};

describe("Property 6: durable reconstruction equality (Req 8.5, 15.2, 15.3, 15.4, 15.5)", () => {
  it("deriveChartView(fromStatus(R)) ≡ deriveChartView(fromStream(R)) for the same record set", () => {
    fc.assert(
      fc.property(
        recordSetArb,
        selectionArb,
        // a permutation + how many records to re-send (duplicate) on the stream
        fc.array(fc.nat(), { maxLength: 40 }),
        (R, selection, dupIdx) => {
          // Status backfill carries R (windowed/whole) directly.
          const status: EvalStatus = { ...baseStatus, instances: R, instance_count: R.length };

          // The live stream is delta-only and may re-send records out of order:
          // shuffle R and append duplicates of arbitrary members. Dedupe by id
          // must collapse these to the same underlying set.
          const deltas: EvalInstanceAppended[] = R.map((instance) => ({ instance }));
          for (const idx of dupIdx) {
            if (R.length > 0) deltas.push({ instance: R[idx % R.length]! });
          }
          // rotate to perturb arrival order deterministically per-run
          const rotate = R.length > 0 ? dupIdx.length % deltas.length : 0;
          const rotated = [...deltas.slice(rotate), ...deltas.slice(0, rotate)];

          const fromStatusView = deriveChartView(fromStatus(status), selection);
          const fromStreamView = deriveChartView(fromStream(rotated), selection);

          expect(fromStreamView).toEqual(fromStatusView);
        },
      ),
    );
  });

  it("a record set delivered ONLY via the stream still reconstructs the full view (no blanking)", () => {
    fc.assert(
      fc.property(recordSetArb, selectionArb, (R, selection) => {
        const status: EvalStatus = { ...baseStatus, instances: R, instance_count: R.length };
        const streamView = deriveChartView(
          fromStream(R.map((instance) => ({ instance }))),
          selection,
        );
        const statusView = deriveChartView(fromStatus(status), selection);
        // Same plotted content regardless of which channel delivered the records.
        expect(streamView.accounting).toEqual(statusView.accounting);
        expect(streamView.instances.map((i) => i.instance_id)).toEqual(
          statusView.instances.map((i) => i.instance_id),
        );
      }),
    );
  });
});
