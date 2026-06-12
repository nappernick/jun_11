# Plan: Build the Skywalker query pipeline up to and through the reranker

Status: READY — all Open Questions resolved 2026-06-03; routing gate (T4) dropped (no variant set this pass); execution can begin
Date: 2026-06-03
Author: Kiro + nmatnich

## Goal

Build the missing runtime query pipeline in `ATESkywalkerQuery` so that a scoped
request flows: **MCP entry → scope → query embedding → routing gate → hybrid
retrieval from AOSS → candidate normalization → evidence rerank**, ending with a
reranked candidate list. Stop at the reranker output. Do not build anything
downstream of the reranker (abstain composite, linked-item/sibling expansion,
final citation envelope assembly, Slack/QuickSuite/agent, MCP Gateway onboarding).

Model-hosting split (RESOLVED 2026-06-03):
- **Query embedding → Amazon Bedrock Cohere Embed v4** (`cohere.embed-v4:0`).
  This is the same model that built the index, so the query vector is in the
  identical vector space — no parity risk. The earlier "no Bedrock" assumption
  was retracted by the user.
- **SageMaker hosts only the evidence reranker.** Model is **Cohere Rerank v4.0
  Pro** (AWS Marketplace `prodview-du2svpomxs5vw`, product `prod-b3hko54dqpujq`,
  v1.1.0; flat **$3.50/instance-hour** software fee on every instance type).
  **Starting instance: `ml.p5.4xlarge` (H100).** A later perf test compares H100
  against a single high-end GPU (e.g. `ml.g6e.xlarge`) on **latency** at the
  production payload shape (20 candidates × ~1000 tokens, `top_n` per
  shortlist), measuring p50/p95 `ModelLatency`. The decision rule: pick the
  cheapest instance whose p95 keeps the end-to-end reranker call inside the
  architecture's p95 250–450 ms retrieval budget (the reranker is the dominant
  term in that budget). H100 is the starting point because it is the safe upper
  bound on latency headroom; the cheaper GPU wins only if it still clears the
  budget.
- The gate's contingent cross-encoder is also a Rerank call; whether it shares
  the evidence endpoint or gets its own is a sub-decision in T4 (the design uses
  a separate, smaller endpoint, but for alpha a single shared endpoint is the
  cheaper starting point).

## Current state (verified, not assumed)

- **Query service code**: `SearchByExplicitScopeActivity` is a hardcoded stub
  returning one canned `EvidenceCandidate` with a fake `0.92` rerank score.
  There is **no** OpenSearch client, **no** embedding call, **no** reranker
  call, **no** routing logic, **no** PAPI call. Other activities (`Beer*`,
  `GetCorpus*`) are scaffold leftovers.
- **Output model already exists**: `SearchResult`, `EvidenceCandidate`,
  `RouteInfo`, `ScopeSnapshot` are generated types from the
  `ATESkywalkerQueryModel` package (referenced in `Config`), which is **not in
  this workspace**.
- **AOSS (alpha ingest account 948580600005)**: collection `skywalker-faq-alpha`
  (VECTORSEARCH, ACTIVE), endpoint
  `https://3z3yxvl1s09ylso0dgh.us-west-2.aoss.amazonaws.com`. Two indices
  `faq_evidence_a` / `faq_evidence_b`, 56 docs each, **no `faq_evidence_current`
  alias set**. Search pipeline `skywalker-faq-hybrid` exists and is correctly
  configured (`min_max` + `arithmetic_mean`).
- **Hybrid search works today**: verified by running a single `hybrid` query
  (BM25 `match` on `text` + `knn` on `embedding`, fused by the pipeline) against
  `faq_evidence_a` — it returned sensibly ranked FAQ titles. AOSS requires the
  `X-Amz-Content-SHA256` SigV4 header on bodied requests; omitting it returns a
  bare 403 (this was the cause of the earlier 403s, confirmed empirically).
