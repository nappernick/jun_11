# API Contract 11. S3 Variant Set

Covers the static S3 artifact that holds the Top 50 FAQ variants used by the hybrid routing gate. Pairs with Section 04 (routing gate) and API_10 Part B (gate reranker).

## What the architecture has already fixed

- Section 03 decision on variants (D-29): variants are a static S3 artifact, not an ingestion-pipeline output. They are maintained by hand and change only when the team decides they should change. No daily rebuild. No OpenSearch index. No publication pipeline.
- Section 04 routing gate: cosine similarity against the in-memory variant embeddings is the first pass. On cosine-ambiguous cases, a cross-encoder call to Cohere Rerank 3.5 on Bedrock (API_10 Part B) provides the second opinion.
- Section 10 D-29: the variant set is pre-embedded in S3 at maintenance time, not at service boot, so the service starts with zero embedding calls.

## The artifact

One S3 object per stage.

```
s3://skywalker-config-{stage}/variants/top_50.json
```

Example stages: `beta`, `gamma`, `prod`. Same file shape across stages; values may differ during staging rollouts.

### File shape

JSON object containing the variant set with pre-computed Cohere Embed v4 embeddings. All entries are variants of one or another Top 50 FAQ question; there is no canonical-vs-variant distinction. The set is simply 50 short question texts that define the shape of the controlled FAQ space.

```json
{
  "schema_version": 1,
  "embedding_model": "cohere.embed-v4:0",
  "embedding_dimension": 1024,
  "embedding_input_type": "search_document",
  "generated_at": "2026-04-22T12:00:00Z",
  "variants": [
    {
      "id": "v_001",
      "text": "Can I expense a dinner with a recruiting candidate?",
      "embedding": [0.0123, -0.0456, ...]  // 1024 floats
    },
    {
      "id": "v_002",
      "text": "What is the per diem rate for domestic travel in India?",
      "embedding": [...]
    }
  ]
}
```

Fields:

- **`schema_version`** — integer. Bumped if the file shape ever changes. Service refuses to load files whose schema_version it does not know about.
- **`embedding_model`** — the exact Bedrock model ID used to generate the embeddings. Must match the model used by the query-time embedding call at runtime, or cosine similarities are meaningless.
- **`embedding_dimension`** — 1024. Must match API_04's D-21 decision.
- **`embedding_input_type`** — `"search_document"`. The variants are documents; the query is embedded with `"search_query"`. This asymmetry is correct for Cohere Embed v4.
- **`generated_at`** — ISO-8601 UTC. Informational only; not used for staleness checks.
- **`variants[]`** — the set itself. `id` is a stable identifier for referring to a specific variant in logs and metrics. `text` is the canonical phrasing. `embedding` is the pre-computed vector.

### Update mechanics

The file is manually maintained. To add, remove, or edit a variant:

1. A team member edits the source list (whatever internal tool or repo holds the variant texts — out of scope for this doc).
2. A small one-off job or CLI reads the new list, calls Cohere Embed v4 with `input_type: "search_document"` on each variant text, produces a new `top_50.json`.
3. The new file is uploaded to S3, overwriting the existing object (S3 versioning is enabled so prior versions are recoverable).
4. Running service instances pick up the new file on their next boot. A rolling restart across the fleet propagates the change. **There is no hot reload for variants**; this is a deliberate simplicity choice because variants change infrequently.

Emphasis for the team: **the variant list is a control-plane artifact maintained by humans.** It is one of the highest-leverage tuning surfaces in the system because it defines the scope of the FAQ-only routing path. Treat changes with the same seriousness as architecture changes, not as configuration tweaks.

## Service boot behavior

On startup the service:

1. Reads `s3://skywalker-config-{stage}/variants/top_50.json` via `s3:GetObject`.
2. Validates `schema_version` and `embedding_dimension` against expectations.
3. Holds the `variants[]` array in memory: a list of `(id, text, embedding)` tuples.
4. Exposes a metric `skywalker.variants.count` reporting the number of variants loaded.
5. Exposes a metric `skywalker.variants.schema_version` reporting the loaded schema version.

If the file is missing, malformed, or fails validation, **the service refuses to start**. There is no fallback behavior. A service without a usable variant set cannot make the routing decision correctly, and silently starting with an empty variant set would route every request to dual-arm — a hidden quality regression. Hard-fail is the right posture.

## Runtime usage

Per request, after the query has been embedded via Cohere Embed v4 with `input_type: "search_query"`:

1. Compute cosine similarity between the query vector and each of the 50 variant embeddings. In-memory, vectorized — negligible latency.
2. Find the top similarity score (and which variant produced it, for logging).
3. Compare against the two cosine thresholds in SSM (see API_10 Part B and API_07): `cosine_high_threshold` and `cosine_low_threshold`.
4. If the score is outside the ambiguity band, the gate decision is made immediately.
5. If the score is inside the ambiguity band, the gate calls Bedrock Rerank 3.5 (API_10 Part B) against the 50 variant texts for a second opinion.

Every request produces a log line recording: the top cosine score, which variant it matched (by `id`), whether the result landed above/below/within the ambiguity band, and the final gate decision. This is essential telemetry for tuning the two cosine thresholds and the rerank floor over time.

## IAM

Service execution role needs, scoped to the specific S3 key prefix:

- `s3:GetObject` on `arn:aws:s3:::skywalker-config-{stage}/variants/*`
- `s3:GetObjectVersion` (for rollback scenarios)

Nothing else. No write access from the service role. The artifact is only written by the manual update path, which uses a separate role.

## Cost

S3 Standard storage for a single ~500 KB JSON file (50 variants × 1024-dim embeddings + text ≈ a few hundred KB), one GET per service boot. Effectively zero.

## Why pre-embed in S3 instead of embedding at boot

Pre-embedding means:

- Service boot has **zero** Cohere Embed v4 calls. Startup is purely S3 read plus JSON parse.
- Failure modes are simpler: file either loads or doesn't. No partial-embedding-failure state.
- Embedding is done once at variant-authoring time, not N times across N service instances.

The one cost is that the variant-authoring workflow has to include an embedding step. That is a maintenance-time operation, not a hot-path operation, so it's fine.

## What is deliberately not here

- No variant versioning inside the file beyond `schema_version`. The team maintains the list; git or whatever tool they use outside the service is the version-control surface. S3 object versioning is the recovery surface.
- No per-variant metadata beyond `id` and `text`. If future needs require more, bump `schema_version`.
- No hot reload. Variant changes require a service restart (or rolling restart across the fleet). Explicit simplicity choice given how rarely variants should change.
- No fallback if the file is unreadable. Hard-fail service start.

## Sections of the architecture this binds

- Section 03 D-29 (variants are static, not pipelined).
- Section 04 routing gate.
- Section 10 D-29, D-30, D-31.
- API_04 (Cohere Embed v4 as the embedding contract).
- API_10 Part B (gate reranker, second stage of the hybrid gate).

## Outstanding unknowns

- Final bucket naming convention (aligned with whatever internal S3 standards Skywalker is deployed into).
- The variant-authoring workflow itself (who owns it, what internal tool holds the source list, how the embedding step is invoked). Out of scope for this doc.
- Whether to add `schema_version = 2` that supports multilingual variants or other extensions. Launch at `1`, revisit if needed.
