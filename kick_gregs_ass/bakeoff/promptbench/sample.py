"""
Load the pinned, deterministic 24-conversation Prompt Bench sample.

The sample is committed at :data:`config.PROMPT_BENCH_SAMPLE_PATH` (see
``data/bakeoff/prompt_bench/sample_24.json``): a fixed list of item ids. The current sample
is 24 SINGLE-turn queries drawn from ``data/synthetic/queries.jsonl`` (the single-turn
source; the optimizer only ever runs the multi-turn ``conversations.jsonl`` items, so these
are inherently held out from it), stratified by answerability. This module resolves those
ids to live :class:`~bakeoff.types.Item` objects, in the pinned order, so a reload always
scores exactly the same 24 in the same positions (the scatter's X axis).
"""
from __future__ import annotations

import dataclasses
import json
from typing import Sequence

from bakeoff import config
from bakeoff.dataset import DatasetLoader
from bakeoff.types import Item, Turn

__all__ = ["load_sample_spec", "load_sample_items", "sample_index_by_item_id"]


def load_sample_spec() -> dict:
    """Return the parsed ``sample_24.json`` spec (item_ids + metadata)."""
    return json.loads(config.PROMPT_BENCH_SAMPLE_PATH.read_text(encoding="utf-8"))


def _as_one_turn(item: Item) -> Item:
    """Present a single-turn item as a 1-turn conversation (it stays single-turn).

    A single-turn item carries its content at the ITEM level (``query`` / ``wants`` /
    ``gold`` / ``answerability``) and has an EMPTY ``turns`` tuple. The quality scoring path
    (:func:`bakeoff.quality.dataset.turn_reference` / ``_turn_judge_inputs``) was built for
    multi-turn items and, on an item with no turns, returns an EMPTY reference — so the judge
    would get no ideal/gold and the score would be degenerate. Giving the item exactly one
    ``Turn`` (built from its item-level fields) makes the turn-0 reference path resolve the
    item's gold/wants while keeping ``is_multi_turn`` False (it is keyed on ``turn_type``, not
    on the presence of turns). Items that already carry turns are returned unchanged.
    """
    if item.turns:
        return item
    turn = Turn(
        turn=1,
        user_utterance=item.query or "",
        momentary_state=getattr(item.cohort, "momentary_state", "neutral") or "neutral",
        answerability=item.answerability,
        wants=item.wants,
        gold=list(item.gold or []),
    )
    return dataclasses.replace(item, turns=(turn,))


def load_sample_items(spec: dict | None = None) -> list[Item]:
    """Resolve the pinned item ids to :class:`Item`\\ s, in order. Raises if any is missing.

    Resolves against the FULL dataset (single-turn ``queries.jsonl`` + multi-turn
    ``conversations.jsonl``) so single-turn ids resolve; the harness's normalization is
    identical to the rest of the study. Single-turn items are presented as a 1-turn
    conversation via :func:`_as_one_turn` so the judge resolves their item-level gold.
    """
    spec = spec or load_sample_spec()
    ids: Sequence[str] = list(spec["item_ids"])
    by_id = {it.item_id: it for it in DatasetLoader().load_items()}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise ValueError(
            f"Prompt Bench sample references {len(missing)} item id(s) not in the dataset: "
            f"{missing}. The pinned sample is stale relative to the dataset."
        )
    return [_as_one_turn(by_id[i]) for i in ids]


def sample_index_by_item_id(spec: dict | None = None) -> dict[str, int]:
    """Map each pinned item id to its 1-based conversation index (the scatter X)."""
    spec = spec or load_sample_spec()
    return {item_id: i + 1 for i, item_id in enumerate(spec["item_ids"])}
