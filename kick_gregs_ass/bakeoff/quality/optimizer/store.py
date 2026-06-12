"""
Durable record types for the closed-loop prompt optimizer's append-only stores.

This module defines the immutable record *shapes* the optimizer persists, plus their
single-line JSON (de)serialization. It is the value-object layer for three append-only
JSONL stores (paths live in ``bakeoff/config.py`` as ``QUALITY_OPT_ITERATIONS_PATH`` /
``QUALITY_OPT_AUDIT_PATH`` and the disposable ``QUALITY_OPT_ERRORS_PATH``):

* :class:`IterationRecord` — the per-iteration **source of truth** written to
  ``quality_opt_iterations.jsonl``: the decision metric (champion/challenger triad
  scores + CIs), the promotion outcome, the gain in both representations, the
  noise-floor context (slice size + between-conversation SD), and the convergence state.
* :class:`AuditRecord` — the rich, human-facing version-lookback record written to
  ``quality_opt_audit.jsonl``: full champion/challenger prompt text, the unified diff,
  the Author's rationale, the judge-evidenced :class:`DrivingFailure`\\ s that drove the
  rewrite, the challenger's triad + CI + per-dimension breakdown, and accept/reject.
* :class:`DrivingFailure` — one judge-scored failing turn (with the judge's quoted
  evidence) carried inside an :class:`AuditRecord`.

The on-disk discipline mirrors ``bakeoff/quality/types.py`` and ``bakeoff/quality/judge.py``
exactly: **one complete JSON object per physical line**, compact separators, and
``ensure_ascii=False`` so non-ASCII evidence/answer text is stored verbatim rather than
escaped. Every record is a frozen dataclass, so a persisted record is an immutable value.

The (de)serialization is **round-trip exact** by construction (design Properties 13 and
14): ``from_jsonl(to_jsonl(x)) == x`` holds for any record, including the nested
:class:`DrivingFailure` tuple and every ``Optional``/``None`` field. Serialization uses
the dataclass field-declaration order, giving deterministic, stable JSON key ordering.

This module also layers the append-only IO surface on top of those record types:

* :class:`OptimizerStore` — the durable, path-injectable reader/writer over the four
  optimizer stores (iterations SoT, audit, disposable errors, single-object results). Every
  durable append is one ``flush()`` + ``os.fsync()``'d JSONL line and every reader tolerates
  a single truncated trailing line, mirroring ``bakeoff/quality/judge.py`` and
  ``bakeoff/quality/types.py`` exactly (Req 10.1/10.8). The single-object results file is
  written atomically (temp file + ``os.replace``), the repo's single-object JSON discipline.
* :class:`PromptVersion` — a small ordered-history view projected from an
  :class:`AuditRecord` (prompt-version id, champion/challenger instruction, diff, score,
  accept/reject) used to reconstruct the per-model version history and lookback (Req 8.4/8.5).
* The history/resume helpers (:meth:`OptimizerStore.iteration_history`,
  :meth:`OptimizerStore.prompt_version_history`, :meth:`OptimizerStore.lookback`,
  :meth:`OptimizerStore.completed_iteration_ids`) reconstruct per-model ordered state from
  the durable stores so an interrupted run resumes by skipping already-durable iterations
  (Req 8.2/8.4/8.5/10.3).

The two small pure helpers (``_now_iso`` for a UTC ISO timestamp and ``make_prompt_diff``
for a ``difflib`` unified diff that callers store into ``AuditRecord.prompt_diff``) remain.
"""
from __future__ import annotations

import dataclasses
import difflib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, TypeVar, Union

from bakeoff import config

__all__ = [
    "DrivingFailure",
    "IterationRecord",
    "AuditRecord",
    "PromptVersion",
    "OptimizerStore",
    "make_prompt_diff",
]

PathLike = Union[str, "os.PathLike[str]"]


def _now_iso() -> str:
    """Return the current UTC instant as an ISO-8601 string (timezone-aware).

    Mirrors ``bakeoff/quality/judge.py::_now_iso`` so every optimizer record stamps its
    ``created_at`` with the same timestamp shape the rest of the quality study uses.
    """
    return datetime.now(timezone.utc).isoformat()


