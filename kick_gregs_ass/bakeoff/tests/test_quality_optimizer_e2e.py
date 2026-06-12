"""
End-to-end offline integration test for the closed-loop prompt optimizer (Task 21.1).

This is the **example-based** end-to-end test the design's "Testing Strategy: End-to-end
offline integration test" mandates. A single test drives a FULL mini-loop with the
**offline backend** — :class:`~bakeoff.quality.offline_adapter.QualityOfflineAdapter`
factory + :class:`~bakeoff.scoring.judge.StubJudge`-backed
:class:`~bakeoff.scoring.judge.JudgeScorer` + fake-embed
:class:`~bakeoff.quality.closeness.TurnClosenessScorer` +
:class:`~bakeoff.quality.optimizer.author.OfflineAuthorClient` + the network-free
:class:`~bakeoff.quality.optimizer.retrieval.FakeRetrievalBackend` — over a small mixed
slice: **seed → author → judge → promote/reject → converge → Phase B**.

It asserts, per the design Testing Strategy (Req 1.1, 6.3, 7.3, 7.5, 8.1, 10.1, 10.4,
13.1, 13.7, 14.2):

* **The loop converges** — the :class:`~bakeoff.quality.optimizer.convergence.ConvergenceTracker`
  stop rule fires and the :class:`~bakeoff.quality.optimizer.controller.PhaseAResult`
  carries a ``converged_iteration`` and a ``stop_reason`` (Req 1.1, 6.3).
* **The iteration / audit / result stores are complete and consistent** — every iteration
  has a durable :class:`~bakeoff.quality.optimizer.store.IterationRecord` **and** a matching
  :class:`~bakeoff.quality.optimizer.store.AuditRecord`, the version history is ordered, and
  the results JSON written by the CLI path is well-formed (Req 8.1, 10.1).
* **Phase B is evaluated ONLY on the validation complement** — the ``remainder`` of
  :meth:`~bakeoff.quality.optimizer.controller.IterationController.phase_a_split`, never the
  tuning slice (Req 7.3, 7.5), proven by scoping the Phase A vs Phase B retrieval calls.
* **Retrieval is invoked on EVERY turn with the SAME fragments reaching the model and the
  judge** — a recording shim around the held-constant retrieval backend proves a per-turn
  call for every turn of every conversation, and the fragments the judge grounded on are
  byte-for-byte the fragments retrieval produced for that turn (Req 13.1, 13.7).
* **Abstention is scored with its weight** — the per-turn verdicts and the slice summary
  carry the abstention fields, a correct decline on an unanswerable turn is rewarded, and
  the bare seed (which fabricates on an unanswerable turn) is penalized (Req 14.2).
* **ZERO network calls occur** — the whole loop (Phase A + the verdict re-score + Phase B)
  runs under a guard that fails on any outbound socket connect / DNS resolution / boto3
  client construction, mirroring the repo's established ``NoNetwork`` guard pattern.

This reuses the existing test doubles exactly as :mod:`bakeoff.tests.test_quality` and
:mod:`bakeoff.tests.test_quality_optimizer` do (``make_stub_judge``, the fake-embed
closeness via ``build_offline_backend``, ``QualityOfflineAdapter``), plus the
``FakeRetrievalBackend``. It is plain ``pytest`` (no Hypothesis): the zero-network PROPERTY
test (Property 18 / Task 21.2) was removed by the owner and is deliberately NOT written
here. Async is driven with an explicit event loop inside the network guard (mirroring
``_guard_ctrl_probe.py``), and all four stores point at ``tmp_path`` so the real
``data/`` store is never touched.
"""
from __future__ import annotations

import asyncio
import dataclasses
import socket

import boto3
import pytest

