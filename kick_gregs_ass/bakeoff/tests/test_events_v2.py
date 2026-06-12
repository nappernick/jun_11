"""Unit tests for v2 island-tournament SSE event methods on OptimizerEventEmitter."""
from __future__ import annotations

from bakeoff.app import SSEBroker
from bakeoff.quality.optimizer.events import (
    EVENT_ISLAND_STEP,
    EVENT_MIGRATION,
    EVENT_RUNG_ESCALATED,
    EVENT_TOURNAMENT,
    MODEL_CHANNEL,
    OptimizerEventEmitter,
)


def _drain(sub) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


def test_island_step():
    broker = SSEBroker()
    sub = broker.open()
    emitter = OptimizerEventEmitter(broker)

    emitter.island_step("haiku-4.5", island_id=1, rung_index=2,
                        champion_score=0.72, ci_half_width=0.05, state="iterating")

    [(etype, payload)] = _drain(sub)
    assert etype == EVENT_ISLAND_STEP
    assert payload[MODEL_CHANNEL] == "haiku-4.5"
    assert payload["island_id"] == 1
    assert payload["rung_index"] == 2
    assert payload["champion_score"] == 0.72
    assert payload["ci_half_width"] == 0.05
    assert payload["state"] == "iterating"


def test_rung_escalated():
    broker = SSEBroker()
    sub = broker.open()
    emitter = OptimizerEventEmitter(broker)

    emitter.rung_escalated("sonnet-4.6", island_id=0, from_rung=1, to_rung=2)

    [(etype, payload)] = _drain(sub)
    assert etype == EVENT_RUNG_ESCALATED
    assert payload[MODEL_CHANNEL] == "sonnet-4.6"
    assert payload["island_id"] == 0
    assert payload["from_rung"] == 1
    assert payload["to_rung"] == 2


def test_tournament():
    broker = SSEBroker()
    sub = broker.open()
    emitter = OptimizerEventEmitter(broker)

    island_a = {"champion_score": 0.68, "ci_half_width": 0.04}
    island_b = {"champion_score": 0.71, "ci_half_width": 0.03}
    emitter.tournament("haiku-4.5", round=2, island_a=island_a,
                       island_b=island_b, shared_rung=3, winner=1)

    [(etype, payload)] = _drain(sub)
    assert etype == EVENT_TOURNAMENT
    assert payload[MODEL_CHANNEL] == "haiku-4.5"
    assert payload["round"] == 2
    assert payload["island_a"] == island_a
    assert payload["island_b"] == island_b
    assert payload["shared_rung"] == 3
    assert payload["winner"] == 1


def test_migration():
    broker = SSEBroker()
    sub = broker.open()
    emitter = OptimizerEventEmitter(broker)

    emitter.migration("sonnet-4.6", round=3, winning_prompt_version_id="pv-abc123")

    [(etype, payload)] = _drain(sub)
    assert etype == EVENT_MIGRATION
    assert payload[MODEL_CHANNEL] == "sonnet-4.6"
    assert payload["round"] == 3
    assert payload["winning_prompt_version_id"] == "pv-abc123"
