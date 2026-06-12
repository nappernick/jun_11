"""
Unit tests for per-family / per-thinking-mode prompt selection (Task: prompts).

Covers two contracts:

* :func:`bakeoff.prompts.system_instruction_for` — every roster family resolves to
  a non-empty instruction; the Sonnet families differ between thinking ON and OFF
  (the load-bearing research finding: omit CoT scaffolding when thinking is on,
  add it when thinking is off); the Haiku families are thinking-OFF-only and
  return the same variant regardless of the flag; an unknown family falls back to
  the default instruction; and every variant preserves the answerability
  discipline (ground in fragments; refuse/escalate; flag partial gaps).

* :func:`bakeoff.adapters.base.build_prompt` — threads ``family`` and
  ``thinking_enabled`` through into the system instruction, WHILE keeping the
  retrieved-fragments context byte-identical across families and thinking modes
  (retrieval is the held constant, design AD-2 — only instruction phrasing varies).

No network and no servers are started.
"""
from __future__ import annotations

import pytest

from bakeoff.adapters.base import (
    SYSTEM_INSTRUCTION,
    assemble_context,
    build_prompt,
)
from bakeoff.prompts import (
    DEFAULT_SYSTEM_INSTRUCTION,
    FAMILY_INSTRUCTIONS,
    system_instruction_for,
)
from bakeoff.types import CohortKey, Item, Turn


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
ROSTER_FAMILIES = ("sonnet-4.6", "sonnet-4.5", "haiku-4.5", "haiku-3.5")
THINKING_FAMILIES = ("sonnet-4.6", "sonnet-4.5")
NONTHINKING_ONLY_FAMILIES = ("haiku-4.5", "haiku-3.5")


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


