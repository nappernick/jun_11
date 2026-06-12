"""
The durable, append-only Event_Store for the eval dashboard (Req 8.1, 8.2).

This is the **single source of truth** feeding every dashboard view: the status
endpoint, the SSE replay seed, and every 2D/3D chart all *derive* from the
records written here, so the complete state of every view is reconstructable from
the store alone (Req 8.2). It mirrors the discipline already proven in
:mod:`bakeoff.eventlog` rather than reinventing it:

* **One record per line.** Each :class:`~bakeoff.eval.models.EvalInstance` is
  serialized with :meth:`EvalInstance.to_dict` to a single-line, newline-free
  JSON object, so the file is a true JSONL and a partial trailing line is
  unambiguous.
* **Durable append.** :meth:`EvalEventStore.append` writes the complete line in a
  single ``write`` in append mode, then ``flush`` + ``fsync`` so the record is on
  disk before returning. The single-write-of-a-complete-line discipline is what
  makes a crash leave at most one truncated trailing line.
* **Crash-tolerant read.** :meth:`EvalEventStore.read_all` discards *only* a
  truncated/malformed **final** line (the signature of a process killed
  mid-write) and returns the complete prefix without raising. A malformed line
  that is NOT the final line is real corruption and is surfaced loudly via
  :class:`EvalEventStoreError`.

Pure standard library (``json``, ``os``, ``pathlib``) plus
:mod:`bakeoff.eval.models`. No network, no third-party deps.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Union

from bakeoff.eval.models import EvalInstance

__all__ = ["EvalEventStore", "EvalEventStoreError"]

PathLike = Union[str, "os.PathLike[str]"]


class EvalEventStoreError(ValueError):
    """Raised when the Event_Store is genuinely corrupt.

    Subclasses :class:`ValueError` so callers can catch it with the broad
    ``ValueError`` net (the same net that already covers
    :class:`json.JSONDecodeError`). A *truncated final line* is NOT corruption
    and never raises this — see :meth:`EvalEventStore.read_all`.
    """


class EvalEventStore:
    """An append-only JSONL store of :class:`EvalInstance` records.

    Bound to a single ``path``. Constructing a fresh instance over the same path
    reads exactly the same records (durability across reader instances), because
    all state lives in the file, never in memory.
    """

    def __init__(self, path: PathLike) -> None:
        self.path = Path(path)

    # --- durable append (Req 8.1) ---------------------------------------
    def append(self, instance: EvalInstance) -> None:
        """Append exactly one record line to the store, durably.

        Parent directories are created if missing. The line
        (``json(instance.to_dict())`` + one ``"\\n"``) is emitted in a single
        :meth:`write` in append mode, then flushed and ``fsync``'d so the record
        is on disk before returning. The single-write-of-a-complete-line
        discipline is what makes a crash leave at most one truncated trailing
        line, which :meth:`read_all` tolerates.

        Args:
            instance: the record to append.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # compact separators -> no spaces; default settings never emit newlines,
        # so the serialized record is always exactly one physical line.
        line = json.dumps(
            instance.to_dict(), ensure_ascii=False, separators=(",", ":")
        ) + "\n"
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)          # single write of the complete line
            f.flush()
            os.fsync(f.fileno())   # durability: the record is on disk on return

    # --- crash-tolerant read (Req 8.2) ----------------------------------
    def read_all(self) -> list[EvalInstance]:
        """Read every record in append order, tolerating a truncated final line.

        Behavior (mirrors :func:`bakeoff.eventlog.read_events`):

        * A missing file yields ``[]`` (a store that has not been written yet).
        * Every complete line is parsed into an :class:`EvalInstance`.
        * If the **final** line fails to parse — the signature of a process
          killed mid-``append`` — that single trailing partial line is discarded
          and the complete prefix is returned, *without raising*.
        * A line that fails to parse but is **not** the final line is treated as
          real corruption and raises :class:`EvalEventStoreError`.

        Returns:
            The list of successfully-parsed records (the complete prefix), in the
            order they were appended — sufficient to fully reconstruct every view
            (Req 8.2).

        Raises:
            EvalEventStoreError: if a non-final line is malformed.
        """
        if not self.path.exists():
            return []

        with open(self.path, "r", encoding="utf-8") as f:
            # readlines keeps line terminators, so we never invent a phantom
            # trailing empty line the way str.split("\n") would.
            raw_lines = f.readlines()

        records: list[EvalInstance] = []
        last_index = len(raw_lines) - 1
        for i, raw in enumerate(raw_lines):
            is_last = i == last_index
            line = raw.rstrip("\n")
            try:
                records.append(EvalInstance.from_dict(json.loads(line)))
            except Exception as exc:  # noqa: BLE001 - re-raised below unless final
                if is_last:
                    # Crash-truncated trailing line: discard just this one line.
                    break
                raise EvalEventStoreError(
                    f"malformed non-final line {i + 1} in {self.path}: {exc}"
                ) from exc
        return records

    def reconstruct(self) -> list[EvalInstance]:
        """Alias for :meth:`read_all` — the full reconstruction of view state.

        Named to match the requirement language: the complete state of every
        view is *reconstructed* from the store alone (Req 8.2).
        """
        return self.read_all()

    def read_recent(self, limit: int) -> list[EvalInstance]:
        """Return the most recently appended records, up to ``limit``.

        Used by the dashboard's replay-seed endpoint. The records are returned in
        append order (oldest of the recent window first), so they flow the same
        code path as the live stream. A non-positive ``limit`` yields ``[]``.

        Args:
            limit: maximum number of trailing records to return.

        Returns:
            The last ``min(limit, len)`` records in append order.
        """
        if limit <= 0:
            return []
        return self.read_all()[-limit:]
