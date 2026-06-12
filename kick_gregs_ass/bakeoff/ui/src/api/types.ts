/**
 * Typed mirror of the Python backend's JSON payloads (bakeoff/app.py + the
 * dataclasses in bakeoff/types.py and bakeoff/aggregate.py).
 *
 * This is the contract seam (design AD-4): every shape the dashboard reads from
 * the API is declared here, so a backend field rename surfaces as a compile error
 * in the client rather than a silently-wrong chart. Because accuracy is the
 * headline requirement, that guarantee is load-bearing.
 *
 * Judge-rework insulation: the *set* of quality dimensions is intentionally NOT
 * hard-coded. The pending LLM-as-judge rework will change which dimensions exist;
 * the dashboard treats "metric" as an opaque string and renders whatever the API
 * reports, so the rework changes data, not components.
 */

export type CIMethod = "cluster_bootstrap" | "normal_approx";

export interface CI {
  readonly point: number;
  readonly low: number;
  readonly high: number;
  readonly method: string; // CIMethod, kept open for forward-compat
}

export interface VarianceDecomp {
  readonly between: number;
  readonly within: number;
  readonly judge: number;
}

export interface LatencyQuantiles {
  readonly p50: number;
  readonly p90: number;
  readonly p95: number;
}

export interface Aggregate {
  readonly group: Readonly<Record<string, string>>;
  readonly metric: string;
  readonly n_items: number;
  readonly n_trials: number;
  /** null only when the cell is explicitly insufficient-data (Property 10) */
  readonly mean_ci: CI | null;
  readonly variance_decomp: VarianceDecomp | Readonly<Record<string, number>>;
  readonly latency_quantiles: LatencyQuantiles | null;
  /** True exactly when mean_ci is null (thin cell); the P10 exclusive-or. */
  readonly insufficient_data: boolean;
}

export interface AggregateResponse {
  readonly group_by: readonly string[];
  readonly metric: string;
  readonly ci_method: string;
  readonly aggregates: readonly Aggregate[];
}

export interface BakeOffDistribution {
  readonly mean: number | null;
  readonly p50: number | null;
  readonly p90: number | null;
  readonly p95: number | null;
}

export interface BakeOffModelCard {
  readonly model: string;
  readonly n_trials: number;
  readonly n_items: number;
  readonly n_quality_trials: number;
  readonly n_quality_items: number;
  readonly quality: Aggregate | null;
  readonly timing: Readonly<Record<string, BakeOffDistribution>>;
  readonly token_usage_mean: Readonly<Record<string, number | null>>;
  readonly component_means: Readonly<Record<string, number | null>>;
  readonly answerability_counts: Readonly<Record<string, number>>;
  readonly turn_type_counts: Readonly<Record<string, number>>;
}

export interface BakeOffPairedDelta {
  readonly model_a: string;
  readonly model_b: string;
  readonly metric: string;
  readonly shared_items: number;
  readonly delta_ci: CI;
  readonly winner: string | null;
}

export interface BakeOffTimingStage {
  readonly model: string;
  readonly embed_query_ms?: number | null;
  readonly bm25_vectorize_ms?: number | null;
  readonly hybrid_search_ms?: number | null;
  readonly rerank_ms?: number | null;
  readonly retrieval_total_ms?: number | null;
  readonly ttft_ms?: number | null;
  readonly generation_total_ms?: number | null;
  readonly end_to_end_ms?: number | null;
}

export interface BakeOffRetrievalRegression {
  readonly trial_id: string;
  readonly model: string;
  readonly item_id: string;
  readonly answerability: string;
  readonly composite: number;
  readonly recall_at_k: number;
  readonly ndcg_at_k: number;
  readonly latency_ms: number;
  readonly query: string;
  readonly answer_excerpt: string;
}

