"""
Unit tests for :mod:`bakeoff.eval.models` (Task 1.2).

Covers the EvalInstance construction-time validation invariants from design C1:

* every metric value is clamped to ``[0, 1]`` (non-finite -> 0.0) (Req 1.3, P3);
* a :class:`MetricValue` is consistent iff ``value is None`` exactly when
  ``unavailable is True`` (Req 1.4, 2.3, P10);
* ``ragas`` and ``retrieval`` metric maps have disjoint key sets (Req 2.4, P9);
* a non-positive / non-finite ``latency_ms`` is flagged (recorded, not dropped);
* per-stage timings are stored separately from end-to-end latency (Req 7.3);
* ``from_dict(to_dict(x)) == x`` for a fully-populated instance (Req 7.4 round-trip).

Pure example-based tests, network-free.
"""
from __future__ import annotations

import math

import pytest

from bakeoff.eval.models import EvalInstance, MetricValue, StageTimings, clamp_unit


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _timings() -> StageTimings:
    return StageTimings(
        retrieval_ms=12.0,
        generation_ms=30.0,
        extra_ms={"rerank_ms": 4.0},
    )


def _instance(
    *,
    instance_id: str = "inst-1",
    latency_ms: float = 42.0,
    ragas: dict | None = None,
    retrieval: dict | None = None,
    status: str = "ok",
    error: str | None = None,
) -> EvalInstance:
    return EvalInstance(
        instance_id=instance_id,
        agent_id="agent-A",
        session_id="sess-1",
        instance_index=0,
        timestamp="2025-01-01T00:00:00Z",
        latency_ms=latency_ms,
        stage_timings=_timings(),
        corpus_size=1000,
        retrieval_cached=False,
        ragas=ragas if ragas is not None else {
            "faithfulness": MetricValue.available(
                0.9, ragas_version="0.2.1", bedrock_model_id="anthropic.claude"
            ),
            "answer_relevancy": MetricValue.missing(
                ragas_version="0.2.1", bedrock_model_id="anthropic.claude"
            ),
        },
        retrieval=retrieval if retrieval is not None else {
            "precision_at_k": MetricValue.available(0.5, k=5),
            "ndcg_at_k": MetricValue.available(0.75, k=5),
        },
        confidence=0.8,
        volume=168.0,
        cost=0.0012,
        prompt_id="prompt-7",
        category="profile",
        status=status,
        error=error,
    )


# ---------------------------------------------------------------------------
# clamp_unit / MetricValue range clamping (Req 1.3, P3)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, expected",
    [
        (0.0, 0.0),
        (1.0, 1.0),
        (0.37, 0.37),
        (1.5, 1.0),          # above range -> clamped down
        (-0.5, 0.0),         # below range -> clamped up
        (42.0, 1.0),
        (-3.0, 0.0),
    ],
)
def test_clamp_unit_clamps_to_range(raw, expected):
    assert clamp_unit(raw) == expected


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_clamp_unit_non_finite_becomes_zero(bad):
    assert clamp_unit(bad) == 0.0


@pytest.mark.parametrize(
    "raw, expected",
    [(1.5, 1.0), (-0.5, 0.0), (0.42, 0.42), (math.inf, 0.0), (math.nan, 0.0)],
)
def test_metric_value_clamps_on_construction(raw, expected):
    mv = MetricValue.available(raw)
    assert mv.value == expected
    assert mv.unavailable is False


# ---------------------------------------------------------------------------
# value / unavailable coupling (Req 1.4, 2.3, P10)
# ---------------------------------------------------------------------------
def test_available_metric_has_value_and_not_unavailable():
    mv = MetricValue.available(0.5)
    assert mv.value == 0.5
    assert mv.unavailable is False


def test_missing_metric_has_none_value_and_unavailable():
    mv = MetricValue.missing(k=5)
    assert mv.value is None
    assert mv.unavailable is True
    assert mv.k == 5


def test_value_none_with_unavailable_false_raises():
    with pytest.raises(ValueError, match="must set unavailable=True"):
        MetricValue(value=None, unavailable=False)


def test_value_present_with_unavailable_true_raises():
    with pytest.raises(ValueError, match="must have value=None"):
        MetricValue(value=0.5, unavailable=True)


def test_metric_value_coupling_holds_for_both_constructors():
    # value is None  <=>  unavailable is True, for every constructed value.
    for mv in (MetricValue.available(0.3), MetricValue.missing()):
        assert (mv.value is None) == mv.unavailable


