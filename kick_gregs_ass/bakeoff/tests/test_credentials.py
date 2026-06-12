"""
Tests for the centralized credential broker (bakeoff.credentials).

Pins the load-bearing behaviors with ZERO real ada/AWS: an injected ``ada_runner``
records invocations, and an injected clock drives the TTL / coalescing logic.

What these lock down (the two failure modes the broker exists to fix):

* A refresh actually RUNS ada (the old hook only re-read the same expired file).
* Concurrent / rapid refreshes COALESCE to one real ada call via the cross-process
  min-interval + mint sidecar (so a burst of auth-expiry retries doesn't stampede).
* A lapsed-Midway ada failure surfaces as CredentialRefreshError(needs_mwinit=True)
  rather than looping forever.
* An unknown profile raises rather than silently doing nothing.
* The refresh_callback_for hook (the call_with_resilience seam) forces a real mint.
"""
from __future__ import annotations

import pytest

from bakeoff.credentials import (
    AdaResult,
    CredentialBroker,
    CredentialRefreshError,
    UnknownProfileError,
)

_PROFILES = {
    "alpha": {"account": "111111111111", "role": "R", "provider": "conduit", "region": "us-west-2"},
}


class _FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _RecordingAda:
    """Records each (profile) call; returns a configurable result."""

    def __init__(self, result: AdaResult | None = None) -> None:
        self.result = result or AdaResult(returncode=0)
        self.calls: list[str] = []

    def __call__(self, profile: str, spec: dict) -> AdaResult:
        self.calls.append(profile)
        return self.result


def _broker(tmp_path, ada, clock, **kw) -> CredentialBroker:
    return CredentialBroker(
        profiles=_PROFILES,
        default_profile="alpha",
        refresh_ttl_s=kw.pop("refresh_ttl_s", 1500.0),
        min_refresh_interval_s=kw.pop("min_refresh_interval_s", 20.0),
        lock_dir=tmp_path / "locks",
        ada_runner=ada,
        clock=clock,
        **kw,
    )


def test_refresh_runs_ada_and_records_mint(tmp_path):
    ada = _RecordingAda()
    clock = _FakeClock()
    broker = _broker(tmp_path, ada, clock)

    ran = broker.refresh("alpha", force=True)

    assert ran is True
    assert ada.calls == ["alpha"]  # ada actually ran (the core fix)


def test_rapid_second_refresh_coalesces(tmp_path):
    ada = _RecordingAda()
    clock = _FakeClock()
    broker = _broker(tmp_path, ada, clock, min_refresh_interval_s=20.0)

    assert broker.refresh("alpha", force=True) is True
    clock.advance(5.0)  # within the 20s coalescing window
    # A second forced refresh inside the window must NOT re-run ada.
    assert broker.refresh("alpha", force=True) is False
    assert ada.calls == ["alpha"]  # exactly one real ada call


def test_refresh_after_interval_runs_ada_again(tmp_path):
    ada = _RecordingAda()
    clock = _FakeClock()
    broker = _broker(tmp_path, ada, clock, min_refresh_interval_s=20.0)

    assert broker.refresh("alpha", force=True) is True
    clock.advance(25.0)  # past the coalescing window
    assert broker.refresh("alpha", force=True) is True
    assert ada.calls == ["alpha", "alpha"]


def test_midway_failure_surfaces_needs_mwinit(tmp_path):
    ada = _RecordingAda(
        AdaResult(returncode=1, stderr="... did not redirect. Status code: 401. run mwinit")
    )
    clock = _FakeClock()
    broker = _broker(tmp_path, ada, clock)

    with pytest.raises(CredentialRefreshError) as ei:
        broker.refresh("alpha", force=True)
    assert ei.value.needs_mwinit is True


def test_generic_ada_failure_is_not_mwinit(tmp_path):
    ada = _RecordingAda(AdaResult(returncode=2, stderr="some other ada error"))
    clock = _FakeClock()
    broker = _broker(tmp_path, ada, clock)

    with pytest.raises(CredentialRefreshError) as ei:
        broker.refresh("alpha", force=True)
    assert ei.value.needs_mwinit is False


def test_unknown_profile_raises(tmp_path):
    ada = _RecordingAda()
    clock = _FakeClock()
    broker = _broker(tmp_path, ada, clock)

    with pytest.raises(UnknownProfileError):
        broker.refresh("does-not-exist", force=True)
    assert ada.calls == []  # never attempted ada for an unknown profile


def test_refresh_callback_forces_a_real_mint(tmp_path):
    ada = _RecordingAda()
    clock = _FakeClock()
    broker = _broker(tmp_path, ada, clock)

    cb = broker.refresh_callback_for("alpha")
    cb()
    assert ada.calls == ["alpha"]  # the resilience seam mints for real


def test_default_profile_used_when_unspecified(tmp_path):
    ada = _RecordingAda()
    clock = _FakeClock()
    broker = _broker(tmp_path, ada, clock)

    broker.refresh(force=True)  # no profile -> default "alpha"
    assert ada.calls == ["alpha"]
