"""
Model adapters for the bakeoff harness (Task 5, Req 3).

One adapter per candidate model behind a single uniform :class:`ModelAdapter`
protocol. Adding a candidate is implementing (or, for Bedrock, configuring) one
adapter — nothing else in the system changes (Req 3, design Component 3).

* :class:`bakeoff.adapters.base.ModelAdapter` — the protocol + shared prompt
  assembly + the latency-capture helper every adapter reuses.
* :class:`bakeoff.adapters.mock.MockAdapter` — deterministic, no network; the
  adapter the runner/scorer tests run against.
* :class:`bakeoff.adapters.bedrock.BedrockModelAdapter` — the real adapter, a
  streaming Bedrock invocation wrapped in the shared credential-refresh + retry
  resilience helper, measuring a true time-to-first-token.
"""
from __future__ import annotations

from bakeoff.adapters.base import (
    ModelAdapter,
    Prompt,
    PromptMessage,
    assemble_context,
    build_prompt,
)

__all__ = [
    "ModelAdapter",
    "Prompt",
    "PromptMessage",
    "assemble_context",
    "build_prompt",
]
