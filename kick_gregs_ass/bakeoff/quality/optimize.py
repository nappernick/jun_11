"""
Offline prompt-optimization harness for the quality study.

Goal: for each of the two target models, pick the multi-turn system-prompt
variant (:mod:`bakeoff.quality.prompts`) that maximizes per-turn closeness on the
held-out tuning slice (:func:`bakeoff.quality.dataset.split_items`), then record
the winner + the full scored leaderboard so the subsequent quality run uses a
*recorded decision*, not a re-run of the optimizer.

This is the "iterate on the prompt until you're confident it's the best
reasonable attempt" step. It is structured so that:

* it is **backend-agnostic** — it takes an ``adapter_factory(model_key, variant,
  item_lookup) -> ModelAdapter`` and a closeness scorer, so the exact same harness
  ranks variants offline (deterministic :class:`QualityOfflineAdapter` + offline
  embedder) in tests and ranks them for real (the Bedrock adapter + Embed v4)
  when run live. The offline path makes real Bedrock calls only when wired to the
  real factory;
* the ranking metric is **per-turn closeness averaged the way the study reports
  it** — the mean over turns of each item's composite closeness, then averaged
  over items and reps — so the optimizer optimizes the same number the dashboard
  shows, not a proxy;
* the leaderboard is **interpretable**: each variant row carries its lever set
  and per-turn-position means (turn-1 vs later), so the winner explains *which
  multi-turn levers helped*.

Honesty note: a meaningful "best prompt" requires real model outputs. With the
offline adapter the optimizer's ranking reflects the offline adapter's
synthetic lever→quality mapping (useful for validating the harness), not a real
model. The recorded winner is only authoritative when the optimizer was run with
the real Bedrock factory — :func:`optimize_prompts` records which backend produced
the decision (``backend`` field) so a reader can tell.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

from bakeoff import config
from bakeoff.quality.closeness import TurnClosenessScorer
from bakeoff.quality.dataset import load_multi_turn_items, split_items, turn_reference
from bakeoff.quality.prompts import (
    PromptVariant,
    quality_system_instruction,
    variants_for_model,
)
from bakeoff.types import Item

__all__ = [
    "VariantScore",
    "ModelOptimizationResult",
    "AdapterFactory",
    "score_variant",
    "optimize_model",
    "optimize_prompts",
    "write_optimizer_report",
    "write_chosen_prompts",
    "load_chosen_prompts",
]

#: Builds an adapter for one (model_key, variant) over a known item set. The
#: offline factory returns a QualityOfflineAdapter; the real factory returns a
#: BedrockModelAdapter with instruction_override set to the variant's instruction.
AdapterFactory = Callable[[str, PromptVariant, dict[str, Item]], object]


@dataclass(frozen=True)
class VariantScore:
    """One variant's scored result for one model on the tuning slice."""

    variant_id: str
    levers: tuple[str, ...]
    mean_closeness: float          # the optimization objective (higher is better)
    turn1_mean: float              # mean composite on turn-1 (gold/abstention)
    later_mean: float              # mean composite on later turns (wants)
    n_items: int
    n_turns_scored: int
    instruction: str = ""          # the full instruction this variant sent

    def to_dict(self) -> dict:
        return {
            "variant_id": self.variant_id,
            "levers": list(self.levers),
            "mean_closeness": self.mean_closeness,
            "turn1_mean": self.turn1_mean,
            "later_mean": self.later_mean,
            "n_items": self.n_items,
            "n_turns_scored": self.n_turns_scored,
        }


@dataclass(frozen=True)
class ModelOptimizationResult:
    """The optimizer's outcome for one model: the ranked leaderboard + winner."""

    model_key: str
    family: str
    thinking: bool
    leaderboard: tuple[VariantScore, ...]   # sorted best-first
    chosen_variant_id: str
    chosen_instruction: str

    def to_dict(self) -> dict:
        return {
            "model_key": self.model_key,
            "family": self.family,
            "thinking": self.thinking,
            "chosen_variant_id": self.chosen_variant_id,
            "leaderboard": [v.to_dict() for v in self.leaderboard],
        }


