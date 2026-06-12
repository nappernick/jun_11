"""
Ragas_Adapter — the Tier-1 seam that runs ragas RAG-eval metrics as a SECONDARY,
non-deciding signal (spec: optimizer-ragas-gepa; design Component C1; Req 1, 2, 4, 5).

ragas (`Faithfulness`, `FactualCorrectness`, `ContextPrecision`, `ContextRecall`) is wired in
exactly as the existing :class:`~bakeoff.quality.closeness.TurnClosenessScorer` is — a
cross-check recorded on every :class:`~bakeoff.quality.optimizer.judge_loop.TurnVerdict` that
**never** decides promotion (the Opus judge triad stays the sole decision metric, Req 11).
Two roles:

* **cross_check** → ragas Faithfulness + FactualCorrectness, computed from the **same** answer
  text and **same** retrieved fragments the Judge received (Req 1.2 / 13.3).
* **retrieval_diagnostic** → ragas ContextPrecision + ContextRecall against the turn's gold
  reference, plus a gold-node-presence flag answering "is the gold node actually present in
  the retrieved fragments?" — the mechanized form of the `optimizer-quality-uplift` Effort A
  diagnosis (Req 2).

This module mirrors :mod:`bakeoff.quality.optimizer.retrieval`'s structure and import
discipline exactly: a Protocol + a deterministic network-free fake + a live Bedrock adapter
+ a selector, with **no** `ragas`/boto3 import at module load (the live adapter imports
`ragas` lazily inside its methods). Importing this module, and running the whole offline
suite, therefore works whether or not `ragas` is installed (Req 5.1 / 5.2 / 16.4).

Failure tolerance (Req 3.5): every metric is computed independently and any failure (including
`ragas` not being installed) is caught and returned as ``None`` for that signal — the turn's
verdict is always complete on the Judge triad, and the loop never crashes on a ragas error.

Sourcing caveat (Req 18): ragas is an EXTERNAL open-source framework, not Amazon-internal
guidance; the live Bedrock model ids are config-driven assumptions to confirm at
implementation time, and any ragas-derived number must be re-validated before it is used to
defend a decision upward.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from bakeoff import config

__all__ = [
    "RagasSignals",
    "RagasAdapter",
    "FakeRagasAdapter",
    "BedrockRagasAdapter",
    "build_ragas_adapter",
]

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class RagasSignals:
    """The ragas signals for one turn — all secondary, all optional, none deciding.

    Every field is ``Optional`` so a metric that was not requested (its flag is off) or that
    failed to compute is simply absent (``None``) rather than a fabricated value (Req 3.5).
    ``backend`` records which adapter produced the signals so a reader can tell an offline
    (``"fake"``) signal from a live (``"bedrock"``) one (Req 5.4).
    """

    faithfulness: Optional[float] = None
    factual_correctness: Optional[float] = None
    context_precision: Optional[float] = None
    context_recall: Optional[float] = None
    gold_node_present: Optional[bool] = None
    backend: Optional[str] = None


@runtime_checkable
class RagasAdapter(Protocol):
    """The ragas seam the loop depends on — a stable ``name`` + two async metric methods.

    The adapter is injected through the :class:`~bakeoff.quality.optimizer.backends.OptimizerBackend`
    bundle (the same one move that swaps judge / closeness / retrieval), so the whole outside
    world is swapped together (Req 5.3). Both methods are **read-only** and operate **only** on
    the inputs handed to them — the diagnostic never issues its own retrieval query (Req 2.2 /
    13.3). Each returns ``None`` for any signal it could not compute (Req 3.5).
    """

    name: str  # "fake" | "bedrock"

    async def cross_check(
        self,
        *,
        answer_text: str,
        fragments: Sequence[Mapping[str, Any]],
        reference_texts: Sequence[str],
        question: str = "",
    ) -> tuple[Optional[float], Optional[float]]:
        """Return ``(faithfulness, factual_correctness)`` for the answer (Req 1)."""
        ...

    async def retrieval_diagnostic(
        self,
        *,
        fragments: Sequence[Mapping[str, Any]],
        reference_texts: Sequence[str],
        gold_node_ids: Sequence[str],
    ) -> tuple[Optional[float], Optional[float], Optional[bool]]:
        """Return ``(context_precision, context_recall, gold_node_present)`` (Req 2)."""
        ...


# ---------------------------------------------------------------------------
# Shared, dependency-free text helpers (used by the offline fake)
# ---------------------------------------------------------------------------
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'-]*")
_STOP = frozenset(
    "a an the of to in on at for from by with and or but if is are was were be this that "
    "you your i we they it he she please contact your support team help can do does".split()
)


def _content_words(text: str) -> set[str]:
    """Lowercased content-word set for ``text`` (stopwords removed); ``set()`` when empty."""
    return {
        w
        for w in (m.group(0).lower() for m in _WORD.finditer(text or ""))
        if w not in _STOP
    }


def _coverage(by: set[str], of: set[str]) -> float:
    """Fraction of ``of``'s words that appear in ``by`` (``0.0`` when ``of`` is empty)."""
    if not of:
        return 0.0
    return len(by & of) / len(of)


