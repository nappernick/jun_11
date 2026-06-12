"""
V3Orchestrator — concurrent, contained orchestration over v2's island-tournament logic.

Subclasses :class:`bakeoff.quality.optimizer.orchestrator.PerModelOrchestrator` to REUSE
its durable persistence (``_persist_iteration``), Phase B plumbing (``_run_phase_b``),
and item-split helpers — and replaces the run loop with V3's failure envelope:

* **Models always concurrent.** v2 gated concurrency on every model having an active
  Per_Model_View; V3 is live-only and always gathers the models. Each model is
  contained: its failure produces a ``{"status": "failed"}`` result, never an exception
  that takes the sibling models down.
* **Islands step concurrently in waves.** Both islands' ``step()`` (contained — a V3
  island never raises) are gathered per wave; escalation / stuck / tournament checks run
  between waves, so v2's tournament semantics hold while the expensive scoring overlaps.
* **Island death, not run death.** An island whose ``consecutive_failures`` reaches
  ``config.QUALITY_OPT_V3_ISLAND_MAX_CONSECUTIVE_FAILURES`` is marked dead (emitted +
  recorded); the survivor continues alone. When every island dies the model freezes its
  best-known champion and still proceeds to Phase B, flagged ``degraded``.
* **Tournaments degrade instead of failing.** Each champion's tournament score is
  attempted independently; a failed side falls back to its island's last known rung
  score (flagged stale). If both sides fail the round is skipped (emitted as
  ``optimizer_tournament_degraded``) and the budget still advances, so a flaky patch
  can never wedge the loop short of the freeze point.
* **Phase sentinel + resume.** A small atomically-written state file
  (``config.QUALITY_OPT_V3_STATE_PATH``) records per-model progress
  (``phase_a_complete`` + frozen champion, ``phase_b_done`` + result). Resume skips
  straight past completed phases; island-grain resume inside Phase A reuses v2's
  durable-record restore against the v3 store paths.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from bakeoff import config
from bakeoff.quality.optimizer.orchestrator import ConcurrencyDecision, PerModelOrchestrator
from bakeoff.quality.optimizer.rungs import build_rung_ladder
from bakeoff.quality.optimizer.tournament import (
    TournamentBudget,
    choose_shared_rung,
    decide_winner,
    migration_plan,
    should_run_tournament,
)
from bakeoff.quality.optimizer.v3.guards import guarded_call
from bakeoff.quality.optimizer.v3.island import ResilientIslandLoop

__all__ = ["V3Orchestrator"]

_LOG = logging.getLogger("bakeoff.opt.v3.orchestrator")

#: V3-only SSE event names (the v3 broker is separate, so no collision with v2).
EVENT_ISLAND_DEAD = "optimizer_island_dead"
EVENT_TOURNAMENT_DEGRADED = "optimizer_tournament_degraded"
EVENT_PHASE = "optimizer_phase"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_override_for(model: str, island_id: int) -> Optional[str]:
    """The owner-provided seed prompt for ``(model, island)``, or ``None``.

    Read from ``config.QUALITY_OPT_V3_SEEDS_DIR/<model>_i<island>.txt`` — fully
    optional; a missing/unreadable file falls back to the default seed.
    """
    seed_path = config.QUALITY_OPT_V3_SEEDS_DIR / f"{model}_i{island_id}.txt"
    try:
        text = seed_path.read_text(encoding="utf-8").strip()
        return text or None
    except OSError:
        return None


def _rounds_budget_for(rung_index: int) -> int:
    """Rounds the fixed schedule allots at ``rung_index`` (last value repeats)."""
    schedule = config.QUALITY_OPT_V3_ROUNDS_PER_RUNG
    if not schedule:
        return 1
    clamped_index = min(max(0, rung_index), len(schedule) - 1)
    return max(1, int(schedule[clamped_index]))


class V3Orchestrator(PerModelOrchestrator):
    """v2's persistence + Phase B under V3's concurrency and containment."""

    def __init__(
        self, *args, state_path: Optional[Path] = None, turn_mode: str = "multi", **kwargs
    ) -> None:
        super().__init__(*args, **kwargs)
        self._state_path = Path(state_path or config.QUALITY_OPT_V3_STATE_PATH)
        # Stamped onto every persisted record so the dashboard can split single-run vs
        # multi-run views; read by the base _persist_iteration via getattr(self, "_turn_mode").
        self._turn_mode = turn_mode if turn_mode in ("single", "multi", "both") else "multi"

    # -- run-state sentinel (atomic read/write) -------------------------------------------
    def _read_state(self) -> dict:
        """Read the per-model run-state sentinel; empty dict when absent/corrupt."""
        try:
            return json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _write_model_state(self, model: str, **fields: Any) -> None:
        """Merge ``fields`` into ``model``'s sentinel entry; atomic temp+rename write."""
        state = self._read_state()
        entry = dict(state.get(model) or {})
        entry.update(fields, updated_at=_utc_now())
        state[model] = entry
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._state_path.parent), prefix=self._state_path.name, suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(state, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self._state_path)
        except OSError:
            _LOG.warning("state sentinel write failed; continuing", exc_info=True)
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

    # -- entry point -----------------------------------------------------------------------
    async def run_v3(
        self,
        models: Sequence[str],
        backend: Any,
        *,
        emitter: Any,
        store: Any,
        all_items: Sequence[Any],
    ) -> dict[str, Any]:
        """Run every model concurrently, each contained; return per-model results."""
        self._backend = backend
        self._store = store
        self._emitter = emitter
        effective_models = tuple(dict.fromkeys(models))
        # V3 is live-only and always concurrent — record the decision for observability.
        self.last_decision = ConcurrencyDecision(
            mode="concurrent",
            models=effective_models,
            viewable=effective_models,
            all_viewable=True,
        )
        if not effective_models:
            return {}

        results = await asyncio.gather(
            *(self._run_model_contained(m, all_items) for m in effective_models)
        )
        return dict(zip(effective_models, results))

    async def _run_model_contained(self, model: str, all_items: Sequence[Any]) -> dict:
        """One model's full run; ANY failure becomes a structured failed result."""
        try:
            return await self._run_model_v3(model, all_items)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — model containment boundary
            _LOG.exception("model %s: run failed: %r", model, exc)
            self._store.append_error(
                {"model": model, "where": "run_model_v3", "error": repr(exc), "ts": _utc_now()}
            )
            self._write_model_state(model, status="failed", error=repr(exc))
            return {"status": "failed", "model": model, "error": repr(exc)}

    # -- per-model loop ----------------------------------------------------------------------
    async def _run_model_v3(self, model: str, all_items: Sequence[Any]) -> dict:
        from bakeoff.quality.optimizer.controller import IterationController

        tuning, validation = IterationController.phase_a_split(all_items)
        sentinel = self._read_state().get(model) or {}
        # Record this run's turn_mode in the state sentinel so the dashboard can route the
        # LIVE view to the correct single/multi section (records carry it for the durable side).
        self._write_model_state(model, turn_mode=self._turn_mode)

        # Resume past completed phases.
        if sentinel.get("phase_b_done") and sentinel.get("result") is not None:
            _LOG.info("model %s: phase B already done (sentinel); returning stored result", model)
            return dict(sentinel["result"])
        if sentinel.get("phase_a_complete") and sentinel.get("champion_instruction"):
            _LOG.info("model %s: phase A complete (sentinel); going straight to Phase B", model)
            self._emit_phase(model, phase="B", note="resumed past completed Phase A")
            return await self._phase_b_contained(
                model, sentinel["champion_instruction"], validation, degraded=False
            )

        # V3 cycles are one-rep: a rung pass is its items exactly once (6 at rung 0),
        # so the first visible result lands in minutes, not a 3-rep grind.
        ladder = build_rung_ladder(tuning, reps_per_rung=config.QUALITY_OPT_V3_RUNG_REPS)
        islands, budget, total_iters = self._restore_or_seed_islands(model=model, ladder=ladder)
        dead_cap = max(1, int(config.QUALITY_OPT_V3_ISLAND_MAX_CONSECUTIVE_FAILURES))
        dead_ids: set[int] = set(sentinel.get("dead_islands") or [])

        # Announce each island's starting (seed/restored) prompt so the UI can pin
        # the ORIGINAL entry of the prompt lineage before any round completes.
        for island in islands:
            try:
                self._emitter.emit(
                    "optimizer_island_seeded",
                    model,
                    {
                        "island_id": island.island_id,
                        "champion_instruction": island.champion_instruction,
                    },
                )
            except Exception:  # noqa: BLE001 — observability only
                _LOG.warning("island_seeded emit failed; ignoring", exc_info=True)

        # FIXED SCHEDULE (owner direction): an island spends its allotted rounds at
        # each rung (config.QUALITY_OPT_V3_ROUNDS_PER_RUNG), climbs win-or-lose, and
        # is DONE when its top-rung allotment is spent. Phase A ends when every live
        # island has finished its schedule (tournament cadence unchanged).
        top_rung = len(ladder) - 1

        def schedule_done(island) -> bool:
            state = island.state()
            return (
                state.rung_index >= top_rung
                and state.iterations_at_rung >= _rounds_budget_for(state.rung_index)
            )

        self._emit_phase(model, phase="A", note=f"loop enter (islands={len(islands)})")
        while True:
            alive = [isl for isl in islands if isl.island_id not in dead_ids]
            if not alive:
                _LOG.error("model %s: every island is dead; freezing best-known champion", model)
                break
            pending = [isl for isl in alive if not schedule_done(isl)]
            if not pending:
                _LOG.info("model %s: every live island finished its rung schedule", model)
                break

            # Wave: every still-scheduled island steps CONCURRENTLY (contained).
            states = list(await asyncio.gather(*(isl.step() for isl in pending)))
            total_iters += len(pending)
            alive = pending

            for island, state in zip(alive, states):
                self._emitter.island_step(
                    model=model,
                    island_id=island.island_id,
                    rung_index=state.rung_index,
                    champion_score=state.champion_score,
                    ci_half_width=state.champion_ci_half_width,
                    state="stuck" if state.stuck else "iterating",
                )
                self._persist_iteration(
                    model, state, island.last_step_detail(), budget.current_round
                )
                if island.last_skip is not None:
                    self._store.append_error(
                        {
                            "model": model,
                            "island_id": island.island_id,
                            "where": "iteration_skipped",
                            "survivors": island.last_skip.survivors,
                            "total": island.last_skip.total,
                            "failures": [f.to_dict() for f in island.last_skip.failures],
                            "ts": _utc_now(),
                        }
                    )

                # Island death check (deterministic failures converge here, not spin).
                if island.consecutive_failures >= dead_cap:
                    dead_ids.add(island.island_id)
                    _LOG.error(
                        "model %s: island %d marked DEAD after %d consecutive failures",
                        model, island.island_id, island.consecutive_failures,
                    )
                    self._emitter.emit(
                        EVENT_ISLAND_DEAD,
                        model,
                        {
                            "island_id": island.island_id,
                            "consecutive_failures": island.consecutive_failures,
                        },
                    )
                    self._write_model_state(model, dead_islands=sorted(dead_ids))
                    continue

                # FIXED-SCHEDULE escalation: climb once this rung's round budget is
                # spent, promotion or not (owner direction — replaces v2's
                # promotion-gated should_escalate / is_stuck pair).
                if (
                    state.rung_index < top_rung
                    and state.iterations_at_rung >= _rounds_budget_for(state.rung_index)
                ):
                    await self._escalate_contained(model, island, budget)

            # Wave-level tournament check (v2 cadence, alive islands only).
            alive = [isl for isl in islands if isl.island_id not in dead_ids]
            if len(alive) >= 2:
                states_now = [isl.state() for isl in alive]
                if should_run_tournament(states_now, total_iters=total_iters):
                    await self._tournament_contained(model, alive, states_now, budget, ladder)
                    budget = TournamentBudget(current_round=budget.current_round + 1)

        # Freeze the survivor: best champion across ALL islands (dead ones still carry
        # their last good champion — death must not erase progress).
        survivor_states = [isl.state() for isl in islands]
        best = max(survivor_states, key=lambda s: s.champion_score or 0.0)
        degraded = len(dead_ids) >= len(islands)
        self._write_model_state(
            model,
            phase_a_complete=True,
            champion_instruction=best.champion_instruction,
            champion_score=best.champion_score,
            dead_islands=sorted(dead_ids),
            degraded=degraded,
        )
        self._emit_phase(model, phase="B", note="Phase A frozen")
        return await self._phase_b_contained(
            model, best.champion_instruction, validation, degraded=degraded
        )

    # -- contained collaborators ---------------------------------------------------------
    async def _escalate_contained(self, model: str, island, budget) -> None:
        """advance_rung with containment: a failed re-score leaves the island at the new
        rung with no baseline (the next step measures it) instead of failing the run."""
        old_rung = island.state().rung_index
        try:
            new_state = await island.advance_rung()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — containment boundary
            _LOG.warning(
                "model %s island %d: escalation re-score failed (%r); island continues at "
                "its new rung without a baseline",
                model, island.island_id, exc,
            )
            self._store.append_error(
                {
                    "model": model,
                    "island_id": island.island_id,
                    "where": "advance_rung",
                    "error": repr(exc),
                    "ts": _utc_now(),
                }
            )
            return
        self._emitter.rung_escalated(
            model=model,
            island_id=island.island_id,
            from_rung=old_rung,
            to_rung=new_state.rung_index,
        )
        if new_state.champion_score is not None:
            self._emitter.island_step(
                model=model,
                island_id=island.island_id,
                rung_index=new_state.rung_index,
                champion_score=new_state.champion_score,
                ci_half_width=new_state.champion_ci_half_width,
                state="stuck" if new_state.stuck else "iterating",
            )
            # POSITION-ONLY record (detail=None): the escalation re-score is a
            # measurement, not an iteration. Passing last_step_detail here (v2's
            # pattern) duplicated the just-persisted iteration's audit record —
            # double prompt-history entries — and stamped the STALE pre-promotion
            # champion score onto the new rung. The position-only record carries
            # new_state's re-scored champion at the new rung instead.
            self._persist_iteration(model, new_state, None, budget.current_round)

    async def _tournament_contained(self, model, islands, states, budget, ladder) -> None:
        """One tournament round where a failed side degrades to its last known score.

        Both champions are scored CONCURRENTLY on the shared rung. A side whose scoring
        fails falls back to its island's last rung score (flagged stale); when both
        fail the round is skipped via ``optimizer_tournament_degraded`` (the caller
        still advances the budget, so the loop always progresses toward the freeze).
        """
        shared_rung_idx = choose_shared_rung(states)
        rung = ladder[shared_rung_idx]

        async def score_side(island, state):
            scorer = island._scorer_for(rung.reps)  # the island's V3 resilient scorer
            try:
                score = await scorer.score_prompt(
                    model=model,
                    instruction=island.champion_instruction,
                    items=rung.items,
                    prompt_role="champion",
                )
                return score.triad_score, score.ci_half_width, False
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — tournament containment
                _LOG.warning(
                    "model %s island %d: tournament scoring failed (%r); using last "
                    "known rung score",
                    model, island.island_id, exc,
                )
                if state.champion_score is None:
                    return None, None, True
                return state.champion_score, state.champion_ci_half_width or 0.0, True

        sides = await asyncio.gather(
            score_side(islands[0], states[0]), score_side(islands[1], states[1])
        )
        (score_a, ci_a, stale_a), (score_b, ci_b, stale_b) = sides

        if score_a is None and score_b is None:
            self._emitter.emit(
                EVENT_TOURNAMENT_DEGRADED,
                model,
                {"round": budget.current_round, "reason": "both sides unscorable; round skipped"},
            )
            return
        if score_a is None:
            score_a, ci_a = float("-inf"), 0.0
        if score_b is None:
            score_b, ci_b = float("-inf"), 0.0

        decision = decide_winner(score_a, ci_a, score_b, ci_b)
        self._emitter.tournament(
            model=model,
            round=budget.current_round,
            island_a={"champion_score": score_a, "ci_half_width": ci_a, "stale": stale_a},
            island_b={"champion_score": score_b, "ci_half_width": ci_b, "stale": stale_b},
            shared_rung=shared_rung_idx,
            winner=decision.winner_island_id,
        )

        winner_island = islands[decision.winner_island_id]
        plan = migration_plan(
            winner_island_id=decision.winner_island_id,
            winning_instruction=winner_island.champion_instruction,
        )
        # MONOTONIC MIGRATION: the winning prompt carries its established (carried,
        # monotonic) champion score to BOTH islands. Nulling it here forced the next
        # round to re-baseline the belt-holder against a fresh, noisy re-measurement that
        # could dip — the non-monotonic "bounce" the owner observed. The best estimate of
        # the migrated prompt's quality IS the winner's carried score, so both islands
        # adopt it as the bar a challenger must beat: the belt's value travels with the
        # belt, and `_step_single`'s kept-champion branch (which only seeds when the score
        # is None) then holds it instead of overwriting it with a re-roll.
        winner_score = winner_island._champion_score
        winner_ci = winner_island._champion_ci_half_width
        for island in islands:
            island._champion_instruction = plan.winning_instruction
            island._champion_score = winner_score
            island._champion_ci_half_width = winner_ci

        import uuid

        self._emitter.migration(
            model=model,
            round=budget.current_round,
            winning_prompt_version_id=f"v3-tournament-r{budget.current_round}-{uuid.uuid4().hex[:8]}",
        )

    async def _phase_b_contained(
        self, model: str, champion_instruction: str, validation_items, *, degraded: bool
    ) -> dict:
        """Phase B with one guarded retry; a final failure still preserves the champion."""
        try:
            result = await guarded_call(
                f"phase_b:{model}",
                lambda: self._run_phase_b(model, champion_instruction, validation_items),
                # Phase B scores the whole validation slice — give it a wide wall.
                timeout_s=4 * 3600.0,
                max_retries=1,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — Phase B containment
            _LOG.exception("model %s: Phase B failed after retry: %r", model, exc)
            failed = {
                "status": "phase_b_failed",
                "model": model,
                "champion_instruction": champion_instruction,
                "degraded": degraded,
                "error": repr(exc),
            }
            self._write_model_state(model, phase_b_done=False, result=failed, status="phase_b_failed")
            return failed

        payload = {
            "status": "completed",
            "model": model,
            "degraded": degraded,
            "champion_instruction": champion_instruction,
            "phase_b": result.to_dict() if hasattr(result, "to_dict") else result,
        }
        self._write_model_state(model, phase_b_done=True, result=_json_safe(payload), status="completed")
        return payload

    # -- restore/seed with the V3 island class ----------------------------------------------
    def _restore_or_seed_islands(self, *, model: str, ladder: Sequence[Any]):
        """v2's durable restore, constructing :class:`ResilientIslandLoop` islands."""
        island_groups = self._store.iteration_history_by_island(model)
        v2_groups = {
            island_id: recs
            for (_, island_id), recs in island_groups.items()
            if island_id is not None and recs
        }

        max_tournament_round = 0
        all_records = [r for recs in v2_groups.values() for r in recs]
        if all_records:
            rounds = [r.tournament_round for r in all_records if r.tournament_round is not None]
            if rounds:
                max_tournament_round = max(rounds)

        budget = TournamentBudget(current_round=max_tournament_round)
        total_iters = sum(len(recs) for recs in v2_groups.values())
        champion_by_island = self._store.last_champion_per_island(model) if v2_groups else {}

        islands: list[ResilientIslandLoop] = []
        for island_id in range(config.QUALITY_OPT_ISLANDS_PER_MODEL):
            if island_id in v2_groups and island_id in champion_by_island:
                recs = v2_groups[island_id]
                last = recs[-1]
                isl = ResilientIslandLoop(
                    island_id=island_id,
                    model=model,
                    backend=self._backend,
                    ladder=ladder,
                    store=self._store,
                    emitter=self._emitter,
                    style=config.QUALITY_OPT_ISLAND_STYLES[island_id],
                    seed_instruction=champion_by_island[island_id],
                )
                isl._total_iterations = last.iteration_index + 1
                isl._rung_index = last.rung_index if last.rung_index is not None else 0
                # Restore the carried (monotonic) champion score verbatim — do NOT coerce a
                # legitimate score to None (the old `if x else None` nulled a real 0.0 and
                # forced a re-baseline on resume, breaking monotonicity across a restart).
                isl._champion_score = last.champion_score
                isl._champion_ci_half_width = last.champion_ci_half_width
                isl._consecutive_non_improving = last.consecutive_non_improving
                current_rung = isl._rung_index
                isl._iterations_at_rung = sum(
                    1 for r in recs if (r.rung_index or 0) == current_rung
                )
                isl._improved_at_rung = any(
                    r.promoted for r in recs if (r.rung_index or 0) == current_rung
                )
                islands.append(isl)
            else:
                islands.append(
                    ResilientIslandLoop(
                        island_id=island_id,
                        model=model,
                        backend=self._backend,
                        ladder=ladder,
                        store=self._store,
                        emitter=self._emitter,
                        style=config.QUALITY_OPT_ISLAND_STYLES[island_id],
                        # Owner-provided per-(model, island) seed when present
                        # (data/bakeoff/v3_seeds/); None falls back to the default.
                        seed_instruction=_seed_override_for(model, island_id),
                    )
                )
        return islands, budget, total_iters

    # -- small helpers -------------------------------------------------------------------
    def _emit_phase(self, model: str, *, phase: str, note: str) -> None:
        try:
            self._emitter.emit(EVENT_PHASE, model, {"phase": phase, "note": note})
        except Exception:  # noqa: BLE001 — observability only
            _LOG.warning("phase emit failed; ignoring", exc_info=True)


def _json_safe(value: Any) -> Any:
    """Best-effort coercion of a result payload into JSON-serializable primitives."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return json.loads(json.dumps(value, default=repr))
