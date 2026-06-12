"""
Persistent-session inline-agent adapter for the closed-loop prompt optimizer
(design Component 9, Req 3.6 / 12.1 / 12.4).

This is the **quality-study** answer adapter. It drives a Target_Model through Bedrock
**Agent Runtime** ``InvokeInlineAgent`` with a prompt override, exactly like
:class:`bakeoff.adapters.inline_agent.InlineAgentAdapter`, but with three deliberate
differences that make it the *optimizer's* adapter rather than the bake-off's:

1. **One conversation == one stable ``sessionId``, one invoke PER TURN.** The bake-off
   adapter flattens every turn of a multi-turn item into a single ``inputText`` under a
   throwaway random ``sessionId`` (it only cares about a single answer). The optimizer
   instead issues a **separate** ``invoke_inline_agent`` call for each turn, all under the
   **same** stable ``sessionId``, sending only that turn's user utterance as
   ``inputText``/``$question$`` — matching what production actually runs and letting
   Bedrock retain turn history server-side (``history_mode="server"``). An AWS-doc-grounded
   fallback (``history_mode="explicit"``) replays the prior turns via
   ``inlineSessionState.conversationHistory`` if the live probe (Task 9.6) shows the server
   does not auto-inject history under an OVERRIDDEN minimal template.

2. **The optimized prompt is the ONLY system instruction.** ``instruction_override`` (the
   challenger/champion prompt under test) is passed verbatim as ``instruction`` and becomes
   ``$instruction$`` in the OVERRIDDEN minimal base template
   (:data:`bakeoff.config.QUALITY_OPT_INLINE_TEMPLATE`). That template's system block is
   ``$instruction$`` followed by a framed ``$prompt_session_attributes$`` placeholder where
   the turn's retrieved fragments are injected (see point 3); the user turn is the bare
   ``$question$``.

3. **Fragments ride the per-turn ``promptSessionAttributes`` channel, NOT the question.**
   No ``actionGroups`` and no ``knowledgeBases`` are ever attached (the no-noise / no-tool
   fidelity invariant the mandatory unit test and Property 23 assert against — no
   ``<tools>``, no function-call schema, no "you have access to the following action groups",
   no ReAct ``Thought/Action/Observation``). The retrieved fragments are passed through
   ``inlineSessionState.promptSessionAttributes`` (a ``Map<String,String>`` AWS renders into
   the ``$prompt_session_attributes$`` placeholder in our OVERRIDDEN template) — they were
   formerly concatenated INLINE into ``$question$``, but ``promptSessionAttributes``
   **persists over a single turn only** (one ``InvokeInlineAgent`` call), whereas the bare
   ``$question$`` is persisted into the session's server-side conversation history. Inlining
   fragments into the question therefore made every later turn replay ALL prior turns'
   fragments, blowing past the 200k context limit on multi-turn items; routing them through
   the single-turn attribute channel makes each turn carry ONLY its own fragments, bounded
   regardless of turn count. ``sessionAttributes`` (the session-scoped channel) is still
   never set.

**Fragments are off by default** (``send_fragments=config.QUALITY_OPT_SEND_FRAGMENTS`` ==
``False``): the judge grounds later turns against ``wants`` and turn-1 against the
gold-derived ideal/abstention, none of which requires the model to have been handed
retrieval fragments. When ``send_fragments=True`` the fragments are injected through the
per-turn ``inlineSessionState.promptSessionAttributes`` channel (rendered into the
``$prompt_session_attributes$`` placeholder), so they are visible to the model for that turn
but never accumulate into the persisted conversation history.

Extended thinking is **not honored** on the inline path (recorded ``thinking_honored=False``
on ``raw``); acceptable because both Target_Models are thinking-off.

Resilience mirrors the existing adapters: each invoke is wrapped in
:func:`bakeoff.resilience.call_with_resilience` (auth-expiry client rebuild +
throttle/transient backoff), boto3 is blocking so the invoke runs in a worker thread, and
the ``bedrock-agent-runtime`` client is built lazily via an injectable ``client_factory``
(default: a real boto3 builder) so tests need no network.

Latency capture is best-effort here (quality study, not the speed study): TTFT is the first
turn's first chunk and ``generation_total_ms`` is the summed per-turn wall-clock — kept
because it is cheap, but the load-bearing outputs are the answer text, the ordered
``per_turn_answers``, and the no-noise request shape.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

import bakeoff.config as config
from bakeoff.adapters.base import assemble_context
from bakeoff.resilience import call_with_resilience
from bakeoff.types import Item, ModelResponse

__all__ = ["PersistentSessionInlineAdapter"]

#: ``generate()`` handles exactly one conversation and carries no rep dimension at this
#: layer, so it derives the per-conversation ``sessionId`` by calling ``session_id_for``
#: with this fixed rep. The result is therefore ONE stable id per ``Item`` (all turns of
#: the conversation reuse it). A caller that wants distinct sessions per repetition
#: injects its own ``session_id_for``.
_GENERATE_REP: int = 0

#: The method tag recorded on ``ModelResponse.raw`` so a reader can tell this adapter's
#: rows apart from the bake-off inline adapter's (``"inline_agent"``).
_METHOD: str = "persistent_session_inline_agent"


class _TurnStream:
    """The result of consuming one turn's ``invoke_inline_agent`` completion stream."""

    __slots__ = ("text", "ttft_ms", "generation_total_ms", "finish_reason")

    def __init__(self, text: str, ttft_ms: float, generation_total_ms: float,
                 finish_reason: Optional[str]) -> None:
        self.text = text
        self.ttft_ms = ttft_ms
        self.generation_total_ms = generation_total_ms
        self.finish_reason = finish_reason


