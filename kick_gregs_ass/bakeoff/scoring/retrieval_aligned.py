"""
Layer-A retrieval-aligned accuracy scorer (Task 6, Req 4.1).

Two distinct sub-measurements, kept apart on purpose (design "Layer A"):

1. **Retrieval ranking quality vs gold** — precision@k, recall@k, MRR, nDCG@k of
   the **constant** ``/retrieve`` ranking (the ranked ``fragment_ids``) against the
   item's ``gold_node_ids``. This is a property of the *substrate*, not of any
   candidate model: it characterizes the accuracy *ceiling* (if a gold fragment
   was never retrieved, no model can ground on it). It is logged for context so a
   model is not blamed for a retrieval miss — it is **not** a model differentiator.

2. **Answer grounding vs gold** — grounding precision/recall measuring whether the
   *model's answer* actually used the gold fragments that were retrieved. This
   **is** the model differentiator: a model that ignored the gold fragment sitting
   in its context and answered from parametric memory scores low here even if the
   words sound plausible.

Everything in this module is **pure, deterministic, CPU-only math** (no network,
no embeddings) so it is trivially unit-testable against hand-computed gold
rankings. The "semantic attribution of answer sentences to fragment text"
described in the design is realized here as a **deterministic lexical
content-overlap** attribution (token containment over stop-word-filtered content
words), with an explicit **citation-overlap** fast path when the model cites
fragments (by node id or by bracketed ``[n]`` rank reference). Lexical overlap is
chosen over an embedding model so this scorer stays cheap, deterministic, and
network-free; the embedding-based cross-check lives in the separate
:mod:`bakeoff.scoring.semantic` Layer-B scorer.

Formulas (all binary relevance: a fragment is relevant iff its id ∈ gold set):

* **precision@k** = ``hits@k / k`` — the textbook definition: relevant fragments
  among the top ``k`` divided by ``k`` (so a system that returns fewer than ``k``
  is penalized for the missing slots). 0.0 when ``k <= 0``.
* **recall@k**    = ``hits@k / |gold|``  (0.0 when the gold set is empty).
* **MRR / reciprocal rank** = ``1 / rank`` where ``rank`` is the 1-indexed
  position of the *first* relevant fragment in the full ranking; 0.0 if no gold
  fragment appears. (The "mean" of MRR is taken across items at aggregation; this
  module returns the per-item reciprocal rank that feeds it; the function is named
  ``mrr`` to match the :class:`bakeoff.types.AccuracyScores` field.)
* **nDCG@k** with the standard log2 discount:
  ``DCG@k = Σ_{i=1..k} rel_i / log2(i + 1)`` (i 1-indexed, so position 1 → log2 2 =
  1), ``IDCG@k = Σ_{i=1..min(k, |gold|)} 1 / log2(i + 1)`` (the ideal ranking puts
  every relevant fragment first), ``nDCG@k = DCG@k / IDCG@k`` (0.0 when IDCG = 0).

* **grounding_precision** = ``|used ∩ gold| / |used|`` — of the fragments the answer
  actually relied on, how many were gold (0.0 when the answer used nothing).
* **grounding_recall**    = ``|used ∩ gold ∩ retrieved| / |gold ∩ retrieved|`` — of
  the gold fragments that *were in context*, how many did the answer use (0.0 when
  no gold fragment was retrieved — that retrieval-ceiling case is already surfaced
  by recall@k, so grounding does not double-count it as model skill).
"""
from __future__ import annotations

import math
import re
from typing import Iterable, Mapping, Optional, Sequence

from bakeoff import config

__all__ = [
    "precision_at_k",
    "recall_at_k",
    "mrr",
    "ndcg_at_k",
    "detect_citations",
    "used_fragment_ids",
    "grounding_precision_recall",
    "score_retrieval_aligned",
    "RetrievalAlignedScorer",
    "GROUNDING_OVERLAP_THRESHOLD",
    "GROUNDING_MIN_OVERLAP_TOKENS",
]

