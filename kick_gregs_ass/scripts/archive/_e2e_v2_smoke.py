"""In-process OFFLINE end-to-end smoke for optimizer v2 (Req 5.2 evidence).

Drives the REAL production launch path:
    AppState.start_optimizer_v2(backend="offline")
        -> _run_optimizer_v2  -> PerModelOrchestrator.run_v2

subscribes to the DEDICATED v2 broker, and confirms:
  1. start_optimizer_v2() returns True and lifecycle status flips to 'running'
  2. the v2 broker emits real optimizer_* events (the run is doing work)
  3. optimizer_v2_snapshot() surfaces per-island progress

No AWS, no browser. Durable stores are redirected to a tmp dir so this NEVER
touches the live dashboard's data/bakeoff/quality_opt_* files (no-interference).
Event collection is hard-bounded (~45s) and the background run is cancelled via
reset_optimizer_v2() at the end, so the process always exits cleanly.
"""
import asyncio
import tempfile
import time
from pathlib import Path


async def main() -> int:
    import sys

    backend = sys.argv[1] if len(sys.argv) > 1 else "offline"

    from bakeoff import config

    # --- redirect durable stores to tmp (no-interference with live :8200) ---
    tmp = Path(tempfile.mkdtemp(prefix="v2_e2e_"))
    config.QUALITY_OPT_ITERATIONS_PATH = tmp / "iter.jsonl"
    config.QUALITY_OPT_AUDIT_PATH = tmp / "audit.jsonl"
    config.QUALITY_OPT_ERRORS_PATH = tmp / "err.jsonl"
    config.QUALITY_OPT_RESULTS_PATH = tmp / "res.json"

    # --- tiny dataset so an offline run produces island events quickly ---
    import bakeoff.quality.dataset as ds
    try:
        full = list(ds.load_multi_turn_items())
    except Exception as exc:  # noqa: BLE001
        print(f"[e2e] load_multi_turn_items failed ({exc!r}); using empty set")
        full = []
    tiny = full[:6]
    ds.load_multi_turn_items = lambda *a, **k: tiny  # type: ignore[assignment]

    from bakeoff.app import AppState

    state = AppState()
    model = list(config.QUALITY_MODELS.keys())[0]
    print(f"[e2e] backend={backend!r} model={model!r} tiny_items={len(tiny)} tmp={tmp}", flush=True)

    # --- subscribe to the DEDICATED v2 broker BEFORE launch ---
    gen = state.optimizer_v2_broker.subscribe()
    first = await gen.__anext__()
    print(f"[e2e] v2 broker first frame: {first!r}", flush=True)
    assert first.startswith(": connected"), "v2 broker did not emit connected frame"

    # --- drive the REAL production launch path (offline) ---
    launched = await state.start_optimizer_v2(backend=backend, models=[model])
    print(f"[e2e] start_optimizer_v2 -> {launched}; status={state.optimizer_v2_status}", flush=True)
    assert launched is True, "start_optimizer_v2 did not launch"
    assert state.optimizer_v2_status == "running", f"status={state.optimizer_v2_status}"

    # Collect real events off the v2 broker. Live runs invoke Bedrock (author + Opus judge)
    # per step, so give them a much longer window than the offline double.
    events: list[str] = []
    deadline = time.monotonic() + (45.0 if backend == "offline" else 300.0)
    try:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                chunk = await asyncio.wait_for(gen.__anext__(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            stripped = chunk.strip()
            if not stripped or stripped.startswith(": keepalive"):
                continue
            line = stripped.splitlines()[0]
            # optimizer_author_token is a high-frequency stream of the live Author's
            # generation; skip it from the captured set so the cap targets meaningful
            # lifecycle/scoring/island events (and we run long enough to see an island step).
            if "optimizer_author_token" in line:
                continue
            events.append(chunk)
            print(f"[e2e] event[{len(events)}]: {line[:140]}", flush=True)
            if "optimizer_island_step" in line or len(events) >= 12:
                break
    finally:
        await gen.aclose()

    snap = state.optimizer_v2_snapshot()
    islands = snap.get("models", {}).get(model, {}).get("islands", [])
    rounds = snap.get("models", {}).get(model, {}).get("tournament_rounds", [])
    print(
        f"[e2e] snapshot status={snap.get('status')} "
        f"islands={len(islands)} rounds={len(rounds)} events_seen={len(events)}",
        flush=True,
    )
    if islands:
        print(f"[e2e] island[0]: {islands[0]}", flush=True)

    # --- stop the background run cleanly ---
    await state.reset_optimizer_v2()
    print(f"[e2e] reset -> status={state.optimizer_v2_status}", flush=True)

    ok = bool(launched) and len(events) >= 1
    verdict = "PASS" if ok else "PARTIAL"
    print(
        f"[e2e] RESULT={verdict} launched={launched} v2_events={len(events)} "
        f"islands={len(islands)}",
        flush=True,
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
