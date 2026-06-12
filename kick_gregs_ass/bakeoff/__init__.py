"""
model-bakeoff-harness — a throwaway local harness for choosing which candidate
model should be the brain of a Slack FAQ bot, on the balance of speed and
quality.

This package is intentionally light at import time: importing ``bakeoff`` pulls
in no heavy dependencies (no httpx, no numpy, no FastAPI). Submodules
(``bakeoff.config``, ``bakeoff.types``, ``bakeoff.ids``) are imported explicitly
by callers.

See ``.kiro/specs/model-bakeoff-harness/`` for the requirements and design.
"""
from __future__ import annotations

from bakeoff.ids import SCHEMA_VERSION, trial_id

__all__ = ["SCHEMA_VERSION", "trial_id"]
