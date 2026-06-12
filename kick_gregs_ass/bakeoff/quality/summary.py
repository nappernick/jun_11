"""
Aggregation of quality outcomes into the shape the dashboard's Quality tab reads.

Pure, deterministic rollup over the recorded :class:`QualityOutcome`s: per model,
the per-turn-POSITION mean closeness (turn 1, turn 2, …), the gold/wants/abstention
breakdown, the overall mean, and a few representative example conversations. The
turn-position curve is the headline of this study — it shows whether (and how
fast) each model's answers drift away from the correct answer as a conversation
goes deeper (the conversational feed-forward compounding).

No I/O beyond reading the outcomes store; returns plain JSON-serializable dicts so
it drops straight into a FastAPI response. Insufficient-data honesty: a
turn-position with too few samples for a meaningful mean is marked rather than
shown as a confident number (mirrors the bake-off's thin-cell discipline).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional, Sequence

from bakeoff import config
from bakeoff.quality.types import GroundTruthKind, QualityOutcome, read_outcomes

__all__ = [
    "MIN_SAMPLES_FOR_TURN_MEAN",
    "summarize_quality",
]

#: A turn-position mean is only reported when at least this many turn samples
#: back it; thinner positions are marked insufficient-data rather than shown as a
#: confident value (the bake-off's thin-cell honesty, applied per turn-position).
MIN_SAMPLES_FOR_TURN_MEAN: int = 2


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarize_quality(
    outcomes: Optional[Sequence[QualityOutcome]] = None,
    *,
    outcomes_path=None,
    examples_per_model: int = 3,
) -> dict:
    """Roll quality outcomes up into the dashboard's Quality-tab shape.

    Per model:
    * ``turn_closeness`` — list of ``{turn, mean, n, insufficient_data}`` giving
      the mean composite closeness at each turn position (the drift curve);
    * ``overall_mean`` — mean composite across all turns;
    * ``turn1_mean`` / ``later_mean`` — split so the gold-anchored turn-1 number is
      never silently averaged with the wants-anchored later turns;
    * ``ground_truth_counts`` — how many turns were scored against gold / wants /
      abstention;
    * ``judged_fraction`` — fraction of (non-abstention) turns that have a judge
      verdict folded in (so the UI can show "Phase-2 N% complete");
    * ``examples`` — a few representative conversations (best/median/worst by mean
      closeness) with each turn's answer excerpt + closeness, so the view can show
      what a good vs drifting conversation actually looks like.

    ``outcomes_path`` defaults to ``config.QUALITY_OUTCOMES_PATH`` resolved AT CALL
    TIME (not bound at import), so monkeypatching the config path in a test — or
    pointing the study at a different store at runtime — is honored.
    """
    if outcomes is None:
        if outcomes_path is None:
            outcomes_path = config.QUALITY_OUTCOMES_PATH
        outcomes = read_outcomes(outcomes_path)
    # Only successful outcomes feed the rollup.
    outcomes = [o for o in outcomes if o.error is None]

    by_model: dict[str, list[QualityOutcome]] = defaultdict(list)
    for o in outcomes:
        by_model[o.model].append(o)

    models_out: list[dict] = []
    for model in sorted(by_model):
        recs = by_model[model]
        # turn-position -> list of composite closeness
        by_turn: dict[int, list[float]] = defaultdict(list)
        turn1_vals: list[float] = []
        later_vals: list[float] = []
        gt_counts: dict[str, int] = defaultdict(int)
        judged = 0
        judgeable = 0
        all_composites: list[float] = []
        for o in recs:
            for t in o.turns:
                by_turn[t.turn].append(t.closeness.composite)
                all_composites.append(t.closeness.composite)
                gt_counts[t.closeness.ground_truth_kind] += 1
                if t.turn == 1:
                    turn1_vals.append(t.closeness.composite)
                else:
                    later_vals.append(t.closeness.composite)
                if t.closeness.ground_truth_kind != GroundTruthKind.ABSTENTION:
                    judgeable += 1
                    if t.closeness.judge is not None:
                        judged += 1

        turn_closeness = []
        for turn in sorted(by_turn):
            vals = by_turn[turn]
            insufficient = len(vals) < MIN_SAMPLES_FOR_TURN_MEAN
            turn_closeness.append(
                {
                    "turn": turn,
                    "mean": None if insufficient else _mean(vals),
                    "n": len(vals),
                    "insufficient_data": insufficient,
                }
            )

        models_out.append(
            {
                "model": model,
                "n_outcomes": len(recs),
                "overall_mean": _mean(all_composites),
                "turn1_mean": _mean(turn1_vals),
                "later_mean": _mean(later_vals),
                "turn_closeness": turn_closeness,
                "ground_truth_counts": dict(gt_counts),
                "judged_fraction": (judged / judgeable) if judgeable else 0.0,
                "examples": _examples(recs, examples_per_model),
            }
        )

    return {
        "n_outcomes": len(outcomes),
        "models": models_out,
        "min_samples_for_turn_mean": MIN_SAMPLES_FOR_TURN_MEAN,
    }


def _outcome_mean(o: QualityOutcome) -> float:
    vals = [t.closeness.composite for t in o.turns]
    return _mean(vals)


def _examples(recs: Sequence[QualityOutcome], n: int) -> list[dict]:
    """Best/median/worst conversations by mean closeness, with per-turn detail."""
    ranked = sorted(recs, key=_outcome_mean)
    if not ranked:
        return []
    idxs = sorted({0, len(ranked) - 1, len(ranked) // 2})
    picks = [ranked[i] for i in idxs][:n]
    return [
        {
            "trial_id": o.trial_id,
            "item_id": o.item_id,
            "rep": o.rep,
            "prompt_variant_id": o.prompt_variant_id,
            "mean_closeness": _outcome_mean(o),
            "turns": [
                {
                    "turn": t.turn,
                    "ground_truth_kind": t.closeness.ground_truth_kind,
                    "answerability": t.answerability,
                    "response_dependent": t.response_dependent,
                    "semantic": t.closeness.semantic,
                    "judge": t.closeness.judge,
                    "composite": t.closeness.composite,
                    "answer_excerpt": (t.answer_text or "")[:300],
                    "reference_excerpt": (t.reference_text or "")[:300],
                }
                for t in o.turns
            ],
        }
        for o in picks
    ]
