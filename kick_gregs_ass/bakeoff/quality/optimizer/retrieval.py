"""
Pluggable, held-constant, read-only retrieval substrate for the closed-loop prompt
optimizer (design Component 5b "RetrievalBackend", "Retrieval-always data flow"; Req 12,
13, 16).

The quality answer path is now **retrieval-always** (Req 13): on **every turn of every
conversation** the loop calls a :class:`RetrievalBackend` with the turn's query, gets back
fragments (the common ``{id, text, metadata, confidence, ...}`` shape), renders them
inline into the visible prompt, and threads the **same** fragments into the judge as the
grounding evidence. The substrate is **held constant and read-only** — the Optimizer
invokes it but never tunes or mutates it (Req 12.1, 16.5) — so for a given turn the same
query yields the same fragments for the Champion and the Challenger and the only varied
element is the system-instruction text (Req 12.4, 13.3).

This module provides:

* :class:`RetrievalQuery` — the immutable per-turn query (item/turn/query + optional
  filters, candidate_n, top_k).
* :class:`RetrievalBackend` — the read-only ``Protocol`` every implementation satisfies
  (a ``name`` and ``async def retrieve(q) -> Sequence[Fragment]``).
* :class:`MemoizingRetrievalBackend` — wraps any backend with per-``(turn-query)``
  memoization whose key **excludes** the prompt role and the instruction text, making
  "same query → byte-identical fragments for champion and challenger" a structural
  guarantee rather than a hope (Req 13.3).
* :class:`OpenSearchRetrievalBackend` — the **preferred** backend (Req 16.1): a read-only
  query against the ALPHA OpenSearch service (AWS account ``948580600005``). Its
  endpoint / index / auth are **owner-provided, injected** assumptions (never hard-coded,
  Req 16.6); an injectable ``client`` lets tests exercise the mapping with a **fake**
  OpenSearch client (no real AWS, no module-load boto3/opensearch import).
* :class:`LocalRetrievalBackend` — the guaranteed-workable **fallback** (Req 16.2): the
  repo's ``POST /retrieve`` service documented in the top-level ``README.md``.
* :class:`FakeRetrievalBackend` — the offline test double: deterministic, network-free
  fixed fragments keyed by ``(item_id, turn)`` (zero sockets / boto3 / HTTP, Req 10.4).
* :func:`build_retrieval_backend` — selection + fallback (Req 16.1/16.2/16.3): prefer
  OpenSearch, fall back to local when it is onerous/unworkable; ``"local"`` → local;
  ``"fake"`` → fake. The chosen backend is **always** wrapped in
  :class:`MemoizingRetrievalBackend` before it is returned.

All three concrete implementations return the **same fragment shape** so downstream
grounding and judging are unaffected by which one served a query (Req 16.4), and all issue
**read-only** queries only (Req 16.5).

Import discipline (Req 10.4): this module pulls in **no** boto3 / opensearch / httpx at
import time. The OpenSearch and local HTTP clients are imported **lazily** inside the
methods that need them, so importing ``bakeoff.quality.optimizer.retrieval`` (and running
the offline ``FakeRetrievalBackend``) opens no sockets and constructs no AWS clients.

Sourcing caveat (carried from ``requirements.md`` / ``design.md``): the ALPHA OpenSearch
endpoint, index, and auth for account ``948580600005`` are **owner-provided** operational
facts, not values verified against an Amazon-internal primary source in this execution
environment. They are an assumption to confirm with the owner at implementation time,
which is exactly why this module mandates a guaranteed-workable local fallback rather than
an OpenSearch-only dependency (Req 16.6).
"""
from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, Sequence, runtime_checkable

from bakeoff import config
from bakeoff.resilience import call_with_resilience

__all__ = [
    "RetrievalQuery",
    "Fragment",
    "RetrievalBackend",
    "MemoizingRetrievalBackend",
    "OpenSearchRetrievalBackend",
    "LocalRetrievalBackend",
    "RerankedRetrievalBackend",
    "FakeRetrievalBackend",
    "build_retrieval_backend",
]


# A fragment is the SAME shape every implementation returns (Req 16.4):
#   {"id": str, "text": str, "metadata": dict, "confidence": float, ...}
Fragment = Dict[str, Any]


