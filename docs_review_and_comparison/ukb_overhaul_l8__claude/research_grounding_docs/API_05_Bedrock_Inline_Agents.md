# API Contract 05. Amazon Bedrock — Inline Agents with RETURN_CONTROL

Covers how the Slack-side model layer runs its conversation loop and how it triggers a Skywalker MCP call. Pairs with Section 08 (Slack integration) and API_01 (MCP server surface).

## What the architecture has already fixed

- Section 08 §3 decision six: the Slack-side conversational model is Claude Sonnet 4.6.
- Section 08 §3 decision three: the Slack integration consumes Skywalker exclusively through MCP, routed through Amazon MCP Gateway. Slack is an MCP client of Skywalker like the UAT inline-agent orchestrator and QuickSuite are.
- Section 08 §6: the model is expected to choose between the alias-based tool and the explicit-scope tool deliberately, read the structured backend result, and compose the final Slack-visible message from it.
- Section 08 §3 decision nine: citations must trace back to the returned evidence package.
- Section 08 §3 decision ten: end-to-end latency target 4 s p95, of which roughly 3.5 s is available for Slack intake plus model reasoning plus MCP tool call plus model generation plus Slack transport.

## Chosen shape: Inline Agents with a RETURN_CONTROL action group

The Slack-side model layer uses Bedrock [InvokeInlineAgent](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-runtime_InvokeInlineAgent.html). The alternative would have been Converse with client-side tool-use; we do not use Converse for orchestration. Converse is explicitly out of scope for this path.

Within `InvokeInlineAgent`, we declare **one action group** whose `actionGroupExecutor` is `{ customControl: "RETURN_CONTROL" }` per [ActionGroupExecutor](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-runtime_ActionGroupExecutor.html). We explicitly reject the `lambda` executor alternative because it introduces a deployment surface we do not need for a tool that lives in the same process as the Slack application and already has a well-defined MCP contract.

With RETURN_CONTROL, the agent emits a return-control event when it decides to invoke a tool; our Java code executes the tool however it wants and feeds the result back into the next `InvokeInlineAgent` turn via `inlineSessionState.returnControlInvocationResults`. In our case, "execute the tool however it wants" means **issue a real MCP `tools/call` to Skywalker**. The agent still sees a tool invocation; the application decides that the tool is Skywalker over MCP.

Content from external sources has been rephrased for compliance with licensing restrictions.

## Per-turn flow

1. Bolt handler receives the Slack event and builds the normalized turn object (Section 08 §2).
2. First `InvokeInlineAgent` call with:
   - `foundationModel`: the Claude Sonnet 4.6 model ID.
   - `instruction`: the stable Slack behavioral prompt (Section 08 §3 decision six). Teaches the model that Skywalker is the source of scoped retrieval truth, abstention is valid, sources and policy links must be preserved, and ungrounded answers are not allowed.
   - `sessionId`: a stable ID derived from the Slack thread or DM channel so multi-turn state persists within the inline-agent session.
   - `actionGroups`: one action group declaring `skywalker_search` via `functionSchema` and `actionGroupExecutor: { customControl: "RETURN_CONTROL" }`.
   - `inputText`: the user's current turn text plus any normalized context the prompt assembly step includes.
   - `streamingConfigurations`: `{ streamFinalResponse: true }`. The agent's final response text streams back as multiple `chunk` events on the completion stream rather than as one consolidated chunk after generation completes (see API_12). `applyGuardrailInterval` is left at the default of 50 characters and is effectively dormant at launch because no Guardrail is configured.
3. The inline agent either replies directly (clarification turn — text streams as `chunk` events) or emits a `returnControl` event containing the tool name and arguments. The `returnControl` event itself is sync and discrete; only generated narrative text is streamed.
4. On return-control, the Java code extracts the arguments and performs an MCP `tools/call` against Skywalker's MCP server, **routed through Amazon MCP Gateway** with SigV4 inbound auth using the Slack application's IAM execution role (see API_01 and Section 08 §3). The Slack application is an MCP client of Skywalker. **The MCP boundary stays sync** — the gateway's custom HTTP transport does not stream on our auth combinations and Skywalker's evidence envelope is structured rather than narrative.
5. Skywalker returns the structured backend result (answerable evidence package or structured abstain package).
6. Second `InvokeInlineAgent` call using the same `sessionId`, with `inlineSessionState.returnControlInvocationResults` carrying the Skywalker MCP result serialized to match the action group's function schema, and `streamingConfigurations: { streamFinalResponse: true }` set again. **This call's output streams**: the agent's final grounded answer arrives as multiple `chunk` events as Claude Sonnet 4.6 generates the assistant message from the evidence package.
7. The inline agent produces the final grounded assistant message with citations as a token stream. The surface-specific renderer consumes the chunks and progressively writes them to the user — rate-limited `chat.update` for Slack at a 2.5-second cadence (API_12 and Section 08 §6), SSE for the UAT React frontend (API_12 and Section 05 §6).

## Action group shape

One action group at launch with **one function**. Per Section 08 §3, Slack uses the alias-based path as its fixed identity choice; the explicit-scope path is not on the Slack action group. Tentative declaration:

