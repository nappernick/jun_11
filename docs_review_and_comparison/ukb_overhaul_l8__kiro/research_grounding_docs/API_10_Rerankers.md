# API Contract 10. Rerankers (Evidence + Gate)

Skywalker uses two rerankers. They have different purposes, different hosting models, different latency and cost profiles, and should never be conflated.

- **Evidence reranker: Cohere Rerank 4 Pro, self-hosted on SageMaker (`ml.p5.4xlarge`).** Scores the 20-candidate pool that comes out of retrieval against the user query. Determines the final answer shortlist and drives the evidence-side abstain decision. Called once per request.
- **Gate reranker: Cohere Rerank 3.5, via Bedrock Agent Runtime's `Rerank` API.** Acts as the second stage of the hybrid routing gate. Only invoked when cosine similarity against the static variant set (API_11) lands in the ambiguity band. Roughly 20% of requests at launch estimates.

This document covers both.

## Shared context

Both rerankers are Cohere cross-encoders and both return a `relevance_score` in `[0, 1]`. Higher is more relevant. Neither score is a calibrated probability; both are relative-relevance judgments useful as threshold signals. The architecture treats them the same way in that respect, but the service boundaries, costs, and integration contracts are different.

Content from external sources has been rephrased for compliance with licensing restrictions.

---

# Part A — Evidence Reranker: Cohere Rerank 4 Pro on SageMaker

Pairs with Section 07 (answerability and abstain), Section 04 (candidate convergence and handoff), Section 03 (children produced by ingestion).

## What the architecture has already fixed

- Section 07 §3 decision one: one common reranking surface for both retrieval arms.
- Section 07 §3 decision four: source-arm identity is preserved in metadata and excluded from the reranker text surface.
- Section 07 §3 decision five: reranker input includes the candidate's meaningful evidence payload and may include title when useful.
- Section 07 §3 decision ten: the common scoring surface is Cohere Rerank 4 Pro, self-hosted on SageMaker.
- Section 07 §3 decision eleven: the abstain rule is a two-branch composite — `NO_USABLE_EVIDENCE` (empty shortlist after rerank) and `EVIDENCE_TOO_WEAK_AFTER_RERANK` (top reranked `relevance_score` below the absolute floor).
- Section 07 §7: reranker failure falls back to a retrieval-order package flagged with `reranker_state: RERANKER_FAILURE_FALLBACK`.

## Deployment

