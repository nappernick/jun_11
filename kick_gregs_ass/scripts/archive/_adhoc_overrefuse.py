"""Rigorous over-refusal + abstention metrics. Read-only join.
over-refusal = on a 'full' item, model refused even though gold fragment was retrieved.
abstention-correct = on a 'none' item, model correctly declined."""
import json, collections, statistics, re

def load(p): return [json.loads(l) for l in open(p) if l.strip()]
js = load("data/bakeoff/judge_scores.jsonl")
short = lambda m: m.replace("claude-","").replace("-converse","")

out_idx = collections.defaultdict(list)
for o in (json.loads(l) for l in open("data/bakeoff/outcomes.jsonl") if l.strip()):
    out_idx[(o["model"], o["item_id"])].append(o)

REFUSE = re.compile(r"don't have|do not have|don't have any information|not have (that|enough) inform|outside (my|the) (scope|coverage|area)|isn't (in|covered)|not (in|covered) (in )?(my|the) (reference|resource|fragment)|can't (say|give|tell|advise|answer)|genuinely don't|don't have anything", re.I)

def match_outcome(r):
    outs = out_idx.get((r["model"], r["item_id"]), [])
    ex = r.get("answer_excerpt","")
    for o in outs:
        if o.get("answer_text","")[:60]==ex[:60]:
            return o, True
    return (outs[0], False) if outs else (None, False)

# join quality
matched=exact=0
for r in js:
    o,exact_hit=match_outcome(r); matched+= o is not None; exact+=exact_hit
print(f"join: {matched}/{len(js)} judge rows matched to an outcome; {exact} exact-excerpt matches")

# Over-refusal on FULL items
print("\n=== OVER-REFUSAL ON 'full' ITEMS (refused despite gold-in-context) ===")
print(f"{'model':24s}{'#full':>6s}{'gold_in_ctx':>12s}{'refused&gold':>14s}{'overrefuse%':>12s}{'mean_faith':>11s}")
for m in sorted({r['model'] for r in js}):
    full=[r for r in js if r['model']==m and r['answerability']=='full']
    gic=0; orf=0; faiths=[]
    for r in full:
        o,_=match_outcome(r)
        gold=set(o.get('gold_node_ids',[])) if o else set()
        ret=set(o.get('retrieval',{}).get('fragment_ids',[])) if o else set()
        has=bool(gold& ret)
        gic+=has
        ref=bool(REFUSE.search(r.get('answer_excerpt','')))
        if ref and has: orf+=1
        if r['judge'].get('faithfulness') is not None: faiths.append(r['judge']['faithfulness'])
    den=gic if gic else 1
    print(f"{short(m):24s}{len(full):>6d}{gic:>12d}{orf:>14d}{100*orf/den:>11.1f}%{statistics.mean(faiths):>11.3f}")

# Abstention correctness on NONE items: did model refuse (correct) and score high?
print("\n=== ABSTENTION ON 'none' ITEMS (should refuse; high score = clean refusal) ===")
print(f"{'model':24s}{'#none':>6s}{'refused%':>10s}{'mean_triad':>11s}")
for m in sorted({r['model'] for r in js}):
    none=[r for r in js if r['model']==m and r['answerability']=='none']
    ref=0; triads=[]
    for r in none:
        if REFUSE.search(r.get('answer_excerpt','')): ref+=1
        vals=[r['judge'][d] for d in ('faithfulness','correctness','completeness') if r['judge'].get(d) is not None]
        if vals: triads.append(statistics.mean(vals))
    print(f"{short(m):24s}{len(none):>6d}{100*ref/len(none):>9.1f}%{statistics.mean(triads):>11.3f}")

# Single vs multi turn split (item_id pattern: c<conv>-s<turn> is multi; b<batch>-q<n> single)
print("\n=== SINGLE vs MULTI-TURN (mean triad) ===")
print(f"{'model':24s}{'single_n':>9s}{'single':>8s}{'multi_n':>9s}{'multi':>8s}")
for m in sorted({r['model'] for r in js}):
    s=[]; mu=[]
    for r in js:
        if r['model']!=m: continue
        vals=[r['judge'][d] for d in ('faithfulness','correctness','completeness') if r['judge'].get(d) is not None]
        if not vals: continue
        t=statistics.mean(vals)
        iid=r['item_id']
        (mu if iid.startswith('c') and '-s' in iid else s).append(t)
    sm = statistics.mean(s) if s else float('nan')
    mm = statistics.mean(mu) if mu else float('nan')
    print(f"{short(m):24s}{len(s):>9d}{sm:>8.3f}{len(mu):>9d}{mm:>8.3f}")