class PersistentSessionInlineAdapter:
    """A per-turn, persistent-session Bedrock inline-agent :class:`ModelAdapter`.

    Implements the :class:`bakeoff.adapters.base.ModelAdapter` protocol (``name`` +
    ``async generate(item, fragments, temperature) -> ModelResponse``). Distinct from
    :class:`bakeoff.adapters.inline_agent.InlineAgentAdapter`.
    """

    def __init__(
        self,
        name: str,
        bedrock_model_id: str,
        *,
        instruction_override: str,
        send_fragments: bool = config.QUALITY_OPT_SEND_FRAGMENTS,
        history_mode: str = config.QUALITY_OPT_INLINE_HISTORY_MODE,
        client_factory: "Callable[[], Any] | None" = None,
        credential_profile: Optional[str] = None,
        region: Optional[str] = None,
        session_id_for: "Callable[[Item, int], str] | None" = None,
        clock: Callable[[], float] = time.perf_counter,
        sleep: "Callable[[float], Awaitable[None]] | None" = None,
    ) -> None:
        """Construct the adapter.

        Args:
            name: stable adapter/candidate name (stamped onto ``ModelResponse.model`` and
                used in the default ``sessionId`` scheme).
            bedrock_model_id: the Bedrock foundation-model id to invoke (e.g.
                ``"us.anthropic.claude-haiku-4-5-20251001-v1:0"``).
            instruction_override: the optimized system prompt under test. This is the
                **only** system instruction the model receives — it becomes ``$instruction$``
                in the OVERRIDDEN minimal template.
            send_fragments: when ``True``, the turn's retrieved fragments are passed through
                the per-turn ``inlineSessionState.promptSessionAttributes`` channel (rendered
                into the ``$prompt_session_attributes$`` placeholder), NOT concatenated into
                the question. Default ``False`` (fragment-free) per the design.
            history_mode: ``"server"`` relies on Bedrock's server-side session history keyed
                to the stable ``sessionId`` (the owner-asserted behavior, validated by the
                Task 9.6 live probe). ``"explicit"`` replays prior turns via the AWS-documented
                ``inlineSessionState.conversationHistory`` field. Any other value is treated
                as ``"server"`` defensively.
            client_factory: zero-arg callable returning a ``bedrock-agent-runtime`` client.
                Defaults to a real boto3 builder. Injected by tests so no network is needed;
                also the seam the auth-expiry refresh uses to rebuild the client.
            region: AWS region for the default client factory (defaults to
                :data:`bakeoff.config.AWS_REGION`).
            session_id_for: ``(item, rep) -> str`` producing the conversation's stable
                ``sessionId``. Defaults to ``f"opt-{name}-{item.item_id}-{rep}"``; ``generate``
                calls it with ``rep=0`` (see :data:`_GENERATE_REP`) so each conversation maps
                to exactly one stable id.
            clock: monotonic clock (injectable for deterministic latency in tests).
            sleep: async sleep used for resilience backoff (injectable; defaults to
                :func:`asyncio.sleep`).
        """
        self.name = name
        self.bedrock_model_id = bedrock_model_id
        self.instruction_override = instruction_override
        self.send_fragments = bool(send_fragments)
        self.history_mode = history_mode
        self.region = region or config.AWS_REGION
        #: Credential profile (account) the target client + its auth-expiry refresh bind to
        #: via the broker; None -> the broker default. The dedicated EXECUTION account is
        #: injected here so target generation has its own quota for the ~24-concurrent lane.
        self.credential_profile = credential_profile
        self._session_id_for = session_id_for or self._default_session_id_for
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._client_factory = client_factory or self._default_client_factory
        self._client: Optional[Any] = None

    # -- client lifecycle / credential chain ------------------------------
    def _default_client_factory(self) -> Any:
        """Build a ``bedrock-agent-runtime`` client from the credential broker.

        Binds to the explicit broker profile (never the ambient ``AWS_PROFILE``), so
        a sibling agent flipping the environment cannot redirect this client, and the
        session is proactively TTL-refreshed by the broker.
        """
        from bakeoff.credentials import get_broker

        session = get_broker().get_session(self.credential_profile, region=self.region)
        return session.client("bedrock-agent-runtime", region_name=self.region)

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def _refresh_credentials(self) -> None:
        """Auth-expiry refresh hook: actually MINT new credentials, then rebuild.

        The previous hook only rebuilt the client from the same on-disk file, so an
        expired token was simply re-read. Here the broker re-runs ``ada`` (subject to
        cross-process coalescing) so the rebuilt client gets a genuinely fresh token.
        A lapsed-Midway failure propagates so the run fails fast with an actionable
        message instead of looping the retry budget on an unfixable cause.
        """
        from bakeoff.credentials import get_broker

        try:
            get_broker().refresh()
        except Exception:  # noqa: BLE001 - mint failure shouldn't mask the rebuild attempt
            # If the broker has no identity for the profile (or ada genuinely failed),
            # still rebuild from whatever is on disk — matches the old behavior as a
            # floor, while the broker path above is the real fix.
            import logging
            logging.getLogger("bakeoff.credentials").warning(
                "inline adapter credential refresh via broker failed; rebuilding from disk",
                exc_info=True,
            )
        self._client = self._client_factory()

    # -- session id -------------------------------------------------------
    def _default_session_id_for(self, item: Item, rep: int) -> str:
        """Default stable ``sessionId`` scheme: ``opt-<name>-<item_id>-<rep>``."""
        return f"opt-{self.name}-{item.item_id}-{rep}"

    # -- prompt / question shaping ----------------------------------------
    def _instruction(self) -> str:
        """The system instruction, padded to Bedrock's inline minimum length.

        ``instruction_override`` is the ONLY system instruction the model sees. Bedrock's
        inline path enforces a minimum ``instruction`` length (~40 chars); we pad with
        trailing spaces defensively so a short optimized prompt never 400s, without changing
        the visible content.
        """
        instruction = self.instruction_override or ""
        if len(instruction) < config.INLINE_AGENT_MIN_INSTRUCTION_CHARS:
            instruction = instruction + (
                " " * (config.INLINE_AGENT_MIN_INSTRUCTION_CHARS - len(instruction))
            )
        return instruction

    def _turn_utterances(self, item: Item) -> list[str]:
        """The ordered per-turn user utterances for ``item``.

        Multi-turn → each turn's ``user_utterance`` in order; single-turn → the focal
        ``query`` as one utterance. A multi-turn item with no turn records degrades to its
        ``query`` so a malformed item never crashes generation.
        """
        if item.is_multi_turn and item.turns:
            return [t.user_utterance for t in item.turns]
        return [item.query or ""]

    def _prompt_session_attributes(self, fragments: Sequence[dict]) -> dict:
        """The per-turn ``promptSessionAttributes`` map carrying this turn's fragments.

        Empty unless ``send_fragments`` is on AND there are fragments. When present, it is a
        single ``{"retrieved_context": <assembled context>}`` entry whose value is the same
        :func:`bakeoff.adapters.base.assemble_context` rendering the rest of the study uses.
        AWS renders this ``Map<String,String>`` into the ``$prompt_session_attributes$``
        placeholder in our OVERRIDDEN template — visible to the model for THIS turn only, and
        (unlike the bare ``$question$``) never persisted into the conversation history, so
        fragments cannot accumulate across turns.
        """
        if not self.send_fragments or not fragments:
            return {}
        return {"retrieved_context": assemble_context(fragments)}

    def _inference_configuration(self, temperature: float) -> dict:
        """Inference knobs for the override config.

        The inline path accepts ``temperature`` even on the 4.x models (verified for the
        bake-off inline adapter), so the trial temperature is passed straight through. The
        answer cap reuses :data:`bakeoff.config.DEFAULT_MAX_TOKENS` (both Target_Models are
        thinking-off, so no thinking budget headroom is needed).
        """
        return {
            "maximumLength": int(config.DEFAULT_MAX_TOKENS),
            "temperature": float(temperature),
        }

    def _prompt_override_configuration(self, temperature: float) -> dict:
        """The OVERRIDDEN minimal ORCHESTRATION prompt override (the fidelity invariant).

        ``promptCreationMode=OVERRIDDEN`` with the minimal
        :data:`bakeoff.config.QUALITY_OPT_INLINE_TEMPLATE` (``$instruction$`` plus a framed
        ``$prompt_session_attributes$`` in the system, the bare ``$question$``, and the
        required empty ``$agent_scratchpad$``), ``parserMode=DEFAULT``, ``promptState=ENABLED``.
        """
        return {
            "promptConfigurations": [
                {
                    "promptType": "ORCHESTRATION",
                    "promptCreationMode": "OVERRIDDEN",
                    "parserMode": "DEFAULT",
                    "promptState": "ENABLED",
                    "basePromptTemplate": config.QUALITY_OPT_INLINE_TEMPLATE,
                    "inferenceConfiguration": self._inference_configuration(temperature),
                }
            ]
        }

    def _conversation_history(
        self, prior_turns: Sequence[tuple[str, str]]
    ) -> dict:
        """Build the AWS-documented ``inlineSessionState.conversationHistory`` payload.

        Used only in ``history_mode="explicit"``. ``prior_turns`` is the ordered list of
        ``(question_text, answer_text)`` for the turns already taken in this conversation;
        each becomes a ``user`` then ``assistant`` :class:`Message`. The ``ContentBlock`` for
        this API is a union whose ``text`` member is the plain string (``{"text": ...}``) —
        distinct from the Anthropic template's ``{"type": "text", "text": ...}`` shape.
        """
        messages: list[dict] = []
        for question_text, answer_text in prior_turns:
            messages.append({"role": "user", "content": [{"text": question_text}]})
            messages.append({"role": "assistant", "content": [{"text": answer_text}]})
        return {"conversationHistory": {"messages": messages}}

    # -- request shaping (the unit-testable seam) -------------------------
    def _build_request(
        self,
        item: Item,
        turn_index: int,
        prior_turns: Sequence[tuple[str, str]],
        temperature: float,
        *,
        fragments: Sequence[dict] = (),
    ) -> dict:
        """Build the ``invoke_inline_agent`` request dict for one turn.

        This is the seam the no-noise fidelity test (Task 9.2) and Property 23 (Task 9.3)
        assert against, so it is pure and free of network/clock effects. It produces the
        EXACT kwargs handed to ``client.invoke_inline_agent`` for ``item``'s turn at
        ``turn_index``:

        * ``sessionId`` — the conversation's one stable id (``session_id_for(item, 0)``), so
          every turn of the conversation shares it.
        * ``inputText`` — ONLY this turn's bare user utterance (no fragments concatenated in,
          so nothing fragment-sized is persisted into the conversation history).
        * ``instruction`` — the optimized prompt under test (padded), the only system text.
        * ``promptOverrideConfiguration`` — OVERRIDDEN minimal template, no tool scaffolding.
        * **No** ``actionGroups``, **no** ``knowledgeBases``, and **no** ``sessionAttributes``.
        * ``inlineSessionState`` — present when there is anything per-turn to carry: this
          turn's fragments via ``promptSessionAttributes`` (when ``send_fragments`` and the
          turn has fragments) and/or the replayed ``conversationHistory`` (only in
          ``history_mode="explicit"`` with prior turns). Absent entirely when neither applies.

        Args:
            item: the conversation being answered.
            turn_index: 0-based index of the turn this request answers.
            prior_turns: ordered ``(question_text, answer_text)`` for the turns already taken
                (used only to replay history in ``explicit`` mode).
            temperature: the trial temperature, passed through to the inference config.
            fragments: the constant retrieved fragments; only consulted when
                ``send_fragments`` is on.

        Returns:
            The request kwargs dict for ``invoke_inline_agent``.
        """
        utterances = self._turn_utterances(item)
        idx = max(0, min(turn_index, len(utterances) - 1))
        # The question is the BARE utterance — fragments ride promptSessionAttributes below,
        # so nothing fragment-sized is persisted into the session's conversation history.
        question_text = utterances[idx]

        request: dict[str, Any] = {
            "sessionId": self._session_id_for(item, _GENERATE_REP),
            "foundationModel": self.bedrock_model_id,
            "instruction": self._instruction(),
            "inputText": question_text,
            "enableTrace": False,
            "promptOverrideConfiguration": self._prompt_override_configuration(temperature),
        }

        # inlineSessionState carries the PER-TURN payload. Build it incrementally and only
        # attach it when non-empty, so the server-history success path stays minimal.
        inline_session_state: dict[str, Any] = {}

        # This turn's fragments, through the single-turn promptSessionAttributes channel
        # (rendered into $prompt_session_attributes$). Single-turn → no accumulation.
        prompt_session_attributes = self._prompt_session_attributes(fragments)
        if prompt_session_attributes:
            inline_session_state["promptSessionAttributes"] = prompt_session_attributes

        # Explicit-history fallback (AWS-doc-grounded): replay prior turns through the
        # documented conversationHistory field, keyed to the SAME sessionId. Server mode
        # attaches nothing here — Bedrock supplies prior turns from the persistent session.
        if self.history_mode == "explicit" and prior_turns:
            inline_session_state.update(self._conversation_history(prior_turns))

        if inline_session_state:
            request["inlineSessionState"] = inline_session_state

        return request

    # -- the blocking stream consumer (runs in a worker thread) -----------
    def _invoke_stream_sync(self, request: dict) -> _TurnStream:
        """Open one turn's inline-agent stream and consume it (blocking; worker thread).

        Mirrors :class:`bakeoff.adapters.inline_agent.InlineAgentAdapter`: the agent runtime
        returns an EventStream under ``response["completion"]`` yielding dicts; ``chunk``
        carries answer bytes (TTFT stamped on the first byte-bearing chunk), ``returnControl``
        would mean the model tried a tool call (impossible with the scaffolding stripped, but
        recorded as the finish reason if it ever occurs), other event types are tolerated.
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
                finish_reason = "return_control"

        total_ms = (self._clock() - start) * 1000.0
        if ttft_ms is None:
            ttft_ms = total_ms  # empty completion: TTFT degenerates to total
        return _TurnStream(
            text="".join(parts),
            ttft_ms=ttft_ms,
            generation_total_ms=total_ms,
            finish_reason=finish_reason,
        )

    async def _generate_turn(self, request: dict) -> _TurnStream:
        """Invoke one turn with auth-expiry + throttle/transient resilience."""

        async def attempt() -> _TurnStream:
            return await asyncio.to_thread(self._invoke_stream_sync, request)

        return await call_with_resilience(
            attempt, refresh_credentials=self._refresh_credentials, sleep=self._sleep
        )

    # -- the protocol method ----------------------------------------------
    async def generate(
        self,
        item: Item,
        fragments: "Sequence[dict] | Mapping[int, Sequence[dict]]",
        temperature: float,
    ) -> ModelResponse:
        """Answer ``item`` turn-by-turn under one persistent session.

        Issues one ``invoke_inline_agent`` PER TURN, all under the conversation's single
        stable ``sessionId``, sending only that turn's visible question as ``inputText``.
        Prior turns reach the model server-side (``history_mode="server"``) or via replayed
        ``conversationHistory`` (``history_mode="explicit"``). ``per_turn_answers`` holds each
        turn's answer in order; ``text`` is the last turn's answer. TTFT is the first turn's
        first chunk; ``generation_total_ms`` is the summed per-turn wall-clock.

        ``fragments`` may be EITHER a flat sequence (the same fragments applied to every
        turn) OR a ``{turn_index: fragments}`` mapping (turn N is grounded on exactly turn
        N's fragments — what the Judge grades that turn against). The mapping form is what
        the optimizer's grounded path uses so the model and the Judge see byte-identical
        per-turn evidence; a turn absent from the mapping gets no ``<context>`` block.
        """
        per_turn_fragments = isinstance(fragments, Mapping)
        session_id = self._session_id_for(item, _GENERATE_REP)
        utterances = self._turn_utterances(item)

        per_turn_answers: list[str] = []
        prior_turns: list[tuple[str, str]] = []
        ttft_ms: Optional[float] = None
        total_ms = 0.0
        finish_reason: Optional[str] = None

        for turn_index in range(len(utterances)):
            turn_fragments = (
                fragments.get(turn_index, ()) if per_turn_fragments else fragments
            )
            request = self._build_request(
                item, turn_index, prior_turns, temperature, fragments=turn_fragments
            )
            result = await self._generate_turn(request)

            per_turn_answers.append(result.text)
            if ttft_ms is None:
                ttft_ms = result.ttft_ms
            total_ms += result.generation_total_ms
            finish_reason = result.finish_reason

            # Record what was actually sent for this turn so an explicit-history replay on
            # the next turn is faithful to the visible question text.
            prior_turns.append((request["inputText"], result.text))

        if ttft_ms is None:  # defensive: no turns at all
            ttft_ms = 0.0
        text = per_turn_answers[-1] if per_turn_answers else ""

        raw: dict[str, object] = {
            "method": _METHOD,
            "bedrock_model_id": self.bedrock_model_id,
            # The inline path does not honor extended thinking (verified live for the
            # bake-off adapter); recorded honestly. Both Target_Models are thinking-off.
            "thinking_honored": False,
            "history_mode": self.history_mode,
            "send_fragments": self.send_fragments,
            "sessionId": session_id,
            "n_turns": len(per_turn_answers),
            "requested_temperature": temperature,
            "finish_reason": finish_reason,
        }

        return ModelResponse(
            text=text,
            ttft_ms=ttft_ms,
            generation_total_ms=total_ms,
            token_usage={},  # the inline-agent stream does not surface token usage
            per_turn_answers=per_turn_answers,
            finish_reason=finish_reason,
            model=self.name,
            raw=raw,
        )