export interface BakeOffDiagnostics {
  readonly source: {
    readonly success_store_only: boolean;
    readonly total_trials: number;
    readonly total_items: number;
    readonly quality_source: "phase2_judge_scores" | "outcomes_composite" | string;
    readonly quality_trials: number;
    readonly quality_items: number;
    readonly judge_scores_total: number;
    readonly judge_scores_joined: number;
    readonly composite_weights_version: string;
    readonly models: readonly string[];
    readonly passes: Readonly<Record<string, number>>;
    readonly answerability: Readonly<Record<string, number>>;
    readonly turn_type: Readonly<Record<string, number>>;
    readonly schema_version: readonly string[];
  };
  readonly model_cards: readonly BakeOffModelCard[];
  readonly paired_deltas: readonly BakeOffPairedDelta[];
  readonly cohort_slices: Readonly<Record<string, readonly Aggregate[]>>;
  readonly timing_stages: readonly BakeOffTimingStage[];
  readonly high_variance: readonly Readonly<Record<string, unknown>>[];
  readonly retrieval_regressions: readonly BakeOffRetrievalRegression[];
  /** Per-trial (latency, quality) points for the decision-surface scatter cloud. */
  readonly quality_latency?: readonly BakeOffQualityLatencyPoint[];
}

export interface BakeOffQualityLatencyPoint {
  readonly model: string;
  readonly item_id: string;
  readonly answerability: string;
  readonly composite: number;
  readonly latency_ms: number;
}

export type RunStatus = "idle" | "running" | "paused" | "aborted" | "completed";

export interface ModelCounts {
  readonly planned: number;
  readonly done: number;
  readonly in_flight: number;
  readonly errored: number;
}

export interface RunTotals {
  readonly done: number;
  readonly errored: number;
}

export interface RunSnapshot {
  readonly status: RunStatus | string;
  readonly auto_paused: boolean;
  readonly auth_refreshes: number;
  readonly totals: RunTotals;
  readonly models: Readonly<Record<string, ModelCounts>>;
  /** The AWS account the bake-off's target models run on (broker default profile). */
  readonly credential_profile?: string;
  readonly account?: string;
}

export interface BakeOffSession {
  readonly id: string;
  readonly label: string;
  readonly notes: string;
  readonly created_at: string;
  readonly updated_at: string;
  readonly archived: boolean;
  readonly kind: "legacy" | "session" | string;
  readonly root: string;
  readonly outcomes_path: string;
  readonly run_errors_path: string;
  readonly judge_scores_path: string;
  readonly reports_dir: string;
  readonly prompt_path: string;
  readonly roster: readonly string[];
  readonly roster_signature: string;
  readonly total_trials: number;
  readonly total_errors: number;
  readonly judge_scores_total: number;
  readonly models: readonly string[];
}

export interface BakeOffSessionsResponse {
  readonly active_session_id: string;
  readonly sessions: readonly BakeOffSession[];
}

/** One "trial_completed" SSE payload — bakeoff.runner._summarize. */
export interface TrialCompleted {
  readonly trial_id: string;
  readonly model: string;
  readonly item_id: string;
  readonly pass: string;
  readonly rep: number;
  readonly answerability: string;
  readonly error: boolean;
  readonly composite: number;
  /** Time to FIRST token (responsiveness) — the headline latency signal. */
  readonly ttft_ms: number;
  /** Time to FINAL token (full end-to-end generation latency). */
  readonly end_to_end_ms: number;
  readonly cohort: Readonly<Record<string, string>>;
}

export interface HarnessHealth {
  readonly status: string;
  readonly service: string;
  readonly run_status: string;
  readonly subscribers: number;
}

export type ControlAction = "pause" | "resume" | "abort";

/**
 * Body for POST /api/run/start (bakeoff/app.py api_run_start). All fields are
 * optional; the backend defaults reps=3, temperature=config.DEFAULT_TEMPERATURE,
 * max_trials=null (no clamp). `max_trials` is nullable so the caller can be
 * explicit about "no cap" without omitting the key.
 */
export interface StartRunBody {
  readonly reps?: number;
  readonly temperature?: number;
  readonly max_trials?: number | null;
}

export interface ControlResponse extends RunSnapshot {
  readonly action: ControlAction | string;
}

export interface ExecReportsList {
  readonly reports: readonly string[];
}

// --- Exec report (bakeoff/aggregate.py build_report) -----------------------

/** One speed/quality frontier point (FrontierPoint). */
export interface FrontierPoint {
  readonly model: string;
  /** null only if marked insufficient elsewhere; the route refuses bare nulls. */
  readonly quality: CI | null;
  readonly speed_p50_ms: number;
  readonly speed_p90_ms: number;
  readonly on_pareto_front: boolean;
}

