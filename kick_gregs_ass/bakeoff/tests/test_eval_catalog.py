"""
Unit tests for :mod:`bakeoff.eval.catalog` (Task 2.2).

Covers the metric-catalog-as-data contract (Req 4):

* out-of-scope entries are absent from ``default_enabled()`` (Req 4.3, 4.4);
* the in-scope candidate families (Req 4.1) are prioritized ahead of the
  lower-priority in-scope metrics (Req 4.2), which precede the out-of-scope
  families (Req 4.3);
* every entry carries ``external is True`` (Req 4.6 / P13);
* the specific Req 4.1/4.2/4.3 metrics and family markings are present.

Pure example-based tests, network-free.
"""
from __future__ import annotations

import pytest

from bakeoff.eval import catalog
from bakeoff.eval.catalog import (
    CATALOG,
    IN_SCOPE_FAMILIES,
    OUT_OF_SCOPE_FAMILIES,
    MetricCatalogEntry,
)


# ---------------------------------------------------------------------------
# out-of-scope excluded from the default enabled set (Req 4.3, 4.4)
# ---------------------------------------------------------------------------
def test_default_enabled_excludes_every_out_of_scope_entry():
    enabled = catalog.default_enabled()
    assert enabled, "default enabled set must be non-empty"
    assert all(e.scope == "in" for e in enabled)
    # no out-of-scope entry leaks into the default enabled set.
    assert not any(e.scope == "out" for e in enabled)


def test_default_enabled_names_excludes_out_of_scope_names():
    names = set(catalog.default_enabled_names())
    out_names = {e.name for e in catalog.out_of_scope()}
    assert names.isdisjoint(out_names)
    # and the in-scope names are exactly the default enabled names.
    assert names == {e.name for e in catalog.in_scope()}


def test_out_of_scope_families_are_marked_and_present(
):
    out = catalog.out_of_scope()
    assert out, "the catalog must include out-of-scope candidates (Req 4.3)"
    out_families = {e.family for e in out}
    # multimodal, agent/tool, and SQL are the families marked out of scope.
    assert out_families == OUT_OF_SCOPE_FAMILIES == {"multimodal", "agentic", "sql"}
    for entry in out:
        assert entry.scope == "out"
        assert entry.enabled_by_default is False


# ---------------------------------------------------------------------------
# priority ordering: 4.1 candidates before 4.2 metrics before 4.3 out-of-scope
# ---------------------------------------------------------------------------
def test_in_scope_candidate_families_are_prioritized_first():
    ordered = catalog.catalog_by_priority()
    # map each family to the worst (largest) priority value it appears at.
    worst = {}
    best = {}
    for e in ordered:
        worst[e.family] = max(worst.get(e.family, e.priority), e.priority)
        best[e.family] = min(best.get(e.family, e.priority), e.priority)

    # Req 4.1 families (rag, nvidia, nl-comparison) all sort ahead of the Req 4.2
    # lower-priority in-scope families (traditional, general).
    candidate_worst = max(worst["rag"], worst["nvidia"], worst["nl-comparison"])
    lower_best = min(best["traditional"], best["general"])
    assert candidate_worst < lower_best

    # Req 4.2 in-scope metrics sort ahead of every Req 4.3 out-of-scope family.
    lower_worst = max(worst["traditional"], worst["general"])
    out_best = min(best[f] for f in OUT_OF_SCOPE_FAMILIES)
    assert lower_worst < out_best


def test_catalog_by_priority_is_sorted_nondecreasing():
    ordered = catalog.catalog_by_priority()
    priorities = [e.priority for e in ordered]
    assert priorities == sorted(priorities)


def test_out_of_scope_entries_have_lowest_priority():
    in_scope_max = max(e.priority for e in catalog.in_scope())
    out_min = min(e.priority for e in catalog.out_of_scope())
    assert in_scope_max < out_min


# ---------------------------------------------------------------------------
# every entry is external methodology (Req 4.6 / P13)
# ---------------------------------------------------------------------------
def test_every_catalog_entry_is_external():
    assert CATALOG, "catalog must not be empty"
    for entry in CATALOG:
        assert entry.external is True


def test_entry_rejects_non_external():
    with pytest.raises(ValueError, match="external=True"):
        MetricCatalogEntry("x", "rag", "in", 0, external=False)


# ---------------------------------------------------------------------------
# the specific Req 4.1 / 4.2 metrics are present in the right families
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name, family",
    [
        # RAG family (Req 4.1)
        ("context_precision", "rag"),
        ("context_recall", "rag"),
        ("context_entities_recall", "rag"),
        ("noise_sensitivity", "rag"),
        ("response_relevancy", "rag"),
        ("faithfulness", "rag"),
        # Nvidia family (Req 4.1)
        ("answer_accuracy", "nvidia"),
        ("context_relevance", "nvidia"),
        ("response_groundedness", "nvidia"),
        # natural-language-comparison family (Req 4.1)
        ("factual_correctness", "nl-comparison"),
        ("semantic_similarity", "nl-comparison"),
        # traditional non-LLM (Req 4.2)
        ("bleu_score", "traditional"),
        ("rouge_score", "traditional"),
        ("chrf_score", "traditional"),
        ("string_presence", "traditional"),
        ("exact_match", "traditional"),
        # general-purpose (Req 4.2)
        ("aspect_critic", "general"),
        ("simple_criteria", "general"),
        ("rubrics_score", "general"),
        ("instance_rubrics", "general"),
    ],
)
def test_in_scope_metric_present_with_family(name, family):
    entry = catalog.get(name)
    assert entry.family == family
    assert entry.scope == "in"
    assert entry.family in IN_SCOPE_FAMILIES
    assert catalog.is_enabled_by_default(name) is True


def test_lookup_unknown_metric_raises_keyerror():
    with pytest.raises(KeyError):
        catalog.get("not_a_real_metric")


# ---------------------------------------------------------------------------
# catalog self-consistency: family/scope agreement, unique names (Req 4.5 data)
# ---------------------------------------------------------------------------
def test_family_and_scope_agree_for_every_entry():
    for e in CATALOG:
        if e.family in OUT_OF_SCOPE_FAMILIES:
            assert e.scope == "out"
        else:
            assert e.scope == "in"


def test_catalog_names_are_unique():
    names = [e.name for e in CATALOG]
    assert len(names) == len(set(names))


def test_entry_rejects_inconsistent_family_scope():
    # an in-scope family must not be marked out, and vice versa.
    with pytest.raises(ValueError, match="disagree"):
        MetricCatalogEntry("faith2", "rag", "out", 0)
    with pytest.raises(ValueError, match="disagree"):
        MetricCatalogEntry("mm2", "multimodal", "in", 0)
