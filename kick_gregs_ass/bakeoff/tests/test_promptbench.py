"""
Prompt Bench tests — fully offline (zero network, no AWS).

Cover the load-bearing, independent-of-v3 behaviors:

* prompts load VERBATIM from arbitrary-named ``*.txt`` files; empty files are skipped;
* the fixed 24-conversation sample resolves and its index map is 1-based and complete;
* the durable store roundtrips and reconstructs; archive moves files aside;
* ``compute_winner`` crowns the highest mean and flags a within-CI tie;
* the PromptBenchScorer scores a slice end-to-end on the OFFLINE backend, firing one
  ``on_conversation_scored`` per conversation — and uses its OWN semaphore registry, never
  touching the optimizer v3 scorer's global semaphores.
"""
from __future__ import annotations

import asyncio

import pytest

from bakeoff import config
from bakeoff.promptbench.prompts import load_prompts
from bakeoff.promptbench.runner import compute_winner
from bakeoff.promptbench.sample import load_sample_items, sample_index_by_item_id
from bakeoff.promptbench.scorer import PromptBenchScorer, _PB_RESOURCE_SEMAPHORES
from bakeoff.promptbench.store import PointRecord, PromptBenchStore, ResultRecord
from bakeoff.quality.optimizer.backends import build_offline_backend
from bakeoff.quality.optimizer.v3 import scorer as v3_scorer


# ---------------------------------------------------------------------------
# prompts: verbatim, arbitrary names, skip empties
# ---------------------------------------------------------------------------
def test_load_prompts_reads_verbatim_arbitrary_names(tmp_path, monkeypatch):
    d = tmp_path / "prompts"
    d.mkdir()
    (d / "zeta.txt").write_text("PROMPT Z body", encoding="utf-8")
    (d / "alpha.txt").write_text("PROMPT A body", encoding="utf-8")
    (d / "empty.txt").write_text("   \n", encoding="utf-8")  # skipped
    monkeypatch.setattr(config, "PROMPT_BENCH_PROMPTS_DIR", d)

    specs = load_prompts()
    # sorted by filename; empty skipped; key == stem; text verbatim
    assert [s.key for s in specs] == ["alpha", "zeta"]
    assert specs[0].text == "PROMPT A body"
    assert specs[1].label == "ZETA"


def test_load_prompts_raises_when_no_usable_files(tmp_path, monkeypatch):
    d = tmp_path / "prompts"
    d.mkdir()
    (d / "empty.txt").write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "PROMPT_BENCH_PROMPTS_DIR", d)
    with pytest.raises(FileNotFoundError):
        load_prompts()


# ---------------------------------------------------------------------------
# sample
# ---------------------------------------------------------------------------
def test_sample_resolves_and_indexes():
    from bakeoff.promptbench.sample import load_sample_spec

    n = int(load_sample_spec()["n"])  # spec-driven so the sample size can change
    items = load_sample_items()
    assert len(items) == n
    idx = sample_index_by_item_id()
    assert len(idx) == n
    assert min(idx.values()) == 1 and max(idx.values()) == n
    # every resolved item has an index
    assert all(it.item_id in idx for it in items)


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------
def test_store_roundtrip_reconstruct_and_archive(tmp_path):
    store = PromptBenchStore(points_path=tmp_path / "p.jsonl", results_path=tmp_path / "r.jsonl")
    store.append_point(PointRecord("a", 1, "c1-s1", "full", 3, 0.42))
    store.append_point(PointRecord("a", 2, "c2-s1", "none", 3, 0.10))
    store.append_result(
        ResultRecord("a", "A", 0.42, 0.05, 0.37, 0.47, 24, {"faithfulness": 0.4}, 0.5, 0.1, 2)
    )
    recon = store.reconstruct()
    assert [p["conversation_index"] for p in recon["points"]["a"]] == [1, 2]  # sorted
    assert recon["results"]["a"]["triad"] == pytest.approx(0.42)

    archive = store.archive()
    assert archive is not None and archive.exists()
    assert not (tmp_path / "p.jsonl").exists()  # moved aside, not destroyed
    assert (archive / "p.jsonl").exists()


def test_reconstruct_dedups_repeated_conversation_points_keeping_latest(tmp_path):
    """Re-scoring a prompt appends a fresh point for an already-scored conversation; reconstruct
    must dedup by item_id (last-write-wins) so a prompt never shows more points than the sample
    has conversations (the 474-rows-for-400-items bug)."""
    store = PromptBenchStore(points_path=tmp_path / "p.jsonl", results_path=tmp_path / "r.jsonl")
    # Two conversations, then a RE-SCORE of the first (same item_id, new score).
    store.append_point(PointRecord("xml", 1, "c1-s1", "full", 3, 0.40))
    store.append_point(PointRecord("xml", 2, "c2-s1", "none", 3, 0.10))
    store.append_point(PointRecord("xml", 1, "c1-s1", "full", 3, 0.55))  # re-run of c1-s1

    pts = store.reconstruct()["points"]["xml"]
    assert len(pts) == 2  # deduped: one point per conversation, not three rows
    by_item = {p["item_id"]: p for p in pts}
    assert by_item["c1-s1"]["overall"] == pytest.approx(0.55)  # the LATEST score wins
    assert [p["conversation_index"] for p in pts] == [1, 2]  # still sorted by X


