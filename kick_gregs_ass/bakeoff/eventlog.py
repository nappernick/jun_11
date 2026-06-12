"""
TrialEvent JSONL serialization, validation, and the durable append-only log.

This module owns the on-disk contract for the single source of truth (design
AD-1): every trial is one JSON object on one line in
``data/bakeoff/trial_events.jsonl``. The UI, the aggregation engine, and the
exec viz all *derive* from these lines, so the read/write path here must be:

* **Lossless** — ``from_jsonl(to_jsonl(e)) == e`` for every event (Req 8.5).
* **Single-line** — exactly one physical line per event, no embedded newlines,
  so the file is a true JSONL and a partial trailing line is unambiguous.
* **Crash-tolerant on read** — a process killed mid-``write`` can leave a
  truncated final line; :func:`read_events` discards *only* that trailing
  partial line and returns the complete prefix without raising (Req 8.5, design
  Error Scenario 5). A malformed line that is *not* the final line is real
  corruption and is surfaced loudly.
* **Validated** — :func:`validate_event` enforces the design's "Validation
  rules": the answerability/abstention coupling and the timing identity (Req
  8.3, Req 8.4).

Pure standard library (``json``, ``dataclasses``, ``math``, ``os``, ``pathlib``)
plus the existing :mod:`bakeoff.types`. No network, no third-party deps.
"""
from __future__ import annotations

import dataclasses
import json
import math
import os
from pathlib import Path
from typing import Union

from bakeoff.types import (
    AccuracyScores,
    CohortKey,
    JudgeScores,
    QualityScores,
    RetrievalRecord,
    StageTimings,
    TrialEvent,
)

__all__ = [
    "to_jsonl",
    "from_jsonl",
    "append_event",
    "read_events",
    "validate_event",
    "EventLogError",
]

PathLike = Union[str, "os.PathLike[str]"]

# ---------------------------------------------------------------------------
# Timing-identity tolerance (design "Validation rules", Req 8.4)
# ---------------------------------------------------------------------------
# ``end_to_end_ms == retrieval_total_ms + generation_total_ms`` is asserted
# within a float epsilon. We accept a match within either a relative tolerance
# of 1e-6 OR an absolute tolerance of 1e-3 ms (i.e. one microsecond), which is
# exactly the semantics of ``math.isclose(rel_tol=1e-6, abs_tol=1e-3)``: the
# comparison passes when ``|a-b| <= max(1e-6*max(|a|,|b|), 1e-3)``. The absolute
# floor keeps near-zero timings from being held to an unreasonably tight bar;
# the relative term keeps large end-to-end figures honest.
_TIMING_REL_TOL: float = 1e-6
_TIMING_ABS_TOL: float = 1e-3


class EventLogError(ValueError):
    """Raised when the event log is genuinely corrupt.

    Subclasses :class:`ValueError` so callers can catch it with the broad
    ``ValueError`` net (the same net that already covers
    :class:`json.JSONDecodeError`). A *truncated final line* is NOT corruption
    and never raises this — see :func:`read_events`.
    """


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
def to_jsonl(event: TrialEvent) -> str:
    """Serialize a :class:`TrialEvent` to a single-line JSON string.

    Nested frozen dataclasses (:class:`CohortKey`, :class:`RetrievalRecord`,
    :class:`StageTimings`, :class:`QualityScores` and its
    :class:`AccuracyScores`/:class:`JudgeScores`) become nested JSON objects via
    :func:`dataclasses.asdict`. The result contains no embedded newline: any
    ``"\\n"`` inside a string field is JSON-escaped to the two characters
    ``\\`` + ``n``, so the returned value is always exactly one physical line.

    Args:
        event: the fully-populated trial event to serialize.

    Returns:
        A single-line JSON string (no trailing newline).
    """
    payload = dataclasses.asdict(event)
    # compact separators -> no spaces; default settings never emit newlines.
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def from_jsonl(line: str) -> TrialEvent:
    """Parse one JSON line back into a fully-typed :class:`TrialEvent`.

    Every nested dataclass is reconstructed, so the result is type-identical to
    the original and ``from_jsonl(to_jsonl(e)) == e`` holds for any event with
    finite numeric fields (dataclass ``__eq__`` does the comparison). JSON
    preserves number types (an integer literal parses to ``int``, a literal with
    a decimal point or exponent parses to ``float``), and :func:`json.dumps`
    always renders Python floats with a decimal point, so ``int``/``float``
    field types survive the round-trip without coercion.

    Args:
        line: one JSON object on a single line (a trailing newline is fine).

    Returns:
        The reconstructed :class:`TrialEvent`.

    Raises:
        json.JSONDecodeError: if ``line`` is not valid JSON.
        KeyError / TypeError: if a required field is missing or malformed.
    """
    d = json.loads(line)
    return _event_from_dict(d)


