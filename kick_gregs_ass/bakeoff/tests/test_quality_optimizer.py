"""
Unit / example / smoke tests for the closed-loop prompt optimizer (Tasks 4.2 + 6.2).

This file holds the plain-``pytest`` example/edge tests for the optimizer's
deterministic, zero-network surface. The Hypothesis property tests live in the
separate ``test_quality_optimizer_pbt.py`` (owned elsewhere); nothing here uses
Hypothesis.

Two task areas are covered:

* **Task 4.2 — RetrievalBackend unit tests** (Req 16.3, 16.4, 16.5, 13.3): Protocol
  conformance against the ``runtime_checkable`` ``RetrievalBackend`` Protocol; the
  identical ``{id, text, metadata, ...}`` fragment shape across all three concrete
  implementations (exercised with fakes / injected fake clients — no real AWS, no
  network); read-only behavior; and the held-constant memoization guarantee that a
  repeated ``(turn-query)`` returns byte-identical fragments regardless of the prompt
  role / instruction (which are not even part of ``RetrievalQuery``, so they cannot
  enter the cache key). The OpenSearch path is exercised entirely through an injected
  **fake** OpenSearch client.

* **Task 6.2 — Prompting_Guidance smoke tests** (Req 15.2, 15.3, 15.6): the guidance
  covers the required sections; it is a repo constant (a non-empty ``str`` available at
  import) and is NOT parsed from the raw PDF at runtime; and it is flagged
  external/vendor-sourced.

Async backends are driven with ``asyncio.run`` inside sync test functions (mirroring
``test_retrieval_client.py``), so no ``pytest-asyncio`` dependency is required. Every
network-touching path uses an injected offline double, so the whole module runs in the
standard offline suite.
"""
from __future__ import annotations

import asyncio
import builtins
import importlib
import json
import os

import httpx
import pytest

from bakeoff.quality.optimizer import prompting_guidance as pg
from bakeoff.quality.optimizer.retrieval import (
    FakeRetrievalBackend,
    LocalRetrievalBackend,
    MemoizingRetrievalBackend,
    OpenSearchRetrievalBackend,
    RerankedRetrievalBackend,
    RetrievalBackend,
    RetrievalQuery,
    build_retrieval_backend,
)


# The core fragment shape every implementation must return (Req 16.4). Concrete
# backends may add fields, but these three keys (and their types) are the contract
# downstream grounding/judging relies on.
_REQUIRED_FRAGMENT_KEYS = {"id", "text", "metadata"}


def _run(coro):
    """Drive an awaitable to completion without a pytest-asyncio plugin."""
    return asyncio.run(coro)


def _assert_fragment_shape(fragments) -> None:
    """Assert a retrieve() result is a sequence of common-shape fragments."""
    assert isinstance(fragments, (list, tuple))
    assert len(fragments) >= 1
    for frag in fragments:
        assert isinstance(frag, dict)
        assert _REQUIRED_FRAGMENT_KEYS.issubset(frag.keys())
        assert isinstance(frag["id"], str)
        assert isinstance(frag["text"], str)
        assert isinstance(frag["metadata"], dict)


# ---------------------------------------------------------------------------
# Offline doubles (zero network): a fake OpenSearch client and a fake /retrieve
# HTTP transport. Both record what was invoked so the read-only assertions can
# prove no write/mutation method was ever exercised.
# ---------------------------------------------------------------------------
_OS_HITS = [
    {
        "_id": "frag-1",
        "_score": 0.81,
        "_source": {"text": "Corporate Card FAQ", "metadata": {"job_level": "L5"}},
    },
    {
        "_id": "frag-2",
        "_score": 0.55,
        "_source": {"text": "Travel profile name change", "metadata": {"loc": "Corp"}},
    },
]

# The local /retrieve service already returns fragments in the common shape; the
# canned payload mirrors that exactly so the cross-impl shape comparison is honest.
_LOCAL_FRAGMENTS = [
    {
        "id": "frag-1",
        "text": "Corporate Card FAQ",
        "metadata": {"job_level": "L5"},
        "confidence": 0.81,
    },
    {
        "id": "frag-2",
        "text": "Travel profile name change",
        "metadata": {"loc": "Corp"},
        "confidence": 0.55,
    },
]

# Read vs write OpenSearch surface: only ``search`` is a read query; the rest mutate.
_OS_WRITE_METHODS = ("index", "update", "delete", "bulk", "create", "delete_by_query")


class _FakeOpenSearchClient:
    """A network-free stand-in for an opensearch-py client (Req 16.5/16.6 test path).

    Exposes the ``search(index=, body=)`` read shape the backend calls and records every
    method invocation in ``calls`` so a test can assert the backend issued a read query
    only. The write-ish methods are present (so calling one would be recorded) but the
    backend must never touch them.
    """

    def __init__(self, hits=None):
        self._hits = hits if hits is not None else _OS_HITS
        self.calls: list[str] = []
        self.search_bodies: list[dict] = []

    def search(self, index=None, body=None):
        self.calls.append("search")
        self.search_bodies.append({"index": index, "body": body})
        return {"hits": {"hits": self._hits}}

    def _record_write(self, name):
        def _method(*_args, **_kwargs):
            self.calls.append(name)
            return {}

        return _method

    def __getattr__(self, name):
        # Any write-ish attribute access returns a recorder so an accidental mutate call
        # is captured (and fails the read-only assertion) rather than raising AttributeError.
        if name in _OS_WRITE_METHODS:
            return self._record_write(name)
        raise AttributeError(name)


