"""
Coverage-ladder rung sampler for the closed-loop prompt optimizer (v2).

The v2 optimizer evaluates a prompt against a *small indicative subset* first and
only escalates to broader coverage once the prompt earns it (successive-halving /
Hyperband-style). The quality of that subset selection is load-bearing: the
between-conversation SD is ~0.228, so a small rung's CI is wide (±0.10 at n=20),
which means small rungs can only *eliminate* clearly-worse prompts, not *select*
between good ones. Getting the subset wrong in a different way — dropping the rare
signal-bearing cohorts — is worse still: it would make a whole failure mode
invisible at that rung.

Observed tuning-slice composition (60 multi-turn items, measured 2026-06-04):

    answerability: full=49, partial=8, none=3
    turn_count:    3-turn=50, 5-turn=10

The abstention signal (Req 14 — the VITAL behavior) lives almost entirely in the
3 ``none`` (must-abstain) and 8 ``partial`` items; topic-bleed lives in the 5-turn
conversations. A naive random 20-item rung could contain **zero** ``none`` items,
making correct-abstention unmeasurable at that rung. So this sampler is:

* **Stratified** by ``(answerability, turn_bucket)`` so every rung preserves the
  indicative mix rather than drifting toward the dominant ``full``/3-turn cell;
* **Rare-stratum-guaranteed** — every non-empty stratum contributes **at least one**
  item to the smallest rung (so ``none`` and ``partial`` are never absent while any
  remain), which is exactly the conscientious-selection the owner asked for;
* **Nested** — rung ``k`` is a strict subset of rung ``k+1`` (and the last rung is
  the full slice). Nesting is what lets escalation *reuse* the work already done at
  the lower rung (the held-constant memoized retrieval + the per-conversation
  verdicts carry straight up) and keeps the score comparison monotonic as coverage
  grows;
* **Deterministic** — seeded ranking so the ladder is reproducible across runs and
  across the two models, and identical for both islands (a prompt is never advantaged
  by a luckier rung).

No new synthetic data is created. The rungs are strict subsets of the existing
held-out tuning slice (``split_items`` ``heldout``); "everything" / the final
reported number remains the reserved validation complement, scored in Phase B
exactly as before.

Sourcing honesty: successive-halving / Hyperband is an external/industry technique,
not Amazon-internal guidance — same posture as the rest of this spec's methodology.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from bakeoff import config
from bakeoff.quality.optimizer import stats
from bakeoff.types import Item

__all__ = [
    "Rung",
    "stratum_key",
    "build_rung_ladder",
]


def _turn_bucket(item: Item) -> str:
    """Bucket an item by conversation length (the topic-bleed-relevant axis).

    The dataset is overwhelmingly 3-turn with a 5-turn minority; longer
    conversations are where topic-bleed/drift failures concentrate (per the
    bake-off analysis). Bucketing on ``>=5`` vs ``<5`` keeps the long-conversation
    cohort represented at every rung without over-fragmenting the strata on a
    near-continuous turn count.
    """
    n = len(item.turns) if item.turns else 1
    return "long" if n >= 5 else "short"


def _answerability(item: Item) -> str:
    """Resolve an item's answerability regime (``full`` | ``partial`` | ``none``).

    Prefers the item-level value and falls back to the cohort axis, mirroring how
    the rest of the quality study reads it. ``none`` items are the must-abstain
    cohort and ``partial`` the answer-what-you-can cohort — the two that carry the
    abstention signal the ladder must not drop.
    """
    return item.answerability or (item.cohort.answerability if item.cohort else "full") or "full"


def stratum_key(item: Item) -> tuple[str, str]:
    """The stratification cell for ``item``: ``(answerability, turn_bucket)``.

    These two axes are the signal-bearing ones for this study — abstention
    (answerability) and topic-bleed (conversation length) — so preserving their
    mix at every rung is what keeps a small rung *indicative* rather than just
    *small*.
    """
    return (_answerability(item), _turn_bucket(item))


def _rank(seed: int, item_id: str) -> str:
    """A deterministic, uniform-ish ranking key for an item within its stratum.

    A seeded SHA-256 over ``"{seed}:{item_id}"`` — stable across runs and across
    models/islands (so neither model nor island gets a luckier rung), and
    independent of input order. Items are sorted ascending by this key within each
    stratum, and rungs take prefixes, which is what makes the ladder nested.
    """
    return hashlib.sha256(f"{seed}:{item_id}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Rung:
    """One rung of the coverage ladder — an indicative subset + its rep count.

    ``items`` is a strict subset of the tuning slice (and ``items`` of rung ``k`` is
    a subset of rung ``k+1``). ``reps`` is how many times each item is run as a
    conversation when scoring at this rung; ``n_conversations`` is therefore
    ``len(items) * reps`` — the sample size the rung's CI is computed from. ``index``
    is the rung's position on the ladder (0 = smallest/cheapest).
    """

    index: int
    items: tuple[Item, ...]
    reps: int

    @property
    def n_items(self) -> int:
        return len(self.items)

    @property
    def n_conversations(self) -> int:
        """Conversations scored at this rung (the CI sample size)."""
        return len(self.items) * self.reps

    def ci_half_width(self, between_conversation_sd: float) -> float:
        """The 95% CI half-width this rung can resolve at a given between-conv SD.

        ``z * sd / sqrt(n_conversations)`` via :func:`stats.ci_half_width`. Lets the
        escalation policy reason about whether a rung can even resolve the
        significance threshold (it cannot at the small rungs — they eliminate, they
        do not select).
        """
        return stats.ci_half_width(between_conversation_sd, self.n_conversations)


def _allocate(sizes_sorted: Sequence[int], strata: dict[tuple[str, str], list[Item]], total: int) -> None:
    """(internal) sanity placeholder — allocation is done inline in build_rung_ladder."""
    raise NotImplementedError  # pragma: no cover


def build_rung_ladder(
    tuning_items: Sequence[Item],
    *,
    sizes: Optional[Sequence[int]] = None,
    reps_per_rung: Optional[Sequence[int]] = None,
    seed: int = config.QUALITY_OPT_SPLIT_SEED,
    stratify: Callable[[Item], tuple[str, str]] = stratum_key,
) -> list[Rung]:
    """Build the nested, stratified, rare-stratum-guaranteed coverage ladder.

    Args:
        tuning_items: the held-out tuning slice (``split_items`` ``heldout``). The
            rungs are strict subsets of this; no new data is created.
        sizes: target item counts per rung, ascending. Defaults to
            ``config.QUALITY_OPT_RUNG_SIZES``. Any size ``>= len(tuning_items)`` (or
            the final rung) is clamped to the full slice, so the top rung is always
            "everything in the tuning slice". Sizes are de-duplicated after clamping
            so the ladder never has two identical rungs.
        reps_per_rung: reps for each rung, ascending (cheap rungs use fewer reps,
            higher rungs more, to tighten the CI where selection actually happens).
            Defaults to ``config.QUALITY_OPT_RUNG_REPS``; the last value is reused
            if there are more rungs than rep entries.
        seed: ranking seed (default the study split seed) so the ladder is
            reproducible and identical across models/islands.
        stratify: the stratification key function (default :func:`stratum_key`).

    Returns:
        A list of :class:`Rung`, ascending by coverage, each a strict subset of the
        next, every rung preserving the ``(answerability, turn_bucket)`` mix and
        including at least one item from every non-empty stratum while items remain.

    Selection algorithm (deterministic):
        1. Partition the tuning slice into strata by ``stratify`` and rank each
           stratum's items by the seeded key (stable, order-independent).
        2. For each target ``size`` (ascending): allocate that many slots across
           strata **proportionally** to stratum size, but give every non-empty
           stratum a floor of 1 (rare-stratum guarantee) until the budget is spent;
           distribute any rounding remainder to the largest strata. Take the first
           ``alloc[stratum]`` ranked items from each stratum.
        3. Because every rung takes a *prefix* of each stratum's fixed ranking and
           allocations are non-decreasing in ``size``, rung ``k`` is automatically a
           subset of rung ``k+1`` (nesting), with the final rung = the whole slice.
    """
    items = list(tuning_items)
    n_total = len(items)
    if n_total == 0:
        return []

    sizes = list(sizes) if sizes is not None else list(config.QUALITY_OPT_RUNG_SIZES)
    reps_per_rung = (
        list(reps_per_rung) if reps_per_rung is not None else list(config.QUALITY_OPT_RUNG_REPS)
    )
    if not sizes:
        sizes = [n_total]

    # Clamp to the slice size, always include the full slice as the top rung, and
    # de-duplicate while preserving ascending order (so no two rungs are identical).
    clamped = sorted({min(max(1, s), n_total) for s in sizes} | {n_total})

    # Partition into strata and rank each stratum deterministically.
    strata: dict[tuple[str, str], list[Item]] = {}
    for it in items:
        strata.setdefault(stratify(it), []).append(it)
    for key in strata:
        strata[key].sort(key=lambda it: _rank(seed, it.item_id))

    ordered_strata = sorted(strata.items(), key=lambda kv: (-len(kv[1]), kv[0]))

    rungs: list[Rung] = []
    for ri, size in enumerate(clamped):
        alloc = _allocate_size(size, ordered_strata, n_total)
        chosen: list[Item] = []
        for key, group in ordered_strata:
            chosen.extend(group[: alloc[key]])
        reps = reps_per_rung[min(ri, len(reps_per_rung) - 1)] if reps_per_rung else 1
        rungs.append(Rung(index=ri, items=tuple(chosen), reps=max(1, int(reps))))

    return rungs


def _allocate_size(
    size: int,
    ordered_strata: Sequence[tuple[tuple[str, str], list[Item]]],
    n_total: int,
) -> dict[tuple[str, str], int]:
    """Allocate ``size`` item-slots across strata: proportional, floor-1, prefix-safe.

    Every non-empty stratum gets at least 1 (the rare-stratum guarantee) until the
    budget runs out, then the remaining budget is distributed proportionally to
    stratum size with the rounding remainder going to the largest strata. Each
    stratum's allocation is capped at its size. Because allocations are
    non-decreasing in ``size`` and rungs take ranked prefixes, this keeps the ladder
    nested.
    """
    alloc: dict[tuple[str, str], int] = {key: 0 for key, _ in ordered_strata}
    if size >= n_total:  # full slice — take everything
        for key, group in ordered_strata:
            alloc[key] = len(group)
        return alloc

    remaining = size
    # 1) floor of 1 per non-empty stratum (rare cohorts never dropped), largest first
    #    so that if the budget is smaller than the number of strata, the biggest
    #    (most representative) cells are covered first.
    for key, group in ordered_strata:
        if remaining <= 0:
            break
        if group:
            alloc[key] = 1
            remaining -= 1

    # 2) proportional top-up of the leftover budget by stratum size.
    if remaining > 0:
        # capacity left per stratum
        caps = {key: len(group) - alloc[key] for key, group in ordered_strata}
        capacity_total = sum(caps.values())
        if capacity_total > 0:
            # largest-remainder apportionment over remaining capacity
            raw = {
                key: (caps[key] / capacity_total) * remaining for key, _ in ordered_strata
            }
            floor = {key: int(raw[key]) for key in raw}
            for key in floor:
                add = min(floor[key], caps[key])
                alloc[key] += add
                remaining -= add
                caps[key] -= add
            # distribute the rounding remainder to the largest-capacity strata
            if remaining > 0:
                for key, _ in sorted(ordered_strata, key=lambda kv: -caps[kv[0]]):
                    if remaining <= 0:
                        break
                    if caps[key] > 0:
                        alloc[key] += 1
                        caps[key] -= 1
                        remaining -= 1
    return alloc
