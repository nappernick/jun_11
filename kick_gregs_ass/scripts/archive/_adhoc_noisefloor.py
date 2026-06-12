"""Estimate the judge-score noise floor to ground a 'significant gain' threshold.
Read-only. Uses the existing bake-off judge_scores.jsonl as the best on-domain
estimate of per-item judge variance for the SAME Opus judge + triad dims."""
import json, collections, statistics, math
def load(p): return [json.loads(l) for l in open(p) if l.strip()]
js = load("data/bakeoff/judge_scores.jsonl")
DIMS=["faithfulness","correctness","completeness"]

def triad(r):
    v=[r["judge"][d] for d in DIMS if r["judge"].get(d) is not None]
    return statistics.mean(v) if v else None

# Multi-turn items only (c<conv>-s<turn>): these are the regime the quality loop tunes on.
multi=[r for r in js if r["item_id"].startswith("c") and "-s" in r["item_id"]]
# Per-conversation cluster: group turn-level triad by conversation id (c15 from c15-s03)
def conv_id(iid): return iid.split("-s")[0]

for label, rows in [("ALL multi (3 models pooled)", multi)]:
    per_turn=[triad(r) for r in rows if triad(r) is not None]
    # conversation-level means (cluster)
    byconv=collections.defaultdict(list)
    for r in rows:
        t=triad(r)
        if t is not None: byconv[(r["model"],conv_id(r["item_id"]))].append(t)
    conv_means=[statistics.mean(v) for v in byconv.values()]
    print(f"=== {label} ===")
    print(f"  turn-level n={len(per_turn)}  mean={statistics.mean(per_turn):.3f}  sd={statistics.pstdev(per_turn):.3f}")
    print(f"  conv-level  n={len(conv_means)} mean={statistics.mean(conv_means):.3f}  sd={statistics.pstdev(conv_means):.3f}")
    sd=statistics.pstdev(conv_means)
    # CI half-width (95%) on the slice mean for various #conversations in tuning slice, reps fold in as more clusters-ish
    print(f"  between-conversation SD (the noise that matters) = {sd:.3f}")
    for nconv in [30,60,120,250]:
        # naive SEM treating each conversation-mean as one observation; reps reduce within-cluster only
        hw=1.96*sd/math.sqrt(nconv)
        print(f"    slice={nconv:3d} convs -> 95% CI half-width on slice mean ≈ ±{hw:.3f}  (min detectable gain ~ {2*hw:.3f})")
print()
print("Interpretation: a candidate prompt's measured gain must exceed the CI half-width")
print("to be a REAL improvement and not slice noise. Threshold should sit at/above that.")
