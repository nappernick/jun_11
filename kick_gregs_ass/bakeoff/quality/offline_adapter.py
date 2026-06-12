"""
Deterministic, network-free multi-turn adapter for the quality study's tests.

The bake-off's :class:`bakeoff.adapters.mock.MockAdapter` is great for exercising
the runner, but it IGNORES the system instruction by construction — so it cannot
validate that the optimizer actually prefers a better *prompt*. This adapter is
the opposite: its answer text is a deterministic function of (the injected system
instruction, the turn's reference text, the prior answers), so a prompt variant
that includes more of the helpful multi-turn levers produces answers measurably
closer to the reference. That lets the WHOLE optimization + run + scoring pipeline
be tested offline with a real ranking signal, without any Bedrock call.

It is explicitly a TEST/OFFLINE double, not a model of any real model's behavior.
The real quality run uses :class:`bakeoff.adapters.bedrock.BedrockModelAdapter`
(with ``instruction_override`` set). The contract it honors:

* implements the :class:`bakeoff.adapters.base.ModelAdapter` protocol
  (``name`` + ``async generate(item, fragments, temperature) -> ModelResponse``);
* multi-turn conversational: each turn is "answered" with the prior turns'
  answers available, and ``per_turn_answers`` holds every turn in order;
* the per-turn answer echoes a fraction of that turn's reference text, where the
  fraction scales with how many known-helpful lever markers the instruction
  contains — so closeness rises with prompt quality, deterministically.

``quality_lift`` exposes the lever→lift mapping the answers are built from, so a
test can assert the optimizer recovers the intended ranking.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional, Sequence

from bakeoff.quality.dataset import turn_reference
from bakeoff.quality.types import GroundTruthKind
from bakeoff.types import Item, ModelResponse

__all__ = ["QualityOfflineAdapter", "LEVER_MARKERS", "quality_lift"]

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'-]*")

# Marker substrings that appear in a variant's instruction iff the corresponding
# multi-turn lever is on (they are the XML tag names from
# bakeoff.quality.prompts.MULTI_TURN_BLOCKS). Each contributes a deterministic
# lift to how closely the offline answer tracks the reference text.
LEVER_MARKERS: dict[str, float] = {
    "<conversation>": 0.18,
    "<grounding_each_turn>": 0.22,
    "<answerability_every_turn>": 0.12,
    "<consistency>": 0.06,
}


def quality_lift(instruction: str) -> float:
    """Total closeness lift implied by the levers present in ``instruction``.

    Sum of :data:`LEVER_MARKERS` whose marker appears in the instruction, capped
    at a base offset so even the bare "base" variant produces a partially-correct
    answer (otherwise the optimizer could not distinguish "bad" from "empty").
    Deterministic and monotonic in the lever set, so more helpful levers => higher
    lift => closer answers.
    """
    lift = 0.30  # base competence floor for the control variant
    for marker, delta in LEVER_MARKERS.items():
        if marker in instruction:
            lift += delta
    return min(1.0, lift)


class QualityOfflineAdapter:
    """A deterministic offline :class:`ModelAdapter` whose quality tracks the prompt.

    Args:
        name: candidate name stamped on the response.
        instruction_override: the exact system instruction this adapter "obeys"
            (the optimizer/run inject the variant here, mirroring the Bedrock
            adapter's parameter). The number of helpful levers in it drives how
            close the answers are to the references.
        item_lookup: maps ``item_id`` -> :class:`Item`, so the adapter can find the
            turn references its answers are built to approximate. (Real adapters do
            not need this; this offline double does, because it deliberately
            synthesizes reference-tracking answers.)
        floor: minimum per-turn competence multiplier (keeps determinism stable).
    """

    def __init__(
        self,
        name: str,
        *,
        instruction_override: str,
        item_lookup: dict[str, Item],
        family: Optional[str] = None,
        thinking: bool = False,
    ) -> None:
        self.name = name
        self.instruction_override = instruction_override
        self.family = family or name
        self.thinking = thinking
        self._items = item_lookup
        self._lift = quality_lift(instruction_override)

    def _turn_answer(self, item: Item, turn_index: int, prior: Sequence[str]) -> str:
        """Synthesize a deterministic answer for one turn that tracks its reference.

        For an abstention turn (turn-1 of an unanswerable item) the answer is a
        correct refusal iff the instruction carries the answerability lever — so a
        variant without that lever fabricates and scores 0 on abstention, exactly
        the behavior the optimizer should punish.
        """
        kind, reference = turn_reference(item, turn_index)

        if kind == GroundTruthKind.ABSTENTION:
            if "<answerability_every_turn>" in self.instruction_override or (
                "<grounding_each_turn>" in self.instruction_override
            ):
                return (
                    "I don't have that information in the reference material. "
                    "Please contact your support team for help."
                )
            # No answerability discipline => confident fabrication (wrong).
            return (
                "Yes, you can complete this within 30 days by submitting the "
                "standard form; it is then approved automatically."
            )

        words = _WORD.findall(reference)
        if not words:
            return "I'll help with that."

        # Echo a deterministic prefix of the reference whose length scales with the
        # prompt-implied lift; a small per-(item,turn) jitter keeps reps varied.
        jitter = self._jitter(item.item_id, turn_index)
        frac = min(1.0, max(0.05, self._lift * (0.9 + 0.2 * jitter)))
        take = max(1, int(round(len(words) * frac)))
        echoed = " ".join(words[:take])
        return f"Based on the reference material: {echoed}."

    def _jitter(self, item_id: str, turn_index: int) -> float:
        """Deterministic jitter in ``[0, 1]`` from a stable hash (no salted hash())."""
        h = hashlib.sha256(f"{self.name}\x1f{item_id}\x1f{turn_index}".encode()).hexdigest()
        return int(h[:8], 16) / 0xFFFFFFFF

    async def generate(
        self, item: Item, fragments: Sequence[dict], temperature: float
    ) -> ModelResponse:
        """Generate deterministic per-turn answers conversationally (offline)."""
        n_turns = len(item.turns) if (item.is_multi_turn and item.turns) else 1
        per_turn: list[str] = []
        for ti in range(n_turns):
            per_turn.append(self._turn_answer(item, ti, per_turn))
        return ModelResponse(
            text=per_turn[-1] if per_turn else "",
            ttft_ms=1.0,
            generation_total_ms=float(n_turns),
            token_usage={"prompt": 0, "completion": 0, "total": 0},
            per_turn_answers=per_turn,
            finish_reason="stop",
            model=self.name,
            raw={"instruction_lift": self._lift, "n_turns": n_turns},
        )
