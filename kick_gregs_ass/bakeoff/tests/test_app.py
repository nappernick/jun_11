"""
Tests for :mod:`bakeoff.app` — the FastAPI backend (Task 12, Req 10/13/15).

All OFFLINE: the FastAPI app is exercised with :class:`fastapi.testclient.TestClient`
(httpx-backed, no real server), and any live run reuses the deterministic offline
doubles from ``test_runner.py`` (MockAdapter + FakeRetrieval + StubScoring). No
network, no Bedrock, no uvicorn.

Coverage:
* SSE fan-out: the :class:`~bakeoff.app.SSEBroker` delivers exactly one message
  per published event to every connected subscriber (Req 10.3), and implements
  the runner's ``CompletionBroker`` seam so a real ``schedule_run`` streams live.
* ``/api/models`` returns the run snapshot shape (idle when no run) (Req 10.1).
* ``/api/aggregate`` returns CI-bearing aggregates with ``normal_approx`` method
  and refuses to blend accuracy across answerability (Req 10.4, P4).
* ``/api/control/{pause,resume,abort}`` toggles the RunController and returns the
  new status; 409 when no active run, 404 on an unknown action (Req 10.5).
* ``/exec/aggregate`` refuses a CI-less report (Property 10 → 422) and serves a
  clean one.
* loopback/no-auth posture: the app records a loopback bind target; ``serve``
  refuses a non-loopback host without an explicit override (Req 15.1/15.2).
* static serving: ``/`` returns a graceful JSON stub when ``dist/`` is absent,
  and serves the bundle's ``index.html`` when present.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

import bakeoff.config as config
from bakeoff.app import (
    SSEBroker,
    JudgeStatus,
    create_app,
    is_loopback_host,
    serve,
)
from bakeoff.eventlog import append_event, read_events
from bakeoff.judge_phase2 import JudgeScoreRecord, append_judge_score
from bakeoff.runner import RunController, RunStatus, planned_trials, schedule_run
from bakeoff.tests.test_runner import (
    FakeRetrieval,
    StubScoring,
    _instant_sleep,
    build_plan,
    make_item,
)
from bakeoff.adapters.mock import MockAdapter
from bakeoff.types import JudgeScores


# ===========================================================================
# SSEBroker — exactly-once-per-subscriber fan-out (Req 10.3)
# ===========================================================================
def test_broker_delivers_each_event_once_to_every_subscriber():
    broker = SSEBroker()
    sub_a = broker.open()
    sub_b = broker.open()
    assert broker.subscriber_count == 2

    broker.publish("trial_completed", {"trial_id": "t1"})
    broker.publish("trial_completed", {"trial_id": "t2"})

    # each subscriber received both events, in order, exactly once.
    got_a = [sub_a.queue.get_nowait() for _ in range(2)]
    got_b = [sub_b.queue.get_nowait() for _ in range(2)]
    assert [p["trial_id"] for _, p in got_a] == ["t1", "t2"]
    assert [p["trial_id"] for _, p in got_b] == ["t1", "t2"]
    assert sub_a.queue.empty() and sub_b.queue.empty()


def test_broker_publish_skips_unregistered_subscriber():
    broker = SSEBroker()
    sub = broker.open()
    sub.close()
    assert broker.subscriber_count == 0
    broker.publish("trial_completed", {"trial_id": "t1"})  # must not raise
    assert sub.queue.empty()


def test_broker_late_joiner_does_not_get_past_events():
    # Live-monitoring semantics: only subscribers connected at publish time get it.
    broker = SSEBroker()
    broker.publish("trial_completed", {"trial_id": "early"})
    late = broker.open()
    broker.publish("trial_completed", {"trial_id": "late"})
    (_etype, payload) = late.queue.get_nowait()
    assert payload["trial_id"] == "late"
    assert late.queue.empty()


def test_broker_streams_one_sse_message_per_appended_event_during_a_run(tmp_path):
    """End-to-end: subscribe, drive a real schedule_run with the broker, assert
    one ``trial_completed`` SSE frame per appended event (Req 7.3 + 10.3)."""
    broker = SSEBroker()
    items = [make_item(f"i{i}") for i in range(3)]
    models = [MockAdapter(name="A")]
    plan = build_plan(items, reps=2)
    path = tmp_path / "events.jsonl"

    async def scenario():
        sub = broker.open()  # registered synchronously before the run starts
        await schedule_run(
            plan, models, path, broker,
            items=items, retr=FakeRetrieval(), scoring=StubScoring(),
            resilience_sleep=_instant_sleep,
        )
        # drain everything currently queued
        frames = []
        while not sub.queue.empty():
            frames.append(sub.queue.get_nowait())
        return frames

    frames = asyncio.run(scenario())
    events = read_events(path)
    planned = list(planned_trials(plan, models))
    assert len(events) == len(planned)
    # exactly one published frame per appended event.
    assert len(frames) == len(events)
    assert all(etype == "trial_completed" for etype, _ in frames)
    assert {p["trial_id"] for _, p in frames} == {ev.trial_id for ev in events}


# ===========================================================================
# Fixtures: a TestClient over a temp events log + reports dir
# ===========================================================================
@pytest.fixture
def client(tmp_path):
    events_path = tmp_path / "events.jsonl"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    dist_dir = tmp_path / "dist-absent"  # intentionally does not exist
    app = create_app(
        events_path=events_path,
        reports_dir=reports_dir,
        dist_dir=dist_dir,
        judge_scores_path=tmp_path / "judge_scores.jsonl",
    )
    c = TestClient(app)
    c.app_state = app.state.bakeoff  # convenience handle for tests
    return c


def _seed_events(events_path, *, n_items=6, reps=2, answerability="full"):
    """Run a small offline plan to seed a real event log for aggregate tests."""
    items = [make_item(f"i{i}", answerability=answerability) for i in range(n_items)]
    models = [MockAdapter(name="A"), MockAdapter(name="B")]
    plan = build_plan(items, reps=reps)
    asyncio.run(
        schedule_run(
            plan, models, events_path, SSEBroker(),
            items=items, retr=FakeRetrieval(), scoring=StubScoring(),
            resilience_sleep=_instant_sleep,
        )
    )


def _seed_judge_scores(judge_scores_path, events):
    for event in events:
        if event.model == "A":
            judge = JudgeScores(
                faithfulness=0.9,
                correctness=0.8,
                completeness=0.7,
                judge_sample_count=3,
                judge_model="stub-judge",
                judge_dim_sd={
                    "faithfulness": 0.01,
                    "correctness": 0.01,
                    "completeness": 0.01,
                },
            )
        else:
            judge = JudgeScores(
                faithfulness=0.3,
                correctness=0.2,
                completeness=0.1,
                judge_sample_count=3,
                judge_model="stub-judge",
                judge_dim_sd={
                    "faithfulness": 0.01,
                    "correctness": 0.01,
                    "completeness": 0.01,
                },
            )
        append_judge_score(
            judge_scores_path,
            JudgeScoreRecord(
                trial_id=event.trial_id,
                model=event.model,
                item_id=event.item_id,
                answerability=event.answerability,
                judge=judge,
                judged_at="2025-01-01T00:00:00Z",
                evidence={"faithfulness": "stub evidence"},
                answer_excerpt=event.answer_text[:100],
                momentary_state=event.cohort.momentary_state,
            ),
        )


# ===========================================================================
# /healthz + /api/models
# ===========================================================================
def test_healthz_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "model-bakeoff-harness"
    assert body["run_status"] == RunStatus.IDLE


def test_api_models_idle_snapshot(client):
    r = client.get("/api/models")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == RunStatus.IDLE
    assert body["models"] == {}
    assert body["totals"] == {"done": 0, "errored": 0}


def test_api_models_reflects_active_controller(client):
    # Attach a controller with some counts; /api/models must mirror it.
    ctrl = RunController()
    ctrl.ensure_model("A", planned=4, done=0)
    ctrl.start()
    ctrl.note_completion("A", errored=False)
    client.app_state.controller = ctrl

    body = client.get("/api/models").json()
    assert body["status"] == RunStatus.RUNNING
    assert body["models"]["A"]["planned"] == 4
    assert body["models"]["A"]["done"] == 1


# ===========================================================================
# /api/aggregate — normal-approx CIs (Req 10.4) + P4 blend guard
# ===========================================================================
def test_api_aggregate_returns_normal_approx_cis(client):
    _seed_events(client.app_state.events_path, n_items=6, reps=2, answerability="full")
    r = client.get("/api/aggregate", params={"group_by": "model", "metric": "composite"})
    assert r.status_code == 200
    body = r.json()
    assert body["ci_method"] == "normal_approx"
    assert body["metric"] == "composite"
    assert body["group_by"] == ["model"]
    aggs = body["aggregates"]
    assert len(aggs) == 2  # models A and B
    for a in aggs:
        assert a["group"]["model"] in {"A", "B"}
        # composite has >= min_items_for_ci items -> a populated CI.
        assert a["mean_ci"] is not None
        assert a["mean_ci"]["method"] == "normal_approx"
        assert a["mean_ci"]["low"] <= a["mean_ci"]["point"] <= a["mean_ci"]["high"]


def test_api_aggregate_refuses_accuracy_blend_across_answerability(client):
    # Seed a mix of answerability classes, then group by model only and request an
    # accuracy metric -> the engine must refuse (P4) with 422.
    ev_path = client.app_state.events_path
    _seed_events(ev_path, n_items=4, reps=1, answerability="full")
    # append more events for a different answerability class to the same log.
    items_none = [make_item(f"n{i}", answerability="none") for i in range(4)]
    asyncio.run(
        schedule_run(
            build_plan(items_none, reps=1), [MockAdapter(name="A")],
            ev_path, SSEBroker(), items=items_none,
            retr=FakeRetrieval(), scoring=StubScoring(), resilience_sleep=_instant_sleep,
        )
    )
    r = client.get(
        "/api/aggregate",
        params={"group_by": "model", "metric": "abstention_correct"},
    )
    assert r.status_code == 422


def test_api_aggregate_unknown_dimension_is_400(client):
    _seed_events(client.app_state.events_path, n_items=3, reps=1)
    response = client.get("/api/aggregate", params={"group_by": "not_a_dimension"})
    assert response.status_code == 400


def test_api_bakeoff_diagnostics_surfaces_decision_evidence(client):
    _seed_events(client.app_state.events_path, n_items=6, reps=2, answerability="full")
    response = client.get("/api/bakeoff/diagnostics")
    assert response.status_code == 200

    body = response.json()
    assert body["source"]["success_store_only"] is True
    assert body["source"]["total_trials"] == 6 * 2 * 2
    assert body["source"]["total_items"] == 6
    assert body["source"]["quality_source"] == "outcomes_composite"
    assert body["source"]["quality_trials"] == body["source"]["total_trials"]
    assert body["source"]["judge_scores_total"] == 0
    assert body["source"]["judge_scores_joined"] == 0
    assert body["source"]["models"] == ["A", "B"]

    model_cards = body["model_cards"]
    assert {model_card["model"] for model_card in model_cards} == {"A", "B"}
    for model_card in model_cards:
        assert model_card["n_quality_trials"] == model_card["n_trials"]
        assert model_card["quality"]["mean_ci"] is not None
        assert model_card["quality"]["mean_ci"]["method"] == "normal_approx"
        assert "end_to_end_ms" in model_card["timing"]
        assert model_card["timing"]["end_to_end_ms"]["p50"] is not None
        assert "total" in model_card["token_usage_mean"]
        assert "composite" in model_card["component_means"]
        assert model_card["answerability_counts"] == {"full": 12}

    assert len(body["paired_deltas"]) == 1
    paired_delta = body["paired_deltas"][0]
    assert paired_delta["model_a"] == "A"
    assert paired_delta["model_b"] == "B"
    assert paired_delta["shared_items"] == 6
    assert paired_delta["delta_ci"]["method"] == "paired_normal_approx"

    assert "answerability" in body["cohort_slices"]
    assert body["cohort_slices"]["answerability"]
    assert body["timing_stages"]
    assert "high_variance" in body
    assert "retrieval_regressions" in body


def test_api_bakeoff_diagnostics_joins_phase2_judge_scores(client):
    _seed_events(client.app_state.events_path, n_items=6, reps=2, answerability="full")
    events = read_events(client.app_state.events_path)
    for event in events:
        if event.model == "A":
            judge = JudgeScores(
                faithfulness=0.9,
                correctness=0.8,
                completeness=0.7,
                judge_sample_count=3,
                judge_model="stub-judge",
                judge_dim_sd={"faithfulness": 0.01, "correctness": 0.01, "completeness": 0.01},
            )
        else:
            judge = JudgeScores(
                faithfulness=0.3,
                correctness=0.2,
                completeness=0.1,
                judge_sample_count=3,
                judge_model="stub-judge",
                judge_dim_sd={"faithfulness": 0.01, "correctness": 0.01, "completeness": 0.01},
            )
        append_judge_score(
            client.app_state.judge_scores_path,
            JudgeScoreRecord(
                trial_id=event.trial_id,
                model=event.model,
                item_id=event.item_id,
                answerability=event.answerability,
                judge=judge,
                judged_at="2025-01-01T00:00:00Z",
                evidence={"faithfulness": "stub evidence"},
                answer_excerpt=event.answer_text[:100],
                momentary_state=event.cohort.momentary_state,
            ),
        )

    response = client.get("/api/bakeoff/diagnostics")
    assert response.status_code == 200
    body = response.json()
    assert body["source"]["quality_source"] == "phase2_judge_scores"
    assert body["source"]["judge_scores_total"] == len(events)
    assert body["source"]["judge_scores_joined"] == len(events)
    assert body["source"]["quality_trials"] == len(events)

    cards_by_model = {model_card["model"]: model_card for model_card in body["model_cards"]}
    assert cards_by_model["A"]["n_quality_trials"] == 12
    assert cards_by_model["B"]["n_quality_trials"] == 12
    assert cards_by_model["A"]["component_means"]["faithfulness"] == pytest.approx(0.9)
    assert cards_by_model["B"]["component_means"]["faithfulness"] == pytest.approx(0.3)
    assert cards_by_model["A"]["quality"]["mean_ci"]["point"] == pytest.approx(0.83)
    assert cards_by_model["B"]["quality"]["mean_ci"]["point"] == pytest.approx(0.23)
    assert body["paired_deltas"][0]["winner"] == "A"


# ===========================================================================
# /api/trials/recent — replay from disk so a page reload isn't blank
# ===========================================================================
def test_trials_recent_empty_when_no_log(client):
    body = client.get("/api/trials/recent").json()
    assert body["total"] == 0
    assert body["trials"] == []


def test_trials_recent_replays_summaries_newest_first(client):
    _seed_events(client.app_state.events_path, n_items=4, reps=2, answerability="full")
    body = client.get("/api/trials/recent").json()
    # every appended outcome is replayed, in the SSE summary shape (with ttft_ms).
    assert body["total"] == 4 * 2 * 2  # n_items x reps x 2 models (A,B)
    assert len(body["trials"]) == body["total"]
    first = body["trials"][0]
    for key in ("trial_id", "model", "item_id", "composite", "ttft_ms", "end_to_end_ms", "error"):
        assert key in first
    # ttft_ms is a real number (the headline latency signal now streams from disk).
    assert isinstance(first["ttft_ms"], (int, float))


def test_trials_recent_honors_limit(client):
    _seed_events(client.app_state.events_path, n_items=5, reps=2, answerability="full")
    total = 5 * 2 * 2
    body = client.get("/api/trials/recent", params={"limit": 3}).json()
    assert body["total"] == total          # total reflects the whole log
    assert len(body["trials"]) == 3        # but only `limit` are returned


# ===========================================================================
# Bake-Off sessions — session registry + active-session routing
# ===========================================================================
def test_bakeoff_sessions_api_lists_legacy_session_first(client):
    body = client.get("/api/bakeoff/sessions").json()
    assert body["active_session_id"] == "legacy-root"
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["id"] == "legacy-root"
    assert body["sessions"][0]["kind"] == "legacy"
    assert body["sessions"][0]["archived"] is False


def test_bakeoff_session_create_activate_and_archive_rules(client):
    created_response = client.post(
        "/api/bakeoff/sessions",
        json={"label": "Inline XML short run", "notes": "New approach"},
    )
    assert created_response.status_code == 201
    created_body = created_response.json()
    first_session = created_body["sessions"][0]
    first_session_id = first_session["id"]
    assert created_body["active_session_id"] == first_session_id
    assert first_session["kind"] == "session"
    assert client.app_state.events_path.as_posix() == first_session["outcomes_path"]
    assert client.app_state.run_errors_path.as_posix() == first_session["run_errors_path"]
    assert client.app_state.judge_scores_path.as_posix() == first_session["judge_scores_path"]
    assert client.app_state.reports_dir.as_posix() == first_session["reports_dir"]

    update_response = client.patch(
        f"/api/bakeoff/sessions/{first_session_id}",
        json={"label": "Renamed inline run", "notes": "Updated notes"},
    )
    assert update_response.status_code == 200
    updated_body = update_response.json()
    updated_session = next(
        session for session in updated_body["sessions"] if session["id"] == first_session_id
    )
    assert updated_session["label"] == "Renamed inline run"
    assert updated_session["notes"] == "Updated notes"

    assert client.patch(
        f"/api/bakeoff/sessions/{first_session_id}",
        json={"archived": True},
    ).status_code == 409

    second_response = client.post(
        "/api/bakeoff/sessions",
        json={"label": "Second inline run"},
    )
    assert second_response.status_code == 201
    second_body = second_response.json()
    second_session_id = second_body["active_session_id"]
    assert second_session_id != first_session_id

    archive_response = client.patch(
        f"/api/bakeoff/sessions/{first_session_id}",
        json={"archived": True},
    )
    assert archive_response.status_code == 200
    archive_body = archive_response.json()
    archived_session = next(
        session for session in archive_body["sessions"] if session["id"] == first_session_id
    )
    assert archived_session["archived"] is True

    assert client.post(f"/api/bakeoff/sessions/{first_session_id}/activate").status_code == 409


def test_active_session_routes_use_session_scoped_logs(client):
    client.post(
        "/api/bakeoff/sessions",
        json={"label": "Session-scoped evidence"},
    )
    _seed_events(client.app_state.events_path, n_items=3, reps=1, answerability="full")
    events = read_events(client.app_state.events_path)
    _seed_judge_scores(client.app_state.judge_scores_path, events)

    recent_response = client.get("/api/trials/recent", params={"limit": 2})
    assert recent_response.status_code == 200
    recent_body = recent_response.json()
    assert recent_body["total"] == len(events)
    assert len(recent_body["trials"]) == 2

    judge_scores_response = client.get("/api/judge/scores", params={"refresh": "true"})
    assert judge_scores_response.status_code == 200
    judge_scores_body = judge_scores_response.json()
    assert judge_scores_body["n_records"] == len(events)

    diagnostics_response = client.get("/api/bakeoff/diagnostics")
    assert diagnostics_response.status_code == 200
    diagnostics_body = diagnostics_response.json()
    assert diagnostics_body["source"]["quality_source"] == "phase2_judge_scores"
    assert diagnostics_body["source"]["judge_scores_total"] == len(events)
    assert diagnostics_body["source"]["judge_scores_joined"] == len(events)


def test_run_start_uses_active_session_run_errors_path(client, monkeypatch):
    client.post(
        "/api/bakeoff/sessions",
        json={"label": "Run-path check"},
    )

    captured: dict = {}

    async def fake_start_run(*args, **kwargs):
        captured["errors_path"] = kwargs.get("errors_path")
        controller = RunController()
        controller.start()
        client.app_state.controller = controller
        return controller

    monkeypatch.setattr(client.app_state, "start_run", fake_start_run)

    response = client.post("/api/run/start", json={"methods": ["inline_agent"], "reps": 1})
    assert response.status_code == 202
    assert captured["errors_path"] == client.app_state.run_errors_path


# ===========================================================================
# /api/control/{action} — pause/resume/abort (Req 10.5)
# ===========================================================================
def test_control_without_active_run_is_409(client):
    r = client.post("/api/control/pause")
    assert r.status_code == 409


def test_control_unknown_action_is_404(client):
    client.app_state.controller = RunController()
    r = client.post("/api/control/frobnicate")
    assert r.status_code == 404


def test_control_pause_resume_abort_toggle_status(client):
    ctrl = RunController()
    ctrl.start()
    client.app_state.controller = ctrl

    assert client.post("/api/control/pause").json()["status"] == RunStatus.PAUSED
    assert client.post("/api/control/resume").json()["status"] == RunStatus.RUNNING
    assert client.post("/api/control/abort").json()["status"] == RunStatus.ABORTED


# ===========================================================================
# /exec/aggregate — refuses CI-less numbers (Property 10)
# ===========================================================================
def _write_report(reports_dir, plan_version, report):
    (reports_dir / f"aggregate_{plan_version}.json").write_text(
        json.dumps(report), encoding="utf-8"
    )


def test_exec_aggregate_404_when_absent(client):
    assert client.get("/exec/aggregate", params={"plan_version": "nope"}).status_code == 404


def test_exec_aggregate_serves_clean_report(client):
    report = {
        "plan_version": "v1",
        "by_model": [
            {
                "group": {"model": "A"},
                "metric": "composite",
                "n_items": 50,
                "n_trials": 100,
                "mean_ci": {"point": 0.8, "low": 0.75, "high": 0.85, "method": "cluster_bootstrap"},
                "insufficient_data": False,
            }
        ],
        "frontier": [
            {
                "model": "A",
                "quality": {"point": 0.8, "low": 0.75, "high": 0.85, "method": "cluster_bootstrap"},
                "speed_p50_ms": 300.0,
                "speed_p90_ms": 600.0,
                "on_pareto_front": True,
            }
        ],
    }
    _write_report(client.app_state.reports_dir, "v1", report)
    r = client.get("/exec/aggregate", params={"plan_version": "v1"})
    assert r.status_code == 200
    assert r.json()["plan_version"] == "v1"


def test_exec_aggregate_refuses_ci_less_number(client):
    # An aggregate with mean_ci == null that is NOT marked insufficient_data is a
    # bare number reaching the exec layer -> Property 10 violation -> 422.
    report = {
        "plan_version": "bad",
        "by_model": [
            {
                "group": {"model": "A"},
                "metric": "composite",
                "n_items": 50,
                "n_trials": 100,
                "mean_ci": None,
                "insufficient_data": False,
            }
        ],
    }
    _write_report(client.app_state.reports_dir, "bad", report)
    r = client.get("/exec/aggregate", params={"plan_version": "bad"})
    assert r.status_code == 422


def test_exec_aggregate_refuses_frontier_point_without_ci(client):
    # A frontier point lacking a quality CI is also a P10 violation -> 422.
    report = {
        "plan_version": "badfp",
        "by_model": [],
        "frontier": [
            {"model": "A", "quality": None, "speed_p50_ms": 300.0,
             "speed_p90_ms": 600.0, "on_pareto_front": True}
        ],
    }
    _write_report(client.app_state.reports_dir, "badfp", report)
    assert client.get("/exec/aggregate", params={"plan_version": "badfp"}).status_code == 422


def test_exec_reports_lists_versions(client):
    _write_report(client.app_state.reports_dir, "v1", {"aggregates": []})
    _write_report(client.app_state.reports_dir, "v2", {"aggregates": []})
    body = client.get("/exec/reports").json()
    assert set(body["reports"]) == {"v1", "v2"}


# ===========================================================================
# Static serving — graceful stub when the bundle is absent
# ===========================================================================
def test_root_returns_stub_when_dist_absent(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["ui"] == "not-built"
    assert "/api/models" in body["api"]


def test_root_serves_index_when_dist_present(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>GBBO</title>", encoding="utf-8")
    app = create_app(
        events_path=tmp_path / "events.jsonl",
        reports_dir=tmp_path / "reports",
        dist_dir=dist,
    )
    c = TestClient(app)
    r = c.get("/")
    assert r.status_code == 200
    assert "GBBO" in r.text


# ===========================================================================
# Loopback / no-auth posture (Req 15.1 / 15.2)
# ===========================================================================
def test_app_records_loopback_bind_target(client):
    assert is_loopback_host(client.app_state.host)


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True),
    ("localhost", True),
    ("::1", True),
    ("0.0.0.0", False),
    ("10.0.0.5", False),
    ("example.com", False),
])
def test_is_loopback_host(host, expected):
    assert is_loopback_host(host) is expected


def test_serve_refuses_non_loopback_without_override():
    with pytest.raises(RuntimeError, match="non-loopback"):
        serve(host="0.0.0.0")


# ===========================================================================
# SSE wire format + route headers (no hang: pull a single frame and stop)
# ===========================================================================
def test_subscription_stream_emits_connected_then_events():
    """The per-subscriber generator flushes ': connected' then forwards events.

    Driven directly (not through TestClient) so we can pull exactly two frames and
    stop — the live ``/api/stream`` generator is intentionally infinite.
    """
    broker = SSEBroker()

    async def scenario():
        sub = broker.open()
        gen = sub.stream(heartbeat=0.01)
        first = await gen.__anext__()           # immediate ": connected" flush
        broker.publish("trial_completed", {"trial_id": "t1"})
        second = await gen.__anext__()          # the forwarded event frame
        await gen.aclose()                      # triggers finally -> unregister
        return first, second

    first, second = asyncio.run(scenario())
    assert first == ": connected\n\n"
    assert second.startswith("event: trial_completed")
    assert "id: t1" in second
    assert '"trial_id":"t1"' in second
    assert second.endswith("\n\n")


def test_subscription_stream_emits_keepalive_when_idle():
    broker = SSEBroker()

    async def scenario():
        sub = broker.open()
        gen = sub.stream(heartbeat=0.01)
        await gen.__anext__()                    # ": connected"
        frame = await gen.__anext__()            # idle -> keepalive comment
        await gen.aclose()
        return frame

    assert asyncio.run(scenario()) == ": keepalive\n\n"


# ===========================================================================
# POST /api/run/start — kick off a flat fixed-rep run from the browser
# ===========================================================================
_SNAPSHOT_KEYS = {"status", "auto_paused", "auth_refreshes", "totals", "models"}


class _GatedRetrieval(FakeRetrieval):
    """A :class:`FakeRetrieval` that blocks in ``retrieve`` until a gate is set.

    Lets a test hold a launched run in the ``running`` state deterministically
    (so a second start can be observed returning 409) and then release it so the
    background run drains to completion. ``is_set`` on a :class:`threading.Event`
    is thread-safe, and the poll loop awaits so the event loop keeps serving the
    control/poll requests meanwhile.
    """

    def __init__(self, gate: threading.Event) -> None:
        super().__init__()
        self._gate = gate

    async def retrieve(self, query, filters=None):
        while not self._gate.is_set():
            await asyncio.sleep(0.005)
        return await super().retrieve(query, filters)


def _patch_offline_backend(monkeypatch, items, models, *, retr):
    """Patch the route's lazily-imported backend so a start is fully offline.

    Mirrors the task-15 offline injection: a fixed item list (no real dataset),
    MockAdapter candidates (no Bedrock), a fixed-RetrievalResult client (no
    network), and the stub/offline scoring pipeline (no Bedrock judge/embed).
    The route imports these names *inside* the handler from their source modules,
    so the patches target those source modules.
    """
    from bakeoff.scoring.pipeline import ScoringPipeline as _RealScoringPipeline

    offline_scoring = _RealScoringPipeline.offline()

    class _FakeLoader:
        def __init__(self, *a, **k) -> None:
            pass

        def load_items(self):
            return list(items)

    monkeypatch.setattr("bakeoff.dataset.DatasetLoader", _FakeLoader)
    monkeypatch.setattr(
        "bakeoff.adapters.bedrock.build_candidate_adapters",
        lambda *a, **k: list(models),
    )
    monkeypatch.setattr(
        "bakeoff.retrieval_client.RetrievalClient", lambda *a, **k: retr
    )
    # The run-start handler constructs the pipeline via the generation_phase()
    # classmethod (Phase 1: local scorers only, no Bedrock judge/embed). The
    # offline double must therefore expose generation_phase() — and offline() —
    # both returning the deterministic offline pipeline, so the test stays
    # network-free while matching the handler's real call.
    class _OfflineScoringPipeline:
        @staticmethod
        def generation_phase(*a, **k):
            return offline_scoring

        @staticmethod
        def offline(*a, **k):
            return offline_scoring

    monkeypatch.setattr(
        "bakeoff.scoring.pipeline.ScoringPipeline", _OfflineScoringPipeline
    )


def _wait_for(client, predicate, *, timeout=5.0):
    """Poll ``GET /api/models`` until ``predicate(snapshot)`` or time out."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get("/api/models").json()
        if predicate(last):
            return last
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting on run snapshot; last={last!r}")


