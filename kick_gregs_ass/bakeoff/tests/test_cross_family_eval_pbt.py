"""
Property-based + example tests for the cross-family evaluation feature
(spec optimizer-cross-family-eval; design "Correctness Properties" 1–8 and
"Testing Strategy").

The eight universal Correctness Properties from the design are each exercised by exactly one
Hypothesis property test (tagged below), and the example/non-regression checks pin the config
seams, the genericized author contract, the gates-off legacy path, and the held-constant
retrieval on the in-loop path. Everything here is zero-network: the loop-based properties
inject the offline backend (or scripted scorers) and fakes, exactly as the existing optimizer
PBT suite does.
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bakeoff import config
from bakeoff.quality.optimizer import audit
from bakeoff.quality.optimizer.audit import (
    AuditItem,
    AuditSample,
    AuditSeam,
    contains_authorship_markers,
    evaluate_self_preference,
    obfuscate,
    ranking_divergence,
)
from bakeoff.quality.optimizer.author import (
    AuthoredChallenger,
    BedrockAuthorClient,
    build_author_prompt,
)
from bakeoff.quality.optimizer.backends import (
    AuthorJudgeConflictError,
    AuthorJudgeFamilyConflictError,
    OptimizerBackend,
    build_live_backend,
    model_family,
)
from bakeoff.quality.optimizer.events import (
    EVENT_ITERATION_COMPLETED,
    OptimizerEventEmitter,
)
from bakeoff.quality.optimizer.judge_loop import JudgeInLoopScorer, SliceScore
from bakeoff.quality.optimizer.rungs import build_rung_ladder
import dataclasses

from bakeoff.scoring.judge import JUDGE_DIMENSIONS
from bakeoff.types import CohortKey, GoldFragment, Item, Turn


# ===========================================================================
# Shared helpers
# ===========================================================================
@contextlib.contextmanager
def _config(**overrides):
    """Temporarily set ``config`` attributes, restoring them on exit.

    Used instead of the ``monkeypatch`` fixture inside ``@given`` tests so the gate flips are
    per-example and never trip Hypothesis's function-scoped-fixture health check.
    """
    saved = {k: getattr(config, k) for k in overrides}
    try:
        for k, v in overrides.items():
            setattr(config, k, v)
        yield
    finally:
        for k, v in saved.items():
            setattr(config, k, v)


class _RecordingBroker:
    """A duck-typed SSE broker that records every ``publish(event_type, payload)``."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    def publish(self, event_type: str, payload: dict) -> None:
        self.published.append((event_type, dict(payload)))


def _lazy_factory():
    """A zero-arg client factory returning a sentinel (never actually called at build time)."""
    return lambda: object()


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
    """A turn-1 GOLD (answerable) single-turn item (mirrors the existing suites' fixture)."""
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
                markdown="Request a corporate card through the expense portal; it arrives in five business days.",
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


def _items() -> list[Item]:
    return [_gold_item("g-0"), _gold_item("g-1")]


def _slice_score(triad: float, role: str = "champion") -> SliceScore:
    """Build a minimal, well-formed :class:`SliceScore` with a chosen triad (verdicts empty)."""
    return SliceScore(
        model="m",
        prompt_role=role,
        triad_score=float(triad),
        ci_half_width=0.0,
        ci_low=float(triad),
        ci_high=float(triad),
        n_conversations=1,
        between_conv_sd=0.0,
        per_dimension_mean={d: float(triad) for d in JUDGE_DIMENSIONS},
        abstention_reward_mean=0.0,
        answered_when_unsure_rate=0.0,
        mean_closeness=float(triad),
        verdicts=(),
    )


