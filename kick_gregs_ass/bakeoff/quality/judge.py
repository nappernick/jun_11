"""
Phase-2 deferred per-turn judge for the quality study.

Like the bake-off's :mod:`bakeoff.judge_phase2`, the judge (Opus, slow + TPM
limited) runs AFTER generation, over the recorded quality outcomes, and writes a
SEPARATE store keyed back to the turn it graded. The difference is granularity:
the bake-off judges one answer per trial; here we judge **every turn** of every
multi-turn outcome, because the whole point of this study is per-turn closeness.

What each turn is graded against (the dataset's two regimes):

* **turn 1, answerable** — graded against the gold-derived ideal (the same
  ``ideal_text`` the closeness scorer used), with the gold fragment texts as the
  grounding reference. ``answerability`` is the item's turn-1 label.
* **turn 1, answerability ``none``** — graded as a ``none`` answerability case
  (the judge rubric's completeness/faithfulness already reward correct
  abstention), with the ideal being "correctly decline".
* **later turn** — graded against the turn's ``wants`` as the ideal (the only
  ground truth later turns carry). There is no gold for later turns, so there are
  no grounding fragments; the judge scores correctness/completeness of the answer
  vs ``wants``. ``answerability`` for later turns is unknown in the dataset, so it
  is treated as ``"full"`` for rubric purposes (the judge is asked "how close is
  this to what the turn wanted").

The judge's overall verdict for a turn is the mean of its rubric dimensions; that
verdict is folded into the turn's closeness composite via
:func:`bakeoff.quality.closeness.blend_closeness` (semantic + judge), and the
enriched outcomes are rewritten. Records are also persisted to
``QUALITY_JUDGE_SCORES_PATH`` keyed by ``(trial_id, turn)`` for the dashboard +
audit + resume.

Resumable: a turn already present in the judge store is skipped. Backend
injectable: pass a :class:`bakeoff.scoring.judge.StubJudge`-backed
:class:`JudgeScorer` for a fully-offline pass; the default is the real Opus judge.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence, Union

from bakeoff import config
from bakeoff.quality.closeness import blend_closeness
from bakeoff.quality.dataset import load_multi_turn_items
from bakeoff.quality.types import (
    GroundTruthKind,
    QualityOutcome,
    TurnCloseness,
    TurnOutcome,
    append_outcome,
    read_outcomes,
)
from bakeoff.scoring.judge import JUDGE_DIMENSIONS, JudgeScorer
from bakeoff.scoring.semantic import ideal_response_text
from bakeoff.types import Item

__all__ = [
    "TurnJudgeRecord",
    "QualityJudgeResult",
    "read_turn_judge_scores",
    "append_turn_judge_score",
    "run_quality_judge",
    "enrich_outcomes_with_judge",
]

PathLike = Union[str, "os.PathLike[str]"]


@dataclasses.dataclass(frozen=True)
class TurnJudgeRecord:
    """One Phase-2 judge verdict for one turn, keyed by ``(trial_id, turn)``."""

    trial_id: str
    model: str
    item_id: str
    turn: int
    ground_truth_kind: str
    overall: float
    dimensions: dict[str, float]
    judge_model: str
    judged_at: str
    evidence: dict[str, str] = dataclasses.field(default_factory=dict)
    answer_excerpt: str = ""

    def key(self) -> tuple[str, int]:
        return (self.trial_id, self.turn)

    def to_jsonl(self) -> str:
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_jsonl(cls, line: str) -> "TurnJudgeRecord":
        d = json.loads(line)
        return cls(
            trial_id=d["trial_id"],
            model=d["model"],
            item_id=d["item_id"],
            turn=int(d["turn"]),
            ground_truth_kind=d["ground_truth_kind"],
            overall=float(d["overall"]),
            dimensions={k: float(v) for k, v in (d.get("dimensions") or {}).items()},
            judge_model=str(d["judge_model"]),
            judged_at=d["judged_at"],
            evidence={k: str(v) for k, v in (d.get("evidence") or {}).items()},
            answer_excerpt=str(d.get("answer_excerpt", "")),
        )


@dataclasses.dataclass(frozen=True)
class QualityJudgeResult:
    """Summary of one quality Phase-2 judge pass."""

    outcomes_seen: int
    turns_total: int
    turns_judged: int
    turns_skipped: int
    judge_scores_path: Path
    by_model: dict[str, int]


def read_turn_judge_scores(path: PathLike) -> list[TurnJudgeRecord]:
    """Read all per-turn judge verdicts (``[]`` if absent), tolerating a truncated tail."""
    p = Path(path)
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8") as f:
        raw_lines = f.readlines()
    out: list[TurnJudgeRecord] = []
    last = len(raw_lines) - 1
    for i, raw in enumerate(raw_lines):
        line = raw.rstrip("\n")
        if not line:
            continue
        try:
            out.append(TurnJudgeRecord.from_jsonl(line))
        except Exception:  # noqa: BLE001 - tolerate only a truncated final line
            if i == last:
                break
            raise
    return out


def append_turn_judge_score(path: PathLike, record: TurnJudgeRecord) -> None:
    """Append one verdict line, durably (fsync'd)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = record.to_jsonl() + "\n"
    with open(p, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _turn_judge_inputs(
    item: Item, turn_index: int, turn_outcome: TurnOutcome
) -> tuple[str, list[str], str]:
    """Return ``(ideal_text, gold_texts, answerability)`` for judging one turn.

    Turn-1 answerable: ideal = gold-derived ideal, gold_texts = the gold bodies,
    answerability = the item label. Turn-1 abstention: ideal describes correct
    declining, answerability ``none``. Later turn of an answerable conversation:
    ideal = the turn's ``wants``, no gold, answerability ``full`` (we ask how close
    the answer is to what the turn wanted). Later turn of an UNANSWERABLE
    conversation (item answerability ``none``): routed to the abstention regime —
    the conversation is out-of-domain throughout, so the judge is told
    ``answerability="none"`` and the ideal is "correctly decline", rewarding a
    correct decline instead of grading it against a ``wants`` it should not answer.
    """
    kind = turn_outcome.closeness.ground_truth_kind
    if kind == GroundTruthKind.GOLD:
        gold = item.gold
        gold_texts = [g.markdown or g.snippet or g.title for g in gold if (g.markdown or g.snippet or g.title)]
        ideal = ideal_response_text(gold, item.wants)
        return ideal, gold_texts, (item.answerability or item.cohort.answerability or "full")
    if kind == GroundTruthKind.ABSTENTION:
        ideal = (
            "Correctly decline: state you don't have the information in the "
            "reference material and point the user to the right owner."
        )
        return ideal, [], "none"
    # WANTS (later turn). A later turn of an unanswerable conversation should be
    # judged as abstention, not as a "full" answerable turn — consult the
    # conversation-level answerability (later-turn answerability is None by
    # construction) so we never hand the judge answerability="full" for an
    # out-of-domain conversation, even for stored outcomes whose ground_truth_kind
    # predates the turn_reference abstention routing.
    if (item.answerability or item.cohort.answerability) == "none":
        ideal = (
            "Correctly decline: state you don't have the information in the "
            "reference material and point the user to the right owner."
        )
        return ideal, [], "none"
    return (turn_outcome.reference_text or ""), [], "full"


async def run_quality_judge(
    *,
    judge_scorer: JudgeScorer,
    outcomes_path=config.QUALITY_OUTCOMES_PATH,
    judge_scores_path=config.QUALITY_JUDGE_SCORES_PATH,
    items: Optional[Sequence[Item]] = None,
    model_keys: Optional[Sequence[str]] = None,
    max_concurrency: Optional[int] = None,
    enrich: bool = True,
) -> QualityJudgeResult:
    """Judge every turn of every recorded quality outcome (Phase-2), resumably.

    For each outcome turn not already judged: reconstruct the judge inputs for that
    turn's regime, run ``judge_scorer.score_detailed`` (k debiased samples), persist
    a :class:`TurnJudgeRecord`, and (if ``enrich``) fold the judge verdict into the
    turn's closeness composite. Returns a summary. Idempotent: re-running judges
    only not-yet-judged turns.
    """
    outcomes = read_outcomes(outcomes_path)
    item_index = {it.item_id: it for it in (items if items is not None else load_multi_turn_items())}
    wanted = set(model_keys) if model_keys is not None else None

    already = {r.key() for r in read_turn_judge_scores(judge_scores_path)}
    cap = max_concurrency if max_concurrency is not None else config.CONCURRENCY_CAPS["judge"]
    sem = asyncio.Semaphore(max(1, cap))
    write_lock = asyncio.Lock()

    turns_total = 0
    turns_judged = 0
    turns_skipped = 0
    by_model: dict[str, int] = {}

    async def judge_turn(outcome: QualityOutcome, turn_index: int, turn: TurnOutcome) -> None:
        nonlocal turns_judged, turns_skipped
        key = (outcome.trial_id, turn.turn)
        if key in already:
            turns_skipped += 1
            return
        item = item_index.get(outcome.item_id)
        if item is None:
            turns_skipped += 1
            return
        ideal, gold_texts, answerability = _turn_judge_inputs(item, turn_index, turn)
        momentary_state = (
            item.turns[turn_index].momentary_state
            if turn_index < len(item.turns)
            else "neutral"
        )
        async with sem:
            scores, evidence = await asyncio.to_thread(
                judge_scorer.score_detailed,
                turn.answer_text,
                ideal_text=ideal,
                fragments=[],
                gold_texts=gold_texts,
                momentary_state=momentary_state,
                answerability=answerability,
            )
        dims = {d: getattr(scores, d) for d in JUDGE_DIMENSIONS}
        overall = sum(dims.values()) / len(dims) if dims else 0.0
        record = TurnJudgeRecord(
            trial_id=outcome.trial_id,
            model=outcome.model,
            item_id=outcome.item_id,
            turn=turn.turn,
            ground_truth_kind=turn.closeness.ground_truth_kind,
            overall=overall,
            dimensions=dims,
            judge_model=scores.judge_model,
            judged_at=_now_iso(),
            evidence=evidence,
            answer_excerpt=(turn.answer_text or "")[:600],
        )
        async with write_lock:
            append_turn_judge_score(judge_scores_path, record)
            turns_judged += 1
            by_model[outcome.model] = by_model.get(outcome.model, 0) + 1

    tasks = []
    for outcome in outcomes:
        if outcome.error is not None:
            continue
        if wanted is not None and outcome.model not in wanted:
            continue
        for ti, turn in enumerate(outcome.turns):
            turns_total += 1
            tasks.append(judge_turn(outcome, ti, turn))
    if tasks:
        await asyncio.gather(*tasks)

    if enrich:
        enrich_outcomes_with_judge(
            outcomes_path=outcomes_path, judge_scores_path=judge_scores_path
        )

    return QualityJudgeResult(
        outcomes_seen=len(outcomes),
        turns_total=turns_total,
        turns_judged=turns_judged,
        turns_skipped=turns_skipped,
        judge_scores_path=Path(judge_scores_path),
        by_model=by_model,
    )


def enrich_outcomes_with_judge(
    *,
    outcomes_path=config.QUALITY_OUTCOMES_PATH,
    judge_scores_path=config.QUALITY_JUDGE_SCORES_PATH,
) -> int:
    """Fold the per-turn judge verdicts into the outcomes' closeness composites.

    Reads the judge store, then rewrites the outcomes file in place so each turn's
    :class:`TurnCloseness` carries the judge overall + per-dimension means and a
    recomputed composite (semantic + judge blend; abstention turns keep their 0/1
    composite). Returns the number of turns enriched. Atomic via a temp file
    rename so a crash never leaves a half-written outcomes store.

    An abstention turn is left as-is (its closeness is already the correct 0/1 and
    the judge does not change that). A turn with no judge verdict yet is left at
    its Phase-1 (semantic-only) composite.
    """
    judge_index: dict[tuple[str, int], TurnJudgeRecord] = {
        r.key(): r for r in read_turn_judge_scores(judge_scores_path)
    }
    if not judge_index:
        return 0

    outcomes = read_outcomes(outcomes_path)
    enriched_count = 0
    new_lines: list[str] = []
    for outcome in outcomes:
        new_turns: list[TurnOutcome] = []
        for turn in outcome.turns:
            rec = judge_index.get((outcome.trial_id, turn.turn))
            if rec is None or turn.closeness.ground_truth_kind == GroundTruthKind.ABSTENTION:
                new_turns.append(turn)
                continue
            new_closeness = TurnCloseness(
                ground_truth_kind=turn.closeness.ground_truth_kind,
                semantic=turn.closeness.semantic,
                composite=blend_closeness(turn.closeness.semantic, rec.overall),
                judge=rec.overall,
                abstention=turn.closeness.abstention,
                judge_dimensions=dict(rec.dimensions),
            )
            new_turns.append(dataclasses.replace(turn, closeness=new_closeness))
            enriched_count += 1
        new_outcome = dataclasses.replace(outcome, turns=tuple(new_turns))
        new_lines.append(new_outcome.to_jsonl())

    p = Path(outcomes_path)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")
    os.replace(tmp, p)
    return enriched_count
