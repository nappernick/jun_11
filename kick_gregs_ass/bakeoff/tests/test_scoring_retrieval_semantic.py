"""
Task-6 acceptance tests for the Layer-A retrieval-aligned and Layer-B semantic
scorers (Requirements 4.1, 4.2, 4.7). Everything here runs **offline**.

This file carries the exact hand-computed fixtures named in the task
(gold={A,B}, ranked=[A,X,B,Y], k=4 → precision@4=0.5, recall@4=1.0, MRR=1.0,
nDCG@4≈0.9197) plus two more rankings, the empty-gold and empty-ranked edge
cases, the grounded-vs-ungrounded grounding direction, and a fake-embedder
semantic test asserting the documented cosine convention and the content-hash
cache's zero-extra-call behavior. It complements the broader coverage in
``test_retrieval_aligned.py`` and ``test_semantic.py``.

Conventions the assertions match (as DOCUMENTED in the implementation):
* ``precision_at_k`` uses the textbook ``hits@k / k`` denominator, so a ranked
  list shorter than ``k`` is penalized for the empty slots.
* Semantic similarity is **raw cosine in [-1, 1]** (identical→1.0, orthogonal→0.0,
  opposite→-1.0); the scorer reports the raw value rather than a [0,1] remap.

Validates: Requirements 4.1, 4.2, 4.7
"""
from __future__ import annotations

import json
import math

import pytest

from bakeoff.scoring import retrieval_aligned as ra
from bakeoff.scoring.semantic import EmbeddingClient, SemanticSimilarityScorer


def _mrr(ranked, gold):
    """Per-item reciprocal rank — resolves whichever name the module exports.

    The implementation names this ``mrr`` (it populates ``AccuracyScores.mrr``);
    the bundle key is ``"mrr"`` in all variants. This shim keeps the test pinned
    to the stable concept rather than a single function name.
    """
    fn = getattr(ra, "mrr", None) or getattr(ra, "reciprocal_rank")
    return fn(ranked, gold)


# ===========================================================================
# IR ranking metrics — hand-computed fixtures (Req 4.1)
# ===========================================================================
def test_fixture1_interleaved_relevant():
    # gold={A,B}, ranked=[A,X,B,Y], k=4  (the exact fixture from the task)
    #   precision@4 = 2 relevant / 4 (window)   = 0.5
    #   recall@4    = 2 relevant / 2 gold        = 1.0
    #   MRR         = A at rank 1                = 1.0
    #   DCG@4  = rel[1,0,1,0] = 1/log2(2) + 1/log2(4) = 1.0 + 0.5     = 1.5
    #   IDCG@4 = 2 ideal      = 1/log2(2) + 1/log2(3) = 1.0 + 0.63093 = 1.63093
    #   nDCG@4 = 1.5 / 1.63093 = 0.9197207891481876
    ranked = ["A", "X", "B", "Y"]
    gold = {"A", "B"}
    assert ra.precision_at_k(ranked, gold, 4) == 0.5
    assert ra.recall_at_k(ranked, gold, 4) == 1.0
    assert _mrr(ranked, gold) == 1.0
    assert ra.ndcg_at_k(ranked, gold, 4) == pytest.approx(0.9197207891481876, abs=1e-12)


def test_fixture2_relevant_lower_in_ranking():
    # gold={A,B,C}, ranked=[X,A,Y,B], k=4
    #   precision@4 = 2 relevant / 4             = 0.5
    #   recall@4    = 2 relevant / 3 gold        = 0.6666...
    #   MRR         = A at rank 2                = 0.5
    #   DCG@4  = rel[0,1,0,1] = 1/log2(3) + 1/log2(5) = 0.63093 + 0.43068 = 1.06161
    #   IDCG@4 = 3 ideal      = 1 + 1/log2(3) + 1/log2(4) = 2.13093
    #   nDCG@4 = 1.06161 / 2.13093 = 0.49818925746641285
    ranked = ["X", "A", "Y", "B"]
    gold = {"A", "B", "C"}
    assert ra.precision_at_k(ranked, gold, 4) == 0.5
    assert ra.recall_at_k(ranked, gold, 4) == pytest.approx(2 / 3, abs=1e-12)
    assert _mrr(ranked, gold) == 0.5
    assert ra.ndcg_at_k(ranked, gold, 4) == pytest.approx(0.49818925746641285, abs=1e-12)


def test_fixture3_perfect_ranking_small_k():
    # gold={A,B}, ranked=[A,B,X,Y], k=2 -> perfect at the cutoff.
    #   precision@2 = 2/2 = 1.0 ; recall@2 = 2/2 = 1.0 ; MRR = 1.0
    #   DCG@2 = 1/log2(2)+1/log2(3) ; IDCG@2 identical -> nDCG = 1.0
    ranked = ["A", "B", "X", "Y"]
    gold = {"A", "B"}
    assert ra.precision_at_k(ranked, gold, 2) == 1.0
    assert ra.recall_at_k(ranked, gold, 2) == 1.0
    assert _mrr(ranked, gold) == 1.0
    assert ra.ndcg_at_k(ranked, gold, 2) == pytest.approx(1.0, abs=1e-12)