def _single_item(item_id: str = "b0-q01") -> Item:
    return Item(
        id=item_id,
        turn_type="single",
        cohort=_cohort(),
        query="how do I get a corporate card?",
        answerability="full",
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
# system_instruction_for — each family, thinking on/off, default fallback
# ===========================================================================
@pytest.mark.parametrize("family", ROSTER_FAMILIES)
@pytest.mark.parametrize("thinking", [True, False])
def test_every_roster_family_returns_a_nonempty_instruction(family: str, thinking: bool) -> None:
    instruction = system_instruction_for(family, thinking_enabled=thinking)
    assert isinstance(instruction, str)
    assert instruction.strip()  # non-empty


@pytest.mark.parametrize("family", THINKING_FAMILIES)
def test_sonnet_families_differ_between_thinking_on_and_off(family: str) -> None:
    # The headline finding: with thinking ON, omit hand-written CoT scaffolding;
    # with thinking OFF, add an explicit reasoning method + self-check. So the two
    # variants must NOT be identical for the thinking-capable families.
    on = system_instruction_for(family, thinking_enabled=True)
    off = system_instruction_for(family, thinking_enabled=False)
    assert on != off
    # The thinking-OFF variant carries an explicit reasoning/method scaffold that
    # the thinking-ON variant does not.
    assert "<method>" in off
    assert "<method>" not in on


def test_sonnet_45_nonthinking_avoids_the_literal_word_think() -> None:
    # Anthropic: with extended thinking disabled, the 4.5 generation is "particularly
    # sensitive to the word 'think'"; use "consider/evaluate/reason through" instead.
    off = system_instruction_for("sonnet-4.5", thinking_enabled=False).lower()
    assert "think" not in off
    # ...but it still prescribes a manual reasoning method (just phrased differently).
    assert "<method>" in system_instruction_for("sonnet-4.5", thinking_enabled=False)


def test_sonnet_46_thinking_omits_prescriptive_cot_steps() -> None:
    # Thinking-ON Sonnet 4.6 should NOT carry a hand-written numbered method block.
    on = system_instruction_for("sonnet-4.6", thinking_enabled=True)
    assert "<method>" not in on
    assert "<steps>" not in on


@pytest.mark.parametrize("family", NONTHINKING_ONLY_FAMILIES)
def test_thinking_off_only_families_ignore_the_flag(family: str) -> None:
    # Haiku 4.5 / 3.5 are thinking-OFF only in this roster: passing thinking_enabled
    # must degrade gracefully to their single variant (same string either way).
    on = system_instruction_for(family, thinking_enabled=True)
    off = system_instruction_for(family, thinking_enabled=False)
    assert on == off


def test_haiku_35_is_the_most_prescriptive_variant() -> None:
    # The oldest/smallest candidate gets the classic 3.5-era directive scaffolding
    # (the scaffolding newer models no longer need): an explicit numbered procedure,
    # firm ALL-CAPS grounding rules, per-case answer templates, and a pre-send check.
    haiku_35 = system_instruction_for("haiku-3.5", thinking_enabled=False)
    assert "<procedure>" in haiku_35          # explicit numbered procedure
    assert "<answer_templates>" in haiku_35   # literal per-case templates to copy
    assert "<final_check>" in haiku_35        # mandatory pre-send checklist
    assert "ONLY" in haiku_35                 # firm, emphatic grounding instruction
    assert "NEVER" in haiku_35                # firm ALL-CAPS prohibitions


@pytest.mark.parametrize(
    "unknown",
    ["default", "", "gpt-4", "nova-lite", "llama-3-3-70b", "totally-unknown-family"],
)
@pytest.mark.parametrize("thinking", [True, False])
def test_unknown_family_falls_back_to_default(unknown: str, thinking: bool) -> None:
    assert system_instruction_for(unknown, thinking_enabled=thinking) == DEFAULT_SYSTEM_INSTRUCTION


def test_default_matches_backward_compatible_system_instruction() -> None:
    # The default branch is the instruction re-exported from base for back-compat.
    assert DEFAULT_SYSTEM_INSTRUCTION == SYSTEM_INSTRUCTION


@pytest.mark.parametrize(
    "alias,expected_family",
    [
        ("Sonnet-4.6", "sonnet-4.6"),
        ("SONNET-4.5", "sonnet-4.5"),
        ("claude-haiku-4-5", "haiku-4.5"),
        ("haiku_3.5", "haiku-3.5"),
        ("claude-sonnet-4-6", "sonnet-4.6"),
    ],
)
@pytest.mark.parametrize("thinking", [True, False])
def test_family_name_matching_is_lenient(alias: str, expected_family: str, thinking: bool) -> None:
    # Common spelling/separator/prefix variants resolve to the same instruction as
    # the canonical family key (so a caller's "claude-sonnet-4-6" is not mistaken
    # for an unknown family and silently demoted to the default).
    assert system_instruction_for(alias, thinking_enabled=thinking) == system_instruction_for(
        expected_family, thinking_enabled=thinking
    )


# ===========================================================================
# answerability discipline preserved in EVERY variant
# ===========================================================================
def _all_variants():
    seen = set()
    for nonthinking, thinking in FAMILY_INSTRUCTIONS.values():
        for text in (nonthinking, thinking):
            if text not in seen:
                seen.add(text)
                yield text
    yield DEFAULT_SYSTEM_INSTRUCTION


@pytest.mark.parametrize("instruction", list(_all_variants()))
def test_every_variant_preserves_answerability_discipline(instruction: str) -> None:
    low = instruction.lower()
    # grounding: answer only from the provided fragments
    assert "fragment" in low
    # refuse/escalate rather than fabricate: a "don't have that information" + point
    # the user somewhere clause is present (worded variously across variants).
    assert ("don't have" in low) or ("do not have" in low) or ("don't make anything up" in low)
    assert (
        ("point the user" in low)
        or ("where to go next" in low)
        or ("right place" in low)
        or ("contact your support" in low)
        or ("redirect" in low)
    )
    # do-not-guess / do-not-fabricate signal
    assert ("guess" in low) or ("fabricat" in low) or ("make anything up" in low)


@pytest.mark.parametrize("family", ROSTER_FAMILIES)
@pytest.mark.parametrize("thinking", [True, False])
def test_every_roster_variant_flags_partial_gaps(family: str, thinking: bool) -> None:
    # Partial-answerability behavior (answer what's answerable AND flag the gap)
    # must be present in every roster variant.
    low = system_instruction_for(family, thinking_enabled=thinking).lower()
    assert ("part" in low) and (("missing" in low) or ("flag" in low))


@pytest.mark.parametrize("family", ROSTER_FAMILIES)
@pytest.mark.parametrize("thinking", [True, False])
def test_every_roster_variant_addresses_user_tone(family: str, thinking: bool) -> None:
    low = system_instruction_for(family, thinking_enabled=thinking).lower()
    assert "tone" in low


# ===========================================================================
# build_prompt threads family/thinking AND keeps the fragments context constant
# ===========================================================================
def test_build_prompt_threads_family_instruction_into_system() -> None:
    # The chosen family/thinking instruction must actually appear in the system msg.
    item = _single_item()
    haiku = build_prompt(item, FRAGMENTS, family="haiku-3.5")
    sonnet = build_prompt(item, FRAGMENTS, family="sonnet-4.6", thinking_enabled=True)
    assert system_instruction_for("haiku-3.5", thinking_enabled=False) in haiku.system
    assert system_instruction_for("sonnet-4.6", thinking_enabled=True) in sonnet.system
    # different families => different system instruction text
    assert haiku.system != sonnet.system


def test_build_prompt_thinking_flag_changes_system_for_sonnet() -> None:
    item = _single_item()
    on = build_prompt(item, FRAGMENTS, family="sonnet-4.6", thinking_enabled=True)
    off = build_prompt(item, FRAGMENTS, family="sonnet-4.6", thinking_enabled=False)
    assert on.system != off.system


def test_build_prompt_default_family_uses_backward_compatible_instruction() -> None:
    # Old call sites (no family kwarg) keep the default instruction verbatim.
    item = _single_item()
    prompt = build_prompt(item, FRAGMENTS)
    assert SYSTEM_INSTRUCTION in prompt.system


def _fragments_block(system: str) -> str:
    """Extract the constant 'Reference fragments:' block from a system message."""
    marker = "\n\nReference fragments:\n"
    idx = system.index(marker)
    return system[idx + len(marker):]


@pytest.mark.parametrize("thinking", [True, False])
def test_fragments_context_identical_across_all_families(thinking: bool) -> None:
    # The CORE fairness invariant: retrieval is the held constant. Regardless of
    # family or thinking mode, the rendered fragments block must be byte-identical
    # (and equal to assemble_context's output). Only the instruction differs.
    item = _single_item()
    expected = assemble_context(FRAGMENTS)
    blocks = {
        fam: _fragments_block(build_prompt(item, FRAGMENTS, family=fam, thinking_enabled=thinking).system)
        for fam in ROSTER_FAMILIES + ("default",)
    }
    for fam, block in blocks.items():
        assert block == expected, f"fragments block changed for family={fam}"
    # all families produced the exact same block
    assert len(set(blocks.values())) == 1


def test_fragments_context_identical_across_thinking_modes() -> None:
    item = _single_item()
    on = _fragments_block(build_prompt(item, FRAGMENTS, family="sonnet-4.6", thinking_enabled=True).system)
    off = _fragments_block(build_prompt(item, FRAGMENTS, family="sonnet-4.6", thinking_enabled=False).system)
    assert on == off == assemble_context(FRAGMENTS)


def test_build_prompt_multi_turn_threads_family_but_keeps_turns_and_context() -> None:
    # Family/thinking only affect the system instruction; the multi-turn message
    # assembly (prior turns + prior answers) and the fragments context are unchanged.
    item = _multi_item()
    prior = ["You can self-attest under $75.", "The form is in the expense portal."]
    h = build_prompt(item, FRAGMENTS, family="haiku-3.5", prior_answers=prior, upto_turn_index=2)
    s = build_prompt(
        item, FRAGMENTS, family="sonnet-4.5", thinking_enabled=True,
        prior_answers=prior, upto_turn_index=2,
    )
    # identical conversational messages regardless of family/thinking
    assert [m.role for m in h.messages] == [m.role for m in s.messages]
    assert [m.content for m in h.messages] == [m.content for m in s.messages]
    # identical fragments context
    assert _fragments_block(h.system) == _fragments_block(s.system) == assemble_context(FRAGMENTS)
    # but different system instruction
    assert h.system != s.system


def test_build_prompt_no_fragments_still_renders_no_context_for_every_family() -> None:
    item = _single_item()
    for fam in ROSTER_FAMILIES + ("default",):
        prompt = build_prompt(item, [], family=fam)
        assert "no reference fragments" in prompt.system.lower()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