- **Index schema** (confirmed correct per user): fragment model — `fragment_id`,
  `source_id`, `text` (BM25), `embedding` (knn_vector dim 1024, FAISS/HNSW
  m:24 ef_construction:128 cosinesimil), `country`/`level`/`role` (keyword
  arrays, scope filter), `followup_fragment_ids`, `line_of_business`,
  `content_type`, `source_metadata` (flat_object, holds `title`),
  `corpus_version`. Sample `corpus_version` = `prod-demo-v4-2026-06-03`.
- **No SageMaker endpoints or models exist** in the alpha ingest account.
- **Cross-account retrieval auth path is ready**: role
  `ATESkywalkerIngest-OpenSearchQueryRole-alpha` (in 948580600005) trusts the
  query-service account `465556393784`; CDK `app.ts` already grants the query
  task role `sts:AssumeRole` on it. AOSS data-access policy
  `skywalker-faq-alpha-access` grants index read/write to any account principal
  with `aoss:APIAccessAll`.
- **Service deps available** (`Config`): CoralGuice, CoralRpcSupport,
  CoralCloudAuthSupport, AwsJavaSdk-Core-Auth, AppConfig, Guava, JDK25. **No AWS
  SDK service clients** (no SageMaker runtime, no OpenSearch client, no STS) are
  declared yet.

## Authoritative grounding (internal sources)

- **Cohere Embed v4 is available on Amazon Bedrock** (`cohere.embed-v4:0`, dim
  1024) — the same model the ingestion pipeline used to build the stored
  vectors, so a query embedded via Bedrock lands in the identical vector space.
  No parity risk; no self-hosted embedding endpoint needed.
- Cohere **Rerank** is self-hostable on SageMaker (AWS Marketplace) — AWS
  Prescriptive Guidance "Reranker – LLM as judge and cross-encoder(s)" and
  multiple internal LLDs. **Cohere Rerank v4.0 Pro** Marketplace listing
  `prodview-du2svpomxs5vw` (product `prod-b3hko54dqpujq`, v1.1.0), flat
  **$3.50/instance-hour** software fee, `api_version: 2`, `max_tokens_per_doc`
  default 4096. (Verified from the live Marketplace listing.)
- OpenSearch hybrid (BM25+kNN) with a `normalization-processor` using `min_max`
  + `arithmetic_mean` weights `[0.3, 0.7]` is a proven internal pattern (e.g.
  AWS BuilderLabs ATKB, Bonobo KB/RAG). The deployed `skywalker-faq-hybrid`
  pipeline matches this exactly.
- **Spend approval (S&TP):** thresholds keyed to the greater of 12-month
  projected or committed spend. 2× `ml.p5.4xlarge` prod HA fleet ≈ $228K/yr →
  **$75K–$500K tier → Level 7 + L7 Finance.** A single beta/alpha endpoint
  (~$45K/yr) → **$10K–$75K tier → Level 6.** "AWS Infrastructure" consistent
  with an OP1/OP2 plan may be carved out of CFO/CEO approval — confirm with
  finance POC.

## Scope

### Scope: In
- New Coral activities + service-layer code in `ATESkywalkerQuery` for the
  query pipeline stages through the evidence reranker.
- A **Bedrock embedding client** — Cohere Embed v4 (`cohere.embed-v4:0`,
  `input_type: search_query`, dim 1024) for the query vector.
- A SageMaker-runtime **rerank client** for the evidence reranker (Cohere Rerank
  v4.0 Pro payload shape).
- ~~The routing gate~~ **— DROPPED (2026-06-03, no variant set this pass).** With
  UKB out of scope there is no second arm to route to, so the gate adds no
  behavior. Pipeline is FAQ-only; `RouteInfo` carries a static FAQ route marker.
  The gate returns as a later edition alongside UKB.
- An **AOSS hybrid retrieval client** (SigV4 incl. `X-Amz-Content-SHA256`,
  cross-account `sts:AssumeRole`, hybrid query body, scope `efficient_filter`).
