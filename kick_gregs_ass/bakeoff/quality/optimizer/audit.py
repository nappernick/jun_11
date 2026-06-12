"""
Cross-family audit seam for the closed-loop prompt optimizer
(spec optimizer-cross-family-eval, Req 3; design Component "audit.py (NEW)").

The Author swap (Req 2) makes the *rewriter* a different Family from the Judge, but the two
fixed Claude Target_Models are still graded by the Opus Judge — so a Judge-to-target
self-preference (the "Opus likes Claude house style") can still inflate the in-loop signal.
This module adds the detector the Author swap alone cannot provide: a periodic **non-Claude
Audit_Judge** that re-scores the current winner on a sample, with light authorship/style
**obfuscation** applied to the material before scoring, plus a **proxy-vs-audit divergence**
check that flags a potential self-preference (Goodhart) condition when the two rankings
disagree more than a configurable threshold.

Everything here that can be pure *is* pure (no I/O, no global state) so it is exhaustively
property-testable:

* :func:`obfuscate` — idempotent authorship/style scrubbing applied BEFORE the Audit_Judge
  sees any material (Req 3.3).
* :func:`ranking_divergence` — the normalized Kendall-tau distance (fraction of discordant
  pairs) in ``[0, 1]``, symmetric, identity-zero, ``1.0`` for a fully reversed order (Req 3.4).
* :func:`evaluate_self_preference` — wraps the divergence in a :class:`DivergenceReport` and
  flags iff it strictly exceeds the threshold (Req 3.5).
* :class:`AuditSeam` — owns the interval cadence (Req 3.2): ``maybe_run`` obfuscates the
  sample, scores it with the Audit_Judge, and returns a report only on an audit round.

The only I/O is the Audit_Judge Bedrock call, built lazily through an injectable
``client_factory`` exactly like
:class:`bakeoff.quality.optimizer.author.BedrockAuthorClient`, so importing this module needs
no boto3 and tests inject a fake (no real AWS, Req 4.5 — reuses the existing Bedrock
credential chain, introduces no new secret).

Sourcing caveat (carried from requirements.md / design.md): cross-family / cross-model
evaluation, authorship obfuscation, and the proxy-vs-audit divergence Goodhart check are
**external / industry** techniques (``docs/solo-model-prompt-iteration.md`` and the GEPA /
RAGAS / self-preference literature it cites), **not** Amazon-internal guidance. The
Audit_Judge model id (Assumption A3) is a config-driven **placeholder to confirm** against a
live Bedrock check at implementation time; nothing here hardcodes an unverified id.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable, Optional, Sequence

from bakeoff import config
from bakeoff.resilience import call_with_resilience

__all__ = [
    "AuditSample",
    "AuditItem",
    "DivergenceReport",
    "obfuscate",
    "contains_authorship_markers",
    "ranking_divergence",
    "evaluate_self_preference",
    "AuditJudge",
    "AuditSeam",
]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AuditSample:
    """One sampled conversation handed to the seam, with RAW (un-obfuscated) material.

    The caller (the orchestrator) draws these from the Phase A tuning slice and attaches the
    proxy (Opus) score the in-loop study already assigned the conversation. The seam
    obfuscates ``material`` into an :class:`AuditItem` before it ever reaches the Audit_Judge
    (Req 3.3).
    """

    item_id: str
    material: str  # raw conversation material (answers / instruction) before obfuscation
    proxy_score: float  # the proxy (Opus) score already assigned by the in-loop study


@dataclass(frozen=True)
class AuditItem:
    """One sampled conversation's material to audit (POST-obfuscation), plus its proxy score.

    Produced from an :class:`AuditSample` by :func:`obfuscate`; this is the only shape the
    Audit_Judge ever receives, so it never sees a raw authorship/style marker (Req 3.3).
    """

    item_id: str
    obfuscated_material: str
    proxy_score: float


@dataclass(frozen=True)
class DivergenceReport:
    """The outcome of one audit (Req 3.4 / 3.5).

    ``divergence`` is the normalized ranking disagreement in ``[0, 1]`` between the proxy
    (Opus) ranking and the Audit ranking over the same ``n_items`` conversations; ``flagged``
    is ``True`` iff ``divergence`` strictly exceeds ``threshold`` (Req 3.5).
    """

    n_items: int
    proxy_scores: tuple[float, ...]
    audit_scores: tuple[float, ...]
    divergence: float
    threshold: float
    flagged: bool

    def to_dict(self) -> dict:
        """Return a JSON-ready dict of this report (field-declaration order)."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Obfuscation (Req 3.3) — pure, idempotent authorship/style scrubbing