# --- grounding (lexical attribution) tunables ------------------------------
# A retrieved fragment is judged "used" by the answer if some answer sentence's
# content-word containment in the fragment is at least this fraction AND at least
# this many distinct content words overlap. Both guards together prevent a single
# shared common word from counting as grounding. Module-level constants (not
# hard-coded inline) so the pipeline/plan can tune attribution strictness later.
GROUNDING_OVERLAP_THRESHOLD: float = 0.5
GROUNDING_MIN_OVERLAP_TOKENS: int = 2

# A small, deterministic English stop-word set. Kept intentionally tiny and
# inlined (no NLTK dependency) so attribution is reproducible and hand-checkable.
_STOPWORDS: frozenset[str] = frozenset(
    """
    a an the this that these those of to in on at for from by with without and or
    but if then else when while is are was were be been being am do does did done
    have has had having i you he she it we they me him her them my your his its our
    their as so not no yes can could should would may might will shall must about
    into over under again more most some any all each every which who whom what how
    why where there here be you're i'm how's it's within
    """.split()
)

# Bracketed citation like "[1]" / "[12]" referencing a fragment by 1-indexed rank.
_BRACKET_CITATION = re.compile(r"\[(\d+)\]")
# Word tokenizer: runs of letters/digits (also keeps intra-word hyphens/underscores
# joined so node-id-like tokens survive as single tokens).
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
# Sentence splitter: break on . ! ? and newlines (kept deliberately simple;
# grounding is a coarse signal, not linguistics).
_SENTENCE_SPLIT = re.compile(r"[.!?\n]+")


# ---------------------------------------------------------------------------
# Ranking metrics vs gold (substrate ceiling; context, not a differentiator)
# ---------------------------------------------------------------------------
def precision_at_k(
    ranked_ids: Sequence[str], gold_ids: Iterable[str], k: int
) -> float:
    """Textbook precision@k: relevant fragments among the top ``k``, divided by ``k``.

    Dividing by ``k`` (not by the number actually returned) means a system that
    returns fewer than ``k`` fragments is penalized for the empty slots. Returns
    0.0 when ``k <= 0``.
    """
    if k <= 0:
        return 0.0
    gold = set(gold_ids)
    hits = sum(1 for fid in ranked_ids[:k] if fid in gold)
    return hits / k


def recall_at_k(
    ranked_ids: Sequence[str], gold_ids: Iterable[str], k: int
) -> float:
    """Fraction of all gold fragments that appear in the top-``k`` returned.

    Returns 0.0 when the gold set is empty (recall is undefined with no gold; the
    conservative 0.0 keeps the metric a plain float) or when ``k <= 0``.
    """
    if k <= 0:
        return 0.0
    gold = set(gold_ids)
    if not gold:
        return 0.0
    topk = set(ranked_ids[:k])
    return len(gold & topk) / len(gold)


def mrr(ranked_ids: Sequence[str], gold_ids: Iterable[str]) -> float:
    """Reciprocal of the 1-indexed rank of the first gold fragment.

    This is the per-item term whose *mean across items* is MRR; it populates the
    ``mrr`` field of :class:`bakeoff.types.AccuracyScores`. Scans the full ranking
    (not just a top-k window), per the standard MRR definition. Returns 0.0 if no
    gold fragment appears anywhere in the ranking.
    """
    gold = set(gold_ids)
    for idx, fid in enumerate(ranked_ids, start=1):
        if fid in gold:
            return 1.0 / idx
    return 0.0


def ndcg_at_k(
    ranked_ids: Sequence[str], gold_ids: Iterable[str], k: int
) -> float:
    """Normalized DCG at ``k`` with the standard log2 discount, binary relevance.

    ``DCG@k = Σ_{i=1..k} rel_i / log2(i + 1)`` over the actual ranking;
    ``IDCG@k = Σ_{i=1..min(k, |gold|)} 1 / log2(i + 1)`` over the ideal ranking
    (all relevant first). Returns 0.0 when IDCG is 0 (no gold, or ``k <= 0``).
    """
    if k <= 0:
        return 0.0
    gold = set(gold_ids)
    n_gold = len(gold)
    if n_gold == 0:
        return 0.0

    dcg = 0.0
    for i, fid in enumerate(ranked_ids[:k], start=1):
        if fid in gold:
            dcg += 1.0 / math.log2(i + 1)

    ideal_hits = min(k, n_gold)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    if idcg == 0.0:
        return 0.0
    return dcg / idcg


