/**
 * Typed HTTP client for the GBBO backend (bakeoff/app.py).
 *
 * In dev, Vite proxies /api, /exec, /healthz to the FastAPI backend, so relative
 * URLs work from the browser's single origin. In prod the backend serves this
 * bundle at / and answers the same paths. No base URL is hard-coded.
 */
import type {
  AggregateResponse,
  BakeOffDiagnostics,
  BakeOffSessionsResponse,
  ControlAction,
  ControlResponse,
  ExecReport,
  ExecReportsList,
  HarnessHealth,
  JudgeStartBody,
  JudgeStatus,
  JudgeSummary,
  OptimizerHistory,
  OptimizerStatus,
  OptimizerV2Status,
  OptimizerV3Status,
  QualitySummary,
  RunSnapshot,
  StartRunBody,
  TrialCompleted,
} from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;
  constructor(status: number, detail: unknown, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function safeJSON(res: Response): Promise<unknown> {
  try {
    return await res.json();
  } catch {
    return null;
  }
}

async function getJSON<T>(url: string, signal?: AbortSignal): Promise<T> {
  const init: RequestInit = { headers: { accept: "application/json" } };
  if (signal) init.signal = signal;
  const res = await fetch(url, init);
  if (!res.ok) {
    const detail = await safeJSON(res);
    throw new ApiError(res.status, detail, `GET ${url} -> ${res.status}`);
  }
  return (await res.json()) as T;
}

async function sendJSON<T>(
  url: string,
  method: "POST" | "PATCH",
  body: unknown,
): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await safeJSON(res);
    throw new ApiError(res.status, detail, `${method} ${url} -> ${res.status}`);
  }
  return (await res.json()) as T;
}

/** GET /api/models — run status + per-model progress (idle if no active run). */
export function fetchModels(signal?: AbortSignal): Promise<RunSnapshot> {
  return getJSON<RunSnapshot>("/api/models", signal);
}

/**
 * GET /api/trials/recent — replay the most recent completed trials from the
 * durable outcomes log. The SSE stream has no replay buffer, so the dashboard
 * calls this once on load to seed its in-memory buffer from disk (otherwise a
 * page reload starts blank even though thousands of trials are on disk). The
 * shape matches the SSE `trial_completed` payload, so seeded events flow through
 * the identical code path.
 */
export function fetchRecentTrials(
  limit = 2000,
  signal?: AbortSignal,
): Promise<{ trials: readonly TrialCompleted[]; total: number }> {
  return getJSON<{ trials: readonly TrialCompleted[]; total: number }>(
    `/api/trials/recent?limit=${limit}`,
    signal,
  );
}

/**
 * GET /api/aggregate — live aggregates with cheap normal-approx CIs.
 * `groupBy` is repeated as multiple query params (the backend reads them as a
 * list); `filters` become equality query params (cohort axis or identity).
 */
export function fetchAggregate(
  params: {
    readonly metric: string;
    readonly groupBy: readonly string[];
    readonly filters?: Readonly<Record<string, string>>;
  },
  signal?: AbortSignal,
): Promise<AggregateResponse> {
  const q = new URLSearchParams();
  q.set("metric", params.metric);
  for (const dim of params.groupBy) q.append("group_by", dim);
  if (params.filters) {
    for (const [k, v] of Object.entries(params.filters)) q.set(k, v);
  }
  return getJSON<AggregateResponse>(`/api/aggregate?${q.toString()}`, signal);
}

/** GET /api/bakeoff/diagnostics — Bake-Off decision cockpit evidence. */
export function fetchBakeOffDiagnostics(signal?: AbortSignal): Promise<BakeOffDiagnostics> {
  return getJSON<BakeOffDiagnostics>("/api/bakeoff/diagnostics", signal);
}

export function fetchBakeOffSessions(signal?: AbortSignal): Promise<BakeOffSessionsResponse> {
  return getJSON<BakeOffSessionsResponse>("/api/bakeoff/sessions", signal);
}

export function createBakeOffSession(body: { label?: string; notes?: string }): Promise<BakeOffSessionsResponse> {
  return sendJSON<BakeOffSessionsResponse>("/api/bakeoff/sessions", "POST", body);
}

