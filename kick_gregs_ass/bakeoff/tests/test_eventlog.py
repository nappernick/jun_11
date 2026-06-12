"""
Unit tests for :mod:`bakeoff.eventlog` (Task 2).

Covers:

* round-trip losslessness on a fully-populated event;
* each :func:`validate_event` rule firing on a crafted bad event and passing on
  a good one (one test per rule);
* the JSONL parse guard — a truncated final line is discarded (no raise), a
  malformed *non-final* line raises, and a missing file reads as ``[]``;
* :func:`append_event` writes exactly one line per event and that line is
  newline-free internally.

These are example-based tests; the universal timing/round-trip property lives in
``test_eventlog_pbt.py`` (design Property 5 / Req 14.1).
"""
from __future__ import annotations

import dataclasses

import pytest

import bakeoff.config as config
from bakeoff.eventlog import (
    EventLogError,
    append_event,
    from_jsonl,
    read_events,
    to_jsonl,
    validate_event,
)
from bakeoff.ids import SCHEMA_VERSION, trial_id
from bakeoff.types import (
    AccuracyScores,
    CohortKey,
    JudgeScores,
    QualityScores,
    RetrievalRecord,
    StageTimings,
    TrialEvent,
)


# ---------------------------------------------------------------------------
# Builders — one fully-populated, valid (full-answerability) event.
# ---------------------------------------------------------------------------
def _cohort(answerability: str = "full") -> CohortKey:
    return CohortKey(
        geography="Nigeria (Lagos)",
        proficiency="broken",
        tone="terse",
        entry_route="slack",
        momentary_state="frustrated",
        answerability=answerability,
        turn_type="single",
    )


def _timings(
    retrieval_total_ms: float = 10.0, generation_total_ms: float = 20.0
) -> StageTimings:
    return StageTimings(
        embed_query_ms=1.5,
        bm25_vectorize_ms=2.25,
        hybrid_search_ms=3.0,
        rerank_ms=3.25,
        retrieval_total_ms=retrieval_total_ms,
        ttft_ms=5.0,
        generation_total_ms=generation_total_ms,
        end_to_end_ms=retrieval_total_ms + generation_total_ms,
    )


def _quality(
    *, abstention_correct=None, unwarranted_refusal=0
) -> QualityScores:
    accuracy = AccuracyScores(
        precision_at_k=0.8,
        recall_at_k=0.6,
        mrr=0.75,
        ndcg_at_k=0.71,
        grounding_precision=0.9,
        grounding_recall=0.5,
        semantic_similarity=0.88,
        abstention_correct=abstention_correct,
        unwarranted_refusal=unwarranted_refusal,
    )
    judge = JudgeScores(
        faithfulness=4.5,
        correctness=4.0,
        completeness=3.5,
        judge_sample_count=3,
        judge_model=config.JUDGE_MODEL_ID,
        judge_dim_sd={"faithfulness": 0.1, "correctness": 0.2},
    )
    return QualityScores(
        accuracy=accuracy,
        judge=judge,
        composite=0.823,
        composite_weights_version=config.COMPOSITE_WEIGHTS_VERSION,
    )


def _event(
    *,
    answerability: str = "full",
    abstention_correct=None,
    unwarranted_refusal=0,
    error=None,
    retrieval_total_ms: float = 10.0,
    generation_total_ms: float = 20.0,
    end_to_end_override=None,
) -> TrialEvent:
    timings = _timings(retrieval_total_ms, generation_total_ms)
    if end_to_end_override is not None:
        timings = dataclasses.replace(timings, end_to_end_ms=end_to_end_override)
    return TrialEvent(
        trial_id=trial_id("nova-lite", "b0-q01", 0, "wide", "plan-v1"),
        schema_version=SCHEMA_VERSION,
        plan_version="plan-v1",
        model="nova-lite",
        item_id="b0-q01",
        turn_type="single",
        pass_name="wide",
        rep=0,
        temperature=0.2,
        cohort=_cohort(answerability),
        query="how do i add my passport name?",
        gold_node_ids=["n1", "n2"],
        answerability=answerability,
        retrieval=RetrievalRecord(
            fragment_ids=["n1", "n2", "n3"],
            confidence=[0.91, 0.55, 0.4],
            cache_hit=False,
        ),
        answer_text="Go to Profile > Travel docs and add the name exactly.",
        token_usage={"prompt": 120, "completion": 48, "total": 168},
        timings=timings,
        quality=_quality(
            abstention_correct=abstention_correct,
            unwarranted_refusal=unwarranted_refusal,
        ),
        started_at="2025-01-01T00:00:00Z",
        completed_at="2025-01-01T00:00:00.030Z",
        error=error,
    )


