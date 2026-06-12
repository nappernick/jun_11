/**
 * On-demand combinatorial run logic (Area F / Req 22) — the PURE half.
 *
 * This module holds the framework-free request-building and confirmation-gate
 * logic for the latent, on-demand evaluation-run capability, kept separate from
 * the React control (`OnDemandRunControl.tsx`) so it is unit-testable as plain
 * functions (no DOM, no network).
 *
 * What it expresses:
 *
 *  - **An arbitrary pool** (Req 22.2–22.5): one or more agents, an arbitrary
 *    metric subset (ragas + retrieval), an arbitrary corpus size / sweep series,
 *    and an arbitrary query subset — assembled interactively, never from a config
 *    file or code edit (Req 22.1).
 *  - **The cartesian combination count** (Req 22.6): the run produces one Instance
 *    per element of `agents x corpus sizes x queries`, so the count is the product
 *    of the three distinct-selection sizes.
 *  - **The confirmation gate** (Req 22.12): a combination count over the configured
 *    threshold may not launch without explicit user confirmation.
 *  - **The non-default posture** (Req 22.8): {@link ON_DEMAND_DEFAULT_OPEN} is
 *    `false` — the control is collapsed by default so the primary surface remains
 *    visualization of already-recorded runs.
 *
 * The request shape produced here is exactly the `POST /api/eval/runs/start` body
 * the backend's on-demand path consumes (`on_demand: true`, agents, metrics,
 * corpus_sizes, query_ids, confirm).
 */
import type { EvalRunStartBody } from "../api/client";

/** The default combination threshold; mirrors the backend default (Req 22.12). */
export const DEFAULT_ONDEMAND_THRESHOLD = 256;

/**
 * The on-demand control is NOT open by default (Req 22.8): the default and
 * primary surface of the feature remains visualization of already-recorded runs.
 * The control is reachable (Req 22.7) but collapsed until the user opens it.
 */
export const ON_DEMAND_DEFAULT_OPEN = false;

/** A user-assembled on-demand run selection (arbitrary pool; Req 22.2–22.5). */
export interface OnDemandSelection {
  /** One or more agents — NOT bound to the >= 3 comparison primitive (Req 22.2). */
  readonly agents: readonly string[];
  /** Arbitrary subset of enabled ragas + retrieval metrics (Req 22.3). */
  readonly metrics: readonly string[];
  /** Arbitrary corpus size / sweep series; empty → a single default size (Req 22.4). */
  readonly corpusSizes: readonly number[];
  /** Arbitrary query subset, by id (Req 22.5). */
  readonly queryIds: readonly string[];
}

/** Distinct, order-preserving copy of a list (dedupe without sorting). */
function distinct<T>(xs: readonly T[]): T[] {
  const seen = new Set<T>();
  const out: T[] = [];
  for (const x of xs) {
    if (!seen.has(x)) {
      seen.add(x);
      out.push(x);
    }
  }
  return out;
}

/**
 * The number of Instances the on-demand run would produce (Req 22.6):
 * `|distinct agents| x |distinct corpus sizes| x |distinct queries|`.
 *
 * An empty corpus-size selection means "a single default corpus size", so it
 * contributes a factor of 1 (mirroring the backend, which runs a single-size
 * multi-agent run when no sweep series is given). Duplicate selections collapse.
 */
export function combinationCount(sel: OnDemandSelection): number {
  const nAgents = distinct(sel.agents).length;
  const nSizes = Math.max(1, distinct(sel.corpusSizes).length);
  const nQueries = distinct(sel.queryIds).length;
  return nAgents * nSizes * nQueries;
}

/** Whether the selection's combination count exceeds the confirmation threshold. */
export function requiresConfirmation(
  sel: OnDemandSelection,
  threshold: number = DEFAULT_ONDEMAND_THRESHOLD,
): boolean {
  return combinationCount(sel) > threshold;
}

/** The outcome of the pre-launch gate for a selection. */
export interface LaunchDecision {
  /** True iff the run may be launched right now. */
  readonly ok: boolean;
  /** True iff the combination count is over threshold (confirmation needed). */
  readonly requiresConfirmation: boolean;
  /** The cartesian combination count for the selection. */
  readonly count: number;
  /** A human-readable reason the run is blocked (when `ok` is false). */
  readonly reason?: string;
}

/**
 * Decide whether an on-demand selection may launch (Req 22.12).
 *
 * Blocks an empty agent or query selection, and — when the combination count is
 * over the threshold — blocks the launch until the user has confirmed. The
 * `confirmed` flag is the explicit user confirmation Req 22.12 requires.
 */
export function canLaunch(
  sel: OnDemandSelection,
  opts: { confirmed?: boolean; threshold?: number } = {},
): LaunchDecision {
  const threshold = opts.threshold ?? DEFAULT_ONDEMAND_THRESHOLD;
  const confirmed = opts.confirmed ?? false;
  const count = combinationCount(sel);
  const needsConfirm = count > threshold;

  if (distinct(sel.agents).length < 1) {
    return { ok: false, requiresConfirmation: needsConfirm, count, reason: "select at least one agent" };
  }
  if (distinct(sel.queryIds).length < 1) {
    return { ok: false, requiresConfirmation: needsConfirm, count, reason: "select at least one query" };
  }
  if (needsConfirm && !confirmed) {
    return {
      ok: false,
      requiresConfirmation: true,
      count,
      reason: `combination count ${count} exceeds threshold ${threshold}; confirm to launch`,
    };
  }
  return { ok: true, requiresConfirmation: needsConfirm, count };
}

/**
 * Build the `POST /api/eval/runs/start` body for an on-demand run from an
 * arbitrary selection (Req 22.1–22.6). Agents / corpus sizes / queries are
 * de-duplicated (order preserved). An empty corpus-size selection omits
 * `corpus_sizes` so the backend runs a single default-size run. `confirm` is set
 * only when explicitly passed (the over-threshold gate, Req 22.12).
 */
export function buildOnDemandRequest(
  sel: OnDemandSelection,
  opts: { confirm?: boolean } = {},
): EvalRunStartBody {
  const sizes = distinct(sel.corpusSizes);
  const body: EvalRunStartBody = {
    on_demand: true,
    agents: distinct(sel.agents),
    metrics: distinct(sel.metrics),
    query_ids: distinct(sel.queryIds),
    ...(sizes.length > 0 ? { corpus_sizes: sizes } : {}),
    ...(opts.confirm ? { confirm: true } : {}),
  };
  return body;
}
