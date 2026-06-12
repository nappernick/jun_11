"""
PromptBenchScorer — Prompt Bench's pipelined, guarded, per-conversation scorer.

A near-copy of :class:`bakeoff.quality.optimizer.v3.scorer.ResilientScorer`, deliberately
duplicated here (rather than imported/subclassed-and-mutated) so the optimizer v3 scoring
path is NEVER touched and a Prompt Bench run can execute concurrently with a live v3 run
without sharing any state.

Two intentional differences from the v3 scorer:

* **Its OWN per-resource semaphore registry** (:data:`_PB_RESOURCE_SEMAPHORES`), so Prompt
  Bench gets its own model/judge concurrency caps instead of sharing the optimizer's
  process-wide pool. With Prompt Bench on a separate Bedrock account, sharing the
  optimizer's caps would needlessly throttle it against v3; a private registry gives it the
  full cap on its own quota.
* It surfaces **every** conversation's overall mean through ``on_conversation_scored`` (the
  live per-conversation point the scatter plots fill in), and contains failures the same
  way v3 does.

It subclasses the SHARED base :class:`~bakeoff.quality.optimizer.judge_loop.JudgeInLoopScorer`
(the same base v2/v3 use — not a v3-specific module) so the per-turn judging, retrieval, and
aggregation are byte-identical to the real scoring path, and reuses the generic
:func:`~bakeoff.quality.optimizer.v3.guards.guarded_call` helper (a pure timeout/retry
wrapper, no shared mutable state).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from bakeoff import config
from bakeoff.quality.optimizer.judge_loop import JudgeInLoopScorer, SliceScore, TurnVerdict
from bakeoff.quality.optimizer.v3.guards import GuardedCallError, guarded_call

__all__ = ["PromptBenchConversationFailure", "PromptBenchIterationSkipped", "PromptBenchScorer"]

_LOG = logging.getLogger("bakeoff.promptbench.scorer")

# ---------------------------------------------------------------------------
# Prompt Bench's PRIVATE per-resource semaphores (per event loop). Separate
# registry from the optimizer's, so Prompt Bench never contends with a live v3
# run for judge/model slots. Keyed by running-loop id so a fresh test loop never
# reuses a semaphore bound to a dead loop.
# ---------------------------------------------------------------------------
_PB_RESOURCE_SEMAPHORES: dict[tuple[int, str, int], asyncio.Semaphore] = {}


def _pb_resource_semaphore(resource: str, cap: int) -> asyncio.Semaphore:
    """Prompt Bench's loop-wide semaphore for ``resource`` at ``cap`` concurrency."""
    bounded_cap = max(1, int(cap))
    key = (id(asyncio.get_running_loop()), resource, bounded_cap)
    semaphore = _PB_RESOURCE_SEMAPHORES.get(key)
    if semaphore is None:
        semaphore = asyncio.Semaphore(bounded_cap)
        _PB_RESOURCE_SEMAPHORES[key] = semaphore
    return semaphore


_TRACEBACK_TAIL_CHARS = 2500


@dataclass(frozen=True)
class PromptBenchConversationFailure:
    """One ``(item, rep)`` conversation that failed scoring after its guards."""

    item_id: str
    rep: int
    stage: str  # "generate" | "judge"
    error: str
    elapsed_s: float = 0.0
    traceback: str = ""

    def to_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "rep": self.rep,
            "stage": self.stage,
            "error": self.error,
            "elapsed_s": round(self.elapsed_s, 1),
            "traceback": self.traceback,
        }


def _failure_from(item_id: str, rep: int, stage: str, exc: BaseException) -> PromptBenchConversationFailure:
    import traceback as _traceback

    if isinstance(exc, GuardedCallError):
        return PromptBenchConversationFailure(
            item_id=item_id, rep=rep, stage=stage,
            error=repr(exc.last_error), elapsed_s=exc.elapsed_s,
            traceback=exc.traceback_text[-_TRACEBACK_TAIL_CHARS:],
        )
    chained = "".join(_traceback.format_exception(type(exc), exc, exc.__traceback__, chain=True))
    return PromptBenchConversationFailure(
        item_id=item_id, rep=rep, stage=stage, error=repr(exc),
        traceback=chained[-_TRACEBACK_TAIL_CHARS:],
    )


class PromptBenchIterationSkipped(Exception):
    """Raised when too few conversations survived to produce a valid score."""

    def __init__(self, *, survivors: int, total: int, failures: Sequence[PromptBenchConversationFailure]):
        self.survivors = survivors
        self.total = total
        self.failures = list(failures)
        super().__init__(f"prompt bench pass: only {survivors}/{total} conversations survived")