# ---------------------------------------------------------------------------
# Round-trip losslessness
# ---------------------------------------------------------------------------
def test_round_trip_lossless_full_event():
    event = _event()
    line = to_jsonl(event)
    assert "\n" not in line, "serialized event must be a single physical line"
    restored = from_jsonl(line)
    assert restored == event
    # idempotent: serializing the restored event reproduces the same line.
    assert to_jsonl(restored) == line


def test_round_trip_preserves_nested_dataclass_types():
    event = _event(answerability="none", abstention_correct=1,
                   unwarranted_refusal=None)
    restored = from_jsonl(to_jsonl(event))
    assert isinstance(restored.cohort, CohortKey)
    assert isinstance(restored.retrieval, RetrievalRecord)
    assert isinstance(restored.timings, StageTimings)
    assert isinstance(restored.quality, QualityScores)
    assert isinstance(restored.quality.accuracy, AccuracyScores)
    assert isinstance(restored.quality.judge, JudgeScores)
    assert restored == event


def test_round_trip_with_embedded_newline_in_answer():
    # A newline inside a string field must not break the single-line invariant.
    base = _event()
    event = dataclasses.replace(base, answer_text="line one\nline two\twith tab")
    line = to_jsonl(event)
    assert "\n" not in line
    assert from_jsonl(line) == event


# ---------------------------------------------------------------------------
# Validation rule 1: abstention_correct populated iff answerability in {none, partial}
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("answerability", ["none", "partial"])
def test_abstention_required_passes_when_populated(answerability):
    validate_event(
        _event(
            answerability=answerability,
            abstention_correct=1,
            unwarranted_refusal=None,
        )
    )


@pytest.mark.parametrize("answerability", ["none", "partial"])
def test_abstention_missing_raises(answerability):
    bad = _event(
        answerability=answerability,
        abstention_correct=None,  # violation: should be populated
        unwarranted_refusal=None,
    )
    with pytest.raises(ValueError, match="abstention_correct must be populated"):
        validate_event(bad)


def test_abstention_present_on_full_raises():
    bad = _event(
        answerability="full",
        abstention_correct=0,  # violation: must be None for full
        unwarranted_refusal=0,
    )
    with pytest.raises(ValueError, match="abstention_correct must be None"):
        validate_event(bad)


# ---------------------------------------------------------------------------
# Validation rule 2: unwarranted_refusal populated iff answerability == full
# ---------------------------------------------------------------------------
def test_unwarranted_refusal_populated_on_full_passes():
    validate_event(
        _event(answerability="full", abstention_correct=None,
               unwarranted_refusal=1)
    )


def test_unwarranted_refusal_missing_on_full_raises():
    bad = _event(
        answerability="full",
        abstention_correct=None,
        unwarranted_refusal=None,  # violation: should be populated for full
    )
    with pytest.raises(ValueError, match="unwarranted_refusal must be populated"):
        validate_event(bad)


@pytest.mark.parametrize("answerability", ["none", "partial"])
def test_unwarranted_refusal_present_on_non_full_raises(answerability):
    bad = _event(
        answerability=answerability,
        abstention_correct=1,
        unwarranted_refusal=0,  # violation: must be None when not full
    )
    with pytest.raises(ValueError, match="unwarranted_refusal must be None"):
        validate_event(bad)


