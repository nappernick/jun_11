"""Read-only trace diagnostic: is the ~0.4 ceiling a PROMPT problem or a RETRIEVAL problem?

For a small stratified held-out sample, runs the REAL live pipeline exactly as the
optimizer does — AOSS retrieve -> Rerank v4 -> sonnet-4.6-thinking-off generate ->
the new answerability-aware Opus judge + faithfulness gate — and dumps, per turn:

  question | answerability | gold node ids | retrieved ids (+ was gold retrieved?)
  | retrieved fragment texts | the model's answer | judge faith/corr/comp + evidence
  | the computed overall.

Then it aggregates the decisive signal: GOLD-RETRIEVAL RATE on answerable turns. If the
gold fragment usually isn't retrieved, no prompt can ground -> RETRIEVAL problem. If the
gold IS retrieved but scores are still low, the model is fumbling good fragments ->
PROMPT problem.

Writes nothing to the optimizer stores; touches no running process. Usage:
    PYTHONPATH=. AWS_PROFILE=alpha .venv/bin/python scripts/diagnose_traces.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import os
os.environ.setdefault("AWS_PROFILE", "alpha")

from bakeoff import config
from bakeoff.quality.dataset import load_multi_turn_items, turn_reference
from bakeoff.quality.judge import _turn_judge_inputs
from bakeoff.quality.optimizer.backends import build_live_backend
from bakeoff.quality.optimizer.retrieval import RetrievalQuery
from bakeoff.quality.types import GroundTruthKind, TurnOutcome
from bakeoff.scoring.judge import JUDGE_DIMENSIONS

MODEL = "sonnet-4.6-thinking-off"
N_PER_BUCKET = 3  # items per answerability bucket (full / partial / none)
OUT = _ROOT / "data" / "bakeoff" / "diagnose_traces.json"


def _seed_instruction() -> str:
    """Use the island-0 seed file as the prompt under test (what's actually running)."""
    seed = config.QUALITY_OPT_V3_SEEDS_DIR / f"{MODEL}_i0.txt"
    try:
        return seed.read_text(encoding="utf-8").strip()
    except OSError:
        from bakeoff.quality.optimizer.island import _seed_instruction_for
        return _seed_instruction_for(MODEL)


def _stratified_sample(items):
    """A few items from each answerability bucket so we see all regimes."""
    buckets: dict[str, list] = {"full": [], "partial": [], "none": []}
    for it in items:
        a = (it.answerability or it.cohort.answerability or "full").lower()
        if a in buckets and len(buckets[a]) < N_PER_BUCKET:
            buckets[a].append(it)
    out = []
    for a in ("full", "partial", "none"):
        out.extend(buckets[a])
    return out


async def main() -> int:
    instruction = _seed_instruction()
    items = _stratified_sample(load_multi_turn_items())
    print(f"index={config.QUALITY_OPT_OPENSEARCH_ALPHA_INDEX} model={MODEL} "
          f"sample={len(items)} items\n")

    backend = build_live_backend()
    item_lookup = {it.item_id: it for it in items}
    traces = []
    gold_turns = 0
    gold_retrieved_turns = 0

    for it in items:
        # Generate the whole conversation once (as the optimizer does).
        adapter = backend.answer_adapter_factory(MODEL, instruction, item_lookup)
        resp = await adapter.generate(it, [], config.DEFAULT_TEMPERATURE)
        answers = list(resp.per_turn_answers or [resp.text])

        for ti, turn in enumerate(it.turns):
            ans = answers[ti] if ti < len(answers) else ""
            query_text = getattr(turn, "user_utterance", None) or it.query or ""
            frags = await backend.retrieval.retrieve(
                RetrievalQuery(item_id=it.item_id, turn=ti + 1, query=query_text)
            )
            retrieved_ids = [str(f.get("id", "")) for f in frags]
            turn_gold_ids = [g.node_id for g in (getattr(turn, "gold", None) or [])]
            kind, reference_text = turn_reference(it, ti)
            answerability = turn.answerability or "full"
            gold_hit = bool(set(turn_gold_ids) & set(retrieved_ids))
            if turn_gold_ids:
                gold_turns += 1
                gold_retrieved_turns += int(gold_hit)

            # Judge exactly as the optimizer does.
            outcome = TurnOutcome(
                turn=ti + 1, answerability=answerability, response_dependent=False,
                answer_text=ans, reference_text=reference_text, closeness=None,
            )
            ideal, gold_texts, judge_answerability = _turn_judge_inputs(it, ti, outcome)
            scores, evidence = await asyncio.to_thread(
                backend.judge_scorer.score_detailed, ans,
                ideal_text=ideal, fragments=frags, gold_texts=gold_texts,
                momentary_state=getattr(turn, "momentary_state", "neutral"),
                answerability=judge_answerability, question=query_text,
            )
            dims = {d: round(float(getattr(scores, d)), 3) for d in JUDGE_DIMENSIONS}

            trace = {
                "item_id": it.item_id, "turn": ti + 1, "answerability": answerability,
                "question": query_text,
                "gold_node_ids": turn_gold_ids,
                "retrieved_ids": retrieved_ids,
                "gold_retrieved": gold_hit,
                "retrieved_fragments": [
                    {"id": str(f.get("id", "")), "conf": round(float(f.get("confidence", 0)), 3),
                     "text": str(f.get("text", ""))[:240]}
                    for f in frags
                ],
                "answer": ans[:500],
                "judge": dims,
                "judge_evidence": {k: v[:200] for k, v in (evidence or {}).items()},
            }
            traces.append(trace)
            print(f"--- {it.item_id} t{ti+1} [{answerability}] gold={turn_gold_ids or '∅'} "
                  f"gold_retrieved={gold_hit} faith={dims['faithfulness']} "
                  f"corr={dims['correctness']} comp={dims['completeness']}")
            print(f"    Q: {query_text[:110]}")
            print(f"    retrieved: {retrieved_ids}")
            print(f"    A: {ans[:160]!r}")

    summary = {
        "model": MODEL,
        "index": config.QUALITY_OPT_OPENSEARCH_ALPHA_INDEX,
        "n_turns": len(traces),
        "gold_turns": gold_turns,
        "gold_retrieved_turns": gold_retrieved_turns,
        "gold_retrieval_rate": round(gold_retrieved_turns / gold_turns, 3) if gold_turns else None,
        "mean_faithfulness": round(sum(t["judge"]["faithfulness"] for t in traces) / len(traces), 3),
        "mean_correctness": round(sum(t["judge"]["correctness"] for t in traces) / len(traces), 3),
        "mean_completeness": round(sum(t["judge"]["completeness"] for t in traces) / len(traces), 3),
    }
    OUT.write_text(json.dumps({"summary": summary, "traces": traces}, indent=2))
    print("\n===== SUMMARY =====")
    print(json.dumps(summary, indent=2))
    print(f"\nfull traces -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
