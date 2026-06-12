"""
CLI orchestrator for the closed-loop prompt optimizer (design **Architecture: CLI**,
**Component 10 (PerModelOrchestrator)**, **Migration / Fresh Start**, and **Error
Handling: quota guard**; Req 1.9, 7.3, 7.4, 7.5, 10.4, 10.5, 10.6, 10.7, 4.2, 11.1,
11.2, 11.3, 11.4, 13.2, 16.1).

This module is the operator entry point for the Optimizer. It wires the already-built
optimizer pieces (the offline/live :class:`~bakeoff.quality.optimizer.backends.OptimizerBackend`
bundle, the per-model :class:`~bakeoff.quality.optimizer.controller.IterationController`,
the :class:`~bakeoff.quality.optimizer.validate.PhaseBValidator`, the
:class:`~bakeoff.quality.optimizer.orchestrator.PerModelOrchestrator`, the durable
:class:`~bakeoff.quality.optimizer.store.OptimizerStore`, and the
:class:`~bakeoff.quality.optimizer.events.OptimizerEventEmitter`) into one command and
chooses the **backend** explicitly, mirroring the existing
:mod:`bakeoff.quality.main` argparse structure exactly.

Backend selection (mirrors ``bakeoff.quality.main``)
----------------------------------------------------
* ``--backend offline`` (default) — the deterministic, zero-network bundle from
  :func:`bakeoff.quality.optimizer.backends.build_offline_backend`
  (:class:`~bakeoff.quality.offline_adapter.QualityOfflineAdapter` factory +
  :class:`~bakeoff.scoring.judge.StubJudge`-backed judge + fake-embed closeness +
  network-free :class:`~bakeoff.quality.optimizer.retrieval.FakeRetrievalBackend` +
  :class:`~bakeoff.quality.optimizer.author.OfflineAuthorClient`). Runs anywhere,
  instantly, and CANNOT touch the bake-off run or any real Bedrock quota (Req 10.4).
* ``--backend live`` — the real study from
  :func:`bakeoff.quality.optimizer.backends.build_live_backend` (persistent-session inline
  adapter + real Opus judge + Embed v4 closeness + OpenSearch-preferred / local-fallback
  retrieval + Bedrock Sonnet-4.6 author). This makes real Bedrock / AWS calls and shares
  the Opus judge quota with the bake-off, so it is refused while a bake-off run looks
  active unless ``--force`` is given (Req 10.7), and it refuses to start at all when the
  Author and Judge resolve to the same model (Req 4.2).

The retrieval substrate for the live backend is selected by ``--retrieval-backend``
(``opensearch`` preferred | ``local`` fallback | ``fake``; default
``config.QUALITY_OPT_RETRIEVAL_BACKEND``); the offline backend always wires the
network-free fake (Req 16.1).

Subcommands (each independently runnable so a long live run is resumable)
-------------------------------------------------------------------------
* ``iterate`` — **Phase A** only. Runs each Target_Model's champion/challenger loop to
  convergence on the held-out ~20% Tuning_Slice, driven through the
  :class:`PerModelOrchestrator` so the visualization-gated concurrency decision (Req 1.11)
  and the strictly-sequential-within-a-model rule (Req 1.10) are honored. Persists the
  per-iteration audit / SoT records and prints the converged Champion per model. Writes no
  ``quality_opt_results.json`` (that file is the converged-champion + **Phase B** artifact).
* ``validate`` — **Phase B** only. Reconstructs each model's converged Champion from the
  durable stores and scores it **once** on the reserved ~80% Validation_Set at the higher
  Phase B rep count via :class:`~bakeoff.quality.optimizer.validate.PhaseBValidator`
  (Req 7.3/7.4), then writes ``quality_opt_results.json`` — the final reported number is
  always the Phase B value (Req 7.5).
* ``all`` — ``iterate`` then ``validate`` in one invocation: each model converges in
  Phase A and is immediately validated in Phase B, then ``quality_opt_results.json`` is
  written from the in-memory Phase A + Phase B results.
* ``reset`` — the one-time **Migration / Fresh Start** step (design "Migration / Fresh
  Start", Req 11): empties the old one-shot artifacts so the closed-loop Optimizer starts
  clean. The Optimizer reads none of those artifacts for any decision (Req 11.2) and runs
  correctly from empty stores (Req 11.3). The fixed five-variant menu
  (``MULTI_TURN_BLOCKS`` / ``variants_for_model`` in ``prompts.py``) is left intact and
  remains only the iteration-0 seed source (Req 11.4); this command never touches it.

Default model set
-----------------
The Target_Models default to exactly the two fixed quality models
(``config.QUALITY_MODELS`` — ``sonnet-4.6-thinking-off`` and ``haiku-4.5``), per the owner
decision and Req 12.3; ``--models`` overrides them.

Sourcing caveat (carried from requirements.md / design.md): the judge triad as the
decision signal, the abstention failure modes, the significance threshold's noise-floor
grounding, the modern Claude 4.5 prompting guidance, and the ALPHA OpenSearch endpoint
specifics are grounded in external/industry RAG-evaluation practice, this repo's own
observed Opus verdicts, AWS public API docs, an external/vendor prompting source, and
owner-provided operational facts — **not** Amazon-internal primary sources; re-validate any
judge-derived number against internal guidance before using it to defend a decision upward.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from typing import Optional, Sequence

from bakeoff import config
from bakeoff.quality.dataset import load_multi_turn_items
from bakeoff.quality.main import _bakeoff_run_looks_active
from bakeoff.quality.optimizer.backends import (
    AuthorJudgeConflictError,
    OptimizerBackend,
    build_live_backend,
    build_offline_backend,
)
from bakeoff.quality.optimizer.controller import IterationController, PhaseAResult
from bakeoff.quality.optimizer.events import OptimizerEventEmitter
from bakeoff.quality.optimizer.ids import prompt_version_id
from bakeoff.quality.optimizer.orchestrator import PerModelOrchestrator, ViewRegistry
from bakeoff.quality.optimizer.store import OptimizerStore
from bakeoff.quality.optimizer.validate import PhaseBResult, PhaseBValidator

__all__ = ["main"]


# ---------------------------------------------------------------------------
# A no-op SSE broker for CLI runs (no live Per_Model_View attached).
# ---------------------------------------------------------------------------
class _NullBroker:
    """A do-nothing broker for CLI runs where no live ``Per_Model_View`` is attached.

    The :class:`~bakeoff.quality.optimizer.events.OptimizerEventEmitter` only needs a
    duck-typed object exposing a synchronous ``publish(event_type, payload)`` method (the
    contract of :class:`bakeoff.app.SSEBroker`). When the Optimizer is driven from the CLI
    there is no SSE subscriber connected, so events have nowhere to go; this broker accepts
    and discards every publish. Using it (rather than the real :class:`bakeoff.app.SSEBroker`)
    keeps the CLI import-light — it does not pull in FastAPI/Starlette — and the live
    dashboard path still uses the real broker through the additive API routes.
    """

    def publish(self, event_type: str, payload: dict) -> None:  # noqa: D401 - trivial sink
        """Discard the event (no subscribers on the CLI path)."""
        return None


def _now_iso() -> str:
    """Return the current UTC instant as a timezone-aware ISO-8601 string.

    Mirrors the ``created_at`` shape every optimizer store record uses so the
    ``generated_at`` stamp on ``quality_opt_results.json`` is consistent with the rest of
    the harness.
    """
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Backend factory selection (offline | live) — mirrors bakeoff.quality.main.
# ---------------------------------------------------------------------------
def _build_backend(backend: str, *, retrieval_backend: str) -> OptimizerBackend:
    """Build the injectable :class:`OptimizerBackend` bundle for ``backend``.

    * ``"offline"`` → :func:`~bakeoff.quality.optimizer.backends.build_offline_backend`: the
      deterministic, zero-network bundle (Req 10.4). The retrieval substrate is always the
      network-free fake here, so ``retrieval_backend`` is not consulted.
    * ``"live"`` → :func:`~bakeoff.quality.optimizer.backends.build_live_backend`: the real
      Bedrock-backed bundle (Req 10.5), with the held-constant retrieval substrate selected
      by ``retrieval_backend`` (OpenSearch preferred / local fallback / fake — Req 16.1).
      May raise :class:`AuthorJudgeConflictError` when the Author and Judge resolve to the
      same model (Req 4.2); the caller surfaces that as a clean refusal-to-start.

    Args:
        backend: ``"offline"`` or ``"live"``.
        retrieval_backend: which retrieval substrate the live backend should prefer
            (``"opensearch"`` | ``"local"`` | ``"fake"``); ignored for the offline backend.

    Returns:
        The wired :class:`OptimizerBackend`.

    Raises:
        AuthorJudgeConflictError: from the live builder when Author == Judge (Req 4.2).
    """
    if backend == "offline":
        return build_offline_backend()
    return build_live_backend(retrieval_backend=retrieval_backend)


def _make_emitter() -> OptimizerEventEmitter:
    """Build the per-Model_Channel event emitter over a no-op broker for the CLI.

    The emitter stamps ``model_channel`` on every payload and publishes over the duck-typed
    broker; on the CLI there is no subscriber, so a :class:`_NullBroker` is used (the live
    dashboard path injects the real :class:`bakeoff.app.SSEBroker` instead).
    """
    return OptimizerEventEmitter(_NullBroker())


# ---------------------------------------------------------------------------
# Phase A summary helpers — build / reconstruct the per-model results block.
# ---------------------------------------------------------------------------
def _phase_a_block_from_result(result: PhaseAResult) -> dict:
    """Project a freshly-computed :class:`PhaseAResult` into its results-JSON block.

    Used by the ``iterate`` and ``all`` paths, which hold the in-memory
    :class:`PhaseAResult` returned by the controller, to assemble the per-model Phase A
    fields of ``quality_opt_results.json`` (design "PhaseB / results"): the converged
    iteration and stop reason (Req 6.6), the converged Champion's prompt-version id, and the
    in-loop Phase A triad + CI (never the final reported number, which is the Phase B value,
    Req 7.5).
    """
    return {
        "converged_iteration": result.converged_iteration,
        "stop_reason": result.stop_reason,
        "champion_prompt_version_id": result.champion_prompt_version_id,
        "phase_a_final_triad": result.phase_a_final_triad,
        "phase_a_ci_half_width": result.phase_a_ci_half_width,
    }


def _reconstruct_champion(store: OptimizerStore, model: str) -> Optional[dict]:
    """Reconstruct a model's converged Champion + Phase A summary from the durable stores.

    Used by the standalone ``validate`` path, which has no in-memory
    :class:`PhaseAResult` — it must read the converged Champion (the prompt to score in
    Phase B) back out of the append-only stores a prior ``iterate`` run persisted. This
    walks the ordered per-model prompt-version history exactly as the
    :class:`~bakeoff.quality.optimizer.controller.IterationController` does on resume: the
    Champion starts as the iteration-0 seed and is replaced by each accepted iteration's
    challenger instruction, so the final Champion is the last accepted challenger (or the
    seed if nothing was ever promoted). The convergence metadata (converged iteration / stop
    reason) and the in-loop Phase A triad + CI are read from the SoT
    :class:`~bakeoff.quality.optimizer.store.IterationRecord`\\ s.

    Args:
        store: the durable optimizer store to read from.
        model: the Target_Model whose converged Champion to reconstruct.

    Returns:
        A dict with ``champion_instruction`` plus the same Phase A block fields
        :func:`_phase_a_block_from_result` produces, or ``None`` if the model has no durable
        iterations yet (no ``iterate`` run has been recorded for it).
    """
    versions = store.prompt_version_history(model)
    if not versions:
        return None

    # The Champion starts at the seed (iteration 0) and advances to each accepted
    # challenger, in iteration order — identical to the controller's resume replay.
    champion_instruction = versions[0].champion_instruction
    champion_index = 0
    for pv in versions:
        if pv.iteration_index == 0:
            continue
        if pv.accepted and pv.challenger_instruction:
            champion_instruction = pv.challenger_instruction
            champion_index = pv.iteration_index

    # Convergence metadata + the in-loop Phase A triad/CI from the SoT iteration records.
    iterations = store.iteration_history(model)
    converged_iteration: Optional[int] = None
    stop_reason: Optional[str] = None
    for rec in iterations:
        if rec.converged:
            converged_iteration = rec.iteration_index
            stop_reason = rec.stop_reason

    phase_a_final_triad: Optional[float] = None
    phase_a_ci_half_width: Optional[float] = None
    if iterations:
        last = iterations[-1]
        if last.promoted and last.challenger_score is not None:
            phase_a_final_triad = last.challenger_score
            phase_a_ci_half_width = last.challenger_ci_half_width
        else:
            phase_a_final_triad = last.champion_score
            phase_a_ci_half_width = last.champion_ci_half_width

    return {
        "champion_instruction": champion_instruction,
        "converged_iteration": converged_iteration,
        "stop_reason": stop_reason,
        "champion_prompt_version_id": prompt_version_id(model, champion_index),
        "phase_a_final_triad": phase_a_final_triad,
        "phase_a_ci_half_width": phase_a_ci_half_width,
    }


def _phase_b_block(result: PhaseBResult) -> dict:
    """Project a :class:`PhaseBResult` into the ``phase_b`` sub-block of the results JSON.

    Matches the design's "PhaseB / results" shape: the final reported triad on the
    Validation_Set, its 95% CI (half-width + bounds), the conversation count, and the Phase
    B rep count (Req 7.4/7.5).
    """
    return {
        "triad": result.triad_score,
        "ci_half_width": result.ci_half_width,
        "ci_low": result.ci_low,
        "ci_high": result.ci_high,
        "n_conversations": result.n_conversations,
        "reps": result.reps,
    }


def _write_results(
    store: OptimizerStore,
    *,
    backend_name: str,
    phase_a_blocks: dict[str, dict],
    phase_b_results: dict[str, PhaseBResult],
) -> None:
    """Assemble and atomically write ``quality_opt_results.json`` (design "PhaseB / results").

    Builds the single-object results artifact — ``generated_at`` + ``backend`` + a per-model
    block carrying the converged-champion Phase A summary and the final reported Phase B
    triad/CI — and writes it through the store's atomic results writer
    (:meth:`OptimizerStore.write_results`). Every model present in ``phase_b_results`` is
    included; its Phase A block is taken from ``phase_a_blocks`` (empty if Phase A was not
    run in this invocation). The final reported performance is always the Phase B number on
    the Validation_Set (Req 7.5).
    """
    models_block: dict[str, dict] = {}
    for model, pb in phase_b_results.items():
        block = dict(phase_a_blocks.get(model, {}))
        block["phase_b"] = _phase_b_block(pb)
        models_block[model] = block

    store.write_results(
        {
            "generated_at": _now_iso(),
            "backend": backend_name,
            "models": models_block,
        }
    )


# ---------------------------------------------------------------------------
# Phase drivers
# ---------------------------------------------------------------------------
async def _do_iterate(
    *,
    backend: OptimizerBackend,
    models: Sequence[str],
    store: OptimizerStore,
    emitter: OptimizerEventEmitter,
    all_items: list,
    threshold: float,
    stop_limit: int,
    failures_k: int,
    phase_a_reps: int,
) -> dict[str, PhaseAResult]:
    """Run Phase A for every model through the orchestrator and return the converged results.

    Each model's champion/challenger loop is built with
    :meth:`IterationController.for_phase_a` (which scopes it to the held-out Tuning_Slice via
    the deterministic seeded split — Req 7.1/7.6) and run to convergence by its
    :meth:`~bakeoff.quality.optimizer.controller.IterationController.run_phase_a`. The
    per-model loops are driven through a :class:`PerModelOrchestrator` via its ``model_runner``
    seam so the visualization-gated concurrent-vs-sequential decision (Req 1.11) and the
    strictly-sequential-within-a-model rule (Req 1.10) are applied; on the CLI no
    ``Per_Model_View`` is active, so the gate resolves to sequential. Returns a mapping of
    model to its :class:`PhaseAResult` (the converged Champion + its Phase A summary).
    """
    phase_a_results: dict[str, PhaseAResult] = {}

    async def _run_phase_a(model: str) -> PhaseAResult:
        controller = IterationController.for_phase_a(
            model=model,
            backend=backend,
            all_items=all_items,
            store=store,
            emitter=emitter,
            threshold=threshold,
            stop_limit=stop_limit,
            failures_k=failures_k,
            reps=phase_a_reps,
        )
        result = await controller.run_phase_a()
        phase_a_results[model] = result
        return result

    orchestrator = PerModelOrchestrator(
        models=models,
        backend=backend,
        store=store,
        emitter=emitter,
        view_registry=ViewRegistry(),
        model_runner=_run_phase_a,
    )
    await orchestrator.run()
    return phase_a_results


async def _do_all(
    *,
    backend: OptimizerBackend,
    models: Sequence[str],
    store: OptimizerStore,
    emitter: OptimizerEventEmitter,
    all_items: list,
    threshold: float,
    stop_limit: int,
    failures_k: int,
    phase_a_reps: int,
    phase_b_reps: int,
) -> tuple[dict[str, PhaseAResult], dict[str, PhaseBResult]]:
    """Run Phase A → Phase B for every model through the orchestrator; return both results.

    Each model converges in Phase A (held-out Tuning_Slice) and is then validated **once**
    in Phase B on the reserved Validation_Set complement at the higher Phase B rep count
    (Req 7.3/7.4) via :class:`~bakeoff.quality.optimizer.validate.PhaseBValidator`. Both
    phases run inside the orchestrator's ``model_runner`` seam so the concurrency gate
    (Req 1.11) governs how the two per-model loops are scheduled while each model stays
    sequential (Req 1.10) and Phase A fully converges before that model's Phase B begins. The
    Validation_Set is the ``remainder`` of the same deterministic seeded split the controller
    uses for the Tuning_Slice, so the strict train/test boundary holds (Req 7.7). Returns the
    per-model Phase A and Phase B results for the results-JSON writer.
    """
    phase_a_results: dict[str, PhaseAResult] = {}
    phase_b_results: dict[str, PhaseBResult] = {}

    # The reserved ~80% Validation_Set complement (Phase B only; the Author never sees it).
    _tuning, validation_items = IterationController.phase_a_split(all_items)
    validator = PhaseBValidator(backend)

    async def _run_model(model: str) -> PhaseBResult:
        controller = IterationController.for_phase_a(
            model=model,
            backend=backend,
            all_items=all_items,
            store=store,
            emitter=emitter,
            threshold=threshold,
            stop_limit=stop_limit,
            failures_k=failures_k,
            reps=phase_a_reps,
        )
        phase_a = await controller.run_phase_a()
        phase_a_results[model] = phase_a

        phase_b = await validator.validate(
            model=model,
            champion_instruction=phase_a.champion_instruction,
            validation_items=validation_items,
            reps=phase_b_reps,
        )
        phase_b_results[model] = phase_b
        # The model_runner seam bypasses the orchestrator's own Phase B emit, so stream the
        # final validation number for this model's Per_Model_View here (design "PhaseB --> EMIT").
        emitter.phase_b(
            model=model,
            triad=phase_b.triad_score,
            ci_half_width=phase_b.ci_half_width,
            n_conversations=phase_b.n_conversations,
        )
        return phase_b

    orchestrator = PerModelOrchestrator(
        models=models,
        backend=backend,
        store=store,
        emitter=emitter,
        view_registry=ViewRegistry(),
        model_runner=_run_model,
    )
    await orchestrator.run()
    return phase_a_results, phase_b_results


async def _do_validate(
    *,
    backend: OptimizerBackend,
    models: Sequence[str],
    store: OptimizerStore,
    all_items: list,
    phase_b_reps: int,
) -> tuple[dict[str, dict], dict[str, PhaseBResult]]:
    """Run Phase B only for every model: reconstruct the Champion, then validate it.

    For each model the converged Champion is reconstructed from the durable stores a prior
    ``iterate`` run wrote (:func:`_reconstruct_champion`), then scored **once** on the
    reserved ~80% Validation_Set complement at the higher Phase B rep count via
    :class:`~bakeoff.quality.optimizer.validate.PhaseBValidator` (Req 7.3/7.4); the returned
    triad is always the final reported number (Req 7.5). A model with no durable iterations
    yet is skipped with a message rather than silently producing an empty result. Returns the
    per-model Phase A summary blocks (reconstructed) and Phase B results for the writer.
    """
    validator = PhaseBValidator(backend)
    _tuning, validation_items = IterationController.phase_a_split(all_items)

    phase_a_blocks: dict[str, dict] = {}
    phase_b_results: dict[str, PhaseBResult] = {}

    for model in models:
        reconstructed = _reconstruct_champion(store, model)
        if reconstructed is None:
            print(
                f"[validate] no durable Phase A iterations for {model!r} — run the "
                "'iterate' (or 'all') phase first. Skipping."
            )
            continue

        champion_instruction = reconstructed.pop("champion_instruction")
        phase_a_blocks[model] = reconstructed
        phase_b = await validator.validate(
            model=model,
            champion_instruction=champion_instruction,
            validation_items=validation_items,
            reps=phase_b_reps,
        )
        phase_b_results[model] = phase_b

    return phase_a_blocks, phase_b_results


# ---------------------------------------------------------------------------
# reset — Migration / Fresh Start (Req 11)
# ---------------------------------------------------------------------------
#: The old one-shot quality artifacts emptied by ``reset`` (design "Migration / Fresh
#: Start", Req 11). Resolved from ``config`` so the paths track the single source of truth.
#: The Optimizer reads none of these for any decision (Req 11.2) and runs correctly from
#: empty stores (Req 11.3). The five-variant menu in ``prompts.py`` is deliberately NOT in
#: this list — it is retained as the iteration-0 seed source only (Req 11.4).
_RESET_PATHS = (
    config.QUALITY_OUTCOMES_PATH,           # data/bakeoff/quality_outcomes.jsonl
    config.QUALITY_PROMPTS_PATH,            # data/bakeoff/quality_prompts.json
    config.QUALITY_OPTIMIZER_REPORT_PATH,   # data/bakeoff/quality_optimizer_report.json
    config.QUALITY_JUDGE_SCORES_PATH,       # data/bakeoff/quality_judge_scores.jsonl
    config.QUALITY_RUN_ERRORS_PATH,         # data/bakeoff/quality_run_errors.jsonl
)


def _do_reset() -> int:
    """Empty the old one-shot quality artifacts for a clean closed-loop start (Req 11).

    The one-time Migration / Fresh Start operator step (design "Migration / Fresh Start").
    Each path in :data:`_RESET_PATHS` is **emptied in place** — truncated to a zero-byte
    file, creating it empty if it is absent — rather than deleted, so the on-disk layout and
    the directory itself are preserved and the Optimizer (which reads none of these for any
    decision, Req 11.2) starts from empty stores (Req 11.3). Only those five specific files
    are touched; the directory is never removed, and the fixed five-variant menu in
    ``prompts.py`` is left intact as the iteration-0 seed source (Req 11.4).

    Returns:
        ``0`` on success.
    """
    config.ensure_dirs()
    print("[reset] emptying the old one-shot quality artifacts (fresh start, Req 11):")
    for path in _RESET_PATHS:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate-in-place (remove-and-recreate-empty semantics) — never delete the file
        # or the directory; the Optimizer runs correctly from empty stores (Req 11.3).
        with open(path, "w", encoding="utf-8"):
            pass
        print(f"  emptied {path}")
    print(
        "[reset] done. The closed-loop Optimizer reads none of these for any decision "
        "(Req 11.2); the new quality_opt_* stores are its source of truth, retrieval is "
        "always-on, and the five-variant menu remains only the iteration-0 seed source "
        "(Req 11.4)."
    )
    return 0


# ---------------------------------------------------------------------------
# Argument parsing + dispatch
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser, mirroring the :mod:`bakeoff.quality.main` structure.

    A single positional ``command`` (``iterate`` | ``validate`` | ``all`` | ``reset``) plus
    the shared backend/tuning flags, exactly like the existing quality CLI's ``phase``
    positional + ``--backend`` / ``--force`` flags. The ``reset`` command ignores the
    backend/tuning flags (it only empties the old artifacts). The Target_Models default to
    the two fixed ``config.QUALITY_MODELS`` (Req 12.3).
    """
    parser = argparse.ArgumentParser(
        prog="python -m bakeoff.quality.optimizer.main",
        description=(
            "Closed-loop prompt optimizer: iterate (Phase A), validate (Phase B), all, "
            "islands (v2 island-tournament), and reset (fresh start)."
        ),
    )
    parser.add_argument("command", choices=["iterate", "validate", "all", "islands", "reset"])
    parser.add_argument(
        "--backend",
        choices=["offline", "live"],
        default="offline",
        help="offline (deterministic, no network) or live (real Bedrock). Default offline.",
    )
    parser.add_argument(
        "--retrieval-backend",
        choices=["opensearch", "local", "fake"],
        default=config.QUALITY_OPT_RETRIEVAL_BACKEND,
        help=(
            "retrieval substrate for --backend live: opensearch (preferred) | local "
            "(fallback) | fake. Offline always uses the network-free fake. "
            f"Default {config.QUALITY_OPT_RETRIEVAL_BACKEND}."
        ),
    )
    parser.add_argument(
        "--models",
        nargs="+",
        default=list(config.QUALITY_MODELS),
        help="Target_Models to optimize (default: the two fixed config.QUALITY_MODELS).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=config.QUALITY_OPT_SIGNIFICANCE_THRESHOLD,
        help="significance threshold: minimum absolute triad gain to promote (Req 5.2).",
    )
    parser.add_argument(
        "--stop-limit",
        type=int,
        default=config.QUALITY_OPT_STOP_LIMIT,
        help="consecutive non-improving iterations before Phase A converges (Req 6.4).",
    )
    parser.add_argument(
        "--failures-k",
        type=int,
        default=config.QUALITY_OPT_FAILURES_K,
        help="number of worst judged turns handed to the Author each iteration (Req 3.4).",
    )
    parser.add_argument(
        "--phase-a-reps",
        type=int,
        default=config.QUALITY_OPT_PHASE_A_REPS,
        help="reps per item when scoring the Tuning_Slice in Phase A.",
    )
    parser.add_argument(
        "--phase-b-reps",
        type=int,
        default=config.QUALITY_OPT_PHASE_B_REPS,
        help="reps per item when validating on the Validation_Set in Phase B (Req 7.4).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow --backend live even if a bake-off run looks active (Req 10.7).",
    )
    return parser


