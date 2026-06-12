"""
Unit tests for the model adapters (Task 5, Req 3.1-3.5, Req 15.3).

Covers the things the task calls out plus the cross-cutting resilience and the
extended-thinking-on/off behavior the Bedrock adapter must implement:

* **Mock determinism (Req 3.5)** — same ``(seed, profile, inputs)`` ⇒ byte-identical
  answer, TTFT, latency, and token usage, across fresh adapter instances.
* **TTFT / latency captured (Req 3.2)** — every response carries a positive TTFT
  ≤ total generation latency; for the mock the captured TTFT matches the profile.
* **Multi-turn prompt includes prior turns (Req 3.3)** — the assembled prompt for
  the final turn contains every earlier user utterance and the model's own prior
  answers, in order.
* **Adapters honor the answerability behaviors the scorer tests need (Req 3.5)** —
  fabricate-on-unanswerable produces a confident non-refusal on an ``answerability
  == "none"`` item; refuse-on-answerable produces a refusal on a ``"full"`` item.
* **BedrockModelAdapter (Req 3.1/3.2/3.3, 15.3)** — drives a *fake* streaming
  Converse client (no network): true TTFT is measured at the first streamed delta;
  multi-turn assembles prior turns; an expired-credentials stream triggers the
  injected refresh callback (client rebuild) and retries to success.
* **Extended thinking on/off as DISTINCT candidates** — a thinking-on candidate
  sends ``additionalModelRequestFields`` with thinking enabled AND forces
  temperature 1.0 even if asked for 0.2; a thinking-off candidate sends no thinking
  field and honors the requested temperature; ``reasoningContent`` deltas are
  excluded from ``.text`` but a reasoning-TTFT is captured; and
  ``build_candidate_adapters`` yields the two locked inline candidates with the
  XML_short universal prompt override.

No real Bedrock/boto3 calls and no servers are started.

Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 15.3
"""
from __future__ import annotations

import asyncio

import pytest

from bakeoff.adapters.base import ModelAdapter, build_prompt
from bakeoff.adapters.bedrock import BedrockModelAdapter
from bakeoff.adapters.mock import GAP_FLAG_MARKER, MockAdapter, MockProfile
from bakeoff.types import CohortKey, Item, ModelResponse, Turn


# ---------------------------------------------------------------------------
# fixtures: minimal items + constant fragments
# ---------------------------------------------------------------------------
def _cohort(answerability: str = "full", turn_type: str = "single") -> CohortKey:
    return CohortKey(
        geography="Nigeria (Lagos)",
        proficiency="fluent",
        tone="terse",
        entry_route="slack",
        momentary_state="neutral",
        answerability=answerability,
        turn_type=turn_type,
    )


def _single_item(answerability: str = "full", item_id: str = "b0-q01") -> Item:
    return Item(
        id=item_id,
        turn_type="single",
        cohort=_cohort(answerability, "single"),
        query="how do I get a corporate card?",
        answerability=answerability,
        gold_node_ids=["node-aaa"],
    )


def _multi_item(item_id: str = "c0-s01") -> Item:
    turns = (
        Turn(turn=1, user_utterance="i lost my receipt", momentary_state="anxious",
             answerability="full"),
        Turn(turn=2, user_utterance="where is the form", momentary_state="frustrated",
             answerability="full", response_dependent=True, depends_on_turn=1),
        Turn(turn=3, user_utterance="and the deadline?", momentary_state="rushed",
             answerability="full"),
    )
    return Item(
        id=item_id,
        turn_type="multi",
        cohort=_cohort("full", "multi"),
        query="i lost my receipt",
        answerability="full",
        turns=turns,
    )


FRAGMENTS = [
    {"id": "frag-1", "text": "Submit the corporate card request in the portal.", "metadata": {}},
    {"id": "frag-2", "text": "Approval typically completes within two business days.", "metadata": {}},
    {"id": "frag-3", "text": "Lost receipts can be self-attested under $75.", "metadata": {}},
]


# ===========================================================================
# Prompt assembly (shared) — Req 3.3
# ===========================================================================
def test_single_turn_prompt_has_one_user_message_with_query() -> None:
    item = _single_item()
    prompt = build_prompt(item, FRAGMENTS)
    assert len(prompt.user_messages) == 1
    assert prompt.user_messages[0].content == item.query
    # constant fragments are in the system context block
    assert "frag-1" in prompt.system
    assert "Reference fragments:" in prompt.system


