"""
Dataset selection + per-turn reference resolution for the quality study.

Two jobs:

1. **Select the multi-turn items.** The study runs only the multi-turn items
   (300 of them — 250 three-turn + 50 five-turn) loaded by the existing
   :class:`bakeoff.dataset.DatasetLoader`. Single-turn items are out of scope.

2. **Resolve each turn's ground-truth reference.** Per the dataset's shape
   (verified on disk): turn-1 carries gold fragments + an answerability label;
   later turns carry only ``wants``. :func:`turn_reference` returns, for a given
   turn, the ``(kind, reference_text)`` the closeness scorer measures against:

   * turn-1, answerability ``none`` → ``(ABSTENTION, "")`` (scored as
     abstention-correctness; there is no correct content to be close to);
   * turn-1, otherwise → ``(GOLD, ideal_response_text(gold, wants))`` (the
     gold-derived ideal, reusing the bake-off's ideal assembly so the two studies
     define "ideal" identically);
   * later turn of an answerable conversation → ``(WANTS, turn.wants)`` (the only
     ground truth it carries; a later turn with no ``wants`` degrades to an empty
     reference, which the scorer treats as unscoreable rather than crashing);
   * later turn of an UNANSWERABLE conversation (item answerability ``none``) →
     ``(ABSTENTION, "")`` — the conversation is out-of-domain throughout, so the
     correct behavior is to decline; scoring it as WANTS would penalize a correct
     decline against a ``wants`` the model should not answer.

The held-out split (:func:`split_items`) deterministically partitions the items
into the optimizer's tuning slice and the remainder, seeded so it is
reproducible. The prompt is tuned on the held-out slice and the FULL set is run
with the chosen prompt, so the reported quality is not measured on the same data
the prompt was tuned on (a standard guard against overfitting the prompt to its
own eval).
"""
from __future__ import annotations

import hashlib
from typing import Optional, Sequence

from bakeoff import config
from bakeoff.dataset import DatasetLoader
from bakeoff.quality.types import GroundTruthKind
from bakeoff.scoring.semantic import ideal_response_text
from bakeoff.types import Item, Turn

__all__ = [
    "load_multi_turn_items",
    "turn_reference",
    "split_items",
]


def load_multi_turn_items(loader: Optional[DatasetLoader] = None) -> list[Item]:
    """Return the multi-turn items the quality study runs against.

    Uses the shared :class:`bakeoff.dataset.DatasetLoader` (so cohort/gold
    normalization is identical to the bake-off) and filters to ``is_multi_turn``.
    Deterministic order (the loader's order) so downstream splits are stable.
    """
    ldr = loader or DatasetLoader()
    return [it for it in ldr.load_items() if it.is_multi_turn]


def turn_reference(item: Item, turn_index: int) -> tuple[str, str]:
    """Return ``(ground_truth_kind, reference_text)`` for a turn (0-based index).

    The reference is what the closeness scorer measures the model's answer
    against. See the module docstring for the three regimes. ``reference_text`` is
    the empty string for the abstention regime (closeness there is abstention
    correctness, not text similarity) and for a later turn that carries no
    ``wants`` (unscoreable — the scorer returns a neutral, flagged result).

    Later turns of an UNANSWERABLE conversation (item answerability ``none``) are
    scored via the ABSTENTION regime rather than WANTS: the conversation is
    out-of-domain throughout, so the correct later-turn behavior is to decline,
    and abstention-scoring rewards that instead of penalizing it against a
    ``wants`` the model should not satisfy.
    """
    turns: Sequence[Turn] = item.turns
    if not turns:
        # Defensive: a "multi" item with no turns has nothing to score.
        return GroundTruthKind.WANTS, ""

    idx = max(0, min(turn_index, len(turns) - 1))
    turn = turns[idx]

    if idx == 0:
        # Turn 1: gold-anchored, unless the item is unanswerable (abstention).
        answerability = item.answerability or item.cohort.answerability
        if answerability == "none":
            return GroundTruthKind.ABSTENTION, ""
        ideal = ideal_response_text(item.gold, item.wants)
        return GroundTruthKind.GOLD, ideal

    # Later turn. In an UNANSWERABLE conversation (item answerability "none") the
    # whole conversation is out-of-domain, so a correct later-turn behavior is to
    # decline — score it via the ABSTENTION regime so a correct decline earns
    # abstention credit instead of being measured against a `wants` it should not
    # answer. (Per-later-turn answerability is `None` by construction in
    # `_build_multi_turn`, so the conversation-level label on `item` is the only
    # signal available; we consult it here — the single site that decides a turn's
    # ground-truth regime — rather than mutating every later ItemTurn, so the fix
    # lives in one place and flows to every consumer via `ground_truth_kind`.)
    # Answerable conversations' later turns stay WANTS-scored, unchanged.
    if (item.answerability or item.cohort.answerability) == "none":
        return GroundTruthKind.ABSTENTION, ""

    # Otherwise only `wants` is available as ground truth.
    return GroundTruthKind.WANTS, (turn.wants or "")


def split_items(
    items: Sequence[Item],
    *,
    heldout_fraction: float = config.QUALITY_OPTIMIZER_HELDOUT_FRACTION,
    seed: int = config.QUALITY_OPTIMIZER_SPLIT_SEED,
) -> tuple[list[Item], list[Item]]:
    """Deterministically split ``items`` into ``(heldout, remainder)``.

    The held-out slice is the optimizer's tuning set; the prompt chosen on it is
    then run on ALL items (the remainder plus the held-out, since the final run
    measures the chosen prompt on the full set). The split is by a seeded hash of
    the item id, so it is reproducible and independent of item order, and it is
    *stratified by turn count* (3-turn vs 5-turn) so the tuning slice keeps the
    same multi-turn-length mix as the full set.

    ``heldout_fraction`` is clamped to ``[0, 1]``. With fraction 0 the held-out
    slice is empty (the optimizer would have nothing to tune on — callers guard
    against that); with fraction 1 every item is held out.
    """
    frac = min(1.0, max(0.0, heldout_fraction))

    # Stratify by turn count so the tuning slice mirrors the 3-turn/5-turn mix.
    by_len: dict[int, list[Item]] = {}
    for it in items:
        by_len.setdefault(len(it.turns), []).append(it)

    heldout: list[Item] = []
    remainder: list[Item] = []
    for _length, group in sorted(by_len.items()):
        ranked = sorted(group, key=lambda it: _split_rank(seed, it.item_id))
        n_held = int(round(len(ranked) * frac))
        # Keep at least one in each non-empty stratum's tuning slice when frac>0,
        # and never take the whole stratum unless frac==1 (so the run set is not
        # degenerate for that length).
        if frac > 0.0 and n_held == 0 and ranked:
            n_held = 1
        if frac < 1.0 and n_held == len(ranked) and ranked:
            n_held = len(ranked) - 1
        heldout.extend(ranked[:n_held])
        remainder.extend(ranked[n_held:])
    return heldout, remainder


def _split_rank(seed: int, item_id: str) -> str:
    """Stable seeded ordering key for an item within its stratum."""
    return hashlib.sha256(f"{seed}\x1f{item_id}".encode("utf-8")).hexdigest()
