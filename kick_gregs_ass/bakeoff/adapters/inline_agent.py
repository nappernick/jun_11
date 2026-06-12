"""
Inline-agent model adapter (the secondary invocation method, Req 3 + bake-off v2).

This is the second way the bake-off drives a candidate model: Bedrock **Agent
Runtime** ``InvokeInlineAgent`` instead of the Converse API. It implements the same
:class:`bakeoff.adapters.base.ModelAdapter` protocol as
:class:`bakeoff.adapters.bedrock.BedrockModelAdapter`, so the runner treats a
"converse" candidate and an "inline" candidate identically — they are just separate
rows with the same shape of :class:`~bakeoff.types.ModelResponse`.

WHY THIS EXISTS (and the hard part). Bedrock's inline agent, by default, wraps your
instruction in a large orchestration prompt that *teaches the model it is a
tool-calling agent*: function-call format, ``<tools>`` blocks, "you have access to
the following action groups", ReAct ``Thought/Action/Observation`` scaffolding, etc.
That default is the "extra stuff" we must NOT have — even with zero tools defined it
tells the model it *can* call tools. We strip ALL of it the way the internal
``AtoZAgoraAppChatIntakeService`` does, verified live against Bedrock:

* **Override the orchestration prompt.** A ``promptOverrideConfiguration`` with
  ``promptType=ORCHESTRATION`` + ``promptCreationMode=OVERRIDDEN`` and a MINIMAL
  ``basePromptTemplate`` (:data:`bakeoff.config.INLINE_AGENT_PROMPT_TEMPLATE`) that
  contains only ``$instruction$`` (system), ``$question$`` (user), and the required
  empty ``$agent_scratchpad$`` assistant turn. No tool text, no ReAct.
* **Attach no actionGroups and no knowledgeBases.** There is literally nothing for
  the model to call.
* The orchestration trace confirms the model receives ONLY our system + question
  (a live probe showed every tool/function/action-group/ReAct marker absent).

So the model sees only the reality we provide via ``instruction`` + ``inputText``.

OTHER VERIFIED BEHAVIORS (live probes, recorded so the adapter is honest):
* The inline path **streams** ``chunk`` events, so TTFT is the wall-clock to the
  first chunk — measured exactly like the Converse adapter (first packet we could
  render, Req 3.2).
* The inline path **accepts ``temperature``** in the override ``inferenceConfiguration``
  even for the 4.x models (unlike Converse, which 400s "temperature is deprecated").
* ``instruction`` has a **minimum length** (~40 chars); the adapter pads defensively.
* Extended **thinking is NOT honored** through this path (a ``thinking`` block in the
  override template is silently ignored). A "thinking-on" inline candidate therefore
  runs as non-thinking in practice — that asymmetry is left visible in the data, not
  papered over, and ``ModelResponse.raw`` records ``thinking_honored=False``.

Credential-expiry resilience mirrors the Converse adapter: each invoke is wrapped in
:func:`bakeoff.resilience.call_with_resilience`, and an auth-expired failure rebuilds
the ``bedrock-agent-runtime`` client from a fresh session. boto3 is blocking, so the
stream is consumed in a worker thread with TTFT stamped inside the thread.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Awaitable, Callable, Optional, Sequence

import bakeoff.config as config
from bakeoff.adapters.base import assemble_context
from bakeoff.prompts import system_instruction_for
from bakeoff.resilience import call_with_resilience
from bakeoff.types import Item, ModelResponse

__all__ = ["InlineAgentAdapter"]


class _StreamResult:
    __slots__ = ("text", "ttft_ms", "generation_total_ms", "token_usage", "finish_reason")

    def __init__(self, text, ttft_ms, generation_total_ms, token_usage, finish_reason):
        self.text = text
        self.ttft_ms = ttft_ms
        self.generation_total_ms = generation_total_ms
        self.token_usage = token_usage
        self.finish_reason = finish_reason


class InlineAgentAdapter:
    """A streaming Bedrock **inline-agent** :class:`ModelAdapter` (scaffolding stripped)."""

    def __init__(
        self,
        name: str,
        bedrock_model_id: str,
        *,
        region: Optional[str] = None,
        client: Optional[Any] = None,
        client_factory: "Callable[[], Any] | None" = None,
        family: Optional[str] = None,
        thinking: bool = False,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        accepts_temperature: bool = True,
        clock: Callable[[], float] = time.perf_counter,
        sleep: "Callable[[float], Awaitable[None]] | None" = None,
        instruction_override: Optional[str] = None,
    ) -> None:
        self.name = name
        self.bedrock_model_id = bedrock_model_id
        self.region = region or config.AWS_REGION
        self.family = family or name
        self.thinking = thinking
        # The inline path accepts temperature even on 4.x; default True. A None
        # effective temperature omits the field.
        self.accepts_temperature = accepts_temperature
        self.temperature_override = temperature
        # max_tokens: reuse the shared resolver so inline + converse size answers
        # the same way (thinking-on gets answer headroom even though inline won't
        # actually run the reasoning — keeps the answer cap generous either way).
        self.max_tokens = config.resolve_max_tokens(thinking, None, max_tokens)
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._client_factory = client_factory or self._default_client_factory
        self._client = client
        self.instruction_override = instruction_override

    # -- client lifecycle / credential chain ------------------------------
    def _default_client_factory(self) -> Any:
        """Build a ``bedrock-agent-runtime`` client via the credential broker.

        Binds to the broker's explicit named profile (never ambient env) with
        proactive TTL refresh, so a sibling agent cannot redirect the client and the
        token is kept fresh.
        """
        from bakeoff.credentials import get_broker

        session = get_broker().get_session(region=self.region)
        return session.client("bedrock-agent-runtime", region_name=self.region)

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def _refresh_credentials(self) -> None:
        """Mint fresh credentials via the broker, then rebuild (auth-expiry refresh hook)."""
        from bakeoff.credentials import get_broker

        try:
            get_broker().refresh()
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger("bakeoff.credentials").warning(
                "inline_agent credential refresh via broker failed; rebuilding from disk",
                exc_info=True,
            )
        self._client = self._client_factory()

    # -- request shaping ---------------------------------------------------
    def _instruction_question_context(
        self, item: Item, fragments: Sequence[dict]
    ) -> tuple[str, str, str]:
        """Build (instruction, question, context) for the inline request.

        CRITICAL SIZE SPLIT. The inline-agent ``instruction`` field is quota-limited
        (``max-instruction-size`` = 20000 chars; exceeding it is a hard
        ``ServiceQuotaExceededException``, not retryable). The retrieved-fragment
        context is large (tens of KB), so it MUST NOT go in ``instruction``. We
        therefore split the same content the Converse adapter sends, three ways:

        * ``instruction`` — ONLY the family-tuned system prompt (~1 KB), well under
          the quota. (Same instruction text the Converse system block leads with.)
        * ``context`` — the constant retrieved-fragment block, passed as a
          ``promptSessionAttributes`` value the override template injects at
          ``$prompt_session_attributes$`` (verified to carry ~100 KB to the model).
        * ``question`` — the focal query (single-turn) or a flattened transcript
          (multi-turn), injected at ``$question$``.

        Net: the model sees the identical system + fragments + question content as
        the Converse path, retrieval stays the held constant, and ``instruction``
        stays tiny so the quota is never hit.
        """
        instruction = (
            self.instruction_override
            if self.instruction_override is not None
            else system_instruction_for(self.family, self.thinking)
        )
        if len(instruction) < config.INLINE_AGENT_MIN_INSTRUCTION_CHARS:
            instruction = instruction + (
                " " * (config.INLINE_AGENT_MIN_INSTRUCTION_CHARS - len(instruction))
            )

        context = assemble_context(fragments)

        if item.is_multi_turn and item.turns:
            lines = []
            for t in item.turns:
                lines.append(f"User: {t.user_utterance}")
            question = "\n".join(lines)
        else:
            question = item.query or ""

        return instruction, question, context

    def _inference_configuration(self, temperature: float) -> dict:
        cfg: dict[str, Any] = {"maximumLength": int(self.max_tokens)}
        eff_temp = config.resolve_temperature(
            # Inline ignores thinking, so never force the thinking temperature here;
            # treat as non-thinking for temperature resolution.
            False, self.temperature_override, temperature,
            accepts_temperature=self.accepts_temperature,
        )
        if eff_temp is not None:
            cfg["temperature"] = float(eff_temp)
        return cfg

    def _build_request(self, item: Item, fragments: Sequence[dict], temperature: float) -> dict:
        instruction, question, context = self._instruction_question_context(item, fragments)
        return dict(
            sessionId=f"bakeoff-{uuid.uuid4()}",
            foundationModel=self.bedrock_model_id,
            instruction=instruction,
            inputText=question,
            enableTrace=False,  # we don't need the trace in production; faster
            # The retrieved-fragment context rides in promptSessionAttributes (NOT
            # instruction) to stay under the 20K instruction quota; the override
            # template injects it at $prompt_session_attributes$.
            inlineSessionState={"promptSessionAttributes": {"context": context}},
            promptOverrideConfiguration={
                "promptConfigurations": [
                    {
                        "promptType": "ORCHESTRATION",
                        "promptCreationMode": "OVERRIDDEN",
                        "parserMode": "DEFAULT",
                        "promptState": "ENABLED",
                        "basePromptTemplate": config.INLINE_AGENT_PROMPT_TEMPLATE,
                        "inferenceConfiguration": self._inference_configuration(temperature),
                    }
                ]
            },
        )

    # -- the blocking stream consumer (runs in a worker thread) -----------
    def _invoke_stream_sync(self, request: dict) -> _StreamResult:
        """Open the inline-agent stream and consume it, capturing a true TTFT.

        ``start`` is taken immediately before the call. TTFT is stamped on the first
        ``chunk`` event carrying bytes (the first packet we could render). The agent
        runtime returns an EventStream under ``response["completion"]`` yielding
        dicts; ``chunk`` carries answer bytes, ``trace`` is ignored (disabled),
        other event types are tolerated.
        """
        client = self._get_client()
        start = self._clock()
        response = client.invoke_inline_agent(**request)

        ttft_ms: Optional[float] = None
        parts: list[str] = []
        finish_reason: Optional[str] = None

        for event in response["completion"]:
            if not isinstance(event, dict):
                continue
            if "chunk" in event:
                raw = event["chunk"].get("bytes")
                if raw:
                    if ttft_ms is None:
                        ttft_ms = (self._clock() - start) * 1000.0
                    parts.append(raw.decode("utf-8", "replace"))
            elif "returnControl" in event:
                # Defensive: returnControl means the model tried a tool call. With
                # the scaffolding stripped this should never happen; record it as
                # the finish reason so it surfaces in data rather than silently.
                finish_reason = "return_control"

        total_ms = (self._clock() - start) * 1000.0
        if ttft_ms is None:
            ttft_ms = total_ms  # empty completion: TTFT degenerates to total
        return _StreamResult(
            text="".join(parts),
            ttft_ms=ttft_ms,
            generation_total_ms=total_ms,
            token_usage={},  # the inline-agent stream does not surface token usage
            finish_reason=finish_reason,
        )

    async def _generate_once(self, request: dict) -> _StreamResult:
        async def attempt() -> _StreamResult:
            return await asyncio.to_thread(self._invoke_stream_sync, request)

        return await call_with_resilience(
            attempt, refresh_credentials=self._refresh_credentials, sleep=self._sleep
        )

    # -- the protocol method ----------------------------------------------
    async def generate(
        self, item: Item, fragments: Sequence[dict], temperature: float
    ) -> ModelResponse:
        """Generate an answer for ``item`` via the inline-agent path.

        Unlike the Converse multi-turn flow (which issues one call per turn), the
        inline override template is a single user message, so a multi-turn item is
        sent as one flattened-transcript request. TTFT is the first rendered chunk;
        total is the full stream wall-clock.
        """
        request = self._build_request(item, fragments, temperature)
        result = await self._generate_once(request)

        raw: dict[str, object] = {
            "bedrock_model_id": self.bedrock_model_id,
            "method": "inline_agent",
            "max_tokens": self.max_tokens,
            "accepts_temperature": self.accepts_temperature,
            "requested_temperature": temperature,
            "effective_temperature": self._inference_configuration(temperature).get("temperature"),
            "thinking_requested": self.thinking,
            "prompt_override": self.instruction_override is not None,
            # The inline path does not honor extended thinking (verified live); a
            # thinking-on inline candidate runs as non-thinking. Recorded honestly.
            "thinking_honored": False,
            "finish_reason": result.finish_reason,
        }

        return ModelResponse(
            text=result.text,
            ttft_ms=result.ttft_ms,
            generation_total_ms=result.generation_total_ms,
            token_usage=result.token_usage,
            per_turn_answers=[result.text],
            finish_reason=result.finish_reason,
            model=self.name,
            raw=raw,
        )
