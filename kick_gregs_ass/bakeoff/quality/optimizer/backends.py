"""
Backend wiring for the closed-loop prompt optimizer (design Component 1 "Backend
wiring (offline | live)"; Req 10.4, 10.6, 4.1, 4.5, 13.1, 15.4, 16.3).

Everything the loop and the :class:`bakeoff.quality.optimizer.judge_loop.JudgeInLoopScorer`
need from the outside world is bundled into a single injectable
:class:`OptimizerBackend` — the answer-adapter factory, the Judge scorer, the closeness
(semantic) scorer, the held-constant :class:`~bakeoff.quality.optimizer.retrieval.RetrievalBackend`,
and the :class:`~bakeoff.quality.optimizer.author.AuthorClient`. Injecting one bundle (rather
than five separate seams) lets a test or the CLI swap the whole "outside world" in one move
and lets every persisted record stamp which backend produced it (Req 10.6).

This mirrors the existing ``build_offline_scorers`` / ``build_live_scorers`` +
``_offline_run_factory`` seam in :mod:`bakeoff.quality.main`, extended with the Author, the
answer-adapter factory, and the **RetrievalBackend** (the new held-constant, read-only
retrieval substrate, design Component 5b). The :class:`JudgeInLoopScorer` consumes this
bundle **duck-typed** — it only reads ``answer_adapter_factory``, ``judge_scorer``,
``closeness_scorer`` and ``retrieval`` — so the field names here match those attributes
exactly and the scorer never has to hard-import this module.

This module wires **both** bundles. :func:`build_offline_backend` wires the **offline**
bundle: a :class:`~bakeoff.quality.offline_adapter.QualityOfflineAdapter` factory, a
:class:`~bakeoff.scoring.judge.StubJudge`-backed :class:`~bakeoff.scoring.judge.JudgeScorer`,
a fake-embed :class:`~bakeoff.quality.closeness.TurnClosenessScorer`, a network-free
:class:`~bakeoff.quality.optimizer.retrieval.FakeRetrievalBackend` (wrapped for held-constant
reuse via :func:`~bakeoff.quality.optimizer.retrieval.build_retrieval_backend`), and an
:class:`~bakeoff.quality.optimizer.author.OfflineAuthorClient` — the whole offline path makes
**zero network calls** (Req 10.4). :func:`build_live_backend` wires the **live** Bedrock-backed
bundle: a :class:`~bakeoff.quality.optimizer.inline_session_adapter.PersistentSessionInlineAdapter`
factory, the real Opus :class:`~bakeoff.scoring.judge.JudgeScorer` (optionally supplied the
grounding/abstention guidance per Req 15.4), an Embed v4
:class:`~bakeoff.quality.closeness.TurnClosenessScorer`, an
:class:`~bakeoff.quality.optimizer.retrieval.OpenSearchRetrievalBackend` (preferred) with a
:class:`~bakeoff.quality.optimizer.retrieval.LocalRetrievalBackend` fallback, and a Bedrock
Sonnet-4.6 :class:`~bakeoff.quality.optimizer.author.BedrockAuthorClient`. The live builder
refuses to start (raising :class:`AuthorJudgeConflictError`) when the configured Author and
Judge models are the same (Req 4.2), and every Bedrock-touching client is built lazily through
injectable factories so importing this module needs no boto3 and tests construct the live
bundle with fakes (no real AWS).

Sourcing caveat (carried from requirements.md / design.md): the judge triad as the decision
signal, the abstention failure modes, and the modern Claude 4.5 prompting guidance the
Author carries are grounded in external/industry RAG-evaluation practice, this repo's own
observed Opus verdicts, and an external/vendor prompt-engineering source — **not** in
Amazon-internal primary sources.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional, Protocol, Sequence, runtime_checkable

from bakeoff import config
from bakeoff.quality.closeness import TurnClosenessScorer
from bakeoff.quality.offline_adapter import QualityOfflineAdapter
from bakeoff.quality.optimizer.author import (
    AuthorClient,
    BedrockAuthorClient,
    OfflineAuthorClient,
    _default_author_model,
)
from bakeoff.quality.optimizer.inline_session_adapter import PersistentSessionInlineAdapter
from bakeoff.quality.optimizer.prompting_guidance import GROUNDING_ABSTENTION_EXCERPT
from bakeoff.quality.optimizer.retrieval import RetrievalBackend, build_retrieval_backend
from bakeoff.quality.optimizer.ragas_adapter import RagasAdapter, build_ragas_adapter
from bakeoff.scoring.judge import (
    JudgeBackend,
    JudgeRequest,
    JudgeSample,
    JudgeScorer,
    make_bedrock_judge,
    make_stub_judge,
)
from bakeoff.scoring.pipeline import _make_fake_embed_fn
from bakeoff.scoring.semantic import EmbeddingClient, SemanticSimilarityScorer
from bakeoff.types import Item, ModelResponse

__all__ = [
    "QualityAnswerAdapter",
    "AnswerAdapterFactory",
    "OptimizerBackend",
    "AuthorJudgeConflictError",
    "AuthorJudgeFamilyConflictError",
    "model_family",
    "build_offline_backend",
    "build_live_backend",
]

if TYPE_CHECKING:  # annotation-only; never imported at runtime (no import cycle, audit is lazy).
    from bakeoff.quality.optimizer.audit import AuditJudge


@runtime_checkable
class QualityAnswerAdapter(Protocol):
    """ModelAdapter-compatible producer of per-turn answers for a multi-turn item.

    Satisfied by both the offline :class:`bakeoff.quality.offline_adapter.QualityOfflineAdapter`
    and the live persistent-session inline adapter (later task). The :class:`JudgeInLoopScorer`
    calls ``generate(item, fragments, temperature)`` once per conversation and reads
    ``ModelResponse.per_turn_answers``; the offline adapter ignores ``fragments`` (the live
    inline adapter renders them inline into the visible prompt).
    """

    name: str

    async def generate(
        self, item: Item, fragments: Sequence[dict], temperature: float
    ) -> ModelResponse:
        """Produce a :class:`ModelResponse` (with per-turn answers) for ``item``."""
        ...


#: Builds the answer adapter for a model under a given instruction override
#: (``(model, instruction, items_by_id) -> adapter``). This is exactly how
#: :meth:`bakeoff.quality.optimizer.judge_loop.JudgeInLoopScorer._score_conversation`
#: invokes it — ``backend.answer_adapter_factory(model, instruction, item_lookup)`` — so the
#: instruction override is the only thing that varies between Champion and Challenger.
AnswerAdapterFactory = Callable[[str, str, dict], QualityAnswerAdapter]


@dataclass(frozen=True)
class OptimizerBackend:
    """Everything the loop needs from the outside world, injected as one bundle.

    Offline: ``answer_adapter_factory`` → :class:`QualityOfflineAdapter`, ``judge_scorer`` →
    :class:`StubJudge`-backed :class:`JudgeScorer`, ``closeness_scorer`` → fake-embed
    :class:`TurnClosenessScorer`, ``retrieval`` → memoized :class:`FakeRetrievalBackend`,
    ``author`` → :class:`OfflineAuthorClient`. Zero network (Req 10.4).

    Live (later task 11.4): ``answer_adapter_factory`` → persistent-session inline adapter,
    ``judge_scorer`` → real Opus :class:`JudgeScorer`, ``closeness_scorer`` → Embed v4
    :class:`TurnClosenessScorer`, ``retrieval`` → ``OpenSearchRetrievalBackend`` (preferred)
    with a ``LocalRetrievalBackend`` fallback, ``author`` → Bedrock Sonnet-4.6 author.

    The field names match the attributes :class:`JudgeInLoopScorer` reads off the backend
    (``answer_adapter_factory``, ``judge_scorer``, ``closeness_scorer``, ``retrieval``), so
    the scorer's duck-typed contract is satisfied without importing this module. ``name`` is
    recorded on every persisted record so a reader can tell whether a result came from the
    offline or the live backend (Req 10.6). Frozen so a backend bundle cannot be mutated
    out from under the held-constant retrieval guarantee.
    """

    name: str  # "offline" | "live" (recorded on every record, Req 10.6)
    answer_adapter_factory: AnswerAdapterFactory
    judge_scorer: JudgeScorer
    closeness_scorer: TurnClosenessScorer
    retrieval: RetrievalBackend  # held-constant, read-only; invoked every turn (Req 13/16)
    author: AuthorClient  # carries the repo-baked Prompting_Guidance (Req 15)
    ragas_adapter: Optional["RagasAdapter"] = None  # Tier-1 ragas cross-check seam (Req 5.3); None = absent
    #: Optional non-Claude Audit_Judge seam (Req 3.1). ``None`` unless the audit feature is
    #: enabled (``config.QUALITY_OPT_AUDIT_ENABLED``); the duck-typed contract the
    #: :class:`JudgeInLoopScorer` relies on is unaffected because the field is optional and
    #: defaults to ``None``. Annotated as a string (the module's ``from __future__ import
    #: annotations`` defers it) so ``audit.py`` is imported lazily, never at module load.
    audit_judge: "Optional[AuditJudge]" = None


class AuthorJudgeConflictError(Exception):
    """Raised when the Author model and the Judge model are configured to be the same.

    The Author and the Judge must be different models (Req 4.1): the Judge must never grade
    a prompt authored by itself, and the loop must not contend with itself for the shared
    Opus quota. The live builder (``build_live_backend``, task 11.4) raises this and refuses
    to start when ``author == judge`` (Req 4.2). The offline backend uses a distinct stub
    judge and a deterministic offline author, so the conflict cannot arise offline — the
    exception is defined here, alongside the bundle it guards, for the live wiring to use.
    """


class AuthorJudgeFamilyConflictError(AuthorJudgeConflictError):
    """Raised when the Author model's FAMILY equals the Judge model's family (Req 2.4).

    The cross-family-eval feature strengthens the identity-only Author≠Judge guard to a
    **family-level** separation: a Sonnet author against an Opus judge is rejected by neither
    the identity check (different ids) nor this check would have caught it before — both are
    Anthropic. When ``config.QUALITY_OPT_CROSS_FAMILY_AUTHOR_ENABLED`` is on,
    ``build_live_backend`` raises this if the resolved Author family equals the Judge family,
    refusing to start rather than silently optimizing with a same-family Author and Judge.

    It subclasses :class:`AuthorJudgeConflictError` so existing callers that catch the
    identity conflict still catch this stronger family conflict unchanged.
    """


#: Provider tokens recognized in a Bedrock model id's ``[region.]provider.model`` convention.
_KNOWN_FAMILIES: frozenset[str] = frozenset(
    {
        "anthropic", "amazon", "meta", "mistral", "cohere", "ai21", "deepseek",
        "stability", "nvidia", "writer", "luma", "twelvelabs", "qwen",
    }
)
#: Short leading region-like segments to skip when inferring a family from a model id.
_REGION_LIKE: frozenset[str] = frozenset(
    {"us", "eu", "apac", "ap", "ca", "sa", "af", "me", "gov", "us-gov", "apne", "apse"}
)


def model_family(model_id: Optional[str], *, declared: Optional[str] = None) -> str:
    """Return the provider/lineage token for a Bedrock model id (Req 2.3).

    Uses ``declared`` when provided (the config-declared family, since the A2/A3 ids are
    placeholders to confirm at implementation time); otherwise infers from the id's provider
    segment in the Bedrock ``[region.]provider.model`` convention — for example
    ``us.anthropic.claude-opus-4-8`` → ``"anthropic"``, ``us.amazon.nova-pro`` → ``"amazon"``,
    ``mistral.mistral-large-2`` → ``"mistral"``. Lower-cased; never raises (an empty/None id
    returns ``"unknown"``). The inference first looks for any recognized provider token, then
    strips a leading region-like segment, then falls back to the first token — so an
    unrecognized provider id still yields a stable, comparable family token.
    """
    if declared and declared.strip():
        return declared.strip().lower()
    s = (model_id or "").strip().lower()
    if not s:
        return "unknown"
    tokens = [t for t in s.replace("/", ".").split(".") if t]
    if not tokens:
        return "unknown"
    for tok in tokens:
        if tok in _KNOWN_FAMILIES:
            return tok
    if len(tokens) >= 2 and tokens[0] in _REGION_LIKE:
        return tokens[1]
    return tokens[0]


def _offline_answer_adapter_factory(
    model_key: str, instruction: str, item_lookup: dict
) -> QualityAnswerAdapter:
    """Build a :class:`QualityOfflineAdapter` for ``model_key`` under ``instruction``.

    Mirrors :func:`bakeoff.quality.main._offline_run_factory` exactly: the instruction is
    injected as the adapter's ``instruction_override`` (the only varied element between
    Champion and Challenger), and the model's ``family`` is resolved from
    :data:`bakeoff.config.QUALITY_MODELS`. Falls back to the model key as the family for any
    model not in ``QUALITY_MODELS`` so the factory never raises a ``KeyError`` on an
    unexpected key. Deterministic and network-free.
    """
    spec = config.QUALITY_MODELS.get(model_key, {})
    return QualityOfflineAdapter(
        model_key,
        instruction_override=instruction,
        item_lookup=item_lookup,
        family=str(spec.get("family", model_key)),
    )


def build_offline_backend(author_model: str = "offline-author") -> OptimizerBackend:
    """Wire the deterministic, zero-network offline :class:`OptimizerBackend` (Req 10.4).

    Mirrors the ``build_offline_scorers`` + ``_offline_run_factory`` seam in
    :mod:`bakeoff.quality.main`, bundling:

    * ``answer_adapter_factory`` — :func:`_offline_answer_adapter_factory`, producing a
      :class:`QualityOfflineAdapter` whose answer quality tracks the injected instruction
      (so the loop sees a real improving signal offline);
    * ``judge_scorer`` — a :class:`JudgeScorer` backed by the deterministic
      :class:`StubJudge` (``disk_cache=False`` so tests stay hermetic), the same Judge
      implementation the rest of the study uses (Req 2.2);
    * ``closeness_scorer`` — a :class:`TurnClosenessScorer` over a fake-embed
      :class:`SemanticSimilarityScorer` (recorded as a secondary cross-check only, Req 2.3);
    * ``retrieval`` — a network-free :class:`FakeRetrievalBackend` selected via
      :func:`build_retrieval_backend`, which wraps it in a
      :class:`MemoizingRetrievalBackend` so Champion and Challenger receive byte-identical
      fragments per ``(turn-query)`` (held-constant retrieval, Req 13.3/16.3);
    * ``author`` — a deterministic :class:`OfflineAuthorClient` (a different "model" from
      the stub Judge, Req 4.1/4.5), whose contract embeds the repo-baked Prompting_Guidance
      on every call (Req 15.1).

    Every piece imports lazily-or-not but performs **no network I/O**: the fake embedder,
    the stub judge, the fake retrieval backend, and the offline adapter open no sockets and
    construct no boto3 clients (Req 10.4).

    Note on Req 15.4: the Judge MAY be supplied the grounding/abstention portion of the
    Prompting_Guidance (``GROUNDING_ABSTENTION_EXCERPT``) so its evaluation stays consistent
    with the guidance the Author follows. The offline :class:`StubJudge` derives its scores
    from the answer's structure and answerability rather than from rendered prompt text, so
    supplying the excerpt offline would change nothing; the live builder (task 11.4) is the
    place that excerpt is wired into the real Opus judge prompt.

    Args:
        author_model: identity recorded for the offline Author (default ``"offline-author"``).

    Returns:
        A fully-wired offline :class:`OptimizerBackend` (``name="offline"``).
    """
    semantic = SemanticSimilarityScorer(embed_fn=_make_fake_embed_fn(), disk_cache=False)
    closeness_scorer = TurnClosenessScorer(semantic)
    judge_scorer = JudgeScorer(backend=make_stub_judge(), disk_cache=False)
    retrieval = build_retrieval_backend("fake")
    author = OfflineAuthorClient(author_model=author_model)

    return OptimizerBackend(
        name="offline",
        answer_adapter_factory=_offline_answer_adapter_factory,
        judge_scorer=judge_scorer,
        closeness_scorer=closeness_scorer,
        retrieval=retrieval,
        author=author,
        ragas_adapter=build_ragas_adapter("fake"),
    )


# ---------------------------------------------------------------------------
# Live backend — the real Bedrock-backed bundle (task 11.4).
# ---------------------------------------------------------------------------
class _GroundingGuidanceJudgeBackend:
    """Wrap a :data:`JudgeBackend` to supply the grounding/abstention guidance (Req 15.4).

    The live judge MAY be handed the grounding/abstention portion of the
    Prompting_Guidance (:data:`~bakeoff.quality.optimizer.prompting_guidance.GROUNDING_ABSTENTION_EXCERPT`)
    so its faithfulness/abstention evaluation stays consistent with the guidance the Author
    follows. The :class:`~bakeoff.scoring.judge.JudgeScorer` renders its prompt from a
    :class:`~bakeoff.scoring.judge.JudgeRequest` and never exposes a "system guidance" seam,
    so rather than editing ``judge.py`` this wrapper prepends the excerpt to each request's
    ``prompt_text`` just before the call reaches the inner backend. The wrapper is a no-op
    for any backend that ignores ``prompt_text`` (e.g. the offline stub), which is why it is
    only applied on the live path.

    It is intentionally thin: it neither parses nor mutates the judge's scores, so the
    decision metric (the Opus triad) is unchanged — the excerpt only conditions *how* the
    judge reads the same fragments/answer it was already given.
    """

    def __init__(self, inner: JudgeBackend, excerpt: str) -> None:
        self._inner = inner
        self._excerpt = excerpt

    def __call__(self, req: JudgeRequest) -> JudgeSample:
        guided_prompt = f"{self._excerpt}\n\n{req.prompt_text}" if req.prompt_text else req.prompt_text
        guided_req = JudgeRequest(**{**req.__dict__, "prompt_text": guided_prompt})
        return self._inner(guided_req)


def _bedrock_model_id_for(model_key: str) -> str:
    """Resolve a Target_Model key to its Bedrock foundation-model id from ``config``.

    Reads :data:`bakeoff.config.QUALITY_MODELS` (the single source of truth for the two
    fixed Target_Models, Req 12.3); falls back to the key itself so an unexpected key never
    raises a ``KeyError`` at construction (it would simply 400 later if it were not a real
    id). Mirrors how :func:`bakeoff.quality.main._live_run_factory` resolves the id.
    """
    spec = config.QUALITY_MODELS.get(model_key, {})
    return str(spec.get("bedrock_model_id", model_key))


def _live_answer_adapter_factory(
    model_key: str, instruction: str, item_lookup: dict
) -> QualityAnswerAdapter:
    """Build a :class:`PersistentSessionInlineAdapter` for ``model_key`` under ``instruction``.

    The live counterpart of :func:`_offline_answer_adapter_factory`: the optimized prompt
    under test (``instruction``) is passed as the adapter's ``instruction_override`` — the
    only element that varies between Champion and Challenger (Req 3.6 / 12.4) — and the
    Bedrock foundation-model id is resolved from :data:`bakeoff.config.QUALITY_MODELS`. The
    adapter builds its ``bedrock-agent-runtime`` client lazily, so this factory performs no
    network I/O until a turn is actually generated. ``item_lookup`` is accepted to match the
    :data:`AnswerAdapterFactory` contract (the inline adapter answers each item directly and
    does not consult it).
    """
    # Targets run on the dedicated EXECUTION account (config.QUALITY_OPT_EXECUTION_PROFILE)
    # in its region, so the ~24-concurrent generation lane has its own quota and the
    # adapter's auth-expiry rebuild re-mints THAT account.
    _exec_profile = config.QUALITY_OPT_EXECUTION_PROFILE
    _exec_region = config.CREDENTIAL_PROFILES.get(_exec_profile, {}).get(
        "region", config.AWS_REGION
    )
    return PersistentSessionInlineAdapter(
        model_key,
        _bedrock_model_id_for(model_key),
        instruction_override=instruction,
        credential_profile=_exec_profile,
        region=_exec_region,
    )


def build_live_backend(
    author_model: Optional[str] = None,
    *,
    retrieval_backend: str = config.QUALITY_OPT_RETRIEVAL_BACKEND,
    judge_client_factory: Optional[Callable[[], Any]] = None,
    author_client_factory: Optional[Callable[[], Any]] = None,
    audit_client_factory: Optional[Callable[[], Any]] = None,
    embedding_client_factory: Optional[Callable[[], Any]] = None,
    opensearch_client: Optional[Any] = None,
    opensearch_usable: "Optional[Callable[[Any], bool]]" = None,
    local_client: Optional[Any] = None,
    rerank_client: Optional[Any] = None,
    supply_judge_grounding_guidance: bool = True,
) -> OptimizerBackend:
    """Wire the real Bedrock-backed live :class:`OptimizerBackend` (Req 10.5, 4.x, 13/16).

    The live counterpart of :func:`build_offline_backend`, mirroring the
    ``build_live_scorers`` + ``_live_run_factory`` seam in :mod:`bakeoff.quality.main` and
    bundling:

    * ``answer_adapter_factory`` — :func:`_live_answer_adapter_factory`, producing a
      :class:`~bakeoff.quality.optimizer.inline_session_adapter.PersistentSessionInlineAdapter`
      that drives the Target_Model through Bedrock ``InvokeInlineAgent`` with the optimized
      prompt as the only system instruction and the retrieved fragments rendered inline
      (Req 3.6 / 13);
    * ``judge_scorer`` — the real Opus :class:`~bakeoff.scoring.judge.JudgeScorer` (the same
      Judge implementation the rest of the study uses, Req 2.2), backed by the resilient
      Bedrock judge (``config.JUDGE_MODEL_ID``). When ``supply_judge_grounding_guidance`` is
      set (the default), its backend is wrapped so each judge prompt is prefixed with the
      grounding/abstention excerpt (Req 15.4);
    * ``closeness_scorer`` — a :class:`~bakeoff.quality.closeness.TurnClosenessScorer` over
      the real Embed v4 :class:`~bakeoff.scoring.semantic.SemanticSimilarityScorer` (recorded
      as a secondary cross-check only, Req 2.3);
    * ``retrieval`` — the held-constant, read-only substrate from
      :func:`~bakeoff.quality.optimizer.retrieval.build_retrieval_backend`: the
      ``OpenSearchRetrievalBackend`` (preferred) with a ``LocalRetrievalBackend`` fallback,
      wrapped in the memoizing layer so Champion and Challenger see byte-identical fragments
      per ``(turn-query)`` (Req 13.3 / 16.1 / 16.2);
    * ``author`` — the live :class:`~bakeoff.quality.optimizer.author.BedrockAuthorClient`
      (default Author = Sonnet 4.6 via :func:`~bakeoff.quality.optimizer.author._default_author_model`,
      Req 4.4), built on the same Prompting_Guidance-bearing contract as the offline author
      (Req 15.1).

    Author/Judge separation (Req 4.1 / 4.2) is enforced **before** any client is built: if
    the resolved Author model id equals the Judge model id (``config.JUDGE_MODEL_ID``), this
    raises :class:`AuthorJudgeConflictError` and refuses to start, so the Judge never grades
    a prompt authored by itself and the loop never contends with itself for the shared Opus
    quota.

    Every Bedrock-touching client is built **lazily** through the injectable
    ``*_client_factory`` / ``*_client`` seams (exactly as the live judge, embedder, author,
    and inline adapter already do), so this function — and importing this module — performs
    **no** network I/O and needs **no** boto3 at import time. Tests construct the whole live
    bundle with fakes and zero AWS by passing those seams.

    Args:
        author_model: Bedrock id for the Author; defaults to Sonnet 4.6 resolved from
            ``config`` via :func:`~bakeoff.quality.optimizer.author._default_author_model`
            (Req 4.4). Must differ from ``config.JUDGE_MODEL_ID`` (Req 4.2).
        retrieval_backend: which substrate to prefer (``"opensearch"`` | ``"local"`` |
            ``"fake"``); defaults to ``config.QUALITY_OPT_RETRIEVAL_BACKEND``. Always wrapped
            in the memoizing held-constant layer (Req 13.3).
        judge_client_factory: zero-arg ``bedrock-runtime`` client factory for the Opus judge
            (injected by tests). Defaults to the resilient judge's own lazy boto3 builder.
        author_client_factory: zero-arg ``bedrock-runtime`` client factory for the Author
            (injected by tests). Defaults to the author's own lazy boto3 builder.
        embedding_client_factory: zero-arg ``bedrock-runtime`` client factory for the Embed
            v4 closeness scorer (injected by tests). Defaults to the real chain.
        opensearch_client / opensearch_usable / local_client: retrieval seams forwarded to
            :func:`~bakeoff.quality.optimizer.retrieval.build_retrieval_backend` so tests can
            exercise the preferred/fallback policy with fakes (no real AWS / HTTP).
        supply_judge_grounding_guidance: when ``True`` (default) the live judge prompt is
            prefixed with the grounding/abstention excerpt (Req 15.4); set ``False`` to grade
            with the bare rubric.

    Returns:
        A fully-wired live :class:`OptimizerBackend` (``name="live"``).

    Raises:
        AuthorJudgeConflictError: if the resolved Author model equals the Judge model
            (Req 4.2).
    """
    # --- Author resolution + Author≠Judge separation --------------------------------------
    # Precedence: an explicit ``author_model`` arg always wins (tests / call sites pass one).
    # Otherwise, when the cross-family Author feature is ON, resolve from the SEPARATE slot
    # ``QUALITY_OPT_AUTHOR_MODEL_ID`` (Req 2.1) — never silently falling back to the Claude
    # default, which would defeat the feature; when OFF, keep today's default Sonnet author
    # (non-regression, Req 4.2).
    if author_model is not None:
        resolved_author_model = author_model
    elif config.QUALITY_OPT_CROSS_FAMILY_AUTHOR_ENABLED:
        if not config.QUALITY_OPT_AUTHOR_MODEL_ID:
            raise AuthorJudgeConflictError(
                "QUALITY_OPT_CROSS_FAMILY_AUTHOR_ENABLED is on but QUALITY_OPT_AUTHOR_MODEL_ID "
                "is unset (Req 2.1/2.2): configure a non-Anthropic Bedrock Author id "
                "(Assumption A2 — confirm against a live Bedrock check). The builder will NOT "
                "silently fall back to the Claude default, which would defeat the feature."
            )
        resolved_author_model = config.QUALITY_OPT_AUTHOR_MODEL_ID
    else:
        resolved_author_model = _default_author_model()

    if config.QUALITY_OPT_CROSS_FAMILY_AUTHOR_ENABLED:
        # Family-aware guard (Req 2.3 / 2.4): refuse to start when Author and Judge share a
        # Family (e.g. a Sonnet author against the Opus judge — both Anthropic).
        author_family = model_family(
            resolved_author_model, declared=config.QUALITY_OPT_AUTHOR_FAMILY
        )
        judge_family = model_family(
            config.JUDGE_MODEL_ID, declared=config.QUALITY_OPT_JUDGE_FAMILY
        )
        if author_family == judge_family:
            raise AuthorJudgeFamilyConflictError(
                f"Author family {author_family!r} (model {resolved_author_model!r}) equals the "
                f"Judge family {judge_family!r} (model {config.JUDGE_MODEL_ID}) (Req 2.4): the "
                "cross-family Author feature requires a DIFFERENT family from the Opus Judge. "
                "Set QUALITY_OPT_AUTHOR_MODEL_ID / QUALITY_OPT_AUTHOR_FAMILY to a non-Anthropic "
                "provider and retry."
            )
    else:
        # Today's identity-only guard (non-regression, Req 4.1 / 4.2).
        if resolved_author_model == config.JUDGE_MODEL_ID:
            raise AuthorJudgeConflictError(
                "Author and Judge must be different models (Req 4.1/4.2): the Author resolved "
                f"to {resolved_author_model!r}, which is the configured Judge model "
                "(config.JUDGE_MODEL_ID). The Opus model is reserved for the Judge role "
                "(Req 4.4); configure a different Author (default Author = Sonnet 4.6) and retry."
            )

    # Answer adapter factory — live persistent-session inline adapter (Req 3.6 / 13).
    answer_adapter_factory: AnswerAdapterFactory = _live_answer_adapter_factory

    # Judge — the real Opus JudgeScorer (Req 2.2), optionally given the grounding/abstention
    # guidance so its evaluation stays consistent with the Author's guidance (Req 15.4).
    # Judge on its OWN dedicated account (config.QUALITY_OPT_JUDGE_PROFILE) so the Opus
    # bottleneck never shares quota; an injected test factory still overrides the profile.
    judge_backend: JudgeBackend = make_bedrock_judge(
        config.JUDGE_MODEL_ID,
        client_factory=judge_client_factory,
        credential_profile=config.QUALITY_OPT_JUDGE_PROFILE,
    )
    if supply_judge_grounding_guidance:
        judge_backend = _GroundingGuidanceJudgeBackend(judge_backend, GROUNDING_ABSTENTION_EXCERPT)
    judge_scorer = JudgeScorer(backend=judge_backend, judge_model=config.JUDGE_MODEL_ID)

    # Closeness — real Embed v4 semantic scorer (secondary cross-check only, Req 2.3).
    # Pinned to the dedicated EMBED account (config.QUALITY_OPT_EMBED_PROFILE) in its own
    # region, so alpha is left exclusively for OpenSearch/AOSS. An injected test factory
    # (embedding_client_factory) still wins.
    if embedding_client_factory is None:
        _embed_profile = config.QUALITY_OPT_EMBED_PROFILE
        _embed_region = config.CREDENTIAL_PROFILES.get(_embed_profile, {}).get(
            "region", config.AWS_REGION
        )

        def _embed_client_factory():  # noqa: lazy — no boto3 at module load
            from bakeoff.credentials import get_broker

            session = get_broker().get_session(_embed_profile, region=_embed_region)
            return session.client("bedrock-runtime", region_name=_embed_region)

        embedding_client = EmbeddingClient(client_factory=_embed_client_factory)
    else:
        embedding_client = EmbeddingClient(client_factory=embedding_client_factory)
    closeness_scorer = TurnClosenessScorer(SemanticSimilarityScorer(client=embedding_client))

    # Retrieval — held-constant, read-only; OpenSearch preferred / local fallback (Req 16),
    # always wrapped in the memoizing layer by build_retrieval_backend (Req 13.3).
    # When no client was injected (tests pass one) and we're targeting OpenSearch, build
    # the aoss-signed client from the alpha profile (lazy imports so the module never
    # requires boto3/opensearchpy at import time; gracefully skipped if unavailable).
    # Build the live aoss client through a FACTORY closure (not a one-shot eager build) and
    # wire a broker refresh callback, so a mid-run credential-expiry 403 HEALS instead of
    # killing the run. A long dashboard/optimizer run outlives the ~1h ALPHA token; without
    # the rebuild seam the stale client just re-403s until the retry budget is exhausted and
    # the run dies (observed live: status=failed, AuthorizationException 403). On auth-expiry
    # the OpenSearchRetrievalBackend now: forces an ada mint via ``refresh_credentials`` ->
    # drops the stale client -> rebuilds via this factory bound to the fresh credentials ->
    # retries. Lazy imports keep the module boto3/opensearchpy-free at import time.
    _aoss_client_factory: "Optional[Callable[[], Any]]" = None
    _aoss_refresh: "Optional[Callable[[], Any]]" = None
    if retrieval_backend == "opensearch" and config.QUALITY_OPT_OPENSEARCH_ALPHA_ENDPOINT:
        def _aoss_client_factory():  # noqa: lazy — no boto3/opensearchpy at module load
            from opensearchpy import OpenSearch, Urllib3AWSV4SignerAuth, Urllib3HttpConnection

            from bakeoff.credentials import get_broker

            host = config.QUALITY_OPT_OPENSEARCH_ALPHA_ENDPOINT.replace("https://", "")
            # Resolve creds through the broker bound to the AOSS profile (proactively
            # TTL-refreshed, explicit profile, never ambient env). After a refresh the
            # broker invalidates its session, so this re-reads genuinely fresh credentials.
            creds = get_broker().get_credentials(config.QUALITY_OPT_OPENSEARCH_ALPHA_PROFILE)
            auth = Urllib3AWSV4SignerAuth(
                creds,
                config.QUALITY_OPT_OPENSEARCH_ALPHA_REGION,
                config.QUALITY_OPT_OPENSEARCH_ALPHA_SERVICE,
            )
            return OpenSearch(
                hosts=[{"host": host, "port": 443}],
                http_auth=auth,
                use_ssl=True,
                verify_certs=True,
                connection_class=Urllib3HttpConnection,
                # Bound every request so an unreachable endpoint fails fast (and the selector
                # falls back to local, Req 16.2) instead of hanging a worker thread forever.
                timeout=15,
                max_retries=2,
                retry_on_timeout=True,
            )

        try:
            from bakeoff.credentials import get_broker

            _aoss_refresh = get_broker().refresh_callback_for(
                config.QUALITY_OPT_OPENSEARCH_ALPHA_PROFILE
            )
        except Exception:  # noqa: BLE001 - refresh wiring is best-effort; heal still rebuilds
            _aoss_refresh = None

        # Build the initial client eagerly (when one was not injected by a test) so
        # is_usable() is True and the first query uses it; on a 403 the backend drops it and
        # rebuilds via the same factory above.
        if opensearch_client is None:
            try:
                opensearch_client = _aoss_client_factory()
            except Exception:
                import logging

                logging.getLogger(__name__).warning(
                    "live aoss client build failed; falling back to local retrieval (Req 16.2)",
                    exc_info=True,
                )
                _aoss_client_factory = None
                _aoss_refresh = None

    # Only forward endpoint/index when a real client (or a factory that can build one) exists;
    # otherwise leave them unset so is_usable() stays False and the selector falls back to
    # local (Req 16.2) rather than picking a half-built OpenSearch backend that fails at query time.
    _os_target = (
        dict(
            opensearch_endpoint=config.QUALITY_OPT_OPENSEARCH_ALPHA_ENDPOINT,
            opensearch_index=config.QUALITY_OPT_OPENSEARCH_ALPHA_INDEX,
        )
        if opensearch_client is not None
        else {}
    )
    # Rerank v4 second stage (OPTIMIZER V2 ONLY): Cohere Rerank v4.0 Pro on a SageMaker
    # endpoint in the owner's PERSONAL account 429134228173 — a credential chain entirely
    # separate from alpha, hence its own profile-pinned client factory. boto3 imports
    # lazily inside the factory, and the factory only ever runs on the first live rerank
    # call (RerankedRetrievalBackend._ensure_client), keeping construction network-free.
    def _rerank_client_factory():  # noqa: lazy — no boto3 at module load
        import boto3

        session = boto3.Session(profile_name=config.QUALITY_OPT_RERANK_V4_PROFILE)
        return session.client(
            "sagemaker-runtime", region_name=config.QUALITY_OPT_RERANK_V4_REGION
        )

    retrieval = build_retrieval_backend(
        retrieval_backend,
        opensearch_client=opensearch_client,
        opensearch_client_factory=_aoss_client_factory,
        opensearch_refresh_credentials=_aoss_refresh,
        opensearch_usable=opensearch_usable,
        local_client=local_client,
        rerank_endpoint_name=config.QUALITY_OPT_RERANK_V4_ENDPOINT_NAME,
        rerank_client=rerank_client,
        rerank_client_factory=_rerank_client_factory,
        **_os_target,
    )

    # Author — live Bedrock author on the Prompting_Guidance contract (Req 4.4 / 15.1), with
    # PROVIDER-AWARE temperature handling (Req 2.5–2.7): the temperature is sent only when the
    # configured provider accepts it. The config defaults (accepts_temperature=False) reproduce
    # today's Claude posture exactly (temperature omitted), so this is non-regressing when the
    # cross-family feature is off.
    author: AuthorClient = BedrockAuthorClient(
        resolved_author_model,
        client_factory=author_client_factory,
        credential_profile=config.QUALITY_OPT_AUTHOR_PROFILE,
        accepts_temperature=config.QUALITY_OPT_AUTHOR_ACCEPTS_TEMPERATURE,
        temperature=config.QUALITY_OPT_AUTHOR_TEMPERATURE,
    )

    # Optional cross-family Audit_Judge seam (Req 3.1). Built only when the audit feature is
    # enabled; otherwise the bundle's ``audit_judge`` stays ``None`` and nothing is constructed.
    # Imported lazily so this module never requires the audit module (or boto3) at import time.
    audit_judge: "Optional[AuditJudge]" = None
    if config.QUALITY_OPT_AUDIT_ENABLED:
        from bakeoff.quality.optimizer.audit import AuditJudge as _AuditJudge

        audit_judge = _AuditJudge(
            config.QUALITY_OPT_AUDIT_JUDGE_MODEL_ID,
            client_factory=audit_client_factory,
            declared_family=config.QUALITY_OPT_AUDIT_JUDGE_FAMILY,
        )

    return OptimizerBackend(
        name="live",
        answer_adapter_factory=answer_adapter_factory,
        judge_scorer=judge_scorer,
        closeness_scorer=closeness_scorer,
        retrieval=retrieval,
        author=author,
        audit_judge=audit_judge,
        ragas_adapter=build_ragas_adapter(config.QUALITY_OPT_RAGAS_BACKEND),
    )
