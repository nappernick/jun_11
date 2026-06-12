"""
The multi-turn quality run (Phase-1 generation + local closeness).

Generates, for each (model, item, rep), the model's answer to every turn of a
multi-turn item — conversationally, with the model's own prior answers fed
forward (the load-bearing decision: errors compound, as in production) — through
the prompt variant the optimizer chose, scores each turn's Phase-1 (local)
closeness, and appends one :class:`bakeoff.quality.types.QualityOutcome` per
trial to the quality outcomes store.

It mirrors the bake-off's two-phase + durability discipline:

* **Two stores, by type.** Successful trials (the decision data) go to
  ``QUALITY_OUTCOMES_PATH``; failed attempts (disposable) go to
  ``QUALITY_RUN_ERRORS_PATH``. They never mix.
* **Deferred judge.** Phase-1 records only the local closeness (semantic +
  turn-1 abstention); the per-turn judge runs as Phase-2
  (:mod:`bakeoff.quality.judge`) over the recorded outcomes and enriches them by
  ``trial_id``. So the only Bedrock surface in this hot loop is the candidate
  models themselves (retrieval is not even needed — see below).
* **Resumable.** The trial id is a deterministic hash of (model, item, rep); a
  re-run skips trial ids already present in the outcomes store.
* **Durable.** Each outcome is a single fsync'd JSONL line.

Retrieval note: this study measures *answer closeness*, and the dataset's
multi-turn gold/wants are the ground truth — the retrieval substrate is not part
of what we are comparing here. To keep the run self-contained and free of any
dependency on the bake-off's retrieval backend (which is busy serving the
converse run), the quality run passes an EMPTY fragment list to the adapter by
default: the models answer from the conversation + their training, and closeness
is measured against gold/wants. A caller that wants grounded answers can inject a
``fragments_provider``; the default is "no fragments" so the two studies stay
fully decoupled. This is an explicit, recorded design choice, not an oversight.

Backend-agnostic, exactly like the optimizer: it takes an
``adapter_factory(model_key, instruction, item_lookup) -> ModelAdapter``. Tests
pass the offline factory (zero network); a live run passes the Bedrock factory.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional, Sequence

from bakeoff import config
from bakeoff.ids import trial_id as _trial_id
from bakeoff.quality.closeness import TurnClosenessScorer
from bakeoff.quality.dataset import load_multi_turn_items, turn_reference
from bakeoff.quality.types import (
    QualityOutcome,
    TurnOutcome,
    append_outcome,
    read_outcomes,
)
from bakeoff.types import Item

__all__ = [
    "QualityRunResult",
    "QualityAdapterFactory",
    "FragmentsProvider",
    "run_quality",
    "resume_point",
]

#: Builds the run adapter for one model from its chosen instruction. Mirrors the
#: optimizer's factory but keyed by the final chosen instruction string.
QualityAdapterFactory = Callable[[str, str, dict[str, Item]], object]

#: Optional provider of retrieved fragments for an item (default: none). Async so
#: a live caller could re-use the retrieval client; the default returns ``[]``.
FragmentsProvider = Callable[[Item], Awaitable[Sequence[dict]]]

#: The plan_version stamped into the quality trial id (keeps quality trial ids in
#: their own namespace, distinct from any bake-off plan).
QUALITY_PLAN_VERSION: str = "quality-v1"
#: pass_name component of the quality trial id.
QUALITY_PASS_NAME: str = "quality"


class QualityRunResult:
    """Summary of one quality-run invocation (for the CLI / API / tests)."""

    __slots__ = ("planned", "generated", "skipped_existing", "errored", "outcomes_path", "by_model")

    def __init__(self) -> None:
        self.planned = 0
        self.generated = 0
        self.skipped_existing = 0
        self.errored = 0
        self.outcomes_path = config.QUALITY_OUTCOMES_PATH
        self.by_model: dict[str, int] = {}

    def to_dict(self) -> dict:
        return {
            "planned": self.planned,
            "generated": self.generated,
            "skipped_existing": self.skipped_existing,
            "errored": self.errored,
            "outcomes_path": str(self.outcomes_path),
            "by_model": dict(self.by_model),
        }


def resume_point(outcomes_path=config.QUALITY_OUTCOMES_PATH) -> set[str]:
    """Return the set of trial ids already durable + successful in the store.

    Only error-free outcomes count as done, so a previously-errored trial is
    retried on resume, and a fully-successful store makes a re-run a no-op.
    """
    return {o.trial_id for o in read_outcomes(outcomes_path) if o.error is None}


async def _no_fragments(_item: Item) -> Sequence[dict]:
    return []


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def run_quality(
    *,
    adapter_factory: QualityAdapterFactory,
    closeness_scorer: TurnClosenessScorer,
    chosen_instructions: dict[str, str],
    model_keys: Optional[Sequence[str]] = None,
    items: Optional[Sequence[Item]] = None,
    reps: int = config.QUALITY_RUN_REPS,
    outcomes_path=config.QUALITY_OUTCOMES_PATH,
    errors_path=config.QUALITY_RUN_ERRORS_PATH,
    fragments_provider: Optional[FragmentsProvider] = None,
    max_concurrency: Optional[int] = None,
    progress: Optional[Callable[[QualityOutcome], None]] = None,
    chosen_variant_ids: Optional[dict[str, str]] = None,
) -> QualityRunResult:
    """Run the multi-turn quality generation over all target models + items.

    For each (model, item, rep) not already done: generate every turn
    conversationally through the model's chosen instruction, score each turn's
    Phase-1 closeness, and append a :class:`QualityOutcome`. Resumable + durable.

    Args:
        adapter_factory: builds the run adapter for ``(model_key, instruction,
            item_lookup)``. Offline factory for tests, Bedrock factory for a live
            run.
        closeness_scorer: the Phase-1 closeness scorer (local components).
        chosen_instructions: ``model_key -> instruction`` (from the optimizer's
            recorded decision). A model missing here is skipped with a clear count.
        reps: reps per (model, item).
        fragments_provider: optional retrieved-fragments provider (default: none —
            the two studies stay decoupled; see the module docstring).
        max_concurrency: cap on concurrent generations (default model cap).
        chosen_variant_ids: optional ``model_key -> variant_id`` stamped onto each
            outcome for provenance (the instruction is the source of truth; the id
            is informational). Defaults to ``"chosen"`` when absent.
    """
    keys = list(model_keys) if model_keys is not None else list(config.QUALITY_MODELS)
    all_items = list(items) if items is not None else load_multi_turn_items()
    item_lookup = {it.item_id: it for it in all_items}
    frags = fragments_provider or _no_fragments
    variant_ids = chosen_variant_ids or {}

    already = resume_point(outcomes_path)
    cap = max_concurrency if max_concurrency is not None else config.CONCURRENCY_CAPS["model"]
    sem = asyncio.Semaphore(max(1, cap))
    write_lock = asyncio.Lock()
    result = QualityRunResult()
    result.outcomes_path = outcomes_path

    # Build one adapter per model (instruction is fixed per model for the run).
    adapters: dict[str, object] = {}
    for model_key in keys:
        instruction = chosen_instructions.get(model_key)
        if instruction is None:
            continue
        adapters[model_key] = adapter_factory(model_key, instruction, item_lookup)

    async def run_one(model_key: str, adapter: object, item: Item, rep: int) -> None:
        tid = _trial_id(model_key, item.item_id, rep, QUALITY_PASS_NAME, QUALITY_PLAN_VERSION)
        if tid in already:
            result.skipped_existing += 1
            return
        result.planned += 1
        started = _now_iso()
        try:
            fragments = list(await frags(item))
            async with sem:
                resp = await adapter.generate(item, fragments, config.DEFAULT_TEMPERATURE)
            answers = resp.per_turn_answers or [resp.text]
            turn_outcomes: list[TurnOutcome] = []
            for ti, ans in enumerate(answers):
                kind, ref = turn_reference(item, ti)
                turn = item.turns[ti] if ti < len(item.turns) else None
                answerability = turn.answerability if turn is not None else None
                closeness = closeness_scorer.score_turn(
                    answer_text=ans,
                    reference_text=ref,
                    ground_truth_kind=kind,
                    answerability=answerability,
                )
                turn_outcomes.append(
                    TurnOutcome(
                        turn=ti + 1,
                        answerability=answerability,
                        response_dependent=bool(turn.response_dependent) if turn else False,
                        answer_text=ans,
                        reference_text=ref,
                        closeness=closeness,
                    )
                )
            outcome = QualityOutcome(
                trial_id=tid,
                model=model_key,
                item_id=item.item_id,
                rep=rep,
                turn_count=len(turn_outcomes),
                prompt_variant_id=variant_ids.get(model_key, "chosen"),
                turns=tuple(turn_outcomes),
                started_at=started,
                completed_at=_now_iso(),
                error=None,
            )
            async with write_lock:
                append_outcome(outcomes_path, outcome)
                result.generated += 1
                result.by_model[model_key] = result.by_model.get(model_key, 0) + 1
                if progress is not None:
                    progress(outcome)
        except Exception as exc:  # noqa: BLE001 - record the failed attempt, continue
            err_outcome = QualityOutcome(
                trial_id=tid,
                model=model_key,
                item_id=item.item_id,
                rep=rep,
                turn_count=0,
                prompt_variant_id=variant_ids.get(model_key, "chosen"),
                turns=(),
                started_at=started,
                completed_at=_now_iso(),
                error=repr(exc),
            )
            async with write_lock:
                append_outcome(errors_path, err_outcome)
                result.errored += 1

    tasks = [
        run_one(model_key, adapters[model_key], item, rep)
        for model_key in keys
        if model_key in adapters
        for item in all_items
        for rep in range(max(1, reps))
    ]
    if tasks:
        await asyncio.gather(*tasks)
    return result
