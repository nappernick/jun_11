"""bakeoff: model-agnostic reranker eval harness contract and sample data."""
from bakeoff.contract import (  # noqa: F401
    Candidate, Fixture, RankedDoc, Reranker, AbstainClass,
    ScoredRow, AbstainPoint, ModelMeta, Gates,
    results_to_json, results_from_json,
)