def test_multi_turn_prompt_includes_all_prior_turns_in_order() -> None:
    # Req 3.3: the prompt for the final turn carries every earlier user utterance
    # AND the model's own prior answers, in order.
    item = _multi_item()
    prior = ["You can self-attest under $75.", "The form is in the expense portal."]
    prompt = build_prompt(item, FRAGMENTS, prior_answers=prior, upto_turn_index=2)

    roles = [m.role for m in prompt.messages]
    contents = [m.content for m in prompt.messages]
    # user, assistant, user, assistant, user (turns 1,2 with answers, then turn 3)
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    assert contents == [
        "i lost my receipt",
        "You can self-attest under $75.",
        "where is the form",
        "The form is in the expense portal.",
        "and the deadline?",
    ]


def test_multi_turn_prompt_uses_placeholder_when_prior_answer_absent() -> None:
    item = _multi_item()
    prompt = build_prompt(item, FRAGMENTS, prior_answers=[], upto_turn_index=1)
    # turn 1's assistant answer is unknown -> placeholder, but the prior user
    # utterance is still present (prior turns are always included).
    assert prompt.messages[0].content == "i lost my receipt"
    assert prompt.messages[1].role == "assistant"
    assert "omitted" in prompt.messages[1].content.lower()
    assert prompt.messages[2].content == "where is the form"


def test_no_fragments_renders_explicit_no_context() -> None:
    prompt = build_prompt(_single_item(), [])
    assert "no reference fragments" in prompt.system.lower()


# ===========================================================================
# MockAdapter — determinism, TTFT/latency, behaviors
# ===========================================================================
def test_mock_is_a_model_adapter() -> None:
    assert isinstance(MockAdapter(), ModelAdapter)


def test_mock_determinism_same_seed_same_output() -> None:
    # Req 3.5: identical (seed, profile, inputs) -> identical response, even across
    # fresh adapter instances.
    item = _single_item()
    a = MockAdapter("m", seed=7, profile=MockProfile.grounded())
    b = MockAdapter("m", seed=7, profile=MockProfile.grounded())

    r1 = asyncio.run(a.generate(item, FRAGMENTS, 0.2))
    r2 = asyncio.run(b.generate(item, FRAGMENTS, 0.2))

    assert r1.text == r2.text
    assert r1.ttft_ms == r2.ttft_ms
    assert r1.generation_total_ms == r2.generation_total_ms
    assert r1.token_usage == r2.token_usage
    assert r1.per_turn_answers == r2.per_turn_answers


def test_mock_different_seed_changes_latency_at_nonzero_temperature() -> None:
    item = _single_item()
    a = MockAdapter("m", seed=1)
    b = MockAdapter("m", seed=2)
    r1 = asyncio.run(a.generate(item, FRAGMENTS, 0.7))
    r2 = asyncio.run(b.generate(item, FRAGMENTS, 0.7))
    # text is governed by profile+fragments (same), latency by the seeded RNG.
    assert r1.text == r2.text
    assert r1.generation_total_ms != r2.generation_total_ms


def test_mock_temperature_zero_is_fully_repeatable_across_seeds() -> None:
    # At temperature 0 there is no jitter, so latency is identical regardless of seed.
    item = _single_item()
    r1 = asyncio.run(MockAdapter("m", seed=1).generate(item, FRAGMENTS, 0.0))
    r2 = asyncio.run(MockAdapter("m", seed=999).generate(item, FRAGMENTS, 0.0))
    assert r1.generation_total_ms == r2.generation_total_ms
    assert r1.ttft_ms == r2.ttft_ms


def test_mock_captures_ttft_and_latency() -> None:
    # Req 3.2: TTFT and total latency captured; 0 < ttft <= total.
    item = _single_item()
    profile = MockProfile(base_latency_ms=500.0, ttft_fraction=0.4)
    resp = asyncio.run(MockAdapter("m", seed=0, profile=profile).generate(item, FRAGMENTS, 0.0))
    assert resp.ttft_ms > 0
    assert resp.ttft_ms <= resp.generation_total_ms
    # with no jitter (temp 0), the captured times equal the profile targets.
    assert resp.generation_total_ms == pytest.approx(500.0)
    assert resp.ttft_ms == pytest.approx(200.0)


def test_mock_token_usage_populated() -> None:
    resp = asyncio.run(MockAdapter().generate(_single_item(), FRAGMENTS, 0.2))
    assert resp.token_usage["prompt"] > 0
    assert resp.token_usage["completion"] > 0
    assert resp.token_usage["total"] == resp.token_usage["prompt"] + resp.token_usage["completion"]


def test_mock_multi_turn_produces_per_turn_answers_and_sums_latency() -> None:
    item = _multi_item()
    resp = asyncio.run(MockAdapter("m", seed=3).generate(item, FRAGMENTS, 0.0))
    assert len(resp.per_turn_answers) == len(item.turns)
    assert resp.text == resp.per_turn_answers[-1]
    # total generation latency is the sum across the 3 turns (each ~400ms default)
    assert resp.generation_total_ms == pytest.approx(400.0 * 3)
    # TTFT is the first turn's first token (a fraction of one turn), not the sum.
    assert resp.ttft_ms < resp.generation_total_ms


