"""
Regression tests for the retrieval backend's Bedrock credential resilience
(src/bedrock_client.py).

The first overnight bake-off run died because the retrieval backend's AWS
credentials expired ~40 min in: it cached one boto3 client at import and never
rebuilt it, so every embed/rerank call raised ``ExpiredTokenException`` and every
``/retrieve`` 500'd until the harness auto-paused. The fix wraps each Bedrock call
in a rebuild-session-and-retry loop. These tests pin that behavior with NO network
(fake boto3 clients only).
"""
import io
import json

import pytest
from botocore.exceptions import ClientError

import src.bedrock_client as bc


def _expired_error():
    return ClientError(
        {"Error": {"Code": "ExpiredTokenException",
                   "Message": "The security token included in the request is expired"}},
        "InvokeModel",
    )


def _embed_body(vec):
    return {"body": io.BytesIO(json.dumps({"embeddings": {"float": [vec]}}).encode())}


@pytest.fixture(autouse=True)
def _reset_clients(monkeypatch):
    # Each test drives its own fake clients + no real backoff sleep.
    monkeypatch.setattr(bc, "_runtime", None)
    monkeypatch.setattr(bc, "_agent_runtime", None)
    monkeypatch.setattr(bc, "_REFRESH_BACKOFF_S", 0)
    yield


def test_embed_recovers_from_expired_token_by_rebuilding(monkeypatch):
    calls = {"n": 0}

    class FakeRuntime:
        def invoke_model(self, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _expired_error()
            return _embed_body([0.1, 0.2, 0.3])

    builds = {"n": 0}

    def fake_build():
        builds["n"] += 1
        bc._runtime = FakeRuntime()
        bc._agent_runtime = object()

    monkeypatch.setattr(bc, "_build_clients", fake_build)

    out = bc.embed(["q"], "search_query")
    assert out == [[0.1, 0.2, 0.3]]
    assert calls["n"] == 2          # one expired, one success after rebuild
    assert builds["n"] >= 2          # initial ensure + at least one refresh rebuild


def test_embed_gives_up_after_max_cycles(monkeypatch):
    class AlwaysExpired:
        def invoke_model(self, **kw):
            raise _expired_error()

    def fake_build():
        bc._runtime = AlwaysExpired()
        bc._agent_runtime = object()

    monkeypatch.setattr(bc, "_build_clients", fake_build)
    monkeypatch.setattr(bc, "_MAX_REFRESH_CYCLES", 3)

    with pytest.raises(ClientError):
        bc.embed(["q"], "search_query")


def test_non_auth_error_propagates_without_retry(monkeypatch):
    calls = {"n": 0}

    class FakeRuntime:
        def invoke_model(self, **kw):
            calls["n"] += 1
            raise ClientError(
                {"Error": {"Code": "ValidationException", "Message": "bad input"}},
                "InvokeModel",
            )

    builds = {"n": 0}

    def fake_build():
        builds["n"] += 1
        bc._runtime = FakeRuntime()
        bc._agent_runtime = object()

    monkeypatch.setattr(bc, "_build_clients", fake_build)

    with pytest.raises(ClientError) as ei:
        bc.embed(["q"], "search_query")
    assert ei.value.response["Error"]["Code"] == "ValidationException"
    assert calls["n"] == 1           # a validation error is NOT retried
    assert builds["n"] == 1          # only the initial ensure; no refresh rebuild


def test_rerank_recovers_from_expired_token(monkeypatch):
    calls = {"n": 0}

    class FakeAgentRuntime:
        def rerank(self, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _expired_error()
            return {"results": [{"index": 0, "relevanceScore": 0.9},
                                {"index": 1, "relevanceScore": 0.4}]}

    def fake_build():
        bc._runtime = object()
        bc._agent_runtime = FakeAgentRuntime()

    monkeypatch.setattr(bc, "_build_clients", fake_build)

    out = bc.rerank("q", ["doc a", "doc b"], top_n=2)
    assert out == [{"index": 0, "score": 0.9}, {"index": 1, "score": 0.4}]
    assert calls["n"] == 2           # one expired, one success after rebuild


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
