"""
Core types for the model-bakeoff-harness (codename **GBBO** — the Great British
Bake-Off of FAQ-bot brains: many candidates, one shared kitchen, judged on the
balance of speed and quality).

Everything here is a **frozen dataclass** (immutable value object) or a small
enum/helper. The spine is :class:`TrialEvent`: the runner produces exactly one
per trial, appends it to ``data/bakeoff/trial_events.jsonl``, and the live UI,
the aggregation engine, and the exec viz all *derive* from it (design AD-1). The
types upstream of the event (:class:`Item`, :class:`GoldFragment`,
:class:`RetrievalResult`, :class:`ModelResponse`, :class:`TrialSpec`) and the
types downstream of it (:class:`CI`, :class:`Aggregate`, :class:`FrontierPoint`,
:class:`SamplingPlan`) round out the contract.

Two cross-cutting design rules are encoded here so later tasks inherit them:

* **Reusability over hard-coding (Req 12, Req 1.7).** No dataset size, cohort
  cardinality, or rep count is baked into any type. The same schema that answers
  "which model" today can answer "is the chosen model 95% accurate" later by
  changing the *plan*, not these types.
* **Credential-expiry resilience is first class.** :class:`ErrorClass` is the
  shared taxonomy the runner and every Bedrock-touching client (model adapters,
  judge, embedding scorer) use to decide whether a failed call should trigger a
  credential refresh + retry. The *signatures* and *retry policy* live in
  :mod:`bakeoff.config`; the classify-and-refresh *logic* is implemented by the
  consuming tasks (5/6/7/10). Task 1 only establishes the taxonomy + surface.

Import-light on purpose: pure standard library (``dataclasses``, ``enum``,
``typing``) plus :func:`bakeoff.ids.trial_id` (also stdlib-only), so importing
:mod:`bakeoff.types` pulls in no httpx/numpy/FastAPI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from bakeoff.ids import trial_id as _trial_id

__all__ = [
    # error taxonomy (credential-expiry resilience surface)
    "ErrorClass",
    # cohort
    "COHORT_DIMENSIONS",
    "CohortKey",
    # dataset / inputs
    "GoldFragment",
    "Turn",
    "ItemTurn",
    "Item",
    # substrate + model I/O
    "RetrievalResult",
    "RetrievalRecord",
    "ModelResponse",
    # scoring
    "AccuracyScores",
    "JudgeScores",
    "QualityScores",
    # event spine
    "StageTimings",
    "TrialEvent",
    # planning
    "TrialSpec",
    "StratumPlan",
    "SamplingPlan",
    # aggregation outputs
    "CI",
    "Aggregate",
    "FrontierPoint",
]


# ---------------------------------------------------------------------------
# Error taxonomy — the credential-expiry-resilience shared type (cross-cutting)
# ---------------------------------------------------------------------------
class ErrorClass(str, Enum):
    """Coarse classification of a failed downstream call.

    The runner and every Bedrock-touching client classify a raised exception or
    HTTP failure into one of these buckets to decide retry strategy. This is the
    *taxonomy only*; the matching signatures (exception names, error codes, HTTP
    statuses) and the retry/backoff policy live in :mod:`bakeoff.config`, and the
    classify-and-retry implementation is owned by the consuming tasks.

    Subclassing ``str`` keeps the value JSON-serializable so an ``error_class``
    can be recorded directly onto a :class:`TrialEvent` without conversion.
    """

    #: Expired or otherwise invalid credentials (e.g. ExpiredTokenException,
    #: UnrecognizedClientException, HTTP 401/403). Warrants a credential refresh
    #: followed by a retry of the affected call — the central resilience case
    #: for long runs that outlive short-lived STS/Bedrock sessions.
    AUTH_EXPIRED = "auth_expired"
    #: Rate limited / throttled (e.g. ThrottlingException, HTTP 429). Warrants a
    #: backoff + retry, but no credential refresh.
    THROTTLED = "throttled"
    #: Transient server-side or network blip (5xx, connection reset, timeout).
    #: Warrants a backoff + retry.
    TRANSIENT = "transient"
    #: Permanent client-side/logic error (most 4xx other than auth/throttle).
    #: MUST NOT be retried; record the trial as errored and continue.
    PERMANENT = "permanent"
    #: Could not be classified; treated conservatively (recorded, not retried).
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Cohort
# ---------------------------------------------------------------------------
#: The ordered cohort axes. The single source of truth for "what dimensions a
#: cell has", so cells can be enumerated/sliced without hard-coding the axis
#: list anywhere else. Adding an axis is editing this tuple + CohortKey.
COHORT_DIMENSIONS: tuple[str, ...] = (
    "geography",
    "proficiency",
    "tone",
    "entry_route",
    "momentary_state",
    "answerability",
    "turn_type",
)


@dataclass(frozen=True)
class CohortKey:
    """One sliceable position in the cohort design.

    Every field is a known, enumerable axis, so the set of non-empty cells can be
    enumerated for stratification (Req 1.6). Frozen + all-``str`` fields makes
    this hashable, so a ``CohortKey`` can be used directly as a dict key when
    grouping events into cells.
    """

    geography: str            # e.g. "Nigeria (Lagos)"
    proficiency: str          # broken | functional | near-native | fluent | uneven
    tone: str                 # disposition/voice, e.g. "terse", "chatty"
    entry_route: str          # slack | quicksuite
    momentary_state: str      # neutral | frustrated | anxious | rushed | confused
    answerability: str        # full | partial | none
    turn_type: str            # single | multi

    # --- helpers ---------------------------------------------------------
    def to_dict(self) -> dict[str, str]:
        """Return the cohort as a plain dict keyed by axis name (full vector)."""
        return {dim: getattr(self, dim) for dim in COHORT_DIMENSIONS}

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> "CohortKey":
        """Rebuild a :class:`CohortKey` from a (full) axis->value mapping."""
        return cls(**{dim: d[dim] for dim in COHORT_DIMENSIONS})

    def project(self, dims: "list[str] | tuple[str, ...]") -> dict[str, str]:
        """Return only the selected axes — used to group by a *subset* of the
        cohort (e.g. slice by ``geography`` alone) without inventing ad-hoc keys.
        """
        return {dim: getattr(self, dim) for dim in dims}

    def cell_id(self) -> str:
        """A stable, human-readable id for this full cell (axes in order).

        Uses a Unit-Separator join so values containing ``|`` or ``/`` do not
        collide. Deterministic — safe as a stratum/cell identifier.
        """
        return "\x1f".join(getattr(self, dim) for dim in COHORT_DIMENSIONS)


# ---------------------------------------------------------------------------
# Dataset / inputs (produced by the loader, Req 1)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GoldFragment:
    """A resolved ideal FAQ fragment for an item (gold)."""

    node_id: str
    title: str
    markdown: Optional[str] = None     # full ground-truth content if resolvable
    snippet: Optional[str] = None      # from corpus_index.tsv if markdown absent


@dataclass(frozen=True)
class Turn:
    """One turn of a multi-turn item, retaining per-turn state and gold (Req 1.3).

    The minimal required fields are ``turn``/``user_utterance``/``momentary_state``;
    the rest describe inter-turn dependency and per-turn answerability/gold and
    default to empty so a turn can be constructed cheaply in tests/fixtures.
    """

    turn: int
    user_utterance: str
    momentary_state: str
    answerability: Optional[str] = None
    wants: Optional[str] = None
    response_dependent: bool = False
    depends_on_turn: Optional[int] = None
    gold: list[GoldFragment] = field(default_factory=list)
    relationship: Optional[str] = None   # conversation edge, e.g. "drill_down"


# Sibling-spec naming compatibility (that spec calls the per-turn type
# ``ItemTurn``); kept as an alias so either name resolves to the same type.
ItemTurn = Turn


@dataclass(frozen=True)
class Item:
    """A distinct synthetic record (single-turn query or multi-turn set),
    normalized so the runner/scorers/aggregations treat all items identically
    (Req 1.2/1.3). The unit of the *perspective sample* (between-item variance).

    ``id`` is the dataset id (``QueryRecord.id`` / ``ConversationRecord.set_id``);
    ``item_id`` is exposed as a read-only alias for downstream code that thinks in
    those terms (it is the value stamped onto :class:`TrialEvent.item_id`).
    """

    id: str
    turn_type: str                     # "single" | "multi"
    cohort: CohortKey
    # single-turn convenience (None for multi):
    query: Optional[str] = None
    wants: Optional[str] = None
    answerability: Optional[str] = None
    gold_node_ids: list[str] = field(default_factory=list)   # raw gold ids
    gold: list[GoldFragment] = field(default_factory=list)   # resolved turn-1/single gold
    # multi-turn:
    turns: tuple[Turn, ...] = ()       # () for single-turn
    # provenance for display/debug:
    persona: Optional[str] = None
    batch: Optional[int] = None
    label_note: Optional[str] = None
    # hard filters to pass to /retrieve (usually empty here):
    retrieval_filters: dict[str, str] = field(default_factory=dict)

    @property
    def item_id(self) -> str:
        """Alias for :attr:`id` (the value used as ``TrialEvent.item_id``)."""
        return self.id

    @property
    def is_multi_turn(self) -> bool:
        """True iff this is a multi-turn item (keyed on ``turn_type``, not the
        presence of turns — a single-turn item may still carry one turn record).
        """
        return self.turn_type == "multi"


# ---------------------------------------------------------------------------
# Shared retrieval substrate (the held constant, Req 2)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RetrievalResult:
    """Return of :class:`bakeoff.retrieval_client.RetrievalClient.retrieve`.

    Holds the ``/retrieve`` response *verbatim* (fragments, per-fragment
    confidence, raw timings, cache_hit) so nothing is lost in translation. The
    runner projects ``timings`` into the typed :class:`StageTimings` on the
    event, and ``fragment_ids`` into the :class:`RetrievalRecord`.
    """

    fragments: list[dict]              # verbatim fragment objects from the backend
    fragment_ids: list[str]            # ranked nodeIds (the cross-rep constant)
    confidence: list[float]            # per-fragment relevanceScore (relative)
    timings: dict[str, float]          # verbatim /retrieve "timings"
    cache_hit: bool


@dataclass(frozen=True)
class RetrievalRecord:
    """The CONSTANT substrate, captured compactly onto each :class:`TrialEvent`.

    ``fragment_ids`` is asserted identical across all reps of the same item — the
    invariant that makes retrieval a constant rather than a confound (design AD-2).
    """

    fragment_ids: list[str]            # ranked nodeIds returned by /retrieve
    confidence: list[float]            # per-fragment relevanceScore (relative)
    cache_hit: bool


# ---------------------------------------------------------------------------
# Model adapter output (Req 3)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ModelResponse:
    """Normalized output of one model adapter for one trial.

    Adapters own prompt assembly, the endpoint call, temperature handling, and
    latency capture — and *only* those (Req 3.4). Scoring happens elsewhere.
    Streaming is required so ``ttft_ms`` is a true time-to-first-token (Req 3.2).
    """

    text: str
    ttft_ms: float                     # time to first token (streamed)
    generation_total_ms: float         # first token -> last token
    token_usage: dict[str, int] = field(default_factory=dict)  # prompt/completion/total
    per_turn_answers: list[str] = field(default_factory=list)  # multi-turn (in order)
    finish_reason: Optional[str] = None
    model: Optional[str] = None        # adapter name, if the adapter sets it
    raw: dict[str, object] = field(default_factory=dict)       # adapter passthrough


# ---------------------------------------------------------------------------
# Scoring (Req 4, Req 5)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AccuracyScores:
    """Layer-A retrieval-aligned accuracy + semantic similarity + answerability.

    ``abstention_correct`` is populated iff ``answerability in {none, partial}``;
    ``unwarranted_refusal`` iff ``answerability == full`` (validated downstream).
    """

    # retrieval ranking vs gold (substrate ceiling, context only)
    precision_at_k: float
    recall_at_k: float
    mrr: float
    ndcg_at_k: float
    # answer grounding vs gold (the model differentiator)
    grounding_precision: float
    grounding_recall: float
    # semantic similarity to the ideal response
    semantic_similarity: float
    # answerability behavior (exactly one of these is populated; see validation)
    abstention_correct: Optional[int] = None   # 1/0 for answerability none/partial
    unwarranted_refusal: Optional[int] = None   # 1/0 for answerability full


@dataclass(frozen=True)
class JudgeScores:
    """LLM-as-judge scores on the THREE dimensions we actually decide on.

    Deliberately narrowed (owner decision) to the quality signals that matter for
    "would a subject-matter expert call this answer correct and grounded?":

    * ``faithfulness`` — the MOST important: is every claim grounded in the
      retrieved fragments (no fabrication)?
    * ``correctness`` — would an SME judge the answer correct for the question?
    * ``completeness`` — does it fully address what was answerable?

    The earlier interaction dimensions (tone/empathy/clarity/actionability) were
    removed entirely — we trust the judge model (Opus 4.8) on substance and do not
    score voice. ``judge_dim_sd`` carries the per-dimension SD across the ``k``
    judge samples, making judge variance a *measured, stored* quantity.
    """

    faithfulness: float
    correctness: float
    completeness: float
    judge_sample_count: int                     # k
    judge_model: str
    judge_dim_sd: dict[str, float] = field(default_factory=dict)  # per-dim SD over k


@dataclass(frozen=True)
class QualityScores:
    """The layered quality bundle for one trial.

    The transparent weighted ``composite`` is always carried *alongside* its
    components (never instead of them, Req 4.6); ``composite_weights_version``
    records which weight set produced it so the exec discussion can re-weight live.
    """

    accuracy: AccuracyScores
    judge: JudgeScores
    composite: float
    composite_weights_version: str


# ---------------------------------------------------------------------------
# Event spine (Req 8) — TrialEvent and its timings
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StageTimings:
    """Mirrors the ``/retrieve`` ``timings`` verbatim + generation timings.

    Invariant (validated for non-error events, Req 8.4):
    ``end_to_end_ms == retrieval_total_ms + generation_total_ms`` (float eps).
    """

    embed_query_ms: float
    bm25_vectorize_ms: float
    hybrid_search_ms: float
    rerank_ms: float
    retrieval_total_ms: float
    ttft_ms: float                     # generation: time to first token (model-owned)
    generation_total_ms: float
    end_to_end_ms: float               # retrieval_total_ms + generation_total_ms


@dataclass(frozen=True)
class TrialEvent:
    """The shared per-trial event — the single source of truth (design AD-1).

    One JSON object per line in ``data/bakeoff/trial_events.jsonl``. The UI, the
    aggregation engine, and the exec viz are all *derived* from these lines.
    ``cohort`` is denormalized onto every event so slicing needs no re-join.

    Field order follows the design exactly (identity → what-was-run → inputs →
    outputs → provenance). No field carries a default: every TrialEvent is fully
    specified at write time (best-effort partial fields + ``error`` set on
    failure, Req 7.5).
    """

    # --- identity ---
    trial_id: str            # deterministic hash(model, item_id, rep, pass, plan_version)
    schema_version: str      # event schema version, for forward-compat
    plan_version: str        # which sampling_plan.json produced this trial
    # --- what was run ---
    model: str
    item_id: str             # e.g. "b0-q01" or "c0-s01"
    turn_type: str           # single | multi
    pass_name: str           # wide | deep | targeted | pilot
    rep: int
    temperature: float
    cohort: CohortKey
    # --- inputs captured for replay/audit ---
    query: str               # for multi-turn: the resolved focal/turn-1 query
    gold_node_ids: list[str]
    answerability: str
    retrieval: RetrievalRecord
    # --- outputs ---
    answer_text: str
    token_usage: dict[str, int]       # prompt/completion/total
    timings: StageTimings
    quality: QualityScores
    # --- provenance ---
    started_at: str          # ISO 8601
    completed_at: str         # ISO 8601
    error: Optional[str]      # set (other fields best-effort) if the trial failed


# ---------------------------------------------------------------------------
# Planning (Req 6, Req 7) — TrialSpec, StratumPlan, SamplingPlan
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TrialSpec:
    """A single planned (not yet executed) trial.

    ``planned_trials`` yields these as the product over (pass, model, item, rep).
    The dedup key the resume logic diffs against the log is the *derived*
    :attr:`trial_id` (a pure function of the identity fields), so a spec and the
    event built from the same identity always share an id (Req 7.1/7.4, P3).
    """

    model: str
    item_id: str
    rep: int
    pass_name: str
    plan_version: str
    temperature: float
    turn_type: Optional[str] = None

    @property
    def trial_id(self) -> str:
        """Deterministic id; identical to :func:`bakeoff.ids.trial_id` for the
        same identity and to the id stamped on the resulting :class:`TrialEvent`.
        """
        return _trial_id(
            self.model, self.item_id, self.rep, self.pass_name, self.plan_version
        )


@dataclass(frozen=True)
class StratumPlan:
    """Per-stratum rep configuration within a :class:`SamplingPlan`."""

    cohort_predicate: dict[str, object]   # which items match this stratum
    passes: dict[str, int]                # {"wide": R_wide, "deep": R_deep, ...} reps
    rationale: str                        # e.g. "multi-turn: R raised to equalize CI"


@dataclass(frozen=True)
class SamplingPlan:
    """The pilot-produced, code-external experiment description (design AD-6).

    Changing the experiment (more reps on multi-turn, a different temperature, a
    different target CI, re-weighted composite) is editing/regenerating this
    object — never editing the runner (Req 6.6, Req 12.1).
    """

    plan_version: str
    temperature: float                    # pilot-confirmed (default starts ~0.2)
    target_ci_halfwidth: float
    confidence_level: float               # e.g. 0.95
    strata: list[StratumPlan]
    budget: dict[str, int]                # caps: max trials, max judge calls, etc.
    pilot_variance_model: dict[str, object]   # sigma_within/sigma_between per stratum
    composite_weights: dict[str, float]


# ---------------------------------------------------------------------------
# Aggregation outputs (Req 9, Req 11)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CI:
    """A confidence interval with the method that produced it.

    ``method`` is ``"cluster_bootstrap"`` (the defensible default shown in the
    exec viz) or ``"normal_approx"`` (the cheap incremental estimate for the live
    UI). No number reaches the exec viz without one of these (Req 11.1, P10).
    """

    point: float
    low: float
    high: float
    method: str          # "cluster_bootstrap" | "normal_approx"


@dataclass(frozen=True)
class Aggregate:
    """One aggregated metric for one group (model, (model, cohort cell), or pass).

    ``n_items`` is the distinct-item count (the between-item power that drives CI
    width); ``n_trials`` is total reps. ``latency_quantiles`` is populated only
    for latency metrics (reported as a distribution, never a lone mean, Req 9.5).

    **Insufficient-data marking (Req 9.8, Req 13.4, design Property 10).** A
    cohort cell too thin for a meaningful CI is *explicitly marked* rather than
    emitted as a confident value: when :attr:`insufficient_data` is ``True``,
    :attr:`mean_ci` is ``None`` (no fabricated number escapes). The invariant the
    aggregation engine guarantees and the exec viz relies on is the exclusive-or
    ``(mean_ci is None) == insufficient_data`` — every cell either carries a
    populated CI or is flagged insufficient-data, never neither and never both.
    """

    group: dict[str, str]                 # e.g. {"model": "A", "answerability": "none"}
    metric: str                           # e.g. "composite", "abstention_correct"
    n_items: int                          # distinct items (between-item power)
    n_trials: int                         # total reps
    mean_ci: Optional[CI]                 # populated iff not insufficient_data (P10)
    variance_decomp: dict[str, float]     # {"between": x, "within": y, "judge": z}
    latency_quantiles: Optional[dict[str, float]] = None  # {"p50":..,"p90":..,"p95":..}
    insufficient_data: bool = False       # thin cell: marked, not a confident value


@dataclass(frozen=True)
class FrontierPoint:
    """One model's position on the speed/quality frontier (Req 9.6, Req 11.2)."""

    model: str
    quality: CI                           # composite quality with CI
    speed_p50_ms: float
    speed_p90_ms: float
    on_pareto_front: bool