def test_edge_empty_gold_is_zero_documented():
    # Empty gold (unanswerable item) -> every ranking metric is 0.0 (documented).
    ranked = ["A", "B", "C"]
    gold: set[str] = set()
    assert ra.precision_at_k(ranked, gold, 4) == 0.0
    assert ra.recall_at_k(ranked, gold, 4) == 0.0
    assert _mrr(ranked, gold) == 0.0
    assert ra.ndcg_at_k(ranked, gold, 4) == 0.0


def test_edge_empty_ranked_list_is_zero():
    # Empty ranked list -> nothing relevant retrieved -> all 0.0.
    ranked: list[str] = []
    gold = {"A", "B"}
    assert ra.precision_at_k(ranked, gold, 4) == 0.0
    assert ra.recall_at_k(ranked, gold, 4) == 0.0
    assert _mrr(ranked, gold) == 0.0
    assert ra.ndcg_at_k(ranked, gold, 4) == 0.0


def test_edge_k_larger_than_list_length():
    # ranked=[A], gold={A}, k=5: textbook precision divides by k -> 1/5 = 0.2;
    #   recall = 1/1 = 1.0; MRR = 1.0; nDCG = 1.0 (single ideal hit, single hit).
    ranked = ["A"]
    gold = {"A"}
    assert ra.precision_at_k(ranked, gold, 5) == pytest.approx(0.2)
    assert ra.recall_at_k(ranked, gold, 5) == 1.0
    assert _mrr(ranked, gold) == 1.0
    assert ra.ndcg_at_k(ranked, gold, 5) == pytest.approx(1.0, abs=1e-12)


def test_k_zero_or_negative_is_zero():
    ranked = ["A", "B"]
    gold = {"A"}
    assert ra.precision_at_k(ranked, gold, 0) == 0.0
    assert ra.ndcg_at_k(ranked, gold, 0) == 0.0
    assert ra.recall_at_k(ranked, gold, -1) == 0.0


# ===========================================================================
# Answer grounding (Req 4.1 — the model differentiator)
# ===========================================================================
GOLD_TEXT = (
    "Submit your expense report within thirty days of the purchase date "
    "to be reimbursed for business travel costs."
)
DISTRACTOR_TEXT = (
    "The corporate travel booking tool is available in the Slack application "
    "sidebar under the shortcuts menu."
)
FRAGMENTS = [
    {"id": "G1", "text": GOLD_TEXT},
    {"id": "D1", "text": DISTRACTOR_TEXT},
]
FRAGMENT_IDS = ["G1", "D1"]
GOLD_IDS = ["G1"]


def test_grounded_answer_scores_high():
    # The answer uses the gold fragment's content -> attributed to G1.
    p, r = ra.grounding_precision_recall(GOLD_TEXT, FRAGMENTS, GOLD_IDS, FRAGMENT_IDS)
    assert p == 1.0  # the one grounded claim rests on the gold fragment
    assert r == 1.0  # the retrieved gold fragment was used


def test_ungrounded_answer_scores_low():
    # The answer is built from the distractor, ignoring the gold fragment.
    p, r = ra.grounding_precision_recall(
        DISTRACTOR_TEXT, FRAGMENTS, GOLD_IDS, FRAGMENT_IDS
    )
    assert p == 0.0  # the claim rests on a non-gold fragment
    assert r == 0.0  # the gold fragment went unused


def test_grounded_strictly_beats_ungrounded():
    gp, gr = ra.grounding_precision_recall(GOLD_TEXT, FRAGMENTS, GOLD_IDS, FRAGMENT_IDS)
    up, ur = ra.grounding_precision_recall(
        DISTRACTOR_TEXT, FRAGMENTS, GOLD_IDS, FRAGMENT_IDS
    )
    assert gp > up
    assert gr > ur


def test_empty_answer_grounding_is_zero_documented():
    # No claims attributed -> documented edge behavior is 0.0 / 0.0.
    p, r = ra.grounding_precision_recall("", FRAGMENTS, GOLD_IDS, FRAGMENT_IDS)
    assert p == 0.0
    assert r == 0.0


def test_score_returns_full_layer_a_bundle():
    # score_retrieval_aligned combines ranking-vs-gold + grounding into one dict.
    result = ra.score_retrieval_aligned(
        ["G1", "D1"], GOLD_IDS, GOLD_TEXT, FRAGMENTS, k=2
    )
    assert set(result) == {
        "precision_at_k",
        "recall_at_k",
        "mrr",
        "ndcg_at_k",
        "grounding_precision",
        "grounding_recall",
    }
    assert result["mrr"] == 1.0  # gold at rank 1
    assert result["recall_at_k"] == 1.0
    assert result["grounding_precision"] == 1.0
    assert result["grounding_recall"] == 1.0
    assert all(0.0 <= v <= 1.0 for v in result.values())