def _make_local_client(recorder, fragments=None):
    """Build an httpx.AsyncClient on a MockTransport that answers POST /retrieve.

    ``recorder`` captures the request method/path/body so the read-only assertions can
    confirm the backend issued only the documented read query.
    """
    payload = fragments if fragments is not None else _LOCAL_FRAGMENTS

    def handler(request: httpx.Request) -> httpx.Response:
        recorder["method"] = request.method
        recorder["path"] = request.url.path
        recorder["body"] = json.loads(request.content.decode("utf-8"))
        if request.url.path == "/retrieve":
            return httpx.Response(200, json={"fragments": payload, "timings": {}})
        return httpx.Response(404, json={"error": "not found"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class _VaryingBackend:
    """An inner backend whose output changes on EVERY call (zero network).

    Used to prove the memoization layer pins the first result: if the wrapper ever
    re-invoked the inner backend, the fragments would differ. Because the prompt role
    and instruction text are not part of ``RetrievalQuery`` at all, the only way a
    Champion and a Challenger could ever get different fragments for the same turn is a
    cache miss — which this backend makes loudly visible.
    """

    name = "varying"

    def __init__(self):
        self.calls = 0

    async def retrieve(self, q: RetrievalQuery):
        self.calls += 1
        n = self.calls
        return [
            {
                "id": f"frag-{n}",
                "text": f"call {n} for {q.item_id} t{q.turn}",
                "metadata": {"call": n},
                "confidence": round(1.0 / (n + 1), 4),
            }
        ]


# ===========================================================================
# Task 4.2 — RetrievalBackend unit tests (Req 16.3, 16.4, 16.5, 13.3)
# ===========================================================================

# --- Protocol conformance (Req 16.3) ---------------------------------------
def test_all_backends_are_instances_of_the_runtime_checkable_protocol():
    """Every concrete impl (and the memoizing wrapper) satisfies the read-only
    ``RetrievalBackend`` Protocol via ``isinstance`` (Req 16.3: one interface)."""
    fake = FakeRetrievalBackend()
    local = LocalRetrievalBackend(base_url="http://test", client=_make_local_client({}))
    opensearch = OpenSearchRetrievalBackend(client=_FakeOpenSearchClient())
    memo = MemoizingRetrievalBackend(fake)

    for backend in (fake, local, opensearch, memo):
        assert isinstance(backend, RetrievalBackend)
        # The Protocol contract: a stable ``name`` plus an async ``retrieve``.
        assert isinstance(backend.name, str) and backend.name
        assert callable(backend.retrieve)


def test_non_conforming_object_is_not_a_backend():
    """An object lacking ``retrieve`` is not a ``RetrievalBackend`` — the conformance
    check is meaningful, not vacuously true."""
    class _NotABackend:
        name = "nope"

    assert not isinstance(_NotABackend(), RetrievalBackend)
    assert not isinstance(object(), RetrievalBackend)


def test_build_retrieval_backend_returns_protocol_with_name_passthrough():
    """The selector always returns a Protocol-conforming, memoizing-wrapped backend
    whose ``name`` still reflects the underlying impl (Req 16.3)."""
    fake_built = build_retrieval_backend("fake")
    assert isinstance(fake_built, RetrievalBackend)
    assert isinstance(fake_built, MemoizingRetrievalBackend)
    assert fake_built.name == "fake"

    # OpenSearch path with an injected fake client stays usable and keeps its name.
    os_built = build_retrieval_backend(
        "opensearch", opensearch_client=_FakeOpenSearchClient()
    )
    assert isinstance(os_built, RetrievalBackend)
    assert os_built.name == "opensearch"


# --- Identical fragment shape across all three impls (Req 16.4) ------------
def test_identical_fragment_shape_across_all_three_impls():
    """Fake, OpenSearch (injected fake client), and Local (injected fake transport) all
    return fragments in the common ``{id, text, metadata, ...}`` shape (Req 16.4)."""
    q = RetrievalQuery(item_id="c0-s01", turn=1, query="how do I get a corporate card?")

    fake_frags = _run(FakeRetrievalBackend().retrieve(q))

    os_backend = OpenSearchRetrievalBackend(client=_FakeOpenSearchClient())
    os_frags = _run(os_backend.retrieve(q))

    local_backend = LocalRetrievalBackend(
        base_url="http://test", client=_make_local_client({})
    )
    local_frags = _run(local_backend.retrieve(q))

    for frags in (fake_frags, os_frags, local_frags):
        _assert_fragment_shape(frags)

    # With the canned offline data, the shape is not just compatible but identical:
    # all three carry exactly {id, text, metadata, confidence}.
    fake_keys = {frozenset(f.keys()) for f in fake_frags}
    os_keys = {frozenset(f.keys()) for f in os_frags}
    local_keys = {frozenset(f.keys()) for f in local_frags}
    assert fake_keys == os_keys == local_keys == {frozenset({"id", "text", "metadata", "confidence"})}


def test_opensearch_hit_mapping_to_common_shape():
    """An OpenSearch hit maps to the common fragment shape: ``_id``→id, ``_source.text``→
    text, ``_source.metadata``→metadata, ``_score``→confidence (Req 16.4)."""
    backend = OpenSearchRetrievalBackend(client=_FakeOpenSearchClient())
    frags = _run(backend.retrieve(RetrievalQuery("c0-s01", 1, "card")))
    assert [f["id"] for f in frags] == ["frag-1", "frag-2"]
    assert frags[0]["text"] == "Corporate Card FAQ"
    assert frags[0]["metadata"] == {"job_level": "L5"}
    assert frags[0]["confidence"] == pytest.approx(0.81)


def test_local_returns_fragments_verbatim_in_common_shape():
    """The local ``/retrieve`` response's fragments are returned verbatim in the common
    shape (Req 16.4)."""
    recorder: dict = {}
    backend = LocalRetrievalBackend(
        base_url="http://test", client=_make_local_client(recorder)
    )
    frags = _run(backend.retrieve(RetrievalQuery("c0-s01", 1, "card")))
    assert frags == _LOCAL_FRAGMENTS


# --- Read-only behavior (Req 16.5) -----------------------------------------
def test_opensearch_issues_read_only_search_query():
    """The OpenSearch backend issues only a read ``search`` query and never a write
    method, and the body is a bounded read query (Req 16.5)."""
    client = _FakeOpenSearchClient()
    backend = OpenSearchRetrievalBackend(index="faq", client=client)
    _run(backend.retrieve(RetrievalQuery("c0-s01", 1, "card", filters={"job_level": "L5"})))

    # Only the read path was exercised — no index/update/delete/bulk/create.
    assert client.calls == ["search"]
    body = client.search_bodies[0]["body"]
    # A read query body: a bounded bool/match search, with the filter mapped to a term.
    assert "query" in body
    assert body["query"]["bool"]["must"] == [{"match": {"text": "card"}}]
    assert {"term": {"metadata.job_level": "L5"}} in body["query"]["bool"]["filter"]
    # No mutation/write directives leak into the request body.
    assert not (set(body) & {"index", "update", "delete", "create", "doc", "script"})


def test_local_issues_read_only_post_to_retrieve():
    """The local backend issues a read-only ``POST /retrieve`` query whose body carries
    only query parameters — no mutation fields (Req 16.5)."""
    recorder: dict = {}
    backend = LocalRetrievalBackend(
        base_url="http://test", client=_make_local_client(recorder)
    )
    _run(
        backend.retrieve(
            RetrievalQuery("c0-s01", 1, "card", filters={"a": "1"}, candidate_n=20, top_k=5)
        )
    )
    assert recorder["method"] == "POST"
    assert recorder["path"] == "/retrieve"
    # The body is a pure query descriptor; it contains no write/mutation keys.
    assert set(recorder["body"]).issubset({"query", "filters", "candidate_n", "top_k"})
    assert recorder["body"]["query"] == "card"


# --- Held-constant memoization across champion/challenger (Req 13.3) -------
def test_memoization_returns_identical_fragments_regardless_of_role_or_instruction():
    """A repeated ``(turn-query)`` yields byte-identical fragments and re-invokes the
    inner backend exactly once (Req 13.3 / 12.4).

    The prompt *role* (champion vs challenger) and the *instruction text* are not part of
    ``RetrievalQuery``, so they cannot enter the cache key. We model a Champion request
    and a Challenger request as two retrieve() calls with the same query; the varying
    inner backend would return different fragments on a second call, so identical output
    proves the result was served from cache — i.e. retrieval is held constant and the
    instruction is the only varied element.
    """
    inner = _VaryingBackend()
    memo = MemoizingRetrievalBackend(inner)
    q = RetrievalQuery(item_id="c0-s01", turn=1, query="how do I get a corporate card?")

    champion_frags = _run(memo.retrieve(q))      # scored under the Champion instruction
    challenger_frags = _run(memo.retrieve(q))    # same turn, scored under the Challenger

    assert inner.calls == 1                       # inner hit once; challenger served cached
    assert champion_frags == challenger_frags     # byte-identical fragments
    assert [f["id"] for f in challenger_frags] == ["frag-1"]

    # A freshly constructed but equal query (different object identity) still hits the
    # same cache key — equality of the (item_id, turn, query, filters, ...) fields, not
    # object identity or any prompt context, decides the key.
    equal_q = RetrievalQuery(item_id="c0-s01", turn=1, query="how do I get a corporate card?")
    again = _run(memo.retrieve(equal_q))
    assert inner.calls == 1
    assert again == champion_frags


def test_memoization_distinguishes_different_turn_queries():
    """A different turn or query text is a cache miss, so the held-constant guarantee is
    scoped to the actual ``(turn-query)`` and does not collapse distinct turns."""
    inner = _VaryingBackend()
    memo = MemoizingRetrievalBackend(inner)

    _run(memo.retrieve(RetrievalQuery("c0-s01", 1, "card")))
    _run(memo.retrieve(RetrievalQuery("c0-s01", 2, "card")))       # different turn
    _run(memo.retrieve(RetrievalQuery("c0-s01", 2, "expenses")))   # different query
    assert inner.calls == 3


def test_memoization_cache_key_is_filter_order_insensitive():
    """Two semantically-identical filter dicts in any key order collapse to one cache
    entry (the key uses ``frozenset(filters.items())``)."""
    inner = _VaryingBackend()
    memo = MemoizingRetrievalBackend(inner)

    _run(memo.retrieve(RetrievalQuery("c0-s01", 1, "card", filters={"a": "1", "b": "2"})))
    _run(memo.retrieve(RetrievalQuery("c0-s01", 1, "card", filters={"b": "2", "a": "1"})))
    assert inner.calls == 1


def test_memoization_preserves_underlying_backend_name():
    """The wrapper is transparent about identity — ``name`` passes the wrapped backend's
    name through so audit records still see the real backend (Req 16.3)."""
    assert MemoizingRetrievalBackend(FakeRetrievalBackend()).name == "fake"
    assert MemoizingRetrievalBackend(OpenSearchRetrievalBackend(client=_FakeOpenSearchClient())).name == "opensearch"
    assert MemoizingRetrievalBackend(LocalRetrievalBackend(base_url="http://test")).name == "local"


def test_build_opensearch_falls_back_to_local_when_unusable():
    """When OpenSearch is unconfigured/unusable the selector falls back to the local
    backend, still wrapped for held-constant reuse and still read-only (Req 16.2/16.5)."""
    # No client and no endpoint/index -> is_usable() is False -> local fallback.
    built = build_retrieval_backend("opensearch")
    assert isinstance(built, MemoizingRetrievalBackend)
    assert built.name == "local"


# --- Rerank v4 second stage (optimizer v2 only) -----------------------------
class _FakeBody:
    """Minimal ``invoke_endpoint`` response body: bytes with a ``.read()``."""

    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload


class _FakeSageMakerRuntimeClient:
    """A network-free stand-in for a ``sagemaker-runtime`` client.

    Records every ``invoke_endpoint`` call (endpoint name + parsed body) and answers
    with a canned Cohere Rerank response, so tests can assert exactly what the wrapper
    sent and how it mapped the reranked order back onto fragments.
    """

    def __init__(self, results=None):
        # Default: reverse of input order, with descending relevance scores.
        self._results = results
        self.calls: list[dict] = []

    def invoke_endpoint(self, EndpointName=None, ContentType=None, Accept=None, Body=None):
        request = json.loads(Body)
        self.calls.append({"endpoint": EndpointName, "body": request})
        if self._results is not None:
            results = self._results
        else:
            doc_count = len(request["documents"])
            results = [
                {"index": doc_index, "relevance_score": round(0.9 - 0.1 * rank, 4)}
                for rank, doc_index in enumerate(reversed(range(doc_count)))
            ][: request.get("top_n", doc_count)]
        return {"Body": _FakeBody({"results": results})}


class _FixedCandidatesBackend:
    """Inner backend returning a fixed candidate pool; records the queries it saw."""

    name = "opensearch"

    def __init__(self, count=4):
        self._count = count
        self.queries: list[RetrievalQuery] = []

    async def retrieve(self, q: RetrievalQuery):
        self.queries.append(q)
        return [
            {
                "id": f"frag-{pos}",
                "text": f"candidate text {pos}",
                "metadata": {"pos": pos},
                "confidence": round(1.0 / (pos + 1), 4),
            }
            for pos in range(self._count)
        ]


def test_reranked_backend_reorders_by_relevance_and_cuts_to_top_k():
    """The wrapper fetches the candidate pool, sends it to the rerank endpoint, and
    returns fragments in the endpoint's order with ``relevance_score`` as confidence."""
    inner = _FixedCandidatesBackend(count=4)
    client = _FakeSageMakerRuntimeClient(
        results=[
            {"index": 2, "relevance_score": 0.97},
            {"index": 0, "relevance_score": 0.55},
            {"index": 3, "relevance_score": 0.12},
        ]
    )
    backend = RerankedRetrievalBackend(
        inner, endpoint_name="cohere-rerank-v4-0-pro", client=client
    )
    frags = _run(backend.retrieve(RetrievalQuery("c0-s01", 1, "card", top_k=3)))

    assert [f["id"] for f in frags] == ["frag-2", "frag-0", "frag-3"]
    assert [f["confidence"] for f in frags] == [0.97, 0.55, 0.12]
    # Read-only + correct request shape: one invoke, the turn's query text, the
    # candidate texts, and top_n bounded by top_k.
    assert len(client.calls) == 1
    sent = client.calls[0]
    assert sent["endpoint"] == "cohere-rerank-v4-0-pro"
    assert sent["body"]["query"] == "card"
    assert sent["body"]["documents"] == [f"candidate text {pos}" for pos in range(4)]
    assert sent["body"]["top_n"] == 3


def test_reranked_backend_widens_candidate_pool_and_passes_name_through():
    """The inner backend is queried for the FULL candidate pool (top_k cleared,
    candidate_n pinned), and the wrapper is identity-transparent like the memo layer."""
    inner = _FixedCandidatesBackend()
    backend = RerankedRetrievalBackend(
        inner,
        endpoint_name="cohere-rerank-v4-0-pro",
        candidate_n=20,
        client=_FakeSageMakerRuntimeClient(),
    )
    assert backend.name == "opensearch"

    _run(backend.retrieve(RetrievalQuery("c0-s01", 1, "card", top_k=5)))
    widened = inner.queries[0]
    assert widened.top_k is None
    assert widened.candidate_n == 20
    # The turn identity/filters that key the memo cache upstream are untouched.
    assert (widened.item_id, widened.turn, widened.query) == ("c0-s01", 1, "card")


def test_reranked_backend_short_circuits_tiny_pools_without_invoking_endpoint():
    """Zero or one candidate means nothing to reorder — the endpoint is never invoked."""
    client = _FakeSageMakerRuntimeClient()
    backend = RerankedRetrievalBackend(
        _FixedCandidatesBackend(count=1), endpoint_name="cohere-rerank-v4-0-pro", client=client
    )
    frags = _run(backend.retrieve(RetrievalQuery("c0-s01", 1, "card", top_k=5)))
    assert [f["id"] for f in frags] == ["frag-0"]
    assert client.calls == []


def test_build_retrieval_backend_applies_rerank_inside_memo_on_opensearch_only():
    """Opting in via ``rerank_endpoint_name`` wraps the AOSS backend as
    ``memo(rerank(opensearch))``; the bare default and the local fallback stay
    rerank-free, so nothing outside the v2 live path ever touches the endpoint."""
    # Opted in, OpenSearch usable -> memo(rerank(opensearch)), name passthrough intact.
    built = build_retrieval_backend(
        "opensearch",
        opensearch_client=_FakeOpenSearchClient(),
        rerank_endpoint_name="cohere-rerank-v4-0-pro",
        rerank_client=_FakeSageMakerRuntimeClient(),
    )
    assert isinstance(built, MemoizingRetrievalBackend)
    assert isinstance(built.inner, RerankedRetrievalBackend)
    assert isinstance(built.inner.inner, OpenSearchRetrievalBackend)
    assert built.name == "opensearch"

    # Bare default (no rerank_endpoint_name) -> unchanged memo(opensearch).
    bare = build_retrieval_backend("opensearch", opensearch_client=_FakeOpenSearchClient())
    assert isinstance(bare.inner, OpenSearchRetrievalBackend)

    # OpenSearch unusable -> local fallback is NOT rerank-wrapped (it reranks internally).
    fallback = build_retrieval_backend(
        "opensearch", rerank_endpoint_name="cohere-rerank-v4-0-pro"
    )
    assert fallback.name == "local"
    assert isinstance(fallback.inner, LocalRetrievalBackend)


def test_memo_over_rerank_serves_repeat_queries_without_reinvoking_endpoint():
    """A repeated (turn-query) is served from the memo cache: the rerank endpoint and
    the inner backend are each hit exactly once (Req 13.3 extended to the reranked
    result — and no duplicate spend on the personal-account endpoint)."""
    client = _FakeSageMakerRuntimeClient()
    inner = _FixedCandidatesBackend()
    memo = MemoizingRetrievalBackend(
        RerankedRetrievalBackend(inner, endpoint_name="cohere-rerank-v4-0-pro", client=client)
    )
    q = RetrievalQuery("c0-s01", 1, "card", top_k=3)

    champion_frags = _run(memo.retrieve(q))
    challenger_frags = _run(memo.retrieve(q))
    assert champion_frags == challenger_frags
    assert len(client.calls) == 1
    assert len(inner.queries) == 1


# ===========================================================================
# Task 6.2 — Prompting_Guidance smoke tests (Req 15.2, 15.3, 15.6)
# ===========================================================================

# --- Required-section coverage (Req 15.2) ----------------------------------
# Each tuple is (human label, list of lowercase substrings, at least one of which must
# appear) — the guidance must touch every required section.
_REQUIRED_SECTION_KEYWORDS = [
    ("Claude 4.5 XML/tagged layered structure", ["xml", "tagged", "layered"]),
    ("refusal/abstention handling", ["abstention", "refusal", "decline"]),
    ("tone and formatting control", ["tone", "formatting"]),
    ("knowledge-grounding", ["grounding", "grounded", "evidence"]),
    ("steerability", ["steerab"]),
    ("Claude 4.x ALL-CAPS over-trigger caution", ["all-caps", "over-trigger", "over-triggering"]),
]


@pytest.mark.parametrize("label,keywords", _REQUIRED_SECTION_KEYWORDS)
def test_prompting_guidance_covers_required_section(label, keywords):
    """PROMPTING_GUIDANCE covers every section required by Req 15.2."""
    text = pg.PROMPTING_GUIDANCE.lower()
    assert any(k in text for k in keywords), f"missing required section: {label}"


def test_prompting_guidance_mentions_tagged_structure_and_tone_formatting():
    """Spot-check the two compound sections so the keyword scan can't pass on a single
    stray word: tagged/layered structure AND both tone and formatting are present."""
    text = pg.PROMPTING_GUIDANCE.lower()
    assert ("xml" in text or "tag" in text) and "layered" in text
    assert "tone" in text and "formatting" in text
    # The 4.x caution is phrased as a calm-not-loud instruction about ALL-CAPS emphasis.
    assert "all-caps" in text and ("over-trigger" in text or "over-triggering" in text)


def test_grounding_abstention_excerpt_covers_grounding_and_abstention():
    """The judge-facing excerpt covers both halves of the rule (Req 15.2/15.4)."""
    text = pg.GROUNDING_ABSTENTION_EXCERPT.lower()
    assert "grounding" in text or "grounded" in text
    assert "abstention" in text or "abstain" in text or "decline" in text


# --- Repo constant, NOT parsed from the PDF at runtime (Req 15.3) ----------
def test_prompting_guidance_constants_are_nonempty_strings_at_import():
    """The guidance is available as a non-empty ``str`` module constant immediately at
    import — no runtime loading, no lazy parse (Req 15.3)."""
    assert isinstance(pg.PROMPTING_GUIDANCE, str)
    assert pg.PROMPTING_GUIDANCE.strip()
    assert isinstance(pg.GROUNDING_ABSTENTION_EXCERPT, str)
    assert pg.GROUNDING_ABSTENTION_EXCERPT.strip()


def test_prompting_guidance_module_does_not_read_a_pdf():
    """Static guard: the module imports no PDF-parsing dependency (Req 15.3).

    The constant is baked in as string literals, so the module never needs a PDF reader.
    (The runtime no-open behavior is proven separately by the reload tripwire below; this
    test only asserts the absence of a parsing dependency in the source.)
    """
    with open(pg.__file__, "r", encoding="utf-8") as fh:
        source = fh.read().lower()
    for pdf_lib in ("pypdf", "pypdf2", "pdfplumber", "pdfminer", "fitz"):
        assert f"import {pdf_lib}" not in source
        assert f"from {pdf_lib}" not in source


def test_prompting_guidance_import_does_not_open_the_pdf(monkeypatch):
    """Dynamic guard: reloading the module with a tripwire on any ``.pdf`` ``open()``
    still succeeds and yields the constant — proving the PDF is never read at import
    (Req 15.3)."""
    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        if isinstance(file, (str, bytes, os.PathLike)) and str(file).lower().endswith(".pdf"):
            raise AssertionError(f"Prompting_Guidance must not open the PDF at runtime: {file!r}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    reloaded = importlib.reload(pg)
    try:
        assert isinstance(reloaded.PROMPTING_GUIDANCE, str)
        assert reloaded.PROMPTING_GUIDANCE.strip()
    finally:
        # Restore the original ``open`` before reloading once more so the module object
        # the rest of the suite sees is rebuilt under normal conditions.
        monkeypatch.undo()
        importlib.reload(pg)


# --- Flagged external / vendor-sourced (Req 15.6) --------------------------
def test_prompting_guidance_is_flagged_external_vendor_sourced():
    """The guidance carries its source-attribution: external / vendor-sourced, not an
    Amazon-internal primary source (Req 15.6)."""
    guidance = pg.PROMPTING_GUIDANCE.lower()
    assert "external" in guidance
    assert "vendor" in guidance

    # The module-level provenance documentation states it is not Amazon-internal and
    # names the originating PDF as the (human-consulted) source, not a runtime input.
    module_doc = (pg.__doc__ or "").lower()
    assert "external" in module_doc and "vendor" in module_doc
    assert "amazon-internal" in module_doc
    assert "modern_system_prompting.pdf" in module_doc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


# ===========================================================================
# Task 7.2 — JudgeInLoopScorer unit / example tests
# (design Component 2 "JudgeInLoopScorer", "Retrieval-always data flow",
#  "Abstention weighting"; Req 2.2, 2.3, 2.5, 2.6, 13.7, 13.8, 14.2, 14.7)
# ===========================================================================
#
# These tests exercise the synchronous, per-iteration decision metric of the
# closed-loop optimizer with ZERO network. They build a minimal DUCK-TYPED
# backend exposing exactly the four attributes ``JudgeInLoopScorer.score_prompt``
# reads (``answer_adapter_factory``, ``judge_scorer``, ``closeness_scorer``,
# ``retrieval``) — deliberately NOT importing ``backends.py`` (which may be
# authored concurrently and may not exist at import time). We reuse the existing
# offline doubles where possible: ``build_offline_scorers`` (StubJudge-backed
# ``JudgeScorer`` + fake-embed ``TurnClosenessScorer``) from ``quality.main`` and
# the network-free ``FakeRetrievalBackend`` from ``quality.optimizer.retrieval``.
# Async is driven with ``asyncio.run`` via the existing ``_run`` helper.
#
# The load-bearing properties under test (design Component 2):
#   * the scorer wraps a real ``JudgeScorer`` and the triad is THE decision metric
#     (Req 2.2);
#   * closeness is a SECONDARY cross-check only — recorded but never driving the
#     score (Req 2.3), proven by showing two wildly different closeness scorers
#     produce byte-identical triad scores;
#   * per-dimension means are recorded (Req 2.6);
#   * on an over-refusal turn the triad governs even when closeness is HIGH
#     (Req 2.5);
#   * the judge grounds on the SAME fragments retrieval produced for that turn
#     (Req 13.7);
#   * a correct decline on an unanswerable/insufficient turn is NOT penalized —
#     it is rewarded above its own triad (Req 13.8, 14.7);
#   * the abstention-weighting summary fields are recorded (Req 14.2).

from bakeoff import config
from bakeoff.quality.main import build_offline_scorers
from bakeoff.quality.optimizer.judge_loop import (
    REFUSAL,
    JudgeInLoopScorer,
    SliceScore,
    TurnVerdict,
)
from bakeoff.quality.types import GroundTruthKind, TurnCloseness
from bakeoff.scoring.judge import JUDGE_DIMENSIONS, JudgeScorer, make_stub_judge
from bakeoff.types import CohortKey, GoldFragment, Item, ModelResponse, Turn


# A grounded, declining answer that the abstention detectors (the module-level
# ``REFUSAL`` regex AND the StubJudge's own refusal hints) both classify as a
# correct decline rather than a guess.
_DECLINE = (
    "I don't have that information in the reference material. "
    "Please contact your support team for help."
)
# A confident fabrication: neither detector reads this as a decline, so on an
# unanswerable turn it is scored as answering-when-unsure (the expensive error).
_FABRICATION = (
    "Yes, you can complete this within 30 days by submitting the standard form; "
    "it is then approved automatically."
)


# ---------------------------------------------------------------------------
# Minimal offline doubles for the JudgeInLoopScorer (zero network)
# ---------------------------------------------------------------------------
class _DuckBackend:
    """The minimal DUCK-TYPED backend ``score_prompt`` reads — nothing more.

    Exposes exactly ``answer_adapter_factory`` / ``judge_scorer`` /
    ``closeness_scorer`` / ``retrieval`` (plus a ``name`` for parity with the real
    bundle). It never imports or depends on ``backends.py``; the scorer only does
    attribute access on these four members.
    """

    def __init__(self, *, answer_adapter_factory, judge_scorer, closeness_scorer, retrieval, name="offline-test"):
        self.name = name
        self.answer_adapter_factory = answer_adapter_factory
        self.judge_scorer = judge_scorer
        self.closeness_scorer = closeness_scorer
        self.retrieval = retrieval


class _ScriptedAdapter:
    """A deterministic answer adapter that returns scripted per-turn answers.

    Mirrors the ``ModelAdapter`` contract the scorer uses (``name`` + ``async
    generate``). The scorer calls ``generate`` once per conversation and reads
    ``per_turn_answers``; we return exactly the answers scripted for the item so a
    test can place a refusal or a fabrication on a chosen turn.
    """

    def __init__(self, name, answers_by_item):
        self.name = name
        self._answers_by_item = answers_by_item

    async def generate(self, item, fragments, temperature):
        answers = list(self._answers_by_item.get(item.item_id, ["I'll help with that."]))
        return ModelResponse(
            text=answers[-1] if answers else "",
            ttft_ms=1.0,
            generation_total_ms=float(len(answers)),
            token_usage={"prompt": 0, "completion": 0, "total": 0},
            per_turn_answers=answers,
            finish_reason="stop",
            model=self.name,
        )


def _scripted_factory(answers_by_item):
    """Build an ``AnswerAdapterFactory`` returning a scripted adapter.

    The factory signature mirrors the real one ``(model, instruction,
    item_lookup) -> adapter``; the offline scripted adapter ignores the
    instruction (only the live path varies behavior on it) so the same scripted
    answers are produced under Champion and Challenger — exactly the held-constant
    discipline the loop relies on.
    """

    def factory(model, instruction, item_lookup):
        return _ScriptedAdapter(model, answers_by_item)

    return factory


class _FixedClosenessScorer:
    """A closeness scorer that returns a FIXED composite regardless of the answer.

    Lets a test pin closeness HIGH or LOW independently of the triad, so the
    "closeness is secondary and never drives the score" property (Req 2.3) and the
    over-refusal property (Req 2.5) can be exercised deterministically. It returns
    a real :class:`TurnCloseness`, preserving the ``ground_truth_kind`` the scorer
    passes through (the judge-input reconstruction keys on it).
    """

    def __init__(self, composite: float):
        self._composite = float(composite)

    def score_turn(self, *, answer_text, reference_text, ground_truth_kind, answerability):
        return TurnCloseness(
            ground_truth_kind=ground_truth_kind,
            semantic=self._composite,
            composite=self._composite,
            judge=None,
            abstention=None,
        )


class _RecordingJudgeBackend:
    """Wrap a ``JudgeBackend`` and record the fragment ids each judge call saw.

    Proves Req 13.7: the fragments threaded into the judge as grounding evidence
    are the SAME ones retrieval produced for that turn. Delegates scoring to a
    real :class:`StubJudge` so the full ``JudgeScorer`` aggregation path still runs.
    """

    def __init__(self, inner):
        self._inner = inner
        self.fragment_ids_seen: list[tuple[str, ...]] = []

    def __call__(self, req):
        self.fragment_ids_seen.append(tuple(str(f.get("id", "")) for f in req.fragments))
        return self._inner(req)


class _RecordingRetrieval:
    """Wrap a retrieval backend and record exactly what fragments it returned.

    Closes the Req 13.7 loop: we compare the ids retrieval RETURNED for a turn
    against the ids the judge SAW for that turn and against the ids recorded on the
    verdict — they must all be identical, because retrieval is the single
    held-constant substrate feeding both the model and the judge.
    """

    name = "recording-fake"

    def __init__(self, inner):
        self._inner = inner
        self.returned: dict[tuple[str, int], tuple[str, ...]] = {}

    async def retrieve(self, q):
        frags = await self._inner.retrieve(q)
        self.returned[(q.item_id, q.turn)] = tuple(str(f.get("id", "")) for f in frags)
        return frags


# ---------------------------------------------------------------------------
# Item builders
# ---------------------------------------------------------------------------
def _cohort(answerability: str = "full") -> CohortKey:
    return CohortKey(
        geography="US",
        proficiency="fluent",
        tone="neutral",
        entry_route="slack",
        momentary_state="neutral",
        answerability=answerability,
        turn_type="multi",
    )


def _gold_item(item_id: str) -> Item:
    """A turn-1 GOLD (answerable) single-turn-conversation item.

    ``answerability='full'`` + resolvable gold so ``turn_reference`` yields
    ``(GOLD, ideal)`` and ``_turn_judge_inputs`` supplies non-empty gold texts —
    the judge can then score faithfulness/correctness/completeness for real.
    """
    return Item(
        id=item_id,
        turn_type="multi",
        cohort=_cohort("full"),
        wants="how to request a corporate card",
        answerability="full",
        gold=[
            GoldFragment(
                node_id="g1",
                title="Corporate Card",
                markdown="Request a corporate card through the expense portal; it arrives within five business days.",
            )
        ],
        turns=(
            Turn(
                turn=1,
                user_utterance="How do I get a corporate card?",
                momentary_state="neutral",
                answerability="full",
            ),
        ),
    )


def _abstention_item(item_id: str) -> Item:
    """A turn-1 unanswerable item (answerability ``none``) → ABSTENTION regime.

    ``turn_reference`` returns ``(ABSTENTION, "")`` and ``_turn_judge_inputs``
    supplies the "correctly decline" ideal with answerability ``none`` — the turn
    on which a correct decline must be rewarded and answering-when-unsure penalized.
    """
    return Item(
        id=item_id,
        turn_type="multi",
        cohort=_cohort("none"),
        answerability="none",
        turns=(
            Turn(
                turn=1,
                user_utterance="Can I expense my neighbor's dental surgery?",
                momentary_state="neutral",
                answerability="none",
            ),
        ),
    )


def _two_turn_item(item_id: str) -> Item:
    """A 2-turn item: turn-1 GOLD (answerable), turn-2 WANTS (later turn)."""
    return Item(
        id=item_id,
        turn_type="multi",
        cohort=_cohort("full"),
        wants="how to request a corporate card",
        answerability="full",
        gold=[
            GoldFragment(
                node_id="g1",
                title="Corporate Card",
                markdown="Request a corporate card through the expense portal; it arrives within five business days.",
            )
        ],
        turns=(
            Turn(turn=1, user_utterance="How do I get a corporate card?", momentary_state="neutral", answerability="full"),
            Turn(turn=2, user_utterance="And how do I raise its limit?", momentary_state="neutral",
                 wants="Submit a limit-increase request to your manager for approval."),
        ),
    )


def _verdict_by_turn(score: SliceScore) -> dict[int, TurnVerdict]:
    return {v.turn: v for v in score.verdicts}


# ---------------------------------------------------------------------------
# Req 2.2 — wraps a real JudgeScorer; the triad is the decision metric
# ---------------------------------------------------------------------------
def test_judge_in_loop_wraps_real_judgescorer_and_triad_is_the_decision_metric():
    """The scorer drives the SAME ``JudgeScorer`` the study uses, and the reported
    ``triad_score`` is built from the judge's three dimensions — not closeness
    (Req 2.2).

    For a slice of single-turn answerable conversations (no abstention weighting),
    each verdict's ``overall`` equals the mean of its three judge dimensions, so the
    slice ``triad_score`` is algebraically the mean of ``per_dimension_mean`` — i.e.
    the triad IS the decision metric and it comes straight from the judge.
    """
    closeness, judge = build_offline_scorers()
    assert isinstance(judge, JudgeScorer)
    calls_before = judge.call_count

    items = [_gold_item("g-a"), _gold_item("g-b"), _gold_item("g-c")]
    answers = {it.item_id: ["Request a corporate card through the expense portal; it arrives within five business days."] for it in items}
    backend = _DuckBackend(
        answer_adapter_factory=_scripted_factory(answers),
        judge_scorer=judge,
        closeness_scorer=closeness,
        retrieval=FakeRetrievalBackend(),
    )
    scorer = JudgeInLoopScorer(backend, reps=1)
    score = _run(scorer.score_prompt(model="haiku-4.5", instruction="sys", items=items, prompt_role="champion"))

    # The real judge was actually invoked (k samples per turn).
    assert judge.call_count > calls_before
    assert isinstance(score, SliceScore)
    assert score.n_conversations == 3
    # The triad is the abstention-weighted per-conversation mean; for single-turn
    # answerable conversations that equals the mean of the three judge dimensions.
    assert score.triad_score == pytest.approx(sum(score.per_dimension_mean.values()) / 3.0)
    # Each verdict's decision value is exactly the mean of its judge dimensions.
    for v in score.verdicts:
        assert v.overall == pytest.approx(sum(v.dimensions.values()) / len(v.dimensions))


# ---------------------------------------------------------------------------
# Req 2.6 — per-dimension means are recorded
# ---------------------------------------------------------------------------
def test_per_dimension_triad_means_are_recorded():
    """The auditable per-dimension breakdown carries exactly the three judge
    dimensions, each in ``[0, 1]`` (Req 2.6)."""
    closeness, judge = build_offline_scorers()
    items = [_gold_item("g-a"), _gold_item("g-b")]
    answers = {it.item_id: ["Request a corporate card through the expense portal."] for it in items}
    backend = _DuckBackend(
        answer_adapter_factory=_scripted_factory(answers),
        judge_scorer=judge, closeness_scorer=closeness, retrieval=FakeRetrievalBackend(),
    )
    score = _run(JudgeInLoopScorer(backend, reps=1).score_prompt(
        model="haiku-4.5", instruction="sys", items=items, prompt_role="champion"))

    assert set(score.per_dimension_mean) == set(JUDGE_DIMENSIONS)
    assert set(JUDGE_DIMENSIONS) == {"faithfulness", "correctness", "completeness"}
    for dim, mean in score.per_dimension_mean.items():
        assert 0.0 <= mean <= 1.0, dim
    # Every verdict carries the same three dimensions for auditability.
    for v in score.verdicts:
        assert set(v.dimensions) == set(JUDGE_DIMENSIONS)


# ---------------------------------------------------------------------------
# Req 2.3 — closeness is a SECONDARY cross-check only; it never drives the score
# ---------------------------------------------------------------------------
def test_closeness_is_recorded_but_never_drives_the_score():
    """Closeness appears on every verdict and as ``mean_closeness``, but swapping a
    LOW closeness scorer for a HIGH one leaves the triad decision metric and every
    per-turn ``overall`` byte-identical (Req 2.3).

    Same scripted answers, same retrieval, same judge instance → identical judge
    triad. Only the (secondary) closeness scorer differs, so any difference in the
    decision metric would mean closeness leaked into it. There is none.
    """
    items = [_gold_item("g-a"), _gold_item("g-b")]
    answers = {it.item_id: ["Request a corporate card through the expense portal; it arrives within five business days."] for it in items}
    judge = JudgeScorer(backend=make_stub_judge(), k=1, disk_cache=False)
    retrieval = FakeRetrievalBackend()

    def run_with(closeness_composite):
        backend = _DuckBackend(
            answer_adapter_factory=_scripted_factory(answers),
            judge_scorer=judge,
            closeness_scorer=_FixedClosenessScorer(closeness_composite),
            retrieval=retrieval,
        )
        return _run(JudgeInLoopScorer(backend, reps=1).score_prompt(
            model="haiku-4.5", instruction="sys", items=items, prompt_role="champion"))

    low = run_with(0.05)
    high = run_with(0.95)

    # Closeness IS recorded (secondary cross-check) and tracks the scorer.
    assert low.mean_closeness == pytest.approx(0.05)
    assert high.mean_closeness == pytest.approx(0.95)
    for v in low.verdicts:
        assert v.closeness == pytest.approx(0.05)
    for v in high.verdicts:
        assert v.closeness == pytest.approx(0.95)

    # The decision metric does NOT move with closeness — it is judge-only.
    assert low.triad_score == pytest.approx(high.triad_score)
    assert low.per_dimension_mean == pytest.approx(high.per_dimension_mean)
    assert [v.overall for v in low.verdicts] == pytest.approx([v.overall for v in high.verdicts])


# ---------------------------------------------------------------------------
# Req 2.5 — over-refusal: the triad governs even when closeness is HIGH
# ---------------------------------------------------------------------------
def test_over_refusal_triad_governs_even_when_closeness_is_high():
    """On an answerable turn where the gold was retrievable but the model declined
    (over-refusal), the judge triad is the authoritative signal even though
    closeness is pinned HIGH (Req 2.5).

    The triad (an unwarranted refusal scores low) governs the decision metric; the
    HIGH closeness is recorded only as the non-deciding secondary cross-check.
    """
    item = _gold_item("g-overrefuse")
    answers = {item.item_id: [_DECLINE]}  # decline on an ANSWERABLE turn
    closeness, judge = build_offline_scorers()
    backend = _DuckBackend(
        answer_adapter_factory=_scripted_factory(answers),
        judge_scorer=judge,
        closeness_scorer=_FixedClosenessScorer(0.95),  # closeness says "great!"
        retrieval=FakeRetrievalBackend(),
    )
    score = _run(JudgeInLoopScorer(backend, reps=1).score_prompt(
        model="haiku-4.5", instruction="sys", items=[item], prompt_role="champion"))

    v = _verdict_by_turn(score)[1]
    # This is an answerable (GOLD) turn, so abstention weighting does not apply:
    # overall == the raw triad, and that triad is LOW for an unwarranted refusal.
    assert v.ground_truth_kind == GroundTruthKind.GOLD
    assert v.abstention_correct is None
    assert v.overall == pytest.approx(sum(v.dimensions.values()) / len(v.dimensions))
    assert v.overall < 0.5
    # Closeness is HIGH and recorded, but the decision metric followed the triad.
    assert v.closeness == pytest.approx(0.95)
    assert score.mean_closeness == pytest.approx(0.95)
    assert score.triad_score < 0.5
    assert score.triad_score < score.mean_closeness - 0.3


# ---------------------------------------------------------------------------
# Req 13.7 — the judge grounds on the SAME fragments the model received
# ---------------------------------------------------------------------------
def test_judge_grounds_on_the_same_fragments_retrieval_produced_for_the_turn():
    """The fragments threaded into the judge for a turn are byte-identical (by id)
    to the ones the held-constant retrieval substrate produced for that turn — the
    same fragments the model is given (Req 13.7).

    A ``FakeRetrievalBackend`` with a per-``(item, turn)`` preset makes the expected
    ids exact; a recording judge backend captures what the judge actually received;
    a recording retrieval wrapper captures what retrieval actually returned. All
    three (retrieval output, judge grounding, verdict record) must agree per turn.
    """
    item = _two_turn_item("itm-13-7")
    presets = {
        (item.item_id, 1): [
            {"id": "f1a", "text": "card via portal", "metadata": {}, "confidence": 0.9},
            {"id": "f1b", "text": "arrives in five days", "metadata": {}, "confidence": 0.5},
        ],
        (item.item_id, 2): [
            {"id": "f2a", "text": "limit increase needs manager approval", "metadata": {}, "confidence": 0.8},
        ],
    }
    retrieval = _RecordingRetrieval(FakeRetrievalBackend(fragments_by_key=presets))
    recording_judge_backend = _RecordingJudgeBackend(make_stub_judge())
    judge = JudgeScorer(backend=recording_judge_backend, k=1, disk_cache=False)
    closeness, _ = build_offline_scorers()

    answers = {item.item_id: ["Request a card via the expense portal.", "Ask your manager to approve a limit increase."]}
    backend = _DuckBackend(
        answer_adapter_factory=_scripted_factory(answers),
        judge_scorer=judge, closeness_scorer=closeness, retrieval=retrieval,
    )
    score = _run(JudgeInLoopScorer(backend, reps=1).score_prompt(
        model="haiku-4.5", instruction="sys", items=[item], prompt_role="champion"))

    expected_turn1 = ("f1a", "f1b")
    expected_turn2 = ("f2a",)

    # Retrieval produced exactly the preset fragments per turn.
    assert retrieval.returned[(item.item_id, 1)] == expected_turn1
    assert retrieval.returned[(item.item_id, 2)] == expected_turn2

    # The verdict records the SAME grounding fragment ids the model received.
    verdicts = _verdict_by_turn(score)
    assert verdicts[1].grounding_fragment_ids == expected_turn1
    assert verdicts[2].grounding_fragment_ids == expected_turn2

    # The judge actually GROUNDED on those same fragments (k=1 → one call per turn).
    assert set(recording_judge_backend.fragment_ids_seen) == {expected_turn1, expected_turn2}
    # Closed loop: what retrieval returned == what the judge saw, per turn.
    assert set(retrieval.returned.values()) == set(recording_judge_backend.fragment_ids_seen)


# ---------------------------------------------------------------------------
# Req 13.8 / 14.7 — declining on an unanswerable turn is NOT penalized
# ---------------------------------------------------------------------------
def test_correct_decline_on_unanswerable_turn_is_rewarded_not_penalized():
    """A model that correctly declines on an unanswerable/insufficient turn is
    rewarded, not penalized: abstention weighting lifts ``overall`` to at or above
    the raw triad, and the turn is flagged a correct abstention (Req 13.8, 14.7)."""
    item = _abstention_item("ab-decline")
    answers = {item.item_id: [_DECLINE]}
    closeness, judge = build_offline_scorers()
    backend = _DuckBackend(
        answer_adapter_factory=_scripted_factory(answers),
        judge_scorer=judge, closeness_scorer=closeness, retrieval=FakeRetrievalBackend(),
    )
    score = _run(JudgeInLoopScorer(backend, reps=1).score_prompt(
        model="haiku-4.5", instruction="sys", items=[item], prompt_role="champion"))

    v = _verdict_by_turn(score)[1]
    assert v.ground_truth_kind == GroundTruthKind.ABSTENTION
    assert v.abstention_correct is True
    assert v.answered_when_unsure is False
    triad_mean = sum(v.dimensions.values()) / len(v.dimensions)
    # NOT penalized: the abstention reward lifts overall to >= its own triad,
    # landing near the top of the scale rather than being docked.
    assert v.overall >= triad_mean
    assert v.overall >= 0.8


def test_answering_when_unsure_is_penalized_relative_to_declining():
    """Answering-when-unsure on the SAME unanswerable turn is strongly penalized —
    its ``overall`` falls below its own triad and far below a correct decline
    (Req 14.4, 14.7)."""
    item = _abstention_item("ab-guess")
    closeness, judge = build_offline_scorers()

    def overall_for(answer_text):
        answers = {item.item_id: [answer_text]}
        backend = _DuckBackend(
            answer_adapter_factory=_scripted_factory(answers),
            judge_scorer=judge, closeness_scorer=closeness, retrieval=FakeRetrievalBackend(),
        )
        score = _run(JudgeInLoopScorer(backend, reps=1).score_prompt(
            model="haiku-4.5", instruction="sys", items=[item], prompt_role="champion"))
        return _verdict_by_turn(score)[1]

    guessed = overall_for(_FABRICATION)
    declined = overall_for(_DECLINE)

    assert guessed.answered_when_unsure is True
    assert guessed.abstention_correct is False
    guessed_triad = sum(guessed.dimensions.values()) / len(guessed.dimensions)
    # Penalized: answering-when-unsure is docked below its own triad and far below
    # a correct decline on the identical turn.
    assert guessed.overall < guessed_triad
    assert guessed.overall < declined.overall - 0.5


# ---------------------------------------------------------------------------
# Req 14.2 — abstention-weighting summary fields are recorded
# ---------------------------------------------------------------------------
def test_abstention_weighting_summary_fields_are_recorded():
    """The slice records ``abstention_reward_mean`` and ``answered_when_unsure_rate``
    and they reflect the slice's abstention behavior (Req 14.2).

    A slice of all-correct declines reports full reward and a zero
    over-claim rate; a slice of all fabrications reports zero reward and a full
    over-claim rate.
    """
    closeness, judge = build_offline_scorers()

    def score_with(answer_text):
        items = [_abstention_item("ab-1"), _abstention_item("ab-2")]
        answers = {it.item_id: [answer_text] for it in items}
        backend = _DuckBackend(
            answer_adapter_factory=_scripted_factory(answers),
            judge_scorer=judge, closeness_scorer=closeness, retrieval=FakeRetrievalBackend(),
        )
        return _run(JudgeInLoopScorer(backend, reps=1).score_prompt(
            model="haiku-4.5", instruction="sys", items=items, prompt_role="champion"))

    declined = score_with(_DECLINE)
    assert declined.abstention_reward_mean == pytest.approx(1.0)
    assert declined.answered_when_unsure_rate == pytest.approx(0.0)

    guessed = score_with(_FABRICATION)
    assert guessed.abstention_reward_mean == pytest.approx(0.0)
    assert guessed.answered_when_unsure_rate == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Backend contract + the REFUSAL detector (supporting units)
# ---------------------------------------------------------------------------
def test_scorer_rejects_a_backend_missing_required_attributes():
    """The duck-typed contract is enforced: a backend missing any of the four
    required members raises a clear ``AttributeError`` at construction."""
    class _Incomplete:
        answer_adapter_factory = staticmethod(lambda *a, **k: None)
        judge_scorer = object()
        # missing closeness_scorer and retrieval

    with pytest.raises(AttributeError) as ei:
        JudgeInLoopScorer(_Incomplete(), reps=1)
    msg = str(ei.value)
    assert "closeness_scorer" in msg and "retrieval" in msg


def test_refusal_detector_matches_declines_not_fabrications():
    """The module-level ``REFUSAL`` detector keys on explicit decline phrasing and
    does not misread a confident fabrication as an abstention."""
    assert REFUSAL.search(_DECLINE)
    assert not REFUSAL.search(_FABRICATION)
    assert not REFUSAL.search("Request a corporate card through the expense portal.")


# ===========================================================================
# Task 10.2 — OfflineAuthorClient unit / example tests
# (design "Component 5: AuthorClient", "Author prompt design"; Req 1.4, 3.1,
#  3.2, 3.3, 13.6, 14.6, 15.1)
# ===========================================================================
#
# These tests pin the AUTHOR seam of the loop with ZERO network. They drive the
# deterministic ``OfflineAuthorClient`` (the offline counterpart of the live
# Bedrock author, built on the SAME ``build_author_prompt`` contract) and the
# contract builder directly. The load-bearing properties (design Component 5):
#
#   * the Author is handed the Champion + the selected failures *with their judge
#     evidence* — all of it lands in the recorded contract (Req 1.4 / 3.1);
#   * a change rationale is always produced (Req 3.3);
#   * the Author AUTHORS new standalone instruction text — the result is never a
#     pick from the fixed five-variant menu (Req 3.2);
#   * the repo-baked PROMPTING_GUIDANCE rides EVERY invocation's contract
#     (Req 15.1);
#   * the contract steers fragments-only grounding (Req 13.6) and explicit,
#     reliable abstention (Req 14.6); and
#   * when the failures show the model answered when it should have abstained, the
#     grounding/abstention lever is prioritized over the general next lever
#     (Req 14.6).

from bakeoff.quality.optimizer.author import (
    AuthoredChallenger,
    OfflineAuthorClient,
    build_author_prompt,
)
from bakeoff.quality.prompts import (
    MULTI_TURN_BLOCKS,
    quality_system_instruction,
    variants_for_model,
)


# The two grounding/abstention levers the offline author surfaces first on an
# answering-when-unsure failure mix (mirrors author._GROUNDING_ABSTENTION_LEVERS;
# kept local so the test documents the contract rather than coupling to a private
# name). Both carry a marker the offline adapter reads as "decline correctly".
_GROUNDING_ABSTENTION_LEVERS = ("reground", "answerability_persist")


def _author_failure(
    *,
    item_id: str = "c0-s07",
    turn: int = 2,
    answered_when_unsure: bool = False,
    ground_truth_kind=GroundTruthKind.ABSTENTION,
    evidence=None,
    answer_excerpt: str = "UNIQUE_ANSWER_EXCERPT_42",
    grounding_fragment_ids=("frag-aaa", "frag-bbb"),
) -> TurnVerdict:
    """Build a driving :class:`TurnVerdict` failure with controllable abstention flag.

    Carries distinctive ``evidence`` / ``answer_excerpt`` / ``grounding_fragment_ids``
    so a test can prove the Author contract actually embeds the selected failures and
    their judge evidence (Req 1.4 / 3.1).
    """
    return TurnVerdict(
        item_id=item_id,
        rep=0,
        turn=turn,
        ground_truth_kind=ground_truth_kind,
        overall=0.18,
        dimensions={"faithfulness": 0.20, "correctness": 0.30, "completeness": 0.10},
        abstention_correct=(False if answered_when_unsure else None),
        answered_when_unsure=answered_when_unsure,
        fragments_sufficient=False,
        grounding_fragment_ids=tuple(grounding_fragment_ids),
        evidence=evidence if evidence is not None else {"grounding_span": "FRAGMENT-XYZ-EVIDENCE"},
        answer_excerpt=answer_excerpt,
        closeness=0.42,
    )


def _menu_instructions() -> set[str]:
    """Every instruction reachable from the fixed five-variant menu (Req 3.2).

    The Author must AUTHOR new text, not select one of these. We gather both the
    fully-assembled per-model instructions and the raw multi-turn blocks so the
    "not a menu pick" assertion is honest against either granularity.
    """
    menu: set[str] = set()
    for model_key, spec in config.QUALITY_MODELS.items():
        for variant in variants_for_model(model_key):
            menu.add(
                quality_system_instruction(
                    family=str(spec["family"]),
                    thinking_enabled=bool(spec["thinking"]),
                    variant=variant,
                )
            )
            if variant.multi_turn_block:
                menu.add(variant.multi_turn_block)
    return menu


# --- Req 1.4 / 3.1 — the Author receives the Champion + the selected failures --
def test_offline_author_contract_embeds_champion_and_selected_failures_with_evidence():
    """The recorded contract carries the verbatim Champion AND every selected
    failure's judge evidence — per-dimension scores, quoted evidence, the answer
    excerpt and the grounding fragment ids (Req 1.4 / 3.1)."""
    champion = "You are an FAQ assistant. Answer the user's question."
    failures = [
        _author_failure(
            item_id="c0-s07",
            turn=2,
            answered_when_unsure=True,
            evidence={"grounding_span": "FRAGMENT-XYZ-EVIDENCE", "note": "answered beyond fragments"},
            answer_excerpt="UNIQUE_ANSWER_EXCERPT_42",
            grounding_fragment_ids=("frag-aaa", "frag-bbb"),
        )
    ]
    author = OfflineAuthorClient(author_model="offline-author")
    result = _run(
        author.author(target_model="sonnet-4.6", champion_instruction=champion, failures=failures)
    )

    contract = result.raw["prompt"]
    # The verbatim Champion is in the contract (Req 3.1).
    assert champion in contract
    # The selected failure and its judge evidence are rendered as data (Req 1.4 / 3.1).
    assert "FRAGMENT-XYZ-EVIDENCE" in contract
    assert "UNIQUE_ANSWER_EXCERPT_42" in contract
    assert "frag-aaa" in contract and "frag-bbb" in contract
    assert "item=c0-s07" in contract and "turn=2" in contract
    # The per-dimension judge scores ride along too (auditable failure data).
    assert "faithfulness=0.20" in contract
    # The recorded invocation shape reflects what the Author was handed.
    assert result.raw["n_failures"] == 1
    assert result.raw["answered_when_unsure"] is True


# --- Req 3.3 — a rationale is always present -------------------------------
def test_offline_author_produces_a_nonempty_rationale():
    """Every authoring call returns a non-empty change rationale (Req 3.3)."""
    champion = "You are an FAQ assistant."
    result = _run(
        OfflineAuthorClient().author(
            target_model="haiku-4.5",
            champion_instruction=champion,
            failures=[_author_failure(answered_when_unsure=False)],
        )
    )
    assert isinstance(result.rationale, str)
    assert result.rationale.strip()


# --- Req 3.2 — AUTHORED text, never a menu pick ----------------------------
def test_offline_author_authors_new_text_not_a_menu_pick():
    """The rewrite is newly authored standalone instruction text built FROM the
    Champion, not equal to any member of the fixed variant menu (Req 3.2)."""
    champion = "You are an FAQ assistant. Answer strictly and helpfully."
    result = _run(
        OfflineAuthorClient().author(
            target_model="sonnet-4.6",
            champion_instruction=champion,
            failures=[_author_failure(answered_when_unsure=True)],
        )
    )

    # It authored a complete standalone instruction that extends the Champion text.
    assert result.usable is True
    assert result.instruction != champion
    assert result.instruction.startswith(champion)
    assert len(result.instruction) > len(champion)

    # And it is NOT a selection from the fixed five-variant menu (Req 3.2).
    menu = _menu_instructions()
    assert result.instruction not in menu
    for variant in variants_for_model("sonnet-4.6-thinking-off"):
        assert result.instruction != variant.multi_turn_block


# --- Req 15.1 — PROMPTING_GUIDANCE on EVERY invocation ---------------------
def test_prompting_guidance_rides_every_offline_author_invocation():
    """The repo-baked PROMPTING_GUIDANCE is embedded in the contract on every call
    — including the terminal no-op call where every lever is already present
    (Req 15.1)."""
    author = OfflineAuthorClient()

    # A normal authoring call.
    r1 = _run(
        author.author(
            target_model="sonnet-4.6",
            champion_instruction="You are an FAQ assistant.",
            failures=[_author_failure(answered_when_unsure=True)],
        )
    )
    assert pg.PROMPTING_GUIDANCE in r1.raw["prompt"]
    assert "<prompting_guidance>" in r1.raw["prompt"]

    # The terminal call: a champion that already carries every lever marker still
    # builds the same guidance-bearing contract (the guidance is unconditional).
    full_champion = "You are an FAQ assistant.\n\n" + "\n\n".join(MULTI_TURN_BLOCKS.values())
    r2 = _run(
        author.author(
            target_model="sonnet-4.6",
            champion_instruction=full_champion,
            failures=[_author_failure(answered_when_unsure=False)],
        )
    )
    assert pg.PROMPTING_GUIDANCE in r2.raw["prompt"]
    # Sanity: this WAS the non-improving terminal iteration (no lever left to add).
    assert r2.raw["added_lever"] is None
    assert r2.usable is False


def test_build_author_prompt_directly_carries_guidance_and_steering():
    """The contract builder itself embeds PROMPTING_GUIDANCE (Req 15.1) and steers
    fragments-only grounding (Req 13.6) and explicit abstention (Req 14.6)."""
    contract = build_author_prompt(
        target_model="sonnet-4.6",
        champion_instruction="You are an FAQ assistant.",
        failures=[_author_failure(answered_when_unsure=True)],
    )
    # Req 15.1 — the modern prompting guidance is part of the contract.
    assert pg.PROMPTING_GUIDANCE in contract

    # Req 13.6 — fragments-only grounding is steered explicitly.
    low = contract.lower()
    assert "strictly from the retrieved fragments" in low
    assert "outside or training knowledge" in low or "training knowledge" in low

    # Req 14.6 — explicit, reliable abstention is steered, and a correct decline is
    # framed as a good outcome.
    assert "abstain" in low or "decline" in low
    assert "insufficient" in low


# --- Req 14.6 — grounding/abstention lever is prioritized on over-answering --
def test_answering_when_unsure_prioritizes_the_grounding_abstention_lever():
    """When the failures show the model answered when it should have abstained, the
    offline author surfaces a grounding/abstention lever ahead of the general next
    lever (Req 14.6).

    The Champion already carries the ``reground`` marker, so the two orders diverge:
      * answered-when-unsure → the grounding/abstention order picks ``answerability_persist``;
      * otherwise → the general order picks ``conversation_aware``.
    """
    # Champion already has the reground lever, so it is skipped in both orders.
    champion = "You are an FAQ assistant.\n\n" + MULTI_TURN_BLOCKS["reground"]
    author = OfflineAuthorClient()

    unsure = _run(
        author.author(
            target_model="sonnet-4.6",
            champion_instruction=champion,
            failures=[_author_failure(answered_when_unsure=True)],
        )
    )
    not_unsure = _run(
        author.author(
            target_model="sonnet-4.6",
            champion_instruction=champion,
            failures=[_author_failure(answered_when_unsure=False)],
        )
    )

    # The over-answering mix prioritizes a grounding/abstention lever...
    assert unsure.raw["added_lever"] == "answerability_persist"
    assert unsure.raw["added_lever"] in _GROUNDING_ABSTENTION_LEVERS
    # ...which is a different (de-prioritized) choice than the general order makes.
    assert not_unsure.raw["added_lever"] == "conversation_aware"
    assert unsure.raw["added_lever"] != not_unsure.raw["added_lever"]

    # The rewrite added that lever's actual block, and the rationale ties the change
    # to the grounding/abstention behavior.
    assert MULTI_TURN_BLOCKS["answerability_persist"] in unsure.instruction
    assert any(w in unsure.rationale.lower() for w in ("abstain", "decline", "grounding"))


def test_offline_author_build_marks_champion_echo_unusable():
    """``AuthoredChallenger.build`` flags an empty/Champion-identical rewrite as
    ``usable=False`` so the loop counts it as a non-improving iteration (Req 3.5).

    (Supporting unit for the AUTHORED-text contract: a usable Challenger must be new
    text, which is the same rule the menu-pick test exercises from the other side.)
    """
    champion = "You are an FAQ assistant."
    echo = AuthoredChallenger.build(
        instruction=champion, rationale="r", author_model="m", raw={}, champion_instruction=champion
    )
    empty = AuthoredChallenger.build(
        instruction="   ", rationale="r", author_model="m", raw={}, champion_instruction=champion
    )
    new = AuthoredChallenger.build(
        instruction=champion + "\n\nmore", rationale="r", author_model="m", raw={}, champion_instruction=champion
    )
    assert echo.usable is False
    assert empty.usable is False
    assert new.usable is True


# ===========================================================================
# Task 14.2 — PhaseBValidator unit / example tests
# (design Component 8 "PhaseBValidator" / "Two-phase train/test"; Req 7.4, 7.5,
#  7.7)
# ===========================================================================
#
# Phase B takes a converged Champion and scores it ONCE on the reserved
# Validation_Set to produce the final reported number. ZERO network: the duck-typed
# offline backend (``build_offline_backend``) supplies the answer adapter / judge /
# closeness / retrieval seam, and the item builders from task 7.2 supply the slice.
# The load-bearing properties (design Component 8):
#
#   * Phase B reps are strictly greater than Phase A reps, and a CI rides the
#     ``PhaseBResult`` (Req 7.4);
#   * the returned triad is the Phase-B-rep value on the Validation_Set — i.e. the
#     final reported number (Req 7.5); and
#   * the Author is NEVER invoked in Phase B (Req 7.7): we wire a backend whose
#     ``author`` raises if touched and confirm ``validate()`` still completes.

from bakeoff.quality.optimizer.backends import build_offline_backend
from bakeoff.quality.optimizer.validate import PhaseBResult, PhaseBValidator


class _ExplodingAuthor:
    """An author double that fails the test loudly if it is ever invoked (Req 7.7)."""

    author_model = "should-never-run"

    async def author(self, *, target_model, champion_instruction, failures, stream=None):
        raise AssertionError("The Author must NEVER be invoked during Phase B (Req 7.7)")


class _PhaseBBackend:
    """A duck-typed Phase B backend: the offline seam + an exploding ``author``.

    Carries exactly the four members :class:`JudgeInLoopScorer` reads
    (``answer_adapter_factory`` / ``judge_scorer`` / ``closeness_scorer`` /
    ``retrieval``) plus a ``name`` and an ``author`` that raises on use, so a test
    can prove Phase B completes without ever touching the Author (Req 7.7).
    """

    def __init__(self, inner, author):
        self.name = inner.name
        self.answer_adapter_factory = inner.answer_adapter_factory
        self.judge_scorer = inner.judge_scorer
        self.closeness_scorer = inner.closeness_scorer
        self.retrieval = inner.retrieval
        self.author = author


def _phase_b_backend() -> _PhaseBBackend:
    """The offline bundle wrapped so any Author access during Phase B explodes."""
    return _PhaseBBackend(build_offline_backend(), _ExplodingAuthor())


def _validation_slice():
    """A small reserved Validation_Set spanning answerable, abstention and 2-turn."""
    return [
        _gold_item("vs-1"),
        _gold_item("vs-2"),
        _abstention_item("vs-3"),
        _two_turn_item("vs-4"),
    ]


# --- Req 7.4 — Phase B reps > Phase A reps, and a CI is present ------------
def test_phase_b_reps_exceed_phase_a_and_result_carries_a_ci():
    """Phase B validates at strictly more reps than Phase A iterates, and the
    ``PhaseBResult`` carries a well-formed 95% CI around the reported triad
    (Req 7.4)."""
    # The config-level invariant the design asserts at import time.
    assert config.QUALITY_OPT_PHASE_B_REPS > config.QUALITY_OPT_PHASE_A_REPS

    items = _validation_slice()
    validator = PhaseBValidator(_phase_b_backend())
    result = _run(
        validator.validate(model="haiku-4.5", champion_instruction="You are an FAQ assistant.", validation_items=items)
    )

    assert isinstance(result, PhaseBResult)
    # The reps actually used default to the Phase B count and exceed Phase A.
    assert result.reps == config.QUALITY_OPT_PHASE_B_REPS
    assert result.reps > config.QUALITY_OPT_PHASE_A_REPS
    # One conversation per (item, rep) — proves Phase B really ran at Phase B reps.
    assert result.n_conversations == len(items) * config.QUALITY_OPT_PHASE_B_REPS

    # A well-formed CI rides the result (Req 7.4).
    assert 0.0 <= result.triad_score <= 1.0
    assert result.ci_half_width >= 0.0
    assert result.ci_low <= result.triad_score <= result.ci_high
    assert result.ci_high - result.ci_low == pytest.approx(2.0 * result.ci_half_width)
    # The backend identity that produced the number is recorded (Req 10.6).
    assert result.backend == "offline"


# --- Req 7.5 — the returned triad IS the final reported (Phase B) value ----
def test_phase_b_reports_the_phase_b_value_on_the_validation_set():
    """The number Phase B returns is exactly the Champion's triad scored at the
    Phase B rep count on the Validation_Set — the final reported value (Req 7.5).

    We recompute the slice score directly with :class:`JudgeInLoopScorer` at the
    Phase B reps over the same backend + items and confirm the validator surfaced
    that very number (triad + CI), not the Phase A in-loop signal.
    """
    backend = _phase_b_backend()
    items = _validation_slice()
    champion = "You are an FAQ assistant."

    result = _run(
        PhaseBValidator(backend).validate(model="haiku-4.5", champion_instruction=champion, validation_items=items)
    )

    # Independent recompute at the Phase B rep count, champion role, same backend.
    direct = _run(
        JudgeInLoopScorer(backend, reps=config.QUALITY_OPT_PHASE_B_REPS).score_prompt(
            model="haiku-4.5", instruction=champion, items=items, prompt_role="champion"
        )
    )
    assert result.triad_score == pytest.approx(direct.triad_score)
    assert result.ci_half_width == pytest.approx(direct.ci_half_width)
    assert result.ci_low == pytest.approx(direct.ci_low)
    assert result.ci_high == pytest.approx(direct.ci_high)
    assert result.n_conversations == direct.n_conversations
    # The validator echoes back the Champion it was asked to validate.
    assert result.champion_instruction == champion


# --- Req 7.7 — the Author is NEVER invoked in Phase B ----------------------
def test_phase_b_never_invokes_the_author():
    """Phase B is pure scoring: ``validate()`` completes without ever touching the
    backend's Author, even though that Author would raise on use (Req 7.7)."""
    backend = _phase_b_backend()

    # Pre-flight: the wired author really does explode when called, so the test is
    # not vacuous — a Phase B that touched it would surface this AssertionError.
    with pytest.raises(AssertionError):
        _run(backend.author.author(target_model="haiku-4.5", champion_instruction="x", failures=[]))

    # validate() nonetheless completes and returns a final result (Author untouched).
    result = _run(
        PhaseBValidator(backend).validate(
            model="haiku-4.5",
            champion_instruction="You are an FAQ assistant.",
            validation_items=_validation_slice(),
        )
    )
    assert isinstance(result, PhaseBResult)
    assert result.n_conversations > 0


# ===========================================================================
# Task 15.2 — OptimizerEventEmitter unit / example tests
# (design Component 11 "OptimizerEventEmitter" / Property 19; Req 9.1, 9.7)
# ===========================================================================
#
# The emitter is the single seam the optimizer streams its live view through. It
# rides the EXISTING ``bakeoff.app.SSEBroker`` UNCHANGED (Req 9.7) and stamps every
# payload with ``model_channel`` so a Per_Model_View can recover only its own
# model's events (Req 9.1 / Property 19). We subscribe to a real broker and confirm
# receipt; ZERO network. The load-bearing properties:
#
#   * every emitted payload carries ``model_channel`` equal to the model it
#     describes, across every documented event type;
#   * the events ride the existing broker (a real ``SSEBroker`` subscriber receives
#     them, in order, exactly once);
#   * ``optimizer_champion_scored`` carries the abstention/retrieval fields
#     (``abstention_reward_mean`` / ``answered_when_unsure_rate`` /
#     ``retrieval_backend``); and
#   * the emitter never mutates the caller's payload dict.

from bakeoff.app import SSEBroker
from bakeoff.quality.optimizer.events import (
    EVENT_AUTHOR_TOKEN,
    EVENT_CHAMPION_SCORED,
    EVENT_CONVERGED,
    EVENT_ITERATION_COMPLETED,
    EVENT_PHASE_B,
    MODEL_CHANNEL,
    OPTIMIZER_EVENT_TYPES,
    OptimizerEventEmitter,
)


def _drain(sub) -> list[tuple[str, dict]]:
    """Drain a Subscription's queue synchronously (mirrors test_app.py)."""
    out: list[tuple[str, dict]] = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


def _emit_one_of_each(emitter: OptimizerEventEmitter, model: str) -> None:
    """Emit one of every documented optimizer event type for ``model``."""
    emitter.champion_scored(
        model=model, phase="A", iteration_index=1, role="champion",
        triad=0.62, ci_half_width=0.04, ci_low=0.58, ci_high=0.66,
        per_dimension={"faithfulness": 0.6, "correctness": 0.65, "completeness": 0.61},
        abstention_reward_mean=0.75, answered_when_unsure_rate=0.10,
        retrieval_backend="fake", mean_closeness=0.5, n_conversations=12,
    )
    emitter.author_token(model=model, iteration_index=1, delta="rewriting...")
    emitter.iteration_completed(
        model=model, iteration_index=1, challenger_triad=0.70, challenger_ci_half_width=0.03,
        gain_absolute=0.08, gain_percent=12.9, accepted=True, consecutive_non_improving=0,
        champion_instruction="new champion text", prompt_diff="@@ -1 +1 @@", lookback_version_ids=["v1", "v2"],
    )
    emitter.converged(model=model, converged_iteration=4, stop_reason="no_improvement")
    emitter.phase_b(model=model, triad=0.71, ci_half_width=0.025, n_conversations=40)


# --- Req 9.1 / Property 19 — model_channel stamped on every payload ---------
def test_emitter_stamps_model_channel_on_every_event_and_rides_the_broker():
    """Every documented event the emitter publishes rides the EXISTING broker and is
    stamped with ``model_channel`` equal to the model it describes (Req 9.1 / 9.7)."""
    broker = SSEBroker()
    sub = broker.open()
    emitter = OptimizerEventEmitter(broker)

    _emit_one_of_each(emitter, "haiku-4.5")
    received = _drain(sub)

    # Rides the existing broker: one frame per emit, exactly once, in order.
    assert len(received) == 5
    etypes = [etype for etype, _ in received]
    assert etypes == [
        EVENT_CHAMPION_SCORED,
        EVENT_AUTHOR_TOKEN,
        EVENT_ITERATION_COMPLETED,
        EVENT_CONVERGED,
        EVENT_PHASE_B,
    ]
    # Every event type is a documented optimizer type, and every payload is stamped.
    for etype, payload in received:
        assert etype in OPTIMIZER_EVENT_TYPES
        assert payload[MODEL_CHANNEL] == "haiku-4.5"


# --- Req 9.1 — champion_scored carries the abstention/retrieval fields ------
def test_champion_scored_event_carries_abstention_and_retrieval_fields():
    """``optimizer_champion_scored`` carries the abstention-behavior summary and the
    held-constant retrieval backend name alongside the triad + CI + per-dimension
    breakdown (Req 9.1; the design payload shape)."""
    broker = SSEBroker()
    sub = broker.open()
    emitter = OptimizerEventEmitter(broker)

    emitter.champion_scored(
        model="sonnet-4.6", phase="A", iteration_index=2, role="challenger",
        triad=0.62, ci_half_width=0.04, ci_low=0.58, ci_high=0.66,
        per_dimension={"faithfulness": 0.6, "correctness": 0.65, "completeness": 0.61},
        abstention_reward_mean=0.80, answered_when_unsure_rate=0.15,
        retrieval_backend="opensearch", mean_closeness=0.5, n_conversations=12,
    )
    (etype, payload) = _drain(sub)[0]

    assert etype == EVENT_CHAMPION_SCORED
    # The abstention/retrieval fields (Req 14.2 / 16.1) ride the event verbatim.
    assert payload["abstention_reward_mean"] == pytest.approx(0.80)
    assert payload["answered_when_unsure_rate"] == pytest.approx(0.15)
    assert payload["retrieval_backend"] == "opensearch"
    # ...together with the triad, its CI, the per-dimension breakdown, and the stamp.
    assert payload["triad"] == pytest.approx(0.62)
    assert payload["ci_half_width"] == pytest.approx(0.04)
    assert set(payload["per_dimension"]) == set(JUDGE_DIMENSIONS)
    assert payload["mean_closeness"] == pytest.approx(0.5)
    assert payload["model"] == "sonnet-4.6"
    assert payload[MODEL_CHANNEL] == "sonnet-4.6"


# --- Property 19 — two models on one broker partition cleanly by channel ----
def test_two_models_on_one_broker_partition_cleanly_by_model_channel():
    """Two models publishing onto the SAME broker interleave on the wire, but
    filtering by ``model_channel`` recovers each model's events with none
    misattributed (Req 9.1; design Property 19)."""
    broker = SSEBroker()
    sub = broker.open()
    emitter = OptimizerEventEmitter(broker)

    # Interleave emissions from the two Target_Models.
    emitter.converged(model="haiku-4.5", converged_iteration=3, stop_reason="ci_overlap")
    emitter.converged(model="sonnet-4.6", converged_iteration=5, stop_reason="no_improvement")
    emitter.author_token(model="haiku-4.5", iteration_index=1, delta="h")
    emitter.phase_b(model="sonnet-4.6", triad=0.7, ci_half_width=0.02, n_conversations=40)

    received = _drain(sub)
    haiku = [p for _, p in received if p[MODEL_CHANNEL] == "haiku-4.5"]
    sonnet = [p for _, p in received if p[MODEL_CHANNEL] == "sonnet-4.6"]

    # Clean partition: 2 each, none misattributed, total accounted for.
    assert len(haiku) == 2 and len(sonnet) == 2
    assert len(haiku) + len(sonnet) == len(received)
    assert all(p[MODEL_CHANNEL] == "haiku-4.5" for p in haiku)
    assert all(p[MODEL_CHANNEL] == "sonnet-4.6" for p in sonnet)


# --- Non-mutation — the caller's payload dict is never stamped in place -----
def test_emit_does_not_mutate_the_callers_payload():
    """``emit`` stamps ``model_channel`` on a shallow COPY; the caller's own dict is
    untouched, and the published copy carries the stamp (Req 9.7 non-mutation)."""
    broker = SSEBroker()
    sub = broker.open()
    emitter = OptimizerEventEmitter(broker)

    payload = {"triad": 0.5}
    emitter.emit(EVENT_PHASE_B, "haiku-4.5", payload)

    # The caller's dict is not mutated...
    assert MODEL_CHANNEL not in payload
    assert payload == {"triad": 0.5}
    # ...but the delivered copy is stamped.
    (_etype, delivered) = _drain(sub)[0]
    assert delivered[MODEL_CHANNEL] == "haiku-4.5"
    assert delivered["triad"] == pytest.approx(0.5)


def test_emitter_uses_existing_broker_unchanged_via_duck_typed_publish():
    """The emitter rides ANY object exposing the broker's ``publish(event_type,
    payload)`` contract unchanged — it neither subclasses nor reconfigures the
    broker (Req 9.7)."""
    class _RecordingBroker:
        def __init__(self):
            self.published: list[tuple[str, dict]] = []

        def publish(self, event_type, payload):
            self.published.append((event_type, payload))

    rec = _RecordingBroker()
    OptimizerEventEmitter(rec).converged(model="haiku-4.5", converged_iteration=2, stop_reason="done")

    assert len(rec.published) == 1
    (etype, payload) = rec.published[0]
    assert etype == EVENT_CONVERGED
    assert payload[MODEL_CHANNEL] == "haiku-4.5"
    assert payload["converged_iteration"] == 2 and payload["stop_reason"] == "done"


# ===========================================================================
# Task 10.6 — BedrockAuthorClient unit / example tests (live author, FAKE client)
# (design "Component 5: AuthorClient", "Author prompt design", the live
#  Converse-streaming + resilience author; Req 3.3, 4.4, 9.3, 15.1)
# ===========================================================================
#
# These tests pin the LIVE author seam with ZERO network. They drive the real
# ``BedrockAuthorClient`` against a FAKE ``bedrock-runtime`` client whose
# ``converse_stream`` yields the exact ``contentBlockDelta`` event shape the
# adapter's ``_invoke_stream_sync`` consumes — injected via ``client`` /
# ``client_factory`` — and an instant async ``sleep`` so the resilience backoff
# never waits. The load-bearing behaviors (design Component 5, mirroring the live
# judge / inline adapter):
#
#   * the streaming token callback fires — each visible-answer delta is forwarded
#     to ``stream`` as it arrives, and the deltas accumulate into the parsed
#     response text (Req 9.3);
#   * the strict-JSON ``{"instruction", "rationale"}`` contract is parsed, tolerating
#     a fenced ```json block and surrounding prose, exactly like the judge parser
#     (Req 3.3);
#   * the credential-expiry resilience path works — a first invoke raising an
#     auth-expired error rebuilds the boto3 client via the injected factory and the
#     retry succeeds, incrementing ``refresh_count`` (Req 4.4, cross-cutting
#     resilience); and
#   * the repo-baked PROMPTING_GUIDANCE rides the contract on EVERY invocation
#     (it appears in ``raw["prompt"]`` each call — Req 15.1).

from bakeoff.quality.optimizer.author import BedrockAuthorClient, _default_author_model


async def _instant_author_sleep(_delay: float) -> None:
    """An async sleep that returns immediately so the resilience backoff never waits."""
    return None


class _FakeBedrockClientError(Exception):
    """Mimics a botocore ``ClientError``: carries a ``.response`` dict with an error code.

    Mirrors the ``FakeClientError`` shape in ``test_resilience.py`` so the shared
    :func:`bakeoff.resilience.classify_error` classifies an injected failure WITHOUT
    importing botocore — ``ExpiredTokenException`` lands in
    ``config.AUTH_EXPIRED_ERROR_CODES`` and is read off ``.response["Error"]["Code"]``.
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.response = {
            "Error": {"Code": code, "Message": message or code},
            "ResponseMetadata": {},
        }
        super().__init__(message or code)


class _FakeAuthorBedrockClient:
    """A network-free ``bedrock-runtime`` stand-in whose ``converse_stream`` yields deltas.

    Yields the exact event shape ``BedrockAuthorClient._invoke_stream_sync`` consumes
    (``_author_delta_text`` reads ``event["contentBlockDelta"]["delta"]["text"]``) and
    returns the boto3 ``{"stream": <iterable>}`` envelope the adapter tolerates. The
    response text the contract parser sees is exactly ``"".join(deltas)``; ``captured``
    records the last request kwargs and ``calls`` counts invocations, so a test can assert
    the request shaping (model id, omitted temperature) and that a real Converse call was
    made per ``author()``.
    """

    def __init__(self, *, deltas, captured=None) -> None:
        self._deltas = list(deltas)
        self.captured = captured if captured is not None else {}
        self.calls = 0

    @property
    def output_text(self) -> str:
        return "".join(self._deltas)

    def converse_stream(self, **kwargs):
        self.calls += 1
        self.captured.clear()
        self.captured.update(kwargs)
        events = [{"contentBlockDelta": {"delta": {"text": d}}} for d in self._deltas]
        # boto3 returns {"stream": <EventStream>}; the adapter tolerates this envelope.
        return {"stream": iter(events)}


class _AuthExpiredOnceClient:
    """A fake client whose ``converse_stream`` always raises an auth-expired error.

    Used as the FIRST client a refresh-path factory hands out, so the initial invoke
    fails with a credential-expiry signature and the resilience helper rebuilds the
    client via the factory before retrying against a healthy client.
    """

    def converse_stream(self, **kwargs):
        raise _FakeBedrockClientError(
            "ExpiredTokenException",
            "The security token included in the request is expired",
        )


# --- Req 9.3 — the streaming token callback fires for each delta -----------
def test_bedrock_author_streams_each_delta_to_the_callback():
    """Each visible-answer delta is forwarded to the ``stream`` callback as it arrives,
    in order, and the accumulated deltas are what the contract parser decodes (Req 9.3).

    The fake ``converse_stream`` splits the strict-JSON output across several
    ``contentBlockDelta`` events; the callback must see exactly those chunks, their
    concatenation must equal the model's full output, and the parsed Challenger must come
    from that streamed text. The request also omits ``temperature`` (Sonnet 4.6 deprecated
    it) and targets the configured author model id.
    """
    deltas = [
        '{"instruction": "You are an ',
        "improved FAQ assistant who ",
        'grounds strictly in fragments.", ',
        '"rationale": "addresses the over-answering failure"}',
    ]
    captured: dict = {}
    client = _FakeAuthorBedrockClient(deltas=deltas, captured=captured)
    author = BedrockAuthorClient("author-model-x", client=client, sleep=_instant_author_sleep)

    streamed: list[str] = []
    result = _run(
        author.author(
            target_model="sonnet-4.6",
            champion_instruction="You are an FAQ assistant.",
            failures=[_author_failure()],
            stream=streamed.append,
        )
    )

    # Each delta was forwarded once, in order, and the callback fired more than once.
    assert streamed == deltas
    assert len(streamed) >= 2
    assert "".join(streamed) == client.output_text

    # The Challenger was parsed from the streamed JSON.
    assert result.instruction == "You are an improved FAQ assistant who grounds strictly in fragments."
    assert result.rationale == "addresses the over-answering failure"
    assert result.usable is True
    assert result.raw["raw_output"] == client.output_text

    # A real Converse-stream call was issued, against the configured author model, with
    # temperature OMITTED (Sonnet 4.6 deprecated it; accepts_temperature defaults False).
    assert client.calls == 1
    assert captured["modelId"] == "author-model-x"
    assert "temperature" not in captured["inferenceConfig"]
    assert captured["inferenceConfig"]["maxTokens"] == author.max_tokens
    # The contract is delivered as the user message text.
    assert captured["messages"][0]["content"][0]["text"] == result.raw["prompt"]


# --- Req 3.3 — strict JSON parsed, tolerating a fenced block + surrounding prose --
def test_bedrock_author_parses_fenced_json_with_surrounding_prose():
    """The strict-JSON ``{"instruction", "rationale"}`` contract is parsed even when the
    model wraps it in a ```json fence and surrounds it with prose (Req 3.3).

    Mirrors the judge's tolerant parser: the first ``{...}`` object is extracted and
    decoded, so the rewritten instruction + rationale survive the chrome a chatty model
    adds around the JSON.
    """
    instruction = (
        "You are a grounded FAQ assistant. Answer strictly from the retrieved fragments "
        "and decline plainly when they are insufficient."
    )
    rationale = "Adds explicit fragments-only grounding and reliable abstention to fix the failures."
    payload = {"instruction": instruction, "rationale": rationale}
    output = (
        "Here is my rewrite of the instruction:\n\n"
        "```json\n" + json.dumps(payload, indent=2) + "\n```\n\n"
        "Let me know if you'd like any further changes."
    )
    client = _FakeAuthorBedrockClient(deltas=[output])
    author = BedrockAuthorClient("author-model-x", client=client, sleep=_instant_author_sleep)

    result = _run(
        author.author(
            target_model="haiku-4.5",
            champion_instruction="You are an FAQ assistant.",
            failures=[_author_failure(answered_when_unsure=True)],
        )
    )

    assert result.instruction == instruction
    assert result.rationale == rationale
    assert result.usable is True
    # The raw output (with the prose + fence) is recorded for audit, parsed result aside.
    assert result.raw["raw_output"] == output
    assert result.raw["response"] == {"instruction": instruction, "rationale": rationale}


# --- Req 4.4 / resilience — auth-expiry rebuilds the client and the retry succeeds --
def test_bedrock_author_refreshes_credentials_and_retries_on_auth_expiry():
    """A first invoke that fails with an auth-expired error triggers a client rebuild via
    the injected factory, and the retry against a healthy client succeeds — incrementing
    ``refresh_count`` exactly once (Req 4.4, cross-cutting credential-expiry resilience).

    No real STS/boto3: the factory hands out an expired-credentials client first and a
    healthy fake second, and the instant async ``sleep`` keeps the backoff from waiting.
    """
    deltas = [
        '{"instruction": "Recovered improved instruction.", ',
        '"rationale": "authored after a credential refresh"}',
    ]
    good_client = _FakeAuthorBedrockClient(deltas=deltas)

    class _ExpiredThenGoodFactory:
        def __init__(self) -> None:
            self.builds = 0

        def __call__(self):
            self.builds += 1
            if self.builds == 1:
                return _AuthExpiredOnceClient()
            return good_client

    factory = _ExpiredThenGoodFactory()
    author = BedrockAuthorClient(
        "author-model-x", client_factory=factory, sleep=_instant_author_sleep
    )
    assert author.refresh_count == 0

    result = _run(
        author.author(
            target_model="sonnet-4.6",
            champion_instruction="You are an FAQ assistant.",
            failures=[_author_failure()],
        )
    )

    # Exactly one credential refresh happened, and the client was rebuilt via the factory
    # (initial expired client + the rebuilt healthy one).
    assert author.refresh_count == 1
    assert factory.builds >= 2
    # The retry against the healthy client produced the usable Challenger.
    assert result.instruction == "Recovered improved instruction."
    assert result.rationale == "authored after a credential refresh"
    assert result.usable is True
    assert good_client.calls == 1


# --- Req 15.1 — PROMPTING_GUIDANCE rides the contract on EVERY invocation ---
def test_prompting_guidance_rides_every_bedrock_author_invocation():
    """The repo-baked PROMPTING_GUIDANCE is embedded in the contract handed to the model
    on every ``author()`` call, and the verbatim Champion rides along too (Req 15.1 / 3.1).

    Two successive calls (different Champion text, different abstention mix) both carry the
    guidance inside ``raw["prompt"]`` — the contract is built unconditionally on each call,
    so the guidance is never dropped between iterations.
    """
    client = _FakeAuthorBedrockClient(deltas=['{"instruction": "Improved.", "rationale": "r"}'])
    author = BedrockAuthorClient("author-model-x", client=client, sleep=_instant_author_sleep)

    r1 = _run(
        author.author(
            target_model="sonnet-4.6",
            champion_instruction="CHAMPION_ALPHA text.",
            failures=[_author_failure(answered_when_unsure=True)],
        )
    )
    r2 = _run(
        author.author(
            target_model="haiku-4.5",
            champion_instruction="CHAMPION_BRAVO text.",
            failures=[_author_failure(answered_when_unsure=False)],
        )
    )

    for r in (r1, r2):
        assert pg.PROMPTING_GUIDANCE in r.raw["prompt"]
        assert "<prompting_guidance>" in r.raw["prompt"]

    # The verbatim Champion is in each call's contract (Req 3.1), and both calls really
    # went through converse_stream (the guidance rides EVERY invocation, not just the first).
    assert "CHAMPION_ALPHA text." in r1.raw["prompt"]
    assert "CHAMPION_BRAVO text." in r2.raw["prompt"]
    assert client.calls == 2


# ===========================================================================
# Task 11.5 — build_live_backend construction unit / example tests (FAKE factories)
# (design Component 1 "Backend wiring (offline | live)", Author/Judge separation,
#  preferred/fallback retrieval; Req 10.5, 4.2, 16.1, 16.2)
# ===========================================================================
#
# These tests pin the LIVE backend WIRING with ZERO network. ``build_live_backend``
# builds every Bedrock-touching client lazily through injectable factories, so a
# test constructs the whole live bundle with fakes and no AWS. The load-bearing
# properties (design Component 1):
#
#   * the live bundle builds from injected fake client factories (Bedrock judge +
#     author + embedding) and a fake OpenSearch client/usable predicate, exposing
#     all five seams the loop reads, and invoking NONE of the factories at
#     construction time (genuinely lazy / zero network — Req 10.5);
#   * it REFUSES to start when the resolved Author equals the Judge, raising
#     ``AuthorJudgeConflictError`` (Req 4.2) — and the default Author differs from
#     the Judge so the default construction is allowed; and
#   * retrieval defaults to OpenSearch-preferred with a local fallback: a usable
#     (fake) OpenSearch client wires ``retrieval.name == "opensearch"``, while an
#     unusable OpenSearch falls back to ``retrieval.name == "local"`` — both still
#     wrapped in the held-constant memoizing layer (Req 16.1 / 16.2).

from bakeoff.quality.optimizer.backends import (
    AuthorJudgeConflictError,
    OptimizerBackend,
    build_live_backend,
)


class _CountingClientFactory:
    """A lazy client factory that records how many times it was invoked.

    Lets a test prove ``build_live_backend`` performs ZERO network at construction: every
    Bedrock-touching client is built lazily, so a freshly-constructed live bundle must
    leave ``builds == 0`` for the judge / author / embedding factories.
    """

    def __init__(self, client=None) -> None:
        self.builds = 0
        self._client = client if client is not None else object()

    def __call__(self):
        self.builds += 1
        return self._client


# A distinct Author model id that is guaranteed != config.JUDGE_MODEL_ID, used wherever a
# test needs a valid (non-conflicting) author without depending on the default resolution.
_NONCONFLICTING_AUTHOR = "us.anthropic.claude-sonnet-4-6-test-author"


def test_build_live_backend_constructs_with_injected_fakes_and_zero_network():
    """The live bundle builds from injected fake client factories + a fake OpenSearch
    client, exposing all five seams the loop reads, and touching NO network at
    construction (every client factory is lazy — Req 10.5)."""
    assert _NONCONFLICTING_AUTHOR != config.JUDGE_MODEL_ID  # guard the fixture itself

    judge_factory = _CountingClientFactory()
    author_factory = _CountingClientFactory()
    embed_factory = _CountingClientFactory()
    fake_os = _FakeOpenSearchClient()

    backend = build_live_backend(
        _NONCONFLICTING_AUTHOR,
        judge_client_factory=judge_factory,
        author_client_factory=author_factory,
        embedding_client_factory=embed_factory,
        opensearch_client=fake_os,
        opensearch_usable=lambda b: True,
    )

    # A fully-wired live bundle exposing all five duck-typed seams the loop reads.
    assert isinstance(backend, OptimizerBackend)
    assert backend.name == "live"
    assert callable(backend.answer_adapter_factory)
    assert isinstance(backend.judge_scorer, JudgeScorer)
    assert backend.judge_scorer.judge_model == config.JUDGE_MODEL_ID
    assert backend.author.author_model == _NONCONFLICTING_AUTHOR
    assert isinstance(backend.retrieval, MemoizingRetrievalBackend)
    assert hasattr(backend.closeness_scorer, "score_turn")

    # ZERO network: no Bedrock client factory was invoked at construction (all lazy),
    # and the injected OpenSearch client was never queried.
    assert judge_factory.builds == 0
    assert author_factory.builds == 0
    assert embed_factory.builds == 0
    assert fake_os.calls == []


def test_build_live_backend_default_author_differs_from_judge_and_builds():
    """With no explicit Author the default resolves to Sonnet 4.6 (≠ the Opus Judge), so
    the default construction is allowed and the author/judge separation holds (Req 4.4)."""
    backend = build_live_backend(
        judge_client_factory=_CountingClientFactory(),
        author_client_factory=_CountingClientFactory(),
        embedding_client_factory=_CountingClientFactory(),
        opensearch_client=_FakeOpenSearchClient(),
        opensearch_usable=lambda b: True,
    )
    assert backend.author.author_model == _default_author_model()
    assert backend.author.author_model != config.JUDGE_MODEL_ID
    assert backend.name == "live"


def test_build_live_backend_refuses_when_author_equals_judge():
    """The live builder refuses to start when the resolved Author == the Judge, raising
    ``AuthorJudgeConflictError`` BEFORE any client is built (Req 4.1 / 4.2).

    The Judge must never grade a prompt authored by itself, and the loop must not contend
    with itself for the shared Opus quota — so an Author resolved to ``config.JUDGE_MODEL_ID``
    is rejected up front.
    """
    judge_factory = _CountingClientFactory()
    author_factory = _CountingClientFactory()

    with pytest.raises(AuthorJudgeConflictError) as ei:
        build_live_backend(
            config.JUDGE_MODEL_ID,  # Author == Judge -> conflict
            judge_client_factory=judge_factory,
            author_client_factory=author_factory,
            embedding_client_factory=_CountingClientFactory(),
            opensearch_client=_FakeOpenSearchClient(),
            opensearch_usable=lambda b: True,
        )

    # The refusal names the conflict and references the configured Judge model id.
    msg = str(ei.value)
    assert "Author and Judge" in msg
    assert config.JUDGE_MODEL_ID in msg
    # Refused before building anything — no client factory was touched.
    assert judge_factory.builds == 0
    assert author_factory.builds == 0


def test_build_live_backend_retrieval_prefers_opensearch_then_falls_back_to_local():
    """Retrieval defaults to OpenSearch-preferred with a guaranteed-workable local
    fallback, both wrapped in the held-constant memoizing layer (Req 16.1 / 16.2).

    A usable (injected fake) OpenSearch client wires ``retrieval.name == "opensearch"``;
    flipping the usability probe to ``False`` falls the selector back to the local backend
    (``retrieval.name == "local"``) — all with zero network (injected fakes / lazy clients).
    """
    # Preferred: a usable fake OpenSearch client -> the OpenSearch backend is wired.
    preferred = build_live_backend(
        _NONCONFLICTING_AUTHOR,
        judge_client_factory=_CountingClientFactory(),
        author_client_factory=_CountingClientFactory(),
        embedding_client_factory=_CountingClientFactory(),
        opensearch_client=_FakeOpenSearchClient(),
        opensearch_usable=lambda b: True,
    )
    assert isinstance(preferred.retrieval, MemoizingRetrievalBackend)
    assert preferred.retrieval.name == "opensearch"

    # Fallback: OpenSearch unusable -> the guaranteed-workable local backend (Req 16.2),
    # still wrapped for held-constant per-(turn-query) reuse.
    fallback = build_live_backend(
        _NONCONFLICTING_AUTHOR,
        judge_client_factory=_CountingClientFactory(),
        author_client_factory=_CountingClientFactory(),
        embedding_client_factory=_CountingClientFactory(),
        opensearch_client=None,
        opensearch_usable=lambda b: False,
        local_client=object(),
    )
    assert isinstance(fallback.retrieval, MemoizingRetrievalBackend)
    assert fallback.retrieval.name == "local"
