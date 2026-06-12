"""
The Ragas_Adapter (Req 1): generation-quality metrics, offline-first.

This component computes ragas generation-quality metrics for one Instance and
records, for each value, the provenance that makes it reproducible: the metric
name, the numeric value, the ragas version, and the Bedrock model id (Req 1.2).
Values are stored on a 0.0–1.0, higher-is-better scale (Req 1.3, enforced by
:class:`~bakeoff.eval.models.MetricValue`).

Stability is the headline constraint here:

* **ragas is an optional, lazily-handled dependency.** It is **not** installed in
  the test/research environment and MUST NOT be a hard import. The import is
  guarded (``try/except ImportError``) so this module *always* loads and the
  offline mode works with ZERO network and without ragas present (Req 1.5). The
  declared dependency for the live path lives in ``requirements.txt``; nothing
  here relies on it being importable.
* **OFFLINE mode is the default and the only path tests exercise (Req 1.5).** It
  computes deterministic metric values from an **injected fake LLM + fake
  embedding** component, with no network call. The values are a reproducible,
  lexical/embedding function of the sample text — meaningful enough to drive the
  dashboard offline, deterministic enough to test.
* **The live Bedrock path is written behind the guard.** It is reached only when
  ragas is importable AND the adapter is constructed with ``live=True``. It is
  never exercised by the offline test suite. Because a live ragas/Bedrock install
  could not be validated in this environment, its exact wiring is an
  **assumption to confirm at deploy time** (mirroring the ``ASSUMPTION — confirm
  at impl time`` posture already recorded in :mod:`bakeoff.config`).

Per-metric failure isolation (Req 1.4): each enabled metric is computed in its
own ``try``; a metric that raises or yields no value is recorded ``unavailable``
(``value=None``) with its provenance retained, and every *successfully* computed
metric for the same Instance is kept. One bad metric never drops the others.

The metric names produced here are ragas generation-quality names (from
:mod:`bakeoff.eval.catalog`); they are deliberately disjoint from the retrieval
metric names, so the two signals are never conflated in storage (Req 2.4 / P9).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence

from bakeoff import config
from bakeoff.eval.models import MetricValue

# ---------------------------------------------------------------------------
# Guarded ragas import (Req 1.5 stability constraint).
# ragas pulls in a heavy tree (langchain/datasets) and is NOT installed here.
# The except clause makes this module import-safe with or without ragas; the
# offline path below never touches `ragas`.
# ---------------------------------------------------------------------------
try:  # pragma: no cover - import outcome depends on the environment
    import ragas as _ragas  # type: ignore

    RAGAS_AVAILABLE = True
except Exception:  # noqa: BLE001 - ImportError or a broken partial install
    _ragas = None  # type: ignore
    RAGAS_AVAILABLE = False


def _detect_ragas_version() -> Optional[str]:
    """Best-effort installed ragas version, or ``None`` when ragas is absent.

    Tries the package metadata first (works even if the module exposes no
    ``__version__``), then a module attribute. Never raises.
    """
    try:  # pragma: no cover - exercised only where ragas is installed
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("ragas")
        except PackageNotFoundError:
            pass
    except Exception:  # noqa: BLE001
        pass
    if _ragas is not None:  # pragma: no cover
        return getattr(_ragas, "__version__", None)
    return None


#: Provenance markers used when no real ragas runs (offline/fake mode). They make
#: a recorded value's origin honest: it was produced by the injected fakes, not
#: by a live ragas+Bedrock evaluation.
OFFLINE_RAGAS_VERSION: str = "offline-fake"
OFFLINE_BEDROCK_MODEL_ID: str = "offline-fake-llm"

__all__ = [
    "RAGAS_AVAILABLE",
    "RagasSample",
    "FakeLLM",
    "FakeEmbedding",
    "RagasAdapterError",
    "RagasNotInstalledError",
    "RagasAdapter",
]


class RagasAdapterError(RuntimeError):
    """Base error for the Ragas_Adapter."""


class RagasNotInstalledError(RagasAdapterError):
    """Raised when the live Bedrock path is requested but ragas is not importable."""


# ---------------------------------------------------------------------------
# The sample one Instance is scored from
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RagasSample:
    """The inputs one Instance's generation-quality metrics are computed from.

    Mirrors a ragas single-turn sample: the user question, the model's answer,
    the retrieved contexts, and (optionally) a gold reference answer. All text
    is the harness's synthetic, non-PII data (Req 21.3).
    """

    question: str
    answer: str
    contexts: Sequence[str] = field(default_factory=tuple)
    reference: Optional[str] = None


# ---------------------------------------------------------------------------
# Injected fakes (Req 1.5): deterministic, network-free stand-ins
# ---------------------------------------------------------------------------
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def _tokens(text: Optional[str]) -> set[str]:
    """Lowercased word-token set of ``text`` (empty for falsy input)."""
    if not text:
        return set()
    return {m.group(0).lower() for m in _WORD.finditer(text)}


def _containment(a: Optional[str], b: Optional[str]) -> float:
    """Fraction of ``a``'s tokens also present in ``b`` (0.0 when ``a`` empty).

    A deterministic, network-free proxy for "how grounded/relevant is ``a`` given
    ``b``" — meaningful enough to drive the offline dashboard, simple enough to
    hand-check. Always in ``[0, 1]``.
    """
    ta = _tokens(a)
    if not ta:
        return 0.0
    tb = _tokens(b)
    if not tb:
        return 0.0
    return len(ta & tb) / len(ta)


def _jaccard(a: Optional[str], b: Optional[str]) -> float:
    """Token Jaccard similarity of two strings (0.0 when both empty)."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta and not tb:
        return 0.0
    union = ta | tb
    if not union:
        return 0.0
    return len(ta & tb) / len(union)


