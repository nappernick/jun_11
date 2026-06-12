"""Read-only deep-dive over judge_scores.jsonl + outcomes.jsonl. No writes to data."""
import json, collections, statistics

JS = "data/bakeoff/judge_scores.jsonl"
OUT = "data/bakeoff/outcomes.jsonl"
DIMS3 = ["faithfulness","correctness","completeness"]

def load(path):
    return [json.loads(l) for l in open(path) if l.strip()]

js = load(JS)
models = sorted({r["model"] for r in js})
short = lambda m: m.replace("claude-","").replace("-converse","")

# ---- per (model, answerability) means + counts ----
print("=== JUDGE TRIAD MEANS BY (MODEL, ANSWERABILITY) ===")
cell = collections.defaultdict(lambda: collections.defaultdict(list))
n = collections.Counter()
for r in js:
    a = r["answerability"]; m = r["model"]; n[(m,a)] += 1
    for d in DIMS3:
        v = r["judge"].get(d)
        if v is not None: cell[(m,a)][d].append(v)

ans_order = ["full","partial","none"]
print(f"{'model':40s}{'ans':9s}{'n':>5s}" + "".join(f"{d:>14s}" for d in DIMS3) + f"{'mean3':>10s}")
for m in models:
    for a in ans_order:
        vals3 = [statistics.mean(cell[(m,a)][d]) for d in DIMS3 if cell[(m,a)][d]]
        row = f"{short(m):40s}{a:9s}{n[(m,a)]:>5d}"
        for d in DIMS3:
            xs = cell[(m,a)][d]
            row += f"{statistics.mean(xs):>14.3f}" if xs else f"{'--':>14s}"
        row += f"{statistics.mean(vals3):>10.3f}" if vals3 else f"{'--':>10s}"
        print(row)
    print()

# ---- score distribution: how often is each dim exactly 0 / 1 / mid ----
print("=== FAITHFULNESS SCORE DISTRIBUTION (per model) ===")
for m in models:
    f = [r["judge"].get("faithfulness") for r in js if r["model"]==m and r["judge"].get("faithfulness") is not None]
    buckets = collections.Counter()
    for v in f:
        if v <= 0.001: buckets["0.0"]+=1
        elif v >= 0.999: buckets["1.0"]+=1
        elif v < 0.5: buckets["<0.5"]+=1
        else: buckets["0.5-0.99"]+=1
    print(f"{short(m):40s} n={len(f):4d}  " + "  ".join(f"{k}:{buckets[k]}" for k in ['0.0','<0.5','0.5-0.99','1.0']))

# ---- worst items: lowest mean-triad per model ----
print("\n=== LOWEST-SCORING ITEMS (mean triad) PER MODEL (bottom 5) ===")
for m in models:
    rows=[]
    for r in js:
        if r["model"]!=m: continue
        vals=[r["judge"][d] for d in DIMS3 if r["judge"].get(d) is not None]
        if vals: rows.append((statistics.mean(vals), r["item_id"], r["answerability"], r.get("momentary_state",""), r.get("answer_excerpt","")[:90]))
    rows.sort()
    print(f"\n--- {short(m)} ---")
    for mean,iid,a,ms,ex in rows[:5]:
        print(f"  {mean:.2f} {iid:10s} ans={a:8s} state={ms:12s} | {ex!r}")
