"""bakeoff.contract — shared types for the reranker eval harness.

All downstream agents (metrics, harness, decide, dashboard) import from here.
Pure stdlib, Python 3.12+.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal, Protocol, Sequence


# ---------------------------------------------------------------------------
# Core types
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Candidate:
    """A document candidate presented to a reranker."""
    node_id: str
    text: str
    source_metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"node_id": self.node_id, "text": self.text, "source_metadata": self.source_metadata}

    @classmethod
    def from_dict(cls, d: dict) -> "Candidate":
        return cls(node_id=d["node_id"], text=d["text"], source_metadata=d.get("source_metadata", {}))


@dataclass(slots=True)
class Fixture:
    """A frozen eval query with known-good answers and slice metadata.

    answerability: one of 'unanswerable', 'answerable_retrievable', 'answerable_not_retrieved'.
    """
    query_id: str
    query: str
    gold_node_ids: set[str]
    candidates: list[Candidate]
    slice: dict[str, str]
    answerability: str

    def to_dict(self) -> dict:
        return {
            "query_id": self.query_id, "query": self.query,
            "gold_node_ids": sorted(self.gold_node_ids),
            "candidates": [c.to_dict() for c in self.candidates],
            "slice": self.slice, "answerability": self.answerability,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Fixture":
        return cls(
            query_id=d["query_id"], query=d["query"],
            gold_node_ids=set(d["gold_node_ids"]),
            candidates=[Candidate.from_dict(c) for c in d["candidates"]],
            slice=d["slice"], answerability=d["answerability"],
        )


@dataclass(slots=True)
class RankedDoc:
    """A single document in a reranker's output list.

    rank: 0-indexed (0 = best).
    raw_score: model-native score (unbounded).
    norm_score: comparable [0,1] score via normalize.squash or PlattCalibrator.
    """
    node_id: str
    rank: int
    raw_score: float
    norm_score: float

    def to_dict(self) -> dict:
        return {"node_id": self.node_id, "rank": self.rank,
                "raw_score": self.raw_score, "norm_score": self.norm_score}

    @classmethod
    def from_dict(cls, d: dict) -> "RankedDoc":
        return cls(node_id=d["node_id"], rank=d["rank"],
                   raw_score=d["raw_score"], norm_score=d["norm_score"])


class Reranker(Protocol):
    """Protocol that every reranker adapter must satisfy.

    Contract:
      - id: unique string identifying the model (e.g. 'cohere-rerank-v3.5').
      - rerank(): returns top_k RankedDocs sorted by descending relevance.
        - rank 0 = best document.
        - raw_score: model-native score (logit, probability, etc.).
        - norm_score: comparable [0,1] via normalize module.
        - NEVER throw on a bad/corrupt document — score it low (0.0 norm).
        - Transport failures (network, timeout, 5xx) MAY throw.
    """
    @property
    def id(self) -> str: ...

    def rerank(self, query: str, candidates: list[Candidate], top_k: int) -> list[RankedDoc]: ...


# ---------------------------------------------------------------------------
# Abstain classification
# ---------------------------------------------------------------------------

AbstainClass = Literal["unanswerable", "answerable_retrievable", "answerable_not_retrieved"]


# ---------------------------------------------------------------------------
# Scoring / results types
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ScoredRow:
    """One model × one query evaluation row."""
    model_id: str
    query_id: str
    slice: dict[str, str]
    latency_ms: float
    rels: list[int]
    gold_total: int
    gold_retrievable: int
    abstain_class: str
    expect_abstain: bool
    top_norm: float

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id, "query_id": self.query_id,
            "slice": self.slice, "latency_ms": self.latency_ms,
            "rels": self.rels, "gold_total": self.gold_total,
            "gold_retrievable": self.gold_retrievable,
            "abstain_class": self.abstain_class,
            "expect_abstain": self.expect_abstain, "top_norm": self.top_norm,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ScoredRow":
        return cls(
            model_id=d["model_id"], query_id=d["query_id"], slice=d["slice"],
            latency_ms=d["latency_ms"], rels=d["rels"],
            gold_total=d["gold_total"], gold_retrievable=d["gold_retrievable"],
            abstain_class=d["abstain_class"], expect_abstain=d["expect_abstain"],
            top_norm=d["top_norm"],
        )


@dataclass(slots=True)
class AbstainPoint:
    """One point on the abstention operating-characteristic curve."""
    t: float
    abstain_recall: float
    false_answer_rate: float
    false_abstain_rate: float

    def to_dict(self) -> dict:
        return {"t": self.t, "abstain_recall": self.abstain_recall,
                "false_answer_rate": self.false_answer_rate,
                "false_abstain_rate": self.false_abstain_rate}

    @classmethod
    def from_dict(cls, d: dict) -> "AbstainPoint":
        return cls(t=d["t"], abstain_recall=d["abstain_recall"],
                   false_answer_rate=d["false_answer_rate"],
                   false_abstain_rate=d["false_abstain_rate"])


@dataclass(slots=True)
class ModelMeta:
    """Metadata about a model under evaluation."""
    id: str
    display_name: str
    params: str
    max_seq_len: int
    deploy_path: str
    license: str
    instruction_following: bool
    calibrated_scores: bool

    def to_dict(self) -> dict:
        return {
            "id": self.id, "display_name": self.display_name,
            "params": self.params, "max_seq_len": self.max_seq_len,
            "deploy_path": self.deploy_path, "license": self.license,
            "instruction_following": self.instruction_following,
            "calibrated_scores": self.calibrated_scores,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ModelMeta":
        return cls(
            id=d["id"], display_name=d["display_name"], params=d["params"],
            max_seq_len=d["max_seq_len"], deploy_path=d["deploy_path"],
            license=d["license"], instruction_following=d["instruction_following"],
            calibrated_scores=d["calibrated_scores"],
        )


@dataclass(slots=True)
class Gates:
    """Pass/fail thresholds for the DECIDE stage."""
    accuracy_bar: float
    latency_budget_ms: float
    false_answer_ceiling: float

    def to_dict(self) -> dict:
        return {"accuracy_bar": self.accuracy_bar,
                "latency_budget_ms": self.latency_budget_ms,
                "false_answer_ceiling": self.false_answer_ceiling}

    @classmethod
    def from_dict(cls, d: dict) -> "Gates":
        return cls(accuracy_bar=d["accuracy_bar"],
                   latency_budget_ms=d["latency_budget_ms"],
                   false_answer_ceiling=d["false_answer_ceiling"])


# ---------------------------------------------------------------------------
# ResultsFile JSON round-trip
# ---------------------------------------------------------------------------
# Shape:
# {
#   "run_id": str,
#   "gates": Gates,
#   "baseline_model_id": str,
#   "models": [ModelMeta],
#   "cells": [{
#     "model_id": str, "N": int,
#     "by_slice": {<slice_name>: {
#       "ndcg10": float, "ndcg10_ci": [lo, hi],
#       "recall10": float, "mrr10": float,
#       "p50": float, "p95": float, "p99": float,
#       "throughput_qps": float, "cost_per_1k": float,
#       "sig_vs_baseline": float,
#       "abstain": {"operating_t": float, "recall": float,
#                   "false_answer_rate": float, "false_abstain_rate": float},
#       "abstain_curve": [AbstainPoint]
#     }},
#     "rows": [{"id": str, "slice": dict, "rels": [int], "top_norm": float, "latency": float}]
#   }]
# }


def results_to_json(results: dict) -> str:
    """Serialize a ResultsFile dict (with dataclass values) to JSON string."""
    def _encode(obj):
        if hasattr(obj, "to_dict"):
            return obj.to_dict()
        raise TypeError(f"Cannot serialize {type(obj)}")

    out = {
        "run_id": results["run_id"],
        "gates": results["gates"].to_dict() if hasattr(results["gates"], "to_dict") else results["gates"],
        "baseline_model_id": results["baseline_model_id"],
        "models": [m.to_dict() if hasattr(m, "to_dict") else m for m in results["models"]],
        "cells": [],
    }
    for cell in results["cells"]:
        c = {"model_id": cell["model_id"], "N": cell["N"], "by_slice": {}, "rows": cell["rows"]}
        for sname, sdata in cell["by_slice"].items():
            sd = dict(sdata)  # shallow copy
            if "abstain" in sd and hasattr(sd["abstain"], "to_dict"):
                sd["abstain"] = sd["abstain"].to_dict()
            if "abstain_curve" in sd:
                sd["abstain_curve"] = [
                    p.to_dict() if hasattr(p, "to_dict") else p for p in sd["abstain_curve"]
                ]
            c["by_slice"][sname] = sd
        out["cells"].append(c)
    return json.dumps(out, indent=2)


def results_from_json(text: str) -> dict:
    """Deserialize a ResultsFile JSON string back into typed objects."""
    raw = json.loads(text)
    results: dict = {
        "run_id": raw["run_id"],
        "gates": Gates.from_dict(raw["gates"]),
        "baseline_model_id": raw["baseline_model_id"],
        "models": [ModelMeta.from_dict(m) for m in raw["models"]],
        "cells": [],
    }
    for cell in raw["cells"]:
        c: dict = {"model_id": cell["model_id"], "N": cell["N"], "by_slice": {}, "rows": cell["rows"]}
        for sname, sdata in cell["by_slice"].items():
            sd = dict(sdata)
            if "abstain" in sd and isinstance(sd["abstain"], dict):
                sd["abstain"] = sd["abstain"]  # keep as plain dict for lightweight access
            if "abstain_curve" in sd:
                sd["abstain_curve"] = [AbstainPoint.from_dict(p) for p in sd["abstain_curve"]]
            c["by_slice"][sname] = sd
        results["cells"].append(c)
    return results
