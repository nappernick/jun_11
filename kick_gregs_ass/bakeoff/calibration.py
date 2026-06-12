"""
Judge calibration: judge↔human agreement, reported (never gated) — Task 15.

This module makes the LLM-as-judge **defensible** the way the design's "Layer C"
asks: a small human-labeled calibration set is scored by the *same* judge the
harness uses, and we report **how well the judge tracks humans per dimension**.
Crucially this is **reporting only** — poor agreement never raises and never
blocks a run (Req 14.4). What it *does* drive is a transparent, operator-visible
signal: which judge dimensions fall below an agreement threshold so the composite
can **soften or exclude** them (Req 13.2), and the judge↔human agreement numbers
that ride in every exec chart's provenance footer (Req 11.7).

--------------------------------------------------------------------------------
Agreement metric (the choice, documented)
--------------------------------------------------------------------------------
Judge and human both produce **graded** rubric scores (the judge a continuous
mean in ``[0, 1]`` over its ``k`` samples; the human a graded score per
dimension). For graded scores the right family is a **correlation**, not a
categorical-agreement coefficient:

* **Cohen's kappa** is for *categorical* labels — it would require binning the
  graded scores into discrete classes, throwing away ordering information and
  making the result sensitive to arbitrary bin edges. Rejected.
* **Pearson** measures *linear* agreement, so it is depressed by the systematic
  scale-usage differences that are endemic to LLM-judge-vs-human graded scoring
  (a judge that is uniformly stricter but ranks answers the same way is what we
  want to keep).

We therefore use **Spearman's rank correlation (ρ)** as the headline
per-dimension agreement metric. Spearman measures *monotonic* (rank) agreement,
so it rewards a judge that **orders** answers the way humans do even if its
absolute scale differs — exactly the property that makes a judge defensible for
*ranking* candidates (which is what the bakeoff does). Pearson and mean absolute
error are computed alongside ρ for transparency, but ρ is the number the
low-agreement flag and the footer use.

**Sourcing caveat.** Spearman-ρ for judge↔human graded agreement, and the
moderate-agreement threshold below, are **general industry practice, not
Amazon-internal guidance** (the design's sourcing note applies: the internal
primary sources the rigor steering rule prefers were unreachable here). Treat the
threshold as a reporting aid to re-validate internally before any number defends
a decision upward.

Undefined cases are handled honestly: with fewer than two paired items, or when
either rater gives a **constant** score across the set (zero variance → ρ
undefined), the agreement is reported as ``None`` and the dimension is flagged
low-agreement — we cannot *establish* that the judge tracks humans there, so it
should be softened/excluded rather than trusted.

--------------------------------------------------------------------------------
Calibration-set input format
--------------------------------------------------------------------------------
A small JSONL, one record per line. Each record carries enough to (a) re-score
the answer with the judge and (b) compare to the human labels::

    {
      "item_id": "b0-q01",                 # optional, for traceability
      "answer": "the model answer to grade",
      "answerability": "full",             # full | partial | none
      "momentary_state": "neutral",        # drives the interaction rubric
      "ideal_text": "the ideal response",  # optional
      "gold_texts": ["gold fragment text"],# optional (grounding signal)
      "fragments": [{"id": "n1", "text": "..."}],  # optional retrieved context
      "human_scores": {"faithfulness": 0.75, "correctness": 1.0, "tone": 0.5}
    }

``human_scores`` are in ``[0, 1]`` by default (matching the judge's normalized
output); pass ``human_scale="1-5"`` to :func:`load_calibration_set` /
:func:`score_calibration_set` to auto-normalize a 1–5 graded scale via
``(s-1)/4``. A human need only label the dimensions they actually graded; a
dimension scored on too few records is reported as undefined.

The judge is **injectable** (any object exposing
``score(answer_text, *, ideal_text, fragments, gold_texts, momentary_state,
answerability) -> JudgeScores`` — i.e. a :class:`bakeoff.scoring.judge.JudgeScorer`):
tests pass a ``JudgeScorer`` wrapping the deterministic
:class:`~bakeoff.scoring.judge.StubJudge`, so calibration runs with **zero
Bedrock calls**.

Dependencies: stdlib (``json``/``dataclasses``/``math``) + ``numpy`` (ranks /
correlation) + :mod:`bakeoff.scoring.judge` (dimension names only). No network.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence, Union

import numpy as np

from bakeoff.scoring.judge import JUDGE_DIMENSIONS

__all__ = [
    "DEFAULT_AGREEMENT_THRESHOLD",
    "AGREEMENT_METRIC",
    "CalibrationRecord",
    "DimensionAgreement",
    "CalibrationReport",
    "spearman_rho",
    "load_calibration_set",
    "score_calibration_set",
]

PathLike = Union[str, "os.PathLike[str]"]

#: Headline agreement metric (documented above): Spearman's rank correlation.
AGREEMENT_METRIC: str = "spearman_rho"

#: Reporting threshold below which a dimension is flagged low-agreement so the
#: composite can soften/exclude it (Req 13.2). 0.6 is a conventional
#: moderate-to-strong correlation cut. **General industry practice, not
#: Amazon-internal guidance** — a reporting aid, NOT a pass/fail gate (Req 14.4).
DEFAULT_AGREEMENT_THRESHOLD: float = 0.6


# ---------------------------------------------------------------------------
# Input record
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CalibrationRecord:
    """One human-labeled calibration item (an answer + its human rubric scores).

    ``human_scores`` maps judge-dimension name → graded score in ``[0, 1]`` (after
    any scale normalization). Only the dimensions a human actually graded need be
    present. The remaining fields feed the judge so it re-scores the *same* answer
    under the *same* rubric.
    """

    answer: str
    answerability: str = "full"
    momentary_state: str = "neutral"
    ideal_text: str = ""
    gold_texts: tuple[str, ...] = ()
    fragments: tuple[dict, ...] = ()
    human_scores: Mapping[str, float] = field(default_factory=dict)
    item_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Per-dimension result + the report
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DimensionAgreement:
    """Judge↔human agreement for one rubric dimension over the calibration set.

    ``agreement`` is Spearman's ρ (``None`` when undefined — fewer than two paired
    items, or a constant rater). ``pearson`` and ``mae`` are transparency
    cross-checks. ``low_agreement`` is ``True`` iff ρ is undefined OR below the
    threshold — the signal the composite uses to soften/exclude the dimension
    (Req 13.2). This is descriptive; nothing here raises.
    """

    dimension: str
    agreement: Optional[float]      # Spearman rho; None if undefined
    pearson: Optional[float]
    mae: float
    n: int                          # paired (judge, human) observations
    judge_mean: float
    human_mean: float
    low_agreement: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "dimension": self.dimension,
            "agreement": self.agreement,
            "pearson": self.pearson,
            "mae": self.mae,
            "n": self.n,
            "judge_mean": self.judge_mean,
            "human_mean": self.human_mean,
            "low_agreement": self.low_agreement,
        }


@dataclass(frozen=True)
class CalibrationReport:
    """The structured judge↔human calibration result (reported, never gated).

    Carries the per-dimension agreement, the threshold used, the judge model, and
    helpers to (a) feed the exec provenance footer
    (:meth:`agreement_for_footer`) and (b) soften/exclude low-agreement dimensions
    from a composite-weights dict (:meth:`adjusted_weights`) per Req 13.2. The
    report itself applies neither — it only *exposes* the signal, so the decision
    to re-weight stays explicit and operator-visible.
    """

    agreement_metric: str
    threshold: float
    n_items: int
    judge_model: str
    per_dimension: dict[str, DimensionAgreement]

    @property
    def low_agreement_dimensions(self) -> list[str]:
        """Dimensions below the agreement threshold (or undefined), sorted.

        These are the dimensions a composite should soften or exclude (Req 13.2):
        the judge has not been shown to track humans on them.
        """
        return sorted(d for d, a in self.per_dimension.items() if a.low_agreement)

    @property
    def defensible_dimensions(self) -> list[str]:
        """Dimensions whose agreement meets the threshold (sorted)."""
        return sorted(d for d, a in self.per_dimension.items() if not a.low_agreement)

    def agreement_for_footer(self) -> dict[str, float]:
        """``{dimension: rho}`` for dimensions with a *defined* ρ (exec footer).

        Feeds :meth:`bakeoff.aggregate.AggregationEngine.materialize`'s
        ``judge_human_agreement`` so every exec chart's provenance footer carries
        the judge↔human agreement it was calibrated at (Req 11.7). Undefined
        dimensions are omitted (a missing number is honest; a fabricated one is
        not).
        """
        return {
            dim: float(a.agreement)
            for dim, a in self.per_dimension.items()
            if a.agreement is not None
        }

    def adjusted_weights(
        self,
        weights: Mapping[str, float],
        *,
        mode: str = "soften",
        soften_factor: float = 0.5,
    ) -> dict[str, float]:
        """Return composite ``weights`` with low-agreement dimensions adjusted (Req 13.2).

        ``mode="soften"`` (default) multiplies each low-agreement dimension's
        weight by ``soften_factor`` (down-weighting a judge dimension we cannot
        fully defend); ``mode="exclude"`` drops it to ``0.0``. Weight keys that are
        not judge dimensions (e.g. ``grounding``, ``semantic_similarity``) are left
        untouched. The returned dict is a copy — the caller decides whether to use
        it; calibration never mutates or gates on it.

        Raises:
            ValueError: on an unknown ``mode``.
        """
        if mode not in ("soften", "exclude"):
            raise ValueError(f"unknown mode {mode!r}; expected 'soften' or 'exclude'")
        low = set(self.low_agreement_dimensions)
        out: dict[str, float] = {}
        for key, w in weights.items():
            if key in low:
                out[key] = 0.0 if mode == "exclude" else float(w) * float(soften_factor)
            else:
                out[key] = float(w)
        return out

    def to_dict(self) -> dict[str, object]:
        """JSON-ready view of the whole report (for logging / inspection)."""
        return {
            "agreement_metric": self.agreement_metric,
            "threshold": self.threshold,
            "n_items": self.n_items,
            "judge_model": self.judge_model,
            "low_agreement_dimensions": self.low_agreement_dimensions,
            "per_dimension": {d: a.to_dict() for d, a in self.per_dimension.items()},
        }


# ---------------------------------------------------------------------------
# Agreement math (Spearman ρ via average ranks + Pearson on ranks)
# ---------------------------------------------------------------------------
def _average_ranks(values: Sequence[float]) -> list[float]:
    """Return average (tie-corrected) ranks of ``values`` (0-based mean ranks)."""
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    """Pearson correlation of ``a`` vs ``b``; ``None`` if undefined (constant/short)."""
    if len(a) < 2 or len(b) < 2:
        return None
    arr_a = np.asarray(a, dtype=np.float64)
    arr_b = np.asarray(b, dtype=np.float64)
    if arr_a.std() == 0.0 or arr_b.std() == 0.0:
        return None
    r = float(np.corrcoef(arr_a, arr_b)[0, 1])
    if np.isnan(r):
        return None
    return r


def spearman_rho(human: Sequence[float], judge: Sequence[float]) -> Optional[float]:
    """Spearman's rank correlation between paired ``human`` and ``judge`` scores.

    Computed as the Pearson correlation of the two tie-corrected rank vectors.
    Returns ``None`` when undefined — fewer than two pairs, or either rater
    constant across the set (zero rank variance) — which calibration treats as
    "agreement not establishable" (flagged low-agreement). The headline agreement
    metric documented in this module.
    """
    if len(human) != len(judge):
        raise ValueError("human and judge score series must be the same length")
    if len(human) < 2:
        return None
    return _pearson(_average_ranks(human), _average_ranks(judge))


# ---------------------------------------------------------------------------
# Loading the calibration set
# ---------------------------------------------------------------------------
def _normalize_human_scores(
    raw: Mapping[str, object], human_scale: str
) -> dict[str, float]:
    """Coerce a record's ``human_scores`` to a ``[0, 1]`` dict per ``human_scale``."""
    out: dict[str, float] = {}
    for dim, val in (raw or {}).items():
        try:
            f = float(val)
        except (TypeError, ValueError):
            continue
        if human_scale == "1-5":
            f = (f - 1.0) / 4.0
        out[dim] = _clip01(f)
    return out