async def score_variant(
    model_key: str,
    variant: PromptVariant,
    items: Sequence[Item],
    *,
    family: str,
    thinking: bool,
    adapter_factory: AdapterFactory,
    closeness_scorer: TurnClosenessScorer,
    reps: int = config.QUALITY_OPTIMIZER_REPS,
    max_concurrency: Optional[int] = None,
) -> VariantScore:
    """Score one variant for one model over ``items`` (the tuning slice).

    For each item × rep, generate all turns conversationally through the variant's
    instruction, score each turn's closeness, and average. The objective is the
    mean over items/reps of each generation's per-turn-mean composite closeness —
    the same quantity the study reports. Turn-1 and later-turn means are tracked
    separately so the leaderboard shows where a lever helped.

    ``max_concurrency`` bounds how many generations run at once. ``None`` (the
    default) runs serially — this keeps the deterministic offline tests' ordering
    stable. A live pass passes the model concurrency cap so the (otherwise serial)
    tuning sweep over the held-out slice actually finishes in bounded wall-clock
    instead of one-Bedrock-call-at-a-time. Closeness scoring is order-independent
    (it aggregates means), so concurrency does not change the result.
    """
    import asyncio

    instruction = quality_system_instruction(
        family=family, thinking_enabled=thinking, variant=variant
    )
    item_lookup = {it.item_id: it for it in items}
    adapter = adapter_factory(model_key, variant, item_lookup)

    per_gen_means: list[float] = []
    turn1_vals: list[float] = []
    later_vals: list[float] = []
    n_turns_scored = 0

    async def _score_one(item: Item) -> None:
        nonlocal n_turns_scored
        resp = await adapter.generate(item, [], config.DEFAULT_TEMPERATURE)
        answers = resp.per_turn_answers or [resp.text]
        comps: list[float] = []
        for ti, ans in enumerate(answers):
            kind, ref = turn_reference(item, ti)
            answerability = (
                item.turns[ti].answerability if ti < len(item.turns) else None
            )
            c = closeness_scorer.score_turn(
                answer_text=ans,
                reference_text=ref,
                ground_truth_kind=kind,
                answerability=answerability,
            )
            comps.append(c.composite)
            n_turns_scored += 1
            if ti == 0:
                turn1_vals.append(c.composite)
            else:
                later_vals.append(c.composite)
        if comps:
            per_gen_means.append(sum(comps) / len(comps))

    jobs = [item for item in items for _rep in range(max(1, reps))]

    if max_concurrency is None or max_concurrency <= 1:
        for item in jobs:
            await _score_one(item)
    else:
        sem = asyncio.Semaphore(max_concurrency)

        async def _guarded(item: Item) -> None:
            async with sem:
                await _score_one(item)

        await asyncio.gather(*(_guarded(item) for item in jobs))

    return VariantScore(
        variant_id=variant.variant_id,
        levers=variant.levers,
        mean_closeness=_mean(per_gen_means),
        turn1_mean=_mean(turn1_vals),
        later_mean=_mean(later_vals),
        n_items=len(items),
        n_turns_scored=n_turns_scored,
        instruction=instruction,
    )


