"""
CLI orchestrator for the multi-turn quality study.

Wires the four phases into one command and chooses the BACKEND explicitly:

* ``--backend offline`` (default) — deterministic, zero-network: the
  :class:`bakeoff.quality.offline_adapter.QualityOfflineAdapter` + the offline
  embedder + the :class:`bakeoff.scoring.judge.StubJudge`. Runs anywhere, instantly,
  and CANNOT touch the bake-off run. Use it to validate the whole pipeline.
* ``--backend live`` — the real study: the real
  :class:`bakeoff.adapters.bedrock.BedrockModelAdapter` (Converse, streaming, with
  the optimizer-chosen ``instruction_override``), the real Embed v4 semantic
  scorer, and the real Opus judge. This makes real Bedrock calls and MUST only be
  run once the bake-off converse run has finished (it shares the Bedrock account /
  rate limits). The command refuses ``live`` while a bake-off run looks active
  unless ``--force`` is given.

Phases (each independently runnable so a long live run is resumable):

* ``optimize`` — rank prompt variants per model on the held-out slice; write the
  chosen prompt + the leaderboard report.
* ``run`` — generate the multi-turn quality outcomes through the chosen prompts.
* ``judge`` — Phase-2 per-turn judge over the outcomes; enrich closeness.
* ``all`` — optimize → run → judge in sequence.

Backend-agnostic factories live here; the phase modules stay pure.
"""
from __future__ import annotations

import argparse
import asyncio
from typing import Optional

from bakeoff import config
from bakeoff.quality import optimize as opt
from bakeoff.quality import run as qrun
from bakeoff.quality import judge as qjudge
from bakeoff.quality.closeness import TurnClosenessScorer
from bakeoff.quality.dataset import load_multi_turn_items, split_items
from bakeoff.quality.offline_adapter import QualityOfflineAdapter
from bakeoff.quality.prompts import quality_system_instruction, variants_for_model
from bakeoff.scoring.judge import JudgeScorer, make_stub_judge
from bakeoff.scoring.pipeline import _make_fake_embed_fn
from bakeoff.scoring.semantic import SemanticSimilarityScorer

__all__ = ["main", "build_offline_scorers", "build_live_scorers"]


# ---------------------------------------------------------------------------
# Backend wiring — the two factories + the closeness/judge scorers per backend
# ---------------------------------------------------------------------------
def build_offline_scorers(disk_cache: bool = False):
    """Return ``(closeness_scorer, judge_scorer)`` for the offline backend."""
    sem = SemanticSimilarityScorer(embed_fn=_make_fake_embed_fn(), disk_cache=disk_cache)
    closeness = TurnClosenessScorer(sem)
    judge = JudgeScorer(backend=make_stub_judge(), disk_cache=disk_cache)
    return closeness, judge


def build_live_scorers():
    """Return ``(closeness_scorer, judge_scorer)`` for the live (Bedrock) backend.

    Uses the real Embed v4 semantic scorer (its default resilient Bedrock embedder)
    and the real resilient Opus judge — both reuse the credential chain + region
    from :mod:`bakeoff.config`. Built lazily so importing this module needs no boto3.
    """
    sem = SemanticSimilarityScorer()  # real Embed v4 client (resilient)
    closeness = TurnClosenessScorer(sem)
    judge = JudgeScorer()  # real resilient Opus judge, k from config
    return closeness, judge


def _offline_optimize_factory(model_key, variant, item_lookup):
    spec = config.QUALITY_MODELS[model_key]
    instruction = quality_system_instruction(
        family=str(spec["family"]), thinking_enabled=bool(spec["thinking"]), variant=variant
    )
    return QualityOfflineAdapter(
        model_key, instruction_override=instruction, item_lookup=item_lookup,
        family=str(spec["family"]),
    )


def _offline_run_factory(model_key, instruction, item_lookup):
    spec = config.QUALITY_MODELS[model_key]
    return QualityOfflineAdapter(
        model_key, instruction_override=instruction, item_lookup=item_lookup,
        family=str(spec["family"]),
    )


def _live_optimize_factory(model_key, variant, item_lookup):
    from bakeoff.adapters.bedrock import BedrockModelAdapter

    spec = config.QUALITY_MODELS[model_key]
    instruction = quality_system_instruction(
        family=str(spec["family"]), thinking_enabled=bool(spec["thinking"]), variant=variant
    )
    return BedrockModelAdapter(
        model_key,
        str(spec["bedrock_model_id"]),
        family=str(spec["family"]),
        thinking=bool(spec["thinking"]),
        accepts_temperature=bool(spec.get("accepts_temperature", False)),
        instruction_override=instruction,
    )


def _live_run_factory(model_key, instruction, item_lookup):
    from bakeoff.adapters.bedrock import BedrockModelAdapter

    spec = config.QUALITY_MODELS[model_key]
    return BedrockModelAdapter(
        model_key,
        str(spec["bedrock_model_id"]),
        family=str(spec["family"]),
        thinking=bool(spec["thinking"]),
        accepts_temperature=bool(spec.get("accepts_temperature", False)),
        instruction_override=instruction,
    )


