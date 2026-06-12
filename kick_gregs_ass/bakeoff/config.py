"""
Configuration for the model-bakeoff-harness.

This is a SEPARATE module from the repo's top-level ``config.py`` (the retrieval
backend's config). It is never imported by the backend and never modifies it.
Everything tunable for the bakeoff lives here so the experiment can be re-shaped
without touching logic.

Two design rules are encoded structurally here:

* **The sampling plan is data, not code (AD-6).** The values below
  (``DEFAULT_TEMPERATURE``, ``TARGET_CI_HALFWIDTH``, ``COMPOSITE_WEIGHTS``, ...)
  are *defaults/seeds*; the authoritative per-run values are written by the pilot
  into ``data/bakeoff/sampling_plan.json`` and consumed by the runner. Editing
  the experiment is editing that file or re-running the pilot, not editing code.
* **Local-only, throwaway posture (Req 15).** Paths live under ``data/bakeoff/``;
  the web app binds to loopback; no new secrets are introduced (the Bedrock
  credential chain and region are reused from the existing backend ``config``).

Kept import-light on purpose (pure stdlib: ``pathlib`` + ``typing``) so importing
``bakeoff.config`` pulls in no heavy dependencies.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Repository / data roots
# ---------------------------------------------------------------------------
# Repo root = two levels up from this file (bakeoff/config.py -> repo/).
REPO_ROOT: Path = Path(__file__).resolve().parent.parent

# The synthetic dataset the harness evaluates against (read-only; never modified).
DATASET_DIR: Path = REPO_ROOT / "data" / "synthetic"

# All harness outputs live under data/bakeoff/ (Req 8.6).
BAKEOFF_DIR: Path = REPO_ROOT / "data" / "bakeoff"
BAKEOFF_SESSIONS_DIR: Path = BAKEOFF_DIR / "sessions"
BAKEOFF_SESSIONS_MANIFEST_PATH: Path = BAKEOFF_SESSIONS_DIR / "manifest.json"
BAKEOFF_ACTIVE_SESSION_PATH: Path = BAKEOFF_SESSIONS_DIR / "active_session.json"
BAKEOFF_UNIVERSAL_PROMPT_PATH: Path = REPO_ROOT / "data" / "prompts" / "XML_short.txt"

# --- canonical on-disk layout (design "On-disk layout") --------------------
SAMPLING_PLAN_PATH: Path = BAKEOFF_DIR / "sampling_plan.json"

# Two SEPARATE stores, by deliberate design (they are different *types* serving
# different masters — see docs and the runner):
#   * OUTCOMES_PATH — the DECISION data: only successful, completed trials (the
#     model's answer + latency + local quality components). This is the clean
#     source of truth aggregation reads; a failed attempt NEVER lands here, so
#     execution failures can never pollute the numbers we choose a model on.
#   * RUN_ERRORS_PATH — the disposable EXECUTION record: failed/errored trials
#     with their messages, for debugging a run. Aggregation never reads it; it is
#     safe to delete between runs (no decision value).
OUTCOMES_PATH: Path = BAKEOFF_DIR / "outcomes.jsonl"          # successes only (SoT)
RUN_ERRORS_PATH: Path = BAKEOFF_DIR / "run_errors.jsonl"      # disposable failures
# Phase-2 LLM-as-judge enrichment, keyed by trial_id; written by the deferred
# judge pass over a sampled subset of OUTCOMES (never in the generation loop).
JUDGE_SCORES_PATH: Path = BAKEOFF_DIR / "judge_scores.jsonl"

# Back-compat alias: older code/tests referenced a single combined event log.
# It now points at OUTCOMES_PATH (the successes store) so existing readers get
# the clean decision data, never the error rows.
TRIAL_EVENTS_PATH: Path = OUTCOMES_PATH
PILOT_EVENTS_PATH: Path = BAKEOFF_DIR / "pilot_events.jsonl"   # pilot trials only

CACHE_DIR: Path = BAKEOFF_DIR / "cache"
RETRIEVAL_CACHE_DIR: Path = CACHE_DIR / "retrieval"   # optional /retrieve mirror
JUDGE_CACHE_DIR: Path = CACHE_DIR / "judge"           # judge-score cache
EMBEDDINGS_CACHE_DIR: Path = CACHE_DIR / "embeddings"  # semantic-sim embed cache

REPORTS_DIR: Path = BAKEOFF_DIR / "reports"           # materialized aggregates


# ---------------------------------------------------------------------------
# Phase-2 deferred judge (LLM-as-judge runs AFTER generation, on a subset)
# ---------------------------------------------------------------------------
# The judge (Opus 4.x) is SLOW and TPM-limited and must never run in the
# generation hot loop (it would risk not finishing the full run and would couple
# candidate-data collection to grader fragility). It runs as a separate Phase 2
# over a stratified SAMPLE of the outcomes. This default sizes the sample to the
# owner's target of 300 judged items per model across the 2-model inline roster:
#   judged_records ≈ JUDGE_SAMPLE_ITEMS_PER_MODEL × n_models
# With 300 items/model × 2 models = 600 judged records (each record internally
# runs JUDGE_SAMPLES_K debiased samples). Override per-invocation.
JUDGE_SAMPLE_ITEMS_PER_MODEL: int = 300


def ensure_dirs() -> None:
    """Create the on-disk layout under ``data/bakeoff/`` if it does not exist.

    Idempotent. Callers (the runner, the planner, the scorers) invoke this lazily
    so that merely importing the config never touches the filesystem.
    """
    for d in (
        BAKEOFF_DIR,
        BAKEOFF_SESSIONS_DIR,
        CACHE_DIR,
        RETRIEVAL_CACHE_DIR,
        JUDGE_CACHE_DIR,
        EMBEDDINGS_CACHE_DIR,
        REPORTS_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Retrieval substrate (the held constant) — Req 2
# ---------------------------------------------------------------------------
# The harness talks to the existing backend over HTTP; it never re-implements
# retrieval. These point at the locally-running backend server. The backend
# serves on :8080 (run.sh + README); keep this in sync with it.
RETRIEVE_BASE_URL: str = "http://127.0.0.1:8080"
RETRIEVE_ENDPOINT: str = "/retrieve"
HEALTHZ_ENDPOINT: str = "/healthz"
RETRIEVE_TIMEOUT_S: float = 60.0

# ---------------------------------------------------------------------------
# Per-resource asyncio concurrency caps (AD-3)
# ---------------------------------------------------------------------------
# Separate bounded semaphores per downstream resource so each backend's rate
# limit is respected independently. The judge is the most expensive/limited, so
# it gets the smallest cap. CPU-bound scoring is offloaded via asyncio.to_thread.
CONCURRENCY_CAPS: dict[str, int] = {
    # Sized for the multi-account optimizer (each role on its OWN account, so these
    # per-resource caps no longer contend with each other for a single account's quota):
    #   * model  — target generation on the dedicated EXECUTION account: up to
    #     6 conversations/island × 4 islands = 24 concurrent (split across the two
    #     target models' independent per-model quota pools, ~12 each).
    #   * judge  — the Opus judge on its OWN dedicated account; raised from 4 now that it
    #     no longer shares Opus quota with anything else. Tunable to that account's TPM.
    "model": 24,     # candidate model generation endpoints (EXECUTION account)
    "judge": 8,      # LLM-as-judge endpoint (dedicated JUDGE account)
    "embed": 8,      # embedding endpoint (semantic similarity)
    "retrieve": 8,   # /retrieve substrate (memoized, so usually cheap)
}

# Size of the thread pool used to offload CPU-bound scoring (nDCG math, etc.)
# off the event loop. None lets asyncio.to_thread use its default executor.
CPU_SCORING_MAX_WORKERS: int | None = None

# Max worker threads for the event loop's DEFAULT executor — the pool every blocking
# boto3 call (target generation, Opus judge, Embed v4, AOSS retrieve) runs in via
# ``asyncio.to_thread``. Python's default is ``min(32, cpu_count + 4)`` — on this box
# that was only 18, so the semaphores (model 24 + judge 8 + embed 8 + retrieve 8 = 48)
# could NEVER all be in flight: blocking calls queued on ~18 threads and generation
# starved (observed: model_cap=24 but effective concurrency ~10 once the judge took its
# slots). These calls are I/O-bound (parked on a Bedrock socket), so threads ≫ cores is
# correct — size the pool to comfortably exceed the summed semaphore budget so each
# resource actually reaches its cap. Installed on the running loop at app startup.
BLOCKING_IO_MAX_WORKERS: int = 64

# ---------------------------------------------------------------------------
# Credential-expiry resilience (first-class concern; CROSS-CUTTING surface)
# ---------------------------------------------------------------------------
# This run can outlive a short-lived STS/Bedrock session: a full WIDE+DEEP run
# across several candidate models is hours of wall-clock, and the underlying
# credentials may expire partway through. Rather than crashing a multi-hour run,
# every Bedrock-touching client (model adapters task 5, semantic/judge scorers
# tasks 6/7) and the runner (task 10) classify a failed call and, when it looks
# like expired/invalid credentials, refresh the credential chain and retry the
# affected call up to a bounded number of times.
#
# TASK 1 ESTABLISHES THE CONFIG SURFACE ONLY. The classify-and-refresh logic is
# implemented by the consuming tasks; they read these constants and use the
# ``ErrorClass`` taxonomy in ``bakeoff.types`` to drive the decision. Nothing
# here performs a refresh.

# Maximum number of credential-refresh-then-retry cycles for a single call that
# keeps failing with an auth-expired signature. After this many refreshes the
# call is recorded as errored (the trial is logged with ``error`` set) and the
# run continues / can resume later. Kept small: a refresh that does not fix the
# auth error is a configuration problem, not a transient one.
AUTH_MAX_REFRESH_CYCLES: int = 8

# Exponential backoff between refresh+retry cycles, in seconds:
#   delay(attempt) = min(AUTH_BACKOFF_BASE_S * 2**attempt, AUTH_BACKOFF_MAX_S)
# A little jitter should be added by the consumer to avoid thundering-herd when
# many concurrent in-flight calls all hit expiry at once.
AUTH_BACKOFF_BASE_S: float = 1.0
AUTH_BACKOFF_MAX_S: float = 30.0

# Backoff for the non-auth retryable classes (throttling / transient). Separate
# from the auth backoff because throttling typically wants a longer, jittered
# wait and never triggers a credential refresh.
RETRY_MAX_ATTEMPTS: int = 5
RETRY_BACKOFF_BASE_S: float = 0.5
RETRY_BACKOFF_MAX_S: float = 20.0

# Error signatures that classify a failure as EXPIRED/INVALID CREDENTIALS
# (ErrorClass.AUTH_EXPIRED) and therefore warrant a credential refresh + retry.
# Consumers match case-insensitively against the exception/class name, the
# botocore error ``Code``, and the string form of the error.
AUTH_EXPIRED_ERROR_CODES: frozenset[str] = frozenset(
    {
        # botocore/Bedrock client error codes (response["Error"]["Code"]):
        "ExpiredTokenException",
        "ExpiredToken",
        "UnrecognizedClientException",   # token not yet/no longer recognized
        "InvalidClientTokenId",
        "InvalidSignatureException",     # often a clock/cred drift symptom
        "AccessDeniedException",         # creds present but no longer authorized
        "CredentialsError",
        "NoCredentialsError",
        "TokenRefreshError",
    }
)

# HTTP status codes that, on a model/judge/embedding endpoint call, indicate an
# auth problem worth a credential refresh + retry (ErrorClass.AUTH_EXPIRED).
AUTH_EXPIRED_HTTP_STATUSES: frozenset[int] = frozenset({401, 403})

# Substrings (matched case-insensitively in the error's string form) that also
# indicate expired/invalid credentials, for transports that do not surface a
# structured error code (e.g. a raw httpx response from a model gateway).
AUTH_EXPIRED_MESSAGE_SIGNATURES: tuple[str, ...] = (
    "expired token",
    "the security token included in the request is expired",
    "security token",
    "credentials",
    "unrecognizedclient",
    "invalidsignature",
    "not authorized to perform",
)

# Signatures that classify a failure as THROTTLING (ErrorClass.THROTTLED): a
# backoff + retry, never a credential refresh.
THROTTLE_ERROR_CODES: frozenset[str] = frozenset(
    {"ThrottlingException", "Throttling", "TooManyRequestsException", "LimitExceededException"}
)
THROTTLE_HTTP_STATUSES: frozenset[int] = frozenset({429})

# HTTP status codes treated as TRANSIENT (ErrorClass.TRANSIENT): backoff + retry.
TRANSIENT_HTTP_STATUSES: frozenset[int] = frozenset({500, 502, 503, 504})

# ---------------------------------------------------------------------------
# Sampling / statistics defaults (seeds for the pilot; authoritative values
# land in sampling_plan.json — AD-6, Req 6)
# ---------------------------------------------------------------------------
# Starting temperature; the pilot confirms or overrides it (design pilot step).
DEFAULT_TEMPERATURE: float = 0.2

# Target half-width for reported confidence intervals (drives required reps).
TARGET_CI_HALFWIDTH: float = 0.05

# Confidence level for every reported CI.
CONFIDENCE_LEVEL: float = 0.95

# Reps floor per stratum (Req 6.4): >= 2 so within-item signal always exists.
MIN_REPS_PER_STRATUM: int = 2

# Pilot reps per (model, item) on the stratified subsample (Req 6.2).
PILOT_REPS: int = 10

# Number of bootstrap resamples for the item-level cluster bootstrap (Req 9.2).
BOOTSTRAP_N: int = 2000

# Fixed seed so aggregation is a pure, reproducible function of the log (Req 9.1).
BOOTSTRAP_SEED: int = 1729

# ---------------------------------------------------------------------------
# Aggregation engine (Task 11) — thin-cell + high-variance thresholds
# ---------------------------------------------------------------------------
# Minimum distinct items a cohort cell must contain for the engine to emit a
# confident CI. A cell with fewer distinct items (after sparse-cell collapse) is
# marked insufficient-data rather than rendered as a confident value (Req 9.8,
# Req 13.4, design Error Scenario 4 / Property 10). Two is the smallest count for
# which a between-item spread is even defined; the bootstrap of a 1-item cell
# would be a degenerate point with a zero-width interval that masquerades as
# certainty, which is exactly the failure mode this guard prevents.
MIN_ITEMS_FOR_CI: int = 2

# Per-item rep-SD threshold above which an item is flagged "high variance" for
# the TARGETED pass (design tiered design: flagged items get extra reps). An item
# whose within-item rep SD on the target metric exceeds this is individually
# unstable and most likely to flip a decision, so it earns extra reps. This is a
# default seed; the authoritative value can be carried in the sampling plan.
HIGH_VARIANCE_REP_SD_THRESHOLD: float = 0.15

# Retrieval top_k / candidate_n requested from the substrate. Left as None to
# defer to the backend's own config defaults; set to pin them per run.
RETRIEVE_TOP_K: int | None = None
RETRIEVE_CANDIDATE_N: int | None = None
# k used for ranking metrics (precision@k / recall@k / nDCG@k) when computed.
SCORING_K: int = 5

# ---------------------------------------------------------------------------
# Composite quality weights (transparent; stored in the plan — Req 4.6)
# ---------------------------------------------------------------------------
# A transparent dict over quality components. The owner's decision: trust the
# judge model (Opus 4.8) on substance and nothing else, so the composite is the
# THREE judge dimensions only, with faithfulness (no fabrication) weighted most.
# The CPU cross-checks (grounding, semantic_similarity) are still computed and
# stored as components but no longer enter the decision composite. Weights sum to
# 1.0; compute_composite normalizes over whatever keys are present + nonzero.
COMPOSITE_WEIGHTS_VERSION: str = "judge-3dim-v1"
COMPOSITE_WEIGHTS: dict[str, float] = {
    "faithfulness": 0.50,   # judge: every claim grounded — the cardinal signal
    "correctness": 0.30,    # judge: an SME would call it correct
    "completeness": 0.20,   # judge: fully answers what is answerable
}

# ---------------------------------------------------------------------------
# Models — judge and candidate registry (Bedrock; reuses backend region/creds)
# ---------------------------------------------------------------------------
# Region is reused from the backend's posture (us-west-2 -> cross-region
# inference profiles). No new secrets; the Bedrock credential chain is shared.
AWS_REGION: str = "us-west-2"

# The judge model is held FIXED and MUST NOT be one of the candidates, to avoid
# self-preference bias (design Layer C / Req 4.5). Per the owner's lock the judge
# is Claude Opus 4.8 (preferred) with Opus 4.6 as the fallback; both are verified
# invocable in this account (948580600005, us-west-2) and neither is one of the six
# candidates, so the judge != candidate assertion below still holds. 4.8 is present
# in the account's inference-profile list, so it is the active judge; the fallback
# id is recorded for an operator who needs to drop down a tier.
JUDGE_MODEL_ID: str = "us.anthropic.claude-opus-4-8"
#: Fallback judge if the preferred judge is unavailable (operator override). Also
#: stronger than any candidate and not in the candidate set (no self-preference).
JUDGE_MODEL_ID_FALLBACK: str = "us.anthropic.claude-opus-4-6-v1"

# Number of judge samples per answer (k); pilot may override (Req 4.5).
JUDGE_SAMPLES_K: int = 3

# ---------------------------------------------------------------------------
# Inline-agent invocation (the secondary test method) — Bedrock Agent Runtime
# ---------------------------------------------------------------------------
# The inline-agent path (InvokeInlineAgent) wraps a candidate model in Bedrock's
# agent orchestration. By DEFAULT that orchestration injects tool-use / ReAct /
# action-group scaffolding that teaches the model it is a tool-calling agent —
# exactly the "extra stuff" we must NOT have. We suppress ALL of it by OVERRIDING
# the orchestration prompt with a minimal passthrough template (the pattern used by
# the internal AtoZAgoraAppChatIntakeService, verified live: the trace shows the
# model receives only our system + question, with zero tool/function/action-group
# text). No actionGroups and no knowledgeBases are ever attached, so there is
# nothing for the model to call. The template placeholders Bedrock fills:
#   $instruction$               -> our system instruction
#   $prompt_session_attributes$ -> our injected context (we pass the fragments here)
#   $question$                  -> our user input
#   $agent_scratchpad$          -> Bedrock's required assistant turn (left empty)
INLINE_AGENT_PROMPT_TEMPLATE: str = (
    '{\n'
    '    "anthropic_version": "bedrock-2023-05-31",\n'
    '    "system": "$instruction$",\n'
    '    "messages": [\n'
    '        {\n'
    '            "role": "user",\n'
    '            "content": [{"type": "text", "text": "<context>\\n$prompt_session_attributes$\\n</context>\\n\\n$question$"}]\n'
    '        }\n'
    '    ]\n'
    '}'
)

# InvokeInlineAgent enforces a minimum length on the ``instruction`` field
# (observed live: ValidationError "valid min length: 40"). Our family system
# instructions are well over this, but the adapter pads defensively to be safe.
INLINE_AGENT_MIN_INSTRUCTION_CHARS: int = 40

# Embedding model for semantic similarity — the SAME Embed v4 substrate the
# retrieval backend uses, for consistency (design Layer B / Req 4.2).
EMBED_MODEL_ID: str = "us.cohere.embed-v4:0"

# ---------------------------------------------------------------------------
# Extended-thinking defaults (Bedrock Claude reasoning) — per-candidate overridable
# ---------------------------------------------------------------------------
# Extended thinking on Bedrock Claude is enabled by passing
# ``additionalModelRequestFields={"thinking": {"type": "enabled",
# "budget_tokens": <N>}}`` to converse/converse_stream; omitting it (the default)
# disables it. Two constraints come bundled with enabling thinking (general
# Anthropic/Bedrock guidance, NOT Amazon-internal):
#
#   1. ``max_tokens`` INCLUDES the thinking budget and is a strict limit, so it
#      MUST exceed ``budget_tokens`` and leave room for the visible answer.
#   2. When thinking is enabled Anthropic requires ``temperature == 1.0`` (a
#      custom temperature is rejected), so thinking-on candidates pin temperature
#      to 1.0 regardless of the per-trial temperature the runner passes.
#
# These are the DEFAULTS a thinking-on candidate inherits when it does not set its
# own ``budget_tokens`` / ``max_tokens``; they are not magic numbers scattered
# through the adapter. ``THINKING_FORCED_TEMPERATURE`` is the temperature a
# thinking-on candidate is forced to use.
THINKING_DEFAULT_BUDGET_TOKENS: int = 2048
# Answer headroom added on top of the thinking budget to form max_tokens. We keep
# the ANSWER allowance generous on purpose: max_tokens is a hard output cap, and
# clipping a model's answer mid-sentence would unfairly depress its quality score
# (the bound we actually want is on *thinking*, not on the user-facing answer). So
# the default thinking-on max_tokens is 2048 (budget) + 6000 (answer) = 8048.
THINKING_DEFAULT_ANSWER_TOKENS: int = 6000

# The non-thinking generation cap. Deliberately generous (NOT a tight 1024) so a
# longer-but-legitimate grounded answer is never truncated; FAQ answers are
# usually short, but we do not want to arbitrarily limit a model's quality by
# capping its output low. Raise further per-candidate via CandidateModel.max_tokens.
DEFAULT_MAX_TOKENS: int = 6000
# Anthropic requires temperature == 1.0 whenever extended thinking is enabled.
THINKING_FORCED_TEMPERATURE: float = 1.0    


# ---------------------------------------------------------------------------
# Thinking-knob resolution (the SINGLE source of truth, shared by the registry
# descriptor and the Bedrock adapter so they can never disagree about what a
# candidate actually sends to Converse)
# ---------------------------------------------------------------------------
def resolve_budget_tokens(thinking: bool, budget_tokens: Optional[int]) -> Optional[int]:
    """Reasoning budget actually used when thinking is on, else ``None``.

    Falls back to :data:`THINKING_DEFAULT_BUDGET_TOKENS` when thinking is on and no
    explicit budget was pinned. Returns ``None`` when thinking is off.
    """
    if not thinking:
        return None
    return budget_tokens or THINKING_DEFAULT_BUDGET_TOKENS


def resolve_max_tokens(
    thinking: bool, budget_tokens: Optional[int], max_tokens: Optional[int]
) -> int:
    """Generation cap actually sent to Converse.

    Explicit ``max_tokens`` wins. Otherwise thinking-on derives
    ``budget + THINKING_DEFAULT_ANSWER_TOKENS`` (so max_tokens always exceeds the
    thinking budget with answer headroom) and thinking-off uses
    :data:`DEFAULT_MAX_TOKENS`.
    """
    if max_tokens is not None:
        return max_tokens
    if thinking:
        budget = resolve_budget_tokens(thinking, budget_tokens) or THINKING_DEFAULT_BUDGET_TOKENS
        return budget + THINKING_DEFAULT_ANSWER_TOKENS
    return DEFAULT_MAX_TOKENS


def resolve_temperature(
    thinking: bool,
    temperature_override: Optional[float],
    requested: float,
    *,
    accepts_temperature: bool = True,
) -> Optional[float]:
    """Temperature actually invoked with for a trial, or ``None`` to OMIT it.

    The newest Claude models (Sonnet 4.6/4.5, Haiku 4.5) **deprecate the
    ``temperature`` parameter** and reject any value at the Converse API (observed
    at runtime as ``"`temperature` is deprecated for this model."``). There is no
    drop-in replacement sampling knob for them, so the correct call is to send no
    temperature at all and let the model use its own default sampling. A candidate
    flagged ``accepts_temperature=False`` therefore resolves to ``None`` (omit the
    field) — this short-circuits everything else, including the thinking force.

    For models that DO accept it (older models, e.g. Claude 3.5 Haiku) the prior
    precedence holds: thinking-on is forced to :data:`THINKING_FORCED_TEMPERATURE`
    (Anthropic rejects a custom temperature when extended thinking is enabled);
    otherwise a fixed per-candidate override wins; otherwise the runner's per-trial
    ``requested`` value is used. Pure / side-effect free so the adapter and any
    caller share one rule.
    """
    if not accepts_temperature:
        return None
    if thinking:
        return THINKING_FORCED_TEMPERATURE
    if temperature_override is not None:
        return temperature_override
    return requested


class CandidateModel:
    """Static descriptor for one registered candidate model.

    A plain class (not a dataclass) to keep this module import-light and to make
    the registry trivially extensible: adding a candidate is appending one entry
    to ``CANDIDATE_MODELS`` (mirrors the "adding a model touches nothing else"
    goal in Req 3).

    **Extended thinking is modeled per-candidate, not per-base-model.** "Thinking
    on" and "thinking off" of the same base Bedrock model are registered as two
    SEPARATE candidates (distinct ``name``, possibly the SAME ``bedrock_model_id``,
    different ``thinking`` flag), so they are invoked separately and show up as
    separate rows in every result. The candidate ``name`` — not the bedrock id —
    is what distinguishes them on ``TrialEvent.model``, so two candidates sharing
    one bedrock id is expected, not a bug. The thinking-specific knobs are optional
    overrides:

    * ``thinking`` — whether to enable extended thinking for this candidate.
    * ``budget_tokens`` — the reasoning budget when ``thinking`` is on; falls back
      to :data:`THINKING_DEFAULT_BUDGET_TOKENS`. Ignored when thinking is off.
    * ``max_tokens`` — the generation cap. When ``None`` it is derived: for a
      thinking-on candidate it is ``budget_tokens + THINKING_DEFAULT_ANSWER_TOKENS``
      (max_tokens must exceed the thinking budget and leave answer room); for a
      thinking-off candidate it is :data:`DEFAULT_MAX_TOKENS`.
    * ``temperature`` — a fixed temperature override. When ``None`` the candidate
      uses the per-trial temperature the runner passes. A thinking-on candidate is
      ALWAYS forced to :data:`THINKING_FORCED_TEMPERATURE` (1.0) regardless of this
      field or the runner's value, per Anthropic's constraint.

    The derived knobs are exposed as :meth:`effective_budget_tokens`,
    :meth:`effective_max_tokens`, and :meth:`resolve_temperature` so the single
    source of truth for "what does this candidate actually send" lives here (atop
    the module-level ``resolve_*`` helpers), not duplicated in the adapter.
    """

    def __init__(
        self,
        name: str,
        bedrock_model_id: str,
        *,
        family: Optional[str] = None,
        thinking: bool = False,
        budget_tokens: Optional[int] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        accepts_temperature: bool = False,
        method: str = "converse",
        enabled: bool = True,
    ):
        self.name = name
        self.bedrock_model_id = bedrock_model_id
        # How this candidate is invoked: "converse" (the Bedrock Converse streaming
        # API, the default) or "inline_agent" (Bedrock Agent Runtime
        # InvokeInlineAgent with an OVERRIDDEN orchestration template that strips
        # all agent/tool scaffolding — see bakeoff.adapters.inline_agent). The two
        # methods are SEPARATE candidates (separate names, separate result rows) so
        # the bake-off compares "same model via Converse vs via inline agent".
        self.method = method
        # The prompt FAMILY this candidate is prompted as (selects the per-family
        # system instruction in bakeoff.prompts). Distinct from ``name`` because
        # thinking-on and thinking-off share one family but are separate candidates,
        # and distinct from ``bedrock_model_id`` because the family is about prompt
        # dialect, not the wire id. Falls back to ``name`` (the prompts selector is
        # lenient and maps an unknown family to the default instruction).
        self.family = family or name
        self.thinking = thinking
        self.budget_tokens = budget_tokens
        self.max_tokens = max_tokens
        self.temperature = temperature
        # Whether this model accepts the ``temperature`` Converse parameter. The
        # newest Claude models (Sonnet 4.6/4.5, Haiku 4.5) DEPRECATED it and reject
        # any value, so they MUST send no temperature at all; older models (e.g.
        # Claude 3.5 Haiku) still accept it. Defaults to False — the safe default
        # for the current roster — so a newly-added modern candidate does not
        # silently 400; flip it to True only for a model verified to accept it.
        self.accepts_temperature = accepts_temperature
        self.enabled = enabled

    # -- derived, single-source-of-truth knobs ----------------------------
    def effective_budget_tokens(self) -> Optional[int]:
        """Reasoning budget actually sent when thinking is on, else ``None``."""
        return resolve_budget_tokens(self.thinking, self.budget_tokens)

    def effective_max_tokens(self) -> int:
        """Generation cap actually sent to Converse for this candidate."""
        return resolve_max_tokens(self.thinking, self.budget_tokens, self.max_tokens)

    def resolve_temperature(self, requested: float) -> Optional[float]:
        """Temperature this candidate actually invokes with, or ``None`` to omit it."""
        return resolve_temperature(
            self.thinking, self.temperature, requested,
            accepts_temperature=self.accepts_temperature,
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"CandidateModel(name={self.name!r}, "
            f"bedrock_model_id={self.bedrock_model_id!r}, "
            f"method={self.method!r}, "
            f"thinking={self.thinking}, enabled={self.enabled})"
        )


# The candidate registry — the locked roster. None of these may equal
# JUDGE_MODEL_ID (asserted below). Adding/removing a candidate is a one-line edit;
# nothing else in the system hard-codes a candidate name or count.
#
# Roster (owner decision): the active Bake-Off roster is the TWO inline-agent
# candidates below. PromptBench / Quality / Eval rosters are separate.
CANDIDATE_MODELS: list[CandidateModel] = [
    CandidateModel(
        "claude-sonnet-4.6-thinking-off-inline",
        "us.anthropic.claude-sonnet-4-6",
        family="sonnet-4.6",
        thinking=False,
        method="inline_agent",
    ),
    CandidateModel(
        "claude-haiku-4.5-inline",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        family="haiku-4.5",
        thinking=False,
        method="inline_agent",
    ),
]

# Enforce judge != candidate at import time (cheap, catches a misconfig early).
assert JUDGE_MODEL_ID not in {
    c.bedrock_model_id for c in CANDIDATE_MODELS
}, "JUDGE_MODEL_ID must not be one of the candidate models (self-preference bias)"

# ---------------------------------------------------------------------------
# Live monitoring / exec UI (local-only — Req 15.1)
# ---------------------------------------------------------------------------
# Bind to loopback only by default; the no-auth posture is valid ONLY for
# loopback binding (Req 15.1/15.2).
UI_HOST: str = "127.0.0.1"
UI_PORT: int = 8200


# ---------------------------------------------------------------------------
# Multi-turn QUALITY experiment (a SELF-CONTAINED, separate study) — own store,
# own process, own dashboard tab. NOT part of the speed/quality bake-off above.
# ---------------------------------------------------------------------------
# This is the owner's second study: take the multi-turn dataset and measure, per
# turn, how CLOSE each model's answer is to the correct answer — turn-1 against
# the gold fragments (or abstention-correctness when turn-1 is answerability
# "none"), and each later turn against that turn's ``wants`` (the only ground
# truth later turns carry; they have no gold). It is deliberately isolated from
# the bake-off: its own outcomes store, its own judge store, its own optimized
# prompts file, so it can never perturb (or be perturbed by) the bake-off run.
#
# Only TWO models are under test here (owner decision): Sonnet 4.6 thinking-OFF
# and Haiku 4.5. They are referenced by the keys below; the quality module
# resolves each to its Converse bedrock id + family from this single place.
QUALITY_MODELS: dict[str, dict[str, object]] = {
    "sonnet-4.6-thinking-off": {
        "bedrock_model_id": "us.anthropic.claude-sonnet-4-6",
        "family": "sonnet-4.6",
        "thinking": False,
        "accepts_temperature": False,
    },
    "haiku-4.5": {
        "bedrock_model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "family": "haiku-4.5",
        "thinking": False,
        "accepts_temperature": False,
    },
}

# Quality study on-disk layout (all SEPARATE from the bake-off stores so the two
# studies are independent and individually disposable). Lives under the same
# data/bakeoff/ root for convenience but never shares a file with the bake-off.
QUALITY_OUTCOMES_PATH: Path = BAKEOFF_DIR / "quality_outcomes.jsonl"       # successes (SoT)
QUALITY_RUN_ERRORS_PATH: Path = BAKEOFF_DIR / "quality_run_errors.jsonl"   # disposable failures
QUALITY_JUDGE_SCORES_PATH: Path = BAKEOFF_DIR / "quality_judge_scores.jsonl"  # per-turn judge
# The optimized system prompt chosen per model by the offline optimization pass,
# plus its score/rationale, so the quality run is reproducible from a recorded
# decision rather than a re-run of the optimizer.
QUALITY_PROMPTS_PATH: Path = BAKEOFF_DIR / "quality_prompts.json"
# The optimizer's full scored leaderboard (every variant, per-turn closeness),
# kept for the dashboard + audit of WHY a prompt was chosen.
QUALITY_OPTIMIZER_REPORT_PATH: Path = BAKEOFF_DIR / "quality_optimizer_report.json"

# Reps per (model, item) in the quality run. Quality (not latency) is the focus,
# so a small rep count captures within-item answer variance without a large
# budget. The optimizer uses its own (smaller) rep count on a held-out slice.
QUALITY_RUN_REPS: int = 3
# Fraction of the 300 multi-turn items held out as the optimizer's tuning slice
# (the prompt is tuned on this slice; the full set is run with the chosen prompt
# so the reported quality is not the same data the prompt was tuned on). The
# split is deterministic + seeded so it is reproducible.
QUALITY_OPTIMIZER_HELDOUT_FRACTION: float = 0.2
QUALITY_OPTIMIZER_SPLIT_SEED: int = 1729
# Reps per (model, item, variant) during optimization (kept low — the optimizer
# ranks variants, it does not need tight CIs).
QUALITY_OPTIMIZER_REPS: int = 2

# ---------------------------------------------------------------------------
# Closed-loop prompt OPTIMIZER (the champion/challenger study) — own config,
# own append-only stores, own minimal inline template. SEPARATE from both the
# bake-off above and the one-shot quality selector above.
# ---------------------------------------------------------------------------
# This block configures the closed-loop optimizer that REPLACES the one-shot
# quality prompt selector (the fixed five-variant menu ranked on cosine
# closeness). The optimizer runs a champion/challenger loop scored by the real
# Opus judge (``JUDGE_MODEL_ID`` above — reused, never re-defined here, so the
# author!=judge separation in backends.py compares against a single source of
# truth) over a held-out tuning slice, then validates the converged champion on
# the reserved complement at higher reps. Everything tunable for that loop lives
# here so the methodology can be re-shaped without touching loop logic.
#
# Methodology sourcing caveat (carried from requirements/design): the triad-as-
# signal decision, the noise-floor SD ≈ 0.24 / 0.05-threshold CI reasoning, and
# the inline persistent-session history behavior are grounded in external/
# industry RAG-eval practice, this repo's own observed Opus verdicts, and AWS
# *public* API docs — NOT Amazon-internal primary sources (which were
# unavailable when the design was set). Re-validate any judge-derived number
# against internal guidance before defending a decision upward.

# --- significance / promotion (Req 5) --------------------------------------
# Minimum ABSOLUTE judge-triad-score gain (challenger over current champion, on
# the 0.0-1.0 scale) that counts as a real improvement and promotes the
# challenger. At small rungs (n=18 scored conversations, CI ≈ ±0.095) a gain of
# 0.05 is well inside one CI half-width and virtually never fires — use 0.01 so
# rung-0 can actually produce promotions and escalate. The upper rungs
# (n=24–60, CI ≈ ±0.058–0.091) provide the real statistical filter. Configurable.
QUALITY_OPT_SIGNIFICANCE_THRESHOLD: float = 0.01

# --- convergence / stop rule (Req 6) ---------------------------------------
# Number of CONSECUTIVE non-improving iterations (a challenger that fails to beat
# the champion by at least the significance threshold, OR a non-usable/empty/
# identical challenger) after which Phase A stops and the current champion is
# marked converged. Default 5 (Req 6.4); configurable (Req 6.5).
QUALITY_OPT_STOP_LIMIT: int = 5

# --- failure selection (Req 3.4) -------------------------------------------
# How many of the lowest-scoring judged turns (with the judge's evidence) are
# handed to the Author each iteration to drive the rewrite. Configurable so the
# author can be given a wider or narrower view of the champion's worst turns.
QUALITY_OPT_FAILURES_K: int = 8

# --- per-phase rep counts (Req 5.8, 7.4) -----------------------------------
# Reps per (model, item) when SCORING a prompt on a slice. Phase A (the iterate
# phase) runs on the small ~20% tuning slice and uses fewer reps — it only needs
# to rank champion vs challenger, not publish a tight CI. Phase B (the validate
# phase) runs on the reserved ~80% complement and MUST use a higher rep count
# than Phase A (Req 7.4) so the final reported triad score carries a tighter CI.
# Invariant enforced below: QUALITY_OPT_PHASE_B_REPS > QUALITY_OPT_PHASE_A_REPS.
QUALITY_OPT_PHASE_A_REPS: int = 3
QUALITY_OPT_PHASE_B_REPS: int = 5

# --- deterministic train/test split (Req 7.6) ------------------------------
# Seed for the deterministic, stratified ``split_items`` that carves the dataset
# into the held-out Tuning_Slice (Phase A) and the reserved Validation_Set
# (Phase B). Echoes the existing one-shot ``QUALITY_OPTIMIZER_SPLIT_SEED`` (1729)
# so both studies reproduce the SAME split and the optimizer's reported number is
# never measured on data the prompt was tuned on.
QUALITY_OPT_SPLIT_SEED: int = QUALITY_OPTIMIZER_SPLIT_SEED

# ===========================================================================
# v2: coverage-ladder + island-tournament optimizer (docs/OPTIMIZER_V2_DESIGN_NOTES.md)
# ---------------------------------------------------------------------------
# v1 scored every attempted prompt against the full 180-conversation tuning slice,
# so the first score took many minutes and cycles were far too slow. v2 evaluates
# a prompt against a SMALL INDICATIVE rung first, iterates fast, and only escalates
# coverage once a prompt earns it (successive-halving / Hyperband). Two islands per
# model evolve divergent prompts independently; when both are confident, a
# tournament on a shared higher rung picks a winner that becomes the new baseline
# for BOTH islands. Numbers below are adaptive/config-driven — the IDEA (fast early,
# slower as confidence grows) matters, not the specific counts.
#
# Sourcing honesty: Hyperband/successive-halving, island-model coevolution, and
# tournament selection are EXTERNAL/industry techniques, not Amazon-internal
# guidance — same posture as the rest of this spec's methodology.

# Coverage ladder: target ITEM counts per rung (ascending). Rungs are nested,
# stratified subsets of the held-out tuning slice (never new data). The final rung
# is always clamped to the full tuning slice (~60 items) regardless of this list,
# and sizes >= the slice size collapse into it. At SD~0.228 the CI half-width is
# ~0.10 at 20 conversations and ~0.05 only near ~80, so the small rungs ELIMINATE
# clearly-worse prompts and the upper rungs SELECT between good ones.
QUALITY_OPT_RUNG_SIZES: tuple[int, ...] = (6, 12, 24, 40, 60)
# Reps per item at each rung. Front-loaded as a funnel (owner decision 2026-06-04):
# the small entry rungs run each item multiple times (6 items x3 reps = 18 scored,
# 12 items x2 reps = 24 scored) so a cheap rung still has enough conversations to be
# indicative, then reps drop to 1 as the item count itself grows to carry coverage.
# Last value is reused if there are more rungs than entries.
QUALITY_OPT_RUNG_REPS: tuple[int, ...] = (3, 2, 1, 1, 1)

# Islands per model (independent coevolution loops). 2 per the owner decision.
QUALITY_OPT_ISLANDS_PER_MODEL: int = 2

# Per-island author-style nudges (anti-over-convergence): each island appends a
# different stance to the author contract so the two islands pursue meaningfully
# DIFFERENT prompt shapes rather than collapsing to one line. Indexed by island.
# Per-island authoring STANCES — the divergence knob. V3 applies a stance ONLY on
# an island's very first authoring round (the kickoff): it steers the two islands
# into genuinely different starting shapes, after which each island optimizes
# freely (writes its best prompt however it sees fit) every subsequent round. The
# two stances are deliberately at OPPOSITE extremes of the prompt-design space so
# the islands explore as differently as possible while both staying high-quality.
QUALITY_OPT_ISLAND_STYLES: tuple[str, ...] = (
    (
        "Island A stance — FEW WORDS, HIGH DENSITY. Your job is to say it in as few "
        "words as possible. Write information-dense, not prosaic: terse, high-signal "
        "lines that pack the most meaning per word, rather than flowing explanatory "
        "prose. Compressed formats are welcome where they help — short bullet points, "
        "clipped imperatives, sentence fragments. Trust the model to generalize from "
        "clear principles instead of spelling out every case, and cut anything a capable "
        "model would already infer. Use whatever structure you like; just keep it lean. "
        "Economy is the whole point — every word must earn its place."
    ),
    (
        "Island B stance — RICHLY EXPLICIT & STRUCTURED. Write a thorough, sectioned "
        "'constitution'-style instruction: clearly delineated XML-tagged sections "
        "(role, grounding rules, an abstention/refusal procedure, an answerability "
        "check, multi-turn handling, tone & formatting), each with concrete, itemized, "
        "step-by-step guidance and at least one short illustrative example of the "
        "desired behavior (e.g. exact decline phrasing). Leave as little to inference "
        "as possible — anticipate edge cases and name them explicitly. Favor "
        "completeness and unambiguous coverage over brevity."
    ),
)

# Escalation gate (hybrid). A prompt graduates from rung k to rung k+1 when it is
# NOT significantly worse than the rung's incumbent baseline — i.e. its triad is
# within this many CI half-widths below the baseline (elimination, not selection).
# The model-judgment half of the hybrid (the author/loop deciding a prompt is
# "worth more coverage") rides on top of this floor.
QUALITY_OPT_ESCALATION_CI_SLACK: float = 1.0
# Max author iterations an island runs at a single rung before it must either
# escalate (if it has a candidate that passed the gate) or be declared stuck.
# 2 steps at rung-0 (~40 min) is enough to determine the prompt isn't improving
# there; stuck islands force a tournament then re-try from the winner's prompt.
QUALITY_OPT_ISLAND_RUNG_PATIENCE: int = 2

# Tournament: run an island-vs-island head-to-head once both islands have a
# candidate that reached at least this rung index, OR after this many total island
# iterations, whichever first. The head-to-head is scored on a shared rung at least
# this size so the 0.05 winner test is actually resolvable.
QUALITY_OPT_TOURNAMENT_MIN_RUNG: int = 2
QUALITY_OPT_TOURNAMENT_EVERY_ITERS: int = 6
# Number of tournament rounds (migrate-and-diverge cycles) before the surviving
# champion per model is frozen and handed to Phase B validation on the full set.
QUALITY_OPT_TOURNAMENT_ROUNDS: int = 3

# Author model for v2: Sonnet 4.6 with ADAPTIVE THINKING (NOT Opus). The big
# model's learning is already encoded in the failure evidence + baked
# Prompting_Guidance handed to the author, so authoring is transmission not
# discovery. Judge stays Opus (config.JUDGE_MODEL_ID); author != judge still holds.
# Resolved from QUALITY_MODELS so the id lives in one place; thinking is requested
# via the adapter when this flavor is selected.
QUALITY_OPT_V2_AUTHOR_MODEL_KEY: str = "sonnet-4.6-thinking-on"


# --- retrieval-always posture (Req 13) -------------------------------------
# RETRIEVAL-ALWAYS by DEFAULT. The quality answer path now invokes the
# held-constant, read-only ``RetrievalBackend`` on EVERY turn and concatenates
# that turn's fragments INLINE into the visible question text (via
# ``assemble_context`` into ``$question$``) — the fragments are the model's only
# grounding and the SAME fragments are threaded into the judge as the
# faithfulness/grounding evidence (Req 13.7). The fragments are NEVER delivered
# via ``promptSessionAttributes`` or ``sessionAttributes`` (Req 13.4): the minimal
# OVERRIDDEN template has no ``$prompt_session_attributes$`` placeholder, so the
# only channel that reaches the model is the inline question text.
#
# This REVERSES the prior revision's fragment-free default. ``False`` survives
# ONLY as a diagnostic escape hatch (e.g. isolating a parsing bug); it is never
# the default and must not be used for a real study. Retrieval-always does NOT
# mean answer-always: the model MAY still decline when the fragments are
# insufficient (Req 13.8), and that correct abstention is rewarded (see below).
QUALITY_OPT_SEND_FRAGMENTS: bool = True

# How prior turns of a conversation reach the model under the OVERRIDDEN minimal
# template: "server" relies on Bedrock's server-side session history keyed to a
# stable ``sessionId`` (the owner-asserted behavior, validated by the live probe
# in Task 11.6); "explicit" is the AWS-doc-grounded fallback that replays prior
# turns via ``inlineSessionState.conversationHistory``. Default "server".
QUALITY_OPT_INLINE_HISTORY_MODE: str = "server"

# --- abstention as a primary, heavily-weighted behavior (Req 14) -----------
# Abstention-correctness (declining when the retrieved fragments are insufficient
# or the turn is unanswerable, and answering when they are sufficient) is a
# FIRST-CLASS, primary term in the per-turn ``overall`` the judge produces — not
# a separate metric (the judge remains the sole decision signal, Req 2). This
# weight controls how strongly that term dominates the per-turn aggregation:
# correct abstention on an insufficient/unanswerable turn scores near the top,
# and answering-when-unsure (an unsupported answer) is strongly penalized. With a
# weight of 0.5, abstention-correctness and the rest of the triad each contribute
# half of a turn's ``overall``, making abstention co-equal with substantive
# quality rather than a tie-breaker. Higher → abstention dominates more; the gap
# between a correct decline and an unsupported answer is non-decreasing in this
# weight (Property 27). Range (0.0, 1.0); configurable.
QUALITY_OPT_ABSTENTION_WEIGHT: float = 0.5

# --- confident-wrong hammer: faithfulness gate (owner priority 2026-06-10) ---
# A wrong answer delivered with false certainty is the MOST costly failure on
# this task — far worse than an incomplete answer or an honest decline — because
# a user trusts it. ``faithfulness`` is the judge's measure of "every claim is
# supported by the retrieved fragments"; a low value means the model asserted
# unsupported content (a fabrication / confident-wrong). When a turn's normalized
# faithfulness falls BELOW this floor, the turn's per-turn ``overall`` is capped
# at the faithfulness value itself (see judge_loop._judge_turn), so a fluent
# fabrication can NOT be averaged back up by completeness/correctness — it scores
# at the bottom regardless. This makes confident-wrong near-disqualifying while
# leaving genuinely grounded answers (and correct declines, which assert nothing
# and so score high faithfulness) untouched. Normalized scale: the judge's 1–5 maps
# to 0/0.25/0.5/0.75/1.0, so 0.5 == "any unsupported detail (judge ≤ 3/5) caps the
# turn." Raise toward 1.0 to punish even harder; lower to soften. Range [0, 1].
QUALITY_OPT_FAITHFULNESS_FLOOR: float = 0.5

# --- retrieval backend selection + ALPHA OpenSearch placeholders (Req 16) --
# Which held-constant, read-only ``RetrievalBackend`` the loop builds:
#   "opensearch" -> the ALPHA OpenSearch service (PREFERRED, Req 16.1), with an
#                   automatic fallback to the local backend if it is unreachable
#                   or onerous to use (Req 16.2);
#   "local"      -> the repo's ``POST /retrieve`` service (the guaranteed-workable
#                   fallback that lets the study run with no OpenSearch at all);
#   "fake"       -> the deterministic, network-free offline test double.
# All three return the same ``{id, text, metadata, ...}`` fragment shape (Req
# 16.4) and issue read-only queries only (Req 16.5); every backend is wrapped in a
# memoizing layer so champion and challenger see byte-identical fragments per
# (turn-query) (held-constant retrieval, Req 13.3).
QUALITY_OPT_RETRIEVAL_BACKEND: str = "opensearch"

# OpenSearch ALPHA connection facts for AWS account ``948580600005``. These are
# OWNER-PROVIDED operational assumptions, NOT verified against an Amazon-internal
# primary source in this environment — they MUST be confirmed with the owner at
# implementation time (Req 16.6). They are deliberately left ``None`` here and are
# INJECTED into ``OpenSearchRetrievalBackend`` rather than hard-coded, so nothing
# in the loop depends on a value baked into source. The guaranteed-workable
# ``LocalRetrievalBackend`` (``"local"``) exists precisely so the study never
# depends on these being filled in: if they are absent or the endpoint is
# unworkable, the selector falls back to local (Req 16.2).
QUALITY_OPT_OPENSEARCH_ENDPOINT: Optional[str] = None   # populated by build_live_backend from ALPHA constants below
QUALITY_OPT_OPENSEARCH_INDEX: Optional[str] = None      # populated by build_live_backend from ALPHA constants below
QUALITY_OPT_OPENSEARCH_AUTH: Optional[str] = None       # populated by build_live_backend from ALPHA constants below

# --- ALPHA OpenSearch Serverless connection facts (account 948580600005) -----
# These are the REAL values for the AOSS collection. build_live_backend reads
# them directly and injects a pre-signed client into the retrieval builder.
# Kept separate from the QUALITY_OPT_OPENSEARCH_* selector defaults so the
# retrieval builder's bare-call fallback path (no client, no endpoint -> local)
# still works for offline tests that exercise the unconfigured case.
QUALITY_OPT_OPENSEARCH_ALPHA_ENDPOINT: str = "https://3z3yxvl1s09ylso0dgh.us-west-2.aoss.amazonaws.com"
# faq_evidence_b is the LIVE index (56 docs; the SSM pointer target). The old
# default faq_evidence_a is a stale 4-doc leftover — runs grounded against it
# produce systematically depressed judge scores (observed 2026-06-10) and
# intermittent index_not_found 404s. Verified live before changing.
QUALITY_OPT_OPENSEARCH_ALPHA_INDEX: str = "faq_evidence_b"
QUALITY_OPT_OPENSEARCH_ALPHA_REGION: str = "us-west-2"
QUALITY_OPT_OPENSEARCH_ALPHA_SERVICE: str = "aoss"
QUALITY_OPT_OPENSEARCH_ALPHA_PROFILE: str = "alpha"

# --- Cohere Rerank v4.0 Pro endpoint (OPTIMIZER V2 ONLY) ----------------------
# A SageMaker real-time endpoint running Cohere Rerank v4.0 Pro (the reranker
# prod uses; Bedrock only carries 3.5) in the owner's PERSONAL AWS account
# ``429134228173`` (profile ``nick-caia``) — deliberately separate from the
# alpha credential chain everything else uses. The ml.g5.2xlarge quota landed
# in us-east-1, so the endpoint lives there; the cross-region hop from the
# optimizer is noise next to rerank inference time.
#
# Consumed ONLY by the v2 optimizer's live retrieval path: build_live_backend
# injects these into ``build_retrieval_backend``, which wraps the AOSS backend
# in a ``RerankedRetrievalBackend`` INSIDE the memoizing layer (so champion and
# challenger still see byte-identical fragments per (turn-query), Req 13.3, and
# a cache hit never re-invokes the endpoint). The selector's bare default is
# rerank-OFF, exactly like the QUALITY_OPT_OPENSEARCH_* placeholders above, so
# offline/fake paths and existing tests never touch the personal account.
QUALITY_OPT_RERANK_V4_ENDPOINT_NAME: str = "cohere-rerank-v4-pro"
QUALITY_OPT_RERANK_V4_REGION: str = "us-east-1"
QUALITY_OPT_RERANK_V4_PROFILE: str = "nick-caia"
# Candidates fetched from AOSS for the reranker to reorder (top_k comes out).
QUALITY_OPT_RERANK_V4_CANDIDATE_N: int = 20
# Per-document char cap sent to the reranker (mirrors the Bedrock Rerank 3.5
# inline-document cap used by src/bedrock_client.py; only the scored COPY is
# truncated — the full fragment text still flows downstream).
QUALITY_OPT_RERANK_V4_DOC_CHAR_LIMIT: int = 32000

# ---------------------------------------------------------------------------
# OPTIMIZER V3 (bakeoff/quality/optimizer/v3/) — the hardened, LIVE-ONLY rebuild
# of the v2 island-tournament loop. Everything v3 is namespaced here and writes
# to its OWN durable files so v2's data/config is never touched. Design points
# (from the v2 post-mortem, data/opt_v2_instrumented.log: both real runs died on
# an unhandled AOSS 403 at the ~1h token wall):
#   * every external call is guarded: hard timeout + classify/backoff/retry
#     (bakeoff.resilience) on TOP of the clients' internal auth healing;
#   * failures are CONTAINED: a failed turn fails its conversation, a failed
#     conversation is collated out (survivor scoring), a failed iteration is
#     skipped, a repeatedly-failing island is marked dead — the run never dies
#     from item-level errors;
#   * concurrency: models concurrent, islands concurrent (wave-stepped so
#     tournament semantics hold), conversations pipelined gen→judge per item
#     under per-resource semaphores (no global phase barrier).
# ---------------------------------------------------------------------------
QUALITY_OPT_V3_ITERATIONS_PATH: Path = BAKEOFF_DIR / "quality_opt_v3_iterations.jsonl"
QUALITY_OPT_V3_AUDIT_PATH: Path = BAKEOFF_DIR / "quality_opt_v3_audit.jsonl"
QUALITY_OPT_V3_RESULTS_PATH: Path = BAKEOFF_DIR / "quality_opt_v3_results.json"
QUALITY_OPT_V3_ERRORS_PATH: Path = BAKEOFF_DIR / "quality_opt_v3_errors.jsonl"
# Run-state sentinel (phase progress + island liveness) — what makes resume
# skip straight to Phase B instead of re-entering a completed Phase A.
QUALITY_OPT_V3_STATE_PATH: Path = BAKEOFF_DIR / "quality_opt_v3_state.json"

# Hard per-call timeouts (seconds). The v2 heartbeat data showed judge calls
# routinely >20s (530 SLOW warnings in one run), so the judge gets the widest
# window; a call past its window is cancelled and classified TRANSIENT (retried
# with backoff by the guard, then contained).
QUALITY_OPT_V3_TIMEOUT_MODEL_S: float = 120.0      # target-model generate (multi-turn)
# Wide enough for the fattest legitimate call (a turn-5 judge with full history)
# even when the global judge queue is busy — observed: the largest turn prompts
# blew a 90s component repeatedly while ordinary turns ran 20-50s.
QUALITY_OPT_V3_TIMEOUT_JUDGE_S: float = 300.0      # Opus score_detailed (one turn)
QUALITY_OPT_V3_TIMEOUT_RETRIEVAL_S: float = 45.0   # AOSS query (+ rerank stage)
QUALITY_OPT_V3_TIMEOUT_CLOSENESS_S: float = 45.0   # Embed v4 closeness
QUALITY_OPT_V3_TIMEOUT_AUTHOR_S: float = 240.0     # Sonnet author (streams a full rewrite)

# Collate-survivors policy: an iteration's score stands when at least this
# fraction of its conversations produced verdicts; below it, the failed
# conversations are retried ONCE as a batch, and if still below the iteration
# is SKIPPED (champion kept, loop continues) rather than failing the run.
QUALITY_OPT_V3_MIN_SUCCESS_FRACTION: float = 0.8
# An island that fails this many consecutive iterations is marked dead for the
# run (its partner island continues; the model fails only when ALL islands die).
QUALITY_OPT_V3_ISLAND_MAX_CONSECUTIVE_FAILURES: int = 3
# Guard retry budget for THROTTLED/TRANSIENT classifications at the call site
# (auth healing happens inside the clients; the guard handles everything else).
QUALITY_OPT_V3_GUARD_MAX_RETRIES: int = 4

# V3 cycle size: ONE rep per item at every rung (owner direction 2026-06-10:
# a cycle is the rung's items once — e.g. 6 conversations at rung 0 — then the
# judge adjudication; fast visible cycles beat the tighter per-rung CI v2 bought
# with 3 reps). The CI machinery is unchanged — it just sees n = rung size.
QUALITY_OPT_V3_RUNG_REPS: tuple[int, ...] = (1, 1, 1, 1, 1)

# V3 FIXED escalation schedule (owner direction 2026-06-10): rounds spent per
# rung before climbing, WIN OR LOSE — 4 at n=6, 4 at n=12, 4 at n=24, 2 at
# n=40, 2 at n=60 (16 rounds per island), then Phase B. Replaces v2's
# promotion-gated should_escalate/is_stuck pair in the v3 orchestrator; the
# tournament cadence is unchanged. Indexed by rung; the last value repeats if
# the ladder is longer.
QUALITY_OPT_V3_ROUNDS_PER_RUNG: tuple[int, ...] = (4, 4, 4, 2, 2)

# Per-(model, island) seed prompt overrides: data/bakeoff/v3_seeds/<model>_i<id>.txt.
# When a file exists, a FRESH island seeds from it verbatim (resume still
# restores the durable champion); when absent, the default fixed-menu seed is
# used, so the override directory is fully optional.
QUALITY_OPT_V3_SEEDS_DIR: Path = BAKEOFF_DIR / "v3_seeds"

# ---------------------------------------------------------------------------
# Eval dashboard REAL-DATA backfill (bakeoff/eval/real_backfill.py).
# Maps the REAL bake-off run records (outcomes.jsonl + judge_scores.jsonl) into
# the eval dashboard's EvalInstance shape so Eval 3D / Eval 2D display real
# data. Outputs land in their OWN files (clear lineage, never mixed with the
# synthetic producer's default store):
#   * instances  — the EvalEventStore the dashboard reads (point the app at it
#                  via the GBBO_EVAL_EVENTS_PATH env var; dashboard.sh sets it);
#   * run details — per-instance provenance (trial_id, source file, pass, item);
#   * judge      — the joined Opus judge outputs per trial (kept separate from
#                  ragas-style metrics, Req 18.2/18.3).
# ---------------------------------------------------------------------------
EVAL_REAL_INSTANCES_PATH: Path = BAKEOFF_DIR / "eval_real_instances.jsonl"
EVAL_REAL_RUN_DETAILS_PATH: Path = BAKEOFF_DIR / "eval_real_run_details.jsonl"
EVAL_REAL_JUDGE_PATH: Path = BAKEOFF_DIR / "eval_real_judge.jsonl"

# ---------------------------------------------------------------------------
# ragas + GEPA integration (spec: optimizer-ragas-gepa) — flag-gated, default OFF
# ---------------------------------------------------------------------------
# Tier 1: ragas as a SECONDARY, non-deciding signal (the exact role `closeness` plays).
# Both flags default OFF so config-off behavior is byte-identical to pre-feature (Req 3.2 /
# 3.3 / 17). The ragas scores are recorded on the TurnVerdict as a cross-check and a retrieval
# diagnostic; they NEVER enter `overall` or any promotion decision — the Opus judge triad
# stays the sole decision metric (Req 11). Sourcing: ragas is an EXTERNAL framework, not
# Amazon-internal guidance (Req 18).
QUALITY_OPT_RAGAS_CROSS_CHECK_ENABLED: bool = False     # ragas Faithfulness/FactualCorrectness (Req 1, 3.1)
QUALITY_OPT_RAGAS_RETRIEVAL_DIAG_ENABLED: bool = False   # ragas ContextPrecision/Recall + gold-presence (Req 2, 3.1)
# Which ragas adapter to build: "fake" (deterministic, network-free; offline tests) or
# "bedrock" (live, lazy-imports ragas, runs through its Amazon Bedrock adapter). Req 4.1 / 5.1.
QUALITY_OPT_RAGAS_BACKEND: str = "fake"
# Bedrock eval models the LIVE ragas adapter uses; default to the harness's existing eval
# models (the Opus judge LLM + Embed v4). ASSUMPTION TO CONFIRM AT IMPLEMENTATION TIME that
# ragas' Bedrock adapter accepts these exact ids (Req 4.2 / 4.3 / 4.4 / 18.3) — re-validate any
# ragas-derived number before defending a decision upward.
QUALITY_OPT_RAGAS_LLM_MODEL_ID: str = JUDGE_MODEL_ID     # ASSUMPTION — confirm at impl time
QUALITY_OPT_RAGAS_EMBED_MODEL_ID: str = EMBED_MODEL_ID   # ASSUMPTION — confirm at impl time

# Tier 2: the standalone GEPA engine as a GATED, additive alternative model-runner. When OFF
# (default) the island/tournament path is byte-for-byte unchanged (Req 6). When ON, GEPA's
# reflective proposer + Pareto frontier + merge replace the hand-rolled search machinery for
# that run; the Opus judge triad is GEPA's metric and the ragas signals become named
# JudgeDimensions. GEPA is an EXTERNAL framework, not Amazon-internal guidance (Req 18).
QUALITY_OPT_TIER2_GEPA_ENABLED: bool = False
# Which GEPA engine to build: "fake" (deterministic offline propose/Pareto/merge) or "live"
# (lazy-imports the installed standalone `gepa` engine). Req 6.4.
QUALITY_OPT_GEPA_BACKEND: str = "fake"
# The reflective-proposer model key (resolved from QUALITY_MODELS). MUST differ from the Judge
# (Opus); defaults to the v2 author (Sonnet 4.6) so proposer != judge holds (Req 12).
QUALITY_OPT_GEPA_PROPOSER_MODEL_KEY: str = QUALITY_OPT_V2_AUTHOR_MODEL_KEY
# Rollout budget for GEPA. 0 => derive from the coverage-ladder cadence (sum of rung
# conversations); a positive value caps total metric calls directly (MaxMetricCallsStopper).
# ASSUMPTION TO CONFIRM (Req 9.3).
QUALITY_OPT_GEPA_ROLLOUT_BUDGET: int = 0
# Max GEPA merge invocations (mirrors gepa's max_merge_invocations). ASSUMPTION (Req 9.3).
QUALITY_OPT_GEPA_MAX_MERGE_INVOCATIONS: int = 5
# Which ragas signals are promoted to named JudgeDimensions GEPA can target / the dashboard
# can show (Req 8.1). Faithfulness + factual-correctness are the substance axes.
QUALITY_OPT_GEPA_NAMED_RAGAS_DIMENSIONS: tuple[str, ...] = (
    "ragas_faithfulness",
    "ragas_factual_correctness",
)

# ===========================================================================
# Cross-family evaluation (spec: optimizer-cross-family-eval) — three coupled,
# ADDITIVE, config-gated changes. Every gate below defaults OFF, so a run with
# these at their defaults behaves byte-for-byte as today (Req 4.2). Sourcing:
# the techniques (cheap in-loop proxy, cross-family judging, authorship
# obfuscation, proxy-vs-audit divergence as a Goodhart detector) are EXTERNAL/
# industry, NOT Amazon-internal guidance. Every non-Anthropic model id is a
# PLACEHOLDER to confirm against a live Bedrock check at implementation time
# (a live `aws bedrock list-inference-profiles` could not be run in-session).
# ---------------------------------------------------------------------------

# --- Req 1: corrected loop cadence (Opus out of the per-iteration hot loop) --
# When ON, one IslandLoop.step() runs a full ROUND: the Author self-iterates
# QUALITY_OPT_ROUND_STEPS times scored ONLY by the cheap In_Loop_Signal (closeness
# + abstention, NEVER the Judge), and the Opus Judge adjudicates ONCE at the
# Round's conclusion to decide promotion. OFF -> today's single-iteration,
# Opus-scored step (legacy path, unchanged).
QUALITY_OPT_ROUND_CADENCE_ENABLED: bool = False
# Author iterations per Round (Assumption A1 — confirm). A NEW knob (not the
# rung-patience knob, which is a distinct concept); the default 6 reflects the
# user's "six terms" remark and the existing QUALITY_OPT_TOURNAMENT_EVERY_ITERS.
QUALITY_OPT_ROUND_STEPS: int = 6

# --- Req 2: non-Anthropic Author, configured separately from the targets -----
# When ON, the Author resolves from the SEPARATE slot below (not QUALITY_MODELS)
# and the Author!=Judge guard becomes FAMILY-aware. OFF -> today's default Sonnet
# author + identity-only guard (non-regression). An explicit author_model arg to
# build_live_backend always takes precedence over this slot.
QUALITY_OPT_CROSS_FAMILY_AUTHOR_ENABLED: bool = False
# A2 PLACEHOLDER: a non-Anthropic Bedrock model of ~Sonnet-4.6 size (candidates,
# unverified: Amazon Nova Pro, Meta Llama 4 Maverick, Mistral Large 2). None by
# default; required (no silent Claude fallback) when the gate above is ON.
QUALITY_OPT_AUTHOR_MODEL_ID: Optional[str] = None
# Declared Author family for the family-aware guard (e.g. "amazon"/"meta"/
# "mistral"). When None the family is INFERRED from the model id's provider
# segment. Used only when QUALITY_OPT_CROSS_FAMILY_AUTHOR_ENABLED is ON.
QUALITY_OPT_AUTHOR_FAMILY: Optional[str] = None
# Declared Judge family for the guard; None -> inferred from JUDGE_MODEL_ID
# ("anthropic"). Lets the guard compare declared families without re-parsing ids.
QUALITY_OPT_JUDGE_FAMILY: Optional[str] = None
# Provider temperature behavior (Req 2.5-2.7): the temperature is sent in the
# Bedrock request ONLY when the configured Author provider accepts it. Default
# False reproduces today's Claude posture (temperature omitted); a provider that
# accepts temperature sets this True. Passed into BedrockAuthorClient unconditionally
# so the handling follows config, not a fixed Claude assumption.
QUALITY_OPT_AUTHOR_ACCEPTS_TEMPERATURE: bool = False
QUALITY_OPT_AUTHOR_TEMPERATURE: float = 0.2

# --- Req 3: cross-family audit seam (self-preference / Goodhart detector) -----
# When ON, a periodic non-Claude Audit_Judge re-scores the current winner on a
# sample (with authorship/style obfuscation applied first) and a proxy-vs-audit
# ranking-divergence check flags a potential self-preference condition. OFF -> no
# Audit_Judge is built and the orchestrator audit hook is a no-op.
QUALITY_OPT_AUDIT_ENABLED: bool = False
# A3 PLACEHOLDER: a non-Claude Bedrock judge (in-Bedrock candidates, unverified:
# Nova Premier, Llama 4 Maverick, Mistral Large, DeepSeek-R1). None by default;
# required when the gate above is ON.
QUALITY_OPT_AUDIT_JUDGE_MODEL_ID: Optional[str] = None
QUALITY_OPT_AUDIT_JUDGE_FAMILY: Optional[str] = None
# Audit cadence + sample size + flag threshold (all PLACEHOLDERS — confirm).
# Audit every N rounds; re-score this many sampled conversations; flag when the
# normalized ranking divergence in [0,1] exceeds this threshold.
QUALITY_OPT_AUDIT_INTERVAL: int = 3
QUALITY_OPT_AUDIT_SAMPLE_SIZE: int = 12
QUALITY_OPT_AUDIT_DIVERGENCE_THRESHOLD: float = 0.3

# ---------------------------------------------------------------------------
# Credential broker (centralized, multi-agent-safe credential acquisition)
# ---------------------------------------------------------------------------
# The single entry point every Bedrock/AOSS-touching client uses to acquire a
# boto3 Session. Previously each adapter did a bare ``boto3.Session()`` resolving
# the AMBIENT ``AWS_PROFILE``/``default`` (shared, mutable global state any other
# agent on the box can clobber), and "credential refresh" only rebuilt the client
# from the SAME on-disk file — so an expired token was simply re-read and the run
# died (observed: ExpiredTokenException on InvokeInlineAgent ~2 min into a run).
#
# The broker (``bakeoff.credentials``) fixes both:
#   * it binds every session to an EXPLICIT named profile (never ambient env), so
#     a sibling agent flipping ``AWS_PROFILE`` cannot redirect this app's calls;
#   * on an auth-expiry (or proactively, before the TTL elapses) it actually
#     RE-RUNS ``ada`` to mint a fresh token, under a cross-process file lock so
#     concurrent agents/processes never stampede or clobber each other's refresh.
#
# A registry of named credential PROFILES the broker knows how to refresh. Each
# entry is the full ``ada credentials update`` identity for one profile. The
# Bedrock roles and the OpenSearch/AOSS role both live in the same alpha account
# here, so there is one profile today; the registry shape supports a second
# account/profile (the user's "two accounts simultaneously") without code change.
CREDENTIAL_PROFILES: dict[str, dict[str, str]] = {
    "alpha": {
        "account": "948580600005",
        "role": "IibsAdminAccess-DO-NOT-DELETE",
        "provider": "conduit",
        "region": "us-west-2",
    },
    # Prompt Bench's DEDICATED Bedrock account (target generation + Opus judge +
    # Embed v4) so a Prompt Bench run never shares the alpha Bedrock quota with a
    # live optimizer-v3 run — true resource isolation (same role/provider/region
    # as alpha, different account). Retrieval/AOSS stays on alpha (that is where
    # the skywalker-faq-alpha collection lives; a separate service quota).
    "promptbench": {
        "account": "299635194521",
        "role": "IibsAdminAccess-DO-NOT-DELETE",
        "provider": "conduit",
        "region": "us-west-2",
    },
    # Prompt Bench's DEDICATED JUDGE account — the Opus judge (the throughput
    # bottleneck) gets its OWN account, separate from Prompt Bench's target-generation
    # account (``promptbench`` above), so judging never contends with target generation
    # within a Prompt Bench run. Verified live for both the Sonnet target and the Opus
    # judge (Converse) on 2026-06-11.
    "promptbench-judge": {
        "account": "582260130393",
        "role": "IibsAdminAccess-DO-NOT-DELETE",
        "provider": "conduit",
        "region": "us-west-2",
    },
    # --- Multi-account optimizer: one dedicated account PER ROLE -------------
    # Phase 0 verified all five accounts for every model (Converse) AND the
    # InvokeInlineAgent target path, so any account can fill any role; this mapping
    # is the chosen default and is remappable by editing the three role constants
    # below — no code change. Each role gets its own account so the Opus judge
    # (the bottleneck) never shares quota with target generation or the author, and
    # the high-concurrency target lane (~24 concurrent) has an account to itself.
    # (Retrieval/AOSS stays on ``alpha`` — that is where the collection lives.)
    "judge": {
        "account": "334296258454",
        "role": "IibsAdminAccess-DO-NOT-DELETE",
        "provider": "conduit",
        "region": "us-west-2",
    },
    "author": {
        "account": "465556393784",
        "role": "IibsAdminAccess-DO-NOT-DELETE",
        "provider": "conduit",
        "region": "us-west-2",
    },
    "execution": {
        "account": "278522729570",
        "role": "IibsAdminAccess-DO-NOT-DELETE",
        "provider": "conduit",
        "region": "us-west-2",
    },
    # Embed v4 (closeness) on the 5th account so ALL available accounts are in use and
    # ``alpha`` is left to do nothing but OpenSearch/AOSS (the one account that can sign it).
    # This account is us-east-1; ``us.cohere.embed-v4:0`` is a US-wide cross-region profile,
    # verified on this account/region in Phase 0.
    "embed": {
        "account": "817294254658",
        "role": "IibsAdminAccess-DO-NOT-DELETE",
        "provider": "conduit",
        "region": "us-east-1",
    },
}

# Role → credential-profile routing for the live optimizer. Each live component
# resolves its boto3 session (and its auth-expiry refresh) through the broker bound
# to ITS role's profile, so the judge / author / target lanes draw on independent
# accounts. Remap a role to a different account by pointing it at another profile in
# CREDENTIAL_PROFILES above (or back to ``alpha`` to collapse roles onto one account).
QUALITY_OPT_JUDGE_PROFILE: str = "judge"
QUALITY_OPT_AUTHOR_PROFILE: str = "author"
QUALITY_OPT_EXECUTION_PROFILE: str = "execution"
# The real-eval (Metrics tab) runs its model + Opus judge on a DEDICATED account so it
# never contends with the optimizer's judge (334) / execution (278) accounts when both run
# at once. Account 817 (the "embed" profile) is otherwise used only for Cohere embeddings —
# a DIFFERENT per-model quota pool — so the eval's Opus/Sonnet calls and the optimizer's
# embeds on that same account do not compete. (AOSS retrieval still shares alpha — only
# alpha can sign OpenSearch.)
QUALITY_OPT_EVAL_PROFILE: str = "embed"
# Closeness/Embed v4 on its OWN (5th) account so alpha is reserved exclusively for
# OpenSearch/AOSS retrieval — the only account that can sign it.
QUALITY_OPT_EMBED_PROFILE: str = "embed"

# The default profile the broker hands out when a caller does not name one. Every
# live Bedrock client (adapter, author, judge, embedder) and the AOSS signer
# resolve through this unless explicitly overridden.
CREDENTIAL_DEFAULT_PROFILE: str = "alpha"

# Treat a profile's credentials as "due for proactive refresh" once they are this
# old, in seconds. Comfortably inside the ~1h STS/Bedrock token lifetime so a long
# run renews well before expiry rather than discovering it mid-call. The broker
# refreshes lazily on access when the cached mint is older than this.
CREDENTIAL_REFRESH_TTL_S: float = 1500.0  # 25 min

# Minimum seconds between two ada invocations for the SAME profile, regardless of
# how many callers ask. Coalesces a burst of concurrent auth-expiry retries into a
# single real refresh (the others wait on the lock, then see a fresh mint).
CREDENTIAL_MIN_REFRESH_INTERVAL_S: float = 20.0

# Hard timeout for a single ``ada credentials update`` subprocess. ada normally
# returns in 1-3s; a hang past this means a broken Midway session (needs mwinit)
# or a network problem, which the broker surfaces rather than blocking forever.
CREDENTIAL_ADA_TIMEOUT_S: float = 60.0

# Cross-process lock + last-mint-timestamp sidecar live here so refreshes are
# coordinated across EVERY process on the box that uses this broker (the dashboard,
# a CLI run, a sibling agent), not just threads within one process.
CREDENTIAL_LOCK_DIR: Path = BAKEOFF_DIR / "cred_locks"

# ---------------------------------------------------------------------------
# Prompt Bench — fixed A/B/C/D prompt leaderboard (completely independent of the
# optimizer v3: its own Bedrock account/profile, its own concurrency semaphores,
# its own broker, and its own durable stores below). See bakeoff/promptbench/.
# ---------------------------------------------------------------------------
#: The dedicated Bedrock credential profile Prompt Bench binds its TARGET-generation
#: and embed clients to (account 299635194521), so a run never shares the alpha
#: Bedrock quota with a live optimizer-v3 run. Retrieval/AOSS stays on alpha.
PROMPT_BENCH_PROFILE: str = "promptbench"
#: The dedicated profile for Prompt Bench's OPUS JUDGE (account 582260130393) — the
#: judge is the throughput bottleneck, so it gets its OWN account, isolated from this
#: run's target-generation quota (``PROMPT_BENCH_PROFILE``). Verified live 2026-06-11.
PROMPT_BENCH_JUDGE_PROFILE: str = "promptbench-judge"
#: The single Target_Model Prompt Bench scores (owner decision).
PROMPT_BENCH_MODEL: str = "sonnet-4.6-thinking-off"
#: On-disk layout — all SEPARATE from every optimizer store (archive-on-reset).
PROMPT_BENCH_DIR: Path = BAKEOFF_DIR / "prompt_bench"
#: The pinned, deterministic sample. Bumped to 400 single-turn queries, proportionally
#: stratified by answerability (mirrors the real query mix) for an equitable, higher-power
#: comparison. The prior 24-query sample remains at sample_24.json.
PROMPT_BENCH_SAMPLE_PATH: Path = PROMPT_BENCH_DIR / "sample_400.json"
#: The candidate prompt texts under test (scored VERBATIM; what is scored == what is
#: shown). These are the operator-authored ``*.txt`` files in the package prompts dir.
PROMPT_BENCH_PROMPTS_DIR: Path = Path(__file__).resolve().parent / "promptbench" / "prompts"
#: Durable per-conversation point store (one row per (prompt, conversation)).
PROMPT_BENCH_POINTS_PATH: Path = PROMPT_BENCH_DIR / "prompt_bench_points.jsonl"
#: Durable per-prompt aggregate store (one row per prompt when its pass completes).
PROMPT_BENCH_RESULTS_PATH: Path = PROMPT_BENCH_DIR / "prompt_bench_results.jsonl"
#: Reps per conversation (owner decision: 1 rep -> 24 conversations -> 24 points).
PROMPT_BENCH_REPS: int = 1
#: Concurrent TARGET MODEL generations for a Prompt Bench pass. Raised to 16 (from 4)
#: now that the sample is 400 (1600 executions) and Prompt Bench runs on its OWN isolated
#: account (PROMPT_BENCH_PROFILE), so it can use that account's full quota without touching
#: the optimizer. Prompt Bench-specific — never alters the optimizer's CONCURRENCY_CAPS.
PROMPT_BENCH_MODEL_CONCURRENCY: int = 16
#: Concurrent Opus JUDGE calls for a Prompt Bench pass — its OWN cap (not the optimizer's
#: shared CONCURRENCY_CAPS["judge"]), since the judge is the throughput bottleneck and runs
#: on Prompt Bench's isolated account. The resilience layer backs off if the account throttles.
PROMPT_BENCH_JUDGE_CONCURRENCY: int = 16

# Closed-loop optimizer on-disk layout (all SEPARATE files from the bake-off AND
# from the one-shot quality stores above, so the three studies are independent
# and individually disposable; never shares a file with either). Append-only
# JSONL except the single-object results JSON. See design "Data Models".
QUALITY_OPT_ITERATIONS_PATH: Path = BAKEOFF_DIR / "quality_opt_iterations.jsonl"  # SoT, per-iteration
QUALITY_OPT_AUDIT_PATH: Path = BAKEOFF_DIR / "quality_opt_audit.jsonl"            # full audit records + version history
QUALITY_OPT_RESULTS_PATH: Path = BAKEOFF_DIR / "quality_opt_results.json"         # converged champions + Phase B results
QUALITY_OPT_ERRORS_PATH: Path = BAKEOFF_DIR / "quality_opt_errors.jsonl"          # disposable failed attempts

# Minimal OVERRIDDEN orchestration template for the optimizer's inline adapter.
# Modeled on ``INLINE_AGENT_PROMPT_TEMPLATE`` above. The retrieved reference fragments
# are injected via ``$prompt_session_attributes$`` IN THE SYSTEM — NOT concatenated into
# the user ``$question$``. This is load-bearing: the user turn is persisted into the
# session's conversation history, so inlining fragments there made every later turn replay
# ALL prior turns' fragments, blowing past the 200k context limit on multi-turn items.
# The system is set fresh per invoke (and is NOT part of the accumulating message history),
# so each turn carries ONLY its own turn's fragments — bounded regardless of turn count.
# Placeholders Bedrock fills:
#   $instruction$               -> our optimized system instruction (the prompt under test)
#   $prompt_session_attributes$ -> the per-turn retrieved reference context (grounding)
#   $question$                  -> the turn's bare user input (no fragments → no accumulation)
#   $agent_scratchpad$          -> Bedrock's required assistant turn (left empty)
QUALITY_OPT_INLINE_TEMPLATE: str = (
    '{\n'
    '    "anthropic_version": "bedrock-2023-05-31",\n'
    '    "system": "$instruction$\\n\\nRETRIEVED REFERENCE CONTEXT for THIS turn '
    '(your only source of truth — ground every claim in it):\\n$prompt_session_attributes$",\n'
    '    "messages": [\n'
    '        {\n'
    '            "role": "user",\n'
    '            "content": [{"type": "text", "text": "$question$"}]\n'
    '        },\n'
    '        {\n'
    '            "role": "assistant",\n'
    '            "content": [{"type": "text", "text": "$agent_scratchpad$"}]\n'
    '        }\n'
    '    ]\n'
    '}'
)

# Phase B reps must exceed Phase A reps (Req 7.4): the validate phase reports the
# final number and needs a tighter CI than the iterate phase. Caught at import
# time so a misconfig surfaces immediately rather than at run time.
assert QUALITY_OPT_PHASE_B_REPS > QUALITY_OPT_PHASE_A_REPS, (
    "QUALITY_OPT_PHASE_B_REPS must be greater than QUALITY_OPT_PHASE_A_REPS "
    "(Phase B validates at a higher rep count than Phase A iterates)"
)
