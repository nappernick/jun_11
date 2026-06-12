"""
Unit tests for the dataset loader and cohort normalization (Task 3, Requirement 1).

Two flavors of test live here:

* **Real-data tests** run :class:`bakeoff.dataset.DatasetLoader` against the actual
  files under ``data/synthetic/``. They assert structural invariants that must
  hold for *whatever counts exist at run time* (the dataset is still being
  generated, so nothing here hard-codes a size — Req 1.7): non-empty load, every
  item carries a fully-populated :class:`bakeoff.types.CohortKey`, gold resolves,
  and cohort cells enumerate.
* **Fixture tests** build a tiny synthetic dataset in a ``tmp_path`` directory to
  exercise the precise join/normalization/failure logic deterministically: the
  multi-turn ``set_id``+``turn`` join (Req 1.3), gold resolution via the corpus
  index (Req 1.4), and the fail-loud gold-integrity path on a dangling
  ``gold_node_id`` (Req 1.5).

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bakeoff import config
from bakeoff.dataset import DatasetLoader, GoldIntegrityError
from bakeoff.types import COHORT_DIMENSIONS, CohortKey, Item


# ---------------------------------------------------------------------------
# Real-data tests (operate on whatever counts exist — Req 1.7)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def real_loader() -> DatasetLoader:
    loader = DatasetLoader()  # defaults to config.DATASET_DIR (data/synthetic/)
    if not (loader.data_dir / DatasetLoader.QUERIES_FILE).exists():
        pytest.skip(f"synthetic dataset not present at {loader.data_dir}")
    return loader


@pytest.fixture(scope="module")
def real_items(real_loader: DatasetLoader) -> list[Item]:
    return real_loader.load_items()


def test_dataset_dir_default_is_data_synthetic() -> None:
    # Req 1.1: the loader reads from data/synthetic/ by default.
    assert config.DATASET_DIR.name == "synthetic"
    assert config.DATASET_DIR.parent.name == "data"
    assert DatasetLoader().data_dir == config.DATASET_DIR


def test_loads_some_items_of_each_kind(real_items: list[Item]) -> None:
    # Req 1.7: counts > 0, but no specific count is hard-coded.
    assert len(real_items) > 0
    singles = [i for i in real_items if i.turn_type == "single"]
    multis = [i for i in real_items if i.turn_type == "multi"]
    assert len(singles) > 0, "expected at least one single-turn query"
    assert len(multis) > 0, "expected at least one multi-turn set"


def test_every_item_has_a_fully_populated_cohort_key(real_items: list[Item]) -> None:
    # Req 1.2/1.3: every item carries a CohortKey with every axis non-empty.
    for item in real_items:
        assert isinstance(item.cohort, CohortKey)
        d = item.cohort.to_dict()
        assert set(d) == set(COHORT_DIMENSIONS)
        for axis, value in d.items():
            assert isinstance(value, str) and value != "", (
                f"item {item.id} has empty cohort axis {axis!r}"
            )
        # turn_type axis agrees with the item's own turn_type
        assert item.cohort.turn_type == item.turn_type


def test_single_turn_items_carry_query_gold_and_answerability(
    real_items: list[Item],
) -> None:
    # Req 1.2: single-turn -> Item with gold_node_ids, answerability, cohort.
    singles = [i for i in real_items if i.turn_type == "single"]
    sample = singles[0]
    assert sample.query, "single-turn item should have a query string"
    assert sample.cohort.turn_type == "single"
    assert sample.answerability in {"full", "partial", "none", "unknown"}
    # raw gold ids are retained alongside resolved fragments
    assert len(sample.gold) == len(sample.gold_node_ids)


def test_multi_turn_items_retain_ordered_turns_and_per_turn_state(
    real_items: list[Item],
) -> None:
    # Req 1.3: multi-turn -> ordered turns + per-turn momentary_state retained.
    multis = [i for i in real_items if i.turn_type == "multi"]
    sample = next(m for m in multis if len(m.turns) > 1)
    turn_numbers = [t.turn for t in sample.turns]
    assert turn_numbers == sorted(turn_numbers), "turns must be in order"
    for t in sample.turns:
        assert t.user_utterance != ""
        assert isinstance(t.momentary_state, str) and t.momentary_state != ""


def test_resolve_gold_maps_ids_to_titles(real_loader: DatasetLoader) -> None:
    # Req 1.4: gold ids resolve to title/snippet via corpus_index.tsv.
    any_id = next(iter(real_loader.corpus))
    fragments = real_loader.resolve_gold([any_id])
    assert len(fragments) == 1
    assert fragments[0].node_id == any_id
    assert fragments[0].title != ""


def test_cohort_cells_enumerates_only_non_empty_cells(
    real_loader: DatasetLoader, real_items: list[Item]
) -> None:
    # Req 1.6: cohort_cells enumerates exactly the cells present in the data.
    cells = real_loader.cohort_cells()
    assert len(cells) > 0
    present = {i.cohort.cell_id() for i in real_items}
    enumerated = {c.cell_id() for c in cells}
    assert enumerated == present


def test_geography_comes_from_ledger_not_persona_string(
    real_loader: DatasetLoader, real_items: list[Item]
) -> None:
    # Req 1.2: cohort geography/proficiency/tone are taken from the ledger join
    # (authoritative), not the noisier persona free-text on the record.
    # The ledger normalizes "Nigeria(Lagos)" -> "Nigeria (Lagos)".
    if 0 not in real_loader.ledger:
        pytest.skip("batch 0 not present in ledger")
    geo_ledger, prof_ledger, tone_ledger = real_loader.ledger[0]
    batch0 = [i for i in real_items if i.batch == 0]
    assert batch0, "expected items in batch 0"
    for item in batch0:
        assert item.cohort.geography == geo_ledger
        assert item.cohort.proficiency == prof_ledger
        assert item.cohort.tone == tone_ledger


# ---------------------------------------------------------------------------
# Fixture tests (deterministic join / failure logic)
# ---------------------------------------------------------------------------
def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )


def _write_corpus(path: Path, rows: list[tuple[str, str, str]]) -> None:
    lines = ["nodeId\ttitle\tsnippet"]
    lines += [f"{nid}\t{title}\t{snippet}" for nid, title, snippet in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_fixture_dataset(d: Path, *, dangling: bool = False) -> None:
    """Write a tiny self-consistent (or deliberately dangling) dataset into ``d``."""
    _write_corpus(
        d / DatasetLoader.CORPUS_INDEX_FILE,
        [
            ("node-aaa", "How to book travel", "Book within the program..."),
            ("node-bbb", "Lost receipt policy", "If you lose a receipt..."),
        ],
    )
    _write_jsonl(
        d / DatasetLoader.LEDGER_FILE,
        [
            {
                "batch": 0,
                "persona": "Nigeria (Lagos) | broken | terse",
                "origin": "Nigeria (Lagos)",
                "proficiency": "broken",
                "disposition": "terse",
            }
        ],
    )
    bad_id = "node-DOES-NOT-EXIST" if dangling else "node-aaa"
    _write_jsonl(
        d / DatasetLoader.QUERIES_FILE,
        [
            {
                "id": "b0-q01",
                "batch": 0,
                "persona": "Nigeria(Lagos) | broken | terse",
                "entry_route": "slack",
                "momentary_state": "neutral",
                "query": "how do i book travel",
                "wants": "How to book travel.",
                "gold_node_ids": [bad_id],
                "answerability": "full",
            }
        ],
    )
    _write_jsonl(
        d / DatasetLoader.CONVERSATIONS_FILE,
        [
            {
                "set_id": "c0-s01",
                "batch": 0,
                "persona_tag": "Nigeria(Lagos) | broken | terse",
                "entry_route": "slack",
                "turn_count": 2,
                "turns": [
                    {
                        "turn": 2,
                        "momentary_state": "frustrated",
                        "response_dependent": True,
                        "depends_on_turn": 1,
                        "user_utterance": "where is the form",
                        "wants": "Where to find the form.",
                    },
                    {
                        "turn": 1,
                        "momentary_state": "anxious",
                        "response_dependent": False,
                        "depends_on_turn": None,
                        "user_utterance": "i lost my receipt",
                        "wants": "Whether a lost receipt can still be claimed.",
                    },
                ],
            }
        ],
    )
    _write_jsonl(
        d / DatasetLoader.CONVO_GOLD_FILE,
        [
            {
                "set_id": "c0-s01",
                "turn": 1,
                "gold_node_ids": ["node-bbb"],
                "answerability": "partial",
            }
        ],
    )


def test_multi_turn_join_is_correct_on_a_fixture(tmp_path: Path) -> None:
    # Req 1.3/1.4: join the multi-turn set to its gold by set_id+turn==1,
    # order the turns, retain per-turn state, and resolve the gold fragment.
    _build_fixture_dataset(tmp_path)
    loader = DatasetLoader(data_dir=tmp_path)
    items = {i.id: i for i in loader.load_items()}

    convo = items["c0-s01"]
    assert convo.turn_type == "multi"
    assert convo.is_multi_turn is True

    # turns are ordered by turn number even though the file listed turn 2 first
    assert [t.turn for t in convo.turns] == [1, 2]
    assert convo.turns[0].momentary_state == "anxious"
    assert convo.turns[1].momentary_state == "frustrated"
    assert convo.turns[1].response_dependent is True
    assert convo.turns[1].depends_on_turn == 1

    # gold joined by set_id+turn==1 and resolved via the corpus index
    assert convo.gold_node_ids == ["node-bbb"]
    assert [g.node_id for g in convo.gold] == ["node-bbb"]
    assert convo.gold[0].title == "Lost receipt policy"
    # answerability + focal query/state come from turn 1
    assert convo.answerability == "partial"
    assert convo.cohort.answerability == "partial"
    assert convo.cohort.momentary_state == "anxious"
    assert convo.query == "i lost my receipt"

    # turn-1 gold attaches to the turn-1 object only
    assert [g.node_id for g in convo.turns[0].gold] == ["node-bbb"]
    assert convo.turns[1].gold == []

    # cohort geography/proficiency/tone resolved from the ledger join
    assert convo.cohort.geography == "Nigeria (Lagos)"
    assert convo.cohort.proficiency == "broken"
    assert convo.cohort.tone == "terse"


def test_gold_integrity_failure_on_dangling_id(tmp_path: Path) -> None:
    # Req 1.5: a dangling gold_node_id makes load fail loudly, listing the id.
    _build_fixture_dataset(tmp_path, dangling=True)
    loader = DatasetLoader(data_dir=tmp_path)
    with pytest.raises(GoldIntegrityError) as excinfo:
        loader.load_items()
    assert "node-DOES-NOT-EXIST" in excinfo.value.missing_ids
    # the message names the offending id, not just a count
    assert "node-DOES-NOT-EXIST" in str(excinfo.value)


def test_resolve_gold_raises_on_unresolved_id(tmp_path: Path) -> None:
    # Req 1.5: resolve_gold itself fails loudly on an unknown id.
    _build_fixture_dataset(tmp_path)
    loader = DatasetLoader(data_dir=tmp_path)
    with pytest.raises(GoldIntegrityError) as excinfo:
        loader.resolve_gold(["node-aaa", "node-missing"])
    assert excinfo.value.missing_ids == ["node-missing"]


def test_conversation_without_gold_is_tracked_not_fatal(tmp_path: Path) -> None:
    # Req 1.5 boundary: a conversation with no turn-1 gold row is handled
    # gracefully (empty gold, answerability "none") and tracked, never crashing.
    _build_fixture_dataset(tmp_path)
    # add a second conversation with no matching gold row
    convos_path = tmp_path / DatasetLoader.CONVERSATIONS_FILE
    with convos_path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "set_id": "c0-s02",
                    "batch": 0,
                    "persona_tag": "Nigeria(Lagos) | broken | terse",
                    "entry_route": "quicksuite",
                    "turns": [
                        {
                            "turn": 1,
                            "momentary_state": "neutral",
                            "user_utterance": "hello?",
                            "wants": "greeting",
                        }
                    ],
                }
            )
            + "\n"
        )
    loader = DatasetLoader(data_dir=tmp_path)
    items = {i.id: i for i in loader.load_items()}
    assert "c0-s02" in loader.conversations_without_gold
    no_gold = items["c0-s02"]
    assert no_gold.gold_node_ids == []
    assert no_gold.gold == []
    assert no_gold.answerability == "none"


def test_no_hardcoded_sizes_loader_reflects_file_contents(tmp_path: Path) -> None:
    # Req 1.7: the loader operates on whatever counts exist (here: 1 query,
    # 1 conversation) — no size is baked in.
    _build_fixture_dataset(tmp_path)
    loader = DatasetLoader(data_dir=tmp_path)
    items = loader.load_items()
    singles = [i for i in items if i.turn_type == "single"]
    multis = [i for i in items if i.turn_type == "multi"]
    assert len(singles) == 1
    assert len(multis) == 1
    assert len(items) == 2