/** Provenance footer carried by every exec view (Req 11.7). */
export interface ExecProvenance {
  readonly plan_version: string;
  readonly generated_at: string;
  readonly n_items: number;
  readonly n_trials: number;
  readonly judge_model: string | readonly string[];
  readonly judge_human_agreement: Readonly<Record<string, number>>;
  readonly ci_method: string;
  readonly ci_level: number;
  readonly bootstrap_n: number;
  readonly bootstrap_seed: number;
  readonly schema_version: readonly string[];
}

/** The materialized exec report served by GET /exec/aggregate. */
export interface ExecReport {
  readonly frontier: readonly FrontierPoint[];
  readonly by_model: readonly Aggregate[];
  readonly safety: readonly Aggregate[];
  readonly cohort_heatmaps: Readonly<Record<string, readonly Aggregate[]>>;
  readonly high_variance: readonly Readonly<Record<string, unknown>>[];
  readonly provenance: ExecProvenance;
}

// --- Phase-2 deferred judge (bakeoff/judge_phase2.py + app.py) -------------

export type JudgeStatusState = "idle" | "running" | "completed" | "failed";

/** GET /api/judge/status — the deferred judge lifecycle + progress. */
export interface JudgeStatus {
  readonly status: JudgeStatusState | string;
  readonly progress: {
    readonly judged: number;
    readonly sampled: number;
    readonly skipped_existing: number;
  };
  readonly error: string | null;
  readonly started_at: string | null;
  readonly finished_at: string | null;
  readonly has_summary: boolean;
}

/** Body for POST /api/judge/start. `items_per_model` is the sample dial. */
export interface JudgeStartBody {
  readonly items_per_model?: number;
}

/** One representative judged example (the judge's actual opinion on one answer). */
export interface JudgeExample {
  readonly trial_id: string;
  readonly item_id: string;
  readonly answerability: string;
  readonly momentary_state: string;
  readonly overall: number;
  readonly dimensions: Readonly<Record<string, number>>;
  readonly dim_sd: Readonly<Record<string, number>>;
  readonly evidence: Readonly<Record<string, string>>;
  readonly answer_excerpt: string;
  readonly judge_model: string;
}

/** Per-model judge rollup (continuous means + binary pass rates + examples). */
export interface JudgeModelSummary {
  readonly model: string;
  readonly n_judged: number;
  readonly overall_mean: number;
  readonly dimension_means: Readonly<Record<string, number>>;
  readonly dimension_pass_rates: Readonly<Record<string, number>>;
  readonly answerability_counts: Readonly<Record<string, number>>;
  readonly examples: readonly JudgeExample[];
}

/** GET /api/judge/scores — the per-model judge summary the judge view renders. */
export interface JudgeSummary {
  readonly dimensions: readonly string[];
  readonly pass_threshold: number;
  readonly judge_models: readonly string[];
  readonly n_records: number;
  readonly models: readonly JudgeModelSummary[];
}

// --- Multi-turn quality study (bakeoff/quality/summary.py) -----------------

/** One turn-position's mean closeness (the drift curve point). */
export interface QualityTurnPoint {
  readonly turn: number;
  /** null when the position is insufficient-data (too few samples). */
  readonly mean: number | null;
  readonly n: number;
  readonly insufficient_data: boolean;
}

/** One turn within an example conversation. */
export interface QualityExampleTurn {
  readonly turn: number;
  readonly ground_truth_kind: string; // "gold" | "wants" | "abstention"
  readonly answerability: string | null;
  readonly response_dependent: boolean;
  readonly semantic: number;
  readonly judge: number | null;
  readonly composite: number;
  readonly answer_excerpt: string;
  readonly reference_excerpt: string;
}

/** One representative conversation (best / median / worst by mean closeness). */
export interface QualityExample {
  readonly trial_id: string;
  readonly item_id: string;
  readonly rep: number;
  readonly prompt_variant_id: string;
  readonly mean_closeness: number;
  readonly turns: readonly QualityExampleTurn[];
}