- **Candidate normalization** into the common envelope the reranker consumes.
- SSM-backed config reads for the calibration-active knobs (gate thresholds,
  hybrid weights, candidate budgets, timeouts).
- CDK changes in `ATESkywalkerQueryCDK` for the new IAM
  (`sagemaker:InvokeEndpoint`, `bedrock:InvokeModel` on the Embed v4 model ARN,
  `ssm:GetParameter`) and endpoint wiring config.
- A **latency perf-test harness** comparing the reranker on `ml.p5.4xlarge`
  (H100, start here) vs a single high-end GPU (e.g. `ml.g6e.xlarge`) at the
  production payload shape, against the p95 250–450 ms budget.
- Unit tests with fake clients for the new service logic.

### Scope: Out (explicitly not built in this effort)
- Everything downstream of the evidence reranker: abstain composite
  (`NO_USABLE_EVIDENCE` / `EVIDENCE_TOO_WEAK_AFTER_RERANK`), post-rerank
  linked-item expansion, sibling reconstruction, citation/`citations[]` envelope
  assembly.
- **The UKB general arm (Section 06) — out of scope; later edition.** The gate
  still runs and records its verdict, but the only retrieval arm wired now is the
  FAQ arm. On a "would-be dual-arm" verdict the pipeline retrieves FAQ-only and
  marks the route honestly.
- MCP Gateway, CloudAuth OBO, TransitiveAuth, Slack, QuickSuite, UAT frontend,
  inline agent, response streaming. **We assume an MCP gateway will sit in front
  of this service later**; this effort builds the backend it will call, not the
  gateway integration.
- The ingestion pipeline (separate `ATESkywalkerIngest` package/account).
- Removing the existing `Beer*` / `GetCorpus*` scaffold activities (leave as-is
  unless asked; not in the critical path).
- Do not add defensive config, abstraction layers, or speculative extensibility
  beyond what each stage needs to run.

## Approach

Build the pipeline as discrete, independently testable service components behind
the existing Coral activity layer, wired with Guice (the service already uses
CoralGuice). Each external dependency (SageMaker embed, SageMaker rerank, AOSS,
gate rerank) gets a thin client interface with a fake implementation for tests,
matching the testing posture the docs assume ("reusable fake dependencies").

Pipeline orchestration (a `QueryPipeline` service) sequences: embed → gate →
retrieve → normalize → rerank, reading calibration knobs from SSM/AppConfig and
emitting route metadata into `RouteInfo`.

### Alternative considered: keep query embedding self-hosted on SageMaker
Rejected — Cohere Embed v4 on Bedrock is the same model that built the index, so
Bedrock gives vector-space parity with zero extra hosting. A self-hosted embed
endpoint would add cost and a parity risk for no benefit. The embedding client
is still a thin seam, so swapping providers later is a one-class change.

### Routing gate: skip it, always FAQ-only — ADOPTED (2026-06-03)
Originally rejected (the gate is a fixed architectural decision in Section 04),
but the user directed "no variants" for this pass. With UKB out of scope there is
no second arm to route to, so the gate adds no behavior now. The pipeline is
FAQ-only and records a static route marker; the gate returns as a later edition
alongside UKB.

## Decomposition (tasks, each ≈ one commit, sized to the ≤~500-line/≤~10-file ceiling)

> Ordering puts the empirically-riskiest unknowns first.

- [ ] **T1 — AOSS hybrid retrieval client (Java).** SigV4 (`aoss`, incl.
      `X-Amz-Content-SHA256`), cross-account assume-role, hybrid query body
      (BM25 `match` on `text`+`title` from `source_metadata`, `knn` on
      `embedding` with FAISS `efficient_filter` on country/level/role),
      `search_pipeline=skywalker-faq-hybrid`. Fake + unit tests. Add AWS SDK deps.
