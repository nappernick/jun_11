"""
Tests for :mod:`bakeoff.adapters.inline_agent` — the inline-agent invocation method.

All OFFLINE: a fake ``bedrock-agent-runtime`` client yields a ``completion`` event
stream (the shape ``invoke_inline_agent`` returns), so the adapter's request
shaping, scaffolding-strip override, streaming TTFT, and credential resilience are
exercised with zero network.

The load-bearing assertions:
* The request OVERRIDES the orchestration prompt (promptCreationMode=OVERRIDDEN)
  with the minimal passthrough template and attaches NO actionGroups / NO
  knowledgeBases — so Bedrock injects no tool/agent scaffolding.
* ``instruction`` carries the universal Bake-Off XML_short prompt override plus
  the constant fragments, and meets Bedrock's minimum length.
* TTFT is the wall-clock to the first streamed chunk; total is the full stream.
* An expired-credentials failure triggers a client rebuild + retry to success.
* It satisfies the ModelAdapter protocol (so the runner is method-agnostic).
"""
from __future__ import annotations

import asyncio

from bakeoff.adapters.base import ModelAdapter
from bakeoff.adapters.bedrock import BedrockModelAdapter, load_bakeoff_instruction_override
from bakeoff.adapters.inline_agent import InlineAgentAdapter
from bakeoff.types import CohortKey, Item, ModelResponse


def _virtual_clock(state):
    def clock():
        return state[0]
    return clock


async def _instant_sleep(_s):
    return None


def _single_item(answerability="full", item_id="b0-q01"):
    return Item(
        id=item_id,
        turn_type="single",
        cohort=CohortKey(
            geography="g", proficiency="fluent", tone="terse", entry_route="slack",
            momentary_state="neutral", answerability=answerability, turn_type="single",
        ),
        query="how do I get a corporate card?",
        answerability=answerability,
        gold_node_ids=["node-aaa"],
    )


FRAGMENTS = [
    {"id": "frag-1", "text": "Submit the corporate card request in the portal.", "metadata": {}},
    {"id": "frag-2", "text": "Approval completes within two business days.", "metadata": {}},
]


class FakeAgentClient:
    """A fake bedrock-agent-runtime client whose invoke_inline_agent streams chunks.

    Advances the virtual clock as it yields, so TTFT/total are deterministic.
    Captures the request kwargs for assertions.
    """

    def __init__(self, clock_state, *, chunks=("Hello, ", "world."),
                 ttft_advance=20.0, per_chunk_advance=5.0, captured=None):
        self._clock = clock_state
        self._chunks = chunks
        self._ttft_advance = ttft_advance
        self._per_chunk = per_chunk_advance
        self.captured = captured if captured is not None else {}
        self.calls = 0

    def invoke_inline_agent(self, **kwargs):
        self.calls += 1
        self.captured.clear()
        self.captured.update(kwargs)

        chunks = self._chunks
        ttft_advance = self._ttft_advance
        per_chunk = self._per_chunk
        clock = self._clock

        def _gen():
            for i, c in enumerate(chunks):
                clock[0] += ttft_advance if i == 0 else per_chunk
                yield {"chunk": {"bytes": c.encode("utf-8")}}

        return {"completion": _gen()}


def test_build_candidate_adapters_methods_filter_enables_inline_only_roster():
    from bakeoff.adapters.bedrock import build_candidate_adapters

    expected_instruction = load_bakeoff_instruction_override()
    expected_names = {
        "claude-sonnet-4.6-thinking-off-inline",
        "claude-haiku-4.5-inline",
    }
    inline_only = build_candidate_adapters(methods=["inline_agent"])
    converse_only = build_candidate_adapters(methods=["converse"])
    both = build_candidate_adapters()

    assert all(isinstance(a, InlineAgentAdapter) for a in inline_only)
    assert converse_only == []
    assert len(inline_only) == 2
    assert len(both) == 2
    assert {a.name for a in inline_only} == expected_names
    assert {a.name for a in both} == expected_names
    assert all(a.name.endswith("-inline") for a in inline_only)
    assert all(a.instruction_override == expected_instruction for a in inline_only)


def test_inline_adapter_is_a_model_adapter():
    assert isinstance(
        InlineAgentAdapter("x", "us.anthropic.claude-haiku-4-5-20251001-v1:0", client=object()),
        ModelAdapter,
    )