- **Marketplace product:** Cohere Rerank v4.0 Pro — AWS Marketplace listing [prodview-du2svpomxs5vw](https://aws.amazon.com/marketplace/pp/prodview-du2svpomxs5vw), product ID `prod-b3hko54dqpujq`. Subscribe, accept EULA, then create a SageMaker endpoint from the Model Package ARN.
- **Model name:** `rerank-v4.0-pro`.
- **IAM for setup:** `aws-marketplace:Subscribe`, `AmazonSageMakerFullAccess` on the role performing the deployment.
- **IAM at runtime:** `sagemaker:InvokeEndpoint` scoped to the endpoint ARN.
- **AMI requirement:** `InferenceAmiVersion=al2-ami-sagemaker-inference-gpu-2`.
- **VPC + PrivateLink:** via SageMaker's standard `VpcConfig`. Endpoint runs in our VPC with no egress to Cohere's backend.
- **Region:** `us-east-1` at launch.

## Instance choice

`ml.p5.4xlarge` (1× H100, 80 GB). 2-instance always-on fleet for production, 1-instance fleet for beta. Auto-scaling is not used; SageMaker cold start (~3–5 minutes) is longer than any burst window.

- Latency: ~180–300 ms p50 at 20K-token payloads. Timeout budget 350 ms.
- Sustained throughput per instance: ~12–20 QPS.
- Cost (us-east-1, on-demand): ≈ $9,486/month/instance including the flat $3.50/host-hour Cohere Marketplace fee. HA production fleet ≈ $18,972/month on-demand.

## SageMaker hosting details

SageMaker real-time inference hosts Rerank 4 Pro as the evidence-scoring surface for Skywalker. The hosting model is the same three-resource pattern SageMaker uses for every real-time endpoint — `Model`, `EndpointConfig`, `Endpoint` — built from a Marketplace Model Package ARN rather than a custom container image. Invocation at runtime goes through the separate SageMaker Runtime service (`runtime.sagemaker.<region>.amazonaws.com`), not the control-plane SageMaker service. These two are distinct AWS services with distinct SDK clients, distinct IAM actions, and distinct endpoints; the query service only ever talks to the runtime service. All control-plane work (`CreateModel`, `CreateEndpointConfig`, `CreateEndpoint`, `UpdateEndpoint`, `DeleteEndpoint`) is done once at deployment time by the deploy role and is not exercised on the hot path.

### Subscription and Model Package ARN

Before any SageMaker resource can be created, the AWS account hosting the endpoint must subscribe to the Cohere Rerank v4.0 Pro Marketplace listing. Subscription is a one-time account-level action: navigate to the listing, click **Continue to Subscribe**, accept the EULA, and confirm the $3.50/host-hour software fee (billed on top of the `ml.p5.4xlarge` instance hour rate). On acceptance, AWS Marketplace provisions a region-specific Model Package ARN into the subscribed account. The ARN format is:

```
arn:aws:sagemaker:us-east-1:<aws-marketplace-vendor-account>:model-package/cohere-rerank-v4-0-pro-<version-suffix>
```

The concrete ARN surfaces in the Marketplace console after subscription and is recorded in SSM at `/skywalker/runtime/rerank/evidence_model_package_arn` so the deploy job can read it without being hand-edited. The IAM role performing deployment needs `aws-marketplace:Subscribe`, `aws-marketplace:ViewSubscriptions`, and `AmazonSageMakerFullAccess`; the subscription itself is tied to the account, not the role, so any deployment-capable role in the account can use it after the one-time subscribe.

### Three-step deployment with the AWS SDK for Java v2

The control-plane client is `software.amazon.awssdk:sagemaker`. Deployment runs as three sequential API calls: `CreateModel`, `CreateEndpointConfig`, `CreateEndpoint`. Each produces a named resource that the next references. Concretely:

**1. `CreateModel`** binds a logical model name to the Marketplace Model Package ARN and to the SageMaker execution role the endpoint will run under. It also attaches the VPC configuration and enables network isolation so the container has no outbound network access.

```java
SageMakerClient sm = SageMakerClient.builder()
    .region(Region.US_EAST_1)
    .build();

sm.createModel(CreateModelRequest.builder()
    .modelName("skywalker-rerank-v4-pro-model")
    .executionRoleArn("arn:aws:iam::<account>:role/SkywalkerRerankExecutionRole")
    .enableNetworkIsolation(true)
    .primaryContainer(ContainerDefinition.builder()
        .modelPackageName(modelPackageArn) // read from SSM
        .build())
    .vpcConfig(VpcConfig.builder()
        .subnets("subnet-aaaaaaaa", "subnet-bbbbbbbb", "subnet-cccccccc")
        .securityGroupIds("sg-xxxxxxxx")
        .build())
    .build());
```

The execution role (`SkywalkerRerankExecutionRole`) is trusted by `sagemaker.amazonaws.com` and has `AmazonSageMakerFullAccess` plus permissions to pull the Marketplace container and read any S3 model artifacts the Model Package points at. With `enableNetworkIsolation(true)`, the container cannot make outbound calls — acceptable for Rerank 4 Pro because it ships self-contained inference code and does not phone home. The `VpcConfig` places ENIs in the same VPC and subnets as the query service so invocations stay on the private network; security-group rules permit only inbound `tcp/443` from the query service's security group.

**2. `CreateEndpointConfig`** describes how the model is hosted — instance type, count, AMI, health-check and model-download timeouts, and the production variant structure that SageMaker uses internally to route traffic. There is one production variant per endpoint at launch; the variant structure would only matter if we were doing A/B traffic splits between model versions.

```java
sm.createEndpointConfig(CreateEndpointConfigRequest.builder()
    .endpointConfigName("skywalker-rerank-v4-pro-config-v1")
    .productionVariants(ProductionVariant.builder()
        .variantName("primary")
        .modelName("skywalker-rerank-v4-pro-model")
        .initialInstanceCount(2)                   // 1 for beta
        .instanceType(ProductionVariantInstanceType.ML_P5_4_XLARGE)
        .initialVariantWeight(1.0f)
        .inferenceAmiVersion(ProductionVariantInferenceAmiVersion.AL2_AMI_SAGEMAKER_INFERENCE_GPU_2)
        .modelDataDownloadTimeoutInSeconds(1200)   // Rerank 4 Pro weights are large
        .containerStartupHealthCheckTimeoutInSeconds(600)
        .build())
    .build());
```

The two timeout values matter because Rerank 4 Pro is a large model — model data can take several minutes to pull onto a fresh instance, and the container must pass SageMaker's `/ping` health check before traffic is routed to it. `modelDataDownloadTimeoutInSeconds` covers weight download; `containerStartupHealthCheckTimeoutInSeconds` covers the time from container start to first healthy ping response. Setting both to generous values (20 minutes and 10 minutes respectively) accommodates first-provision and instance-replacement cases without SageMaker giving up and marking the instance failed. The `InferenceAmiVersion` of `al2-ami-sagemaker-inference-gpu-2` is the Cohere-required NVIDIA-capable AMI; any other AMI will fail the Model Package's compatibility check.

**3. `CreateEndpoint`** binds the named endpoint to the config and triggers provisioning. The endpoint name is the handle that runtime invocations use; it is what `sagemaker:InvokeEndpoint` scopes against.

```java
sm.createEndpoint(CreateEndpointRequest.builder()
    .endpointName("skywalker-rerank-prod-a")
    .endpointConfigName("skywalker-rerank-v4-pro-config-v1")
    .build());
```

Endpoint creation is asynchronous. SageMaker transitions the endpoint through `Creating` → `InService` over roughly 5–15 minutes depending on image pull, weight download, and GPU health checks. The deploy script polls with `DescribeEndpoint` until `EndpointStatus == InService`. Production uses two endpoints (`skywalker-rerank-prod-a` and `skywalker-rerank-prod-b`) fronted by a client-side round-robin in the query service rather than a single multi-instance endpoint, because two distinct endpoints give independent failure domains and independent rolling updates. Beta uses one endpoint (`skywalker-rerank-beta`). The pair of production endpoints is the HA posture referenced elsewhere in this document.

### Runtime invocation from Java

Invocation uses a different client — `software.amazon.awssdk:sagemakerruntime` — against the SageMaker Runtime service. The query service holds one long-lived `SageMakerRuntimeClient` per JVM with a connection pool sized to the sustained QPS target. The request body is the same Cohere JSON shape shown above; SageMaker Runtime forwards the bytes straight to the container's `/invocations` endpoint without inspecting them.

```java
SageMakerRuntimeClient rt = SageMakerRuntimeClient.builder()
    .region(Region.US_EAST_1)
    .overrideConfiguration(ClientOverrideConfiguration.builder()
        .apiCallTimeout(Duration.ofMillis(350))            // overall call timeout
        .apiCallAttemptTimeout(Duration.ofMillis(350))
        .retryPolicy(RetryPolicy.builder()
            .numRetries(1)                                  // one retry on connection-level failure
            .backoffStrategy(FixedDelayBackoffStrategy.create(Duration.ofMillis(50)))
            .build())
        .build())
    .build();

InvokeEndpointResponse resp = rt.invokeEndpoint(InvokeEndpointRequest.builder()
    .endpointName(chosenEndpointName)   // round-robin between prod-a and prod-b
    .contentType("application/json")
    .accept("application/json")
    .body(SdkBytes.fromUtf8String(rerankRequestJson))
    .build());

String rerankResponseJson = resp.body().asUtf8String();
```

The `apiCallTimeout` and `apiCallAttemptTimeout` together produce the 350 ms budget fixed in Section 07. `numRetries(1)` with a 50 ms fixed backoff lets the client absorb a single transient TCP reset without breaching the budget; longer retry policies would breach it. On any failure that breaks the 350 ms budget or exhausts the retry, the caller falls through to the reranker-failure fallback path rather than attempting further retries.

### IAM at runtime

The query service's IAM execution role carries one scoped permission for reranker invocation:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "sagemaker:InvokeEndpoint",
      "Resource": [
        "arn:aws:sagemaker:us-east-1:<account>:endpoint/skywalker-rerank-prod-a",
        "arn:aws:sagemaker:us-east-1:<account>:endpoint/skywalker-rerank-prod-b",
        "arn:aws:sagemaker:us-east-1:<account>:endpoint/skywalker-rerank-beta"
      ]
    }
  ]
}
```

`InvokeEndpoint` is authenticated with SigV4 using the role's temporary credentials. No separate API key or service-account credential is involved; SageMaker Runtime authorization is pure IAM.

### VPC wiring and network isolation

The endpoint's ENIs live in the same VPC as the query service. The security group attached to the endpoint (`sg-xxxxxxxx` above) allows inbound `tcp/443` only from the query service's security group; outbound is restricted because `enableNetworkIsolation(true)` is set at the model level. The query service reaches the endpoint through a SageMaker Runtime VPC interface endpoint (`com.amazonaws.us-east-1.sagemaker.runtime`) so traffic never traverses the public internet. The VPC endpoint policy restricts which endpoint ARNs can be invoked to the three endpoint ARNs listed above, providing a defense-in-depth control on top of the IAM policy on the query service role.

### Endpoint lifecycle and updates

Updating a deployed endpoint (new instance type, new model version, new config) uses `UpdateEndpoint` against a new `EndpointConfig` rather than deleting and recreating the endpoint. SageMaker performs a blue-green rollout: it provisions new instances with the new config, health-checks them, shifts traffic, and terminates the old instances. The endpoint stays `InService` throughout and `InvokeEndpoint` keeps succeeding. A failed health check during rollout triggers automatic rollback to the previous config. To roll back manually after a rollout completes, `UpdateEndpoint` points the endpoint back at the previous `EndpointConfig` name. Endpoint configs are cheap and immutable — we keep a history (`skywalker-rerank-v4-pro-config-v1`, `-v2`, `-v3`…) rather than mutating them, so rollback is always one API call.

Deletion is a two-step sequence: `DeleteEndpoint` removes the live resource and stops billing for instance hours; `DeleteEndpointConfig` and `DeleteModel` remove the dormant resources. The Model Package itself is not deleted — it is a Marketplace resource owned by the vendor.

### Monitoring

SageMaker emits CloudWatch metrics automatically under the `AWS/SageMaker` namespace, dimensioned by `EndpointName` and `VariantName`:

- `Invocations`, `Invocation4XXErrors`, `Invocation5XXErrors`, `InvocationsPerInstance` — call volume and error rates.
- `ModelLatency` — time spent inside the container on the prediction itself (excludes network).
- `OverheadLatency` — SageMaker-side overhead (authentication, routing, response framing).
- `CPUUtilization`, `GPUUtilization`, `GPUMemoryUtilization`, `MemoryUtilization`, `DiskUtilization` — per-instance resource health.

Alarms track three things: `Invocation5XXErrors > 0` for 3 consecutive minutes (endpoint-side failure), `ModelLatency` p95 > 300 ms for 5 consecutive minutes (latency drift feeding into the 350 ms budget), and `GPUUtilization` sustained > 85% (capacity pressure ahead of saturation). All three alarm thresholds are environment-parameterized and live in the same CloudWatch alarms stack as the rest of the query service.

### Idempotency and deployment safety

`CreateModel`, `CreateEndpointConfig`, and `CreateEndpoint` are not natively idempotent — repeat calls with the same name return a conflict. The deploy job treats this as a feature rather than a bug: names include a content-derived suffix (`…-v1`, `…-v2`) so a re-run either succeeds with a fresh name or detects that the intended config already exists and skips the redundant call. `CreateEndpoint` is the exception — the endpoint name is fixed (`skywalker-rerank-prod-a`), so re-runs call `DescribeEndpoint` first, and if the endpoint exists with the same config, exit cleanly; otherwise they call `UpdateEndpoint` to roll the endpoint to the new config.

## Request and response shape

```json
POST /invocations
Content-Type: application/json