# ---------------------------------------------------------------------------
# Phase drivers
# ---------------------------------------------------------------------------
async def _do_optimize(backend: str, reps: int) -> dict:
    closeness, _judge = (
        build_offline_scorers() if backend == "offline" else build_live_scorers()
    )
    factory = _offline_optimize_factory if backend == "offline" else _live_optimize_factory
    items = load_multi_turn_items()
    heldout, _ = split_items(items)
    # Live: bound concurrency to the model cap so the tuning sweep finishes in
    # bounded wall-clock instead of one serial Bedrock call at a time. Offline:
    # stay serial (None) so the deterministic test ordering is unchanged.
    opt_concurrency = None if backend == "offline" else config.CONCURRENCY_CAPS["model"]
    results = await opt.optimize_prompts(
        adapter_factory=factory, closeness_scorer=closeness, reps=reps, backend=backend,
        max_concurrency=opt_concurrency,
    )
    report_path = opt.write_optimizer_report(
        results, backend=backend, heldout_n=len(heldout)
    )
    prompts_path = opt.write_chosen_prompts(results, backend=backend)
    print(f"[optimize] backend={backend} heldout_items={len(heldout)}")
    for mk, r in results.items():
        best = r.leaderboard[0]
        print(
            f"  {mk:28s} -> {r.chosen_variant_id:42s} "
            f"closeness={best.mean_closeness:.4f} (t1={best.turn1_mean:.3f} later={best.later_mean:.3f})"
        )
    print(f"[optimize] chosen prompts -> {prompts_path}")
    print(f"[optimize] leaderboard    -> {report_path}")
    return opt.load_chosen_prompts()


async def _do_run(backend: str, reps: int, chosen: Optional[dict]) -> None:
    closeness, _judge = (
        build_offline_scorers() if backend == "offline" else build_live_scorers()
    )
    factory = _offline_run_factory if backend == "offline" else _live_run_factory
    chosen = chosen or opt.load_chosen_prompts()
    models_blob = (chosen or {}).get("models", {})
    if not models_blob:
        raise SystemExit(
            "[run] no chosen prompts found — run the 'optimize' phase first "
            "(writes quality_prompts.json)."
        )
    instructions = {mk: m["instruction"] for mk, m in models_blob.items()}
    variant_ids = {mk: m.get("chosen_variant_id", "chosen") for mk, m in models_blob.items()}

    def _progress(o) -> None:
        print(f"  generated {o.model:28s} {o.item_id} rep={o.rep} turns={o.turn_count}", flush=True)

    result = await qrun.run_quality(
        adapter_factory=factory,
        closeness_scorer=closeness,
        chosen_instructions=instructions,
        chosen_variant_ids=variant_ids,
        reps=reps,
        progress=_progress,
    )
    print(f"[run] backend={backend} {result.to_dict()}")


async def _do_judge(backend: str) -> None:
    _closeness, judge = (
        build_offline_scorers() if backend == "offline" else build_live_scorers()
    )
    result = await qjudge.run_quality_judge(judge_scorer=judge)
    print(
        f"[judge] backend={backend} turns_total={result.turns_total} "
        f"judged={result.turns_judged} skipped={result.turns_skipped} by_model={result.by_model}"
    )


# ---------------------------------------------------------------------------
# Safety: refuse live while a bake-off run looks active (shared Bedrock budget)
# ---------------------------------------------------------------------------
def _bakeoff_run_looks_active() -> bool:
    """Heuristic: is a bake-off generation run currently writing outcomes?

    Checks whether the bake-off outcomes file has been modified in the last 2
    minutes. Not authoritative (the live operator knows best), but enough to make
    ``--backend live`` refuse-by-default while the converse run is still writing,
    so the quality study cannot silently contend for the Bedrock rate limit.
    """
    import time

    p = config.OUTCOMES_PATH
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return False
    return (time.time() - mtime) < 120.0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bakeoff.quality.main",
        description="Multi-turn quality study: optimize prompts, run, judge.",
    )
    parser.add_argument("phase", choices=["optimize", "run", "judge", "all"])
    parser.add_argument(
        "--backend", choices=["offline", "live"], default="offline",
        help="offline (deterministic, no network) or live (real Bedrock). Default offline.",
    )
    parser.add_argument(
        "--optimizer-reps", type=int, default=config.QUALITY_OPTIMIZER_REPS,
        help="reps per (item,variant) during optimization",
    )
    parser.add_argument(
        "--run-reps", type=int, default=config.QUALITY_RUN_REPS,
        help="reps per (model,item) during the quality run",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="allow --backend live even if a bake-off run looks active",
    )
    args = parser.parse_args(argv)

    config.ensure_dirs()

    if args.backend == "live" and _bakeoff_run_looks_active() and not args.force:
        print(
            "[guard] A bake-off run looks active (outcomes.jsonl written in the last "
            "2 min). Refusing --backend live to avoid contending for the shared "
            "Bedrock rate limit. Re-run with --force once the bake-off run is done."
        )
        return 2

    if args.phase in ("optimize", "all"):
        chosen = asyncio.run(_do_optimize(args.backend, args.optimizer_reps))
    else:
        chosen = None

    if args.phase in ("run", "all"):
        asyncio.run(_do_run(args.backend, args.run_reps, chosen))

    if args.phase in ("judge", "all"):
        asyncio.run(_do_judge(args.backend))

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