def _event_from_dict(d: dict) -> TrialEvent:
    """Rebuild a :class:`TrialEvent` (and every nested dataclass) from a dict."""
    return TrialEvent(
        # --- identity ---
        trial_id=d["trial_id"],
        schema_version=d["schema_version"],
        plan_version=d["plan_version"],
        # --- what was run ---
        model=d["model"],
        item_id=d["item_id"],
        turn_type=d["turn_type"],
        pass_name=d["pass_name"],
        rep=d["rep"],
        temperature=d["temperature"],
        cohort=CohortKey(**d["cohort"]),
        # --- inputs captured for replay/audit ---
        query=d["query"],
        gold_node_ids=list(d["gold_node_ids"]),
        answerability=d["answerability"],
        retrieval=RetrievalRecord(
            fragment_ids=list(d["retrieval"]["fragment_ids"]),
            confidence=list(d["retrieval"]["confidence"]),
            cache_hit=d["retrieval"]["cache_hit"],
        ),
        # --- outputs ---
        answer_text=d["answer_text"],
        token_usage=dict(d["token_usage"]),
        timings=StageTimings(**d["timings"]),
        quality=_quality_from_dict(d["quality"]),
        # --- provenance ---
        started_at=d["started_at"],
        completed_at=d["completed_at"],
        error=d["error"],
    )


def _quality_from_dict(q: dict) -> QualityScores:
    """Rebuild a :class:`QualityScores` and its nested score dataclasses.

    The judge block is read field-by-field against the CURRENT
    :class:`JudgeScores` schema (faithfulness/correctness/completeness), ignoring
    any extra keys a previously-serialized row may carry (e.g. the removed
    tone/empathy/clarity/actionability dimensions). Missing judge dimensions
    default to 0.0, so a stale Phase-1 ``(deferred)`` judge block — whose
    dimensions were all zero anyway — loads cleanly under the narrowed schema.
    """
    j = q["judge"]
    judge = JudgeScores(
        faithfulness=float(j.get("faithfulness", 0.0)),
        correctness=float(j.get("correctness", 0.0)),
        completeness=float(j.get("completeness", 0.0)),
        judge_sample_count=int(j.get("judge_sample_count", 0)),
        judge_model=str(j.get("judge_model", "")),
        judge_dim_sd=dict(j.get("judge_dim_sd") or {}),
    )
    return QualityScores(
        accuracy=AccuracyScores(**q["accuracy"]),
        judge=judge,
        composite=q["composite"],
        composite_weights_version=q["composite_weights_version"],
    )


