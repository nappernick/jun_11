"""Durable LIVE GEPA run (run-time script, not throwaway).

Runs the verified live GEPA chain at scale and PERSISTS every rollout so progress
survives a crash/disconnect and can be watched live:

  build_live_backend (Opus judge + Embed v4 + alpha OpenSearch faq_evidence_b)
    -> JudgeInLoopScorer -> JudgeBackedGepaMetric  [wrapped for incremental persistence]
    -> LiveGepaEngine (real gepa.optimize) <- make_bedrock_reflection_lm (Sonnet, auth-healing)

Each candidate rollout appends a record to data/bakeoff/gepa_live_progress.jsonl and updates
data/bakeoff/gepa_live_status.json; the final winner lands in data/bakeoff/gepa_live_result.json.

Usage (launch detached so it outlives the shell):
    AWS_PROFILE=alpha PYTHONPATH=. nohup .venv/bin/python scripts/gepa_live_run.py \
        --budget 300 --trainset 24 --model haiku-4.5 >> logs/gepa_live_run.log 2>&1 &

The reflection LM self-heals on credential expiry (see make_bedrock_reflection_lm); on-disk
alpha creds are kept fresh by scripts/creds.sh, so a multi-hour run does not lapse.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback
from pathlib import Path

# repo root on sys.path (running from scripts/ otherwise can't import `bakeoff`).
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("AWS_PROFILE", "alpha")

from bakeoff import config

# SSM live pointer resolves to faq_evidence_b; the config default (faq_evidence_a) is stale.
config.QUALITY_OPT_OPENSEARCH_ALPHA_INDEX = "faq_evidence_b"

from bakeoff.quality.dataset import load_multi_turn_items
from bakeoff.quality.optimizer.backends import build_live_backend
from bakeoff.quality.optimizer.controller import _seed_instruction_for
from bakeoff.quality.optimizer.gepa_engine import (
    JudgeBackedGepaMetric,
    LiveGepaEngine,
    MetricResult,
    make_bedrock_reflection_lm,
)
from bakeoff.quality.optimizer.judge_loop import JudgeInLoopScorer

DATA = _ROOT / "data" / "bakeoff"
PROGRESS = DATA / "gepa_live_progress.jsonl"
STATUS = DATA / "gepa_live_status.json"
RESULT = DATA / "gepa_live_result.json"


def _now() -> float:
    return time.time()


def _log(msg: str) -> None:
    print(f"[gepa-run {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class PersistingMetric:
    """Delegate to JudgeBackedGepaMetric; append a record per rollout + roll the status file.

    This is the only reliable per-rollout hook: gepa.optimize runs opaque in a thread and
    returns just the final result, so persistence/observability must live in the metric the
    adapter calls once per (candidate, item).
    """

    def __init__(self, inner: JudgeBackedGepaMetric, *, started_at: float, budget: int, model: str) -> None:
        self._inner = inner
        self._n = 0
        self._best = float("-inf")
        self._started = started_at
        self._budget = int(budget)
        self._model = model

    async def evaluate(self, instruction, items=None) -> MetricResult:  # noqa: ANN001
        res = await self._inner.evaluate(instruction, items=items)
        self._n += 1
        if res.score > self._best:
            self._best = res.score
        rec = {
            "ts": _now(),
            "n": self._n,
            "score": round(float(res.score), 4),
            "per_dimension": {k: round(float(v), 4) for k, v in (res.per_dimension or {}).items()},
            "instr_len": len(instruction),
            "instr_head": instruction[:160],
        }
        with PROGRESS.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        elapsed = _now() - self._started
        rate = elapsed / self._n if self._n else 0.0
        STATUS.write_text(json.dumps({
            "state": "running",
            "model": self._model,
            "budget": self._budget,
            "rollouts_done": self._n,
            "best_score": round(self._best, 4),
            "started_at": self._started,
            "elapsed_s": round(elapsed, 1),
            "sec_per_rollout": round(rate, 1),
            "eta_s_remaining": round(max(0, self._budget - self._n) * rate, 1),
            "last_ts": _now(),
        }, indent=2))
        _log(f"rollout {self._n}/{self._budget} score={res.score:.3f} best={self._best:.3f} "
             f"({rate:.1f}s/rollout, eta~{max(0, self._budget - self._n) * rate / 60:.0f}m)")
        return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=300, help="max metric calls (rollouts)")
    ap.add_argument("--trainset", type=int, default=24, help="# items GEPA evolves against")
    ap.add_argument("--model", default="haiku-4.5", help="target model whose instruction is optimized")
    args = ap.parse_args()

    DATA.mkdir(parents=True, exist_ok=True)
    # fresh progress for this run
    PROGRESS.write_text("")
    started = _now()
    STATUS.write_text(json.dumps({"state": "starting", "started_at": started}, indent=2))

    try:
        items = load_multi_turn_items()
        train = items[: args.trainset]
        _log(f"loaded {len(items)} items; trainset={len(train)}; budget={args.budget}; model={args.model}")

        backend = build_live_backend()
        _log(f"live backend: retrieval={getattr(backend.retrieval, 'name', None)} "
             f"judge={getattr(backend.judge_scorer, 'judge_model', None)} "
             f"author={getattr(backend.author, 'author_model', None)} "
             f"ragas={getattr(backend.ragas_adapter, 'name', None)}")

        scorer = JudgeInLoopScorer(backend, reps=1)
        inner = JudgeBackedGepaMetric(scorer=scorer, model=args.model, items=train)
        metric = PersistingMetric(inner, started_at=started, budget=args.budget, model=args.model)
        reflection = make_bedrock_reflection_lm()
        engine = LiveGepaEngine(items=train, reflection_lm=reflection, use_merge=True)
        seed = _seed_instruction_for(args.model)
        _log(f"seed_instruction[:120]={seed[:120]!r}")

        res = asyncio.run(engine.optimize(seed_instruction=seed, metric=metric, budget=args.budget))

        elapsed = _now() - started
        RESULT.write_text(json.dumps({
            "state": "done",
            "model": args.model,
            "budget": args.budget,
            "trainset": len(train),
            "elapsed_s": round(elapsed, 1),
            "best_score": res.best_score,
            "per_dimension": res.per_dimension,
            "seed_changed": res.best_instruction != seed,
            "best_instruction": res.best_instruction,
            "history": [{"score": s, "instr_head": instr[:120]} for instr, s in res.history],
        }, indent=2))
        STATUS.write_text(json.dumps({
            "state": "done", "model": args.model, "budget": args.budget,
            "best_score": res.best_score, "elapsed_s": round(elapsed, 1),
            "seed_changed": res.best_instruction != seed, "finished_at": _now(),
        }, indent=2))
        _log(f"DONE_OK elapsed={elapsed:.0f}s best_score={res.best_score:.4f} "
             f"seed_changed={res.best_instruction != seed}")
        return 0
    except Exception:
        _log("FAILED with exception:")
        traceback.print_exc()
        STATUS.write_text(json.dumps({"state": "failed", "finished_at": _now(),
                                      "error": traceback.format_exc()[-1500:]}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
