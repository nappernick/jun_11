"""
IslandLoop — the per-island rapid inner loop of the v2 coverage-ladder + island-tournament
optimizer (design ``docs/OPTIMIZER_V2_DESIGN_NOTES.md`` §"Structure (FULL)" items 1–2).

v1 scored every attempted prompt against the full ~180-conversation tuning slice, so the
first score took many minutes and cycles were far too slow. v2 evaluates a prompt against a
**small indicative rung first**, iterates fast, and only escalates coverage once a prompt
*earns* it (successive-halving / Hyperband). This module is the **island inner loop**: each
island runs its own ``author → score → re-author`` hill-climb on its *current* rung,
evolving its own best (Champion) prompt independently of the other island. Two islands per
model coevolve, and per-island author prompting is deliberately divergent
(anti-over-convergence) so the two pursue meaningfully different prompt shapes; when both are
confident the orchestrator runs a tournament on a shared higher rung and migrates the winner
(both of which live ABOVE this file — the orchestrator agent owns the tournament scheduler,
the island/tournament SSE event types, and the island-partitioned durable store fields).

What this file is responsible for (the inner loop only):

* :class:`IslandState` — a frozen, serializable snapshot of one island's current position on
  the ladder (rung, Champion + its in-loop score/CI, the per-rung and total iteration
  counters, the stuck flag, and this island's anti-over-convergence ``style``) that the
  orchestrator/UI read.
* :class:`IslandLoop` — the loop itself:
  * :meth:`IslandLoop.step` runs ONE ``author → score`` iteration at the *current* rung:
    score the Champion on the rung's items at the rung's reps via
    :class:`~bakeoff.quality.optimizer.judge_loop.JudgeInLoopScorer`, select its worst judged
    turns via :func:`~bakeoff.quality.optimizer.failures.select_failures`, author a Challenger
    (streaming the author's reasoning to the emitter), score the Challenger on the SAME rung,
    and promote it iff the gain is significant via
    :class:`~bakeoff.quality.optimizer.convergence.PromotionDecider`.
  * :meth:`IslandLoop.should_escalate` — the **hybrid** escalation gate (design §"Owner
    decisions": statistical "not significantly worse at this rung" + the model-judgment
    "worth more coverage" proxy).
  * :meth:`IslandLoop.advance_rung` — graduate to the next rung and re-score the Champion on
    the bigger rung for a tighter baseline (clamped at the top rung).
  * :meth:`IslandLoop.is_stuck` — the patience gate the orchestrator uses to force a
    tournament/escalation when an island churns without improving.

Per-island author divergence WITHOUT changing the AuthorClient seam (constraint). The
:class:`~bakeoff.quality.optimizer.author.AuthorClient` Protocol's ``author(...)`` has no
``style`` parameter, and the ``champion_instruction`` it receives is embedded **verbatim**
into the author contract's ``<current_instruction>`` block (the text the author is asked to
revise). The :class:`~bakeoff.quality.optimizer.author.OfflineAuthorClient` also *appends* to
whatever ``champion_instruction`` it is handed. So this loop injects the island's ``style``
by prepending it as a **sentinel-delimited authoring-stance block** to the champion the
author sees (:data:`_STANCE_OPEN`/:data:`_STANCE_CLOSE`), and then **strips that exact block
back out** of the author's returned instruction before it is scored, promoted, or stored
(:func:`_strip_stance`). The strip is idempotent and the augmentation re-strips first, so the
stance never compounds across iterations and never pollutes the scored/stored Champion;
usability is recomputed against the TRUE (un-styled) Champion so the "byte-identical /
empty → non-usable" rule (Req 3.5) is preserved. This keeps ``author.py`` untouched while
still threading the per-island stance through the contract path.

Reused unchanged (the expensive correctness core, per the design's impact map):
``JudgeInLoopScorer`` (retrieval-always, held-constant memoized fragments, Opus triad,
abstention weighting), ``select_failures``, the ``AuthorClient`` seam, ``PromotionDecider``,
and :func:`bakeoff.quality.optimizer.stats.gain_report`. The ``backend``, ``store``, and
``emitter`` are injected duck-typed objects; this module performs no network I/O of its own,
so it runs identically against the offline bundle (zero network) or the live bundle.

Store note (deliberate): the ``store`` is accepted and held for forward use, but this inner
loop does **not** write durable records to it. The current
:class:`~bakeoff.quality.optimizer.store.IterationRecord` / ``AuditRecord`` schema is
model-partitioned with no ``island_id`` / ``rung`` fields — the design's impact map lists
those store fields as orchestrator-owned NEW work — so writing here would collide the two
islands' records under one model. Durable, island-partitioned persistence is wired by the
orchestrator agent once the schema carries ``island_id``.

Event note: this file only reuses the EXISTING
:class:`~bakeoff.quality.optimizer.events.OptimizerEventEmitter` methods
(:meth:`~bakeoff.quality.optimizer.events.OptimizerEventEmitter.champion_scored`,
:meth:`~bakeoff.quality.optimizer.events.OptimizerEventEmitter.author_token`,
:meth:`~bakeoff.quality.optimizer.events.OptimizerEventEmitter.iteration_completed`); it does
NOT invent island/tournament event types (those are the orchestrator agent's to add). Because
both islands of a model share that model's Model_Channel, their reused events interleave on
the wire until the orchestrator adds island-stamped event types.

Sourcing honesty (carried from the design notes / requirements.md): the coverage-ladder
(successive-halving / Hyperband), island-model coevolution, and tournament-with-migration are
**external / industry** techniques, and the judge triad as the decision signal, the
abstention failure modes, and the between-conversation-SD / CI reasoning are grounded in
external/industry RAG-evaluation practice and this repo's own observed Opus verdicts — **not**
in Amazon-internal primary sources. Re-validate any judge-derived number against internal
guidance before using it to defend a decision upward.
"""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Sequence

