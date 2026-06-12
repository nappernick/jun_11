"""
ResilientScorer — V3's contained, pipelined replacement for ``JudgeInLoopScorer``.

Subclasses :class:`bakeoff.quality.optimizer.judge_loop.JudgeInLoopScorer` so the
verdict construction (``_judge_turn``: retrieval-always grounding, abstention
weighting, closeness cross-check) and the aggregation (``_aggregate``: per-conversation
triad → slice mean + CI) are REUSED byte-for-byte — only the orchestration of a scoring
pass is replaced:

* **Pipelined, not phase-barriered.** v2 generated ALL conversations, then judged ALL
  turns (two global barriers — a hang anywhere stalled everything behind it). V3 runs
  one task per ``(item, rep)`` conversation: generate under the model semaphore, then
  judge that conversation's turns under the judge semaphore, immediately. Item 1 can be
  in judging while item 6 is still generating — within one iteration the rung's items
  run concurrently end-to-end, bounded only by the per-resource caps.
* **Guarded calls.** Generation and each turn-judge go through
  :func:`~bakeoff.quality.optimizer.v3.guards.guarded_call` (hard timeout + classified
  backoff/retry on top of the clients' internal auth healing).
* **Contained failures, collate survivors.** A turn that exhausts its guard fails only
  its conversation. The pass collates the surviving conversations; failed ones are
  retried ONCE as a batch; if survivors still fall below
  ``config.QUALITY_OPT_V3_MIN_SUCCESS_FRACTION`` the pass raises
  :class:`IterationSkipped` (the island skips the iteration and the loop continues —
  the run never dies from item-level failures). Every conversation failure is reported
  through the optional ``on_conversation_failure`` callback for events/audit.

The returned type is v2's :class:`~bakeoff.quality.optimizer.judge_loop.SliceScore`
(over the survivors), so ``select_failures``, ``PromotionDecider``, the emitter, and the
durable records all reuse v2's shapes unchanged.
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

__all__ = ["ConversationFailure", "IterationSkipped", "ResilientScorer"]

_LOG = logging.getLogger("bakeoff.opt.v3.scorer")

# ---------------------------------------------------------------------------
# GLOBAL per-resource semaphores (per event loop).
#
# V3 runs many scoring passes concurrently (2 models × 2 islands). If each pass
# carried its own semaphores (the v2 scorer's pattern), the per-resource caps
# would MULTIPLY — observed live: 4 passes × judge_cap 4 = 16 concurrent Opus
# calls, which queued/throttled the judge and fattened its latency tail past the
# turn window (turn-5 calls that take ~35s solo blew 180s repeatedly). Sharing
# ONE semaphore per (loop, resource) makes config.CONCURRENCY_CAPS a true
# process-wide cap again while keeping the per-conversation pipelining.
# Keyed by running-loop id so tests that spin up fresh loops never reuse a
# semaphore bound to a dead loop.
# ---------------------------------------------------------------------------
_RESOURCE_SEMAPHORES: dict[tuple[int, str, int], asyncio.Semaphore] = {}


def _resource_semaphore(resource: str, cap: int) -> asyncio.Semaphore:
    """The loop-wide shared semaphore for ``resource`` at ``cap`` concurrency."""
    bounded_cap = max(1, int(cap))
    key = (id(asyncio.get_running_loop()), resource, bounded_cap)
    semaphore = _RESOURCE_SEMAPHORES.get(key)
    if semaphore is None:
        semaphore = asyncio.Semaphore(bounded_cap)
        _RESOURCE_SEMAPHORES[key] = semaphore
    return semaphore


@dataclass(frozen=True)
class ConversationFailure:
    """One ``(item, rep)`` conversation that failed a scoring pass after its guards.

    ``stage`` is ``"generate"`` or ``"judge"``; ``error`` is the repr of the final
    underlying exception; ``elapsed_s`` is the wall-clock the guard burned before
    giving up; ``traceback`` is the (tail-trimmed) full chained traceback — for a
    timeout it shows the exact inner await that was in flight when the window
    expired. Serializable as-is for events and the disposable error store.
    """

    item_id: str
    rep: int
    stage: str
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


#: keep persisted/streamed tracebacks readable but bounded.
_TRACEBACK_TAIL_CHARS = 2500


def _failure_from(
    item_id: str, rep: int, stage: str, exc: BaseException
) -> ConversationFailure:
    """Build a fully-forensic ConversationFailure from any contained exception."""
    import traceback as _traceback

    if isinstance(exc, GuardedCallError):
        return ConversationFailure(
            item_id=item_id,
            rep=rep,
            stage=stage,
            error=repr(exc.last_error),
            elapsed_s=exc.elapsed_s,
            traceback=exc.traceback_text[-_TRACEBACK_TAIL_CHARS:],
        )
    chained = "".join(
        _traceback.format_exception(type(exc), exc, exc.__traceback__, chain=True)
    )
    return ConversationFailure(
        item_id=item_id,
        rep=rep,
        stage=stage,
        error=repr(exc),
        traceback=chained[-_TRACEBACK_TAIL_CHARS:],
    )


class IterationSkipped(Exception):
    """A scoring pass could not produce a valid score — skip the iteration, keep going.

    Raised when, even after the one batch retry, fewer than ``min_success_fraction`` of
    the pass's conversations produced verdicts. Carries the surviving/total counts and
    the per-conversation failures so the island can emit and persist exactly what was
    lost. This is a CONTROL-FLOW signal for the V3 island, never a run-fatal error.
    """

    def __init__(
        self,
        *,
        role: str,
        survivors: int,
        total: int,
        failures: Sequence[ConversationFailure],
    ) -> None:
        super().__init__(
            f"scoring pass ({role}) kept {survivors}/{total} conversations — below the "
            f"min success fraction; iteration skipped"
        )
        self.role = role
        self.survivors = survivors
        self.total = total
        self.failures = tuple(failures)


class ResilientScorer(JudgeInLoopScorer):
    """Pipelined + guarded + contained scoring over the inherited verdict machinery."""

    def __init__(
        self,
        backend,
        *,
        reps: int,
        min_success_fraction: float = config.QUALITY_OPT_V3_MIN_SUCCESS_FRACTION,
        model_timeout_s: float = config.QUALITY_OPT_V3_TIMEOUT_MODEL_S,
        turn_timeout_s: Optional[float] = None,
        on_conversation_failure: Optional[Callable[[ConversationFailure], None]] = None,
        on_conversation_scored: Optional[Callable[..., None]] = None,
        **kwargs,
    ) -> None:
        super().__init__(backend, reps=reps, **kwargs)
        self._min_success_fraction = float(min_success_fraction)
        self._model_timeout_s = float(model_timeout_s)
        # One turn = retrieval + closeness + judge; bound it by the sum of their windows
        # so a single hard timeout covers the composite without a per-call wrap (the
        # guard's fresh attempt re-runs retrieval, which the memoizing layer makes free).
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
        # Live cycle visibility (owner direction): called after EVERY conversation
        # finishes judging, with keyword args (role, done, total, item_id, rep,
        # conversation_mean) — so the UI ticks during a pass instead of going dark
        # until the iteration completes. Best-effort: a raising callback is logged
        # and ignored, never affecting scoring.
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
        """Score ``instruction`` on ``items`` × ``reps`` with containment; collate survivors.

        Same signature and return type as the v2 scorer. Raises :class:`IterationSkipped`
        (never the underlying exception) when too few conversations survive.
        """
        item_lookup = {it.item_id: it for it in items}
        model_cap = (
            max_concurrency if max_concurrency is not None else config.CONCURRENCY_CAPS["model"]
        )
        # GLOBAL caps: shared across every concurrent pass on this loop (see the
        # module-level note — per-pass semaphores multiplied the Opus load 4x).
        gen_sem = _resource_semaphore("model", int(model_cap))
        judge_sem = _resource_semaphore("judge", int(config.CONCURRENCY_CAPS["judge"]))

        conversations = [(item, rep) for item in items for rep in range(self._reps)]
        total = len(conversations)
        scored_so_far = {"done": 0}
        started = time.monotonic()
        _LOG.info(
            "score[%s/%s]: %d conversations pipelined (model_cap=%s judge_cap=%s)",
            model, prompt_role, total, model_cap, config.CONCURRENCY_CAPS["judge"],
        )

        async def run_conversation(item, rep: int):
            """Generate then judge ONE conversation end-to-end; contain its failures.

            Returns ``(verdicts, None)`` on success or ``(None, ConversationFailure)``
            on a contained failure. Never raises (cancellation excepted).
            """
            try:
                async with gen_sem:
                    answers = await guarded_call(
                        f"generate:{item.item_id}#{rep}",
                        lambda: self._generate_conversation(
                            model=model,
                            instruction=instruction,
                            item=item,
                            item_lookup=item_lookup,
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
                await asyncio.gather(
                    *(judge_turn(ti, ans) for ti, ans in enumerate(answers))
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — containment boundary
                return None, _failure_from(item.item_id, rep, "judge", exc)
            finished = [v for v in verdicts if v is not None]
            # Live cycle visibility: one progress tick per finished conversation.
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
            failures: list[ConversationFailure] = []
            for (item, rep), (verdicts, failure) in zip(batch, results):
                if failure is not None:
                    failed.append((item, rep))
                    failures.append(failure)
                    self._report_failure(failure)
                elif verdicts:
                    survived.append(verdicts)
            return survived, failed, failures

        survived, failed, failures = await run_batch(conversations)

        # One batch retry of the failed conversations (transient weather often clears).
        if failed and (len(survived) / total) < self._min_success_fraction:
            _LOG.warning(
                "score[%s/%s]: %d/%d conversations failed; retrying the failed batch once",
                model, prompt_role, len(failed), total,
            )
            retried, _still_failed, retry_failures = await run_batch(failed)
            survived.extend(retried)
            # The retry's failures are the authoritative remainder: a conversation that
            # succeeded on retry drops out, one that failed twice keeps its LATEST error.
            failures = list(retry_failures)

        if total and (len(survived) / total) < self._min_success_fraction:
            raise IterationSkipped(
                role=prompt_role, survivors=len(survived), total=total, failures=failures
            )

        _LOG.info(
            "score[%s/%s]: collated %d/%d conversations in %.1fs (%d contained failures)",
            model, prompt_role, len(survived), total, time.monotonic() - started, len(failures),
        )
        return self._aggregate(
            model=model, prompt_role=prompt_role, conversation_verdicts=survived
        )

    def _report_scored(self, **progress: object) -> None:
        """Forward one conversation-scored progress tick (best-effort, never raises)."""
        if self._on_conversation_scored is None:
            return
        try:
            self._on_conversation_scored(**progress)
        except Exception:  # noqa: BLE001 — observability must never affect scoring
            _LOG.warning("on_conversation_scored callback raised; ignoring", exc_info=True)

    def _report_failure(self, failure: ConversationFailure) -> None:
        """Forward one contained failure to the observability callback (never raises)."""
        _LOG.warning(
            "contained failure: item=%s rep=%d stage=%s error=%s",
            failure.item_id, failure.rep, failure.stage, failure.error,
        )
        if self._on_conversation_failure is None:
            return
        try:
            self._on_conversation_failure(failure)
        except Exception:  # noqa: BLE001 — observability must never affect scoring
            _LOG.warning("on_conversation_failure callback raised; ignoring", exc_info=True)