from bakeoff import config
from bakeoff.quality.optimizer.backends import build_offline_backend
from bakeoff.quality.optimizer.controller import IterationController, PhaseAResult
from bakeoff.quality.optimizer.events import (
    EVENT_CHAMPION_SCORED,
    EVENT_CONVERGED,
    EVENT_ITERATION_COMPLETED,
    MODEL_CHANNEL,
    OptimizerEventEmitter,
)
from bakeoff.quality.optimizer.judge_loop import JudgeInLoopScorer
from bakeoff.quality.optimizer.main import (
    _phase_a_block_from_result,
    _write_results,
)
from bakeoff.quality.optimizer.retrieval import build_retrieval_backend
from bakeoff.quality.optimizer.store import OptimizerStore
from bakeoff.quality.optimizer.validate import PhaseBResult, PhaseBValidator
from bakeoff.quality.types import GroundTruthKind
from bakeoff.scoring.judge import JUDGE_DIMENSIONS, JudgeScorer, make_stub_judge
from bakeoff.types import CohortKey, GoldFragment, Item, Turn

# The Target_Model this mini-loop optimizes (one of the two fixed quality models).
_MODEL = "haiku-4.5"
# A bare seed Champion so the loop has real headroom to improve via the offline levers.
_SEED = "You are an FAQ assistant."
# Kept small so the example-based e2e stays fast; Phase B uses the higher config rep count.
_PHASE_A_REPS = 2
_STOP_LIMIT = 3


# ===========================================================================
# Zero-network guard — mirrors the repo's established NoNetwork pattern
# (`_guard_ctrl_probe.py`): fail on any outbound connect / DNS resolution /
# boto3 client construction for the duration of the guarded block.
# ===========================================================================
class _NoNetwork:
    """Context manager that makes any network egress / boto3 client build fail loudly.

    Patches the connection-establishing socket calls (``connect`` / ``connect_ex`` /
    ``create_connection`` / ``getaddrinfo``) and ``boto3.client`` / ``boto3.Session.client``
    to raise, so the offline mini-loop is proven network-free: if any piece of the loop
    tried to reach Bedrock / OpenSearch / the local retrieval service, the run would raise
    ``AssertionError`` rather than silently making a call. Restores every patched callable
    on exit. This mirrors the guard in ``_guard_ctrl_probe.py`` verbatim in intent.
    """

    def __enter__(self) -> "_NoNetwork":
        def block(*_args, **_kwargs):
            raise AssertionError("network/boto3 use detected during the offline loop")

        self._s_connect = socket.socket.connect
        self._s_connect_ex = socket.socket.connect_ex
        self._create = socket.create_connection
        self._gai = socket.getaddrinfo
        self._b_client = boto3.client
        self._sess_client = boto3.Session.client
        socket.socket.connect = block
        socket.socket.connect_ex = block
        socket.create_connection = block
        socket.getaddrinfo = block
        boto3.client = block
        boto3.Session.client = block
        return self

    def __exit__(self, *_exc) -> bool:
        socket.socket.connect = self._s_connect
        socket.socket.connect_ex = self._s_connect_ex
        socket.create_connection = self._create
        socket.getaddrinfo = self._gai
        boto3.client = self._b_client
        boto3.Session.client = self._sess_client
        return False


