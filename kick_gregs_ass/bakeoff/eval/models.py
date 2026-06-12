"""
EvalInstance data contract + JSON (de)serialization for the eval dashboard.

This module owns the on-disk / over-the-wire contract for the dashboard's atomic
unit, the **EvalInstance** (design C1): one record per
``(Agent_Under_Test, query, corpus_size)`` execution. Every downstream view —
2D/3D charts, the durable-backfill status endpoint, the SSE stream — *derives*
from these records, so the construction and round-trip path here must be
**lossless, validated, and traceable** (correctness outranks latency for this
feature).

It deliberately mirrors the discipline already established in
:mod:`bakeoff.types` / :mod:`bakeoff.eventlog`: **frozen dataclasses** (immutable
value objects), pure standard library (``dataclasses``, ``math``, ``json`` at the
store layer), no third-party deps, and a ``to_dict`` / ``from_dict`` pair whose
round-trip is exact (``from_dict(to_dict(x)) == x``).

Construction-time validation (design C1 + "EvalInstance" Data Models):

* **Metric range-clamping (Req 1.3, 2, P3).** Every :class:`MetricValue` score is
  clamped to the unit interval ``[0, 1]`` on construction; a non-finite score is
  coerced to ``0.0``. A recorded metric value never escapes that range.
* **value / unavailable coupling (Req 1.4, 2.3, 3.5, P10).** A
  :class:`MetricValue` is consistent iff ``value is None`` exactly when
  ``unavailable is True``. An inconsistent pair is a programming error and raises.
* **Disjoint metric maps (Req 2.4, P9).** ``EvalInstance.ragas`` (generation
  quality) and ``EvalInstance.retrieval`` (gold-link retrieval quality) are kept
  in separate maps and their key sets MUST be disjoint, so the two signals are
  never conflated in storage.
* **Latency flagged, not dropped (Req 7.2, design Data Models / P7).** A
  non-positive or non-finite ``latency_ms`` is **flagged**
  (:attr:`EvalInstance.latency_flagged`) rather than rejected: the record is still
  recorded and visible (the log axis floors it for display), so no measurement is
  silently lost.
* **Per-stage timings kept separate from end-to-end latency (Req 7.3).**
  :class:`StageTimings` (retrieval / generation / extra) is stored *alongside*,
  and independently of, ``EvalInstance.latency_ms`` — the two are never summed or
  derived from one another here.

Operates only on synthetic, non-PII fields (Req 21.3).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

__all__ = [
    "clamp_unit",
    "MetricValue",
    "StageTimings",
    "EvalInstance",
]


# ---------------------------------------------------------------------------
# Unit-interval clamping (Req 1.3, 2, design Property 3)
# ---------------------------------------------------------------------------
def clamp_unit(x: float) -> float:
    """Clamp a numeric score to the unit interval ``[0.0, 1.0]``.

    A non-finite input (``nan``/``inf``/``-inf``) is coerced to ``0.0`` rather
    than propagated, mirroring the frontend ``clampUnit`` (design C2 / P3): a
    recorded metric value is always a finite number in ``[0, 1]``.

    Args:
        x: the raw metric score.

    Returns:
        ``float(x)`` clamped to ``[0.0, 1.0]``; ``0.0`` if ``x`` is not finite.
    """
    if not math.isfinite(x):
        return 0.0
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return float(x)


# ---------------------------------------------------------------------------
# MetricValue — one metric score plus reproducibility provenance
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MetricValue:
    """A single metric value plus the provenance that makes it reproducible.

    Used for both ragas generation-quality metrics (which carry ``ragas_version``
    + ``bedrock_model_id``, Req 1.2) and gold-link retrieval metrics (which carry
    the ``k`` used, Req 2.2).

    Invariants enforced on construction:

    * ``value`` is ``None`` **iff** ``unavailable`` is ``True`` (Req 1.4, 2.3,
      3.5 / P10). An inconsistent pair raises :class:`ValueError`.
    * an available ``value`` is clamped to ``[0, 1]`` via :func:`clamp_unit`
      (Req 1.3, P3).
    """

    #: Unit-interval score; ``None`` means unavailable for this instance.
    value: Optional[float]
    #: ``True`` exactly when ``value is None`` — the explicit unavailable flag.
    unavailable: bool = False
    #: For retrieval metrics: the ``k`` used (Req 2.2). ``None`` for ragas metrics.
    k: Optional[int] = None
    #: ragas version for ragas metrics (Req 1.2); ``None`` for retrieval metrics.
    ragas_version: Optional[str] = None
    #: Bedrock model id for ragas metrics (Req 1.2); ``None`` for retrieval.
    bedrock_model_id: Optional[str] = None
    #: The id of the prompt configuration that produced this ragas value (Req
    #: 16.6), so every recorded value is traceable to the exact prompt used.
    #: ``None`` for retrieval metrics and for ragas values produced without a
    #: prompt store wired in.
    prompt_config_id: Optional[str] = None

    def __post_init__(self) -> None:
        # value/unavailable coupling (the P10 exclusive-or).
        if self.unavailable:
            if self.value is not None:
                raise ValueError(
                    "MetricValue marked unavailable must have value=None, got "
                    f"value={self.value!r}"
                )
        else:
            if self.value is None:
                raise ValueError(
                    "MetricValue with value=None must set unavailable=True"
                )
            # clamp the available score to the unit interval (P3).
            object.__setattr__(self, "value", clamp_unit(self.value))

    # --- ergonomic constructors -----------------------------------------
    @classmethod
    def available(
        cls,
        value: float,
        *,
        k: Optional[int] = None,
        ragas_version: Optional[str] = None,
        bedrock_model_id: Optional[str] = None,
        prompt_config_id: Optional[str] = None,
    ) -> "MetricValue":
        """Construct an available (clamped) metric value."""
        return cls(
            value=value,
            unavailable=False,
            k=k,
            ragas_version=ragas_version,
            bedrock_model_id=bedrock_model_id,
            prompt_config_id=prompt_config_id,
        )

    @classmethod
    def missing(
        cls,
        *,
        k: Optional[int] = None,
        ragas_version: Optional[str] = None,
        bedrock_model_id: Optional[str] = None,
        prompt_config_id: Optional[str] = None,
    ) -> "MetricValue":
        """Construct an unavailable metric value (``value=None``)."""
        return cls(
            value=None,
            unavailable=True,
            k=k,
            ragas_version=ragas_version,
            bedrock_model_id=bedrock_model_id,
            prompt_config_id=prompt_config_id,
        )

    # --- serialization ---------------------------------------------------
    def to_dict(self) -> dict:
        """Serialize to a JSON-ready dict; absent provenance is omitted."""
        d: dict = {"value": self.value, "unavailable": self.unavailable}
        if self.k is not None:
            d["k"] = self.k
        if self.ragas_version is not None:
            d["ragas_version"] = self.ragas_version
        if self.bedrock_model_id is not None:
            d["bedrock_model_id"] = self.bedrock_model_id
        if self.prompt_config_id is not None:
            d["prompt_config_id"] = self.prompt_config_id
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "MetricValue":
        """Rebuild a :class:`MetricValue` from a dict (round-trip exact)."""
        value = d.get("value")
        # default unavailable from the value when absent, so an externally
        # produced row that omits the flag still satisfies the coupling.
        unavailable = d.get("unavailable", value is None)
        return cls(
            value=value,
            unavailable=unavailable,
            k=d.get("k"),
            ragas_version=d.get("ragas_version"),
            bedrock_model_id=d.get("bedrock_model_id"),
            prompt_config_id=d.get("prompt_config_id"),
        )


# ---------------------------------------------------------------------------
# StageTimings — per-stage timings, separate from end-to-end latency (Req 7.3)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StageTimings:
    """Per-stage timings for one instance, kept separate from end-to-end latency.

    Retrieval and generation time are distinguishable (Req 7.3); any additional
    named stage the runner records (embed, rerank, …) lives in ``extra_ms``.
    These are stored *alongside* — never summed into or derived from —
    ``EvalInstance.latency_ms``.
    """

    retrieval_ms: Optional[float] = None
    generation_ms: Optional[float] = None
    extra_ms: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "retrieval_ms": self.retrieval_ms,
            "generation_ms": self.generation_ms,
            "extra_ms": dict(self.extra_ms),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StageTimings":
        return cls(
            retrieval_ms=d.get("retrieval_ms"),
            generation_ms=d.get("generation_ms"),
            extra_ms=dict(d.get("extra_ms") or {}),
        )


# ---------------------------------------------------------------------------
# EvalInstance — the atomic plotted unit (design C1)
# ---------------------------------------------------------------------------
_VALID_STATUS = ("ok", "failed")


@dataclass(frozen=True)
class EvalInstance:
    """The atomic plotted unit — one execution of one agent against one query.

    One ``(Agent_Under_Test, query, corpus_size)`` execution maps to exactly one
    of these (Req 5.2, 6.2, 7.1; the P4 bijection key is :attr:`instance_id`).
    Generation-quality (``ragas``) and retrieval-quality (``retrieval``) metrics
    are separate, disjoint maps so they are never conflated (Req 2.4 / P9).

    Construction-time validation:

    * ``ragas`` and ``retrieval`` key sets MUST be disjoint (Req 2.4 / P9).
    * ``status`` MUST be ``"ok"`` or ``"failed"`` (Req 5.5).
    * a non-positive / non-finite ``latency_ms`` is **flagged** via
      :attr:`latency_flagged` (recorded, not dropped; design Data Models / P7).

    Cross-instance invariants (e.g. ``instance_index`` strictly increasing within
    a ``session_id``, Req 7.4) are the runner's / store's responsibility and are
    not enforceable on a single record here.
    """

    # --- identity / placement ---
    instance_id: str                 # stable unique id (dedupe + bijection key)
    agent_id: str                    # Agent_Under_Test (N >= 3 supported)
    session_id: str                  # the progression group
    instance_index: int              # ordinal within a Session (Req 7.4)
    timestamp: str                   # ISO-8601 capture time
    # --- speed (X-axis signal) ---
    latency_ms: float                # end-to-end response time; flagged if <= 0 / non-finite
    stage_timings: StageTimings      # per-stage timings, separate from latency_ms (Req 7.3)
    # --- experiment axis / substrate ---
    corpus_size: int                 # the Corpus_Size_Sweep axis (Req 6)
    retrieval_cached: bool           # cold vs cached never conflated (Req 7.5)
    # --- quality (separate, disjoint maps) ---
    ragas: dict = field(default_factory=dict)        # generation quality (Req 1)
    retrieval: dict = field(default_factory=dict)    # gold-link retrieval quality (Req 2)
    # --- bubble-size source candidates (Req 10.5) ---
    confidence: Optional[float] = None
    volume: Optional[float] = None
    cost: Optional[float] = None
    # --- Control_Panel filters (Req 12.4) ---
    prompt_id: Optional[str] = None
    category: Optional[str] = None
    # --- failure status (a failed execution is still a recorded Instance) ---
    status: str = "ok"               # "ok" | "failed" (Req 5.5)
    error: Optional[str] = None
    # --- derived flag (not an init arg; recomputed from latency_ms) ---
    latency_flagged: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        # ragas / retrieval maps must be disjoint (P9).
        overlap = set(self.ragas) & set(self.retrieval)
        if overlap:
            raise ValueError(
                "ragas and retrieval metric maps must be disjoint; overlapping "
                f"keys: {sorted(overlap)}"
            )
        # status must be a known value (Req 5.5).
        if self.status not in _VALID_STATUS:
            raise ValueError(
                f"status must be one of {_VALID_STATUS}, got {self.status!r}"
            )
        # latency flag: non-positive or non-finite is flagged, not rejected (P7).
        flagged = not (math.isfinite(self.latency_ms) and self.latency_ms > 0)
        object.__setattr__(self, "latency_flagged", flagged)

    # --- serialization ---------------------------------------------------
    def to_dict(self) -> dict:
        """Serialize to a JSON-ready dict (the durable store + HTTP seam shape).

        The derived :attr:`latency_flagged` is intentionally NOT serialized; it
        is recomputed from ``latency_ms`` on :meth:`from_dict`, so the round-trip
        stays exact without storing redundant state.
        """
        return {
            "instance_id": self.instance_id,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "instance_index": self.instance_index,
            "timestamp": self.timestamp,
            "latency_ms": self.latency_ms,
            "stage_timings": self.stage_timings.to_dict(),
            "corpus_size": self.corpus_size,
            "retrieval_cached": self.retrieval_cached,
            "ragas": {name: mv.to_dict() for name, mv in self.ragas.items()},
            "retrieval": {name: mv.to_dict() for name, mv in self.retrieval.items()},
            "confidence": self.confidence,
            "volume": self.volume,
            "cost": self.cost,
            "prompt_id": self.prompt_id,
            "category": self.category,
            "status": self.status,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EvalInstance":
        """Rebuild a fully-typed :class:`EvalInstance` from a dict.

        Every nested value object (:class:`StageTimings`, each
        :class:`MetricValue`) is reconstructed, so ``from_dict(to_dict(x)) == x``
        holds for any instance.
        """
        return cls(
            instance_id=d["instance_id"],
            agent_id=d["agent_id"],
            session_id=d["session_id"],
            instance_index=d["instance_index"],
            timestamp=d["timestamp"],
            latency_ms=d["latency_ms"],
            stage_timings=StageTimings.from_dict(d.get("stage_timings") or {}),
            corpus_size=d["corpus_size"],
            retrieval_cached=d["retrieval_cached"],
            ragas={
                name: MetricValue.from_dict(v)
                for name, v in (d.get("ragas") or {}).items()
            },
            retrieval={
                name: MetricValue.from_dict(v)
                for name, v in (d.get("retrieval") or {}).items()
            },
            confidence=d.get("confidence"),
            volume=d.get("volume"),
            cost=d.get("cost"),
            prompt_id=d.get("prompt_id"),
            category=d.get("category"),
            status=d.get("status", "ok"),
            error=d.get("error"),
        )