# ---------------------------------------------------------------------------
# Durable append-only write (design AD-1)
# ---------------------------------------------------------------------------
def append_event(path: PathLike, event: TrialEvent) -> None:
    """Append exactly one event line to the log at ``path``, durably.

    Parent directories are created if missing. The line (``to_jsonl(event)`` +
    one ``"\\n"``) is emitted in a single :meth:`write` call in append mode, then
    flushed and ``fsync``'d so the record is on disk before returning — this is
    the durable source of truth a resumed run diffs against (AD-1). The
    single-write-of-a-complete-line discipline is what makes a crash leave at
    most one truncated trailing line, which :func:`read_events` tolerates.

    Args:
        path: destination JSONL file (created if absent).
        event: the event to append.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = to_jsonl(event) + "\n"
    with open(p, "a", encoding="utf-8") as f:
        f.write(line)          # single write of the complete line
        f.flush()
        os.fsync(f.fileno())   # durability: the record is on disk on return


# ---------------------------------------------------------------------------
# Crash-tolerant read (Req 8.5, design Error Scenario 5)
# ---------------------------------------------------------------------------
def read_events(path: PathLike) -> list[TrialEvent]:
    """Read all events from the log, tolerating a truncated final line.

    Behavior:

    * A missing file yields ``[]`` (a run that has not written anything yet).
    * Every complete line is parsed into a :class:`TrialEvent`.
    * If the **final** line fails to parse — the signature of a process killed
      mid-``write`` (design Error Scenario 5) — that single trailing partial
      line is discarded and the complete prefix is returned, *without raising*.
    * A line that fails to parse but is **not** the final line is treated as
      real corruption and raises :class:`EventLogError`.

    Args:
        path: the JSONL log to read.

    Returns:
        The list of successfully-parsed events (the complete prefix).

    Raises:
        EventLogError: if a non-final line is malformed.
    """
    p = Path(path)
    if not p.exists():
        return []

    with open(p, "r", encoding="utf-8") as f:
        # ``readlines`` keeps line terminators, so we never invent a phantom
        # trailing empty line the way ``str.split("\n")`` would.
        raw_lines = f.readlines()

    events: list[TrialEvent] = []
    last_index = len(raw_lines) - 1
    for i, raw in enumerate(raw_lines):
        is_last = i == last_index
        line = raw.rstrip("\n")
        try:
            events.append(from_jsonl(line))
        except Exception as exc:  # noqa: BLE001 - re-raised below unless final
            if is_last:
                # Crash-truncated trailing line: discard just this one line.
                break
            raise EventLogError(
                f"malformed non-final line {i + 1} in {p}: {exc}"
            ) from exc
    return events


# ---------------------------------------------------------------------------
# Validation (design "Validation rules", Req 8.3 / Req 8.4)
# ---------------------------------------------------------------------------
def validate_event(event: TrialEvent) -> None:
    """Enforce the design's per-event validation rules.

    Rules (raise :class:`ValueError` on any violation):

    1. ``abstention_correct`` is populated (not ``None``) **iff**
       ``answerability in {"none", "partial"}`` — and therefore MUST be ``None``
       when ``answerability == "full"`` (Req 8.3).
    2. ``unwarranted_refusal`` is populated **iff** ``answerability == "full"``
       — and therefore MUST be ``None`` otherwise (Req 8.3).
    3. For non-error events (``event.error is None``):
       ``timings.end_to_end_ms == retrieval_total_ms + generation_total_ms``
       within a float epsilon (relative 1e-6 or absolute 1e-3 ms; see
       :data:`_TIMING_REL_TOL` / :data:`_TIMING_ABS_TOL`) (Req 8.4).

    Error events (``event.error`` set) are intentionally NOT held to the timing
    identity: their fields are best-effort partial captures (Req 7.5), so only
    the answerability/abstention coupling is checked.

    Args:
        event: the event to validate.

    Raises:
        ValueError: if any rule is violated, with a message naming the rule.
    """
    answerability = event.answerability
    acc = event.quality.accuracy

    # --- Rule 1: abstention_correct populated iff answerability in {none, partial}
    expects_abstention = answerability in ("none", "partial")
    has_abstention = acc.abstention_correct is not None
    if expects_abstention and not has_abstention:
        raise ValueError(
            f"abstention_correct must be populated when answerability="
            f"{answerability!r} (in {{'none','partial'}}), but it is None"
        )
    if not expects_abstention and has_abstention:
        raise ValueError(
            f"abstention_correct must be None when answerability="
            f"{answerability!r} (not in {{'none','partial'}}), but it is "
            f"{acc.abstention_correct!r}"
        )

    # --- Rule 2: unwarranted_refusal populated iff answerability == full
    expects_refusal = answerability == "full"
    has_refusal = acc.unwarranted_refusal is not None
    if expects_refusal and not has_refusal:
        raise ValueError(
            "unwarranted_refusal must be populated when answerability=='full', "
            "but it is None"
        )
    if not expects_refusal and has_refusal:
        raise ValueError(
            f"unwarranted_refusal must be None when answerability="
            f"{answerability!r} (!= 'full'), but it is "
            f"{acc.unwarranted_refusal!r}"
        )

    # --- Rule 3: timing identity for non-error events
    if event.error is None:
        t = event.timings
        expected = t.retrieval_total_ms + t.generation_total_ms
        if not math.isclose(
            t.end_to_end_ms,
            expected,
            rel_tol=_TIMING_REL_TOL,
            abs_tol=_TIMING_ABS_TOL,
        ):
            raise ValueError(
                "timing identity violated: end_to_end_ms="
                f"{t.end_to_end_ms!r} != retrieval_total_ms + "
                f"generation_total_ms = {t.retrieval_total_ms!r} + "
                f"{t.generation_total_ms!r} = {expected!r} "
                f"(rel_tol={_TIMING_REL_TOL}, abs_tol={_TIMING_ABS_TOL})"
            )