export function activateBakeOffSession(sessionId: string): Promise<BakeOffSessionsResponse> {
  return sendJSON<BakeOffSessionsResponse>(
    `/api/bakeoff/sessions/${encodeURIComponent(sessionId)}/activate`,
    "POST",
    {},
  );
}

export function updateBakeOffSession(
  sessionId: string,
  body: { label?: string; notes?: string; archived?: boolean },
): Promise<BakeOffSessionsResponse> {
  return sendJSON<BakeOffSessionsResponse>(
    `/api/bakeoff/sessions/${encodeURIComponent(sessionId)}`,
    "PATCH",
    body,
  );
}

/** GET /healthz — harness liveness. */
export function fetchHealth(signal?: AbortSignal): Promise<HarnessHealth> {
  return getJSON<HarnessHealth>("/healthz", signal);
}

/** GET /exec/reports — list materialized exec report plan-versions. */
export function fetchExecReports(signal?: AbortSignal): Promise<ExecReportsList> {
  return getJSON<ExecReportsList>("/exec/reports", signal);
}

/**
 * GET /exec/aggregate — the materialized exec report (cluster-bootstrap CIs).
 * Optionally pinned to a `planVersion`; the backend serves the newest otherwise
 * and returns 422 if any aggregate lacks a CI without being marked insufficient
 * (Property 10) — surfaced here as a typed ApiError the exec view renders calmly.
 */
export function fetchExecAggregate(
  planVersion?: string,
  signal?: AbortSignal,
): Promise<ExecReport> {
  const q = planVersion ? `?plan_version=${encodeURIComponent(planVersion)}` : "";
  return getJSON<ExecReport>(`/exec/aggregate${q}`, signal);
}

/** POST /api/control/{action} — pause / resume / abort the active run. */
export async function postControl(action: ControlAction): Promise<ControlResponse> {
  const res = await fetch(`/api/control/${action}`, { method: "POST" });
  if (!res.ok) {
    const detail = await safeJSON(res);
    // 409 => no active run; surface as a typed error the UI can render calmly.
    throw new ApiError(res.status, detail, `POST control/${action} -> ${res.status}`);
  }
  return (await res.json()) as ControlResponse;
}

/**
 * POST /api/run/start — kick off a flat fixed-rep run from the browser.
 *
 * The backend returns 202 + a RunSnapshot (same shape as GET /api/models) once
 * the run is launched, or 409 with `{ "detail": "a run is already active" }` if
 * a run is already running/paused. The 409 (and any other non-2xx) is surfaced
 * as a typed ApiError the Bake-Off view renders inline — mirroring postControl.
 */