def _clip01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _record_from_dict(d: Mapping[str, object], human_scale: str) -> CalibrationRecord:
    """Build a :class:`CalibrationRecord` from one parsed JSONL object."""
    fragments = tuple(dict(f) for f in (d.get("fragments") or []))
    gold_texts = tuple(str(g) for g in (d.get("gold_texts") or []))
    return CalibrationRecord(
        answer=str(d.get("answer", "")),
        answerability=str(d.get("answerability", "full")),
        momentary_state=str(d.get("momentary_state", "neutral")),
        ideal_text=str(d.get("ideal_text", "")),
        gold_texts=gold_texts,
        fragments=fragments,
        human_scores=_normalize_human_scores(d.get("human_scores") or {}, human_scale),
        item_id=(str(d["item_id"]) if d.get("item_id") is not None else None),
    )


def load_calibration_set(
    path: PathLike, *, human_scale: str = "unit"
) -> list[CalibrationRecord]:
    """Load a calibration JSONL into :class:`CalibrationRecord`s.

    ``human_scale`` is ``"unit"`` (scores already in ``[0, 1]``, the default) or
    ``"1-5"`` (graded 1–5, normalized via ``(s-1)/4``). Blank lines are skipped.

    Raises:
        ValueError: on an unknown ``human_scale``.
        FileNotFoundError: if ``path`` does not exist.
    """
    if human_scale not in ("unit", "1-5"):
        raise ValueError(f"unknown human_scale {human_scale!r}; expected 'unit' or '1-5'")
    p = Path(path)
    records: list[CalibrationRecord] = []
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                records.append(_record_from_dict(json.loads(line), human_scale))
    return records


