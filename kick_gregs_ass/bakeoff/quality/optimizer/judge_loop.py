"""
JudgeInLoopScorer — the synchronous, per-iteration decision metric of the closed-loop
prompt optimizer (design Component 2 "JudgeInLoopScorer"; Req 2, 13, 14).

This module turns the *deferred* Phase-2 quality judge into a *synchronous, per-iteration*
scorer over a slice of multi-turn conversations. For one prompt (a Champion or a
Challenger) it scores every turn of every conversation in the slice with the **real Opus
judge** (the faithfulness/correctness/completeness triad), aggregates to a
per-conversation triad then a slice mean with a 95% confidence interval, and returns a
:class:`SliceScore`. That triad mean — abstention-weighted (Req 14) — is the **sole
promotion-decision metric** (Req 2.1/2.2/2.4); closeness is recorded only as a
non-deciding secondary cross-check (Req 2.3/2.4).

Retrieval-always (Req 13). For **every turn** the scorer calls the injected, held-constant
:class:`bakeoff.quality.optimizer.retrieval.RetrievalBackend` (memoized per
``(turn-query)`` so the Champion and Challenger receive byte-identical fragments for the
same turn, Req 13.3) and threads those **same fragments** into the judge as the
**faithfulness/grounding** evidence (Req 13.5/13.7). Correctness/completeness still use the
gold-derived ideal (turn-1) or ``wants`` (later turns) / the abstention ideal via
:func:`bakeoff.quality.judge._turn_judge_inputs`, exactly as the rest of the study does —
so the only thing retrieval changes is that faithfulness now grounds on the actual
retrieved fragments rather than ``[]``.

Abstention as a first-class, heavily-weighted behavior (Req 14). On a turn whose ground
truth is unanswerable or whose fragments are insufficient, a **correct decline** earns the
full abstention reward and **answering-when-unsure** earns the full penalty, blended into
the per-turn ``overall`` with weight ``w = config.QUALITY_OPT_ABSTENTION_WEIGHT``::

    overall = (1 - w) * triad_mean + w * (1.0 if abstention_correct else 0.0)   # insufficient turn
    overall = triad_mean                                                        # sufficient turn

So, holding all else equal, a correct-abstention answer scores ``>=`` an
answering-when-unsure answer and the gap (``w``) is non-decreasing in the weight (design
Property 27). The judge stays the sole decision metric; the weighting enters the per-turn
aggregation, not a separate metric (Req 14.5).

Backend injection. The scorer takes the optimizer backend as a **duck-typed** object — it
only needs ``answer_adapter_factory``, ``judge_scorer``, ``closeness_scorer`` and
``retrieval`` — so this module never hard-imports ``backends.py`` and works identically
with the offline bundle (zero network) or the live bundle.

Sourcing caveat (carried from requirements.md / design.md): the judge triad as the signal,
the abstention failure modes, and the between-conversation-SD / CI reasoning are grounded
in external/industry RAG-evaluation practice and this repo's own observed Opus verdicts,
**not** in Amazon-internal primary sources; re-validate any judge-derived number against
internal guidance before using it to defend a decision upward.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Optional, Sequence

from bakeoff import config
from bakeoff.quality.dataset import turn_reference
from bakeoff.quality.judge import _turn_judge_inputs
from bakeoff.quality.optimizer import stats
from bakeoff.quality.optimizer.retrieval import RetrievalQuery
from bakeoff.quality.types import GroundTruthKind, TurnOutcome
from bakeoff.scoring.judge import JUDGE_DIMENSIONS
from bakeoff.quality.optimizer.ragas_adapter import RagasSignals

__all__ = [
    "TurnVerdict",
    "SliceScore",
    "JudgeInLoopScorer",
    "REFUSAL",
]

_RAGAS_LOG = logging.getLogger(__name__)
_HB = logging.getLogger("bakeoff.opt.heartbeat")


#: Module-level compiled refusal/abstention detector. A turn whose answer matches this is
#: treated as a decline (the model abstained rather than guessing). Kept deliberately broad
#: but conservative — it keys on explicit "I cannot / I don't have / no information" style
#: phrasings rather than on hedging, so an ordinary grounded answer is not misread as a
#: refusal. Compiled once with IGNORECASE so it is cheap to evaluate per turn.
REFUSAL: re.Pattern[str] = re.compile(
    r"don't have|do not have|outside (my|the)|can't (help|answer|say|tell|advise)|"
    r"not (in|covered)|insufficient|don't have enough|no information",
    re.IGNORECASE,
)


def _mean(values: Sequence[float]) -> float:
    """Arithmetic mean that returns ``0.0`` for an empty sequence (never raises).

    Used everywhere a mean is taken so a degenerate (empty) slice, conversation, or
    dimension list flows through to a well-defined ``0.0`` rather than raising — the same
    "no resolvable signal → 0.0" discipline the CI math in :mod:`stats` uses.
    """
    vals = list(values)
    return (sum(vals) / len(vals)) if vals else 0.0


@dataclass(frozen=True)
class TurnVerdict:
    """The judge's verdict for one turn of one ``(item, rep)`` conversation.

    ``overall`` is the **abstention-weighted** triad for the turn — the per-turn quantity
    the slice triad (and therefore the promotion decision) is built from. ``dimensions``
    carries the raw faithfulness/correctness/completeness means for auditability (Req 2.6).
    The abstention fields (Req 14) record whether the turn was an
    unanswerable/insufficiently-grounded one and whether the model correctly declined;
    ``grounding_fragment_ids`` are the ids of the **same** fragments handed to the judge as
    faithfulness evidence (Req 13.7). ``closeness`` is the secondary cross-check only and is
    never read by any decision (Req 2.3/2.4).
    """

    item_id: str
    rep: int
    turn: int  # 1-based
    ground_truth_kind: str
    overall: float  # abstention-weighted triad (0..1) — the decision metric
    dimensions: dict[str, float]  # faithfulness / correctness / completeness
    abstention_correct: Optional[bool]  # True/False on insufficient turns; None when N/A
    answered_when_unsure: bool  # True iff the model asserted unsupported content (Req 14.4)
    fragments_sufficient: bool  # whether the retrieved fragments supported a grounded answer
    grounding_fragment_ids: tuple[str, ...]  # ids of the SAME fragments the model received
    evidence: dict[str, str]  # judge's quoted span(s), incl. the grounding fragment span
    answer_excerpt: str
    closeness: float  # SECONDARY cross-check only (never decides)
    # --- Tier-1 ragas signals (spec: optimizer-ragas-gepa) — SECONDARY, non-deciding. Each is
    # None when the flag is off or the metric failed (Req 3.5); ragas NEVER enters `overall`
    # or any promotion decision (Req 1.3 / 1.4 / 11). `gold_node_present` is the retrieval
    # diagnostic's gold-node-in-fragments check (None on turns with no gold node id, Req 2.3).
    ragas_faithfulness: Optional[float] = None
    ragas_factual_correctness: Optional[float] = None
    ragas_context_precision: Optional[float] = None
    ragas_context_recall: Optional[float] = None
    gold_node_present: Optional[bool] = None
    ragas_backend: Optional[str] = None  # which adapter produced the signals (Req 5.4)


@dataclass(frozen=True)
class SliceScore:
    """The aggregate score of one prompt on one slice — the unit the loop decides on.

    ``triad_score`` is the abstention-weighted per-conversation mean (the decision metric,
    Req 2.1) with its 95% CI (``ci_half_width`` / ``ci_low`` / ``ci_high``) computed from
    the between-conversation SD over ``n_conversations`` (Req 5.3). ``per_dimension_mean``
    is the auditable triad breakdown (Req 2.6); ``abstention_reward_mean`` and
    ``answered_when_unsure_rate`` summarize abstention behavior across the slice (Req 14.2);
    ``mean_closeness`` is the secondary cross-check (Req 2.3). ``verdicts`` carries every
    per-turn verdict (deterministically ordered) for failure selection downstream.
    """

    model: str
    prompt_role: str  # "champion" | "challenger"
    triad_score: float  # abstention-weighted per-conversation mean (decision metric)
    ci_half_width: float  # 95% CI half-width on this slice
    ci_low: float
    ci_high: float
    n_conversations: int
    between_conv_sd: float
    per_dimension_mean: dict[str, float]  # auditable triad breakdown (Req 2.6)
    abstention_reward_mean: float  # mean abstention-correctness contribution (Req 14.2)
    answered_when_unsure_rate: float  # fraction of turns that over-claimed (Req 14.4)
    mean_closeness: float  # secondary cross-check (Req 2.3)
    verdicts: tuple[TurnVerdict, ...]  # all per-turn verdicts (failure selection)
    # --- Tier-1 ragas slice aggregates (spec: optimizer-ragas-gepa) — SECONDARY, non-deciding;
    # mean over verdicts carrying a non-None value, None when none did (Req 2.4 / 3.4). ---
    ragas_faithfulness_mean: Optional[float] = None
    ragas_factual_correctness_mean: Optional[float] = None
    ragas_context_precision_mean: Optional[float] = None
    ragas_context_recall_mean: Optional[float] = None
    gold_presence_rate: Optional[float] = None  # fraction of gold turns whose gold node was retrieved


class JudgeInLoopScorer:
    """Score a prompt on a slice with the real judge — the loop's decision metric.

    The backend is **duck-typed**: only ``answer_adapter_factory``, ``judge_scorer``,
    ``closeness_scorer`` and ``retrieval`` are required, so this module never hard-imports
    the backend bundle and works identically offline or live. ``reps`` is the number of
    repetitions per item (each ``(item, rep)`` is one conversation), and
    ``abstention_weight`` is the primary-behavior weight ``w`` (Req 14.2).
    """

    def __init__(
        self,
        backend,
        *,
        reps: int,
        abstention_weight: float = config.QUALITY_OPT_ABSTENTION_WEIGHT,
        ragas_cross_check: bool = config.QUALITY_OPT_RAGAS_CROSS_CHECK_ENABLED,
        retrieval_diagnostic: bool = config.QUALITY_OPT_RAGAS_RETRIEVAL_DIAG_ENABLED,
    ) -> None:
        required = ("answer_adapter_factory", "judge_scorer", "closeness_scorer", "retrieval")
        missing = [attr for attr in required if not hasattr(backend, attr)]
        if missing:
            raise AttributeError(
                "JudgeInLoopScorer backend is missing required attribute(s): "
                f"{', '.join(missing)}. Expected a duck-typed OptimizerBackend exposing "
                f"{', '.join(required)}."
            )
        self._backend = backend
        self._reps = int(reps)
        self._abstention_weight = float(abstention_weight)
        # Tier-1 ragas gates (default OFF from config; Req 3.1/3.2). When both are off the
        # scorer never touches the ragas adapter and verdicts are byte-identical to before.
        self._ragas_cross_check = bool(ragas_cross_check)
        self._retrieval_diagnostic = bool(retrieval_diagnostic)

    async def score_prompt(
        self,
        *,
        model: str,
        instruction: str,
        items: Sequence,
        prompt_role: str,
        max_concurrency: Optional[int] = None,
    ) -> SliceScore:
        """Score ``instruction`` on ``items`` (× ``reps``) and return a :class:`SliceScore`.

        Two explicitly-bounded phases so each downstream resource is saturated up to its own
        cap rather than throttled by the other (Req 2 decision metric; AD-3 per-resource
        caps):

        1. **Generate** — every conversation's answers are produced concurrently, bounded by
           the *model* cap (``max_concurrency`` or ``config.CONCURRENCY_CAPS["model"]``).
        2. **Judge** — every turn of every conversation is retrieved + judged concurrently,
           bounded independently by the *judge* cap (``config.CONCURRENCY_CAPS["judge"]``),
           since per-turn verdicts are independent (each needs only its own answer, the
           turn's held-constant retrieved fragments, and the turn reference). Judging the
           Opus triad is the expensive, lowest-cap resource, so filling it across ALL turns
           of ALL conversations at once — instead of one conversation's turns at a time —
           is where the wall-clock win comes from, without raising the instantaneous judge
           peak beyond its own cap.

        The verdicts are then aggregated to an abstention-weighted per-conversation triad,
        then a slice mean + CI — identical to the prior single-phase form, so the returned
        :class:`SliceScore` is unchanged. Retrieval stays held-constant + memoized, so the
        Champion and Challenger still receive byte-identical fragments per ``(turn-query)``
        (Req 13.3). The two per-model loops above this remain the outer parallelism.
        """
        item_lookup = {it.item_id: it for it in items}
        model_cap = (
            max_concurrency if max_concurrency is not None else config.CONCURRENCY_CAPS["model"]
        )
        gen_sem = asyncio.Semaphore(max(1, int(model_cap)))
        judge_sem = asyncio.Semaphore(max(1, int(config.CONCURRENCY_CAPS["judge"])))

        # -- Phase 1: generate every conversation's answers (model-capped). --
        async def generate(item, rep: int):
            async with gen_sem:
                answers = await self._generate_conversation(
                    model=model, instruction=instruction, item=item, item_lookup=item_lookup
                )
            return (item, rep, answers)

        gen_tasks = [generate(item, rep) for item in items for rep in range(self._reps)]
        _t_gen = time.monotonic()
        _HB.info("score_prompt[%s/%s]: PHASE1 generate START (%d conversations, model_cap=%s)",
                 model, prompt_role, len(gen_tasks), model_cap)
        generated = list(await asyncio.gather(*gen_tasks)) if gen_tasks else []
        _HB.info("score_prompt[%s/%s]: PHASE1 generate DONE in %.1fs",
                 model, prompt_role, time.monotonic() - _t_gen)

        # -- Phase 2: judge every turn of every conversation (judge-capped). --
        # Each conversation keeps an ordered slot list so its turns reassemble in order
        # regardless of which judge calls finish first.
        conversation_verdicts: list[list[Optional[TurnVerdict]]] = [
            [None] * len(answers) for (_, _, answers) in generated
        ]

        async def judge(conv_index: int, turn_index: int, item, rep: int, ans: str) -> None:
            async with judge_sem:
                verdict = await self._judge_turn(
                    model=model, item=item, rep=rep, turn_index=turn_index, ans=ans
                )
            conversation_verdicts[conv_index][turn_index] = verdict

        judge_tasks = [
            judge(ci, ti, item, rep, ans)
            for ci, (item, rep, answers) in enumerate(generated)
            for ti, ans in enumerate(answers)
        ]
        _t_judge = time.monotonic()
        _HB.info("score_prompt[%s/%s]: PHASE2 judge START (%d turns, judge_cap=%s)",
                 model, prompt_role, len(judge_tasks), config.CONCURRENCY_CAPS["judge"])
        if judge_tasks:
            await asyncio.gather(*judge_tasks)
        _HB.info("score_prompt[%s/%s]: PHASE2 judge DONE in %.1fs",
                 model, prompt_role, time.monotonic() - _t_judge)

        # Drop the now-filled Optional sentinels (every slot is written exactly once).
        finalized = [[v for v in conv if v is not None] for conv in conversation_verdicts]
        return self._aggregate(
            model=model, prompt_role=prompt_role, conversation_verdicts=finalized
        )

    async def score_in_loop(
        self,
        *,
        model: str,
        instruction: str,
        items: Sequence,
        prompt_role: str,
        max_concurrency: Optional[int] = None,
    ) -> SliceScore:
        """Score ``instruction`` using ONLY the In_Loop_Signal — never the Judge (Req 1.2).

        The cheap per-iteration signal that drives the Author's within-Round self-iteration
        (corrected loop cadence, Req 1.1/1.2). It mirrors :meth:`score_prompt`'s generate +
        held-constant retrieval + closeness path exactly, but derives each turn's ``overall``
        from the **closeness composite** blended with the same abstention-reward term used on
        the judge path, and **does not call** ``self._backend.judge_scorer.score_detailed`` at
        all — so a Round's in-loop iterations make zero Judge invocations and the Opus Judge is
        out of the per-iteration hot loop.

        Returns a :class:`SliceScore` of the same shape as :meth:`score_prompt`, so
        :func:`~bakeoff.quality.optimizer.failures.select_failures`,
        :class:`~bakeoff.quality.optimizer.convergence.PromotionDecider`, and the aggregation
        are reused unchanged. Retrieval stays held-constant + memoized (Req 4.3): this path
        calls the same injected ``retrieval`` backend as the judge path, so the Champion and a
        Challenger receive byte-identical fragments per ``(turn-query)``. The duck-typed
        backend contract is unchanged — this path reads only ``answer_adapter_factory``,
        ``closeness_scorer`` and ``retrieval`` (the ``judge_scorer`` attribute is never
        touched here).
        """
        model_cap = (
            max_concurrency if max_concurrency is not None else config.CONCURRENCY_CAPS["model"]
        )
        gen_sem = asyncio.Semaphore(max(1, int(model_cap)))
        # No judge on this path; bound the retrieve+closeness scoring phase conservatively by
        # the same cap the judge phase would use so the in-loop path never bursts retrieval
        # harder than the judge path it stands in for.
        score_sem = asyncio.Semaphore(max(1, int(config.CONCURRENCY_CAPS["judge"])))

        async def generate(item, rep: int):
            async with gen_sem:
                answers = await self._generate_conversation(
                    model=model, instruction=instruction, item=item, item_lookup={}
                )
            return (item, rep, answers)

        gen_tasks = [generate(item, rep) for item in items for rep in range(self._reps)]
        generated = list(await asyncio.gather(*gen_tasks)) if gen_tasks else []

        conversation_verdicts: list[list[Optional[TurnVerdict]]] = [
            [None] * len(answers) for (_, _, answers) in generated
        ]

        async def score(conv_index: int, turn_index: int, item, rep: int, ans: str) -> None:
            async with score_sem:
                verdict = await self._score_turn_in_loop(
                    model=model, item=item, rep=rep, turn_index=turn_index, ans=ans
                )
            conversation_verdicts[conv_index][turn_index] = verdict

        score_tasks = [
            score(ci, ti, item, rep, ans)
            for ci, (item, rep, answers) in enumerate(generated)
            for ti, ans in enumerate(answers)
        ]
        if score_tasks:
            await asyncio.gather(*score_tasks)

        finalized = [[v for v in conv if v is not None] for conv in conversation_verdicts]
        return self._aggregate(
            model=model, prompt_role=prompt_role, conversation_verdicts=finalized
        )

    async def _generate_conversation(
        self, *, model: str, instruction: str, item, item_lookup: dict
    ) -> list:
        """Generate one conversation's per-turn answers under ``instruction`` (no judging).

        The answer adapter is built per conversation with ``instruction`` as the override
        (the only varied element between Champion and Challenger). The model is fed, PER TURN,
        the SAME held-constant, memoized retrieved fragments the Judge will later grade that
        turn against (:meth:`_conversation_fragments` returns a ``{turn_index: fragments}``
        map), rendered into the model-visible question by the live inline adapter's
        ``<context>`` block — so the model grounds turn N on exactly the evidence the Judge
        credits for turn N. (Previously this passed ``[]``: the model saw NO retrieved policy,
        so a grounding-mandate prompt was forced to decline or guess and faithfulness
        collapsed — observed live. The offline adapter ignores the ``fragments`` argument, so
        offline behavior is unchanged.) Retrieval is memoized per ``(item, turn, query)``, so
        feeding the model here incurs no extra retrieval cost when the Judge retrieves the same
        keys. Returns the ordered list of answers.
        """
        adapter = self._backend.answer_adapter_factory(model, instruction, item_lookup)
        fragments_by_turn = await self._conversation_fragments(item)
        _t = time.monotonic()
        resp = await adapter.generate(item, fragments_by_turn, config.DEFAULT_TEMPERATURE)
        _dt = time.monotonic() - _t
        if _dt > 20.0:
            _HB.warning("SLOW model.generate: model=%s item=%s took %.1fs",
                        model, getattr(item, "item_id", "?"), _dt)
        return list(resp.per_turn_answers or [resp.text])

    async def _conversation_fragments(self, item) -> dict:
        """Per-turn retrieved fragments for the conversation, keyed by turn index.

        Retrieves each turn's fragments via the SAME injected, memoized
        :class:`RetrievalBackend` the Judge uses per turn (keyed by ``(item, turn, query)``)
        and returns a ``{turn_index: fragments}`` map. The live inline adapter then grounds
        turn N on EXACTLY turn N's fragments — byte-identical to what the Judge later grades
        turn N against — so the model and the Judge see the same evidence per turn. This is
        deliberately NOT a whole-conversation union: a union would let the model ground a turn
        on an off-turn fragment the Judge won't credit for that turn (tripping the
        faithfulness gate) and would leak later-turn policy into earlier turns. Because
        retrieval is held-constant + memoized, the Judge's later per-turn ``retrieve`` calls
        hit the cache, so feeding the model here incurs no extra retrieval.

        A turn whose retrieval FAILS is logged (never swallowed silently) and omitted from the
        map, so that turn degrades to no context rather than failing generation — but the
        failure is surfaced, so a retrieval outage can never silently re-create the
        fragment-starvation bug this method exists to fix.
        """
        n_turns = len(item.turns) if (getattr(item, "is_multi_turn", False) and item.turns) else 1
        per_turn: dict[int, list] = {}
        for ti in range(n_turns):
            turn = item.turns[ti] if ti < len(item.turns) else None
            if turn is not None and getattr(turn, "user_utterance", None):
                query_text = turn.user_utterance
            else:
                query_text = item.query or ""
            try:
                frags = await self._backend.retrieval.retrieve(
                    RetrievalQuery(item_id=item.item_id, turn=ti + 1, query=query_text)
                )
            except Exception:  # noqa: BLE001 - grounding retrieval must never break generation
                _HB.warning(
                    "grounding retrieval FAILED for item=%s turn=%d — model gets NO context "
                    "for this turn (degraded grounding, not silent starvation)",
                    getattr(item, "item_id", "?"), ti + 1, exc_info=True,
                )
                continue
            per_turn[ti] = list(frags)
        return per_turn

    async def _score_conversation(
        self, *, model: str, instruction: str, item, rep: int, item_lookup: dict
    ) -> list:
        """Generate one conversation's answers, then judge each turn → ``list[TurnVerdict]``.

        Retained as the single-conversation path (generate then judge its turns
        sequentially) for callers/tests that score one conversation in isolation;
        :meth:`score_prompt` no longer routes through it (it runs the two phases across all
        conversations for resource saturation), but the behavior here is identical for a
        single conversation.
        """
        answers = await self._generate_conversation(
            model=model, instruction=instruction, item=item, item_lookup=item_lookup
        )
        verdicts: list[TurnVerdict] = []
        for ti, ans in enumerate(answers):
            verdicts.append(
                await self._judge_turn(model=model, item=item, rep=rep, turn_index=ti, ans=ans)
            )
        return verdicts

    async def _ragas_signals(
        self,
        *,
        answer_text: str,
        fragments,
        reference_texts,
        gold_node_ids,
        ground_truth_kind: str,
        question: str,
    ) -> RagasSignals:
        """Compute the Tier-1 ragas signals for one turn — SECONDARY, non-deciding (Req 1/2).

        Returns an all-``None`` :class:`RagasSignals` when both flags are off or no adapter is
        present on the backend, so config-off behavior is byte-identical to pre-feature
        (Req 3.3 / 17). Each metric group is independently failure-tolerant: any exception
        (including ``ragas`` not being installed on the live path) is logged at WARNING with
        ``exc_info`` and recorded as ``None`` for that signal, and the iteration continues on
        the Judge triad (Req 3.5). These signals NEVER enter ``overall`` or any promotion
        decision (Req 1.3 / 1.4 / 11). The diagnostic reads the SAME fragments the Judge saw
        (Req 2.2 / 13.3); gold-node presence is only meaningful on a gold turn (Req 2.3).
        """
        adapter = getattr(self._backend, "ragas_adapter", None)
        if adapter is None or not (self._ragas_cross_check or self._retrieval_diagnostic):
            return RagasSignals()
        faithfulness = factual = precision = recall = None
        gold_present = None
        if self._ragas_cross_check:
            try:
                faithfulness, factual = await adapter.cross_check(
                    answer_text=answer_text,
                    fragments=fragments,
                    reference_texts=reference_texts,
                    question=question,
                )
            except Exception:  # noqa: BLE001 — failure-tolerant (Req 3.5)
                _RAGAS_LOG.warning("ragas cross_check failed for a turn; recording None", exc_info=True)
        if self._retrieval_diagnostic:
            # gold-presence only applies to a turn that carries a gold node id (Req 2.3);
            # later wants-only turns pass [] so the adapter returns None.
            gold_ids = list(gold_node_ids) if ground_truth_kind == GroundTruthKind.GOLD else []
            try:
                precision, recall, gold_present = await adapter.retrieval_diagnostic(
                    fragments=fragments,
                    reference_texts=reference_texts,
                    gold_node_ids=gold_ids,
                )
            except Exception:  # noqa: BLE001 — failure-tolerant (Req 3.5)
                _RAGAS_LOG.warning("ragas retrieval_diagnostic failed for a turn; recording None", exc_info=True)
        return RagasSignals(
            faithfulness=faithfulness,
            factual_correctness=factual,
            context_precision=precision,
            context_recall=recall,
            gold_node_present=gold_present,
            backend=getattr(adapter, "name", None),
        )

    async def _judge_turn(self, *, model: str, item, rep: int, turn_index: int, ans: str) -> TurnVerdict:
        """Retrieve → close → judge one turn and build its abstention-weighted verdict."""
        ti = turn_index
        turn = item.turns[ti] if ti < len(item.turns) else None

        # 1) Held-constant, per-turn retrieval (memoized so champion/challenger match).
        if turn is not None and getattr(turn, "user_utterance", None):
            query_text = turn.user_utterance
        else:
            query_text = item.query or ""
        _t_r = time.monotonic()
        frags = await self._backend.retrieval.retrieve(
            RetrievalQuery(item_id=item.item_id, turn=ti + 1, query=query_text)
        )
        _dt_r = time.monotonic() - _t_r
        if _dt_r > 20.0:
            _HB.warning("SLOW retrieval.retrieve: item=%s turn=%d took %.1fs",
                        item.item_id, ti + 1, _dt_r)
        grounding_fragment_ids = tuple(str(f.get("id", "")) for f in frags)

        # 2) Closeness (secondary) + judge inputs (correctness/completeness ideal).
        kind, reference_text = turn_reference(item, ti)
        answerability = turn.answerability if turn else None
        # closeness uses the real Bedrock Embed v4 client — a blocking network call. Run it
        # off the event loop (same reason as retrieval above) so PHASE-2 doesn't freeze the
        # server; without this, fixing retrieval alone would just move the freeze here.
        _t_c = time.monotonic()
        closeness = await asyncio.to_thread(
            self._backend.closeness_scorer.score_turn,
            answer_text=ans,
            reference_text=reference_text,
            ground_truth_kind=kind,
            answerability=answerability,
        )
        _dt_c = time.monotonic() - _t_c
        if _dt_c > 20.0:
            _HB.warning("SLOW closeness.score_turn (embed): item=%s turn=%d took %.1fs",
                        item.item_id, ti + 1, _dt_c)
        turn_outcome = TurnOutcome(
            turn=ti + 1,
            answerability=answerability,
            response_dependent=bool(getattr(turn, "response_dependent", False)),
            answer_text=ans,
            reference_text=reference_text,
            closeness=closeness,
        )
        ideal, gold_texts, judge_answerability = _turn_judge_inputs(item, ti, turn_outcome)
        momentary_state = (
            item.turns[ti].momentary_state if ti < len(item.turns) else "neutral"
        )

        # The judge is CPU/IO-bound and synchronous; run it off the event loop. The SAME
        # retrieved fragments the model received are passed as the grounding evidence
        # (Req 13.7) — faithfulness grounds on these, not on `[]`. The focal query
        # (`query_text`) is passed as `question=` so the in-loop judge prompt renders
        # the question (the Phase-2 path passes it too; omitting it here depressed
        # correctness/completeness — see docs/QUALITY_SIGNAL_DIAGNOSIS.md).
        _t_j = time.monotonic()
        scores, evidence = await asyncio.to_thread(
            self._backend.judge_scorer.score_detailed,
            ans,
            ideal_text=ideal,
            fragments=frags,
            gold_texts=gold_texts,
            momentary_state=momentary_state,
            answerability=judge_answerability,
            question=query_text,
        )
        _dt_j = time.monotonic() - _t_j
        if _dt_j > 20.0:
            _HB.warning("SLOW judge.score_detailed (Opus): item=%s turn=%d took %.1fs",
                        item.item_id, ti + 1, _dt_j)
        dims = {d: float(getattr(scores, d)) for d in JUDGE_DIMENSIONS}
        triad_mean = _mean(list(dims.values()))

        # 3) Abstention weighting (Req 14). `fragments_sufficient` is recorded for audit;
        # the `overall` branch keys on whether this is an insufficient/unanswerable turn.
        w = self._abstention_weight
        fragments_sufficient = (kind == GroundTruthKind.GOLD) or (
            answerability in {"full", "partial"}
        )
        insufficient = (kind == GroundTruthKind.ABSTENTION) or (answerability == "none")
        if insufficient:
            refused = bool(REFUSAL.search(ans or ""))
            abstention_correct: Optional[bool] = refused
            answered_when_unsure = not refused
            overall = (1.0 - w) * triad_mean + w * (1.0 if abstention_correct else 0.0)
        else:
            abstention_correct = None
            answered_when_unsure = False
            overall = triad_mean

        # 3b) CONFIDENT-WRONG HAMMER (owner priority): a wrong answer delivered with
        # false certainty is the most costly failure on this task. `faithfulness` is the
        # judge's "every claim is supported by the fragments" score (now graded against an
        # answerability-aware prompt that frames confident-wrong as the worst outcome). When
        # it falls below the floor, the model asserted unsupported content — cap the turn's
        # overall at the faithfulness value itself so a fluent fabrication cannot be averaged
        # back up by completeness/correctness. A correct grounded decline asserts nothing, so
        # its faithfulness is high and this gate never touches it. See
        # config.QUALITY_OPT_FAITHFULNESS_FLOOR.
        faithfulness = dims.get("faithfulness", triad_mean)
        if faithfulness < config.QUALITY_OPT_FAITHFULNESS_FLOOR:
            overall = min(overall, faithfulness)

        # 4) Tier-1 ragas signals (spec: optimizer-ragas-gepa) — computed AFTER `overall` and
        # never folded into it. SECONDARY cross-check + retrieval diagnostic only (Req 1/2);
        # gated + failure-tolerant inside the helper (Req 3.5). `overall` above is final.
        ragas = await self._ragas_signals(
            answer_text=ans,
            fragments=frags,
            reference_texts=gold_texts,
            gold_node_ids=getattr(item, "gold_node_ids", None) or [],
            ground_truth_kind=kind,
            question=query_text,
        )

        return TurnVerdict(
            item_id=item.item_id,
            rep=rep,
            turn=ti + 1,
            ground_truth_kind=kind,
            overall=float(overall),
            dimensions=dims,
            abstention_correct=abstention_correct,
            answered_when_unsure=answered_when_unsure,
            fragments_sufficient=fragments_sufficient,
            grounding_fragment_ids=grounding_fragment_ids,
            evidence=dict(evidence or {}),
            answer_excerpt=(ans or "")[:600],
            closeness=float(closeness.composite),
            ragas_faithfulness=ragas.faithfulness,
            ragas_factual_correctness=ragas.factual_correctness,
            ragas_context_precision=ragas.context_precision,
            ragas_context_recall=ragas.context_recall,
            gold_node_present=ragas.gold_node_present,
            ragas_backend=ragas.backend,
        )

    async def _score_turn_in_loop(
        self, *, model: str, item, rep: int, turn_index: int, ans: str
    ) -> TurnVerdict:
        """Build one turn's verdict from the In_Loop_Signal — closeness + abstention, NO judge.

        The Judge-free sibling of :meth:`_judge_turn` used by :meth:`score_in_loop`. It runs
        the identical held-constant per-turn retrieval and closeness path, but instead of
        calling the Opus triad it stands the **closeness composite** in for the triad mean and
        blends it with the same abstention-reward branch (the ``REFUSAL`` match on an
        insufficient/unanswerable turn) that :meth:`_judge_turn` uses, so ``overall``,
        ``answered_when_unsure`` and the per-turn ordering remain meaningful for
        :func:`select_failures`. ``dimensions`` carries the closeness composite under each
        triad key so :meth:`_aggregate` produces a well-defined ``per_dimension_mean`` without
        any judge call; ``evidence`` is empty because no judge produced a quote.
        """
        ti = turn_index
        turn = item.turns[ti] if ti < len(item.turns) else None

        # 1) Held-constant, per-turn retrieval (memoized so champion/challenger match, Req 4.3).
        if turn is not None and getattr(turn, "user_utterance", None):
            query_text = turn.user_utterance
        else:
            query_text = item.query or ""
        frags = await self._backend.retrieval.retrieve(
            RetrievalQuery(item_id=item.item_id, turn=ti + 1, query=query_text)
        )
        grounding_fragment_ids = tuple(str(f.get("id", "")) for f in frags)

        # 2) Closeness (the in-loop proxy) — NO judge call on this path (Req 1.2).
        kind, reference_text = turn_reference(item, ti)
        answerability = turn.answerability if turn else None
        closeness = self._backend.closeness_scorer.score_turn(
            answer_text=ans,
            reference_text=reference_text,
            ground_truth_kind=kind,
            answerability=answerability,
        )
        composite = float(closeness.composite)
        # The closeness composite stands in for the triad mean; record it under each triad
        # key so per_dimension_mean is well-defined without a judge verdict.
        dims = {d: composite for d in JUDGE_DIMENSIONS}

        # 3) Abstention weighting (Req 14) — identical branch to the judge path, with the
        # closeness composite standing in for the triad mean.
        w = self._abstention_weight
        fragments_sufficient = (kind == GroundTruthKind.GOLD) or (
            answerability in {"full", "partial"}
        )
        insufficient = (kind == GroundTruthKind.ABSTENTION) or (answerability == "none")
        if insufficient:
            refused = bool(REFUSAL.search(ans or ""))
            abstention_correct: Optional[bool] = refused
            answered_when_unsure = not refused
            overall = (1.0 - w) * composite + w * (1.0 if abstention_correct else 0.0)
        else:
            abstention_correct = None
            answered_when_unsure = False
            overall = composite

        return TurnVerdict(
            item_id=item.item_id,
            rep=rep,
            turn=ti + 1,
            ground_truth_kind=kind,
            overall=float(overall),
            dimensions=dims,
            abstention_correct=abstention_correct,
            answered_when_unsure=answered_when_unsure,
            fragments_sufficient=fragments_sufficient,
            grounding_fragment_ids=grounding_fragment_ids,
            evidence={},
            answer_excerpt=(ans or "")[:600],
            closeness=composite,
        )

    def _aggregate(
        self, *, model: str, prompt_role: str, conversation_verdicts: Sequence[Sequence[TurnVerdict]]
    ) -> SliceScore:
        """Roll per-turn verdicts up to a per-conversation triad then a slice mean + CI.

        ``conv_triad = mean over its turns of TurnVerdict.overall``; ``triad_score = mean
        over conversations``; the CI half-width comes from the between-conversation SD over
        ``n_conversations`` (an infinite/NaN half-width — a degenerate empty slice — is
        recorded as ``0.0``). The per-dimension means, abstention summaries, and mean
        closeness are taken over every verdict; the verdicts are returned deterministically
        ordered by ``(item_id, rep, turn)``.
        """
        all_verdicts = [v for conv in conversation_verdicts for v in conv]
        conv_means = [_mean([v.overall for v in conv]) for conv in conversation_verdicts if conv]

        n_conversations = len(conv_means)
        triad_score = _mean(conv_means)
        between_conv_sd = stats.between_conversation_sd(conv_means)
        hw = stats.ci_half_width(between_conv_sd, n_conversations)
        if not math.isfinite(hw):
            hw = 0.0
        ci_low = triad_score - hw
        ci_high = triad_score + hw

        per_dimension_mean = {
            d: _mean([v.dimensions.get(d, 0.0) for v in all_verdicts]) for d in JUDGE_DIMENSIONS
        }
        abstention_reward_mean = _mean(
            [(1.0 if v.abstention_correct else 0.0) for v in all_verdicts if v.abstention_correct is not None]
        )
        answered_when_unsure_rate = (
            sum(1 for v in all_verdicts if v.answered_when_unsure) / len(all_verdicts)
            if all_verdicts
            else 0.0
        )
        mean_closeness = _mean([v.closeness for v in all_verdicts])
        verdicts = tuple(sorted(all_verdicts, key=lambda v: (v.item_id, v.rep, v.turn)))

        # Tier-1 ragas slice aggregates (SECONDARY, non-deciding): mean over verdicts that
        # carry a non-None value (None when none did); gold-presence rate over gold turns only
        # (Req 2.4 / 3.4). None of these feed the decision metric.
        def _opt_mean(values):
            vals = [x for x in values if x is not None]
            return (sum(vals) / len(vals)) if vals else None

        _gold_flags = [v.gold_node_present for v in all_verdicts if v.gold_node_present is not None]
        gold_presence_rate = (
            (sum(1 for g in _gold_flags if g) / len(_gold_flags)) if _gold_flags else None
        )

        return SliceScore(
            model=model,
            prompt_role=prompt_role,
            triad_score=triad_score,
            ci_half_width=hw,
            ci_low=ci_low,
            ci_high=ci_high,
            n_conversations=n_conversations,
            between_conv_sd=between_conv_sd,
            per_dimension_mean=per_dimension_mean,
            abstention_reward_mean=abstention_reward_mean,
            answered_when_unsure_rate=answered_when_unsure_rate,
            mean_closeness=mean_closeness,
            verdicts=verdicts,
            ragas_faithfulness_mean=_opt_mean([v.ragas_faithfulness for v in all_verdicts]),
            ragas_factual_correctness_mean=_opt_mean([v.ragas_factual_correctness for v in all_verdicts]),
            ragas_context_precision_mean=_opt_mean([v.ragas_context_precision for v in all_verdicts]),
            ragas_context_recall_mean=_opt_mean([v.ragas_context_recall for v in all_verdicts]),
            gold_presence_rate=gold_presence_rate,
        )
