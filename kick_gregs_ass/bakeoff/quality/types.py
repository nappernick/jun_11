"""
Value objects + durable JSONL for the multi-turn quality study.

The unit of this study is the **per-turn closeness measurement**: one model
answer for one turn of one multi-turn item, scored on how close it is to that
turn's ground truth. A multi-turn item produces one :class:`TurnOutcome` per turn
(3 or 5 of them), all sharing the item's ``item_id`` and the generation's
``trial_id`` so they can be grouped back into a conversation.

Two ground-truth regimes (the dataset's shape, verified on disk):

* **turn 1** carries gold fragments (and an answerability label). Closeness is
  measured against the gold-derived ideal — or, when turn-1 answerability is
  ``none``, as abstention-correctness (did the model correctly decline?).
* **later turns** carry only ``wants`` (no gold). Closeness is measured against
  ``wants`` — the only ground truth available — and the judge grades the answer
  against ``wants`` as the reference.

``ground_truth_kind`` records which regime produced a turn's score so the
dashboard never silently compares a gold-anchored turn-1 number against a
wants-anchored later-turn number as if they were the same measurement.

Mirrors :mod:`bakeoff.eventlog`'s on-disk discipline: one JSON object per line,
single physical line, fsync'd append, crash-tolerant read (a truncated trailing
line is dropped, a malformed interior line is corruption). All frozen dataclasses
so an outcome is an immutable record.
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Optional, Union

__all__ = [
    "GroundTruthKind",
    "TurnCloseness",
    "TurnOutcome",
    "QualityOutcome",
    "to_jsonl",
    "from_jsonl",
    "append_outcome",
    "read_outcomes",
]

PathLike = Union[str, "os.PathLike[str]"]


class GroundTruthKind:
    """Which ground truth a turn's closeness was measured against (string consts).

    * ``GOLD`` — turn-1 of an answerable item: closeness vs the gold-derived ideal.
    * ``ABSTENTION`` — turn-1 of an answerability-``none`` item: scored as
      abstention-correctness (1.0 iff the model correctly declined) rather than
      text closeness, since there is no correct *content* to be close to.
    * ``WANTS`` — any later turn: closeness vs the turn's ``wants`` text (the only
      ground truth later turns carry).
    """

    GOLD = "gold"
    ABSTENTION = "abstention"
    WANTS = "wants"


@dataclasses.dataclass(frozen=True)
class TurnCloseness:
    """The closeness components for one turn (each in ``[0, 1]`` unless noted).

    ``semantic`` is the cosine of the answer vs the turn's reference text, clamped
    to ``[0, 1]`` (a negative cosine contributes 0 to a closeness blend). ``judge``
    is the deferred judge's overall closeness verdict (mean of its rubric
    dimensions), left ``None`` until Phase-2 judging fills it. ``abstention`` is
    the turn-1 abstention-correctness 0/1, populated only for
    :data:`GroundTruthKind.ABSTENTION` turns. ``composite`` is the transparent
    blend actually reported, computed by :mod:`bakeoff.quality.closeness`.
    """

    ground_truth_kind: str
    semantic: float
    composite: float
    judge: Optional[float] = None
    abstention: Optional[int] = None
    # The judge's per-dimension means (filled in Phase-2); empty in Phase-1.
    judge_dimensions: dict[str, float] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class TurnOutcome:
    """One model answer for one turn, with its closeness measurement.

    ``turn`` is the 1-based turn number; ``answer_text`` is the model's answer for
    that turn (from ``ModelResponse.per_turn_answers``); ``reference_text`` is the
    ground-truth text the closeness was measured against (the gold-derived ideal
    for turn-1, or the turn's ``wants`` for later turns) — stored so the dashboard
    can show what the answer was compared to and the judge can be re-run.
    """

    turn: int
    answerability: Optional[str]
    response_dependent: bool
    answer_text: str
    reference_text: str
    closeness: TurnCloseness


@dataclasses.dataclass(frozen=True)
class QualityOutcome:
    """All per-turn outcomes for one (model, item, rep) multi-turn generation.

    Keyed by ``trial_id`` (deterministic, like the bake-off) so a re-run resumes
    by skipping already-present trial ids. ``turn_count`` is denormalized for a
    cheap sanity check on read. The conversational feed-forward decision means
    each turn's answer was generated with the model's own prior answers in
    context, so the per-turn closeness reflects compounding, not isolated turns.
    """

    trial_id: str
    model: str
    item_id: str
    rep: int
    turn_count: int
    prompt_variant_id: str
    turns: tuple[TurnOutcome, ...]
    started_at: str
    completed_at: str
    error: Optional[str] = None

    def to_jsonl(self) -> str:
        """Serialize to a single-line JSON string (no embedded newline)."""
        return json.dumps(
            dataclasses.asdict(self), ensure_ascii=False, separators=(",", ":")
        )

    @classmethod
    def from_jsonl(cls, line: str) -> "QualityOutcome":
        """Parse one JSON line back into a fully-typed outcome."""
        d = json.loads(line)
        turns = tuple(
            TurnOutcome(
                turn=int(t["turn"]),
                answerability=t.get("answerability"),
                response_dependent=bool(t.get("response_dependent", False)),
                answer_text=t.get("answer_text", ""),
                reference_text=t.get("reference_text", ""),
                closeness=TurnCloseness(
                    ground_truth_kind=t["closeness"]["ground_truth_kind"],
                    semantic=float(t["closeness"]["semantic"]),
                    composite=float(t["closeness"]["composite"]),
                    judge=(
                        float(t["closeness"]["judge"])
                        if t["closeness"].get("judge") is not None
                        else None
                    ),
                    abstention=(
                        int(t["closeness"]["abstention"])
                        if t["closeness"].get("abstention") is not None
                        else None
                    ),
                    judge_dimensions={
                        k: float(v)
                        for k, v in (t["closeness"].get("judge_dimensions") or {}).items()
                    },
                ),
            )
            for t in d.get("turns", [])
        )
        return cls(
            trial_id=d["trial_id"],
            model=d["model"],
            item_id=d["item_id"],
            rep=int(d["rep"]),
            turn_count=int(d["turn_count"]),
            prompt_variant_id=d.get("prompt_variant_id", ""),
            turns=turns,
            started_at=d["started_at"],
            completed_at=d["completed_at"],
            error=d.get("error"),
        )


# Module-level (de)serialization aliases mirroring bakeoff.eventlog's surface.
def to_jsonl(outcome: QualityOutcome) -> str:
    """Serialize a :class:`QualityOutcome` to one JSON line."""
    return outcome.to_jsonl()


def from_jsonl(line: str) -> QualityOutcome:
    """Parse one JSON line into a :class:`QualityOutcome`."""
    return QualityOutcome.from_jsonl(line)


def append_outcome(path: PathLike, outcome: QualityOutcome) -> None:
    """Append exactly one outcome line to ``path``, durably (fsync'd).

    Single write of a complete line in append mode, then flush + ``os.fsync`` so
    the record is on disk before returning — the same durability discipline as
    :func:`bakeoff.eventlog.append_event`, so a crash leaves at most one truncated
    trailing line (which :func:`read_outcomes` tolerates).
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = outcome.to_jsonl() + "\n"
    with open(p, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def read_outcomes(path: PathLike) -> list[QualityOutcome]:
    """Read all outcomes from ``path`` (``[]`` if absent), tolerating a truncated tail.

    A missing file yields ``[]``. Every complete line is parsed. If the FINAL line
    fails to parse — the signature of a process killed mid-write — that single
    trailing partial line is discarded and the complete prefix is returned without
    raising. A non-final malformed line is real corruption and raises.
    """
    p = Path(path)
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8") as f:
        raw_lines = f.readlines()
    out: list[QualityOutcome] = []
    last = len(raw_lines) - 1
    for i, raw in enumerate(raw_lines):
        line = raw.rstrip("\n")
        if not line:
            continue
        try:
            out.append(QualityOutcome.from_jsonl(line))
        except Exception:  # noqa: BLE001 - tolerate only a truncated final line
            if i == last:
                break
            raise
    return out
