"""
Dataset loader and cohort normalization for the model-bakeoff-harness (Req 1).

Reads the synthetic corpus under ``data/synthetic/`` and normalizes every record
— single-turn queries (``queries.jsonl``) and multi-turn conversation sets
(``conversations.jsonl``) — into uniform :class:`bakeoff.types.Item` objects so
the runner, scorers, and aggregation engine treat all items identically and can
slice by every cohort dimension.

Design ties (see ``design.md`` "Component 1: Dataset loader" and Requirement 1):

* **One uniform Item.** Single-turn and multi-turn collapse to the same shape; a
  multi-turn item keeps its ordered :class:`bakeoff.types.ItemTurn`s and uses its
  turn-1 query as the focal query for the constant retrieval call (design AD-2).
* **Cohort vector from the LEDGER, not the persona string (Req 1.2/1.3).** The
  persona free-text varies ("Nigeria(Lagos)" vs "Nigeria (Lagos)", and some
  batches carry a dashed slug like "ar-buenosaires-near-native-deadpan-literal").
  The authoritative geography/proficiency/tone come from
  ``perspectives_ledger.jsonl`` joined by the integer ``batch`` key
  (``origin`` -> geography, ``proficiency``, ``disposition`` -> tone). Parsing the
  persona string is a *fallback* used only when the ledger lacks the batch.
* **Gold-link integrity is enforced (Req 1.5).** Every ``gold_node_id`` referenced
  by any item MUST resolve in ``corpus_index.tsv``; if any does not, loading fails
  loudly with a :class:`GoldIntegrityError` listing the offending id(s) (mirrors
  PROGRESS.md's "0 invalid gold nodeIds").
* **No hard-coded dataset sizes (Req 1.7).** The loader operates on whatever
  counts exist at run time.

Multi-turn gold (``conversation_turn1_gold.jsonl``) is keyed by ``set_id`` at
``turn == 1``. Not every conversation has a gold row; conversations with no
matching gold row are handled gracefully (answerability ``"none"``, empty gold)
and counted in :attr:`DatasetLoader.conversations_without_gold` rather than
crashing the load.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable, Optional

from bakeoff import config
from bakeoff.types import CohortKey, GoldFragment, Item, ItemTurn

__all__ = ["GoldIntegrityError", "DatasetLoader"]

# Default cohort axis values used when a record/ledger genuinely lacks a value.
# Kept explicit (never blank) so every Item carries a fully-populated CohortKey
# (the loader's tests assert no empty geography/proficiency/tone).
_UNKNOWN = "unknown"

# answerability assigned to a multi-turn set that has no turn-1 gold row.
_NO_GOLD_ANSWERABILITY = "none"


class GoldIntegrityError(RuntimeError):
    """Raised when one or more referenced ``gold_node_id``s do not resolve.

    Carries the sorted list of offending ids on :attr:`missing_ids` so callers
    (and the failure-path test) can assert on the specific dangling id(s).
    """

    def __init__(self, missing_ids: Iterable[str]):
        self.missing_ids: list[str] = sorted(set(missing_ids))
        preview = ", ".join(self.missing_ids[:20])
        more = "" if len(self.missing_ids) <= 20 else f" (+{len(self.missing_ids) - 20} more)"
        super().__init__(
            f"{len(self.missing_ids)} gold_node_id(s) do not resolve in the corpus "
            f"index: {preview}{more}"
        )


class DatasetLoader:
    """Load and normalize the synthetic dataset into uniform :class:`Item`s.

    Args:
        data_dir: directory holding the dataset files. Defaults to
            :data:`bakeoff.config.DATASET_DIR` (``data/synthetic/``).

    The reference tables (ledger, corpus index, multi-turn gold) are loaded lazily
    on first use and cached. :meth:`load_items` is the entry point; it enforces
    gold-link integrity across the whole dataset before returning.
    """

    # filenames (relative to data_dir)
    QUERIES_FILE = "queries.jsonl"
    CONVERSATIONS_FILE = "conversations.jsonl"
    CONVO_GOLD_FILE = "conversation_turn1_gold.jsonl"
    LEDGER_FILE = "perspectives_ledger.jsonl"
    CORPUS_INDEX_FILE = "corpus_index.tsv"
    # any additional *_gold.jsonl beyond the conversation gold are folded in too
    GOLD_GLOB = "*_gold.jsonl"
    # Full FAQ body source for the judge reference (NOT used for gold integrity).
    # Lives in the repo data dir (one level ABOVE data_dir=data/synthetic/), keyed
    # by ``nodeId`` with the full answer ``markdown`` (median ~1000 chars, up to
    # ~3000) — the authoritative full-body source per the owner. ``corpus_index.tsv``
    # only carries a 200-char snippet, which is too thin for the judge's
    # correctness/completeness reference (see docs/QUALITY_SIGNAL_DIAGNOSIS.md).
    RESULTS_FILE = "results.jsonl"

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir: Path = Path(data_dir) if data_dir is not None else config.DATASET_DIR
        # reference tables (lazy)
        self._corpus: Optional[dict[str, tuple[str, Optional[str]]]] = None
        self._ledger: Optional[dict[int, tuple[str, str, str]]] = None
        self._convo_gold: Optional[dict[str, dict]] = None
        self._body_table: Optional[dict[str, str]] = None
        # cached load
        self._items: Optional[list[Item]] = None
        #: set_ids of multi-turn conversations that had no turn-1 gold row.
        self.conversations_without_gold: list[str] = []

    # ------------------------------------------------------------------
    # Reference tables
    # ------------------------------------------------------------------
    def _read_jsonl(self, path: Path) -> list[dict]:
        """Read a JSONL file, skipping blank lines. Missing file -> empty list."""
        if not path.exists():
            return []
        records: list[dict] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    records.append(json.loads(line))
        return records

    @property
    def corpus(self) -> dict[str, tuple[str, Optional[str]]]:
        """``nodeId -> (title, snippet)`` from ``corpus_index.tsv`` (cached)."""
        if self._corpus is None:
            self._corpus = self._load_corpus_index()
        return self._corpus

    def _load_corpus_index(self) -> dict[str, tuple[str, Optional[str]]]:
        path = self.data_dir / self.CORPUS_INDEX_FILE
        out: dict[str, tuple[str, Optional[str]]] = {}
        if not path.exists():
            return out
        with path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh, delimiter="\t")
            for row in reader:
                node_id = (row.get("nodeId") or "").strip()
                if not node_id:
                    continue
                title = (row.get("title") or "").strip()
                snippet = row.get("snippet")
                out[node_id] = (title, snippet if snippet else None)
        return out

    @property
    def body_table(self) -> dict[str, str]:
        """``nodeId -> full_markdown_body`` from ``results.jsonl`` (cached).

        The full FAQ answer body used as the judge's correctness/completeness
        reference, in place of the 200-char ``corpus_index.tsv`` snippet (see
        docs/QUALITY_SIGNAL_DIAGNOSIS.md). Resolved relative to ``data_dir.parent``
        (the repo ``data/`` dir) because ``data_dir`` defaults to
        ``data/synthetic/`` while ``results.jsonl`` lives one level up. Missing
        file -> empty table (the loader degrades gracefully to snippet behavior).
        """
        if self._body_table is None:
            self._body_table = self._load_body_table()
        return self._body_table

    def _load_body_table(self) -> dict[str, str]:
        # results.jsonl lives in data/ (data_dir.parent), not data/synthetic/.
        path = self.data_dir.parent / self.RESULTS_FILE
        out: dict[str, str] = {}
        for rec in self._read_jsonl(path):
            node_id = (rec.get("nodeId") or "").strip()
            if not node_id:
                continue
            markdown = rec.get("markdown")
            if isinstance(markdown, str) and markdown.strip():
                # Duplicate nodeIds in the source are byte-identical; last wins.
                out[node_id] = markdown
        return out

    @property
    def ledger(self) -> dict[int, tuple[str, str, str]]:
        """``batch -> (geography, proficiency, tone)`` from the ledger (cached)."""
        if self._ledger is None:
            self._ledger = self._load_ledger()
        return self._ledger

    def _load_ledger(self) -> dict[int, tuple[str, str, str]]:
        out: dict[int, tuple[str, str, str]] = {}
        for rec in self._read_jsonl(self.data_dir / self.LEDGER_FILE):
            batch = rec.get("batch")
            if batch is None:
                continue
            geography = (rec.get("origin") or "").strip() or _UNKNOWN
            proficiency = (rec.get("proficiency") or "").strip() or _UNKNOWN
            tone = (rec.get("disposition") or "").strip() or _UNKNOWN
            out[batch] = (geography, proficiency, tone)
        return out

    @property
    def convo_gold(self) -> dict[str, dict]:
        """``set_id -> {gold_node_ids, answerability}`` for turn-1 (cached).

        Folds in ``conversation_turn1_gold.jsonl`` plus any other ``*_gold.jsonl``
        that carry a ``set_id`` (turn-1). Single-turn query gold lives inline on
        the query record, so id-keyed gold files (no ``set_id``) are ignored here.
        """
        if self._convo_gold is None:
            self._convo_gold = self._load_convo_gold()
        return self._convo_gold

    def _load_convo_gold(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        gold_files: list[Path] = []
        primary = self.data_dir / self.CONVO_GOLD_FILE
        if primary.exists():
            gold_files.append(primary)
        # any other *_gold.jsonl, deduped against the primary
        for extra in sorted(self.data_dir.glob(self.GOLD_GLOB)):
            if extra != primary:
                gold_files.append(extra)
        for path in gold_files:
            for rec in self._read_jsonl(path):
                set_id = rec.get("set_id")
                # only turn-1, set_id-keyed rows define a conversation's focal gold
                if set_id is None or rec.get("turn", 1) != 1:
                    continue
                out[set_id] = {
                    "gold_node_ids": list(rec.get("gold_node_ids") or []),
                    "answerability": rec.get("answerability") or _NO_GOLD_ANSWERABILITY,
                }
        return out

    # ------------------------------------------------------------------
    # Cohort derivation (ledger join, persona-string fallback)
    # ------------------------------------------------------------------
    def _cohort_geo_prof_tone(
        self, batch: Optional[int], persona: Optional[str]
    ) -> tuple[str, str, str]:
        """Derive (geography, proficiency, tone) primarily from the ledger.

        The ledger (joined by integer ``batch``) is authoritative. Only when the
        ledger lacks the batch do we fall back to parsing the pipe-delimited
        persona string ("Origin | proficiency | disposition"); a dashed-slug
        persona with no pipes degrades to ``unknown`` rather than crashing.
        """
        if batch is not None and batch in self.ledger:
            return self.ledger[batch]
        return self._parse_persona(persona)

    @staticmethod
    def _parse_persona(persona: Optional[str]) -> tuple[str, str, str]:
        """Fallback parse of a "geo | proficiency | tone" persona string."""
        if not persona:
            return (_UNKNOWN, _UNKNOWN, _UNKNOWN)
        parts = [p.strip() for p in persona.split("|")]
        geography = parts[0] if len(parts) >= 1 and parts[0] else _UNKNOWN
        proficiency = parts[1] if len(parts) >= 2 and parts[1] else _UNKNOWN
        tone = parts[2] if len(parts) >= 3 and parts[2] else _UNKNOWN
        return (geography, proficiency, tone)

    # ------------------------------------------------------------------
    # Gold resolution
    # ------------------------------------------------------------------
    def resolve_gold(self, node_ids: list[str]) -> list[GoldFragment]:
        """Map each ``node_id`` to a :class:`GoldFragment` via the corpus index.

        Raises:
            GoldIntegrityError: if any id does not resolve in ``corpus_index.tsv``
                (enforces gold-link integrity, Req 1.5). The error lists every
                offending id, not just the first.
        """
        missing = [nid for nid in node_ids if nid not in self.corpus]
        if missing:
            raise GoldIntegrityError(missing)
        fragments: list[GoldFragment] = []
        for nid in node_ids:
            title, snippet = self.corpus[nid]
            # Full body (from results.jsonl) is the judge reference when available;
            # it falls back to the snippet/title in ideal_response_text when absent.
            # This is an ADDITIONAL field only — gold integrity still keys on the
            # corpus index above, which must still fail loudly on a dangling id.
            markdown = self.body_table.get(nid)
            fragments.append(
                GoldFragment(node_id=nid, title=title, markdown=markdown, snippet=snippet)
            )
        return fragments

    # ------------------------------------------------------------------
    # Loading items
    # ------------------------------------------------------------------
    def load_items(self) -> list[Item]:
        """Load both single-turn and multi-turn records into uniform :class:`Item`s.

        Enforces dataset-wide gold-link integrity before returning: if any
        referenced ``gold_node_id`` does not resolve, raises
        :class:`GoldIntegrityError` listing ALL offending ids at once. Result is
        cached; subsequent calls return the same list.
        """
        if self._items is not None:
            return self._items

        self.conversations_without_gold = []
        queries = self._read_jsonl(self.data_dir / self.QUERIES_FILE)
        conversations = self._read_jsonl(self.data_dir / self.CONVERSATIONS_FILE)

        # 1) Dataset-wide gold-integrity pre-check (comprehensive, fail loudly).
        referenced: set[str] = set()
        for q in queries:
            referenced.update(q.get("gold_node_ids") or [])
        for c in conversations:
            gold = self.convo_gold.get(c.get("set_id"))
            if gold:
                referenced.update(gold["gold_node_ids"])
        unresolved = [nid for nid in referenced if nid not in self.corpus]
        if unresolved:
            raise GoldIntegrityError(unresolved)

        # 2) Build items (gold now guaranteed resolvable).
        items: list[Item] = []
        for q in queries:
            items.append(self._build_single_turn(q))
        for c in conversations:
            items.append(self._build_multi_turn(c))

        self._items = items
        return items

    def _build_single_turn(self, rec: dict) -> Item:
        batch = rec.get("batch")
        persona = rec.get("persona")
        geography, proficiency, tone = self._cohort_geo_prof_tone(batch, persona)
        answerability = rec.get("answerability") or _UNKNOWN
        cohort = CohortKey(
            geography=geography,
            proficiency=proficiency,
            tone=tone,
            entry_route=rec.get("entry_route") or _UNKNOWN,
            momentary_state=rec.get("momentary_state") or _UNKNOWN,
            answerability=answerability,
            turn_type="single",
        )
        gold_ids = list(rec.get("gold_node_ids") or [])
        return Item(
            id=rec["id"],
            turn_type="single",
            cohort=cohort,
            query=rec.get("query"),
            # `wants` is the closest available "ideal intent" the dataset carries;
            # used as the ideal-response source for semantic scoring (Req 1; no
            # explicit ideal_response field exists in the synthetic records).
            wants=rec.get("wants"),
            answerability=answerability,
            gold_node_ids=gold_ids,
            gold=self.resolve_gold(gold_ids),
            turns=(),
            persona=persona,
            batch=batch,
            label_note=rec.get("label_note"),
            # the dataset has no per-item retrieval filter fields beyond cohort;
            # retrieval uses its own defaults, so filters stay empty (Req 1).
            retrieval_filters={},
        )

    def _build_multi_turn(self, rec: dict) -> Item:
        set_id = rec["set_id"]
        batch = rec.get("batch")
        persona = rec.get("persona_tag")
        geography, proficiency, tone = self._cohort_geo_prof_tone(batch, persona)

        raw_turns = sorted(rec.get("turns") or [], key=lambda t: t.get("turn", 0))

        gold = self.convo_gold.get(set_id)
        if gold is None:
            # No turn-1 gold row for this conversation: handle gracefully.
            self.conversations_without_gold.append(set_id)
            gold_ids: list[str] = []
            answerability = _NO_GOLD_ANSWERABILITY
        else:
            gold_ids = list(gold["gold_node_ids"])
            answerability = gold["answerability"]

        gold_fragments = self.resolve_gold(gold_ids)

        # turn-1 drives the focal query, momentary_state, and cohort answerability
        turn1 = raw_turns[0] if raw_turns else {}
        focal_query = turn1.get("user_utterance")
        turn1_state = turn1.get("momentary_state") or _UNKNOWN

        item_turns: list[ItemTurn] = []
        for t in raw_turns:
            is_turn1 = t.get("turn") == turn1.get("turn")
            item_turns.append(
                ItemTurn(
                    turn=t.get("turn"),
                    user_utterance=t.get("user_utterance") or "",
                    momentary_state=t.get("momentary_state") or _UNKNOWN,
                    # only turn-1 carries resolved gold/answerability from the
                    # multi-turn gold file; later turns are response-dependent
                    # and not separately gold-labeled in this dataset.
                    answerability=answerability if is_turn1 else None,
                    wants=t.get("wants"),
                    response_dependent=bool(t.get("response_dependent", False)),
                    depends_on_turn=t.get("depends_on_turn"),
                    relationship=t.get("relationship"),
                    gold=gold_fragments if is_turn1 else [],
                )
            )

        cohort = CohortKey(
            geography=geography,
            proficiency=proficiency,
            tone=tone,
            entry_route=rec.get("entry_route") or _UNKNOWN,
            momentary_state=turn1_state,
            answerability=answerability,
            turn_type="multi",
        )
        return Item(
            id=set_id,
            turn_type="multi",
            cohort=cohort,
            query=focal_query,
            wants=turn1.get("wants"),
            answerability=answerability,
            gold_node_ids=list(gold_ids),
            gold=gold_fragments,
            turns=tuple(item_turns),
            persona=persona,
            batch=batch,
            label_note=None,
            retrieval_filters={},
        )

    # ------------------------------------------------------------------
    # Cohort cells (Req 1.6)
    # ------------------------------------------------------------------
    def cohort_cells(self) -> list[CohortKey]:
        """Enumerate the distinct non-empty cohort cells across loaded items.

        The returned cells are exactly those present in the data (used for
        stratified subsampling); cells with no items never appear.
        """
        items = self.load_items()
        seen: dict[str, CohortKey] = {}
        for item in items:
            seen[item.cohort.cell_id()] = item.cohort
        return list(seen.values())
