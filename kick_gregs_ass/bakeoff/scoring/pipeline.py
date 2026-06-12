"""
Scoring pipeline — composes the layered scorers into one QualityScores (Task 7).

:class:`ScoringPipeline.score_trial` runs all four scorers for one trial and folds
their outputs into a single :class:`bakeoff.types.QualityScores` (design "Component
5", Req 4):

* **Layer A — retrieval-aligned** (:mod:`bakeoff.scoring.retrieval_aligned`):
  precision@k/recall@k/MRR/nDCG@k of the constant ``/retrieve`` ranking vs gold
  (substrate ceiling, context) + answer-grounding precision/recall (model
  differentiator).
* **Layer B — semantic similarity** (:mod:`bakeoff.scoring.semantic`): cosine of the
  answer vs the ideal response, content-hash cached.
* **Layer C — judge** (:mod:`bakeoff.scoring.judge`): k anchored, position-debiased
  samples → per-dimension mean + SD, content-hash cached.
* **Answerability** (:mod:`bakeoff.scoring.answerability`): the first-class
  abstention dimension — ``abstention_correct`` for ``none``/``partial`` and
  ``unwarranted_refusal`` for ``full`` — **never blended into accuracy**.

The composite is a **transparent weighted blend** whose weights come from
``config.COMPOSITE_WEIGHTS`` by default and can be **overridden per call/plan**
(``weights=...``); the weight-set identity is recorded on
``QualityScores.composite_weights_version`` so the exec discussion can re-weight live
and every composite traces to its weights (Req 4.6). The component scores are always
retained alongside the composite (never instead of it).

**Offline construction (the demo seam).** The default pipeline uses the resilient
Bedrock judge + the real Embed v4 substrate. :meth:`ScoringPipeline.offline` builds a
fully network-free pipeline — :class:`bakeoff.scoring.judge.StubJudge` + an injected
fake ``embed_fn`` + (optionally) a supplied mock retrieval result — so the whole
harness can be watched end-to-end with **zero Bedrock calls**.
"""
from __future__ import annotations

from typing import Callable, Optional, Sequence

from bakeoff import config
from bakeoff.scoring.answerability import score_answerability
from bakeoff.scoring.judge import JudgeScorer, make_stub_judge
from bakeoff.scoring.retrieval_aligned import RetrievalAlignedScorer
from bakeoff.scoring.semantic import (
    SemanticSimilarityScorer,
    ideal_response_text,
)
from bakeoff.types import (
    AccuracyScores,
    GoldFragment,
    Item,
    JudgeScores,
    ModelResponse,
    QualityScores,
)

__all__ = ["compute_composite", "ScoringPipeline", "DEFERRED_JUDGE_MODEL"]

#: Sentinel ``judge_model`` stamped on a trial whose judge scoring was DEFERRED
#: (Phase 1 generation run). Lets Phase 2 and the dashboard distinguish
#: "not yet judged" from a real judge verdict, and lets the deferred-judge pass
#: select exactly the trials that still need an Opus score.
DEFERRED_JUDGE_MODEL: str = "(deferred)"


def _neutral_judge_scores() -> JudgeScores:
    """A schema-valid, zero-valued :class:`JudgeScores` for an unjudged trial.

    Every judge dimension is 0.0 and ``judge_model`` is :data:`DEFERRED_JUDGE_MODEL`,
    so a Phase-1 outcome carries a well-formed quality object (it serializes and
    aggregates) while being unambiguously flagged as awaiting the Phase-2 judge.
    The 0.0 judge dimensions simply do not contribute to any composite until
    Phase 2 enriches the trial — composites in Phase 1 are computed from the local
    components only (see the run script's weighting).
    """
    return JudgeScores(
        faithfulness=0.0,
        correctness=0.0,
        completeness=0.0,
        judge_sample_count=0,
        judge_model=DEFERRED_JUDGE_MODEL,
        judge_dim_sd={},
    )


