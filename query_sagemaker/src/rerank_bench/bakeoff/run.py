"""bakeoff.run — Stage 5 RENDER: wire harness → metrics → decide, emit ResultsFile.

Runs the MockReranker over sample fixtures across all sample slices and N values,
with no AWS / no labels / no network. Produces a valid ResultsFile JSON.

Usage:
    python -m bakeoff.run                # writes bakeoff/sample/sample_results.json
    python -m bakeoff.run path/out.json  # writes to custom path
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from bakeoff.contract import (
    AbstainPoint, Gates, ModelMeta, ScoredRow, results_to_json,
)
from bakeoff.decide import recommend
from bakeoff.harness import MockReranker, load_fixtures, run_model
from bakeoff.metrics import aggregate

SAMPLE_DIR = Path(__file__).parent / "sample"
FIXTURES_PATH = SAMPLE_DIR / "sample_fixtures.jsonl"

# Example gates for the sample run
DEFAULT_GATES = Gates(accuracy_bar=0.70, latency_budget_ms=500.0, false_answer_ceiling=0.05)

# MockReranker metadata
MOCK_META = ModelMeta(
    id="mock",
    display_name="MockReranker (token-overlap)",
    params="0",
    max_seq_len=999999,
    deploy_path="local/mock",
    license="internal",
    instruction_following=False,
    calibrated_scores=False,
)

# N values to sweep (the sample has 12 fixtures, use top_k values that exercise the data)
N_VALUES = [3, 5]


def build_slice_key(slice_dict: dict[str, str]) -> str:
    """Canonical slice key matching metrics.aggregate convention."""
    return "&".join(f"{k}={v}" for k, v in sorted(slice_dict.items()))


def build_results(fixtures_path: Path = FIXTURES_PATH, gates: Gates = DEFAULT_GATES) -> dict:
    """Run the full pipeline and return a ResultsFile dict."""
    fixtures = load_fixtures(fixtures_path)
    reranker = MockReranker()

    cells: list[dict] = []
    for n in N_VALUES:
        # Stage 1 RERANK + Stage 2 SCORE
        rows: list[ScoredRow] = run_model(reranker, fixtures, top_k=n)

        # Stage 3 AGGREGATE (model is its own baseline for the mock)
        by_slice = aggregate(rows, baseline_rows=rows, k=10)

        # Convert AbstainPoint dataclasses in abstain_curve to dicts for serialization
        for slice_data in by_slice.values():
            if "abstain_curve" in slice_data:
                slice_data["abstain_curve"] = [
                    p if isinstance(p, dict) else p
                    for p in slice_data["abstain_curve"]
                ]
            # Remove three_way_counts — not in ResultsFile contract
            slice_data.pop("three_way_counts", None)

        # Build rows for drill-down
        row_dicts = [
            {
                "id": r.query_id,
                "slice": r.slice,
                "rels": r.rels,
                "top_norm": r.top_norm,
                "latency": r.latency_ms,
            }
            for r in rows
        ]

        cells.append({
            "model_id": reranker.id,
            "N": n,
            "by_slice": by_slice,
            "rows": row_dicts,
        })

    # Stage 4 DECIDE — run recommend for each slice in the first cell
    first_cell = cells[0] if cells else None
    recommendation = None
    if first_cell:
        for slice_name in first_cell["by_slice"]:
            rec = recommend(cells, slice_name, gates)
            if rec:
                recommendation = rec
                break

    return {
        "run_id": f"mock-sample-N{'-'.join(str(n) for n in N_VALUES)}",
        "gates": gates,
        "baseline_model_id": reranker.id,
        "models": [MOCK_META],
        "cells": cells,
    }


def main() -> None:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else SAMPLE_DIR / "sample_results.json"
    results = build_results()
    json_str = results_to_json(results)
    out_path.write_text(json_str + "\n")
    print(f"Wrote {out_path} ({len(json_str)} bytes)")


if __name__ == "__main__":
    main()
