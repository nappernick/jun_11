"""
Per-turn closeness scorer for the quality study (Phase-1 local components).

For one turn this turns ``(answer_text, reference_text, ground_truth_kind,
answerability)`` into a :class:`bakeoff.quality.types.TurnCloseness`. It mirrors
the bake-off's two-phase split: the **local, CPU-only** components (semantic
similarity + turn-1 abstention-correctness) are computed in Phase-1 during the
run; the **judge** verdict is deferred to Phase-2 (:mod:`bakeoff.quality.judge`)
and folded into the composite there.

The three regimes (set by :func:`bakeoff.quality.dataset.turn_reference`):

* :data:`GroundTruthKind.GOLD` (turn-1, answerable) — semantic cosine of the
  answer vs the gold-derived ideal. Composite (Phase-1) = the clamped cosine;
  Phase-2 re-mixes in the judge.
* :data:`GroundTruthKind.ABSTENTION` (turn-1, answerability ``none``) — there is
  no correct content to be close to, so closeness IS abstention-correctness: 1.0
  iff the model correctly declined/escalated (via
  :func:`bakeoff.scoring.answerability.score_answerability`), else 0.0. Semantic
  similarity is not meaningful here and is recorded as 0.0.
* :data:`GroundTruthKind.WANTS` (later turns) — semantic cosine of the answer vs
  the turn's ``wants`` text. An empty ``wants`` is unscoreable: the scorer returns
  a neutral, explicitly-flagged result (semantic 0.0) rather than a fake number.

Backend injection: the semantic scorer is a
:class:`bakeoff.scoring.semantic.SemanticSimilarityScorer`, which already takes an
injectable ``embed_fn`` — so passing one built on the deterministic offline
embedder (or the real Embed v4) is all that differs between an offline test run
and a real run. The scorer here never imports boto3.

The Phase-1 composite is intentionally the local signal only (clamped cosine, or
abstention 0/1). Phase-2 recomputes a blended composite once the judge verdict
exists; the blend weights live in :data:`CLOSENESS_WEIGHTS` so both phases agree.
"""
from __future__ import annotations

from typing import Optional

from bakeoff.quality.types import GroundTruthKind, TurnCloseness
from bakeoff.scoring.answerability import score_answerability
from bakeoff.scoring.semantic import SemanticSimilarityScorer

__all__ = [
    "CLOSENESS_WEIGHTS",
    "clip01",
    "blend_closeness",
    "TurnClosenessScorer",
]

#: Transparent weights for the Phase-2 blended closeness composite. Semantic is a
#: cheap judge-independent cross-check; the judge is the richer signal, so it
#: carries more weight — but both are kept and the blend is transparent (the same
#: discipline as the bake-off's composite). Used only when a judge verdict exists;
#: in Phase-1 the composite is the local component alone.
CLOSENESS_WEIGHTS: dict[str, float] = {
    "semantic": 0.35,
    "judge": 0.65,
}


def clip01(x: float) -> float:
    """Clamp ``x`` to ``[0, 1]`` (a negative cosine contributes 0 to closeness)."""
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def blend_closeness(
    semantic: float,
    judge: Optional[float],
    *,
    abstention: Optional[int] = None,
    weights: dict[str, float] = CLOSENESS_WEIGHTS,
) -> float:
    """Transparent closeness composite from the available components.

    * An abstention turn (``abstention`` is not None) IS its abstention score —
      no text-closeness term applies.
    * With a judge verdict present: weighted blend of clamped ``semantic`` and
      ``judge`` over the weight keys present (normalized, so it stays in
      ``[0, 1]`` for any nonnegative weights).
    * With no judge verdict (Phase-1): the composite is the clamped semantic
      alone (the only local closeness signal for gold/wants turns).
    """
    if abstention is not None:
        return float(abstention)
    sem = clip01(semantic)
    if judge is None:
        return sem
    components = {"semantic": sem, "judge": clip01(judge)}
    total_w = 0.0
    acc = 0.0
    for key, w in weights.items():
        if key in components:
            acc += w * components[key]
            total_w += w
    return acc / total_w if total_w > 0.0 else sem


class TurnClosenessScorer:
    """Compute the Phase-1 (local) closeness for one turn.

    Args:
        semantic_scorer: a :class:`SemanticSimilarityScorer`. Inject one built on
            the offline embedder for tests / the offline optimizer, or the real
            Embed v4 client for a real run. Required (no implicit real-Bedrock
            default — the caller chooses the backend explicitly so this module is
            never an accidental network call).
    """

    def __init__(self, semantic_scorer: SemanticSimilarityScorer) -> None:
        self.semantic_scorer = semantic_scorer

    def score_turn(
        self,
        *,
        answer_text: str,
        reference_text: str,
        ground_truth_kind: str,
        answerability: Optional[str],
    ) -> TurnCloseness:
        """Return the Phase-1 :class:`TurnCloseness` for one turn (judge deferred).

        For GOLD/WANTS turns this is the clamped semantic cosine (judge left
        ``None`` for Phase-2). For ABSTENTION turns this is the 0/1
        abstention-correctness, with semantic recorded as 0.0 (not meaningful).
        """
        if ground_truth_kind == GroundTruthKind.ABSTENTION:
            # No correct content to be close to: closeness == abstention correctness.
            scored = score_answerability(answer_text, answerability or "none")
            abstention = int(scored.get("abstention_correct", 0) or 0)
            return TurnCloseness(
                ground_truth_kind=ground_truth_kind,
                semantic=0.0,
                composite=blend_closeness(0.0, None, abstention=abstention),
                judge=None,
                abstention=abstention,
            )

        # GOLD / WANTS: semantic closeness vs the reference. Empty reference (a
        # later turn with no `wants`) is unscoreable -> neutral, flagged 0.0.
        if not reference_text or not answer_text:
            semantic = 0.0
        else:
            semantic = self.semantic_scorer.score(answer_text, reference_text)
        return TurnCloseness(
            ground_truth_kind=ground_truth_kind,
            semantic=semantic,
            composite=blend_closeness(semantic, None),
            judge=None,
            abstention=None,
        )