def compute_composite(
    accuracy: AccuracyScores,
    judge: JudgeScores,
    weights: dict[str, float],
) -> float:
    """Transparent weighted composite of the quality components (Req 4.6).

    Maps each weight key onto a component score in ``[0, 1]``:

    * ``grounding`` → mean of grounding precision & recall (the model differentiator);
    * ``semantic_similarity`` → the raw cosine **clamped to ``[0, 1]``** (a negative
      cosine contributes 0 to a quality blend rather than a negative term);
    * ``faithfulness``/``correctness``/``completeness`` and ``tone``/``empathy``/
      ``clarity``/``actionability`` → the judge means (already ``[0, 1]``).

    The blend is ``Σ wᵢ·componentᵢ / Σ wᵢ`` over the keys present, so it stays in
    ``[0, 1]`` for any nonnegative weights (whether or not they sum to 1) and an
    unknown weight key is ignored rather than silently treated as zero-valued. Pure
    and deterministic. Returns 0.0 if the weights sum to 0.
    """
    components: dict[str, float] = {
        "grounding": (accuracy.grounding_precision + accuracy.grounding_recall) / 2.0,
        "semantic_similarity": _clip01(accuracy.semantic_similarity),
        "faithfulness": judge.faithfulness,
        "correctness": judge.correctness,
        "completeness": judge.completeness,
    }
    total_w = 0.0
    acc = 0.0
    for key, w in weights.items():
        if key in components:
            acc += w * components[key]
            total_w += w
    if total_w <= 0.0:
        return 0.0
    return acc / total_w


def _clip01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