def _offline_items():
    return [make_item(f"i{i}", answerability=("full" if i % 2 else "none")) for i in range(6)]


def test_run_start_202_then_409_and_appends_events(tmp_path, monkeypatch):
    """202 + snapshot on start, 409 on an immediate second start, events after drain."""
    events_path = tmp_path / "events.jsonl"
    items = _offline_items()
    models = [MockAdapter(name="A"), MockAdapter(name="B")]
    gate = threading.Event()  # held closed -> the run stays running
    _patch_offline_backend(monkeypatch, items, models, retr=_GatedRetrieval(gate))

    app = create_app(events_path=events_path, reports_dir=tmp_path / "reports",
                     dist_dir=tmp_path / "dist-absent")
    # Context-manager form so the lifespan/event loop runs the background task.
    with TestClient(app) as client:
        r1 = client.post("/api/run/start", json={"reps": 3})
        assert r1.status_code == 202
        snap = r1.json()
        assert _SNAPSHOT_KEYS <= set(snap)
        assert snap["status"] in (RunStatus.IDLE, RunStatus.RUNNING)

        # The run is gated open -> it reaches RUNNING and a controller is present.
        _wait_for(client, lambda s: s["status"] == RunStatus.RUNNING)
        assert client.app.state.bakeoff.controller is not None

        # A second immediate start must be refused with 409 (run already active).
        r2 = client.post("/api/run/start", json={"reps": 3})
        assert r2.status_code == 409
        assert r2.json() == {"detail": "a run is already active"}

        # Release the gate and let the background run drain to completion.
        gate.set()
        _wait_for(client, lambda s: s["status"] == RunStatus.COMPLETED)

    # Trial events were appended to the tmp events_path: every item x reps x model.
    events = read_events(events_path)
    assert len(events) == len(items) * 3 * len(models)
    assert {ev.model for ev in events} == {"A", "B"}
    assert {ev.item_id for ev in events} == {it.item_id for it in items}


