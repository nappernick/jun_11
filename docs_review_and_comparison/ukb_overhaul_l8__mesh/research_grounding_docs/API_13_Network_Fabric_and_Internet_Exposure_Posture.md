# API Contract 13. Network Fabric and Authorization Posture

This contract pins the network-fabric and authorization posture for Skywalker across all three production client paths. It complements [API_01](API_01_MCP_Server_Protocol.md), [API_02](API_02_Slack_Platform_Surface.md), and [API_08](API_08_QuickSuite_MCP_Gateway_Integration.md). It exists because earlier drafts of the architecture used "internal-only" language that did not accurately describe what hosting Skywalker on Amazon MCP Gateway actually means, and because integration-partner conversations have surfaced friction that comes from misunderstanding what the gateway already provides.

The two-sentence summary: **Skywalker's MCP server is hosted behind Amazon MCP Gateway, whose endpoint is a public DNS name with auth enforced at the gateway. The architecture inherits the gateway's public-endpoint-with-auth posture, which is the BuilderHub Golden Path's recommended shape for cross-fabric Amazon-internal connectivity.**

Sources cited in this document are internal Amazon wikis and BuilderHub. Content from external sources has been rephrased for compliance with internal-source-only restrictions.

## What "internal" actually meant in earlier drafts

Earlier drafts of Sections 01, 02, 05, 08, and 09 used phrases like "internal MCP server" and "Skywalker stays inside Amazon's network" in a way that was not quite right. What those phrases meant correctly: the Coral service backing the gateway is not directly addressable on its own public DNS — clients cannot reach the Coral service except through MCP Gateway's authenticated path. What those phrases did not mean, even though they read that way: that the architecture is unreachable from anywhere outside Amazon's internal network fabric.

The accurate description is: **MCP Gateway's endpoint at `api.mcp.asbx.aws.dev` is a public DNS name reachable from anywhere on the internet, with three inbound auth routes (SigV4, CloudAuth, Federate OAuth). The auth boundary lives on the gateway. The Coral service the gateway forwards to is internal in the sense that it's behind the gateway's auth termination, not in the sense that the architecture lacks a public-DNS surface.** This is by design. It's the BuilderHub Golden Path's recommended posture for cross-fabric service connectivity, captured at [Web service (EC2) — PROD/CORP-to-AWS-VPC connectivity](https://docs.hub.amazon.dev/docs/golden-path/web-service-ec2/recommendation/application-infrastructure/networking/prod-corp-to-aws-vpc-connectivity/) and [CDK how-to-add-service](https://docs.hub.amazon.dev/docs/native-aws/developer-guide/cdk-howto-add-service/): "Expose your Service's DNS endpoint to the world. Use strong authentication/authorization in addition to encryption to secure it. This is the simplest approach as talking between your services is not any different than talking to any other service (internal or external to Amazon)."

In practice this means the architecture is reachable from CORP fabric (all regions), PROD fabric (PDX/IAD/DUB/NRT), and Native AWS (PDX/IAD/DUB/NRT), with no VPN required — the gateway's network-fabric coverage is documented in the [MCP Gateway concepts](https://docs.hub.amazon.dev/docs/mcp-gateway/user-guide/concepts/) page. Any Amazon-internal client that can resolve `api.mcp.asbx.aws.dev` and present valid auth can in principle invoke Skywalker, subject to Bindle authorization on `MCPGateway::{skywalker-server-id}`.

## The actual authorization knob

The architecturally meaningful posture decision is not "internal vs internet-exposed." That decision was made by hosting on MCP Gateway. The architecturally meaningful decision is **which clients we authorize on the gateway's Bindle resource**, which controls which callers' authenticated requests actually reach Skywalker.

Today the Bindle authorizes three production caller principals:

- The Slack application's IAM execution role (SigV4-inbound route, `/mcp/{registry}/{server}`, today; CloudAuth-inbound `/ca/mcp/{registry}/{server}` after the migration in Sections 05 §3 and 08 §3).
- The UAT inline-agent orchestrator's IAM execution role (same route as Slack).
- The QuickSuite Federate Prod Service Profile (Federate-OAuth-inbound route, `/federate/mcp/{registry}/{server}`).

Adding a new caller is a Bindle-permission change plus an auth-shape conversation, not a re-litigation of the architecture. Removing a caller is a Bindle-permission change. The gateway is the seam where authorization decisions live.