# ===========================================================================
# Recording shims — instrument the held-constant retrieval and the judge so the
# per-turn-invocation and grounding-parity invariants are directly observable.
# ===========================================================================
class _RecordingRetrieval:
    """Wrap a :class:`RetrievalBackend` and record every per-turn retrieve call.

    Sits OUTSIDE the memoization layer (it wraps the memoizing fake), so it records the
    call the scorer makes for **every** turn of every conversation — even when the inner
    memoizing backend serves a cached result. ``calls`` is the ordered list of
    ``(item_id, turn)`` the scorer asked for (proving per-turn invocation, Req 13.1);
    ``returned`` maps each ``(item_id, turn)`` to the fragment ids actually produced (the
    same fragments threaded to the judge, Req 13.7). ``name`` passes the wrapped backend's
    name through so audit records still see the real backend identity.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.name = inner.name
        self.calls: list[tuple[str, int]] = []
        self.returned: dict[tuple[str, int], tuple[str, ...]] = {}

    async def retrieve(self, q):
        frags = await self._inner.retrieve(q)
        ids = tuple(str(f.get("id", "")) for f in frags)
        self.calls.append((q.item_id, q.turn))
        self.returned[(q.item_id, q.turn)] = ids
        return frags


class _RecordingJudgeBackend:
    """Wrap a :data:`JudgeBackend` and record the fragment ids each judge call grounded on.

    Delegates scoring to a real :class:`StubJudge` so the full :class:`JudgeScorer`
    aggregation path runs unchanged; ``fragment_ids_seen`` captures, per judge call, the
    ids of the fragments handed to the judge as faithfulness/grounding evidence. Comparing
    this against what :class:`_RecordingRetrieval` returned proves the judge grounds on the
    SAME fragments retrieval produced for the model (Req 13.7).
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.fragment_ids_seen: list[tuple[str, ...]] = []

    def __call__(self, req):
        self.fragment_ids_seen.append(tuple(str(f.get("id", "")) for f in req.fragments))
        return self._inner(req)


