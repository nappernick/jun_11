"""
Guarded external calls for optimizer V3 — hard timeout + classified retry, composed
over :func:`bakeoff.resilience.call_with_resilience`.

The v2 post-mortem split external-call failure into three layers, and the live clients
only cover the first:

1. **Auth expiry** — already healed INSIDE the live clients (``ResilientBedrockJudge``,
   ``EmbeddingClient``, the AOSS backend's client-factory rebuild). The guard does not
   duplicate that; it passes an optional ``refresh_credentials`` through for callers
   that have an outer seam.
2. **Hangs** — nothing in v2 bounded a call. The guard wraps every attempt in
   ``asyncio.wait_for``; a call past its window raises ``TimeoutError``, which
   :func:`~bakeoff.resilience.classify_error` reads as TRANSIENT, so it is retried
   with backoff like any other transient failure.
3. **Throttle / transient leakage** — errors that escape the clients' internal budgets
   are classified and retried here with their own budget, instead of shooting up the
   stack and killing the run.

A guard that exhausts its budget raises :class:`GuardedCallError` wrapping the last
underlying exception, so containment layers above (the V3 scorer / island) can tell a
"guard gave up" from a programming error.
"""
from __future__ import annotations

import asyncio
import logging
import time
import traceback
from typing import Awaitable, Callable, Optional, TypeVar

from bakeoff import config
from bakeoff.resilience import call_with_resilience

__all__ = ["GuardedCallError", "guarded_call"]

_LOG = logging.getLogger("bakeoff.opt.v3.guards")

ResultT = TypeVar("ResultT")


class GuardedCallError(RuntimeError):
    """A guarded call exhausted its timeout/retry budget.

    Carries the guard ``label``, the last underlying exception (as ``__cause__``),
    the wall-clock spent, and the FULL chained traceback text — for a timeout the
    chain includes the cancellation frame, i.e. exactly which inner call (retrieve
    / closeness / judge / generate) was in flight when the window expired. Raised
    ONLY by :func:`guarded_call`.
    """

    def __init__(
        self,
        label: str,
        last_error: BaseException,
        *,
        elapsed_s: float = 0.0,
        traceback_text: str = "",
    ) -> None:
        super().__init__(
            f"guarded call {label!r} exhausted its budget after {elapsed_s:.1f}s: "
            f"{last_error!r}"
        )
        self.label = label
        self.last_error = last_error
        self.elapsed_s = elapsed_s
        self.traceback_text = traceback_text


async def guarded_call(
    label: str,
    attempt: Callable[[], Awaitable[ResultT]],
    *,
    timeout_s: float,
    refresh_credentials: Optional[Callable[[], object]] = None,
    max_retries: int = config.QUALITY_OPT_V3_GUARD_MAX_RETRIES,
) -> ResultT:
    """Run ``attempt`` with a hard per-attempt timeout and classified retries.

    Args:
        label: short identifier for logs/errors (e.g. ``"judge:item-3:turn-2"``).
        attempt: zero-arg async callable performing ONE fresh attempt of the work
            (wrap sync work in ``asyncio.to_thread`` inside it). It is re-invoked on
            each retry, so per-attempt state (clients rebuilt by inner healing, the
            memoized retrieval cache) is re-resolved naturally.
        timeout_s: hard wall-clock bound per attempt; a breach cancels the attempt and
            counts as a TRANSIENT failure (retried with backoff).
        refresh_credentials: optional outer auth-heal seam, passed through to
            :func:`call_with_resilience`. ``None`` is the norm — the live clients heal
            auth internally.
        max_retries: THROTTLED/TRANSIENT retry budget at this layer.

    Returns:
        ``attempt``'s result on the first success.

    Raises:
        GuardedCallError: when every attempt failed (the last exception is chained as
            ``__cause__``).
    """

    started = time.monotonic()

    async def _timed_attempt() -> ResultT:
        return await asyncio.wait_for(attempt(), timeout=timeout_s)

    def _log_retry(error_class, exc, attempt_index) -> None:
        _LOG.warning(
            "guard[%s]: %s (%r) — retry %d (%.1fs elapsed)",
            label, error_class.name, exc, attempt_index, time.monotonic() - started,
        )

    try:
        return await call_with_resilience(
            _timed_attempt,
            refresh_credentials=refresh_credentials,
            retry_max_attempts=max_retries,
            on_retry=_log_retry,
        )
    except asyncio.CancelledError:
        raise  # never swallow cooperative cancellation
    except Exception as exc:
        elapsed_s = time.monotonic() - started
        # The FULL chained traceback — for a wait_for timeout the chain carries the
        # cancellation frame, i.e. the exact inner await that was in flight. This is
        # the "see exactly what's going on" record: logged loudly AND attached to
        # the raised error so containment layers persist it.
        chained_traceback = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__, chain=True)
        )
        _LOG.error(
            "guard[%s]: EXHAUSTED after %.1fs (timeout_s=%.0f, max_retries=%d)\n%s",
            label, elapsed_s, timeout_s, max_retries, chained_traceback,
        )
        raise GuardedCallError(
            label, exc, elapsed_s=elapsed_s, traceback_text=chained_traceback
        ) from exc
