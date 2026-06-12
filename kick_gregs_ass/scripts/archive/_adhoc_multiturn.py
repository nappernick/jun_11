"""Inspect full multi-turn outcome records to see if context assembly is sane. Read-only."""
import json, collections, statistics
def load(p): return [json.loads(l) for l in open(p) if l.strip()]
out=[json.loads(l) for l in open("data/bakeoff/outcomes.jsonl") if l.strip()]
# Show all fields present on a multi-turn record
mt=[o for o in out if o.get("turn_type")=="multi"]
print(f"multi outcomes: {len(mt)}; single: {sum(1 for o in out if o.get('turn_type')=='single')}")
print("keys on a multi record:", sorted(mt[0].keys()))

# Show the full record for haiku c15-s03 (the Singapore-visa topic-bleed case)
print("\n=== full haiku c15-s03 record (trimmed answer) ===")
for o in out:
    if o["model"]=="claude-haiku-4.5-converse" and o["item_id"]=="c15-s03":
        d=dict(o); d["answer_text"]=d.get("answer_text","")[:200]
        # drop big arrays
        for k in ("retrieval",): d[k]={kk:(vv[:3] if isinstance(vv,list) else vv) for kk,vv in d[k].items()}
        print(json.dumps(d, indent=2)[:2500])
        break

# Is there any conversation history / prior turns stored on the record?
print("\n=== does any multi record carry prior-turn context fields? ===")
keys_union=set()
for o in mt[:50]: keys_union|=set(o.keys())
print("union of keys across 50 multi records:", sorted(keys_union))

# Group multi items by conversation, show how turns relate (do s-numbers share a conv?)
print("\n=== sample conversation c15 across turns (haiku), query per turn ===")
conv=[(o["item_id"],o.get("query","")[:80],o.get("answerability"),o.get("gold_node_ids",[])) for o in out if o["model"]=="claude-haiku-4.5-converse" and o["item_id"].startswith("c15-s")]
for iid,q,a,g in sorted(set(conv)):
    print(f"  {iid:9s} ans={a:8s} gold={len(g)}  q={q!r}")
