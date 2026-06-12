# API Contract 03. Bolt for Java Framework

Covers the Slack app framework Section 08 §4 names as the leading candidate. Narrower than API_02 (which is about the Slack platform itself) — this is about the library and its runtime shape.

## What the architecture has already fixed

- Section 08 §4: the architectural fit points toward a Java-hosted Slack integration. The final choice is between Bolt for Java and any required Amazon-internal Slack framework. The library name is not load-bearing; the Java environment is.

## What Bolt for Java gives us (baseline facts)

Bolt for Java is Slack's official JVM framework for building Slack apps. Dependency coordinates and patterns come from the [Bolt for Java getting-started guide](https://slack.dev/java-slack-sdk/guides/getting-started-with-bolt/).

- Core dependency: `com.slack.api:bolt:<version>` (1.48.0 at time of writing).
- For HTTP-mode deployments on servlet containers (Spring Boot, Quarkus with Undertow, etc.): `com.slack.api:bolt-servlet`.
- For Socket Mode deployments: `com.slack.api:bolt-socket-mode` plus a WebSocket client implementation (`javax.websocket-api` + `tyrus-standalone-client`, or `Java-WebSocket`).
- Minimum JVM: Java 8.

The framework is organized around an `App` instance to which you attach handlers for incoming event types. Handler registration follows a builder pattern; commands are registered with `app.command("/name", (req, ctx) -> { ... })`, events with `app.event(AppMentionEvent.class, ...)`, and message events with `app.message(...)`.

Request signing verification is handled automatically by the framework when the signing secret is provided to the `AppConfig`. Socket Mode uses the app token instead.

Content from external sources has been rephrased for compliance with licensing restrictions.

## What we still need to decide and write into Section 08

1. **Bolt version pin.** Pick an exact version and treat upgrades as deliberate.
2. **Hosting topology.** Two realistic shapes:
   - Bolt + `bolt-servlet` inside a Spring Boot or Quarkus service fronted by an HTTPS load balancer. Requires publishing the Request URL to Slack.
   - Bolt + `bolt-socket-mode` inside a plain Java service with no inbound HTTP surface. Simpler in an internal environment; eliminates the HTTPS-ingress design.
3. **Configuration inputs.** The `AppConfig` needs:
   - `singleTeamBotToken` (if single-workspace install) or the OAuth installer wiring (if distributable).
   - `signingSecret` (HTTP mode).
   - App token (Socket Mode).
4. **Handler skeleton.** The Slack subsystem needs three concrete handlers corresponding to the three supported surfaces from Section 08 §3:
   - A slash command handler for the launch command.
   - An `AppMentionEvent` handler for channel mentions.
   - A `MessageEvent` handler for direct messages (filtered to IM channels and non-bot users).
5. **Bridge to the Skywalker MCP client.** Each handler builds the normalized turn object (raw text, Slack surface type, Slack user ID, Amazon alias after resolution, channel/thread return target, prior-turn context) and hands it to the Slack-side model layer. The model layer is responsible for calling the MCP tool and composing the reply — the Bolt handler itself should not be doing MCP retrieval work.
6. **Acknowledgement discipline.** Slack requires the 3-second acknowledgement on commands and interactive payloads. Bolt handles this automatically for `ack()` calls but the retrieval work must happen after ack, either on a background executor or via `response_url`/`chat.postMessage` edits.

## Sections of the architecture this binds

- Section 08 §4 (framework choice and Java-heavy deployment assumption).
- Section 08 §6 (the "event intake" and "response delivery" steps live inside Bolt handlers).

## Outstanding unknowns to resolve before coding

- Whether the team must use an Amazon-internal Slack framework instead of Bolt. If such a framework exists and is required, a separate PDF is needed.
- Final hosting topology (HTTP vs. Socket Mode) — same open item as API_02.
- Thread-pool sizing for the background executor that runs retrieval after `ack()`. This is a latency concern given the 4-second p95 budget.