/** Per-model quality rollup (the turn-drift curve + gold/wants split). */
export interface QualityModelSummary {
  readonly model: string;
  readonly n_outcomes: number;
  readonly overall_mean: number;
  readonly turn1_mean: number;
  readonly later_mean: number;
  readonly turn_closeness: readonly QualityTurnPoint[];
  readonly ground_truth_counts: Readonly<Record<string, number>>;
  readonly judged_fraction: number;
  readonly examples: readonly QualityExample[];
}

/** GET /api/quality/summary — per-model per-turn closeness for the Quality tab. */
export interface QualitySummary {
  readonly n_outcomes: number;
  readonly models: readonly QualityModelSummary[];
  readonly min_samples_for_turn_mean: number;
}

// --- Closed-loop prompt optimizer (bakeoff/quality/optimizer + app.py) ------
//
// The optimizer streams its live champion/challenger loop into the Quality_Tab
// over the EXISTING SSE broker as NEW event TYPES only (Req 9.7). Every payload
// is stamped with `model_channel` so a Per_Model_View can filter the shared
// stream down to its own Target_Model (Req 9.10/9.11). These shapes mirror
// `bakeoff/quality/optimizer/events.py` 1:1 (the design's "Per-iteration SSE
// event shape" section) plus the two read endpoints in `bakeoff/app.py`.

/** event: optimizer_champion_scored — a scored champion OR challenger on a slice. */
export interface OptimizerChampionScored {
  readonly model_channel: string;
  readonly model: string;
  readonly phase: string; // "A" | "B"
  readonly iteration_index: number;
  readonly role: string; // "champion" | "challenger"
  readonly triad: number;
  readonly ci_half_width: number;
  readonly ci_low: number;
  readonly ci_high: number;
  readonly per_dimension: Readonly<Record<string, number>>;
  readonly abstention_reward_mean: number;
  readonly answered_when_unsure_rate: number;
  readonly retrieval_backend: string;
  readonly mean_closeness: number;
  readonly n_conversations: number;
  /** v2 only: which island the score belongs to. null/absent for the v1 controller. */
  readonly island_id?: number | null;
}

/** event: optimizer_author_token — one streamed chunk of the Author's reasoning. */
export interface OptimizerAuthorToken {
  readonly model_channel: string;
  readonly iteration_index: number;
  readonly delta: string;
  /** v2 only: which island is authoring. null/absent for the v1 controller. */
  readonly island_id?: number | null;
}

/** event: optimizer_iteration_completed — accept/reject + the new champion state. */
export interface OptimizerIterationCompleted {
  readonly model_channel: string;
  readonly iteration_index: number;
  readonly challenger_triad: number | null;
  readonly challenger_ci_half_width: number | null;
  readonly gain_absolute: number | null;
  readonly gain_percent: number | null;
  readonly accepted: boolean;
  readonly consecutive_non_improving: number;
  readonly champion_instruction: string;
  readonly prompt_diff: string;
  readonly lookback_version_ids: readonly string[];
  /** v2 only: which island completed the iteration. null/absent for the v1 controller. */
  readonly island_id?: number | null;
}

/** event: optimizer_converged — Phase A stopped for this model. */
export interface OptimizerConverged {
  readonly model_channel: string;
  readonly converged_iteration: number;
  readonly stop_reason: string;
}

/** event: optimizer_phase_b — the final validation triad on the held-out set. */
export interface OptimizerPhaseB {
  readonly model_channel: string;
  readonly triad: number;
  readonly ci_half_width: number;
  readonly n_conversations: number;
}

/** Per-model progress block on the optimizer status snapshot (durable reconstruction). */
export interface OptimizerModelProgress {
  readonly phase?: string | null;
  readonly iteration_index?: number | null;
  readonly champion_score?: number;
  readonly champion_ci_half_width?: number;
  readonly challenger_score?: number | null;
  readonly promoted?: boolean;
  readonly consecutive_non_improving?: number;
  readonly converged?: boolean;
  readonly stop_reason?: string | null;
  readonly iterations: number;
  readonly viewable?: boolean;
  readonly error?: string;
}

