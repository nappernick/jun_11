#!/usr/bin/env python3
"""ragas_testset_gen.py — faithful re-implementation of the RAGAS testset-generation
METHODOLOGY directly on Bedrock (the `ragas` library is unusable in this env due to a
langchain conflict, so nothing from ragas/langchain is imported here).

The RAGAS testset-generation pipeline, reproduced step-by-step:

  STEP 1  Knowledge extraction (LLM): for every corpus document, extract a set of
          named ENTITIES and a set of KEYPHRASES. This is RAGAS's
          `NERExtractor` + `KeyphraseExtractor` stage that decorates each node.

  STEP 2  Relationship building (CODE, no LLM): connect documents that share enough
          entities/keyphrases. RAGAS uses a Jaccard-style overlap to build a
          knowledge graph; we rank every document pair by Jaccard overlap of their
          (entities ∪ keyphrases) sets and keep the strongest edges. Multi-hop
          synthesizers later walk these edges to pick 2 related documents.

  STEP 3  Query synthesis (LLM): RAGAS samples a distribution over "query
          synthesizers". We reproduce the four canonical scenario types —
          single-hop specific, single-hop abstract, multi-hop specific, multi-hop
          abstract — and additionally vary PERSONA (new hire / frequent traveler /
          manager / finance) and STYLE (web-search keywords / natural chat). The
          target distribution is decided in CODE (round-robin assignment) so the
          spread is guaranteed; the LLM only writes the query + reference answer for
          each fully-specified scenario. (Opus 4.8 rejects the temperature param, so
          diversity comes from varied inputs, not sampling.)

Output: oss_bakeoff/ragas_testset.json as {meta, samples:[...]} where each sample is
  {id, query, type, persona, style, source_node_ids, reference_contexts,
   reference_answer}.

Usage:
  python ragas_testset_gen.py                  # full run, ~50 samples
  python ragas_testset_gen.py --target 48      # request ~48 samples
  python ragas_testset_gen.py --limit-docs 8   # quick smoke on first 8 docs
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
CORPUS = HERE.parent / "query_chunks.jsonl"
OUT = HERE / "ragas_testset.json"

PROFILE = "alpha"
REGION = "us-west-2"
# Opus 4.8 — used for BOTH extraction and synthesis. NO temperature param (rejected).
MODEL = "us.anthropic.claude-opus-4-8"

# RAGAS scenario taxonomy (the four canonical query-synthesizer families).
TYPES = [
    "single_hop_specific",
    "single_hop_abstract",
    "multi_hop_specific",
    "multi_hop_abstract",
]
PERSONAS = ["new_hire", "frequent_traveler", "manager", "finance"]
STYLES = ["web_search_keywords", "natural_chat"]

PERSONA_DESC = {
    "new_hire": "a brand-new Amazon employee booking corporate travel for the first time, "
                "unfamiliar with the program, jargon, and tools",
    "frequent_traveler": "a seasoned employee who travels constantly, already knows the "
                         "basics, and wants efficient answers about edge cases and limits",
    "manager": "a people-manager who books or approves travel for their team and cares "
               "about approvals, policy exceptions, and oversight",
    "finance": "a finance / expense-focused stakeholder concerned with costs, "
               "reimbursement, receipts, billing, and policy compliance",
}
STYLE_DESC = {
    "web_search_keywords": "terse search-engine-style keywords, lowercase, no full "
                           "sentence, no punctuation (e.g. 'rental car size limit policy')",
    "natural_chat": "a natural, conversational full-sentence question as if typing to a "
                    "chat assistant",
}

# ─────────────────────────── Bedrock plumbing ───────────────────────────
# Mirrors the robust helpers already used in oss_bakeoff/ragas_eval.py.

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_local = threading.local()


def _new_client():
    import boto3
    from botocore.config import Config
    return boto3.Session(profile_name=PROFILE, region_name=REGION).client(
        "bedrock-runtime",
        config=Config(retries={"max_attempts": 10, "mode": "adaptive"}, read_timeout=180),
    )


def client():
    existing = getattr(_local, "c", None)
    if existing is None:
        existing = _local.c = _new_client()
    return existing


def converse(prompt, max_tokens=1500):
    # NOTE: no temperature in inferenceConfig — Opus 4.8 rejects it.
    resp = client().converse(
        modelId=MODEL,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": max_tokens},
    )
    return resp["output"]["message"]["content"][0]["text"]


def parse_json(raw):
    """Tolerant JSON parse: strip code fences, else find the largest JSON span."""
    try:
        return json.loads(_FENCE.sub("", raw.strip()))
    except json.JSONDecodeError:
        pass
    # Try the widest object/array span.
    match = re.search(r"(\{.*\}|\[.*\])", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    # Last resort: scan balanced fragments.
    for cand in reversed(re.findall(r"\{.*?\}|\[.*?\]", raw, re.DOTALL)):
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    raise ValueError(f"could not parse JSON from model output: {raw[:300]!r}")


# ─────────────────────────── corpus ───────────────────────────

def load_corpus(limit=None):
    """Load the corpus, deduplicating by nodeId (keep first occurrence). The corpus
    contains one repeated nodeId; without dedup the relationship graph would pair a
    node with a byte-identical copy of itself (Jaccard 1.0), which is not a valid
    multi-hop pair. RAGAS builds its graph over DISTINCT nodes, so we do the same."""
    docs = []
    seen = set()
    for line in open(CORPUS):
        if not line.strip():
            continue
        rec = json.loads(line)
        node_id = rec["nodeId"]
        if node_id in seen:
            continue
        seen.add(node_id)
        docs.append({
            "node_id": node_id,
            "title": rec.get("title") or rec.get("h1") or "",
            "markdown": rec.get("markdown") or "",
        })
    if limit:
        docs = docs[:limit]
    return docs


# ─────────────────── STEP 1: entity + keyphrase extraction ───────────────────

_EXTRACT_PROMPT = """You are the knowledge-extraction stage of a RAG testset generator.
Given one FAQ document about Amazon's corporate travel program, extract:
  - "entities": specific named things mentioned (tools, systems, policies, suppliers,
    org names, document/portal names, roles, concrete travel concepts). Lowercase,
    deduplicated, 4-12 items.
  - "keyphrases": the salient topical phrases that capture what this document is ABOUT
    (e.g. "rental car size policy", "out of policy booking"). Lowercase, 4-10 items.