{
  "model": "rerank-v4.0-pro",
  "query": "<query text>",
  "documents": ["<candidate 1 text>", "<candidate 2 text>", ...],
  "top_n": 5,
  "max_tokens_per_doc": 4096,
  "api_version": 2
}
```

Response:

```json
{
  "id": "...",
  "results": [
    { "index": 3, "relevance_score": 0.8234 },
    { "index": 7, "relevance_score": 0.7116 }
  ],
  "meta": { ... }
}
```

`index` refers back to the position in the input `documents` array; use it to recover the original child chunk.

### Document text rendering

Per Section 07 §3 decisions four and five: source-arm identity is **not** in the reranker text. Per-document render:

```
<title>

<text>
```

No arm prefix, no rank, no source URL. Arm origin and candidate metadata stay in the surrounding envelope. For UKB candidates without a usable title, the title line is omitted.

### Context window budget

32,768 tokens per (query, document) pair. Skywalker's 1000-token child ceiling (Section 03 D-26) means 20 candidates fit comfortably with room for the query. No max-over-chunks behavior is needed for FAQ-origin candidates. UKB passages are accommodated within the window for essentially any realistic size. `max_tokens_per_doc` stays at the 4096 default.

## Abstain thresholds — control plane

Two branches, both reading live values from SSM Parameter Store (API_07):

- **`NO_USABLE_EVIDENCE`** — `response.results` is empty.
- **`EVIDENCE_TOO_WEAK_AFTER_RERANK`** — `response.results[0].relevance_score < /skywalker/runtime/abstain/floor` (launch default 0.30).

The floor is a **control-plane value** — operators adjust it in SSM against judged-traffic calibration without a redeploy. See Section 10 C-02, C-12, and D-31.

## Failure handling

- **Transport failure** (endpoint unreachable, 5xx, timeout): return a reranker-failure fallback package with `reranker_state: RERANKER_FAILURE_FALLBACK`. Shortlist is the pre-rerank candidate order, truncated.
- **Throttling on an individual endpoint:** the 2-instance HA fleet absorbs single-instance failures. A single retry with short backoff (≤50 ms) on connection-level failures; longer retries violate the latency budget.
- **Endpoint capacity exhaustion** (both instances saturated beyond sustained QPS): treat as transport failure and flip to reranker-failure fallback.

## Configuration

- **Timeout:** 350 ms — read from SSM at `/skywalker/runtime/rerank/evidence_timeout_ms` (control-plane).
- **Endpoint names:** `skywalker-rerank-prod-a`, `skywalker-rerank-prod-b`, `skywalker-rerank-beta`.
- **SDK:** AWS SDK for Java v2, `software.amazon.awssdk:sagemakerruntime`, `InvokeEndpointRequest` / `InvokeEndpointResponse`.
- **Auth:** SigV4 via the query service's IAM execution role.

---

# Part B — Gate Reranker: Cohere Rerank 3.5 via Bedrock

Pairs with Section 04 (hybrid routing gate), API_11 (in-memory variant embeddings for the cosine first pass).

## Purpose

The routing gate decides whether a request gets FAQ-only retrieval or fans out to the dual-arm path. A bi-encoder cosine comparison against the static variant set is the cheap first pass (API_11). When cosine's top score lands in the ambiguity band, Skywalker calls Cohere Rerank 3.5 on Bedrock to get a cross-encoder judgment, which is materially more accurate than cosine on paraphrase, negation, entity substitution, and compound queries.

This reranker is **not** the evidence reranker. It runs on ~20% of requests (the cosine-ambiguous ones), against 50 very short documents (the variants), and produces a single "is this a Top 50 question" signal.

## The three gate thresholds — control plane

The gate reads three values from SSM Parameter Store on every request (via the control-plane refresh loop, see API_07):

```
/skywalker/runtime/gate/cosine_high_threshold    # default 0.80
/skywalker/runtime/gate/cosine_low_threshold     # default 0.30
/skywalker/runtime/gate/rerank_floor             # default 0.50
```

Behavior:

- `top_cosine > cosine_high_threshold` → FAQ-only. Skip Rerank 3.5.
- `top_cosine < cosine_low_threshold` → dual-arm. Skip Rerank 3.5.
- Otherwise (ambiguity band) → call Rerank 3.5 against the variants. If top `relevance_score > rerank_floor` → FAQ-only. Otherwise → dual-arm.

**All three values are tunable without a redeploy.** They are explicitly expected to move against real traffic. Any documentation or code touching these values should be clear that they are control-plane knobs.

## Bedrock invocation

- **Service:** Bedrock Agent Runtime `Rerank` operation.
- **Model ID:** `cohere.rerank-v3-5:0`.
- **SDK:** AWS SDK for Java v2, `software.amazon.awssdk:bedrockagentruntime`, `RerankRequest` / `RerankResponse`.
- **Auth:** SigV4 via the query service's IAM execution role.

Request shape:

```java
RerankRequest.builder()
    .queries(List.of(
        RerankQuery.builder()
            .textQuery(RerankTextDocument.builder().text(queryText).build())
            .type(QueryType.TEXT)
            .build()
    ))
    .sources(variantTexts.stream().map(t -> RerankSource.builder()
        .inlineDocumentSource(InlineDocumentSource.builder()
            .type(InlineDocumentSourceType.TEXT)
            .textDocument(RerankTextDocument.builder().text(t).build())
            .build())
        .type(RerankDocumentType.INLINE)
        .build()).toList())
    .rerankingConfiguration(RerankingConfiguration.builder()
        .type(RerankingConfigurationType.BEDROCK_RERANKING_MODEL)
        .bedrockRerankingConfiguration(BedrockRerankingConfiguration.builder()
            .modelConfiguration(BedrockRerankingModelConfiguration.builder()
                .modelArn("arn:aws:bedrock:us-east-1::foundation-model/cohere.rerank-v3-5:0")
                .build())
            .numberOfResults(1)
            .build())
        .build())
    .build();