class PromptBenchScorer(JudgeInLoopScorer):
    """Pipelined + guarded scoring over the inherited verdict machinery (own semaphores)."""

    def __init__(
        self,
        backend,
        *,
        reps: int,
        min_success_fraction: float = config.QUALITY_OPT_V3_MIN_SUCCESS_FRACTION,
        model_timeout_s: float = config.QUALITY_OPT_V3_TIMEOUT_MODEL_S,
        turn_timeout_s: Optional[float] = None,
        on_conversation_failure: Optional[Callable[[PromptBenchConversationFailure], None]] = None,
        on_conversation_scored: Optional[Callable[..., None]] = None,
        **kwargs,
    ) -> None:
        super().__init__(backend, reps=reps, **kwargs)
        self._min_success_fraction = float(min_success_fraction)
        self._model_timeout_s = float(model_timeout_s)
        self._turn_timeout_s = float(
            turn_timeout_s
            if turn_timeout_s is not None
            else (
                config.QUALITY_OPT_V3_TIMEOUT_RETRIEVAL_S
                + config.QUALITY_OPT_V3_TIMEOUT_CLOSENESS_S
                + config.QUALITY_OPT_V3_TIMEOUT_JUDGE_S
            )
        )
        self._on_conversation_failure = on_conversation_failure
        self._on_conversation_scored = on_conversation_scored

    async def score_prompt(
        self,
        *,
        model: str,
        instruction: str,
        items: Sequence,
        prompt_role: str,
        max_concurrency: Optional[int] = None,
    ) -> SliceScore:
        """Score ``instruction`` on ``items`` × ``reps`` with containment; collate survivors."""
        item_lookup = {it.item_id: it for it in items}
        model_cap = (
            max_concurrency
            if max_concurrency is not None
            else config.PROMPT_BENCH_MODEL_CONCURRENCY
        )
        # PRIVATE caps (see module note): never shares the optimizer's pool. The MODEL
        # cap is Prompt Bench's own (default 4 concurrent target generations); the judge
        # cap is the shared judge cap (the owner asked to bound model calls, not judging).
        gen_sem = _pb_resource_semaphore("model", int(model_cap))
        judge_sem = _pb_resource_semaphore("judge", int(config.PROMPT_BENCH_JUDGE_CONCURRENCY))

        conversations = [(item, rep) for item in items for rep in range(self._reps)]
        total = len(conversations)
        scored_so_far = {"done": 0}
        started = time.monotonic()
        _LOG.info(
            "promptbench score[%s]: %d conversations pipelined (model_cap=%s judge_cap=%s)",
            prompt_role, total, model_cap, config.PROMPT_BENCH_JUDGE_CONCURRENCY,
        )

        async def run_conversation(item, rep: int):
            try:
                async with gen_sem:
                    answers = await guarded_call(
                        f"generate:{item.item_id}#{rep}",
                        lambda: self._generate_conversation(
                            model=model, instruction=instruction, item=item, item_lookup=item_lookup
                        ),
                        timeout_s=self._model_timeout_s,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — containment boundary
                return None, _failure_from(item.item_id, rep, "generate", exc)

            verdicts: list[Optional[TurnVerdict]] = [None] * len(answers)

            async def judge_turn(turn_index: int, answer_text: str) -> None:
                async with judge_sem:
                    verdicts[turn_index] = await guarded_call(
                        f"judge:{item.item_id}#{rep}:t{turn_index + 1}",
                        lambda: self._judge_turn(
                            model=model, item=item, rep=rep, turn_index=turn_index, ans=answer_text
                        ),
                        timeout_s=self._turn_timeout_s,
                    )

            try:
                await asyncio.gather(*(judge_turn(ti, ans) for ti, ans in enumerate(answers)))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — containment boundary
                return None, _failure_from(item.item_id, rep, "judge", exc)

            finished = [v for v in verdicts if v is not None]
            scored_so_far["done"] += 1
            self._report_scored(
                role=prompt_role,
                done=scored_so_far["done"],
                total=total,
                item_id=item.item_id,
                rep=rep,
                conversation_mean=(
                    sum(v.overall for v in finished) / len(finished) if finished else 0.0
                ),
            )
            return finished, None

        async def run_batch(batch):
            results = await asyncio.gather(*(run_conversation(item, rep) for item, rep in batch))
            survived: list[list[TurnVerdict]] = []
            failed: list[tuple] = []
            failures: list[PromptBenchConversationFailure] = []
            for (item, rep), (verdicts, failure) in zip(batch, results):
                if failure is not None:
                    failed.append((item, rep))
                    failures.append(failure)
                    self._report_failure(failure)
                elif verdicts:
                    survived.append(verdicts)
            return survived, failed, failures

        survived, failed, failures = await run_batch(conversations)

        if failed and (len(survived) / total) < self._min_success_fraction:
            _LOG.warning(
                "promptbench score[%s]: %d/%d failed; retrying the failed batch once",
                prompt_role, len(failed), total,
            )
            retried, _still_failed, retry_failures = await run_batch(failed)
            survived.extend(retried)
            failures = list(retry_failures)

        if total and (len(survived) / total) < self._min_success_fraction:
            raise PromptBenchIterationSkipped(survivors=len(survived), total=total, failures=failures)

        _LOG.info(
            "promptbench score[%s]: collated %d/%d conversations in %.1fs (%d contained failures)",
            prompt_role, len(survived), total, time.monotonic() - started, len(failures),
        )
        return self._aggregate(
            model=model, prompt_role=prompt_role, conversation_verdicts=survived
        )

    def _report_scored(self, **progress: object) -> None:
        if self._on_conversation_scored is None:
            return
        try:
            self._on_conversation_scored(**progress)
        except Exception:  # noqa: BLE001 — observability must never affect scoring
            _LOG.warning("on_conversation_scored callback raised; ignoring", exc_info=True)

    def _report_failure(self, failure: PromptBenchConversationFailure) -> None:
        _LOG.warning(
            "promptbench contained failure: item=%s rep=%d stage=%s error=%s",
            failure.item_id, failure.rep, failure.stage, failure.error,
        )
        if self._on_conversation_failure is None:
            return
        try:
            self._on_conversation_failure(failure)
        except Exception:  # noqa: BLE001 — observability must never affect scoring
            _LOG.warning("on_conversation_failure callback raised; ignoring", exc_info=True)