Return ONLY a JSON object: {{"entities": [...], "keyphrases": [...]}}

TITLE: {title}

DOCUMENT:
{markdown}
"""


def extract_one(doc):
    raw = converse(_EXTRACT_PROMPT.format(title=doc["title"], markdown=doc["markdown"][:6000]),
                   max_tokens=600)
    data = parse_json(raw)
    entities = [str(x).strip().lower() for x in data.get("entities", []) if str(x).strip()]
    keyphrases = [str(x).strip().lower() for x in data.get("keyphrases", []) if str(x).strip()]
    return entities, keyphrases


def extract_all(docs, workers=8):
    """Thread one extraction call per doc (robust: no batch-truncation risk).
    Retries each doc up to 3 times; asserts coverage for all docs."""
    results = {}

    def work(doc):
        last_err = None
        for _ in range(3):
            try:
                entities, keyphrases = extract_one(doc)
                if entities or keyphrases:
                    return doc["node_id"], entities, keyphrases
            except Exception as err:  # noqa: BLE001
                last_err = err
        raise RuntimeError(f"extraction failed for {doc['node_id']}: {last_err}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, doc): doc for doc in docs}
        for fut in as_completed(futures):
            node_id, entities, keyphrases = fut.result()
            results[node_id] = {"entities": entities, "keyphrases": keyphrases}

    missing = [doc["node_id"] for doc in docs if doc["node_id"] not in results]
    if missing:
        raise RuntimeError(f"missing extractions for {len(missing)} docs: {missing}")
    return results


# ─────────────── STEP 2: relationship graph via Jaccard overlap ───────────────

def jaccard(set_a, set_b):
    if not set_a or not set_b:
        return 0.0
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    return inter / union if union else 0.0


def base_title(title):
    """Strip a trailing region marker so regional variants of the SAME FAQ collapse
    to one base, e.g. 'How Do I Set Up My Travel Profile? (India)' and '(Japan)' ->
    'how do i set up my travel profile?'. Used to detect region-swap pairs, which are
    near-duplicate content and make degenerate multi-hop pairs."""
    stripped = re.sub(r"\s*\([^)]*\)\s*$", "", title)            # '(India)', '(Global Access)'
    stripped = re.sub(r"\s*[-–]\s*[A-Z][A-Za-z ]+$", "", stripped)  # '- Germany', '- Switzerland'
    return stripped.strip().lower()


def build_relationships(docs, extractions):
    """Rank every document pair by Jaccard overlap of (entities ∪ keyphrases).
    Returns a list of pairs sorted strongest-first. Falls back to keyphrase-only
    overlap when the combined sets are too sparse to connect documents."""
    combined = {}
    keyphrase_only = {}
    norm_md = {}
    base = {}
    for doc in docs:
        ext = extractions[doc["node_id"]]
        combined[doc["node_id"]] = set(ext["entities"]) | set(ext["keyphrases"])
        keyphrase_only[doc["node_id"]] = set(ext["keyphrases"])
        norm_md[doc["node_id"]] = re.sub(r"\s+", " ", doc["markdown"]).strip().lower()
        base[doc["node_id"]] = base_title(doc["title"])

    node_ids = [doc["node_id"] for doc in docs]
    pairs = []
    for left_idx in range(len(node_ids)):
        for right_idx in range(left_idx + 1, len(node_ids)):
            left, right = node_ids[left_idx], node_ids[right_idx]
            if left == right:
                continue
            # Cheap deterministic guard: two docs with byte-identical normalized
            # bodies are not a valid multi-hop pair (no second hop to make).
            if norm_md[left] and norm_md[left] == norm_md[right]:
                continue
            score_combined = jaccard(combined[left], combined[right])
            score_keyphrase = jaccard(keyphrase_only[left], keyphrase_only[right])
            # Primary signal is combined overlap; keyphrase overlap is the fallback
            # so two docs that are clearly the SAME topic still connect even if their
            # named entities differ.
            score = max(score_combined, score_keyphrase)
            shared = sorted((combined[left] & combined[right]))
            if score > 0:
                pairs.append({
                    "a": left,
                    "b": right,
                    "score": round(score, 4),
                    "score_combined": round(score_combined, 4),
                    "score_keyphrase": round(score_keyphrase, 4),
                    "shared": shared,
                    # same_base => regional variants of one FAQ (near-duplicate text);
                    # cross-topic pairs (False) make stronger, genuinely 2-hop queries.
                    "same_base": base[left] == base[right],
                })
    pairs.sort(key=lambda p: p["score"], reverse=True)
    return pairs


# ─────────────────── STEP 3: scenario specs (code-driven) ───────────────────

def build_specs(docs, pairs, target):
    """Decide the FULL scenario distribution in code so the spread is guaranteed.
    Round-robin over type, persona, style; assign source doc(s).

    Single-hop scenarios get one doc (cycled across the corpus for coverage).
    Multi-hop scenarios get a related doc PAIR drawn from the strongest graph edges.

    Returns (specs, spare_pairs). spare_pairs are the ranked-but-unused edges; the
    synthesizer uses them to BACKFILL any multi-hop scenario whose LLM call fails or
    refuses (e.g. because the chosen pair turned out to be near-duplicate text), so a
    single refusal never costs us a sample.
    """
    single_types = ["single_hop_specific", "single_hop_abstract"]
    multi_types = ["multi_hop_specific", "multi_hop_abstract"]

    # Roughly half single-hop, half multi-hop. Multi-hop is capped by available pairs.
    n_multi = min(target // 2, len(pairs))
    n_single = target - n_multi

    specs = []
    persona_cycle = 0
    style_cycle = 0

    # Single-hop: cycle through docs for broad corpus coverage.
    doc_cycle = 0
    for index in range(n_single):
        doc = docs[doc_cycle % len(docs)]
        doc_cycle += 1
        spec = {
            "type": single_types[index % len(single_types)],
            "persona": PERSONAS[persona_cycle % len(PERSONAS)],
            "style": STYLES[style_cycle % len(STYLES)],
            "source_node_ids": [doc["node_id"]],
        }
        persona_cycle += 1
        style_cycle += 1
        specs.append(spec)

    # Multi-hop edge ORDER: prefer CROSS-TOPIC pairs (different base FAQ) — these make
    # genuine 2-hop queries — and keep region-swap pairs (same base FAQ across
    # countries) only as a fallback, since those are near-duplicate content that tends
    # to produce degenerate "multi-hop" queries answerable from one doc. Within each
    # group we keep the strongest Jaccard edges first.
    cross_topic = [p for p in pairs if not p["same_base"]]
    region_swap = [p for p in pairs if p["same_base"]]
    ordered = cross_topic + region_swap

    # Walk the ordered edges. Avoid reusing the exact same pair, and avoid reusing the
    # same UNORDERED doc set so the multi-hop set stays topically varied.
    used_pairs = set()
    made = 0
    consumed = 0
    for pair in ordered:
        if made >= n_multi:
            break
        consumed += 1
        key = (pair["a"], pair["b"])
        if key in used_pairs:
            continue
        used_pairs.add(key)
        spec = {
            "type": multi_types[made % len(multi_types)],
            "persona": PERSONAS[persona_cycle % len(PERSONAS)],
            "style": STYLES[style_cycle % len(STYLES)],
            "source_node_ids": [pair["a"], pair["b"]],
            "shared": pair["shared"],
        }
        persona_cycle += 1
        style_cycle += 1
        specs.append(spec)
        made += 1

    # Remaining edges (in the same cross-topic-first order) become the backfill reserve.
    spare_pairs = [p for p in ordered if (p["a"], p["b"]) not in used_pairs]
    return specs, spare_pairs


_SINGLE_PROMPT = """You are the query-synthesis stage of a RAGAS-style RAG testset generator.
Produce ONE test query and its grounded reference answer from a SINGLE source document.