```json
{
  "actionGroupName": "SkywalkerRetrieval",
  "description": "Scoped retrieval against Skywalker. Returns a structured backend result containing either an evidence package with citations or a structured abstain package. The agent must prefer this tool over answering from model knowledge on any question that could have a scoped policy answer.",
  "actionGroupExecutor": { "customControl": "RETURN_CONTROL" },
  "functionSchema": {
    "functions": [
      {
        "name": "skywalker_search_by_alias",
        "description": "Slack path. Use this for any policy question. Skywalker resolves scope through PAPI internally.",
        "parameters": {
          "query_text": {
            "type": "string",
            "description": "The user's current question, rephrased for retrieval if needed.",
            "required": true
          },
          "alias": {
            "type": "string",
            "description": "The Amazon alias of the requesting user.",
            "required": true
          }
        }
      }
    ]
  }
}
```

The function name and parameters mirror the MCP tool name in API_01 with dots replaced by underscores so both the inline-agent schema and the MCP tool registry stay legible against each other.

The UAT inline-agent orchestrator (Section 05) declares a different action group with `skywalker_search_by_explicit_scope` as its single function, because UAT carries the full `(country, level, role)` triple from custom Federate claims and bypasses PAPI. The two action groups are distinct because they correspond to distinct integration identity-carriage models, not because the inline-agent surface itself differs.

## Return-control handling

When the agent emits a return-control event, we receive a structured payload containing the invocation ID, the function name, and the parameters. Our Java code:

1. Maps the function name to the corresponding MCP tool name (`skywalker_search_by_alias` → `skywalker.search.by_alias`).
2. Builds the MCP `tools/call` request with the `arguments` object matching the MCP input schema.
3. Calls the Skywalker MCP server.
4. Serializes the Skywalker response into the shape expected by `returnControlInvocationResults` — this is typically a JSON body representing the function return value. The shape should mirror the MCP tool's `outputSchema` so the agent sees the same structured backend result it would have seen if Bedrock had executed the tool itself.
5. Invokes `InvokeInlineAgent` again with the same `sessionId` and the results populated.

## Session state and multi-turn posture

Section 08 §3 decision five fixes multi-turn handling on the Slack side. The `sessionId` passed to `InvokeInlineAgent` should be derived from the Slack thread context (for channel mentions), the Slack IM channel (for DMs), or the correlation between a slash command and its follow-up thread. `idleSessionTTLInSeconds` should be set so sessions reasonably outlive a user's typing pause but do not accumulate state forever. A starting value of 900 seconds (15 minutes) is a reasonable launch default; revisit against real usage.

## Failure handling

Per Section 08 §7, the Slack application must not let the model answer from its own knowledge when the backend call fails. In inline-agent terms:

- If the Skywalker MCP call fails at the transport level (connection error, timeout), the return-control result passed back to the agent should explicitly indicate the failure. The behavioral prompt instructs the model that this is a system-failure state and the reply should surface an integration problem, not an evidence answer.
- If Skywalker returns an abstain package, that is a normal tool result; the agent is instructed to honor it and produce an abstain-shaped Slack reply per Section 08 §3 decision seven.
- If the model ignores the prompt and tries to answer from memory, that is a prompt-compliance issue handled at calibration (Section 08 §8 calibration surface one), not an API-layer concern.

## Auth and deployment

- The Slack-side service's execution role needs permission to call Bedrock Agents for Amazon Bedrock Runtime (`bedrock:InvokeInlineAgent` or the equivalent in the final API name).
- **Streaming requires `bedrock:InvokeModelWithResponseStream` on the same execution role** — this is required by the public docs whenever `streamingConfigurations.streamFinalResponse` is `true`. It applies to both the Slack application's IAM role and the UAT inline-agent orchestrator's IAM role.
- The Claude Sonnet 4.6 foundation model must be enabled on the AWS account in the region we deploy to.
- The service also needs whatever auth is required by Skywalker's MCP transport (see API_01 outstanding unknowns).

## Sections of the architecture this binds

- Section 08 §2 (normalized turn → inline-agent inputs).
- Section 08 §3 (model naming, MCP-only Skywalker consumption, citation requirement, latency budget).
- Section 08 §6 (end-to-end data flow including tool selection and answer composition).
- API_01 (the MCP tool names and schemas the return-control handler maps to).
- API_12 (streaming the inline agent's final response to the user-facing surface — UAT React via SSE, Slack via rate-limited `chat.update`).

## Outstanding unknowns

- Exact Bedrock model ID for Claude Sonnet 4.6 and its regional availability.
- Whether any Amazon-internal framework provides a ready-made bridge between inline-agent RETURN_CONTROL and an MCP client. If not, the return-control-to-MCP translation is plain Java code we write.
- Inference parameters (`temperature`, `maxTokens`, `topP`) — starting defaults: `temperature: 0`, `maxTokens: 1024`, adjust against real evaluation.
- `idleSessionTTLInSeconds` final value.
- Whether the team wants to enable `enableTrace` at launch for observability (useful during rollout, may be turned down later).
- Whether feature-preview status of inline agents is acceptable for the launch timeline, or whether an eventual GA commitment from Bedrock is required before we commit. Per the [inline-agent user guide](https://docs.aws.amazon.com/bedrock/latest/userguide/inline-agent-invoke.html), inline agents are currently marked preview.
