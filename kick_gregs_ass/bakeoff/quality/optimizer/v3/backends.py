"""
V3 backend builder — LIVE ONLY (per the owner's direction: no offline functionality).

V3 reuses :func:`bakeoff.quality.optimizer.backends.build_live_backend` verbatim — the
live bundle already carries every seam V3's guards compose over (the internally-healing
judge/embed clients, the resilient AOSS retrieval with the Rerank-v4 second stage, the
resilient author). What this module adds is the *policy*: V3 never constructs an offline
bundle, so there is no fake path to silently fall back to in a live run.

Tests inject their fakes through the same ``**seams`` keyword arguments
``build_live_backend`` exposes (``judge_client_factory``, ``opensearch_client``,
``local_client``, ``rerank_client``, …) — fakes via seams, never an offline mode.
"""
from __future__ import annotations

from typing import Any

from bakeoff.quality.optimizer.backends import build_live_backend

__all__ = ["build_v3_backend"]


def build_v3_backend(**seams: Any):
    """Build the live optimizer backend for V3 (no offline variant exists for V3)."""
    return build_live_backend(**seams)
