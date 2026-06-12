"""
The multi-turn **quality** study (codename GBBO's second study).

A self-contained experiment, deliberately separate from the speed/quality
bake-off: it takes the multi-turn dataset and measures, *per turn*, how close
each model's answer is to the correct answer — turn-1 against the gold fragments
(or abstention-correctness when turn-1 is answerability ``none``), each later
turn against that turn's ``wants`` (the only ground truth later turns carry).

Three pieces, all importable without touching Bedrock:

* :mod:`bakeoff.quality.types` — the per-turn outcome/closeness value objects +
  their durable JSONL (de)serialization (mirrors :mod:`bakeoff.eventlog`).
* :mod:`bakeoff.quality.dataset` — selects the multi-turn items and splits them
  into the optimizer's held-out tuning slice vs the full run set (seeded).
* :mod:`bakeoff.quality.closeness` — the per-turn closeness scorer (semantic +
  judge + turn-1 abstention), backend-injectable so it runs fully offline.
* :mod:`bakeoff.quality.prompts` — the candidate multi-turn system-prompt
  variants the optimizer ranks, grounded in the same internal guidance as
  :mod:`bakeoff.prompts`.
* :mod:`bakeoff.quality.optimize` — the offline prompt-optimization harness that
  ranks the variants per model on the held-out slice and records the winner.
* :mod:`bakeoff.quality.run` — the multi-turn quality run that generates each
  turn conversationally through the chosen prompt and writes per-turn outcomes.

Conversational feed-forward (the load-bearing decision): each turn is generated
with the model's OWN previous answers in context, so errors compound exactly as
they would in production — the realistic, harder setting. This reuses the
existing :meth:`bakeoff.adapters.bedrock.BedrockModelAdapter.generate` multi-turn
loop unchanged.
"""
from __future__ import annotations

__all__ = []