# ---------------------------------------------------------------------------
# resume: a prompt with a durable ResultRecord is reused, never re-scored
# ---------------------------------------------------------------------------
def test_runner_resume_reuses_completed_prompt_and_scores_the_rest(tmp_path, monkeypatch):
    """A re-run reuses any prompt that already has a durable ResultRecord (no model/judge
    calls, no new points) and scores only the not-yet-completed prompt(s)."""
    from bakeoff.promptbench import runner as runner_mod
    from bakeoff.promptbench.prompts import PromptSpec
    from bakeoff.promptbench.runner import PromptBenchRunner

    # Two prompts; "done" is pre-completed in the store, "todo" is not.
    prompts = [PromptSpec(key="done", label="DONE", text="grounded A"),
               PromptSpec(key="todo", label="TODO", text="grounded B")]
    one_item = load_sample_items()[:1]
    item_id = one_item[0].item_id
    monkeypatch.setattr(runner_mod, "load_prompts", lambda: prompts)
    monkeypatch.setattr(runner_mod, "load_sample_spec", lambda: {"n": 1, "items": [{"item_id": item_id}]})
    monkeypatch.setattr(runner_mod, "load_sample_items", lambda spec=None: one_item)
    monkeypatch.setattr(runner_mod, "sample_index_by_item_id", lambda spec=None: {item_id: 1})

    store = PromptBenchStore(points_path=tmp_path / "p.jsonl", results_path=tmp_path / "r.jsonl")
    # Pre-seed the durable result for "done" only (as if a prior run completed it).
    store.append_result(
        ResultRecord("done", "DONE", 0.71, 0.03, 0.68, 0.74, 1, {"faithfulness": 0.7}, 0.5, 0.0, 0)
    )

    events: list[tuple[str, dict]] = []
    runner = PromptBenchRunner(
        backend=build_offline_backend(),
        store=store,
        emit=lambda t, p: events.append((t, p)),
        resume=True,
    )
    out = asyncio.run(runner.run())

    # "done" was reused intact (its persisted triad), "todo" was scored fresh.
    keys = {r["prompt_key"] for r in out["results"]}
    assert keys == {"done", "todo"}
    done_result = next(r for r in out["results"] if r["prompt_key"] == "done")
    assert done_result["triad"] == pytest.approx(0.71)  # the durable value, not a re-score

    # The reused prompt was flagged resumed; the scored one was not.
    completed = {p["prompt_key"]: p for (t, p) in events if t == "promptbench_prompt_completed"}
    assert completed["done"].get("resumed") is True
    assert not completed["todo"].get("resumed")

    # Resume did NOT append any new points for "done" (no model/judge calls); only "todo" did.
    point_keys = {p.prompt_key for p in store.read_points()}
    assert "done" not in point_keys
    assert "todo" in point_keys


# ---------------------------------------------------------------------------
# winner
# ---------------------------------------------------------------------------
def test_compute_winner_highest_mean_and_ci_tie_flag():
    none = compute_winner([])
    assert none is None

    # clear win: gap (0.10) exceeds winner CI (0.02)
    clear = compute_winner([
        {"prompt_key": "a", "label": "A", "triad": 0.50, "ci_half_width": 0.02},
        {"prompt_key": "b", "label": "B", "triad": 0.40, "ci_half_width": 0.02},
    ])
    assert clear["prompt_key"] == "a" and clear["tie_within_ci"] is False

    # tie: gap (0.01) within winner CI (0.05)
    tie = compute_winner([
        {"prompt_key": "a", "label": "A", "triad": 0.50, "ci_half_width": 0.05},
        {"prompt_key": "b", "label": "B", "triad": 0.49, "ci_half_width": 0.05},
    ])
    assert tie["prompt_key"] == "a" and tie["tie_within_ci"] is True


# ---------------------------------------------------------------------------
# scorer: offline end-to-end + per-conversation callback + OWN semaphores
# ---------------------------------------------------------------------------
def test_scorer_offline_end_to_end_and_isolated_semaphores():
    items = load_sample_items()[:3]
    backend = build_offline_backend()

    ticks: list[dict] = []

    async def go():
        # Snapshot the v3 scorer's global registry size BEFORE scoring.
        before = len(v3_scorer._RESOURCE_SEMAPHORES)
        _PB_RESOURCE_SEMAPHORES.clear()
        scorer = PromptBenchScorer(
            backend,
            reps=1,
            on_conversation_scored=lambda **kw: ticks.append(kw),
        )
        score = await scorer.score_prompt(
            model=config.PROMPT_BENCH_MODEL,
            instruction="be helpful and grounded",
            items=items,
            prompt_role="a",
        )
        after = len(v3_scorer._RESOURCE_SEMAPHORES)
        return score, before, after

    score, before, after = asyncio.run(go())

    assert score.n_conversations == 3
    assert len(ticks) == 3  # one callback per conversation
    # Prompt Bench used its OWN semaphores...
    assert len(_PB_RESOURCE_SEMAPHORES) >= 1
    # ...the MODEL cap is Prompt Bench's OWN (not the optimizer's CONCURRENCY_CAPS["model"])...
    pb_cap = config.PROMPT_BENCH_MODEL_CONCURRENCY
    assert pb_cap != config.CONCURRENCY_CAPS["model"]  # genuinely its own, not the shared one
    assert any(k[1] == "model" and k[2] == pb_cap for k in _PB_RESOURCE_SEMAPHORES)
    # ...and never populated the optimizer v3 scorer's global registry.
    assert after == before