# ---------------------------------------------------------------------------
# Validation rule 3: timing identity for non-error events
# ---------------------------------------------------------------------------
def test_timing_identity_passes_when_consistent():
    validate_event(_event(retrieval_total_ms=12.0, generation_total_ms=33.0))


def test_timing_identity_within_epsilon_passes():
    # off by 5e-4 ms — inside the 1e-3 absolute tolerance.
    validate_event(
        _event(
            retrieval_total_ms=10.0,
            generation_total_ms=20.0,
            end_to_end_override=30.0005,
        )
    )


def test_timing_identity_violation_raises():
    bad = _event(
        retrieval_total_ms=10.0,
        generation_total_ms=20.0,
        end_to_end_override=42.0,  # 30 != 42
    )
    with pytest.raises(ValueError, match="timing identity violated"):
        validate_event(bad)


def test_timing_identity_not_enforced_for_error_events():
    # error set -> fields are best-effort; the timing identity is NOT checked.
    err = _event(
        error="ThrottlingException after 3 retries",
        retrieval_total_ms=10.0,
        generation_total_ms=20.0,
        end_to_end_override=999.0,  # inconsistent, but tolerated for error events
    )
    validate_event(err)  # must not raise


# ---------------------------------------------------------------------------
# append_event + read_events: durable write, exactly one line per event
# ---------------------------------------------------------------------------
def test_append_writes_one_line_per_event(tmp_path):
    log = tmp_path / "nested" / "trial_events.jsonl"
    e1 = _event()
    e2 = dataclasses.replace(_event(), rep=1, answer_text="second\nanswer")
    append_event(log, e1)
    append_event(log, e2)

    raw = log.read_text(encoding="utf-8")
    assert raw.count("\n") == 2, "exactly one terminator per appended event"
    assert raw.endswith("\n")

    events = read_events(log)
    assert events == [e1, e2]


def test_read_missing_file_returns_empty(tmp_path):
    assert read_events(tmp_path / "does_not_exist.jsonl") == []


# ---------------------------------------------------------------------------
# Parse guard (Req 8.5, design Error Scenario 5)
# ---------------------------------------------------------------------------
def test_truncated_final_line_is_discarded_without_raising(tmp_path):
    log = tmp_path / "trial_events.jsonl"
    good = [_event(), dataclasses.replace(_event(), rep=1),
            dataclasses.replace(_event(), rep=2)]
    for e in good:
        append_event(log, e)

    # Simulate a crash mid-write: append a half-written (truncated) JSON line
    # with no trailing newline, exactly as os.fsync-less death would leave it.
    full_line = to_jsonl(dataclasses.replace(_event(), rep=3))
    truncated = full_line[: len(full_line) // 2]
    with open(log, "a", encoding="utf-8") as f:
        f.write(truncated)  # no newline -> partial final line

    events = read_events(log)  # must NOT raise
    assert events == good, "the complete prefix is returned, partial line dropped"


def test_truncated_final_line_with_trailing_newline_is_discarded(tmp_path):
    # Even if the partial line happens to have ended on a newline boundary, an
    # unparseable final line is still treated as crash-truncation and dropped.
    log = tmp_path / "trial_events.jsonl"
    append_event(log, _event())
    with open(log, "a", encoding="utf-8") as f:
        f.write('{"trial_id": "oops", "incomplete":\n')
    events = read_events(log)
    assert len(events) == 1


def test_malformed_non_final_line_raises(tmp_path):
    log = tmp_path / "trial_events.jsonl"
    append_event(log, _event())
    # inject a corrupt line that is NOT the final line
    with open(log, "a", encoding="utf-8") as f:
        f.write("this is not json at all\n")
    append_event(log, dataclasses.replace(_event(), rep=1))

    with pytest.raises(EventLogError, match="malformed non-final line"):
        read_events(log)


def test_empty_file_reads_as_empty(tmp_path):
    log = tmp_path / "empty.jsonl"
    log.write_text("", encoding="utf-8")
    assert read_events(log) == []
