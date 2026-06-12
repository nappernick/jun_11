"""
Deterministic, network-free mock model adapter (Task 5, Req 3.5).

:class:`MockAdapter` implements the :class:`bakeoff.adapters.base.ModelAdapter`
protocol without any endpoint, so the runner, the scorers (Tasks 6/7), and the
integration tests (Task 10) can exercise the whole harness with no live model.
Two properties make it useful:

* **Deterministic given a seed (Req 3.5).** For a fixed ``(seed, profile)`` the
  answer text, TTFT, total latency, and token usage are a pure function of the
  inputs ``(item, fragments, temperature)`` — identical across calls, processes,
  and machines (the per-call RNG is seeded from a stable SHA-256, never Python's
  salted ``hash()``).
* **Configurable answer-quality profile (Req 3.5).** A :class:`MockProfile`
  selects latency and *behavior*, including the two behaviors the answerability
  scorer tests need (design Req 5, Tasks 6/7):
  ``fabricate_on_unanswerable`` (answer confidently when the item is
  ``answerability == "none"`` → the scorer should mark ``abstention_correct == 0``
  and count a fabrication) and ``refuse_on_answerable`` (refuse a ``full`` item →
  the scorer should mark ``unwarranted_refusal == 1``).

It still goes through the *real* streaming/latency-capture path
(:func:`bakeoff.adapters.base.consume_text_stream`) so TTFT is measured exactly as
a live adapter measures it — but against a **virtual clock** the adapter advances
itself, so tests are instant and the timing is deterministic (no real sleeping).
Like every adapter, it owns only prompt assembly + "generation" + latency capture
and **never scores** (Req 3.4).
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import AsyncIterator, Optional, Sequence

from bakeoff.adapters.base import Prompt, build_prompt, consume_text_stream
from bakeoff.types import Item, ModelResponse

__all__ = ["MockProfile", "MockAdapter", "REFUSAL_MARKERS", "GAP_FLAG_MARKER"]


# Phrases a refusal/escalation answer is guaranteed to contain, so the
# answerability scorer (Task 7) has a deterministic signal to key on in tests.
# Exposed so that scorer can import the contract rather than duplicating strings.
REFUSAL_MARKERS: tuple[str, ...] = (
    "i don't have that information",
    "please contact",
)

# Phrase a "partial" answer uses to flag the unanswerable gap (rewards
# answer-the-answerable-part-and-flag-the-rest; design Req 5.2).
GAP_FLAG_MARKER: str = "i don't have information about the rest; please contact support"


@dataclass(frozen=True)
class MockProfile:
    """Latency + answer-quality/behavior profile for a :class:`MockAdapter`.

    ``quality`` shapes how grounded an *answerable* answer is:
    ``"high"`` quotes several retrieved fragments, ``"medium"`` quotes one,
    ``"low"`` ignores the fragments (a parametric-memory-style answer that should
    score poorly on grounding). The two booleans inject the misbehaviors the
    answerability scorer tests assert against.
    """

    quality: str = "high"                  # "high" | "medium" | "low"
    fabricate_on_unanswerable: bool = False
    refuse_on_answerable: bool = False
    base_latency_ms: float = 400.0         # nominal total generation wall-clock
    ttft_fraction: float = 0.3             # fraction of latency before first token
    latency_jitter_ms: float = 60.0        # max +/- jitter; scaled by temperature

    # --- presets the scorer/runner tests reach for -----------------------
    @classmethod
    def grounded(cls, **kw: object) -> "MockProfile":
        """A well-behaved, grounded, high-quality candidate (the default)."""
        return cls(quality="high", **kw)  # type: ignore[arg-type]

    @classmethod
    def fabricator(cls, **kw: object) -> "MockProfile":
        """Fabricates on unanswerable items (abstention_correct should be 0)."""
        return cls(fabricate_on_unanswerable=True, **kw)  # type: ignore[arg-type]

    @classmethod
    def over_refuser(cls, **kw: object) -> "MockProfile":
        """Refuses answerable items (unwarranted_refusal should be 1)."""
        return cls(refuse_on_answerable=True, **kw)  # type: ignore[arg-type]


class MockAdapter:
    """A deterministic, no-network :class:`ModelAdapter` implementation."""

    def __init__(
        self,
        name: str = "mock",
        *,
        seed: int = 0,
        profile: Optional[MockProfile] = None,
    ) -> None:
        self.name = name
        self.seed = seed
        self.profile = profile or MockProfile()

    # -- determinism helpers ----------------------------------------------
    def _derive_seed(self, item: Item, temperature: float, turn_index: int) -> int:
        """A stable integer seed for one (item, temperature, turn) generation.

        Uses SHA-256 (not the salted built-in ``hash``) so the value is identical
        across processes and runs — the basis of the determinism guarantee.
        """
        canonical = "\x1f".join(
            (
                str(self.seed),
                self.name,
                item.id,
                f"{temperature:.6f}",
                str(turn_index),
            )
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return int(digest[:16], 16)

    def _latencies(self, rng: random.Random, temperature: float) -> tuple[float, float]:
        """Return ``(ttft_ms, total_ms)`` for one generation, deterministically.

        Jitter scales with temperature so a higher temperature widens run-to-run
        latency spread (a knob the pilot can use), while ``temperature == 0`` is
        perfectly repeatable. Always returns ``total_ms >= ttft_ms > 0``.
        """
        p = self.profile
        jitter_scale = p.latency_jitter_ms * min(max(temperature, 0.0), 1.0)
        jitter = rng.uniform(-jitter_scale, jitter_scale) if jitter_scale else 0.0
        total = max(1.0, p.base_latency_ms + jitter)
        ttft = max(0.5, total * p.ttft_fraction)
        return ttft, total

    @staticmethod
    async def _token_stream(
        tokens: Sequence[str], ttft_ms: float, total_ms: float, clock_state: list[float]
    ) -> AsyncIterator[str]:
        """Yield tokens while advancing a virtual clock to model TTFT + latency.

        The clock is in **seconds** (like ``time.perf_counter``), so the latency-
        capture helper's seconds→ms conversion produces the intended ms figures.
        It jumps by ``ttft_ms`` (converted to seconds) before the first token (so
        the captured TTFT equals ``ttft_ms``) and spreads the remaining time evenly
        across the rest of the tokens (so the captured total equals ``total_ms``).
        """
        if not tokens:
            clock_state[0] += total_ms / 1000.0
            return
        per_token = (total_ms - ttft_ms) / max(1, len(tokens) - 1) if len(tokens) > 1 else 0.0
        for i, tok in enumerate(tokens):
            clock_state[0] += (ttft_ms if i == 0 else per_token) / 1000.0
            yield tok

    # -- answer composition (behavior; never scores) ----------------------
    def _effective_answerability(self, item: Item, turn_index: int) -> str:
        """Answerability governing the current turn (per-turn for multi-turn)."""
        if item.is_multi_turn and item.turns:
            turn = item.turns[min(turn_index, len(item.turns) - 1)]
            if turn.answerability:
                return turn.answerability
        return item.answerability or "unknown"

    def _compose_answer(
        self, item: Item, fragments: Sequence[dict], turn_index: int
    ) -> str:
        """Produce the answer text for one turn per the profile + answerability."""
        p = self.profile
        answerability = self._effective_answerability(item, turn_index)
        refusal = (
            "I don't have that information in the reference material. "
            "Please contact your support team for help."
        )

        if answerability == "none":
            if p.fabricate_on_unanswerable:
                # Confident, specific, ungrounded — a fabrication (no refusal).
                return (
                    "Yes. Per the standard policy you can complete this within 30 "
                    "days by submitting the appropriate request form, after which it "
                    "is approved automatically."
                )
            return refusal

        if answerability == "full" and p.refuse_on_answerable:
            return refusal

        if answerability == "partial" and not p.fabricate_on_unanswerable:
            return f"{self._grounded_answer(fragments)} However, {GAP_FLAG_MARKER}."

        # full / partial-overclaim / unknown -> a (more or less) grounded answer.
        return self._grounded_answer(fragments)

    def _grounded_answer(self, fragments: Sequence[dict]) -> str:
        """A grounded answer whose richness tracks ``profile.quality``."""
        p = self.profile
        texts = [str(f.get("text", "")).strip() for f in fragments]
        texts = [t for t in texts if t]
        if p.quality == "low" or not texts:
            # Ignores the fragments — should score low on grounding.
            return "You should be able to handle this through the usual process."
        n = 2 if p.quality == "high" else 1
        used = texts[:n]
        body = " ".join(used)
        return f"Based on the reference material: {body}"

    # -- the protocol method ----------------------------------------------
    async def generate(
        self,
        item: Item,
        fragments: Sequence[dict],
        temperature: float,
    ) -> ModelResponse:
        """Generate a deterministic answer, capturing true (virtual) TTFT/latency.

        For a multi-turn item, each turn is "generated" in order with its prompt
        built from the prior turns + the prior answers (Req 3.3); the trial's TTFT
        is the first turn's first token and the total generation latency is the sum
        across turns. ``per_turn_answers`` holds every turn's answer in order and
        ``text`` is the final turn's answer; for single-turn they coincide.
        """
        clock_state = [0.0]
        clock = lambda: clock_state[0]  # noqa: E731 - tiny virtual clock

        n_turns = len(item.turns) if (item.is_multi_turn and item.turns) else 1

        per_turn_answers: list[str] = []
        prompts: list[Prompt] = []
        prompt_tokens = 0
        completion_tokens = 0
        trial_ttft_ms: Optional[float] = None
        generation_total_ms = 0.0

        for turn_index in range(n_turns):
            prompt = build_prompt(
                item,
                fragments,
                prior_answers=per_turn_answers,
                upto_turn_index=turn_index if n_turns > 1 else None,
            )
            prompts.append(prompt)

            rng = random.Random(self._derive_seed(item, temperature, turn_index))
            ttft_ms, total_ms = self._latencies(rng, temperature)

            answer = self._compose_answer(item, fragments, turn_index)
            tokens = _tokenize_for_stream(answer)

            start_perf = clock()
            stream = self._token_stream(tokens, ttft_ms, total_ms, clock_state)
            text, captured_ttft_ms, gen_ms = await consume_text_stream(
                stream, start_perf, clock=clock
            )

            per_turn_answers.append(text)
            if trial_ttft_ms is None:
                trial_ttft_ms = captured_ttft_ms
            generation_total_ms += gen_ms
            prompt_tokens += _count_tokens(prompt.to_text())
            completion_tokens += len(tokens)

        ttft_ms = trial_ttft_ms if trial_ttft_ms is not None else generation_total_ms

        return ModelResponse(
            text=per_turn_answers[-1],
            ttft_ms=ttft_ms,
            generation_total_ms=generation_total_ms,
            token_usage={
                "prompt": prompt_tokens,
                "completion": completion_tokens,
                "total": prompt_tokens + completion_tokens,
            },
            per_turn_answers=per_turn_answers,
            finish_reason="stop",
            model=self.name,
            raw={"prompts": [p.to_text() for p in prompts]},
        )


# ---------------------------------------------------------------------------
# Tiny tokenization helpers (whitespace-level; deterministic)
# ---------------------------------------------------------------------------
def _tokenize_for_stream(text: str) -> list[str]:
    """Split into stream chunks, preserving spacing so ``"".join`` rebuilds text."""
    words = text.split(" ")
    return [w if i == 0 else " " + w for i, w in enumerate(words)]


def _count_tokens(text: str) -> int:
    """A crude whitespace token count for usage accounting (deterministic)."""
    return len(text.split())
