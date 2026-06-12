# API Contract 02. Slack Platform Surface

Covers the Slack API calls and Slack-platform conventions the Skywalker Slack application has to implement. Pairs with Section 08.

## What the architecture has already fixed

- Three supported Slack surfaces from day one: slash commands, direct messages, channel mentions. Normalized into one internal turn object early. (Section 08 §3)
- The Slack application consumes Skywalker only through MCP. Identity default is Amazon alias. (Section 08 §3)
- End-to-end p95 under 4 seconds from user message receipt to final Slack reply. Skywalker itself is budgeted at 200–400 ms, leaving ~3.5 s for Slack intake, model reasoning, MCP tool call, model generation, and Slack transport. (Section 08 §3)
- Citations are a non-negotiable part of the user-facing message on any answerable result. (Section 08 §3)

## What Slack's platform gives us (baseline facts)

Slack apps receive events from three subscription surfaces that map to our three supported UX entry points:

- **Slash commands** — configured per command at the app level; Slack POSTs a payload to the command's Request URL when a user invokes `/command`. Docs: [Slash commands (Bolt for Java)](https://slack.dev/java-slack-sdk/guides/slash-commands).
- **Events API** — covers `app_mention` for channel mentions and `message.im` for DMs. Docs: [Events API (Bolt for Java)](https://tools.slack.dev/java-slack-sdk/guides/events-api/).
- **URL verification** — the initial `url_verification` event Slack sends when the Request URL is configured; the app must echo the `challenge` field. Docs: [url_verification event](https://docs.slack.dev/reference/events/url_verification).

Every incoming HTTP request from Slack must be verified against the app's signing secret. Slack sends an `X-Slack-Signature` header computed by HMAC-SHA256 over the request body combined with a timestamp header; the app recomputes the signature using its signing secret and rejects non-matching or stale requests. Docs: [Verifying requests from Slack](https://api.slack.com/authentication/verifying-requests-from-slack). The raw request body must be read before any JSON deserialization; parsing first will break verification.

Outbound messages are sent via `chat.postMessage` on the Slack Web API, or via the `response_url` included in slash-command payloads for quick turn-around replies. Threaded replies use `thread_ts`.

Content from external sources has been rephrased for compliance with licensing restrictions.

## What we still need to decide and write into Section 08

1. **Connection mode.** Bolt for Java supports two deployment shapes:
   - **HTTP mode** — Slack POSTs events to a public HTTPS endpoint we host. Requires signing-secret verification on every request.
   - **Socket Mode** — the app opens an outbound WebSocket to Slack and receives events over that socket; no public inbound endpoint required. Uses an app-level token rather than signing-secret verification.

   Choose based on the deployment environment constraints. Socket Mode removes the public-endpoint requirement, which matters for an internal-only Amazon service.

2. **Secrets we need to provision.**
   - `SLACK_BOT_TOKEN` (`xoxb-*`) — used by the Web API client for `chat.postMessage` and profile lookups.
   - `SLACK_SIGNING_SECRET` — used to verify inbound HTTP requests (HTTP mode only).
   - `SLACK_APP_TOKEN` (`xapp-*` with `connections:write` scope) — used by Socket Mode only.

3. **OAuth scopes needed on the bot.**
   - `commands` — to receive slash command invocations.
   - `app_mentions:read` — to receive `app_mention` events.
   - `im:history`, `im:read`, `im:write` — to receive and reply in DMs.
   - `chat:write` — to post messages.
   - `users:read`, `users:read.email` — to resolve Slack user → Amazon alias (see open item below).

4. **Event subscription list.**
   - `app_mention`
   - `message.im`
   - Any channel-mention variants required by how the team deploys the bot.

5. **Slash command definitions.** One command at launch (tentative: `/skywalker`) with a placeholder Request URL or Socket Mode handler.

6. **Reply transports for the three surfaces.**
   - Slash command: POST to `response_url` within 3 seconds for the first ack; use `response_type: "ephemeral"` by default, `"in_channel"` when the user explicitly asks for a public answer. Use `chat.postMessage` for follow-ups beyond 30 minutes or after the `response_url` TTL.
   - DM: `chat.postMessage` with `channel` set to the IM channel ID.
   - Channel mention: `chat.postMessage` with `thread_ts` set to the triggering message's `ts` so replies thread cleanly.

7. **Slack user → Amazon alias resolution.** The normalized turn object in Section 08 §2 requires an Amazon alias, but Slack events give us a Slack user ID. Two realistic paths:
   - Use `users.profile.get` or `users.info` to pull the user's email, then derive the alias from the Amazon email domain.
   - Call an Amazon-internal directory service keyed by Slack user ID or email.

   This is the one piece of the Slack surface that is likely **not** public. Flagged as an outstanding dependency below.

8. **Citation rendering for each surface.** Per Section 08 §3 decision nine, citations must trace back to the evidence package. Surface-specific rendering (inline links in Block Kit `section` blocks, a `context` block footer with source links, or an attachment) remains a calibration decision but needs a concrete default. Recommended default: a Block Kit message with a `section` block for the answer body and a `context` block listing source titles + URLs.

9. **Latency posture.** The 4-second p95 budget means slash-command flows should ack immediately (within 3 s of receiving the invocation) with an interim "working on it" response and then edit/update or post the final reply when the Skywalker call returns.

## Sections of the architecture this binds

- Section 08 §2 (normalized turn contract, identity handoff).
- Section 08 §3 (supported surfaces, citation requirement, latency target).
- Section 08 §4 (Bolt for Java framework choice).
- Section 08 §6 (end-to-end data flow, including response delivery).

## Outstanding unknowns to resolve before coding

- Final connection mode (HTTP vs. Socket Mode).
- The Slack-user-to-Amazon-alias resolution path. If it's a non-public internal service, a separate PDF is needed. If it's email-based using Slack's `users.profile.get`, no new doc is required but the domain-to-alias rule should be written down.
- Whether the app needs to be distributable across workspaces or installed once into a single internal workspace (affects whether OAuth installation flow is needed at all).
- Final command name and whether there are multiple commands at launch.
