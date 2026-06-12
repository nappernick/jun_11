"""Tiny LIVE GEPA smoke (throwaway). Proves the live chain end-to-end against real Bedrock:
build_live_backend (Opus judge + Embed v4 + alpha OpenSearch) -> JudgeBackedGepaMetric ->
LiveGepaEngine -> make_bedrock_reflection_lm (Sonnet). A couple of candidate evals over a
tiny slice. Cents, ~minutes. Usage: python scripts/_gepa_live_smoke.py [budget] [n_items]
"""
import asyncio
import os
import sys
import time
import traceback

os.environ.setdefault("AWS_PROFILE", "alpha")

from bakeoff import config

# SSM live pointer resolves to faq_evidence_b; the config default (faq_evidence_a) is stale.
config.QUALITY_OPT_OPENSEARCH_ALPHA_INDEX = "faq_evidence_b"

from bakeoff.quality.dataset import load_multi_turn_items
from bakeoff.quality.optimizer.backends import build_live_backend
from bakeoff.quality.optimizer.judge_loop import JudgeInLoopScorer
from bakeoff.quality.optimizer.gepa_engine import (
    JudgeBackedGepaMetric,
    LiveGepaEngine,
    make_bedrock_reflection_lm,
)
from bakeoff.quality.optimizer.controller import _seed_instruction_for

MODEL = "haiku-4.5"
BUDGET = int(sys.argv[1]) if len(sys.argv) > 1 else 2
N_ITEMS = int(sys.argv[2]) if len(sys.argv) > 2 else 2


def main() -> None:
    try:
        items = load_multi_turn_items()
        sl = items[:N_ITEMS]
        print(f"[smoke] loaded {len(items)} items; using {len(sl)}; budget={BUDGET}; model={MODEL}", flush=True)
        backend = build_live_backend()
        print(f"[smoke] live backend: name={backend.name} retrieval={getattr(backend.retrieval,'name',None)} "
              f"judge={getattr(backend.judge_scorer,'judge_model',None)} "
              f"author={getattr(backend.author,'author_model',None)} "
              f"ragas={getattr(backend.ragas_adapter,'name',None)}", flush=True)
        scorer = JudgeInLoopScorer(backend, reps=1)
        metric = JudgeBackedGepaMetric(scorer=scorer, model=MODEL, items=sl)
        reflection = make_bedrock_reflection_lm()
        engine = LiveGepaEngine(items=sl, reflection_lm=reflection, merge_max=1, use_merge=False)
        seed = _seed_instruction_for(MODEL)
        print(f"[smoke] seed_instruction[:120]={seed[:120]!r}", flush=True)
        t0 = time.time()
        res = asyncio.run(engine.optimize(seed_instruction=seed, metric=metric, budget=BUDGET))
        dt = time.time() - t0
        print("[smoke] === LIVE GEPA RESULT ===", flush=True)
        print(f"[smoke] elapsed_s={dt:.1f}", flush=True)
        print(f"[smoke] best_score={res.best_score}", flush=True)
        print(f"[smoke] per_dimension={res.per_dimension}", flush=True)
        print(f"[smoke] seed_changed={res.best_instruction != seed}", flush=True)
        print(f"[smoke] best_instruction[:400]={res.best_instruction[:400]!r}", flush=True)
        print("[smoke] DONE_OK", flush=True)
    except Exception:
        print("[smoke] FAILED with exception:", flush=True)
        traceback.print_exc()
        print("[smoke] DONE_FAIL", flush=True)


if __name__ == "__main__":
    main()
