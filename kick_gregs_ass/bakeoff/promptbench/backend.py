"""
Build the Prompt Bench live backend — the optimizer's live stack, but with every Bedrock
client bound to the DEDICATED ``promptbench`` credential profile (account 299635194521).

What moves to the promptbench account: target generation (the inline agent runtime), the
Opus judge, and Embed v4 closeness — the Bedrock calls whose quota a live v3 run on alpha
would otherwise contend for. What STAYS on alpha: retrieval (AOSS) and Rerank v4, because
the ``skywalker-faq-alpha`` collection / rerank endpoint live in the alpha account and AOSS
is a separate service quota from Bedrock (so it does not contend with v3's Opus quota).

The bundle is assembled by calling the existing :func:`build_live_backend` (so the judge,
closeness, retrieval, and author wiring stays identical to the real path) and then swapping
in a promptbench-bound answer-adapter factory via :func:`dataclasses.replace` — the
optimizer code is reused, never modified.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Callable

from bakeoff import config
from bakeoff.quality.optimizer.backends import (
    OptimizerBackend,
    _bedrock_model_id_for,
    build_live_backend,
)
from bakeoff.quality.optimizer.inline_session_adapter import PersistentSessionInlineAdapter

__all__ = ["build_promptbench_backend"]


def _pb_client_factory(service: str, profile: str | None = None) -> Callable[[], Any]:
    """A zero-arg boto3 client factory bound to a Prompt Bench profile for ``service``.

    Resolves the session through the credential broker bound to the explicit ``profile``
    (defaults to ``PROMPT_BENCH_PROFILE`` — the target-generation/embed account
    299635194521; the Opus judge passes ``PROMPT_BENCH_JUDGE_PROFILE`` so it runs on its
    OWN account 582260130393). Never the ambient env / default profile, and the broker
    TTL-refreshes each profile independently. Lazy import keeps this module boto3-free at
    import time.
    """
    resolved_profile = profile or config.PROMPT_BENCH_PROFILE

    def factory() -> Any:
        from bakeoff.credentials import get_broker

        session = get_broker().get_session(resolved_profile, region=config.AWS_REGION)
        return session.client(service, region_name=config.AWS_REGION)

    return factory


def _pb_answer_adapter_factory(model_key: str, instruction: str, item_lookup: dict):
    """Live target adapter (inline agent) bound to the promptbench Bedrock account.

    Mirrors the optimizer's ``_live_answer_adapter_factory`` exactly, except the inline
    ``bedrock-agent-runtime`` client is built from the promptbench profile so target
    generation runs on the dedicated account.
    """
    return PersistentSessionInlineAdapter(
        model_key,
        _bedrock_model_id_for(model_key),
        instruction_override=instruction,
        client_factory=_pb_client_factory("bedrock-agent-runtime"),
    )


def build_promptbench_backend() -> OptimizerBackend:
    """Assemble the live backend with Prompt Bench's Bedrock clients on dedicated accounts.

    The Opus JUDGE binds to ``PROMPT_BENCH_JUDGE_PROFILE`` (account 582260130393) so it runs
    on its OWN account — the judge is the throughput bottleneck and must not contend with
    target generation. Target generation and Embed v4 closeness stay on ``PROMPT_BENCH_PROFILE``
    (account 299635194521). Retrieval (AOSS) + Rerank stay on alpha (where the collection /
    endpoint live). ``name`` is stamped ``"promptbench"`` so persisted records are attributable.
    """
    bundle = build_live_backend(
        retrieval_backend="opensearch",
        judge_client_factory=_pb_client_factory("bedrock-runtime", config.PROMPT_BENCH_JUDGE_PROFILE),
        embedding_client_factory=_pb_client_factory("bedrock-runtime"),
    )
    return dataclasses.replace(
        bundle,
        name="promptbench",
        answer_adapter_factory=_pb_answer_adapter_factory,
    )