# ---------------------------------------------------------------------------
#: Neutral replacement token for any model/provider name mention.
_NEUTRAL_MODEL: str = "[model]"

#: Model / provider name mentions — the strongest authorship tell. Word-bounded and
#: case-insensitive so "Claude"/"claude" both neutralize but "innovation" (containing "nova"
#: only mid-word) does not.
_MODEL_NAME_RE: re.Pattern[str] = re.compile(
    r"\b(?:claude|opus|sonnet|haiku|anthropic|gpt-?\d*|gemini|llama|mistral|"
    r"nova|deepseek|cohere|titan|qwen)\b",
    re.IGNORECASE,
)

#: The island authoring-stance sentinel block (and any trailing whitespace) — a house-style
#: provenance marker the optimizer threads through the author contract. DOTALL so a
#: multi-line stance is consumed; non-greedy so adjacent blocks strip independently.
_STANCE_BLOCK_RE: re.Pattern[str] = re.compile(
    r"<<<ISLAND_AUTHORING_STANCE>>>.*?<<<END_ISLAND_AUTHORING_STANCE>>>\s*",
    re.DOTALL,
)

#: A residual stance header line (if the sentinels were already stripped upstream).
_STANCE_HEADER_RE: re.Pattern[str] = re.compile(
    r"(?im)^\s*authoring stance for this island.*$"
)


def contains_authorship_markers(text: str) -> bool:
    """Return whether ``text`` still carries any known authorship/style marker.

    The companion predicate to :func:`obfuscate`: a model/provider name mention, an island
    authoring-stance sentinel block, or a residual stance header. ``obfuscate(x)`` is
    guaranteed to leave no marker, so ``contains_authorship_markers(obfuscate(x))`` is always
    ``False`` (the property the audit path relies on).
    """
    t = text or ""
    return bool(
        _STANCE_BLOCK_RE.search(t)
        or _STANCE_HEADER_RE.search(t)
        or _MODEL_NAME_RE.search(t)
    )


def obfuscate(material: str) -> str:
    """Light authorship/style obfuscation applied to material BEFORE the Audit_Judge (Req 3.3).

    Strips/neutralizes authorship and house-style markers so the Audit_Judge grades content,
    not provenance: the island authoring-stance sentinel blocks (and any residual stance
    header) are removed, and every model/provider name mention is replaced with a neutral
    token. Pure and **idempotent**: the neutral token carries no marker and the removed blocks
    are gone, so ``obfuscate(obfuscate(x)) == obfuscate(x)`` and
    ``contains_authorship_markers(obfuscate(x))`` is ``False`` for any input.
    """
    if not material:
        return ""
    text = _STANCE_BLOCK_RE.sub("", material)
    text = _STANCE_HEADER_RE.sub("", text)
    text = _MODEL_NAME_RE.sub(_NEUTRAL_MODEL, text)
    return text