def test_mock_multi_turn_prompt_records_prior_turns(monkeypatch) -> None:
    # Req 3.3: the prompt actually fed for the last turn includes earlier turns.
    item = _multi_item()
    resp = asyncio.run(MockAdapter("m", seed=0).generate(item, FRAGMENTS, 0.0))
    last_prompt = resp.raw["prompts"][-1]
    # earlier user utterances appear in the final-turn prompt
    assert "i lost my receipt" in last_prompt
    assert "where is the form" in last_prompt
    assert "and the deadline?" in last_prompt
    # and the model's own prior answer to turn 1 is present as assistant context
    assert resp.per_turn_answers[0] in last_prompt


def test_mock_fabricate_on_unanswerable_does_not_refuse() -> None:
    # Req 3.5: behavior the answerability scorer test keys on (abstention should be 0).
    item = _single_item(answerability="none")
    resp = asyncio.run(
        MockAdapter("m", seed=0, profile=MockProfile.fabricator()).generate(item, FRAGMENTS, 0.0)
    )
    text = resp.text.lower()
    assert "i don't have that information" not in text
    assert "please contact" not in text
    # a default (non-fabricating) adapter DOES refuse the same item
    resp2 = asyncio.run(MockAdapter("m", seed=0).generate(item, FRAGMENTS, 0.0))
    assert "please contact" in resp2.text.lower()


def test_mock_refuse_on_answerable_refuses_full_item() -> None:
    # Req 3.5: behavior the answerability scorer test keys on (unwarranted_refusal == 1).
    item = _single_item(answerability="full")
    resp = asyncio.run(
        MockAdapter("m", seed=0, profile=MockProfile.over_refuser()).generate(item, FRAGMENTS, 0.0)
    )
    assert "i don't have that information" in resp.text.lower()


def test_mock_partial_answers_and_flags_the_gap() -> None:
    item = _single_item(answerability="partial")
    resp = asyncio.run(MockAdapter("m", seed=0).generate(item, FRAGMENTS, 0.0))
    assert GAP_FLAG_MARKER in resp.text


def test_mock_low_quality_ignores_fragments() -> None:
    item = _single_item()
    high = asyncio.run(MockAdapter("m", seed=0, profile=MockProfile(quality="high")).generate(item, FRAGMENTS, 0.0))
    low = asyncio.run(MockAdapter("m", seed=0, profile=MockProfile(quality="low")).generate(item, FRAGMENTS, 0.0))
    assert "reference material" in high.text.lower()
    # the low-quality answer does not quote a fragment's text
    assert FRAGMENTS[0]["text"] not in low.text


# ===========================================================================
# BedrockModelAdapter — streaming, TTFT, multi-turn, credential resilience
# ===========================================================================
class FakeClientError(Exception):
    """botocore-ClientError-shaped exception for the resilience path."""

    def __init__(self, code: str):
        self.response = {"Error": {"Code": code, "Message": code}}
        super().__init__(code)


def _converse_events(text_chunks, *, input_tokens=11, output_tokens=7):  # pragma: no cover - reference shape
    """Build a Converse-stream event list mimicking boto3's shape (reference)."""
    events = []
    for chunk in text_chunks:
        events.append({"contentBlockDelta": {"delta": {"text": chunk}}})
    events.append({"messageStop": {"stopReason": "end_turn"}})
    events.append(
        {"metadata": {"usage": {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": input_tokens + output_tokens,
        }}}
    )
    return events


class FakeBedrockClient:
    """A fake bedrock-runtime client whose converse_stream returns canned events.

    A virtual clock is advanced as the stream is consumed so TTFT/latency are
    deterministic. Optionally fails the first ``fail_first`` calls with a given
    error (to drive the resilience/refresh path).
    """

    def __init__(self, clock_state, *, chunks=("Hello", " there", "."),
                 ttft_advance=30.0, per_chunk_advance=10.0,
                 fail_first=0, fail_error=None, instance_id=0):
        self._clock_state = clock_state
        self._chunks = chunks
        self._ttft_advance = ttft_advance
        self._per_chunk_advance = per_chunk_advance
        self._fail_first = fail_first
        self._fail_error = fail_error
        self.calls = 0
        self.instance_id = instance_id

    def converse_stream(self, **kwargs):
        self.calls += 1
        if self.calls <= self._fail_first:
            raise self._fail_error
        clock_state = self._clock_state
        chunks = self._chunks
        ttft_advance = self._ttft_advance
        per_chunk_advance = self._per_chunk_advance

        def gen():
            for i, chunk in enumerate(chunks):
                # virtual clock is in SECONDS (like time.perf_counter); the
                # adapter converts to ms. advances are given in ms for readability.
                clock_state[0] += (ttft_advance if i == 0 else per_chunk_advance) / 1000.0
                yield {"contentBlockDelta": {"delta": {"text": chunk}}}
            yield {"messageStop": {"stopReason": "end_turn"}}
            yield {"metadata": {"usage": {
                "inputTokens": 11, "outputTokens": len(chunks),
                "totalTokens": 11 + len(chunks),
            }}}

        return {"stream": gen()}


