"""Pull concrete full multi-turn over-refusals and 'none' non-regex-matches to verify interpretation. Read-only."""
import json, collections, re, statistics
def load(p): return [json.loads(l) for l in open(p) if l.strip()]
js = load("data/bakeoff/judge_scores.jsonl")
out_idx = collections.defaultdict(list)
for o in (json.loads(l) for l in open("data/bakeoff/outcomes.jsonl") if l.strip()):
    out_idx[(o["model"], o["item_id"])].append(o)
REFUSE = re.compile(r"don't have|do not have|not have (that|enough) inform|outside (my|the) (scope|coverage|area)|isn't (in|covered)|not (in|covered) (in )?(my|the) (reference|resource|fragment)|can't (say|give|tell|advise|answer)|genuinely don't|don't have anything", re.I)
def match(r):
    for o in out_idx.get((r["model"], r["item_id"]), []):
        if o.get("answer_text","")[:60]==r.get("answer_excerpt","")[:60]: return o
    return None

print("########## A) THREE FULL MULTI-TURN OVER-REFUSALS (full ans, gold retrieved, model refused, low score) ##########")
shown=0
for r in js:
    if r['answerability']!='full': continue
    o=match(r)
    if not o: continue
    gold=set(o.get('gold_node_ids',[])); ret=set(o.get('retrieval',{}).get('fragment_ids',[]))
    vals=[r['judge'][d] for d in ('faithfulness','correctness','completeness') if r['judge'].get(d) is not None]
    if not vals or statistics.mean(vals)>=0.3: continue
    if not (gold&ret) or not REFUSE.search(o.get('answer_text','')): continue
    if not (o['item_id'].startswith('c') and '-s' in o['item_id']): continue
    shown+=1
    print(f"\n--- {r['model']} {o['item_id']} turn_type={o.get('turn_type')} score={statistics.mean(vals):.2f} judge={r['judge']} ---")
    print(f"QUERY: {o.get('query','')!r}")
    print(f"gold={list(gold)}  topconf={max(o['retrieval']['confidence']) if o['retrieval'].get('confidence') else None}")
    print(f"ANSWER: {o.get('answer_text','')[:380]!r}")
    print(f"JUDGE EVIDENCE: {r.get('evidence',{})}")
    if shown>=3: break

print("\n\n########## B) SONNET-ON 'none' items my REFUSE regex did NOT match (are these real non-refusals?) ##########")
shown=0
for r in js:
    if r['model']!='claude-sonnet-4.6-thinking-on-converse' or r['answerability']!='none': continue
    o=match(r)
    if not o: continue
    if REFUSE.search(o.get('answer_text','')): continue
    vals=[r['judge'][d] for d in ('faithfulness','correctness','completeness') if r['judge'].get(d) is not None]
    shown+=1
    print(f"\n--- {o['item_id']} triad={statistics.mean(vals):.2f} ---")
    print(f"QUERY: {o.get('query','')!r}")
    print(f"ANSWER: {o.get('answer_text','')[:300]!r}")
    if shown>=6: break
print(f"\n(total sonnet-on 'none' rows not matched by regex: {sum(1 for r in js if r['model']=='claude-sonnet-4.6-thinking-on-converse' and r['answerability']=='none' and (match(r) is None or not REFUSE.search(match(r).get('answer_text',''))))})")
