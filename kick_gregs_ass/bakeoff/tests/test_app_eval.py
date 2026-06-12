"""
Tests for the eval-dashboard endpoints on :mod:`bakeoff.app` (Task 4 / Area B/C/E).

All OFFLINE and network-free: the FastAPI app is exercised with
:class:`fastapi.testclient.TestClient` (httpx-backed, no real server), and every
eval run uses the app's default OFFLINE producer (offline ragas + injected offline
retrieval/agent providers) or a gated offline provider the test injects. No
network, no Bedrock, no uvicorn, no AWS.

Coverage (Req 8.3, 8.4, 15.1, 15.2, 15.5):

* ``POST /api/eval/runs/start`` returns **202** + a snapshot and flips the
  lifecycle to ``running``; a second immediate start returns **409**; an unknown
  agent or metric returns **422** and starts nothing.
* ``GET /api/eval/status`` is well-formed before and after a run and never 500s —
  including against a deliberately malformed Event_Store (Req 15.5).
* ``GET /api/eval/stream`` opens (``text/event-stream``) over the DEDICATED eval
  broker and flushes its initial ``: connected`` frame.
* ``GET /api/eval/instances/recent`` replays seed rows shaped identically to the
  ``eval_instance_appended`` event payload.
* **Broker isolation:** an eval event NEVER appears on the bake-off ``/api/stream``
  broker or either optimizer broker, and exactly one ``eval_instance_appended`` is
  published per appended record.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from bakeoff.app import (
    EVAL_AGENTS,
    EVAL_INSTANCE_EVENT,
    EvalStatus,
    create_app,
)
from bakeoff.eval.event_store import EvalEventStore
from bakeoff.eval.experiment_runner import AgentAnswer

# A small, valid metric subset (keeps offline runs fast). Both are real catalog
# names (bakeoff.eval.catalog.CATALOG).
_METRICS = ["faithfulness", "answer_accuracy"]
_AGENTS3 = ["agent-a", "agent-b", "agent-c"]

# The keys every recent-seed row / eval_instance_appended payload carries
# (EvalInstance.to_dict()), so seed and live deltas share one client code path.
_INSTANCE_KEYS = {
    "instance_id",
    "agent_id",
    "session_id",
    "instance_index",
    "timestamp",
    "latency_ms",
    "stage_timings",
    "corpus_size",
    "retrieval_cached",
    "ragas",
    "retrieval",
    "status",
}

_STATUS_KEYS = {
    "status",
    "request",
    "error",
    "agents",
    "sessions",
    "corpus_sizes",
    "instance_count",
    "instances",
    "rollups",
    "sweep",
}


# ===========================================================================
# Fixtures
# ===========================================================================
@pytest.fixture
def client(tmp_path):
    """A TestClient over temp stores (bake-off + eval), with a convenience handle."""
    app = create_app(
        events_path=tmp_path / "events.jsonl",
        reports_dir=tmp_path / "reports",
        dist_dir=tmp_path / "dist-absent",  # intentionally absent
        eval_events_path=tmp_path / "eval_instances.jsonl",
    )
    c = TestClient(app)
    c.app_state = app.state.bakeoff
    return c


def _make_gated_agent(gate: threading.Event):
    """An offline agent provider that blocks (in its worker thread) until ``gate``.

    Holds a launched run in ``running`` deterministically so a second start can be
    observed returning 409, then releases so the run drains to completion. The
    Experiment_Runner runs on a worker thread (``asyncio.to_thread``), so this
    thread-blocking poll never blocks the event loop — the status route keeps
    serving meanwhile.
    """

    def gated_agent(agent_id, query, retrieval):
        while not gate.is_set():
            time.sleep(0.005)
        return AgentAnswer(answer=f"{agent_id}:{query.query_id}", generation_ms=1.0)

    return gated_agent


def _wait_for_eval(client, predicate, *, timeout=5.0):
    """Poll ``GET /api/eval/status`` until ``predicate(snapshot)`` or time out."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get("/api/eval/status").json()
        if predicate(last):
            return last
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting on eval snapshot; last={last!r}")


