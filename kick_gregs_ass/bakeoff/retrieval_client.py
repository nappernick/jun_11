"""
Retrieval client over the existing ``/retrieve`` substrate (Task 4, Req 2).

The harness **never re-implements retrieval** (Req 2.1). It talks to the existing
backend (`src/server.py`) over HTTP:

* ``POST /retrieve``  ``{query, filters?, candidate_n?, top_k?}``
  -> ``{fragments:[{id,text,metadata,fusion_score,confidence}], timings, cache_hit}``
* ``GET /healthz``    -> ``{status:"ok"|"degraded", ...}`` (``"ok"`` == healthy)

:class:`RetrievalClient` maps the ``/retrieve`` response **verbatim** into the
frozen :class:`bakeoff.types.RetrievalResult` (Req 2.2) and layers an **optional
local result cache** keyed by ``(query, filters, candidate_n, top_k)`` (Req 2.5).
The cache exists for two reasons:

1. **Backend-less replay** — a previously-seen ``(query, filters, n, k)`` returns
   identical ``fragment_ids`` without the backend running.
2. **Reinforcing "retrieval is a held constant" (Req 2.3, design AD-2)** — the
   backend already memoizes per the same key; the local cache mirrors that so the
   *same* item produces *identical* ``fragment_ids`` across every rep and every
   candidate model, on disk, surviving process restarts.

The cache is two-tier: an in-process dict (fast path within one run) plus an
optional JSON-file mirror under ``config.RETRIEVAL_CACHE_DIR`` (survives a new
process / new client instance). The disk mirror is optional via a constructor
flag (default on).

Concurrency model: this is an ``asyncio`` client (the runner is asyncio,
design AD-3). It supports dependency-injecting an :class:`httpx.AsyncClient`
(e.g. one built on an :class:`httpx.MockTransport`) so the unit tests exercise
the full mapping/cache logic with no real network.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional

import httpx

import bakeoff.config as config
from bakeoff.types import RetrievalResult

__all__ = ["RetrievalClient"]


class RetrievalClient:
    """Async client for the constant retrieval substrate, with a local cache.

    Args:
        base_url: Backend base URL. Defaults to ``config.RETRIEVE_BASE_URL``.
        client: Optional injected :class:`httpx.AsyncClient`. When provided it is
            used as-is and the caller owns its lifecycle (``close`` / the async
            context manager will **not** close an injected client). When omitted,
            one is created lazily with the configured timeout and closed by us.
        cache_dir: Directory for the optional disk mirror. Defaults to
            ``config.RETRIEVAL_CACHE_DIR``.
        timeout: Per-request timeout in seconds. Defaults to
            ``config.RETRIEVE_TIMEOUT_S``.
        disk_cache: When ``True`` (default) the local result cache is mirrored to
            JSON files so it survives process restart; when ``False`` only the
            in-memory cache is used.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        *,
        client: Optional[httpx.AsyncClient] = None,
        cache_dir: "str | os.PathLike[str] | None" = None,
        timeout: Optional[float] = None,
        disk_cache: bool = True,
    ) -> None:
        self.base_url = (base_url or config.RETRIEVE_BASE_URL).rstrip("/")
        self._retrieve_url = f"{self.base_url}{config.RETRIEVE_ENDPOINT}"
        self._healthz_url = f"{self.base_url}{config.HEALTHZ_ENDPOINT}"
        self._timeout = (
            timeout if timeout is not None else config.RETRIEVE_TIMEOUT_S
        )

        # httpx client (injected => caller-owned; else lazily created + owned).
        self._client = client
        self._owns_client = client is None

        # Two-tier result cache.
        self._mem_cache: dict[tuple, RetrievalResult] = {}
        self._disk_cache_enabled = disk_cache
        self._cache_dir = (
            Path(cache_dir) if cache_dir is not None else config.RETRIEVAL_CACHE_DIR
        )
        if self._disk_cache_enabled:
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    # -- client lifecycle --------------------------------------------------
    def _get_client(self) -> httpx.AsyncClient:
        """Return the httpx client, creating an owned one lazily if needed."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._owns_client = True
        return self._client

    async def close(self) -> None:
        """Close the underlying httpx client iff we created it.

        Injected clients are left open — their lifecycle belongs to the caller.
        """
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "RetrievalClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- cache key / disk mirror ------------------------------------------
    @staticmethod
    def _cache_key(
        query: str,
        filters: Optional[dict],
        candidate_n: Optional[int],
        top_k: Optional[int],
    ) -> tuple:
        """The cache key: ``(query, canonical_filters, candidate_n, top_k)``.

        ``filters`` is canonicalized with ``json.dumps(..., sort_keys=True)`` so
        that semantically-identical filter dicts (any key order) map to the same
        key — mirroring the backend's own memoization key in ``src/retrieve.py``.
        """
        return (
            query,
            json.dumps(filters or {}, sort_keys=True),
            candidate_n,
            top_k,
        )

    def _disk_path(self, key: tuple) -> Path:
        """Stable on-disk filename for a cache key (sha256 of the canonical key)."""
        canonical = json.dumps(list(key), sort_keys=True, default=str)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return self._cache_dir / f"{digest}.json"

    def _load_from_disk(self, key: tuple) -> Optional[RetrievalResult]:
        """Load a cached result from the disk mirror, or ``None`` if absent/bad."""
        path = self._disk_path(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt/partial mirror file is treated as a miss, not a crash.
            return None
        return RetrievalResult(
            fragments=payload["fragments"],
            fragment_ids=payload["fragment_ids"],
            confidence=payload["confidence"],
            timings=payload["timings"],
            cache_hit=payload["cache_hit"],
        )

    def _write_to_disk(self, key: tuple, result: RetrievalResult) -> None:
        """Atomically mirror a result to disk (temp file + ``os.replace``)."""
        path = self._disk_path(key)
        payload = {
            "key": list(key),  # retained for debugging / collision diagnosis
            "fragments": result.fragments,
            "fragment_ids": result.fragment_ids,
            "confidence": result.confidence,
            "timings": result.timings,
            "cache_hit": result.cache_hit,
        }
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)

    # -- public API --------------------------------------------------------
    async def retrieve(
        self,
        query: str,
        filters: Optional[dict] = None,
        candidate_n: Optional[int] = None,
        top_k: Optional[int] = None,
    ) -> RetrievalResult:
        """Fetch ranked fragments for ``query`` (cached per Req 2.5).

        Resolution order: in-memory cache -> disk mirror -> backend ``/retrieve``.
        The same ``(query, filters, candidate_n, top_k)`` always yields identical
        ``fragment_ids`` (Req 2.3), without re-hitting the backend on a hit.
        """
        # Resolve optional params against config (both default to None, so this
        # only matters if a run pins them in config) and key the cache on the
        # resolved values so the request and the key never diverge.
        if candidate_n is None:
            candidate_n = config.RETRIEVE_CANDIDATE_N
        if top_k is None:
            top_k = config.RETRIEVE_TOP_K

        key = self._cache_key(query, filters, candidate_n, top_k)

        # Tier 1: in-memory.
        cached = self._mem_cache.get(key)
        if cached is not None:
            return cached

        # Tier 2: disk mirror (backend-less replay / surviving process restart).
        if self._disk_cache_enabled:
            disk_hit = self._load_from_disk(key)
            if disk_hit is not None:
                self._mem_cache[key] = disk_hit
                return disk_hit

        # Miss: call the backend and map the response verbatim (Req 2.2).
        result = await self._fetch(query, filters, candidate_n, top_k)
        self._mem_cache[key] = result
        if self._disk_cache_enabled:
            self._write_to_disk(key, result)
        return result

    async def _fetch(
        self,
        query: str,
        filters: Optional[dict],
        candidate_n: Optional[int],
        top_k: Optional[int],
    ) -> RetrievalResult:
        """POST to ``/retrieve`` and map the JSON response verbatim."""
        body: dict[str, object] = {"query": query}
        if filters is not None:
            body["filters"] = filters
        if candidate_n is not None:
            body["candidate_n"] = candidate_n
        if top_k is not None:
            body["top_k"] = top_k

        response = await self._get_client().post(self._retrieve_url, json=body)
        response.raise_for_status()
        data = response.json()

        fragments = data["fragments"]
        return RetrievalResult(
            fragments=fragments,
            fragment_ids=[f["id"] for f in fragments],
            confidence=[f.get("confidence") for f in fragments],
            timings=data["timings"],
            cache_hit=data["cache_hit"],
        )

    async def healthz(self) -> bool:
        """Return ``True`` iff the backend reports ``status == "ok"``.

        Used to gate a run at start (Req 2.4). A connection error / unreachable
        backend / non-2xx response is treated as **not healthy** and returns
        ``False`` (so the runner's gate fails fast cleanly) rather than raising.
        """
        try:
            response = await self._get_client().get(self._healthz_url)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError):
            # httpx.HTTPError covers connect/read/timeout/status errors;
            # ValueError covers a non-JSON body. Either => unhealthy.
            return False
        return data.get("status") == "ok"
