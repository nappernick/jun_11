"""
Unit tests for the shared credential-expiry resilience helper (Task 5,
cross-cutting credential-expiry resilience).

Two surfaces are covered:

* :func:`bakeoff.resilience.classify_error` — the pure classifier mapping a raised
  exception onto :class:`bakeoff.types.ErrorClass` using the configured signatures
  (botocore ``ClientError`` code, HTTP status, message substrings, connection/
  timeout exception types).
* :func:`bakeoff.resilience.call_with_resilience` — the retry/refresh loop. The
  load-bearing scenarios the user flagged ("everything's 200, then everything's
  400 five minutes later — refresh creds and redo failed attempts"):
    - an injected fake that fails with an auth-expiry signature N times then
      succeeds → the refresh callback is invoked exactly N times and the call
      ultimately succeeds;
    - a throttle case that backs off and retries WITHOUT ever refreshing;
    - permanent errors propagate immediately;
    - the refresh budget is bounded (re-raises after AUTH_MAX_REFRESH_CYCLES).

No real STS/boto3: the refresh callback and the unit of work are plain callables,
and ``sleep`` is injected so the backoff loop runs instantly.

Validates: Requirements 3.1, 15.3
"""
from __future__ import annotations

import asyncio

import pytest

import bakeoff.config as config
from bakeoff.resilience import backoff_delay, call_with_resilience, classify_error
from bakeoff.types import ErrorClass


# ---------------------------------------------------------------------------
# Exception fakes (shapes the classifier must handle without importing boto3)
# ---------------------------------------------------------------------------
class FakeClientError(Exception):
    """Mimics botocore.exceptions.ClientError: carries a ``.response`` dict."""

    def __init__(self, code: str, http_status: int | None = None, message: str = ""):
        self.response = {
            "Error": {"Code": code, "Message": message or code},
            "ResponseMetadata": {"HTTPStatusCode": http_status} if http_status else {},
        }
        super().__init__(message or code)


class FakeHttpxResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class FakeHTTPStatusError(Exception):
    """Mimics httpx.HTTPStatusError: carries a ``.response`` with ``status_code``."""

    def __init__(self, status_code: int, message: str = ""):
        self.response = FakeHttpxResponse(status_code)
        super().__init__(message or f"HTTP {status_code}")


class ConnectTimeoutError(Exception):
    """A connection/timeout-shaped exception (classified by class name)."""


# ---------------------------------------------------------------------------
# classify_error
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code", sorted(config.AUTH_EXPIRED_ERROR_CODES))
def test_classify_auth_expired_by_botocore_code(code: str) -> None:
    assert classify_error(FakeClientError(code)) == ErrorClass.AUTH_EXPIRED


@pytest.mark.parametrize("status", sorted(config.AUTH_EXPIRED_HTTP_STATUSES))
def test_classify_auth_expired_by_http_status(status: int) -> None:
    assert classify_error(FakeHTTPStatusError(status)) == ErrorClass.AUTH_EXPIRED


def test_classify_auth_expired_by_message_signature() -> None:
    # No structured code/status — only the message names the expiry.
    exc = RuntimeError("The security token included in the request is expired")
    assert classify_error(exc) == ErrorClass.AUTH_EXPIRED


@pytest.mark.parametrize("code", sorted(config.THROTTLE_ERROR_CODES))
def test_classify_throttled_by_code(code: str) -> None:
    assert classify_error(FakeClientError(code)) == ErrorClass.THROTTLED


def test_classify_throttled_by_http_429() -> None:
    assert classify_error(FakeHTTPStatusError(429)) == ErrorClass.THROTTLED


@pytest.mark.parametrize("status", sorted(config.TRANSIENT_HTTP_STATUSES))
def test_classify_transient_by_5xx(status: int) -> None:
    assert classify_error(FakeHTTPStatusError(status)) == ErrorClass.TRANSIENT


def test_classify_transient_by_connection_exception_type() -> None:
    assert classify_error(ConnectTimeoutError("timed out")) == ErrorClass.TRANSIENT