def make_prompt_diff(
    old: str,
    new: str,
    old_label: str = "champion",
    new_label: str = "challenger",
) -> str:
    """Return a unified diff of ``old`` → ``new`` as a single string.

    Pure helper (no IO, no global state) that callers use to populate
    :attr:`AuditRecord.prompt_diff`. Splits both prompts into lines and runs
    :func:`difflib.unified_diff`, joining the hunk lines with ``"\\n"``. ``lineterm=""``
    keeps each emitted line terminator-free so the join produces a clean diff with no
    doubled newlines. Two identical prompts yield an empty string (``difflib`` emits no
    hunks when there is no change).

    Args:
        old: the current champion instruction text (the "from" side).
        new: the proposed challenger instruction text (the "to" side).
        old_label: label for the ``old`` side in the diff header.
        new_label: label for the ``new`` side in the diff header.

    Returns:
        The unified diff as one string (``""`` when the two inputs are identical).
    """
    diff_lines = difflib.unified_diff(
        old.splitlines(),
        new.splitlines(),
        fromfile=old_label,
        tofile=new_label,
        lineterm="",
    )
    return "\n".join(diff_lines)


@dataclasses.dataclass(frozen=True)
class DrivingFailure:
    """One judge-scored failing turn that drove an Author rewrite (design "AuditRecord").

    Carried (as an ordered tuple) inside an :class:`AuditRecord`. Each failure records the
    conversation/turn it came from, the judge's overall + per-dimension scores, the
    abstention/grounding signals (Req 14), the ids of the fragments the model **and** the
    judge saw for that turn (Req 13.7), the judge's quoted evidence span(s), and an excerpt
    of the model's failing answer — i.e. exactly what the Author was shown to motivate the
    rewrite (Req 1.3/3.1/8.1).

    ``abstention_correct`` is ``None`` on turns where abstention is not the graded behavior
    (i.e. the turn was answerable and adequately grounded), a tri-state that distinguishes
    "abstention not applicable" from "abstained, and that was wrong". ``answered_when_unsure``
    flags the over-claim case the Author is shown first (Req 14.4); ``fragments_sufficient``
    records whether the retrieved fragments could have supported a grounded answer at all.
    """

    item_id: str
    rep: int
    turn: int
    overall: float
    dimensions: dict[str, float]
    abstention_correct: Optional[bool]
    answered_when_unsure: bool
    fragments_sufficient: bool
    grounding_fragment_ids: tuple[str, ...]
    evidence: dict[str, str]
    answer_excerpt: str

    def to_dict(self) -> dict:
        """Return a JSON-ready dict in field-declaration order.

        Used by :meth:`AuditRecord.to_jsonl` to serialize the nested failure tuple. Kept
        separate from a full ``to_jsonl`` because a :class:`DrivingFailure` is only ever
        persisted *inside* an :class:`AuditRecord`, never on its own line. The
        ``grounding_fragment_ids`` tuple is emitted as a JSON array (JSON has no tuple) and
        rebuilt back into a tuple on read so the round-trip is exact.
        """
        return {
            "item_id": self.item_id,
            "rep": self.rep,
            "turn": self.turn,
            "overall": self.overall,
            "dimensions": dict(self.dimensions),
            "abstention_correct": self.abstention_correct,
            "answered_when_unsure": self.answered_when_unsure,
            "fragments_sufficient": self.fragments_sufficient,
            "grounding_fragment_ids": list(self.grounding_fragment_ids),
            "evidence": dict(self.evidence),
            "answer_excerpt": self.answer_excerpt,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DrivingFailure":
        """Reconstruct a :class:`DrivingFailure` from a parsed dict.

        Coerces the scalar/number/text fields so a value that JSON happened to widen
        (e.g. an integer-valued ``overall``) round-trips back to the declared type, rebuilds
        the ``dimensions``/``evidence`` maps defensively (empty when absent), restores the
        ``grounding_fragment_ids`` tuple from its JSON array, and preserves the tri-state
        ``abstention_correct`` ``None`` exactly.
        """
        ac = d.get("abstention_correct")
        return cls(
            item_id=str(d["item_id"]),
            rep=int(d["rep"]),
            turn=int(d["turn"]),
            overall=float(d["overall"]),
            dimensions={k: float(v) for k, v in (d.get("dimensions") or {}).items()},
            abstention_correct=(bool(ac) if ac is not None else None),
            answered_when_unsure=bool(d["answered_when_unsure"]),
            fragments_sufficient=bool(d["fragments_sufficient"]),
            grounding_fragment_ids=tuple(
                str(x) for x in (d.get("grounding_fragment_ids") or [])
            ),
            evidence={k: str(v) for k, v in (d.get("evidence") or {}).items()},
            answer_excerpt=str(d.get("answer_excerpt", "")),
        )


@dataclasses.dataclass(frozen=True)
class IterationRecord:
    """One iteration's source-of-truth row (``quality_opt_iterations.jsonl``).

    The decision metric is the per-conversation triad mean; ``champion_score`` /
    ``challenger_score`` are that metric on the scored slice with their 95% CI half-widths.
    ``promoted`` is the promotion outcome; ``gain_absolute`` / ``gain_percent`` record the
    gain in both representations (Req 5.4). ``slice_n_conversations`` and
    ``between_conversation_sd`` make the noise floor the gain was judged against visible
    (Req 5.7). ``mean_closeness`` is the secondary cross-check only, never a decision input
    (Req 2.3). ``abstention_reward_mean`` is the primary abstention-correctness contribution
    and ``answered_when_unsure_rate`` the over-claim rate that is penalized (Req 14.2/14.4);
    ``retrieval_backend`` records which held-constant backend supplied the grounding
    fragments (Req 16). The seed row (``iteration_index == 0``) has no challenger, so the
    challenger/gain fields are ``None``.
    """

    iteration_id: str
    model: str
    phase: str
    iteration_index: int
    backend: str
    author_model: str
    judge_model: str
    champion_score: float
    champion_ci_half_width: float
    challenger_score: Optional[float]
    challenger_ci_half_width: Optional[float]
    significance_threshold: float
    promoted: bool
    gain_absolute: Optional[float]
    gain_percent: Optional[float]
    slice_n_conversations: int
    between_conversation_sd: float
    consecutive_non_improving: int
    converged: bool
    stop_reason: Optional[str]
    mean_closeness: float
    abstention_reward_mean: float
    answered_when_unsure_rate: float
    retrieval_backend: str
    created_at: str
    island_id: Optional[int] = None
    rung_index: Optional[int] = None
    tournament_round: Optional[int] = None
    #: Conversation type this run appraised on (single|multi|both) — partitions the
    #: dashboard into separate single-run / multi-run views. Defaults to "multi".
    turn_mode: str = "multi"

    def to_jsonl(self) -> str:
        """Serialize to one compact JSON line (no embedded newline).

        Uses :func:`dataclasses.asdict`, which preserves field-declaration order, so the
        JSON key order is deterministic across writes.
        """
        return json.dumps(
            dataclasses.asdict(self), ensure_ascii=False, separators=(",", ":")
        )

    @classmethod
    def from_jsonl(cls, line: str) -> "IterationRecord":
        """Parse one JSON line back into a fully-typed :class:`IterationRecord`.

        Numeric/boolean fields are coerced to their declared types and the four nullable
        fields (``challenger_score``, ``challenger_ci_half_width``, ``gain_absolute``,
        ``gain_percent``, ``stop_reason``) preserve ``None`` exactly, so
        ``from_jsonl(to_jsonl(x)) == x``.
        """
        d = json.loads(line)
        return cls(
            iteration_id=str(d["iteration_id"]),
            model=str(d["model"]),
            phase=str(d["phase"]),
            iteration_index=int(d["iteration_index"]),
            backend=str(d["backend"]),
            author_model=str(d["author_model"]),
            judge_model=str(d["judge_model"]),
            champion_score=float(d["champion_score"]),
            champion_ci_half_width=float(d["champion_ci_half_width"]),
            challenger_score=(
                float(d["challenger_score"])
                if d.get("challenger_score") is not None
                else None
            ),
            challenger_ci_half_width=(
                float(d["challenger_ci_half_width"])
                if d.get("challenger_ci_half_width") is not None
                else None
            ),
            significance_threshold=float(d["significance_threshold"]),
            promoted=bool(d["promoted"]),
            gain_absolute=(
                float(d["gain_absolute"]) if d.get("gain_absolute") is not None else None
            ),
            gain_percent=(
                float(d["gain_percent"]) if d.get("gain_percent") is not None else None
            ),
            slice_n_conversations=int(d["slice_n_conversations"]),
            between_conversation_sd=float(d["between_conversation_sd"]),
            consecutive_non_improving=int(d["consecutive_non_improving"]),
            converged=bool(d["converged"]),
            stop_reason=(
                str(d["stop_reason"]) if d.get("stop_reason") is not None else None
            ),
            mean_closeness=float(d["mean_closeness"]),
            abstention_reward_mean=float(d["abstention_reward_mean"]),
            answered_when_unsure_rate=float(d["answered_when_unsure_rate"]),
            retrieval_backend=str(d["retrieval_backend"]),
            created_at=str(d["created_at"]),
            island_id=(
                int(d["island_id"]) if d.get("island_id") is not None else None
            ),
            rung_index=(
                int(d["rung_index"]) if d.get("rung_index") is not None else None
            ),
            tournament_round=(
                int(d["tournament_round"])
                if d.get("tournament_round") is not None
                else None
            ),
            turn_mode=str(d.get("turn_mode", "multi")),
        )


@dataclasses.dataclass(frozen=True)
class AuditRecord:
    """One iteration's rich audit row (``quality_opt_audit.jsonl``); one per iteration.

    The human-facing version-lookback record (Req 8): the full champion prompt text before
    this iteration, the full proposed challenger text (``None`` for the seed), the unified
    ``prompt_diff`` (built by callers via :func:`build_prompt_diff`), the Author's
    rationale, the judge-evidenced :class:`DrivingFailure`\\ s that drove the rewrite, the
    challenger's triad + CI + per-dimension breakdown (Req 2.6), and the accept/reject
    outcome — stamped with ``model``, ``iteration_index``, ``backend``, and the
    author/judge identities (Req 4.3/10.6) so a reader can fully reconstruct why a prompt
    version exists.
    """

    iteration_id: str
    prompt_version_id: str
    model: str
    iteration_index: int
    backend: str
    author_model: str
    judge_model: str
    champion_instruction: str
    challenger_instruction: Optional[str]
    prompt_diff: str
    author_rationale: str
    driving_failures: tuple[DrivingFailure, ...]
    challenger_triad: Optional[float]
    challenger_ci_half_width: Optional[float]
    challenger_per_dimension: dict[str, float]
    accepted: bool
    created_at: str
    island_id: Optional[int] = None
    rung_index: Optional[int] = None
    tournament_round: Optional[int] = None
    #: Conversation type this run appraised on (single|multi|both) — see IterationRecord.
    turn_mode: str = "multi"

    def to_jsonl(self) -> str:
        """Serialize to one compact JSON line, including the nested failure tuple.

        The ``driving_failures`` tuple is serialized as an ordered JSON array of failure
        objects (via :meth:`DrivingFailure.to_dict`); all other fields are emitted in
        field-declaration order for deterministic, stable key ordering.
        """
        payload = {
            "iteration_id": self.iteration_id,
            "prompt_version_id": self.prompt_version_id,
            "model": self.model,
            "iteration_index": self.iteration_index,
            "backend": self.backend,
            "author_model": self.author_model,
            "judge_model": self.judge_model,
            "champion_instruction": self.champion_instruction,
            "challenger_instruction": self.challenger_instruction,
            "prompt_diff": self.prompt_diff,
            "author_rationale": self.author_rationale,
            "driving_failures": [f.to_dict() for f in self.driving_failures],
            "challenger_triad": self.challenger_triad,
            "challenger_ci_half_width": self.challenger_ci_half_width,
            "challenger_per_dimension": dict(self.challenger_per_dimension),
            "accepted": self.accepted,
            "created_at": self.created_at,
            "island_id": self.island_id,
            "rung_index": self.rung_index,
            "tournament_round": self.tournament_round,
            "turn_mode": self.turn_mode,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_jsonl(cls, line: str) -> "AuditRecord":
        """Parse one JSON line back into a fully-typed :class:`AuditRecord`.

        Rebuilds the ordered ``driving_failures`` tuple from its JSON array (each element
        via :meth:`DrivingFailure.from_dict`) and preserves the ``None`` of every nullable
        field, so ``from_jsonl(to_jsonl(x)) == x`` holds including the nested failures.
        """
        d = json.loads(line)
        return cls(
            iteration_id=str(d["iteration_id"]),
            prompt_version_id=str(d["prompt_version_id"]),
            model=str(d["model"]),
            iteration_index=int(d["iteration_index"]),
            backend=str(d["backend"]),
            author_model=str(d["author_model"]),
            judge_model=str(d["judge_model"]),
            champion_instruction=str(d["champion_instruction"]),
            challenger_instruction=(
                str(d["challenger_instruction"])
                if d.get("challenger_instruction") is not None
                else None
            ),
            prompt_diff=str(d.get("prompt_diff", "")),
            author_rationale=str(d.get("author_rationale", "")),
            driving_failures=tuple(
                DrivingFailure.from_dict(f) for f in (d.get("driving_failures") or [])
            ),
            challenger_triad=(
                float(d["challenger_triad"])
                if d.get("challenger_triad") is not None
                else None
            ),
            challenger_ci_half_width=(
                float(d["challenger_ci_half_width"])
                if d.get("challenger_ci_half_width") is not None
                else None
            ),
            challenger_per_dimension={
                k: float(v)
                for k, v in (d.get("challenger_per_dimension") or {}).items()
            },
            accepted=bool(d["accepted"]),
            created_at=str(d["created_at"]),
            island_id=(
                int(d["island_id"]) if d.get("island_id") is not None else None
            ),
            rung_index=(
                int(d["rung_index"]) if d.get("rung_index") is not None else None
            ),
            tournament_round=(
                int(d["tournament_round"])
                if d.get("tournament_round") is not None
                else None
            ),
            turn_mode=str(d.get("turn_mode", "multi")),
        )


# ---------------------------------------------------------------------------
# Append-only durable IO (Task 5.2) + version-history / resume helpers (Task 5.3)
# ---------------------------------------------------------------------------

_R = TypeVar("_R")


def _append_jsonl_line(path: Path, line: str) -> None:
    """Append exactly one complete JSONL ``line`` to ``path``, durably (fsync'd).

    Mirrors ``bakeoff/quality/judge.py::append_turn_judge_score`` and
    ``bakeoff/quality/types.py::append_outcome`` exactly: create parent dirs, then a single
    ``write`` of the complete line (with its trailing newline) in append mode, followed by
    ``flush()`` + :func:`os.fsync` so the record is on disk before returning. The
    single-write-of-a-complete-line discipline is what guarantees a crash leaves at most one
    truncated trailing line — never a half-written interior record (Req 10.8).

    Args:
        path: the JSONL store to append to (parents created if missing).
        line: the serialized record *without* a trailing newline; the newline is added here
            so callers cannot accidentally emit an embedded newline mid-record.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


def _read_jsonl(path: Path, parse: Callable[[str], _R]) -> list[_R]:
    """Read every record from a JSONL ``path`` (``[]`` if absent), tolerating a truncated tail.

    Mirrors ``bakeoff/quality/judge.py::read_turn_judge_scores`` line-for-line: a missing
    file yields ``[]``; blank lines are skipped; every complete line is parsed via ``parse``.
    If the FINAL physical line fails to parse it is treated as a crash-truncated trailing
    record and dropped (the loop breaks); a parse failure on any *interior* line is genuine
    corruption and is re-raised (Req 10.8).

    Args:
        path: the JSONL store to read.
        parse: the per-line deserializer (e.g. ``IterationRecord.from_jsonl``).

    Returns:
        The parsed records in file (append) order, with at most one truncated trailing line
        dropped.
    """
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        raw_lines = f.readlines()
    out: list[_R] = []
    last = len(raw_lines) - 1
    for i, raw in enumerate(raw_lines):
        line = raw.rstrip("\n")
        if not line:
            continue
        try:
            out.append(parse(line))
        except Exception:  # noqa: BLE001 - tolerate only a truncated final line
            if i == last:
                break
            raise
    return out


@dataclasses.dataclass(frozen=True)
class PromptVersion:
    """One entry in a model's ordered prompt-version history (design "AuditRecord + PromptVersion").

    A small read-side projection of an :class:`AuditRecord` carrying exactly what a history /
    lookback reader needs (Req 8.4/8.5): the stable ``prompt_version_id`` the version is
    retrievable by, the champion text this version started from and the proposed challenger
    text, the unified ``diff`` between them, the challenger's triad ``score`` + CI, and
    whether the challenger was ``accepted``. ``iteration_index`` is the ordering key. The
    seed iteration (index 0) has no challenger, so ``challenger_instruction``, ``score``, and
    ``ci_half_width`` are ``None`` there.
    """

    prompt_version_id: str
    model: str
    iteration_index: int
    champion_instruction: str
    challenger_instruction: Optional[str]
    diff: str
    score: Optional[float]
    ci_half_width: Optional[float]
    accepted: bool

    @classmethod
    def from_audit(cls, rec: AuditRecord) -> "PromptVersion":
        """Project an :class:`AuditRecord` into its history view (pure, no IO)."""
        return cls(
            prompt_version_id=rec.prompt_version_id,
            model=rec.model,
            iteration_index=rec.iteration_index,
            champion_instruction=rec.champion_instruction,
            challenger_instruction=rec.challenger_instruction,
            diff=rec.prompt_diff,
            score=rec.challenger_triad,
            ci_half_width=rec.challenger_ci_half_width,
            accepted=rec.accepted,
        )


class OptimizerStore:
    """Append-only, path-injectable IO over the optimizer's four on-disk stores.

    Wraps the four store paths declared in ``bakeoff/config.py`` (design "Data Models —
    New store layout"):

    * ``iterations_path`` — :class:`IterationRecord` JSONL, the per-iteration **source of
      truth** (the decision data; Req 10.1).
    * ``audit_path`` — :class:`AuditRecord` JSONL, the rich human-facing version-lookback
      store (Req 8).
    * ``errors_path`` — a **disposable** JSONL store of failed attempts (one JSON object per
      line); a scoring/generation failure is recorded here, never in the SoT, so a failed
      attempt can be retried on resume without polluting the decision data (design "Error
      Handling").
    * ``results_path`` — the single-object ``quality_opt_results.json`` (converged champions
      + Phase B results); written atomically, not appended.

    Every durable JSONL append is one ``flush()`` + :func:`os.fsync`'d line and every reader
    tolerates a single truncated trailing line (Req 10.1/10.8), mirroring the rest of the
    harness. All four paths are injectable (defaulting to the ``config`` constants) so tests
    point them at a tmp dir without touching the real ``data/`` store.
    """

    def __init__(
        self,
        *,
        iterations_path: PathLike = config.QUALITY_OPT_ITERATIONS_PATH,
        audit_path: PathLike = config.QUALITY_OPT_AUDIT_PATH,
        errors_path: PathLike = config.QUALITY_OPT_ERRORS_PATH,
        results_path: PathLike = config.QUALITY_OPT_RESULTS_PATH,
    ) -> None:
        """Bind the store to its four paths (defaults are the ``config`` constants).

        Paths are coerced to :class:`pathlib.Path` once here so every method works in terms
        of ``Path``. No directory is created at construction time; parents are created lazily
        on the first write (``mkdir(parents=True)``), so constructing a store over a
        not-yet-existent tmp dir and only ever reading from it stays side-effect-free.
        """
        self.iterations_path = Path(iterations_path)
        self.audit_path = Path(audit_path)
        self.errors_path = Path(errors_path)
        self.results_path = Path(results_path)

    # -- durable appends (one fsync'd JSONL line each) --------------------

    def append_iteration(self, rec: IterationRecord) -> None:
        """Durably append one :class:`IterationRecord` to the SoT iterations store.

        One ``flush()`` + ``os.fsync``'d JSONL line (Req 10.8). This is the resume anchor:
        an iteration whose record is durably present here is considered complete and is
        skipped on re-invocation (see :meth:`completed_iteration_ids`).
        """
        _append_jsonl_line(self.iterations_path, rec.to_jsonl())

    def append_audit(self, rec: AuditRecord) -> None:
        """Durably append one :class:`AuditRecord` to the append-only audit store (Req 8.2)."""
        _append_jsonl_line(self.audit_path, rec.to_jsonl())

    def append_error(self, payload: dict) -> None:
        """Durably append one failed-attempt record to the **disposable** errors store.

        The payload is an arbitrary JSON-serializable dict (e.g. the model/item/turn that
        failed plus an error description). Serialized with the same compact,
        ``ensure_ascii=False`` discipline as every other store and written as one fsync'd
        line. This store is disposable: it never feeds a decision and may be emptied between
        runs (design "Error Handling").
        """
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        _append_jsonl_line(self.errors_path, line)

    def write_results(self, results: dict) -> None:
        """Atomically (over)write the single-object ``quality_opt_results.json``.

        Unlike the append-only stores, the results file is one JSON object that is rewritten
        in full (the Phase-B / converged-champions writer). It is written with the repo's
        single-object durability discipline (temp file + :func:`os.replace`), mirroring
        ``bakeoff/scoring/judge.py`` and ``bakeoff/scoring/semantic.py``: the temp file is
        written, flushed, and fsync'd, then atomically renamed over the destination so a
        reader never observes a partially written results file. Indented + ``sort_keys`` for
        a stable, human-diffable artifact.
        """
        self.results_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.results_path.with_name(self.results_path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.results_path)

    # -- crash-tolerant reads (drop only a truncated trailing line) -------

    def read_iterations(self) -> list[IterationRecord]:
        """Read every :class:`IterationRecord` in append order (``[]`` if absent).

        Tolerates a single truncated trailing line (Req 10.8) via :func:`_read_jsonl`.
        """
        return _read_jsonl(self.iterations_path, IterationRecord.from_jsonl)

    def read_audits(self) -> list[AuditRecord]:
        """Read every :class:`AuditRecord` in append order (``[]`` if absent).

        Tolerates a single truncated trailing line (Req 10.8) via :func:`_read_jsonl`.
        """
        return _read_jsonl(self.audit_path, AuditRecord.from_jsonl)

    def read_results(self) -> Optional[dict]:
        """Read the single-object results JSON, or ``None`` if it has not been written yet."""
        if not self.results_path.exists():
            return None
        return json.loads(self.results_path.read_text(encoding="utf-8"))

    # -- version-history reconstruction / lookback / resume (Task 5.3) ----

    def iteration_history(self, model: str) -> list[IterationRecord]:
        """Return this ``model``'s :class:`IterationRecord`\\ s ordered by ``iteration_index``.

        Filters the SoT store to the given ``model`` and orders by ``iteration_index`` so
        per-model state (champion trajectory, convergence) reconstructs independently of
        write interleaving under a concurrent two-model run (design "Per-model partitioned
        state"). The sort is stable, so two records sharing an index keep their append order.
        """
        recs = [r for r in self.read_iterations() if r.model == model]
        return sorted(recs, key=lambda r: r.iteration_index)

    def prompt_version_history(self, model: str) -> list[PromptVersion]:
        """Return this ``model``'s ordered prompt-version history (Req 8.4/8.5).

        Reads the append-only audit store, filters to ``model``, orders by
        ``iteration_index``, and projects each :class:`AuditRecord` to a :class:`PromptVersion`
        — the ordered sequence of prompt versions with their diffs, scores, and accept/reject
        decisions, each retrievable by its ``prompt_version_id``. The seed iteration (index 0)
        is included so the history starts from the baseline champion (Req 8.6). Stable sort
        preserves append order within an index.
        """
        audits = [a for a in self.read_audits() if a.model == model]
        audits.sort(key=lambda a: a.iteration_index)
        return [PromptVersion.from_audit(a) for a in audits]

    def lookback(self, model: str, n: int) -> list[PromptVersion]:
        """Return the trailing ``n`` prompt versions for ``model`` (most-recent ``n``), in order.

        A slice of :meth:`prompt_version_history` (Req 8.5 — lookback of at least several
        versions). ``n <= 0`` yields ``[]``; ``n`` larger than the available history yields
        the whole history. Order is preserved (oldest → newest of the trailing window).
        """
        if n <= 0:
            return []
        return self.prompt_version_history(model)[-n:]

    def completed_iteration_ids(self, model: str) -> set[str]:
        """Return the set of durable ``iteration_id``\\ s for ``model`` (the resume key set).

        On re-invocation the controller computes this set and skips any iteration whose id is
        present, resuming at the first missing index (Req 10.2/10.3). Because iteration ids
        are deterministic, a completed iteration is never executed twice. Computed from the
        SoT iterations store only — the audit/errors stores never gate resume.
        """
        return {r.iteration_id for r in self.read_iterations() if r.model == model}

    # -- island-partitioned reconstruction (v2 multi-island surface) -------

    def iteration_history_by_island(
        self, model: str
    ) -> dict[tuple[str, Optional[int]], list[IterationRecord]]:
        """Reconstruct progress grouped by ``(model, island_id)``.

        Returns a dict keyed by ``(model, island_id)`` → ordered list of
        :class:`IterationRecord`. Records with ``island_id=None`` (v1 legacy) are grouped
        under ``(model, None)``. Enables durable backfill of the multi-island surface
        without depending on the no-replay event stream.
        """
        groups: dict[tuple[str, Optional[int]], list[IterationRecord]] = {}
        for r in self.read_iterations():
            if r.model != model:
                continue
            key = (r.model, r.island_id)
            groups.setdefault(key, []).append(r)
        for v in groups.values():
            v.sort(key=lambda r: r.iteration_index)
        return groups

    def iteration_history_by_tournament_round(
        self, model: str
    ) -> dict[Optional[int], list[IterationRecord]]:
        """Reconstruct progress grouped by ``tournament_round``.

        Returns a dict keyed by ``tournament_round`` → ordered list of
        :class:`IterationRecord`. Records with ``tournament_round=None`` are non-tournament
        iterations (regular island hill-climb steps). Enables durable backfill of the
        tournament bracket view.
        """
        groups: dict[Optional[int], list[IterationRecord]] = {}
        for r in self.read_iterations():
            if r.model != model:
                continue
            groups.setdefault(r.tournament_round, []).append(r)
        for v in groups.values():
            v.sort(key=lambda r: r.iteration_index)
        return groups

    def last_champion_per_island(
        self, model: str
    ) -> dict[int, str]:
        """Return the current champion instruction for each island of ``model``.

        Walks the audit store in order and tracks the effective champion text after
        each iteration: if the iteration was accepted (``accepted=True``), the
        champion advances to ``challenger_instruction``; if rejected, it stays at
        ``champion_instruction``.  Returns a ``{island_id: champion_text}`` dict
        covering only islands that have at least one durable audit record.  Used to
        reconstruct :class:`~bakeoff.quality.optimizer.island.IslandLoop` state for
        a resume so the loop continues from the correct prompt rather than re-seeding
        from the fixed default.
        """
        audits = [a for a in self.read_audits() if a.model == model and a.island_id is not None]
        audits.sort(key=lambda a: (a.island_id or 0, a.iteration_index))
        champion: dict[int, str] = {}
        for a in audits:
            iid = a.island_id  # type: ignore[assignment]  — filtered above
            # Start from the champion at the beginning of this iteration; if accepted,
            # the challenger became the new champion.
            if a.accepted and a.challenger_instruction:
                champion[iid] = a.challenger_instruction
            else:
                champion[iid] = a.champion_instruction
        return champion