# ===========================================================================
# POST /api/eval/runs/start — 202 / running / 409 / 422
# ===========================================================================
def test_eval_start_202_flips_to_running_then_second_start_409(tmp_path):
    """202 + status running on start; an immediate second start is refused (409)."""
    gate = threading.Event()  # held closed -> the run stays running
    app = create_app(
        events_path=tmp_path / "events.jsonl",
        reports_dir=tmp_path / "reports",
        dist_dir=tmp_path / "dist-absent",
        eval_events_path=tmp_path / "eval_instances.jsonl",
    )
    # Context-manager form so the lifespan/event loop runs the background task.
    with TestClient(app) as client:
        client.app.state.bakeoff.eval_agent_provider = _make_gated_agent(gate)

        r1 = client.post(
            "/api/eval/runs/start",
            json={"agents": _AGENTS3, "metrics": _METRICS, "num_queries": 1},
        )
        assert r1.status_code == 202
        snap = r1.json()
        assert _STATUS_KEYS <= set(snap)
        assert snap["status"] == EvalStatus.RUNNING

        # The durable status endpoint also reports running while gated.
        st = client.get("/api/eval/status").json()
        assert st["status"] == EvalStatus.RUNNING

        # A second immediate start must be refused with 409 (run already active).
        r2 = client.post("/api/eval/runs/start", json={"agents": _AGENTS3})
        assert r2.status_code == 409
        assert r2.json() == {"detail": "an eval run is already active"}

        # Release the gate and let the background run drain to completion.
        gate.set()
        done = _wait_for_eval(client, lambda s: s["status"] == EvalStatus.COMPLETED)
        # 3 agents x 1 query x 1 corpus size = 3 durable instances.
        assert done["instance_count"] == 3
        assert set(done["agents"]) == set(_AGENTS3)


def test_eval_start_default_body_launches_offline_run(tmp_path):
    """An omitted agent/metric set defaults to the configured set and runs offline."""
    app = create_app(
        events_path=tmp_path / "events.jsonl",
        reports_dir=tmp_path / "reports",
        dist_dir=tmp_path / "dist-absent",
        eval_events_path=tmp_path / "eval_instances.jsonl",
    )
    with TestClient(app) as client:
        r = client.post("/api/eval/runs/start", json={"num_queries": 1})
        assert r.status_code == 202
        assert r.json()["status"] == EvalStatus.RUNNING
        done = _wait_for_eval(client, lambda s: s["status"] == EvalStatus.COMPLETED)
        # default agent set x 1 query x 1 corpus size.
        assert done["instance_count"] == len(EVAL_AGENTS)
        assert set(done["agents"]) == set(EVAL_AGENTS)


def test_eval_start_unknown_agent_is_422(client):
    """An agent id outside the configured set is a clean 422 and starts nothing."""
    r = client.post(
        "/api/eval/runs/start",
        json={"agents": ["agent-a", "agent-b", "totally-unknown"]},
    )
    assert r.status_code == 422
    assert client.get("/api/eval/status").json()["status"] == EvalStatus.IDLE


def test_eval_start_too_few_agents_is_422(client):
    """Fewer than three agents fails the multi-agent-comparison floor (422)."""
    r = client.post("/api/eval/runs/start", json={"agents": ["agent-a", "agent-b"]})
    assert r.status_code == 422
    assert client.get("/api/eval/status").json()["status"] == EvalStatus.IDLE


def test_eval_start_unknown_metric_is_422(client):
    """A metric name outside the ragas catalog is a clean 422 and starts nothing."""
    r = client.post(
        "/api/eval/runs/start",
        json={"agents": _AGENTS3, "metrics": ["not_a_real_metric"]},
    )
    assert r.status_code == 422
    assert client.get("/api/eval/status").json()["status"] == EvalStatus.IDLE