def test_run_start_409_when_controller_paused(tmp_path, monkeypatch):
    """A paused (not just running) controller also blocks a new start with 409."""
    events_path = tmp_path / "events.jsonl"
    _patch_offline_backend(
        monkeypatch, _offline_items(), [MockAdapter(name="A")], retr=FakeRetrieval()
    )
    app = create_app(events_path=events_path, reports_dir=tmp_path / "reports",
                     dist_dir=tmp_path / "dist-absent")
    with TestClient(app) as client:
        ctrl = RunController()
        ctrl.start()
        ctrl.pause()
        client.app.state.bakeoff.controller = ctrl
        r = client.post("/api/run/start", json={})
        assert r.status_code == 409
        assert r.json() == {"detail": "a run is already active"}


def test_run_start_defaults_and_max_trials_clamp(tmp_path, monkeypatch):
    """An empty body uses reps=3 defaults; a max_trials clamp is accepted (202)."""
    events_path = tmp_path / "events.jsonl"
    items = _offline_items()
    models = [MockAdapter(name="A")]
    _patch_offline_backend(monkeypatch, items, models, retr=FakeRetrieval())

    app = create_app(events_path=events_path, reports_dir=tmp_path / "reports",
                     dist_dir=tmp_path / "dist-absent")
    with TestClient(app) as client:
        # All-optional body: a small max_trials clamp must not raise (frozen
        # SamplingPlan whose budget dict is mutated in place).
        r = client.post("/api/run/start", json={"max_trials": 5})
        assert r.status_code == 202
        assert _SNAPSHOT_KEYS <= set(r.json())
        # Drain so the background task finishes inside the lifespan.
        _wait_for(client, lambda s: s["status"] == RunStatus.COMPLETED)

    # reps defaulted to 3 -> every item x 3 reps x 1 model recorded.
    events = read_events(events_path)
    assert len(events) == len(items) * 3 * len(models)