# The design references ``config.CANDIDATE_N`` / ``config.TOP_K`` for the per-query
# defaults; the bake-off config actually exposes those knobs as
# ``RETRIEVE_CANDIDATE_N`` / ``RETRIEVE_TOP_K`` (both default ``None`` = defer to the
# substrate's own config). Resolve defensively so the default is correct regardless of
# which name a given config revision uses, and so importing this module never raises an
# ``AttributeError`` on a missing constant.
_DEFAULT_CANDIDATE_N: Optional[int] = getattr(
    config, "CANDIDATE_N", getattr(config, "RETRIEVE_CANDIDATE_N", None)
)
_DEFAULT_TOP_K: Optional[int] = getattr(
    config, "TOP_K", getattr(config, "RETRIEVE_TOP_K", None)
)


@dataclass(frozen=True)
class RetrievalQuery:
    """One immutable per-turn retrieval request (design Component 5b).

    The query identifies the turn it belongs to (``item_id`` + ``turn``) as well as the
    text to retrieve for, so the memoization layer can key on the turn rather than on the
    raw text alone. ``candidate_n`` / ``top_k`` default to the substrate's configured
    values (``config.CANDIDATE_N`` / ``config.TOP_K`` per the design, resolved here to the
    bake-off's ``RETRIEVE_CANDIDATE_N`` / ``RETRIEVE_TOP_K``); ``None`` means "defer to the
    backend's own default".

    Frozen so a query is hashable and safe to use as (part of) a cache key, and so it can
    never be mutated out from under the held-constant guarantee.
    """

    item_id: str
    turn: int
    query: str
    filters: Optional[Dict[str, str]] = None
    candidate_n: Optional[int] = _DEFAULT_CANDIDATE_N
    top_k: Optional[int] = _DEFAULT_TOP_K


# Cache key for the held-constant memoization layer. Documented in the design as:
#   (item_id, turn, query, frozenset(filters.items()) or (), candidate_n, top_k)
# It intentionally EXCLUDES the prompt role and the instruction text — that exclusion is
# what makes retrieval the held constant and the instruction the only varied element
# (Req 12.4, 13.3).
_CacheKey = tuple


def _cache_key(q: RetrievalQuery) -> _CacheKey:
    """Build the documented per-``(turn-query)`` cache key for ``q``.

    ``filters`` is reduced to a ``frozenset`` of its items (order-independent) or ``()``
    when absent, so two semantically-identical filter dicts in any key order collapse to
    the same key.
    """
    filters_key = frozenset(q.filters.items()) if q.filters else ()
    return (q.item_id, q.turn, q.query, filters_key, q.candidate_n, q.top_k)


@runtime_checkable
class RetrievalBackend(Protocol):
    """Read-only retrieval substrate (design Component 5b).

    Invoked on **every** turn (Req 13.1/13.2), **held constant** across champion/challenger
    for the same turn (Req 12.4/13.3), and **never** tuned or mutated by the Optimizer
    (Req 12.1/16.5). An implementation is just a stable ``name`` plus an async
    ``retrieve`` that maps a :class:`RetrievalQuery` to a sequence of fragments in the
    common shape.
    """

    name: str  # "opensearch" | "local" | "fake" (or the wrapped backend's name)

    async def retrieve(self, q: RetrievalQuery) -> Sequence[Fragment]:
        """Return ranked fragments for ``q`` (read-only)."""
        ...


class MemoizingRetrievalBackend:
    """Wrap any :class:`RetrievalBackend` with per-``(turn-query)`` memoization.

    The Champion and the Challenger are scored on the **same** turn within an iteration;
    memoizing on the documented key — ``(item_id, turn, query, frozenset(filters), 
    candidate_n, top_k)`` — guarantees they receive **byte-identical** fragments
    (Req 13.3). The key deliberately excludes the prompt role and the instruction text, so
    varying the instruction can never change the fragments: retrieval is the held constant
    and the instruction is the only varied element (Req 12.4).

    The wrapper is read-only (it only ever delegates to ``inner.retrieve``) and transparent
    about identity: its :attr:`name` passes through the wrapped backend's name, so callers
    and audit records still see ``"opensearch"`` / ``"local"`` / ``"fake"`` rather than a
    wrapper name.
    """

    def __init__(self, inner: RetrievalBackend) -> None:
        self._inner = inner
        self._cache: Dict[_CacheKey, Sequence[Fragment]] = {}

    @property
    def inner(self) -> RetrievalBackend:
        """The wrapped backend (exposed for inspection / tests)."""
        return self._inner

    @property
    def name(self) -> str:
        """Pass the wrapped backend's name through unchanged."""
        return self._inner.name

    async def retrieve(self, q: RetrievalQuery) -> Sequence[Fragment]:
        """Return the cached fragments for ``q``, delegating to ``inner`` on a miss.

        On a miss the inner backend is called exactly once and its result is cached under
        the documented key; every later call for the same ``(turn-query)`` returns that
        identical sequence without re-querying, regardless of the prompt role or
        instruction text that prompted the call.
        """
        key = _cache_key(q)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        fragments = await self._inner.retrieve(q)
        self._cache[key] = fragments
        return fragments