/** GET /api/quality/optimize/status — optimizer lifecycle + per-model progress. */
export interface OptimizerStatus {
  readonly status: string; // "idle" | "running" | "completed" | "failed"
  readonly request: Readonly<Record<string, unknown>> | null;
  readonly error: string | null;
  readonly started_at: string | null;
  readonly finished_at: string | null;
  readonly models: Readonly<Record<string, OptimizerModelProgress>>;
}

/**
 * One entry in a model's ordered prompt-version history
 * (bakeoff/quality/optimizer/store.py::PromptVersion). The seed (index 0) has no
 * challenger, so `challenger_instruction`, `score`, and `ci_half_width` are null.
 */
export interface OptimizerPromptVersion {
  readonly prompt_version_id: string;
  readonly model: string;
  readonly iteration_index: number;
  readonly champion_instruction: string;
  readonly challenger_instruction: string | null;
  readonly diff: string;
  readonly score: number | null;
  readonly ci_half_width: number | null;
  readonly accepted: boolean;
}

/** GET /api/quality/optimize/history?model=... — ordered prompt-version history. */
export interface OptimizerHistory {
  readonly model: string;
  readonly versions: readonly OptimizerPromptVersion[];
}

// --- Optimizer v2 (island/tournament/coverage-ladder; design "Front end v2") ---

export type OptimizerV2IslandState = "iterating" | "escalating" | "stuck";

/** event: optimizer_island_step — one scored iteration within an island's current rung. */
export interface OptimizerIslandStep {
  readonly island_id: number;
  readonly rung_index: number;
  readonly champion_score: number;
  readonly ci_half_width: number;
  readonly state: OptimizerV2IslandState;
}

/** event: optimizer_rung_escalated — an island promoted to a higher rung. */
export interface OptimizerRungEscalated {
  readonly island_id: number;
  readonly from_rung: number;
  readonly to_rung: number;
}

/** event: optimizer_tournament — head-to-head between two island champions. */
export interface OptimizerTournament {
  readonly round: number;
  readonly island_a: { readonly champion_score: number; readonly ci_half_width: number };
  readonly island_b: { readonly champion_score: number; readonly ci_half_width: number };
  readonly shared_rung: number;
  readonly winner: number;
}

/** event: optimizer_migration — winning prompt migrated to both islands. */
export interface OptimizerMigration {
  readonly round: number;
  readonly winning_prompt_version_id: string;
}

/** One point in an island's champion-score trajectory (the durable trend curve). */
export interface OptimizerV2ScorePoint {
  readonly champion_score: number;
  readonly ci_half_width: number;
  readonly rung_index: number;
}

/** Per-island progress in the v2 status endpoint (durable backfill). */
export interface OptimizerV2IslandProgress {
  readonly island_id: number;
  /** Conversation type this island's records were appraised on (single|multi|both);
   * the v3 view splits single-run vs multi-run sections on it. */
  readonly turn_mode?: string;
  readonly rung_index: number;
  readonly champion_score: number;
  /** Backend status endpoint emits the CI under this key. */
  readonly champion_ci_half_width: number;
  readonly state: string;
  readonly stance?: string | null;
  readonly iterations?: number;
  readonly champion_instruction?: string | null;
  readonly prompt_diff?: string | null;
  readonly author_reasoning?: string | null;
  /** The full champion-score trajectory — durable backfill for the trend curve. */
  readonly score_series?: readonly OptimizerV2ScorePoint[];
  /** Latest challenger score (the "current turn" side of the prev-vs-current readout). */
  readonly challenger_score?: number | null;
  readonly challenger_ci_half_width?: number | null;
  readonly accepted?: boolean | null;
}

/** One island's line inside a status-endpoint tournament round (`scores[]`). */
export interface OptimizerV2RoundScore {
  readonly island_id: number;
  readonly champion_score: number;
  readonly champion_ci_half_width: number;
}

/**
 * Per-tournament-round record as the STATUS endpoint emits it (`scores[]` +
 * `winner`), distinct from the SSE event shape below.
 */
export interface OptimizerV2StatusRound {
  readonly round: number;
  readonly scores: readonly OptimizerV2RoundScore[];
  readonly shared_rung: number | null;
  readonly winner: number | null;
  readonly migration: boolean;
}

