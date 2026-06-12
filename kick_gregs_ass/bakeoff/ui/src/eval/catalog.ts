/**
 * The ragas metric catalog as DATA on the frontend (design Data Models; Req 4).
 *
 * A 1:1 projection of the Python producer's catalog (`bakeoff/eval/catalog.py`):
 * the same metric names, families, scope marking, priorities, and
 * `customizablePrompt` flags. Encoding the menu as data (not branching code) is
 * what lets it grow without code changes (Req 4.5) and makes in-scope /
 * out-of-scope a queried property rather than a hard-coded conditional.
 *
 *   - In-scope, prioritized first (Req 4.1): the RAG family, the Nvidia family,
 *     and the natural-language-comparison family.
 *   - In-scope, lower priority (Req 4.2): the traditional non-LLM metrics and the
 *     general-purpose metrics.
 *   - Out of scope for a RAG harness (Req 4.3): the multimodal, agent/tool, and
 *     SQL families — present in the catalog but `scope: "out"`, so they are
 *     excluded from the default enabled set (Req 4.4) and rendered as
 *     out-of-scope.
 *
 * Every entry carries `external: true`: ragas, NDCG, and the composite are
 * general-industry methodology, never Amazon-internal guidance (Req 4.6 / P13).
 *
 * Priority is an ordinal where a smaller number sorts earlier (priority 0 is most
 * prioritized).
 */
import type { MetricCatalogEntry } from "../api/types";

// Priority bands (smaller sorts earlier), mirroring the Python catalog.
const P_RAG = 0;
const P_NVIDIA = 1;
const P_NL = 2;
const P_TRADITIONAL = 3;
const P_GENERAL = 4;
const P_OUT = 9;

/** The catalog (data). Order here is incidental — callers sort by priority. */
export const EVAL_CATALOG: readonly MetricCatalogEntry[] = [
  // --- RAG family: in-scope, prioritized first (Req 4.1) ---
  { name: "context_precision", family: "rag", scope: "in", priority: P_RAG, customizablePrompt: true, external: true },
  { name: "context_recall", family: "rag", scope: "in", priority: P_RAG, customizablePrompt: true, external: true },
  { name: "context_entities_recall", family: "rag", scope: "in", priority: P_RAG, customizablePrompt: true, external: true },
  { name: "noise_sensitivity", family: "rag", scope: "in", priority: P_RAG, customizablePrompt: true, external: true },
  { name: "response_relevancy", family: "rag", scope: "in", priority: P_RAG, customizablePrompt: true, external: true },
  { name: "faithfulness", family: "rag", scope: "in", priority: P_RAG, customizablePrompt: true, external: true },
  // --- Nvidia family: in-scope (Req 4.1) ---
  { name: "answer_accuracy", family: "nvidia", scope: "in", priority: P_NVIDIA, customizablePrompt: true, external: true },
  { name: "context_relevance", family: "nvidia", scope: "in", priority: P_NVIDIA, customizablePrompt: true, external: true },
  { name: "response_groundedness", family: "nvidia", scope: "in", priority: P_NVIDIA, customizablePrompt: true, external: true },
  // --- natural-language-comparison family: in-scope (Req 4.1) ---
  { name: "factual_correctness", family: "nl-comparison", scope: "in", priority: P_NL, customizablePrompt: true, external: true },
  // semantic_similarity is embedding-only -> no editable prompt (Req 16.7).
  { name: "semantic_similarity", family: "nl-comparison", scope: "in", priority: P_NL, customizablePrompt: false, external: true },
  // --- traditional non-LLM metrics: in-scope, lower priority (Req 4.2) ---
  { name: "bleu_score", family: "traditional", scope: "in", priority: P_TRADITIONAL, customizablePrompt: false, external: true },
  { name: "rouge_score", family: "traditional", scope: "in", priority: P_TRADITIONAL, customizablePrompt: false, external: true },
  { name: "chrf_score", family: "traditional", scope: "in", priority: P_TRADITIONAL, customizablePrompt: false, external: true },
  { name: "string_presence", family: "traditional", scope: "in", priority: P_TRADITIONAL, customizablePrompt: false, external: true },
  { name: "exact_match", family: "traditional", scope: "in", priority: P_TRADITIONAL, customizablePrompt: false, external: true },
  // --- general-purpose metrics: in-scope, lower priority (Req 4.2) ---
  { name: "aspect_critic", family: "general", scope: "in", priority: P_GENERAL, customizablePrompt: true, external: true },
  { name: "simple_criteria", family: "general", scope: "in", priority: P_GENERAL, customizablePrompt: true, external: true },
  { name: "rubrics_score", family: "general", scope: "in", priority: P_GENERAL, customizablePrompt: true, external: true },
  { name: "instance_rubrics", family: "general", scope: "in", priority: P_GENERAL, customizablePrompt: true, external: true },
  // --- out of scope for a RAG harness (Req 4.3) ---
  { name: "multimodal_faithfulness", family: "multimodal", scope: "out", priority: P_OUT, customizablePrompt: true, external: true },
  { name: "multimodal_relevance", family: "multimodal", scope: "out", priority: P_OUT, customizablePrompt: true, external: true },
  { name: "agent_goal_accuracy", family: "agentic", scope: "out", priority: P_OUT, customizablePrompt: true, external: true },
  { name: "tool_call_accuracy", family: "agentic", scope: "out", priority: P_OUT, customizablePrompt: true, external: true },
  { name: "topic_adherence", family: "agentic", scope: "out", priority: P_OUT, customizablePrompt: true, external: true },
  { name: "sql_query_equivalence", family: "sql", scope: "out", priority: P_OUT, customizablePrompt: true, external: true },
  { name: "datacompy_score", family: "sql", scope: "out", priority: P_OUT, customizablePrompt: true, external: true },
];

/** Catalog entries ordered as a prioritized menu (Req 4.1, 4.2). Stable within a band by name. */
export function catalogByPriority(): MetricCatalogEntry[] {
  return [...EVAL_CATALOG].sort((a, b) =>
    a.priority !== b.priority ? a.priority - b.priority : a.name < b.name ? -1 : a.name > b.name ? 1 : 0,
  );
}

/** The in-scope entries (Req 4.1, 4.2), in priority order. */
export function inScope(): MetricCatalogEntry[] {
  return catalogByPriority().filter((e) => e.scope === "in");
}

/** The entries marked likely out of scope for a RAG harness (Req 4.3). */
export function outOfScope(): MetricCatalogEntry[] {
  return catalogByPriority().filter((e) => e.scope === "out");
}

/**
 * The default enabled set: the in-scope entries, in priority order (Req 4.4).
 * Out-of-scope entries are present in the catalog but NEVER enabled by default.
 */
export function defaultEnabled(): MetricCatalogEntry[] {
  return inScope();
}

/** The names of the default enabled set (Req 4.4), in priority order. */
export function defaultEnabledNames(): string[] {
  return defaultEnabled().map((e) => e.name);
}

/** Look up a catalog entry by name (undefined if unknown). */
export function getCatalogEntry(name: string): MetricCatalogEntry | undefined {
  return EVAL_CATALOG.find((e) => e.name === name);
}

/** True iff the named metric is in the default enabled set (Req 4.4). */
export function isEnabledByDefault(name: string): boolean {
  const e = getCatalogEntry(name);
  return e !== undefined && e.scope === "in";
}