# ---------------------------------------------------------------------------
# Ranking divergence (Req 3.4) — normalized Kendall-tau distance
# ---------------------------------------------------------------------------
def ranking_divergence(
    proxy_scores: Sequence[float], audit_scores: Sequence[float]
) -> float:
    """Normalized rank-disagreement between two equal-length score vectors (Req 3.4).

    Returns the **normalized Kendall-tau distance** — the fraction of item pairs the two
    vectors order discordantly — in ``[0.0, 1.0]``: ``0.0`` when the two induce the same order
    (in particular ``ranking_divergence(x, x) == 0.0``, including with ties), ``1.0`` when one
    is the strict reverse of the other. The measure is **symmetric** (swapping the arguments
    does not change it). A degenerate input with fewer than two items (no pairs to compare)
    returns ``0.0`` — never raises and never reports spurious disagreement.

    Raises:
        ValueError: if the two vectors are not the same length (an audit always compares the
            proxy and audit scores of the SAME sampled items).
    """
    a = [float(x) for x in proxy_scores]
    b = [float(x) for x in audit_scores]
    if len(a) != len(b):
        raise ValueError(
            "ranking_divergence requires equal-length score vectors (the proxy and audit "
            f"scores of the same sampled items): got {len(a)} vs {len(b)}."
        )
    n = len(a)
    if n < 2:
        return 0.0
    discordant = 0
    total = 0
    for i in range(n):
        for j in range(i + 1, n):
            total += 1
            da = a[i] - a[j]
            db = b[i] - b[j]
            # Discordant iff the two STRICT orderings of the pair disagree. Tied pairs in
            # either vector are neither concordant nor discordant (so x-vs-x is always 0.0).
            if (da > 0 and db < 0) or (da < 0 and db > 0):
                discordant += 1
    return (discordant / total) if total else 0.0


def evaluate_self_preference(
    proxy_scores: Sequence[float],
    audit_scores: Sequence[float],
    *,
    threshold: float = config.QUALITY_OPT_AUDIT_DIVERGENCE_THRESHOLD,
) -> DivergenceReport:
    """Compute the divergence and flag a potential self-preference condition (Req 3.4 / 3.5).

    Builds a :class:`DivergenceReport` whose ``flagged`` is ``True`` **iff** the computed
    :func:`ranking_divergence` strictly exceeds ``threshold`` (Req 3.5). A degenerate sample
    (empty / single item) yields divergence ``0.0`` and therefore never flags.
    """
    proxy = tuple(float(x) for x in proxy_scores)
    audit = tuple(float(x) for x in audit_scores)
    divergence = ranking_divergence(proxy, audit)
    return DivergenceReport(
        n_items=len(proxy),
        proxy_scores=proxy,
        audit_scores=audit,
        divergence=divergence,
        threshold=float(threshold),
        flagged=divergence > float(threshold),
    )


# ---------------------------------------------------------------------------
# Audit_Judge (Req 3.1) — the non-Claude Bedrock judge seam
# ---------------------------------------------------------------------------
ClientFactory = Callable[[], Any]


def _parse_audit_score(text: str) -> float:
    """Extract a ``[0, 1]`` quality score from the Audit_Judge output; default ``0.0``.

    Tolerant: takes the first decimal number found in ``text`` and clamps it to ``[0, 1]``.
    A missing/unparseable response yields ``0.0`` so a degenerate Audit_Judge reply never
    crashes the (observability-only) audit.
    """
    match = re.search(r"-?\d+(?:\.\d+)?", text or "")
    if not match:
        return 0.0
    try:
        value = float(match.group(0))
    except ValueError:
        return 0.0
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