def _virtual_clock(clock_state):
    return lambda: clock_state[0]


async def _instant_sleep(_d: float) -> None:
    return None


def test_bedrock_adapter_is_a_model_adapter() -> None:
    clock_state = [0.0]
    adapter = BedrockModelAdapter(
        "claude-3.5-haiku", "us.anthropic.claude-3-5-haiku-20241022-v1:0",
        client=FakeBedrockClient(clock_state), clock=_virtual_clock(clock_state),
    )
    assert isinstance(adapter, ModelAdapter)


def test_bedrock_single_turn_streams_and_captures_true_ttft() -> None:
    # Req 3.2: TTFT measured at the first streamed delta.
    clock_state = [0.0]
    client = FakeBedrockClient(clock_state, chunks=("Sub", "mit", " the", " form"),
                               ttft_advance=25.0, per_chunk_advance=5.0)
    adapter = BedrockModelAdapter(
        "claude-3.5-haiku", "us.anthropic.claude-3-5-haiku-20241022-v1:0",
        client=client, clock=_virtual_clock(clock_state),
    )
    resp = asyncio.run(adapter.generate(_single_item(), FRAGMENTS, 0.2))

    assert resp.text == "Submit the form"
    # first token after 25ms; 3 more chunks at 5ms each -> total 40ms
    assert resp.ttft_ms == pytest.approx(25.0)
    assert resp.generation_total_ms == pytest.approx(40.0)
    assert resp.ttft_ms < resp.generation_total_ms
    assert resp.finish_reason == "end_turn"
    assert resp.token_usage == {"prompt": 11, "completion": 4, "total": 15}
    assert resp.model == "claude-3.5-haiku"
    assert client.calls == 1


