"""
Offline e2e for the Tier-2 GEPA orchestrator path (spec: optimizer-ragas-gepa, Req 6/9/10/12/15).

Drives ``run_v2`` with ``QUALITY_OPT_TIER2_GEPA_ENABLED=True`` over the OFFLINE backend doubles
(zero network, fake gepa engine). Proves:
  * the gate routes to ``_run_model_gepa`` (NOT the island/tournament loop — no island_step events);
  * the seeded phase_a_split + coverage-ladder budget + the Opus JudgeInLoopScorer metric +
    FakeGepaEngine + the OfflineAuthorClient proposer all wire together;
  * a champion_scored event carries the winner's per_dimension (the named-dimension hook, Req 8);
  * Phase B runs on the validation complement and returns a real triad (KEEP list, Req 10).

It also asserts the flag-OFF run still takes the island path (parity), so the gate is additive.
"""
from __future__ import annotations

import asyncio

from bakeoff import config
from bakeoff.quality.optimizer.backends import build_offline_backend
from bakeoff.quality.optimizer.orchestrator import PerModelOrchestrator, ViewRegistry
from bakeoff.quality.optimizer.store import OptimizerStore
from bakeoff.types import CohortKey, GoldFragment, Item, Turn


class _RecordingEmitter:
    def __init__(self) -> None:
        self.island_steps: list[dict] = []
        self.champion_scored_calls: list[dict] = []

    def island_step(self, **kwargs):
        self.island_steps.append(kwargs)

    def champion_scored(self, **kwargs):
        self.champion_scored_calls.append(kwargs)

    # no-op stubs for the rest of the emitter surface
    def rung_escalated(self, **kwargs):
        pass

    def tournament(self, **kwargs):
        pass

    def migration(self, **kwargs):
        pass

    def author_token(self, **kwargs):
        pass

    def iteration_completed(self, **kwargs):
        pass

    def converged(self, **kwargs):
        pass

    def phase_b(self, **kwargs):
        pass


def _cohort(answerability: str = "full") -> CohortKey:
    return CohortKey(
        geography="US", proficiency="fluent", tone="neutral", entry_route="slack",
        momentary_state="neutral", answerability=answerability, turn_type="multi",
    )


def _gold_item(item_id: str) -> Item:
    return Item(
        id=item_id, turn_type="multi", cohort=_cohort("full"),
        wants="how to request a corporate card", answerability="full",
        gold=[GoldFragment(node_id="g1", title="Card", markdown="Request via portal.")],
        turns=(Turn(turn=1, user_utterance="How?", momentary_state="neutral", answerability="full"),),
    )


def _abstention_item(item_id: str) -> Item:
    return Item(
        id=item_id, turn_type="multi", cohort=_cohort("none"), answerability="none",
        turns=(Turn(turn=1, user_utterance="Can I expense X?", momentary_state="neutral", answerability="none"),),
    )


def _mixed_slice() -> list[Item]:
    items = [_gold_item(f"g-{i}") for i in range(6)]
    items += [_abstention_item(f"ab-{i}") for i in range(4)]
    items += [_gold_item(f"extra-{i}") for i in range(2)]
    return items


def _run_model(tmp_path, monkeypatch, *, gepa_enabled: bool):
    monkeypatch.setattr(config, "QUALITY_OPT_TIER2_GEPA_ENABLED", gepa_enabled)
    monkeypatch.setattr(config, "QUALITY_OPT_GEPA_BACKEND", "fake")
    monkeypatch.setattr(config, "QUALITY_OPT_GEPA_ROLLOUT_BUDGET", 3)  # cap metric calls for speed
    monkeypatch.setattr(config, "QUALITY_OPT_RUNG_SIZES", (4, 8))
    monkeypatch.setattr(config, "QUALITY_OPT_RUNG_REPS", (1, 1))
    monkeypatch.setattr(config, "QUALITY_OPT_TOURNAMENT_ROUNDS", 1)
    monkeypatch.setattr(config, "QUALITY_OPT_TOURNAMENT_EVERY_ITERS", 2)
    monkeypatch.setattr(config, "QUALITY_OPT_TOURNAMENT_MIN_RUNG", 0)

    items = _mixed_slice()
    model = "haiku-4.5"
    store = OptimizerStore(
        iterations_path=tmp_path / "it.jsonl", audit_path=tmp_path / "au.jsonl",
        errors_path=tmp_path / "er.jsonl", results_path=tmp_path / "res.json",
    )
    backend = build_offline_backend()
    emitter = _RecordingEmitter()
    view_registry = ViewRegistry()
    view_registry.mark_active(model)
    orch = PerModelOrchestrator(
        models=[model], backend=backend, store=store, emitter=emitter,
        view_registry=view_registry, validation_items=items, phase_b_reps=1,
    )
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(
            orch.run_v2(models=[model], backend=backend, emitter=emitter, store=store, all_items=items)
        )
    finally:
        loop.close()
    return result, emitter, model


def test_gepa_path_runs_and_skips_islands(tmp_path, monkeypatch):
    result, emitter, model = _run_model(tmp_path, monkeypatch, gepa_enabled=True)
    # Took the GEPA branch: NO island_step events were emitted.
    assert emitter.island_steps == []
    # Winner surfaced with a per_dimension payload (named-dimension hook, Req 8).
    assert len(emitter.champion_scored_calls) >= 1
    assert "per_dimension" in emitter.champion_scored_calls[0]
    # Phase B ran on the validation complement and returned a real triad (KEEP list, Req 10).
    assert model in result
    pb = result[model]
    assert hasattr(pb, "triad_score") and pb.triad_score >= 0.0
    assert pb.n_conversations > 0


def test_flag_off_takes_island_path(tmp_path, monkeypatch):
    # Parity: with the gate OFF, the island/tournament path runs (island_step events fire),
    # proving the GEPA branch is purely additive.
    _result, emitter, _model = _run_model(tmp_path, monkeypatch, gepa_enabled=False)
    assert len(emitter.island_steps) > 0