from bakeoff import config
from bakeoff.quality.optimizer.convergence import PromotionDecider
from bakeoff.quality.optimizer.events import OptimizerEventEmitter
from bakeoff.quality.optimizer.failures import select_failures
from bakeoff.quality.optimizer.judge_loop import JudgeInLoopScorer, SliceScore, TurnVerdict
from bakeoff.quality.optimizer.rungs import Rung
from bakeoff.quality.optimizer.stats import gain_report
from bakeoff.quality.optimizer.store import OptimizerStore, make_prompt_diff
from bakeoff.quality.prompts import quality_system_instruction, variants_for_model

if TYPE_CHECKING:  # typing only — no import cycle (backends.py never imports this module).
    from bakeoff.quality.optimizer.backends import OptimizerBackend

__all__ = [
    "IslandState",
    "StepDetail",
    "IslandLoop",
]

#: The optimizer phase the island inner loop runs in. Islands iterate during the Phase-A
#: "iterate" phase (the reserved validation set is scored later, in Phase B, unchanged), so
#: every reused ``champion_scored`` event is stamped ``phase="A"`` like the v1 controller.
_PHASE: str = "A"

#: The fixed-menu variant used ONLY as the default iteration-0 seed Champion (design
#: §"What's reused vs new": the fixed five-variant menu is a *seed source*, never the
#: iteration mechanism). ``full_stack`` turns on every multi-turn lever — the strongest
#: permitted starting point — mirroring the v1 controller's seed.
_SEED_VARIANT_ID: str = "full_stack"

# -- Per-island authoring-stance injection sentinels --------------------------------------
# The island's ``style`` is prepended to the champion the author SEES, wrapped in these
# unique sentinels so it can be reliably stripped back out of the author's output before the
# challenger is scored/promoted/stored. The sentinels are intentionally not natural-language
# so they never collide with real instruction text.
_STANCE_OPEN: str = "<<<ISLAND_AUTHORING_STANCE>>>"
_STANCE_CLOSE: str = "<<<END_ISLAND_AUTHORING_STANCE>>>"

#: Matches a whole stance block plus any trailing whitespace that separated it from the
#: instruction body, so stripping a block leaves no doubled blank lines behind. ``DOTALL`` so
#: a multi-line stance is consumed; non-greedy so adjacent blocks strip independently.
_STANCE_RE: re.Pattern[str] = re.compile(
    re.escape(_STANCE_OPEN) + r".*?" + re.escape(_STANCE_CLOSE) + r"\s*",
    re.DOTALL,
)


def _strip_stance(instruction: str) -> str:
    """Remove every island authoring-stance block from ``instruction`` (idempotent).

    Used both to clean the author's returned Challenger (so the stance steering never ends
    up scored or stored as part of a prompt) and to clean the Champion before re-augmenting
    it (so the stance never compounds across iterations). Applying it to text that contains
    no stance block is a no-op, so it is safe to call unconditionally.
    """
    return _STANCE_RE.sub("", instruction)


def _stance_block(style: str) -> str:
    """Render the island's ``style`` as a sentinel-delimited authoring-stance block.

    The block is framed as guidance on *how* to rewrite (not text to copy into the
    instruction) and wrapped in :data:`_STANCE_OPEN`/:data:`_STANCE_CLOSE` so
    :func:`_strip_stance` can remove it cleanly afterward.
    """
    return (
        f"{_STANCE_OPEN}\n"
        "Authoring stance for this island (guidance on HOW to write the rewrite, not text "
        "to copy verbatim into the instruction):\n"
        f"{style.strip()}\n"
        f"{_STANCE_CLOSE}"
    )


def _augment_with_stance(champion_instruction: str, style: str) -> str:
    """Prepend this island's stance to the champion the author sees (compounding-safe).

    Any pre-existing stance block is stripped first so re-authoring never stacks stances,
    and an empty/whitespace ``style`` leaves the champion untouched. The returned text is
    what is handed to ``AuthorClient.author(champion_instruction=...)``; the author's output
    is run back through :func:`_strip_stance` before it is used.
    """
    clean = _strip_stance(champion_instruction)
    if not style.strip():
        return clean
    return f"{_stance_block(style)}\n\n{clean}"


def _seed_instruction_for(model: str) -> str:
    """Assemble the default iteration-0 seed Champion instruction for ``model``.

    Resolves the ``full_stack`` variant from
    :func:`bakeoff.quality.prompts.variants_for_model` and composes the full standalone
    system instruction via :func:`bakeoff.quality.prompts.quality_system_instruction`, using
    the model's ``family`` / ``thinking`` from :data:`bakeoff.config.QUALITY_MODELS`. Mirrors
    the v1 controller's seed helper: the fixed menu is used ONLY as this seed source, never as
    the iteration mechanism. Falls back to the model key as the family and ``thinking=False``
    for an unknown key, and to the last variant in the ladder (also ``full_stack``) if the id
    is ever renamed, so the seed never raises.
    """
    spec = config.QUALITY_MODELS.get(model, {})
    family = str(spec.get("family", model))
    thinking = bool(spec.get("thinking", False))
    variants = variants_for_model(model)
    by_id = {v.variant_id: v for v in variants}
    variant = by_id.get(_SEED_VARIANT_ID) or variants[-1]
    return quality_system_instruction(
        family=family, thinking_enabled=thinking, variant=variant
    )


