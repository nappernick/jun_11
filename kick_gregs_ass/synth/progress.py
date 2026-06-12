"""
A glanceable progress readout so the run is VISIBLE, not just a pile of JSONL.
Prints to the terminal AND writes data/synthetic/PROGRESS.md so you can open it.

Run:  python3 -m synth.progress
"""
import json
import csv
import sys
from collections import Counter, defaultdict

QUERIES = "data/synthetic/queries.jsonl"
CONVOS = "data/synthetic/conversations.jsonl"
LEDGER = "data/synthetic/perspectives_ledger.jsonl"
INDEX = "data/synthetic/corpus_index.tsv"
OUT_MD = "data/synthetic/PROGRESS.md"

TARGET_SINGLE = 1000
TARGET_MULTI = 300


def load_jsonl(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def bar(done, total, width=30):
    filled = int(width * done / total) if total else 0
    return "[" + "#" * filled + "-" * (width - filled) + f"] {done}/{total}"


def main():
    queries = load_jsonl(QUERIES)
    convos = load_jsonl(CONVOS)
    ledger = load_jsonl(LEDGER)

    idx = {}
    with open(INDEX, encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="\t"); next(reader)
        for row in reader:
            idx[row[0]] = row[1]

    invalid = [(q["id"], n) for q in queries for n in q["gold_node_ids"] if n not in idx]
    ans = Counter(q["answerability"] for q in queries)
    shape = Counter(q.get("intent_shape", "?") for q in queries)
    route = Counter(q.get("entry_route", "?") for q in queries)
    states = Counter(q.get("momentary_state", "?") for q in queries)

    lines = []
    lines.append(f"# Synthetic data — progress\n")
    lines.append(f"Batches done: **{len(ledger)}/50**\n")
    lines.append(f"Single-turn questions:  `{bar(len(queries), TARGET_SINGLE)}`")
    lines.append(f"Multi-turn sets:        `{bar(len(convos), TARGET_MULTI)}`\n")
    lines.append(f"Integrity: **{'OK — 0 invalid gold nodeIds' if not invalid else f'{len(invalid)} INVALID'}**\n")

    lines.append("## Answerability (single-turn)")
    for k in ("full", "partial", "none"):
        pct = 100 * ans.get(k, 0) / len(queries) if queries else 0
        lines.append(f"- {k}: {ans.get(k,0)} ({pct:.0f}%)")
    lines.append("")
    lines.append("## Intent shape")
    lines.append(", ".join(f"{k}={v}" for k, v in shape.most_common()))
    lines.append("")
    lines.append(f"## Entry route: " + ", ".join(f"{k.split(' ')[0]}={v}" for k, v in route.most_common()))
    lines.append("## Momentary state: " + ", ".join(f"{k.split(' ')[0]}={v}" for k, v in states.most_common()))
    lines.append("")

    lines.append("## Personas so far")
    for entry in ledger:
        d = entry.get("answerability_dist", {})
        lines.append(f"- batch {entry['batch']}: **{entry.get('persona','?')}**  "
                     f"(full {d.get('full',0)}/partial {d.get('partial',0)}/none {d.get('none',0)})")
    lines.append("")

    # A few REAL sample records, spread across batches, so you can read the actual output.
    lines.append("## Sample of the actual data (1 per batch)")
    by_batch = defaultdict(list)
    for q in queries:
        by_batch[q["batch"]].append(q)
    for batch in sorted(by_batch):
        q = by_batch[batch][len(by_batch[batch]) // 2]  # a middle one
        gold = "; ".join(idx[n] for n in q["gold_node_ids"]) or "(none — unanswerable)"
        lines.append(f"**b{batch}** [{q.get('channel','?').split(' ')[0]}/{q.get('entry_route','?').split(' ')[0]}/"
                     f"{q.get('momentary_state','?').split(' ')[0]}] _{q['answerability']}_")
        lines.append(f"> Q: {q['query']}")
        lines.append(f"> → gold: {gold}\n")

    text = "\n".join(lines)
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\n(written to {OUT_MD})")


if __name__ == "__main__":
    main()