# ===========================================================================
# Phase-2 judge endpoints + auto-chain (offline)
# ===========================================================================
def test_judge_status_idle_by_default(client):
    body = client.get("/api/judge/status").json()
    assert body["status"] == "idle"
    assert body["progress"] == {"judged": 0, "sampled": 0, "skipped_existing": 0}


def test_judge_scores_empty_is_well_formed(client, monkeypatch):
    # No judge store on disk yet -> an empty-but-well-formed summary.
    monkeypatch.setattr(config, "JUDGE_SCORES_PATH", client.app_state.events_path.parent / "judge.jsonl")
    body = client.get("/api/judge/scores").json()
    assert body["n_records"] == 0
    assert body["models"] == []
    assert body["dimensions"]


def test_judge_start_runs_and_then_409_while_running(tmp_path, monkeypatch):
    """POST /api/judge/start launches Phase 2; a second call while running is 409."""
    import threading

    from bakeoff.judge_phase2 import JudgeScoreRecord

    events_path = tmp_path / "outcomes.jsonl"
    judge_path = tmp_path / "judge_scores.jsonl"
    monkeypatch.setattr(config, "JUDGE_SCORES_PATH", judge_path)
    monkeypatch.setattr(config, "DATASET_DIR", tmp_path / "data")

    # Seed a few real outcome events so there is something to sample + judge.
    from bakeoff.tests.test_aggregate import build_event as _be

    items = [make_item(f"i{i}", answerability="full") for i in range(3)]
    for it in items:
        append_event(events_path, _be(composite=0.7, item_id=it.item_id, model="A"))

    # Gate the judge so the run stays "running" long enough to observe the 409.
    gate = threading.Event()

    async def _slow_run_deferred_judge(**kwargs):
        from bakeoff.judge_phase2 import Phase2Result

        while not gate.is_set():
            await asyncio.sleep(0.005)
        return Phase2Result(
            outcomes_seen=3, sampled=3, judged=3, skipped_existing=0,
            judge_scores_path=judge_path, models={"A": 3},
        )

    monkeypatch.setattr("bakeoff.judge_phase2.run_deferred_judge", _slow_run_deferred_judge)
    monkeypatch.setattr(
        "bakeoff.judge_phase2.summarize_judge_scores", lambda recs: {"models": [], "n_records": 0, "dimensions": []}
    )

    app = create_app(events_path=events_path, reports_dir=tmp_path / "reports",
                     dist_dir=tmp_path / "dist-absent")
    with TestClient(app) as c:
        r1 = c.post("/api/judge/start", json={})
        assert r1.status_code == 202
        assert r1.json()["status"] == "running"
        _wait_for_judge(c, lambda s: s["status"] == "running")
        # second start while running -> 409
        r2 = c.post("/api/judge/start", json={})
        assert r2.status_code == 409
        assert r2.json() == {"detail": "judging already in progress"}
        gate.set()
        _wait_for_judge(c, lambda s: s["status"] == "completed")


