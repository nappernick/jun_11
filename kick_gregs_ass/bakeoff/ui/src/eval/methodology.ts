/**
 * Canonical external-methodology caveat text for the ragas eval dashboard
 * (design Sourcing note; Req 4.6, 20.1, 20.3 / Property 13).
 *
 * The ragas metric catalog, precision@k / recall@k / NDCG, and the weighted
 * quality composite are GENERAL INDUSTRY PRACTICE, not Amazon-internal guidance.
 * Requirement 20 makes labeling that fact a product obligation: every place a
 * metric value is shown or exported must carry the caveat, and the app shell must
 * carry the longer "not validated against Amazon-internal primary sources" notice.
 *
 * This module is the single source of truth for that text so the wording can
 * never drift between a metric display, the export footer, and the shell.
 */

/**
 * The short caveat shown next to every metric value (per-metric displays and the
 * export footer). Keep it terse — it sits inline alongside numbers.
 */
export const EXTERNAL_METHODOLOGY_CAVEAT =
  "external/industry methodology, not Amazon-internal guidance";

/**
 * The longer notice for the application shell, stated once at the top level so a
 * viewer understands the whole surface reflects external methodology that has not
 * been checked against Amazon's own primary sources.
 */
export const METHODOLOGY_NOT_VALIDATED_NOTICE =
  "These eval metrics (ragas, precision@k / recall@k / NDCG, and the weighted " +
  "quality composite) use external/industry methodology, not Amazon-internal " +
  "guidance, and have not been validated against Amazon-internal primary sources.";

/**
 * Helper used by every metric display and the export footer to label a metric (or
 * a value string) as external methodology. Passing a label prefixes it; passing
 * nothing returns the bare caveat. Output is deterministic and side-effect free.
 *
 * @example methodologyLabel()                 -> "external/industry methodology, not Amazon-internal guidance"
 * @example methodologyLabel("faithfulness")   -> "faithfulness — external/industry methodology, not Amazon-internal guidance"
 */
export function methodologyLabel(label?: string): string {
  const trimmed = label?.trim();
  return trimmed ? `${trimmed} — ${EXTERNAL_METHODOLOGY_CAVEAT}` : EXTERNAL_METHODOLOGY_CAVEAT;
}