async def _resolve(value: Any) -> Any:
    """Await ``value`` if it is awaitable, else return it as-is.

    Lets the OpenSearch path accept either a synchronous fake client (tests) or a real
    async client without branching at the call site.
    """
    if inspect.isawaitable(value):
        return await value
    return value


def _coerce_confidence(value: Any) -> float:
    """Coerce a backend's relevance/confidence signal to a float, defaulting to ``0.0``."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class OpenSearchRetrievalBackend:
    """PREFERRED retrieval backend (Req 16.1): a read-only query against ALPHA OpenSearch.

    Queries the deployed ALPHA OpenSearch service in AWS account ``948580600005``, whose
    data and metadata are essentially identical to the local corpus. Issues **read-only**
    queries only (Req 16.5).

    Connection facts are **injected, never hard-coded** (Req 16.6): ``endpoint``, ``index``,
    and ``auth`` are owner-provided assumptions to confirm at implementation time, and an
    injectable ``client`` lets tests exercise the hit→fragment mapping with a **fake**
    OpenSearch client (no real AWS). The client is expected to expose a
    ``search(index=..., body=...)`` method (the opensearch-py / Elasticsearch shape); the
    return value may be sync or awaitable.

    Construction never raises. If no ``client`` is supplied and no ``endpoint`` is
    configured, the object is still constructable, but a :meth:`retrieve` call will raise a
    clear :class:`RuntimeError` (the selector :func:`build_retrieval_backend` handles
    fallback to the local backend, so the loop never depends on OpenSearch being usable).

    No boto3 / opensearch import happens at module load — any real client is built lazily
    inside :meth:`retrieve`.
    """

    name = "opensearch"

    def __init__(
        self,
        endpoint: Optional[str] = None,
        index: Optional[str] = None,
        auth: Optional[Any] = None,
        *,
        client: Optional[Any] = None,
        client_factory: Optional[Callable[[], Any]] = None,
        refresh_credentials: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._endpoint = endpoint
        self._index = index
        self._auth = auth
        self._client = client
        # ``client_factory`` (re)builds a live OpenSearch client with a FRESH SigV4 signer.
        # It is the seam that lets :meth:`retrieve` recover from credential rotation: when
        # the long optimizer run outlives the ~1h token, an ALPHA OpenSearch query returns
        # 403; we drop the stale client and the factory rebuilds one bound to the
        # newly-minted credentials the external refresher wrote. ``refresh_credentials`` is
        # the (possibly blocking) callback that makes those fresh creds available (re-reads
        # the profile via the credential broker) before the rebuild. Both are optional: an
        # injected static ``client`` (tests) needs neither, and absent a factory the backend
        # behaves exactly as before (no rebuild, query raises straight through).
        self._client_factory = client_factory
        self._refresh_credentials = refresh_credentials

    def is_usable(self) -> bool:
        """Cheap, network-free usability check used by the selector's fallback policy.

        Usable iff a client was injected, or both an ``endpoint`` and an ``index`` are
        configured (so a real client could be built lazily). This is intentionally a
        configuration probe, not a live connectivity probe — it lets the selector fall
        back to local in the common offline case (no endpoint configured) without opening
        a socket. A caller wanting a true reachability probe can pass its own predicate to
        :func:`build_retrieval_backend`.
        """
        return self._client is not None or bool(self._endpoint and self._index)

    def _ensure_client(self) -> Any:
        """Return the injected client, or lazily build a real one; raise if impossible.

        Live-only path: imports ``opensearchpy`` lazily so module import stays
        dependency-free. Raises a clear :class:`RuntimeError` when there is neither an
        injected client nor enough configuration to build one, or when the optional
        dependency is not installed.
        """
        if self._client is None and self._client_factory is not None:
            # Lazily (re)build via the injected factory — this is the rebuild path after a
            # credential rotation dropped the stale client (see :meth:`_on_auth_expiry`).
            self._client = self._client_factory()
        if self._client is not None:
            return self._client
        if not self._endpoint:
            raise RuntimeError(
                "OpenSearchRetrievalBackend has no injected client and no endpoint "
                "configured; cannot issue a query. Inject a client (tests) or configure "
                "QUALITY_OPT_OPENSEARCH_ENDPOINT/INDEX/AUTH, or use the local fallback "
                "backend (build_retrieval_backend handles this automatically)."
            )
        try:  # pragma: no cover - live-only; offline tests inject a client
            from opensearchpy import OpenSearch  # type: ignore
        except ImportError as exc:  # pragma: no cover - live-only
            raise RuntimeError(
                "OpenSearchRetrievalBackend needs the 'opensearch-py' package to build a "
                "live client; install it or inject a client. Falling back to the local "
                "retrieval backend is the supported alternative (Req 16.2)."
            ) from exc
        # The exact auth wiring (SigV4 signer, basic auth, etc.) depends on the
        # owner-provided ``auth`` descriptor (Req 16.6); pass it through as ``http_auth``.
        self._client = OpenSearch(hosts=[self._endpoint], http_auth=self._auth)  # pragma: no cover
        return self._client

    def _build_query_body(self, q: RetrievalQuery) -> Dict[str, Any]:
        """Build a read-only OpenSearch query body for ``q``.

        A bounded ``bool`` query: a ``match`` on the fragment text plus a ``term`` filter
        per supplied metadata filter. ``size`` prefers ``top_k`` then ``candidate_n`` then
        a small default. This is a search (read-only) request only.
        """
        size = q.top_k or q.candidate_n or 10
        filters = [
            {"term": {f"metadata.{key}": value}}
            for key, value in (q.filters or {}).items()
        ]
        return {
            "size": size,
            "query": {
                "bool": {
                    "must": [{"match": {"text": q.query}}],
                    "filter": filters,
                }
            },
        }

    @staticmethod
    def _map_hit(hit: Mapping[str, Any], index: int) -> Fragment:
        """Map one OpenSearch hit to the common ``{id, text, metadata, confidence}`` shape."""
        source = dict(hit.get("_source") or {})
        frag_id = hit.get("_id") or source.get("id") or f"frag-{index}"
        text = source.get("text", "")
        metadata = source.get("metadata")
        if metadata is None:
            # Fall back to the remaining source fields as metadata so nothing is lost.
            metadata = {k: v for k, v in source.items() if k not in {"id", "text"}}
        return {
            "id": str(frag_id),
            "text": str(text),
            "metadata": dict(metadata),
            "confidence": _coerce_confidence(hit.get("_score")),
        }

    async def _on_auth_expiry(self) -> None:
        """Credential-rotation heal: refresh creds, drop the stale client so it rebuilds.

        Fired by :func:`call_with_resilience` when a query is classified AUTH_EXPIRED
        (HTTP 401/403 — the ALPHA OpenSearch token expired mid-run). Steps:

        1. Run ``refresh_credentials`` (if provided) so the broker re-reads the freshly
           minted profile credentials the external refresher wrote. The callback may block
           (it can run ``ada`` / take a cross-process lock), so it is offloaded to a worker
           thread to keep the event loop responsive under the optimizer's fan-out.
        2. Drop the cached client **only when a factory exists** to rebuild it — the next
           :meth:`_ensure_client` then constructs a client whose SigV4 signer is bound to
           the fresh credentials. With no factory (an injected static client), the client
           is left intact so we do not strand the backend with no client at all.
        """
        callback = self._refresh_credentials
        if callback is not None:
            if inspect.iscoroutinefunction(callback):
                await callback()
            else:
                # Possibly-blocking sync refresh (ada subprocess / file lock) — off-loop.
                await asyncio.to_thread(callback)
        if self._client_factory is not None:
            self._client = None  # force _ensure_client to rebuild with fresh creds

    async def retrieve(self, q: RetrievalQuery) -> Sequence[Fragment]:
        """Run a read-only OpenSearch query and map its hits to fragments (Req 16.4/16.5).

        The query is wrapped in :func:`call_with_resilience` so a credential-expiry 403
        (the ALPHA token outliving a long optimizer run) refreshes the creds, rebuilds the
        client, and retries instead of failing the whole run — and a throttle/5xx backs off
        and retries. ``_ensure_client`` is re-resolved on **every** attempt so a rebuilt
        client (post-rotation) is the one actually queried on the retry.
        """
        body = self._build_query_body(q)

        async def _attempt() -> Any:
            client = self._ensure_client()
            # The production opensearch-py client is SYNCHRONOUS, so calling it directly
            # here would block the asyncio event loop for the full network round-trip —
            # under the optimizer's PHASE-2 fan-out (66 turns x 2 concurrent models) that
            # froze the whole server (status -> HTTP 000, "looks dead"). Offload to a worker
            # thread so the loop stays responsive and the judge-capped concurrency actually
            # overlaps. `_resolve` still awaits the result if an injected client returned a
            # coroutine (test fakes).
            raw = await asyncio.to_thread(client.search, index=self._index, body=body)
            return await _resolve(raw)

        response = await call_with_resilience(
            _attempt, refresh_credentials=self._on_auth_expiry
        )
        hits = (response or {}).get("hits", {}).get("hits", []) or []
        return [self._map_hit(hit, i) for i, hit in enumerate(hits, start=1)]


class LocalRetrievalBackend:
    """FALLBACK retrieval backend (Req 16.2): the repo's local ``POST /retrieve`` service.

    Calls the local backend documented in the top-level ``README.md``::

        POST /retrieve  {query, filters?, candidate_n?, top_k?}
          -> {"fragments": [{id, text, metadata, confidence, ...}], "timings": {...}, ...}

    The response's ``fragments`` already carry the common ``{id, text, metadata,
    confidence, ...}`` shape, so they are returned verbatim (Req 16.4). The query is a
    read-only POST (Req 16.5).

    HTTP transport is chosen lazily so the module imports no HTTP client at load time
    (Req 10.4): a lazily-imported ``httpx`` is preferred (it is already a repo dependency
    and supports clean async + test injection); if it is unavailable the stdlib
    ``urllib.request`` is used on a worker thread. An ``httpx``-compatible ``client`` may be
    injected (e.g. one built on an ``httpx.MockTransport``) so tests exercise the full
    mapping with no real network.
    """

    name = "local"

    def __init__(
        self,
        base_url: Optional[str] = None,
        *,
        client: Optional[Any] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self._base_url = (base_url or getattr(config, "RETRIEVE_BASE_URL", "http://localhost:8080")).rstrip("/")
        endpoint = getattr(config, "RETRIEVE_ENDPOINT", "/retrieve")
        self._url = f"{self._base_url}{endpoint}"
        self._client = client
        self._timeout = timeout if timeout is not None else getattr(config, "RETRIEVE_TIMEOUT_S", 60.0)

    @staticmethod
    def _build_body(q: RetrievalQuery) -> Dict[str, Any]:
        """Build the ``/retrieve`` request body, omitting optional fields when unset."""
        body: Dict[str, Any] = {"query": q.query}
        if q.filters is not None:
            body["filters"] = q.filters
        if q.candidate_n is not None:
            body["candidate_n"] = q.candidate_n
        if q.top_k is not None:
            body["top_k"] = q.top_k
        return body

    @staticmethod
    def _map_response(data: Mapping[str, Any]) -> Sequence[Fragment]:
        """Map a ``/retrieve`` response to the common fragment shape (returned verbatim)."""
        fragments = data.get("fragments") or []
        return [dict(frag) for frag in fragments]

    async def retrieve(self, q: RetrievalQuery) -> Sequence[Fragment]:
        """POST to ``/retrieve`` (read-only) and map ``{"fragments": [...]}`` to fragments."""
        body = self._build_body(q)
        if self._client is not None:
            response = await self._client.post(self._url, json=body)
            response.raise_for_status()
            return self._map_response(response.json())
        # No injected client: prefer a lazily-imported httpx, else stdlib urllib.
        try:
            import httpx  # lazy import (Req 10.4): no module-load HTTP dependency
        except ImportError:
            return await self._retrieve_urllib(body)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(self._url, json=body)
            response.raise_for_status()
            return self._map_response(response.json())

    async def _retrieve_urllib(self, body: Mapping[str, Any]) -> Sequence[Fragment]:
        """Blocking stdlib fallback POST, executed on a worker thread to stay async-safe."""
        import asyncio

        def _do_request() -> Sequence[Fragment]:
            import urllib.request  # lazy import (Req 10.4)

            payload = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(
                self._url,
                data=payload,
                headers={"content-type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 - loopback only
                data = json.loads(resp.read().decode("utf-8"))
            return self._map_response(data)

        return await asyncio.to_thread(_do_request)


class RerankedRetrievalBackend:
    """Wrap a backend with a Cohere Rerank v4.0 Pro second stage (OPTIMIZER V2 ONLY).

    Two-stage funnel mirroring the local substrate's design: the wrapped backend (in
    practice the ALPHA AOSS BM25 backend) fetches ``candidate_n`` candidates, and the
    Rerank v4.0 Pro SageMaker endpoint — in the owner's PERSONAL account ``429134228173``
    (profile ``nick-caia``), the reranker prod uses but Bedrock does not carry — reorders
    them by semantic relevance and cuts to ``top_k``. The returned fragments carry the
    reranker's ``relevance_score`` as ``confidence``.

    Placement: :func:`build_retrieval_backend` applies this wrapper INSIDE the
    :class:`MemoizingRetrievalBackend`, i.e. ``memo(rerank(aoss))`` — so the held-constant
    guarantee covers the reranked result (champion and challenger see byte-identical
    fragments per (turn-query), Req 13.3) and a cache hit never re-invokes the endpoint.

    Read-only like every other backend (the endpoint scores documents; it stores nothing),
    and transparent about identity like the memo layer: :attr:`name` passes through the
    wrapped backend's name so audit records keep their ``"opensearch"``/``"local"`` values.

    Import discipline (Req 10.4): boto3 is imported lazily inside the client factory; an
    injectable ``client`` / ``client_factory`` (a ``sagemaker-runtime``-shaped object with
    ``invoke_endpoint``) lets tests exercise the full path with zero AWS. The synchronous
    ``invoke_endpoint`` call runs on a worker thread so the optimizer's fan-out never
    blocks the event loop, and it is wrapped in :func:`call_with_resilience` so a
    throttle/5xx backs off and retries. The personal-account credentials are long-lived
    (no midway-style ~1h rotation), so no credential-heal callback is wired.
    """

    def __init__(
        self,
        inner: RetrievalBackend,
        *,
        endpoint_name: str,
        region: Optional[str] = None,
        profile: Optional[str] = None,
        candidate_n: Optional[int] = None,
        doc_char_limit: Optional[int] = None,
        client: Optional[Any] = None,
        client_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._inner = inner
        self._endpoint_name = endpoint_name
        self._region = region or getattr(config, "QUALITY_OPT_RERANK_V4_REGION", "us-east-1")
        self._profile = profile or getattr(config, "QUALITY_OPT_RERANK_V4_PROFILE", None)
        self._candidate_n = candidate_n or getattr(config, "QUALITY_OPT_RERANK_V4_CANDIDATE_N", 20)
        self._doc_char_limit = doc_char_limit or getattr(
            config, "QUALITY_OPT_RERANK_V4_DOC_CHAR_LIMIT", 32000
        )
        self._client = client
        self._client_factory = client_factory

    @property
    def inner(self) -> RetrievalBackend:
        """The wrapped backend (exposed for inspection / tests)."""
        return self._inner

    @property
    def name(self) -> str:
        """Pass the wrapped backend's name through unchanged (like the memo layer)."""
        return self._inner.name

    def _ensure_client(self) -> Any:
        """Return the injected client, or lazily build the real ``sagemaker-runtime`` one.

        The real client is pinned to the personal profile/region (independent of the
        alpha credential chain the rest of the live backend uses). boto3 is imported
        here, not at module load (Req 10.4).
        """
        if self._client is None and self._client_factory is not None:
            self._client = self._client_factory()
        if self._client is not None:
            return self._client
        import boto3  # lazy import (Req 10.4): no module-load AWS dependency

        session = boto3.Session(profile_name=self._profile) if self._profile else boto3.Session()
        self._client = session.client("sagemaker-runtime", region_name=self._region)
        return self._client

    def _candidate_query(self, q: RetrievalQuery) -> RetrievalQuery:
        """Widen ``q`` so the wrapped backend returns the rerank candidate pool.

        The AOSS query body sizes itself as ``top_k or candidate_n or 10``, so the
        candidate query clears ``top_k`` and pins ``candidate_n`` (never narrower than
        the caller's ``top_k``). The memo layer keys on the ORIGINAL ``q`` upstream of
        this widening, so caller-visible cache semantics are unchanged.
        """
        pool_size = max(int(self._candidate_n), int(q.top_k or 0))
        return RetrievalQuery(
            item_id=q.item_id,
            turn=q.turn,
            query=q.query,
            filters=q.filters,
            candidate_n=pool_size,
            top_k=None,
        )

    async def _rerank(self, query_text: str, documents: Sequence[str], top_n: int) -> Any:
        """Invoke the Rerank v4.0 Pro endpoint (off-loop, with retry/backoff) and parse JSON."""
        body = json.dumps(
            {
                "query": query_text,
                "documents": [d[: self._doc_char_limit] for d in documents],
                "top_n": top_n,
            }
        )

        async def _attempt() -> Any:
            client = self._ensure_client()
            raw = await asyncio.to_thread(
                client.invoke_endpoint,
                EndpointName=self._endpoint_name,
                ContentType="application/json",
                Accept="application/json",
                Body=body,
            )
            response = await _resolve(raw)
            payload = response["Body"].read()
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8")
            return json.loads(payload)

        return await call_with_resilience(_attempt)

    async def retrieve(self, q: RetrievalQuery) -> Sequence[Fragment]:
        """Fetch the candidate pool from ``inner``, rerank it, and return ``top_k``.

        Zero/one candidates short-circuit (nothing to reorder — and the endpoint is never
        invoked with an empty document list). A rerank failure propagates after the
        resilience wrapper's retries: silently falling back to the un-reranked order would
        quietly change what "retrieval" means mid-run.
        """
        candidates = list(await self._inner.retrieve(self._candidate_query(q)))
        top_k = int(q.top_k or _DEFAULT_TOP_K or 5)
        if len(candidates) <= 1:
            return candidates[:top_k]

        parsed = await self._rerank(
            q.query, [str(c.get("text", "")) for c in candidates], min(top_k, len(candidates))
        )
        results = parsed.get("results") or []
        reranked: list[Fragment] = []
        for result in results:
            candidate_index = result.get("index")
            if not isinstance(candidate_index, int) or not (0 <= candidate_index < len(candidates)):
                continue
            fragment = dict(candidates[candidate_index])
            fragment["confidence"] = _coerce_confidence(result.get("relevance_score"))
            reranked.append(fragment)
        return reranked[:top_k]


