"""
The ragas metric catalog as **data**, not code (Req 4).

This module encodes the dashboard's metric menu as a flat, immutable list of
:class:`MetricCatalogEntry` value objects. Encoding the catalog as data (rather
than as branching code) is what lets the catalog **grow without code changes**
(Req 4.5) and lets the in-scope / out-of-scope marking be a queried property
rather than a hard-coded conditional.

What the catalog must express (Req 4):

* **In-scope, prioritized first (Req 4.1).** The RAG family (Context Precision,
  Context Recall, Context Entities Recall, Noise Sensitivity, Response
  Relevancy, Faithfulness), the Nvidia family (Answer Accuracy, Context
  Relevance, Response Groundedness), and the natural-language-comparison family
  (Factual Correctness, Semantic Similarity).
* **In-scope, lower priority (Req 4.2).** The traditional non-LLM metrics (BLEU,
  ROUGE, CHRF, string-presence, exact-match) and the general-purpose metrics
  (Aspect Critic, Simple Criteria, Rubrics-based, Instance-specific rubrics).
* **Out of scope for a RAG harness (Req 4.3).** The multimodal family, the
  agent/tool family, and the SQL family — present in the catalog but marked
  ``scope="out"`` so they are excluded from the default enabled set (Req 4.4)
  and rendered as out-of-scope by the app.
* **External methodology (Req 4.6 / Property 13).** Every entry carries
  ``external=True``: ragas, NDCG, and the composite are general-industry
  methodology, never Amazon-internal guidance.

Priority is an **ordinal where a smaller number sorts earlier** (priority 0 is
the most prioritized). The three in-scope candidate families (Req 4.1) all sort
ahead of the lower-priority in-scope metrics (Req 4.2), which in turn sort ahead
of the out-of-scope families (Req 4.3). :func:`catalog_by_priority` returns the
menu in that order; :func:`default_enabled` returns the in-scope entries that
make up the default enabled set.

Pure standard library (``dataclasses``); no network, no third-party deps. Mirrors
the design's ``MetricCatalogEntry`` shape (design Data Models) on the Python
producer side so the TypeScript ``api/types.ts`` entry is a 1:1 projection.
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "Family",
    "Scope",
    "MetricCatalogEntry",
    "CATALOG",
    "FAMILIES",
    "IN_SCOPE_FAMILIES",
    "OUT_OF_SCOPE_FAMILIES",
    "catalog_by_priority",
    "get",
    "in_scope",
    "out_of_scope",
    "default_enabled",
    "default_enabled_names",
    "is_enabled_by_default",
]

# Family + scope are open string literals (kept as plain ``str`` validated against
# the frozensets below) so the catalog can grow without touching an Enum.
Family = str
Scope = str

#: Every family the catalog knows about (Req 4.1, 4.2, 4.3).
FAMILIES: frozenset[str] = frozenset(
    {
        "rag",
        "nvidia",
        "nl-comparison",
        "traditional",
        "general",
        "multimodal",
        "agentic",
        "sql",
    }
)
#: Families that are in-scope candidates for a RAG harness (Req 4.1, 4.2).
IN_SCOPE_FAMILIES: frozenset[str] = frozenset(
    {"rag", "nvidia", "nl-comparison", "traditional", "general"}
)
#: Families explicitly marked likely out of scope for a RAG harness (Req 4.3).
OUT_OF_SCOPE_FAMILIES: frozenset[str] = frozenset({"multimodal", "agentic", "sql"})

_VALID_SCOPE: frozenset[str] = frozenset({"in", "out"})

# Priority bands (smaller sorts earlier). The Req 4.1 candidate families occupy
# the first three bands; the Req 4.2 lower-priority in-scope metrics the next
# two; the Req 4.3 out-of-scope families the last band.
_P_RAG = 0
_P_NVIDIA = 1
_P_NL = 2
_P_TRADITIONAL = 3
_P_GENERAL = 4
_P_OUT = 9


@dataclass(frozen=True)
class MetricCatalogEntry:
    """One catalog metric, as data (design Data Models / ``MetricCatalogEntry``).

    Fields:

    * ``name`` — the metric's stable id (snake_case; matches the ragas metric
      name used on :class:`~bakeoff.eval.models.MetricValue`).
    * ``family`` — one of :data:`FAMILIES`.
    * ``scope`` — ``"in"`` (an in-scope candidate, Req 4.1/4.2) or ``"out"``
      (likely out of scope for a RAG harness, Req 4.3). An out-of-scope entry is
      excluded from the default enabled set (Req 4.4).
    * ``priority`` — ordinal menu position; a smaller number sorts earlier
      (Req 4.1, 4.2).
    * ``customizable_prompt`` — whether the metric exposes an editable
      instruction + few-shot examples for the Prompt_Manager (Req 16); the
      non-LLM traditional metrics and the embedding-only similarity metric are
      not prompt-customizable.
    * ``external`` — always ``True``: every catalog metric is external/industry
      methodology, not Amazon-internal guidance (Req 4.6 / Property 13).
    """

    name: str
    family: Family
    scope: Scope
    priority: int
    customizable_prompt: bool = True
    external: bool = True

    def __post_init__(self) -> None:
        if self.family not in FAMILIES:
            raise ValueError(
                f"unknown family {self.family!r}; must be one of {sorted(FAMILIES)}"
            )
        if self.scope not in _VALID_SCOPE:
            raise ValueError(
                f"scope must be one of {sorted(_VALID_SCOPE)}, got {self.scope!r}"
            )
        # An out-of-scope family must be marked scope="out", and vice versa, so
        # the data can never disagree with itself (Req 4.3).
        if (self.family in OUT_OF_SCOPE_FAMILIES) != (self.scope == "out"):
            raise ValueError(
                f"family {self.family!r} and scope {self.scope!r} disagree: "
                f"out-of-scope families {sorted(OUT_OF_SCOPE_FAMILIES)} must have "
                'scope="out" and only those'
            )
        # Every catalog metric is external methodology (Req 4.6 / P13).
        if self.external is not True:
            raise ValueError("every catalog metric must carry external=True (Req 4.6)")

    @property
    def enabled_by_default(self) -> bool:
        """``True`` iff this entry is in the default enabled set (Req 4.4).

        The default enabled set is exactly the in-scope entries; an out-of-scope
        entry is never enabled by default.
        """
        return self.scope == "in"


# ---------------------------------------------------------------------------
# The catalog (data). Order here is incidental — callers sort by priority.
# ---------------------------------------------------------------------------
CATALOG: tuple[MetricCatalogEntry, ...] = (
    # --- RAG family: in-scope, prioritized first (Req 4.1) ---
    MetricCatalogEntry("context_precision", "rag", "in", _P_RAG),
    MetricCatalogEntry("context_recall", "rag", "in", _P_RAG),
    MetricCatalogEntry("context_entities_recall", "rag", "in", _P_RAG),
    MetricCatalogEntry("noise_sensitivity", "rag", "in", _P_RAG),
    MetricCatalogEntry("response_relevancy", "rag", "in", _P_RAG),
    MetricCatalogEntry("faithfulness", "rag", "in", _P_RAG),
    # --- Nvidia family: in-scope (Req 4.1) ---
    MetricCatalogEntry("answer_accuracy", "nvidia", "in", _P_NVIDIA),
    MetricCatalogEntry("context_relevance", "nvidia", "in", _P_NVIDIA),
    MetricCatalogEntry("response_groundedness", "nvidia", "in", _P_NVIDIA),
    # --- natural-language-comparison family: in-scope (Req 4.1) ---
    MetricCatalogEntry("factual_correctness", "nl-comparison", "in", _P_NL),
    # semantic_similarity is embedding-only -> no editable prompt.
    MetricCatalogEntry(
        "semantic_similarity", "nl-comparison", "in", _P_NL, customizable_prompt=False
    ),
    # --- traditional non-LLM metrics: in-scope, lower priority (Req 4.2) ---
    # Deterministic string math -> no prompt to customize.
    MetricCatalogEntry("bleu_score", "traditional", "in", _P_TRADITIONAL, customizable_prompt=False),
    MetricCatalogEntry("rouge_score", "traditional", "in", _P_TRADITIONAL, customizable_prompt=False),
    MetricCatalogEntry("chrf_score", "traditional", "in", _P_TRADITIONAL, customizable_prompt=False),
    MetricCatalogEntry("string_presence", "traditional", "in", _P_TRADITIONAL, customizable_prompt=False),
    MetricCatalogEntry("exact_match", "traditional", "in", _P_TRADITIONAL, customizable_prompt=False),
    # --- general-purpose metrics: in-scope, lower priority (Req 4.2) ---
    MetricCatalogEntry("aspect_critic", "general", "in", _P_GENERAL),
    MetricCatalogEntry("simple_criteria", "general", "in", _P_GENERAL),
    MetricCatalogEntry("rubrics_score", "general", "in", _P_GENERAL),
    MetricCatalogEntry("instance_rubrics", "general", "in", _P_GENERAL),
    # --- out of scope for a RAG harness (Req 4.3) ---
    MetricCatalogEntry("multimodal_faithfulness", "multimodal", "out", _P_OUT),
    MetricCatalogEntry("multimodal_relevance", "multimodal", "out", _P_OUT),
    MetricCatalogEntry("agent_goal_accuracy", "agentic", "out", _P_OUT),
    MetricCatalogEntry("tool_call_accuracy", "agentic", "out", _P_OUT),
    MetricCatalogEntry("topic_adherence", "agentic", "out", _P_OUT),
    MetricCatalogEntry("sql_query_equivalence", "sql", "out", _P_OUT),
    MetricCatalogEntry("datacompy_score", "sql", "out", _P_OUT),
)

# A name -> entry index, asserting names are unique (a duplicate would silently
# shadow in the lookup, so fail loudly at import time).
_BY_NAME: dict[str, MetricCatalogEntry] = {}
for _entry in CATALOG:
    if _entry.name in _BY_NAME:
        raise ValueError(f"duplicate catalog metric name: {_entry.name!r}")
    _BY_NAME[_entry.name] = _entry


# ---------------------------------------------------------------------------
# Queries over the catalog
# ---------------------------------------------------------------------------
def catalog_by_priority() -> list[MetricCatalogEntry]:
    """Return every catalog entry ordered as a prioritized menu (Req 4.1, 4.2).

    Sorted by ``(priority, name)`` so the in-scope candidate families come first,
    then the lower-priority in-scope metrics, then the out-of-scope families. The
    secondary ``name`` key makes the order fully deterministic within a band.
    """
    return sorted(CATALOG, key=lambda e: (e.priority, e.name))


def get(name: str) -> MetricCatalogEntry:
    """Return the catalog entry named ``name``.

    Raises:
        KeyError: if no catalog metric has that name.
    """
    return _BY_NAME[name]


def in_scope() -> list[MetricCatalogEntry]:
    """The in-scope entries (Req 4.1, 4.2), in priority order."""
    return [e for e in catalog_by_priority() if e.scope == "in"]


def out_of_scope() -> list[MetricCatalogEntry]:
    """The entries marked likely out of scope for a RAG harness (Req 4.3)."""
    return [e for e in catalog_by_priority() if e.scope == "out"]


def default_enabled() -> list[MetricCatalogEntry]:
    """The default enabled set: the in-scope entries, in priority order (Req 4.4).

    Out-of-scope entries (Req 4.3) are excluded — they are present in the catalog
    but never enabled by default (Req 4.4). Each in-scope metric can then be
    individually enabled/disabled for a run via configuration without code
    changes (Req 4.5), because the catalog is data and the engine takes the
    enabled set as input.
    """
    return in_scope()


def default_enabled_names() -> list[str]:
    """The names of the default enabled set (Req 4.4), in priority order."""
    return [e.name for e in default_enabled()]


def is_enabled_by_default(name: str) -> bool:
    """``True`` iff the named metric is in the default enabled set (Req 4.4)."""
    return _BY_NAME[name].enabled_by_default