- [ ] **T2 — Candidate normalization.** Map AOSS hits → common candidate envelope
      (`EvidenceCandidate`-shaped), preserving `source_id`, title (from
      `source_metadata`), text, arm-local rank, scope snapshot. Unit tests.
- [ ] **T3 — Bedrock embedding client.** `BedrockRuntimeClient`, `InvokeModel`
      against `cohere.embed-v4:0`, body `{texts, input_type: "search_query",
      embedding_types: ["float"]}`, parse `embeddings.float[0]` (1024 floats).
      Fake + tests.
- [~] **T4 — Routing gate. DROPPED (2026-06-03).** No variant set provided this
      pass and UKB is out of scope, so there is no second arm to route to. The
      pipeline runs FAQ-only (embed → retrieve → normalize → rerank); `RouteInfo`
      gets a static FAQ route marker. No cosine/variant gate is built.
- [ ] **T5 — Evidence rerank client (SageMaker).** `SageMakerRuntimeClient`,
      `InvokeEndpoint`, Cohere Rerank v4.0 Pro payload (`model`, `query`,
      `documents`, `top_n`, `max_tokens_per_doc: 4096`, `api_version: 2`), parse
      `results[].index/relevance_score`, map back to candidates. Timeout + single
      retry. Fake + tests.
- [ ] **T6 — Pipeline orchestration + wire real activity.** `QueryPipeline`
      sequences embed → FAQ retrieve → normalize → rerank (no gate). Replace the
      `SearchByExplicitScopeActivity` stub body with a real call. `by_explicit_scope`
      only — no PAPI, no `by_alias`/`by_employee_id` this pass. Integration-style
      test with all fakes.
- [ ] **T7 — CDK: IAM + config + endpoint-creation stack.** Add
      `sagemaker:InvokeEndpoint` (endpoint ARN), `bedrock:InvokeModel` (Embed v4
      model ARN), `ssm:GetParameter` to the task role; add SSM params for
      calibration knobs; endpoint references. Also author the SageMaker
      rerank-endpoint creation CDK (Cohere Rerank v4.0 Pro, `ml.p5.4xlarge`) but
      DO NOT deploy — the user deploys (Q1=b). `npm run build` + vitest.
- [ ] **T8 — Reranker latency perf test (H100 vs single high-end GPU).** Harness
      that drives the rerank client at the production payload shape (20 docs ×
      ~1000 tokens) against each endpoint, captures p50/p95 `ModelLatency` +
      end-to-end, and reports which instances clear the p95 250–450 ms budget.
      Start with `ml.p5.4xlarge`; add `ml.g6e.xlarge` for comparison. (Runs
      against the live endpoint the user deploys per Q1=b.)

## Verification

- Per task: `brazil-build` in `ATESkywalkerQuery` (iteration), `npm run build` +
  vitest in `ATESkywalkerQueryCDK`.
- Final: `brazil-recursive-cmd --allPackages brazil-build` from
  `~/Skywalker/query_sagemaker/src` (canonical full-tree check), run verbatim.
- Empirical: run the real AOSS hybrid client against `faq_evidence_a` in alpha
  (read-only) and confirm ranked results match the Python probe.
- New service logic covered by unit tests with fakes; no live SageMaker
  dependency required to run the suite.

## Open Questions (need your call before / during execution)

### Resolved
- **R1 — Model hosting split.** Query embedding via **Bedrock Cohere Embed v4**;
  SageMaker hosts **only** the evidence reranker (Cohere Rerank v4.0 Pro).
- **R2 — UKB out of scope.** FAQ arm only into the reranker this pass; gate still
  runs and records its verdict. UKB is a later edition.
- **R3 — Reranker instance.** Start on `ml.p5.4xlarge` (H100); latency perf test
  (T8) decides the production instance against the p95 250–450 ms budget.
- **R4 — MCP gateway.** Assume an MCP gateway sits in front later; build the
  backend it will call, not the gateway integration.

