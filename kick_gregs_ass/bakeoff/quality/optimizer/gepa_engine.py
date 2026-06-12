"""
GEPA_Engine seam — Tier-2 reflective prompt evolution (spec: optimizer-ragas-gepa; design
Components C6-C8; Req 6, 7, 8, 9, 12).

When ``QUALITY_OPT_TIER2_GEPA_ENABLED`` is on, the orchestrator runs a candidate prompt
search through a :class:`GepaEngine` instead of the hand-rolled island/tournament machinery
(Req 6). The engine's metric is the **existing Opus judge triad** wrapped as
:class:`JudgeBackedGepaMetric` — it returns a scalar score (the abstention-weighted
``SliceScore.triad_score``, Req 7.2) **and** a natural-language ``feedback_text`` derived from
the Judge's per-turn evidence (Req 7.1 / 7.5), with the ragas signals exposed as **named
JudgeDimensions** (Req 8.1 / 8.2). The Judge triad stays the sole promotion decision (Req 7.4 /
11). The rollout budget is configured from the coverage-ladder cadence (Req 9).

This module mirrors :mod:`bakeoff.quality.optimizer.ragas_adapter` / ``retrieval`` discipline:
a Protocol + a deterministic, network-free fake + a live adapter that lazily imports the
external engine + a selector. Importing this module — and the whole offline suite — works
whether or not the ``gepa`` package is installed: :class:`FakeGepaEngine` carries the offline
tests, and :class:`LiveGepaEngine` imports ``gepa`` only inside :meth:`optimize`.

Sourcing caveat (Req 18): GEPA (``gepa-ai/gepa``) is an EXTERNAL open-source framework, not
Amazon-internal guidance. The live ``gepa.optimize`` wiring targets the installed gepa 0.0.27
public API (``optimize`` + ``GEPAAdapter`` + ``EvaluationBatch``), but a live GEPA run needs a
real reflection LM and the Opus judge, so the live path's exact behavior is an assumption to
confirm at run time; the offline fake is what is verified here. Any judge-derived number must
be re-validated before it is used to defend a decision upward.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol, Sequence, runtime_checkable

from bakeoff import config

__all__ = [
    "MetricResult",
    "GepaResult",
    "GepaMetric",
    "JudgeBackedGepaMetric",
    "Proposer",
    "GepaEngine",
    "FakeGepaEngine",
    "LiveGepaEngine",
    "rollout_budget_from_ladder",
    "make_bedrock_reflection_lm",
    "build_gepa_engine",
]

_LOG = logging.getLogger(__name__)

#: The single GEPA-optimized component name (the system instruction under test). GEPA
#: candidates are ``dict[str, str]`` component->text; the harness optimizes exactly one.
GEPA_COMPONENT = "system_instruction"


@dataclass(frozen=True)
class MetricResult:
    """One candidate's evaluation: the deciding scalar + the reflective feedback + named dims.

    ``score`` is the abstention-weighted Judge triad (``SliceScore.triad_score``) — the SOLE
    promotion signal (Req 7.2 / 7.4). ``feedback_text`` is the natural-language critique the
    reflective proposer reads (Req 7.1 / 7.5). ``per_dimension`` carries the
    faithfulness/correctness/completeness triad PLUS the named ragas dimensions (Req 8.1 / 8.2),
    all feeding the single ``score`` rather than competing as independent deciders (Req 8.4).
    """

    score: float
    feedback_text: str
    per_dimension: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class GepaResult:
    """The outcome of one GEPA optimization run for one Target_Model.

    ``best_instruction`` is the winning candidate the orchestrator hands to Phase B;
    ``best_score`` is its triad; ``per_dimension`` its named-dimension breakdown; ``history``
    is the ordered ``(instruction, score)`` trail of every evaluated candidate.
    """

    best_instruction: str
    best_score: float
    per_dimension: dict[str, float] = field(default_factory=dict)
    history: tuple[tuple[str, float], ...] = ()


@runtime_checkable
class GepaMetric(Protocol):
    """The metric a :class:`GepaEngine` optimizes — judge-as-metric (Req 7)."""

    async def evaluate(self, instruction: str, items: Optional[Sequence[Any]] = None) -> MetricResult:
        """Score ``instruction`` (on ``items`` or the metric's bound batch) → :class:`MetricResult`."""
        ...


#: A reflective proposer: given the current instruction and the metric's feedback text, return
#: a rewritten instruction. The offline fake uses this; the live engine uses gepa's own
#: reflective proposer driven by a reflection LM instead.
Proposer = Callable[[str, str], Awaitable[str]]


class JudgeBackedGepaMetric:
    """Present the existing Opus Judge to GEPA as its metric (Req 7, 8, 10, 12).

    Scores a candidate instruction on a rung's items via the existing
    :class:`~bakeoff.quality.optimizer.judge_loop.JudgeInLoopScorer` (the SAME Judge
    implementation the rest of the study uses, Req 7.3), then returns the abstention-weighted
    triad as the scalar (Req 7.2), a feedback critique assembled from the worst verdicts'
    judge evidence + abstention/ragas signals (Req 7.1 / 7.5), and the per-dimension breakdown
    extended with the configured named ragas dimensions (Req 8.1 / 8.2). The Judge triad
    remains the sole decision signal (Req 7.4 / 11).
    """

    def __init__(
        self,
        *,
        scorer: Any,  # JudgeInLoopScorer (duck-typed to avoid an import cycle)
        model: str,
        items: Sequence[Any],
        prompt_role: str = "champion",
        named_ragas_dimensions: Sequence[str] = config.QUALITY_OPT_GEPA_NAMED_RAGAS_DIMENSIONS,
        max_feedback_turns: int = 5,
    ) -> None:
        self._scorer = scorer
        self._model = model
        self._items = list(items)
        self._prompt_role = prompt_role
        self._named_ragas_dimensions = tuple(named_ragas_dimensions)
        self._max_feedback_turns = int(max_feedback_turns)

    async def evaluate(self, instruction: str, items: Optional[Sequence[Any]] = None) -> MetricResult:
        slice_score = await self._scorer.score_prompt(
            model=self._model,
            instruction=instruction,
            items=list(items) if items is not None else self._items,
            prompt_role=self._prompt_role,
        )
        score = float(slice_score.triad_score)  # abstention-weighted, sole decider (Req 7.2/7.4)
        per_dim: dict[str, float] = {k: float(v) for k, v in slice_score.per_dimension_mean.items()}
        # Promote the ragas slice-means to named JudgeDimensions (Req 8.1/8.2); they inform the
        # dashboard/attribution but feed the single triad decision, not independent deciders.
        ragas_map = {
            "ragas_faithfulness": slice_score.ragas_faithfulness_mean,
            "ragas_factual_correctness": slice_score.ragas_factual_correctness_mean,
            "ragas_context_precision": slice_score.ragas_context_precision_mean,
            "ragas_context_recall": slice_score.ragas_context_recall_mean,
        }
        for name in self._named_ragas_dimensions:
            value = ragas_map.get(name)
            if value is not None:
                per_dim[name] = float(value)
        return MetricResult(
            score=score,
            feedback_text=self._feedback_from(slice_score),
            per_dimension=per_dim,
        )

    def _feedback_from(self, slice_score: Any) -> str:
        """Assemble the reflective feedback the proposer reads (Req 7.1 / 7.5).

        Surfaces the lowest-scoring judged turns — answering-when-unsure first — with their
        per-dimension scores, the judge's quoted evidence, and the ragas/abstention signals,
        so the reflective proposer is conditioned on WHY a turn scored as it did rather than on
        a bare float.
        """
        verdicts = list(getattr(slice_score, "verdicts", ()) or ())
        if not verdicts:
            return "No per-turn verdicts were produced for this candidate."
        # answered-when-unsure first, then lowest overall.
        verdicts.sort(key=lambda v: (not v.answered_when_unsure, v.overall))
        lines = [
            f"Candidate triad={slice_score.triad_score:.3f} "
            f"(abstention_reward_mean={slice_score.abstention_reward_mean:.2f}, "
            f"answered_when_unsure_rate={slice_score.answered_when_unsure_rate:.2f}). "
            "Lowest-scoring turns (address these without changing retrieval or fragment assembly):"
        ]
        for v in verdicts[: self._max_feedback_turns]:
            dims = ", ".join(f"{k}={v.dimensions.get(k, 0.0):.2f}" for k in sorted(v.dimensions))
            evidence = "; ".join(f"{k}: {val}" for k, val in (v.evidence or {}).items()) or "(none)"
            ragas_bits = []
            if v.ragas_faithfulness is not None:
                ragas_bits.append(f"ragas_faithfulness={v.ragas_faithfulness:.2f}")
            if v.gold_node_present is not None:
                ragas_bits.append(f"gold_node_present={v.gold_node_present}")
            ragas_str = ("  " + ", ".join(ragas_bits)) if ragas_bits else ""
            lines.append(
                f"- item={v.item_id} turn={v.turn} overall={v.overall:.2f} "
                f"answered_when_unsure={v.answered_when_unsure} [{dims}]{ragas_str}\n"
                f"    evidence: {evidence}"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Rollout budget from the coverage ladder (Req 9 / design C8)
# ---------------------------------------------------------------------------
def rollout_budget_from_ladder(
    ladder: Sequence[Any],
    configured: int = config.QUALITY_OPT_GEPA_ROLLOUT_BUDGET,
) -> int:
    """Derive the GEPA rollout (metric-call) budget from the coverage-ladder cadence (Req 9).

    A configured positive ``QUALITY_OPT_GEPA_ROLLOUT_BUDGET`` is used verbatim; otherwise the
    budget is the sum of conversations across the ladder's rungs (cheap-early/broad-late,
    mirroring the escalating-coverage idea), with a floor of 1. The numbers are flagged as
    assumptions to confirm (Req 9.3).
    """
    if configured and int(configured) > 0:
        return int(configured)
    total = sum(int(getattr(rung, "n_conversations", 0) or 0) for rung in ladder)
    return max(1, total)


@runtime_checkable
class GepaEngine(Protocol):
    """Reflective-evolution engine the orchestrator drives in Tier 2 (Req 6)."""

    name: str

    async def optimize(self, *, seed_instruction: str, metric: GepaMetric, budget: int) -> GepaResult:
        """Evolve ``seed_instruction`` against ``metric`` within ``budget`` metric calls."""
        ...


class FakeGepaEngine:
    """Deterministic, network-free GEPA-style engine for offline runs/tests (Req 6.1-6.4).

    Implements the reflective-proposer + Pareto-retention + merge loop the standalone gepa
    engine provides, but resolved deterministically against the injected ``metric`` and
    ``proposer`` (no ``gepa`` import, no network), so the offline suite exercises the full
    Tier-2 wiring. Each candidate evaluation consumes one unit of ``budget`` (metric calls);
    the loop reflective-mutates the current best, retains every evaluated candidate (a simple
    Pareto-by-aggregate frontier), and, when mutation stalls, merges the top two frontier
    members (bounded by ``merge_max``) before stopping.
    """

    name = "fake"

    def __init__(
        self,
        *,
        proposer: Proposer,
        merge_max: int = config.QUALITY_OPT_GEPA_MAX_MERGE_INVOCATIONS,
    ) -> None:
        self._proposer = proposer
        self._merge_max = int(merge_max)

    @staticmethod
    def _merge(a: str, b: str) -> str:
        """Deterministically combine two candidates (gepa's merge analogue)."""
        if a == b:
            return a
        return f"{a}\n\n{b}".strip()

    async def optimize(self, *, seed_instruction: str, metric: GepaMetric, budget: int) -> GepaResult:
        calls = 0
        budget = max(1, int(budget))

        async def _eval(instr: str) -> MetricResult:
            nonlocal calls
            calls += 1
            return await metric.evaluate(instr)

        seed_res = await _eval(seed_instruction)
        frontier: list[tuple[str, MetricResult]] = [(seed_instruction, seed_res)]
        best_instr, best_res = seed_instruction, seed_res
        history: list[tuple[str, float]] = [(seed_instruction, seed_res.score)]
        merges = 0

        while calls < budget:
            cand = await self._proposer(best_instr, best_res.feedback_text)
            stalled = (not cand) or (cand == best_instr)
            if stalled:
                # reflective mutation stalled — try a bounded merge of the two best (Req 6.3).
                if merges >= self._merge_max or len(frontier) < 2 or calls >= budget:
                    break
                top2 = sorted(frontier, key=lambda x: x[1].score, reverse=True)[:2]
                merged = self._merge(top2[0][0], top2[1][0])
                merges += 1
                if not merged or merged == best_instr:
                    break
                cand = merged
            res = await _eval(cand)
            history.append((cand, res.score))
            frontier.append((cand, res))  # Pareto retention (Req 6.2)
            if res.score > best_res.score:
                best_instr, best_res = cand, res

        return GepaResult(
            best_instruction=best_instr,
            best_score=best_res.score,
            per_dimension=dict(best_res.per_dimension),
            history=tuple(history),
        )


class LiveGepaEngine:
    """Live engine bound to the standalone ``gepa`` package (Req 6.4; design C6/C12).

    Lazily imports ``gepa`` inside :meth:`optimize` (never at module load), wraps the harness
    :class:`GepaMetric` in a contract-correct :class:`gepa.core.adapter.GEPAAdapter`, and calls
    ``gepa.optimize`` with the seed candidate ``{GEPA_COMPONENT: seed_instruction}``, the rung
    items as the trainset, ``use_merge`` / ``max_merge_invocations`` from config, and
    ``max_metric_calls`` set to the rollout budget (Req 9). Raises a clear :class:`RuntimeError`
    if ``gepa`` is not installed.

    ASSUMPTION TO CONFIRM AT RUN TIME (Req 18.3): a live GEPA run additionally needs a real
    reflection LM (the Sonnet proposer) and the live Opus judge; that end-to-end behavior is
    not validated offline (the :class:`FakeGepaEngine` is). The adapter below is written against
    the gepa 0.0.27 ``GEPAAdapter`` contract; re-validate against the installed gepa version
    before trusting any live GEPA number.
    """

    name = "live"

    def __init__(
        self,
        *,
        items: Sequence[Any],
        reflection_lm: Any = None,
        merge_max: int = config.QUALITY_OPT_GEPA_MAX_MERGE_INVOCATIONS,
        use_merge: bool = True,
    ) -> None:
        self._items = list(items)
        self._reflection_lm = reflection_lm
        self._merge_max = int(merge_max)
        self._use_merge = bool(use_merge)

    def _ensure_gepa(self) -> Any:
        import importlib

        try:
            return importlib.import_module("gepa")
        except ImportError as exc:  # pragma: no cover - gepa is installed in this env
            raise RuntimeError(
                "LiveGepaEngine requires the 'gepa' package, which is not installed. "
                "Install it (it is a dspy dependency and normally already present) or set "
                "QUALITY_OPT_GEPA_BACKEND='fake' to use the deterministic offline engine."
            ) from exc

    async def optimize(self, *, seed_instruction: str, metric: GepaMetric, budget: int) -> GepaResult:
        import asyncio

        gepa = self._ensure_gepa()
        adapter = _HarnessGEPAAdapter(metric)
        seed_candidate = {GEPA_COMPONENT: seed_instruction}
        trainset = list(self._items)

        def _run() -> Any:
            return gepa.optimize(
                seed_candidate=seed_candidate,
                trainset=trainset,
                adapter=adapter,
                reflection_lm=self._reflection_lm,
                use_merge=self._use_merge,
                max_merge_invocations=self._merge_max,
                max_metric_calls=max(1, int(budget)),
            )

        result = await asyncio.to_thread(_run)
        # Map gepa's GEPAResult -> our GepaResult. GEPAResult exposes ``best_candidate`` (a dict
        # component->text) and ``best_idx``; the winning score is
        # ``val_aggregate_scores[best_idx]`` — there is NO ``best_score`` attribute (verified
        # against gepa 0.0.27 core/result.py). Be defensive on a degenerate empty result.
        best_candidate = dict(getattr(result, "best_candidate", {}) or {})
        best_instruction = best_candidate.get(GEPA_COMPONENT, seed_instruction)
        try:
            best_score = float(result.val_aggregate_scores[result.best_idx])
        except (AttributeError, IndexError, ValueError, TypeError):
            best_score = 0.0
        per_dim = dict(getattr(adapter, "last_per_dimension", {}) or {})
        return GepaResult(
            best_instruction=best_instruction,
            best_score=best_score,
            per_dimension=per_dim,
        )


class _HarnessGEPAAdapter:
    """Adapt the harness :class:`GepaMetric` to gepa 0.0.27's ``GEPAAdapter`` contract.

    ``evaluate`` runs the metric on each example in the batch and returns per-example scores
    (higher is better, as gepa requires), capturing the feedback text as the per-example
    trajectory so ``make_reflective_dataset`` can build gepa's reflective dataset (the
    ``{"Inputs", "Generated Outputs", "Feedback"}`` schema gepa documents). Never raises for an
    individual example — a failure yields a 0.0 score with the error in the trajectory, per the
    gepa adapter error-handling contract.
    """

    def __init__(self, metric: GepaMetric) -> None:
        self._metric = metric
        self.last_per_dimension: dict[str, float] = {}

    #: gepa's reflective proposer reads ``adapter.propose_new_texts``; ``None`` tells gepa to use
    #: its DEFAULT instruction-proposal strategy (driven by ``reflection_lm`` + the reflective
    #: dataset built by :meth:`make_reflective_dataset`). We rely on that default rather than
    #: implementing custom proposal logic. Must exist as an attribute (the engine accesses it
    #: unconditionally) — a structural-only Protocol implementation omits it and crashes.
    propose_new_texts = None

    def evaluate(self, batch: list[Any], candidate: dict[str, str], capture_traces: bool = False) -> Any:
        import asyncio

        from gepa.core.adapter import EvaluationBatch

        instruction = candidate.get(GEPA_COMPONENT, "")
        outputs: list[Any] = []
        scores: list[float] = []
        trajectories: list[Any] = []
        for item in batch:
            try:
                res = asyncio.run(self._metric.evaluate(instruction, items=[item]))
                outputs.append(res.score)
                scores.append(float(res.score))
                trajectories.append({"feedback": res.feedback_text, "per_dimension": res.per_dimension})
                self.last_per_dimension = dict(res.per_dimension)
            except Exception as exc:  # noqa: BLE001 — gepa contract: never raise per-example
                _LOG.warning("GEPA adapter evaluate failed for an example; scoring 0.0", exc_info=True)
                outputs.append(None)
                scores.append(0.0)
                trajectories.append({"feedback": f"evaluation failed: {exc}", "per_dimension": {}})
        return EvaluationBatch(
            outputs=outputs,
            scores=scores,
            trajectories=trajectories if capture_traces else None,
        )

    def make_reflective_dataset(
        self,
        candidate: dict[str, str],
        eval_batch: Any,
        components_to_update: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        records: list[dict[str, Any]] = []
        for traj in (eval_batch.trajectories or []):
            records.append(
                {
                    "Inputs": {"system_instruction": candidate.get(GEPA_COMPONENT, "")},
                    "Generated Outputs": str(traj.get("per_dimension", {})),
                    "Feedback": str(traj.get("feedback", "")),
                }
            )
        return {comp: records for comp in (components_to_update or [GEPA_COMPONENT])}


def make_bedrock_reflection_lm(
    proposer_model_key: str = config.QUALITY_OPT_GEPA_PROPOSER_MODEL_KEY,
    *,
    region: Optional[str] = None,
    max_tokens: int = 8196,
) -> Callable[[str], str]:
    """Build a ``Callable[[str], str]`` reflection LM for the LIVE gepa engine (Req 12).

    Resolves the proposer model id from ``config.QUALITY_MODELS[proposer_model_key]`` (the
    Sonnet author — distinct from the Opus judge, Req 12) and returns a synchronous callable
    that runs one Bedrock Converse call and returns the assistant text. The ``bedrock-runtime``
    client is built lazily through the credential broker (no new secrets, Req 16.4); importing
    this module never touches boto3.

    ASSUMPTION TO CONFIRM AT RUN TIME (Req 18.3): this live reflection LM requires Bedrock
    credentials and is NOT validated offline (the FakeGepaEngine and the LiveGepaEngine unit
    test with a stub LM are what is verified). Re-validate before trusting any live GEPA run.
    """
    spec = config.QUALITY_MODELS.get(proposer_model_key) or {}
    model_id = spec.get("bedrock_model_id")
    if not model_id:
        # The key may not be a direct QUALITY_MODELS entry (e.g. the thinking-ON Sonnet flavor
        # shares the thinking-OFF base id and is not listed). Fall back to the author module's
        # resolved Sonnet id, which is still a different model from the Opus judge (Req 12).
        from bakeoff.quality.optimizer.author import _default_author_model

        model_id = _default_author_model()
    model_id = str(model_id)
    resolved_region = region or config.AWS_REGION
    _client: dict[str, Any] = {}

    def _build_client() -> Any:
        from bakeoff.credentials import get_broker

        session = get_broker().get_session(region=resolved_region)
        return session.client("bedrock-runtime", region_name=resolved_region)

    def _is_auth_error(exc: Exception) -> bool:
        # ExpiredTokenException / UnrecognizedClientException / AccessDenied surface as either a
        # botocore ClientError (error Code) or in the message; match defensively on both.
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "") if hasattr(exc, "response") else ""
        text = f"{code} {exc}".lower()
        return any(t in text for t in ("expiredtoken", "expired token", "unrecognizedclient",
                                       "invalidsignature", "accessdenied", "security token"))

    def _reflect(prompt: str) -> str:
        # Rebuild the client on an auth/expired-token error and retry ONCE: a multi-hour live
        # run crosses the ~1h credential lifetime, and the on-disk creds are rotated by
        # scripts/creds.sh, so a fresh session picks up the refreshed credentials (mirrors the
        # judge/retrieval self-heal added in 3bce23c). Without this the reflection LM dies ~1h in.
        last_exc: Optional[Exception] = None
        for attempt in (1, 2):
            if "c" not in _client:
                _client["c"] = _build_client()
            try:
                resp = _client["c"].converse(
                    modelId=model_id,
                    system=[{"text": "You improve a system instruction from feedback. Return the new instruction in ``` blocks."}],
                    messages=[{"role": "user", "content": [{"text": prompt}]}],
                    inferenceConfig={"maxTokens": max_tokens},
                )
                out = (resp or {}).get("output", {}).get("message", {}).get("content", [])
                for block in out:
                    if isinstance(block, dict) and "text" in block:
                        return str(block["text"])
                return ""
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt == 1 and _is_auth_error(exc):
                    _LOG.warning("reflection LM auth error; rebuilding client from fresh creds and retrying", exc_info=True)
                    _client.pop("c", None)
                    continue
                raise
        raise last_exc if last_exc else RuntimeError("reflection LM failed")

    return _reflect


def build_gepa_engine(
    name: str = config.QUALITY_OPT_GEPA_BACKEND,
    *,
    proposer: Optional[Proposer] = None,
    items: Optional[Sequence[Any]] = None,
    reflection_lm: Any = None,
    merge_max: int = config.QUALITY_OPT_GEPA_MAX_MERGE_INVOCATIONS,
) -> GepaEngine:
    """Select a :class:`GepaEngine` by ``name`` (Req 6.4).

    * ``"fake"`` (default) → the deterministic, network-free :class:`FakeGepaEngine` (requires
      a ``proposer`` callable; offline tests / config-off-by-default).
    * ``"live"`` → the :class:`LiveGepaEngine` bound to the standalone ``gepa`` engine
      (requires the rung ``items``; lazily imports ``gepa``).

    Raises:
        ValueError: if ``name`` is neither ``"fake"`` nor ``"live"``, or required deps are missing.
    """
    normalized = (name or "").strip().lower()
    if normalized == "fake":
        if proposer is None:
            raise ValueError("FakeGepaEngine requires a `proposer` callable")
        return FakeGepaEngine(proposer=proposer, merge_max=merge_max)
    if normalized == "live":
        if items is None:
            raise ValueError("LiveGepaEngine requires the rung `items` (the trainset)")
        return LiveGepaEngine(items=items, reflection_lm=reflection_lm, merge_max=merge_max)
    raise ValueError(f"unknown gepa backend {name!r}; expected 'fake' or 'live'")
