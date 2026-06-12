"""
Property-based tests for :mod:`bakeoff.runner` (Task 10).

Two universal Correctness Properties from the design are exercised with Hypothesis
over randomized small plans (varying #models, #items, reps, and answerability
classes). Example counts are kept modest so the suite stays fast while still
sweeping the input space.

* **P2 — every planned trial is recorded exactly once** after a completed run.
  For any generated plan, ``schedule_run`` produces exactly one durable event per
  ``planned_trials`` spec — no duplicates, none dropped — and publishes exactly
  one completion signal per appended event.
  **Validates: Requirements 7.3, 7.5, 14.1**

* **P3 — resume is idempotent.** Re-invoking ``schedule_run`` on an already-complete
  log runs ZERO new trials and leaves the event set unchanged. Splitting the same
  plan across two runs (resume after a partial first pass) yields exactly the
  planned set, each trial once.
  **Validates: Requirements 7.4, 12.3, 13.1, 14.1, 14.3**

All OFFLINE (MockAdapter + FakeRetrieval + StubScoring), driven with
``asyncio.run`` inside sync tests — no ``pytest-asyncio``.
"""
from __future__ import annotations

import asyncio
from collections import Counter

from hypothesis import given, settings
from hypothesis import strategies as st

from bakeoff.adapters.mock import MockAdapter
from bakeoff.eventlog import read_events
from bakeoff.runner import (
    RunStatus,
    planned_trials,
    resume_point,
    schedule_run,
)
from bakeoff.tests.test_runner import (
    FakeBroker,
    FakeRetrieval,
    StubScoring,
    _instant_sleep,
    build_plan,
    make_item,
)

_ANSWERABILITY = st.sampled_from(["full", "partial", "none"])


@st.composite
def small_plans(draw):
    """Generate ``(plan, models, items)`` for a small randomized run.

    Varies the number of models (1-3), items (1-6, each with a drawn
    answerability class so multiple strata arise), and reps per pass (1-3).
    """
    n_models = draw(st.integers(min_value=1, max_value=3))
    n_items = draw(st.integers(min_value=1, max_value=6))
    reps = draw(st.integers(min_value=1, max_value=3))

    models = [MockAdapter(name=chr(ord("A") + i), seed=i) for i in range(n_models)]
    items = [
        make_item(f"i{idx}", answerability=draw(_ANSWERABILITY))
        for idx in range(n_items)
    ]
    plan = build_plan(items, reps=reps)
    return plan, models, items


def _run(plan, models, items, path, broker, **kw):
    return asyncio.run(
        schedule_run(
            plan, models, path, broker,
            items=items, retr=FakeRetrieval(), scoring=StubScoring(),
            resilience_sleep=_instant_sleep, **kw,
        )
    )


# ===========================================================================
# P2 — every planned trial recorded exactly once
# **Validates: Requirements 7.3, 7.5, 14.1**
# ===========================================================================
@settings(max_examples=60, deadline=None)
@given(plan_models_items=small_plans())
def test_p2_every_planned_trial_recorded_exactly_once(plan_models_items, tmp_path_factory):
    plan, models, items = plan_models_items
    path = tmp_path_factory.mktemp("p2") / "events.jsonl"
    broker = FakeBroker()

    ctrl = _run(plan, models, items, path, broker)

    planned_ids = [s.trial_id for s in planned_trials(plan, models)]
    assert len(planned_ids) == len(set(planned_ids))  # planner gives unique ids

    events = read_events(path)
    counts = Counter(ev.trial_id for ev in events)
    # exactly one event per planned trial; nothing extra, nothing dropped.
    assert set(counts) == set(planned_ids)
    assert all(c == 1 for c in counts.values())
    assert len(events) == len(planned_ids)
    # exactly one broker publish per appended event.
    assert len(broker.published) == len(events)
    assert ctrl.status == RunStatus.COMPLETED


# ===========================================================================
# P3 — resume is idempotent
# **Validates: Requirements 7.4, 12.3, 13.1, 14.1, 14.3**
# ===========================================================================
@settings(max_examples=60, deadline=None)
@given(plan_models_items=small_plans())
def test_p3_resume_runs_zero_new_trials(plan_models_items, tmp_path_factory):
    plan, models, items = plan_models_items
    path = tmp_path_factory.mktemp("p3") / "events.jsonl"

    _run(plan, models, items, path, FakeBroker())
    events_first = read_events(path)

    # Re-invoke on the complete log: zero new trials, unchanged event set.
    broker2 = FakeBroker()
    ctrl2 = _run(plan, models, items, path, broker2)
    events_second = read_events(path)

    assert len(broker2.published) == 0
    assert len(events_second) == len(events_first)
    assert {ev.trial_id for ev in events_second} == {ev.trial_id for ev in events_first}
    assert ctrl2.status == RunStatus.COMPLETED


@settings(max_examples=40, deadline=None)
@given(plan_models_items=small_plans())
def test_p3_split_run_resumes_to_exactly_the_planned_set(plan_models_items, tmp_path_factory):
    """A run aborted partway, then resumed, yields exactly the planned set once.

    The first pass aborts after a few completions (simulating a crash); the resume
    completes the remainder. The union is exactly ``planned_trials``, each once
    (Property 3 across a real partial→resume boundary).
    """
    plan, models, items = plan_models_items
    path = tmp_path_factory.mktemp("p3split") / "events.jsonl"

    planned_ids = {s.trial_id for s in planned_trials(plan, models)}

    # Pass 1: abort after the 2nd completion via the controller.
    from bakeoff.runner import RunController

    controller = RunController()
    state = {"n": 0}

    class _AbortingRetr(FakeRetrieval):
        async def retrieve(self, query, filters=None):
            state["n"] += 1
            if state["n"] >= 2:
                controller.abort()
            return await super().retrieve(query, filters)

    asyncio.run(
        schedule_run(
            plan, models, path, FakeBroker(),
            items=items, retr=_AbortingRetr(), scoring=StubScoring(),
            controller=controller, resilience_sleep=_instant_sleep, max_concurrency=1,
        )
    )
    done_after_partial = resume_point(path)
    # an aborted run did not complete everything (unless the plan was tiny).
    assert done_after_partial <= planned_ids

    # Pass 2: resume to completion.
    _run(plan, models, items, path, FakeBroker())

    events = read_events(path)
    counts = Counter(ev.trial_id for ev in events if ev.error is None)
    # every planned trial is present exactly once and successful.
    assert set(counts) == planned_ids
    assert all(c == 1 for c in counts.values())


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