def test_classify_permanent_for_other_4xx() -> None:
    # 404 is a client error that retrying will not fix and is not auth/throttle.
    assert classify_error(FakeHTTPStatusError(404, "not found")) == ErrorClass.PERMANENT


def test_classify_unknown_for_unrecognized() -> None:
    assert classify_error(ValueError("some logic bug")) == ErrorClass.UNKNOWN


def test_auth_code_precedence_over_throttle_status() -> None:
    # An auth error code wins even if a throttle-ish status were present.
    exc = FakeClientError("ExpiredTokenException", http_status=429)
    assert classify_error(exc) == ErrorClass.AUTH_EXPIRED


# ---------------------------------------------------------------------------
# backoff_delay
# ---------------------------------------------------------------------------
def test_backoff_is_exponential_and_capped() -> None:
    assert backoff_delay(1.0, 30.0, 0) == 1.0
    assert backoff_delay(1.0, 30.0, 1) == 2.0
    assert backoff_delay(1.0, 30.0, 2) == 4.0
    assert backoff_delay(1.0, 30.0, 10) == 30.0  # capped


# ---------------------------------------------------------------------------
# call_with_resilience — the auth-refresh-then-succeed scenario
# ---------------------------------------------------------------------------
def test_auth_expiry_refreshes_then_succeeds() -> None:
    """Fail with an auth-expiry signature N times, then succeed.

    Asserts the refresh callback fired exactly N times and the call returned the
    eventual success value — the "refresh creds and redo failed attempts" path.
    """
    fail_times = 2
    state = {"calls": 0, "refreshes": 0}
    slept: list[float] = []

    async def flaky() -> str:
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise FakeClientError("ExpiredTokenException")
        return "ok"

    def refresh() -> None:
        state["refreshes"] += 1

    async def fake_sleep(d: float) -> None:
        slept.append(d)

    async def run() -> str:
        return await call_with_resilience(
            flaky, refresh_credentials=refresh, sleep=fake_sleep
        )

    result = asyncio.run(run())

    assert result == "ok"
    assert state["calls"] == fail_times + 1       # 2 failures + 1 success
    assert state["refreshes"] == fail_times       # one refresh per auth failure
    # Backoff elapsed once per refresh, exponentially.
    assert slept == [config.AUTH_BACKOFF_BASE_S, config.AUTH_BACKOFF_BASE_S * 2]


def test_auth_refresh_callback_can_be_async() -> None:
    state = {"calls": 0, "refreshes": 0}

    async def flaky() -> int:
        state["calls"] += 1
        if state["calls"] == 1:
            raise FakeClientError("UnrecognizedClientException")
        return 42

    async def refresh() -> None:
        state["refreshes"] += 1

    async def run() -> int:
        return await call_with_resilience(
            flaky, refresh_credentials=refresh, sleep=_noop_sleep
        )

    assert asyncio.run(run()) == 42
    assert state["refreshes"] == 1


def test_auth_expiry_exhausts_refresh_budget_and_reraises() -> None:
    """Persistent auth failure re-raises after AUTH_MAX_REFRESH_CYCLES refreshes."""
    state = {"refreshes": 0}

    async def always_expired() -> str:
        raise FakeClientError("ExpiredTokenException")

    def refresh() -> None:
        state["refreshes"] += 1

    async def run() -> str:
        return await call_with_resilience(
            always_expired,
            refresh_credentials=refresh,
            max_refresh_cycles=3,
            sleep=_noop_sleep,
        )

    with pytest.raises(FakeClientError):
        asyncio.run(run())
    # Exactly the budgeted number of refreshes, then give up.
    assert state["refreshes"] == 3


def test_auth_expiry_without_refresh_callback_reraises_immediately() -> None:
    state = {"calls": 0}

    async def expired() -> str:
        state["calls"] += 1
        raise FakeClientError("ExpiredTokenException")

    async def run() -> str:
        return await call_with_resilience(expired, refresh_credentials=None, sleep=_noop_sleep)

    with pytest.raises(FakeClientError):
        asyncio.run(run())
    assert state["calls"] == 1  # no retry without a refresh mechanism


