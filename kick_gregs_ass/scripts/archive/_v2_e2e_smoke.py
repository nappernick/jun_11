"""Requirement 8.3 evidence: a v2 run started through the V2 start path reaches
`running`, emits island events, and surfaces per-island progress on the v2 status
snapshot — driven OFFLINE, in-process, with no TestClient/SSE stream so there is
no interpreter-teardown hang.

Run:  .venv/bin/python scripts/_v2_e2e_smoke.py
Exit 0 = PASS (evidence collected), 1 = FAIL.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile

from bakeoff import config

# Redirect the optimizer's durable stores to a throwaway dir so this proof writes
# nothing into real data/ and the snapshot reflects ONLY this run's records.
_tmp = pathlib.Path(tempfile.mkdtemp(prefix="v2_e2e_"))
for _attr in (
    "QUALITY_OPT_ITERATIONS_PATH",
    "QUALITY_OPT_AUDIT_PATH",
    "QUALITY_OPT_ERRORS_PATH",
    "QUALITY_OPT_RESULTS_PATH",
):
    if hasattr(config, _attr):
        setattr(config, _attr, _tmp / f"{_attr.lower()}.jsonl")
if hasattr(config, "ensure_dirs"):
    config.ensure_dirs()

from bakeoff.app import OptimizerStatus, create_app  # noqa: E402


async def main() -> int:
    app = create_app()
    state = app.state.bakeoff

    # Count every event published on either broker (the v2 path may use a
    # dedicated broker); we only need to know island_step actually fired.
    counts: dict[str, int] = {}

    def _wrap(broker) -> None:
        orig = broker.publish

        def _pub(event_type, payload):
            counts[event_type] = counts.get(event_type, 0) + 1
            return orig(event_type, payload)

        broker.publish = _pub

    _wrap(state.broker)
    if hasattr(state, "optimizer_v2_broker"):
        _wrap(state.optimizer_v2_broker)

    models = list(config.QUALITY_MODELS.keys())[:2]
    ok = await state.start_optimizer_v2(backend="offline", models=models)
    print(f"start_optimizer_v2 -> launched={ok} status={state.optimizer_v2_status}")
    if not ok:
        print("REQ8.3 FAIL (start returned False)")
        return 1

    saw_running = state.optimizer_v2_status == OptimizerStatus.RUNNING
    saw_islands = False
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 55.0
    while loop.time() < deadline:
        await asyncio.sleep(0.5)
        if state.optimizer_v2_status == OptimizerStatus.RUNNING:
            saw_running = True
        snap = state.optimizer_v2_snapshot()
        for blk in snap.get("models", {}).values():
            if blk.get("islands"):
                saw_islands = True
        if saw_running and saw_islands and counts.get("optimizer_island_step", 0) > 0:
            break
        if state.optimizer_v2_status in (OptimizerStatus.COMPLETED, OptimizerStatus.FAILED):
            break

    snap = state.optimizer_v2_snapshot()

    # Stop the background task cleanly so the interpreter can exit.
    task = getattr(state, "_optimizer_v2_task", None)
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except BaseException:
            pass

    island_steps = counts.get("optimizer_island_step", 0)
    print(f"final status={state.optimizer_v2_status} error={state.optimizer_v2_error}")
    print(f"saw_running={saw_running} saw_islands={saw_islands} island_step_events={island_steps}")
    print(f"event types seen: {sorted(counts)}")
    for model, blk in snap.get("models", {}).items():
        islands = blk.get("islands", [])
        rounds = blk.get("tournament_rounds", [])
        print(f"  model={model}: islands={len(islands)} tournament_rounds={len(rounds)}")
        for isl in islands:
            print(
                f"    island {isl['island_id']} rung={isl['rung_index']} "
                f"score={isl['champion_score']:.4f} ci={isl['champion_ci_half_width']:.4f} "
                f"state={isl['state']} iters={isl.get('iterations')}"
            )

    passed = (
        saw_running
        and saw_islands
        and island_steps > 0
        and state.optimizer_v2_status != OptimizerStatus.FAILED
    )
    print("REQ8.3", "PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