export async function startRun(body: StartRunBody = {}): Promise<RunSnapshot> {
  const res = await fetch("/api/run/start", {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await safeJSON(res);
    // 409 => a run is already active; surface as a typed error the UI renders calmly.
    throw new ApiError(res.status, detail, `POST run/start -> ${res.status}`);
  }
  return (await res.json()) as RunSnapshot;
}

/** GET /api/judge/status — the deferred Phase-2 judge lifecycle + progress. */
export function fetchJudgeStatus(signal?: AbortSignal): Promise<JudgeStatus> {
  return getJSON<JudgeStatus>("/api/judge/status", signal);
}

/**
 * GET /api/judge/scores — per-model judge rollups + example verdicts.
 * Pass `refresh` to recompute from the judge-scores store on disk (e.g. right
 * after a pass finishes, or to pick up an out-of-band judge run).
 */
export function fetchJudgeScores(refresh = false, signal?: AbortSignal): Promise<JudgeSummary> {
  const q = refresh ? "?refresh=true" : "";
  return getJSON<JudgeSummary>(`/api/judge/scores${q}`, signal);
}

/**
 * POST /api/judge/start — kick off (or re-run) the deferred Phase-2 judge.
 *
 * Returns 202 + a JudgeStatus once launched, or 409 with
 * `{ "detail": "judging already in progress" }` if a pass is already running.
 * The judge only reads the clean outcomes store and writes its own separate
 * judge-scores store, so a re-run never touches the candidate decision data.
 */
export async function startJudge(body: JudgeStartBody = {}): Promise<JudgeStatus> {
  const res = await fetch("/api/judge/start", {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await safeJSON(res);
    throw new ApiError(res.status, detail, `POST judge/start -> ${res.status}`);
  }
  return (await res.json()) as JudgeStatus;
}

/**
 * GET /api/quality/summary — per-model, per-turn closeness for the Quality tab.
 * Reads the SEPARATE multi-turn quality study store (never the bake-off's), so
 * an empty result simply means the quality run has not been run yet.
 */
export function fetchQualitySummary(signal?: AbortSignal): Promise<QualitySummary> {
  return getJSON<QualitySummary>("/api/quality/summary", signal);
}

// --- Closed-loop prompt optimizer (additive; bakeoff/app.py Component 12) ----

/** Body for POST /api/quality/optimize/start (all fields optional; see app.py). */
export interface OptimizeStartBody {
  readonly backend?: "offline" | "live";
  readonly models?: readonly string[];
  readonly threshold?: number;
  readonly stop_limit?: number;
  readonly phase_a_reps?: number;
  readonly phase_b_reps?: number;
  readonly retrieval_backend?: "opensearch" | "local" | "fake";
  readonly force?: boolean;
}

/**
 * GET /api/quality/optimize/status — the optimizer run lifecycle plus per-model
 * phase/iteration/champion progress reconstructed from the durable stores. Empty
 * -but-well-formed before any optimizer run exists.
 */
export function fetchOptimizeStatus(signal?: AbortSignal): Promise<OptimizerStatus> {
  return getJSON<OptimizerStatus>("/api/quality/optimize/status", signal);
}

/**
 * GET /api/quality/optimize/history?model=... — the ordered prompt-version
 * history for one Target_Model (each version with its diff, triad score + CI, and
 * accept/reject), backing the Per_Model_View's ≥ 2-version lookback (Req 8.5).
 * The backend rejects an unknown model with 422 (surfaced as a typed ApiError).
 */
export function fetchOptimizeHistory(
  model: string,
  signal?: AbortSignal,
): Promise<OptimizerHistory> {
  return getJSON<OptimizerHistory>(
    `/api/quality/optimize/history?model=${encodeURIComponent(model)}`,
    signal,
  );
}

/**
 * POST /api/quality/optimize/start — launch the closed-loop optimizer as a
 * background task. Returns 202 + the optimizer status snapshot once launched, or
 * 409 (`optimizer already running`, live-vs-bakeoff contention, or author/judge
 * conflict) surfaced as a typed ApiError the Quality_Tab renders inline.
 */
export async function startOptimize(body: OptimizeStartBody = {}): Promise<OptimizerStatus> {
  const res = await fetch("/api/quality/optimize/start", {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await safeJSON(res);
    throw new ApiError(res.status, detail, `POST quality/optimize/start -> ${res.status}`);
  }
  return (await res.json()) as OptimizerStatus;
}

// --- Optimizer v2 (island/tournament; bakeoff/app.py C6, frozen contract) ----

/**
 * GET /api/quality/optimize/v2/status — the v2 island-tournament lifecycle plus
 * per-model island/round backfill. Empty-but-well-formed before any v2 run.
 */
export function fetchOptimizeV2Status(signal?: AbortSignal): Promise<OptimizerV2Status> {
  return getJSON<OptimizerV2Status>("/api/quality/optimize/v2/status", signal);
}

/**
 * POST /api/quality/optimize/v2/start — launch the v2 optimizer as a background
 * task. Returns 202 + the v2 status snapshot, or 409 (already running /
 * author-judge conflict) surfaced as a typed ApiError the v2 tab renders inline.
 */
export async function startOptimizeV2(body: OptimizeStartBody = {}): Promise<OptimizerV2Status> {
  const res = await fetch("/api/quality/optimize/v2/start", {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await safeJSON(res);
    throw new ApiError(res.status, detail, `POST quality/optimize/v2/start -> ${res.status}`);
  }
  return (await res.json()) as OptimizerV2Status;
}

/**
 * POST /api/quality/optimize/v2/resume — resume a failed v2 run from its last
 * durable checkpoint, preserving all previously computed island steps.
 * Returns 200 + snapshot if launched, 409 if already running or no prior request.
 */
export async function resumeOptimizeV2(): Promise<OptimizerV2Status> {
  const res = await fetch("/api/quality/optimize/v2/resume", {
    method: "POST",
    headers: { accept: "application/json" },
  });
  if (!res.ok) {
    const detail = await safeJSON(res);
    throw new ApiError(res.status, detail, `POST quality/optimize/v2/resume -> ${res.status}`);
  }
  return (await res.json()) as OptimizerV2Status;
}

/**
 * POST /api/quality/optimize/v2/reset — stop the active v2 run (if any), reset
 * lifecycle to idle, and clear the v2 stores so the next run starts clean.
 * Idempotent; returns 200 + the post-reset (idle/empty) snapshot.
 */
export async function resetOptimizeV2(): Promise<OptimizerV2Status> {
  const res = await fetch("/api/quality/optimize/v2/reset", {
    method: "POST",
    headers: { accept: "application/json" },
  });
  if (!res.ok) {
    const detail = await safeJSON(res);
    throw new ApiError(res.status, detail, `POST quality/optimize/v2/reset -> ${res.status}`);
  }
  return (await res.json()) as OptimizerV2Status;
}

// --- Optimizer V3 (hardened, live-only; /api/quality/optimize/v3/*) ----------

/** POST body for /api/quality/optimize/v3/start. v3 is LIVE-ONLY (no backend field). */
export interface OptimizeV3StartBody {
  readonly models?: readonly string[];
  readonly retrieval_backend?: "opensearch" | "local" | "fake";
  /** Which conversation type to appraise on: single-turn queries, multi-turn
   * conversations (default), or both. Single-turn is the lowest-noise appraisal. */
  readonly turn_mode?: "single" | "multi" | "both";
}

/** GET /api/quality/optimize/v3/status — the v3 lifecycle + durable backfill + sentinel. */
export function fetchOptimizeV3Status(signal?: AbortSignal): Promise<OptimizerV3Status> {
  return getJSON<OptimizerV3Status>("/api/quality/optimize/v3/status", signal);
}

/** POST /api/quality/optimize/v3/freeze — write (model, island)'s current champion
 * prompt into its seed file so the next run starts from it. 404 if no champion yet. */
export async function freezeV3Champion(
  model: string,
  island_id: number,
): Promise<{ frozen: boolean; model: string; island_id: number; path: string; chars: number }> {
  const res = await fetch("/api/quality/optimize/v3/freeze", {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify({ model, island_id }),
  });
  if (!res.ok) {
    const detail = await safeJSON(res);
    throw new ApiError(res.status, detail, `POST quality/optimize/v3/freeze -> ${res.status}`);
  }
  return (await res.json()) as {
    frozen: boolean; model: string; island_id: number; path: string; chars: number;
  };
}

/**
 * POST /api/quality/optimize/v3/start — launch the v3 hardened optimizer (live
 * backend only). 202 + snapshot, 409 (already running / author-judge conflict),
 * 422 (unknown model or a non-live backend) surfaced as a typed ApiError.
 */
export async function startOptimizeV3(body: OptimizeV3StartBody = {}): Promise<OptimizerV3Status> {
  const res = await fetch("/api/quality/optimize/v3/start", {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await safeJSON(res);
    throw new ApiError(res.status, detail, `POST quality/optimize/v3/start -> ${res.status}`);
  }
  return (await res.json()) as OptimizerV3Status;
}

/**
 * POST /api/quality/optimize/v3/resume — resume a v3 run from its durable
 * checkpoints (the sentinel skips completed phases; island records fast-forward
 * an incomplete Phase A). 200 + snapshot, 409 when running / nothing to resume.
 */
export async function resumeOptimizeV3(): Promise<OptimizerV3Status> {
  const res = await fetch("/api/quality/optimize/v3/resume", {
    method: "POST",
    headers: { accept: "application/json" },
  });
  if (!res.ok) {
    const detail = await safeJSON(res);
    throw new ApiError(res.status, detail, `POST quality/optimize/v3/resume -> ${res.status}`);
  }
  return (await res.json()) as OptimizerV3Status;
}

/**
 * POST /api/quality/optimize/v3/reset — stop the active v3 run (if any), reset
 * lifecycle to idle, and clear the v3 stores + run-state sentinel. Idempotent.
 */
export async function resetOptimizeV3(): Promise<OptimizerV3Status> {
  const res = await fetch("/api/quality/optimize/v3/reset", {
    method: "POST",
    headers: { accept: "application/json" },
  });
  if (!res.ok) {
    const detail = await safeJSON(res);
    throw new ApiError(res.status, detail, `POST quality/optimize/v3/reset -> ${res.status}`);
  }
  return (await res.json()) as OptimizerV3Status;
}

// --- Ragas eval visualization dashboard (additive; bakeoff/app.py eval seam) -
//
// These are ADDITIVE typed calls for the eval feature's own dedicated endpoints
// (`/api/eval/*`). They mirror the discipline of the optimizer-v2 calls above:
// a durable-backfill status read, a replay-seed read shaped like the SSE payload,
// and a start that surfaces 409 (already active) / 422 (unknown agent/metric) as
// a typed ApiError the eval views render inline. No existing export is modified.
import type { EvalInstance, EvalStatus } from "./types";

/** Body for POST /api/eval/runs/start (all fields optional; the backend validates). */
export interface EvalRunStartBody {
  /** Agent_Under_Test set (N >= 3). Unknown agents → 422. */
  readonly agents?: readonly string[];
  /** Enabled metric catalog selection. Unknown metrics → 422. */
  readonly metrics?: readonly string[];
  /** Ordered corpus-size sweep series (optional). */
  readonly corpus_sizes?: readonly number[];
  /** Session/grouping id for the run (optional; the backend may assign one). */
  readonly session_id?: string;
  /** Single corpus size for a non-sweep run (optional; defaults server-side). */
  readonly corpus_size?: number;
  /** Number of synthetic queries for a default-query run (optional). */
  readonly num_queries?: number;
  /**
   * On-demand combinatorial mode (Area F / Req 22). When `true`, the backend
   * relaxes the >= 3 agent floor to one-or-more, accepts retrieval-metric names
   * alongside ragas names, and produces one Instance per cartesian combination of
   * agents x corpus sizes x queries.
   */
  readonly on_demand?: boolean;
  /** Arbitrary query subset (ids) for an on-demand run (Req 22.5). */
  readonly query_ids?: readonly string[];
  /** Explicit confirmation for an over-threshold combinatorial pool (Req 22.12). */
  readonly confirm?: boolean;
}

/**
 * GET /api/eval/status — the durable-backfill authority for the eval feature.
 * Lifecycle (idle/running/completed/failed) plus enough reconstructable view
 * state (agents, sessions, corpus sizes, instance count, windowed instances /
 * rollups, sweep progress) to rebuild every view without the stream. Empty-but-
 * well-formed before any run and defensive against a malformed store, so the
 * poll is safe to call on an interval and never blanks the surface (P6).
 */
export function fetchEvalStatus(signal?: AbortSignal): Promise<EvalStatus> {
  return getJSON<EvalStatus>("/api/eval/status", signal);
}

/**
 * GET /api/eval/instances/recent — replay the most recent EvalInstance records
 * from the durable Event_Store. The SSE stream has no replay buffer, so the
 * dashboard calls this once on load to seed its in-memory buffer from disk (a
 * reload otherwise starts blank even though records are on disk). The shape
 * matches the `eval_instance_appended` payload's `instance`, so seeded records
 * flow through the identical merge path (dedupe by instance_id).
 */
export function fetchRecentEvalInstances(
  limit = 2000,
  signal?: AbortSignal,
): Promise<{ instances: readonly EvalInstance[]; total: number }> {
  return getJSON<{ instances: readonly EvalInstance[]; total: number }>(
    `/api/eval/instances/recent?limit=${limit}`,
    signal,
  );
}

/**
 * POST /api/eval/runs/start — launch a multi-agent run / corpus-size sweep over a
 * configured agent set (N >= 3) + metric selection. Returns 202 + the eval status
 * snapshot once launched, 409 if a run is already active, or 422 on an unknown
 * agent/metric — each non-2xx surfaced as a typed ApiError the eval views render
 * inline, mirroring startOptimizeV2.
 */
export async function startEvalRun(body: EvalRunStartBody = {}): Promise<EvalStatus> {
  const res = await fetch("/api/eval/runs/start", {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await safeJSON(res);
    throw new ApiError(res.status, detail, `POST eval/runs/start -> ${res.status}`);
  }
  return (await res.json()) as EvalStatus;
}

import type { EvalPromptConfig, EvalPromptsResponse } from "./types";

/**
 * GET /api/eval/prompts — the ragas-metric prompt catalog + each metric's active
 * prompt configuration (instruction + few-shot examples + config id + version),
 * backing the Prompt_Manager (Req 16.1, 16.7). Customizable metrics are editable;
 * non-customizable ones are flagged so the UI renders them as not editable.
 */
export function fetchEvalPrompts(signal?: AbortSignal): Promise<EvalPromptsResponse> {
  return getJSON<EvalPromptsResponse>("/api/eval/prompts", signal);
}

/** Body for PUT /api/eval/prompts/{metric}: set an override, or reset to default. */
export interface EvalPromptPutBody {
  readonly instruction?: string;
  readonly examples?: readonly { readonly input: string; readonly output: string }[];
  /** When true, reset the metric to its ragas default (Req 16.4). */
  readonly reset?: boolean;
}

/**
 * PUT /api/eval/prompts/{metric} — persist a named, versioned prompt override
 * (Req 16.3) or reset to the ragas default (Req 16.4). Returns the updated prompt
 * row (200), or a typed ApiError: 404 unknown metric, 422 non-customizable /
 * malformed body (Req 16.7). The new config id is what the Metric_Engine records
 * alongside values produced after the change; prior values are untouched.
 */
export async function putEvalPrompt(
  metric: string,
  body: EvalPromptPutBody,
): Promise<EvalPromptConfig> {
  const res = await fetch(`/api/eval/prompts/${encodeURIComponent(metric)}`, {
    method: "PUT",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await safeJSON(res);
    throw new ApiError(res.status, detail, `PUT eval/prompts/${metric} -> ${res.status}`);
  }
  return (await res.json()) as EvalPromptConfig;
}

// --- REAL eval run (prompt files × queries.jsonl over the live stack) ---------

export interface RealEvalProgress {
  readonly series?: string;
  readonly done: number;
  readonly total: number;
  readonly last_quality?: number | null;
  readonly last_latency_ms?: number | null;
}

export interface RealEvalStatus {
  readonly status: "idle" | "running" | "completed" | "failed";
  readonly error?: string | null;
  readonly summary?: Record<string, unknown> | null;
  readonly progress?: RealEvalProgress | null;
}

export interface RealEvalSeries {
  readonly key: string;
  readonly chars: number;
}

/** GET /api/eval/real/prompts — the prompt files (each a series) available to run. */
export function fetchRealEvalPrompts(
  signal?: AbortSignal,
): Promise<{ prompt_dir: string; series: readonly RealEvalSeries[]; error?: string }> {
  return getJSON("/api/eval/real/prompts", signal);
}

/** GET /api/eval/real/status — status of the live-stack eval run. */
export function fetchRealEvalStatus(signal?: AbortSignal): Promise<RealEvalStatus> {
  return getJSON<RealEvalStatus>("/api/eval/real/status", signal);
}

/** POST /api/eval/real/start — launch the real eval over `query_count` queries. */
export async function startRealEval(query_count: 100 | 200 | 500 | 1000): Promise<RealEvalStatus> {
  const res = await fetch("/api/eval/real/start", {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify({ query_count }),
  });
  if (!res.ok) {
    const detail = await safeJSON(res);
    throw new ApiError(res.status, detail, `POST eval/real/start -> ${res.status}`);
  }
  return (await res.json()) as RealEvalStatus;
}

/** POST /api/eval/real/stop — cooperatively stop the run (a later start resumes). */
export async function stopRealEval(): Promise<RealEvalStatus> {
  const res = await fetch("/api/eval/real/stop", {
    method: "POST",
    headers: { accept: "application/json" },
  });
  if (!res.ok) {
    const detail = await safeJSON(res);
    throw new ApiError(res.status, detail, `POST eval/real/stop -> ${res.status}`);
  }
  return (await res.json()) as RealEvalStatus;
}

/** POST /api/eval/real/wipe — truncate the metric data the dashboard reads. */
export async function wipeEvalData(): Promise<{ wiped: boolean; discarded: number }> {
  const res = await fetch("/api/eval/real/wipe", {
    method: "POST",
    headers: { accept: "application/json" },
  });
  if (!res.ok) {
    const detail = await safeJSON(res);
    throw new ApiError(res.status, detail, `POST eval/real/wipe -> ${res.status}`);
  }
  return (await res.json()) as { wiped: boolean; discarded: number };
}
