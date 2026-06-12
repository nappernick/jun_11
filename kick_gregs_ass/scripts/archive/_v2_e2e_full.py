"""Full v2 run to COMPLETION (offline): does it climb the rungs, run tournaments,
finish BOTH models, and reach a terminal `completed` state? Prints periodic
progress so partial results survive a wall-clock kill. No TestClient/SSE.

Run:  PYTHONPATH=<repo> .venv/bin/python scripts/_v2_e2e_full.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile

from bakeoff import config

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="v2_full_"))
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


def _summarize(state) -> str:
    snap = state.optimizer_v2_snapshot()
    parts = [f"status={state.optimizer_v2_status}"]
    for model, blk in snap.get("models", {}).items():
        islands = blk.get("islands", [])
        rounds = blk.get("tournament_rounds", [])
        max_rung = max((i.get("rung_index", 0) for i in islands), default=-1)
        parts.append(f"{model}:isl={len(islands)},maxRung={max_rung},rounds={len(rounds)}")
    return " | ".join(parts)


async def main() -> int:
    app = create_app()
    state = app.state.bakeoff

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

    models = list(config.QUALITY_MODELS.keys())
    print(f"models={models} tournament_rounds_cfg={getattr(config, 'QUALITY_OPT_TOURNAMENT_ROUNDS', '?')}")
    ok = await state.start_optimizer_v2(backend="offline", models=models)
    print(f"launched={ok} status={state.optimizer_v2_status}", flush=True)
    if not ok:
        return 1

    task = getattr(state, "_optimizer_v2_task", None)
    deadline = asyncio.get_event_loop().time() + 240.0
    tick = 0
    while task is not None and not task.done():
        await asyncio.sleep(5.0)
        tick += 1
        print(f"[t+{tick*5}s] {_summarize(state)} escalations={counts.get('optimizer_island_step', 0)}/"
              f"rung_esc={counts.get('optimizer_rung_escalated', 0)} tourn={counts.get('optimizer_tournament', 0)}",
              flush=True)
        if asyncio.get_event_loop().time() > deadline:
            print("[timeout] 240s elapsed; not awaiting further", flush=True)
            break

    if task is not None and task.done():
        exc = task.exception() if not task.cancelled() else None
        if exc:
            print(f"task raised: {exc!r}", flush=True)

    snap = state.optimizer_v2_snapshot()
    print("=== FINAL ===", flush=True)
    print(f"status={state.optimizer_v2_status} error={state.optimizer_v2_error}")
    print(f"event types: {sorted(counts)}")
    print(f"counts: island_step={counts.get('optimizer_island_step',0)} "
          f"rung_escalated={counts.get('optimizer_rung_escalated',0)} "
          f"tournament={counts.get('optimizer_tournament',0)} "
          f"migration={counts.get('optimizer_migration',0)} "
          f"phase_b={counts.get('optimizer_phase_b',0)}")
    finished_models = 0
    for model, blk in snap.get("models", {}).items():
        islands = blk.get("islands", [])
        rounds = blk.get("tournament_rounds", [])
        max_rung = max((i.get("rung_index", 0) for i in islands), default=-1)
        if len(islands) >= 2:
            finished_models += 1
        print(f"  {model}: islands={len(islands)} maxRung={max_rung} rounds={len(rounds)} "
              f"best={blk.get('best_triad')} phase_b={blk.get('phase_b_triad')}")
        for isl in islands:
            print(f"    island {isl['island_id']} rung={isl['rung_index']} "
                  f"score={isl['champion_score']:.4f} state={isl['state']} iters={isl.get('iterations')}")

    completed = state.optimizer_v2_status == OptimizerStatus.COMPLETED
    climbed = counts.get("optimizer_rung_escalated", 0) > 0
    both_models = finished_models == len(models)
    print(f"VERDICT completed={completed} climbed_rungs={climbed} both_models={both_models}")
    print("FULL_RUN", "PASS" if (completed and both_models) else "INCOMPLETE")
    # leave any lingering task cancelled for a clean exit
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except BaseException:
            pass
    return 0 if (completed and both_models) else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
