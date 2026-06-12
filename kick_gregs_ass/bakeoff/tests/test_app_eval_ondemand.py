"""
Tests for the on-demand combinatorial eval-run capability (Task 15 / Area F / Req 22).

All OFFLINE and network-free: the FastAPI app is exercised with
:class:`fastapi.testclient.TestClient` (httpx-backed, no real server), and every
eval run uses the app's default OFFLINE producer (offline ragas + injected
offline retrieval/agent providers) or a gated offline provider the test injects.
No network, no Bedrock, no uvicorn, no AWS.

Coverage (Req 22.6, 22.9, 22.10, 22.11, 22.12):

* The cartesian product yields exactly ``|agents| x |corpus sizes| x |queries|``
  durable Instances (Req 22.6), and on-demand Instances appear via the SAME
  status / recent / stream path as every other run (Req 22.9).
* A second on-demand request while one is active is ENQUEUED in a bounded queue
  and starts only AFTER the active run completes (Req 22.10/22.11); a full queue
  is refused with 429.
* An over-threshold combinatorial request SIGNALS the confirmation requirement
  (structured 409 + ``confirmation_required``) and launches nothing; resending
  with ``confirm=true`` launches it (Req 22.12).
* The on-demand path relaxes the >= 3 agent floor to one-or-more (Req 22.2) and
  accepts retrieval-metric names alongside ragas names (Req 22.3).
"""
from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from bakeoff.app import EvalStatus, create_app
from bakeoff.eval.event_store import EvalEventStore
from bakeoff.eval.experiment_runner import AgentAnswer, combination_count

_METRICS = ["faithfulness", "answer_accuracy"]


def _make_gated_agent(gate: threading.Event):
    """An offline agent provider that blocks (in its worker thread) until ``gate``."""

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


def _make_app(tmp_path):
    return create_app(
        events_path=tmp_path / "events.jsonl",
        reports_dir=tmp_path / "reports",
        dist_dir=tmp_path / "dist-absent",
        eval_events_path=tmp_path / "eval_instances.jsonl",
    )


# ===========================================================================
# combination_count — the cartesian sizing used by the confirmation gate
# ===========================================================================
def test_combination_count_is_cartesian_product():
    assert combination_count(["a", "b"], [10, 20, 30], 4) == 2 * 3 * 4
    # duplicates collapse (the runner would reject them anyway).
    assert combination_count(["a", "a", "b"], [10, 10], 2) == 2 * 1 * 2
    assert combination_count(["a"], [10], 1) == 1


# ===========================================================================
# Req 22.6 / 22.9 — cartesian product Instances via the same status/recent path
# ===========================================================================
def test_on_demand_cartesian_product_yields_all_instances(tmp_path):
    """|agents| x |sizes| x |queries| durable Instances, visible via status+recent."""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        agents = ["agent-a", "agent-b"]  # only 2 — below the >=3 primitive (Req 22.2)
        sizes = [50, 100, 200]
        query_ids = ["q0", "q1"]
        r = client.post(
            "/api/eval/runs/start",
            json={
                "on_demand": True,
                "agents": agents,
                "metrics": _METRICS,
                "corpus_sizes": sizes,
                "query_ids": query_ids,
            },
        )
        assert r.status_code == 202
        snap = r.json()
        assert snap["enqueued"] is False
        # 2 agents x 3 sizes x 2 queries = 12 combinations (Req 22.6).
        expected = len(agents) * len(sizes) * len(query_ids)
        assert snap["combination_count"] == expected

        done = _wait_for_eval(client, lambda s: s["status"] == EvalStatus.COMPLETED)
        # Appears via the SAME status path as every other run (Req 22.9).
        assert done["instance_count"] == expected
        assert set(done["agents"]) == set(agents)
        assert set(done["corpus_sizes"]) == set(sizes)

        # And via the SAME recent-seed path (Req 22.9), shaped like every record.
        recent = client.get("/api/eval/instances/recent").json()
        assert recent["total"] == expected
        # Exactly one Instance per (agent, size, query) cartesian cell.
        cells = {
            (row["agent_id"], int(row["corpus_size"]), row["instance_id"].rsplit(":", 1)[-1])
            for row in recent["instances"]
        }
        assert len(cells) == expected

        # The durable Event_Store agrees (single source of truth).
        n = len(EvalEventStore(client.app.state.bakeoff.eval_events_path).read_all())
        assert n == expected


def test_on_demand_single_agent_single_size_single_query(tmp_path):
    """The smallest possible on-demand pool: one agent, one size, one query."""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        r = client.post(
            "/api/eval/runs/start",
            json={
                "on_demand": True,
                "agents": ["agent-c"],
                "metrics": _METRICS,
                "query_ids": ["q0"],
            },
        )
        assert r.status_code == 202
        assert r.json()["combination_count"] == 1
        done = _wait_for_eval(client, lambda s: s["status"] == EvalStatus.COMPLETED)
        assert done["instance_count"] == 1


def test_on_demand_accepts_retrieval_metric_names(tmp_path):
    """An on-demand request may select retrieval-metric entries too (Req 22.3)."""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        r = client.post(
            "/api/eval/runs/start",
            json={
                "on_demand": True,
                "agents": ["agent-a"],
                "metrics": ["faithfulness", "precision_at_k", "ndcg_at_k"],
                "query_ids": ["q0"],
            },
        )
        assert r.status_code == 202
        _wait_for_eval(client, lambda s: s["status"] == EvalStatus.COMPLETED)


def test_on_demand_unknown_metric_is_422(tmp_path):
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        r = client.post(
            "/api/eval/runs/start",
            json={
                "on_demand": True,
                "agents": ["agent-a"],
                "metrics": ["not_a_real_metric"],
            },
        )
        assert r.status_code == 422
        assert client.get("/api/eval/status").json()["status"] == EvalStatus.IDLE


