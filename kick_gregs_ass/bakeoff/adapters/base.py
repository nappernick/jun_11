"""
Model-adapter protocol + shared prompt assembly + latency capture (Task 5, Req 3).

The :class:`ModelAdapter` ``Protocol`` is the single seam that makes the harness
reusable: adding a candidate is implementing one adapter, and nothing else in the
system changes (Req 3, design Component 3). An adapter owns **only** four things
(Req 3.4):

1. **Prompt assembly** — turn the item (and, for multi-turn, its prior turns) plus
   the constant retrieved fragments into a provider-agnostic :class:`Prompt`
   (:func:`build_prompt`). Adapters serialize that into their provider's wire
   format.
2. **The endpoint call** — stream so a true time-to-first-token is measurable
   (Req 3.2).
3. **Temperature handling** — pass the trial's temperature through to the model.
4. **Latency / TTFT / token-usage capture** — via :func:`consume_text_stream`,
   which records TTFT at the *first streamed token* and the total generation
   wall-clock.

Adapters **never score** (Req 3.4) — they return a normalized
:class:`bakeoff.types.ModelResponse` and the scoring pipeline (Tasks 6/7) takes it
from there.

This module is import-light (pure stdlib + :mod:`bakeoff.types`): it pulls in no
boto3/httpx, so the protocol and prompt assembly are usable and testable anywhere.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Optional, Protocol, Sequence, runtime_checkable

from bakeoff.prompts import DEFAULT_SYSTEM_INSTRUCTION, system_instruction_for
from bakeoff.types import Item, ModelResponse

__all__ = [
    "ModelAdapter",
    "PromptMessage",
    "Prompt",
    "SYSTEM_INSTRUCTION",
    "assemble_context",
    "build_prompt",
    "consume_text_stream",
]


# The "default" system instruction, re-exported for backward compatibility. The
# task and the answerability discipline the scorers grade against (ground in the
# provided context; refuse/escalate rather than fabricate when the answer is not
# present) are constant across candidates; what now varies per model FAMILY and
# per thinking-mode is only the *phrasing/structure* of that instruction, selected
# by :func:`bakeoff.prompts.system_instruction_for` (the deliberate per-family
# trade documented in ``docs/PROMPT_DESIGN.md``). Retrieval — the context every
# candidate receives — stays the held constant (design AD-2). Adapters still do
# NOT score that behavior (Req 3.4). The canonical text now lives in
# :mod:`bakeoff.prompts`; this alias keeps existing imports working.
SYSTEM_INSTRUCTION: str = DEFAULT_SYSTEM_INSTRUCTION

# Header used when there are no retrieved fragments at all, so the model is told
# explicitly rather than handed an empty context block (an unanswerable signal).
_NO_CONTEXT = "(no reference fragments were retrieved for this question)"

# Placeholder used for a prior assistant turn when the real prior answer is not
# supplied (e.g. when assembling a preview prompt). Generation in the adapters
# always feeds the model's *actual* prior answers, so this is only a fallback.
_ABSENT_PRIOR_ANSWER = "(previous assistant reply omitted)"


# ---------------------------------------------------------------------------
# Provider-agnostic prompt representation
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PromptMessage:
    """One conversational message in a :class:`Prompt` (role + text content)."""

    role: str          # "user" | "assistant"
    content: str


@dataclass(frozen=True)
class Prompt:
    """A provider-agnostic prompt: a system instruction + ordered messages.

    Each adapter renders this into its provider's request shape (e.g. Bedrock
    Converse ``system`` + ``messages``). :meth:`to_text` flattens it to a single
    string for transports/mocks that take plain text and for deterministic hashing.
    """

    system: str
    messages: tuple[PromptMessage, ...]

    def to_text(self) -> str:
        """Flatten to a single plain-text transcript (system, then each message)."""
        lines = [f"[system]\n{self.system}"]
        for m in self.messages:
            lines.append(f"[{m.role}]\n{m.content}")
        return "\n\n".join(lines)

    @property
    def user_messages(self) -> tuple[PromptMessage, ...]:
        """The user-role messages, in order (convenience for tests/inspection)."""
        return tuple(m for m in self.messages if m.role == "user")


# ---------------------------------------------------------------------------
# Prompt assembly (shared by every adapter — Req 3.3)
# ---------------------------------------------------------------------------
def assemble_context(fragments: Sequence[dict]) -> str:
    """Render the constant retrieved fragments into a numbered context block.

    Each fragment dict is the verbatim ``/retrieve`` shape
    (``{id, text, metadata, ...}``); we render its id + text so the model (and a
    later grounding scorer) can attribute claims to a specific fragment. Robust to
    a missing ``text``/``id`` (uses ``""``/index) so a partial fragment never
    crashes prompt assembly.
    """
    if not fragments:
        return _NO_CONTEXT
    blocks = []
    for i, frag in enumerate(fragments, start=1):
        frag_id = str(frag.get("id", f"frag-{i}"))
        text = str(frag.get("text", "")).strip()
        blocks.append(f"[{i}] (id={frag_id})\n{text}")
    return "\n\n".join(blocks)


def build_prompt(
    item: Item,
    fragments: Sequence[dict],
    *,
    family: str = "default",
    thinking_enabled: bool = False,
    prior_answers: Sequence[str] = (),
    upto_turn_index: Optional[int] = None,
    instruction_override: Optional[str] = None,
) -> Prompt:
    """Assemble the prompt for one generation, incorporating prior turns (Req 3.3).

    The **system** message is ``<family/thinking-aware instruction>`` +
    ``\\n\\nReference fragments:\\n`` + the constant retrieved fragments. Only the
    instruction portion varies per model family and thinking-mode (via
    :func:`bakeoff.prompts.system_instruction_for`); the fragments block produced
    by :func:`assemble_context` is **identical for every candidate** — retrieval
    is the held constant (design AD-2), so the comparison stays about how each
    model *uses* the same context, not about giving some models more of it. See
    ``docs/PROMPT_DESIGN.md`` for the sourced rationale behind per-family prompts.

    The conversation is built as:

    * **single-turn** — one user message carrying the item's focal ``query``.
    * **multi-turn** — the ordered prior turns rendered as ``user``/``assistant``
      pairs (``prior_answers`` supplies the assistant side; an explicit placeholder
      is used for any prior turn whose answer is not yet available), followed by
      the current turn's utterance as the trailing ``user`` message. This is what
      makes a multi-turn prompt *include prior turns* (Req 3.3).

    Args:
        item: the normalized item (single- or multi-turn).
        fragments: the constant retrieved fragments (verbatim ``/retrieve`` shape).
        family: candidate model family, e.g. ``"sonnet-4.6"``, ``"haiku-3.5"``.
            Selects the family-tuned system instruction; an unknown family (the
            ``"default"`` sentinel) yields the backward-compatible
            :data:`SYSTEM_INSTRUCTION`.
        thinking_enabled: whether extended/adaptive thinking is ON for this
            candidate. Selects the thinking-ON instruction variant where the family
            has one (Sonnet families); ignored for thinking-OFF-only families.
        instruction_override: when provided, this exact string is used as the
            system instruction INSTEAD of the family/thinking-selected one. The
            constant retrieved fragments are still appended unchanged, so the
            held-constant context guarantee is preserved — only the instruction
            phrasing changes. This is the seam the multi-turn quality study uses to
            inject an optimized prompt variant; ``family``/``thinking_enabled`` are
            ignored for instruction selection when it is set (``None`` => unchanged
            family-selected behavior).
        prior_answers: the model's own answers to the earlier turns, in order;
            aligned to ``item.turns[: upto_turn_index]``. Empty when assembling the
            first turn or a single-turn prompt.
        upto_turn_index: index of the turn currently being answered (0-based). When
            ``None``, the final turn is the current one (so the full conversation is
            assembled). Ignored for single-turn items.

    Returns:
        A :class:`Prompt` ready for an adapter to serialize.
    """
    instruction = (
        instruction_override
        if instruction_override is not None
        else system_instruction_for(family, thinking_enabled)
    )
    system = f"{instruction}\n\nReference fragments:\n{assemble_context(fragments)}"

    if item.is_multi_turn and item.turns:
        turns = item.turns
        current = len(turns) - 1 if upto_turn_index is None else upto_turn_index
        current = max(0, min(current, len(turns) - 1))

        messages: list[PromptMessage] = []
        for i in range(current):
            messages.append(PromptMessage("user", turns[i].user_utterance))
            answer = (
                prior_answers[i]
                if i < len(prior_answers)
                else _ABSENT_PRIOR_ANSWER
            )
            messages.append(PromptMessage("assistant", answer))
        messages.append(PromptMessage("user", turns[current].user_utterance))
        return Prompt(system=system, messages=tuple(messages))

    # Single-turn: one user message with the focal query.
    query = item.query or ""
    return Prompt(system=system, messages=(PromptMessage("user", query),))


# ---------------------------------------------------------------------------
# Latency / TTFT capture (shared by every streaming adapter — Req 3.2)
# ---------------------------------------------------------------------------
async def consume_text_stream(
    deltas: AsyncIterator[str],
    start_perf: float,
    *,
    clock: Callable[[], float] = time.perf_counter,
) -> tuple[str, float, float]:
    """Consume a stream of text deltas, measuring a TRUE time-to-first-token.

    ``start_perf`` is the ``clock()`` value captured by the caller *immediately
    before* it opened the stream (so TTFT includes the request round-trip, exactly
    as a user feels it — Req 3.2). TTFT is stamped when the **first non-empty
    delta** arrives; the total generation latency is the wall-clock from
    ``start_perf`` to stream exhaustion.

    Args:
        deltas: an async iterator yielding text chunks (token deltas). Empty
            chunks are accumulated but do not set TTFT.
        start_perf: ``clock()`` captured just before the stream was opened.
        clock: monotonic clock (injectable so tests can drive deterministic times).

    Returns:
        ``(text, ttft_ms, generation_total_ms)`` — concatenated text, time to first
        token in ms, total generation wall-clock in ms.
    """
    ttft_ms: Optional[float] = None
    parts: list[str] = []
    async for delta in deltas:
        if delta:
            if ttft_ms is None:
                ttft_ms = (clock() - start_perf) * 1000.0
            parts.append(delta)
    total_ms = (clock() - start_perf) * 1000.0
    if ttft_ms is None:
        # Empty stream: no token ever arrived; TTFT degenerates to total.
        ttft_ms = total_ms
    return "".join(parts), ttft_ms, total_ms


# ---------------------------------------------------------------------------
# The adapter protocol (the reusability seam — Req 3.1)
# ---------------------------------------------------------------------------
@runtime_checkable
class ModelAdapter(Protocol):
    """Uniform interface every candidate model is driven through (Req 3.1).

    Implementations accept an :class:`Item`, the constant retrieved ``fragments``,
    and a ``temperature``, and return a normalized :class:`ModelResponse` capturing
    TTFT, total generation latency, and token usage (Req 3.2). Implementations own
    prompt assembly, the endpoint call, temperature handling, and latency capture —
    and nothing else; **they never score** (Req 3.4).
    """

    #: Stable adapter/candidate name (stamped onto ``TrialEvent.model``).
    name: str

    async def generate(
        self,
        item: Item,
        fragments: Sequence[dict],
        temperature: float,
    ) -> ModelResponse:
        """Generate an answer for ``item`` given the constant ``fragments``."""
        ...