# ===========================================================================
# Semantic similarity — deterministic FAKE embedder, offline (Req 4.2, 4.7)
# ===========================================================================
class _FakeBody:
    """Mimics the Bedrock streaming body: a ``.read()`` returning JSON bytes."""

    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class FakeEmbedClient:
    """boto3-shaped bedrock-runtime client with no network + call accounting.

    ``vectors`` maps an exact text -> the vector to return. Identical text always
    yields the identical vector (so cosine 1.0). ``calls`` counts ``invoke_model``
    invocations, which is what the cache test asserts against.
    """

    def __init__(self, vectors: dict[str, list[float]]):
        self.vectors = vectors
        self.calls = 0
        self.seen_texts: list[str] = []

    def invoke_model(self, *, modelId, body, accept, contentType):  # noqa: N803
        self.calls += 1
        parsed = json.loads(body)
        texts = parsed["texts"]
        self.seen_texts.extend(texts)
        out = [self.vectors.get(t, [float(len(t)), 1.0, 0.0]) for t in texts]
        return {"body": _FakeBody({"embeddings": {"float": out}})}


def _scorer(fake: FakeEmbedClient, tmp_path, *, disk_cache: bool = False):
    client = EmbeddingClient(
        client_factory=lambda: fake, cache_dir=tmp_path, disk_cache=disk_cache
    )
    return SemanticSimilarityScorer(client=client)


def test_identical_text_is_max_similarity(tmp_path):
    # Identical answer == ideal -> identical vector -> raw cosine 1.0.
    fake = FakeEmbedClient({"the answer text": [0.3, 0.7, 0.1]})
    scorer = _scorer(fake, tmp_path)
    assert scorer.score("the answer text", "the answer text") == pytest.approx(1.0)


def test_orthogonal_is_zero_raw_cosine(tmp_path):
    # Orthogonal vectors -> raw cosine 0.0 (documented raw-cosine convention).
    fake = FakeEmbedClient({"east": [1.0, 0.0, 0.0], "north": [0.0, 1.0, 0.0]})
    scorer = _scorer(fake, tmp_path)
    assert scorer.score("east", "north") == pytest.approx(0.0, abs=1e-12)


def test_opposite_is_minus_one_raw_cosine(tmp_path):
    # Antipodal vectors -> raw cosine -1.0.
    fake = FakeEmbedClient({"pos": [1.0, 0.0], "neg": [-1.0, 0.0]})
    scorer = _scorer(fake, tmp_path)
    assert scorer.score("pos", "neg") == pytest.approx(-1.0, abs=1e-12)


def test_identical_beats_different(tmp_path):
    fake = FakeEmbedClient({"a": [1.0, 0.0, 0.0], "b": [0.0, 1.0, 0.0]})
    scorer = _scorer(fake, tmp_path)
    identical = scorer.score("a", "a")
    different = scorer.score("a", "b")
    assert identical > different


def test_empty_text_is_zero_without_calls(tmp_path):
    fake = FakeEmbedClient({})
    scorer = _scorer(fake, tmp_path)
    assert scorer.score("", "something") == 0.0
    assert scorer.score("something", "") == 0.0
    assert fake.calls == 0  # no embedding attempted for degenerate input


def test_cache_prevents_second_embed_call_for_identical_content(tmp_path):
    # Req 4.7: re-scoring identical content makes ZERO new embed calls.
    fake = FakeEmbedClient({"ans": [1.0, 2.0, 3.0], "ideal": [3.0, 2.0, 1.0]})
    scorer = _scorer(fake, tmp_path, disk_cache=True)

    first = scorer.score("ans", "ideal")
    assert fake.calls == 2  # one invoke per distinct text (answer + ideal)
    assert sorted(fake.seen_texts) == ["ans", "ideal"]

    # Identical content again -> served entirely from cache -> no new invocation.
    second = scorer.score("ans", "ideal")
    assert second == pytest.approx(first)
    assert fake.calls == 2  # unchanged: zero new embed calls
    assert sorted(fake.seen_texts) == ["ans", "ideal"]  # nothing re-embedded


def test_disk_cache_survives_new_scorer_instance(tmp_path):
    # The content-hash disk cache means a fresh scorer makes zero embed calls.
    vectors = {"ans": [1.0, 2.0, 3.0], "ideal": [3.0, 2.0, 1.0]}
    fake_a = FakeEmbedClient(dict(vectors))
    scorer_a = _scorer(fake_a, tmp_path, disk_cache=True)
    a = scorer_a.score("ans", "ideal")
    assert fake_a.calls == 2

    fake_b = FakeEmbedClient({"ans": [9.9], "ideal": [9.9]})  # would differ if used
    scorer_b = _scorer(fake_b, tmp_path, disk_cache=True)
    b = scorer_b.score("ans", "ideal")
    assert b == pytest.approx(a, abs=1e-12)
    assert fake_b.calls == 0  # served entirely from the shared disk cache


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
