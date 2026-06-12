"""
Optimizer V3 route tests — the /api/quality/optimize/v3/* surface.

Validates the lifecycle contract the V3 tab depends on: an empty-but-well-formed
status before any run, the live-only start policy (offline -> 422), model
validation, idempotent reset, and the resume-without-history 409.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from bakeoff import config
from bakeoff.app import create_app


@pytest.fixture(autouse=True)
def _isolated_v3_stores(tmp_path, monkeypatch):
    """Point EVERY v3 store path at a tmp dir before the app is built.

    Without this, the reset-idempotency test below would truncate the REAL
    ``data/bakeoff/quality_opt_v3_*`` files — which is exactly how a live run's
    data was destroyed on 2026-06-10. Tests must never see real store paths.
    """
    monkeypatch.setattr(config, "QUALITY_OPT_V3_ITERATIONS_PATH", tmp_path / "iters.jsonl")
    monkeypatch.setattr(config, "QUALITY_OPT_V3_AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(config, "QUALITY_OPT_V3_ERRORS_PATH", tmp_path / "errors.jsonl")
    monkeypatch.setattr(config, "QUALITY_OPT_V3_RESULTS_PATH", tmp_path / "results.json")
    monkeypatch.setattr(config, "QUALITY_OPT_V3_STATE_PATH", tmp_path / "state.json")


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


def test_v3_status_is_empty_but_well_formed_before_any_run(client):
    response = client.get("/api/quality/optimize/v3/status")
    assert response.status_code == 200
    snapshot = response.json()
    assert snapshot["status"] == "idle"
    assert snapshot["error"] is None
    assert isinstance(snapshot["models"], dict) and snapshot["models"]
    for model_block in snapshot["models"].values():
        assert "islands" in model_block
        assert "tournament_rounds" in model_block


def test_v3_start_rejects_offline_backend(client):
    """V3 is live-only by design — an offline request is a 422, never a silent fallback."""
    response = client.post("/api/quality/optimize/v3/start", json={"backend": "offline"})
    assert response.status_code == 422
    assert "live-only" in response.json()["detail"]


def test_v3_start_rejects_unknown_models(client):
    response = client.post(
        "/api/quality/optimize/v3/start", json={"models": ["not-a-model"]}
    )
    assert response.status_code == 422


def test_v3_reset_is_idempotent(client):
    response = client.post("/api/quality/optimize/v3/reset")
    assert response.status_code == 200
    assert response.json()["status"] == "idle"
    # And again — nothing running, still a clean 200.
    response = client.post("/api/quality/optimize/v3/reset")
    assert response.status_code == 200


def test_v3_resume_without_prior_request_is_409(client):
    response = client.post("/api/quality/optimize/v3/resume")
    assert response.status_code == 409
