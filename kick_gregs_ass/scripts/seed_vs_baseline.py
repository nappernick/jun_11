"""Isolate WHY live answers fail on answerable turns: RETRIEVAL vs SEED PROMPT.

For a stratified sample of ANSWERABLE turns (full/partial), holds retrieval constant
and measures, per turn:

  * gold_retrieved  — is the turn's gold node in the retrieved set? (retrieval health;
    if gold is absent, no prompt can ground -> RETRIEVAL problem.)
  * SEED prompt     — the optimizer's running seed (v3_seeds/<model>_i0.txt): does the
    model answer or refuse, and the judge's correctness/completeness.
  * BASELINE prompt — a minimal "answer from the fragments, decline only if truly absent"
    prompt with NONE of the heavy refusal scaffolding.

The 2x2 that gives certainty (on answerable turns where gold IS retrieved):

  * both fail            -> generation/fragments problem (the model can't use good evidence)
  * baseline OK, seed bad -> the SEED over-refuses / fights the model (prompt problem; fixable)
  * both OK               -> not a failure case
  * gold NOT retrieved    -> RETRIEVAL problem (unfixable by prompting)

Writes only to a scratch JSON; touches no optimizer store. Usage:
    PYTHONPATH=. AWS_PROFILE=alpha .venv/bin/python scripts/seed_vs_baseline.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("AWS_PROFILE", "alpha")

from bakeoff import config
from bakeoff.quality.dataset import ideal_response_text, load_multi_turn_items, turn_reference
from bakeoff.quality.optimizer.backends import build_live_backend
from bakeoff.quality.optimizer.retrieval import RetrievalQuery
from bakeoff.quality.types import GroundTruthKind
from bakeoff.scoring.judge import JUDGE_DIMENSIONS

MODEL = "sonnet-4.6-thinking-off"
N_PER_BUCKET = 3        # items per answerable bucket (full / partial)
MAX_TURNS = 4
OUT = _ROOT / "data" / "bakeoff" / "seed_vs_baseline.json"

# Minimal grounded-answer prompt: NO refusal scaffolding, NO silent answerability check.
BASELINE_PROMPT = (
    "You are an FAQ assistant for a Travel, Events and Expenses help desk. "
    "Answer the user's question using ONLY the retrieved reference fragments shown to you "
    "this turn. If the fragments contain the answer, give it directly and concisely. "
    "Only if the fragments genuinely do not contain the answer, say you don't have that "
    "information. Do not invent facts that are not in the fragments."
)

REFUSAL = re.compile(
    r"(don't|do not|don’t) have (that|the|this|enough) (information|info|details)"
    r"|not (in|contained in|covered) (the|my|these|those|that) (reference|material|fragments|information|resources)"
    r"|I (can't|cannot|am unable to|'m unable to) (answer|help|provide|find)"
    r"|please (contact|reach out)|isn't (in|something) (the|my)",
    re.IGNORECASE,
)


def _decline_inputs():
    return ("Correctly decline: state you don't have the information and point to the right owner.", [], "none")


def _judge_inputs_from_kind(item, kind, reference_text):
    conv_answerability = item.answerability or item.cohort.answerability
    if kind == GroundTruthKind.GOLD:
        gold = item.gold
        gold_texts = [g.markdown or g.snippet or g.title for g in gold
                      if (g.markdown or g.snippet or g.title)]
        ideal = ideal_response_text(gold, item.wants)
        return ideal, gold_texts, (conv_answerability or "full")
    if kind == GroundTruthKind.ABSTENTION:
        return _decline_inputs()
    if conv_answerability == "none":
        return _decline_inputs()
    return (reference_text or ""), [], "full"


def _stratified_answerable(items):
    buckets = {"full": [], "partial": []}
    for it in items:
        a = (getattr(it, "answerability", None) or it.cohort.answerability or "full").lower()
        if a in buckets and len(buckets[a]) < N_PER_BUCKET:
            buckets[a].append(it)
    return buckets["full"] + buckets["partial"]


def _seed_instruction():
    return (config.QUALITY_OPT_V3_SEEDS_DIR / f"{MODEL}_i0.txt").read_text(encoding="utf-8").strip()


async def _answers_for(backend, instruction, item, item_lookup):
    adapter = backend.answer_adapter_factory(MODEL, instruction, item_lookup)
    resp = await adapter.generate(item, [], config.DEFAULT_TEMPERATURE)
    return list(resp.per_turn_answers or [resp.text])


async def main() -> int:
    seed = _seed_instruction()
    items = _stratified_answerable(load_multi_turn_items())
    backend = build_live_backend()
    item_lookup = {it.item_id: it for it in items}
    judge = backend.judge_scorer

    rows = []
    agg = {"seed": {d: [] for d in JUDGE_DIMENSIONS}, "base": {d: [] for d in JUDGE_DIMENSIONS}}
    gold_turns = 0; gold_hit = 0
    seed_refused = 0; base_refused = 0; answerable_turns = 0

    print(f"index={config.QUALITY_OPT_OPENSEARCH_ALPHA_INDEX} model={MODEL} "
          f"items={len(items)} max_turns={MAX_TURNS}\n")

    for it in items:
        seed_answers = await _answers_for(backend, seed, it, item_lookup)
        base_answers = await _answers_for(backend, BASELINE_PROMPT, it, item_lookup)

        for ti, turn in enumerate(it.turns):
            if ti >= MAX_TURNS:
                break
            kind, reference_text = turn_reference(it, ti)
            answerability = turn.answerability or "full"
            # only answerable turns are the failure surface we care about
            if answerability not in ("full", "partial") or kind == GroundTruthKind.ABSTENTION:
                continue
            answerable_turns += 1
            query_text = getattr(turn, "user_utterance", None) or it.query or ""
            frags = await backend.retrieval.retrieve(
                RetrievalQuery(item_id=it.item_id, turn=ti + 1, query=query_text)
            )
            retrieved_ids = [str(f.get("id", "")) for f in frags]
            gold_ids = [g.node_id for g in (getattr(turn, "gold", None) or [])]
            this_gold_hit = bool(set(gold_ids) & set(retrieved_ids))
            if gold_ids:
                gold_turns += 1; gold_hit += int(this_gold_hit)

            ideal, gold_texts, judge_answerability = _judge_inputs_from_kind(it, kind, reference_text)
            seed_ans = seed_answers[ti] if ti < len(seed_answers) else ""
            base_ans = base_answers[ti] if ti < len(base_answers) else ""
            s_ref = bool(REFUSAL.search(seed_ans or "")); b_ref = bool(REFUSAL.search(base_ans or ""))
            seed_refused += int(s_ref); base_refused += int(b_ref)

            def judge_dims(ans):
                scores, _ = judge.score_detailed(
                    ans, ideal_text=ideal, fragments=frags, gold_texts=gold_texts,
                    momentary_state=getattr(turn, "momentary_state", "neutral"),
                    answerability=judge_answerability, question=query_text,
                )
                return {d: round(float(getattr(scores, d)), 3) for d in JUDGE_DIMENSIONS}

            s_dims = await asyncio.to_thread(judge_dims, seed_ans)
            b_dims = await asyncio.to_thread(judge_dims, base_ans)
            for d in JUDGE_DIMENSIONS:
                agg["seed"][d].append(s_dims[d]); agg["base"][d].append(b_dims[d])

            rows.append({
                "item_id": it.item_id, "turn": ti + 1, "answerability": answerability,
                "gold_ids": gold_ids, "retrieved_ids": retrieved_ids, "gold_retrieved": this_gold_hit,
                "question": query_text[:120],
                "seed": {"refused": s_ref, "dims": s_dims, "answer": seed_ans[:200]},
                "baseline": {"refused": b_ref, "dims": b_dims, "answer": base_ans[:200]},
            })
            tag = "GOLD✓" if this_gold_hit else ("GOLD✗" if gold_ids else "noGold")
            print(f"--- {it.item_id} t{ti+1} [{answerability}] {tag}  "
                  f"SEED corr={s_dims['correctness']} comp={s_dims['completeness']} ref={int(s_ref)}  |  "
                  f"BASE corr={b_dims['correctness']} comp={b_dims['completeness']} ref={int(b_ref)}")

    def mean(xs): return round(sum(xs) / len(xs), 3) if xs else None
    summary = {
        "answerable_turns": answerable_turns,
        "gold_retrieval_rate": round(gold_hit / gold_turns, 3) if gold_turns else None,
        "gold_turns": gold_turns,
        "seed_refusal_rate": round(seed_refused / answerable_turns, 3) if answerable_turns else None,
        "baseline_refusal_rate": round(base_refused / answerable_turns, 3) if answerable_turns else None,
        "seed_dims": {d: mean(agg["seed"][d]) for d in JUDGE_DIMENSIONS},
        "baseline_dims": {d: mean(agg["base"][d]) for d in JUDGE_DIMENSIONS},
    }
    OUT.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
    print("\n===== SEED vs BASELINE on answerable turns =====")
    print(json.dumps(summary, indent=2))
    print(f"\nfull rows -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
