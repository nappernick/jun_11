"""
Unit tests for :mod:`bakeoff.eval.event_store` (Task 1.4).

Covers the append-only Event_Store discipline (Req 8.1, 8.2), mirroring the
guarantees of :mod:`bakeoff.eventlog`:

* append-then-reconstruct returns every record in append order;
* durability across a *fresh* reader instance over the same path;
* ``read_recent(limit)`` returns the trailing window;
* a truncated / malformed **trailing** line is tolerated (no raise);
* a malformed **non-final** line is surfaced as :class:`EvalEventStoreError`;
* a missing or empty file reads as ``[]``;
* each appended record is exactly one newline-free physical line.

Network-free.
"""
from __future__ import annotations

import json

import pytest

from bakeoff.eval.event_store import EvalEventStore, EvalEventStoreError
from bakeoff.eval.models import EvalInstance, MetricValue, StageTimings


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
def _instance(idx: int) -> EvalInstance:
    return EvalInstance(
        instance_id=f"inst-{idx}",
        agent_id="agent-A",
        session_id="sess-1",
        instance_index=idx,
        timestamp=f"2025-01-01T00:00:0{idx}Z",
        latency_ms=10.0 + idx,
        stage_timings=StageTimings(retrieval_ms=4.0, generation_ms=6.0),
        corpus_size=1000,
        retrieval_cached=False,
        ragas={"faithfulness": MetricValue.available(0.9, ragas_version="0.2.1")},
        retrieval={"precision_at_k": MetricValue.available(0.5, k=5)},
        confidence=0.7,
        volume=None,
        cost=None,
        prompt_id="p1",
        category="profile",
        status="ok",
        error=None,
    )


# ---------------------------------------------------------------------------
# append -> reconstruct order (Req 8.1, 8.2)
# ---------------------------------------------------------------------------
def test_append_then_reconstruct_returns_every_record_in_order(tmp_path):
    store = EvalEventStore(tmp_path / "eval_instances.jsonl")
    records = [_instance(i) for i in range(5)]
    for r in records:
        store.append(r)

    assert store.read_all() == records
    # reconstruct() is an alias for read_all()
    assert store.reconstruct() == records


def test_durability_across_fresh_reader_instance(tmp_path):
    path = tmp_path / "nested" / "eval_instances.jsonl"
    writer = EvalEventStore(path)
    records = [_instance(i) for i in range(3)]
    for r in records:
        writer.append(r)

    # a brand-new store over the same path sees everything (state lives on disk).
    reader = EvalEventStore(path)
    assert reader.reconstruct() == records


def test_append_writes_one_newline_free_line_per_record(tmp_path):
    path = tmp_path / "eval_instances.jsonl"
    store = EvalEventStore(path)
    store.append(_instance(0))
    store.append(_instance(1))

    raw = path.read_text(encoding="utf-8")
    assert raw.count("\n") == 2, "exactly one terminator per appended record"
    assert raw.endswith("\n")
    for line in raw.splitlines():
        # each line is a complete, single JSON object (no embedded newline).
        assert json.loads(line)["instance_id"].startswith("inst-")


# ---------------------------------------------------------------------------
# read_recent
# ---------------------------------------------------------------------------
def test_read_recent_returns_trailing_window(tmp_path):
    store = EvalEventStore(tmp_path / "eval_instances.jsonl")
    records = [_instance(i) for i in range(6)]
    for r in records:
        store.append(r)

    assert store.read_recent(2) == records[-2:]
    assert store.read_recent(100) == records  # limit beyond size -> all
    assert store.read_recent(0) == []
    assert store.read_recent(-5) == []


# ---------------------------------------------------------------------------
# missing / empty file
# ---------------------------------------------------------------------------
def test_missing_file_reads_as_empty(tmp_path):
    assert EvalEventStore(tmp_path / "does_not_exist.jsonl").read_all() == []


def test_empty_file_reads_as_empty(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    assert EvalEventStore(path).read_all() == []


# ---------------------------------------------------------------------------
# crash tolerance: malformed trailing line discarded without raising
# ---------------------------------------------------------------------------
def test_truncated_trailing_line_is_discarded_without_raising(tmp_path):
    path = tmp_path / "eval_instances.jsonl"
    store = EvalEventStore(path)
    good = [_instance(i) for i in range(3)]
    for r in good:
        store.append(r)

    # simulate a crash mid-append: a half-written JSON line, no trailing newline.
    full_line = json.dumps(_instance(3).to_dict(), separators=(",", ":"))
    truncated = full_line[: len(full_line) // 2]
    with open(path, "a", encoding="utf-8") as f:
        f.write(truncated)

    assert store.read_all() == good, "complete prefix returned, partial line dropped"


def test_truncated_trailing_line_with_newline_is_discarded(tmp_path):
    path = tmp_path / "eval_instances.jsonl"
    store = EvalEventStore(path)
    store.append(_instance(0))
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"instance_id": "oops", "incomplete":\n')

    assert len(store.read_all()) == 1


# ---------------------------------------------------------------------------
# real corruption: malformed NON-final line raises
# ---------------------------------------------------------------------------
def test_malformed_non_final_line_raises(tmp_path):
    path = tmp_path / "eval_instances.jsonl"
    store = EvalEventStore(path)
    store.append(_instance(0))
    with open(path, "a", encoding="utf-8") as f:
        f.write("this is not json at all\n")  # corrupt, and NOT the final line
    store.append(_instance(1))

    with pytest.raises(EvalEventStoreError, match="malformed non-final line"):
        store.read_all()