# ===========================================================================
# GET /api/eval/status — well-formed before/after; never 500s on a bad store
# ===========================================================================
def test_eval_status_empty_but_well_formed_before_any_run(client):
    body = client.get("/api/eval/status").json()
    assert body["status"] == EvalStatus.IDLE
    assert _STATUS_KEYS <= set(body)
    assert body["instance_count"] == 0
    assert body["agents"] == [] and body["sessions"] == [] and body["instances"] == []
    assert body["sweep"] == {
        "requested_sizes": [],
        "completed_sizes": [],
        "remaining": [],
    }


def test_eval_status_never_500s_on_malformed_store(client):
    """A malformed (corrupt non-final line) Event_Store degrades, never 500s (Req 15.5)."""
    p = client.app_state.eval_events_path
    p.parent.mkdir(parents=True, exist_ok=True)
    # Two lines, the FIRST malformed (valid JSON but not an EvalInstance) — a
    # non-final corrupt line, which EvalEventStore surfaces as a hard error. The
    # snapshot must catch it and degrade to the empty-but-well-formed shape.
    p.write_text('{"foo": 1}\n{"bar": 2}\n', encoding="utf-8")

    r = client.get("/api/eval/status")
    assert r.status_code == 200
    body = r.json()
    assert _STATUS_KEYS <= set(body)
    assert body["instance_count"] == 0
    assert body["instances"] == []


# ===========================================================================
# GET /api/eval/stream — opens over the DEDICATED eval broker (no replay)
# ===========================================================================
def test_eval_stream_route_is_registered(client):
    """The eval stream route exists and is wired (its open/flush behavior is proved
    at the generator level below, which is exactly what the route returns —
    ``StreamingResponse(state.eval_broker.subscribe(), ...)``). We avoid reading the
    body over HTTP here because the SSE stream is intentionally infinite."""
    paths = {r.path for r in client.app.routes if hasattr(r, "path")}
    assert "/api/eval/stream" in paths


def test_eval_stream_uses_a_dedicated_broker_generator(client):
    """The eval stream drains its OWN broker (not the bake-off / optimizer ones)."""
    state = client.app_state

    async def scenario():
        gen = state.eval_broker.subscribe(heartbeat=0.01)
        first = await gen.__anext__()  # ": connected"
        # An event published on the eval broker reaches this stream...
        state.eval_broker.publish(EVAL_INSTANCE_EVENT, {"instance_id": "x"})
        frame = await gen.__anext__()
        await gen.aclose()
        return first, frame

    first, frame = asyncio.run(scenario())
    assert first == ": connected\n\n"
    assert frame.startswith(f"event: {EVAL_INSTANCE_EVENT}")


# ===========================================================================
# GET /api/eval/instances/recent — replay seed shaped like the stream payload
# ===========================================================================
def test_eval_recent_empty_when_no_log(client):
    body = client.get("/api/eval/instances/recent").json()
    assert body == {"instances": [], "total": 0}


def test_eval_recent_replays_seed_in_stream_payload_shape(tmp_path):
    app = create_app(
        events_path=tmp_path / "events.jsonl",
        reports_dir=tmp_path / "reports",
        dist_dir=tmp_path / "dist-absent",
        eval_events_path=tmp_path / "eval_instances.jsonl",
    )
    with TestClient(app) as client:
        r = client.post(
            "/api/eval/runs/start",
            json={"agents": _AGENTS3, "metrics": _METRICS, "num_queries": 2},
        )
        assert r.status_code == 202
        _wait_for_eval(client, lambda s: s["status"] == EvalStatus.COMPLETED)

        body = client.get("/api/eval/instances/recent").json()
        # 3 agents x 2 queries x 1 corpus size = 6 records.
        assert body["total"] == 6
        assert len(body["instances"]) == 6
        for row in body["instances"]:
            assert _INSTANCE_KEYS <= set(row)
            # ragas/retrieval are the two disjoint metric maps (never conflated).
            assert isinstance(row["ragas"], dict)
            assert isinstance(row["retrieval"], dict)


