"""
Property-based tests for :mod:`bakeoff.eventlog` (Task 2).

**Validates: Requirements 14.1** (and Requirements 8.4, 8.5 by extension) —
design **Property 5: Timings are consistent** and the lossless-round-trip
guarantee.

The single named property exercised here:

    For every generated *valid, non-error* TrialEvent ``e``:
      (a) ``validate_event(e)`` does not raise, and
      (b) ``from_jsonl(to_jsonl(e)) == e`` (lossless round-trip).

The generator constructs events that satisfy the design's validation rules *by
construction*:

* ``end_to_end_ms`` is COMPUTED as ``retrieval_total_ms + generation_total_ms``
  so the timing invariant (Property 5) holds exactly;
* a random ``answerability`` is chosen and ``abstention_correct`` /
  ``unwarranted_refusal`` are populated/None consistently with it
  (abstention iff answerability in {none, partial}; unwarranted_refusal iff
  answerability == full).

All floats are constrained finite (no nan/inf) so dataclass ``__eq__`` is
well-defined for the round-trip assertion.
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from bakeoff.eventlog import from_jsonl, to_jsonl, validate_event
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

# Finite floats only, in a sane metric/latency range -> equality is well-defined
# and JSON round-trips exactly (json renders/parses IEEE-754 doubles losslessly).
_finite = st.floats(
    min_value=0.0,
    max_value=1e6,
    allow_nan=False,
    allow_infinity=False,
    width=64,
)
# Unit-interval metric scores (precision/recall/similarity/composite).
_unit = st.floats(
    min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False, width=64
)
# Short, finite latency components.
_latency = st.floats(
    min_value=0.0, max_value=5e4, allow_nan=False, allow_infinity=False, width=64
)
_text = st.text(max_size=40)
_token_counts = st.integers(min_value=0, max_value=100000)


@st.composite
def trial_events(draw) -> TrialEvent:
    """Generate a valid, non-error :class:`TrialEvent`.

    The timing identity and the answerability/abstention coupling both hold by
    construction, so :func:`validate_event` must accept every generated event.
    """
    answerability = draw(st.sampled_from(["full", "partial", "none"]))

    # Consistent answerability behavior fields.
    if answerability in ("none", "partial"):
        abstention_correct = draw(st.integers(min_value=0, max_value=1))
        unwarranted_refusal = None
    else:  # "full"
        abstention_correct = None
        unwarranted_refusal = draw(st.integers(min_value=0, max_value=1))

    cohort = CohortKey(
        geography=draw(_text),
        proficiency=draw(st.sampled_from(
            ["broken", "functional", "near-native", "fluent", "uneven"])),
        tone=draw(_text),
        entry_route=draw(st.sampled_from(["slack", "quicksuite"])),
        momentary_state=draw(st.sampled_from(
            ["neutral", "frustrated", "anxious", "rushed", "confused"])),
        answerability=answerability,
        turn_type=draw(st.sampled_from(["single", "multi"])),
    )

    # Timing invariant by construction: end_to_end == retrieval + generation.
    retrieval_total_ms = draw(_latency)
    generation_total_ms = draw(_latency)
    timings = StageTimings(
        embed_query_ms=draw(_latency),
        bm25_vectorize_ms=draw(_latency),
        hybrid_search_ms=draw(_latency),
        rerank_ms=draw(_latency),
        retrieval_total_ms=retrieval_total_ms,
        ttft_ms=draw(_latency),
        generation_total_ms=generation_total_ms,
        end_to_end_ms=retrieval_total_ms + generation_total_ms,
    )

    accuracy = AccuracyScores(
        precision_at_k=draw(_unit),
        recall_at_k=draw(_unit),
        mrr=draw(_unit),
        ndcg_at_k=draw(_unit),
        grounding_precision=draw(_unit),
        grounding_recall=draw(_unit),
        semantic_similarity=draw(_unit),
        abstention_correct=abstention_correct,
        unwarranted_refusal=unwarranted_refusal,
    )

    judge_dims = ["faithfulness", "correctness", "completeness"]
    judge = JudgeScores(
        faithfulness=draw(_finite),
        correctness=draw(_finite),
        completeness=draw(_finite),
        judge_sample_count=draw(st.integers(min_value=1, max_value=10)),
        judge_model=draw(_text),
        judge_dim_sd=draw(
            st.dictionaries(st.sampled_from(judge_dims), _finite, max_size=3)
        ),
    )
    quality = QualityScores(
        accuracy=accuracy,
        judge=judge,
        composite=draw(_unit),
        composite_weights_version=draw(_text),
    )

    fragment_ids = draw(st.lists(_text, max_size=6))
    confidence = draw(
        st.lists(_unit, min_size=len(fragment_ids), max_size=len(fragment_ids))
    )

    model = draw(_text)
    item_id = draw(_text)
    rep = draw(st.integers(min_value=0, max_value=50))
    pass_name = draw(st.sampled_from(["wide", "deep", "targeted", "pilot"]))
    plan_version = draw(_text)

    return TrialEvent(
        trial_id=trial_id(model, item_id, rep, pass_name, plan_version),
        schema_version=SCHEMA_VERSION,
        plan_version=plan_version,
        model=model,
        item_id=item_id,
        turn_type=cohort.turn_type,
        pass_name=pass_name,
        rep=rep,
        temperature=draw(_unit),
        cohort=cohort,
        query=draw(_text),
        gold_node_ids=draw(st.lists(_text, max_size=5)),
        answerability=answerability,
        retrieval=RetrievalRecord(
            fragment_ids=fragment_ids,
            confidence=confidence,
            cache_hit=draw(st.booleans()),
        ),
        answer_text=draw(_text),
        token_usage=draw(
            st.dictionaries(st.sampled_from(["prompt", "completion", "total"]),
                            _token_counts, max_size=3)
        ),
        timings=timings,
        quality=quality,
        started_at=draw(_text),
        completed_at=draw(_text),
        error=None,  # non-error events only (Property 5 quantifies over these)
    )


@settings(max_examples=200)
@given(event=trial_events())
def test_valid_event_validates_and_round_trips(event: TrialEvent):
    # (a) validate_event accepts every generated valid non-error event.
    validate_event(event)
    # (b) lossless round-trip through the JSONL serializer.
    assert from_jsonl(to_jsonl(event)) == event