# ---------------------------------------------------------------------------
# Answer grounding vs gold (the model differentiator)
# ---------------------------------------------------------------------------
def _content_tokens(text: Optional[str]) -> set[str]:
    """Lowercased content-word token set of ``text`` (stop-words removed)."""
    if not text:
        return set()
    return {
        tok
        for tok in (m.group(0).lower() for m in _WORD.finditer(text))
        if tok not in _STOPWORDS
    }


def _sentence_content_token_sets(text: Optional[str]) -> list[set[str]]:
    """Per-sentence content-token sets (empty sentences dropped)."""
    if not text:
        return []
    out: list[set[str]] = []
    for raw in _SENTENCE_SPLIT.split(text):
        toks = _content_tokens(raw)
        if toks:
            out.append(toks)
    return out


def detect_citations(
    answer_text: Optional[str], ranked_ids: Sequence[str]
) -> set[str]:
    """Return the set of fragment ids the answer *explicitly cites*, if any.

    Two citation styles are recognized (their union is returned):

    * **Node-id citation** — a fragment's ``node_id`` appears verbatim in the
      answer text.
    * **Bracketed rank citation** — ``[n]`` references the fragment at 1-indexed
      rank ``n`` in ``ranked_ids`` (out-of-range ``n`` is ignored).

    An empty result means the answer cited nothing recognizable, in which case
    grounding falls back to lexical attribution.
    """
    if not answer_text:
        return set()
    cited: set[str] = set()
    for fid in ranked_ids:
        if fid and fid in answer_text:
            cited.add(fid)
    for m in _BRACKET_CITATION.finditer(answer_text):
        rank = int(m.group(1))
        if 1 <= rank <= len(ranked_ids):
            cited.add(ranked_ids[rank - 1])
    return cited


def used_fragment_ids(
    answer_text: Optional[str],
    fragments: Sequence[Mapping[str, object]],
    ranked_ids: Optional[Sequence[str]] = None,
    *,
    overlap_threshold: float = GROUNDING_OVERLAP_THRESHOLD,
    min_overlap_tokens: int = GROUNDING_MIN_OVERLAP_TOKENS,
) -> set[str]:
    """Set of retrieved fragment ids the answer is judged to have *used*.

    Resolution: if the answer cites fragments (see :func:`detect_citations`), the
    cited set is returned (citation-overlap path). Otherwise a fragment is "used"
    iff some answer sentence's content-word **containment** in the fragment text
    is at least ``overlap_threshold`` AND at least ``min_overlap_tokens`` distinct
    content words overlap (lexical-attribution path).

    ``fragments`` are the verbatim ``/retrieve`` fragment dicts (``id`` + ``text``).
    ``ranked_ids`` (the ranked id list) is only needed for bracketed-rank
    citations; it defaults to the ids pulled from ``fragments``.
    """
    if ranked_ids is None:
        ranked_ids = [str(f.get("id")) for f in fragments]

    cited = detect_citations(answer_text, ranked_ids)
    if cited:
        return cited

    sentence_sets = _sentence_content_token_sets(answer_text)
    if not sentence_sets:
        return set()

    used: set[str] = set()
    for frag in fragments:
        fid = frag.get("id")
        if fid is None:
            continue
        frag_tokens = _content_tokens(str(frag.get("text") or ""))
        if not frag_tokens:
            continue
        for sent in sentence_sets:
            overlap = sent & frag_tokens
            if (
                len(overlap) >= min_overlap_tokens
                and (len(overlap) / len(sent)) >= overlap_threshold
            ):
                used.add(str(fid))
                break
    return used


