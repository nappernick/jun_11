"""
Focused tests for the semantic scorer's **credential-expiry resilience** and
error classification (Task 6 — the design's "first-class concern" that the
embedding call survives an expired-credential burst by refreshing + retrying).

These complement ``test_scoring_retrieval_semantic.py`` (which covers the cosine
math and the content-hash cache via an injected ``embed_fn``). Here we exercise
the **default** Bedrock embedder (:class:`_ResilientBedrockEmbedder`) with a fake
boto3-shaped client, so **no real Bedrock calls** happen. Backoff sleeps are
monkeypatched out so the tests are instant.
"""
from __future__ import annotations

import json

import pytest

from bakeoff import config
from bakeoff.scoring import semantic
from bakeoff.scoring.semantic import (
    SemanticSimilarityScorer,
    classify_error,
    make_bedrock_embed_fn,
)
from bakeoff.types import ErrorClass


# --- fakes ----------------------------------------------------------------
class _FakeBody:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class _FakeClientError(Exception):
    """A botocore-ClientError-shaped exception carrying a structured code."""

    def __init__(self, code: str, status: int | None = None):
        super().__init__(f"{code} raised")
        self.response = {"Error": {"Code": code}}
        if status is not None:
            self.response["ResponseMetadata"] = {"HTTPStatusCode": status}


class FlakyBedrockClient:
    """Raises ``error`` for the first ``fail_times`` calls, then returns vectors."""

    def __init__(self, error: Exception, fail_times: int, vector: list[float]):
        self.error = error
        self.fail_times = fail_times
        self.vector = vector
        self.calls = 0

    def invoke_model(self, *, modelId, body, accept, contentType):  # noqa: N803
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error
        n = len(json.loads(body)["texts"])
        return {"body": _FakeBody({"embeddings": {"float": [self.vector] * n}})}


# ===========================================================================
# Error classification
# ===========================================================================
class TestClassifyError:
    def test_expired_token_code_is_auth(self):
        assert classify_error(_FakeClientError("ExpiredTokenException")) is ErrorClass.AUTH_EXPIRED

    def test_unrecognized_client_is_auth(self):
        assert classify_error(_FakeClientError("UnrecognizedClientException")) is ErrorClass.AUTH_EXPIRED

    def test_throttling_code_is_throttled(self):
        assert classify_error(_FakeClientError("ThrottlingException")) is ErrorClass.THROTTLED

    def test_403_status_is_auth(self):
        assert classify_error(_FakeClientError("X", status=403)) is ErrorClass.AUTH_EXPIRED

    def test_429_status_is_throttled(self):
        assert classify_error(_FakeClientError("X", status=429)) is ErrorClass.THROTTLED

    def test_500_status_is_transient(self):
        assert classify_error(_FakeClientError("X", status=500)) is ErrorClass.TRANSIENT

    def test_message_signature_is_auth(self):
        err = Exception("The security token included in the request is expired")
        assert classify_error(err) is ErrorClass.AUTH_EXPIRED

    def test_unknown_is_unknown(self):
        assert classify_error(Exception("totally unrelated failure")) is ErrorClass.UNKNOWN


# ===========================================================================
# Credential-expiry refresh + retry on the default embedder
# ===========================================================================
class TestResilience:
    def test_auth_expiry_triggers_refresh_then_succeeds(self, monkeypatch):
        monkeypatch.setattr(semantic.time, "sleep", lambda *_: None)
        succeeding = FlakyBedrockClient(_FakeClientError("X"), fail_times=0, vector=[1.0, 0.0])
        always_expired = FlakyBedrockClient(
            _FakeClientError("ExpiredTokenException"), fail_times=10_000, vector=[1.0, 0.0]
        )
        factory_calls = {"n": 0}

        def factory():
            factory_calls["n"] += 1
            # first build -> perpetually-expired client; after a refresh -> success
            return always_expired if factory_calls["n"] == 1 else succeeding

        embedder = make_bedrock_embed_fn(client_factory=factory)
        out = embedder(["q"], "search_document")
        assert out == [[1.0, 0.0]]
        assert embedder.refresh_count == 1     # one refresh fixed it
        assert factory_calls["n"] == 2         # initial build + one refresh

    def test_auth_expiry_gives_up_after_max_cycles(self, monkeypatch):
        monkeypatch.setattr(semantic.time, "sleep", lambda *_: None)
        err = _FakeClientError("ExpiredTokenException")

        def factory():
            return FlakyBedrockClient(err, fail_times=10_000, vector=[1.0])

        embedder = make_bedrock_embed_fn(client_factory=factory)
        with pytest.raises(_FakeClientError):
            embedder(["q"], "search_document")
        assert embedder.refresh_count == config.AUTH_MAX_REFRESH_CYCLES

    def test_throttle_retries_without_refresh_then_succeeds(self, monkeypatch):
        monkeypatch.setattr(semantic.time, "sleep", lambda *_: None)
        flaky = FlakyBedrockClient(
            _FakeClientError("ThrottlingException", status=429), fail_times=2, vector=[0.0, 1.0]
        )
        embedder = make_bedrock_embed_fn(client_factory=lambda: flaky)
        out = embedder(["q"], "search_document")
        assert out == [[0.0, 1.0]]
        assert flaky.calls == 3            # 2 throttles + 1 success
        assert embedder.refresh_count == 0  # throttling never refreshes creds

    def test_permanent_error_is_not_retried(self, monkeypatch):
        monkeypatch.setattr(semantic.time, "sleep", lambda *_: None)
        flaky = FlakyBedrockClient(Exception("hard logic bug"), fail_times=10_000, vector=[1.0])
        embedder = make_bedrock_embed_fn(client_factory=lambda: flaky)
        with pytest.raises(Exception, match="hard logic bug"):
            embedder(["q"], "search_document")
        assert flaky.calls == 1             # tried once, not retried
        assert embedder.refresh_count == 0


# ===========================================================================
# The default embedder integrates with the scorer (still no network)
# ===========================================================================
def test_scorer_uses_resilient_default_embedder_through_a_fake_client(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic.time, "sleep", lambda *_: None)
    # One client that throttles once then serves a fixed vector for every text.
    flaky = FlakyBedrockClient(
        _FakeClientError("ThrottlingException", status=429), fail_times=1, vector=[1.0, 0.0, 0.0]
    )
    embed_fn = make_bedrock_embed_fn(client_factory=lambda: flaky)
    scorer = SemanticSimilarityScorer(
        embed_fn=embed_fn, cache_dir=tmp_path, disk_cache=False
    )
    # identical vectors for both texts -> cosine 1 -> normalized 1.0
    sim = scorer.similarity("answer", "ideal")
    assert sim == pytest.approx(1.0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