def _fragment_ids(fragments: Sequence[Mapping[str, Any]]) -> set[str]:
    """The set of fragment ids (string-coerced), mirroring ``grounding_fragment_ids``."""
    return {str(f.get("id", "")) for f in fragments if f.get("id") is not None}


def _joined_text(fragments: Sequence[Mapping[str, Any]]) -> str:
    """All fragment texts joined — the grounding corpus a faithfulness check reads."""
    return "\n".join(str(f.get("text", "")) for f in fragments)


class FakeRagasAdapter:
    """Deterministic, network-free ragas double for offline runs/tests (Req 5.1 / 5.2).

    Produces plausible ragas-shaped signals from pure content-word overlap — zero sockets,
    zero boto3, zero ``ragas`` import — so the full offline loop runs with no network and no
    dependency on ragas being installed:

    * **faithfulness** — fraction of the answer's content words grounded in the fragments;
    * **factual_correctness** — overlap between the answer and the gold reference;
    * **context_precision** — fraction of fragments that share content with the reference;
    * **context_recall** — fraction of the reference's content words covered by the fragments;
    * **gold_node_present** — exact set intersection of ``gold_node_ids`` with the fragment ids
      (``None`` when the turn carries no gold node id, e.g. a later ``wants``-only turn).

    All outputs are a deterministic function of the inputs (no randomness), in ``[0, 1]``, so
    a test can assert exact values and a re-run is reproducible.
    """

    name = "fake"

    async def cross_check(
        self,
        *,
        answer_text: str,
        fragments: Sequence[Mapping[str, Any]],
        reference_texts: Sequence[str],
        question: str = "",
    ) -> tuple[Optional[float], Optional[float]]:
        answer_words = _content_words(answer_text)
        if not answer_words:
            # An empty/refusal answer makes no unsupported claims: faithfulness is vacuously
            # high, factual correctness undefined against a non-empty reference -> 0.0.
            faithfulness = 1.0 if not _content_words(_joined_text(fragments)) else 1.0
            ref_words = _content_words(" ".join(reference_texts))
            return faithfulness, (0.0 if ref_words else None)
        frag_words = _content_words(_joined_text(fragments))
        faithfulness = round(_coverage(frag_words, answer_words), 4)
        ref_words = _content_words(" ".join(reference_texts))
        factual = round(_coverage(answer_words, ref_words), 4) if ref_words else None
        return faithfulness, factual

    async def retrieval_diagnostic(
        self,
        *,
        fragments: Sequence[Mapping[str, Any]],
        reference_texts: Sequence[str],
        gold_node_ids: Sequence[str],
    ) -> tuple[Optional[float], Optional[float], Optional[bool]]:
        ref_words = _content_words(" ".join(reference_texts))
        frags = list(fragments)
        if ref_words and frags:
            # precision: fraction of fragments that share >=1 reference content word.
            relevant = sum(
                1 for f in frags if _content_words(str(f.get("text", ""))) & ref_words
            )
            context_precision: Optional[float] = round(relevant / len(frags), 4)
            # recall: fraction of reference content words present across all fragments.
            context_recall: Optional[float] = round(
                _coverage(_content_words(_joined_text(frags)), ref_words), 4
            )
        else:
            context_precision = None if not ref_words else 0.0
            context_recall = None if not ref_words else 0.0
        gold_ids = {str(g) for g in gold_node_ids if g}
        gold_node_present = bool(gold_ids & _fragment_ids(frags)) if gold_ids else None
        return context_precision, context_recall, gold_node_present