# ===========================================================================
# Req 22.10 / 22.11 — at most one active; a second request is enqueued (bounded)
# ===========================================================================
def test_second_on_demand_request_is_enqueued_and_starts_after_active(tmp_path):
    """A second on-demand request while one is active is enqueued and runs next."""
    gate = threading.Event()  # held closed -> the first run stays running
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        client.app.state.bakeoff.eval_agent_provider = _make_gated_agent(gate)

        # First on-demand run: 1 agent x 1 size x 1 query = 1 instance, gated open.
        r1 = client.post(
            "/api/eval/runs/start",
            json={"on_demand": True, "agents": ["agent-a"], "metrics": _METRICS, "query_ids": ["q0"]},
        )
        assert r1.status_code == 202
        assert r1.json()["status"] == EvalStatus.RUNNING
        _wait_for_eval(client, lambda s: s["status"] == EvalStatus.RUNNING)

        # Second on-demand run arrives while the first is active -> ENQUEUED, not
        # 409 (Req 22.11). It targets a DISTINCT agent set so we can prove it ran.
        r2 = client.post(
            "/api/eval/runs/start",
            json={"on_demand": True, "agents": ["agent-b", "agent-c"], "metrics": _METRICS, "query_ids": ["q0"]},
        )
        assert r2.status_code == 202
        body2 = r2.json()
        assert body2["enqueued"] is True
        assert body2["queue_depth"] == 1
        # The first run is still the active one (the queued one has NOT started).
        assert body2["status"] == EvalStatus.RUNNING

        # Release the gate: the first run drains, THEN the queued run starts and
        # drains. The final durable state must reflect BOTH runs' instances.
        gate.set()
        done = _wait_for_eval(
            client,
            lambda s: s["status"] == EvalStatus.COMPLETED
            and {"agent-a", "agent-b", "agent-c"} <= set(s["agents"]),
        )
        # run 1: agent-a x q0 = 1 ; run 2: {agent-b, agent-c} x q0 = 2 ; total 3.
        assert done["instance_count"] == 3
        assert set(done["agents"]) == {"agent-a", "agent-b", "agent-c"}


def test_on_demand_queue_is_bounded_and_full_is_429(tmp_path):
    """When the bounded queue is full, a further on-demand request is refused 429."""
    gate = threading.Event()
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        state = client.app.state.bakeoff
        state.eval_agent_provider = _make_gated_agent(gate)
        state.eval_queue_max = 1  # tiny bound so the queue fills immediately

        # Active run (gated open).
        r1 = client.post(
            "/api/eval/runs/start",
            json={"on_demand": True, "agents": ["agent-a"], "metrics": _METRICS, "query_ids": ["q0"]},
        )
        assert r1.status_code == 202
        _wait_for_eval(client, lambda s: s["status"] == EvalStatus.RUNNING)

        # Fills the single queue slot.
        r2 = client.post(
            "/api/eval/runs/start",
            json={"on_demand": True, "agents": ["agent-b"], "metrics": _METRICS, "query_ids": ["q0"]},
        )
        assert r2.status_code == 202 and r2.json()["enqueued"] is True

        # Queue is now full -> 429, nothing dropped silently.
        r3 = client.post(
            "/api/eval/runs/start",
            json={"on_demand": True, "agents": ["agent-c"], "metrics": _METRICS, "query_ids": ["q0"]},
        )
        assert r3.status_code == 429
        assert r3.json()["queue_depth"] == 1

        gate.set()
        _wait_for_eval(
            client,
            lambda s: s["status"] == EvalStatus.COMPLETED and {"agent-a", "agent-b"} <= set(s["agents"]),
        )


# ===========================================================================
# Req 22.12 — over-threshold request signals the confirmation requirement
# ===========================================================================
def test_over_threshold_request_signals_confirmation_and_launches_nothing(tmp_path):
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        client.app.state.bakeoff.eval_ondemand_threshold = 4  # tiny threshold

        # 2 agents x 1 size x 4 queries = 8 > 4 -> confirmation required.
        r = client.post(
            "/api/eval/runs/start",
            json={
                "on_demand": True,
                "agents": ["agent-a", "agent-b"],
                "metrics": _METRICS,
                "query_ids": ["q0", "q1", "q2", "q3"],
            },
        )
        assert r.status_code == 409
        body = r.json()
        assert body["confirmation_required"] is True
        assert body["combination_count"] == 8
        assert body["threshold"] == 4
        # Nothing launched: the lifecycle is still idle.
        assert client.get("/api/eval/status").json()["status"] == EvalStatus.IDLE


def test_over_threshold_with_confirm_true_launches(tmp_path):
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        client.app.state.bakeoff.eval_ondemand_threshold = 4

        r = client.post(
            "/api/eval/runs/start",
            json={
                "on_demand": True,
                "agents": ["agent-a", "agent-b"],
                "metrics": _METRICS,
                "query_ids": ["q0", "q1", "q2", "q3"],
                "confirm": True,
            },
        )
        assert r.status_code == 202
        assert r.json()["combination_count"] == 8
        done = _wait_for_eval(client, lambda s: s["status"] == EvalStatus.COMPLETED)
        assert done["instance_count"] == 8


def test_under_threshold_needs_no_confirmation(tmp_path):
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        client.app.state.bakeoff.eval_ondemand_threshold = 100
        r = client.post(
            "/api/eval/runs/start",
            json={
                "on_demand": True,
                "agents": ["agent-a", "agent-b"],
                "metrics": _METRICS,
                "query_ids": ["q0", "q1"],
            },
        )
        assert r.status_code == 202
        _wait_for_eval(client, lambda s: s["status"] == EvalStatus.COMPLETED)
