#!/usr/bin/env python3
"""norm_score: map model-native rerank scores to a comparable [0,1].

Honest scope (per the bakeoff plan): cross-model comparability is APPROXIMATE.
This is for reading *trends* and driving the abstention gate, not a literal 1:1
mapping between, say, an Ettin logit and a Cohere relevance score.

Two tiers:
  Tier A  squash(raw, kind)      -- label-free. Gets every model onto a monotonic,
                                    absolute [0,1] using a fixed function of the raw
                                    score. Enough for within-model abstention curves.
  Tier B  PlattCalibrator        -- fit per model on the labeled (raw, rel) pairs so
                                    norm_score ~= P(relevant). Makes a threshold
                                    semantically meaningful and curves roughly
                                    cross-model comparable.

Three rules this module enforces, because getting them wrong invalidates abstention:
  1. NEVER per-query normalize (no min-max over a candidate set). The top doc would be
     ~1.0 on every query and the "is the best good enough?" signal vanishes. Every
     transform here is a function of the raw score ALONE.
  2. Monotonic: norm is non-decreasing in raw, so it never reorders a query.
  3. Cross-model abstention comparison is done at MATCHED operating points (e.g. a
     fixed false-answer-rate), never a shared raw threshold. Calibration only makes
     that comparison fair; it does not make raw scales identical.

No third-party deps (pure stdlib) so it stays a trivially testable rigor-core unit.
"""
from __future__ import annotations
import math
from typing import Sequence


def sigmoid(x: float) -> float:
    # numerically stable
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


# --- Tier A: label-free squash -------------------------------------------------
# kind describes the model's native score space:
#   "unit"   already in [0,1] (Cohere relevance_score) -> clamp, identity
#   "logit"  unbounded real logit (Ettin, nemotron, gte seq-cls) -> sigmoid
#   "margin" yes/no token logit pair passed as (logit_yes - logit_no) -> sigmoid
#            (Qwen3-Reranker native form: P(yes) = sigmoid(yes - no))
def squash(raw: float, kind: str = "logit") -> float:
    if kind == "unit":
        return min(1.0, max(0.0, float(raw)))
    if kind in ("logit", "margin"):
        return sigmoid(float(raw))
    raise ValueError(f"unknown score kind: {kind!r}")


# --- Tier B: per-model Platt scaling ------------------------------------------
# norm = sigmoid(a * raw + b), with (a, b) fit by logistic regression on labeled
# (raw_score, rel in {0,1}) pairs. Raw scores are standardized internally for
# stable fitting, then folded back into (a, b) on the original raw scale so the
# learned calibrator is a plain function of the raw score (rule #1 preserved).
class PlattCalibrator:
    def __init__(self, a: float = 1.0, b: float = 0.0, kind: str = "logit"):
        self.a = a
        self.b = b
        self.kind = kind  # recorded for provenance; fit operates on raw directly

    def __call__(self, raw: float) -> float:
        return sigmoid(self.a * float(raw) + self.b)

    @classmethod
    def fit(cls, raws: Sequence[float], labels: Sequence[int],
            iters: int = 2000, lr: float = 0.1, kind: str = "logit") -> "PlattCalibrator":
        n = len(raws)
        if n == 0 or len(labels) != n:
            raise ValueError("raws and labels must be non-empty and equal length")
        if len(set(labels)) < 2:
            # Degenerate: can't calibrate with one class. Fall back to Tier-A squash
            # (a=1,b=0 => sigmoid(raw)); caller should treat as uncalibrated.
            return cls(a=1.0, b=0.0, kind=kind)

        # standardize for conditioning
        mu = sum(raws) / n
        var = sum((x - mu) ** 2 for x in raws) / n
        sd = math.sqrt(var) if var > 1e-12 else 1.0
        xs = [(x - mu) / sd for x in raws]

        # logistic regression on standardized x: p = sigmoid(w*x + c)
        w, c = 0.0, 0.0
        for _ in range(iters):
            gw = gc = 0.0
            for x, y in zip(xs, labels):
                p = sigmoid(w * x + c)
                err = p - y
                gw += err * x
                gc += err
            w -= lr * gw / n
            c -= lr * gc / n

        # fold standardization back: w*((raw-mu)/sd)+c = (w/sd)*raw + (c - w*mu/sd)
        a = w / sd
        b = c - w * mu / sd
        return cls(a=a, b=b, kind=kind)

    def to_dict(self) -> dict:
        return {"a": self.a, "b": self.b, "kind": self.kind, "method": "platt"}

    @classmethod
    def from_dict(cls, d: dict) -> "PlattCalibrator":
        return cls(a=d.get("a", 1.0), b=d.get("b", 0.0), kind=d.get("kind", "logit"))


def normalize_scores(raws: Sequence[float], kind: str = "logit",
                     calibrator: "PlattCalibrator | None" = None) -> list[float]:
    """Map a list of raw scores to norm_score in [0,1]. Uses the calibrator if given
    (Tier B), else the label-free squash (Tier A). Operates per-score, never per-query."""
    if calibrator is not None:
        return [calibrator(r) for r in raws]
    return [squash(r, kind) for r in raws]


if __name__ == "__main__":
    # Smoke test: synthetic relevant/irrelevant logits, confirm calibration improves
    # separation and stays monotonic.
    import random
    random.seed(0)
    pos = [random.gauss(3.0, 2.0) for _ in range(200)]   # relevant: higher logits
    neg = [random.gauss(-1.0, 2.0) for _ in range(200)]  # irrelevant: lower logits
    raws = pos + neg
    labels = [1] * len(pos) + [0] * len(neg)

    cal = PlattCalibrator.fit(raws, labels, kind="logit")
    print("fitted Platt:", cal.to_dict())

    # monotonic check
    grid = [-5, -2, 0, 2, 5]
    vals = [cal(x) for x in grid]
    assert all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1)), "not monotonic!"
    print("monotonic over", grid, "->", [round(v, 3) for v in vals])

    # mean norm should be much higher for positives than negatives after calibration
    mp = sum(cal(x) for x in pos) / len(pos)
    mn = sum(cal(x) for x in neg) / len(neg)
    print(f"mean norm  positives={mp:.3f}  negatives={mn:.3f}  separation={mp - mn:.3f}")

    # Tier-A squash still works with no labels
    print("squash(unit, 0.73) =", round(squash(0.73, "unit"), 3))
    print("squash(logit, 2.0) =", round(squash(2.0, "logit"), 3))
    print("OK")
