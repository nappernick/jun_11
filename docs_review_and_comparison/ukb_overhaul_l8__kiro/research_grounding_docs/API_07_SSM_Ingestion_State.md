# API Contract 07. SSM Parameter Store — Ingestion State

Covers the single piece of persisted state the ingestion pipeline keeps between daily runs: the CoreX snapshot high-water mark for the evidence corpus. Pairs with Section 03 (ingestion and publication discipline) and API_06 (OpenSearch vector index, which holds the alias that resolves "what is live").

This doc also briefly enumerates the runtime control-plane parameters that live alongside the ingestion state in SSM Parameter Store, because SSM is Skywalker's general home for tunable values. See §"Control plane" below.

## What the architecture has already fixed

- Section 03 §3 decision nine: publication safety. Partial state never becomes live. The alias swap is the atomic primitive.
- Section 03 §3 decision four: rebuild-and-republish is the launch mutation model.
- Section 03 §3 decision three: the pipeline is schedule-driven on a daily cadence. One writer per corpus per day.
- Section 10 D-22: the job persists exactly one piece of state between runs — the CoreX snapshot marker — and nothing else. The variant set is not pipeline-driven; it lives as a static S3 artifact (API_11) and does not have an ingestion parameter.
- Section 10 D-23: the alias swap happens before the SSM update.

Every other piece of publication "state" is derivable from OpenSearch itself:

- **What is live?** The alias — `faq_evidence_current` points at whichever versioned index is live.
- **What was previous?** Whichever versioned indexes still exist under the retention window. `GET /_cat/indices/faq_evidence_v*` lists them.
- **Did the last publish succeed?** Implicit — if the alias advanced, it succeeded. If not, the candidate index is an orphan awaiting garbage collection.

The high-water mark is the only thing that cannot be reconstructed from OpenSearch, so it is the only thing that needs to persist elsewhere.

## Ingestion parameter

**One `String` parameter.** That is the complete ingestion state surface.

```
/skywalker/ingestion/faq_evidence/last_snapshot_marker
```

- **Tier:** Standard. Not `SecureString` — the marker is not sensitive.
- **Value shape:** depends on the change-marker strategy locked during Phase 1 (per-node version map versus content hash; see `ARCHITECTURE_TODOS.md` open decisions). Either a plain content hash string (e.g., `sha256:abc123...`) or a compact JSON payload encoded as a string. Opaque to everything except the ingestion job.
- **First-run behavior:** the parameter does not exist. `GetParameter` returns `ParameterNotFound`; the job treats absence as "no prior snapshot" and builds from scratch.

The variant set does not have an ingestion parameter. Variants are a static S3 artifact, updated by hand when the team decides to change them, loaded into memory at service boot. See API_11.

## The ingestion job flow

1. `ssm:GetParameter` for `faq_evidence`. On `ParameterNotFound`, treat as first run and fall through to build.
2. Fetch current CoreX snapshot, compute its marker.
3. Compare. Match → exit no-op. Mismatch → build.
4. Build `faq_evidence_v<N+1>` in OpenSearch. Write all children with embeddings. Validate.
5. Atomically swap the alias via OpenSearch `_aliases`.
6. `ssm:PutParameter` with `Overwrite: true`, writing the new snapshot marker.
7. Garbage-collect old versioned indexes past the retention window (default: keep the last three).

Order of steps 5 and 6 matters. Alias swap happens first. If step 6 fails after step 5 succeeds, the next daily run will see `CoreX snapshot != SSM marker` and rebuild unnecessarily — benign waste, not a correctness hazard. The opposite order (SSM first, alias second) would produce the actual hazard: remembered marker advances while live surface does not.

## Rollback runbook

Rollback is an operator action, not an automated path. Two steps in two services, performed in order:

1. **Flip the OpenSearch alias** back to the previous version:
   ```
   POST /_aliases
   {
     "actions": [
       { "remove": { "index": "faq_evidence_v<N>", "alias": "faq_evidence_current" }},
       { "add":    { "index": "faq_evidence_v<N-1>", "alias": "faq_evidence_current" }}
     ]
   }
   ```
2. **Reset the SSM parameter** to the snapshot marker that produced version `N-1`. That marker is recorded in the ingestion job's structured logs at the time of the original publish; operators retrieve it from CloudWatch Logs and write it with:
   ```
   aws ssm put-parameter \
     --name /skywalker/ingestion/faq_evidence/last_snapshot_marker \
     --value "<marker-for-v(N-1)>" \
     --type String \
     --overwrite
   ```

If step 2 is skipped or fails, the next daily run will observe `CoreX snapshot != SSM marker` and rebuild, producing a fresh `v<N+1>`. The live surface stays correct throughout. Skipping step 2 is therefore recoverable; skipping step 1 is not (if only the SSM marker is reset, live traffic continues serving `v<N>` until the next rebuild).