def test_bedrock_passes_temperature_and_model_id_to_converse() -> None:
    clock_state = [0.0]
    captured = {}

    class CapturingClient(FakeBedrockClient):
        def converse_stream(self, **kwargs):
            captured.update(kwargs)
            return super().converse_stream(**kwargs)

    adapter = BedrockModelAdapter(
        "claude-haiku-4.5", "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        client=CapturingClient(clock_state), clock=_virtual_clock(clock_state),
        accepts_temperature=True,  # exercise the temperature pass-through path
    )
    asyncio.run(adapter.generate(_single_item(), FRAGMENTS, 0.35))

    assert captured["modelId"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert captured["inferenceConfig"]["temperature"] == 0.35
    # a thinking-off candidate sends NO thinking field
    assert "additionalModelRequestFields" not in captured
    # the constant fragments rode in the system block
    assert "frag-1" in captured["system"][0]["text"]
    # single-turn -> exactly one user message
    assert len(captured["messages"]) == 1
    assert captured["messages"][0]["role"] == "user"


def test_bedrock_multi_turn_includes_prior_turns_in_final_call() -> None:
    # Req 3.3: the final turn's Converse request carries earlier user turns +
    # the model's own prior answers.
    clock_state = [0.0]
    calls_messages = []

    class RecordingClient(FakeBedrockClient):
        def converse_stream(self, **kwargs):
            calls_messages.append(kwargs["messages"])
            return super().converse_stream(**kwargs)

    adapter = BedrockModelAdapter(
        "claude-haiku-4.5", "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        client=RecordingClient(clock_state, chunks=("ans",)),
        clock=_virtual_clock(clock_state),
    )
    resp = asyncio.run(adapter.generate(_multi_item(), FRAGMENTS, 0.2))

    assert len(calls_messages) == 3                 # one call per turn
    assert len(resp.per_turn_answers) == 3
    final_messages = calls_messages[-1]
    # final call: user, assistant, user, assistant, user
    roles = [m["role"] for m in final_messages]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    user_texts = [m["content"][0]["text"] for m in final_messages if m["role"] == "user"]
    assert user_texts == ["i lost my receipt", "where is the form", "and the deadline?"]
    # generation latency summed across turns; TTFT is the first turn's first token
    assert resp.ttft_ms < resp.generation_total_ms


def test_bedrock_credential_expiry_refreshes_client_and_retries() -> None:
    # The headline cross-cutting scenario: an expired-credentials stream triggers
    # the injected refresh (a fresh client from the credential chain) and retries.
    clock_state = [0.0]
    built = {"count": 0}

    def client_factory():
        # First client always 401s (expired); the rebuilt client succeeds.
        first = built["count"] == 0
        built["count"] += 1
        if first:
            return FakeBedrockClient(
                clock_state, fail_first=10**9,
                fail_error=FakeClientError("ExpiredTokenException"),
                instance_id=built["count"],
            )
        return FakeBedrockClient(clock_state, chunks=("ok",), instance_id=built["count"])

    adapter = BedrockModelAdapter(
        "claude-3.5-haiku", "us.anthropic.claude-3-5-haiku-20241022-v1:0",
        client_factory=client_factory, clock=_virtual_clock(clock_state),
        sleep=_instant_sleep,
    )
    resp = asyncio.run(adapter.generate(_single_item(), FRAGMENTS, 0.2))

    assert resp.text == "ok"
    # initial client built (1) + rebuilt on refresh (1) = 2 client builds
    assert built["count"] == 2


def test_bedrock_permanent_error_propagates() -> None:
    clock_state = [0.0]
    client = FakeBedrockClient(
        clock_state, fail_first=10**9, fail_error=FakeClientError("ValidationException"),
    )
    adapter = BedrockModelAdapter(
        "claude-3.5-haiku", "us.anthropic.claude-3-5-haiku-20241022-v1:0",
        client=client, clock=_virtual_clock(clock_state),
    )
    # ValidationException is not auth/throttle/transient -> permanent -> propagate
    with pytest.raises(FakeClientError):
        asyncio.run(adapter.generate(_single_item(), FRAGMENTS, 0.2))


def test_build_candidate_adapters_covers_registry() -> None:
    import bakeoff.config as config
    from bakeoff.adapters.bedrock import build_candidate_adapters

    adapters = build_candidate_adapters()
    enabled = [c for c in config.CANDIDATE_MODELS if c.enabled]
    assert len(adapters) == len(enabled)
    assert {a.name for a in adapters} == {c.name for c in enabled}
    assert {c.method for c in enabled} == {"inline_agent"}
    # adding a candidate touches only CANDIDATE_MODELS: ids flow straight through
    assert {a.bedrock_model_id for a in adapters} == {c.bedrock_model_id for c in enabled}


# ===========================================================================
# BedrockModelAdapter — extended-thinking on/off as DISTINCT candidates
# ===========================================================================
class ThinkingFakeClient(FakeBedrockClient):
    """Fake client that captures kwargs and streams reasoning BEFORE answer text.

    ``reasoning_chunks`` are emitted first (as ``contentBlockDelta`` events whose
    delta carries a ``reasoningContent.text`` union member — the Bedrock Converse
    extended-thinking stream shape), then the visible answer ``chunks``, then the
    stop + usage events. The virtual clock advances on every delta so a true
    reasoning-vs-answer TTFT split is observable.
    """

    def __init__(self, clock_state, *, reasoning_chunks=("think", "ing"),
                 chunks=("Ans", "wer"), reasoning_advance=20.0, ttft_advance=15.0,
                 per_chunk_advance=5.0, captured=None, **kw):
        super().__init__(clock_state, chunks=chunks, ttft_advance=ttft_advance,
                         per_chunk_advance=per_chunk_advance, **kw)
        self._reasoning_chunks = reasoning_chunks
        self._reasoning_advance = reasoning_advance
        # shared dict the test inspects for the last converse_stream kwargs
        self.captured = captured if captured is not None else {}

    def converse_stream(self, **kwargs):
        self.calls += 1
        self.captured.clear()
        self.captured.update(kwargs)
        clock_state = self._clock_state
        reasoning_chunks = self._reasoning_chunks
        chunks = self._chunks
        reasoning_advance = self._reasoning_advance
        ttft_advance = self._ttft_advance
        per_chunk_advance = self._per_chunk_advance

        def gen():
            for r in reasoning_chunks:
                clock_state[0] += reasoning_advance / 1000.0
                yield {"contentBlockDelta": {"delta": {"reasoningContent": {"text": r}}}}
            for i, chunk in enumerate(chunks):
                clock_state[0] += (ttft_advance if i == 0 else per_chunk_advance) / 1000.0
                yield {"contentBlockDelta": {"delta": {"text": chunk}}}
            yield {"messageStop": {"stopReason": "end_turn"}}
            yield {"metadata": {"usage": {
                "inputTokens": 11, "outputTokens": len(chunks),
                "totalTokens": 11 + len(chunks),
            }}}

        return {"stream": gen()}


def test_bedrock_thinking_on_sends_thinking_field_and_forces_temperature_one() -> None:
    # (a) A thinking-on candidate sends additionalModelRequestFields with thinking
    # enabled AND temperature == 1.0 even when the runner asks for 0.2.
    clock_state = [0.0]
    captured: dict = {}
    client = ThinkingFakeClient(clock_state, captured=captured)
    adapter = BedrockModelAdapter(
        "claude-sonnet-4.6-thinking-on", "us.anthropic.claude-sonnet-4-6",
        client=client, clock=_virtual_clock(clock_state),
        thinking=True, accepts_temperature=True,  # accepts temp => thinking forces 1.0
    )
    resp = asyncio.run(adapter.generate(_single_item(), FRAGMENTS, 0.2))

    amrf = captured["additionalModelRequestFields"]
    assert amrf == {"thinking": {"type": "enabled", "budget_tokens": 2048}}
    # temperature forced to 1.0 despite the 0.2 request (model accepts temperature)
    assert captured["inferenceConfig"]["temperature"] == 1.0
    # max_tokens derived above the budget with generous answer headroom (2048 + 6000)
    assert captured["inferenceConfig"]["maxTokens"] == 8048
    # the enforcement is auditable in raw
    assert resp.raw["thinking"] is True
    assert resp.raw["requested_temperature"] == 0.2
    assert resp.raw["effective_temperature"] == 1.0
    assert resp.raw["budget_tokens"] == 2048


def test_bedrock_thinking_off_sends_no_thinking_field_and_honors_temperature() -> None:
    # (b) A thinking-off candidate sends NO thinking field and honors the passed
    # temperature, with the default (non-thinking) max_tokens.
    clock_state = [0.0]
    captured: dict = {}
    client = ThinkingFakeClient(clock_state, reasoning_chunks=(), captured=captured)
    adapter = BedrockModelAdapter(
        "claude-sonnet-4.6-thinking-off", "us.anthropic.claude-sonnet-4-6",
        client=client, clock=_virtual_clock(clock_state),
        thinking=False, accepts_temperature=True,  # exercise the honors-temperature path
    )
    resp = asyncio.run(adapter.generate(_single_item(), FRAGMENTS, 0.2))

    assert "additionalModelRequestFields" not in captured
    assert captured["inferenceConfig"]["temperature"] == 0.2
    assert captured["inferenceConfig"]["maxTokens"] == 6000
    assert resp.raw["thinking"] is False
    assert resp.raw["effective_temperature"] == 0.2
    # no thinking-only keys leak onto a thinking-off response
    assert "budget_tokens" not in resp.raw
    assert "reasoning_ttft_ms" not in resp.raw


def test_bedrock_thinking_excludes_reasoning_from_text_but_captures_reasoning_ttft() -> None:
    # (c) reasoningContent deltas are excluded from .text, but a reasoning-TTFT is
    # captured separately (and precedes the visible-answer TTFT).
    clock_state = [0.0]
    client = ThinkingFakeClient(
        clock_state,
        reasoning_chunks=("let me ", "think"),  # 2 * 20ms = 40ms before answer
        chunks=("Submit", " it"),               # first answer token at 40+15=55ms
        reasoning_advance=20.0, ttft_advance=15.0, per_chunk_advance=5.0,
    )
    adapter = BedrockModelAdapter(
        "claude-sonnet-4.5-thinking-on",
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        client=client, clock=_virtual_clock(clock_state), thinking=True,
    )
    resp = asyncio.run(adapter.generate(_single_item(), FRAGMENTS, 0.9))

    # visible answer only — reasoning text never bleeds into .text
    assert resp.text == "Submit it"
    assert "think" not in resp.text
    # visible-answer TTFT is time to first TEXT delta (after reasoning streamed)
    assert resp.ttft_ms == pytest.approx(55.0)
    # reasoning TTFT captured separately and is earlier than the answer TTFT
    assert resp.raw["reasoning_ttft_ms"] == pytest.approx(20.0)
    assert resp.raw["reasoning_ttft_ms"] < resp.ttft_ms
    # total wall-clock includes the thinking time (40ms reasoning + 15 + 5 answer)
    assert resp.generation_total_ms == pytest.approx(60.0)
    assert resp.raw["reasoning_chars"] == len("let me think")


def test_bedrock_invoke_stream_sync_thinking_request_shape_is_independent_of_build_prompt() -> None:
    # The thinking request-shaping + reasoning/TTFT logic lives entirely in the
    # stream consumer, so it is exercised DIRECTLY (no build_prompt / no item
    # involved at all) to prove the request shape is correct independent of prompt
    # assembly.
    clock_state = [0.0]
    captured: dict = {}
    client = ThinkingFakeClient(
        clock_state,
        reasoning_chunks=("plan ", "ahead"),    # 2 * 20ms = 40ms of reasoning
        chunks=("Do", " this"),                 # first answer token at 40+15=55ms
        reasoning_advance=20.0, ttft_advance=15.0, per_chunk_advance=5.0,
        captured=captured,
    )
    adapter = BedrockModelAdapter(
        "claude-sonnet-4.6-thinking-on", "us.anthropic.claude-sonnet-4-6",
        client=client, clock=_virtual_clock(clock_state), thinking=True,
        accepts_temperature=True,
    )

    system = [{"text": "sys"}]
    messages = [{"role": "user", "content": [{"text": "hi"}]}]
    result = adapter._invoke_stream_sync(system, messages, 0.2)

    # request shape: thinking field present + temperature forced to 1.0
    assert captured["additionalModelRequestFields"] == {
        "thinking": {"type": "enabled", "budget_tokens": 2048}
    }
    assert captured["inferenceConfig"]["temperature"] == 1.0
    assert captured["inferenceConfig"]["maxTokens"] == 8048
    # reasoning excluded from the answer text; reasoning-TTFT captured separately
    assert result.text == "Do this"
    assert result.ttft_ms == pytest.approx(55.0)
    assert result.reasoning_ttft_ms == pytest.approx(20.0)
    assert result.reasoning_ttft_ms < result.ttft_ms


def test_bedrock_invoke_stream_sync_thinking_off_omits_field_and_passes_temperature() -> None:
    # Direct (no build_prompt) proof of the thinking-OFF request shape.
    clock_state = [0.0]
    captured: dict = {}
    client = ThinkingFakeClient(clock_state, reasoning_chunks=(), chunks=("ok",),
                                captured=captured)
    adapter = BedrockModelAdapter(
        "claude-sonnet-4.6-thinking-off", "us.anthropic.claude-sonnet-4-6",
        client=client, clock=_virtual_clock(clock_state), thinking=False,
        accepts_temperature=True,
    )

    system = [{"text": "sys"}]
    messages = [{"role": "user", "content": [{"text": "hi"}]}]
    result = adapter._invoke_stream_sync(system, messages, 0.2)

    assert "additionalModelRequestFields" not in captured
    assert captured["inferenceConfig"]["temperature"] == 0.2
    assert captured["inferenceConfig"]["maxTokens"] == 6000
    assert result.text == "ok"
    assert result.reasoning_ttft_ms is None


def test_build_candidate_adapters_yields_two_inline_candidates() -> None:
    # (d) build_candidate_adapters yields the locked candidates with the right
    # names, base ids, methods, and instruction override — driven entirely off
    # CANDIDATE_MODELS (no count or name hard-coded in the adapter layer).
    import bakeoff.config as config
    from bakeoff.adapters.bedrock import build_candidate_adapters, load_bakeoff_instruction_override
    from bakeoff.adapters.inline_agent import InlineAgentAdapter

    adapters = build_candidate_adapters()
    by_name = {a.name: a for a in adapters}

    expected_instruction = load_bakeoff_instruction_override()
    expected = {
        "claude-sonnet-4.6-thinking-off-inline":
            ("us.anthropic.claude-sonnet-4-6", False, InlineAgentAdapter),
        "claude-haiku-4.5-inline":
            ("us.anthropic.claude-haiku-4-5-20251001-v1:0", False, InlineAgentAdapter),
    }
    # the adapter set matches the registry exactly (one-line edits stay one-line)
    assert by_name.keys() == expected.keys()
    for name, (model_id, thinking, adapter_type) in expected.items():
        adapter = by_name[name]
        assert adapter.bedrock_model_id == model_id
        assert adapter.thinking is thinking
        assert getattr(adapter, "instruction_override", None) == expected_instruction
        assert isinstance(adapter, adapter_type)

    # every candidate is an inline adapter (no Converse candidates remain)
    assert all(isinstance(a, InlineAgentAdapter) for a in adapters)

    # the judge model is none of the candidates (self-preference assertion holds)
    assert config.JUDGE_MODEL_ID not in {a.bedrock_model_id for a in adapters}


def test_registered_candidates_use_universal_xml_short_instruction() -> None:
    import bakeoff.config as config
    from bakeoff.adapters.bedrock import build_candidate_adapters, load_bakeoff_instruction_override

    expected_instruction = load_bakeoff_instruction_override()
    adapters = build_candidate_adapters(methods=["inline_agent"])
    expected_names = {candidate.name for candidate in config.CANDIDATE_MODELS if candidate.enabled}

    assert {adapter.name for adapter in adapters} == expected_names
    for adapter in adapters:
        assert getattr(adapter, "instruction_override", None) == expected_instruction


def test_bedrock_omits_temperature_for_deprecating_models() -> None:
    # Regression for the live ValidationException "`temperature` is deprecated for
    # this model.": a candidate with accepts_temperature=False (the 4.x default)
    # must NOT send a temperature field at all — sending any value 400s.
    for thinking in (False, True):
        clock_state = [0.0]
        captured: dict = {}

        class CapturingClient(FakeBedrockClient):
            def converse_stream(self, **kwargs):
                captured.update(kwargs)
                return super().converse_stream(**kwargs)

        adapter = BedrockModelAdapter(
            "claude-sonnet-4.6-x", "us.anthropic.claude-sonnet-4-6",
            client=CapturingClient(clock_state), clock=_virtual_clock(clock_state),
            thinking=thinking, accepts_temperature=False,
        )
        resp = asyncio.run(adapter.generate(_single_item(), FRAGMENTS, 0.2))
        assert "temperature" not in captured["inferenceConfig"], (
            f"temperature must be omitted for a deprecating model (thinking={thinking})"
        )
        # maxTokens is still sent; only temperature is omitted
        assert "maxTokens" in captured["inferenceConfig"]
        # the omission is auditable
        assert resp.raw["effective_temperature"] is None
        assert resp.raw["accepts_temperature"] is False


def test_all_registered_candidates_send_a_valid_temperature_shape() -> None:
    # End-to-end over the REAL roster: every candidate either omits temperature
    # (deprecating models) or sends a numeric one (accepting models) — never sends
    # a value to a model that rejects it. Mirrors what hits Converse on a real run.
    import bakeoff.config as config

    for c in config.CANDIDATE_MODELS:
        if not c.enabled:
            continue
        clock_state = [0.0]
        captured: dict = {}

        class CapturingClient(FakeBedrockClient):
            def converse_stream(self, **kwargs):
                captured.update(kwargs)
                return super().converse_stream(**kwargs)

        adapter = BedrockModelAdapter(
            c.name, c.bedrock_model_id,
            client=CapturingClient(clock_state), clock=_virtual_clock(clock_state),
            family=c.family, thinking=c.thinking, budget_tokens=c.budget_tokens,
            max_tokens=c.max_tokens, temperature=c.temperature,
            accepts_temperature=c.accepts_temperature,
        )
        asyncio.run(adapter.generate(_single_item(), FRAGMENTS, 0.2))
        ic = captured["inferenceConfig"]
        if c.accepts_temperature:
            assert isinstance(ic.get("temperature"), (int, float)), (
                f"{c.name}: accepts temperature but none was sent"
            )
        else:
            assert "temperature" not in ic, (
                f"{c.name}: deprecating model must not send temperature"
            )


def test_thinking_on_candidate_max_tokens_exceeds_budget_in_registry() -> None:
    # Anthropic constraint: max_tokens INCLUDES the thinking budget, so every
    # thinking-on candidate's effective max_tokens must exceed its budget.
    import bakeoff.config as config

    for c in config.CANDIDATE_MODELS:
        if c.thinking:
            budget = c.effective_budget_tokens()
            assert budget is not None
            assert c.effective_max_tokens() > budget, (
                f"{c.name}: max_tokens must exceed the thinking budget"
            )


def test_candidate_model_temperature_override_only_applies_when_thinking_off() -> None:
    # The per-candidate temperature rule, now gated on whether the model ACCEPTS
    # temperature at all (the 4.x roster deprecated it):
    from bakeoff.config import CandidateModel

    # accepts_temperature=True + thinking off + a pinned override -> override wins.
    pinned = CandidateModel("pinned", "us.anthropic.claude-3-5-haiku-20241022-v1:0",
                            thinking=False, temperature=0.0, accepts_temperature=True)
    assert pinned.resolve_temperature(0.7) == 0.0

    # accepts_temperature=True + thinking on -> forced to 1.0 (override ignored).
    thinking = CandidateModel("th", "us.anthropic.claude-sonnet-4-6",
                              thinking=True, temperature=0.0, accepts_temperature=True)
    assert thinking.resolve_temperature(0.7) == 1.0

    # A model that DEPRECATED temperature (accepts_temperature=False, the default)
    # resolves to None — the field is omitted — regardless of override OR thinking,
    # because sending any value 400s ("temperature is deprecated for this model").
    deprecated_off = CandidateModel("dep-off", "us.anthropic.claude-sonnet-4-6",
                                    thinking=False, temperature=0.0)
    assert deprecated_off.resolve_temperature(0.7) is None
    deprecated_on = CandidateModel("dep-on", "us.anthropic.claude-sonnet-4-6",
                                   thinking=True)
    assert deprecated_on.resolve_temperature(0.7) is None


def test_adapters_return_modelresponse_type() -> None:
    clock_state = [0.0]
    bedrock = BedrockModelAdapter(
        "claude-3.5-haiku", "us.anthropic.claude-3-5-haiku-20241022-v1:0",
        client=FakeBedrockClient(clock_state), clock=_virtual_clock(clock_state),
    )
    assert isinstance(asyncio.run(MockAdapter().generate(_single_item(), FRAGMENTS, 0.2)), ModelResponse)
    assert isinstance(asyncio.run(bedrock.generate(_single_item(), FRAGMENTS, 0.2)), ModelResponse)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