```

We only need the top result; `numberOfResults(1)` minimizes response payload.

## Latency and timeout

- Bedrock Rerank 3.5 typical latency: ~200–350 ms p95 for 50 very short variant documents.
- **Gate timeout:** 300 ms — read from SSM at `/skywalker/runtime/gate/rerank_timeout_ms` (control-plane).
- On timeout, the gate falls through to dual-arm. **A gate-reranker timeout is not an error**; it is a widening of the route. Skywalker logs a metric and proceeds.

Because the gate reranker runs on only ~20% of queries and only in the cosine-ambiguity band, its contribution to p95 latency across the whole pipeline is bounded. Requests that skip it (the confident-cosine cases) see no gate-reranker latency at all.

## Failure handling

- **Transport failure:** fall through to dual-arm. Not an error; log as a widening event.
- **Throttling:** Bedrock has shared regional throughput; under sustained throttling, fall through to dual-arm and alarm.
- **Bad model response / parse failure:** fall through to dual-arm.

The gate is designed so that any reranker-side failure mode degrades to dual-arm, which is the safe direction. The worst outcome of a gate-reranker failure is paying UKB latency on a request that could have been FAQ-only, which is an acceptable cost.

## Cost

Pay-per-call through Bedrock. At Skywalker's projected volume and the ~20% invocation rate, gate-reranker cost is expected to be a rounding error relative to the evidence reranker's fixed SageMaker fleet cost. No capacity planning needed.

## Configuration

- **Region:** `us-east-1`.
- **Model ID:** `cohere.rerank-v3-5:0`.
- **No VPC endpoint required at launch** (Bedrock API is accessed over the standard AWS service endpoint); revisit if Amazon-internal network policy requires PrivateLink.

---

# What binds to both parts

- Section 04 (hybrid routing gate and the decision to use a two-stage gate).
- Section 07 (evidence answerability and abstain).
- Section 10 D-25 (evidence reranker), D-30 (hybrid routing gate), D-31 (control-plane values in SSM).
- API_07 (SSM, home of all tunable thresholds and timeouts for both rerankers).
- API_11 (static variant set, feeds the cosine first pass).

## Outstanding unknowns

- Final us-east-1 Model Package ARN for Rerank 4 Pro (surfaces after Marketplace subscription).
- Measured p95 latency for both rerankers at production volume with Skywalker's real payloads.
- Whether Amazon-internal network policy requires a VPC endpoint for Bedrock Agent Runtime.
- Whether a Private Offer with Cohere lowers the $3.50/host-hour SageMaker Marketplace fee.
- Ambiguity-band calibration: the `cosine_low`, `cosine_high`, and `rerank_floor` defaults (0.30, 0.80, 0.50) are starting points; real traffic moves them.