# ---------------------------------------------------------------------------
# Scoring the calibration set with the judge → the report
# ---------------------------------------------------------------------------
def score_calibration_set(
    records: Sequence[CalibrationRecord],
    judge,
    *,
    threshold: float = DEFAULT_AGREEMENT_THRESHOLD,
    human_scale: str = "unit",
) -> CalibrationReport:
    """Score ``records`` with ``judge`` and report per-dimension judge↔human agreement.

    For each record the (injectable) ``judge`` re-scores the answer under the same
    anchored rubric, yielding a :class:`~bakeoff.types.JudgeScores`; for every
    judge dimension a human graded, the paired (human, judge-mean) series is
    collected across records and summarized by Spearman's ρ (plus Pearson and MAE
    for transparency). A dimension with ρ below ``threshold`` — or undefined — is
    flagged ``low_agreement`` so the composite can soften/exclude it (Req 13.2).

    **Reported, never gated (Req 14.4).** Poor agreement does not raise; the
    report simply surfaces it.

    Args:
        records: the human-labeled calibration items. May be raw dicts, which are
            coerced via ``human_scale`` (so a caller can pass parsed JSONL
            directly).
        judge: any object exposing ``score(answer_text, *, ideal_text, fragments,
            gold_texts, momentary_state, answerability) -> JudgeScores`` — e.g. a
            :class:`bakeoff.scoring.judge.JudgeScorer` (pass one wrapping the
            offline :class:`~bakeoff.scoring.judge.StubJudge` for tests).
        threshold: agreement cut for the low-agreement flag (default
            :data:`DEFAULT_AGREEMENT_THRESHOLD`).
        human_scale: scale of any raw-dict ``human_scores`` (``"unit"`` | ``"1-5"``).

    Returns:
        A :class:`CalibrationReport`.
    """
    recs = [_coerce_record(r, human_scale) for r in records]

    # Collect paired (human, judge_mean) per dimension across the set.
    human_by_dim: dict[str, list[float]] = {dim: [] for dim in JUDGE_DIMENSIONS}
    judge_by_dim: dict[str, list[float]] = {dim: [] for dim in JUDGE_DIMENSIONS}
    judge_model = getattr(judge, "judge_model", "unknown-judge")

    for rec in recs:
        scores = judge.score(
            rec.answer,
            ideal_text=rec.ideal_text,
            fragments=list(rec.fragments),
            gold_texts=list(rec.gold_texts),
            momentary_state=rec.momentary_state,
            answerability=rec.answerability,
        )
        judge_model = getattr(scores, "judge_model", judge_model)
        judge_dim_values = _judge_scores_as_dict(scores)
        for dim in JUDGE_DIMENSIONS:
            if dim in rec.human_scores:
                human_by_dim[dim].append(float(rec.human_scores[dim]))
                judge_by_dim[dim].append(float(judge_dim_values[dim]))

    per_dimension: dict[str, DimensionAgreement] = {}
    for dim in JUDGE_DIMENSIONS:
        humans = human_by_dim[dim]
        judges = judge_by_dim[dim]
        if not humans:
            continue  # no human labels for this dimension → not reported
        rho = spearman_rho(humans, judges)
        pear = _pearson(humans, judges)
        mae = float(np.mean(np.abs(np.asarray(humans) - np.asarray(judges))))
        # Low-agreement iff undefined OR below threshold (we cannot defend it).
        low = rho is None or rho < threshold
        per_dimension[dim] = DimensionAgreement(
            dimension=dim,
            agreement=rho,
            pearson=pear,
            mae=mae,
            n=len(humans),
            judge_mean=float(np.mean(judges)),
            human_mean=float(np.mean(humans)),
            low_agreement=low,
        )

    return CalibrationReport(
        agreement_metric=AGREEMENT_METRIC,
        threshold=float(threshold),
        n_items=len(recs),
        judge_model=judge_model,
        per_dimension=per_dimension,
    )


def _coerce_record(rec, human_scale: str) -> CalibrationRecord:
    """Accept a :class:`CalibrationRecord` or a raw dict (coerce the latter)."""
    if isinstance(rec, CalibrationRecord):
        return rec
    if isinstance(rec, Mapping):
        return _record_from_dict(rec, human_scale)
    raise TypeError(
        f"calibration record must be a CalibrationRecord or mapping, got {type(rec)!r}"
    )


def _judge_scores_as_dict(scores) -> dict[str, float]:
    """Read a :class:`~bakeoff.types.JudgeScores` into a ``{dimension: value}`` dict."""
    return {dim: float(getattr(scores, dim)) for dim in JUDGE_DIMENSIONS}
