#!/usr/bin/env python3
"""bakeoff.eval_gen — generate a labeled eval set WITHOUT human judging.

Why this exists: there are no human relevance labels for the FAQ corpus and none
are coming. Correctness (nDCG / recall / MRR) needs gold anyway, so we build it
with a hybrid, fully-reproducible pipeline:

  1. STRUCTURAL GOLD. For each FAQ node, an LLM writes a handful of natural
     user questions that node answers. The generating node is gold for its own
     queries by construction (exact binary relevance, zero judgment).

  2. JUDGE AUGMENTATION. Structural gold understates relevance — sibling FAQs
     often also answer a query (overlapping topics, country variants of the same
     policy). For each query we retrieve a BM25 pool and ask an LLM judge which
     pool docs are ALSO relevant, then union those into the gold set. This is a
     model judging a model — stated plainly — but it is reproducible and removes
     the single-gold-doc bias that would crush recall metrics unfairly.

  3. UNANSWERABLE SET. We generate travel-adjacent questions the corpus does NOT
     answer (gold = ∅, answerability = "unanswerable") so the abstention panel
     has real negatives. The judge confirms none of the retrieved pool is truly
     relevant; any query the judge "rescues" is dropped (it wasn't unanswerable).

Output: JSONL of contract.Fixture with FROZEN candidate pools (so every model
reranks identical candidates) and slice tags (english clean|broken, channel
typed|voice). Run is bounded by corpus size (~56 docs); use --limit to smoke.

Auth: alpha creds (see bakeoff.access). LLM: Bedrock converse on alpha.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from bakeoff.access import AossAccess, scope_filter
from bakeoff.contract import Candidate, Fixture

GEN_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"      # cheap: query generation
JUDGE_MODEL = "us.anthropic.claude-sonnet-4-6"                  # stronger: relevance judging
REGION = "us-west-2"

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _strip_fence(text: str) -> str:
    """Bedrock/Claude often wraps JSON in a ```json fence. Strip it."""
    return _FENCE_RE.sub("", text.strip())


def _llm_json(client, model: str, prompt: str, max_tokens: int = 1024, retries: int = 2):
    """Converse call that returns parsed JSON, tolerant of markdown fences."""
    last_err = None
    for attempt in range(retries + 1):
        resp = client.converse(
            modelId=model,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0 if attempt == 0 else 0.3},
        )
        raw = resp["output"]["message"]["content"][0]["text"]
        try:
            return json.loads(_strip_fence(raw))
        except json.JSONDecodeError as e:
            last_err = e
            # last resort: grab the outermost JSON array/object
            m = re.search(r"(\[.*\]|\{.*\})", raw, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(1))
                except json.JSONDecodeError:
                    pass
    raise RuntimeError(f"LLM did not return valid JSON after {retries + 1} tries: {last_err}")


# --- prompts -----------------------------------------------------------------

def _gen_prompt(title: str, text: str, n: int) -> str:
    return (
        f"You are generating realistic employee questions for an HR/travel FAQ search eval.\n"
        f"Below is ONE FAQ article. Write {n} distinct natural-language questions a real "
        f"employee would type or say that THIS article directly answers.\n"
        f"Vary phrasing and specificity. Do not copy the title verbatim. No numbering.\n\n"
        f"TITLE: {title}\n\nARTICLE:\n{text[:4000]}\n\n"
        f'Return ONLY a JSON array of {n} strings.'
    )


def _voice_prompt(query: str) -> str:
    return (
        "Rewrite this typed search query as a messy voice-transcribed utterance: lowercase, "
        "no punctuation, a filler word or two, maybe a minor transcription slip. Keep the meaning.\n\n"
        f'QUERY: {query}\n\nReturn ONLY a JSON object: {{"voice": "..."}}'
    )


def _judge_prompt(query: str, cands: list[Candidate]) -> str:
    docs = "\n\n".join(
        f"[{i}] node={c.node_id}\n{(c.text or '')[:1200]}" for i, c in enumerate(cands)
    )
    return (
        "You are a strict relevance judge for an FAQ retrieval eval.\n"
        "A document is RELEVANT only if it directly answers the user's question (not merely "
        "same general topic). Be conservative.\n\n"
        f"QUESTION: {query}\n\nCANDIDATES:\n{docs}\n\n"
        'Return ONLY a JSON object mapping each relevant candidate index to true, e.g. '
        '{"relevant_indices": [0, 3]}. If none are relevant, return {"relevant_indices": []}.'
    )


def _unanswerable_prompt(titles: list[str], n: int) -> str:
    joined = "\n".join(f"- {t}" for t in titles)
    return (
        "Here are the titles of every article in a travel/HR FAQ corpus:\n"
        f"{joined}\n\n"
        f"Write {n} realistic employee questions that are travel/work-adjacent but that this "
        f"corpus clearly does NOT answer (out of scope, or a topic absent above). They should be "
        f"plausible questions someone might still ask this system.\n"
        f"Return ONLY a JSON array of {n} strings."
    )


# --- pipeline ----------------------------------------------------------------

def generate(
    out_path: Path,
    *,
    queries_per_doc: int = 2,
    pool_size: int = 20,
    limit: int | None = None,
    unanswerable: int = 12,
    add_voice: bool = True,
    profile: str = "alpha",
):
    import boto3

    sess = boto3.Session(profile_name=profile, region_name=REGION)
    br = sess.client("bedrock-runtime")
    acc = AossAccess(profile=profile)

    corpus = acc.fetch_all()
    if limit:
        corpus = corpus[:limit]
    titles = [(c.source_metadata or {}).get("title") or c.node_id for c in corpus]
    print(f"[gen] corpus={len(corpus)} index={acc.index} q/doc={queries_per_doc} pool={pool_size}")

    fixtures: list[Fixture] = []
    qid = 0

    # 1+2: answerable queries with structural gold + judge augmentation
    for di, doc in enumerate(corpus):
        title = (doc.source_metadata or {}).get("title") or doc.node_id
        try:
            queries = _llm_json(br, GEN_MODEL, _gen_prompt(title, doc.text, queries_per_doc))
        except Exception as e:
            print(f"  [skip doc {di}] query-gen failed: {e}")
            continue
        if not isinstance(queries, list):
            continue

        for q in queries:
            q = str(q).strip()
            if not q:
                continue
            pool = freeze_pool(acc, q, pool_size)
            gold = {doc.node_id}
            judged = _judge(br, q, pool)
            gold |= judged
            # only keep gold that is actually in the retrieved pool for retrievable accounting,
            # but record full gold (score_one computes gold_retrievable via intersection).
            fixtures.append(Fixture(
                query_id=f"ans-{qid:04d}", query=q, gold_node_ids=gold,
                candidates=pool, slice={"english": "clean", "channel": "typed"},
                answerability="answerable_retrievable" if (gold & {c.node_id for c in pool})
                              else "answerable_not_retrieved",
            ))
            qid += 1

            if add_voice:
                try:
                    v = _llm_json(br, GEN_MODEL, _voice_prompt(q), max_tokens=200)["voice"]
                except Exception:
                    v = None
                if v:
                    vpool = freeze_pool(acc, v, pool_size)
                    vgold = {doc.node_id} | _judge(br, v, vpool)
                    fixtures.append(Fixture(
                        query_id=f"ans-{qid:04d}", query=v, gold_node_ids=vgold,
                        candidates=vpool, slice={"english": "broken", "channel": "voice"},
                        answerability="answerable_retrievable" if (vgold & {c.node_id for c in vpool})
                                      else "answerable_not_retrieved",
                    ))
                    qid += 1
        print(f"  [doc {di+1}/{len(corpus)}] {title[:48]!r} -> {len(queries)} queries")

    # 3: unanswerable set
    if unanswerable:
        try:
            uqs = _llm_json(br, GEN_MODEL, _unanswerable_prompt(titles, unanswerable), max_tokens=1200)
        except Exception as e:
            print(f"  [unanswerable] gen failed: {e}")
            uqs = []
        kept = 0
        for q in uqs:
            q = str(q).strip()
            if not q:
                continue
            pool = freeze_pool(acc, q, pool_size)
            judged = _judge(br, q, pool)
            if judged:
                # judge rescued it -> not truly unanswerable, drop
                continue
            fixtures.append(Fixture(
                query_id=f"unans-{kept:04d}", query=q, gold_node_ids=set(),
                candidates=pool, slice={"english": "clean", "channel": "typed"},
                answerability="unanswerable",
            ))
            kept += 1
        print(f"  [unanswerable] kept {kept}/{len(uqs)} (judge-confirmed no relevant doc)")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        for fx in fixtures:
            f.write(json.dumps(fx.to_dict()) + "\n")

    _summary(fixtures, out_path)
    return fixtures


def freeze_pool(acc: AossAccess, query: str, size: int) -> list[Candidate]:
    """Unscoped BM25 pool, frozen for replay. (Scope hard-filtering is a separate
    seam; these fixtures carry no level/role/country requester, matching the
    eval-dataset design where answerability is a slice, not a hard filter.)"""
    return acc.search(query, size=size)


def _judge(br, query: str, cands: list[Candidate]) -> set[str]:
    if not cands:
        return set()
    try:
        out = _llm_json(br, JUDGE_MODEL, _judge_prompt(query, cands), max_tokens=300)
        idxs = out.get("relevant_indices", []) if isinstance(out, dict) else []
        return {cands[i].node_id for i in idxs if isinstance(i, int) and 0 <= i < len(cands)}
    except Exception as e:
        print(f"    [judge warn] {type(e).__name__}: {str(e)[:80]} (no augmentation for this query)")
        return set()


def _summary(fixtures: list[Fixture], out_path: Path):
    from collections import Counter
    cls = Counter(f.answerability for f in fixtures)
    sl = Counter((f.slice.get("channel"), f.slice.get("english")) for f in fixtures)
    gold_sizes = [len(f.gold_node_ids) for f in fixtures if f.answerability != "unanswerable"]
    avg_gold = sum(gold_sizes) / len(gold_sizes) if gold_sizes else 0
    print(f"\n[done] {len(fixtures)} fixtures -> {out_path}")
    print(f"  answerability: {dict(cls)}")
    print(f"  slices(channel,english): {dict(sl)}")
    print(f"  avg gold/answerable query: {avg_gold:.2f}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate a labeled FAQ rerank eval set (no human labels).")
    ap.add_argument("-o", "--out", default="bakeoff/sample/eval_fixtures.jsonl")
    ap.add_argument("--queries-per-doc", type=int, default=2)
    ap.add_argument("--pool-size", type=int, default=20)
    ap.add_argument("--limit", type=int, default=None, help="cap docs (smoke testing)")
    ap.add_argument("--unanswerable", type=int, default=12)
    ap.add_argument("--no-voice", action="store_true", help="skip voice/broken-english variants")
    ap.add_argument("--profile", default="alpha")
    args = ap.parse_args(argv)

    t0 = time.perf_counter()
    generate(
        Path(args.out),
        queries_per_doc=args.queries_per_doc,
        pool_size=args.pool_size,
        limit=args.limit,
        unanswerable=args.unanswerable,
        add_voice=not args.no_voice,
        profile=args.profile,
    )
    print(f"[time] {time.perf_counter() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