### Resolved 2026-06-03 (this session)
- **R5 (was Q1) — Endpoint creation: (b).** I author the SageMaker rerank
  endpoint-creation CDK; **the user deploys it** (Marketplace subscribe + EULA +
  spend). I never deploy or subscribe.
- **R6 (was Q2) — Entry modes: (b).** `by_explicit_scope` only this pass. No
  PAPI, no `by_alias`/`by_employee_id`.
- **R7 (was Q3) — Variants: none.** No variant set this pass → the routing gate
  (T4) is dropped; FAQ-only pipeline through the reranker, nothing further.
- **R8 (was Q4) — Model package: added.** `ATESkywalkerQueryModel` is now in the
  workspace (Smithy model, verified 2026-06-03), so the generated types and tool
  shapes are available.

## Changelog
- 2026-06-03: initial draft.
- 2026-06-03: embedding moved to Bedrock Cohere Embed v4 (parity, no self-hosted
  embed endpoint); SageMaker hosts reranker only; corrected to Cohere Rerank
  v4.0 Pro with verified $3.50/hr fee + S&TP approval tiers; UKB out of scope;
  MCP-gateway-in-front assumption recorded; reranker instance set to H100 start
  with a latency perf test (new T8) deciding production; dropped T0 parity
  investigation (moot under Bedrock).
- 2026-06-03: Open Questions resolved — Q1=(b) I author endpoint-creation CDK,
  user deploys; Q2=(b) `by_explicit_scope` only, no PAPI; Q3 = no variant set →
  **T4 routing gate DROPPED** (FAQ-only pipeline); Q4 = `ATESkywalkerQueryModel`
  added to the workspace. Status DRAFT → READY.

## Update 2026-06-03 (PM) — Rerank path redirect + diagnostics experiment

The reranker is now a **config-selectable choice between two models — not a fallback chain:**
- **(A) Amazon Bedrock on-demand `cohere.rerank-v3-5`** (Rerank 3.5, **4,096-token** context window). On-demand: no SageMaker endpoint, no Marketplace subscription/EULA, no L6 spend approval, no deploy gate.
- **(B) SageMaker Cohere Rerank v4.0 Pro** (**~38,000-token** context window) — the originally-planned endpoint.

Selection is by config; the two are alternatives chosen per run. **This pass is an experiment**: run path (A) 3.5 against the real corpus and use the T5b diagnostics to observe whether the 4K window overflows (fragments are long — the sample FAQ answer alone is ~1,000+ tokens). SageMaker v4 (38K) is the larger-window alternative, **not** an overflow fallback.

Decomposition deltas:
- **T5** — rerank client targets Bedrock Rerank 3.5 by default (config-selectable to the SageMaker v4 path). Wraps each call with the T5b diagnostics; diagnostics default `enabled=true`, `contextWindowTokens=4096`, `charsPerToken=4.0`. Embedding (T3) stays Bedrock Cohere Embed v4 (unchanged).
- **T5b — DONE (commit 0b5e745).** `com.amazon.ateskywalkerquery.diagnostics.RerankDiagnostics` (pure analyzer) + `RerankDiagnosticsReport`.
- **T7** — shrinks to IAM (`bedrock:InvokeModel` / Bedrock Rerank + `ssm:GetParameter`; add `sagemaker:InvokeEndpoint` only if the SageMaker path is selected). The SageMaker endpoint-creation stack is authored-but-shelved (not deployed).
- **T8** — SageMaker latency perf test **shelved** for this experiment.

## Changelog
- 2026-06-03 (PM): rerank redirected to a config-selectable choice between Bedrock on-demand `cohere.rerank-v3-5` (4K) and SageMaker v4.0 Pro (38K) — not a fallback; experiment observes 3.5 overflow via T5b diagnostics; T5b implemented; T7 reduced to IAM; SageMaker endpoint stack + T8 shelved.