# ---------------------------------------------------------------------------
# ragas / retrieval disjoint maps (Req 2.4, P9)
# ---------------------------------------------------------------------------
def test_disjoint_ragas_and_retrieval_maps_ok():
    inst = _instance()  # default maps are disjoint
    assert set(inst.ragas).isdisjoint(set(inst.retrieval))


def test_overlapping_metric_key_raises():
    shared = {"faithfulness": MetricValue.available(0.9)}
    with pytest.raises(ValueError, match="disjoint"):
        _instance(
            ragas={"faithfulness": MetricValue.available(0.9)},
            retrieval={
                "faithfulness": MetricValue.available(0.5, k=5),  # collides with ragas
            },
        )
    # the same key in both maps is the violation, regardless of value
    assert "faithfulness" in shared


# ---------------------------------------------------------------------------
# latency flagging (non-positive / non-finite recorded, not dropped) (P7)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("good", [0.001, 1.0, 42.0, 1e6])
def test_positive_finite_latency_is_not_flagged(good):
    inst = _instance(latency_ms=good)
    assert inst.latency_flagged is False


@pytest.mark.parametrize("bad", [0.0, -1.0, -42.0, math.inf, -math.inf, math.nan])
def test_non_positive_or_non_finite_latency_is_flagged_not_dropped(bad):
    inst = _instance(latency_ms=bad)
    # the record is still constructed (not dropped) and carries the flag.
    assert isinstance(inst, EvalInstance)
    assert inst.latency_flagged is True
    # the raw value is preserved for traceability (nan compares unequal to itself)
    if math.isnan(bad):
        assert math.isnan(inst.latency_ms)
    else:
        assert inst.latency_ms == bad


# ---------------------------------------------------------------------------
# status validation (Req 5.5)
# ---------------------------------------------------------------------------
def test_failed_status_is_allowed():
    inst = _instance(status="failed", error="boom", latency_ms=5.0)
    assert inst.status == "failed"
    assert inst.error == "boom"


def test_unknown_status_raises():
    with pytest.raises(ValueError, match="status must be one of"):
        _instance(status="weird")


# ---------------------------------------------------------------------------
# per-stage timings kept separate from end-to-end latency (Req 7.3)
# ---------------------------------------------------------------------------
def test_stage_timings_are_independent_of_latency():
    # latency is chosen to differ from the stage sum (12 + 30 = 42) so the
    # independence is unambiguous: the model never ties the two together.
    inst = _instance(latency_ms=100.0)
    # latency is the end-to-end signal; per-stage timings are stored separately
    # and are NOT derived from or summed into latency_ms.
    assert inst.latency_ms == 100.0
    assert inst.stage_timings.retrieval_ms == 12.0
    assert inst.stage_timings.generation_ms == 30.0
    assert inst.stage_timings.extra_ms == {"rerank_ms": 4.0}
    # the sum of stages (42.0) deliberately differs from latency_ms (100.0):
    # the model never derives or constrains one from the other.
    assert inst.latency_ms != (
        inst.stage_timings.retrieval_ms + inst.stage_timings.generation_ms
    )


def test_stage_timings_allow_missing_stages():
    st = StageTimings()
    assert st.retrieval_ms is None
    assert st.generation_ms is None
    assert st.extra_ms == {}


# ---------------------------------------------------------------------------
# JSON round-trip: from_dict(to_dict(x)) == x  (Req 7.4)
# ---------------------------------------------------------------------------
def test_metric_value_round_trip():
    for mv in (
        MetricValue.available(0.6, ragas_version="0.2.1", bedrock_model_id="m"),
        MetricValue.available(0.6, k=5),
        MetricValue.missing(k=10),
        MetricValue.missing(),
    ):
        assert MetricValue.from_dict(mv.to_dict()) == mv


def test_stage_timings_round_trip():
    st = _timings()
    assert StageTimings.from_dict(st.to_dict()) == st


def test_eval_instance_round_trip_full():
    inst = _instance()
    restored = EvalInstance.from_dict(inst.to_dict())
    assert restored == inst


def test_eval_instance_round_trip_failed_with_empty_maps():
    inst = _instance(
        instance_id="inst-failed",
        ragas={},
        retrieval={},
        status="failed",
        error="ThrottlingException after 3 retries",
        latency_ms=7.5,
    )
    restored = EvalInstance.from_dict(inst.to_dict())
    assert restored == inst
    assert restored.ragas == {}
    assert restored.retrieval == {}


def test_eval_instance_to_dict_is_json_serializable():
    import json

    inst = _instance()
    # the durable store + HTTP seam require a JSON-serializable, single-line dict.
    line = json.dumps(inst.to_dict(), separators=(",", ":"))
    assert "\n" not in line
    assert EvalInstance.from_dict(json.loads(line)) == inst