# ---------------------------------------------------------------------------
# call_with_resilience — the throttle scenario (backoff, NO refresh)
# ---------------------------------------------------------------------------
def test_throttle_backs_off_and_retries_without_refreshing() -> None:
    """A throttled call backs off and retries, and the refresh callback is never
    invoked (throttling is not a credential problem)."""
    state = {"calls": 0, "refreshes": 0}
    slept: list[float] = []

    async def throttled_then_ok() -> str:
        state["calls"] += 1
        if state["calls"] <= 2:
            raise FakeClientError("ThrottlingException", http_status=429)
        return "done"

    def refresh() -> None:  # pragma: no cover - must never be called
        state["refreshes"] += 1

    async def fake_sleep(d: float) -> None:
        slept.append(d)

    async def run() -> str:
        return await call_with_resilience(
            throttled_then_ok, refresh_credentials=refresh, sleep=fake_sleep
        )

    assert asyncio.run(run()) == "done"
    assert state["calls"] == 3
    assert state["refreshes"] == 0  # NEVER refreshed for a throttle
    assert slept == [config.RETRY_BACKOFF_BASE_S, config.RETRY_BACKOFF_BASE_S * 2]


def test_transient_retries_then_exhausts_attempts() -> None:
    state = {"calls": 0}

    async def always_503() -> str:
        state["calls"] += 1
        raise FakeHTTPStatusError(503)

    async def run() -> str:
        return await call_with_resilience(
            always_503,
            refresh_credentials=None,
            retry_max_attempts=4,
            sleep=_noop_sleep,
        )

    with pytest.raises(FakeHTTPStatusError):
        asyncio.run(run())
    # initial attempt + 4 retries = 5 calls
    assert state["calls"] == 5


def test_permanent_error_propagates_without_retry() -> None:
    state = {"calls": 0}

    async def permanent() -> str:
        state["calls"] += 1
        raise FakeHTTPStatusError(400, "bad request")

    async def run() -> str:
        return await call_with_resilience(
            permanent, refresh_credentials=lambda: None, sleep=_noop_sleep
        )

    with pytest.raises(FakeHTTPStatusError):
        asyncio.run(run())
    assert state["calls"] == 1  # no retry for a permanent error


def test_auth_and_throttle_budgets_are_independent() -> None:
    """A call that hits throttles AND an expiry still gets its full refresh
    allowance: the budgets are tracked separately."""
    state = {"calls": 0, "refreshes": 0}

    async def mixed() -> str:
        state["calls"] += 1
        n = state["calls"]
        if n == 1:
            raise FakeClientError("ThrottlingException", http_status=429)
        if n == 2:
            raise FakeClientError("ExpiredTokenException")
        if n == 3:
            raise FakeHTTPStatusError(503)
        return "recovered"

    def refresh() -> None:
        state["refreshes"] += 1

    async def run() -> str:
        return await call_with_resilience(
            mixed, refresh_credentials=refresh, sleep=_noop_sleep
        )

    assert asyncio.run(run()) == "recovered"
    assert state["calls"] == 4
    assert state["refreshes"] == 1  # only the one expiry triggered a refresh


def test_on_retry_observer_is_invoked() -> None:
    seen: list[tuple[ErrorClass, int]] = []

    async def flaky() -> str:
        if not seen:
            raise FakeClientError("ThrottlingException", http_status=429)
        return "ok"

    def on_retry(ec: ErrorClass, exc: BaseException, attempt: int) -> None:
        seen.append((ec, attempt))

    async def run() -> str:
        return await call_with_resilience(
            flaky, refresh_credentials=None, sleep=_noop_sleep, on_retry=on_retry
        )

    assert asyncio.run(run()) == "ok"
    assert seen == [(ErrorClass.THROTTLED, 0)]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def _noop_sleep(_d: float) -> None:
    return None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
