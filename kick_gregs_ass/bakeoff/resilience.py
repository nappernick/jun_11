"""
Shared error-classification + retry/refresh helper (cross-cutting, Task 5).

The user flagged credential expiry as a first-class concern for this project:
"everything's 200, then everything's 400 five minutes later — refresh creds and
redo failed attempts." A full WIDE+DEEP run across several candidate models is
hours of wall-clock, easily outliving a short-lived STS/Bedrock session. Rather
than crashing a multi-hour run when the underlying credentials roll over, every
Bedrock-touching client (the model adapters here in Task 5, the judge/embedding
scorers in Tasks 6/7) and the runner (Task 10) funnel their endpoint calls
through one shared helper:

* :func:`classify_error` — map a raised exception (botocore ``ClientError``, an
  :class:`httpx` error, a bare connection/timeout error, ...) onto the shared
  :class:`bakeoff.types.ErrorClass` taxonomy, using the signatures configured in
  :mod:`bakeoff.config` (``AUTH_EXPIRED_*``, ``THROTTLE_*``, ``TRANSIENT_*``).
* :func:`call_with_resilience` — invoke an async unit of work and, on
  :attr:`~bakeoff.types.ErrorClass.AUTH_EXPIRED`, call an **injected** credential-
  refresh callback and retry (up to ``AUTH_MAX_REFRESH_CYCLES`` with backoff); on
  :attr:`~bakeoff.types.ErrorClass.THROTTLED`/``TRANSIENT`` back off and retry
  (up to ``RETRY_MAX_ATTEMPTS``); on ``PERMANENT``/``UNKNOWN`` or once a budget is
  exhausted, re-raise so the caller records the trial as errored and the run can
  resume the failed trial later.

The refresh mechanism is **injectable** (a plain callable, sync or async) so the
helper is testable without real STS and reusable by the runner's run-wide
auto-pause/refresh logic. ``classify_error`` is likewise pure and side-effect
free, so the classification can be unit-tested in isolation from any retry loop.

Import-light: standard library plus :mod:`bakeoff.config` and
:class:`bakeoff.types.ErrorClass`. It does **not** import boto3 or httpx (it
inspects exceptions duck-typed), so importing this module pulls in nothing heavy
and it works for any transport.
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Awaitable, Callable, Optional, TypeVar

import bakeoff.config as config
from bakeoff.types import ErrorClass

__all__ = ["classify_error", "call_with_resilience", "backoff_delay"]

T = TypeVar("T")

# Substrings (lower-cased) in an exception's *class name* that mark a transient
# connection/timeout blip when no HTTP status or error code is available — the
# shape both botocore (EndpointConnectionError, ConnectTimeoutError,
# ReadTimeoutError, ...) and httpx (ConnectError, ReadTimeout, ...) take. Kept
# here (not in config) because these are about exception *types*, not the
# configurable provider signatures.
_TRANSIENT_NAME_SIGNATURES: tuple[str, ...] = (
    "timeout",
    "connecterror",
    "connectionerror",
    "connectionreset",
    "connectionclosed",
    "endpointconnection",
    "readtimeout",
    "writetimeout",
    "pooltimeout",
    "networkerror",
    "remoteprotocol",
    "incompleteread",
    "protocolerror",
)


# ---------------------------------------------------------------------------
# Classification (pure, side-effect free)
# ---------------------------------------------------------------------------
def _extract_signals(exc: BaseException) -> tuple[Optional[str], Optional[int], str]:
    """Pull ``(error_code, http_status, message)`` out of a raised exception.

    Handles the two shapes the harness sees without importing either library:

    * **botocore ``ClientError``** — carries a ``.response`` *dict* with
      ``["Error"]["Code"]`` and ``["ResponseMetadata"]["HTTPStatusCode"]``.
    * **httpx ``HTTPStatusError``** — carries a ``.response`` *object* exposing
      ``.status_code``.

    Falls back to the exception's class name as the "code" so a bare
    connection/timeout error still classifies. The message is the ``str()`` of the
    exception (always available), used for the message-signature match.
    """
    code: Optional[str] = None
    status: Optional[int] = None

    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        # botocore ClientError shape.
        err = resp.get("Error") or {}
        raw_code = err.get("Code")
        if raw_code:
            code = str(raw_code)
        meta = resp.get("ResponseMetadata") or {}
        raw_status = meta.get("HTTPStatusCode")
        if isinstance(raw_status, int):
            status = raw_status
    elif resp is not None and hasattr(resp, "status_code"):
        # httpx.Response shape.
        try:
            status = int(resp.status_code)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            status = None

    # opensearch-py / elasticsearch ``TransportError`` shape: the HTTP status lives
    # directly on the exception as ``.status_code`` (an int), with no ``.response``.
    # Without this, an ALPHA OpenSearch 403 (auth-expiry on the retrieval substrate)
    # carries no status into classification and is misread as PERMANENT — so the
    # credential-refresh+retry path never fires and a long optimizer run dies at the
    # ~1h token wall instead of healing. Only consult it when nothing above resolved a
    # status, and only when it is a plain int (the TransportError contract).
    if status is None:
        raw_status = getattr(exc, "status_code", None)
        if isinstance(raw_status, int):
            status = raw_status

    # An explicit botocore error code wins; otherwise fall back to the class name
    # so type-based signatures (e.g. "ExpiredTokenException" raised directly, or a
    # connection-error class) still match.
    if not code:
        code = type(exc).__name__

    return code, status, str(exc)


def classify_error(exc: BaseException) -> ErrorClass:
    """Classify a failed downstream call into the shared :class:`ErrorClass`.

    Decision order (most specific / most actionable first):

    1. **AUTH_EXPIRED** — error code in ``AUTH_EXPIRED_ERROR_CODES`` or HTTP status
       in ``AUTH_EXPIRED_HTTP_STATUSES``. This is the central resilience case: a
       credential refresh + retry may fix it.
    2. **THROTTLED** — error code in ``THROTTLE_ERROR_CODES`` or HTTP 429. Backoff
       + retry, never a refresh.
    3. **AUTH_EXPIRED (message fallback)** — for transports that surface no
       structured code/status, a message containing an ``AUTH_EXPIRED_MESSAGE_
       SIGNATURES`` substring (e.g. "the security token ... is expired").
    4. **TRANSIENT** — 5xx HTTP status, or a connection/timeout exception type.
       Backoff + retry.
    5. **PERMANENT** — any other 4xx (a client/logic error that retrying will not
       fix).
    6. **UNKNOWN** — nothing matched; treated conservatively (the retry loop does
       not retry it).

    Pure: never mutates state, never performs I/O.
    """
    code, status, message = _extract_signals(exc)
    code_l = (code or "").lower()
    message_l = message.lower()

    # 1. Auth-expiry by structured code or status.
    if code and code in config.AUTH_EXPIRED_ERROR_CODES:
        return ErrorClass.AUTH_EXPIRED
    if status is not None and status in config.AUTH_EXPIRED_HTTP_STATUSES:
        return ErrorClass.AUTH_EXPIRED

    # 2. Throttling by structured code or status.
    if code and code in config.THROTTLE_ERROR_CODES:
        return ErrorClass.THROTTLED
    if status is not None and status in config.THROTTLE_HTTP_STATUSES:
        return ErrorClass.THROTTLED

    # 3. Auth-expiry by message signature (unstructured transports).
    for sig in config.AUTH_EXPIRED_MESSAGE_SIGNATURES:
        if sig in message_l:
            return ErrorClass.AUTH_EXPIRED

    # 4. Transient by status or by connection/timeout exception type.
    if status is not None and status in config.TRANSIENT_HTTP_STATUSES:
        return ErrorClass.TRANSIENT
    if any(sig in code_l for sig in _TRANSIENT_NAME_SIGNATURES):
        return ErrorClass.TRANSIENT

    # 5. Any remaining 4xx is a permanent client error.
    if status is not None and 400 <= status < 500:
        return ErrorClass.PERMANENT

    # 6. Unclassifiable.
    return ErrorClass.UNKNOWN


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------
def backoff_delay(
    base_s: float,
    max_s: float,
    attempt: int,
    jitter: "Callable[[float], float] | None" = None,
) -> float:
    """Exponential backoff: ``min(base * 2**attempt, max)``, optionally jittered.

    ``attempt`` is 0-based (first retry uses ``attempt=0`` -> ``base``). ``jitter``
    is an optional callable mapping the computed delay to a jittered delay (the
    consumer supplies one to avoid a thundering herd when many concurrent calls
    expire at once — see the config note); when omitted the delay is deterministic,
    which keeps unit tests reproducible.
    """
    delay = min(base_s * (2 ** max(0, attempt)), max_s)
    if jitter is not None:
        delay = jitter(delay)
    return delay


# ---------------------------------------------------------------------------
# Resilient invocation
# ---------------------------------------------------------------------------
async def call_with_resilience(
    fn: Callable[[], Awaitable[T]],
    *,
    refresh_credentials: "Callable[[], object] | None" = None,
    classify: Callable[[BaseException], ErrorClass] = classify_error,
    max_refresh_cycles: int = config.AUTH_MAX_REFRESH_CYCLES,
    auth_backoff_base_s: float = config.AUTH_BACKOFF_BASE_S,
    auth_backoff_max_s: float = config.AUTH_BACKOFF_MAX_S,
    retry_max_attempts: int = config.RETRY_MAX_ATTEMPTS,
    retry_backoff_base_s: float = config.RETRY_BACKOFF_BASE_S,
    retry_backoff_max_s: float = config.RETRY_BACKOFF_MAX_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: "Callable[[float], float] | None" = None,
    on_retry: "Callable[[ErrorClass, BaseException, int], None] | None" = None,
) -> T:
    """Invoke ``fn`` with credential-refresh + backoff resilience.

    ``fn`` is a zero-argument async callable performing one attempt of the work
    (e.g. one streaming Bedrock invoke). It is awaited; if it raises, the
    exception is classified and handled:

    * **AUTH_EXPIRED** — if a ``refresh_credentials`` callback was provided and the
      refresh budget is not spent, the callback is invoked (awaited if it returns
      an awaitable), a backoff elapses, and ``fn`` is retried. The callback is the
      seam: the Bedrock adapter passes one that rebuilds its boto3 client from the
      standard credential chain; the runner can pass a run-wide auto-pause/refresh.
      After ``max_refresh_cycles`` unsuccessful refreshes the exception re-raises.
    * **THROTTLED / TRANSIENT** — a backoff elapses and ``fn`` is retried, up to
      ``retry_max_attempts`` times. No credential refresh.
    * **PERMANENT / UNKNOWN** — re-raised immediately; retrying will not help.

    The auth and non-auth budgets are tracked **separately**, so a call that hits
    a credential rollover partway through a series of throttles still gets its full
    refresh allowance (and vice-versa).

    Args:
        fn: zero-arg async callable performing one attempt.
        refresh_credentials: optional callable (sync or async) invoked before an
            auth-expiry retry. When ``None``, an AUTH_EXPIRED error re-raises
            immediately (nothing can fix it here).
        classify: the classifier (injectable for testing); defaults to
            :func:`classify_error`.
        sleep: the async sleep used for backoff (injectable so tests run instantly
            and can assert the backoff schedule).
        jitter: optional delay jitter (see :func:`backoff_delay`).
        on_retry: optional observer ``(error_class, exc, attempt_index)`` called
            just before each retry — for logging/metrics; never affects control
            flow.

    Returns:
        Whatever ``fn`` returns on its first successful attempt.

    Raises:
        The last exception raised by ``fn`` once a budget is exhausted, or
        immediately for a PERMANENT/UNKNOWN classification.
    """
    refresh_cycles = 0
    retry_attempts = 0

    while True:
        try:
            return await fn()
        except BaseException as exc:  # noqa: BLE001 - we re-raise everything we don't handle
            # Never swallow control-flow exceptions (CancelledError, KeyboardInterrupt,
            # SystemExit are BaseException-but-not-Exception in 3.8+).
            if not isinstance(exc, Exception):
                raise

            error_class = classify(exc)

            if error_class == ErrorClass.AUTH_EXPIRED:
                if refresh_credentials is None or refresh_cycles >= max_refresh_cycles:
                    raise
                if on_retry is not None:
                    on_retry(error_class, exc, refresh_cycles)
                result = refresh_credentials()
                if inspect.isawaitable(result):
                    await result
                delay = backoff_delay(
                    auth_backoff_base_s, auth_backoff_max_s, refresh_cycles, jitter
                )
                refresh_cycles += 1
                await sleep(delay)
                continue

            if error_class in (ErrorClass.THROTTLED, ErrorClass.TRANSIENT):
                if retry_attempts >= retry_max_attempts:
                    raise
                if on_retry is not None:
                    on_retry(error_class, exc, retry_attempts)
                delay = backoff_delay(
                    retry_backoff_base_s, retry_backoff_max_s, retry_attempts, jitter
                )
                retry_attempts += 1
                await sleep(delay)
                continue

            # PERMANENT / UNKNOWN: not retryable.
            raise