/** Per-tournament-round record as the SSE stream accumulates it (island_a/b). */
export interface OptimizerV2TournamentRound {
  readonly round: number;
  readonly island_a: { readonly champion_score: number; readonly ci_half_width: number };
  readonly island_b: { readonly champion_score: number; readonly ci_half_width: number };
  readonly shared_rung: number;
  readonly winner: number;
  readonly winning_prompt_version_id?: string;
}

/** GET /api/quality/optimize/v2/status — v2 optimizer lifecycle + per-island + per-round. */
export interface OptimizerV2Status {
  readonly status: string; // "idle" | "running" | "completed" | "failed"
  readonly error: string | null;
  readonly started_at: string | null;
  readonly finished_at: string | null;
  readonly models: Readonly<Record<string, OptimizerV2ModelStatus>>;
}

/** Per-model status block in v2 (matches bakeoff/app.py optimizer_v2_snapshot). */
export interface OptimizerV2ModelStatus {
  readonly islands: readonly OptimizerV2IslandProgress[];
  /** Status endpoint key is `tournament_rounds` with the `scores[]` shape. */
  readonly tournament_rounds: readonly OptimizerV2StatusRound[];
  readonly viewable?: boolean;
  readonly best_prompt?: string;
  readonly best_triad?: number;
  readonly best_ci_half_width?: number;
  readonly tournament_rounds_done?: number;
  readonly phase_b_triad?: number;
  readonly phase_b_ci_half_width?: number;
}

// --- Optimizer V3 (hardened, live-only; /api/quality/optimize/v3/*) ----------
//
// The v3 snapshot reuses v2's island/round shapes verbatim (the components are
// shared) and ADDS the per-model run-state sentinel: phase progress, contained
// failure / island-death markers, and the degraded flag.

/** The v3 run-state sentinel entry for one model (quality_opt_v3_state.json). */
export interface OptimizerV3RunState {
  readonly phase_a_complete?: boolean;
  readonly phase_b_done?: boolean;
  readonly status?: string;
  readonly degraded?: boolean;
  readonly dead_islands?: readonly number[];
  readonly champion_score?: number | null;
  readonly champion_instruction?: string;
  readonly error?: string;
  readonly updated_at?: string;
  /** The conversation type the CURRENT run is appraising on — routes the live view. */
  readonly turn_mode?: string;
}

/** One audited prompt-lineage entry (newest-first list per island, v3 snapshot). */
export interface OptimizerV3PromptHistoryEntry {
  readonly iteration_index: number;
  readonly accepted: boolean;
  readonly challenger_score: number | null;
  readonly challenger_instruction: string | null;
  readonly champion_instruction: string | null;
  readonly prompt_diff: string | null;
}

/** v3 per-island progress: the v2 shape + the prompt lineage. */
export interface OptimizerV3IslandProgress extends OptimizerV2IslandProgress {
  readonly prompt_history?: readonly OptimizerV3PromptHistoryEntry[];
}

/** Per-model status block in v3 (matches bakeoff/app.py optimizer_v3_snapshot). */
export interface OptimizerV3ModelStatus {
  readonly islands: readonly OptimizerV3IslandProgress[];
  readonly tournament_rounds: readonly OptimizerV2StatusRound[];
  readonly run_state?: OptimizerV3RunState;
  readonly error?: string;
}

/** SSE `optimizer_scoring_progress` — one judged conversation inside a live pass. */
export interface OptimizerScoringProgress {
  readonly model_channel: string;
  readonly island_id: number;
  readonly rung_index: number;
  readonly role: string; // "champion" | "challenger"
  readonly done: number;
  readonly total: number;
  readonly item_id: string;
  readonly rep: number;
  readonly conversation_mean: number;
}

/** GET /api/quality/optimize/v3/status — v3 lifecycle + durable backfill + sentinel. */
export interface OptimizerV3Status {
  readonly status: string; // "idle" | "running" | "completed" | "failed"
  readonly request: unknown;
  readonly error: string | null;
  readonly started_at: string | null;
  readonly finished_at: string | null;
  readonly models: Readonly<Record<string, OptimizerV3ModelStatus>>;
}

