"""
Real Bedrock model adapter (Task 5, Req 3.1/3.2/3.3, Req 15.3).

:class:`BedrockModelAdapter` drives a real candidate model on Amazon Bedrock
through the same uniform :class:`bakeoff.adapters.base.ModelAdapter` protocol the
mock implements. Three design-critical things it does:

* **Streams so TTFT is a TRUE time-to-first-token (Req 3.2).** It uses the Bedrock
  **Converse streaming** API (``converse_stream``), which is model-agnostic across
  the candidate families — so adding a candidate is appending one entry to
  ``config.CANDIDATE_MODELS`` and nothing else (Req 3). The wall-clock is started
  immediately before the stream is opened.

* **Is extended-thinking aware (per-candidate).** A candidate registered with
  ``thinking=True`` sends ``additionalModelRequestFields={"thinking": {"type":
  "enabled", "budget_tokens": <N>}}`` and is FORCED to ``temperature == 1.0``
  (Anthropic rejects a custom temperature whenever extended thinking is enabled)
  with ``max_tokens`` raised above the thinking budget; a ``thinking=False``
  candidate sends no thinking field and honors the runner's temperature. "Thinking
  on" and "thinking off" of the same base model are SEPARATE candidates (separate
  names, separate invocations, separate result rows). The reasoning-vs-answer TTFT
  split and the forced temperature are surfaced in ``ModelResponse.raw`` so the
  invocation shape is auditable.

* **Survives credential expiry (the cross-cutting concern).** Every streaming
  invoke is wrapped in :func:`bakeoff.resilience.call_with_resilience`. When a call
  fails with an expired/invalid-credentials signature, the helper invokes this
  adapter's refresh callback — which rebuilds the boto3 ``bedrock-runtime`` client
  from a **fresh** :class:`boto3.Session` (re-resolving the standard credential
  chain the existing backend uses, ``src/bedrock_client.py``) — and retries, up to
  ``config.AUTH_MAX_REFRESH_CYCLES``. Throttling/transient errors back off and
  retry without a refresh; permanent errors propagate so the runner records the
  trial as errored and can resume it later.

boto3 is **blocking**, and the harness runs on asyncio (design AD-3). Each
streaming invoke therefore runs in a worker thread via :func:`asyncio.to_thread`,
with TTFT captured *inside* the thread the instant the first token arrives, so the
measurement is not distorted by event-loop scheduling. The adapter introduces no
new secrets and writes none to the log (Req 15.3): it only reuses the ambient
credential chain and the region from :mod:`bakeoff.config`.

Like every adapter it owns only prompt assembly + the endpoint call + temperature
handling + latency capture, and **never scores** (Req 3.4).

Testability: both the boto3 client and the credential-refresh callback are
dependency-injectable, so the adapter's streaming/TTFT/resilience/thinking logic
is fully exercised by unit tests with a fake client and no network — no real
Bedrock call is ever made in tests.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Iterable, Optional, Sequence

import bakeoff.config as config
from bakeoff.adapters.base import Prompt, build_prompt
from bakeoff.resilience import call_with_resilience
from bakeoff.types import Item, ModelResponse

__all__ = [
    "BedrockModelAdapter",
    "build_candidate_adapters",
    "load_bakeoff_instruction_override",
]


# A single streamed-turn measurement returned by the (blocking) worker.
class _StreamResult:
    __slots__ = (
        "text",
        "ttft_ms",
        "generation_total_ms",
        "token_usage",
        "finish_reason",
        "reasoning_ttft_ms",
        "reasoning_chars",
    )

    def __init__(
        self,
        text: str,
        ttft_ms: float,
        generation_total_ms: float,
        token_usage: dict[str, int],
        finish_reason: Optional[str],
        reasoning_ttft_ms: Optional[float] = None,
        reasoning_chars: int = 0,
    ) -> None:
        self.text = text
        self.ttft_ms = ttft_ms
        self.generation_total_ms = generation_total_ms
        self.token_usage = token_usage
        self.finish_reason = finish_reason
        # Time-to-first-REASONING-token (visible-answer TTFT is ``ttft_ms``); only
        # set for a thinking-on turn that actually streamed a reasoning delta.
        self.reasoning_ttft_ms = reasoning_ttft_ms
        # Total reasoning characters streamed (thinking-overhead signal for raw).
        self.reasoning_chars = reasoning_chars


class BedrockModelAdapter:
    """A streaming Bedrock-backed :class:`ModelAdapter` with thinking + resilience."""

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
        budget_tokens: Optional[int] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        accepts_temperature: bool = False,
        clock: Callable[[], float] = time.perf_counter,
        sleep: "Callable[[float], Awaitable[None]] | None" = None,
        instruction_override: Optional[str] = None,
    ) -> None:
        """
        Args:
            name: the candidate name (stamped onto ``TrialEvent.model``). "Thinking
                on" and "thinking off" of the same base model are SEPARATE
                candidates with distinct names (the name, not the bedrock id, is
                what distinguishes them on ``TrialEvent.model``).
            bedrock_model_id: the Bedrock model/inference-profile id to invoke.
            region: AWS region; defaults to ``config.AWS_REGION`` (reuses the
                backend's region posture — us-west-2 cross-region profiles).
            client: an already-built ``bedrock-runtime`` client to use as-is
                (mainly for tests); when ``None`` one is built lazily via
                ``client_factory``.
            client_factory: a zero-arg callable returning a ``bedrock-runtime``
                client. Used both to build the initial client and to **rebuild** it
                on a credential refresh. Defaults to a real boto3 builder that
                re-resolves the standard credential chain from a fresh session.
            thinking: whether to enable Bedrock Claude **extended thinking** for
                this candidate. When ``True`` the adapter sends
                ``additionalModelRequestFields={"thinking": {"type": "enabled",
                "budget_tokens": <N>}}`` and forces ``temperature == 1.0`` (an
                Anthropic constraint); when ``False`` it sends no thinking field
                and honors the requested temperature (subject to a fixed override).
            budget_tokens: reasoning budget when ``thinking`` is on; falls back to
                ``config.THINKING_DEFAULT_BUDGET_TOKENS``. Ignored when off.
            max_tokens: generation cap. When ``None`` it is derived from
                ``config`` (thinking-on: budget + answer headroom; thinking-off:
                the default cap) so max_tokens always exceeds the thinking budget.
            temperature: a fixed per-candidate temperature override used only when
                thinking is off; ``None`` means honor the runner's per-trial value.
            accepts_temperature: whether this model accepts the ``temperature``
                Converse parameter at all. The newest Claude models (Sonnet
                4.6/4.5, Haiku 4.5) DEPRECATED it and reject any value, so when
                this is ``False`` the adapter OMITS ``temperature`` from
                ``inferenceConfig`` entirely (no replacement knob exists for them).
                Older models that still accept it pass ``True``. Defaults to
                ``False`` — the safe default for the modern roster.
            clock: monotonic clock (injectable for deterministic tests).
            sleep: async sleep used by the resilience backoff (injectable so tests
                run instantly); defaults to :func:`asyncio.sleep`.
        """
        self.name = name
        self.bedrock_model_id = bedrock_model_id
        self.region = region or config.AWS_REGION
        self.family = family or name
        self.thinking = thinking
        self.budget_tokens = budget_tokens
        self.accepts_temperature = accepts_temperature
        # Effective per-call knobs are resolved ONCE here from the shared config
        # helpers, so the adapter and the registry descriptor can never disagree
        # about what a candidate sends.
        self.effective_budget_tokens = config.resolve_budget_tokens(thinking, budget_tokens)
        self.max_tokens = config.resolve_max_tokens(thinking, budget_tokens, max_tokens)
        self.temperature_override = temperature
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        # Optional exact system-instruction override (the multi-turn quality
        # study injects an optimized prompt variant here). None => use the
        # family/thinking-selected instruction (normal bake-off behavior).
        self.instruction_override = instruction_override

        self._client_factory = client_factory or self._default_client_factory
        self._client = client  # built lazily if None

    # -- client lifecycle / credential chain ------------------------------
    def _default_client_factory(self) -> Any:
        """Build a ``bedrock-runtime`` client via the credential broker.

        Binds to the broker's explicit named profile (never the ambient
        ``AWS_PROFILE``/``default``, which a sibling agent could clobber) and lets the
        broker proactively TTL-refresh the underlying token. Imports are lazy so the
        module stays import-light. Rebuilding via the broker session is what makes the
        refresh hook pick up genuinely re-minted credentials.
        """
        from bakeoff.credentials import get_broker

        session = get_broker().get_session(region=self.region)
        return session.client("bedrock-runtime", region_name=self.region)

    def _get_client(self) -> Any:
        """Return the current client, building one lazily on first use."""
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def _refresh_credentials(self) -> None:
        """Mint fresh credentials via the broker, then rebuild the client (refresh hook).

        Passed to :func:`call_with_resilience` as ``refresh_credentials``; invoked
        only when a call failed with an auth-expired signature. The broker actually
        re-runs ``ada`` (the previous hook only re-read the same expired file), then
        the client is rebuilt from the now-fresh session. A broker failure falls back
        to a plain rebuild so behavior is never worse than before.
        """
        from bakeoff.credentials import get_broker

        try:
            get_broker().refresh()
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger("bakeoff.credentials").warning(
                "bedrock adapter credential refresh via broker failed; rebuilding from disk",
                exc_info=True,
            )
        self._client = self._client_factory()

    # -- prompt serialization (Converse shape) ----------------------------
    @staticmethod
    def _to_converse(prompt: Prompt) -> tuple[list[dict], list[dict]]:
        """Render a provider-agnostic :class:`Prompt` into Converse system+messages.

        Converse is uniform across the candidate families on Bedrock: ``system`` is
        a list of ``{"text": ...}`` blocks and ``messages`` is a list of
        ``{"role", "content":[{"text": ...}]}``. This uniformity is what lets one
        adapter class serve every candidate.
        """
        system = [{"text": prompt.system}] if prompt.system else []
        messages = [
            {"role": m.role, "content": [{"text": m.content}]} for m in prompt.messages
        ]
        return system, messages

    # -- request shaping (thinking config) --------------------------------
    def _additional_model_request_fields(self) -> Optional[dict]:
        """Build ``additionalModelRequestFields`` for this candidate, or ``None``.

        Thinking-on candidates send ``{"thinking": {"type": "enabled",
        "budget_tokens": <N>}}`` (the Bedrock Claude extended-thinking shape);
        thinking-off candidates send nothing (the field is omitted entirely).
        """
        if not self.thinking:
            return None
        return {
            "thinking": {
                "type": "enabled",
                "budget_tokens": int(self.effective_budget_tokens or 0),
            }
        }

    # -- the blocking stream consumer (runs in a worker thread) -----------
    def _invoke_stream_sync(
        self, system: list[dict], messages: list[dict], temperature: float
    ) -> _StreamResult:
        """Open a Converse stream and consume it, capturing a true TTFT.

        Runs in a worker thread (boto3 is blocking). Any client error
        (``ClientError`` etc.) raised here propagates out of ``asyncio.to_thread``
        and is classified by the resilience helper. ``start`` is taken immediately
        before opening the stream.

        **TTFT policy (thinking-aware, Req 3.2).** A thinking-on model streams
        ``reasoningContent`` deltas BEFORE the visible answer. To keep TTFT
        comparable across thinking/non-thinking candidates, ``ttft_ms`` is the
        time-to-first-token of the **visible answer** (the first ``text`` delta) in
        every case. The time-to-first-**reasoning**-token is captured separately
        (``reasoning_ttft_ms``) so latency analysis can see the thinking overhead.
        Reasoning text is NEVER included in ``.text`` — only the final answer is.
        ``generation_total_ms`` is the full wall-clock including thinking.
        """
        client = self._get_client()
        # Resolve the temperature actually sent. For models that DEPRECATED
        # temperature (the 4.x roster) this is None and the field is OMITTED from
        # inferenceConfig — sending any value 400s ("temperature is deprecated for
        # this model"). Thinking-on (on a model that accepts temperature) forces
        # 1.0. Resolved here so the wire value is auditable.
        effective_temperature = config.resolve_temperature(
            self.thinking, self.temperature_override, temperature,
            accepts_temperature=self.accepts_temperature,
        )
        inference_config: dict[str, Any] = {"maxTokens": self.max_tokens}
        if effective_temperature is not None:
            inference_config["temperature"] = effective_temperature
        kwargs: dict[str, Any] = dict(
            modelId=self.bedrock_model_id,
            system=system,
            messages=messages,
            inferenceConfig=inference_config,
        )
        amrf = self._additional_model_request_fields()
        if amrf is not None:
            kwargs["additionalModelRequestFields"] = amrf

        start = self._clock()
        response = client.converse_stream(**kwargs)

        ttft_ms: Optional[float] = None
        reasoning_ttft_ms: Optional[float] = None
        reasoning_chars = 0
        parts: list[str] = []
        finish_reason: Optional[str] = None
        usage: dict[str, int] = {}

        for event in _iter_stream(response):
            reasoning_delta = _reasoning_text(event)
            if reasoning_delta:
                if reasoning_ttft_ms is None:
                    reasoning_ttft_ms = (self._clock() - start) * 1000.0
                reasoning_chars += len(reasoning_delta)
                # reasoning is timed/measured but NEVER concatenated into the
                # visible answer text.
            delta = _delta_text(event)
            if delta:
                if ttft_ms is None:
                    ttft_ms = (self._clock() - start) * 1000.0
                parts.append(delta)
            stop = _stop_reason(event)
            if stop is not None:
                finish_reason = stop
            ev_usage = _usage(event)
            if ev_usage:
                usage = ev_usage

        total_ms = (self._clock() - start) * 1000.0
        if ttft_ms is None:
            ttft_ms = total_ms  # empty (answer) completion: TTFT degenerates to total

        return _StreamResult(
            text="".join(parts),
            ttft_ms=ttft_ms,
            generation_total_ms=total_ms,
            token_usage=usage,
            finish_reason=finish_reason,
            reasoning_ttft_ms=reasoning_ttft_ms,
            reasoning_chars=reasoning_chars,
        )

    async def _generate_turn(
        self, system: list[dict], messages: list[dict], temperature: float
    ) -> _StreamResult:
        """Run one streaming invoke off the event loop, with resilience wrapping.

        The blocking stream consume is dispatched to a worker thread; the whole
        attempt is wrapped in :func:`call_with_resilience` so a credential rollover
        triggers a client rebuild + retry, and throttling/transient errors back off
        + retry, all transparently to the caller.
        """

        async def attempt() -> _StreamResult:
            return await asyncio.to_thread(
                self._invoke_stream_sync, system, messages, temperature
            )

        return await call_with_resilience(
            attempt, refresh_credentials=self._refresh_credentials, sleep=self._sleep
        )

    # -- the protocol method ----------------------------------------------
    async def generate(
        self,
        item: Item,
        fragments: Sequence[dict],
        temperature: float,
    ) -> ModelResponse:
        """Generate an answer for ``item`` given the constant ``fragments``.

        For a multi-turn item each turn is generated in order; the prompt for turn
        *t* includes the prior turns and the model's own prior answers (Req 3.3).
        The trial's TTFT is the first turn's first (visible-answer) token; total
        generation latency is the sum across turns; token usage is summed. ``text``
        is the final turn's answer and ``per_turn_answers`` holds each turn in order.
        """
        n_turns = len(item.turns) if (item.is_multi_turn and item.turns) else 1

        per_turn_answers: list[str] = []
        prompts: list[Prompt] = []
        trial_ttft_ms: Optional[float] = None
        trial_reasoning_ttft_ms: Optional[float] = None
        reasoning_chars_total = 0
        generation_total_ms = 0.0
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        finish_reason: Optional[str] = None

        for turn_index in range(n_turns):
            prompt = build_prompt(
                item,
                fragments,
                family=self.family,
                thinking_enabled=self.thinking,
                prior_answers=per_turn_answers,
                upto_turn_index=turn_index if n_turns > 1 else None,
                instruction_override=self.instruction_override,
            )
            prompts.append(prompt)
            system, messages = self._to_converse(prompt)

            result = await self._generate_turn(system, messages, temperature)

            per_turn_answers.append(result.text)
            if trial_ttft_ms is None:
                trial_ttft_ms = result.ttft_ms
            if trial_reasoning_ttft_ms is None and result.reasoning_ttft_ms is not None:
                trial_reasoning_ttft_ms = result.reasoning_ttft_ms
            reasoning_chars_total += result.reasoning_chars
            generation_total_ms += result.generation_total_ms
            finish_reason = result.finish_reason
            prompt_tokens += int(result.token_usage.get("prompt", 0))
            completion_tokens += int(result.token_usage.get("completion", 0))
            total_tokens += int(result.token_usage.get("total", 0))

        token_usage = {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": total_tokens or (prompt_tokens + completion_tokens),
        }
        ttft_ms = trial_ttft_ms if trial_ttft_ms is not None else generation_total_ms

        # Auditable record of the invocation shape (Req 15.3): the resolved
        # thinking config and the temperature actually sent. ``effective_temperature``
        # is ``None`` when the model deprecated temperature (the field was omitted
        # from the request) — recorded honestly so the audit shows what hit the wire.
        raw: dict[str, object] = {
            "bedrock_model_id": self.bedrock_model_id,
            "n_turns": n_turns,
            "thinking": self.thinking,
            "max_tokens": self.max_tokens,
            "accepts_temperature": self.accepts_temperature,
            "requested_temperature": temperature,
            "effective_temperature": config.resolve_temperature(
                self.thinking, self.temperature_override, temperature,
                accepts_temperature=self.accepts_temperature,
            ),
        }
        if self.thinking:
            raw["budget_tokens"] = self.effective_budget_tokens
            # time-to-first-reasoning-token (visible-answer TTFT is ``ttft_ms``);
            # None if the model emitted no reasoning delta this trial.
            raw["reasoning_ttft_ms"] = trial_reasoning_ttft_ms
            raw["reasoning_chars"] = reasoning_chars_total

        return ModelResponse(
            text=per_turn_answers[-1] if per_turn_answers else "",
            ttft_ms=ttft_ms,
            generation_total_ms=generation_total_ms,
            token_usage=token_usage,
            per_turn_answers=per_turn_answers,
            finish_reason=finish_reason,
            model=self.name,
            raw=raw,
        )


# ---------------------------------------------------------------------------
# Converse-stream event helpers (tolerant of the event shapes Bedrock emits)
# ---------------------------------------------------------------------------
def _iter_stream(response: Any) -> Iterable[dict]:
    """Yield events from a Converse-stream response.

    boto3 returns ``{"stream": <EventStream>}`` where the EventStream iterates
    event dicts. Tolerant of a response that is already directly iterable (the
    shape test fakes use).
    """
    if isinstance(response, dict) and "stream" in response:
        return response["stream"]
    return response


def _delta_text(event: dict) -> str:
    """Extract the text delta from a ``contentBlockDelta`` event, else ``""``.

    This is the **visible answer** delta. A thinking-on model also streams
    ``reasoningContent`` deltas inside ``contentBlockDelta``; those are handled
    separately by :func:`_reasoning_text` and deliberately excluded here so the
    final answer text never contains reasoning.
    """
    block = event.get("contentBlockDelta")
    if not block:
        return ""
    delta = block.get("delta") or {}
    return delta.get("text", "") or ""


def _reasoning_text(event: dict) -> str:
    """Extract the reasoning (chain-of-thought) delta, else ``""``.

    Bedrock Converse streams extended-thinking reasoning as a union member of the
    ``contentBlockDelta`` delta: ``delta.reasoningContent.text`` (a separate
    ``signature``/``redactedContent`` member carries the integrity signature and
    is not answer text). Returned separately from :func:`_delta_text` so reasoning
    can be timed/measured but kept out of the visible answer.
    """
    block = event.get("contentBlockDelta")
    if not block:
        return ""
    delta = block.get("delta") or {}
    reasoning = delta.get("reasoningContent") or {}
    return reasoning.get("text", "") or ""


def _stop_reason(event: dict) -> Optional[str]:
    """Extract the stop reason from a ``messageStop`` event, else ``None``."""
    block = event.get("messageStop")
    if not block:
        return None
    return block.get("stopReason")


def _usage(event: dict) -> dict[str, int]:
    """Extract token usage from a ``metadata`` event, normalized to our keys."""
    meta = event.get("metadata")
    if not meta:
        return {}
    usage = meta.get("usage") or {}
    out: dict[str, int] = {}
    if "inputTokens" in usage:
        out["prompt"] = int(usage["inputTokens"])
    if "outputTokens" in usage:
        out["completion"] = int(usage["outputTokens"])
    if "totalTokens" in usage:
        out["total"] = int(usage["totalTokens"])
    return out


# ---------------------------------------------------------------------------
# Registry helper — turn config.CANDIDATE_MODELS into adapters
# ---------------------------------------------------------------------------
def load_bakeoff_instruction_override(
    path: Path = config.BAKEOFF_UNIVERSAL_PROMPT_PATH,
) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"universal Bake-Off prompt is empty: {path}")
    return text


def build_candidate_adapters(
    *,
    include_disabled: bool = False,
    client_factory: "Callable[[], Any] | None" = None,
    methods: "Sequence[str] | None" = None,
) -> list:
    """Build one adapter per registered candidate, dispatching by invocation method.

    Reads ``config.CANDIDATE_MODELS`` (the single place a candidate is registered,
    Req 3). Each candidate's ``method`` selects the adapter:

    * ``"converse"`` → :class:`BedrockModelAdapter` (Bedrock Converse streaming).
    * ``"inline_agent"`` → :class:`bakeoff.adapters.inline_agent.InlineAgentAdapter`
      (Bedrock Agent Runtime InvokeInlineAgent with the orchestration scaffolding
      stripped via an overridden prompt template).

    Both implement the same :class:`~bakeoff.adapters.base.ModelAdapter` protocol,
    so the runner is method-agnostic. ``client_factory`` is shared across the built
    adapters when provided (e.g. to inject a fake client in an integration test);
    otherwise each adapter builds its own real client lazily.

    ``methods`` optionally restricts the build to candidates whose ``method`` is in
    the given set — this is how the operator runs the two invocation methods in
    SEPARATE PHASES (e.g. ``methods=["inline_agent"]`` first, then
    ``methods=["converse"]``) so each method's latency is measured without the
    other's worker-pool contention. ``None`` builds every enabled candidate.

    The number of adapters and their names are driven entirely by the registry —
    nothing is hard-coded here.
    """
    from bakeoff.adapters.inline_agent import InlineAgentAdapter

    allowed = set(methods) if methods is not None else None
    instruction_override = load_bakeoff_instruction_override()
    adapters: list = []
    for c in config.CANDIDATE_MODELS:
        if not c.enabled and not include_disabled:
            continue
        method = getattr(c, "method", "converse")
        if allowed is not None and method not in allowed:
            continue
        if method == "inline_agent":
            adapters.append(
                InlineAgentAdapter(
                    c.name,
                    c.bedrock_model_id,
                    client_factory=client_factory,
                    family=c.family,
                    thinking=c.thinking,
                    max_tokens=c.max_tokens,
                    temperature=c.temperature,
                    accepts_temperature=c.accepts_temperature,
                    instruction_override=instruction_override,
                )
            )
        else:
            adapters.append(
                BedrockModelAdapter(
                    c.name,
                    c.bedrock_model_id,
                    client_factory=client_factory,
                    family=c.family,
                    thinking=c.thinking,
                    budget_tokens=c.budget_tokens,
                    max_tokens=c.max_tokens,
                    temperature=c.temperature,
                    accepts_temperature=c.accepts_temperature,
                    instruction_override=instruction_override,
                )
            )
    return adapters
