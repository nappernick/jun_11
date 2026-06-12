# API Contract 04. Amazon Bedrock — Cohere Embed v4

Covers the embedding model invocation used by Section 03 (FAQ evidence ingestion), Section 04 (live query embedding for the routing gate's cosine step and for FAQ-evidence retrieval), and API_11 (pre-embedded variants stored in S3, generated at variant-authoring time).

## What the architecture has already fixed

- Section 03 §3 decision eight: Cohere Embed v4 is the single embedding model across the system. The evidence corpus children are embedded at ingestion time; the query is embedded at runtime; the static S3 variant set (API_11) is pre-embedded at authoring time. A single live query embedding can interrogate both the OpenSearch evidence index and the in-memory variant vectors without vector-space drift.
- A model change forces a coordinated regeneration of the variant S3 artifact, a rebuild of the evidence index, and a code-time change in the runtime — an architecture-class event, not a configuration change. (Section 03 §3, Section 10 D-14, D-21)
- The controlled FAQ arm is allowed to compute one semantic query representation per request and reuse it across the routing-gate cosine check and the evidence retrieval k-NN query. (Section 04 §3 decision nine)

## What Bedrock and Cohere give us (baseline facts)

From the [Amazon Bedrock Embed v4 model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-cohere-embed-v4.html):

- Model ID `cohere.embed-v4:0`.
- Endpoint `https://bedrock-runtime.{region}.amazonaws.com`.
- Cross-region inference IDs `us.cohere.embed-v4:0`, `eu.cohere.embed-v4:0`, `global.cohere.embed-v4:0`.
- Context window 128K tokens.
- Launch date April 15, 2025. Model lifecycle Active.
- Supported output dimensions per Cohere's [Embed v4 changelog](https://docs.cohere.com/changelog/embed-multimodal-v4) are `{256, 512, 1024, 1536}`. Bedrock defaults reported at 1024.

Invocation uses the standard Bedrock `InvokeModel` operation. The request body is model-specific JSON with a `texts` array and Cohere-specific parameters. The response body contains an `embeddings` structure.

Content from external sources has been rephrased for compliance with licensing restrictions.

## What we still need to decide

1. **Output dimension.** Pick one of `{256, 512, 1024, 1536}`. Recommended default 1024, which is Bedrock's stated default. Lower values reduce storage and latency if needed.
2. **`input_type` discipline.** Cohere Embed takes an `input_type` that distinguishes indexing from querying. The pipeline must use:
   - `search_document` when embedding FAQ evidence chunks at ingestion.
   - `search_document` when embedding the static variant set at authoring time (for the S3 artifact per API_11).
   - `search_query` when embedding the live user query at runtime.
3. **Batching.** Ingestion should batch multiple texts per call subject to Bedrock request-size limits. Query-time embedding is single-text.
4. **Region and inference profile.** Pick in-region, geo cross-region (`us.cohere.embed-v4:0`), or global. For internal US residency the geo variant is the likely default.
5. **Auth.** The Skywalker service execution role needs `bedrock:InvokeModel` on the Embed v4 model ARN in the chosen region(s).
6. **Failure handling during ingestion.** Section 03 §7 already says publication is conservative. A `ThrottlingException` or a partial batch failure during ingestion must abort the candidate publication rather than produce a mixed-embedding corpus.

## Concrete request and response shape to pin down

Request body example (confirm final field names against current Cohere-on-Bedrock docs):

```json
{
  "texts": ["chunk body 1", "chunk body 2"],
  "input_type": "search_document",
  "embedding_types": ["float"]
}
```

Response body:

```json
{
  "embeddings": { "float": [[ ... 1024 floats ... ], [ ... 1024 floats ... ]] },
  "id": "...",
  "response_type": "embeddings_by_type"
}
```

## Sections of the architecture this binds

- Section 03 §3 decision eight (embedding contract, model name, shared contract across both surfaces).
- Section 04 §3 decision nine (one query embedding reused across the owned arm).

## Outstanding unknowns

- Final output dimension.
- Whether the team is already standardizing on a specific Bedrock region or inference profile.
- Per-call token limit for batched ingestion under Bedrock.
- Whether the team wants to add Bedrock's `AWS_BEARER_TOKEN_BEDROCK` API-key flow or stay on standard IAM/SigV4.