class FakeRetrievalBackend:
    """OFFLINE test double: deterministic, network-free fixed fragments (Req 10.4).

    Returns a small, deterministic fragment list keyed by ``(item_id, turn)`` — zero
    sockets, zero boto3, zero HTTP — so the full offline loop runs with no network. When
    ``fragments_by_key`` is supplied it is consulted first (key ``(item_id, turn)``);
    otherwise a deterministic list is synthesized from the query so repeated calls for the
    same turn always return identical fragments in the common
    ``{id, text, metadata, confidence}`` shape (Req 16.4).
    """

    name = "fake"

    def __init__(
        self,
        fragments_by_key: Optional[Mapping[tuple, Sequence[Fragment]]] = None,
    ) -> None:
        # Normalize the supplied table to a plain dict of lists of dict-copies so callers
        # cannot mutate our stored fragments and we never share a list instance out.
        self._table: Dict[tuple, list] = {}
        if fragments_by_key:
            for key, frags in fragments_by_key.items():
                self._table[tuple(key)] = [dict(f) for f in frags]

    def _synthesize(self, q: RetrievalQuery) -> list:
        """Build a small deterministic fragment list for ``q`` (network-free)."""
        # Two stable fragments per turn, derived purely from the query fields so the output
        # is a deterministic function of (item_id, turn) and carries no randomness.
        frags: list = []
        for i in range(1, 3):
            frag_id = f"fake-{q.item_id}-t{q.turn}-{i}"
            frags.append(
                {
                    "id": frag_id,
                    "text": f"[fake fragment {i} for item {q.item_id} turn {q.turn}] {q.query}",
                    "metadata": {"item_id": q.item_id, "turn": q.turn, "rank": i},
                    "confidence": round(1.0 / (i + 1), 4),
                }
            )
        return frags

    async def retrieve(self, q: RetrievalQuery) -> Sequence[Fragment]:
        """Return deterministic fixed fragments for ``q`` with zero network activity."""
        preset = self._table.get((q.item_id, q.turn))
        if preset is not None:
            return [dict(f) for f in preset]
        return self._synthesize(q)