class BedrockRagasAdapter:
    """LIVE ragas adapter — runs the four metrics through ragas' Amazon Bedrock integration.

    Lazily imports ``ragas`` inside its methods (never at module load, mirroring
    :class:`~bakeoff.quality.optimizer.retrieval.OpenSearchRetrievalBackend`), wraps the
    Bedrock eval LLM (``config.QUALITY_OPT_RAGAS_LLM_MODEL_ID``) and Embed v4
    (``config.QUALITY_OPT_RAGAS_EMBED_MODEL_ID``) — both config-driven, never hard-coded
    (Req 4.2 / 4.3) — and evaluates one single-turn sample per call. Every metric is wrapped
    so a failure (including ``ragas`` not being installed, or a Bedrock error) is logged at
    ``WARNING`` with ``exc_info=True`` and returned as ``None`` for that signal (Req 3.5) —
    never a bare ``except: pass``.

    IMPORTANT (Req 4.4 / 18.3): ``ragas`` is NOT installed in this environment and the exact
    ragas Bedrock-wrapper API is version-dependent, so the metric-invocation internals below
    are an **assumption to confirm when ``ragas`` is installed**. The contract (lazy import,
    config-driven models, per-metric ``None``-on-failure, graceful absence) is what is
    guaranteed; the precise ragas call surface must be re-validated against the installed
    ragas version before any live ragas number is trusted.
    """

    name = "bedrock"

    def __init__(
        self,
        llm_model_id: Optional[str] = None,
        embed_model_id: Optional[str] = None,
        *,
        region: Optional[str] = None,
        llm_client: Optional[Any] = None,
        embed_client: Optional[Any] = None,
    ) -> None:
        self.llm_model_id = llm_model_id or config.QUALITY_OPT_RAGAS_LLM_MODEL_ID
        self.embed_model_id = embed_model_id or config.QUALITY_OPT_RAGAS_EMBED_MODEL_ID
        self.region = region or config.AWS_REGION
        self._llm_client = llm_client
        self._embed_client = embed_client
        self._ragas_ready: Optional[bool] = None

    def _ensure_ragas(self) -> Any:
        """Lazily import ``ragas`` (never at module load); raise a clear error if absent.

        Returns the imported ``ragas`` module. The caller wraps this in ``try/except`` so a
        missing ``ragas`` degrades to ``None`` signals rather than crashing the loop (Req 3.5).
        """
        import importlib

        try:
            return importlib.import_module("ragas")
        except ImportError as exc:  # ragas not installed in this environment
            raise RuntimeError(
                "BedrockRagasAdapter requires the 'ragas' package, which is not installed. "
                "Install it deliberately (it pulls a heavy langchain/datasets tree) or set "
                "QUALITY_OPT_RAGAS_BACKEND='fake' to use the deterministic offline adapter."
            ) from exc

    async def cross_check(
        self,
        *,
        answer_text: str,
        fragments: Sequence[Mapping[str, Any]],
        reference_texts: Sequence[str],
        question: str = "",
    ) -> tuple[Optional[float], Optional[float]]:
        faithfulness: Optional[float] = None
        factual: Optional[float] = None
        try:
            faithfulness = await self._run_metric(
                "faithfulness",
                question=question,
                answer=answer_text,
                contexts=[str(f.get("text", "")) for f in fragments],
                reference=" ".join(reference_texts),
            )
        except Exception:  # noqa: BLE001 — per-metric tolerance (Req 3.5)
            _LOG.warning("ragas Faithfulness failed; recording None", exc_info=True)
        try:
            factual = await self._run_metric(
                "factual_correctness",
                question=question,
                answer=answer_text,
                contexts=[str(f.get("text", "")) for f in fragments],
                reference=" ".join(reference_texts),
            )
        except Exception:  # noqa: BLE001
            _LOG.warning("ragas FactualCorrectness failed; recording None", exc_info=True)
        return faithfulness, factual

    async def retrieval_diagnostic(
        self,
        *,
        fragments: Sequence[Mapping[str, Any]],
        reference_texts: Sequence[str],
        gold_node_ids: Sequence[str],
    ) -> tuple[Optional[float], Optional[float], Optional[bool]]:
        context_precision: Optional[float] = None
        context_recall: Optional[float] = None
        contexts = [str(f.get("text", "")) for f in fragments]
        reference = " ".join(reference_texts)
        try:
            context_precision = await self._run_metric(
                "context_precision", question="", answer="", contexts=contexts, reference=reference
            )
        except Exception:  # noqa: BLE001
            _LOG.warning("ragas ContextPrecision failed; recording None", exc_info=True)
        try:
            context_recall = await self._run_metric(
                "context_recall", question="", answer="", contexts=contexts, reference=reference
            )
        except Exception:  # noqa: BLE001
            _LOG.warning("ragas ContextRecall failed; recording None", exc_info=True)
        # gold-node presence is a pure set check — it needs no ragas call and never fails.
        gold_ids = {str(g) for g in gold_node_ids if g}
        gold_node_present = bool(gold_ids & _fragment_ids(fragments)) if gold_ids else None
        return context_precision, context_recall, gold_node_present

    async def _run_metric(
        self,
        metric_name: str,
        *,
        question: str,
        answer: str,
        contexts: Sequence[str],
        reference: str,
    ) -> Optional[float]:
        """Evaluate one ragas metric on a single sample (off the event loop).

        ASSUMPTION TO CONFIRM (Req 4.4): the concrete wiring of ragas' Bedrock LLM/embeddings
        wrapper and single-sample evaluation API is version-dependent and unverified here
        (ragas is not installed). This builds the ragas LLM/embeddings from the config model
        ids and evaluates the named metric on one sample; re-validate the call surface against
        the installed ragas version before trusting any number. Runs in a worker thread because
        ragas' evaluate path is synchronous and network-bound.
        """
        import asyncio

        ragas = self._ensure_ragas()  # raises if absent → caller maps to None

        def _evaluate() -> Optional[float]:
            # NOTE: kept intentionally defensive and small; the exact ragas symbols differ
            # across the v0.2/v0.3/v0.4 line. This resolves the metric object dynamically and
            # single-sample-evaluates it through ragas' Bedrock-wrapped models. Confirm at
            # install time (Req 4.4 / 18.3).
            from ragas import evaluate as ragas_evaluate  # type: ignore
            from ragas import metrics as ragas_metrics  # type: ignore
            from datasets import Dataset  # type: ignore

            metric_obj = getattr(ragas_metrics, metric_name, None)
            if metric_obj is None:
                raise RuntimeError(f"ragas has no metric named {metric_name!r}")
            sample = {
                "question": [question],
                "answer": [answer],
                "contexts": [list(contexts)],
                "ground_truth": [reference],
            }
            llm, embeddings = self._build_ragas_models(ragas)
            result = ragas_evaluate(
                Dataset.from_dict(sample),
                metrics=[metric_obj],
                llm=llm,
                embeddings=embeddings,
            )
            scores = result.to_pandas().to_dict(orient="records") if hasattr(result, "to_pandas") else []
            if scores and metric_name in scores[0]:
                value = scores[0][metric_name]
                return float(value) if value is not None else None
            return None

        return await asyncio.to_thread(_evaluate)

    def _build_ragas_models(self, ragas: Any) -> tuple[Any, Any]:
        """Build the ragas-wrapped Bedrock LLM + embeddings from the config model ids.

        ASSUMPTION TO CONFIRM (Req 4.2 / 4.3 / 4.4): uses the config-driven model ids
        (``QUALITY_OPT_RAGAS_LLM_MODEL_ID`` / ``QUALITY_OPT_RAGAS_EMBED_MODEL_ID``) and the
        existing Bedrock credential chain (no new secrets, Req 16.4). The exact ragas wrapper
        classes are version-dependent and unverified here; re-validate at install time.
        """
        from langchain_aws import ChatBedrock, BedrockEmbeddings  # type: ignore
        from ragas.llms import LangchainLLMWrapper  # type: ignore
        from ragas.embeddings import LangchainEmbeddingsWrapper  # type: ignore

        chat = ChatBedrock(model_id=self.llm_model_id, region_name=self.region, client=self._llm_client)
        emb = BedrockEmbeddings(model_id=self.embed_model_id, region_name=self.region, client=self._embed_client)
        return LangchainLLMWrapper(chat), LangchainEmbeddingsWrapper(emb)


def build_ragas_adapter(
    name: str = config.QUALITY_OPT_RAGAS_BACKEND,
    *,
    llm_client: Optional[Any] = None,
    embed_client: Optional[Any] = None,
) -> RagasAdapter:
    """Select a :class:`RagasAdapter` by ``name`` (Req 4.1 / 5.1).

    * ``"fake"`` (default) → the deterministic, network-free :class:`FakeRagasAdapter`
      (offline tests; zero dependency on ``ragas`` being installed).
    * ``"bedrock"`` → the live :class:`BedrockRagasAdapter` (lazy ``ragas`` import;
      config-driven Bedrock models; per-metric ``None``-on-failure).

    The live clients are injectable so tests can exercise the wiring with fakes.

    Raises:
        ValueError: if ``name`` is neither ``"fake"`` nor ``"bedrock"``.
    """
    normalized = (name or "").strip().lower()
    if normalized == "fake":
        return FakeRagasAdapter()
    if normalized == "bedrock":
        return BedrockRagasAdapter(llm_client=llm_client, embed_client=embed_client)
    raise ValueError(
        f"unknown ragas backend {name!r}; expected 'fake' or 'bedrock'"
    )