class _CountingJudge:
    """Wrap a JudgeScorer, counting every ``score_detailed`` call (the Opus invocation)."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls = 0

    def score_detailed(self, *args, **kwargs):
        self.calls += 1
        return self._inner.score_detailed(*args, **kwargs)


class _CommentAuthor:
    """A usable-but-non-improving author: appends a unique comment (no lever marker).

    Different text each call (so the challenger is ``usable``) but adds no offline lever, so it
    never raises the in-loop closeness signal — the Round therefore never puts a candidate
    forward and the conclusion adjudicates the champion only.
    """

    author_model = "comment-author"

    def __init__(self) -> None:
        self.calls = 0

    async def author(self, *, target_model, champion_instruction, failures, stream=None):
        self.calls += 1
        text = f"{champion_instruction}\n<!-- round-iter {self.calls} -->"
        return AuthoredChallenger.build(
            instruction=text,
            rationale="non-improving comment",
            author_model=self.author_model,
            raw={},
            champion_instruction=champion_instruction,
        )


class _FixedAuthor:
    """An author that always returns one fixed, usable challenger text."""

    author_model = "fixed-author"

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    async def author(self, *, target_model, champion_instruction, failures, stream=None):
        self.calls += 1
        return AuthoredChallenger.build(
            instruction=self.text,
            rationale="fixed",
            author_model=self.author_model,
            raw={},
            champion_instruction=champion_instruction,
        )


class _ScriptedScorer:
    """A scorer whose in-loop and Opus rankings deliberately disagree (for Property 2).

    ``score_in_loop`` always favors the challenger (so the Round puts it forward), while
    ``score_prompt`` returns scripted Opus scores — so the promotion outcome can only follow
    the Opus (Round-conclusion) adjudication, never the In_Loop_Signal.
    """

    def __init__(self, champ_opus: float, cand_opus: float) -> None:
        self._champ_opus = champ_opus
        self._cand_opus = cand_opus

    async def score_in_loop(self, *, model, instruction, items, prompt_role, max_concurrency=None):
        return _slice_score(0.0 if prompt_role == "champion" else 1.0, role=prompt_role)

    async def score_prompt(self, *, model, instruction, items, prompt_role, max_concurrency=None):
        triad = self._champ_opus if prompt_role == "champion" else self._cand_opus
        return _slice_score(triad, role=prompt_role)


class _FakeAuditJudge:
    """A duck-typed Audit_Judge that records the material it was handed and returns scores."""

    def __init__(self, score: float = 0.5) -> None:
        self.seen_materials: list[str] = []
        self._score = score

    async def score_sample(self, items):
        self.seen_materials.extend(it.obfuscated_material for it in items)
        return [self._score for _ in items]


def _make_island(backend, *, style: str = "", threshold: float = 0.01):
    """Construct an IslandLoop over a 2-item ladder with a recording emitter."""
    from bakeoff.quality.optimizer.island import IslandLoop

    broker = _RecordingBroker()
    emitter = OptimizerEventEmitter(broker)
    island = IslandLoop(
        island_id=0,
        model="haiku-4.5",
        backend=backend,
        ladder=build_rung_ladder(_items()),
        store=object(),  # held but never used by the inner loop
        emitter=emitter,
        style=style,
        threshold=threshold,
    )
    return island, broker


def _accepted_from(broker: _RecordingBroker):
    """Return the ``accepted`` flag from the recorded iteration_completed event (or None)."""
    for etype, payload in broker.published:
        if etype == EVENT_ITERATION_COMPLETED:
            return payload.get("accepted")
    return None


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
_unit = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
_threshold = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
_family = st.sampled_from(["anthropic", "amazon", "meta", "mistral", "deepseek"])


@st.composite
def _equal_length_pairs(draw):
    n = draw(st.integers(min_value=0, max_value=8))
    a = draw(st.lists(_unit, min_size=n, max_size=n))
    b = draw(st.lists(_unit, min_size=n, max_size=n))
    return a, b


# ===========================================================================
# Property 1 — Round cadence keeps the Judge out of the in-round loop
# ===========================================================================
@given(n_steps=st.integers(min_value=0, max_value=6))
@settings(max_examples=30, deadline=None)
def test_property1_round_keeps_judge_out_of_in_round_loop(n_steps):
    """Feature: optimizer-cross-family-eval, Property 1: Round cadence keeps the Judge out of
    the in-round loop.

    Validates: Requirements 1.1, 1.2, 1.3, 1.5

    The Author is invoked exactly ``N`` times in-round, every in-round candidate is scored by
    the In_Loop_Signal only (zero Judge calls), and the Judge runs only at the Round's
    conclusion with a count that is bounded and INDEPENDENT of ``N`` (it does not grow with N).
    """
    from bakeoff.quality.optimizer.backends import build_offline_backend

    def run_round(n: int):
        offline = build_offline_backend()
        counting = _CountingJudge(offline.judge_scorer)
        author = _CommentAuthor()
        backend = dataclasses.replace(offline, judge_scorer=counting, author=author)
        island, _ = _make_island(backend)
        with _config(QUALITY_OPT_ROUND_CADENCE_ENABLED=True, QUALITY_OPT_ROUND_STEPS=n):
            asyncio.run(island.step())
        return counting.calls, author.calls

    # Baseline: N=0 -> no in-round author iterations, conclusion adjudicates the champion only.
    base_judge, base_author = run_round(0)
    assert base_author == 0
    assert base_judge > 0  # the single Round-conclusion Opus adjudication did happen

    n_judge, n_author = run_round(n_steps)
    # The Author self-iterates exactly N times in-round (Req 1.1 / 1.5).
    assert n_author == n_steps
    # The Judge count is independent of N: in-round iterations made zero Judge calls and the
    # conclusion adjudication count does not grow with N (Req 1.2 / 1.3).
    assert n_judge == base_judge


# ===========================================================================
# Property 2 — Promotion is decided by the Round-conclusion Judge adjudication
# ===========================================================================
@given(champ_opus=_unit, cand_opus=_unit, threshold=st.floats(min_value=1e-3, max_value=1.0))
@settings(max_examples=60, deadline=None)
def test_property2_promotion_follows_round_conclusion_judge(champ_opus, cand_opus, threshold):
    """Feature: optimizer-cross-family-eval, Property 2: Promotion is decided by the
    Round-conclusion Judge adjudication.

    Validates: Requirements 1.4

    The In_Loop_Signal is rigged to always favor the challenger (so it is put forward), yet the
    promotion outcome equals ``PromotionDecider.decide(champ_opus, cand_opus, threshold,
    usable=True)`` — i.e. it follows the concluding Opus adjudication and never the in-loop
    ordering.
    """
    from bakeoff.quality.optimizer.backends import build_offline_backend
    from bakeoff.quality.optimizer.convergence import PromotionDecider

    backend = dataclasses.replace(build_offline_backend(), author=_FixedAuthor("CANDIDATE_PROMPT"))
    island, broker = _make_island(backend, threshold=threshold)
    # Override the scorer factory so both the in-loop and Opus scores are scripted.
    island._scorer_for = lambda reps: _ScriptedScorer(champ_opus, cand_opus)  # type: ignore[attr-defined]

    with _config(QUALITY_OPT_ROUND_CADENCE_ENABLED=True, QUALITY_OPT_ROUND_STEPS=1):
        asyncio.run(island.step())

    accepted = _accepted_from(broker)
    expected = PromotionDecider().decide(champ_opus, cand_opus, threshold, usable=True)
    assert accepted is expected
    # And the champion only moves to the candidate when the Opus adjudication promotes it.
    assert (island.champion_instruction == "CANDIDATE_PROMPT") is expected


# ===========================================================================
# Property 3 — Family-aware Author≠Judge guard
# ===========================================================================
@given(author_fam=_family, judge_fam=_family)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_property3_family_aware_guard(author_fam, judge_fam):
    """Feature: optimizer-cross-family-eval, Property 3: Family-aware Author≠Judge guard.

    Validates: Requirements 2.3, 2.4

    With the cross-family Author feature enabled, building the live backend raises
    ``AuthorJudgeFamilyConflictError`` IFF the resolved Author family equals the Judge family,
    and otherwise constructs the backend without raising.
    """
    same_family = author_fam == judge_fam
    with _config(
        QUALITY_OPT_CROSS_FAMILY_AUTHOR_ENABLED=True,
        QUALITY_OPT_AUTHOR_FAMILY=author_fam,
        QUALITY_OPT_JUDGE_FAMILY=judge_fam,
    ):
        def build():
            return build_live_backend(
                "vendor.some-author-model",  # explicit arg; family driven by declared above
                retrieval_backend="fake",
                judge_client_factory=_lazy_factory(),
                author_client_factory=_lazy_factory(),
                embedding_client_factory=_lazy_factory(),
            )

        if same_family:
            with pytest.raises(AuthorJudgeFamilyConflictError):
                build()
        else:
            backend = build()
            assert isinstance(backend, OptimizerBackend)
            assert backend.name == "live"


# ===========================================================================
# Property 4 — Provider-aware temperature handling
# ===========================================================================
class _CaptureConverseClient:
    """A fake Bedrock client that captures the kwargs of the last converse_stream call."""

    def __init__(self) -> None:
        self.last_kwargs: dict | None = None

    def converse_stream(self, **kwargs):
        self.last_kwargs = kwargs
        return {"stream": []}  # empty event stream -> no deltas


@given(accepts=st.booleans(), temperature=st.floats(min_value=0.0, max_value=1.0))
@settings(max_examples=100, deadline=None)
def test_property4_provider_aware_temperature(accepts, temperature):
    """Feature: optimizer-cross-family-eval, Property 4: Provider-aware temperature handling.

    Validates: Requirements 2.5, 2.6, 2.7

    The Bedrock request includes ``temperature`` in its ``inferenceConfig`` IFF the configured
    ``accepts_temperature`` flag is True, and omits it otherwise — following the configured
    provider flag for both values rather than a fixed Claude assumption.
    """
    client = _CaptureConverseClient()
    author = BedrockAuthorClient(
        "vendor.author-model",
        client=client,
        accepts_temperature=accepts,
        temperature=temperature,
    )
    author._invoke_stream_sync("contract text", None)

    assert client.last_kwargs is not None
    inference_config = client.last_kwargs["inferenceConfig"]
    assert ("temperature" in inference_config) is accepts
    if accepts:
        assert inference_config["temperature"] == temperature


# ===========================================================================
# Property 5 — Audit runs on the configured interval
# ===========================================================================
@given(interval=st.integers(min_value=1, max_value=5), round_index=st.integers(min_value=0, max_value=20))
@settings(max_examples=100, deadline=None)
def test_property5_audit_runs_on_interval(interval, round_index):
    """Feature: optimizer-cross-family-eval, Property 5: Audit runs on the configured interval.

    Validates: Requirements 3.2

    With the seam enabled, ``maybe_run`` returns a DivergenceReport exactly on the rounds where
    the cadence fires (rounds that are positive multiples of the interval) and ``None`` on every
    other round; with the seam disabled it always returns ``None``.
    """
    samples = [AuditSample(item_id="c0", material="text", proxy_score=0.5)]

    enabled_seam = AuditSeam(audit_judge=_FakeAuditJudge(), enabled=True, interval=interval, threshold=0.3)
    report = asyncio.run(enabled_seam.maybe_run(round_index=round_index, samples=samples))
    fires = round_index >= 1 and (round_index % interval == 0)
    assert (report is not None) is fires

    disabled_seam = AuditSeam(audit_judge=_FakeAuditJudge(), enabled=False, interval=interval, threshold=0.3)
    assert asyncio.run(disabled_seam.maybe_run(round_index=round_index, samples=samples)) is None


# ===========================================================================
# Property 6 — Material is obfuscated before it reaches the Audit_Judge
# ===========================================================================
_marker = st.sampled_from(
    [
        "Claude",
        "claude",
        "Opus",
        "Sonnet 4.6",
        "GPT-5",
        "<<<ISLAND_AUTHORING_STANCE>>>concise<<<END_ISLAND_AUTHORING_STANCE>>>",
        "Authoring stance for this island: be terse",
    ]
)


@given(prefix=st.text(max_size=40), marker=_marker, suffix=st.text(max_size=40))
@settings(max_examples=100, deadline=None)
def test_property6_obfuscation_before_audit(prefix, marker, suffix):
    """Feature: optimizer-cross-family-eval, Property 6: Material is obfuscated before it
    reaches the Audit_Judge.

    Validates: Requirements 3.3

    ``obfuscate`` removes all known authorship/style markers and is idempotent, and the audit
    path always submits obfuscated material — the Audit_Judge never receives a raw marker.
    """
    material = f"{prefix} {marker} {suffix}"

    # obfuscate removes every known marker and is idempotent.
    once = obfuscate(material)
    assert not contains_authorship_markers(once)
    assert obfuscate(once) == once

    # The seam scrubs before scoring: the fake Audit_Judge sees no raw marker.
    judge = _FakeAuditJudge()
    seam = AuditSeam(audit_judge=judge, enabled=True, interval=1, threshold=0.3)
    sample = AuditSample(item_id="c0", material=material, proxy_score=0.5)
    asyncio.run(seam.maybe_run(round_index=1, samples=[sample]))
    assert judge.seen_materials  # the audit actually ran
    for seen in judge.seen_materials:
        assert not contains_authorship_markers(seen)


# ===========================================================================
# Property 7 — Ranking-divergence measure is well-formed
# ===========================================================================
@given(pair=_equal_length_pairs(), xs=st.lists(_unit, max_size=8))
@settings(max_examples=150, deadline=None)
def test_property7_divergence_well_formed(pair, xs):
    """Feature: optimizer-cross-family-eval, Property 7: Ranking-divergence measure is
    well-formed.

    Validates: Requirements 3.4

    For any two equal-length vectors the divergence is in [0,1], is 0 for the same ordering
    (in particular ``divergence(x, x) == 0``), is symmetric, and is 1 for a fully reversed
    ordering.
    """
    a, b = pair
    d = ranking_divergence(a, b)
    assert 0.0 <= d <= 1.0
    # Symmetric.
    assert ranking_divergence(a, b) == ranking_divergence(b, a)
    # Identity-zero (same vector -> same ordering, including ties).
    assert ranking_divergence(xs, xs) == 0.0
    # Fully reversed ordering -> 1.0 (strictly distinct, ascending vs its reverse=descending).
    distinct = sorted(set(xs))
    if len(distinct) >= 2:
        assert ranking_divergence(distinct, distinct[::-1]) == 1.0


# ===========================================================================
# Property 8 — Self-preference flag fires iff divergence exceeds the threshold
# ===========================================================================
@given(pair=_equal_length_pairs(), threshold=_threshold)
@settings(max_examples=150, deadline=None)
def test_property8_flag_iff_divergence_exceeds_threshold(pair, threshold):
    """Feature: optimizer-cross-family-eval, Property 8: Self-preference flag fires iff
    divergence exceeds the threshold.

    Validates: Requirements 3.5

    ``evaluate_self_preference`` flags iff the computed divergence is strictly greater than the
    threshold.
    """
    a, b = pair
    report = evaluate_self_preference(a, b, threshold=threshold)
    assert report.divergence == ranking_divergence(a, b)
    assert report.flagged == (report.divergence > threshold)
    assert report.threshold == threshold
    assert report.n_items == len(a)


# ===========================================================================
# Example / non-regression tests
# ===========================================================================
def test_config_slot_wiring_author_and_audit(monkeypatch):
    """Req 2.1 / 3.1: with the gates on, the Author and Audit_Judge carry the ids from their
    SEPARATE config slots (not from QUALITY_MODELS)."""
    monkeypatch.setattr(config, "QUALITY_OPT_CROSS_FAMILY_AUTHOR_ENABLED", True)
    monkeypatch.setattr(config, "QUALITY_OPT_AUTHOR_MODEL_ID", "amazon.nova-pro-sentinel")
    monkeypatch.setattr(config, "QUALITY_OPT_AUTHOR_FAMILY", "amazon")
    monkeypatch.setattr(config, "QUALITY_OPT_AUDIT_ENABLED", True)
    monkeypatch.setattr(config, "QUALITY_OPT_AUDIT_JUDGE_MODEL_ID", "meta.llama-audit-sentinel")
    monkeypatch.setattr(config, "QUALITY_OPT_AUDIT_JUDGE_FAMILY", "meta")

    backend = build_live_backend(
        retrieval_backend="fake",
        judge_client_factory=_lazy_factory(),
        author_client_factory=_lazy_factory(),
        audit_client_factory=_lazy_factory(),
        embedding_client_factory=_lazy_factory(),
    )
    # Author resolved from the separate slot (a non-Anthropic id, not a QUALITY_MODELS id).
    assert backend.author.author_model == "amazon.nova-pro-sentinel"
    assert backend.author.author_model not in {
        spec["bedrock_model_id"] for spec in config.QUALITY_MODELS.values()
    }
    # Audit_Judge built and bound to its separate slot.
    assert backend.audit_judge is not None
    assert backend.audit_judge.audit_model == "meta.llama-audit-sentinel"


def test_missing_author_id_when_feature_on_raises(monkeypatch):
    """Req 2.1 / 2.2: with the cross-family feature on but no Author id configured, the builder
    refuses (no silent Claude fallback)."""
    monkeypatch.setattr(config, "QUALITY_OPT_CROSS_FAMILY_AUTHOR_ENABLED", True)
    monkeypatch.setattr(config, "QUALITY_OPT_AUTHOR_MODEL_ID", None)
    with pytest.raises(AuthorJudgeConflictError):
        build_live_backend(
            retrieval_backend="fake",
            judge_client_factory=_lazy_factory(),
            author_client_factory=_lazy_factory(),
            embedding_client_factory=_lazy_factory(),
        )


def test_author_contract_is_provider_neutral():
    """Req 2.8 / 2.9: the author contract states the task without asserting the Author is a
    Claude model, and frames the embedded guidance as guidance about the Target_Model family."""
    contract = build_author_prompt(
        target_model="haiku-4.5", champion_instruction="Be helpful.", failures=[]
    )
    lowered = contract.lower()
    # No assertion that the Author itself is Claude.
    assert "you are a claude" not in lowered
    assert "the author is itself a claude" not in lowered
    assert "as a claude model" not in lowered
    # Guidance is framed as about the target model's family, not a description of the Author.
    assert "target model's family" in lowered
    assert "haiku-4.5" in contract
    assert "not as a description of you, the author" in lowered


def test_gates_off_non_regression():
    """Req 4.1 / 4.2: with every gate at its default (off), the live backend resolves the
    default Sonnet author with the identity-only guard and builds NO Audit_Judge; the island
    step dispatches to the legacy single-iteration path."""
    # Defaults: cross-family + audit both off (asserted, so the test is self-guarding).
    assert config.QUALITY_OPT_CROSS_FAMILY_AUTHOR_ENABLED is False
    assert config.QUALITY_OPT_AUDIT_ENABLED is False
    assert config.QUALITY_OPT_ROUND_CADENCE_ENABLED is False

    backend = build_live_backend(
        retrieval_backend="fake",
        judge_client_factory=_lazy_factory(),
        author_client_factory=_lazy_factory(),
        embedding_client_factory=_lazy_factory(),
    )
    assert backend.audit_judge is None
    assert backend.author.author_model != config.JUDGE_MODEL_ID

    # Identity-only guard still rejects Author == Judge when the cross-family feature is off.
    with pytest.raises(AuthorJudgeConflictError):
        build_live_backend(
            config.JUDGE_MODEL_ID,
            retrieval_backend="fake",
            judge_client_factory=_lazy_factory(),
            author_client_factory=_lazy_factory(),
            embedding_client_factory=_lazy_factory(),
        )

    # With round cadence off, step() runs the legacy single-iteration path and completes.
    from bakeoff.quality.optimizer.backends import build_offline_backend

    island, broker = _make_island(build_offline_backend())
    state = asyncio.run(island.step())
    assert state.total_iterations == 1
    assert _accepted_from(broker) is not None  # a normal iteration_completed was emitted


def test_in_loop_path_holds_retrieval_constant():
    """Req 4.3: scoring two different instructions via ``score_in_loop`` over the same items
    yields byte-identical fragments per (item, turn) — the in-loop path uses the same
    held-constant, memoized retrieval substrate as the judge path."""
    from bakeoff.quality.optimizer.backends import build_offline_backend

    backend = build_offline_backend()
    scorer = JudgeInLoopScorer(backend, reps=1)
    items = _items()

    a = asyncio.run(
        scorer.score_in_loop(model="haiku-4.5", instruction="A", items=items, prompt_role="champion")
    )
    b = asyncio.run(
        scorer.score_in_loop(model="haiku-4.5", instruction="B", items=items, prompt_role="challenger")
    )

    frags_a = {(v.item_id, v.turn): v.grounding_fragment_ids for v in a.verdicts}
    frags_b = {(v.item_id, v.turn): v.grounding_fragment_ids for v in b.verdicts}
    assert frags_a and frags_a == frags_b  # identical fragments per (item, turn) across prompts


def test_score_in_loop_makes_no_judge_calls():
    """Req 1.2 (example): ``score_in_loop`` derives its signal without invoking the Judge."""
    from bakeoff.quality.optimizer.backends import build_offline_backend

    offline = build_offline_backend()
    counting = _CountingJudge(offline.judge_scorer)
    backend = dataclasses.replace(offline, judge_scorer=counting)
    scorer = JudgeInLoopScorer(backend, reps=1)

    asyncio.run(
        scorer.score_in_loop(
            model="haiku-4.5", instruction="x", items=_items(), prompt_role="champion"
        )
    )
    assert counting.calls == 0
