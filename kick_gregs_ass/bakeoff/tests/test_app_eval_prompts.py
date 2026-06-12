"""
Tests for the eval-dashboard prompt-management endpoints + the Metric_Engine's
prompt-config provenance (Task 13.2 / Area D / Req 16).

All OFFLINE and network-free: the FastAPI app is exercised with
:class:`fastapi.testclient.TestClient`, and the Metric_Engine path is driven
directly with an offline :class:`RagasAdapter` wired to a real
:class:`~bakeoff.eval.prompt_store.PromptStore` on a temp file. No network, no
Bedrock, no uvicorn.

Coverage (Req 16.3–16.7, 19.1):

* ``GET /api/eval/prompts`` lists every catalog metric with its active prompt
  config; ``PUT /api/eval/prompts/{metric}`` round-trips an override and a reset.
* a changed prompt applies ONLY to instances computed after the change, while
  every previously recorded value is unchanged (Req 16.5).
* the prompt-config id is recorded alongside each ragas value (Req 16.6).
* retrieval values are never mutated by a prompt change (Req 19.1).
* an unknown metric → 404; a non-customizable metric → 422 (Req 16.7).
* the whole path issues no network call in fake mode (Req 1.5).
"""
from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient

from bakeoff.app import create_app
from bakeoff.eval.event_store import EvalEventStore
from bakeoff.eval.metric_engine import MetricEngine
from bakeoff.eval.prompt_store import (
    PromptStore,
    UnknownMetricError,
    PromptNotCustomizableError,
    default_prompt_config,
)
from bakeoff.eval.ragas_adapter import RagasAdapter, RagasSample


# ===========================================================================
# Fixtures
# ===========================================================================
@pytest.fixture
def client(tmp_path):
    """A TestClient over temp eval stores (events + prompt overrides)."""
    app = create_app(
        events_path=tmp_path / "events.jsonl",
        reports_dir=tmp_path / "reports",
        dist_dir=tmp_path / "dist-absent",
        eval_events_path=tmp_path / "eval_instances.jsonl",
        eval_prompts_path=tmp_path / "eval_prompts.json",
    )
    c = TestClient(app)
    c.app_state = app.state.bakeoff
    return c


def _sample(answer: str = "Paris is the capital of France.") -> RagasSample:
    return RagasSample(
        question="What is the capital of France?",
        answer=answer,
        contexts=["France is a country in Europe. Its capital city is Paris."],
        reference="Paris is the capital of France.",
    )


# ===========================================================================
# GET /api/eval/prompts — lists the catalog + active config
# ===========================================================================
def test_get_prompts_lists_catalog_with_active_config(client):
    body = client.get("/api/eval/prompts").json()
    assert "prompts" in body
    rows = {r["name"]: r for r in body["prompts"]}

    # a customizable metric is editable, defaults to version 0 / a default id.
    faith = rows["faithfulness"]
    assert faith["customizable"] is True
    assert faith["external"] is True
    assert faith["version"] == 0
    assert faith["is_override"] is False
    assert faith["config_id"] == "faithfulness:default"
    assert isinstance(faith["instruction"], str) and faith["instruction"]
    assert isinstance(faith["examples"], list)

    # a non-customizable metric (embedding-only) is flagged not editable (Req 16.7).
    sem = rows["semantic_similarity"]
    assert sem["customizable"] is False


# ===========================================================================
# PUT /api/eval/prompts/{metric} — override round-trip + reset (Req 16.3, 16.4)
# ===========================================================================
def test_put_override_round_trips_then_get_reflects_it(client):
    put = client.put(
        "/api/eval/prompts/faithfulness",
        json={
            "instruction": "Score faithfulness strictly against the context only.",
            "examples": [{"input": "q + ctx + ans", "output": "0.0 or 1.0"}],
        },
    )
    assert put.status_code == 200
    row = put.json()
    assert row["name"] == "faithfulness"
    assert row["is_override"] is True
    assert row["version"] == 1
    assert row["config_id"].startswith("faithfulness:v1:")
    assert row["instruction"].startswith("Score faithfulness strictly")
    assert row["examples"] == [{"input": "q + ctx + ans", "output": "0.0 or 1.0"}]

    # GET now reflects the override.
    rows = {r["name"]: r for r in client.get("/api/eval/prompts").json()["prompts"]}
    assert rows["faithfulness"]["is_override"] is True
    assert rows["faithfulness"]["config_id"] == row["config_id"]


def test_put_then_reset_returns_to_default(client):
    client.put(
        "/api/eval/prompts/faithfulness",
        json={"instruction": "override one", "examples": []},
    )
    reset = client.put("/api/eval/prompts/faithfulness", json={"reset": True})
    assert reset.status_code == 200
    row = reset.json()
    assert row["is_override"] is False
    assert row["version"] == 0
    assert row["config_id"] == "faithfulness:default"


