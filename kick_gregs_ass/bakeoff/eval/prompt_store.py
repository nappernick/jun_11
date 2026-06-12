"""
The Prompt_Store (design Area D / Req 16): named, versioned ragas-metric prompt
overrides scoped to a run, with reset-to-default.

A researcher tuning evaluation quality wants to adapt a customizable ragas metric
to this domain — editing its instruction and few-shot examples — *without*
altering already-recorded measurements (Req 16). This module owns that override
state.

What it provides:

* a :class:`PromptConfig` value object — the instruction + few-shot examples for
  one metric, plus a version (``0`` == the ragas default, ``>= 1`` == an
  override) and a stable, content-derived :attr:`PromptConfig.config_id` that the
  Metric_Engine records alongside every produced value (Req 16.6), so a recorded
  value is traceable to the exact prompt that produced it;
* a :class:`PromptStore` that persists named, versioned overrides to a JSON file
  scoped to a run, supports reset-to-default (Req 16.4), and refuses to override a
  metric the catalog marks non-customizable (Req 16.7).

The ragas "modifying prompts in metrics" mechanism (read first).
---------------------------------------------------------------
ragas customizes a metric's prompt by **subclassing the metric** and swapping the
``PydanticPrompt`` it carries (its ``instruction`` + ``examples``). ragas is an
**optional, lazily-handled dependency** here (it is not installed in this
research environment — see :mod:`bakeoff.eval.ragas_adapter`), so this module
does **not** import ragas. It models the prompt as data: an instruction string
plus a list of input/output few-shot examples — exactly the two fields the ragas
subclassing mechanism overrides. The live Bedrock path (behind the ragas import
guard in :mod:`~bakeoff.eval.ragas_adapter`) is where a :class:`PromptConfig`
would be materialized into an actual metric subclass with the overridden
``PydanticPrompt``; that wiring is a **deploy-time seam to confirm against a live
ragas install**, mirroring the posture already recorded in the adapter. Offline,
the override is a recorded, versioned configuration whose **id** is what travels
onto every produced value.

Stability / scope posture (owner guidance: STABILITY, SIMPLICITY):

* Pure standard library (``dataclasses``, ``json``, ``hashlib``, ``threading``);
  no third-party deps, **no network**.
* Writes are atomic (temp file + ``os.replace``) and serialized under a lock so a
  concurrent reader (the offline run executes on a worker thread while the HTTP
  PUT mutates on the event loop) never observes a half-written file; reads are
  lock-free and tolerate a missing/garbled file by degrading to defaults.
* Operates only on synthetic, non-PII fields (Req 21.3).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

from bakeoff.eval import catalog

__all__ = [
    "PromptExample",
    "PromptConfig",
    "PromptStoreError",
    "UnknownMetricError",
    "PromptNotCustomizableError",
    "default_prompt_config",
    "PromptStore",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class PromptStoreError(RuntimeError):
    """Base error for the Prompt_Store."""


class UnknownMetricError(PromptStoreError):
    """Raised when an operation names a metric absent from the catalog."""


class PromptNotCustomizableError(PromptStoreError):
    """Raised when an override targets a metric the catalog marks non-customizable.

    The traditional non-LLM metrics (BLEU/ROUGE/…) and the embedding-only
    similarity metric expose no editable prompt (Req 16.7).
    """


# ---------------------------------------------------------------------------
# A single few-shot example (input -> output), the unit ragas overrides
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PromptExample:
    """One few-shot example: an ``input`` and the desired ``output``.

    Mirrors a single ragas ``PydanticPrompt`` example pair. Both fields are
    synthetic, non-PII strings (Req 21.3).
    """

    input: str
    output: str

    def to_dict(self) -> dict:
        return {"input": self.input, "output": self.output}

    @classmethod
    def from_dict(cls, d: dict) -> "PromptExample":
        return cls(input=str(d.get("input", "")), output=str(d.get("output", "")))


def _coerce_examples(examples: Optional[Iterable[object]]) -> tuple[PromptExample, ...]:
    """Coerce a loose examples list into a tuple of :class:`PromptExample`."""
    out: list[PromptExample] = []
    for ex in examples or ():
        if isinstance(ex, PromptExample):
            out.append(ex)
        elif isinstance(ex, dict):
            out.append(PromptExample.from_dict(ex))
        else:  # a bare string is treated as the input with an empty output
            out.append(PromptExample(input=str(ex), output=""))
    return tuple(out)


# ---------------------------------------------------------------------------
# PromptConfig — the instruction + examples + version for one metric
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PromptConfig:
    """A named, versioned prompt configuration for one ragas metric (Req 16.3).

    ``version == 0`` is the ragas default for the metric; ``version >= 1`` is a
    persisted override. :attr:`config_id` is stable and content-derived so two
    runs that produce the same override get the same id, and any change to the
    instruction or examples yields a new id — which is what makes a recorded
    value traceable to the exact prompt that produced it (Req 16.6).
    """

    metric: str
    instruction: str
    examples: tuple[PromptExample, ...] = field(default_factory=tuple)
    version: int = 0

    @property
    def is_override(self) -> bool:
        """``True`` iff this is a user override rather than the ragas default."""
        return self.version > 0

    def _content_json(self) -> str:
        """Canonical JSON of the prompt content (the config-id pre-image)."""
        return json.dumps(
            {
                "instruction": self.instruction,
                "examples": [e.to_dict() for e in self.examples],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    @property
    def config_id(self) -> str:
        """Stable identifier recorded alongside each produced value (Req 16.6).

        The default carries a fixed ``"{metric}:default"`` id; an override carries
        ``"{metric}:v{version}:{hash8}"`` where the hash is over the instruction +
        examples, so distinct prompt content always yields a distinct id.
        """
        if self.version <= 0:
            return f"{self.metric}:default"
        digest = hashlib.sha256(self._content_json().encode("utf-8")).hexdigest()[:8]
        return f"{self.metric}:v{self.version}:{digest}"

    def render(self, sample_input: str) -> str:
        """Render the fully composed prompt string for a sample input (Req 16.2).

        Deterministic and side-effect free: the instruction, then the few-shot
        examples, then the sample input under an ``Input:`` header. This is the
        offline analog of what the ragas metric's ``PydanticPrompt`` would emit;
        it gives the Prompt_Manager an exact rendered preview without ragas.
        """
        lines = [self.instruction.strip(), ""]
        for i, ex in enumerate(self.examples, start=1):
            lines.append(f"Example {i}:")
            lines.append(f"  Input: {ex.input}")
            lines.append(f"  Output: {ex.output}")
        lines.append("")
        lines.append(f"Input: {sample_input}")
        lines.append("Output:")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "metric": self.metric,
            "instruction": self.instruction,
            "examples": [e.to_dict() for e in self.examples],
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PromptConfig":
        return cls(
            metric=str(d["metric"]),
            instruction=str(d.get("instruction", "")),
            examples=_coerce_examples(d.get("examples")),
            version=int(d.get("version", 0)),
        )


# ---------------------------------------------------------------------------
# Default prompt content (the ragas default analog, offline)
# ---------------------------------------------------------------------------
def _default_instruction(metric: str) -> str:
    """A deterministic default instruction for a metric (the ragas-default analog).

    Offline, ragas is not importable so its real default prompt cannot be read;
    this is a stable, human-readable stand-in keyed on the metric name. The live
    path would surface the metric's actual ragas ``PydanticPrompt`` instruction.
    """
    pretty = metric.replace("_", " ")
    return (
        f"Evaluate the '{pretty}' of the answer given the question and the "
        f"retrieved context. Return a score in [0, 1] where higher is better."
    )


def default_prompt_config(metric: str) -> PromptConfig:
    """The version-0 default :class:`PromptConfig` for ``metric``."""
    return PromptConfig(
        metric=metric,
        instruction=_default_instruction(metric),
        examples=(
            PromptExample(
                input="question + context + answer",
                output="a single number in [0, 1]",
            ),
        ),
        version=0,
    )


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------
class PromptStore:
    """Persists named, versioned ragas-metric prompt overrides for a run (Req 16).

    Construct with a path to the per-run override file. The store is the single
    authority for "what prompt configuration is active for metric X *right now*";
    the Metric_Engine reads :meth:`config_id` at score time, so a change applies
    only to instances computed after it and previously recorded values are never
    touched (Req 16.5).
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    # -- persistence (atomic write, tolerant read) -----------------------
    def _load(self) -> dict:
        """Read the override file; degrade to an empty map on any problem."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return {"overrides": {}}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {"overrides": {}}
        if not isinstance(data, dict):
            return {"overrides": {}}
        if not isinstance(data.get("overrides"), dict):
            data["overrides"] = {}
        return data

    def _save(self, data: dict) -> None:
        """Atomically persist ``data`` (temp file + ``os.replace``)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    # -- catalog helpers -------------------------------------------------
    @staticmethod
    def _entry(metric: str):
        try:
            return catalog.get(metric)
        except KeyError as exc:  # unknown metric name
            raise UnknownMetricError(
                f"unknown ragas metric {metric!r}; not in the catalog"
            ) from exc

    def is_customizable(self, metric: str) -> bool:
        """``True`` iff ``metric`` exposes an editable prompt (Req 16.7)."""
        return bool(self._entry(metric).customizable_prompt)

    # -- reads -----------------------------------------------------------
    def current(self, metric: str) -> PromptConfig:
        """The active :class:`PromptConfig` for ``metric`` (override or default).

        Tolerates an unknown metric by returning a synthesized default, so the
        Metric_Engine can stamp a config id for any enabled metric without the
        store needing the catalog to agree — but :meth:`set_override` /
        :meth:`reset` still validate against the catalog.
        """
        override = self._load()["overrides"].get(metric)
        if override:
            try:
                return PromptConfig.from_dict(override)
            except Exception:  # noqa: BLE001 - a garbled override degrades to default
                pass
        return default_prompt_config(metric)

    def config_id(self, metric: str) -> str:
        """The id of the currently-active prompt configuration for ``metric``."""
        return self.current(metric).config_id

    # -- writes ----------------------------------------------------------
    def set_override(
        self,
        metric: str,
        *,
        instruction: str,
        examples: Optional[Iterable[object]] = None,
    ) -> PromptConfig:
        """Persist a new override for ``metric`` (Req 16.3); returns the new config.

        Increments the metric's version (so the id changes) and writes atomically.
        Raises :class:`UnknownMetricError` for an unknown metric and
        :class:`PromptNotCustomizableError` for a non-customizable one (Req 16.7).
        """
        entry = self._entry(metric)
        if not entry.customizable_prompt:
            raise PromptNotCustomizableError(
                f"metric {metric!r} does not support prompt customization (Req 16.7)"
            )
        with self._lock:
            data = self._load()
            overrides = data.setdefault("overrides", {})
            prev = overrides.get(metric)
            prev_version = int(prev.get("version", 0)) if isinstance(prev, dict) else 0
            cfg = PromptConfig(
                metric=metric,
                instruction=str(instruction),
                examples=_coerce_examples(examples),
                version=prev_version + 1,
            )
            overrides[metric] = cfg.to_dict()
            self._save(data)
            return cfg

    def reset(self, metric: str) -> PromptConfig:
        """Reset ``metric`` to its ragas default (Req 16.4); returns the default.

        Removing the override is idempotent — resetting a metric with no override
        is a no-op that still returns the default config.
        """
        self._entry(metric)  # validate the metric is known
        with self._lock:
            data = self._load()
            overrides = data.setdefault("overrides", {})
            if metric in overrides:
                del overrides[metric]
                self._save(data)
        return default_prompt_config(metric)

    # -- listing (backs GET /api/eval/prompts) ---------------------------
    def list_configs(
        self, metrics: Optional[Sequence[str]] = None
    ) -> list[dict]:
        """A JSON-ready row per metric: catalog metadata + active prompt config.

        Defaults to the whole catalog in priority order; pass ``metrics`` to scope
        it. Each row carries the metric's scope/family/customizable/external
        marking, the active config (instruction + examples + id + version +
        is_override), and the default config for reset-preview.
        """
        entries = (
            [catalog.get(m) for m in metrics]
            if metrics is not None
            else catalog.catalog_by_priority()
        )
        rows: list[dict] = []
        for entry in entries:
            cfg = self.current(entry.name)
            default = default_prompt_config(entry.name)
            rows.append(
                {
                    "name": entry.name,
                    "family": entry.family,
                    "scope": entry.scope,
                    "customizable": bool(entry.customizable_prompt),
                    "external": bool(entry.external),
                    "config_id": cfg.config_id,
                    "version": cfg.version,
                    "is_override": cfg.is_override,
                    "instruction": cfg.instruction,
                    "examples": [e.to_dict() for e in cfg.examples],
                    "default_instruction": default.instruction,
                    "default_examples": [e.to_dict() for e in default.examples],
                }
            )
        return rows

    def config_row(self, metric: str) -> dict:
        """The single :meth:`list_configs` row for ``metric`` (validates known)."""
        self._entry(metric)
        return self.list_configs([metric])[0]