Scenario type: {qtype}
  - "single_hop_specific": a concrete, factual question whose answer is a specific
    detail stated in the document (a number, name, rule, or step).
  - "single_hop_abstract": a broader "why / what is the purpose / how does X work"
    question that the document explains conceptually.

Persona: {persona} — {persona_desc}
Write the query the way THIS persona would actually ask it.

Style: {style} — {style_desc}
The query text MUST follow this style exactly.

Rules:
  - The query must be answerable from the document below — do not invent facts.
  - The reference answer must be grounded ONLY in the document, concise, and correct.

Return ONLY JSON: {{"query": "...", "reference_answer": "..."}}

SOURCE DOCUMENT (title: {title}):
{markdown}
"""

_MULTI_PROMPT = """You are the query-synthesis stage of a RAGAS-style RAG testset generator.
Produce ONE MULTI-HOP test query that can only be answered by combining information
from BOTH source documents below, plus its grounded reference answer.

Scenario type: {qtype}
  - "multi_hop_specific": a concrete question whose answer requires a specific fact
    from Document A AND a specific fact from Document B (e.g. comparing or combining
    two rules/limits/steps that live in different documents).
  - "multi_hop_abstract": a broader conceptual question that synthesizes the themes of
    both documents into one answer.

Persona: {persona} — {persona_desc}
Write the query the way THIS persona would actually ask it.