## Failure handling

| Failure | Outcome | Live surface |
|---|---|---|
| `GetParameter` fails at job start | Job exits, next run retries | Unchanged |
| CoreX read fails | Job exits, next run retries | Unchanged |
| Build fails mid-way | Candidate index is an orphan; garbage-collector removes it | Unchanged |
| Alias swap fails | Candidate index built, never promoted; next run rebuilds | Unchanged |
| `PutParameter` fails after alias swap | Alias advanced; marker stale; next run rebuilds unnecessarily once | Correct (new version) |
| SSM throttling | Retry with exponential backoff; SSM Standard tier limits are well above our usage | Unchanged until retry succeeds |

There is no state-divergence failure mode. Either the alias advanced or it did not; either the marker was updated or it was not. The four combinations collapse to either correct steady state or a redundant rebuild on the next run.

## IAM

The ingestion job's execution role needs, scoped to `arn:aws:ssm:{region}:{account}:parameter/skywalker/ingestion/*`:

- `ssm:GetParameter`
- `ssm:PutParameter`

Nothing else. No `DescribeParameters`, no `ssm:*`, no write access to parameters outside the `/skywalker/ingestion/` prefix.

The online query path does not touch `/skywalker/ingestion/*`. It reads from `/skywalker/runtime/*` — the control-plane surface — with its own scoped role.

## Control plane

Skywalker uses SSM Parameter Store as the single home for runtime-tunable values. The ingestion high-water mark above is one kind; the rest are **control-plane knobs** that operators can adjust without a redeploy. They live under `/skywalker/runtime/` and are read at service boot with a periodic refresh loop (launch default: 60-second polling).

```
/skywalker/runtime/gate/cosine_low_threshold              # default 0.30
/skywalker/runtime/gate/cosine_high_threshold             # default 0.80
/skywalker/runtime/gate/rerank_floor                      # default 0.50
/skywalker/runtime/gate/rerank_timeout_ms                 # default 300
/skywalker/runtime/abstain/floor                          # default 0.30 (evidence-side reranker)
/skywalker/runtime/retrieval/ukb_timeout_ms               # default 300
/skywalker/runtime/retrieval/per_arm_candidate_budget     # default 10
/skywalker/runtime/retrieval/shortlist_size               # default 5
/skywalker/runtime/retrieval/knn_overretrieve_k           # default 40
```

**Every one of these is a tuning surface explicitly designed to be adjusted without a code change.** Operators move them against judged-traffic evidence per the calibration surfaces listed in Section 10. Changes take effect within the service's refresh interval; no restart required. The refresh loop emits a CloudWatch metric on each read so dashboards and audits can see exactly which values were in effect at any given time.

The authoritative list of control-plane parameters lives in Section 10 §"Control plane surfaces." API_07 holds the canonical paths because SSM is the underlying service.

## Cost

SSM Parameter Store Standard tier: free for up to 10,000 parameters and standard API throughput. One ingestion parameter plus ~10 runtime parameters; ingestion is written once per day, runtime parameters are read at 60-second polling per running instance. Effectively zero cost. No capacity planning required.

## What is deliberately not here

- No versioning of the markers. SSM Parameter Store has built-in version history we could query if we ever needed to audit past markers, but the ingestion job does not rely on it. Audit lives in the structured logs.
- No `Tier: Advanced` or `Tier: Intelligent-Tiering`. Standard is sufficient.
- No `AllowedPattern` regex constraints. Marker format is opaque to SSM.
- No cross-region replication. Everything runs in one region.
- No alarms on parameter change. Change is the expected pattern for control-plane knobs and the daily pattern for ingestion.

## Sections of the architecture this binds

- Section 03 §2 (named outputs of ingestion, one of which is the SSM high-water mark).
- Section 03 §3 decisions three, four, nine (daily cadence, rebuild-and-republish, publication safety).
- Section 03 §7 (failure postures and the conservatism rule).
- Section 10 D-22 (ingestion state), D-23 (alias-swap atomicity), D-31 (control-plane values in SSM).
- API_06 (alias-swap mechanics in OpenSearch).
- API_11 (the static variant set, which has no SSM parameter).

## Outstanding unknowns

- Final change-marker format (per-node version map vs. content hash) — open decision in `ARCHITECTURE_TODOS.md`. Determines the ingestion parameter's value shape.
- Region (should match OpenSearch and Bedrock).
- Whether to surface the SSM ingestion marker in the ingestion job's CloudWatch metrics for visibility during rollbacks.
- Final control-plane refresh interval (launch default 60 s; tune against how quickly operators want changes to propagate).
