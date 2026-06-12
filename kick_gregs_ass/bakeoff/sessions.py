"""Bake-Off session registry and active-session routing.

This module keeps the historical root data readable as ``legacy-root`` while
letting new Bake-Off experiments write into isolated session directories under
``data/bakeoff/sessions/``.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from bakeoff import config
from bakeoff.eventlog import read_events

__all__ = ["BakeOffSession", "BakeOffSessionManager"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as temp_file:
        json.dump(payload, temp_file, ensure_ascii=False, indent=2, sort_keys=True)
        temp_file.write("\n")
        temp_name = temp_file.name
    os.replace(temp_name, path)


def _slugify_label(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return slug or "inline-run"


@dataclasses.dataclass(frozen=True)
class BakeOffSession:
    id: str
    label: str
    notes: str
    created_at: str
    updated_at: str
    archived: bool
    kind: str
    root: Path
    outcomes_path: Path
    run_errors_path: Path
    judge_scores_path: Path
    reports_dir: Path
    prompt_path: Path
    roster: tuple[str, ...]
    roster_signature: str
    total_trials: int = 0
    total_errors: int = 0
    judge_scores_total: int = 0
    models: tuple[str, ...] = ()

    def to_manifest_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "notes": self.notes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "archived": self.archived,
            "kind": self.kind,
            "root": str(self.root),
            "outcomes_path": str(self.outcomes_path),
            "run_errors_path": str(self.run_errors_path),
            "judge_scores_path": str(self.judge_scores_path),
            "reports_dir": str(self.reports_dir),
            "prompt_path": str(self.prompt_path),
            "roster": list(self.roster),
            "roster_signature": self.roster_signature,
        }

    def to_api_dict(self) -> dict:
        return {
            **self.to_manifest_dict(),
            "total_trials": self.total_trials,
            "total_errors": self.total_errors,
            "judge_scores_total": self.judge_scores_total,
            "models": list(self.models),
        }

    @classmethod
    def from_manifest_dict(cls, payload: dict) -> "BakeOffSession":
        roster = tuple(str(entry) for entry in payload.get("roster", ()))
        return cls(
            id=str(payload["id"]),
            label=str(payload.get("label", "")),
            notes=str(payload.get("notes", "")),
            created_at=str(payload.get("created_at", "")),
            updated_at=str(payload.get("updated_at", payload.get("created_at", ""))),
            archived=bool(payload.get("archived", False)),
            kind=str(payload.get("kind", "session")),
            root=Path(str(payload.get("root", ""))),
            outcomes_path=Path(str(payload.get("outcomes_path", ""))),
            run_errors_path=Path(str(payload.get("run_errors_path", ""))),
            judge_scores_path=Path(str(payload.get("judge_scores_path", ""))),
            reports_dir=Path(str(payload.get("reports_dir", ""))),
            prompt_path=Path(str(payload.get("prompt_path", config.BAKEOFF_UNIVERSAL_PROMPT_PATH))),
            roster=roster,
            roster_signature=str(
                payload.get("roster_signature", "|".join(sorted(roster)))
            ),
        )

    def with_summary(
        self,
        *,
        total_trials: int,
        total_errors: int,
        judge_scores_total: int,
        models: Sequence[str],
    ) -> "BakeOffSession":
        return dataclasses.replace(
            self,
            total_trials=total_trials,
            total_errors=total_errors,
            judge_scores_total=judge_scores_total,
            models=tuple(models),
        )


class BakeOffSessionManager:
    """Track Bake-Off sessions and the current active session."""

    def __init__(
        self,
        *,
        sessions_dir: Path | None = None,
        manifest_path: Path | None = None,
        active_session_path: Path | None = None,
        legacy_outcomes_path: Path | None = None,
        legacy_run_errors_path: Path | None = None,
        legacy_judge_scores_path: Path | None = None,
        legacy_reports_dir: Path | None = None,
        prompt_path: Path | None = None,
        roster: Sequence[str] | None = None,
    ) -> None:
        self.legacy_outcomes_path = Path(
            legacy_outcomes_path if legacy_outcomes_path is not None else config.OUTCOMES_PATH
        )
        self.legacy_run_errors_path = Path(
            legacy_run_errors_path
            if legacy_run_errors_path is not None
            else self.legacy_outcomes_path.parent / "run_errors.jsonl"
        )
        self.legacy_judge_scores_path = Path(
            legacy_judge_scores_path
            if legacy_judge_scores_path is not None
            else self.legacy_outcomes_path.parent / "judge_scores.jsonl"
        )
        self.legacy_reports_dir = Path(
            legacy_reports_dir
            if legacy_reports_dir is not None
            else self.legacy_outcomes_path.parent / "reports"
        )
        self.prompt_path = Path(
            prompt_path if prompt_path is not None else config.BAKEOFF_UNIVERSAL_PROMPT_PATH
        )
        self.roster = tuple(
            str(entry)
            for entry in (
                roster
                if roster is not None
                else [candidate.name for candidate in config.CANDIDATE_MODELS if candidate.enabled]
            )
        )
        self.roster_signature = "|".join(sorted(self.roster))

        base_sessions_dir = (
            Path(sessions_dir)
            if sessions_dir is not None
            else self.legacy_outcomes_path.parent / "sessions"
        )
        self.sessions_dir = base_sessions_dir
        self.manifest_path = (
            Path(manifest_path)
            if manifest_path is not None
            else self.sessions_dir / "manifest.json"
        )
        self.active_session_path = (
            Path(active_session_path)
            if active_session_path is not None
            else self.sessions_dir / "active_session.json"
        )

        config.ensure_dirs()
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._sessions_by_id: dict[str, BakeOffSession] = {}
        self._active_session_id: str = "legacy-root"
        self._load_or_initialize()

    # ------------------------------------------------------------------
    # Internal persistence helpers
    # ------------------------------------------------------------------
    def _legacy_session(self) -> BakeOffSession:
        return BakeOffSession(
            id="legacy-root",
            label="Legacy root Bake-Off data",
            notes="",
            created_at="",
            updated_at="",
            archived=False,
            kind="legacy",
            root=self.legacy_outcomes_path.parent,
            outcomes_path=self.legacy_outcomes_path,
            run_errors_path=self.legacy_run_errors_path,
            judge_scores_path=self.legacy_judge_scores_path,
            reports_dir=self.legacy_reports_dir,
            prompt_path=self.prompt_path,
            roster=self.roster,
            roster_signature=self.roster_signature,
        )

    def _session_root(self, session_id: str) -> Path:
        return self.sessions_dir / session_id

    def _build_session(self, payload: dict) -> BakeOffSession:
        return BakeOffSession.from_manifest_dict(payload)

    def _session_payload(self, session: BakeOffSession) -> dict:
        return session.to_manifest_dict()

    def _persist_manifest(self) -> None:
        payload = {
            "version": 1,
            "active_session_id": self._active_session_id,
            "sessions": {
                session_id: self._session_payload(session)
                for session_id, session in self._sessions_by_id.items()
            },
        }
        _write_json_atomic(self.manifest_path, payload)

    def _persist_active_pointer(self) -> None:
        _write_json_atomic(
            self.active_session_path,
            {
                "active_session_id": self._active_session_id,
                "updated_at": _now_iso(),
            },
        )

    def _load_or_initialize(self) -> None:
        manifest_payload = _read_json(self.manifest_path)
        if not manifest_payload:
            legacy_session = self._legacy_session()
            self._sessions_by_id = {legacy_session.id: legacy_session}
            self._active_session_id = legacy_session.id
            self._persist_manifest()
            self._persist_active_pointer()
            return

        sessions_payload = manifest_payload.get("sessions") or {}
        if isinstance(sessions_payload, dict):
            for session_id, session_payload in sessions_payload.items():
                if isinstance(session_payload, dict):
                    session = self._build_session(session_payload)
                    self._sessions_by_id[session_id] = session

        legacy_session = self._legacy_session()
        self._sessions_by_id.setdefault(legacy_session.id, legacy_session)

        active_pointer_payload = _read_json(self.active_session_path)
        active_session_id = str(active_pointer_payload.get("active_session_id", "")).strip()
        if not active_session_id:
            active_session_id = str(manifest_payload.get("active_session_id", "")).strip()

        if not self._is_valid_active_candidate(active_session_id):
            active_session_id = self._choose_default_active_session_id()

        self._active_session_id = active_session_id
        self._persist_manifest()
        self._persist_active_pointer()

    def _is_valid_active_candidate(self, session_id: str) -> bool:
        session = self._sessions_by_id.get(session_id)
        return bool(session and not session.archived)

    def _choose_default_active_session_id(self) -> str:
        active_sessions = [
            session
            for session in self._sessions_by_id.values()
            if session.kind != "legacy" and not session.archived
        ]
        if active_sessions:
            active_sessions.sort(
                key=lambda session: (session.created_at or session.updated_at, session.id),
                reverse=True,
            )
            return active_sessions[0].id
        return "legacy-root"

    def _require_session(self, session_id: str) -> BakeOffSession:
        try:
            return self._sessions_by_id[session_id]
        except KeyError as exc:
            raise KeyError(session_id) from exc

    def _ensure_session_storage(self, session: BakeOffSession) -> None:
        session.root.mkdir(parents=True, exist_ok=True)
        session.reports_dir.mkdir(parents=True, exist_ok=True)
        session.outcomes_path.touch(exist_ok=True)
        session.run_errors_path.touch(exist_ok=True)
        session.judge_scores_path.touch(exist_ok=True)

    def _updated_session(self, session: BakeOffSession, **changes) -> BakeOffSession:
        return dataclasses.replace(session, **changes, updated_at=_now_iso())

    def _store_session(self, session: BakeOffSession) -> None:
        self._sessions_by_id[session.id] = session
        self._persist_manifest()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def paths_for(self, session_id: str) -> dict[str, Path]:
        session = self._require_session(session_id)
        return {
            "root": session.root,
            "outcomes_path": session.outcomes_path,
            "run_errors_path": session.run_errors_path,
            "judge_scores_path": session.judge_scores_path,
            "reports_dir": session.reports_dir,
            "prompt_path": session.prompt_path,
        }

    def summary(self, session_id: str) -> BakeOffSession:
        session = self._require_session(session_id)
        outcome_records = read_events(session.outcomes_path)
        error_records = read_events(session.run_errors_path)
        from bakeoff.judge_phase2 import read_judge_scores

        judge_records = read_judge_scores(session.judge_scores_path)
        models = sorted({record.model for record in outcome_records})
        return session.with_summary(
            total_trials=len(outcome_records),
            total_errors=len(error_records),
            judge_scores_total=len(judge_records),
            models=models,
        )

    def active(self) -> BakeOffSession:
        return self.summary(self._active_session_id)

    def list(self) -> list[BakeOffSession]:
        active_session = self._active_session_id
        ordered_session_ids = [active_session]
        remaining_sessions = [
            session
            for session_id, session in self._sessions_by_id.items()
            if session_id != active_session
        ]
        remaining_sessions.sort(
            key=lambda session: (
                session.kind == "legacy",
                session.archived,
                session.created_at or session.updated_at,
                session.id,
            ),
            reverse=False,
        )
        ordered_session_ids.extend(session.id for session in remaining_sessions)
        return [self.summary(session_id) for session_id in ordered_session_ids]

    def snapshot(self) -> dict:
        return {
            "active_session_id": self._active_session_id,
            "sessions": [session.to_api_dict() for session in self.list()],
        }

    def create(self, label: str | None, notes: str | None) -> BakeOffSession:
        label_text = (label or "").strip() or "Inline run"
        notes_text = (notes or "").strip()
        timestamp = _now_iso()
        session_id_base = f"{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{_slugify_label(label_text)}"
        session_id = session_id_base
        suffix = 2
        while session_id in self._sessions_by_id or self._session_root(session_id).exists():
            session_id = f"{session_id_base}-{suffix}"
            suffix += 1
        root = self._session_root(session_id)
        session = BakeOffSession(
            id=session_id,
            label=label_text,
            notes=notes_text,
            created_at=timestamp,
            updated_at=timestamp,
            archived=False,
            kind="session",
            root=root,
            outcomes_path=root / "outcomes.jsonl",
            run_errors_path=root / "run_errors.jsonl",
            judge_scores_path=root / "judge_scores.jsonl",
            reports_dir=root / "reports",
            prompt_path=self.prompt_path,
            roster=self.roster,
            roster_signature=self.roster_signature,
        )
        self._ensure_session_storage(session)
        self._sessions_by_id[session.id] = session
        self._active_session_id = session.id
        self._persist_manifest()
        self._persist_active_pointer()
        return self.summary(session.id)

    def activate(self, session_id: str) -> BakeOffSession:
        session = self._require_session(session_id)
        if session.archived:
            raise ValueError(session_id)
        self._active_session_id = session.id
        self._persist_manifest()
        self._persist_active_pointer()
        return self.summary(session.id)

    def update(
        self,
        session_id: str,
        *,
        label: Optional[str] = None,
        notes: Optional[str] = None,
        archived: Optional[bool] = None,
    ) -> BakeOffSession:
        session = self._require_session(session_id)
        if archived is True and session.id == self._active_session_id:
            raise RuntimeError("cannot archive the active session")

        updated_label = session.label
        if label is not None:
            stripped_label = label.strip()
            if stripped_label:
                updated_label = stripped_label
        updated_notes = session.notes
        if notes is not None:
            updated_notes = notes.strip()
        updated_archived = session.archived if archived is None else bool(archived)

        updated_session = self._updated_session(
            session,
            label=updated_label,
            notes=updated_notes,
            archived=updated_archived,
        )
        self._store_session(updated_session)
        if updated_session.id == self._active_session_id:
            self._persist_active_pointer()
        return self.summary(updated_session.id)

    def set_active_session(self, session_id: str) -> BakeOffSession:
        return self.activate(session_id)
