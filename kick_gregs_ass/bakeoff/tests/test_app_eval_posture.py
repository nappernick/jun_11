"""
Cross-cutting invariant suite — eval-route security posture (Task 14.1).

**Property 13 (Python half): the new eval routes preserve the loopback-only,
no-auth posture (Req 21.2).** The eval dashboard is an additive surface on the
same harness app; it must inherit — and not widen — the existing posture:

* served by the SAME app as the rest of the harness (no separate, differently
  configured server);
* require NO authentication — every eval route answers WITHOUT any
  ``Authorization`` (or other credential) header, and answers identically WITH a
  bogus one (the header is simply ignored, never honored or rejected);
* carry NO route-level auth dependency / security scheme (the OpenAPI schema
  exposes no ``security`` requirement and no ``securitySchemes``);
* keep the loopback-only bind precondition (:func:`bakeoff.app.serve` refuses a
  non-loopback bind unless auth is explicitly asserted added).

Every assertion is OFFLINE and network-free: the app is exercised with
:class:`fastapi.testclient.TestClient` (httpx-backed, no real server, no
uvicorn, no AWS, no Bedrock). No eval RUN is launched here — the posture is a
property of the wiring, not of any run — so the test needs no producer.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bakeoff.app import create_app, is_loopback_host, serve


# The additive eval routes whose posture this suite pins. Read-only GETs are
# safe to exercise directly; the start POST is checked for "no auth required" by
# confirming it is NOT rejected with an auth challenge (401/403) — it reaches the
# handler and is validated on its own terms instead.
_EVAL_GET_ROUTES = (
    "/api/eval/status",
    "/api/eval/instances/recent",
    "/api/eval/prompts",
)
_EVAL_ROUTE_PREFIX = "/api/eval"


@pytest.fixture
def client(tmp_path):
    """A TestClient over temp stores — the SAME app the harness serves."""
    app = create_app(
        events_path=tmp_path / "events.jsonl",
        reports_dir=tmp_path / "reports",
        dist_dir=tmp_path / "dist-absent",  # intentionally absent
        eval_events_path=tmp_path / "eval_instances.jsonl",
    )
    return TestClient(app)


# ===========================================================================
# No auth required — eval GET routes answer without any credential header
# ===========================================================================
@pytest.mark.parametrize("route", _EVAL_GET_ROUTES)
def test_eval_get_route_requires_no_auth_header(client, route):
    """Each eval GET route answers WITHOUT any ``Authorization`` header.

    A no-auth surface returns a normal application response (here 200) to an
    unauthenticated request — it never issues an auth challenge (401/403).
    """
    r = client.get(route)  # no headers whatsoever
    assert r.status_code == 200
    assert r.status_code not in (401, 403)


@pytest.mark.parametrize("route", _EVAL_GET_ROUTES)
def test_eval_get_route_ignores_a_bogus_auth_header(client, route):
    """A supplied credential header is neither required nor honored — it is simply
    ignored, so the response is identical with and without it."""
    without = client.get(route)
    with_bogus = client.get(
        route,
        headers={"Authorization": "Bearer totally-bogus-token", "X-Api-Key": "nope"},
    )
    assert without.status_code == with_bogus.status_code == 200
    # The header changes nothing about the response body (it is never consulted).
    assert without.json() == with_bogus.json()


def test_eval_start_route_is_not_gated_by_auth(client):
    """``POST /api/eval/runs/start`` is not behind an auth gate.

    Without any credential header the request must reach the handler and be
    validated on its OWN terms (a deliberately invalid agent set yields a 422
    domain error), NOT bounced with an auth challenge (401/403). 422 here proves
    the request was accepted for processing with no authentication.
    """
    r = client.post(
        "/api/eval/runs/start",
        json={"agents": ["totally-unknown"]},  # invalid on purpose
    )
    assert r.status_code == 422  # domain validation, reached the handler
    assert r.status_code not in (401, 403)


# ===========================================================================
# No route-level auth dependency / security scheme on the eval surface
# ===========================================================================
def test_eval_routes_declare_no_security_scheme(client):
    """The OpenAPI schema exposes NO security requirement on any eval route and no
    security scheme at all — i.e. there is no auth dependency wired in."""
    schema = client.app.openapi()

    # No global/component security schemes are defined anywhere on the app.
    components = schema.get("components", {})
    assert "securitySchemes" not in components or components["securitySchemes"] == {}

    # No eval path operation carries a per-operation `security` requirement.
    for path, item in schema.get("paths", {}).items():
        if not path.startswith(_EVAL_ROUTE_PREFIX):
            continue
        for method, operation in item.items():
            if method.lower() not in {"get", "post", "put", "delete", "patch"}:
                continue
            assert "security" not in operation, (
                f"{method.upper()} {path} unexpectedly declares a security "
                f"requirement: {operation.get('security')!r}"
            )


def test_eval_routes_have_no_dependant_security_requirements(client):
    """At the routing layer, no eval route registers any auth dependency.

    FastAPI records security dependencies on each route's `dependant`; an eval
    route inheriting the no-auth posture must have none.
    """
    eval_routes = [
        r
        for r in client.app.routes
        if getattr(r, "path", "").startswith(_EVAL_ROUTE_PREFIX)
    ]
    assert eval_routes, "expected the eval routes to be registered on the app"
    for route in eval_routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        assert not getattr(dependant, "security_requirements", []), (
            f"{route.path} unexpectedly carries security requirements"
        )


# ===========================================================================
# Served by the same app; loopback-only bind precondition intact (Req 21.2)
# ===========================================================================
def test_eval_routes_are_served_by_the_same_harness_app(client):
    """The eval routes live on the SAME app instance as the rest of the harness —
    not a separate, differently configured (and possibly auth-bearing) server."""
    paths = {getattr(r, "path", "") for r in client.app.routes}
    # A representative harness route AND the eval routes coexist on one app.
    assert "/api/status" in paths or "/api/runs/start" in paths or "/api/stream" in paths
    for route in _EVAL_GET_ROUTES + ("/api/eval/runs/start", "/api/eval/stream"):
        assert route in paths, f"{route} is not registered on the harness app"


def test_app_default_bind_is_loopback(client):
    """The app records a loopback bind target by default (the no-auth precondition)."""
    assert is_loopback_host(client.app.state.host)


def test_serve_refuses_non_loopback_bind_without_explicit_auth_override():
    """:func:`serve` enforces the loopback precondition: a non-loopback host is
    refused unless the caller explicitly asserts auth was added first (Req 21.2 /
    15.2). This is verified WITHOUT starting a server — the guard raises before
    any bind."""
    with pytest.raises(RuntimeError, match="non-loopback"):
        serve(host="0.0.0.0", allow_non_loopback=False)