def test_put_unknown_metric_is_404(client):
    r = client.put(
        "/api/eval/prompts/not_a_real_metric",
        json={"instruction": "x", "examples": []},
    )
    assert r.status_code == 404


def test_put_non_customizable_metric_is_422(client):
    # semantic_similarity is embedding-only -> not prompt-customizable (Req 16.7).
    r = client.put(
        "/api/eval/prompts/semantic_similarity",
        json={"instruction": "x", "examples": []},
    )
    assert r.status_code == 422


def test_put_missing_instruction_is_422(client):
    r = client.put("/api/eval/prompts/faithfulness", json={"examples": []})
    assert r.status_code == 422


# ===========================================================================
# A changed prompt applies ONLY to later instances; prior values unchanged
# (Req 16.5/16.6) + retrieval never mutated (Req 19.1) + no network (Req 1.5)
# ===========================================================================
def test_prompt_change_applies_only_to_later_instances(tmp_path, monkeypatch):
    """The Metric_Engine reads the active prompt at score time, so a mid-run change
    stamps the new config id only on instances scored after it, and the durable
    record of an earlier instance is untouched (Req 16.5/16.6)."""

    def _no_socket(*args, **kwargs):  # pragma: no cover - only runs if violated
        raise AssertionError("the offline prompt path must not open a socket")

    monkeypatch.setattr(socket, "socket", _no_socket)

    store = PromptStore(tmp_path / "prompts.json")
    events = EvalEventStore(tmp_path / "eval.jsonl")
    adapter = RagasAdapter.offline(
        enabled_metrics=["faithfulness"], prompt_store=store
    )
    engine = MetricEngine(events, ragas_adapter=adapter)

    def _score(instance_id: str):
        return engine.score_instance(
            instance_id=instance_id,
            agent_id="agent-a",
            session_id="sess-1",
            instance_index=int(instance_id[-1]),
            timestamp="2025-01-01T00:00:00Z",
            latency_ms=10.0,
            corpus_size=100,
            ragas_sample=_sample(),
            ranked_ids=["d1", "d2", "d3"],
            gold_ids=["d1"],
            k=3,
        )

    # 1) score BEFORE any override -> default config id, retrieval recorded.
    before = _score("inst-0")
    assert before.ragas["faithfulness"].prompt_config_id == "faithfulness:default"
    retrieval_before = {
        n: (mv.value, mv.k) for n, mv in before.retrieval.items()
    }
    assert retrieval_before, "retrieval metrics must have been recorded"

    # 2) change the prompt mid-run.
    new_cfg = store.set_override(
        "faithfulness",
        instruction="A stricter faithfulness instruction.",
        examples=[{"input": "i", "output": "o"}],
    )
    assert new_cfg.config_id.startswith("faithfulness:v1:")

    # 3) score AFTER the change -> the NEW config id.
    after = _score("inst-1")
    assert after.ragas["faithfulness"].prompt_config_id == new_cfg.config_id
    assert after.ragas["faithfulness"].prompt_config_id != "faithfulness:default"

    # 4) the EARLIER durable record is unchanged: re-read from the store and
    #    confirm its config id is still the default and its retrieval is identical.
    reread = {i.instance_id: i for i in events.read_all()}
    prior = reread["inst-0"]
    assert prior.ragas["faithfulness"].prompt_config_id == "faithfulness:default"
    assert {
        n: (mv.value, mv.k) for n, mv in prior.retrieval.items()
    } == retrieval_before, "a prompt change must never mutate recorded retrieval"


def test_prompt_store_rejects_unknown_and_non_customizable(tmp_path):
    store = PromptStore(tmp_path / "prompts.json")
    with pytest.raises(UnknownMetricError):
        store.set_override("nope", instruction="x")
    with pytest.raises(PromptNotCustomizableError):
        store.set_override("semantic_similarity", instruction="x")


def test_prompt_config_id_is_content_stable(tmp_path):
    """The same override content yields the same id; different content differs."""
    s1 = PromptStore(tmp_path / "a.json")
    s2 = PromptStore(tmp_path / "b.json")
    c1 = s1.set_override("faithfulness", instruction="same", examples=[])
    c2 = s2.set_override("faithfulness", instruction="same", examples=[])
    assert c1.config_id == c2.config_id
    c3 = s2.set_override("faithfulness", instruction="different", examples=[])
    assert c3.config_id != c1.config_id


def test_default_prompt_config_renders_for_a_sample(tmp_path):
    cfg = default_prompt_config("faithfulness")
    rendered = cfg.render("a sample question + context + answer")
    assert "a sample question + context + answer" in rendered
    assert cfg.instruction.split(".")[0] in rendered