class FakeEmbedding:
    """Deterministic, network-free stand-in for the ragas Bedrock embedding.

    Similarity is token Jaccard — a pure function of the two strings, so it is
    reproducible and issues no network call (Req 1.5).
    """

    def similarity(self, a: Optional[str], b: Optional[str]) -> float:
        return _jaccard(a, b)


class FakeLLM:
    """Deterministic, network-free stand-in for the ragas Bedrock LLM (Req 1.5).

    :meth:`score` returns a reproducible value in ``[0, 1]`` for a metric/sample
    pair. Context-grounded metrics are scored by the answer's containment in the
    retrieved contexts; answer-comparison metrics by containment in the reference
    (or question when there is no reference); ``noise_sensitivity`` is inverted
    (more grounded ⟹ less noise-sensitive). A tiny deterministic hash jitter
    keeps distinct metrics from collapsing to identical values without
    introducing any randomness.
    """

    #: Metrics scored against the retrieved contexts (grounding-style).
    _CONTEXT_METRICS = frozenset(
        {
            "faithfulness",
            "context_precision",
            "context_recall",
            "context_entities_recall",
            "context_relevance",
            "response_groundedness",
            "noise_sensitivity",
        }
    )
    #: Metrics scored against the reference answer (comparison-style).
    _REFERENCE_METRICS = frozenset(
        {
            "answer_relevancy",
            "response_relevancy",
            "answer_accuracy",
            "factual_correctness",
            "aspect_critic",
            "simple_criteria",
            "rubrics_score",
            "instance_rubrics",
        }
    )

    def _jitter(self, metric_name: str, sample: "RagasSample") -> float:
        """A small deterministic offset in ``[0, 0.05)`` keyed by metric+sample."""
        h = hashlib.sha256(
            f"{metric_name}\x1f{sample.question}\x1f{sample.answer}".encode("utf-8")
        ).hexdigest()
        return (int(h[:8], 16) / 0xFFFFFFFF) * 0.05

    def score(self, metric_name: str, sample: "RagasSample") -> float:
        ctx = " ".join(sample.contexts or ())
        if metric_name in self._CONTEXT_METRICS:
            base = _containment(sample.answer, ctx)
            if metric_name == "noise_sensitivity":
                base = 1.0 - base  # inverse: well-grounded ⟹ low noise sensitivity
        elif metric_name in self._REFERENCE_METRICS:
            target = sample.reference if sample.reference else sample.question
            base = _containment(sample.answer, target)
        else:
            # Unknown metric: a generic answer-vs-question containment.
            base = _containment(sample.answer, sample.question)
        return base + self._jitter(metric_name, sample)


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------
class RagasAdapter:
    """Computes ragas generation-quality metrics for one Instance (Req 1).

    The default and tested path is **offline** (``mode="offline"``): each enabled
    metric is computed from the injected :class:`FakeLLM` + :class:`FakeEmbedding`
    with no network call (Req 1.5). Per-metric failures are isolated (Req 1.4),
    and every value records its provenance (Req 1.2).

    The **live** path (``mode="bedrock"``, ``live=True``) is written behind the
    ragas import guard and is reached only when ragas is importable; it is never
    exercised by the offline tests (see module docstring).
    """

    #: Metrics computed via the embedding component rather than the LLM.
    _EMBEDDING_METRICS = frozenset({"semantic_similarity"})

    def __init__(
        self,
        *,
        mode: str = "offline",
        enabled_metrics: Optional[Iterable[str]] = None,
        llm: Optional[FakeLLM] = None,
        embedding: Optional[FakeEmbedding] = None,
        ragas_version: Optional[str] = None,
        bedrock_model_id: Optional[str] = None,
        live: bool = False,
        prompt_store: Optional[object] = None,
    ) -> None:
        if mode not in ("offline", "bedrock"):
            raise ValueError(f'mode must be "offline" or "bedrock", got {mode!r}')
        self.mode = mode
        self.live = live and mode == "bedrock"
        # Optional Prompt_Store (Req 16). When wired, the config id of the prompt
        # active for each metric AT SCORE TIME is stamped onto every produced
        # value (Req 16.6); reading it live is what makes a prompt change apply
        # only to instances computed after the change (Req 16.5). The store seam
        # is duck-typed (``config_id(metric) -> str``) so this module need not
        # import the prompt-store package.
        self.prompt_store = prompt_store

        # Enabled metrics default to the in-scope catalog metrics (Req 4.4); a
        # caller may pass any subset to enable/disable per run (Req 4.5).
        if enabled_metrics is None:
            from bakeoff.eval import catalog

            enabled_metrics = catalog.default_enabled_names()
        self.enabled_metrics: list[str] = list(enabled_metrics)

        self.llm = llm if llm is not None else FakeLLM()
        self.embedding = embedding if embedding is not None else FakeEmbedding()

        # Provenance recorded on every value (Req 1.2). Offline mode marks itself
        # honestly; the live path records the detected ragas version + the
        # configured Bedrock model id.
        if self.live:
            if not RAGAS_AVAILABLE:
                raise RagasNotInstalledError(
                    "live Bedrock ragas requested (mode='bedrock', live=True) but "
                    "ragas is not importable; install the declared `ragas` "
                    "dependency or use the default offline mode"
                )
            self.ragas_version = ragas_version or _detect_ragas_version() or "unknown"
            self.bedrock_model_id = (
                bedrock_model_id or config.QUALITY_OPT_RAGAS_LLM_MODEL_ID
            )
        else:
            self.ragas_version = ragas_version or OFFLINE_RAGAS_VERSION
            self.bedrock_model_id = bedrock_model_id or OFFLINE_BEDROCK_MODEL_ID

    # --- offline classmethod for ergonomic test/dashboard construction ---
    @classmethod
    def offline(
        cls,
        *,
        enabled_metrics: Optional[Iterable[str]] = None,
        llm: Optional[FakeLLM] = None,
        embedding: Optional[FakeEmbedding] = None,
        ragas_version: str = OFFLINE_RAGAS_VERSION,
        bedrock_model_id: str = OFFLINE_BEDROCK_MODEL_ID,
        prompt_store: Optional[object] = None,
    ) -> "RagasAdapter":
        """Construct an offline adapter (injected fakes, zero network — Req 1.5)."""
        return cls(
            mode="offline",
            enabled_metrics=enabled_metrics,
            llm=llm,
            embedding=embedding,
            ragas_version=ragas_version,
            bedrock_model_id=bedrock_model_id,
            live=False,
            prompt_store=prompt_store,
        )

    # --- the public scoring entry point ---------------------------------
    def score(self, sample: RagasSample) -> dict[str, MetricValue]:
        """Compute every enabled ragas metric for one Instance.

        Returns a dict keyed by metric name. Each value is an available
        :class:`MetricValue` (clamped to ``[0, 1]``, Req 1.3) carrying its
        provenance (Req 1.2), or an ``unavailable`` value if that one metric
        failed or produced no number (Req 1.4) — in which case every other
        metric for the same sample is still returned.
        """
        compute: Callable[[str, RagasSample], Optional[float]] = (
            self._compute_live if self.live else self._compute_offline
        )
        out: dict[str, MetricValue] = {}
        for name in self.enabled_metrics:
            # The prompt configuration active for this metric AT SCORE TIME
            # (Req 16.5/16.6). Read per call so a mid-run prompt change applies
            # only to instances scored after it; ``None`` when no store is wired.
            prompt_config_id = self._prompt_config_id(name)
            try:
                value = compute(name, sample)
            except Exception:  # noqa: BLE001 - isolate per-metric failure (Req 1.4)
                value = None
            if value is None:
                out[name] = MetricValue.missing(
                    ragas_version=self.ragas_version,
                    bedrock_model_id=self.bedrock_model_id,
                    prompt_config_id=prompt_config_id,
                )
            else:
                out[name] = MetricValue.available(
                    value,
                    ragas_version=self.ragas_version,
                    bedrock_model_id=self.bedrock_model_id,
                    prompt_config_id=prompt_config_id,
                )
        return out

    def _prompt_config_id(self, metric_name: str) -> Optional[str]:
        """The active prompt-config id for ``metric_name``, or ``None`` (Req 16.6).

        Delegates to the wired Prompt_Store's ``config_id`` (read live so a prompt
        change applies only to later instances, Req 16.5). Never raises: a store
        that errors degrades to ``None`` rather than failing the whole score.
        """
        store = self.prompt_store
        if store is None:
            return None
        try:
            return store.config_id(metric_name)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - provenance is best-effort, never fatal
            return None

    # --- offline computation (the tested path) --------------------------
    def _compute_offline(self, metric_name: str, sample: RagasSample) -> float:
        """Deterministic, network-free score for one metric (Req 1.5).

        Embedding-only metrics route through the fake embedding; every other
        metric routes through the fake LLM. The returned value is clamped to
        ``[0, 1]`` again by :class:`MetricValue` on construction (Req 1.3).
        """
        if metric_name in self._EMBEDDING_METRICS:
            return self.embedding.similarity(sample.answer, sample.reference)
        return self.llm.score(metric_name, sample)

    # --- live Bedrock computation (behind the guard; not tested) --------
    def _compute_live(self, metric_name: str, sample: RagasSample) -> Optional[float]:
        """Score one metric via live ragas + its Amazon Bedrock adapter (Req 1.1).

        Reached only when ``ragas`` is importable and the adapter was built with
        ``live=True`` (see :meth:`__init__`). The exact ragas+Bedrock wiring
        (LLM/embedding wrapper construction and the per-metric
        ``single_turn_ascore`` call) is an **assumption to confirm at deploy
        time**: a live ragas/Bedrock install could not be validated in this
        environment, so this method is intentionally left as a single, isolated
        seam rather than speculative untested wiring. It raises so a
        misconfigured live run fails loudly (the per-metric ``try`` in
        :meth:`score` then records the metric unavailable, Req 1.4) instead of
        silently fabricating a number.
        """
        raise RagasNotInstalledError(
            "live ragas+Bedrock metric computation is a deploy-time seam that "
            "must be wired and validated against a live ragas install; it is "
            "intentionally not implemented in the offline research environment"
        )
