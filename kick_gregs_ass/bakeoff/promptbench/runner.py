"""
PromptBenchRunner — score every candidate prompt on the fixed 24-conversation sample and
stream/persist the results, fully isolated from the optimizers.

For each prompt, in order:
  1. emit ``promptbench_prompt_started``;
  2. run :class:`PromptBenchScorer` over the pinned 24 items (own semaphores, promptbench
     Bedrock account), with an ``on_conversation_scored`` callback that persists one
     :class:`PointRecord` and emits one ``promptbench_point`` per conversation — the live
     scatter fill-in;
  3. on pass completion, persist a :class:`ResultRecord` (triad + CI + dimension/abstention
     breakdown + confident-wrong gate hit count) and emit ``promptbench_prompt_completed``.
Finally it emits ``promptbench_status`` with the leaderboard + crowned winner.

It never raises out of :meth:`run` — a per-prompt failure is contained and recorded, and a
``PromptBenchIterationSkipped`` (too many conversation failures) is logged and skipped, so a
single bad prompt cannot abort the whole bench.

RESUME (default on): on re-run, any prompt that already has a durable ``ResultRecord`` is
reused as-is instead of being re-scored, so the hundreds of model+judge calls behind a
completed prompt are never redone after an interruption. Resume is per-prompt (the aggregate
result is rebuilt from full verdicts the scatter points don't carry); an explicit Stop &
Reset archives the stores for a clean-slate run.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional, Sequence

from bakeoff import config
from bakeoff.promptbench.backend import build_promptbench_backend
from bakeoff.promptbench.prompts import PromptSpec, load_prompts
from bakeoff.promptbench.sample import (
    load_sample_items,
    load_sample_spec,
    sample_index_by_item_id,
)
from bakeoff.promptbench.scorer import PromptBenchIterationSkipped, PromptBenchScorer
from bakeoff.promptbench.store import PointRecord, PromptBenchStore, ResultRecord

__all__ = ["PromptBenchRunner", "compute_winner"]

_LOG = logging.getLogger("bakeoff.promptbench.runner")

Emit = Callable[[str, dict], None]


def compute_winner(results: Sequence[dict]) -> Optional[dict]:
    """Crown the highest-mean prompt; flag when the runner-up overlaps within CI.

    Returns ``{"prompt_key", "label", "triad", "tie_within_ci"}`` or ``None`` when there are
    no results yet. ``tie_within_ci`` is ``True`` when the second-best prompt's score lies
    within the winner's 95% CI half-width (so we don't over-claim a coin-flip).
    """
    ranked = sorted(results, key=lambda r: r["triad"], reverse=True)
    if not ranked:
        return None
    top = ranked[0]
    tie = False
    if len(ranked) > 1:
        gap = top["triad"] - ranked[1]["triad"]
        tie = gap <= float(top.get("ci_half_width", 0.0))
    return {
        "prompt_key": top["prompt_key"],
        "label": top["label"],
        "triad": top["triad"],
        "tie_within_ci": tie,
    }


class PromptBenchRunner:
    """Drive the fixed prompt leaderboard over the live promptbench stack."""

    def __init__(
        self,
        *,
        backend=None,
        store: Optional[PromptBenchStore] = None,
        emit: Optional[Emit] = None,
        resume: bool = True,
    ) -> None:
        self._backend = backend  # built lazily on run() if None
        self._store = store or PromptBenchStore()
        self._emit: Emit = emit or (lambda _t, _p: None)
        #: When True (the default), a re-run RESUMES: any prompt that already has a durable
        #: ResultRecord is reused as-is rather than re-scored, so the hundreds of model+judge
        #: calls behind a completed prompt are never redone. Resume is per-PROMPT — the
        #: aggregate triad/CI/dimension breakdown is rebuilt from full per-turn verdicts, which
        #: the per-conversation scatter points do NOT carry, so a fully-completed prompt is
        #: reused intact while a partially-scored prompt (no ResultRecord yet) re-runs in full.
        #: An explicit Stop & Reset archives the stores for a clean-slate run (resume finds
        #: nothing to skip).
        self._resume = bool(resume)

    async def run(self) -> dict:
        """Score every prompt on the pinned sample; return the leaderboard.

        Resumes by default: prompts with a durable ResultRecord are reused, not re-scored.
        """
        backend = self._backend or build_promptbench_backend()
        spec = load_sample_spec()
        items = load_sample_items(spec)
        idx_by_id = sample_index_by_item_id(spec)
        # Tolerate either "item_id" or "id" in the spec's items metadata so a key
        # mismatch between sample formats can never crash the run with KeyError.
        meta_by_id = {
            (it.get("item_id") or it.get("id")): it
            for it in spec.get("items", [])
            if (it.get("item_id") or it.get("id"))
        }
        prompts = load_prompts()

        # Resume map: prompt_key -> already-completed ResultRecord dict (newest wins via the
        # store's last-write-wins read). Empty when resume is off, so every prompt is scored.
        completed: dict[str, dict] = {}
        if self._resume:
            completed = {r.prompt_key: r.to_dict() for r in self._store.read_results()}

        results: list[dict] = []
        for pi, prompt in enumerate(prompts):
            # RESUME: a prompt that already completed is reused intact — no model/judge calls.
            if prompt.key in completed:
                done = completed[prompt.key]
                results.append(done)
                self._emit(
                    "promptbench_prompt_started",
                    {"prompt_key": prompt.key, "label": prompt.label, "text": prompt.text,
                     "index": pi, "total_prompts": len(prompts), "resumed": True},
                )
                self._emit("promptbench_prompt_completed", {**done, "resumed": True})
                _LOG.info("promptbench: prompt %s reused from durable result (resume)", prompt.key)
                continue
            self._emit(
                "promptbench_prompt_started",
                {"prompt_key": prompt.key, "label": prompt.label, "text": prompt.text,
                 "index": pi, "total_prompts": len(prompts)},
            )
            try:
                result = await self._score_one(prompt, items, idx_by_id, meta_by_id, backend)
                results.append(result)
            except PromptBenchIterationSkipped as skip:
                _LOG.error(
                    "promptbench: prompt %s skipped — only %d/%d conversations survived",
                    prompt.key, skip.survivors, skip.total,
                )
                self._emit(
                    "promptbench_prompt_failed",
                    {"prompt_key": prompt.key, "label": prompt.label,
                     "reason": f"only {skip.survivors}/{skip.total} conversations survived"},
                )
            except Exception as exc:  # noqa: BLE001 — one prompt must not abort the bench
                _LOG.exception("promptbench: prompt %s failed: %r", prompt.key, exc)
                self._emit(
                    "promptbench_prompt_failed",
                    {"prompt_key": prompt.key, "label": prompt.label, "reason": repr(exc)},
                )

        winner = compute_winner(results)
        self._emit("promptbench_status", {"results": results, "winner": winner, "done": True})
        return {"results": results, "winner": winner}

    async def _score_one(self, prompt: PromptSpec, items, idx_by_id, meta_by_id, backend) -> dict:
        def on_scored(*, role, done, total, item_id, rep, conversation_mean) -> None:
            meta = meta_by_id.get(item_id, {})
            rec = PointRecord(
                prompt_key=prompt.key,
                conversation_index=idx_by_id.get(item_id, done),
                item_id=item_id,
                answerability=str(meta.get("answerability", "")),
                turns=int(meta.get("turns", 0)),
                overall=float(conversation_mean),
            )
            self._store.append_point(rec)
            self._emit("promptbench_point", {**rec.to_dict(), "label": prompt.label})

        scorer = PromptBenchScorer(
            backend,
            reps=config.PROMPT_BENCH_REPS,
            on_conversation_scored=on_scored,
        )
        slice_score = await scorer.score_prompt(
            model=config.PROMPT_BENCH_MODEL,
            instruction=prompt.text,
            items=items,
            prompt_role=prompt.key,
        )
        confident_wrong = sum(
            1
            for v in slice_score.verdicts
            if v.dimensions.get("faithfulness", 1.0) < config.QUALITY_OPT_FAITHFULNESS_FLOOR
        )
        rec = ResultRecord(
            prompt_key=prompt.key,
            label=prompt.label,
            triad=float(slice_score.triad_score),
            ci_half_width=float(slice_score.ci_half_width),
            ci_low=float(slice_score.ci_low),
            ci_high=float(slice_score.ci_high),
            n_conversations=int(slice_score.n_conversations),
            per_dimension_mean=dict(slice_score.per_dimension_mean),
            abstention_reward_mean=float(slice_score.abstention_reward_mean),
            answered_when_unsure_rate=float(slice_score.answered_when_unsure_rate),
            confident_wrong_count=int(confident_wrong),
        )
        self._store.append_result(rec)
        self._emit("promptbench_prompt_completed", rec.to_dict())
        return rec.to_dict()
