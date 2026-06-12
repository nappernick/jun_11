"""
Unit tests for :class:`bakeoff.retrieval_client.RetrievalClient` (Task 4, Req 2).

No real network: every test injects an :class:`httpx.AsyncClient` built on an
:class:`httpx.MockTransport`, so the full verbatim-mapping and cache logic runs
against a canned ``/retrieve`` response. Async calls are driven with
``asyncio.run`` inside sync test functions, so no ``pytest-asyncio`` dependency
is required.

Covered:
* verbatim mapping of the ``/retrieve`` response into ``RetrievalResult``;
* result cache: a repeat call with the same args does NOT hit the transport and
  returns identical ``fragment_ids``; a different query DOES hit it (Req 2.3/2.5);
* disk cache survives a fresh client instance pointed at the same cache dir;
* ``healthz``: ``"ok"`` -> True, ``"degraded"`` -> False, connection error -> False.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from bakeoff.retrieval_client import RetrievalClient


# --- canned backend payloads ---------------------------------------------
CANNED_FRAGMENTS = [
    {
        "id": "frag-1",
        "text": "Corporate Card FAQ",
        "metadata": {"system_job-level": "L5"},
        "fusion_score": 0.81,
        "confidence": 0.74,
    },
    {
        "id": "frag-2",
        "text": "Travel profile name change",
        "metadata": {"system_location-type": "Corporate"},
        "fusion_score": 0.55,
        "confidence": 0.42,
    },
    {
        "id": "frag-3",
        "text": "Expense reimbursement window",
        "metadata": {},
        "fusion_score": 0.33,
        "confidence": 0.21,
    },
]
CANNED_TIMINGS = {
    "embed_query_ms": 120.4,
    "bm25_vectorize_ms": 1.2,
    "hybrid_search_ms": 8.7,
    "rerank_ms": 240.1,
    "total_ms": 370.6,
}


def _retrieve_response(cache_hit: bool = False) -> dict:
    return {
        "fragments": CANNED_FRAGMENTS,
        "timings": CANNED_TIMINGS,
        "cache_hit": cache_hit,
    }


class _Counter:
    """A mutable request counter shared with a MockTransport handler."""

    def __init__(self) -> None:
        self.retrieve = 0
        self.healthz = 0
        self.last_body: dict | None = None


def _make_client(
    counter: _Counter,
    *,
    healthz_status: str = "ok",
    healthz_raises: bool = False,
) -> httpx.AsyncClient:
    """Build an AsyncClient on a MockTransport that counts and answers requests."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/retrieve":
            counter.retrieve += 1
            counter.last_body = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json=_retrieve_response(cache_hit=False))
        if request.url.path == "/healthz":
            counter.healthz += 1
            if healthz_raises:
                raise httpx.ConnectError("backend unreachable", request=request)
            return httpx.Response(
                200,
                json={"status": healthz_status, "collection": "faq_corpus", "points": 56},
            )
        return httpx.Response(404, json={"error": "not found"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- verbatim mapping -----------------------------------------------------
def test_retrieve_maps_response_verbatim():
    counter = _Counter()
    transport_client = _make_client(counter)

    async def run():
        async with RetrievalClient(
            client=transport_client, disk_cache=False
        ) as client:
            return await client.retrieve("how do I get a corporate card?")

    result = asyncio.run(run())

    # fragments: the raw list, untouched.
    assert result.fragments == CANNED_FRAGMENTS
    # fragment_ids: the ranked id list, in order.
    assert result.fragment_ids == ["frag-1", "frag-2", "frag-3"]
    # confidence: per-fragment confidence, in order.
    assert result.confidence == [0.74, 0.42, 0.21]
    # timings: the response dict, identical.
    assert result.timings == CANNED_TIMINGS
    # cache_hit: passed through.
    assert result.cache_hit is False
    # one backend call happened.
    assert counter.retrieve == 1


def test_retrieve_sends_optional_params_in_body():
    counter = _Counter()
    transport_client = _make_client(counter)

    async def run():
        async with RetrievalClient(
            client=transport_client, disk_cache=False
        ) as client:
            await client.retrieve(
                "q", filters={"system_job-level": "L5"}, candidate_n=20, top_k=5
            )

    asyncio.run(run())
    assert counter.last_body == {
        "query": "q",
        "filters": {"system_job-level": "L5"},
        "candidate_n": 20,
        "top_k": 5,
    }


# --- in-memory cache behavior --------------------------------------------
def test_cache_prevents_second_backend_hit_and_is_identical():
    counter = _Counter()
    transport_client = _make_client(counter)

    async def run():
        async with RetrievalClient(
            client=transport_client, disk_cache=False
        ) as client:
            first = await client.retrieve("same query", filters={"a": "1"})
            second = await client.retrieve("same query", filters={"a": "1"})
            third = await client.retrieve("a different query")
            return first, second, third

    first, second, third = asyncio.run(run())

    # The repeat (query, filters) call did not hit the transport again...
    assert counter.retrieve == 2  # call 1 (same), call 3 (different); NOT call 2
    # ...and returned identical fragment_ids.
    assert second.fragment_ids == first.fragment_ids
    assert second.fragments == first.fragments
    # A different query did hit the backend.
    assert third.fragment_ids == first.fragment_ids  # canned response is the same
    assert counter.retrieve == 2


def test_cache_key_is_filter_order_insensitive():
    counter = _Counter()
    transport_client = _make_client(counter)

    async def run():
        async with RetrievalClient(
            client=transport_client, disk_cache=False
        ) as client:
            await client.retrieve("q", filters={"a": "1", "b": "2"})
            # Same filters, different key order -> same cache key -> no new hit.
            await client.retrieve("q", filters={"b": "2", "a": "1"})

    asyncio.run(run())
    assert counter.retrieve == 1


# --- disk cache survives a new client instance ---------------------------
def test_disk_cache_survives_new_client_instance(tmp_path):
    counter = _Counter()

    async def run():
        # Client A writes to the disk mirror.
        client_a = RetrievalClient(
            client=_make_client(counter), cache_dir=tmp_path, disk_cache=True
        )
        async with client_a:
            a = await client_a.retrieve("persisted query", top_k=5)

        # Client B is a fresh instance with a fresh transport, same cache dir.
        # Its transport would count a hit if it were used.
        client_b = RetrievalClient(
            client=_make_client(counter), cache_dir=tmp_path, disk_cache=True
        )
        async with client_b:
            b = await client_b.retrieve("persisted query", top_k=5)
        return a, b

    a, b = asyncio.run(run())

    # Exactly one backend call total: client A. Client B served from disk.
    assert counter.retrieve == 1
    assert b.fragment_ids == a.fragment_ids
    assert b.fragments == a.fragments
    assert b.timings == a.timings


def test_disk_cache_disabled_does_not_persist(tmp_path):
    counter = _Counter()

    async def run():
        client_a = RetrievalClient(
            client=_make_client(counter), cache_dir=tmp_path, disk_cache=False
        )
        async with client_a:
            await client_a.retrieve("ephemeral query")

        client_b = RetrievalClient(
            client=_make_client(counter), cache_dir=tmp_path, disk_cache=False
        )
        async with client_b:
            await client_b.retrieve("ephemeral query")

    asyncio.run(run())
    # With disk cache off, the fresh client must re-hit the backend.
    assert counter.retrieve == 2
    # And nothing was written to the temp cache dir.
    assert list(tmp_path.iterdir()) == []


# --- healthz --------------------------------------------------------------
def test_healthz_ok_is_true():
    counter = _Counter()
    transport_client = _make_client(counter, healthz_status="ok")

    async def run():
        async with RetrievalClient(
            client=transport_client, disk_cache=False
        ) as client:
            return await client.healthz()

    assert asyncio.run(run()) is True
    assert counter.healthz == 1


def test_healthz_degraded_is_false():
    counter = _Counter()
    transport_client = _make_client(counter, healthz_status="degraded")

    async def run():
        async with RetrievalClient(
            client=transport_client, disk_cache=False
        ) as client:
            return await client.healthz()

    assert asyncio.run(run()) is False


def test_healthz_connection_error_is_false():
    counter = _Counter()
    transport_client = _make_client(counter, healthz_raises=True)

    async def run():
        async with RetrievalClient(
            client=transport_client, disk_cache=False
        ) as client:
            return await client.healthz()

    # A connection error surfaces as "not healthy" (False), not an exception.
    assert asyncio.run(run()) is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