def test_eval_recent_honors_limit(tmp_path):
    app = create_app(
        events_path=tmp_path / "events.jsonl",
        reports_dir=tmp_path / "reports",
        dist_dir=tmp_path / "dist-absent",
        eval_events_path=tmp_path / "eval_instances.jsonl",
    )
    with TestClient(app) as client:
        client.post(
            "/api/eval/runs/start",
            json={"agents": _AGENTS3, "metrics": _METRICS, "num_queries": 2},
        )
        _wait_for_eval(client, lambda s: s["status"] == EvalStatus.COMPLETED)
        body = client.get("/api/eval/instances/recent", params={"limit": 2}).json()
        assert len(body["instances"]) == 2
        assert body["total"] == 6  # total is the full count, not the windowed cap


# ===========================================================================
# Broker isolation — eval events never leak onto other streams (hard constraint)
# ===========================================================================
def test_eval_broker_is_isolated_from_bakeoff_and_optimizer_brokers(client):
    """A direct publish on the eval broker reaches only eval subscribers."""
    state = client.app_state
    # The eval broker is a distinct instance from every other broker.
    assert state.eval_broker is not state.broker
    assert state.eval_broker is not state.optimizer_v2_broker

    eval_sub = state.eval_broker.open()
    bake_sub = state.broker.open()
    v2_sub = state.optimizer_v2_broker.open()

    state.eval_broker.publish(EVAL_INSTANCE_EVENT, {"instance_id": "iso-1"})

    # Only the eval subscriber received it.
    (etype, payload) = eval_sub.queue.get_nowait()
    assert etype == EVAL_INSTANCE_EVENT and payload["instance_id"] == "iso-1"
    assert eval_sub.queue.empty()
    # The bake-off and optimizer brokers saw nothing.
    assert bake_sub.queue.empty()
    assert v2_sub.queue.empty()


def test_eval_run_publishes_one_event_per_record_and_isolated(tmp_path):
    """A full offline run publishes exactly one ``eval_instance_appended`` per record
    on the eval broker, and NONE on the bake-off / optimizer brokers."""
    eval_path = tmp_path / "eval_instances.jsonl"
    app = create_app(
        events_path=tmp_path / "events.jsonl",
        reports_dir=tmp_path / "reports",
        dist_dir=tmp_path / "dist-absent",
        eval_events_path=eval_path,
    )
    state = app.state.bakeoff

    async def scenario():
        eval_sub = state.eval_broker.open()
        bake_sub = state.broker.open()
        v2_sub = state.optimizer_v2_broker.open()

        await state.start_eval_run(
            agents=_AGENTS3, metrics=_METRICS, num_queries=2
        )
        await state._eval_task
        # Let the loop drain any call_soon_threadsafe publishes from the worker.
        await asyncio.sleep(0.1)

        def drain(sub):
            out = []
            while not sub.queue.empty():
                out.append(sub.queue.get_nowait())
            return out

        return drain(eval_sub), drain(bake_sub), drain(v2_sub)

    eval_frames, bake_frames, v2_frames = asyncio.run(scenario())

    appended = [p for (etype, p) in eval_frames if etype == EVAL_INSTANCE_EVENT]
    n_records = len(EvalEventStore(eval_path).read_all())
    assert n_records == 6  # 3 agents x 2 queries x 1 corpus size
    # exactly one eval_instance_appended per appended durable record.
    assert len(appended) == n_records
    # the payload shape matches the recent-seed row shape (one client code path).
    for p in appended:
        assert _INSTANCE_KEYS <= set(p)

    # Hard isolation: NOTHING (not even the final eval_status) leaked onto the
    # bake-off or optimizer brokers.
    assert bake_frames == []
    assert v2_frames == []
