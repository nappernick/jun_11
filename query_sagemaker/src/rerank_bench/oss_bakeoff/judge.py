#!/usr/bin/env python3
"""judge.py — pairwise PREFERENCE judge over the real-pool disagreement set.

This is the PRIMARY verdict of the bakeoff. For every (query, model_a, model_b)
disagreement in metrics.json we show the LLM judge the query and BOTH full
rankings, rendered as anonymized numbered lists of "title — snippet" (model
identity is NEVER revealed), and ask which ranking better serves the query.

Position bias is cancelled by running BOTH orderings (swap which model is
"Ranking 1"); a model only wins a pair if both orderings agree on it, otherwise
the pair is a tie. From the per-pair winners we build a pairwise win-rate matrix
and a per-model overall judge score in [0,1] (wins count 1, ties 0.5).

This is a PREFERENCE judge (which ranking is better), NOT gold augmentation —
contrast bakeoff/eval_gen.py, whose judge labels relevant docs. We mirror that
file's Bedrock converse + fence-tolerant JSON parsing style, but the task differs.

Auth: alpha profile, bedrock-runtime, model us.anthropic.claude-opus-4-8 (highest
intelligence — the judge is the most reasoning-critical step in the bakeoff).
Resilient: any parse/transport error on a pair -> tie + note, never crash the batch.

CLI: python oss_bakeoff/judge.py [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent

JUDGE_MODEL = "us.anthropic.claude-opus-4-8"  # highest-intelligence judge (user directive)
REGION = "us-west-2"
PROFILE = "alpha"

# Show the judge the FULL document, not a snippet. Opus 4.8 is far more capable
# and deeply-considering than any reranker here; it must see what it is judging.
# 3000 chars covers essentially every FAQ doc in the corpus (max ~3066); only the
# rare giant outlier truncates (which the rerankers truncate too, so it is fair).
DOC_CHARS = 3000

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _strip_fence(text: str) -> str:
    """Bedrock/Claude often wraps JSON in a ```json fence. Strip it."""
    return _FENCE_RE.sub("", text.strip())


def _parse_json(raw: str):
    """Extract the verdict JSON from Opus's reply, which now contains free-form
    reasoning FOLLOWED BY the JSON object. Strategy: try the whole thing; else take
    the LAST flat {...} object (the verdict has no nested braces, and putting it last
    is what the prompt asks for) so reasoning prose before it never interferes."""
    try:
        return json.loads(_strip_fence(raw))
    except json.JSONDecodeError:
        pass
    flat = re.findall(r"\{[^{}]*\}", raw, re.DOTALL)
    for candidate in reversed(flat):  # the verdict is the last JSON object
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    match = re.search(r"(\{.*\})", raw, re.DOTALL)  # last resort: greedy outermost
    if match:
        return json.loads(match.group(1))
    raise json.JSONDecodeError("no json object in judge reply", raw, 0)


# --- rendering ---------------------------------------------------------------

def _render_ranking(ranking: list[str], pool_index: dict[str, dict]) -> str:
    """Render a ranking as a numbered list of each document's title + FULL text,
    best-first. The judge sees the actual content it is asked to evaluate, not a
    snippet. Unknown ids degrade to a placeholder rather than crashing the render.
    """
    blocks = []
    for rank, node_id in enumerate(ranking, start=1):
        doc = pool_index.get(node_id) or {}
        title = (doc.get("title") or node_id or "").strip()
        text = (doc.get("text") or "").strip()[:DOC_CHARS]
        blocks.append(f"--- position {rank} ---\nTITLE: {title}\n{text}")
    return "\n\n".join(blocks)


def _preference_prompt(query: str, ranking_first: str, ranking_second: str) -> str:
    """The pairwise-preference prompt. The two systems are anonymized as Ranking 1
    and Ranking 2. Opus is given the full documents and full room to reason — we
    trust its judgment and invest only in clear, effective framing of the task."""
    return (
        "You are the authoritative judge of search quality for an Amazon employee "
        "help system. A real employee asked the question below. A retrieval system "
        "pulled a pool of candidate FAQ documents, and TWO different reranking "
        "systems each ordered that SAME pool from most to least relevant. Your job "
        "is to decide which ordering would better serve the employee — i.e. which "
        "one places the documents that most directly and completely answer THIS "
        "question at the top, where the employee will actually read them.\n\n"
        f"THE EMPLOYEE'S QUESTION:\n{query}\n\n"
        "==================== RANKING 1 (most relevant first) ====================\n"
        f"{ranking_first}\n\n"
        "==================== RANKING 2 (most relevant first) ====================\n"
        f"{ranking_second}\n\n"
        "Think it through carefully and on your own terms: read the documents, work "
        "out what would actually resolve this employee's need, and assess which "
        "ordering surfaces the genuinely most-helpful documents earliest. Weigh the "
        "top positions most — that is what the employee sees first. Regional/global "
        "variants, near-duplicates, and partially-relevant docs are where the two "
        "orderings usually differ; use your full judgment on which placement is "
        "better. Reason as deeply as the case warrants. If, after genuine "
        "consideration, the two orderings would serve the employee equally well, "
        "it is correct to call it a tie — do not force a winner.\n\n"
        "Write your reasoning, then END your response with a single JSON object on "
        "its own line capturing your verdict:\n"
        '{"winner": "1" | "2" | "tie", "confidence": <0.0-1.0>, '
        '"rationale": "<your key reason, as long as it needs to be>"}'
    )


# --- one converse call (the single-ordering primitive) -----------------------

def _judge_once(client, query: str, ranking_first: str, ranking_second: str) -> dict:
    """ONE Bedrock converse call for ONE ordering. Returns the parsed verdict
    {"winner":"1|2|tie","confidence":float,"rationale":str}. Raises on transport
    or parse failure (the caller decides how to recover)."""
    prompt = _preference_prompt(query, ranking_first, ranking_second)
    resp = client.converse(
        modelId=JUDGE_MODEL,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 6000},  # ample room for deep reasoning; Opus 4.8 rejects temperature
    )
    raw = resp["output"]["message"]["content"][0]["text"]
    verdict = _parse_json(raw)
    winner = str(verdict.get("winner", "tie")).strip().lower()
    if winner not in ("1", "2", "tie"):
        winner = "tie"
    try:
        confidence = float(verdict.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    rationale = str(verdict.get("rationale", "")).strip()
    return {"winner": winner, "confidence": confidence, "rationale": rationale}


# --- one disagreement (both orderings, anonymized, tie-default) --------------

def _judge_pair(client, disagreement: dict, pool_index: dict[str, dict]) -> dict:
    """Judge ONE disagreement with BOTH orderings and fold to an a/b/tie verdict.

    Mapping (the crux): the model only ever sees Ranking 1 / Ranking 2.
      Ordering 1: Ranking1 = model_a, Ranking2 = model_b -> "1"->a "2"->b.
      Ordering 2: Ranking1 = model_b, Ranking2 = model_a -> "1"->b "2"->a.
    consistent = (order1 == order2) in a/b space; final winner = order1 if
    consistent else tie.

    Resilient: any error -> tie, consistent False, error recorded in rationale.
    """
    model_a = disagreement["model_a"]
    model_b = disagreement["model_b"]
    rendered_a = _render_ranking(disagreement["ranking_a"], pool_index)
    rendered_b = _render_ranking(disagreement["ranking_b"], pool_index)
    query = disagreement["query"]

    base = {
        "id": disagreement.get("id"),
        "query": query,
        "model_a": model_a,
        "model_b": model_b,
    }

    try:
        # Ordering 1: a is Ranking 1, b is Ranking 2.
        first = _judge_once(client, query, rendered_a, rendered_b)
        order1 = {"1": "a", "2": "b", "tie": "tie"}[first["winner"]]

        # Ordering 2: SWAP — b is Ranking 1, a is Ranking 2.
        second = _judge_once(client, query, rendered_b, rendered_a)
        order2 = {"1": "b", "2": "a", "tie": "tie"}[second["winner"]]

        consistent = (order1 == order2)
        winner = order1 if consistent else "tie"
        confidence = round((first["confidence"] + second["confidence"]) / 2.0, 4)
        rationale = first["rationale"]
        base.update({
            "winner": winner, "confidence": confidence,
            "order1": order1, "order2": order2, "consistent": consistent,
            "rationale": rationale,
        })
        return base
    except Exception as exc:  # transport / parse / shape — never crash the batch
        base.update({
            "winner": "tie", "confidence": 0.0,
            "order1": "tie", "order2": "tie", "consistent": False,
            "rationale": f"judge error ({type(exc).__name__}): {str(exc)[:160]}",
        })
        return base


# --- aggregation (pure function, no Bedrock) ---------------------------------

def _empty_cell() -> dict:
    return {"wins": 0, "losses": 0, "ties": 0, "winrate": 0.0}


def _winrate(wins: int, losses: int, ties: int) -> float:
    games = wins + losses + ties
    if games == 0:
        return 0.5
    return (wins + 0.5 * ties) / games


def _aggregate(verdicts: list[dict]):
    """Build winrate_matrix and per-model model_score from folded verdicts.

    For each verdict the final 'winner' is in a/b/tie. We credit both directions
    of the matrix symmetrically (a-vs-b and b-vs-a). model_score[m] is the same
    win-rate formula over m's summed totals across every opponent.
    """
    matrix: dict[str, dict[str, dict]] = {}

    def cell(left: str, right: str) -> dict:
        matrix.setdefault(left, {})
        matrix[left].setdefault(right, _empty_cell())
        return matrix[left][right]

    for verdict in verdicts:
        model_a = verdict["model_a"]
        model_b = verdict["model_b"]
        winner = verdict["winner"]
        cell_ab = cell(model_a, model_b)
        cell_ba = cell(model_b, model_a)
        if winner == "a":
            cell_ab["wins"] += 1
            cell_ab["losses"] += 0
            cell_ba["losses"] += 1
        elif winner == "b":
            cell_ab["losses"] += 1
            cell_ba["wins"] += 1
        else:  # tie
            cell_ab["ties"] += 1
            cell_ba["ties"] += 1

    for left, opponents in matrix.items():
        for right, cell_data in opponents.items():
            cell_data["winrate"] = round(
                _winrate(cell_data["wins"], cell_data["losses"], cell_data["ties"]), 4)

    model_score: dict[str, float] = {}
    for model_id, opponents in matrix.items():
        total_wins = sum(c["wins"] for c in opponents.values())
        total_losses = sum(c["losses"] for c in opponents.values())
        total_ties = sum(c["ties"] for c in opponents.values())
        model_score[model_id] = round(_winrate(total_wins, total_losses, total_ties), 4)

    return matrix, model_score


# --- pool index --------------------------------------------------------------

def _build_pool_index(pools_doc: dict) -> dict[str, dict]:
    """Flatten pools.json into node_id -> {title,text,...}. The same doc can
    appear in many query pools; any copy carries the title/text we need."""
    index: dict[str, dict] = {}
    for items in pools_doc.get("pools", {}).values():
        for item in items:
            node_id = item.get("node_id")
            if node_id and node_id not in index:
                index[node_id] = item
    return index


# --- top-level orchestration -------------------------------------------------

def judge(metrics_path="metrics.json", pools_path="pools.json", limit=None, workers=8):
    """Read metrics.json disagreements + pools.json text, run the pairwise
    preference judge over both orderings, and write judge.json. Returns the doc.

    Disagreements are judged CONCURRENTLY (each is independent; each makes 2 Opus
    calls), with input order preserved in the output. Bare relative defaults resolve
    against this file's dir so the CLI works when invoked from the parent.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import boto3
    from botocore.config import Config

    metrics_file = Path(metrics_path)
    if not metrics_file.is_absolute():
        metrics_file = HERE / metrics_file
    pools_file = Path(pools_path)
    if not pools_file.is_absolute():
        pools_file = HERE / pools_file

    metrics = json.loads(metrics_file.read_text())
    pool_index = _build_pool_index(json.loads(pools_file.read_text()))
    disagreements = metrics.get("disagreements", [])
    if limit is not None:
        disagreements = disagreements[:limit]

    # Adaptive retry absorbs Bedrock throttling under concurrency; boto3 low-level
    # clients are thread-safe, so one client is shared across worker threads.
    client = boto3.Session(profile_name=PROFILE, region_name=REGION).client(
        "bedrock-runtime",
        config=Config(retries={"max_attempts": 10, "mode": "adaptive"}, read_timeout=180))

    verdicts = [None] * len(disagreements)
    progress = {"n": 0}
    lock = threading.Lock()

    def _run(item):
        idx, dis = item
        verdict = _judge_pair(client, dis, pool_index)
        verdicts[idx] = verdict
        with lock:
            progress["n"] += 1
            flag = "ok" if verdict["consistent"] else "tie/inconsistent"
            print(f"  [{progress['n']:3d}/{len(disagreements)}] {verdict.get('id')} "
                  f"{verdict['model_a']} vs {verdict['model_b']} -> "
                  f"{verdict['winner']:>3} ({flag})  {verdict['query'][:42]}", flush=True)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(_run, enumerate(disagreements)))

    matrix, model_score = _aggregate(verdicts)
    doc = {
        "meta": {
            "judge_model": JUDGE_MODEL,
            "n_pairs": len(verdicts),
            "method": "pairwise preference, anonymized, BOTH orderings, tie-default",
        },
        "verdicts": verdicts,
        "winrate_matrix": matrix,
        "model_score": model_score,
    }

    out_path = HERE / "judge.json"
    out_path.write_text(json.dumps(doc, indent=2))
    consistent_count = sum(1 for v in verdicts if v["consistent"])
    print(f"\nwrote judge.json  n_pairs={len(verdicts)}  "
          f"consistent={consistent_count}/{len(verdicts)}")
    print("model_score: " + ", ".join(
        f"{m}={s}" for m, s in sorted(model_score.items(), key=lambda kv: -kv[1])))
    return doc


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Pairwise preference LLM-judge over the disagreement set.")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap number of disagreements judged (smoke testing)")
    parser.add_argument("--metrics", default="metrics.json")
    parser.add_argument("--pools", default="pools.json")
    parser.add_argument("--workers", type=int, default=8,
                        help="concurrent disagreements (each = 2 Opus calls); default 8")
    args = parser.parse_args(argv)
    judge(metrics_path=args.metrics, pools_path=args.pools, limit=args.limit, workers=args.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
