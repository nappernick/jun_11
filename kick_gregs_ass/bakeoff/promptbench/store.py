"""
Durable, append-only stores for Prompt Bench — completely separate files from every
optimizer store, so a Prompt Bench run never reads or writes optimizer data.

Two JSONL stores under :data:`config.PROMPT_BENCH_DIR`:

* **points** (:data:`config.PROMPT_BENCH_POINTS_PATH`) — one row per scored ``(prompt,
  conversation)``: the per-conversation overall score (the scatter's Y) at its pinned
  conversation index (the X), plus the cohort tags. Streamed live and reconstructed on
  reload so a refresh never blanks the plots.
* **results** (:data:`config.PROMPT_BENCH_RESULTS_PATH`) — one row per prompt when its pass
  completes: the aggregate triad + CI, the dimension/abstention breakdown, and the
  confident-wrong gate hit count.

Reset ARCHIVES (never destroys) both stores into a timestamped dir, mirroring the optimizer
v3 reset convention.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bakeoff import config

__all__ = ["PointRecord", "ResultRecord", "PromptBenchStore"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class PointRecord:
    """One scored conversation for one prompt — a single scatter point."""

    prompt_key: str
    conversation_index: int  # 1-based, the pinned sample position (scatter X)
    item_id: str
    answerability: str
    turns: int
    overall: float  # abstention-weighted per-conversation mean (scatter Y, 0..1)
    created_at: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        if not d["created_at"]:
            d["created_at"] = _now_iso()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PointRecord":
        return cls(
            prompt_key=str(d["prompt_key"]),
            conversation_index=int(d["conversation_index"]),
            item_id=str(d["item_id"]),
            answerability=str(d.get("answerability", "")),
            turns=int(d.get("turns", 0)),
            overall=float(d["overall"]),
            created_at=str(d.get("created_at", "")),
        )


@dataclass(frozen=True)
class ResultRecord:
    """One prompt's aggregate result when its pass completes."""

    prompt_key: str
    label: str
    triad: float
    ci_half_width: float
    ci_low: float
    ci_high: float
    n_conversations: int
    per_dimension_mean: dict
    abstention_reward_mean: float
    answered_when_unsure_rate: float
    confident_wrong_count: int
    created_at: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        if not d["created_at"]:
            d["created_at"] = _now_iso()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ResultRecord":
        return cls(
            prompt_key=str(d["prompt_key"]),
            label=str(d.get("label", d["prompt_key"])),
            triad=float(d["triad"]),
            ci_half_width=float(d.get("ci_half_width", 0.0)),
            ci_low=float(d.get("ci_low", 0.0)),
            ci_high=float(d.get("ci_high", 0.0)),
            n_conversations=int(d.get("n_conversations", 0)),
            per_dimension_mean=dict(d.get("per_dimension_mean", {})),
            abstention_reward_mean=float(d.get("abstention_reward_mean", 0.0)),
            answered_when_unsure_rate=float(d.get("answered_when_unsure_rate", 0.0)),
            confident_wrong_count=int(d.get("confident_wrong_count", 0)),
            created_at=str(d.get("created_at", "")),
        )


class PromptBenchStore:
    """Append-only, path-injectable IO over the two Prompt Bench JSONL stores."""

    def __init__(
        self,
        *,
        points_path: Path = config.PROMPT_BENCH_POINTS_PATH,
        results_path: Path = config.PROMPT_BENCH_RESULTS_PATH,
    ) -> None:
        self.points_path = Path(points_path)
        self.results_path = Path(results_path)

    # -- durable appends (one fsync'd JSONL line each) --------------------
    def append_point(self, rec: PointRecord) -> None:
        self._append(self.points_path, rec.to_dict())

    def append_result(self, rec: ResultRecord) -> None:
        self._append(self.results_path, rec.to_dict())

    @staticmethod
    def _append(path: Path, obj: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

    # -- reads (tolerate a truncated trailing line) -----------------------
    def read_points(self) -> list[PointRecord]:
        return [PointRecord.from_dict(d) for d in self._read(self.points_path)]

    def read_results(self) -> list[ResultRecord]:
        return [ResultRecord.from_dict(d) for d in self._read(self.results_path)]

    @staticmethod
    def _read(path: Path) -> list[dict]:
        if not path.exists():
            return []
        out: list[dict] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                break  # tolerate a single truncated trailing line
        return out

    def reconstruct(self) -> dict:
        """Durable backfill: prompts → their points + aggregate result (newest wins).

        Points are DEDUPED per prompt by ``item_id`` (a conversation's true identity),
        last-write-wins. The store is append-only, so re-scoring a prompt (a resumed/repeated
        run) appends a fresh row for an already-scored conversation; without dedup the scatter
        and the conversation count would exceed the sample size (e.g. 474 rows for a 400-item
        sample). Keeping the latest row per ``item_id`` bounds each prompt to ≤ one point per
        conversation and reflects the most recent score — mirroring the results' last-write-wins.
        """
        results = {r.prompt_key: r for r in self.read_results()}  # last write wins
        # Per prompt: item_id -> latest point dict (file order is append order, so a later
        # row for the same conversation overwrites the earlier one).
        latest_by_item: dict[str, dict[str, dict]] = {}
        for p in self.read_points():
            latest_by_item.setdefault(p.prompt_key, {})[p.item_id] = p.to_dict()
        points_by_prompt: dict[str, list[dict]] = {}
        for prompt_key, by_item in latest_by_item.items():
            pts = list(by_item.values())
            pts.sort(key=lambda d: d["conversation_index"])
            points_by_prompt[prompt_key] = pts
        return {
            "points": points_by_prompt,
            "results": {k: r.to_dict() for k, r in results.items()},
        }

    def archive(self) -> Optional[Path]:
        """Move both stores into a timestamped archive dir (never destroy). Returns the dir."""
        existing = [p for p in (self.points_path, self.results_path) if p.exists()]
        if not existing:
            return None
        archive_dir = config.PROMPT_BENCH_DIR / f"_archive_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        archive_dir.mkdir(parents=True, exist_ok=True)
        for path in existing:
            try:
                if path.stat().st_size == 0:
                    path.unlink()  # empty file: just remove it
                else:
                    os.replace(path, archive_dir / path.name)
            except OSError:
                pass  # best-effort; a locked/missing file must not fail the reset
        return archive_dir
