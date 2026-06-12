"""
IterationController — the per-model champion/challenger Phase A loop of the closed-loop
prompt optimizer (design **Component 7: IterationController**, the "Champion/challenger
iteration" sequence, "Two-phase train/test", and "Error Handling: crash resume /
per-model state"; Req 1.1–1.10, 3.5, 6.6, 8.1, 8.6, 10.3, 11.4 and the Phase-A
tuning-slice scoping of Req 7.1, 7.2, 7.6, 7.7).

This is the orchestration spine that ties the already-built optimizer pieces together
into one model's iterate phase. :meth:`IterationController.run_phase_a` runs the loop the
design's sequence diagram describes, **sequentially within the model** (Req 1.10):

1. **Seed (iteration 0).** A permitted iteration-0 baseline Champion — an explicit
   ``seed_instruction`` or, by default, the ``full_stack`` variant from
   :func:`bakeoff.quality.prompts.variants_for_model` assembled by
   :func:`bakeoff.quality.prompts.quality_system_instruction` (Req 1.7/1.8/11.4). The seed
   is scored once on the Tuning_Slice and its baseline triad is persisted as the
   iteration-0 :class:`~bakeoff.quality.optimizer.store.AuditRecord` +
   :class:`~bakeoff.quality.optimizer.store.IterationRecord` (Req 8.6). The fixed
   five-variant menu is used **only** as this seed source, never as the iteration
   mechanism (Req 1.8/11.4).
2. **Iterate** until :attr:`ConvergenceTracker.should_stop`:
   * score the Champion on the Tuning_Slice with the retrieval-always
     :class:`~bakeoff.quality.optimizer.judge_loop.JudgeInLoopScorer` (Req 1.2/13);
   * select the lowest-scoring judged turns, answering-when-unsure first, via
     :func:`bakeoff.quality.optimizer.failures.select_failures` (Req 1.3/14.4);
   * hand the Champion text + those failures to the Author, streaming the author's
     reasoning tokens to the emitter through the author's ``stream=`` callback
     (Req 1.4/3.1/9.3);
   * score the Challenger on the same slice (Req 1.5) and promote it iff the gain is
     significant via :class:`~bakeoff.quality.optimizer.convergence.PromotionDecider`
     (Req 1.6); a non-usable (empty/identical) Challenger is a non-improving iteration,
     never promoted (Req 3.5);
   * persist the durable :class:`AuditRecord` + :class:`IterationRecord` (Req 8.1), emit
     the per-iteration events on this model's Model_Channel, and advance the
     :class:`~bakeoff.quality.optimizer.convergence.ConvergenceTracker` (Req 6).
3. On convergence, emit ``optimizer_converged`` (Req 6.6) and return a :class:`PhaseAResult`
   exposing the converged Champion instruction the orchestrator reads to drive Phase B.

**Two-phase train/test scoping (Req 7).** Phase A iterates on the held-out ~20%
Tuning_Slice **only**, so the Author only ever sees failures drawn from the Tuning_Slice
(Req 7.1/7.2). The Tuning_Slice is the ``heldout`` half of the deterministic, seeded
:func:`bakeoff.quality.dataset.split_items` (seed ``config.QUALITY_OPT_SPLIT_SEED``,
Req 7.6); :meth:`IterationController.for_phase_a` constructs a controller already scoped
to that slice while :meth:`IterationController.phase_a_split` exposes the same
``(tuning, validation)`` partition for the caller that drives Phase B (Req 7.3/7.7). The
controller never scores or selects failures from the Validation_Set.

**Resume / durability (Req 10.3, design "Error Handling").** The controller is
resume-aware: it reads the durable iteration store, skips iterations whose deterministic
``iteration_id`` is already present (``store.completed_iteration_ids(model)``), and
reconstructs the current Champion text and the convergence counter from the durable
records before resuming at the first incomplete iteration. All durable state is
partitioned by ``model`` so two concurrently-running models resume independently. The
durable :class:`AuditRecord` is appended **before** its :class:`IterationRecord` so that,
because the iteration store is the sole resume anchor, the Champion-text reconstruction a
resume depends on is always available for any iteration the resume considers complete.
An exception anywhere inside an iteration is written to the **disposable** errors store
(``store.append_error``) and re-raised — never the source-of-truth iteration/audit stores
— so the non-durable iteration is retried cleanly on the next invocation without
polluting the decision data.

Everything the loop needs from the outside world is the injected
:class:`~bakeoff.quality.optimizer.backends.OptimizerBackend` bundle (answer adapter
factory, judge, closeness, held-constant retrieval, Author); the ``store`` and ``emitter``
are injected too, and ``threshold`` / ``stop_limit`` / ``failures_k`` / ``reps`` default to
their ``config`` values but are injectable. This module performs no network I/O of its own
— it is pure orchestration over those seams, so it runs identically against the offline
bundle (zero network) or the live bundle.

Sourcing caveat (carried from requirements.md / design.md): the judge triad as the
decision signal, the abstention failure modes, the significance threshold's noise-floor
grounding, and the Author's modern Claude 4.5 prompting guidance are grounded in
external/industry RAG-evaluation practice, this repo's own observed Opus verdicts, and an
external/vendor prompt-engineering source — **not** in Amazon-internal primary sources;
re-validate any judge-derived number against internal guidance before using it to defend a
decision upward.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional, Sequence

from bakeoff import config
from bakeoff.quality.dataset import split_items
from bakeoff.quality.optimizer.convergence import ConvergenceTracker, PromotionDecider
from bakeoff.quality.optimizer.events import OptimizerEventEmitter
from bakeoff.quality.optimizer.failures import select_failures
from bakeoff.quality.optimizer.ids import iteration_id, prompt_version_id
from bakeoff.quality.optimizer.judge_loop import (
    JudgeInLoopScorer,
    SliceScore,
    TurnVerdict,
)
from bakeoff.quality.optimizer.stats import gain_report
from bakeoff.quality.optimizer.store import (
    AuditRecord,
    DrivingFailure,
    IterationRecord,
    OptimizerStore,
    make_prompt_diff,
)
from bakeoff.quality.prompts import quality_system_instruction, variants_for_model
from bakeoff.types import Item

if TYPE_CHECKING:  # typing only — no import cycle (backends.py never imports controller).
    from bakeoff.quality.optimizer.backends import OptimizerBackend

__all__ = [
    "PhaseAResult",
    "IterationController",
]

#: The optimizer phase this controller drives. Stamped on every record/id so the SoT and
#: the resume keys are partitioned from Phase B (Req 7, design "Two-phase train/test").
_PHASE: str = "A"

#: The variant id used as the default iteration-0 seed Champion (Req 1.7/1.8/11.4). The
#: ``full_stack`` variant turns on every multi-turn lever; it is the strongest permitted
#: starting point from the fixed menu, which is used ONLY as this seed source.
_SEED_VARIANT_ID: str = "full_stack"

#: How many trailing prompt versions to surface on each ``optimizer_iteration_completed``
#: event so the Quality_Tab can render the diff lookback (Req 8.5/9.5 — "at least two").
_LOOKBACK_VERSIONS: int = 3

#: The author rationale recorded on the seed's AuditRecord (the seed is not authored; it
#: is the baseline Champion the loop starts from — Req 8.6).
_SEED_RATIONALE: str = (
    "Iteration-0 baseline Champion seeded from the permitted fixed-menu variant "
    "(default 'full_stack') / explicit seed instruction. The fixed menu is used only as "
    "this seed source and never as the iteration mechanism for any later iteration "
    "(Req 1.7/1.8/11.4); its baseline Judge_Triad_Score is recorded for the version "
    "history (Req 8.6)."
)


def _now_iso() -> str:
    """Return the current UTC instant as a timezone-aware ISO-8601 string.

    Mirrors ``bakeoff.quality.optimizer.store._now_iso`` so every record this controller
    stamps uses the same ``created_at`` shape as the store layer writes elsewhere.
    """
    return datetime.now(timezone.utc).isoformat()


def _seed_instruction_for(model: str) -> str:
    """Assemble the default iteration-0 seed Champion instruction for ``model``.

    Resolves the ``full_stack`` variant from
    :func:`bakeoff.quality.prompts.variants_for_model` and composes the full standalone
    system instruction via :func:`bakeoff.quality.prompts.quality_system_instruction`,
    using the model's ``family`` / ``thinking`` from
    :data:`bakeoff.config.QUALITY_MODELS` (Req 1.7/1.8/11.4). Falls back to the model key as
    the family and ``thinking=False`` for any model not in ``QUALITY_MODELS`` so the seed
    never raises a ``KeyError`` on an unexpected key, and to the last variant in the ladder
    (also ``full_stack``) if the id is ever renamed.
    """
    spec = config.QUALITY_MODELS.get(model, {})
    family = str(spec.get("family", model))
    thinking = bool(spec.get("thinking", False))
    variants = variants_for_model(model)
    by_id = {v.variant_id: v for v in variants}
    variant = by_id.get(_SEED_VARIANT_ID) or variants[-1]
    return quality_system_instruction(
        family=family, thinking_enabled=thinking, variant=variant
    )


@dataclass(frozen=True)
class PhaseAResult:
    """The converged outcome of one model's Phase A loop (design Component 7).

    The orchestrator reads :attr:`champion_instruction` to drive Phase B; the remaining
    fields let the CLI/results writer reconstruct the ``quality_opt_results.json`` per-model
    block (``converged_iteration`` / ``stop_reason`` / ``champion_prompt_version_id`` /
    ``phase_a_final_triad`` / ``phase_a_ci_half_width``). ``phase_a_final_triad`` /
    ``phase_a_ci_half_width`` are the converged Champion's most recent Tuning_Slice triad +
    CI (the in-loop decision signal — never the final reported number, which is always the
    Phase B value, Req 7.5); they are ``None`` only in the degenerate case where the run
    resumed an already-converged model and executed no scoring.
    """

    model: str
    champion_instruction: str
    champion_prompt_version_id: str
    converged_iteration: Optional[int]
    stop_reason: Optional[str]
    phase_a_final_triad: Optional[float]
    phase_a_ci_half_width: Optional[float]
    backend: str


class IterationController:
    """Run one Target_Model's champion/challenger Phase A loop (design Component 7).

    Sequential within the model (Req 1.10): each iteration scores the current Champion,
    selects its worst judged turns, authors and scores a Challenger, promotes iff
    significant, persists the durable records, emits the per-iteration events on this
    model's Model_Channel, and advances the convergence counter — looping until the stop
    rule fires (Req 6). Resume-aware and per-model partitioned (Req 10.3): already-durable
    iterations are skipped and the Champion/convergence state is reconstructed from the
    durable records before resuming at the first incomplete iteration.

    The backend, store, and emitter are injected; ``threshold`` / ``stop_limit`` /
    ``failures_k`` / ``reps`` default to their ``config`` values but are injectable. The
    backend is consumed duck-typed through the :class:`JudgeInLoopScorer` (answer adapter
    factory, judge, closeness, retrieval) and the Author seam, so the controller never has
    to hard-import the backend bundle and works identically offline or live.
    """

    def __init__(
        self,
        *,
        model: str,
        backend: "OptimizerBackend",
        tuning_items: Sequence[Item],
        store: OptimizerStore,
        emitter: OptimizerEventEmitter,
        threshold: float = config.QUALITY_OPT_SIGNIFICANCE_THRESHOLD,
        stop_limit: int = config.QUALITY_OPT_STOP_LIMIT,
        failures_k: int = config.QUALITY_OPT_FAILURES_K,
        reps: int = config.QUALITY_OPT_PHASE_A_REPS,
        seed_instruction: Optional[str] = None,
    ) -> None:
        """Wire the controller to one model, its Tuning_Slice, and its collaborators.

        Args:
            model: the Target_Model whose prompt this loop optimizes (Req 1.9).
            backend: the injected :class:`OptimizerBackend` bundle; its ``name`` and the
                Author/Judge/retrieval identities are recorded on every record (Req 10.6,
                4.3, 16).
            tuning_items: the held-out Tuning_Slice this loop iterates on **only**
                (Req 7.1). Use :meth:`for_phase_a` to derive this from the full item set via
                the deterministic seeded split, or pass the ``heldout`` slice directly.
            store: the durable :class:`OptimizerStore` (SoT iterations + audit + disposable
                errors); the resume anchor and the audit/version history.
            emitter: the per-Model_Channel :class:`OptimizerEventEmitter` the loop streams
                iteration events through (Req 9).
            threshold: the minimum absolute triad gain that counts as significant
                (Req 1.6/5.1); defaults to ``config.QUALITY_OPT_SIGNIFICANCE_THRESHOLD``.
            stop_limit: consecutive non-improving iterations before convergence
                (Req 6.4/6.5); defaults to ``config.QUALITY_OPT_STOP_LIMIT``.
            failures_k: how many worst judged turns are handed to the Author each iteration
                (Req 3.4); defaults to ``config.QUALITY_OPT_FAILURES_K``.
            reps: repetitions per item when scoring the Tuning_Slice (Req 5.8); defaults to
                ``config.QUALITY_OPT_PHASE_A_REPS``.
            seed_instruction: explicit iteration-0 seed Champion; when ``None`` defaults to
                the ``full_stack`` variant (Req 1.7/1.8/11.4).
        """
        self._model = model
        self._backend = backend
        self._tuning_items: List[Item] = list(tuning_items)
        self._store = store
        self._emitter = emitter
        self._threshold = float(threshold)
        self._stop_limit = int(stop_limit)
        self._failures_k = int(failures_k)
        self._reps = int(reps)
        self._seed_instruction = (
            seed_instruction if seed_instruction is not None else _seed_instruction_for(model)
        )

        # Decision metric + predicate + tracker construction. The scorer is built once and
        # reused for every champion/challenger scoring on this model's slice.
        self._scorer = JudgeInLoopScorer(backend, reps=self._reps)
        self._decider = PromotionDecider()

        # Identities recorded on every record (Req 4.3/10.6/16). Read duck-typed so the
        # controller never hard-depends on the backend bundle's concrete type.
        self._backend_name = str(getattr(backend, "name", "unknown"))
        self._author_model = str(getattr(getattr(backend, "author", None), "author_model", "unknown"))
        self._judge_model = str(
            getattr(getattr(backend, "judge_scorer", None), "judge_model", config.JUDGE_MODEL_ID)
        )
        self._retrieval_backend_name = str(
            getattr(getattr(backend, "retrieval", None), "name", "unknown")
        )

    # -- construction helpers for the deterministic Phase-A tuning slice (Req 7) ---------
    @staticmethod
    def phase_a_split(all_items: Sequence[Item]) -> tuple[list[Item], list[Item]]:
        """Return the deterministic ``(tuning_slice, validation_set)`` partition (Req 7.6).

        Reuses :func:`bakeoff.quality.dataset.split_items` seeded by
        ``config.QUALITY_OPT_SPLIT_SEED`` so the held-out ~20% Tuning_Slice (Phase A) and
        the reserved ~80% Validation_Set (Phase B) are reproducible across runs and
        identical to the split any other caller derives with the same seed. Phase A iterates
        on ``tuning_slice`` only and the Author only ever sees failures drawn from it
        (Req 7.1/7.2); the caller hands ``validation_set`` to Phase B (Req 7.3/7.7).
        """
        return split_items(all_items, seed=config.QUALITY_OPT_SPLIT_SEED)

    @classmethod
    def for_phase_a(
        cls,
        *,
        model: str,
        backend: "OptimizerBackend",
        all_items: Sequence[Item],
        store: OptimizerStore,
        emitter: OptimizerEventEmitter,
        threshold: float = config.QUALITY_OPT_SIGNIFICANCE_THRESHOLD,
        stop_limit: int = config.QUALITY_OPT_STOP_LIMIT,
        failures_k: int = config.QUALITY_OPT_FAILURES_K,
        reps: int = config.QUALITY_OPT_PHASE_A_REPS,
        seed_instruction: Optional[str] = None,
    ) -> "IterationController":
        """Build a controller scoped to the held-out Tuning_Slice of ``all_items`` (Req 7).

        Convenience constructor that derives the Tuning_Slice from the full multi-turn item
        set via :meth:`phase_a_split` (the deterministic seeded
        :func:`bakeoff.quality.dataset.split_items`, seed ``config.QUALITY_OPT_SPLIT_SEED``)
        and constructs the controller over only that ``heldout`` slice (Req 7.1/7.6). The
        complementary Validation_Set is discarded here and never reaches this loop, so the
        Author can never receive a Validation_Set conversation (Req 7.2/7.7).
        """
        tuning_slice, _validation_set = cls.phase_a_split(all_items)
        return cls(
            model=model,
            backend=backend,
            tuning_items=tuning_slice,
            store=store,
            emitter=emitter,
            threshold=threshold,
            stop_limit=stop_limit,
            failures_k=failures_k,
            reps=reps,
            seed_instruction=seed_instruction,
        )

    # -- the entry point -----------------------------------------------------------------
    async def run_phase_a(self) -> PhaseAResult:
        """Run the resume-aware Phase A loop to convergence and return the converged Champion.

        Reconstructs the Champion text and convergence counter from the durable records
        (skipping any already-complete iteration, Req 10.3), seeds and persists the
        iteration-0 baseline if it is not yet durable (Req 8.6), then iterates the
        champion/challenger loop until :attr:`ConvergenceTracker.should_stop` (Req 6),
        emitting the per-iteration events on this model's Model_Channel (Req 9). On
        convergence it emits ``optimizer_converged`` (Req 6.6) and returns the
        :class:`PhaseAResult` whose ``champion_instruction`` the orchestrator scores in
        Phase B.
        """
        champion = self._seed_instruction
        champion_index = 0
        tracker = ConvergenceTracker(stop_limit=self._stop_limit)
        final_triad: Optional[float] = None
        final_ci: Optional[float] = None

        # Durable state for resume (read once; partitioned by model — Req 10.3 / design
        # "Per-model partitioned state under concurrency").
        completed = self._store.completed_iteration_ids(self._model)
        iters_by_index = {
            r.iteration_index: r for r in self._store.iteration_history(self._model)
        }
        audits_by_index = {
            a.iteration_index: a for a in self._store.read_audits() if a.model == self._model
        }

        i = 0
        while True:
            durable = iteration_id(self._model, _PHASE, i) in completed

            # Iteration 0 is the seed baseline: no Author, no promotion decision (Req 8.6).
            if i == 0:
                if durable:
                    rec = iters_by_index.get(0)
                    if rec is not None:
                        final_triad, final_ci = rec.champion_score, rec.champion_ci_half_width
                else:
                    final_triad, final_ci = await self._run_seed(champion)
                i += 1
                continue

            # Stop exactly at the first iteration the trailing reject run reaches the limit
            # (the converging iteration has already been recorded), Req 6.3 / design Property 9.
            if tracker.should_stop:
                break

            if durable:
                # Replay an already-complete iteration from its durable record instead of
                # re-executing it (Req 10.3): advance the Champion/convergence state exactly
                # as the original run did. The Champion text comes from the AuditRecord,
                # which is guaranteed durable because it is appended before the IterationRecord.
                rec = iters_by_index.get(i)
                if rec is not None:
                    if rec.promoted:
                        audit = audits_by_index.get(i)
                        if audit is not None and audit.challenger_instruction:
                            champion = audit.challenger_instruction
                            champion_index = i
                        final_triad, final_ci = rec.challenger_score, rec.challenger_ci_half_width
                    else:
                        final_triad, final_ci = rec.champion_score, rec.champion_ci_half_width
                    tracker.record(promoted=rec.promoted, iteration_index=i)
                i += 1
                continue

            # Fresh execution of iteration i.
            champion, champion_index, final_triad, final_ci = await self._run_iteration(
                i=i,
                champion_instruction=champion,
                champion_index=champion_index,
                tracker=tracker,
            )
            i += 1

        # Converged (Req 6.6): emit the convergence event once for this model's view.
        if tracker.converged_iteration is not None:
            self._emitter.converged(
                model=self._model,
                converged_iteration=tracker.converged_iteration,
                stop_reason=tracker.stop_reason or "",
            )

        return PhaseAResult(
            model=self._model,
            champion_instruction=champion,
            champion_prompt_version_id=prompt_version_id(self._model, champion_index),
            converged_iteration=tracker.converged_iteration,
            stop_reason=tracker.stop_reason,
            phase_a_final_triad=final_triad,
            phase_a_ci_half_width=final_ci,
            backend=self._backend_name,
        )

    # -- iteration 0: seed baseline ------------------------------------------------------
    async def _run_seed(self, seed_instruction: str) -> tuple[float, float]:
        """Score the seed Champion and persist its iteration-0 baseline records (Req 8.6).

        Scores the seed on the Tuning_Slice (emitting ``optimizer_champion_scored`` with
        ``role="champion"``, Req 1.2), then persists the baseline AuditRecord +
        IterationRecord (audit first so a resume that sees the iteration durable can always
        reconstruct the seed Champion text). Returns the seed's ``(triad, ci_half_width)``.
        Any failure is recorded to the disposable errors store and re-raised (design "Error
        Handling") so the non-durable seed is retried on resume.
        """
        try:
            score = await self._score(seed_instruction, role="champion", iteration_index=0)
            ts = _now_iso()
            iid = iteration_id(self._model, _PHASE, 0)
            audit = AuditRecord(
                iteration_id=iid,
                prompt_version_id=prompt_version_id(self._model, 0),
                model=self._model,
                iteration_index=0,
                backend=self._backend_name,
                author_model=self._author_model,
                judge_model=self._judge_model,
                champion_instruction=seed_instruction,
                challenger_instruction=None,
                prompt_diff="",
                author_rationale=_SEED_RATIONALE,
                driving_failures=(),
                challenger_triad=None,
                challenger_ci_half_width=None,
                challenger_per_dimension={},
                accepted=False,
                created_at=ts,
            )
            iteration = IterationRecord(
                iteration_id=iid,
                model=self._model,
                phase=_PHASE,
                iteration_index=0,
                backend=self._backend_name,
                author_model=self._author_model,
                judge_model=self._judge_model,
                champion_score=score.triad_score,
                champion_ci_half_width=score.ci_half_width,
                challenger_score=None,
                challenger_ci_half_width=None,
                significance_threshold=self._threshold,
                promoted=False,
                gain_absolute=None,
                gain_percent=None,
                slice_n_conversations=score.n_conversations,
                between_conversation_sd=score.between_conv_sd,
                consecutive_non_improving=0,
                converged=False,
                stop_reason=None,
                mean_closeness=score.mean_closeness,
                abstention_reward_mean=score.abstention_reward_mean,
                answered_when_unsure_rate=score.answered_when_unsure_rate,
                retrieval_backend=self._retrieval_backend_name,
                created_at=ts,
            )
            # Audit first (reconstruction depends on it), then the SoT iteration anchor.
            self._store.append_audit(audit)
            self._store.append_iteration(iteration)
            return score.triad_score, score.ci_half_width
        except Exception as exc:  # noqa: BLE001 - record to disposable store, then re-raise
            self._record_error(iteration_index=0, stage="seed", exc=exc)
            raise

    # -- iterations 1..N: champion/challenger --------------------------------------------
    async def _run_iteration(
        self,
        *,
        i: int,
        champion_instruction: str,
        champion_index: int,
        tracker: ConvergenceTracker,
    ) -> tuple[str, int, float, float]:
        """Run one champion/challenger iteration; return ``(champion, index, triad, ci)``.

        Implements the design's per-iteration sequence: score the Champion (Req 1.2) →
        select failures answering-when-unsure first (Req 1.3/14.4) → author the Challenger
        streaming author tokens to the emitter (Req 1.4/3.1/9.3) → score the Challenger
        (Req 1.5) → promote iff significant, with a non-usable Challenger never promoted
        (Req 1.6/3.5) → advance the tracker (Req 6) → persist the durable AuditRecord +
        IterationRecord (Req 8.1) → emit ``optimizer_iteration_completed`` (Req 9.6). Returns
        the post-iteration Champion text and index plus that Champion's triad + CI for the
        result. Any failure is recorded to the disposable errors store and re-raised so the
        non-durable iteration is retried on resume (design "Error Handling").
        """
        try:
            # 1) Score the current Champion on the Tuning_Slice (Req 1.2) — also yields the
            #    per-turn verdicts failure selection needs.
            champ_score = await self._score(
                champion_instruction, role="champion", iteration_index=i
            )

            # 2) Select the worst judged turns, answering-when-unsure first (Req 1.3/14.4).
            failures = select_failures(champ_score, k=self._failures_k)

            # 3) Author the Challenger, streaming the author's reasoning to the emitter
            #    (Req 1.4/3.1/9.3). The Author only ever sees Tuning_Slice failures (Req 7.2).
            authored = await self._author(i, champion_instruction, failures)
            usable = bool(authored.usable)

            # 4) Score the Challenger on the same slice (Req 1.5) — only when usable.
            chall_score: Optional[SliceScore] = None
            if usable:
                chall_score = await self._score(
                    authored.instruction, role="challenger", iteration_index=i
                )

            # 5) Promote iff significant; a non-usable Challenger is never promoted (Req 3.5).
            challenger_triad = chall_score.triad_score if chall_score is not None else champ_score.triad_score
            promoted = self._decider.decide(
                champ_score.triad_score, challenger_triad, self._threshold, usable=usable
            )

            # 6) Advance the convergence tracker BEFORE persisting so the IterationRecord
            #    captures the post-record convergence state (Req 6.1/6.2/6.3).
            tracker.record(promoted=promoted, iteration_index=i)

            # Gain reported both ways (Req 5.4); only meaningful for a usable Challenger.
            if usable and chall_score is not None:
                gains = gain_report(champ_score.triad_score, chall_score.triad_score)
                gain_absolute: Optional[float] = gains["absolute_delta"]
                gain_percent: Optional[float] = gains["percent_delta"]
            else:
                gain_absolute = None
                gain_percent = None

            # Resolve the post-iteration Champion + its score for the result/version id.
            if promoted:
                new_champion = authored.instruction
                new_index = i
                champ_final_triad = chall_score.triad_score  # type: ignore[union-attr]
                champ_final_ci = chall_score.ci_half_width  # type: ignore[union-attr]
            else:
                new_champion = champion_instruction
                new_index = champion_index
                champ_final_triad = champ_score.triad_score
                champ_final_ci = champ_score.ci_half_width

            # The proposed diff (challenger vs the champion this iteration started from),
            # used for both the audit record and the streamed iteration event (Req 8.1).
            proposed_diff = make_prompt_diff(champion_instruction, authored.instruction)

            ts = _now_iso()
            iid = iteration_id(self._model, _PHASE, i)
            audit = AuditRecord(
                iteration_id=iid,
                prompt_version_id=prompt_version_id(self._model, i),
                model=self._model,
                iteration_index=i,
                backend=self._backend_name,
                author_model=self._author_model,
                judge_model=self._judge_model,
                champion_instruction=champion_instruction,
                challenger_instruction=authored.instruction,
                prompt_diff=proposed_diff,
                author_rationale=authored.rationale,
                driving_failures=self._driving_failures(failures),
                challenger_triad=(chall_score.triad_score if chall_score is not None else None),
                challenger_ci_half_width=(
                    chall_score.ci_half_width if chall_score is not None else None
                ),
                challenger_per_dimension=(
                    dict(chall_score.per_dimension_mean) if chall_score is not None else {}
                ),
                accepted=promoted,
                created_at=ts,
            )
            iteration = IterationRecord(
                iteration_id=iid,
                model=self._model,
                phase=_PHASE,
                iteration_index=i,
                backend=self._backend_name,
                author_model=self._author_model,
                judge_model=self._judge_model,
                champion_score=champ_score.triad_score,
                champion_ci_half_width=champ_score.ci_half_width,
                challenger_score=(chall_score.triad_score if chall_score is not None else None),
                challenger_ci_half_width=(
                    chall_score.ci_half_width if chall_score is not None else None
                ),
                significance_threshold=self._threshold,
                promoted=promoted,
                gain_absolute=gain_absolute,
                gain_percent=gain_percent,
                slice_n_conversations=champ_score.n_conversations,
                between_conversation_sd=champ_score.between_conv_sd,
                consecutive_non_improving=tracker.consecutive_non_improving,
                converged=tracker.should_stop,
                stop_reason=tracker.stop_reason,
                mean_closeness=champ_score.mean_closeness,
                abstention_reward_mean=champ_score.abstention_reward_mean,
                answered_when_unsure_rate=champ_score.answered_when_unsure_rate,
                retrieval_backend=self._retrieval_backend_name,
                created_at=ts,
            )
            # Audit first (so a resume that sees the iteration complete can always
            # reconstruct a promoted Champion's text), then the SoT iteration anchor.
            self._store.append_audit(audit)
            self._store.append_iteration(iteration)

            # 7) Emit the iteration result on this model's Model_Channel (Req 9.6). Lookback
            #    is read after the audit append so the just-written version is included.
            lookback_ids = [
                pv.prompt_version_id
                for pv in self._store.lookback(self._model, _LOOKBACK_VERSIONS)
            ]
            self._emitter.iteration_completed(
                model=self._model,
                iteration_index=i,
                challenger_triad=(chall_score.triad_score if chall_score is not None else None),
                challenger_ci_half_width=(
                    chall_score.ci_half_width if chall_score is not None else None
                ),
                gain_absolute=gain_absolute,
                gain_percent=gain_percent,
                accepted=promoted,
                consecutive_non_improving=tracker.consecutive_non_improving,
                champion_instruction=new_champion,
                prompt_diff=proposed_diff,
                lookback_version_ids=lookback_ids,
            )

            return new_champion, new_index, champ_final_triad, champ_final_ci
        except Exception as exc:  # noqa: BLE001 - record to disposable store, then re-raise
            self._record_error(iteration_index=i, stage="iteration", exc=exc)
            raise

    # -- small collaborators -------------------------------------------------------------
    async def _score(self, instruction: str, *, role: str, iteration_index: int) -> SliceScore:
        """Score ``instruction`` on the Tuning_Slice and emit ``optimizer_champion_scored``.

        The retrieval-always :class:`JudgeInLoopScorer` scores every turn of every
        Tuning_Slice conversation (Req 1.2/1.5/13), then the scored slice — triad + CI +
        per-dimension breakdown + abstention summary + secondary closeness — is streamed to
        this model's Per_Model_View with ``role`` labelling whether it is the Champion or the
        Challenger (Req 9.2, design "Per-iteration SSE event shape").
        """
        score = await self._scorer.score_prompt(
            model=self._model,
            instruction=instruction,
            items=self._tuning_items,
            prompt_role=role,
        )
        self._emitter.champion_scored(
            model=self._model,
            phase=_PHASE,
            iteration_index=iteration_index,
            role=role,
            triad=score.triad_score,
            ci_half_width=score.ci_half_width,
            ci_low=score.ci_low,
            ci_high=score.ci_high,
            per_dimension=score.per_dimension_mean,
            abstention_reward_mean=score.abstention_reward_mean,
            answered_when_unsure_rate=score.answered_when_unsure_rate,
            retrieval_backend=self._retrieval_backend_name,
            mean_closeness=score.mean_closeness,
            n_conversations=score.n_conversations,
        )
        return score

    async def _author(self, i: int, champion_instruction: str, failures: Sequence[TurnVerdict]):
        """Invoke the Author, streaming its reasoning tokens to the emitter (Req 9.3).

        Hands the current Champion text and the selected Tuning_Slice failures to the
        injected Author (a separate model from the Judge, Req 4) via its ``author(...)``
        contract, forwarding each streamed reasoning chunk to
        ``optimizer_author_token`` on this model's Model_Channel so the Quality_Tab renders
        the Challenger being authored live.
        """

        def _stream(delta: str) -> None:
            self._emitter.author_token(model=self._model, iteration_index=i, delta=delta)

        return await self._backend.author.author(
            target_model=self._model,
            champion_instruction=champion_instruction,
            failures=failures,
            stream=_stream,
        )

    def _driving_failures(self, failures: Sequence[TurnVerdict]) -> tuple[DrivingFailure, ...]:
        """Project the selected :class:`TurnVerdict`\\ s into audit :class:`DrivingFailure`\\ s.

        Carries each failing turn's judge scores, abstention/grounding signals, the ids of
        the same fragments the model and judge saw (Req 13.7), the judge's quoted evidence,
        and the failing-answer excerpt into the audit record (Req 8.1), preserving the
        answering-when-unsure-first order that :func:`select_failures` produced.
        """
        return tuple(
            DrivingFailure(
                item_id=f.item_id,
                rep=f.rep,
                turn=f.turn,
                overall=f.overall,
                dimensions=dict(f.dimensions),
                abstention_correct=f.abstention_correct,
                answered_when_unsure=f.answered_when_unsure,
                fragments_sufficient=f.fragments_sufficient,
                grounding_fragment_ids=tuple(f.grounding_fragment_ids),
                evidence=dict(f.evidence),
                answer_excerpt=f.answer_excerpt,
            )
            for f in failures
        )

    def _record_error(self, *, iteration_index: int, stage: str, exc: Exception) -> None:
        """Append a failed attempt to the **disposable** errors store (design "Error Handling").

        A scoring/generation/authoring failure is written here — never the SoT iteration or
        audit stores — so the non-durable iteration can be retried on resume without
        polluting the decision data. The payload identifies the model, phase, iteration, the
        deterministic ``iteration_id`` (the resume key), the stage that failed, and the error
        text; it is stamped with the backend identity and a timestamp for triage.
        """
        self._store.append_error(
            {
                "model": self._model,
                "phase": _PHASE,
                "iteration_index": iteration_index,
                "iteration_id": iteration_id(self._model, _PHASE, iteration_index),
                "stage": stage,
                "error": f"{type(exc).__name__}: {exc}",
                "backend": self._backend_name,
                "created_at": _now_iso(),
            }
        )
