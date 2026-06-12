"""
Inline no-noise fidelity unit test (Task 11.2 — MANDATORY, not optional) for
:class:`bakeoff.quality.optimizer.inline_session_adapter.PersistentSessionInlineAdapter`.

This is the *fidelity* test the design designates mandatory (design "No-noise unit test
(mandatory)"; Req 3.6, 13.4, 13.7, 13.9). It is NOT the property-test version — Property 23
(Task 11.3) lives in a separate file. Everything here is plain ``pytest`` with ZERO network:
a fake ``bedrock-agent-runtime`` client records every ``invoke_inline_agent(**kwargs)`` call
and returns a minimal valid ``completion`` event stream (the shape the adapter's
``_invoke_stream_sync`` consumes), so ``generate`` runs end-to-end against the fake. The
fake-client/streaming patterns mirror ``bakeoff/tests/test_inline_agent.py`` and async is
driven with ``asyncio.run`` (no pytest-asyncio dependency).

The adapter renders a Bedrock Agent Runtime ``InvokeInlineAgent`` request per turn; this
test renders the request(s) the adapter builds for a multi-turn item carrying retrieved
fragments and asserts the **"no-noise / only-our-prompt"** fidelity invariant:

* ``promptCreationMode == "OVERRIDDEN"`` and the base template is the minimal
  :data:`config.QUALITY_OPT_INLINE_TEMPLATE`, which frames the per-turn
  ``$prompt_session_attributes$`` placeholder in the system;
* ``actionGroups`` and ``knowledgeBases`` are absent/empty;
* the session-scoped ``sessionAttributes`` channel is never set; the turn's fragments ride
  the per-turn ``inlineSessionState.promptSessionAttributes`` channel instead;
* **one** ``invoke_inline_agent`` call per turn under a **single stable** ``sessionId`` that
  is identical across the turns of one conversation and matches ``session_id_for``;
* the rendered request carries our instruction + the **bare** turn question, with the turn's
  retrieved fragments injected through ``promptSessionAttributes`` (never concatenated into
  the question, so nothing fragment-sized is persisted into the conversation history);
* **grounding parity** — the fragment ids handed to the model (via the attribute channel)
  equal those recorded as the grounding ids for that turn (so the judge would receive the
  same set, Req 13.7);
* the **absence** of every orchestration marker: ``"actionGroup"``, ``"action group"``,
  ``"function_call"``, ``"<tools>"``, ``"Thought:"``, ``"Action:"``, ``"Observation:"``,
  ``"you have access to"``.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re

import bakeoff.config as config
from bakeoff.quality.optimizer.inline_session_adapter import (
    PersistentSessionInlineAdapter,
)
from bakeoff.types import CohortKey, Item, Turn


# ---------------------------------------------------------------------------
# Fixed, marker-free test content. Every string below is deliberately clear of
# any orchestration marker the no-noise assertion scans for, so a marker found in
# the rendered request can only have come from injected scaffolding (the failure
# mode this test exists to catch), never from the test data itself.
# ---------------------------------------------------------------------------
ADAPTER_NAME = "claude-haiku-4.5-opt"
MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
TEMPERATURE = 0.2

# The optimized system prompt under test — the ONLY system instruction the model
# receives. >40 chars so the adapter's min-length padding never alters it.
INSTRUCTION = (
    "Answer using only the reference fragments provided below. If they do not support "
    "a confident, grounded answer, decline rather than guess."
)

# A multi-turn conversation: each turn has a distinct, marker-free utterance.
UTTERANCES = (
    "How do I request a corporate card?",
    "What is the approval timeline once submitted?",
    "Can I expedite approval for urgent travel?",
)

# The turn's retrieved fragments (verbatim /retrieve shape: {id, text, metadata}).
FRAGMENTS = [
    {
        "id": "frag-card-101",
        "text": "Submit the corporate card request in the company portal under Finance.",
        "metadata": {},
    },
    {
        "id": "frag-card-102",
        "text": "Approval typically completes within two business days of submission.",
        "metadata": {},
    },
    {
        "id": "frag-card-103",
        "text": "Urgent travel requests can be escalated to your manager for same-day review.",
        "metadata": {},
    },
]

# The exact orchestration markers the design's mandatory no-noise test forbids in the
# rendered request (design "No-noise unit test (mandatory)"; Req 13.9).
ORCHESTRATION_MARKERS = (
    "actionGroup",
    "action group",
    "function_call",
    "<tools>",
    "Thought:",
    "Action:",
    "Observation:",
    "you have access to",
)


async def _instant_sleep(_s):  # resilience backoff hook — never exercised on the success path
    return None


def _multi_turn_item(item_id: str = "c0-s01") -> Item:
    """A small multi-turn :class:`Item` (mirrors the item-builder patterns in the suite)."""
    cohort = CohortKey(
        geography="g",
        proficiency="fluent",
        tone="terse",
        entry_route="slack",
        momentary_state="neutral",
        answerability="full",
        turn_type="multi",
    )
    turns = tuple(
        Turn(turn=i + 1, user_utterance=u, momentary_state="neutral")
        for i, u in enumerate(UTTERANCES)
    )
    return Item(
        id=item_id,
        turn_type="multi",
        cohort=cohort,
        query=UTTERANCES[0],
        answerability="full",
        turns=turns,
    )


class RecordingAgentClient:
    """A network-free ``bedrock-agent-runtime`` stand-in.

    Records a deep copy of every ``invoke_inline_agent(**kwargs)`` call (so per-turn
    requests can be asserted independently) and returns a minimal valid ``completion``
    event stream — exactly the ``{"completion": <iterable of {"chunk": {"bytes": ...}}>}``
    shape :meth:`PersistentSessionInlineAdapter._invoke_stream_sync` consumes, so
    ``generate`` completes for every turn. Mirrors ``FakeAgentClient`` in
    ``test_inline_agent.py`` but keeps a LIST of all calls rather than only the last.
    """

    def __init__(self, answer: str = "Per the reference material, submit the request in the portal."):
        self.calls: list[dict] = []
        self._answer = answer

    def invoke_inline_agent(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        answer = self._answer

        def _gen():
            yield {"chunk": {"bytes": answer.encode("utf-8")}}

        return {"completion": _gen()}


def _build_adapter(
    client: RecordingAgentClient,
    *,
    send_fragments: bool = True,
    history_mode: str = "server",
    session_id_for=None,
    name: str = ADAPTER_NAME,
    instruction: str = INSTRUCTION,
) -> PersistentSessionInlineAdapter:
    """Construct the adapter wired to the injected fake client (no real Bedrock)."""
    return PersistentSessionInlineAdapter(
        name,
        MODEL_ID,
        instruction_override=instruction,
        send_fragments=send_fragments,
        history_mode=history_mode,
        client_factory=lambda: client,
        session_id_for=session_id_for,
        sleep=_instant_sleep,
    )


def _drive_generate(**adapter_kwargs):
    """Run ``generate`` over the multi-turn item and return (client, adapter, item, resp).

    The captured ``client.calls`` are the per-turn ``invoke_inline_agent`` requests the
    assertions render against.
    """
    client = RecordingAgentClient()
    adapter = _build_adapter(client, **adapter_kwargs)
    item = _multi_turn_item()
    resp = asyncio.run(adapter.generate(item, FRAGMENTS, TEMPERATURE))
    return client, adapter, item, resp


def _rendered_fragment_ids(context_text: str) -> tuple[str, ...]:
    """Parse the fragment ids the model actually sees, in render order.

    ``assemble_context`` renders each fragment as ``[i] (id=<frag_id>)\\n<text>``; this
    pulls the ``<frag_id>`` tokens back out so they can be compared to the grounding ids.
    """
    return tuple(re.findall(r"\(id=([^)]+)\)", context_text))


def _rendered_context(call: dict) -> str:
    """The assembled-context string the model sees for one turn.

    Fragments ride the per-turn ``inlineSessionState.promptSessionAttributes`` channel as a
    single ``retrieved_context`` entry (rendered into ``$prompt_session_attributes$``), so
    this pulls that value back out. Returns ``""`` when no fragment channel is attached.
    """
    state = call.get("inlineSessionState") or {}
    attrs = state.get("promptSessionAttributes") or {}
    return attrs.get("retrieved_context", "")


# ===========================================================================
# One invoke per turn, single stable sessionId, matching session_id_for
# ===========================================================================
def test_one_invoke_inline_agent_call_per_turn():
    """Exactly one ``invoke_inline_agent`` call is issued per conversation turn."""
    client, _adapter, item, resp = _drive_generate()
    assert len(client.calls) == len(item.turns) == len(UTTERANCES)
    # The response reflects the same per-turn cardinality.
    assert len(resp.per_turn_answers) == len(item.turns)
    assert resp.raw["n_turns"] == len(item.turns)


def test_single_stable_session_id_across_turns_matches_default_scheme():
    """All turns of one conversation share ONE stable ``sessionId`` derived from the
    default ``session_id_for`` scheme (``opt-<name>-<item_id>-0``)."""
    client, adapter, item, resp = _drive_generate()

    session_ids = {c["sessionId"] for c in client.calls}
    assert len(session_ids) == 1, "every turn must reuse the one conversation sessionId"

    expected = f"opt-{ADAPTER_NAME}-{item.item_id}-0"
    assert session_ids == {expected}
    # The same id is recorded on the response and produced by the adapter's seam.
    assert resp.raw["sessionId"] == expected
    assert adapter._session_id_for(item, 0) == expected


def test_session_id_comes_from_injected_session_id_for():
    """The conversation's ``sessionId`` is exactly what the injected ``session_id_for``
    returns, called with the fixed generate-rep (``_GENERATE_REP == 0``), and is identical
    across turns — proving the stable-session id flows from the documented seam."""
    seen: list[tuple[str, int]] = []

    def sid(item, rep):
        seen.append((item.item_id, rep))
        return f"custom-session-{item.item_id}-{rep}"

    client = RecordingAgentClient()
    item = _multi_turn_item()
    adapter = _build_adapter(client, session_id_for=sid)
    asyncio.run(adapter.generate(item, FRAGMENTS, TEMPERATURE))

    expected = f"custom-session-{item.item_id}-0"
    assert [c["sessionId"] for c in client.calls] == [expected] * len(item.turns)
    assert len({c["sessionId"] for c in client.calls}) == 1
    assert (item.item_id, 0) in seen  # session_id_for was consulted with rep=0


# ===========================================================================
# OVERRIDDEN minimal template, no $prompt_session_attributes$
# ===========================================================================
def test_prompt_override_is_overridden_minimal_template():
    """Every turn overrides the ORCHESTRATION prompt with the minimal optimizer template,
    which frames the per-turn ``$prompt_session_attributes$`` placeholder in the system."""
    client, _adapter, _item, _resp = _drive_generate()
    assert client.calls

    for call in client.calls:
        configs = call["promptOverrideConfiguration"]["promptConfigurations"]
        assert len(configs) == 1
        poc = configs[0]
        assert poc["promptType"] == "ORCHESTRATION"
        assert poc["promptCreationMode"] == "OVERRIDDEN"
        assert poc["parserMode"] == "DEFAULT"
        assert poc["promptState"] == "ENABLED"

        template = poc["basePromptTemplate"]
        # It IS the minimal optimizer template, byte-for-byte.
        assert template == config.QUALITY_OPT_INLINE_TEMPLATE
        # The minimal template references our instruction, the bare question, and the
        # per-turn session-attributes placeholder fragments are rendered into.
        assert "$instruction$" in template
        assert "$question$" in template
        assert "$prompt_session_attributes$" in template


def test_inference_configuration_passes_trial_temperature():
    """The trial temperature rides in the override's inference configuration (sanity that
    the OVERRIDDEN config is the live one the model is steered by)."""
    client, _adapter, _item, _resp = _drive_generate()
    for call in client.calls:
        infcfg = call["promptOverrideConfiguration"]["promptConfigurations"][0][
            "inferenceConfiguration"
        ]
        assert infcfg["temperature"] == TEMPERATURE
        assert "maximumLength" in infcfg


# ===========================================================================
# No action groups / no knowledge bases
# ===========================================================================
def test_no_action_groups_or_knowledge_bases_attached():
    """Nothing for the model to call: ``actionGroups`` and ``knowledgeBases`` are absent
    (and, were they present at all, would have to be empty)."""
    client, _adapter, _item, _resp = _drive_generate()
    for call in client.calls:
        assert "actionGroups" not in call
        assert "knowledgeBases" not in call
        # Defensive: if a future change ever introduced an empty collection, it must
        # still be empty — never carry a tool/agent definition.
        assert not call.get("actionGroups")
        assert not call.get("knowledgeBases")


# ===========================================================================
# Fragments ride the per-turn promptSessionAttributes channel; sessionAttributes never set
# ===========================================================================
def test_fragments_ride_prompt_session_attributes_channel():
    """Fragments ride the per-turn ``inlineSessionState.promptSessionAttributes`` channel
    (a single ``retrieved_context`` entry), and the session-scoped ``sessionAttributes``
    channel is NEVER set — neither at the top level nor nested (Req 13.4)."""
    client, _adapter, _item, _resp = _drive_generate()
    for call in client.calls:
        # The per-turn channel carries exactly our one context attribute.
        state = call["inlineSessionState"]
        assert set(state["promptSessionAttributes"].keys()) == {"retrieved_context"}
        # The session-scoped channel is never set, at the top level or nested.
        assert "sessionAttributes" not in call
        assert "sessionAttributes" not in state

    # The session-scoped key appears nowhere in the serialized request either (a nested or
    # renamed channel would still be caught by the string scan).
    for call in client.calls:
        blob = json.dumps(call, ensure_ascii=False)
        assert "sessionAttributes" not in blob


# ===========================================================================
# Rendered prompt: our instruction + the bare turn question + fragments via attributes
# ===========================================================================
def test_rendered_prompt_carries_instruction_bare_question_and_fragment_attributes():
    """For each turn the rendered request carries our optimized instruction as the only
    system text, the BARE turn question (no fragments concatenated in), and the turn's
    retrieved fragments through the per-turn ``promptSessionAttributes`` channel."""
    client, _adapter, _item, _resp = _drive_generate()
    assert len(client.calls) == len(UTTERANCES)

    for turn_index, call in enumerate(client.calls):
        # The optimized prompt is the only system instruction (padding only appends
        # trailing spaces, so the visible content is unchanged).
        assert call["instruction"].strip() == INSTRUCTION
        assert call["foundationModel"] == MODEL_ID

        # The question is the BARE utterance — no fragments, no <context> block in it.
        assert call["inputText"] == UTTERANCES[turn_index]
        for frag in FRAGMENTS:
            assert frag["id"] not in call["inputText"]
            assert frag["text"] not in call["inputText"]

        # The fragments ride the per-turn promptSessionAttributes channel instead.
        context_text = _rendered_context(call)
        for frag in FRAGMENTS:
            assert frag["id"] in context_text
            assert frag["text"] in context_text


def test_fragments_ride_attributes_not_the_system_instruction():
    """The fragments enter through the per-turn ``promptSessionAttributes`` channel only —
    never the system instruction (which would be a different, accumulating channel)."""
    client, _adapter, _item, _resp = _drive_generate()
    for call in client.calls:
        for frag in FRAGMENTS:
            assert frag["id"] not in call["instruction"]
            assert frag["text"] not in call["instruction"]


def test_fragments_off_renders_bare_question_with_no_attribute_channel():
    """With ``send_fragments=False`` the fragment channel is empty: the question is the bare
    utterance, no ``promptSessionAttributes`` is attached, and — since server history mode
    has nothing else per-turn to carry — no ``inlineSessionState`` at all."""
    client, _adapter, _item, _resp = _drive_generate(send_fragments=False)
    for turn_index, call in enumerate(client.calls):
        assert call["inputText"] == UTTERANCES[turn_index]
        assert "inlineSessionState" not in call
        for frag in FRAGMENTS:
            assert frag["id"] not in call["inputText"]
            assert frag["text"] not in call["inputText"]


# ===========================================================================
# Grounding parity (Req 13.7): rendered ids == the turn's grounding ids
# ===========================================================================
def test_grounding_parity_rendered_ids_equal_judge_grounding_ids():
    """The fragment ids rendered to the model equal the grounding ids the judge would
    receive for that turn — computed with the SAME expression the judge loop uses
    (``tuple(str(f.get("id","")) for f in frags)``), so the model and the judge ground on
    one identical set (Req 13.7)."""
    client, _adapter, _item, _resp = _drive_generate()

    # The grounding ids the JudgeInLoopScorer records for the turn (same construction as
    # bakeoff/quality/optimizer/judge_loop.py).
    judge_grounding_ids = tuple(str(f.get("id", "")) for f in FRAGMENTS)

    for call in client.calls:
        rendered_ids = _rendered_fragment_ids(_rendered_context(call))
        assert rendered_ids == judge_grounding_ids


def test_grounding_parity_holds_per_turn_for_distinct_fragment_sets():
    """Exercising the pure request-building seam directly: when each turn carries its OWN
    retrieved fragments, the ids rendered into that turn's request equal that turn's
    grounding ids — grounding parity is per-turn, not an artifact of one shared set."""
    client = RecordingAgentClient()
    adapter = _build_adapter(client)
    item = _multi_turn_item()

    per_turn_fragments = [
        [{"id": f"t{ti}-frag-{j}", "text": f"turn {ti} fragment {j} body text.", "metadata": {}}
         for j in range(ti + 1)]
        for ti in range(len(item.turns))
    ]

    for ti, frags in enumerate(per_turn_fragments):
        request = adapter._build_request(item, ti, [], TEMPERATURE, fragments=frags)
        expected_grounding_ids = tuple(str(f.get("id", "")) for f in frags)
        assert _rendered_fragment_ids(_rendered_context(request)) == expected_grounding_ids
        # The bare question carries none of the fragments.
        assert request["inputText"] == UTTERANCES[ti]
        # The same stable sessionId is used for each turn's request.
        assert request["sessionId"] == f"opt-{ADAPTER_NAME}-{item.item_id}-0"


# ===========================================================================
# Absence of every orchestration marker
# ===========================================================================
def test_no_orchestration_markers_in_any_rendered_request():
    """The full serialized request for every turn is free of every orchestration marker —
    no tool/function-call schema, no ReAct ``Thought/Action/Observation`` scaffolding, no
    'you have access to ...' action-group preamble (design no-noise invariant, Req 13.9)."""
    client, _adapter, _item, _resp = _drive_generate()
    assert client.calls

    for call in client.calls:
        blob = json.dumps(call, ensure_ascii=False)
        for marker in ORCHESTRATION_MARKERS:
            assert marker not in blob, f"orchestration marker leaked into request: {marker!r}"


def test_no_orchestration_markers_in_the_base_template_itself():
    """The minimal base template — the one place Bedrock would normally inject tool
    scaffolding — is itself marker-free."""
    template = config.QUALITY_OPT_INLINE_TEMPLATE
    for marker in ORCHESTRATION_MARKERS:
        assert marker not in template


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