@dataclass(frozen=True)
class IslandState:
    """A frozen, serializable snapshot of one island's position on the coverage ladder.

    Returned by :meth:`IslandLoop.step` and :meth:`IslandLoop.advance_rung` and read by the
    orchestrator (to decide escalation / tournaments / migration) and the UI (the multi-island
    coverage-ladder view). Every field is a primitive or ``None`` so the snapshot round-trips
    through :meth:`to_dict` / JSON without custom encoders.

    Fields:
        island_id: which island this is (``0``/``1`` for the two islands of a model); the
            index into :data:`bakeoff.config.QUALITY_OPT_ISLAND_STYLES`.
        model: the Target_Model whose prompt this island optimizes.
        style: this island's anti-over-convergence authoring stance (the divergence knob).
        rung_index: the island's current rung on the ladder (``0`` = smallest/cheapest).
        n_rungs: total rungs on the ladder (so the UI can render progress / "top rung").
        rung_n_items / rung_n_conversations: the current rung's coverage (items, and
            ``items * reps`` conversations — the CI sample size at this rung).
        at_top_rung: whether the island is already at the highest-coverage rung.
        champion_instruction: the island's current best (Champion) instruction text.
        champion_score / champion_ci_half_width: the Champion's most recent in-loop triad +
            95% CI half-width on the current rung (the in-loop decision signal, never the
            final reported number — that is always the Phase-B value). ``None`` before the
            Champion has been scored at all.
        prior_rung_score: the Champion's triad on the rung it was on *before* the current one
            (the baseline the escalation gate checks "not significantly worse than"); ``None``
            while the island is still on rung 0.
        iterations_at_rung: ``step`` iterations run since entering the current rung (reset by
            :meth:`IslandLoop.advance_rung`).
        total_iterations: ``step`` iterations run across the whole ladder (monotonic).
        consecutive_non_improving: trailing run of non-promoting iterations (reset on any
            promotion) — the convergence-style counter surfaced on the reused iteration event.
        improved_at_rung: whether the island has produced at least one promotion (usable
            improvement) at the current rung (the model-judgment half of the escalation gate).
        stuck: whether the island has hit its rung patience without improving (see
            :meth:`IslandLoop.is_stuck`).
    """

    island_id: int
    model: str
    style: str
    rung_index: int
    n_rungs: int
    rung_n_items: int
    rung_n_conversations: int
    at_top_rung: bool
    champion_instruction: str
    champion_score: Optional[float]
    champion_ci_half_width: Optional[float]
    prior_rung_score: Optional[float]
    iterations_at_rung: int
    total_iterations: int
    consecutive_non_improving: int
    improved_at_rung: bool
    stuck: bool

    def to_dict(self) -> dict:
        """Return a JSON-ready dict of this snapshot (field-declaration order)."""
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class StepDetail:
    """The rich, durable-persistence detail of ONE :meth:`IslandLoop.step` iteration.

    :meth:`IslandLoop.step` returns an :class:`IslandState` (the position snapshot the
    orchestrator/UI read), but a step also *computes* the full champion/challenger scores,
    the authored challenger text + rationale, the prompt diff, and the promotion outcome —
    everything the orchestrator needs to write a complete ``IterationRecord`` +
    ``AuditRecord`` so the per-iteration detail survives a page reload. Rather than widen
    ``step``'s return type (which the tests and the orchestrator's island-step loop depend
    on), the loop stashes the most recent step's detail and the orchestrator reads it via
    :meth:`IslandLoop.last_step_detail` immediately after awaiting the step.

    All fields are primitives / ``None`` so the orchestrator can build durable records
    without reaching into the loop's private state. ``challenger_*`` and ``gain_*`` are
    ``None`` for an iteration that produced no usable challenger (Req 3.5).
    """

    iteration_index: int
    champion_instruction_before: str
    champion_score: float
    champion_ci_half_width: float
    challenger_instruction: Optional[str]
    challenger_score: Optional[float]
    challenger_ci_half_width: Optional[float]
    challenger_per_dimension: dict[str, float]
    author_rationale: str
    prompt_diff: str
    promoted: bool
    gain_absolute: Optional[float]
    gain_percent: Optional[float]
    slice_n_conversations: int
    between_conversation_sd: float
    mean_closeness: float
    abstention_reward_mean: float
    answered_when_unsure_rate: float
    champion_instruction_after: str


