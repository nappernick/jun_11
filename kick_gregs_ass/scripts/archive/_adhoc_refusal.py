"""Root-cause the low-scoring 'full' items: retrieval miss vs generation refusal.
Read-only join of judge_scores.jsonl to outcomes.jsonl on (model,item_id)."""
import json, collections, statistics, re

def load(p): return [json.loads(l) for l in open(p) if l.strip()]
js = load("data/bakeoff/judge_scores.jsonl")
short = lambda m: m.replace("claude-","").replace("-converse","")

# Build outcomes index: (model,item_id) -> list of outcome dicts (may be multiple reps/turns)
out_idx = collections.defaultdict(list)
for o in (json.loads(l) for l in open("data/bakeoff/outcomes.jsonl") if l.strip()):
    out_idx[(o["model"], o["item_id"])].append(o)

# refusal detector on answer text
REFUSE = re.compile(r"don't have|do not have|outside (my|the)|not have that information|don't have any information|outside my (scope|coverage)|genuinely don't", re.I)

print("=== 'full'-answerability judge rows with mean-triad < 0.4: retrieval context check ===")
DIMS3=["faithfulness","correctness","completeness"]
rows=[]
for r in js:
    if r["answerability"]!="full": continue
    vals=[r["judge"][d] for d in DIMS3 if r["judge"].get(d) is not None]
    if not vals: continue
    mt=statistics.mean(vals)
    if mt>=0.4: continue
    m=r["model"]; iid=r["item_id"]
    outs=out_idx.get((m,iid),[])
    # find an outcome whose answer matches the judged excerpt, else take any with gold
    gold=None; nret=None; conf=None; refused=None; answ=r.get("answer_excerpt","")
    chosen=None
    for o in outs:
        if o.get("answer_text","")[:60]==answ[:60]:
            chosen=o; break
    if chosen is None and outs: chosen=outs[0]
    if chosen:
        gold=chosen.get("gold_node_ids",[])
        ret=chosen.get("retrieval",{})
        nret=len(ret.get("fragment_ids",[]))
        confs=ret.get("confidence",[])
        conf=max(confs) if confs else None
        gold_in_ret=bool(set(gold)&set(ret.get("fragment_ids",[]))) if gold else None
    refused=bool(REFUSE.search(answ))
    rows.append((mt,short(m),iid,refused,gold_in_ret if chosen else None,(gold or []),conf,answ[:70]))

print(f"{'mt':>4s} {'model':24s}{'item':10s}{'refuse':6s}{'goldInRet':10s}{'#gold':6s}{'topconf':8s} excerpt")
for mt,m,iid,refused,gir,gold,conf,ex in sorted(rows):
    print(f"{mt:>4.2f} {m:24s}{iid:10s}{str(refused):6s}{str(gir):10s}{len(gold):>5d}  {('%.2f'%conf) if conf is not None else '   --':>7s}  {ex!r}")

# Summary: of the low full items, how many refused AND had gold in retrieval (true gen failure)
true_gen_fail = sum(1 for mt,m,iid,refused,gir,gold,conf,ex in rows if refused and gir)
refused_total = sum(1 for *_ ,refused,gir,gold,conf,ex in [(r[0],r[1],r[2],r[3],r[4],r[5],r[6],r[7]) for r in rows] if refused)
print(f"\nlow-'full' rows: {len(rows)} | refused: {sum(1 for r in rows if r[3])} | refused WITH gold-in-retrieval (generation failure): {true_gen_fail}")
print("(gold-in-retrieval True + refused = model HAD the answer fragment and declined anyway)")