Style: {style} — {style_desc}
The query text MUST follow this style exactly.

These two documents are topically related; they share: {shared}

CRITICAL multi-hop rules:
  - The query MUST genuinely require BOTH documents. It must NOT be fully answerable
    from either document alone.
  - Do NOT just bolt two unrelated questions together with "and"; ask ONE coherent
    question whose answer needs facts from both.
  - The reference answer must integrate information from BOTH documents and be grounded
    ONLY in them — do not invent facts.

Return ONLY JSON: {{"query": "...", "reference_answer": "..."}}

DOCUMENT A (title: {title_a}):
{markdown_a}

DOCUMENT B (title: {title_b}):
{markdown_b}
"""


def synthesize_one(spec, doc_by_id):
    is_multi = len(spec["source_node_ids"]) >= 2
    if is_multi:
        doc_a = doc_by_id[spec["source_node_ids"][0]]
        doc_b = doc_by_id[spec["source_node_ids"][1]]
        prompt = _MULTI_PROMPT.format(
            qtype=spec["type"],
            persona=spec["persona"], persona_desc=PERSONA_DESC[spec["persona"]],
            style=spec["style"], style_desc=STYLE_DESC[spec["style"]],
            shared=", ".join(spec.get("shared", [])) or "(related topic)",
            title_a=doc_a["title"], markdown_a=doc_a["markdown"][:4000],
            title_b=doc_b["title"], markdown_b=doc_b["markdown"][:4000],
        )
        contexts = [doc_a["markdown"], doc_b["markdown"]]
    else:
        doc = doc_by_id[spec["source_node_ids"][0]]
        prompt = _SINGLE_PROMPT.format(
            qtype=spec["type"],
            persona=spec["persona"], persona_desc=PERSONA_DESC[spec["persona"]],
            style=spec["style"], style_desc=STYLE_DESC[spec["style"]],
            title=doc["title"], markdown=doc["markdown"][:5000],
        )
        contexts = [doc["markdown"]]

    for _ in range(2):
        raw = converse(prompt, max_tokens=900)
        # A refusal (the model judges the pair invalid, e.g. near-duplicate docs) is a
        # PERMANENT verdict about this pair, not a transient error. Surface it so the
        # caller can backfill with a different pair instead of burning retries.
        try:
            data = parse_json(raw)
        except Exception:  # noqa: BLE001
            if _looks_like_refusal(raw):
                return None
            continue  # transient parse hiccup; retry once
        query = str(data.get("query", "")).strip()
        answer = str(data.get("reference_answer", "")).strip()
        if query and answer:
            return query, answer, contexts
    return None  # gave up on this spec


_REFUSAL_HINTS = ("i can't", "i cannot", "cannot produce", "can't produce",
                  "identical", "are the same", "not possible", "unable to")


def _looks_like_refusal(text):
    low = text.lower()
    return any(hint in low for hint in _REFUSAL_HINTS)


def synthesize_all(specs, spare_pairs, doc_by_id, workers=8):
    """Synthesize one sample per spec, with SKIP-AND-BACKFILL robustness:
      - a failed single-hop spec is simply dropped (we have plenty of docs);
      - a failed multi-hop spec pulls the next unused edge from `spare_pairs` and
        retries with the SAME type/persona/style, so a refused near-duplicate pair
        does not cost us a multi-hop sample.
    One refusal never aborts the run. Returns the list of successful samples."""
    spare_lock = threading.Lock()
    spare_iter = iter(spare_pairs)

    def next_spare(seen_pairs):
        with spare_lock:
            for pair in spare_iter:
                key = (pair["a"], pair["b"])
                if key not in seen_pairs:
                    seen_pairs.add(key)
                    return pair
        return None

    def work(spec):
        is_multi = len(spec["source_node_ids"]) >= 2
        result = synthesize_one(spec, doc_by_id)
        if result is None and is_multi:
            # Backfill: try replacement edges until one works (bounded attempts).
            seen = {tuple(spec["source_node_ids"])}
            for _ in range(8):
                pair = next_spare(seen)
                if pair is None:
                    break
                alt = dict(spec)
                alt["source_node_ids"] = [pair["a"], pair["b"]]
                alt["shared"] = pair["shared"]
                result = synthesize_one(alt, doc_by_id)
                if result is not None:
                    spec = alt
                    break
        if result is None:
            return None
        query, answer, contexts = result
        return {
            "query": query,
            "type": spec["type"],
            "persona": spec["persona"],
            "style": spec["style"],
            "source_node_ids": spec["source_node_ids"],
            "reference_contexts": contexts,
            "reference_answer": answer,
        }

    samples = []
    failures = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, spec) for spec in specs]
        for fut in as_completed(futures):
            sample = fut.result()
            if sample is None:
                failures += 1
            else:
                samples.append(sample)
    if failures:
        print(f"[step 3] {failures} scenario(s) dropped after backfill exhausted", flush=True)
    # Stable, deterministic ids assigned after collection.
    for index, sample in enumerate(samples):
        sample["id"] = f"ragas-{index:03d}"
    # Reorder fields so id is first.
    samples = [{"id": s["id"], **{k: v for k, v in s.items() if k != "id"}} for s in samples]
    return samples


# ─────────────────────────── main ───────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=int, default=50,
                        help="approximate number of samples to generate (default 50)")
    parser.add_argument("--limit-docs", type=int, default=None,
                        help="only use the first N corpus docs (smoke testing)")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    docs = load_corpus(limit=args.limit_docs)
    doc_by_id = {doc["node_id"]: doc for doc in docs}
    print(f"[corpus] loaded {len(docs)} documents", flush=True)

    print("[step 1] extracting entities + keyphrases (LLM, threaded)...", flush=True)
    extractions = extract_all(docs, workers=args.workers)
    total_entities = sum(len(v["entities"]) for v in extractions.values())
    total_keyphrases = sum(len(v["keyphrases"]) for v in extractions.values())
    print(f"[step 1] done: {len(extractions)} docs, {total_entities} entities, "
          f"{total_keyphrases} keyphrases", flush=True)

    print("[step 2] building relationship graph (Jaccard overlap, code)...", flush=True)
    pairs = build_relationships(docs, extractions)
    print(f"[step 2] {len(pairs)} connected document pairs (score>0)", flush=True)
    print("[step 2] top 8 strongest edges:", flush=True)
    for pair in pairs[:8]:
        title_a = doc_by_id[pair["a"]]["title"][:38]
        title_b = doc_by_id[pair["b"]]["title"][:38]
        print(f"         score={pair['score']:.3f}  shared={pair['shared'][:4]}  "
              f"| {title_a}  <->  {title_b}", flush=True)

    n_cross = sum(1 for p in pairs if not p["same_base"])
    n_swap = sum(1 for p in pairs if p["same_base"])
    print(f"[step 2] cross-topic edges: {n_cross}, region-swap edges: {n_swap}", flush=True)

    print("[step 3] planning scenario distribution (code-driven)...", flush=True)
    specs, spare_pairs = build_specs(docs, pairs, args.target)
    multi_specs = [s for s in specs if len(s["source_node_ids"]) >= 2]
    swap_in_plan = 0
    for spec in multi_specs:
        ta, tb = (doc_by_id[n]["title"] for n in spec["source_node_ids"])
        if base_title(ta) == base_title(tb):
            swap_in_plan += 1
    print(f"[step 3] planned {len(specs)} scenarios "
          f"({len(spare_pairs)} spare edges held for backfill); "
          f"multi-hop region-swaps in plan: {swap_in_plan}/{len(multi_specs)}", flush=True)

    print("[step 3] synthesizing queries + reference answers (LLM, threaded)...", flush=True)
    samples = synthesize_all(specs, spare_pairs, doc_by_id, workers=args.workers)
    print(f"[step 3] synthesized {len(samples)} samples", flush=True)

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "methodology": "RAGAS testset generation (entity/keyphrase extraction -> "
                       "Jaccard relationship graph -> diverse scenario synthesis), "
                       "re-implemented directly on Bedrock (no ragas/langchain).",
        "gen_model": MODEL,
        "corpus": str(CORPUS),
        "n_corpus_docs": len(docs),
        "n_connected_pairs": len(pairs),
        "n_samples": len(samples),
        "types": TYPES,
        "personas": PERSONAS,
        "styles": STYLES,
    }
    OUT.write_text(json.dumps({"meta": meta, "samples": samples}, indent=2))
    print(f"[out] wrote {len(samples)} samples to {OUT}", flush=True)

    # ─── validation ───
    print("\n========== VALIDATION ==========", flush=True)
    from collections import Counter
    type_counts = Counter(s["type"] for s in samples)
    persona_counts = Counter(s["persona"] for s in samples)
    style_counts = Counter(s["style"] for s in samples)

    print(f"total samples: {len(samples)}", flush=True)
    print("\nTYPE counts:", flush=True)
    for qtype in TYPES:
        print(f"  {qtype:24s} {type_counts.get(qtype, 0)}", flush=True)
    print("\nPERSONA counts:", flush=True)
    for persona in PERSONAS:
        print(f"  {persona:24s} {persona_counts.get(persona, 0)}", flush=True)
    print("\nSTYLE counts:", flush=True)
    for style in STYLES:
        print(f"  {style:24s} {style_counts.get(style, 0)}", flush=True)

    # Hard assertions that DEFEND the requirement (not just describe it).
    assert len(samples) >= 40, f"expected >=40 samples, got {len(samples)}"
    for qtype in TYPES:
        assert type_counts.get(qtype, 0) > 0, f"no samples for type {qtype}"
    assert len([p for p in PERSONAS if persona_counts.get(p, 0) > 0]) >= 2, \
        "expected multiple personas"
    assert len([s for s in STYLES if style_counts.get(s, 0) > 0]) >= 2, \
        "expected multiple styles"
    # Every multi-hop sample MUST draw from 2+ docs.
    multi = [s for s in samples if s["type"].startswith("multi_hop")]
    for sample in multi:
        assert len(sample["source_node_ids"]) >= 2, \
            f"multi-hop sample {sample['id']} has <2 source docs"
        assert len(sample["reference_contexts"]) >= 2, \
            f"multi-hop sample {sample['id']} has <2 reference contexts"
    print(f"\n[assert] OK: {len(multi)} multi-hop samples all draw from >=2 docs", flush=True)

    # Quality report on the multi-hop set: how many are region-swaps (near-duplicate
    # FAQs across countries) vs genuine cross-topic 2-hops, and how many distinct
    # underlying topics back them.
    swaps = 0
    distinct_pairs = set()
    distinct_topics = set()
    for sample in multi:
        ta, tb = (doc_by_id[n]["title"] for n in sample["source_node_ids"])
        distinct_pairs.add(tuple(sorted(sample["source_node_ids"])))
        distinct_topics.add(base_title(ta))
        distinct_topics.add(base_title(tb))
        if base_title(ta) == base_title(tb):
            swaps += 1
    print(f"[multi-hop quality] region-swap pairs: {swaps}/{len(multi)}, "
          f"cross-topic: {len(multi) - swaps}/{len(multi)}, "
          f"distinct doc-pairs: {len(distinct_pairs)}, "
          f"distinct base-topics covered: {len(distinct_topics)}", flush=True)

    # Spot-check: show several multi-hop queries by eye so a human can confirm they need
    # both docs. Prefer cross-topic samples (where the two source titles differ) since
    # those are the strongest evidence of genuine 2-hop reasoning.
    cross_samples = [s for s in multi
                     if base_title(doc_by_id[s["source_node_ids"][0]]["title"])
                     != base_title(doc_by_id[s["source_node_ids"][1]]["title"])]
    spot = (cross_samples + multi)[:5]
    print("\n--- spot-check (5 multi-hop queries, cross-topic first) ---", flush=True)
    for sample in spot:
        titles = [doc_by_id[nid]["title"] for nid in sample["source_node_ids"]]
        print(f"\n  [{sample['id']}] type={sample['type']} persona={sample['persona']} "
              f"style={sample['style']}", flush=True)
        print(f"  sources: {titles}", flush=True)
        print(f"  query:   {sample['query']}", flush=True)
        print(f"  answer:  {sample['reference_answer'][:400]}", flush=True)

    # Re-read the file from disk to prove the deliverable on disk matches (guards
    # against a stale earlier output being mistaken for the result).
    on_disk = json.loads(OUT.read_text())
    assert len(on_disk["samples"]) == len(samples), \
        f"on-disk sample count {len(on_disk['samples'])} != in-memory {len(samples)}"
    assert len(on_disk["samples"]) >= 40, \
        f"on-disk file has only {len(on_disk['samples'])} samples"
    print(f"[assert] OK: file on disk has {len(on_disk['samples'])} samples", flush=True)

    print("\n[done] validation passed.", flush=True)


if __name__ == "__main__":
    main()