async def optimize_model(
    model_key: str,
    items: Sequence[Item],
    *,
    adapter_factory: AdapterFactory,
    closeness_scorer: TurnClosenessScorer,
    reps: int = config.QUALITY_OPTIMIZER_REPS,
    max_concurrency: Optional[int] = None,
) -> ModelOptimizationResult:
    """Rank every variant for ``model_key`` on ``items`` and pick the best.

    The winner is the variant with the highest ``mean_closeness``; ties break
    toward the SIMPLER variant (fewer levers, then variant_id) so we do not adopt
    extra prompt machinery that did not actually help — the rigor-vs-ceremony
    guard. Returns the full sorted leaderboard for the report.
    """
    spec = config.QUALITY_MODELS[model_key]
    family = str(spec["family"])
    thinking = bool(spec["thinking"])

    scores: list[VariantScore] = []
    for variant in variants_for_model(model_key):
        scores.append(
            await score_variant(
                model_key,
                variant,
                items,
                family=family,
                thinking=thinking,
                adapter_factory=adapter_factory,
                closeness_scorer=closeness_scorer,
                reps=reps,
                max_concurrency=max_concurrency,
            )
        )

    # Best-first: higher closeness wins; ties -> fewer levers -> variant_id.
    leaderboard = tuple(
        sorted(scores, key=lambda s: (-s.mean_closeness, len(s.levers), s.variant_id))
    )
    winner = leaderboard[0]
    return ModelOptimizationResult(
        model_key=model_key,
        family=family,
        thinking=thinking,
        leaderboard=leaderboard,
        chosen_variant_id=winner.variant_id,
        chosen_instruction=winner.instruction,
    )


async def optimize_prompts(
    *,
    adapter_factory: AdapterFactory,
    closeness_scorer: TurnClosenessScorer,
    model_keys: Optional[Sequence[str]] = None,
    items: Optional[Sequence[Item]] = None,
    reps: int = config.QUALITY_OPTIMIZER_REPS,
    backend: str = "unknown",
    max_concurrency: Optional[int] = None,
) -> dict[str, ModelOptimizationResult]:
    """Optimize prompts for every target model on the held-out tuning slice.

    Loads the multi-turn items (unless ``items`` is supplied), splits off the
    held-out tuning slice, and ranks variants per model on it. Returns
    ``model_key -> ModelOptimizationResult``. ``backend`` is recorded in the
    written report so a reader knows whether the decision came from the real
    Bedrock models or the offline double. ``max_concurrency`` bounds concurrent
    generations within each variant sweep (``None`` => serial, for offline tests).
    """
    keys = list(model_keys) if model_keys is not None else list(config.QUALITY_MODELS)
    all_items = list(items) if items is not None else load_multi_turn_items()
    heldout, _remainder = split_items(all_items)
    # Guard: never tune on an empty slice.
    tuning = heldout or all_items

    results: dict[str, ModelOptimizationResult] = {}
    for model_key in keys:
        results[model_key] = await optimize_model(
            model_key,
            tuning,
            adapter_factory=adapter_factory,
            closeness_scorer=closeness_scorer,
            reps=reps,
            max_concurrency=max_concurrency,
        )
    return results


def write_optimizer_report(
    results: dict[str, ModelOptimizationResult],
    *,
    backend: str,
    path=config.QUALITY_OPTIMIZER_REPORT_PATH,
    heldout_n: int = 0,
) -> Path:
    """Write the full scored leaderboard report (JSON) for the dashboard + audit."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "backend": backend,
        "heldout_items": heldout_n,
        "models": {k: r.to_dict() for k, r in results.items()},
    }
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p


def write_chosen_prompts(
    results: dict[str, ModelOptimizationResult],
    *,
    backend: str,
    path=config.QUALITY_PROMPTS_PATH,
) -> Path:
    """Write the chosen prompt per model (the run reads exactly this file).

    Records, per model: the chosen variant id, its full instruction (so the run
    sends precisely what was chosen), its lever set, and the tuning closeness it
    won with — plus the backend that produced the decision, so the run can warn if
    it is about to run real models against a prompt chosen by the offline double.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "backend": backend,
        "models": {
            k: {
                "chosen_variant_id": r.chosen_variant_id,
                "family": r.family,
                "thinking": r.thinking,
                "instruction": r.chosen_instruction,
                "levers": list(
                    next(v.levers for v in r.leaderboard if v.variant_id == r.chosen_variant_id)
                ),
                "tuning_mean_closeness": next(
                    v.mean_closeness for v in r.leaderboard if v.variant_id == r.chosen_variant_id
                ),
            }
            for k, r in results.items()
        },
    }
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p


def load_chosen_prompts(path=config.QUALITY_PROMPTS_PATH) -> dict:
    """Load the chosen-prompts file (``{}`` if absent)."""
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0