def grounding_precision_recall(
    answer_text: Optional[str],
    fragments: Sequence[Mapping[str, object]],
    gold_ids: Iterable[str],
    ranked_ids: Optional[Sequence[str]] = None,
    *,
    overlap_threshold: float = GROUNDING_OVERLAP_THRESHOLD,
    min_overlap_tokens: int = GROUNDING_MIN_OVERLAP_TOKENS,
) -> tuple[float, float]:
    """Grounding precision and recall of gold-fragment *usage* by the answer.

    * precision = ``|used ∩ gold| / |used|``        (0.0 if the answer used nothing)
    * recall    = ``|used ∩ gold ∩ retrieved| / |gold ∩ retrieved|``
      (0.0 if no gold fragment was retrieved — that ceiling case belongs to
      recall@k, not to the model's grounding skill).

    Returns ``(grounding_precision, grounding_recall)``.
    """
    if ranked_ids is None:
        ranked_ids = [str(f.get("id")) for f in fragments]
    retrieved = set(ranked_ids)
    gold = set(gold_ids)
    gold_in_context = gold & retrieved

    used = used_fragment_ids(
        answer_text,
        fragments,
        ranked_ids,
        overlap_threshold=overlap_threshold,
        min_overlap_tokens=min_overlap_tokens,
    )

    precision = (len(used & gold) / len(used)) if used else 0.0
    recall = (
        len(used & gold_in_context) / len(gold_in_context)
        if gold_in_context
        else 0.0
    )
    return precision, recall


# ---------------------------------------------------------------------------
# Bundle: all six retrieval-aligned numbers for one trial
# ---------------------------------------------------------------------------
def score_retrieval_aligned(
    ranked_ids: Sequence[str],
    gold_ids: Iterable[str],
    answer_text: Optional[str],
    fragments: Optional[Sequence[Mapping[str, object]]] = None,
    k: Optional[int] = None,
) -> dict[str, float]:
    """Compute the full Layer-A bundle for one trial.

    Returns a dict with ``precision_at_k``, ``recall_at_k``, ``mrr``, ``ndcg_at_k``
    (substrate ranking vs gold) and ``grounding_precision``, ``grounding_recall``
    (model differentiator) — exactly the retrieval-aligned fields of
    :class:`bakeoff.types.AccuracyScores`.

    ``fragments`` (verbatim ``/retrieve`` dicts with ``id``+``text``) are needed for
    grounding's lexical attribution; if omitted, both grounding numbers degrade to
    0.0. ``k`` defaults to :data:`bakeoff.config.SCORING_K`.
    """
    if k is None:
        k = config.SCORING_K
    gold = list(gold_ids)
    frags = list(fragments) if fragments is not None else []

    grounding_p, grounding_r = grounding_precision_recall(
        answer_text, frags, gold, ranked_ids
    )
    return {
        "precision_at_k": precision_at_k(ranked_ids, gold, k),
        "recall_at_k": recall_at_k(ranked_ids, gold, k),
        "mrr": mrr(ranked_ids, gold),
        "ndcg_at_k": ndcg_at_k(ranked_ids, gold, k),
        "grounding_precision": grounding_p,
        "grounding_recall": grounding_r,
    }


class RetrievalAlignedScorer:
    """Stateless scorer exposing the Layer-A bundle (design "Component 5").

    Pure CPU; the runner offloads it via ``asyncio.to_thread`` (design AD-3), so
    :meth:`score` is a plain synchronous method.
    """

    name = "retrieval_aligned"

    def __init__(self, k: Optional[int] = None) -> None:
        self.k = k if k is not None else config.SCORING_K

    def _grounding(
        self,
        answer_text: Optional[str],
        fragments: Sequence[Mapping[str, object]],
        gold_ids: Iterable[str],
        ranked_ids: Optional[Sequence[str]] = None,
    ) -> tuple[float, float]:
        """Grounding (precision, recall) — see :func:`grounding_precision_recall`."""
        return grounding_precision_recall(answer_text, fragments, gold_ids, ranked_ids)

    def score(
        self,
        ranked_ids: Sequence[str],
        gold_ids: Iterable[str],
        answer_text: Optional[str],
        fragments: Optional[Sequence[Mapping[str, object]]] = None,
        k: Optional[int] = None,
    ) -> dict[str, float]:
        """Full Layer-A bundle (ranking-vs-gold + grounding) as a flat dict."""
        return score_retrieval_aligned(
            ranked_ids, gold_ids, answer_text, fragments, k if k is not None else self.k
        )