class AuditJudge:
    """A non-Claude Bedrock judge used only for periodic auditing (Req 3.1).

    Mirrors :class:`bakeoff.quality.optimizer.author.BedrockAuthorClient`'s lazy-client +
    credential-resilience posture (reuses the existing Bedrock credential chain via the
    broker; introduces no new secret, Req 4.5). The model id is config-driven
    (``QUALITY_OPT_AUDIT_JUDGE_MODEL_ID``, Assumption A3) and treated as a placeholder to
    confirm at implementation time. The Bedrock client is built lazily through an injectable
    ``client_factory`` so importing this module needs no boto3 and tests pass a fake client
    that makes no real call.

    It scores each :class:`AuditItem`'s already-obfuscated material with a simple grounded
    quality rubric and parses a ``[0, 1]`` score; it never re-obfuscates (the seam obfuscates
    before handing items here, Req 3.3) and never decides a promotion — it only produces the
    audit ranking the divergence check consumes.
    """

    def __init__(
        self,
        audit_model: Optional[str] = None,
        region: Optional[str] = None,
        *,
        client: Optional[Any] = None,
        client_factory: Optional[ClientFactory] = None,
        declared_family: Optional[str] = None,
        max_tokens: int = 1024,
        sleep: "Optional[Callable[[float], Awaitable[None]]]" = None,
    ) -> None:
        self.audit_model = audit_model
        self.declared_family = declared_family
        self.region = region or config.AWS_REGION
        self.max_tokens = int(max_tokens)
        self._client_factory = client_factory or self._default_client_factory
        self._client = client
        self._sleep = sleep or asyncio.sleep
        #: number of credential refreshes performed (observability / test hook).
        self.refresh_count = 0

    # -- client lifecycle / credential chain ------------------------------
    def _default_client_factory(self) -> Any:
        """Build a ``bedrock-runtime`` client via the credential broker (lazy)."""
        from bakeoff.credentials import get_broker

        session = get_broker().get_session(region=self.region)
        return session.client("bedrock-runtime", region_name=self.region)

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def _refresh_credentials(self) -> None:
        """Mint fresh credentials via the broker, then rebuild the client (refresh hook)."""
        from bakeoff.credentials import get_broker

        self.refresh_count += 1
        try:
            get_broker().refresh()
        except Exception:  # noqa: BLE001 — never let a refresh failure be worse than a rebuild
            import logging

            logging.getLogger("bakeoff.credentials").warning(
                "audit-judge credential refresh via broker failed; rebuilding from disk",
                exc_info=True,
            )
        self._client = self._client_factory()

    # -- the blocking judge call (runs in a worker thread) ----------------
    def _build_audit_prompt(self, obfuscated_material: str) -> str:
        """Build the Audit_Judge prompt for one already-obfuscated conversation."""
        return (
            "You are an impartial evaluator. Read the following anonymized assistant material "
            "from a grounded, abstention-aware FAQ task and rate its overall quality "
            "(grounding, correctness, and correct abstention when evidence is insufficient) on "
            "a 0.0 to 1.0 scale. Reply with the single number only.\n\n"
            "<material>\n"
            f"{obfuscated_material}\n"
            "</material>\n\n"
            "Score (0.0-1.0):"
        )

    def _invoke_sync(self, prompt: str) -> str:
        """Issue one blocking Bedrock ``converse`` call and return the response text."""
        if not self.audit_model:
            raise ValueError(
                "AuditJudge.audit_model is unset: configure QUALITY_OPT_AUDIT_JUDGE_MODEL_ID "
                "(Assumption A3 — a non-Claude Bedrock judge, confirm against a live Bedrock "
                "check) before enabling the audit seam."
            )
        client = self._get_client()
        response = client.converse(
            modelId=self.audit_model,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": self.max_tokens},
        )
        try:
            blocks = response["output"]["message"]["content"]
            return "".join(b.get("text", "") for b in blocks)
        except (KeyError, TypeError, AttributeError):
            return ""

    async def score_one(self, item: AuditItem) -> float:
        """Score one already-obfuscated :class:`AuditItem`, with credential resilience."""
        prompt = self._build_audit_prompt(item.obfuscated_material)

        async def attempt() -> str:
            return await asyncio.to_thread(self._invoke_sync, prompt)

        text = await call_with_resilience(
            attempt,
            refresh_credentials=self._refresh_credentials,
            sleep=self._sleep,
        )
        return _parse_audit_score(text)

    async def score_sample(self, items: Sequence[AuditItem]) -> list[float]:
        """Score a sample of already-obfuscated items → an aligned list of ``[0, 1]`` scores.

        Each item is scored independently and concurrently (bounded by the judge concurrency
        cap), preserving input order so the returned scores align 1:1 with ``items`` for the
        divergence check.
        """
        if not items:
            return []
        sem = asyncio.Semaphore(max(1, int(config.CONCURRENCY_CAPS.get("judge", 4))))

        async def run(idx: int, it: AuditItem) -> tuple[int, float]:
            async with sem:
                return idx, await self.score_one(it)

        results = await asyncio.gather(*(run(i, it) for i, it in enumerate(items)))
        ordered = sorted(results, key=lambda pair: pair[0])
        return [score for _, score in ordered]


