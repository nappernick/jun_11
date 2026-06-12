"""Scratch verification for Task 3 (Effort A). Deleted after capturing evidence."""
from bakeoff.dataset import DatasetLoader
from bakeoff.quality.dataset import load_multi_turn_items, turn_reference
from bakeoff.quality.judge import _turn_judge_inputs
from bakeoff.quality.types import GroundTruthKind, TurnOutcome, TurnCloseness

GOLD_NODE = "eb558531-15e7-4030-bed0-72eaa07b6f9c"

ldr = DatasetLoader()

# (a) body table loaded and full body present
bt = ldr.body_table
print("body_table size:", len(bt))
print("GOLD_NODE full body len:", len(bt.get(GOLD_NODE, "")))

# resolve_gold now carries markdown
frag = ldr.resolve_gold([GOLD_NODE])[0]
print("resolve_gold markdown len:", len(frag.markdown or ""))
print("resolve_gold snippet len:", len(frag.snippet or ""))

# Verification 2: turn_reference(item, 0) for c1-s01 is the FULL body, not 200.
items = {it.item_id: it for it in load_multi_turn_items()}
c1 = items["c1-s01"]
kind, ref = turn_reference(c1, 0)
print("c1-s01 turn0 kind:", kind, "ref len:", len(ref))

# Verification 3: _turn_judge_inputs gold_texts[0] is the full body length.
to = TurnOutcome(
    turn=1, answerability=c1.answerability, response_dependent=False,
    answer_text="x", reference_text=ref,
    closeness=TurnCloseness(ground_truth_kind=kind, semantic=0.0, composite=0.0),
)
ideal, gold_texts, ans = _turn_judge_inputs(c1, 0, to)
print("judge ideal len:", len(ideal), "gold_texts[0] len:", len(gold_texts[0]), "answerability:", ans)

# (b) a later turn of an unanswerable conversation flips to ABSTENTION.
none_item = next(it for it in items.values() if it.answerability == "none" and len(it.turns) > 1)
print("unanswerable item:", none_item.item_id, "answerability:", none_item.answerability, "n_turns:", len(none_item.turns))
later_kind, later_ref = turn_reference(none_item, 1)
print("  later-turn kind:", later_kind, "(expect abstention)")
to2 = TurnOutcome(
    turn=2, answerability=None, response_dependent=False,
    answer_text="x", reference_text=later_ref,
    closeness=TurnCloseness(ground_truth_kind=later_kind, semantic=0.0, composite=0.0),
)
_, _, later_ans = _turn_judge_inputs(none_item, 1, to2)
print("  later-turn judge answerability:", later_ans, "(expect none)")

# (b) regression guard: a later turn of an ANSWERABLE conversation stays WANTS.
full_item = next(it for it in items.values() if it.answerability == "full" and len(it.turns) > 1)
fk, _ = turn_reference(full_item, 1)
print("answerable later-turn kind:", fk, "(expect wants)")
assert fk == GroundTruthKind.WANTS
assert later_kind == GroundTruthKind.ABSTENTION
# ref = wants + "\n\n" + full body (each part .strip()'d by ideal_response_text),
# so the stripped full body is contained in ref and ref is far > 200.
assert bt[GOLD_NODE].strip() in ref and len(bt[GOLD_NODE]) == 1257 and len(ref) > 200
assert len(gold_texts[0]) == len(bt[GOLD_NODE]) == 1257
print("ALL ASSERTIONS PASSED")