def _print_phase_a_summary(phase_a_results: dict[str, PhaseAResult]) -> None:
    """Print a one-line-per-model summary of the converged Phase A Champions."""
    for model, pa in phase_a_results.items():
        triad = "n/a" if pa.phase_a_final_triad is None else f"{pa.phase_a_final_triad:.4f}"
        print(
            f"  {model:28s} converged_iteration={pa.converged_iteration} "
            f"phase_a_triad={triad} stop_reason={pa.stop_reason!r}"
        )


def _print_phase_b_summary(phase_b_results: dict[str, PhaseBResult]) -> None:
    """Print a one-line-per-model summary of the final reported Phase B numbers (Req 7.5)."""
    for model, pb in phase_b_results.items():
        print(
            f"  {model:28s} phase_b_triad={pb.triad_score:.4f} "
            f"ci_half_width={pb.ci_half_width:.4f} "
            f"n_conversations={pb.n_conversations} reps={pb.reps}"
        )


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point: parse args, apply the quota guard, build the backend, and dispatch.

    Mirrors :func:`bakeoff.quality.main.main`: calls :func:`config.ensure_dirs`, refuses
    ``--backend live`` while a bake-off run looks active unless ``--force`` is given
    (Req 10.7, reusing :func:`bakeoff.quality.main._bakeoff_run_looks_active`), and surfaces
    an :class:`~bakeoff.quality.optimizer.backends.AuthorJudgeConflictError` (Author == Judge,
    Req 4.2) as a clean refusal-to-start with a non-zero exit code rather than a traceback.

    Returns:
        ``0`` on success; ``2`` when the live quota guard or the Author/Judge separation
        guard refuses to start.
    """
    args = _build_parser().parse_args(argv)

    config.ensure_dirs()

    # reset is a pure local file operation — no backend, no quota guard (Req 11).
    if args.command == "reset":
        return _do_reset()

    # Quota guard (Req 10.7): refuse live while a bake-off run looks active unless forced,
    # so the Optimizer never silently contends with the bake-off for the shared Opus quota.
    if args.backend == "live" and _bakeoff_run_looks_active() and not args.force:
        print(
            "[guard] A bake-off run looks active (outcomes.jsonl written in the last "
            "2 min). Refusing --backend live so the optimizer does not contend for the "
            "shared Opus judge quota. Re-run with --force once the bake-off run is done."
        )
        return 2

    # Build the backend bundle. The live builder refuses to start when Author == Judge
    # (Req 4.2): surface that as a clean guard message + non-zero exit, never a traceback.
    try:
        backend = _build_backend(args.backend, retrieval_backend=args.retrieval_backend)
    except AuthorJudgeConflictError as exc:
        print(f"[guard] Refusing to start — Author/Judge conflict (Req 4.2): {exc}")
        return 2

    store = OptimizerStore()
    emitter = _make_emitter()
    all_items = load_multi_turn_items()

    if args.command == "iterate":
        phase_a_results = asyncio.run(
            _do_iterate(
                backend=backend,
                models=args.models,
                store=store,
                emitter=emitter,
                all_items=all_items,
                threshold=args.threshold,
                stop_limit=args.stop_limit,
                failures_k=args.failures_k,
                phase_a_reps=args.phase_a_reps,
            )
        )
        print(f"[iterate] backend={args.backend} models={list(args.models)}")
        _print_phase_a_summary(phase_a_results)
        print(
            "[iterate] Phase A complete. Run 'validate' (or 'all') to score the converged "
            "champions on the Validation_Set and write quality_opt_results.json."
        )
        return 0

    if args.command == "islands":
        orchestrator = PerModelOrchestrator(
            models=args.models,
            backend=backend,
            store=store,
            emitter=emitter,
            view_registry=ViewRegistry(),
        )
        results = asyncio.run(
            orchestrator.run_v2(
                args.models, backend, emitter=emitter, store=store, all_items=all_items,
            )
        )
        print(f"[islands] backend={args.backend} models={list(args.models)}")
        print(f"[islands] v2 island-tournament complete. {len(results)} model(s) finished.")
        return 0

    if args.command == "all":
        phase_a_results, phase_b_results = asyncio.run(
            _do_all(
                backend=backend,
                models=args.models,
                store=store,
                emitter=emitter,
                all_items=all_items,
                threshold=args.threshold,
                stop_limit=args.stop_limit,
                failures_k=args.failures_k,
                phase_a_reps=args.phase_a_reps,
                phase_b_reps=args.phase_b_reps,
            )
        )
        phase_a_blocks = {
            model: _phase_a_block_from_result(pa) for model, pa in phase_a_results.items()
        }
        _write_results(
            store,
            backend_name=backend.name,
            phase_a_blocks=phase_a_blocks,
            phase_b_results=phase_b_results,
        )
        print(f"[all] backend={args.backend} models={list(args.models)}")
        _print_phase_a_summary(phase_a_results)
        _print_phase_b_summary(phase_b_results)
        print(f"[all] results -> {store.results_path}")
        return 0

    # validate
    phase_a_blocks, phase_b_results = asyncio.run(
        _do_validate(
            backend=backend,
            models=args.models,
            store=store,
            all_items=all_items,
            phase_b_reps=args.phase_b_reps,
        )
    )
    if phase_b_results:
        _write_results(
            store,
            backend_name=backend.name,
            phase_a_blocks=phase_a_blocks,
            phase_b_results=phase_b_results,
        )
        print(f"[validate] backend={args.backend} models={list(phase_b_results)}")
        _print_phase_b_summary(phase_b_results)
        print(f"[validate] results -> {store.results_path}")
    else:
        print("[validate] nothing to validate — no converged champions found on disk.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
