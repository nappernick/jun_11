/**
 * Eval export (design Area D / Req 17, 20.2) — reusing the `exec/exportSnapshot.ts`
 * discipline: an exported artifact must carry enough provenance to be reproduced
 * and shared outside the dashboard, and it must keep the external-methodology
 * caveat that makes its numbers defensible.
 *
 * `buildEvalExport` serializes the selected Instance records — for each: the
 * Agent_Under_Test, Session, Instance_Index, corpus size, Latency, per-stage
 * timings, EVERY recorded ragas + retrieval value (unchanged), and the
 * recomputed Quality_Score — together with the active Composite_Weight_Set
 * (id + weights), the prompt-configuration ids, the ragas version, and the
 * Bedrock model id (Req 17.1–17.4). Because the component values are exported
 * UNCHANGED and the weight set travels with them, every exported Quality_Score is
 * recomputable from its exported components (Req 17.3). Every export carries the
 * external/industry-methodology caveat (Req 17.5 / 20.2).
 *
 * Pure and deterministic: `buildEvalExport` reads the records and recomputes a
 * score; it never mutates a recorded metric value (the P8 discipline).
 */
import type { EvalInstance, MetricValue, StageTimings } from "../api/types";
import {
  compositeQuality,
  type CompositeWeightSet,
} from "./evalQuality";
import {
  EXTERNAL_METHODOLOGY_CAVEAT,
  METHODOLOGY_NOT_VALIDATED_NOTICE,
} from "./methodology";

/** One exported Instance: identity + timings + unchanged components + the score. */
export interface EvalExportInstance {
  readonly instance_id: string;
  readonly agent_id: string;
  readonly session_id: string;
  readonly instance_index: number;
  readonly corpus_size: number;
  readonly latency_ms: number;
  readonly stage_timings: StageTimings;
  /** Every recorded ragas value, EXACTLY as recorded (Req 17.1, 17.3). */
  readonly ragas: Readonly<Record<string, MetricValue>>;
  /** Every recorded retrieval value, EXACTLY as recorded (Req 17.1, 17.3). */
  readonly retrieval: Readonly<Record<string, MetricValue>>;
  /** The Quality_Score recomputed from the exported components + weights (Req 17.1). */
  readonly quality_score: number | null;
  /** The weight-set id that produced the score (Req 17.2). */
  readonly weight_set_id: string;
  /** The prompt-config id per ragas metric on this instance (Req 17.2). */
  readonly prompt_config_ids: Readonly<Record<string, string>>;
}

/** The full export document — instances + the configs needed to reproduce them. */
export interface EvalExport {
  readonly generated_at: string;
  /** Inline external-methodology caveat (Req 17.5 / 20.2). */
  readonly methodology_caveat: string;
  /** The longer "not validated against Amazon-internal sources" notice (Req 20.3). */
  readonly methodology_notice: string;
  /** Active Composite_Weight_Set: id + weights, sufficient to recompute (Req 17.2). */
  readonly weight_set: { readonly id: string; readonly weights: Readonly<Record<string, number>> };
  /** Which component metrics were eligible for the composite. */
  readonly enabled_components: readonly string[];
  /** The ragas version recorded for the exported metric values (Req 17.4). */
  readonly ragas_version: string | null;
  /** The Bedrock model id recorded for the exported metric values (Req 17.4). */
  readonly bedrock_model_id: string | null;
  /** Union of prompt-config ids seen across exported instances (Req 17.2). */
  readonly prompt_config_ids: readonly string[];
  readonly instances: readonly EvalExportInstance[];
}

/** First defined `field` across an instance's ragas values, else null. */
function firstRagasProvenance(
  instances: readonly EvalInstance[],
  field: "ragas_version" | "bedrock_model_id",
): string | null {
  for (const inst of instances) {
    for (const mv of Object.values(inst.ragas)) {
      const v = mv[field];
      if (typeof v === "string" && v.length > 0) return v;
    }
  }
  return null;
}

/** Per-metric prompt-config ids recorded on one instance's ragas values (Req 17.2). */
function promptConfigIdsOf(inst: EvalInstance): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [name, mv] of Object.entries(inst.ragas)) {
    if (typeof mv.prompt_config_id === "string" && mv.prompt_config_id.length > 0) {
      out[name] = mv.prompt_config_id;
    }
  }
  return out;
}

/**
 * Build the export document from the selected instances, the active weight set,
 * and the enabled component set.
 *
 * The Quality_Score for each instance is recomputed via `compositeQuality` from
 * the UNCHANGED recorded values, so an importer that re-runs the same composite
 * over the exported components and weights reproduces every score exactly
 * (Req 17.3). Component values are copied through without modification.
 */
export function buildEvalExport(
  instances: readonly EvalInstance[],
  weightSet: CompositeWeightSet,
  enabledComponents: readonly string[],
  now: Date = new Date(),
): EvalExport {
  const exportedInstances: EvalExportInstance[] = instances.map((inst) => {
    const result = compositeQuality(inst, weightSet, enabledComponents);
    return {
      instance_id: inst.instance_id,
      agent_id: inst.agent_id,
      session_id: inst.session_id,
      instance_index: inst.instance_index,
      corpus_size: inst.corpus_size,
      latency_ms: inst.latency_ms,
      stage_timings: inst.stage_timings,
      ragas: inst.ragas, // unchanged (Req 17.3)
      retrieval: inst.retrieval, // unchanged (Req 17.3)
      quality_score: result.score,
      weight_set_id: result.weightSetId,
      prompt_config_ids: promptConfigIdsOf(inst),
    };
  });

  // Union of all prompt-config ids seen, deterministic order.
  const allPromptIds = new Set<string>();
  for (const ei of exportedInstances) {
    for (const id of Object.values(ei.prompt_config_ids)) allPromptIds.add(id);
  }

  return {
    generated_at: now.toISOString(),
    methodology_caveat: EXTERNAL_METHODOLOGY_CAVEAT,
    methodology_notice: METHODOLOGY_NOT_VALIDATED_NOTICE,
    weight_set: { id: weightSet.id, weights: weightSet.weights },
    enabled_components: [...enabledComponents],
    ragas_version: firstRagasProvenance(instances, "ragas_version"),
    bedrock_model_id: firstRagasProvenance(instances, "bedrock_model_id"),
    prompt_config_ids: [...allPromptIds].sort(),
    instances: exportedInstances,
  };
}

/** Serialize an export document to a stable, pretty JSON string. */
export function serializeEvalExport(exp: EvalExport): string {
  return JSON.stringify(exp, null, 2);
}

/** Trigger a client-side download of the eval export as JSON (browser only). */
export function downloadEvalExport(
  instances: readonly EvalInstance[],
  weightSet: CompositeWeightSet,
  enabledComponents: readonly string[],
): void {
  const exp = buildEvalExport(instances, weightSet, enabledComponents);
  const blob = new Blob([serializeEvalExport(exp)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `gbbo-eval-export-${exp.weight_set.id}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