class ScoringPipeline:
    """Compose Layer A/B/C + answerability into one :class:`QualityScores`.

    Args:
        retrieval_scorer: Layer-A scorer (default :class:`RetrievalAlignedScorer`).
        semantic_scorer: Layer-B scorer (default builds a real Embed v4 client —
            pass one with an injected ``embed_fn`` for offline use).
        judge_scorer: Layer-C scorer (default builds the resilient Bedrock judge —
            pass one wrapping :class:`StubJudge` for offline use).
        weights: composite weights (default ``config.COMPOSITE_WEIGHTS``); the plan
            may override.
        weights_version: identity stamped onto ``QualityScores.composite_weights_version``
            (default ``config.COMPOSITE_WEIGHTS_VERSION``).
        k: ranking cutoff for Layer A (default ``config.SCORING_K``).
    """

    def __init__(
        self,
        *,
        retrieval_scorer: Optional[RetrievalAlignedScorer] = None,
        semantic_scorer: Optional[SemanticSimilarityScorer] = None,
        judge_scorer: Optional[JudgeScorer] = None,
        weights: Optional[dict[str, float]] = None,
        weights_version: Optional[str] = None,
        k: Optional[int] = None,
        judge_enabled: bool = True,
        semantic_enabled: bool = True,
    ) -> None:
        self.k = k if k is not None else config.SCORING_K
        self.retrieval_scorer = retrieval_scorer or RetrievalAlignedScorer(k=self.k)
        # Phase-1 (the full generation run) sets judge_enabled=semantic_enabled=
        # False so the ONLY Bedrock surface in the hot loop is the candidates +
        # the held-constant retrieval. The judge (Opus, slow/TPM-limited) and the
        # Embed-v4 semantic scorer are deferred to Phase 2 over a sampled subset.
        # When disabled, the corresponding scorer is never constructed (so no
        # boto3 client is built) and its quality components are left neutral.
        self.judge_enabled = judge_enabled
        self.semantic_enabled = semantic_enabled
        self.semantic_scorer = (
            semantic_scorer
            if semantic_scorer is not None
            else (SemanticSimilarityScorer() if semantic_enabled else None)
        )
        self.judge_scorer = (
            judge_scorer
            if judge_scorer is not None
            else (JudgeScorer() if judge_enabled else None)
        )
        self.weights = dict(weights) if weights is not None else dict(config.COMPOSITE_WEIGHTS)
        self.weights_version = (
            weights_version if weights_version is not None else config.COMPOSITE_WEIGHTS_VERSION
        )

    # -- generation-phase construction (Phase 1: no Bedrock scorers) ------
    @classmethod
    def generation_phase(
        cls,
        *,
        weights: Optional[dict[str, float]] = None,
        weights_version: Optional[str] = None,
        k: Optional[int] = None,
    ) -> "ScoringPipeline":
        """Build a Phase-1 pipeline: local scorers only, NO judge, NO semantic.

        This is the pipeline the full 23k generation run uses. It computes only
        the pure-CPU, deterministic scorers (retrieval-aligned ranking metrics +
        the answerability heuristic) and leaves the judge and semantic-similarity
        components neutral. Consequently the ONLY Bedrock calls in the generation
        hot loop come from the candidate adapters and the held-constant retrieval
        substrate — the judge (Opus, slow/TPM-limited) and Embed-v4 semantic
        scorer are deferred to Phase 2 over a sampled subset (so the grader can
        never stall or sabotage candidate-data collection).
        """
        return cls(
            judge_enabled=False,
            semantic_enabled=False,
            weights=weights,
            weights_version=weights_version,
            k=k,
        )

    # -- offline / all-mock construction (the demo seam) ------------------
    @classmethod
    def offline(
        cls,
        *,
        embed_fn: Optional[Callable[[Sequence[str], str], list]] = None,
        judge_spread: float = 0.04,
        weights: Optional[dict[str, float]] = None,
        weights_version: Optional[str] = None,
        k: Optional[int] = None,
        judge_k: Optional[int] = None,
        cache_dir: Optional[str] = None,
        disk_cache: bool = False,
    ) -> "ScoringPipeline":
        """Build a fully network-free pipeline (stub judge + injected embedder).

        Everything that would otherwise touch Bedrock is replaced:

        * the judge uses :class:`bakeoff.scoring.judge.StubJudge` (deterministic,
          content-derived, abstention-aware scores; zero network);
        * the semantic scorer uses the supplied ``embed_fn`` (a deterministic fake);
          if none is given, a built-in deterministic hashing embedder is used so the
          pipeline is offline out of the box.

        Combined with a mock retrieval result and the :class:`MockAdapter`, this lets
        the whole harness be watched end-to-end with no Bedrock calls.
        """
        embed = embed_fn or _make_fake_embed_fn()
        semantic = SemanticSimilarityScorer(embed_fn=embed, disk_cache=disk_cache, cache_dir=cache_dir)
        judge = JudgeScorer(
            backend=make_stub_judge(spread=judge_spread),
            k=judge_k,
            disk_cache=disk_cache,
            cache_dir=cache_dir,
        )
        return cls(
            semantic_scorer=semantic,
            judge_scorer=judge,
            weights=weights,
            weights_version=weights_version,
            k=k,
        )

    # -- the composition ---------------------------------------------------
    def score_trial(
        self,
        item: Item,
        gold: Sequence[GoldFragment],
        fragments: Sequence[dict],
        response: ModelResponse,
        *,
        weights: Optional[dict[str, float]] = None,
        weights_version: Optional[str] = None,
    ) -> QualityScores:
        """Score one trial into a :class:`QualityScores` (components + composite).

        ``weights``/``weights_version`` override the pipeline defaults for this call
        (so a plan can re-weight without rebuilding the pipeline). The composite is
        always carried alongside its components.
        """
        answer_text = response.text or ""
        ranked_ids = [str(f.get("id")) for f in fragments]
        gold_ids = list(item.gold_node_ids)
        momentary_state = item.cohort.momentary_state
        answerability = item.answerability or item.cohort.answerability

        # --- Layer A: retrieval-aligned (pure CPU) -----------------------
        ra = self.retrieval_scorer.score(ranked_ids, gold_ids, answer_text, list(fragments))

        # --- Layer B: semantic similarity vs the ideal response ----------
        # Deferred in Phase 1 (semantic_enabled=False): leave neutral (0.0) so no
        # Embed-v4 Bedrock call happens in the generation loop. Phase 2 fills it.
        ideal_text = ideal_response_text(gold, item.wants)
        if self.semantic_scorer is not None:
            semantic_similarity = self.semantic_scorer.score(answer_text, ideal_text)
        else:
            semantic_similarity = 0.0

        # --- Answerability: the first-class abstention dimension ---------
        ans = score_answerability(answer_text, answerability)
        abstention_correct = ans.get("abstention_correct")
        unwarranted_refusal = ans.get("unwarranted_refusal")

        accuracy = AccuracyScores(
            precision_at_k=ra["precision_at_k"],
            recall_at_k=ra["recall_at_k"],
            mrr=ra["mrr"],
            ndcg_at_k=ra["ndcg_at_k"],
            grounding_precision=ra["grounding_precision"],
            grounding_recall=ra["grounding_recall"],
            semantic_similarity=semantic_similarity,
            abstention_correct=abstention_correct,
            unwarranted_refusal=unwarranted_refusal,
        )

        # --- Layer C: judge (k samples, mean+SD, debiased, cached) -------
        # Deferred in Phase 1 (judge_enabled=False): emit a NEUTRAL JudgeScores so
        # no Opus call happens in the generation loop. The neutral judge is marked
        # judge_model="(deferred)" so Phase 2 / aggregation can tell "not yet
        # judged" apart from a real score, and judge dimensions are 0.0 (they
        # simply do not contribute until Phase 2 enriches this trial by trial_id).
        if self.judge_scorer is not None:
            gold_texts = [
                g.markdown or g.snippet or g.title
                for g in gold
                if (g.markdown or g.snippet or g.title)
            ]
            judge = self.judge_scorer.score(
                answer_text,
                ideal_text=ideal_text,
                fragments=list(fragments),
                gold_texts=gold_texts,
                momentary_state=momentary_state,
                answerability=answerability,
            )
        else:
            judge = _neutral_judge_scores()

        # --- transparent weighted composite -----------------------------
        # Per-call overrides win; otherwise the pipeline defaults are used. If
        # custom weights are supplied without a version, the pipeline's version
        # label is recorded (callers re-weighting live should pass both).
        eff_weights = weights if weights is not None else self.weights
        eff_version = weights_version if weights_version is not None else self.weights_version
        composite = compute_composite(accuracy, judge, eff_weights)

        return QualityScores(
            accuracy=accuracy,
            judge=judge,
            composite=composite,
            composite_weights_version=eff_version,
        )


# ---------------------------------------------------------------------------
# Built-in deterministic fake embedder for fully-offline construction
# ---------------------------------------------------------------------------
def _make_fake_embed_fn(dims: int = 32) -> Callable[[Sequence[str], str], list]:
    """A deterministic, network-free ``embed_fn`` for offline pipeline construction.

    Hashes each text into a fixed-dimension nonnegative vector. Identical text →
    identical vector (cosine 1.0); texts sharing content words have higher cosine
    than unrelated texts — enough structure for the semantic cross-check to behave
    sensibly offline without any model. Not a quality embedder; it exists so the
    pipeline runs end-to-end with zero network.
    """
    import hashlib
    import re

    word = re.compile(r"[A-Za-z0-9]+")

    def embed(texts: Sequence[str], input_type: str) -> list:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * dims
            toks = word.findall((text or "").lower()) or ["\x00"]
            for tok in toks:
                d = hashlib.sha256(tok.encode("utf-8")).digest()
                idx = d[0] % dims
                vec[idx] += 1.0
            out.append(vec)
        return out

    return embed