/** SSE `optimizer_iteration_skipped` — a contained, skipped v3 iteration. */
export interface OptimizerIterationSkipped {
  readonly model_channel: string;
  readonly island_id: number;
  readonly iteration_index: number;
  readonly rung_index: number;
  readonly reason: string;
  readonly error: string | null;
  readonly survivors: number | null;
  readonly total: number | null;
  readonly failures: readonly OptimizerConversationFailure[];
  readonly consecutive_failures: number;
}

/** SSE `optimizer_conversation_failed` — one contained conversation failure. */
export interface OptimizerConversationFailure {
  readonly model_channel?: string;
  readonly island_id?: number;
  readonly rung_index?: number;
  readonly item_id: string;
  readonly rep: number;
  readonly stage: string;
  readonly error: string;
}

/** SSE `optimizer_island_dead` — an island exhausted its consecutive-failure budget. */
export interface OptimizerIslandDead {
  readonly model_channel: string;
  readonly island_id: number;
  readonly consecutive_failures: number;
}

// --- Ragas eval visualization dashboard (design C1/C7/Data Models) ----------
//
// The eval feature visualizes N >= 3 agents across latency + a configurable
// composite of ragas-style metrics. These shapes are the HTTP/SSE contract seam
// for that feature; the pure compute that consumes them lives under src/eval/.
// Judge-rework / catalog-growth insulation is preserved: metric names are open
// strings, never closed enums, and the catalog scope marking is data.

/** A ragas generation-quality metric name (open string; catalog is data, not a closed enum
 *  — judge-rework / catalog-growth insulation, mirroring the existing types.ts posture). */
export type RagasMetricName =
  | "faithfulness"
  | "answer_relevancy" // ragas "Response Relevancy"
  | "context_precision"
  | "context_recall"
  | "context_entities_recall"
  | "noise_sensitivity"
  | "context_relevance" // Nvidia family
  | "answer_accuracy" // Nvidia family
  | "response_groundedness" // Nvidia family
  | "factual_correctness"
  | "semantic_similarity"
  | (string & {}); // forward-compat: unknown catalog metrics still type

/** A retrieval-quality metric computed from gold links — kept DISTINCT from ragas. */
export type RetrievalMetricName = "precision_at_k" | "recall_at_k" | "ndcg_at_k";

/** A metric value plus the provenance that makes it reproducible (Req 1.2, 2.2). */
export interface MetricValue {
  /** Unit-interval score; null === unavailable for this instance (Req 1.4, 2.3, 3.5). */
  readonly value: number | null;
  /** True exactly when value === null (the explicit unavailable flag). */
  readonly unavailable: boolean;
  /** For retrieval metrics: the k used (Req 2.2). Absent for ragas metrics. */
  readonly k?: number;
  /** ragas version + Bedrock model id for ragas metrics (Req 1.2); absent for retrieval. */
  readonly ragas_version?: string;
  readonly bedrock_model_id?: string;
  /** Id of the prompt configuration that produced this ragas value (Req 16.6);
   *  absent for retrieval metrics and for values produced without a prompt store. */
  readonly prompt_config_id?: string;
}

/** Per-stage timings, kept separate from end-to-end latency (Req 7.3). */
export interface StageTimings {
  readonly retrieval_ms: number | null;
  readonly generation_ms: number | null;
  /** Any additional named stages the runner records (embed, rerank, …). */
  readonly extra_ms?: Readonly<Record<string, number>>;
}

/** The atomic plotted unit. One Instance === exactly one of these (P4 bijection). */
export interface EvalInstance {
  /** Stable unique id — the dedupe + bijection key for rendered points (P4). */
  readonly instance_id: string;
  readonly agent_id: string; // Agent_Under_Test (N >= 3 supported)
  readonly session_id: string; // Session (the progression group)
  readonly instance_index: number; // strictly increasing within a Session (Req 7.4)
  readonly timestamp: string; // ISO-8601 capture time
  /** End-to-end response time (ms); the X-axis (log) signal. Validated > 0 (P7). */
  readonly latency_ms: number;
  readonly stage_timings: StageTimings;
  readonly corpus_size: number; // the Corpus_Size_Sweep axis (Req 6)
  readonly retrieval_cached: boolean; // cold vs cached never conflated (Req 7.5)
  /** Generation-quality (ragas). Keyed by RagasMetricName; values validated 0..1. */
  readonly ragas: Readonly<Record<string, MetricValue>>;
  /** Retrieval-quality (gold-link). Kept DISTINCT from ragas (Req 2.4 / P9). */
  readonly retrieval: Readonly<Record<string, MetricValue>>;
  /** Bubble-size source candidates (Req 10.5): confidence/volume/cost. */
  readonly confidence: number | null; // e.g. reranker relevanceScore proxy
  readonly volume: number | null; // e.g. tokens / fragments
  readonly cost: number | null; // e.g. estimated $ or token cost
  readonly prompt_id: string | null; // Control_Panel filter (Req 12.4)
  readonly category: string | null; // Control_Panel filter (Req 12.4)
  /** Failure status — a failed execution is still a recorded Instance (Req 5.5, 1.4). */
  readonly status: "ok" | "failed";
  readonly error: string | null;
}

