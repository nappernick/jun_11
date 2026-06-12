"""bakeoff.harness — stages 0 FREEZE, 1 RERANK, 2 SCORE (per-row).

Pure stdlib. All seams (OpenSearch retrieval, auth, abstention gate,
persona→scope mapping) are clearly marked and raise NotImplementedError.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from bakeoff.contract import Candidate, Fixture, RankedDoc, Reranker, ScoredRow
from bakeoff.normalize import squash


# ===========================================================================
# Stage 0: FREEZE — load fixtures / candidate retrieval seam
# ===========================================================================

def load_fixtures(path: str | Path) -> list[Fixture]:
    """Load frozen fixtures from a JSONL file."""
    fixtures: list[Fixture] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                fixtures.append(Fixture.from_dict(json.loads(line)))
    return fixtures


def freeze_candidates(query: str, scope: dict, *, pool_size: int = 20) -> list[Candidate]:
    """FREEZE: retrieve a BM25 candidate pool from the live alpha AOSS corpus.

    `scope` is a {axis: value(s)} map over the indexed keyword fields
    (country / level / role / line_of_business / content_type). It is turned
    into a sentinel-aware filter (specific value OR the axis "applies-to-all"
    token) by bakeoff.access.scope_filter, so content tagged for everyone is
    always eligible. Pass an empty scope to retrieve unscoped.

    Used once at fixture-build time to FREEZE candidates into a JSONL; the eval
    run itself replays those frozen fixtures and never retrieves live (so the
    rerank comparison sees identical candidates every run). Requires alpha creds
    (see bakeoff.access) and the non-stdlib deps boto3/requests, imported lazily
    so the pure-stdlib MockReranker test path stays dependency-free.
    """
    from bakeoff.access import scope_filter

    acc = _aoss_access()
    sf = scope_filter(scope) if scope else None
    return acc.search(query, size=pool_size, scope_filter=sf)


_AOSS_ACCESS = None


def _aoss_access():
    """Lazily build and cache a single AossAccess (one boto3 session per process)."""
    global _AOSS_ACCESS
    if _AOSS_ACCESS is None:
        from bakeoff.access import AossAccess

        _AOSS_ACCESS = AossAccess()
    return _AOSS_ACCESS


# ===========================================================================
# Stage 1: RERANK — call adapter, time only the rerank() call
# ===========================================================================

def run_model(reranker: Reranker, fixtures: list[Fixture], top_k: int) -> list[ScoredRow]:
    """Run a reranker over all fixtures, returning one ScoredRow per fixture."""
    rows: list[ScoredRow] = []
    for fixture in fixtures:
        t0 = time.perf_counter()
        ranked = reranker.rerank(fixture.query, fixture.candidates, top_k)
        t1 = time.perf_counter()
        latency_ms = (t1 - t0) * 1000.0
        rows.append(score_one(fixture, ranked, latency_ms, reranker.id))
    return rows


# ===========================================================================
# Stage 2: SCORE — per-row scoring
# ===========================================================================

def score_one(fixture: Fixture, ranked: list[RankedDoc], latency_ms: float,
              model_id: str = "unknown") -> ScoredRow:
    """Compute a single evaluation row from a fixture and reranker output."""
    gold = fixture.gold_node_ids
    candidate_node_ids = {c.node_id for c in fixture.candidates}

    rels = [1 if doc.node_id in gold else 0 for doc in ranked]
    gold_total = len(gold)
    gold_retrievable = len(gold & candidate_node_ids)

    if gold_total == 0:
        abstain_class = "unanswerable"
    elif gold_retrievable == 0:
        abstain_class = "answerable_not_retrieved"
    else:
        abstain_class = "answerable_retrievable"

    expect_abstain = abstain_class == "unanswerable"
    top_norm = ranked[0].norm_score if ranked else 0.0

    return ScoredRow(
        model_id=model_id,
        query_id=fixture.query_id,
        slice=fixture.slice,
        latency_ms=latency_ms,
        rels=rels,
        gold_total=gold_total,
        gold_retrievable=gold_retrievable,
        abstain_class=abstain_class,
        expect_abstain=expect_abstain,
        top_norm=top_norm,
    )


# ===========================================================================
# MockReranker — deterministic, token-overlap scoring, never throws
# ===========================================================================

class MockReranker:
    """Deterministic mock reranker using token overlap for pseudo-relevance.

    Never throws on bad/empty docs (scores them very low).
    Used by tests and the integrator.
    """

    @property
    def id(self) -> str:
        return "mock"

    def rerank(self, query: str, candidates: list[Candidate], top_k: int) -> list[RankedDoc]:
        query_tokens = set(query.lower().split())
        scored: list[tuple[float, int, Candidate]] = []
        for i, cand in enumerate(candidates):
            try:
                doc_tokens = set(cand.text.lower().split()) if cand.text else set()
                overlap = len(query_tokens & doc_tokens)
                # raw_score: overlap count (0 for empty/bad docs)
                raw = float(overlap) if overlap > 0 else -5.0
            except Exception:
                raw = -5.0
            scored.append((raw, i, cand))

        # Sort descending by raw score, stable by original index
        scored.sort(key=lambda x: (-x[0], x[1]))
        ranked: list[RankedDoc] = []
        for rank, (raw, _, cand) in enumerate(scored[:top_k]):
            ranked.append(RankedDoc(
                node_id=cand.node_id,
                rank=rank,
                raw_score=raw,
                norm_score=squash(raw, "logit"),
            ))
        return ranked
