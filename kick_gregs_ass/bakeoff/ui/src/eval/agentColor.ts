/**
 * Stable, injective agent → color mapping for the ragas eval dashboard (design C4).
 *
 * The existing `lib/format.ts::modelColor` is deterministic but hash-based, so it
 * is not guaranteed injective across a small set — two agents could collide. For
 * the headline N-agent comparison (N >= 3, concretely four) the mapping must be
 * BOTH stable across renders/tabs AND injective (Req 10.7 / Property 5).
 *
 * This module assigns from a fixed, theme-aligned categorical palette in stable
 * sorted agent-id order, and falls back to the hashed `modelColor` only for the
 * agents beyond the palette. That guarantees injectivity for N <= palette size
 * and a stable color per agent regardless of arrival order, in every chart and
 * tab the map is threaded into.
 */
import { modelColor } from "../lib/format";

/**
 * Fixed categorical palette, aligned with the dark-console theme tokens. Colors
 * are visually distinct and ordered so the first few agents (the common case)
 * get the most separable hues.
 */
export const AGENT_PALETTE: readonly string[] = [
  "#f7a14b", // accent (A)
  "#4cc38a", // good   (B)
  "#5aa9f7", // blue   (C)
  "#e5688b", // bad/pink (D)
  "#c08bf0", // violet
  "#ffc684", // amber
  "#5fd0c8", // teal
  "#d7e36b", // lime
];

/**
 * Build a stable, injective agent→color map for a given agent set.
 *
 * The ids are de-duplicated and sorted before assignment, so the same agent set
 * always yields the same color per agent regardless of the order the ids arrive
 * in (stability). Each of the first `AGENT_PALETTE.length` agents gets a distinct
 * palette entry (injectivity for N <= palette size); any agents beyond that fall
 * back to the deterministic hashed `modelColor`.
 */
export function buildAgentColorMap(
  agentIds: readonly string[],
): ReadonlyMap<string, string> {
  const sorted = [...new Set(agentIds)].sort();
  const map = new Map<string, string>();
  sorted.forEach((id, i) => {
    map.set(id, i < AGENT_PALETTE.length ? AGENT_PALETTE[i]! : modelColor(id));
  });
  return map;
}