/** Backs the metric menu (Req 4). In-scope/out-of-scope marking is DATA so the
 *  catalog can grow without code changes; every entry is external methodology (P13). */
export interface MetricCatalogEntry {
  readonly name: string; // RagasMetricName | RetrievalMetricName
  readonly family:
    | "rag"
    | "nvidia"
    | "nl-comparison"
    | "traditional"
    | "general"
    | "retrieval"
    | "multimodal"
    | "agentic"
    | "sql";
  readonly scope: "in" | "out"; // out-of-scope excluded from default enabled set (Req 4.4)
  readonly priority: number; // prioritized menu ordering (Req 4.1, 4.2)
  readonly customizablePrompt: boolean; // surfaced by the Prompt_Manager (Req 16)
  readonly external: true; // every catalog metric is external methodology (Req 4.6 / P13)
}

/** GET /api/eval/status — the durable-backfill authority. Empty-but-well-formed
 *  before any run; enough to fully reconstruct every view without the stream (Req 8.3, 15.2). */
export interface EvalStatus {
  readonly status: "idle" | "running" | "completed" | "failed";
  readonly error: string | null;
  readonly started_at: string | null;
  readonly finished_at: string | null;
  readonly agents: readonly string[];
  readonly sessions: readonly string[];
  readonly corpus_sizes: readonly number[];
  readonly instance_count: number;
  /** Either the instances themselves (windowed) or per-agent/per-corpus rollups. */
  readonly instances?: readonly EvalInstance[];
  readonly sweep_progress?: readonly EvalSweepProgress[];
}

/** event: eval_instance_appended — exactly one per appended EvalInstance (Req 15.1). */
export interface EvalInstanceAppended {
  /** The full record (or a compact projection sufficient to plot) — keyed by instance_id. */
  readonly instance: EvalInstance;
}

/** event: eval_run_status — lifecycle transitions (idle/running/completed/failed). */
export interface EvalRunStatusEvent {
  readonly status: "idle" | "running" | "completed" | "failed";
  readonly error: string | null;
}

/** event: eval_sweep_progress — corpus-size-sweep progress (optional, for the curve view). */
export interface EvalSweepProgress {
  readonly corpus_size: number;
  readonly completed_instances: number;
  readonly planned_instances: number;
  readonly unavailable: boolean; // a corpus size that could not be prepared (Req 6.5)
}

/** One few-shot example for a ragas metric prompt (input -> output). */
export interface EvalPromptExample {
  readonly input: string;
  readonly output: string;
}

/** GET /api/eval/prompts row — catalog metadata + the metric's active prompt config (Req 16). */
export interface EvalPromptConfig {
  readonly name: string;
  readonly family: string;
  readonly scope: "in" | "out";
  /** Whether the metric exposes an editable prompt; false → render as not editable (Req 16.7). */
  readonly customizable: boolean;
  readonly external: boolean;
  /** The active config id recorded alongside produced values (Req 16.6). */
  readonly config_id: string;
  readonly version: number;
  readonly is_override: boolean;
  readonly instruction: string;
  readonly examples: readonly EvalPromptExample[];
  /** The ragas default, for reset-preview (Req 16.4). */
  readonly default_instruction: string;
  readonly default_examples: readonly EvalPromptExample[];
}

/** GET /api/eval/prompts response. */
export interface EvalPromptsResponse {
  readonly prompts: readonly EvalPromptConfig[];
  readonly error?: string;
}