# ---------------------------------------------------------------------------
# AuditSeam (Req 3.2) — interval cadence + the run path
# ---------------------------------------------------------------------------
class AuditSeam:
    """Own the audit interval cadence and the obfuscate → score → divergence run (Req 3.2).

    ``maybe_run`` returns a :class:`DivergenceReport` only on an audit round — when the seam
    is enabled, an Audit_Judge is present, and ``round_index`` lands on the configured
    ``QUALITY_OPT_AUDIT_INTERVAL`` (every ``interval`` rounds, starting at round
    ``interval``) — and ``None`` on every other round or when the seam is disabled. On an
    audit round it obfuscates each sampled conversation's material (Req 3.3), scores the
    obfuscated items with the Audit_Judge, and evaluates the proxy-vs-audit divergence /
    self-preference flag (Req 3.4 / 3.5).
    """

    def __init__(
        self,
        *,
        audit_judge: "Optional[AuditJudge]" = None,
        enabled: bool = config.QUALITY_OPT_AUDIT_ENABLED,
        interval: int = config.QUALITY_OPT_AUDIT_INTERVAL,
        threshold: float = config.QUALITY_OPT_AUDIT_DIVERGENCE_THRESHOLD,
    ) -> None:
        self._audit_judge = audit_judge
        self._enabled = bool(enabled)
        self._interval = int(interval)
        self._threshold = float(threshold)

    @classmethod
    def from_backend(cls, backend: Any) -> "AuditSeam":
        """Build a seam bound to a backend's ``audit_judge`` (read duck-typed; config-gated).

        The orchestrator uses this: with the audit feature disabled (or no ``audit_judge`` on
        the bundle) the resulting seam's :meth:`maybe_run` is a no-op.
        """
        return cls(audit_judge=getattr(backend, "audit_judge", None))

    @property
    def enabled(self) -> bool:
        """Whether the seam is active (the feature is on AND an Audit_Judge is wired)."""
        return self._enabled and self._audit_judge is not None

    def is_audit_round(self, round_index: int) -> bool:
        """Whether ``round_index`` is an audit round under the configured interval (Req 3.2).

        Fires on rounds ``interval, 2*interval, ...`` (never round 0); always ``False`` when
        the seam is disabled, has no Audit_Judge, or the interval is non-positive.
        """
        if not self.enabled or self._interval < 1:
            return False
        if round_index < 1:
            return False
        return (round_index % self._interval) == 0

    async def maybe_run(
        self, *, round_index: int, samples: Sequence[AuditSample]
    ) -> Optional[DivergenceReport]:
        """Run an audit on an audit round, else return ``None`` (Req 3.2 – 3.5).

        On an audit round with a non-empty sample: obfuscate each sample's material into an
        :class:`AuditItem` (Req 3.3 — the Audit_Judge never receives raw material), score the
        obfuscated items with the Audit_Judge, and return
        :func:`evaluate_self_preference` over the proxy and audit scores (Req 3.4 / 3.5).
        Returns ``None`` on a non-audit round, when the seam is disabled, or when the sample
        is empty.
        """
        if not self.is_audit_round(round_index):
            return None
        items = [
            AuditItem(
                item_id=s.item_id,
                obfuscated_material=obfuscate(s.material),
                proxy_score=float(s.proxy_score),
            )
            for s in samples
        ]
        if not items:
            return None
        audit_scores = await self._audit_judge.score_sample(items)  # type: ignore[union-attr]
        proxy_scores = [it.proxy_score for it in items]
        return evaluate_self_preference(
            proxy_scores, audit_scores, threshold=self._threshold
        )