def _wait_for_judge(client, predicate, *, timeout=5.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get("/api/judge/status").json()
        if predicate(last):
            return last
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting on judge status; last={last!r}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


# ===========================================================================
# Optimizer v2: /status returns per-island + per-tournament-round from store
# ===========================================================================
def _seed_optimizer_store(store_path, *, model="A"):
    """Seed an optimizer iterations JSONL with island + tournament records."""
    from bakeoff.quality.optimizer.store import IterationRecord, OptimizerStore

    store = OptimizerStore(iterations_path=store_path, audit_path=store_path.parent / "audit.jsonl",
                           errors_path=store_path.parent / "err.jsonl", results_path=store_path.parent / "res.json")

    base = {
        "model": model, "phase": "A", "backend": "offline",
        "author_model": "sonnet", "judge_model": "opus",
        "significance_threshold": 0.05, "promoted": False,
        "slice_n_conversations": 20, "between_conversation_sd": 0.23,
        "consecutive_non_improving": 0, "converged": False,
        "stop_reason": None, "mean_closeness": 0.8,
        "abstention_reward_mean": 0.0, "answered_when_unsure_rate": 0.0,
        "retrieval_backend": "local", "created_at": "2026-06-03T00:00:00Z",
    }

    # Island 0 — two iterations at rung 0
    for idx in range(2):
        store.append_iteration(IterationRecord(
            iteration_id=f"it-i0-{idx}", iteration_index=idx,
            champion_score=0.7 + idx * 0.02, champion_ci_half_width=0.1,
            challenger_score=0.68, challenger_ci_half_width=0.1,
            gain_absolute=0.02, gain_percent=3.0,
            island_id=0, rung_index=0, tournament_round=None, **base,
        ))

    # Island 1 — one iteration at rung 0
    store.append_iteration(IterationRecord(
        iteration_id="it-i1-0", iteration_index=0,
        champion_score=0.75, champion_ci_half_width=0.09,
        challenger_score=0.72, challenger_ci_half_width=0.09,
        gain_absolute=0.03, gain_percent=4.0,
        island_id=1, rung_index=0, tournament_round=None, **base,
    ))

    # Tournament round 1 — both islands compete, island 1 wins
    store.append_iteration(IterationRecord(
        iteration_id="it-t1-i0", iteration_index=3,
        champion_score=0.72, champion_ci_half_width=0.07,
        challenger_score=None, challenger_ci_half_width=None,
        gain_absolute=None, gain_percent=None,
        promoted=False, island_id=0, rung_index=1, tournament_round=1, **{
            k: v for k, v in base.items() if k != "promoted"
        },
    ))
    store.append_iteration(IterationRecord(
        iteration_id="it-t1-i1", iteration_index=4,
        champion_score=0.75, champion_ci_half_width=0.07,
        challenger_score=None, challenger_ci_half_width=None,
        gain_absolute=None, gain_percent=None,
        promoted=True, island_id=1, rung_index=1, tournament_round=1, **{
            k: v for k, v in base.items() if k != "promoted"
        },
    ))

    return store


def test_optimizer_status_v2_per_island_and_tournament(tmp_path, monkeypatch):
    """GET /status returns per-island progress + per-tournament-round summaries
    reconstructed durably from the store (no live stream needed)."""
    iter_path = tmp_path / "iterations.jsonl"
    _seed_optimizer_store(iter_path, model="A")

    monkeypatch.setattr(config, "QUALITY_MODELS", {"A": {}})

    # Patch OptimizerStore so its default paths point at our tmp store
    from bakeoff.quality.optimizer.store import OptimizerStore as _RealStore

    class _PatchedStore(_RealStore):
        def __init__(self, **kwargs):
            kwargs.setdefault("iterations_path", iter_path)
            kwargs.setdefault("audit_path", tmp_path / "audit.jsonl")
            kwargs.setdefault("errors_path", tmp_path / "err.jsonl")
            kwargs.setdefault("results_path", tmp_path / "res.json")
            super().__init__(**kwargs)

    monkeypatch.setattr("bakeoff.quality.optimizer.store.OptimizerStore", _PatchedStore)

    app = create_app(events_path=tmp_path / "ev.jsonl", reports_dir=tmp_path / "rpt",
                     dist_dir=tmp_path / "dist-absent")
    c = TestClient(app)

    r = c.get("/api/quality/optimize/status")
    assert r.status_code == 200
    body = r.json()
    model_data = body["models"]["A"]

    # Per-island shape
    islands = model_data["islands"]
    assert len(islands) == 2
    i0 = next(i for i in islands if i["island_id"] == 0)
    i1 = next(i for i in islands if i["island_id"] == 1)
    # Island 0's latest record is the tournament entry (rung_index=1, score=0.72)
    assert i0["rung_index"] == 1
    assert i0["champion_score"] == pytest.approx(0.72)
    assert i0["state"] == "iterating"
    # Island 1's latest is also the tournament entry (rung_index=1, score=0.75)
    assert i1["champion_score"] == pytest.approx(0.75)

    # Per-tournament-round shape
    rounds = model_data["tournament_rounds"]
    assert len(rounds) == 1
    rnd = rounds[0]
    assert rnd["round"] == 1
    assert rnd["shared_rung"] == 1
    assert rnd["winner"] == 1
    assert rnd["migration"] is True
    assert len(rnd["scores"]) == 2


def test_optimizer_start_enforces_loopback(tmp_path, monkeypatch):
    """POST /start still refuses when not on loopback (inherited from serve())."""
    # The route itself doesn't check loopback — that's enforced by serve(). But
    # confirm the app's recorded host is loopback (the test fixture uses 127.0.0.1).
    app = create_app(events_path=tmp_path / "ev.jsonl", reports_dir=tmp_path / "rpt",
                     dist_dir=tmp_path / "dist-absent", host="127.0.0.1")
    assert is_loopback_host(app.state.bakeoff.host)


def test_optimizer_start_enforces_quota_guard(tmp_path, monkeypatch):
    """POST /start still refuses when a bake-off run looks active (quota guard)."""
    monkeypatch.setattr(config, "QUALITY_MODELS", {"A": {}})

    app = create_app(events_path=tmp_path / "ev.jsonl", reports_dir=tmp_path / "rpt",
                     dist_dir=tmp_path / "dist-absent")
    c = TestClient(app)

    # Make _bakeoff_run_looks_active return True
    monkeypatch.setattr("bakeoff.quality.main._bakeoff_run_looks_active", lambda: True)

    r = c.post("/api/quality/optimize/start", json={"backend": "live", "models": ["A"]})
    assert r.status_code == 409
    assert "bake-off run looks active" in r.json()["detail"]


# ===========================================================================
# Optimizer v2 routes: offline /v2/start -> 202 + running, second -> 409,
# /v2/status well-formed pre/post, /v2/stream opens (network-free, Req 5.1/5.3)
# ===========================================================================
def _wait_for_v2(client, predicate, *, timeout=5.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get("/api/quality/optimize/v2/status").json()
        if predicate(last):
            return last
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting on v2 status; last={last!r}")


_V2_SNAPSHOT_KEYS = {"status", "request", "error", "started_at", "finished_at", "models"}


def test_optimize_v2_start_202_then_409_offline(tmp_path, monkeypatch):
    """Offline backend: /v2/start -> 202 + status running; second start -> 409;
    /v2/status well-formed before and after; /v2/stream opens."""
    monkeypatch.setattr(config, "QUALITY_MODELS", {"A": {}})

    # Keep the run "running" deterministically: gate run_v2 until released so the
    # second start observes 409. run_v2 is awaited inside the background task.
    gate = threading.Event()

    async def _gated_run_v2(self, models, backend, *, emitter, store, **opts):
        while not gate.is_set():
            await asyncio.sleep(0.005)
        return {}

    monkeypatch.setattr(
        "bakeoff.quality.optimizer.orchestrator.PerModelOrchestrator.run_v2",
        _gated_run_v2,
    )
    # Dataset load must stay offline/cheap.
    monkeypatch.setattr(
        "bakeoff.quality.dataset.load_multi_turn_items", lambda *a, **k: []
    )

    app = create_app(events_path=tmp_path / "ev.jsonl", reports_dir=tmp_path / "rpt",
                     dist_dir=tmp_path / "dist-absent")
    with TestClient(app) as c:
        # well-formed + idle before any run
        pre = c.get("/api/quality/optimize/v2/status").json()
        assert _V2_SNAPSHOT_KEYS <= set(pre)
        assert pre["status"] == "idle"
        assert pre["models"]["A"] == {"islands": [], "tournament_rounds": [], "viewable": False}

        r1 = c.post("/api/quality/optimize/v2/start", json={"backend": "offline", "models": ["A"]})
        assert r1.status_code == 202
        assert _V2_SNAPSHOT_KEYS <= set(r1.json())

        _wait_for_v2(c, lambda s: s["status"] == "running")

        # second start while running -> 409, starts nothing new
        r2 = c.post("/api/quality/optimize/v2/start", json={"backend": "offline", "models": ["A"]})
        assert r2.status_code == 409
        assert r2.json() == {"detail": "optimizer v2 already running"}

        # /v2/stream is wired to the DEDICATED v2 broker. We verify the route is
        # registered and that the v2 broker's subscribe() emits the initial
        # ": connected" frame, WITHOUT opening an infinite HTTP stream under
        # TestClient. TestClient does not deliver the client disconnect to the
        # server-side generator on context-manager exit, so closing an unending
        # SSE response deadlocks (the generator keeps emitting heartbeats forever
        # and the response never closes). Exercising subscribe() directly hits the
        # exact code path the route uses and closes the generator cleanly via
        # aclose(), which runs its `finally: self.close()` teardown.
        assert any(
            getattr(r, "path", None) == "/api/quality/optimize/v2/stream"
            for r in app.routes
        )

        async def _first_v2_frame():
            gen = app.state.bakeoff.optimizer_v2_broker.subscribe()
            try:
                return await gen.__anext__()
            finally:
                await gen.aclose()

        first_frame = asyncio.run(_first_v2_frame())
        assert first_frame.startswith(": connected")

        gate.set()
        _wait_for_v2(c, lambda s: s["status"] == "completed")

    # status stays well-formed after completion
    post = c.get("/api/quality/optimize/v2/status").json()
    assert _V2_SNAPSHOT_KEYS <= set(post)


def test_optimize_v2_unknown_model_is_422(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QUALITY_MODELS", {"A": {}})
    app = create_app(events_path=tmp_path / "ev.jsonl", reports_dir=tmp_path / "rpt",
                     dist_dir=tmp_path / "dist-absent")
    c = TestClient(app)
    r = c.post("/api/quality/optimize/v2/start", json={"models": ["nope"]})
    assert r.status_code == 422
