"""
Scoring pipeline for the model-bakeoff-harness (design "Component 5",
Requirement 4). Each quality dimension is an **independent, individually-cached
scorer** (design AD-5) so one scorer can be re-run (e.g. swap the judge) without
re-running models or the other scorers.

Layers (design "Quality measurement"):

* **Layer A — retrieval-aligned accuracy** (:mod:`bakeoff.scoring.retrieval_aligned`):
  precision@k / recall@k / MRR / nDCG@k of the constant ``/retrieve`` ranking vs
  ``gold_node_ids`` (substrate ceiling, context only) **and** answer-grounding
  precision/recall (the model differentiator). Pure CPU math, no network.
* **Layer B — semantic similarity** (:mod:`bakeoff.scoring.semantic`): cosine of
  Embed v4 vectors of the model answer vs the ideal response, with a content-hash
  embedding cache so repeats make zero extra Bedrock calls.
* **Layer C — LLM-as-judge** (:mod:`bakeoff.scoring.judge`): anchored rubric,
  k samples per answer with per-dimension mean + measured SD, position/order
  debiasing, fixed judge != candidate, content-hash cached, injectable backend
  (resilient Bedrock by default; deterministic :class:`~bakeoff.scoring.judge.StubJudge`
  for fully-offline runs).
* **Answerability** (:mod:`bakeoff.scoring.answerability`): the first-class
  abstention dimension (none→abstention_correct, partial→answer-and-flag,
  full→unwarranted_refusal), never blended into accuracy.
* **Pipeline** (:mod:`bakeoff.scoring.pipeline`): composes Layers A/B/C +
  answerability into one :class:`~bakeoff.types.QualityScores` with a transparent,
  plan-overridable weighted composite (alongside the components).

Task 6 implements Layers A and B; Task 7 implements Layer C, answerability, and
the composing pipeline.
"""