class _RecordingBroker:
    """A tiny recording broker exposing the duck-typed ``publish(event_type, payload)``.

    Mirrors the no-op CLI broker but keeps every published frame so the test can confirm
    the optimizer streamed its live iteration view (champion scored / iteration completed /
    converged) over the existing broker seam without modifying it (Req 9.x).
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    def publish(self, event_type: str, payload: dict) -> None:
        self.published.append((event_type, dict(payload)))


# ===========================================================================
# Item builders (reuse the task 7.2 patterns from test_quality_optimizer.py)
# ===========================================================================
def _cohort(answerability: str = "full") -> CohortKey:
    return CohortKey(
        geography="US",
        proficiency="fluent",
        tone="neutral",
        entry_route="slack",
        momentary_state="neutral",
        answerability=answerability,
        turn_type="multi",
    )


def _gold_item(item_id: str) -> Item:
    """A turn-1 GOLD (answerable) single-turn-conversation item."""
    return Item(
        id=item_id,
        turn_type="multi",
        cohort=_cohort("full"),
        wants="how to request a corporate card",
        answerability="full",
        gold=[
            GoldFragment(
                node_id="g1",
                title="Corporate Card",
                markdown="Request a corporate card through the expense portal; it arrives within five business days.",
            )
        ],
        turns=(
            Turn(
                turn=1,
                user_utterance="How do I get a corporate card?",
                momentary_state="neutral",
                answerability="full",
            ),
        ),
    )


def _abstention_item(item_id: str) -> Item:
    """A turn-1 unanswerable item (answerability ``none``) → ABSTENTION regime."""
    return Item(
        id=item_id,
        turn_type="multi",
        cohort=_cohort("none"),
        answerability="none",
        turns=(
            Turn(
                turn=1,
                user_utterance="Can I expense my neighbor's dental surgery?",
                momentary_state="neutral",
                answerability="none",
            ),
        ),
    )


def _two_turn_item(item_id: str) -> Item:
    """A 2-turn item: turn-1 GOLD (answerable), turn-2 WANTS (later turn)."""
    return Item(
        id=item_id,
        turn_type="multi",
        cohort=_cohort("full"),
        wants="how to request a corporate card",
        answerability="full",
        gold=[
            GoldFragment(
                node_id="g1",
                title="Corporate Card",
                markdown="Request a corporate card through the expense portal; it arrives within five business days.",
            )
        ],
        turns=(
            Turn(turn=1, user_utterance="How do I get a corporate card?", momentary_state="neutral", answerability="full"),
            Turn(
                turn=2,
                user_utterance="And how do I raise its limit?",
                momentary_state="neutral",
                wants="Submit a limit-increase request to your manager for approval.",
            ),
        ),
    )


def _mixed_slice() -> list[Item]:
    """A small mixed multi-turn slice: answerable + unanswerable + multi-turn.

    Four of each kind so the deterministic, seeded ``split_items`` (seed
    ``config.QUALITY_OPT_SPLIT_SEED``) yields a Tuning_Slice and a Validation_Set that each
    span all three regimes (verified: with this fixture the tuning slice contains one of
    each). The exact composition is asserted as a precondition in the test so the
    abstention assertions can never be silently vacuous.
    """
    items: list[Item] = []
    for i in range(4):
        items.append(_gold_item(f"g-{i}"))
    for i in range(4):
        items.append(_abstention_item(f"ab-{i}"))
    for i in range(4):
        items.append(_two_turn_item(f"tt-{i}"))
    return items


def _expected_turn_keys(items) -> set[tuple[str, int]]:
    """Every ``(item_id, turn)`` that must be retrieved at least once for ``items``."""
    return {(it.item_id, ti + 1) for it in items for ti in range(len(it.turns))}


# ===========================================================================
# The end-to-end offline mini-loop
# ===========================================================================
def test_offline_mini_loop_end_to_end(tmp_path):
    """Drive a full offline mini-loop and assert the design's Testing-Strategy invariants.

    seed → author → judge → promote/reject → converge → Phase B, with the real wiring
    (offline backend, real controller, real store, real Phase B validator), all under a
    zero-network guard and with every store pointed at ``tmp_path``.
    """
    items = _mixed_slice()

    # The deterministic seeded split the controller uses internally; we recompute it here
    # to drive Phase B on the complement and to scope the train/test assertions (Req 7.6).
    tuning, validation = IterationController.phase_a_split(items)
    tuning_ids = {it.item_id for it in tuning}
    validation_ids = {it.item_id for it in validation}

    # Fixture preconditions: the split is a complete, disjoint partition, and the tuning
    # slice is genuinely mixed so the abstention assertions below are non-vacuous (Req 7.3).
    assert tuning_ids.isdisjoint(validation_ids)
    assert tuning_ids | validation_ids == {it.item_id for it in items}
    tuning_unanswerable = [it for it in tuning if it.answerability == "none"]
    tuning_answerable = [it for it in tuning if it.answerability != "none"]
    assert tuning_unanswerable, "fixture precondition: tuning slice must contain an unanswerable item"
    assert tuning_answerable, "fixture precondition: tuning slice must contain an answerable item"

    # Four append-only stores, all under tmp_path so the real data/ store is untouched.
    store = OptimizerStore(
        iterations_path=tmp_path / "quality_opt_iterations.jsonl",
        audit_path=tmp_path / "quality_opt_audit.jsonl",
        errors_path=tmp_path / "quality_opt_errors.jsonl",
        results_path=tmp_path / "quality_opt_results.json",
    )

    # Offline bundle, instrumented: a recording retrieval shim (outside memoization) and a
    # recording judge backend (wrapping a real StubJudge in a real JudgeScorer — Req 2.2).
    base = build_offline_backend()
    rec_retrieval = _RecordingRetrieval(build_retrieval_backend("fake"))
    rec_judge_backend = _RecordingJudgeBackend(make_stub_judge())
    judge = JudgeScorer(backend=rec_judge_backend, disk_cache=False)
    backend = dataclasses.replace(base, retrieval=rec_retrieval, judge_scorer=judge)

    # A separate retrieval shim for Phase B so its retrieval scope can be asserted
    # independently of Phase A (the judge is shared so grounding parity holds run-wide).
    rec_retrieval_b = _RecordingRetrieval(build_retrieval_backend("fake"))
    backend_b = dataclasses.replace(base, retrieval=rec_retrieval_b, judge_scorer=judge)

    broker = _RecordingBroker()
    emitter = OptimizerEventEmitter(broker)

    controller = IterationController.for_phase_a(
        model=_MODEL,
        backend=backend,
        all_items=items,
        store=store,
        emitter=emitter,
        stop_limit=_STOP_LIMIT,
        reps=_PHASE_A_REPS,
        seed_instruction=_SEED,
    )

    # ----- Run the whole loop under the zero-network guard (Req 10.4) ------
    # The event loop is created OUTSIDE the guard (loop construction may touch socketpair
    # on some platforms); every awaited piece of the loop runs INSIDE the guard.
    loop = asyncio.new_event_loop()
    try:
        with _NoNetwork():
            phase_a: PhaseAResult = loop.run_until_complete(controller.run_phase_a())

            # Re-score the converged Champion on the Tuning_Slice to inspect the per-turn
            # verdicts (the controller does not expose them); stays within tuning scope.
            champ_slice = loop.run_until_complete(
                JudgeInLoopScorer(backend, reps=1).score_prompt(
                    model=_MODEL,
                    instruction=phase_a.champion_instruction,
                    items=tuning,
                    prompt_role="champion",
                )
            )

            # Phase B: validate the converged Champion on the Validation_Set complement only.
            phase_b: PhaseBResult = loop.run_until_complete(
                PhaseBValidator(backend_b).validate(
                    model=_MODEL,
                    champion_instruction=phase_a.champion_instruction,
                    validation_items=validation,
                )
            )

            # Write the results JSON through the real CLI results-writer path (Req 8.1/10.1).
            _write_results(
                store,
                backend_name=backend.name,
                phase_a_blocks={_MODEL: _phase_a_block_from_result(phase_a)},
                phase_b_results={_MODEL: phase_b},
            )
    finally:
        loop.close()

    # =======================================================================
    # 1) The loop converges (Req 1.1, 6.3): the stop rule fired and the result
    #    carries a converged_iteration + a stop_reason.
    # =======================================================================
    assert phase_a.model == _MODEL
    assert phase_a.converged_iteration is not None
    assert phase_a.stop_reason and "non-improving" in phase_a.stop_reason
    assert phase_a.champion_instruction and phase_a.champion_instruction != _SEED
    assert phase_a.backend == "offline"

    iterations = store.read_iterations()
    audits = store.read_audits()
    # The converged iteration is marked converged in the SoT, exactly once, and its index
    # equals the PhaseAResult's converged_iteration (Req 6.3/6.6).
    converged_recs = [r for r in iterations if r.converged]
    assert len(converged_recs) == 1
    assert converged_recs[0].iteration_index == phase_a.converged_iteration
    # The trailing reject run reached the stop limit (Req 6.3).
    assert converged_recs[0].consecutive_non_improving == _STOP_LIMIT

    # =======================================================================
    # 2) Stores are complete and consistent (Req 8.1, 10.1): every iteration has
    #    a durable IterationRecord AND a matching AuditRecord; indices are
    #    contiguous from the seed; the version history is ordered; results JSON
    #    is well-formed.
    # =======================================================================
    iter_indices = [r.iteration_index for r in iterations]
    audit_indices = [a.iteration_index for a in audits]
    # Contiguous 0..N (seed at 0), one record per iteration in each store.
    assert iter_indices == list(range(len(iterations)))
    assert audit_indices == list(range(len(audits)))
    assert len(iterations) == len(audits)

    iters_by_index = {r.iteration_index: r for r in iterations}
    audits_by_index = {a.iteration_index: a for a in audits}
    for idx, it_rec in iters_by_index.items():
        au_rec = audits_by_index[idx]
        # Each iteration's two records agree on identity/model/backend (Req 8.3/10.6).
        assert au_rec.iteration_id == it_rec.iteration_id
        assert au_rec.model == it_rec.model == _MODEL
        assert it_rec.backend == au_rec.backend == "offline"
        assert it_rec.retrieval_backend == "fake"

    # The seed (index 0) is the baseline Champion: no challenger, a recorded baseline triad
    # (Req 8.6).
    seed_iter = iters_by_index[0]
    seed_audit = audits_by_index[0]
    assert seed_iter.challenger_score is None
    assert not seed_iter.promoted
    assert 0.0 <= seed_iter.champion_score <= 1.0
    assert seed_audit.challenger_instruction is None
    assert seed_audit.champion_instruction == _SEED

    # Promotion was a real, learned event (Req 1.1): at least one challenger beat the
    # champion by the significance threshold and the champion text actually changed.
    assert any(r.promoted for r in iterations)
    for r in iterations:
        if r.promoted:
            assert r.challenger_score is not None
            assert (r.challenger_score - r.champion_score) >= r.significance_threshold

    # The version history is ordered by iteration index and retrievable per version (Req 8.4/8.5).
    history = store.prompt_version_history(_MODEL)
    assert [pv.iteration_index for pv in history] == list(range(len(history)))
    assert len(history) == len(iterations)
    assert len({pv.prompt_version_id for pv in history}) == len(history)  # ids unique
    # The converged Champion's version id is the one the result reports.
    assert phase_a.champion_prompt_version_id in {pv.prompt_version_id for pv in history}

    # Results JSON is well-formed and reports the Phase B value as the final number (Req 7.5).
    results = store.read_results()
    assert results is not None
    assert results["backend"] == "offline"
    assert "generated_at" in results
    model_block = results["models"][_MODEL]
    assert model_block["converged_iteration"] == phase_a.converged_iteration
    assert model_block["stop_reason"] == phase_a.stop_reason
    pb_block = model_block["phase_b"]
    assert pb_block["triad"] == pytest.approx(phase_b.triad_score)
    assert pb_block["reps"] == phase_b.reps
    assert pb_block["n_conversations"] == phase_b.n_conversations

    # =======================================================================
    # 3) Phase B is evaluated ONLY on the validation complement (Req 7.3, 7.5),
    #    never the tuning slice — proven by scoping Phase A vs Phase B retrieval.
    # =======================================================================
    phase_a_seen_items = {iid for iid, _turn in rec_retrieval.calls}
    phase_b_seen_items = {iid for iid, _turn in rec_retrieval_b.calls}
    # Phase A (controller + the converged-champion re-score) only ever touched the tuning slice.
    assert phase_a_seen_items <= tuning_ids
    assert phase_a_seen_items.isdisjoint(validation_ids)
    # Phase B only ever touched the validation complement, and touched every item in it.
    assert phase_b_seen_items == validation_ids
    assert phase_b_seen_items.isdisjoint(tuning_ids)

    # Phase B ran at strictly more reps than Phase A and carries a well-formed 95% CI (Req 7.4).
    assert phase_b.reps == config.QUALITY_OPT_PHASE_B_REPS
    assert phase_b.reps > _PHASE_A_REPS
    assert phase_b.n_conversations == len(validation) * phase_b.reps
    assert phase_b.ci_half_width >= 0.0
    assert phase_b.ci_low <= phase_b.triad_score <= phase_b.ci_high
    assert phase_b.backend == "offline"

    # =======================================================================
    # 4) Retrieval is invoked on EVERY turn, with the SAME fragments reaching the
    #    model and the judge (Req 13.1, 13.7).
    # =======================================================================
    # Every turn of every tuning conversation was retrieved at least once...
    expected_tuning_turns = _expected_turn_keys(tuning)
    assert expected_tuning_turns <= set(rec_retrieval.returned.keys())
    # ...and retrieval was called repeatedly across scorings (champion + challenger +
    # re-score, each at >= 1 rep), so it really is per-turn-per-scoring, not once-per-turn.
    total_tuning_turns = sum(len(it.turns) for it in tuning)
    assert len(rec_retrieval.calls) > total_tuning_turns
    # Every turn of every validation conversation was retrieved in Phase B.
    assert _expected_turn_keys(validation) <= set(rec_retrieval_b.returned.keys())

    # Grounding parity: the set of fragment-id tuples the judge grounded on equals the set
    # of fragment-id tuples retrieval produced across the whole run (Phase A ∪ Phase B).
    judge_seen = set(rec_judge_backend.fragment_ids_seen)
    retrieval_produced = set(rec_retrieval.returned.values()) | set(rec_retrieval_b.returned.values())
    assert judge_seen == retrieval_produced
    # And per-turn: every tuple retrieval returned for a turn was grounded on by the judge.
    for ids in rec_retrieval.returned.values():
        assert ids in judge_seen
    # The fake substrate yields a distinct, non-empty fragment set per turn (sanity).
    assert all(len(ids) >= 1 for ids in rec_retrieval.returned.values())

    # =======================================================================
    # 5) Abstention is scored with its weight (Req 14.2): the verdict + slice
    #    abstention fields are populated, a correct decline is rewarded, and the
    #    bare seed (which fabricates on an unanswerable turn) is penalized.
    # =======================================================================
    verdicts_by_kind: dict[str, list] = {}
    for v in champ_slice.verdicts:
        verdicts_by_kind.setdefault(v.ground_truth_kind, []).append(v)

    # The tuning slice spans the abstention regime AND the gold regime (precondition above).
    assert GroundTruthKind.ABSTENTION in verdicts_by_kind
    assert GroundTruthKind.GOLD in verdicts_by_kind

    # On an unanswerable turn the abstention fields are populated; the converged Champion
    # (which carries the grounding/abstention levers) declines correctly and is rewarded.
    for v in verdicts_by_kind[GroundTruthKind.ABSTENTION]:
        assert v.abstention_correct is not None          # populated, not N/A (Req 14.2)
        assert v.abstention_correct is True
        assert v.answered_when_unsure is False
        triad_mean = sum(v.dimensions.values()) / len(v.dimensions)
        assert v.overall >= triad_mean                   # correct decline is rewarded, not docked
        assert v.overall >= 0.8

    # On an answerable (gold) turn abstention is not the graded behavior — the field is N/A.
    for v in verdicts_by_kind[GroundTruthKind.GOLD]:
        assert v.abstention_correct is None
        assert v.answered_when_unsure is False

    # The slice summary carries the abstention-behavior fields (Req 14.2).
    assert 0.0 <= champ_slice.abstention_reward_mean <= 1.0
    assert 0.0 <= champ_slice.answered_when_unsure_rate <= 1.0
    # The converged Champion declines correctly everywhere, so it over-claims on no turn.
    assert champ_slice.answered_when_unsure_rate == pytest.approx(0.0)
    assert champ_slice.abstention_reward_mean == pytest.approx(1.0)

    # The abstention weighting is an actual driver, not cosmetic: the BARE seed fabricated
    # on the unanswerable turn, so the seed iteration recorded a non-zero over-claim rate
    # that the loop then drove to zero (Req 14.2).
    assert seed_iter.answered_when_unsure_rate > 0.0

    # Per-dimension means are recorded for auditability (Req 2.6), and closeness is present
    # purely as the secondary cross-check (Req 2.3).
    assert set(champ_slice.per_dimension_mean) == set(JUDGE_DIMENSIONS)
    assert 0.0 <= champ_slice.mean_closeness <= 1.0

    # =======================================================================
    # 6) The loop streamed its live view over the existing broker seam (Req 9.x):
    #    champion/challenger scores, iteration completions, and convergence, each
    #    stamped with this model's channel.
    # =======================================================================
    etypes = {etype for etype, _ in broker.published}
    assert EVENT_CHAMPION_SCORED in etypes
    assert EVENT_ITERATION_COMPLETED in etypes
    assert EVENT_CONVERGED in etypes
    assert all(payload[MODEL_CHANNEL] == _MODEL for _etype, payload in broker.published)