What the architecture **does not** authorize, today or as a planned future, is **callers external to Amazon's auth platforms**. CloudAuth credentials are issued only to AAA-modeled Amazon applications. SigV4 credentials require an AWS-Org IAM principal (which a non-Amazon caller cannot have for our accounts). Federate OAuth Service Profiles are issued only to Amazon-internal teams. The gateway's three supported auth shapes therefore form a closed set against Amazon-internal callers; an external partner would have no path to authenticate even though the gateway's DNS is publicly resolvable. **This is the architecture's posture: any Amazon-internal client we choose to authorize, no clients external to Amazon.**

If a future integration conversation pushes toward authorizing external callers, that's a Section 10 re-litigation event, not a calibration-class adjustment. The conversation would have to address auth-shape (no current shape supports external callers without AppSec review), data classification (Skywalker's evidence packages contain employee-scoped travel/events/expense answers, which are typically Restricted+ under [Amazon's data-handling policy](https://policy.a2z.com/docs/97/publication)), and posture review (an external surface for Restricted+ data is a much heavier review than internal-to-internal). Today: out of scope.

## Slack: outbound from Amazon, never inbound to Skywalker

Slack's servers live on the public internet. This causes recurring architectural worry that Skywalker has to do something special to be Slack-reachable. It does not, because the Slack application — not Skywalker — is the bridge. Two patterns from Slack's platform handle the public-internet boundary, both documented in API_02 and the [SAIL Slack Bot HLD](https://w.amazon.com/bin/view/SWA/ShipperAccountManager/SAIL/SlackBot/HLD/) DD-1. Either pattern keeps Skywalker's posture exactly as described above — the Slack app reaches MCP Gateway from inside Amazon's network, and Slack itself never authenticates against MCP Gateway.

**Socket Mode (Pattern A).** The Slack application opens an outbound WebSocket from inside Amazon's network to Slack's WebSocket endpoint. Slack pushes events down that connection; the Slack application replies by calling Slack's Web API (also outbound HTTPS from inside Amazon's network). No inbound traffic from Slack ever crosses Amazon's perimeter. Slack never needs a URL belonging to anything in Amazon. This is the cleaner default posture and the one [Engram's Slack channel reference](https://w.amazon.com/bin/view/IssueManagement/Engram/Internal/Channels/Slack/) uses by default.

**HTTP Events API (Pattern B).** Slack POSTs events to a public URL the team registers with Slack. The Slack application owns one internet-reachable endpoint — typically API Gateway in front of a signature-verifying Lambda — that validates the HMAC on every inbound request and then either self-invokes or queues the work. The signature-verifying Lambda is the only Slack-app-side internet termination; the Slack application's main compute, and everything behind it, stay in their normal Amazon-network posture.

Section 08 §9 currently leaves the Pattern A vs Pattern B choice as an open implementation detail. Either is consistent with this contract. Neither requires changes to Skywalker's MCP server or to the gateway.

## Integration-partner conversation script

Integration partners sometimes ask Skywalker to "be internet-reachable" because PrivateLink or VPC Endpoint connectivity from their environment to Skywalker's VPC is hard to set up. PrivateLink onboarding is documented at ~8 weeks per service in the [Galaxy PrivateLink Onboarding Plan](https://w.amazon.com/bin/view/Galaxy-apps/Galaxy-PrivateLink-Onboarding-Plan/), and the various managed bridges (SuperStar, Allegiance, Tardigrade ProdLink/CorpLink) have caveats around fabric pairs and regions. The friction is real.

The architecture's response is that this is mostly a misunderstanding about MCP Gateway. The four questions that resolve almost every such conversation:

1. **What network fabric is the partner in?** CORP, PROD, Native AWS in the same AWS Org, or Native AWS in a different account? The first three reach MCP Gateway natively. The fourth reaches it through standard cross-account IAM patterns. All four work.
2. **What auth shape do they hold?** Any AWS-Org IAM principal can sign SigV4. Any AAA-modeled application can present CloudAuth. Any team that can register a Federate Service Profile can use Federate OAuth. The gateway supports all three on different inbound routes.
3. **Have they tried the gateway directly?** Almost always, the partner's friction is "we couldn't make PrivateLink work between our VPC and Skywalker's." The answer is "you don't need to — `api.mcp.asbx.aws.dev` is the public-DNS endpoint with the auth boundary, and you reach it the way QuickSuite and the UAT inline-agent orchestrator reach it."
4. **Would Bindle invocation permission be granted to their principal?** If their principal is an Amazon-internal AAA application or IAM role, this is a routine Bindle-permission addition. If their principal cannot be authorized at Bindle (genuinely external to Amazon), see the previous section about the closed set of supported auth shapes.

The architectural posture toward partners: **integration partners reach Skywalker through MCP Gateway's already-public endpoint, not through a separate Skywalker-owned public surface.** Almost every "we need internet reachability" ask is satisfied by pointing the partner at the gateway and granting Bindle permission to their principal. The exceptions involve auth shapes the gateway does not support, at which point the conversation is about gateway-side support rather than Skywalker-side exposure.

## What would change the posture

Two scenarios would force a Section 10 re-litigation:

1. **A future client integration cannot use any of the three supported auth shapes.** This is the gateway's authorization closed set. The right response is conversation with the MCP Gateway team about adding the new auth shape (calibration-active timeline) rather than adding a parallel Skywalker-owned public surface (architecture-class change, AppSec review territory).

2. **The architecture decides to authorize callers external to Amazon's auth platforms.** This is fundamentally different from "Amazon-internal callers reaching us through the gateway's public DNS." External callers have no current auth path; admitting them requires both a new auth shape and a posture review on the data classification of evidence packages. The conversation would touch every section of the architecture and is not in scope today.

Other changes — adding more Amazon-internal callers, switching one production caller's auth shape from SigV4 to CloudAuth, adjusting Bindle permissions — are calibration-class. They do not move the architecture's posture. They only move the authorization list.

## Sections of the architecture this binds

- **Section 01 §1 boundary statement.** "Skywalker is the retrieval backend behind MCP" specifically means behind Amazon MCP Gateway's authenticated termination. The Coral service is internal in the sense of "not directly addressable"; the architecture as a whole is reachable through the gateway's public-DNS endpoint with auth.
- **Section 02 §2 transport.** The three inbound auth routes are gateway-side. Skywalker's MCP server is CloudAuth-protected on the outbound-from-gateway leg. Section 02 §3 should explicitly acknowledge that the gateway endpoint is public and that the architecture inherits public-with-auth posture from the gateway.
- **Section 05 §3 (UAT).** The UAT inline-agent orchestrator reaches MCP Gateway over its public DNS endpoint with the orchestrator's IAM-or-CloudAuth credentials, depending on which auth route is in force per the Section 05 / 08 / 09 migration.
- **Section 08 §3 (Slack).** Slack reaches MCP Gateway the same way; Slack's own deployment topology (Socket Mode vs HTTP Events API) is a separate question that does not affect the gateway-side posture.
- **Section 09 §1, §3 (QuickSuite).** QuickSuite reaches MCP Gateway on its Federate-inbound route. The same posture inheritance applies.
- **Section 10 (decision log).** The "MCP Gateway is the public-with-auth surface; the closed set of supported auth shapes is the authorization boundary; no callers external to Amazon" rule belongs in the fixed-decisions list, with the trigger that would reopen it (a future request to authorize external callers).

## Outstanding unknowns

- **AgentCore Gateway migration.** The [ASBX AIM and MCP Gateway Roadmap](https://w.amazon.com/bin/view/BuilderTools/GenAIDevX/Roadmap/) lists a planned (not in-progress) MCP Gateway deprecation in favor of AgentCore Gateway for Q3/Q4 2026. AgentCore Gateway has its own public-DNS endpoint and its own auth shapes. The "gateway is the public-with-auth surface" rule should carry forward, but the migration has to confirm it on the new substrate rather than assume it.
- **Data-classification reviews.** Skywalker's evidence packages contain employee-scoped travel/events/expense answers, which are typically Restricted+ under [Amazon's data-handling policy](https://policy.a2z.com/docs/97/publication). The current "behind MCP Gateway with CloudAuth on the backend" posture is consistent with Restricted+ handling for Amazon-internal callers, but specific reviews could add requirements (audit-log retention, additional encryption-in-transit surfaces, monitoring) that should land here when they surface.
- **The closed set of supported auth shapes.** SigV4, CloudAuth, and Federate OAuth cover every production client today. If MCP Gateway adds a new inbound auth shape (Midway-direct on the remote-client path is "in progress" per the gateway concepts page), the architecture should acknowledge the addition and confirm that the closed-set posture against external callers still holds. Adding a new auth shape is itself a calibration-class change; using it to admit external callers would be re-litigation.
