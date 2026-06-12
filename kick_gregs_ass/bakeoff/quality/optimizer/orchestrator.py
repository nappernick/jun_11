"""
Per-model orchestration for the v2 island-tournament closed-loop prompt optimizer.

Preserves the ViewRegistry + the per-model concurrency gate (concurrent iff every running
model has an active Per_Model_View, else sequential; always sequential within a model) and
reuses PhaseBValidator for Phase B. The v2 entry point is ``PerModelOrchestrator.run_v2``.

v2 logic per model: seed 2 islands -> each runs IslandLoop.step at its rung, escalate via
escalation_gate/advance_rung when the gate fires -> when should_run_tournament fires, score
both champions on choose_shared_rung, decide_winner, emit tournament + migrate
(migration_plan) + emit migration -> diverge -> repeat for QUALITY_OPT_TOURNAMENT_ROUNDS
rounds then freeze survivor -> Phase B (PhaseBValidator, unchanged).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Iterator,
    Mapping,
    Optional,
    Sequence,
    Union,
)

from bakeoff import config
from bakeoff.quality.optimizer.island import IslandLoop, IslandState, StepDetail
from bakeoff.quality.optimizer.rungs import build_rung_ladder
from bakeoff.quality.optimizer.tournament import (
    TournamentBudget,
    TournamentDecision,
    MigrationPlan,
    choose_shared_rung,
    decide_winner,
    escalation_gate,
    migration_plan,
    should_run_tournament,
)

if TYPE_CHECKING:
    from bakeoff.quality.optimizer.backends import OptimizerBackend
    from bakeoff.quality.optimizer.events import OptimizerEventEmitter
    from bakeoff.quality.optimizer.store import OptimizerStore
    from bakeoff.quality.optimizer.validate import PhaseBResult
    from bakeoff.types import Item

__all__ = [
    "ViewRegistry",
    "ConcurrencyDecision",
    "PerModelOrchestrator",
]


# ---------------------------------------------------------------------------
# ViewRegistry — the source of truth the concurrency gate consults (Req 9.8).
# ---------------------------------------------------------------------------
class ViewRegistry:
    """Track which Target_Models currently have an active live Per_Model_View."""

    __slots__ = ("_counts", "_lock")

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def mark_active(self, model: str) -> None:
        with self._lock:
            self._counts[model] = self._counts.get(model, 0) + 1

    def mark_inactive(self, model: str) -> None:
        with self._lock:
            current = self._counts.get(model, 0)
            if current <= 1:
                self._counts.pop(model, None)
            else:
                self._counts[model] = current - 1

    def has_active_view(self, model: str) -> bool:
        with self._lock:
            return self._counts.get(model, 0) > 0

    def active_models(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._counts)

    @contextmanager
    def subscription(self, model: str) -> Iterator[None]:
        self.mark_active(model)
        try:
            yield
        finally:
            self.mark_inactive(model)


# ---------------------------------------------------------------------------
# ConcurrencyDecision
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ConcurrencyDecision:
    """Recorded outcome of the visualization gate for one orchestration run."""

    mode: str
    models: tuple[str, ...]
    viewable: tuple[str, ...]
    all_viewable: bool


# ---------------------------------------------------------------------------
# PerModelOrchestrator — v2: 2 islands + tournament loop per model.
# ---------------------------------------------------------------------------
class PerModelOrchestrator:
    """Run each Target_Model's v2 island-tournament loop then Phase B.

    Concurrency gate: concurrent iff every running model has an active Per_Model_View;
    else sequential. Always sequential within a model.
    """

    def __init__(
        self,
        *,
        models: Sequence[str],
        backend: "OptimizerBackend",
        store: "OptimizerStore",
        emitter: "OptimizerEventEmitter",
        view_registry: ViewRegistry,
        validator: Optional[Any] = None,
        validation_items: Optional[Union[Sequence["Item"], Mapping[str, Sequence["Item"]]]] = None,
        model_runner: Optional[Callable[[str], Any]] = None,
        phase_b_reps: Optional[int] = None,
        # v1 compat — unused in v2 but kept so existing call sites don't break
        controllers: Optional[Mapping[str, Any]] = None,
        controller_factory: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self._models: tuple[str, ...] = tuple(dict.fromkeys(models))
        self._backend = backend
        self._store = store
        self._emitter = emitter
        self._view_registry = view_registry
        self._validator = validator
        self._validation_items = validation_items
        self._model_runner = model_runner
        self._phase_b_reps = phase_b_reps
        self.last_decision: Optional[ConcurrencyDecision] = None

    # -- visualization gate --------------------------------------------------
    def decide_concurrency(self) -> ConcurrencyDecision:
        viewable = tuple(m for m in self._models if self._view_registry.has_active_view(m))
        all_viewable = len(viewable) == len(self._models)
        return ConcurrencyDecision(
            mode="concurrent" if all_viewable else "sequential",
            models=self._models,
            viewable=viewable,
            all_viewable=all_viewable,
        )

    # -- v2 entry point ------------------------------------------------------
    async def run_v2(
        self,
        models: Sequence[str],
        backend: "OptimizerBackend",
        *,
        emitter: "OptimizerEventEmitter",
        store: "OptimizerStore",
        **opts: Any,
    ) -> dict[str, Any]:
        """v2 entry point: per model, 2 islands + tournament loop -> Phase B.

        Exposed as the contract the CLI and /start route bind to.
        """
        # Override instance state with explicit args (the contract says these are passed in).
        self._backend = backend
        self._store = store
        self._emitter = emitter
        effective_models = tuple(dict.fromkeys(models))

        decision = self.decide_concurrency()
        self.last_decision = decision

        if not effective_models:
            return {}

        if decision.mode == "concurrent":
            results = await asyncio.gather(
                *(self._run_model_v2(m, **opts) for m in effective_models)
            )
            return dict(zip(effective_models, results))

        out: dict[str, Any] = {}
        for m in effective_models:
            out[m] = await self._run_model_v2(m, **opts)
        return out

    # -- per-model Tier-2 GEPA loop (spec: optimizer-ragas-gepa) ------------
    async def _run_model_gepa(self, model: str, **opts: Any) -> Any:
        """Tier-2: evolve the prompt with the standalone GEPA engine, then Phase B (Req 6-12, 15).

        Reuses the SAME seeded ``phase_a_split`` (Req 15), the SAME coverage ladder as the
        rollout budget (Req 9), the SAME Opus :class:`JudgeInLoopScorer` as the metric (Req 7),
        and the SAME ``PhaseBValidator`` (KEEP list — answer path / retrieval / dashboard /
        Phase B unchanged, Req 10). GEPA's reflective proposer + Pareto frontier + merge replace
        the hand-rolled island/tournament search (Req 6). The reflective proposer uses the
        backend's author model (Sonnet), never the Opus judge (Req 12).
        """
        from bakeoff.quality.optimizer.controller import IterationController, _seed_instruction_for
        from bakeoff.quality.optimizer.judge_loop import JudgeInLoopScorer
        from bakeoff.quality.optimizer.backends import AuthorJudgeConflictError
        from bakeoff.quality.optimizer.gepa_engine import (
            JudgeBackedGepaMetric,
            build_gepa_engine,
            make_bedrock_reflection_lm,
            rollout_budget_from_ladder,
        )

        all_items = opts.get("all_items") or self._validation_items_for_split(model)
        tuning, validation = IterationController.phase_a_split(all_items)
        ladder = build_rung_ladder(tuning)
        budget = rollout_budget_from_ladder(ladder)

        # Proposer != Judge (Req 12): the reflective proposer uses the backend's author model
        # (Sonnet); the Opus model is reserved for the Judge. build_live_backend already enforces
        # this, but assert here too so the GEPA path refuses to start on a misconfig.
        author = getattr(self._backend, "author", None)
        author_model = getattr(author, "author_model", None)
        if author_model is not None and author_model == config.JUDGE_MODEL_ID:
            raise AuthorJudgeConflictError(
                "GEPA reflective proposer and the Judge must be different models (Req 12): the "
                f"author resolved to {author_model!r}, which is config.JUDGE_MODEL_ID."
            )

        # Metric = the existing Opus judge triad over the tuning slice (Req 7); abstention-weighted
        # triad is the sole decider and ragas means ride as named JudgeDimensions (Req 8).
        scorer = JudgeInLoopScorer(self._backend, reps=config.QUALITY_OPT_PHASE_A_REPS)
        metric = JudgeBackedGepaMetric(scorer=scorer, model=model, items=tuning)

        # Reflective proposer for the offline/fake engine: the deterministic author edit. The
        # live engine ignores this and uses gepa's own reflective proposer + a reflection LM.
        async def _proposer(current: str, feedback: str) -> str:
            if author is None:
                return current
            authored = await author.author(
                target_model=model, champion_instruction=current, failures=()
            )
            return authored.instruction if authored.usable else current

        # The LIVE engine needs a real reflection LM (the Sonnet proposer); the FAKE engine
        # ignores it and uses the deterministic `_proposer` above. Proposer != Judge holds either
        # way (Req 12): the proposer model is the Sonnet author, never the Opus judge.
        reflection_lm = (
            make_bedrock_reflection_lm(config.QUALITY_OPT_GEPA_PROPOSER_MODEL_KEY)
            if config.QUALITY_OPT_GEPA_BACKEND == "live"
            else None
        )
        engine = build_gepa_engine(
            config.QUALITY_OPT_GEPA_BACKEND,
            proposer=_proposer,
            items=tuning,
            reflection_lm=reflection_lm,
        )
        seed = _seed_instruction_for(model)
        result = await engine.optimize(seed_instruction=seed, metric=metric, budget=budget)

        # Surface the winner's named dimensions (incl. ragas) to the dashboard (Req 8.2/8.3);
        # best-effort so a display issue never fails the run.
        try:
            self._emitter.champion_scored(
                model=model,
                phase="A",
                iteration_index=0,
                role="champion",
                triad=result.best_score,
                ci_half_width=0.0,
                ci_low=result.best_score,
                ci_high=result.best_score,
                per_dimension=dict(result.per_dimension),
                abstention_reward_mean=0.0,
                answered_when_unsure_rate=0.0,
                retrieval_backend=str(getattr(getattr(self._backend, "retrieval", None), "name", "unknown")),
                mean_closeness=0.0,
                n_conversations=0,
            )
        except Exception:  # noqa: BLE001 — emission is best-effort
            pass

        # Phase B validation on the reserved complement (KEEP, Req 10).
        return await self._run_phase_b(model, result.best_instruction, validation)

    # -- per-model v2 loop ---------------------------------------------------
    async def _run_model_v2(self, model: str, **opts: Any) -> Any:
        """Run one model's v2 island-tournament loop, then Phase B."""
        # Tier-2 gate (spec: optimizer-ragas-gepa, Req 6): when GEPA is enabled the standalone
        # engine replaces the hand-rolled island/tournament search for this run. When OFF
        # (default) this branch is never taken and the island path below is byte-for-byte
        # unchanged (Req 6 / 17).
        if config.QUALITY_OPT_TIER2_GEPA_ENABLED:
            return await self._run_model_gepa(model, **opts)
        from bakeoff.quality.optimizer.controller import IterationController

        # Split items
        all_items = opts.get("all_items") or self._validation_items_for_split(model)
        tuning, validation = IterationController.phase_a_split(all_items)

        # Build the coverage ladder from the tuning slice
        ladder = build_rung_ladder(tuning)

        # Attempt to restore island state from durable records (resume path).
        # If no records exist for this model, falls back to fresh-seed behaviour.
        islands, budget, total_iters = self._restore_or_seed_islands(
            model=model, ladder=ladder
        )

        _hb = logging.getLogger("bakeoff.opt.heartbeat")
        _hb.info("run_model_v2[%s]: ENTER loop (islands=%d, total_iters=%d)",
                 model, len(islands), total_iters)
        while not budget.should_freeze_to_phase_b:
            # Each island iterates, escalating when the gate fires
            for island in islands:
                _hb.info("island.step START: model=%s island=%s iter=%d rung=%s",
                         model, island.island_id, total_iters, getattr(island, "_rung_index", "?"))
                _t_step = time.monotonic()
                state = await island.step()
                _hb.info("island.step DONE: model=%s island=%s iter=%d in %.1fs",
                         model, island.island_id, total_iters, time.monotonic() - _t_step)
                total_iters += 1

                # Emit island_step event
                self._emitter.island_step(
                    model=model,
                    island_id=island.island_id,
                    rung_index=state.rung_index,
                    champion_score=state.champion_score,
                    ci_half_width=state.champion_ci_half_width,
                    state=state.to_dict(),
                )

                # Persist a COMPLETE iteration record + rich audit record from the step's
                # detail so the per-iteration view (prompt, diff, reasoning, scores) and the
                # trend curve survive a page reload — not just the latest position.
                self._persist_iteration(
                    model, state, island.last_step_detail(), budget.current_round
                )

                # Escalation check
                if island.should_escalate():
                    old_rung = state.rung_index
                    new_state = await island.advance_rung()
                    self._emitter.rung_escalated(
                        model=model,
                        island_id=island.island_id,
                        from_rung=old_rung,
                        to_rung=new_state.rung_index,
                    )
                    # Emit an island_step for the post-escalation baseline so the UI
                    # immediately shows the correct score at the new rung (advance_rung
                    # re-scores the champion there; without this the UI shows 0.000
                    # until the next full step completes).
                    if new_state.champion_score is not None:
                        self._emitter.island_step(
                            model=model,
                            island_id=island.island_id,
                            rung_index=new_state.rung_index,
                            champion_score=new_state.champion_score,
                            ci_half_width=new_state.champion_ci_half_width,
                            state=new_state.to_dict(),
                        )
                        self._persist_iteration(
                            model, new_state, island.last_step_detail(), budget.current_round
                        )
                elif island.is_stuck():
                    # Patience exhausted without improvement: force a tournament so the
                    # winner's prompt resets both islands and they can diverge again.
                    # This prevents an island from spinning forever at one rung.
                    states_now = [isl.state() for isl in islands]
                    await self._run_tournament(model, islands, states_now, budget, ladder)
                    budget = TournamentBudget(current_round=budget.current_round + 1)

            # Tournament check
            states = [isl.state() for isl in islands]
            if should_run_tournament(states, total_iters=total_iters):
                await self._run_tournament(model, islands, states, budget, ladder)
                budget = TournamentBudget(current_round=budget.current_round + 1)

            # Cross-family audit hook (Req 3): gated + defensive, a no-op when disabled. Runs
            # at the configured round interval, samples only the Phase A tuning slice, and
            # never aborts the run on failure.
            await self._maybe_audit(model, tuning, islands, budget.current_round)

        # Freeze the survivor: pick the island with the higher champion score
        survivor_states = [isl.state() for isl in islands]
        best = max(survivor_states, key=lambda s: s.champion_score or 0.0)
        champion_instruction = best.champion_instruction

        # Phase B validation on the complement
        result = await self._run_phase_b(model, champion_instruction, validation)
        return result

    def _restore_or_seed_islands(
        self,
        *,
        model: str,
        ladder: Sequence[Any],
    ) -> "tuple[list[IslandLoop], TournamentBudget, int]":
        """Return ``(islands, budget, total_iters)`` from durable records or fresh seeds.

        If durable :class:`~bakeoff.quality.optimizer.store.IterationRecord`\\s exist for
        this ``model``, each :class:`IslandLoop` is reconstructed with its persisted rung,
        champion instruction, and counters so the loop resumes from where it left off rather
        than re-running completed steps. The tournament budget and iteration counter are
        similarly advanced to match the last durable checkpoint.

        If no records exist (new run or after a reset), behaves identically to the
        previous fresh-seed path.
        """
        island_groups = self._store.iteration_history_by_island(model)
        # Only consider v2 records (island_id is not None).
        v2_groups = {
            island_id: recs
            for (_, island_id), recs in island_groups.items()
            if island_id is not None and recs
        }

        # Determine the furthest completed tournament round so the budget starts correctly.
        max_tournament_round = 0
        all_records = [r for recs in v2_groups.values() for r in recs]
        if all_records:
            rounds = [r.tournament_round for r in all_records if r.tournament_round is not None]
            if rounds:
                max_tournament_round = max(rounds)

        budget = TournamentBudget(current_round=max_tournament_round)
        total_iters = sum(len(recs) for recs in v2_groups.values())

        # Load the last champion instruction per island from the audit store.
        champion_by_island = self._store.last_champion_per_island(model) if v2_groups else {}

        islands: list[IslandLoop] = []
        for island_id in range(config.QUALITY_OPT_ISLANDS_PER_MODEL):
            seed_instruction: Optional[str] = None

            if island_id in v2_groups and island_id in champion_by_island:
                # Resume: inject the persisted champion as the seed so the island
                # continues from its last known-good prompt rather than the default.
                recs = v2_groups[island_id]
                last = recs[-1]
                seed_instruction = champion_by_island[island_id]

                isl = IslandLoop(
                    island_id=island_id,
                    model=model,
                    backend=self._backend,
                    ladder=ladder,
                    store=self._store,
                    emitter=self._emitter,
                    style=config.QUALITY_OPT_ISLAND_STYLES[island_id],
                    seed_instruction=seed_instruction,
                )
                # Fast-forward the counters to match the persisted state so the
                # escalation gate, patience check, and tournament trigger all see the
                # correct history rather than treating this as iteration 0.
                isl._total_iterations = last.iteration_index + 1
                isl._rung_index = last.rung_index if last.rung_index is not None else 0
                isl._champion_score = last.champion_score if last.champion_score else None
                isl._champion_ci_half_width = (
                    last.champion_ci_half_width if last.champion_ci_half_width else None
                )
                isl._consecutive_non_improving = last.consecutive_non_improving
                # Restore per-rung counters: count how many iterations at the current rung.
                current_rung = isl._rung_index
                isl._iterations_at_rung = sum(
                    1 for r in recs if (r.rung_index or 0) == current_rung
                )
                # improved_at_rung: any promotion at the current rung.
                isl._improved_at_rung = any(
                    r.promoted for r in recs if (r.rung_index or 0) == current_rung
                )
                islands.append(isl)
            else:
                # Fresh seed for islands with no durable history.
                islands.append(IslandLoop(
                    island_id=island_id,
                    model=model,
                    backend=self._backend,
                    ladder=ladder,
                    store=self._store,
                    emitter=self._emitter,
                    style=config.QUALITY_OPT_ISLAND_STYLES[island_id],
                ))

        return islands, budget, total_iters

    async def _run_tournament(
        self,
        model: str,
        islands: list[IslandLoop],
        states: list[IslandState],
        budget: TournamentBudget,
        ladder: Sequence[Any],
    ) -> None:
        """Run one tournament round: score both on shared rung, decide winner, migrate."""
        shared_rung_idx = choose_shared_rung(states)
        rung = ladder[shared_rung_idx]

        # Score both champions on the shared rung
        from bakeoff.quality.optimizer.judge_loop import JudgeInLoopScorer

        scorer = JudgeInLoopScorer(self._backend, reps=rung.reps)
        scores = []
        for island in islands:
            s = await scorer.score_prompt(
                model=model,
                instruction=island.champion_instruction,
                items=rung.items,
                prompt_role="champion",
            )
            scores.append(s)

        score_a, score_b = scores[0].triad_score, scores[1].triad_score
        ci_a, ci_b = scores[0].ci_half_width, scores[1].ci_half_width

        # Decide winner
        decision: TournamentDecision = decide_winner(score_a, ci_a, score_b, ci_b)

        # Emit tournament event
        self._emitter.tournament(
            model=model,
            round=budget.current_round,
            island_a={"champion_score": score_a, "ci_half_width": ci_a},
            island_b={"champion_score": score_b, "ci_half_width": ci_b},
            shared_rung=shared_rung_idx,
            winner=decision.winner_island_id,
        )

        # Migration: winner's instruction becomes BOTH islands' baseline
        winner_island = islands[decision.winner_island_id]
        plan: MigrationPlan = migration_plan(
            winner_island_id=decision.winner_island_id,
            winning_instruction=winner_island.champion_instruction,
        )

        # Apply migration: replace both islands' champions, preserving distinct styles
        for island in islands:
            island._champion_instruction = plan.winning_instruction
            island._champion_score = None
            island._champion_ci_half_width = None
            # Style is NOT changed — each island keeps its own style (the divergence knob)

        # Generate a version id for the winning prompt
        version_id = f"tournament-r{budget.current_round}-{uuid.uuid4().hex[:8]}"

        # Emit migration event
        self._emitter.migration(
            model=model,
            round=budget.current_round,
            winning_prompt_version_id=version_id,
        )

    async def _maybe_audit(
        self,
        model: str,
        tuning: Sequence["Item"],
        islands: list[IslandLoop],
        round_index: int,
    ) -> None:
        """Gated, defensive cross-family audit hook (Req 3). No-op when disabled.

        When ``config.QUALITY_OPT_AUDIT_ENABLED`` is on and ``round_index`` is an audit round,
        re-scores the current winner on a sample drawn ONLY from the Phase A tuning slice
        (Req 4.4) with the Opus in-loop scorer to obtain the proxy ranking, then runs the
        :class:`~bakeoff.quality.optimizer.audit.AuditSeam` (which obfuscates the material and
        scores it with the non-Claude Audit_Judge, Req 3.3) and emits
        ``optimizer_audit_flag`` if the proxy-vs-audit divergence exceeds the threshold
        (Req 3.4 / 3.5). The whole hook is wrapped so an audit failure is logged and skipped —
        the audit is observability only and must never be able to fail the study it observes.
        """
        if not config.QUALITY_OPT_AUDIT_ENABLED:
            return

        import logging
        from collections import defaultdict

        from bakeoff.quality.optimizer.audit import AuditSample, AuditSeam
        from bakeoff.quality.optimizer.judge_loop import JudgeInLoopScorer

        seam = AuditSeam.from_backend(self._backend)
        if not seam.is_audit_round(round_index):
            return
        try:
            states = [isl.state() for isl in islands]
            best = max(states, key=lambda s: s.champion_score or 0.0)
            winner_instruction = best.champion_instruction

            k = max(1, int(config.QUALITY_OPT_AUDIT_SAMPLE_SIZE))
            sample_items = list(tuning)[:k]
            if not sample_items:
                return

            # Proxy (Opus) ranking: re-score the winner on the sample with the in-loop judge.
            scorer = JudgeInLoopScorer(self._backend, reps=1)
            slice_score = await scorer.score_prompt(
                model=model,
                instruction=winner_instruction,
                items=sample_items,
                prompt_role="champion",
            )

            # Group per-turn verdicts into per-conversation audit samples: proxy = the
            # conversation's mean per-turn overall; material = its answer excerpts.
            by_conv: dict[tuple[str, int], list] = defaultdict(list)
            for v in slice_score.verdicts:
                by_conv[(v.item_id, v.rep)].append(v)
            samples = [
                AuditSample(
                    item_id=f"{item_id}#{rep}",
                    material="\n".join(x.answer_excerpt for x in verdicts),
                    proxy_score=(sum(x.overall for x in verdicts) / len(verdicts)),
                )
                for (item_id, rep), verdicts in by_conv.items()
                if verdicts
            ]

            report = await seam.maybe_run(round_index=round_index, samples=samples)
            if report is not None and report.flagged:
                self._emitter.audit_flag(
                    model=model, round=round_index, report=report.to_dict()
                )
                logging.getLogger(__name__).warning(
                    "cross-family audit flagged potential self-preference for %s at round %d: "
                    "divergence=%.3f > threshold=%.3f (n=%d)",
                    model,
                    round_index,
                    report.divergence,
                    report.threshold,
                    report.n_items,
                )
        except Exception:  # noqa: BLE001 — audit is observability only; never fail the study.
            logging.getLogger(__name__).warning(
                "cross-family audit seam failed for %s at round %d; skipping",
                model,
                round_index,
                exc_info=True,
            )

    def _persist_iteration(
        self,
        model: str,
        state: IslandState,
        detail: "Optional[StepDetail]",
        tournament_round: int,
    ) -> None:
        """Persist a COMPLETE IterationRecord + a rich AuditRecord for one island step.

        The ``IterationRecord`` (decision SoT) carries the champion AND challenger scores,
        the real promotion outcome, and the gain both ways. The ``AuditRecord`` carries the
        full champion/challenger prompt text, the unified diff, and the Author's rationale —
        so a page reload reconstructs the per-iteration prompt/diff/reasoning view from disk
        rather than losing it with the no-replay event stream. Both are stamped with
        ``island_id`` / ``rung_index`` / ``tournament_round`` so the v2 snapshot can
        partition them per island. ``iteration_id`` / ``prompt_version_id`` fold in the
        island id so the two islands of a model never collide on a shared index.

        Falls back to a minimal (position-only) record if ``detail`` is missing, so a step
        that somehow produced no detail still anchors durably.
        """
        from datetime import datetime, timezone

        from bakeoff.quality.optimizer.ids import iteration_id, prompt_version_id
        from bakeoff.quality.optimizer.store import AuditRecord, IterationRecord

        ts = datetime.now(timezone.utc).isoformat()
        # Island-distinct id namespace: fold the island id into the phase tag so island 0 and
        # island 1 of a model never hash to the same iteration_id / prompt_version_id.
        phase_tag = f"A-i{state.island_id}"
        iter_idx = max(0, state.total_iterations - 1)
        iid = iteration_id(model, phase_tag, iter_idx)
        pvid = prompt_version_id(model, iter_idx + state.island_id * 100000)
        author_model = getattr(getattr(self._backend, "author", None), "model_id", "unknown")
        retrieval_backend = str(
            getattr(getattr(self._backend, "retrieval", None), "name", "unknown")
        )

        if detail is not None:
            self._store.append_audit(AuditRecord(
                iteration_id=iid,
                prompt_version_id=pvid,
                model=model,
                iteration_index=iter_idx,
                backend=self._backend.name,
                author_model=author_model,
                judge_model="opus-judge",
                champion_instruction=detail.champion_instruction_before,
                challenger_instruction=detail.challenger_instruction,
                prompt_diff=detail.prompt_diff,
                author_rationale=detail.author_rationale,
                driving_failures=(),
                challenger_triad=detail.challenger_score,
                challenger_ci_half_width=detail.challenger_ci_half_width,
                challenger_per_dimension=detail.challenger_per_dimension,
                accepted=detail.promoted,
                created_at=ts,
                island_id=state.island_id,
                rung_index=state.rung_index,
                tournament_round=tournament_round,
                turn_mode=getattr(self, "_turn_mode", "multi"),
            ))
            rec = IterationRecord(
                iteration_id=iid,
                model=model,
                phase="A",
                iteration_index=iter_idx,
                backend=self._backend.name,
                author_model=author_model,
                judge_model="opus-judge",
                champion_score=detail.champion_score,
                champion_ci_half_width=detail.champion_ci_half_width,
                challenger_score=detail.challenger_score,
                challenger_ci_half_width=detail.challenger_ci_half_width,
                significance_threshold=config.QUALITY_OPT_SIGNIFICANCE_THRESHOLD,
                promoted=detail.promoted,
                gain_absolute=detail.gain_absolute,
                gain_percent=detail.gain_percent,
                slice_n_conversations=detail.slice_n_conversations,
                between_conversation_sd=detail.between_conversation_sd,
                consecutive_non_improving=state.consecutive_non_improving,
                converged=False,
                stop_reason=None,
                mean_closeness=detail.mean_closeness,
                abstention_reward_mean=detail.abstention_reward_mean,
                answered_when_unsure_rate=detail.answered_when_unsure_rate,
                retrieval_backend=retrieval_backend,
                created_at=ts,
                island_id=state.island_id,
                rung_index=state.rung_index,
                tournament_round=tournament_round,
                turn_mode=getattr(self, "_turn_mode", "multi"),
            )
            self._store.append_iteration(rec)
            return

        # Fallback: position-only record (no step detail available).
        rec = IterationRecord(
            iteration_id=iid,
            model=model,
            phase="A",
            iteration_index=iter_idx,
            backend=self._backend.name,
            author_model=author_model,
            judge_model="opus-judge",
            champion_score=state.champion_score or 0.0,
            champion_ci_half_width=state.champion_ci_half_width or 0.0,
            challenger_score=None,
            challenger_ci_half_width=None,
            significance_threshold=config.QUALITY_OPT_SIGNIFICANCE_THRESHOLD,
            promoted=False,
            gain_absolute=None,
            gain_percent=None,
            slice_n_conversations=state.rung_n_conversations,
            between_conversation_sd=0.0,
            consecutive_non_improving=state.consecutive_non_improving,
            converged=False,
            stop_reason=None,
            mean_closeness=0.0,
            abstention_reward_mean=0.0,
            answered_when_unsure_rate=0.0,
            retrieval_backend=retrieval_backend,
            created_at=ts,
            island_id=state.island_id,
            rung_index=state.rung_index,
            tournament_round=tournament_round,
            turn_mode=getattr(self, "_turn_mode", "multi"),
        )
        self._store.append_iteration(rec)

    async def _run_phase_b(
        self, model: str, champion_instruction: str, validation_items: Sequence["Item"]
    ) -> Any:
        """Run PhaseBValidator on the validation complement."""
        validator = self._validator_for()
        if self._phase_b_reps is not None:
            return await validator.validate(
                model=model,
                champion_instruction=champion_instruction,
                validation_items=validation_items,
                reps=self._phase_b_reps,
            )
        return await validator.validate(
            model=model,
            champion_instruction=champion_instruction,
            validation_items=validation_items,
        )

    def _validator_for(self) -> Any:
        if self._validator is None:
            from bakeoff.quality.optimizer.validate import PhaseBValidator
            self._validator = PhaseBValidator(self._backend)
        return self._validator

    def _validation_items_for_split(self, model: str) -> Sequence["Item"]:
        """Resolve items for the phase_a_split. Accepts shared or per-model mapping."""
        items = self._validation_items
        if items is None:
            raise ValueError(
                f"PerModelOrchestrator needs items to run v2 for model {model!r}."
            )
        if isinstance(items, Mapping):
            return items[model]
        return items

    # -- v1 entry point (preserved for backward compat) ----------------------
    async def run(self) -> dict[str, Any]:
        """v1 entry point: preserved for backward compatibility."""
        decision = self.decide_concurrency()
        self.last_decision = decision
        if not self._models:
            return {}
        # v1 model_runner path
        if self._model_runner is not None:
            if decision.mode == "concurrent":
                results = await asyncio.gather(
                    *(self._model_runner(m) for m in self._models)
                )
                return dict(zip(self._models, results))
            out: dict[str, Any] = {}
            for m in self._models:
                out[m] = await self._model_runner(m)
            return out
        raise NotImplementedError("v1 controller path removed; use run_v2")