class IslandLoop:
    """One island's rapid ``author → score → re-author`` hill-climb on its current rung.

    Each island holds its own Champion and climbs independently on the shared, nested
    coverage ladder; per-island ``style`` makes the two islands' author calls diverge
    (anti-over-convergence) without changing the ``AuthorClient`` seam (see the module
    docstring). The orchestrator drives an island by repeatedly calling :meth:`step`, checking
    :meth:`should_escalate` / :meth:`is_stuck`, and calling :meth:`advance_rung` to graduate it
    to broader coverage.

    The ``backend`` is consumed duck-typed through :class:`JudgeInLoopScorer` (answer adapter
    factory, judge, closeness, held-constant retrieval) and the ``AuthorClient`` seam, so this
    loop never hard-imports the backend bundle and works identically offline or live. The
    ``store`` is held for the orchestrator-wired durable persistence (not written here — see
    the module docstring) and the ``emitter`` streams the reused ``optimizer_*`` events.
    """

    def __init__(
        self,
        *,
        island_id: int,
        model: str,
        backend: "OptimizerBackend",
        ladder: Sequence[Rung],
        store: OptimizerStore,
        emitter: OptimizerEventEmitter,
        style: str,
        seed_instruction: Optional[str] = None,
        threshold: float = config.QUALITY_OPT_SIGNIFICANCE_THRESHOLD,
        failures_k: int = config.QUALITY_OPT_FAILURES_K,
        rung_patience: int = config.QUALITY_OPT_ISLAND_RUNG_PATIENCE,
        escalation_ci_slack: float = config.QUALITY_OPT_ESCALATION_CI_SLACK,
    ) -> None:
        """Wire one island to its model, its coverage ladder, and its collaborators.

        Args:
            island_id: which island this is (the index into
                :data:`bakeoff.config.QUALITY_OPT_ISLAND_STYLES`).
            model: the Target_Model whose prompt this island optimizes.
            backend: the injected :class:`OptimizerBackend` bundle (duck-typed); supplies the
                answer adapter factory, judge, closeness, held-constant retrieval, and Author.
            ladder: the nested coverage ladder (ascending rungs) from
                :func:`bakeoff.quality.optimizer.rungs.build_rung_ladder`; shared and identical
                across both islands so neither is advantaged by a luckier rung. Must be
                non-empty.
            store: the durable :class:`OptimizerStore`; held for orchestrator-wired
                island-partitioned persistence, not written by this inner loop (see the module
                docstring).
            emitter: the per-Model_Channel :class:`OptimizerEventEmitter`; the loop reuses its
                existing ``champion_scored`` / ``author_token`` / ``iteration_completed``
                methods (it invents no island event types).
            style: this island's anti-over-convergence authoring stance, threaded into every
                author call (typically ``config.QUALITY_OPT_ISLAND_STYLES[island_id]``).
            seed_instruction: explicit iteration-0 seed Champion; when ``None`` defaults to the
                ``full_stack`` fixed-menu variant for ``model`` (seed source only).
            threshold: minimum absolute triad gain that promotes a Challenger (Req 1.6/5.1);
                defaults to ``config.QUALITY_OPT_SIGNIFICANCE_THRESHOLD``.
            failures_k: how many worst judged turns are handed to the Author each iteration
                (Req 3.4); defaults to ``config.QUALITY_OPT_FAILURES_K``.
            rung_patience: max ``step`` iterations at a single rung before the island is
                declared stuck (if it has not improved there); defaults to
                ``config.QUALITY_OPT_ISLAND_RUNG_PATIENCE``.
            escalation_ci_slack: how many CI half-widths below the prior-rung baseline the
                Champion may sit and still escalate ("not significantly worse"); defaults to
                ``config.QUALITY_OPT_ESCALATION_CI_SLACK``.

        Raises:
            ValueError: if ``ladder`` is empty (an island needs at least one rung to run on).
        """
        self._ladder: list[Rung] = list(ladder)
        if not self._ladder:
            raise ValueError("IslandLoop requires a non-empty coverage ladder.")

        self._island_id = int(island_id)
        self._model = model
        self._backend = backend
        self._store = store
        self._emitter = emitter
        self._style = style
        self._threshold = float(threshold)
        self._failures_k = int(failures_k)
        self._rung_patience = int(rung_patience)
        self._escalation_ci_slack = float(escalation_ci_slack)

        # Champion state. The seed is the iteration-0 baseline Champion; its score is unknown
        # until the first step (or advance_rung) measures it on a rung.
        self._champion_instruction: str = (
            seed_instruction if seed_instruction is not None else _seed_instruction_for(model)
        )
        self._champion_score: Optional[float] = None
        self._champion_ci_half_width: Optional[float] = None

        # Ladder position + the escalation baseline (the Champion's score on the rung it was
        # on *before* the current one; None while still on rung 0).
        self._rung_index: int = 0
        self._prior_rung_score: Optional[float] = None

        # Counters / latches. ``iterations_at_rung`` is the raw step count at the current rung
        # (reset on advance_rung); ``improved_at_rung`` latches on the first promotion at the
        # current rung (reset on advance_rung) and is both the escalation model-judgment proxy
        # and the "not stuck" signal; ``consecutive_non_improving`` resets on any promotion.
        self._total_iterations: int = 0
        self._iterations_at_rung: int = 0
        self._consecutive_non_improving: int = 0
        self._improved_at_rung: bool = False

        # Promotion predicate (stateless) + a per-reps scorer cache (rungs vary their reps, so
        # the in-loop scorer's rep count varies by rung; building is cheap but cache anyway).
        self._decider = PromotionDecider()
        self._scorers: dict[int, JudgeInLoopScorer] = {}

        # The most recent step's rich detail (for orchestrator-wired durable persistence).
        # None until the first step() runs; read via last_step_detail() right after a step.
        self._last_step_detail: Optional[StepDetail] = None

        # Identities recorded on emitted score events (read duck-typed so this loop never
        # hard-depends on the backend bundle's concrete type), mirroring the v1 controller.
        self._retrieval_backend_name = str(
            getattr(getattr(backend, "retrieval", None), "name", "unknown")
        )

    # -- public read-only accessors ------------------------------------------------------
    @property
    def island_id(self) -> int:
        """This island's id (index into ``config.QUALITY_OPT_ISLAND_STYLES``)."""
        return self._island_id

    @property
    def model(self) -> str:
        """The Target_Model this island optimizes."""
        return self._model

    @property
    def rung_index(self) -> int:
        """The island's current rung index (``0`` = smallest/cheapest)."""
        return self._rung_index

    @property
    def champion_instruction(self) -> str:
        """The island's current best (Champion) instruction text."""
        return self._champion_instruction

    def last_step_detail(self) -> Optional[StepDetail]:
        """Return the rich detail of the most recent :meth:`step` (``None`` before any step).

        The orchestrator reads this immediately after awaiting :meth:`step` to write the
        complete durable ``IterationRecord`` + ``AuditRecord`` (so the per-iteration
        champion/challenger scores, authored challenger text + rationale, and prompt diff
        survive a reload). It reflects the single most recent ``step`` only; a re-score from
        :meth:`advance_rung` does not update it (that is a measurement, not an iteration).
        """
        return self._last_step_detail

    def state(self) -> IslandState:
        """Return a frozen, serializable snapshot of the island's current position."""
        return self._snapshot()

    # -- the one-iteration entry point ---------------------------------------------------
    async def step(self) -> IslandState:
        """Run one optimizer iteration at the CURRENT rung; return the new island state.

        Dispatches on the corrected-cadence gate (Req 1): when
        ``config.QUALITY_OPT_ROUND_CADENCE_ENABLED`` is on, ``step()`` runs a full
        :meth:`_step_round` — the Author self-iterates ``config.QUALITY_OPT_ROUND_STEPS`` times
        on the cheap In_Loop_Signal (no Judge) and the Opus Judge adjudicates ONCE at the
        Round's conclusion to decide promotion. When off it runs the legacy
        :meth:`_step_single` (one ``author → Opus-score`` iteration), byte-for-byte today's
        behavior. Both return an :class:`IslandState`, emit the same events, and stash a
        :class:`StepDetail`, so the orchestrator and tests are unchanged.
        """
        if config.QUALITY_OPT_ROUND_CADENCE_ENABLED:
            return await self._step_round()
        return await self._step_single()

    async def _step_single(self) -> IslandState:
        """Run ONE ``author → score`` iteration at the CURRENT rung; return the new state.

        The design's per-island inner-loop sequence, scoped to ``self._rung_index``:

        1. Score the current Champion on the rung's ``items`` at the rung's ``reps`` with the
           retrieval-always :class:`JudgeInLoopScorer` (also yields the per-turn verdicts
           failure selection needs), emitting ``optimizer_champion_scored`` (role
           ``"champion"``).
        2. Select the worst judged turns, answering-when-unsure first, via
           :func:`select_failures`.
        3. Author a Challenger from the Champion + those failures, with this island's
           ``style`` threaded into the author's view of the Champion (then stripped back out
           of the result), streaming the author's reasoning to ``optimizer_author_token``.
        4. Score the Challenger on the SAME rung — only when it is usable (a non-empty rewrite
           that differs from the TRUE Champion) — emitting ``optimizer_champion_scored``
           (role ``"challenger"``).
        5. Promote the Challenger iff its triad beats the Champion by at least ``threshold``
           via :class:`PromotionDecider`; a non-usable Challenger is never promoted (Req 3.5).
        6. Update the Champion (and the per-rung / total counters and latches) and emit
           ``optimizer_iteration_completed``.

        Returns the post-iteration :class:`IslandState`.
        """
        rung = self._ladder[self._rung_index]
        iteration_index = self._total_iterations
        champion_before = self._champion_instruction

        # 1) Score the current Champion on this rung.
        champ_score = await self._score(
            champion_before, role="champion", rung=rung, iteration_index=iteration_index
        )

        # 2) Select the worst judged turns (answering-when-unsure first).
        failures = select_failures(champ_score, k=self._failures_k)

        # 3) Author the Challenger with this island's stance threaded in, then strip the
        #    stance back out so the scored/stored Challenger is the clean instruction text.
        authored = await self._author(
            champion_before, failures, iteration_index=iteration_index
        )
        challenger_instruction = _strip_stance(authored.instruction)

        # Recompute usability against the TRUE (un-styled) Champion so the empty / identical
        # rules (Req 3.5) hold regardless of what the author saw or echoed of the stance.
        usable = bool(challenger_instruction.strip()) and (
            challenger_instruction != champion_before
        )

        # 4) Score the Challenger on the SAME rung (only when usable).
        chall_score: Optional[SliceScore] = None
        if usable:
            chall_score = await self._score(
                challenger_instruction,
                role="challenger",
                rung=rung,
                iteration_index=iteration_index,
            )

        # 5) Promote iff significant; a non-usable Challenger is never promoted (Req 3.5).
        challenger_triad = (
            chall_score.triad_score if chall_score is not None else champ_score.triad_score
        )
        promoted = self._decider.decide(
            champ_score.triad_score, challenger_triad, self._threshold, usable=usable
        )

        # Gain reported both ways (Req 5.4); only meaningful for a usable Challenger.
        if usable and chall_score is not None:
            gains = gain_report(champ_score.triad_score, chall_score.triad_score)
            gain_absolute: Optional[float] = gains["absolute_delta"]
            gain_percent: Optional[float] = gains["percent_delta"]
        else:
            gain_absolute = None
            gain_percent = None

        # 6) Update Champion + counters.
        self._total_iterations += 1
        self._iterations_at_rung += 1
        if promoted and chall_score is not None:
            self._champion_instruction = challenger_instruction
            self._champion_score = chall_score.triad_score
            self._champion_ci_half_width = chall_score.ci_half_width
            self._improved_at_rung = True
            self._consecutive_non_improving = 0
        else:
            # Refresh the Champion's score to this iteration's fresh measurement (same prompt).
            self._champion_score = champ_score.triad_score
            self._champion_ci_half_width = champ_score.ci_half_width
            self._consecutive_non_improving += 1

        # Emit the reused iteration event. ``lookback_version_ids`` is empty here: durable,
        # island-partitioned version history is orchestrator-wired (see the module docstring).
        prompt_diff = (
            make_prompt_diff(champion_before, challenger_instruction) if usable else ""
        )
        self._emitter.iteration_completed(
            model=self._model,
            iteration_index=iteration_index,
            challenger_triad=(chall_score.triad_score if chall_score is not None else None),
            challenger_ci_half_width=(
                chall_score.ci_half_width if chall_score is not None else None
            ),
            gain_absolute=gain_absolute,
            gain_percent=gain_percent,
            accepted=promoted,
            consecutive_non_improving=self._consecutive_non_improving,
            champion_instruction=self._champion_instruction,
            prompt_diff=prompt_diff,
            lookback_version_ids=[],
            island_id=self._island_id,
        )

        # Stash this step's rich detail so the orchestrator can write a complete, durable
        # IterationRecord + AuditRecord (champion/challenger scores, authored challenger
        # text + rationale, diff) — what makes the per-iteration view survive a reload.
        self._last_step_detail = StepDetail(
            iteration_index=iteration_index,
            champion_instruction_before=champion_before,
            champion_score=champ_score.triad_score,
            champion_ci_half_width=champ_score.ci_half_width,
            challenger_instruction=(challenger_instruction if usable else None),
            challenger_score=(chall_score.triad_score if chall_score is not None else None),
            challenger_ci_half_width=(
                chall_score.ci_half_width if chall_score is not None else None
            ),
            challenger_per_dimension=(
                dict(chall_score.per_dimension_mean) if chall_score is not None else {}
            ),
            author_rationale=authored.rationale,
            prompt_diff=prompt_diff,
            promoted=promoted,
            gain_absolute=gain_absolute,
            gain_percent=gain_percent,
            slice_n_conversations=champ_score.n_conversations,
            between_conversation_sd=getattr(champ_score, "between_conv_sd", 0.0),
            mean_closeness=champ_score.mean_closeness,
            abstention_reward_mean=champ_score.abstention_reward_mean,
            answered_when_unsure_rate=champ_score.answered_when_unsure_rate,
            champion_instruction_after=self._champion_instruction,
        )

        return self._snapshot()

    async def _step_round(self) -> IslandState:
        """Run ONE Round at the current rung: in-loop self-iteration + one Opus adjudication.

        The corrected loop cadence (Req 1). Within the Round the Author rewrites and is scored
        ``config.QUALITY_OPT_ROUND_STEPS`` times using ONLY the cheap In_Loop_Signal
        (``score_in_loop`` — closeness + abstention, never the Judge, Req 1.1/1.2), keeping the
        best in-round candidate. The Opus Judge then adjudicates EXACTLY ONCE at the Round's
        conclusion (Req 1.3): the champion and, when a usable candidate was put forward, that
        candidate are scored via :meth:`_score` (which emits ``optimizer_champion_scored``),
        and :class:`PromotionDecider` decides promotion from those Opus scores (Req 1.4). The
        in-round iterations emit no champion_scored events (the Judge did not run), so Opus is
        wholly out of the per-iteration hot loop. External contract is identical to
        :meth:`_step_single`: returns an :class:`IslandState`, emits ``iteration_completed``,
        and stashes a :class:`StepDetail`.
        """
        rung = self._ladder[self._rung_index]
        iteration_index = self._total_iterations
        champion_before = self._champion_instruction
        scorer = self._scorer_for(rung.reps)
        n_steps = max(0, int(config.QUALITY_OPT_ROUND_STEPS))  # Req 1.5: configurable count

        # In-loop baseline for the champion (NO Opus) — drives failure selection and the
        # within-Round candidate choice.
        best_candidate = champion_before
        best_in_loop = await scorer.score_in_loop(
            model=self._model,
            instruction=champion_before,
            items=rung.items,
            prompt_role="champion",
        )
        best_rationale = ""

        for _ in range(n_steps):
            failures = select_failures(best_in_loop, k=self._failures_k)
            authored = await self._author(
                best_candidate, failures, iteration_index=iteration_index
            )
            challenger_instruction = _strip_stance(authored.instruction)
            # Usability recomputed against the current best in-round candidate (Req 3.5).
            usable_cand = bool(challenger_instruction.strip()) and (
                challenger_instruction != best_candidate
            )
            if not usable_cand:
                continue
            cand_in_loop = await scorer.score_in_loop(
                model=self._model,
                instruction=challenger_instruction,
                items=rung.items,
                prompt_role="challenger",
            )
            # In_Loop_Signal selects which candidate the Round puts forward; it never decides
            # the promotion (that is the concluding Opus adjudication below, Req 1.4).
            if cand_in_loop.triad_score > best_in_loop.triad_score:
                best_candidate = challenger_instruction
                best_in_loop = cand_in_loop
                best_rationale = authored.rationale

        # --- Round conclusion: the ONE Opus adjudication (Req 1.3). ---
        champ_opus = await self._score(
            champion_before, role="champion", rung=rung, iteration_index=iteration_index
        )
        round_usable = best_candidate != champion_before
        cand_opus: Optional[SliceScore] = None
        if round_usable:
            cand_opus = await self._score(
                best_candidate, role="challenger", rung=rung, iteration_index=iteration_index
            )

        # Promotion follows the concluding Judge adjudication, never the In_Loop_Signal (Req 1.4).
        candidate_triad = (
            cand_opus.triad_score if cand_opus is not None else champ_opus.triad_score
        )
        promoted = self._decider.decide(
            champ_opus.triad_score, candidate_triad, self._threshold, usable=round_usable
        )

        if round_usable and cand_opus is not None:
            gains = gain_report(champ_opus.triad_score, cand_opus.triad_score)
            gain_absolute: Optional[float] = gains["absolute_delta"]
            gain_percent: Optional[float] = gains["percent_delta"]
        else:
            gain_absolute = None
            gain_percent = None

        # Update Champion + counters (mirrors _step_single bookkeeping exactly).
        self._total_iterations += 1
        self._iterations_at_rung += 1
        if promoted and cand_opus is not None:
            self._champion_instruction = best_candidate
            self._champion_score = cand_opus.triad_score
            self._champion_ci_half_width = cand_opus.ci_half_width
            self._improved_at_rung = True
            self._consecutive_non_improving = 0
        else:
            self._champion_score = champ_opus.triad_score
            self._champion_ci_half_width = champ_opus.ci_half_width
            self._consecutive_non_improving += 1

        prompt_diff = (
            make_prompt_diff(champion_before, best_candidate) if round_usable else ""
        )
        self._emitter.iteration_completed(
            model=self._model,
            iteration_index=iteration_index,
            challenger_triad=(cand_opus.triad_score if cand_opus is not None else None),
            challenger_ci_half_width=(
                cand_opus.ci_half_width if cand_opus is not None else None
            ),
            gain_absolute=gain_absolute,
            gain_percent=gain_percent,
            accepted=promoted,
            consecutive_non_improving=self._consecutive_non_improving,
            champion_instruction=self._champion_instruction,
            prompt_diff=prompt_diff,
            lookback_version_ids=[],
            island_id=self._island_id,
        )

        self._last_step_detail = StepDetail(
            iteration_index=iteration_index,
            champion_instruction_before=champion_before,
            champion_score=champ_opus.triad_score,
            champion_ci_half_width=champ_opus.ci_half_width,
            challenger_instruction=(best_candidate if round_usable else None),
            challenger_score=(cand_opus.triad_score if cand_opus is not None else None),
            challenger_ci_half_width=(
                cand_opus.ci_half_width if cand_opus is not None else None
            ),
            challenger_per_dimension=(
                dict(cand_opus.per_dimension_mean) if cand_opus is not None else {}
            ),
            author_rationale=best_rationale,
            prompt_diff=prompt_diff,
            promoted=promoted,
            gain_absolute=gain_absolute,
            gain_percent=gain_percent,
            slice_n_conversations=champ_opus.n_conversations,
            between_conversation_sd=getattr(champ_opus, "between_conv_sd", 0.0),
            mean_closeness=champ_opus.mean_closeness,
            abstention_reward_mean=champ_opus.abstention_reward_mean,
            answered_when_unsure_rate=champ_opus.answered_when_unsure_rate,
            champion_instruction_after=self._champion_instruction,
        )

        return self._snapshot()

    # -- escalation / rung advancement ---------------------------------------------------
    def should_escalate(self) -> bool:
        """Whether the Champion has earned more coverage — the hybrid escalation gate.

        Hybrid (design §"Owner decisions"): the statistical half is *elimination* — at the
        small rungs the CI is too wide to *select* on a 0.05 gap, so the gate only asks that
        the Champion is **not significantly worse** than its score on the prior rung, i.e. its
        current-rung triad is within ``escalation_ci_slack`` CI half-widths *below* that
        prior-rung baseline. The model-judgment half ("the model says let's bump it up") is
        proxied by requiring **at least one usable improvement at this rung** (a promotion
        here) — the island actually found something better, so the prompt is worth the more
        expensive coverage.

        Returns ``False`` at the top rung (nothing to escalate to) and ``False`` until the
        island has produced a promotion at the current rung. On rung 0 there is no prior-rung
        baseline, so the statistical half is vacuously satisfied and the gate rests on the
        usable-improvement half.
        """
        # Model-judgment half: must have made a real (promoted) improvement at this rung.
        if not self._improved_at_rung:
            return False
        # Nothing to escalate to at the top rung.
        if self._rung_index >= len(self._ladder) - 1:
            return False
        # Statistical half: not significantly worse than the prior-rung baseline. On rung 0
        # (no prior baseline / no measured Champion score) this is vacuously satisfied.
        if self._prior_rung_score is None or self._champion_score is None:
            return True
        slack = self._escalation_ci_slack * (self._champion_ci_half_width or 0.0)
        return self._champion_score >= (self._prior_rung_score - slack)

    async def advance_rung(self) -> IslandState:
        """Graduate to the next rung and re-score the Champion there (clamped at the top).

        Records the Champion's current-rung score as the ``prior_rung_score`` baseline the
        next rung's escalation gate checks against, advances the rung index (a no-op at the
        top rung), resets the per-rung counters/latches, and re-scores the Champion on the
        bigger rung to obtain a tighter baseline (emitting ``optimizer_champion_scored`` for
        the re-score). The re-score is a measurement, not an author iteration, so it does not
        advance ``total_iterations`` / ``iterations_at_rung``. Returns the post-advance state.
        """
        # Already at the top rung: clamp (no-op) and return the current snapshot unchanged.
        if self._rung_index >= len(self._ladder) - 1:
            return self._snapshot()

        # Remember the score on the rung we are leaving as the escalation baseline, then move.
        self._prior_rung_score = self._champion_score
        self._rung_index += 1
        self._iterations_at_rung = 0
        self._improved_at_rung = False

        # Re-score the Champion on the bigger rung for a tighter baseline.
        rung = self._ladder[self._rung_index]
        score = await self._score(
            self._champion_instruction,
            role="champion",
            rung=rung,
            iteration_index=self._total_iterations,
        )
        self._champion_score = score.triad_score
        self._champion_ci_half_width = score.ci_half_width
        return self._snapshot()

    def is_stuck(self) -> bool:
        """Whether the island has churned its rung patience without improving.

        ``True`` once the island has run at least ``rung_patience`` ``step`` iterations at the
        current rung **without** producing a promotion there (``improved_at_rung`` is still
        ``False``). The orchestrator uses this to force a tournament or escalation rather than
        let an island spin forever at one rung. An island that *did* improve at the rung is not
        stuck — it is an escalation candidate instead (see :meth:`should_escalate`).
        """
        return self._iterations_at_rung >= self._rung_patience and not self._improved_at_rung

    # -- small collaborators -------------------------------------------------------------
    async def _score(
        self, instruction: str, *, role: str, rung: Rung, iteration_index: int
    ) -> SliceScore:
        """Score ``instruction`` on ``rung`` and emit ``optimizer_champion_scored``.

        Uses a :class:`JudgeInLoopScorer` built for the rung's ``reps`` (cached per rep count,
        since rungs vary their reps), scoring every turn of every conversation in the rung's
        ``items`` (retrieval-always), then streams the scored slice — triad + CI +
        per-dimension breakdown + abstention summary + secondary closeness — to this model's
        Per_Model_View with ``role`` labelling Champion vs Challenger. ``phase="A"`` (the
        iterate phase).
        """
        score = await self._scorer_for(rung.reps).score_prompt(
            model=self._model,
            instruction=instruction,
            items=rung.items,
            prompt_role=role,
        )
        self._emitter.champion_scored(
            model=self._model,
            phase=_PHASE,
            iteration_index=iteration_index,
            role=role,
            triad=score.triad_score,
            ci_half_width=score.ci_half_width,
            ci_low=score.ci_low,
            ci_high=score.ci_high,
            per_dimension=score.per_dimension_mean,
            abstention_reward_mean=score.abstention_reward_mean,
            answered_when_unsure_rate=score.answered_when_unsure_rate,
            retrieval_backend=self._retrieval_backend_name,
            mean_closeness=score.mean_closeness,
            n_conversations=score.n_conversations,
            island_id=self._island_id,
        )
        return score

    def _scorer_for(self, reps: int) -> JudgeInLoopScorer:
        """Return (cached) the in-loop scorer for a given per-item ``reps`` count.

        Rungs ascend their rep counts (cheap rungs use fewer reps, higher rungs more to
        tighten the CI where real selection happens), and :class:`JudgeInLoopScorer` fixes
        ``reps`` at construction, so one scorer is cached per distinct rep count.
        """
        key = max(1, int(reps))
        scorer = self._scorers.get(key)
        if scorer is None:
            scorer = JudgeInLoopScorer(self._backend, reps=key)
            self._scorers[key] = scorer
        return scorer

    async def _author(
        self, champion_instruction: str, failures: Sequence[TurnVerdict], *, iteration_index: int
    ):
        """Invoke the Author with this island's stance threaded in, streaming its reasoning.

        The island's ``style`` is prepended to the Champion the author sees (then stripped
        back out by :meth:`step` from the result) so the two islands' authors diverge without
        changing the ``AuthorClient`` seam (see the module docstring). Each streamed reasoning
        chunk is forwarded to ``optimizer_author_token`` on this model's Model_Channel so the
        Quality_Tab can render the Challenger being authored live.
        """
        styled_champion = _augment_with_stance(champion_instruction, self._style)

        def _stream(delta: str) -> None:
            self._emitter.author_token(
                model=self._model,
                iteration_index=iteration_index,
                delta=delta,
                island_id=self._island_id,
            )

        return await self._backend.author.author(
            target_model=self._model,
            champion_instruction=styled_champion,
            failures=failures,
            stream=_stream,
        )

    def _snapshot(self) -> IslandState:
        """Build the current :class:`IslandState` (pure; reads only this loop's state)."""
        rung = self._ladder[self._rung_index]
        return IslandState(
            island_id=self._island_id,
            model=self._model,
            style=self._style,
            rung_index=self._rung_index,
            n_rungs=len(self._ladder),
            rung_n_items=rung.n_items,
            rung_n_conversations=rung.n_conversations,
            at_top_rung=self._rung_index >= len(self._ladder) - 1,
            champion_instruction=self._champion_instruction,
            champion_score=self._champion_score,
            champion_ci_half_width=self._champion_ci_half_width,
            prior_rung_score=self._prior_rung_score,
            iterations_at_rung=self._iterations_at_rung,
            total_iterations=self._total_iterations,
            consecutive_non_improving=self._consecutive_non_improving,
            improved_at_rung=self._improved_at_rung,
            stuck=self.is_stuck(),
        )
