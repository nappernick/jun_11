# API Contract 12. Bedrock Inline Agent Streaming Across Skywalker's Client Surfaces

This contract pins how the Slack-side application and the UAT inline-agent orchestrator stream the final user-facing response from `InvokeInlineAgent` instead of waiting for the full text. It complements [API_05](done/API_05_Bedrock_Inline_Agents.md) (the inline-agent + RETURN_CONTROL contract) and pairs with Section 05 (UAT slice), Section 08 (Slack integration), Section 09 (QuickSuite — explicitly sync because we don't run the agent there), and API_01 (the MCP envelope shape, which is unchanged).

The choice this document fixes is straightforward: streaming the inline agent's **final response** is now Skywalker's chosen rendering posture on every client surface where Skywalker itself runs the agent. The Skywalker MCP server's response back to the agent stays sync, because that response is a structured evidence package, not a generated narrative. Streaming sits one layer above the MCP boundary — between the inline agent and the client surface — where there is genuinely text being generated token-by-token and a real perceived-latency win to capture.

## What the architecture has already fixed

- API_05: the Slack and UAT paths use `InvokeInlineAgent` with a single `RETURN_CONTROL` action group. Bedrock returns control to the orchestrator when the agent decides to call retrieval; the orchestrator performs a real MCP `tools/call` against Skywalker through Amazon MCP Gateway and feeds the structured backend result back via `inlineSessionState.returnControlInvocationResults`. Section 08 §3 decision five fixes the same pattern for Slack.
- Section 01 and Section 02: Skywalker is a retrieval backend behind MCP. The MCP envelope contract (`result_kind`, `route`, `scope_snapshot`, `evidence`, `abstain_reason`, `correlation_id`) is sync over JSON-RPC 2.0 and does not stream.
- Section 08 §3 decision ten: the Slack end-to-end target is **under 4 seconds p95** from user message receipt to final reply delivery. Skywalker's retrieval pipeline is budgeted at 250–450 ms p95, leaving roughly 3.5 seconds for the inline-agent loop, the MCP call, generation, and Slack transport. Most of that 3.5 seconds is model generation, which is exactly where streaming buys the most user-visible win.
- Section 09: QuickSuite consumes Skywalker's MCP envelope through MCP Gateway directly. **Skywalker does not run the agent on the QuickSuite path** — QuickSuite's chat-agent runtime does. Streaming on the QuickSuite surface is therefore QuickSuite's own concern, not Skywalker's; this document does not change Section 09's sync MCP boundary.

## Chosen shape: stream the inline agent's final response, keep the MCP boundary sync

Set `streamingConfigurations.streamFinalResponse: true` on every `InvokeInlineAgent` call from the Slack app and the UAT inline-agent orchestrator, in both the first turn (where the agent may produce a clarification reply directly) and the second turn (where the agent composes the grounded final answer from the Skywalker evidence package). Do not change anything below the MCP boundary: Skywalker's MCP server still returns a single sync JSON-RPC result, MCP Gateway still returns a single sync HTTP response, the evidence envelope is unchanged, the route metadata is unchanged, the abstain branches are unchanged.

The reason streaming applies one layer above the MCP boundary and not at it is structural. The Skywalker MCP envelope is a structured object — ranked candidates, citations, route record, abstain reason — not a sequence of tokens. Streaming a JSON object byte-by-byte to the agent buys nothing; the agent has to wait for the close-brace before it can use any of it. Conversely, the inline agent's final response is genuine narrative text generated token-by-token by Claude Sonnet 4.6, and the Slack and UAT users care about time-to-first-token, not total bytes. Streaming the right layer is the win; streaming the wrong one is ceremony.

Content from external sources has been rephrased for compliance with licensing restrictions.

## What `streamingConfigurations` actually does

Per the public [InvokeInlineAgent API reference](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-runtime_InvokeInlineAgent.html), [StreamingConfigurations type](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-runtime_StreamingConfigurations.html), and [Invoke an agent from your application](https://docs.aws.amazon.com/bedrock/latest/userguide/agents-invoke-agent.html), the `streamingConfigurations` object has two fields:

- `streamFinalResponse` (boolean, default `false`). When `true`, the agent's final response text is delivered as multiple `chunk` events on the `completion` event stream, in order, instead of one `chunk` event carrying the whole response. Setting it `false` produces the legacy behavior where the entire response arrives in one chunk after generation completes.
- `applyGuardrailInterval` (integer, default `50` characters). Controls how often Bedrock applies the configured Guardrail to streaming output. Smaller intervals mean more frequent `ApplyGuardrail` calls and finer-grained guardrail enforcement at the cost of extra latency; larger intervals batch more characters between guardrail checks. We do not configure a Guardrail at launch (Section 08 §3 does not require one and Section 05 inherits that), so `applyGuardrailInterval` is effectively dormant for us; we leave it at the default. If a Guardrail is added later, the interval becomes a real calibration surface.

Two operational details are worth pinning explicitly so they don't surprise an implementer:

- **Streaming requires `bedrock:InvokeModelWithResponseStream` on the service execution role** in addition to whatever permission was already required to call `InvokeInlineAgent`. The Slack application's IAM execution role and the UAT inline-agent orchestrator's IAM execution role each need this added.
- **The AWS CLI does not support streaming InvokeInlineAgent calls.** This matters only for ad-hoc operator debugging; runtime traffic uses the AWS SDK for Java v2, which supports event streams natively via `BedrockAgentRuntimeAsyncClient`.

## Interaction with `RETURN_CONTROL`

This is the most easily mis-remembered part of the design, so it's worth being explicit.

`streamFinalResponse` streams **the final response generation only**. It does not stream the agent's tool-decision events. When the inline agent decides to call our Skywalker retrieval tool, Bedrock emits a `returnControl` event on the completion stream as a single discrete event — not as streamed chunks. The orchestrator's return-control handler reads that event, performs the real MCP `tools/call` against Skywalker through MCP Gateway (sync, sub-500 ms), and then issues the second `InvokeInlineAgent` call with `inlineSessionState.returnControlInvocationResults` populated and `streamingConfigurations.streamFinalResponse: true` set again. The second call is the one whose output streams as `chunk` events, because that's where Claude Sonnet 4.6 is actually generating prose from the evidence package.

The per-turn flow therefore looks like this on a typical answerable request:

1. **Turn 1: `InvokeInlineAgent`** with `streamingConfigurations.streamFinalResponse: true`. The agent reasons about the user input and decides to call retrieval. The completion stream carries one `returnControl` event (sync, not streamed) plus any `trace` events. The orchestrator reads the `returnControl` event and continues.
2. **Synchronous MCP call to Skywalker** through MCP Gateway. Sub-500 ms p95. Returns the structured evidence envelope.
3. **Turn 2: `InvokeInlineAgent`** with the same `sessionId`, the same `streamingConfigurations.streamFinalResponse: true`, and `inlineSessionState.returnControlInvocationResults` populated with the Skywalker envelope. The agent generates the final grounded answer with citations. **This call's output streams** as multiple `chunk` events carrying the assistant's text in order, separated at most every `applyGuardrailInterval` characters. `trace` events are interleaved with chunks. The completion stream ends when the model is done generating.

The other paths fall out of the same model. If the agent decides on Turn 1 to ask the user a clarification question directly without calling retrieval, the completion stream carries `chunk` events that stream the clarification text — there is no return-control event and no Turn 2. If the MCP call fails at the transport level, the orchestrator feeds an explicit failure indicator back via `returnControlInvocationResults`, the second invocation streams the model's failure-shaped response, and the agent is instructed by its behavioral prompt to surface a system-failure message rather than answer from memory.

One subtlety from the public docs: enabling streaming does not change which events arrive on the completion stream. `chunk`, `trace`, `returnControl`, `files`, and the various exception events are all still possible on the same stream, just with `chunk` events sub-divided. Code that consumes the stream needs to switch on event type, not assume any single event shape — which the existing return-control handler already does.

## Why the Skywalker MCP boundary stays sync

Three reasons, in increasing order of irreducibility:

1. **Skywalker's response is structured, not narrative.** The MCP envelope is a JSON object with a fixed schema (API_01). There is no time-to-first-token to optimize because there is no text being generated; the reranker scores candidates and the assembler reconstructs evidence, and both finish before anything goes on the wire. Streaming JSON tokens to a consumer that has to parse the whole object before using any of it is not a win.
2. **Skywalker's p95 budget is 250–450 ms.** Time-to-first-byte improvements at that scale are below the threshold of human perception on the user-visible side; the 4-second p95 budget Section 08 §3 decision ten fixes is dominated by model generation, not by retrieval. Optimizing the part of the budget that's already small while ignoring the part that's large would be misallocated effort.
3. **MCP Gateway's HTTP transport on our auth combination is sync.** The [MCP Gateway concepts](https://docs.hub.amazon.dev/docs/mcp-gateway/user-guide/concepts/) page documents the gateway's custom HTTP transport with auth in headers and JSON-RPC 2.0 in the body, with no SSE or streaming framing on the supported auth patterns we use (SigV4 from Slack and UAT, Federate OAuth from QuickSuite). This is a real constraint, not a preference. Even if we wanted to stream the MCP envelope, the gateway path doesn't expose the primitive.

The result is that the architectural seam stays clean. Skywalker is the retrieval backend that returns sync evidence packages. The agent layer above it is the streaming layer. The two responsibilities live where they belong rather than smearing into each other.

## Per-surface streaming behavior

### UAT React frontend (Section 05)

The orchestrator subscribes to the inline-agent completion stream and forwards `chunk` events to the React app over [Server-Sent Events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events) (SSE). The React app uses the browser-native `EventSource` API to receive chunks and renders text progressively into the chat message bubble. On Turn 1 the orchestrator buffers the (sync) `returnControl` event internally and does not forward it to the React app. On Turn 2 the orchestrator forwards each `chunk` event as it arrives. When the completion stream ends, the orchestrator emits a terminating SSE event (e.g., `event: done`) so the React app stops listening.

The orchestrator cannot run on AWS Lambda Function URL with `RESPONSE_STREAM` invoke mode for this path at launch, because Section 05 §3 decision four fixes the orchestrator as a normal compute surface that consumes the inline agent's response as an event stream and re-emits it as SSE. A normal long-lived HTTP connection on the orchestrator is the simplest shape; a Lambda with response streaming would also work but introduces a second deployment surface that doesn't add anything for UAT's deliberately narrow scope. ECS Fargate behind an ALB with HTTP/1.1 keep-alive is the obvious launch posture. The HTTP request from the React app to the orchestrator stays open for the full duration of the request (including the sub-500 ms MCP call between Turn 1 and Turn 2). The ALB idle timeout needs to be set higher than the worst-case end-to-end response time — at the 4-second p95 end-to-end target, an ALB idle timeout of 60 seconds is comfortable.

The React rendering is the simplest part: append each chunk's text to a state buffer, render with a typewriter cursor or similar progressive indicator, replace with the final formatted message (citations, sources list) on the terminating event. On `result_kind: ABSTAIN` (which the agent surfaces in its final response based on the Skywalker envelope), the React app still renders the abstain message progressively but applies the structurally-distinct abstain styling at the end (Section 05 §3 decision nine).

### Slack (Section 08)

The Slack application uses the same inline-agent streaming primitive but renders to Slack's `chat.update` API instead of SSE. The pattern is well-established across multiple internal implementations (SAIL Slack Bot, BulBul, Q Slack Gateway, InternalAnswers Slack Bot all use variants of it):

1. On user message receipt, post an initial placeholder message via `chat.postMessage` with the bot's "thinking" indicator. Capture the returned `ts` (timestamp ID) for subsequent updates.
2. Open the inline-agent invocation with `streamFinalResponse: true`. Buffer incoming `chunk` events into a string.
3. Every **2.5 seconds** (the launch default; SSM-tunable), call `chat.update` with the message `ts`, the accumulated text, and a partial Block Kit rendering. The 2.5-second cadence sits comfortably under Slack's documented `chat.update` rate limit of approximately 1 call per second per channel, and avoids visual flicker that a tighter cadence produces.
4. When the completion stream ends, perform a final `chat.update` with the complete Block Kit response — section block for the answer body, context block for the source titles and URLs drawn from the evidence package's `citations[]` (Section 08 §3 decision nine).
5. On stream error or timeout, transition the message to a structurally-distinct error state via a final `chat.update` (Section 08 §3 decision seven).

Two implementation details from real production Slack-bot code that matter at the architecture layer:

- **Partial Block Kit during streaming must close unterminated markdown elements gracefully.** A streamed chunk that ends mid-code-block or mid-list will make Slack render a malformed block; the partial-block builder needs to close any open code fences and list elements at the current buffer position. This is a rendering-layer concern but worth pinning so it doesn't get rediscovered as a bug.
- **The 3-second Slack acknowledgement deadline is independent of streaming.** Slack requires the bot to acknowledge slash commands and event subscriptions within 3 seconds of receipt; the placeholder `chat.postMessage` in step 1 satisfies that deadline regardless of how long the inline-agent invocation takes. Streaming and the Slack ack rule are orthogonal.

A consequence worth naming: streaming favors deployment topologies that hold long connections naturally. A Lambda-backed Slack webhook on API Gateway has a hard 29-second response limit and an awkward control-flow model where a self-invoke pattern is needed to escape the 3-second deadline. ECS or Fargate behind a long-lived process model handles streaming much more directly; Socket Mode handles it most directly of all because the WebSocket connection is already persistent. The launch deployment topology choice is captured in API_03 and Section 08 §9 as an open implementation-detail item; the streaming requirement is a soft argument in favor of ECS or Socket Mode over Lambda+Webhook, but not strong enough to relitigate the choice on its own.

### QuickSuite (Section 09 — unchanged)

Skywalker does not run the inline agent on the QuickSuite path. QuickSuite's chat-agent runtime is the agent layer; it calls Skywalker's MCP server through MCP Gateway and consumes the sync envelope, then composes its own user-facing response on its own side. Whether QuickSuite's runtime streams its own final generation to its own UI is a QuickSuite product behavior that this architecture does not control or document. Section 09's "responses are sync only" wording therefore stays accurate as a statement about Skywalker's MCP boundary, which is what we control. **No change to Section 09 is required by this contract.**

## Wire shape (AWS SDK for Java V2)

`BedrockAgentRuntimeAsyncClient` exposes `invokeInlineAgent` as an event-stream-returning method. The handler subscribes to a `Flowable` (or equivalent reactive type depending on the SDK version) of `InlineAgentResponseStream` events and pattern-matches on event type. Pseudocode for the second-turn streaming call:

```java
InvokeInlineAgentRequest req = InvokeInlineAgentRequest.builder()
    .foundationModel(CLAUDE_SONNET_46_MODEL_ID)
    .instruction(STABLE_BEHAVIORAL_PROMPT)
    .sessionId(sessionId)
    .actionGroups(skywalkerSearchActionGroup)
    .inlineSessionState(InlineSessionState.builder()
        .invocationId(returnControlInvocationId)
        .returnControlInvocationResults(skywalkerEnvelopeAsResult)
        .build())
    .streamingConfigurations(StreamingConfigurations.builder()
        .streamFinalResponse(true)
        .build())
    .build();

InvokeInlineAgentResponseHandler handler = InvokeInlineAgentResponseHandler.builder()
    .subscriber(InvokeInlineAgentResponseHandler.Visitor.builder()
        .onChunk(chunk -> emitChunkToClient(chunk.bytes().asUtf8String()))
        .onTrace(trace -> recordTrace(trace))
        .onReturnControl(rc -> { /* not expected on Turn 2 */ })
        .onDefault(evt -> handleOther(evt))
        .build())
    .onError(err -> emitErrorToClient(err))
    .onComplete(() -> emitTerminationToClient())
    .build();

bedrockAgentRuntimeAsyncClient.invokeInlineAgent(req, handler).join();
```

`emitChunkToClient` is the surface-specific renderer: SSE write for UAT, buffered `chat.update` for Slack. `emitErrorToClient` and `emitTerminationToClient` are the corresponding terminal handlers. Inference parameters (`temperature: 0`, `maxTokens: 1024`) carry over from API_05 unchanged.

## Latency and the perceived-latency win

Streaming does not change total latency. It changes time-to-first-token, which is what users actually notice on a chat surface.

Concretely, on the Slack 4-second p95 budget (Section 08 §3 decision ten):

- Without streaming, the user sees the placeholder "thinking" indicator for the full duration (Skywalker + Turn-1 model reasoning + MCP call + Turn-2 model generation + Slack transport, on the order of 2.5–3.5 seconds), then the complete answer appears at once.
- With streaming, the user sees the placeholder for only the Turn-1 reasoning portion plus the sub-500 ms MCP call (roughly 1–1.5 seconds), then the answer begins appearing token-by-token as Turn-2 generates. Total time to last token is unchanged, but perceived latency drops materially because the user sees forward progress almost immediately on the dominant generation phase.

On the UAT React surface the gain is similar but more pronounced because typing-in-progress UI is the natural rendering for SSE chunks.

The win is not free. Two real costs:

- **Slack's `chat.update` rate limit means the streamed message updates every 2.5 seconds, not per token.** This is a cadence, not a true token stream. Over a 2-second model generation, the user sees one or two intermediate updates plus the final, which is a clear improvement over silence-then-everything but is not the per-token feel that SSE gives the React app. We accept this asymmetry because Slack's API constraints make per-token streaming infeasible at the platform level, not because we chose to render coarsely.
- **Stream-error handling is more complex than sync-error handling.** A failure mid-stream after some tokens have already been rendered to the user requires a graceful state transition rather than a clean replace. Both UAT and Slack handle this by performing a final write that replaces the partial buffer with a structurally-distinct error message. The architectural rule (errors must be visually and copy-distinct from abstain and from answerable, per Section 05 §3 decision nine and Section 08 §3 decision seven) carries over unchanged; the implementation just has more states to traverse.

## What we still test as if it were sync

The MCP envelope contract (API_01) does not gain new fields and does not change shape. Every existing assertion about `result_kind`, `route`, `scope_snapshot`, `evidence`, `abstain_reason`, and `correlation_id` continues to hold without modification. Tests that exercise Skywalker's MCP surface directly (without going through the inline agent) remain unchanged. Tests that exercise the inline agent's behavior on the structured envelope (e.g., "given an abstain envelope, the agent renders the abstain message structurally distinct from the answerable message") need to handle the streamed event sequence rather than the legacy single-chunk shape, but the assertions about end-state are the same.

This is the property that justifies adding streaming as a rendering-layer concern rather than as a backend concern: nothing below the inline agent has to know it's happening.

## Failure handling

- **MCP call failure mid-loop.** Transport-level failure of the MCP `tools/call` between Turn 1 and Turn 2 is unchanged from the sync world. The orchestrator feeds an explicit failure indicator back via `returnControlInvocationResults`, the agent's behavioral prompt directs it to render a system-failure message, and the streamed text on Turn 2 reflects that. The user sees a streaming error message rather than a streaming answer; both UAT and Slack apply their service-failure styling on the terminating event.
- **Bedrock stream error mid-generation.** A `ModelStreamErrorException` or transport reset arriving mid-stream after some tokens have been rendered. The handler aborts the stream, the surface-specific renderer performs a final replace with a service-failure message, and the partial text the user briefly saw is overwritten. Both UAT and Slack already need a final-state write for the success case; reusing it for the error case is mechanical.
- **Bedrock throttling at stream start.** A `ThrottlingException` thrown before any chunks arrive is identical in shape to the sync case. The orchestrator propagates a service-failure indicator and the surface renders accordingly. Retries for transient throttling follow the inline-agent SDK's normal retry policy (configurable via `ClientOverrideConfiguration` on the async client).
- **Slack `chat.update` rate-limit hit.** If the Slack API rejects a `chat.update` call with a rate-limit error during streaming, the bot drops that interim update and waits for the next 2.5-second tick rather than retrying inline. The final update on stream completion is more important than any intermediate update; if even the final update fails, we treat that as a Slack-side outage and log structured.
- **Stream timeout on the orchestrator→Bedrock connection.** The orchestrator sets an explicit read timeout on the `BedrockAgentRuntimeAsyncClient` that comfortably exceeds the 4-second p95 target — 30 seconds is the launch default, generous enough that legitimate slow generations complete and tight enough that hung connections don't accumulate. A timeout is treated as a service failure.

## Outstanding unknowns

- The exact model ID for Claude Sonnet 4.6 on Bedrock and its regional availability. This is an open item from API_05 and is unchanged here.
- Whether the team eventually adds a Bedrock Guardrail to the path. If a Guardrail is configured later, `applyGuardrailInterval` becomes a real calibration surface (smaller intervals trade latency for finer-grained content checks). Launch posture is no Guardrail and the default 50-character interval is dormant.
- Whether the UAT inline-agent orchestrator runs on ECS Fargate with an ALB or another long-lived compute surface. The streaming requirement strongly prefers a long-lived process model over Lambda, but the deployment topology choice for UAT is otherwise an operational detail (Section 05 §9). Lambda Function URL with `RESPONSE_STREAM` invoke mode is a viable fallback if a different shape becomes preferred.
- Whether the Slack application moves to Socket Mode or stays on HTTP webhook + Lambda for the launch deployment. Streaming is a soft argument in favor of Socket Mode (or ECS) but is not by itself decisive (Section 08 §9). The `chat.update` rate-limited pattern works on either deployment topology.
- Whether `enableTrace: true` should remain on at launch under streaming. Trace events interleave with chunk events on the completion stream and don't materially affect rendering, but they do add bytes; if production observability shows traces are not useful in production, turning them off post-launch is mechanical.

## What this contract binds

- API_05 (the inline-agent + RETURN_CONTROL contract) — extends it with the streaming configuration on every invocation.
- Section 05 §6 (UAT data flow) — replaces "no streaming" with SSE-streamed final response for the React app.
- Section 08 §3 decision six and §6 (Slack inline-agent invocation, end-to-end data flow) — replaces "single complete `chat.postMessage`" with rate-limited `chat.update` against the inline agent's streamed final response.
- Section 02 §2 (output contract back to clients) — clarifies that the MCP envelope itself stays sync; streaming sits one layer above the MCP boundary on the client surfaces where Skywalker runs the agent.
- Section 09 (QuickSuite consumption model) — explicitly unchanged. Skywalker still returns a sync envelope to QuickSuite; QuickSuite's runtime is not Skywalker's agent layer.

## Sources

- [InvokeInlineAgent API reference](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-runtime_InvokeInlineAgent.html)
- [StreamingConfigurations type](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-runtime_StreamingConfigurations.html)
- [Invoke an agent from your application](https://docs.aws.amazon.com/bedrock/latest/userguide/agents-invoke-agent.html)
- [Configure an inline agent at runtime](https://docs.aws.amazon.com/bedrock/latest/userguide/agents-create-inline.html)
- [ResponseStream event type](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-runtime_ResponseStream.html)
- [Return control to the agent developer](https://docs.aws.amazon.com/bedrock/latest/userguide/agents-returncontrol.html)
- BuilderHub [MCP Gateway concepts](https://docs.hub.amazon.dev/docs/mcp-gateway/user-guide/concepts/) — confirms the gateway's custom HTTP transport is sync over JSON-RPC 2.0 with auth in headers, with no SSE framing on the auth combinations we use.
- Internal [Bedrock Training](https://w.amazon.com/bin/view/Users/zhenhez/bedrock/training/2025-06-17-v2/) — concrete examples of `streamingConfigurations: {streamFinalResponse: True}` invocations and the resulting trace + chunk event sequences.
- Internal [SAIL Slack Bot LLD §6.2](https://w.amazon.com/bin/view/SWA/ShipperAccountManager/SAIL/SlackBot/LLD/) — production reference for rate-limited `chat.update` streaming pattern, the 2.5-second cadence, and partial Block Kit rendering. Several other internal Slack bots (BulBul, Q Slack Gateway, InternalAnswers) implement variants of the same pattern.
