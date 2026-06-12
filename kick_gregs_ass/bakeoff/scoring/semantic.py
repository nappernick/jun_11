"""
Layer-B semantic-similarity scorer (Task 6, Req 4.2, 4.7).

Embeds the model's answer and the item's *ideal response* with the **same Embed
v4 substrate** the retrieval backend uses (Bedrock, ``us-west-2`` cross-region
profile — mirrors :func:`src.bedrock_client.embed`), and reports their **raw
cosine similarity in ``[-1, 1]``** (identical text → 1.0, orthogonal → 0.0,
opposite → -1.0). This is a cheap, deterministic, judge-independent cross-check
that catches gross divergence and corroborates the judge; per the design it is
**never trusted alone**.

Two design obligations are met here:

* **Content-hash embedding cache (Req 4.7, design AD-5).** Every embedding is
  cached keyed by a hash of ``(input_type, text)`` under
  :data:`bakeoff.config.EMBEDDINGS_CACHE_DIR`, two-tier (in-process dict + JSON
  disk mirror). The same answer text and the same ideal text recur across reps and
  across scorer re-runs, so repeated scoring makes **zero extra Bedrock calls**. A
  single ``similarity`` call batches the (at most two) *uncached* texts into one
  ``embed_fn`` invocation.
* **Credential-expiry resilience (design "first-class concern").** A long run can
  outlive a short-lived STS/Bedrock session. The default embedder
  (:class:`_ResilientBedrockEmbedder`) wraps the Bedrock ``invoke_model`` call so
  an expired-/invalid-credential failure triggers a **credential refresh (rebuild
  the boto3 client) + retry** up to :data:`bakeoff.config.AUTH_MAX_REFRESH_CYCLES`,
  and a throttle/transient failure triggers a plain backoff + retry up to
  :data:`bakeoff.config.RETRY_MAX_ATTEMPTS`, using the
  :class:`bakeoff.types.ErrorClass` taxonomy.

  **Reuse note:** task 5 owns a shared ``bakeoff/resilience.py`` (``classify_error``
  + ``call_with_resilience``). At the time this module was written that file did
  not yet exist, so the classify-and-refresh logic lives here, deliberately
  factored into :func:`classify_error` + :meth:`_ResilientBedrockEmbedder.__call__`
  so it can later be lifted onto the shared helper without changing call sites. If
  ``bakeoff.resilience`` is present its ``classify_error`` is used preferentially.

The ideal-response source: the synthetic dataset has no explicit
``ideal_response`` field. Per the design and the dataset loader, the ideal is
built from the **resolved gold fragment text/title plus the item's ``wants``**
(the ideal intent). :func:`ideal_response_text` does this assembly.

The scorer accepts an injectable ``embed_fn`` (signature ``(texts, input_type) ->
list[list[float]]`` — identical to :func:`src.bedrock_client.embed`), so tests
stub embeddings entirely; **no real Bedrock calls** are made in the test suite.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from bakeoff import config
from bakeoff.types import ErrorClass, GoldFragment

# Prefer the shared resilience classifier (task 5) if it has landed; otherwise
# use the equivalent logic implemented locally below. Behavior is identical.
try:  # pragma: no cover - exercised only once the sibling task lands
    from bakeoff.resilience import classify_error as _shared_classify_error
except Exception:  # noqa: BLE001 - any import failure => use local fallback
    _shared_classify_error = None

#: Embed-function signature: ``embed_fn(texts, input_type) -> list[list[float]]``.
EmbedFn = Callable[[Sequence[str], str], list]

__all__ = [
    "classify_error",
    "cosine_similarity",
    "ideal_response_text",
    "EmbeddingClient",
    "SemanticSimilarityScorer",
    "make_bedrock_embed_fn",
]


# ---------------------------------------------------------------------------
# Error classification (credential-expiry resilience)
# ---------------------------------------------------------------------------
def _local_classify_error(err: BaseException) -> ErrorClass:
    """Classify a failed Bedrock/HTTP call into an :class:`ErrorClass`.

    Inspects (in order) any structured botocore error ``Code``, an HTTP status
    code (botocore metadata or an httpx-style ``response.status_code``), the
    exception class name, and finally the stringified error against the
    case-insensitive signature lists in :mod:`bakeoff.config`. Conservative:
    anything unrecognized is :attr:`ErrorClass.UNKNOWN` (recorded, not retried).
    """
    name = type(err).__name__
    text = str(err).lower()

    code: Optional[str] = None
    status: Optional[int] = None
    response = getattr(err, "response", None)
    if isinstance(response, dict):
        error_obj = response.get("Error")
        if isinstance(error_obj, dict):
            code = error_obj.get("Code")
        meta = response.get("ResponseMetadata")
        if isinstance(meta, dict):
            status = meta.get("HTTPStatusCode")
    if status is None:
        http_resp = getattr(err, "response", None)
        status = getattr(http_resp, "status_code", None)

    if code is not None:
        if code in config.AUTH_EXPIRED_ERROR_CODES:
            return ErrorClass.AUTH_EXPIRED
        if code in config.THROTTLE_ERROR_CODES:
            return ErrorClass.THROTTLED

    if name in config.AUTH_EXPIRED_ERROR_CODES:
        return ErrorClass.AUTH_EXPIRED
    if name in config.THROTTLE_ERROR_CODES:
        return ErrorClass.THROTTLED

    if isinstance(status, int):
        if status in config.AUTH_EXPIRED_HTTP_STATUSES:
            return ErrorClass.AUTH_EXPIRED
        if status in config.THROTTLE_HTTP_STATUSES:
            return ErrorClass.THROTTLED
        if status in config.TRANSIENT_HTTP_STATUSES:
            return ErrorClass.TRANSIENT

    if any(sig in text for sig in config.AUTH_EXPIRED_MESSAGE_SIGNATURES):
        return ErrorClass.AUTH_EXPIRED

    if any(s in name.lower() for s in ("timeout", "connect", "connection")):
        return ErrorClass.TRANSIENT

    return ErrorClass.UNKNOWN


def classify_error(err: BaseException) -> ErrorClass:
    """Public classifier: delegates to ``bakeoff.resilience`` if present."""
    if _shared_classify_error is not None:  # pragma: no cover - sibling-dependent
        return _shared_classify_error(err)
    return _local_classify_error(err)


def _backoff_delay(base: float, cap: float, attempt: int) -> float:
    """Exponential backoff ``min(base * 2**attempt, cap)`` (attempt 0-indexed)."""
    return min(base * (2 ** attempt), cap)


# ---------------------------------------------------------------------------
# Pure math
# ---------------------------------------------------------------------------
def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Raw cosine similarity of two equal-length vectors (range ``[-1, 1]``).

    Returns 0.0 if either vector is empty, they differ in length, or either has
    zero magnitude (cosine undefined → conservative 0.0). Pure stdlib math so the
    module imports no numpy just for a dot product. The scorer normalizes the
    result into ``[0, 1]``.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# ---------------------------------------------------------------------------
# Ideal-response assembly (no explicit ideal_response field in the dataset)
# ---------------------------------------------------------------------------
def ideal_response_text(
    gold: Iterable[GoldFragment], wants: Optional[str] = None
) -> str:
    """Build the ideal-response text from gold fragments + the item's ``wants``.

    Per the design (Layer B) and the loader, the synthetic records carry no
    explicit ideal response; the ideal is approximated by the **resolved gold
    fragment content** (markdown if present, else snippet, else title) joined
    with the item's ``wants`` (the ideal intent). Deterministic and order-stable
    so the resulting text hashes consistently into the embedding cache.
    """
    parts: list[str] = []
    if wants:
        parts.append(wants.strip())
    for frag in gold:
        body = frag.markdown or frag.snippet or frag.title
        if body:
            parts.append(body.strip())
    return "\n\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Default embedder: Embed v4 via Bedrock, with credential-expiry resilience
# ---------------------------------------------------------------------------
ClientFactory = Callable[[], object]


def _default_client_factory() -> object:
    """Build a ``bedrock-runtime`` client via the credential broker (NOT the ambient chain).

    Binds to the broker's explicit named profile (``alpha``) with proactive TTL refresh,
    matching the author/audit/answer adapters. Previously this did a bare
    ``boto3.client(...)`` against the ambient ``AWS_PROFILE=default`` profile, which the
    background refresher never re-mints (it refreshes ``alpha``), so the embed token
    expired mid-run. Imported lazily so importing this module never requires boto3 (tests
    inject a fake factory or a fake ``embed_fn`` and never trigger this path).
    """
    from bakeoff.credentials import get_broker

    session = get_broker().get_session(region=config.AWS_REGION)
    return session.client("bedrock-runtime", region_name=config.AWS_REGION)


class _ResilientBedrockEmbedder:
    """Default ``embed_fn``: Embed v4 ``invoke_model`` wrapped with auth/throttle retry.

    Callable with the :data:`EmbedFn` signature ``(texts, input_type)``. On
    :attr:`ErrorClass.AUTH_EXPIRED` it rebuilds the boto3 client (re-resolving the
    credential chain) and retries, up to ``config.AUTH_MAX_REFRESH_CYCLES``; on
    ``THROTTLED``/``TRANSIENT`` it backs off and retries up to
    ``config.RETRY_MAX_ATTEMPTS``; ``PERMANENT``/``UNKNOWN`` re-raise (the runner
    records the trial as errored and continues). Mirrors the body shape of
    :func:`src.bedrock_client.embed`. Built to be a drop-in for a future
    ``bakeoff.resilience.call_with_resilience`` wrapper.
    """

    def __init__(
        self,
        model_id: Optional[str] = None,
        region: Optional[str] = None,
        *,
        client_factory: Optional[ClientFactory] = None,
    ) -> None:
        self.model_id = model_id or config.EMBED_MODEL_ID
        self.region = region or config.AWS_REGION
        self._client_factory = client_factory or _default_client_factory
        self._client: Optional[object] = None
        #: number of credential refreshes performed (observability / test hook).
        self.refresh_count = 0

    def _get_client(self) -> object:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def _refresh_client(self) -> object:
        self.refresh_count += 1
        self._client = self._client_factory()
        return self._client

    def _invoke(self, texts: Sequence[str], input_type: str) -> list:
        body = {
            "texts": list(texts),
            "input_type": input_type,
            "embedding_types": ["float"],
        }
        client = self._get_client()
        resp = client.invoke_model(  # type: ignore[attr-defined]
            modelId=self.model_id,
            body=json.dumps(body),
            accept="*/*",
            contentType="application/json",
        )
        result = json.loads(resp["body"].read())
        emb = result["embeddings"]
        if isinstance(emb, dict):
            emb = emb.get("float") or next(iter(emb.values()))
        return emb

    def __call__(self, texts: Sequence[str], input_type: str) -> list:
        auth_cycles = 0
        retry_attempts = 0
        while True:
            try:
                return self._invoke(texts, input_type)
            except Exception as err:  # noqa: BLE001 - classified then re-raised
                klass = classify_error(err)
                if klass is ErrorClass.AUTH_EXPIRED:
                    if auth_cycles >= config.AUTH_MAX_REFRESH_CYCLES:
                        raise
                    delay = _backoff_delay(
                        config.AUTH_BACKOFF_BASE_S, config.AUTH_BACKOFF_MAX_S, auth_cycles
                    )
                    auth_cycles += 1
                    if delay > 0:
                        time.sleep(delay)
                    self._refresh_client()
                    continue
                if klass in (ErrorClass.THROTTLED, ErrorClass.TRANSIENT):
                    if retry_attempts >= config.RETRY_MAX_ATTEMPTS:
                        raise
                    delay = _backoff_delay(
                        config.RETRY_BACKOFF_BASE_S, config.RETRY_BACKOFF_MAX_S, retry_attempts
                    )
                    retry_attempts += 1
                    if delay > 0:
                        time.sleep(delay)
                    continue
                raise


def make_bedrock_embed_fn(
    model_id: Optional[str] = None,
    region: Optional[str] = None,
    *,
    client_factory: Optional[ClientFactory] = None,
) -> _ResilientBedrockEmbedder:
    """Build the default resilient Embed v4 ``embed_fn`` (Bedrock-backed)."""
    return _ResilientBedrockEmbedder(
        model_id, region, client_factory=client_factory
    )


# ---------------------------------------------------------------------------
# EmbeddingClient: a boto3-client-backed embedder owning the content-hash cache
# ---------------------------------------------------------------------------
class EmbeddingClient:
    """Embed v4 client wrapper that owns the content-hash embedding cache.

    This is the cache-and-credentials seam the scorer can delegate to. It wraps a
    boto3-shaped ``bedrock-runtime`` client (built lazily from ``client_factory``,
    defaulting to the real chain) and:

    * embeds texts via Embed v4 ``invoke_model`` (same body shape as
      :func:`src.bedrock_client.embed`);
    * caches every vector keyed by a hash of ``(input_type, text)`` — two-tier
      (in-process dict + optional JSON disk mirror under ``cache_dir``), so
      repeated scoring of identical content makes **zero extra Bedrock calls**
      (Req 4.7);
    * survives an expired-/invalid-credential burst by **refreshing** (rebuilding
      the client via ``client_factory``) and retrying, and backs off on
      throttle/transient errors — using the shared :class:`ErrorClass` taxonomy
      (design "first-class concern").

    Tests inject ``client_factory=lambda: fake_boto3_client`` so no real Bedrock
    call is ever made.
    """

    def __init__(
        self,
        *,
        client_factory: Optional[ClientFactory] = None,
        model_id: Optional[str] = None,
        region: Optional[str] = None,
        cache_dir: "str | os.PathLike[str] | None" = None,
        disk_cache: bool = True,
        input_type: str = "search_document",
    ) -> None:
        self.model_id = model_id or config.EMBED_MODEL_ID
        self.region = region or config.AWS_REGION
        self.input_type = input_type
        self._client_factory = client_factory or _default_client_factory
        self._client: Optional[object] = None
        #: number of credential refreshes performed (observability / test hook).
        self.refresh_count = 0

        self._mem_cache: dict[str, list[float]] = {}
        self._disk_cache_enabled = disk_cache
        self._cache_dir = (
            Path(cache_dir) if cache_dir is not None else config.EMBEDDINGS_CACHE_DIR
        )
        if self._disk_cache_enabled:
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    # -- boto3 client lifecycle -------------------------------------------
    def _get_client(self) -> object:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def _refresh_client(self) -> object:
        self.refresh_count += 1
        self._client = self._client_factory()
        return self._client

    # -- content-hash cache -----------------------------------------------
    def _cache_key(self, text: str) -> str:
        """Content hash over ``(input_type, text)`` (Req 4.7)."""
        h = hashlib.sha256()
        h.update(self.input_type.encode("utf-8"))
        h.update(b"\x1f")
        h.update(text.encode("utf-8"))
        return h.hexdigest()

    def _disk_path(self, key: str) -> Path:
        return self._cache_dir / f"{key}.json"

    def _load_from_disk(self, key: str) -> Optional[list[float]]:
        path = self._disk_path(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None  # corrupt/partial mirror => treat as a miss
        vec = payload.get("embedding")
        return list(vec) if isinstance(vec, list) else None

    def _write_to_disk(self, key: str, text: str, vec: list[float]) -> None:
        path = self._disk_path(key)
        payload = {
            "input_type": self.input_type,
            "text_preview": text[:120],  # debugging aid; the hash is the real key
            "embedding": vec,
        }
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)

    # -- resilient invoke -------------------------------------------------
    def _invoke(self, texts: Sequence[str], input_type: str) -> list:
        body = {
            "texts": list(texts),
            "input_type": input_type,
            "embedding_types": ["float"],
        }
        client = self._get_client()
        resp = client.invoke_model(  # type: ignore[attr-defined]
            modelId=self.model_id,
            body=json.dumps(body),
            accept="*/*",
            contentType="application/json",
        )
        result = json.loads(resp["body"].read())
        emb = result["embeddings"]
        if isinstance(emb, dict):
            emb = emb.get("float") or next(iter(emb.values()))
        return emb

    def _invoke_resilient(self, texts: Sequence[str], input_type: str) -> list:
        """Invoke with credential-refresh + throttle/transient retry."""
        auth_cycles = 0
        retry_attempts = 0
        while True:
            try:
                return self._invoke(texts, input_type)
            except Exception as err:  # noqa: BLE001 - classified then re-raised
                klass = classify_error(err)
                if klass is ErrorClass.AUTH_EXPIRED:
                    if auth_cycles >= config.AUTH_MAX_REFRESH_CYCLES:
                        raise
                    delay = _backoff_delay(
                        config.AUTH_BACKOFF_BASE_S, config.AUTH_BACKOFF_MAX_S, auth_cycles
                    )
                    auth_cycles += 1
                    if delay > 0:
                        time.sleep(delay)
                    self._refresh_client()
                    continue
                if klass in (ErrorClass.THROTTLED, ErrorClass.TRANSIENT):
                    if retry_attempts >= config.RETRY_MAX_ATTEMPTS:
                        raise
                    delay = _backoff_delay(
                        config.RETRY_BACKOFF_BASE_S, config.RETRY_BACKOFF_MAX_S, retry_attempts
                    )
                    retry_attempts += 1
                    if delay > 0:
                        time.sleep(delay)
                    continue
                raise

    # -- public: cache-first, per-text embedding -------------------------
    def embed(self, texts: Sequence[str]) -> dict[str, list[float]]:
        """Return ``text -> vector`` for ``texts``, embedding only uncached ones.

        Cache resolution per text: in-memory -> disk mirror -> resilient invoke.
        Each uncached text is embedded with its **own** resilient invoke (one call
        per distinct text), so a fresh (answer, ideal) pair costs exactly two
        calls and a fully-cached pair costs zero — and a single text that recurs
        is only ever embedded once.
        """
        result: dict[str, list[float]] = {}
        seen: set[str] = set()
        for t in texts:
            if t in seen:
                continue
            seen.add(t)
            key = self._cache_key(t)
            vec = self._mem_cache.get(key)
            if vec is None and self._disk_cache_enabled:
                vec = self._load_from_disk(key)
                if vec is not None:
                    self._mem_cache[key] = vec
            if vec is None:
                vectors = self._invoke_resilient([t], self.input_type)
                vec = list(vectors[0])
                self._mem_cache[key] = vec
                if self._disk_cache_enabled:
                    self._write_to_disk(key, t, vec)
            result[t] = vec
        return result


# ---------------------------------------------------------------------------
# The scorer: content-hash cache + raw cosine
# ---------------------------------------------------------------------------
class SemanticSimilarityScorer:
    """Raw cosine (range ``[-1, 1]``) of Embed v4 vectors, with a content cache.

    Two ways to supply embeddings (exactly one cache lives behind each):

    * ``client``: an :class:`EmbeddingClient` that owns the content-hash cache and
      the resilient Bedrock invoke. Preferred; the scorer delegates all caching
      and embedding to it.
    * ``embed_fn``: a bare callable ``(texts, input_type) -> list[list[float]]``
      (e.g. :func:`make_bedrock_embed_fn`, or a test stub). When supplied without
      a ``client``, the scorer keeps its own content-hash cache around it.

    If neither is supplied, a default :class:`EmbeddingClient` is built (real
    Bedrock chain). ``score(answer, ideal)`` returns the **raw cosine** as a float
    (identical → 1.0, orthogonal → 0.0, opposite → -1.0); per the design the raw
    value is reported rather than a ``[0, 1]`` remap, and it is never trusted alone.

    Args:
        embed_fn: optional bare embed callable (see above).
        client: optional :class:`EmbeddingClient` (preferred).
        model_id / region: forwarded to the default client/embedder.
        cache_dir: embedding cache directory (used for the scorer-local cache when
            an ``embed_fn`` is supplied, or for the default client otherwise).
        disk_cache: when ``True`` (default) embeddings persist across restarts.
        input_type: Cohere embed ``input_type`` for the compared texts.
    """

    name = "semantic_similarity"

    def __init__(
        self,
        embed_fn: Optional[EmbedFn] = None,
        *,
        client: Optional["EmbeddingClient"] = None,
        model_id: Optional[str] = None,
        region: Optional[str] = None,
        cache_dir: "str | os.PathLike[str] | None" = None,
        disk_cache: bool = True,
        input_type: str = "search_document",
    ) -> None:
        self.input_type = input_type
        self._client = client
        self._embed_fn = embed_fn
        # Scorer-local content cache, used ONLY when delegating to a bare embed_fn
        # (an EmbeddingClient owns its own cache, so we never double-cache).
        self._mem_cache: dict[str, list[float]] = {}
        self._disk_cache_enabled = disk_cache
        self._cache_dir = (
            Path(cache_dir) if cache_dir is not None else config.EMBEDDINGS_CACHE_DIR
        )

        if self._client is None and self._embed_fn is None:
            # Neither supplied: build a default EmbeddingClient (real chain).
            self._client = EmbeddingClient(
                model_id=model_id,
                region=region,
                cache_dir=cache_dir,
                disk_cache=disk_cache,
                input_type=input_type,
            )
        if self._client is None and self._disk_cache_enabled:
            # Only the scorer-local cache path needs its own dir.
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    # -- scorer-local cache (only used with a bare embed_fn) --------------
    def _cache_key(self, text: str) -> str:
        """Content hash over ``(input_type, text)`` (Req 4.7)."""
        h = hashlib.sha256()
        h.update(self.input_type.encode("utf-8"))
        h.update(b"\x1f")
        h.update(text.encode("utf-8"))
        return h.hexdigest()

    def _disk_path(self, key: str) -> Path:
        return self._cache_dir / f"{key}.json"

    def _load_from_disk(self, key: str) -> Optional[list[float]]:
        path = self._disk_path(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None  # corrupt/partial mirror => treat as a miss
        vec = payload.get("embedding")
        return list(vec) if isinstance(vec, list) else None

    def _write_to_disk(self, key: str, text: str, vec: list[float]) -> None:
        path = self._disk_path(key)
        payload = {
            "input_type": self.input_type,
            "text_preview": text[:120],  # debugging aid; the hash is the real key
            "embedding": vec,
        }
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)

    # -- embedding (cache-first, batched) ---------------------------------
    def _embed_texts(self, texts: Sequence[str]) -> dict[str, list[float]]:
        """Return ``text -> vector`` for ``texts``.

        Delegates to the :class:`EmbeddingClient` when one is configured (it owns
        the cache); otherwise embeds via the bare ``embed_fn`` behind the
        scorer-local content-hash cache, batching only the uncached texts.
        """
        if self._client is not None:
            return self._client.embed(texts)

        result: dict[str, list[float]] = {}
        uncached: list[str] = []
        for t in texts:
            if t in result or t in uncached:
                continue
            key = self._cache_key(t)
            vec = self._mem_cache.get(key)
            if vec is None and self._disk_cache_enabled:
                vec = self._load_from_disk(key)
                if vec is not None:
                    self._mem_cache[key] = vec
            if vec is not None:
                result[t] = vec
            else:
                uncached.append(t)

        if uncached:
            assert self._embed_fn is not None  # invariant: client-or-embed_fn
            vectors = self._embed_fn(uncached, self.input_type)
            for t, vec in zip(uncached, vectors):
                key = self._cache_key(t)
                self._mem_cache[key] = list(vec)
                if self._disk_cache_enabled:
                    self._write_to_disk(key, t, list(vec))
                result[t] = list(vec)
        return result

    # -- public API --------------------------------------------------------
    def similarity(self, text_a: Optional[str], text_b: Optional[str]) -> float:
        """Embed both texts and return the **raw cosine** in ``[-1, 1]``.

        Identical → 1.0, orthogonal → 0.0, opposite → -1.0. Returns 0.0 (without
        embedding) if either text is empty.
        """
        if not text_a or not text_b:
            return 0.0
        vecs = self._embed_texts([text_a, text_b])
        return cosine_similarity(vecs[text_a], vecs[text_b])

    def score(self, answer_text: Optional[str], ideal_text: Optional[str]) -> float:
        """Return the raw cosine similarity (float) for one trial.

        Returning the bare float matches the Task-6 acceptance contract; callers
        that want the metric-keyed bundle can use :meth:`score_bundle`.
        """
        return self.similarity(answer_text, ideal_text)

    def score_bundle(
        self, answer_text: Optional[str], ideal_text: Optional[str]
    ) -> dict[str, float]:
        """Return ``{"semantic_similarity": <raw cosine>}`` for one trial."""
        return {"semantic_similarity": self.similarity(answer_text, ideal_text)}

    def score_item(
        self,
        answer_text: Optional[str],
        gold: Iterable[GoldFragment],
        wants: Optional[str] = None,
    ) -> dict[str, float]:
        """Score against an ideal assembled from gold fragments + ``wants``."""
        return self.score_bundle(answer_text, ideal_response_text(gold, wants))