def test_inline_request_overrides_orchestration_and_attaches_no_tools():
    clock_state = [0.0]
    captured: dict = {}
    client = FakeAgentClient(clock_state, captured=captured)
    expected_instruction = load_bakeoff_instruction_override()
    adapter = InlineAgentAdapter(
        "claude-haiku-4.5-inline", "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        family="haiku-4.5", client=client, clock=_virtual_clock(clock_state),
        instruction_override=expected_instruction,
    )
    resp = asyncio.run(adapter.generate(_single_item(), FRAGMENTS, 0.2))

    # No agent/tool scaffolding is ever attached.
    assert "actionGroups" not in captured
    assert "knowledgeBases" not in captured

    # The orchestration prompt is OVERRIDDEN with our minimal template.
    poc = captured["promptOverrideConfiguration"]["promptConfigurations"][0]
    assert poc["promptType"] == "ORCHESTRATION"
    assert poc["promptCreationMode"] == "OVERRIDDEN"
    assert "$instruction$" in poc["basePromptTemplate"]
    assert "$question$" in poc["basePromptTemplate"]
    # the template carries NO tool/function/action-group text
    low = poc["basePromptTemplate"].lower()
    for needle in ("tool", "function", "action group", "<tools>", "you have access"):
        assert needle not in low

    # instruction carries ONLY the family system prompt (small; under the 20K
    # inline quota) — the fragments must NOT be in instruction.
    assert len(captured["instruction"]) >= 40
    assert len(captured["instruction"]) < 20000
    assert "frag-1" not in captured["instruction"]
    # the constant retrieved-fragment context rides in promptSessionAttributes,
    # which the override template injects at $prompt_session_attributes$.
    ctx = captured["inlineSessionState"]["promptSessionAttributes"]["context"]
    assert "frag-1" in ctx
    assert "$prompt_session_attributes$" in (
        captured["promptOverrideConfiguration"]["promptConfigurations"][0]["basePromptTemplate"]
    )
    assert captured["inputText"] == "how do I get a corporate card?"
    assert captured["foundationModel"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert captured["instruction"].strip() == expected_instruction
    assert resp.raw["prompt_override"] is True


def test_inline_temperature_is_passed_in_inference_config():
    clock_state = [0.0]
    captured: dict = {}
    client = FakeAgentClient(clock_state, captured=captured)
    adapter = InlineAgentAdapter(
        "claude-haiku-4.5-inline", "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        family="haiku-4.5", accepts_temperature=True,
        client=client, clock=_virtual_clock(clock_state),
    )
    asyncio.run(adapter.generate(_single_item(), FRAGMENTS, 0.35))
    infcfg = captured["promptOverrideConfiguration"]["promptConfigurations"][0]["inferenceConfiguration"]
    assert infcfg["temperature"] == 0.35
    assert "maximumLength" in infcfg


def test_inline_streams_text_and_measures_ttft():
    clock_state = [0.0]
    client = FakeAgentClient(
        clock_state, chunks=("The capital ", "is Paris."),
        ttft_advance=0.030, per_chunk_advance=0.010,  # seconds (adapter x1000 -> ms)
    )
    adapter = InlineAgentAdapter(
        "claude-haiku-4.5-inline", "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        family="haiku-4.5", client=client, clock=_virtual_clock(clock_state),
    )
    resp = asyncio.run(adapter.generate(_single_item(), FRAGMENTS, 0.2))
    assert isinstance(resp, ModelResponse)
    assert resp.text == "The capital is Paris."
    assert resp.ttft_ms == 30.0          # first chunk at +30ms
    assert resp.generation_total_ms == 40.0  # 30 + 10
    assert resp.model == "claude-haiku-4.5-inline"
    assert resp.raw["method"] == "inline_agent"
    # inline never honors extended thinking (recorded honestly)
    assert resp.raw["thinking_honored"] is False


def test_inline_thinking_on_candidate_records_thinking_not_honored():
    clock_state = [0.0]
    client = FakeAgentClient(clock_state)
    adapter = InlineAgentAdapter(
        "claude-sonnet-4.6-thinking-on-inline", "us.anthropic.claude-sonnet-4-6",
        family="sonnet-4.6", thinking=True,
        client=client, clock=_virtual_clock(clock_state),
    )
    resp = asyncio.run(adapter.generate(_single_item(), FRAGMENTS, 0.2))
    assert resp.raw["thinking_requested"] is True
    assert resp.raw["thinking_honored"] is False


def test_inline_recovers_from_expired_credentials():
    clock_state = [0.0]

    class ExpiredOnceFactory:
        def __init__(self):
            self.builds = 0

        def __call__(self):
            self.builds += 1
            if self.builds == 1:
                return _ExpiredClient()
            return FakeAgentClient(clock_state)

    class _ExpiredClient:
        def invoke_inline_agent(self, **kwargs):
            from botocore.exceptions import ClientError
            raise ClientError(
                {"Error": {"Code": "ExpiredTokenException",
                           "Message": "The security token included in the request is expired"}},
                "InvokeInlineAgent",
            )

    factory = ExpiredOnceFactory()
    adapter = InlineAgentAdapter(
        "claude-haiku-4.5-inline", "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        family="haiku-4.5", client_factory=factory,
        clock=_virtual_clock(clock_state), sleep=_instant_sleep,
    )
    resp = asyncio.run(adapter.generate(_single_item(), FRAGMENTS, 0.2))
    assert resp.text  # succeeded after rebuild
    assert factory.builds >= 2  # initial expired client + rebuilt good one


if __name__ == "__main__":  # pragma: no cover
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