def build_retrieval_backend(
    name: str = config.QUALITY_OPT_RETRIEVAL_BACKEND,
    *,
    opensearch_endpoint: Optional[str] = None,
    opensearch_index: Optional[str] = None,
    opensearch_auth: Optional[Any] = None,
    opensearch_client: Optional[Any] = None,
    opensearch_client_factory: Optional[Callable[[], Any]] = None,
    opensearch_refresh_credentials: Optional[Callable[[], Any]] = None,
    opensearch_usable: Optional[Callable[[OpenSearchRetrievalBackend], bool]] = None,
    local_base_url: Optional[str] = None,
    local_client: Optional[Any] = None,
    fake_fragments_by_key: Optional[Mapping[tuple, Sequence[Fragment]]] = None,
    rerank_endpoint_name: Optional[str] = None,
    rerank_client: Optional[Any] = None,
    rerank_client_factory: Optional[Callable[[], Any]] = None,
) -> RetrievalBackend:
    """Select a :class:`RetrievalBackend` by ``name`` and wrap it for held-constant reuse.

    Selection + fallback policy (Req 16.1/16.2/16.3):

    * ``"opensearch"`` (default): build an :class:`OpenSearchRetrievalBackend` from the
      injected/owner-provided ``endpoint`` / ``index`` / ``auth`` (defaulting to the
      ``QUALITY_OPT_OPENSEARCH_*`` config placeholders). If it is **not usable** —
      unreachable, unconfigured, or otherwise onerous — fall back to the
      :class:`LocalRetrievalBackend` (Req 16.2). Usability is decided by ``opensearch_usable``
      when supplied (a true connectivity probe can be injected here), else by the backend's
      cheap, network-free :meth:`OpenSearchRetrievalBackend.is_usable` check; any exception
      raised by the probe is treated as "unworkable" and triggers the fallback.
    * ``"local"``: build the :class:`LocalRetrievalBackend` directly.
    * ``"fake"``: build the network-free :class:`FakeRetrievalBackend` (offline tests).

    When ``rerank_endpoint_name`` is supplied AND the selection landed on the OpenSearch
    backend (not its local fallback — the local substrate already reranks internally),
    the backend is additionally wrapped in :class:`RerankedRetrievalBackend` (Cohere
    Rerank v4.0 Pro, optimizer-v2-only; see the ``QUALITY_OPT_RERANK_V4_*`` config block).
    The bare default is ``None`` = rerank OFF, mirroring the unconfigured-OpenSearch
    default, so offline paths and tests are untouched unless a caller opts in
    (``build_live_backend`` is that caller).

    The chosen backend is **always** wrapped in :class:`MemoizingRetrievalBackend` last
    (i.e. ``memo(rerank(opensearch))``), so retrieval — including the reranked order — is
    held constant per ``(turn-query)`` (Req 13.3) and the returned object's ``name`` still
    reflects the underlying backend.

    Raises:
        ValueError: if ``name`` is not one of ``"opensearch"`` / ``"local"`` / ``"fake"``.
    """
    normalized = (name or "").strip().lower()

    if normalized == "fake":
        inner: RetrievalBackend = FakeRetrievalBackend(fragments_by_key=fake_fragments_by_key)
    elif normalized == "local":
        inner = LocalRetrievalBackend(base_url=local_base_url, client=local_client)
    elif normalized == "opensearch":
        endpoint = (
            opensearch_endpoint
            if opensearch_endpoint is not None
            else getattr(config, "QUALITY_OPT_OPENSEARCH_ENDPOINT", None)
        )
        index = (
            opensearch_index
            if opensearch_index is not None
            else getattr(config, "QUALITY_OPT_OPENSEARCH_INDEX", None)
        )
        auth = (
            opensearch_auth
            if opensearch_auth is not None
            else getattr(config, "QUALITY_OPT_OPENSEARCH_AUTH", None)
        )
        os_backend = OpenSearchRetrievalBackend(
            endpoint=endpoint,
            index=index,
            auth=auth,
            client=opensearch_client,
            client_factory=opensearch_client_factory,
            refresh_credentials=opensearch_refresh_credentials,
        )
        probe = opensearch_usable if opensearch_usable is not None else (lambda b: b.is_usable())
        if _safe_probe(probe, os_backend):
            inner = os_backend
            if rerank_endpoint_name:
                # Rerank v4 second stage (optimizer v2 only) — applied to the AOSS
                # backend ONLY, inside the memo wrap below, so a cache hit never
                # re-invokes the endpoint. The local fallback is left alone: the
                # local substrate already reranks internally (Rerank 3.5).
                inner = RerankedRetrievalBackend(
                    inner,
                    endpoint_name=rerank_endpoint_name,
                    client=rerank_client,
                    client_factory=rerank_client_factory,
                )
        else:
            # OpenSearch onerous / unworkable -> guaranteed-workable local fallback (Req 16.2).
            inner = LocalRetrievalBackend(base_url=local_base_url, client=local_client)
    else:
        raise ValueError(
            f"unknown retrieval backend {name!r}; expected 'opensearch', 'local', or 'fake'"
        )

    return MemoizingRetrievalBackend(inner)


def _safe_probe(
    probe: Callable[[OpenSearchRetrievalBackend], bool],
    backend: OpenSearchRetrievalBackend,
) -> bool:
    """Run the OpenSearch usability ``probe``, treating any raised exception as unworkable.

    A probe that raises (e.g. a real connectivity check that times out) means OpenSearch is
    onerous/unworkable, which the policy maps to ``False`` so the selector falls back to the
    local backend (Req 16.2).
    """
    try:
        return bool(probe(backend))
    except Exception:
        return False
