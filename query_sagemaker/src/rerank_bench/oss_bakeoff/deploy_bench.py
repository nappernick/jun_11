#!/usr/bin/env python3
"""deploy_bench.py — ONE atomic, cost-SAFE GPU latency benchmark for the OSS rerankers.

This is the LATENCY-ONLY GPU phase of the bakeoff. Quality/ranking is measured on
MPS by run_local.py (hardware-independent); this script exists solely to put a
number on "unoptimized eager-PyTorch serving latency" on a real g5 GPU — a
CEILING, not OSS's best (see CONTRACT.md hard rules).

Lifecycle (atomic, single human-run invocation):
  1. package  sagemaker_infer.py + models.py (+ code/requirements.txt) -> model.tar.gz
  2. upload   model.tar.gz -> s3://<bench bucket in nick-caia>/...
  3. create   SageMaker Model (HuggingFace PyTorch inference DLC GPU image, us-east-1)
  4. create   endpoint-config  (ml.g5.2xlarge, role executor-sage)
  5. create   endpoint, wait InService
  6. warm up, then time N=30 reps/model at pool sizes 5/10/20 (discard first 3 cold)
  7. write    latency_gpu.json  {model:{pool:{p50,p90,p99,n}}}
  8. ALWAYS teardown (delete endpoint, config, model) in finally:
  9. PROVE    zero endpoints/configs/models remain (list + print)

SAFETY (the whole point of this module):
  - teardown() runs in a finally: that fires on success, exception, AND
    KeyboardInterrupt.
  - An INDEPENDENT watchdog (forked daemon thread + atexit hook) force-deletes the
    endpoint after a hard cap (default 45 min) regardless of main-thread state.
  - --teardown-only deletes everything for our fixed resource names and prints proof.

NEVER hardcode AWS keys. Auth = boto3 Session(profile_name='nick-caia') (acct
429134228173, us-east-1), exactly like cohere_adapters.CohereV4Reranker.

DO NOT RUN as part of authoring — the human reviews and runs this serially while
the g5 quota (1 endpoint) is free. CLI:
    python deploy_bench.py                 # full deploy + bench + guaranteed teardown
    python deploy_bench.py --teardown-only # just delete everything + prove zero remain
    python deploy_bench.py --watchdog-min 30 --reps 30
"""
from __future__ import annotations

import argparse
import atexit
import io
import json
import sys
import tarfile
import threading
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent

# --- fixed identity (CONTRACT.md > AWS) -----------------------------------------
PROFILE = "nick-caia"
REGION = "us-east-1"
ACCOUNT = "429134228173"
ROLE_ARN = "arn:aws:iam::429134228173:role/executor-sage"
INSTANCE_TYPE = "ml.g5.2xlarge"

# Stable, predictable resource names so --teardown-only (a fresh process that did
# NOT deploy) can always find and delete exactly what a deploy created. No random
# suffix on purpose: there is at most ONE of these alive (g5 quota = 1 endpoint).
RESOURCE_TAG = "oss-rerank-bench"
MODEL_NAME = f"{RESOURCE_TAG}-model"
CONFIG_NAME = f"{RESOURCE_TAG}-config"
ENDPOINT_NAME = f"{RESOURCE_TAG}-ep"
# Name MUST contain "sagemaker": the executor-sage role's AmazonSageMakerFullAccess
# scopes its S3 grant to arn:aws:s3:::*sagemaker* only, so a non-sagemaker bucket name
# fails CreateModel with "Could not access model data" (s3:GetObject denied).
BUCKET = f"sagemaker-oss-rerank-bench-{ACCOUNT}"  # one bench bucket in nick-caia
S3_KEY = "deploy_bench/model.tar.gz"

# OSS models served by sagemaker_infer.py — mirror models.OSS_MODEL_IDS so the GPU
# run covers exactly the local set. (Imported below if available; this is the
# offline fallback to keep authoring/self-test free of any heavy import.)
OSS_MODEL_IDS = ["ettin-1b", "qwen3-0.6b", "qwen3-4b", "nemotron-1b-v2"]
POOL_SIZES = [5, 10, 20]
DEFAULT_REPS = 30
DISCARD_COLD = 3  # first N reps per (model,pool) are cold-start; discarded
DEFAULT_WATCHDOG_MIN = 45

# HuggingFace PyTorch INFERENCE DLC, GPU, us-east-1. Registry account 763104351884
# is the AWS Deep Learning Containers registry. This tag was resolved 2026-06-05 from
# the authoritative AWS DLC listing (aws.github.io/deep-learning-containers/reference/
# available_images) as the NEWEST HF PyTorch *inference* GPU image for us-east-1.
#
# It ships transformers 5.5.3 — essentially the spine-validated version (local 5.10.2),
# so NO transformers override is needed (the prior 4.51.3 assumption was wrong). The DLC
# 5.5.3 already has the from_pretrained `dtype=` alias, Qwen3, and ModernBERT (Ettin),
# so models.py runs as-is on the DLC's own transformers + CUDA-matched torch 2.6. This
# removes the 4.x->5.x override that was the #1 deploy risk. See CODE_REQUIREMENTS.
HF_DLC_IMAGE_FALLBACK = (
    f"763104351884.dkr.ecr.{REGION}.amazonaws.com/"
    "huggingface-pytorch-inference:2.6.0-transformers5.5.3-gpu-py312-cu124-ubuntu22.04"
)


def _log(msg: str) -> None:
    """Timestamped stdout line (latency runs are long; timestamps aid the human)."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# --- AWS clients (lazy, frozen-profile, never any hardcoded keys) ----------------
def _session():
    import boto3  # lazy, mirrors cohere_adapters.py
    return boto3.Session(profile_name=PROFILE, region_name=REGION)


def _clients():
    """Return (sagemaker, s3) boto3 clients on the nick-caia profile."""
    sess = _session()
    return sess.client("sagemaker"), sess.client("s3")


def resolve_dlc_image() -> str:
    """Prefer the sagemaker SDK's image_uris (always-current); fall back to the
    pinned known-good URI when the SDK is not installed (this venv is boto3-only)."""
    try:
        from sagemaker import image_uris  # type: ignore
        uri = image_uris.retrieve(
            framework="huggingface",
            region=REGION,
            image_scope="inference",
            instance_type=INSTANCE_TYPE,
            version="4.51.3",
            base_framework_version="pytorch2.6.0",
        )
        _log(f"resolved HF DLC via sagemaker SDK: {uri}")
        return uri
    except Exception as exc:  # SDK absent or lookup failed -> documented constant
        _log(f"sagemaker SDK image_uris unavailable ({type(exc).__name__}); "
             f"using pinned DLC: {HF_DLC_IMAGE_FALLBACK}")
        return HF_DLC_IMAGE_FALLBACK


# --- packaging -------------------------------------------------------------------
# DLC-aware requirements. The DLC ships transformers 5.5.3 (≈ the spine-validated
# 5.10.2) and a CUDA-matched torch 2.6, so we do NOT override transformers or torch:
#   - torch: LEFT TO THE DLC (never reinstall — would break the GPU/CUDA match).
#   - transformers: PINNED to the DLC's own 5.5.3 (a no-op install) ONLY to stop a
#     sibling dep from silently upgrading it. 5.5.3 already has `dtype=`, Qwen3, and
#     ModernBERT, and models.py also carries a dtype/torch_dtype compat shim, so the
#     spine runs unmodified. No risky 4.x->5.x major jump anymore.
#   - sentence-transformers: required by the Ettin CrossEncoder adapter (the HF DLC
#     usually bundles it, but pin to be certain it is present and 5.5.x-compatible).
#   - einops: trust_remote_code dependency for nemotron-1b-v2's llama_bidirectional code.
CODE_REQUIREMENTS = (
    "# torch + transformers come from the DLC (transformers 5.5.3, torch 2.6/cu124).\n"
    "# transformers pinned to the DLC version only to prevent an accidental upgrade.\n"
    "transformers==5.5.3\n"
    "sentence-transformers==5.5.1\n"
    "einops\n"
)


def build_model_tar(dest: Path | None = None) -> Path:
    """Package the inference handler + scoring spine into a SageMaker model.tar.gz.

    Layout expected by the HF DLC (entry_point via HF_MODEL_* / code/ dir):
        code/inference.py        <- sagemaker_infer.py (model_fn/input_fn/predict_fn/output_fn)
        code/models.py           <- the scoring SPINE (handler imports it)
        code/requirements.txt    <- minimal extras (see CODE_REQUIREMENTS)
    """
    dest = dest or (HERE / "model.tar.gz")
    infer_src = HERE / "sagemaker_infer.py"
    models_src = HERE / "models.py"
    if not infer_src.exists():
        raise FileNotFoundError(
            f"{infer_src} not found — sagemaker_infer.py is a separate deliverable and "
            f"must exist before packaging (it provides model_fn/input_fn/predict_fn/output_fn)."
        )
    if not models_src.exists():
        raise FileNotFoundError(f"{models_src} not found — scoring spine required by the handler.")

    with tarfile.open(dest, "w:gz") as tar:
        # HF DLC convention: the inference entry point is code/inference.py.
        tar.add(infer_src, arcname="code/inference.py")
        tar.add(models_src, arcname="code/models.py")
        req_bytes = CODE_REQUIREMENTS.encode("utf-8")
        info = tarfile.TarInfo("code/requirements.txt")
        info.size = len(req_bytes)
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(req_bytes))
    _log(f"packaged model.tar.gz ({dest.stat().st_size} bytes) -> code/inference.py, "
         f"code/models.py, code/requirements.txt")
    return dest


def ensure_bucket(s3) -> str:
    """Create the bench bucket in nick-caia if absent; return its name. us-east-1
    is the S3 default region, so NO LocationConstraint (specifying it errors)."""
    try:
        s3.head_bucket(Bucket=BUCKET)
        _log(f"bench bucket exists: s3://{BUCKET}")
    except Exception:
        _log(f"creating bench bucket: s3://{BUCKET}")
        s3.create_bucket(Bucket=BUCKET)  # us-east-1: no CreateBucketConfiguration
    return BUCKET


def upload_tar(s3, tar_path: Path) -> str:
    bucket = ensure_bucket(s3)
    s3.upload_file(str(tar_path), bucket, S3_KEY)
    uri = f"s3://{bucket}/{S3_KEY}"
    _log(f"uploaded model artifact -> {uri}")
    return uri


# --- deploy ----------------------------------------------------------------------
def create_model(sm, model_data_uri: str, image_uri: str) -> None:
    _log(f"creating Model '{MODEL_NAME}' (image={image_uri})")
    sm.create_model(
        ModelName=MODEL_NAME,
        ExecutionRoleArn=ROLE_ARN,
        PrimaryContainer={
            "Image": image_uri,
            "ModelDataUrl": model_data_uri,
            "Environment": {
                # HF DLC inference toolkit: point it at our custom handler dir.
                "SAGEMAKER_PROGRAM": "inference.py",
                "SAGEMAKER_SUBMIT_DIRECTORY": "/opt/ml/model/code",
                "SAGEMAKER_CONTAINER_LOG_LEVEL": "20",
                "SAGEMAKER_REGION": REGION,
                # Generous model-server timeout: first request lazy-loads weights.
                "TS_DEFAULT_RESPONSE_TIMEOUT": "900",
            },
        },
    )


def create_endpoint_config(sm) -> None:
    _log(f"creating EndpointConfig '{CONFIG_NAME}' ({INSTANCE_TYPE})")
    sm.create_endpoint_config(
        EndpointConfigName=CONFIG_NAME,
        ProductionVariants=[{
            "VariantName": "AllTraffic",
            "ModelName": MODEL_NAME,
            "InstanceType": INSTANCE_TYPE,
            "InitialInstanceCount": 1,
            "InitialVariantWeight": 1.0,
        }],
    )


def create_and_wait_endpoint(sm, max_wait_s: int = 1800) -> None:
    _log(f"creating Endpoint '{ENDPOINT_NAME}' and waiting for InService")
    sm.create_endpoint(EndpointName=ENDPOINT_NAME, EndpointConfigName=CONFIG_NAME)
    deadline = time.time() + max_wait_s
    while True:
        desc = sm.describe_endpoint(EndpointName=ENDPOINT_NAME)
        status = desc["EndpointStatus"]
        if status == "InService":
            _log(f"endpoint InService after {int(max_wait_s - (deadline - time.time()))}s")
            return
        if status in ("Failed", "OutOfService", "RollingBack"):
            reason = desc.get("FailureReason", "(no reason given)")
            raise RuntimeError(f"endpoint entered terminal status {status}: {reason}")
        if time.time() > deadline:
            raise TimeoutError(f"endpoint not InService within {max_wait_s}s (last={status})")
        _log(f"  endpoint status={status} ...")
        time.sleep(15)


# --- benchmark -------------------------------------------------------------------
def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Nearest-rank percentile on an already-sorted list (stdlib only)."""
    if not sorted_vals:
        return 0.0
    rank = max(1, int(round(pct / 100.0 * len(sorted_vals))))
    return float(sorted_vals[min(rank, len(sorted_vals)) - 1])


def _synthetic_pool(pool_size: int) -> tuple[str, list[str]]:
    """A realistic-shaped (query, docs) payload for latency timing. Latency depends
    on token count, not content, so deterministic synthetic text is fine here."""
    query = "what does the travel policy reimburse for international business trips"
    base = ("The travel policy reimburses the Lowest Logical Fare in economy class. "
            "Employees pay any class upgrade themselves. Per-diem covers meals and "
            "incidentals up to the published city cap; lodging is reimbursed at actuals "
            "against an itemized receipt within the nightly ceiling for the destination. ")
    docs = [f"FAQ passage {idx}: {base}" for idx in range(pool_size)]
    return query, docs


def _parse_body(body):
    """Decode the endpoint response into the handler's dict, defensively unwrapping
    whatever the SageMaker model server wrapped it in. The HF DLC json-encodes the
    handler's output_fn return, which (with a tuple/string return or batch dim) can
    arrive as a 1-2 element list and/or a double-encoded JSON string, e.g.
    ['{"scores":[...]}', 'application/json']. Unwrap lists and re-decode strings
    until we reach the {"scores":...} object."""
    obj = json.loads(body)
    for _ in range(3):  # at most: list -> element -> json-string -> dict
        if isinstance(obj, list):
            obj = obj[0] if obj else {}
        elif isinstance(obj, str):
            obj = json.loads(obj)
        else:
            break
    return obj


def invoke_once(runtime, model_id: str, query: str, docs: list[str]) -> float:
    """One scoring round-trip; returns wall-clock latency in ms. Raises on a
    non-decodable body so a broken handler fails the bench loudly (not silently)."""
    payload = json.dumps({"model": model_id, "query": query, "docs": docs}).encode("utf-8")
    start = time.perf_counter()
    resp = runtime.invoke_endpoint(
        EndpointName=ENDPOINT_NAME, ContentType="application/json",
        Accept="application/json", Body=payload)
    body = resp["Body"].read()
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    parsed = _parse_body(body)
    if "scores" not in parsed or len(parsed["scores"]) != len(docs):
        raise RuntimeError(f"bad handler response for {model_id} pool={len(docs)}: {body[:200]!r}")
    return elapsed_ms


def benchmark(model_ids: list[str], reps: int) -> dict:
    """Warm up each model, then time `reps` reps at each pool size, discarding the
    first DISCARD_COLD as cold-start. Returns {model:{pool:{p50,p90,p99,n}}}."""
    from botocore.config import Config  # lazy
    # The FIRST warm-up invoke per model lazy-loads its weights inside the container
    # (qwen3-4b is ~4B params). botocore's default read_timeout is 60s, which a cold
    # load can exceed -> ReadTimeoutError fails the bench. Match the container-side
    # TS_DEFAULT_RESPONSE_TIMEOUT (900s) and disable retries so a real failure surfaces
    # immediately instead of silently re-invoking and skewing latency.
    runtime = _session().client(
        "sagemaker-runtime",
        config=Config(read_timeout=900, connect_timeout=60, retries={"max_attempts": 0}))
    results: dict[str, dict] = {}
    for model_id in model_ids:
        results[model_id] = {}
        try:
          for pool_size in POOL_SIZES:
            query, docs = _synthetic_pool(pool_size)
            # Time `reps` total invocations, then DISCARD the first DISCARD_COLD as
            # cold-start (per the task: "time N=30 reps ... discard first 3 as cold"),
            # so percentiles come from the warm remainder (e.g. 30 -> n=27). The
            # discarded reps double as the warm-up that triggers lazy model load.
            timings: list[float] = []
            for rep in range(reps):
                latency_ms = invoke_once(runtime, model_id, query, docs)
                phase = "cold-discard" if rep < DISCARD_COLD else "warm"
                if rep < DISCARD_COLD or rep == DISCARD_COLD or rep == reps - 1:
                    _log(f"  {model_id} pool={pool_size} rep {rep + 1}/{reps} "
                         f"({phase}): {latency_ms:.0f}ms")
                timings.append(latency_ms)
            samples = sorted(timings[DISCARD_COLD:])
            results[model_id][str(pool_size)] = {
                "p50": round(_percentile(samples, 50), 2),
                "p90": round(_percentile(samples, 90), 2),
                "p99": round(_percentile(samples, 99), 2),
                "n": len(samples),
            }
            _log(f"  {model_id} pool={pool_size}: p50={results[model_id][str(pool_size)]['p50']}ms "
                 f"p90={results[model_id][str(pool_size)]['p90']}ms "
                 f"p99={results[model_id][str(pool_size)]['p99']}ms (n={len(samples)})")
        except Exception as exc:  # isolate per-model latency failure; keep the others
            results[model_id]["error"] = f"{type(exc).__name__}: {exc}"
            _log(f"  !! {model_id} LATENCY FAILED (continuing): {type(exc).__name__}: {str(exc)[:160]}")
    return results


def write_latency(results: dict) -> Path:
    out = HERE / "latency_gpu.json"
    # MERGE, don't overwrite: a targeted re-run (e.g. --models qwen3-*) must not wipe
    # latency already captured for the models that succeeded in a prior run.
    existing = json.loads(out.read_text()) if out.exists() else {}
    existing.update(results)
    out.write_text(json.dumps(existing, indent=2))
    _log(f"wrote {out} (models: {sorted(existing)})")
    return out


# --- QUALITY scoring on the GPU (the pivot: OSS models never touch the laptop) ---
def _runtime_client():
    from botocore.config import Config  # lazy
    return _session().client(
        "sagemaker-runtime",
        config=Config(read_timeout=900, connect_timeout=60, retries={"max_attempts": 0}))


def invoke_scores(runtime, model_id: str, query: str, docs: list[str]):
    """One real scoring round-trip; returns (scores, kind, latency_ms)."""
    payload = json.dumps({"model": model_id, "query": query, "docs": docs}).encode("utf-8")
    start = time.perf_counter()
    resp = runtime.invoke_endpoint(
        EndpointName=ENDPOINT_NAME, ContentType="application/json",
        Accept="application/json", Body=payload)
    body = resp["Body"].read()
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    parsed = _parse_body(body)
    if "scores" not in parsed or len(parsed["scores"]) != len(docs):
        raise RuntimeError(f"bad handler response for {model_id} pool={len(docs)}: {body[:200]!r}")
    return parsed["scores"], parsed.get("kind", "logit"), elapsed_ms


_MAX_CTX = {"ettin-1b": 7999, "qwen3-0.6b": 131072, "qwen3-4b": 131072, "nemotron-1b-v2": 4096}


def score_real_pools(model_ids: list[str]) -> None:
    """Score the frozen real BM25 pools (pools.json) through the GPU endpoint and
    merge each model's slice into scored.json — SAME schema run_local.py writes for
    cohere-3.5, so metrics/judge/dashboard treat OSS and Cohere identically. This is
    the hardware-independent quality data, gathered on the GPU instead of the Mac."""
    import sys as _sys
    _sys.path.insert(0, str(HERE.parent))  # normalize.py lives one dir up
    from normalize import squash

    pools = json.loads((HERE / "pools.json").read_text())["pools"]
    runtime = _runtime_client()
    scored_path = HERE / "scored.json"
    doc = json.loads(scored_path.read_text()) if scored_path.exists() else {"meta": {}, "models": {}}

    failed = {}
    for model_id in model_ids:
        _log(f"QUALITY scoring real pools through endpoint: {model_id}")
        try:
            queries_out = {}
            kind = "logit"
            for i, (q, items) in enumerate(pools.items()):
                docs = [it["text"] for it in items]
                ids = [it["node_id"] for it in items]
                scores, kind, lat = invoke_scores(runtime, model_id, q, docs)
                norms = [squash(s, kind) for s in scores]
                order = sorted(range(len(docs)), key=lambda j: (-scores[j], j))
                queries_out[q] = {
                    "ranking": [ids[j] for j in order],
                    "raw": {ids[j]: round(scores[j], 5) for j in range(len(docs))},
                    "norm": {ids[j]: round(norms[j], 5) for j in range(len(docs))},
                    "top_id": ids[order[0]],
                    "latency_ms": round(lat, 1),
                }
                if (i + 1) % 10 == 0:
                    _log(f"  {model_id}: {i + 1}/{len(pools)} queries scored")
            doc["models"][model_id] = {
                "kind": kind, "max_context": _MAX_CTX.get(model_id),
                "device": "sagemaker-g5.2xlarge", "queries": queries_out,
            }
            doc["meta"] = {"generated_at": datetime.now().isoformat(),
                           "n_queries": len(pools), "models": sorted(doc["models"].keys())}
            scored_path.write_text(json.dumps(doc, indent=2))
            _log(f"  merged scored.json[{model_id}] ({len(queries_out)} queries)")
            # SANITY (per the "every model returns differently" risk): a wrong field/
            # index read for THIS model would yield degenerate (near-constant) scores
            # that still look valid. Flag it now, before teardown, by checking the
            # per-query raw-score spread. A reranker must SEPARATE docs within a query.
            ranges = []
            for v in queries_out.values():
                vals = list(v["raw"].values())
                ranges.append(max(vals) - min(vals) if vals else 0.0)
            ranges.sort()
            med_range = ranges[len(ranges) // 2] if ranges else 0.0
            degenerate = sum(1 for r in ranges if r < 1e-6)
            _log(f"  SANITY[{model_id}] median within-query score spread={med_range:.4f}  "
                 f"degenerate(flat)={degenerate}/{len(ranges)}  kind={kind}")
            if degenerate > len(ranges) * 0.3:
                _log(f"  !! SANITY WARN[{model_id}]: {degenerate}/{len(ranges)} queries have "
                     f"FLAT scores — likely a wrong output-field read for this model's return shape")
        except Exception as exc:  # isolate per-model GPU failure; keep the others + latency
            failed[model_id] = f"{type(exc).__name__}: {exc}"
            _log(f"  !! {model_id} QUALITY FAILED (continuing): {type(exc).__name__}: {str(exc)[:160]}")
    if failed:
        _log(f"quality scoring finished with {len(failed)} model(s) failed: {list(failed)}")
    return failed


# --- teardown + proof ------------------------------------------------------------
def _safe_delete(label: str, fn) -> None:
    """Call a delete_* op; swallow 'already gone' so teardown is idempotent and a
    second pass (finally + atexit + watchdog) never crashes on a missing resource."""
    try:
        fn()
        _log(f"deleted {label}")
    except Exception as exc:
        msg = str(exc)
        if "Could not find" in msg or "ValidationException" in msg or "does not exist" in msg:
            _log(f"{label} already absent")
        else:
            _log(f"WARN deleting {label}: {type(exc).__name__}: {msg[:160]}")


def teardown(sm=None) -> None:
    """Delete endpoint, then config, then model — idempotent. Order matters: the
    endpoint references the config which references the model."""
    if sm is None:
        sm, _ = _clients()
    _log("TEARDOWN: deleting endpoint, config, model")
    _safe_delete(f"endpoint {ENDPOINT_NAME}",
                 lambda: sm.delete_endpoint(EndpointName=ENDPOINT_NAME))
    _safe_delete(f"endpoint-config {CONFIG_NAME}",
                 lambda: sm.delete_endpoint_config(EndpointConfigName=CONFIG_NAME))
    _safe_delete(f"model {MODEL_NAME}",
                 lambda: sm.delete_model(ModelName=MODEL_NAME))


def prove_clean(sm=None) -> bool:
    """List endpoints / configs / models filtered to our names and PRINT explicit
    proof that zero remain. Returns True iff all three are gone.

    delete_endpoint is ASYNC: right after teardown the endpoint can still list with
    status 'Deleting' for a minute or two. That is a successful teardown in progress,
    NOT a leak — so we treat 'Deleting' as gone to avoid a false 'RESOURCES STILL
    PRESENT' alarm (and a spurious non-zero exit) on an otherwise-clean run. Any other
    live status (InService/Creating/Failed/...) IS counted as remaining."""
    if sm is None:
        sm, _ = _clients()
    all_eps = sm.list_endpoints(NameContains=RESOURCE_TAG).get("Endpoints", [])
    eps = [e for e in all_eps if e.get("EndpointStatus") != "Deleting"]
    deleting = [e for e in all_eps if e.get("EndpointStatus") == "Deleting"]
    cfgs = sm.list_endpoint_configs(NameContains=RESOURCE_TAG).get("EndpointConfigs", [])
    mdls = sm.list_models(NameContains=RESOURCE_TAG).get("Models", [])
    _log("PROOF OF TEARDOWN (resources matching '%s'):" % RESOURCE_TAG)
    _log(f"  endpoints remaining:        {len(eps)} {[e['EndpointName'] for e in eps]}"
         + (f"  (+{len(deleting)} in 'Deleting' — async teardown in progress, treated as gone)"
            if deleting else ""))
    _log(f"  endpoint-configs remaining: {len(cfgs)} {[c['EndpointConfigName'] for c in cfgs]}")
    _log(f"  models remaining:           {len(mdls)} {[m['ModelName'] for m in mdls]}")
    clean = not eps and not cfgs and not mdls
    _log("  RESULT: %s" % ("ZERO remain — clean." if clean else "RESOURCES STILL PRESENT — investigate!"))
    return clean


# --- watchdog (independent of main-thread control flow) --------------------------
def _force_delete_endpoint_only(why: str) -> None:
    """Best-effort, fast force-delete of the ENDPOINT (the only thing that costs
    money per-hour). Configs/models are free to leave briefly; the finally: + the
    --teardown-only mode reap them. Uses its own client/session so it works even if
    the main thread is wedged mid-call."""
    try:
        sm, _ = _clients()
        sm.delete_endpoint(EndpointName=ENDPOINT_NAME)
        _log(f"WATCHDOG ({why}): force-deleted endpoint {ENDPOINT_NAME}")
    except Exception as exc:
        msg = str(exc)
        if "Could not find" in msg or "ValidationException" in msg or "does not exist" in msg:
            _log(f"WATCHDOG ({why}): endpoint already absent")
        else:
            _log(f"WATCHDOG ({why}) WARN: {type(exc).__name__}: {msg[:160]}")


def start_watchdog(watchdog_min: int) -> threading.Event:
    """Register the independent safety net: a forked DAEMON thread that force-deletes
    the endpoint after a hard cap, PLUS an atexit hook as a second, independent
    backstop. Returns a 'fired/cancelled' Event the main path sets on clean exit so
    the thread does not delete a successfully-torn-down endpoint a second time."""
    done = threading.Event()
    cap_s = watchdog_min * 60

    def _watch():
        # Wait up to the hard cap; if the main path finishes first it sets `done`
        # and we exit quietly. If the cap elapses first, force-delete regardless of
        # whatever the main thread is (or isn't) doing.
        if not done.wait(timeout=cap_s):
            _force_delete_endpoint_only(f"hard cap {watchdog_min}min exceeded")

    watcher = threading.Thread(target=_watch, name="endpoint-watchdog", daemon=True)
    watcher.start()
    _log(f"watchdog armed: force-delete endpoint after {watchdog_min} min (daemon thread + atexit)")

    # Independent backstop: even if the interpreter exits abnormally (uncaught
    # error, sys.exit, normal end) atexit still runs and reaps the endpoint.
    def _atexit_reap():
        if not done.is_set():
            _force_delete_endpoint_only("atexit backstop")
    atexit.register(_atexit_reap)
    return done


# --- orchestration ---------------------------------------------------------------
def run_full(reps: int, watchdog_min: int, models_filter=None) -> int:
    """Deploy -> bench -> ALWAYS teardown. Returns process exit code.

    models_filter: if given, only deploy+score these model ids (e.g. re-run just the
    Qwen3 pair after a fix). scored.json + latency_gpu.json MERGE, so prior models'
    data is preserved. Loading fewer models also avoids re-downloading the others."""
    # Mirror the live model list from the spine if importable; else the offline list.
    model_ids = OSS_MODEL_IDS
    try:
        import models  # the scoring spine
        model_ids = list(models.OSS_MODEL_IDS)
    except Exception as exc:
        _log(f"could not import models.OSS_MODEL_IDS ({type(exc).__name__}); "
             f"using built-in list {OSS_MODEL_IDS}")
    if models_filter:
        model_ids = [m for m in model_ids if m in models_filter]
        _log(f"models filter active -> scoring only: {model_ids}")

    done = start_watchdog(watchdog_min)
    sm, s3 = _clients()
    exit_code = 0
    try:
        image_uri = resolve_dlc_image()
        tar_path = build_model_tar()
        model_data_uri = upload_tar(s3, tar_path)
        create_model(sm, model_data_uri, image_uri)
        create_endpoint_config(sm)
        create_and_wait_endpoint(sm)
        # QUALITY FIRST: capture the durable verdict data (real-pool scores) before
        # the latency loop, so a hiccup in timing can't cost us the quality result.
        failed = score_real_pools(model_ids)
        _log("quality scoring complete (scored.json updated)")
        good = [m for m in model_ids if m not in failed]
        if not good:
            _log("WARN: no model scored successfully — skipping latency benchmark")
        results = benchmark(good, reps) if good else {}
        write_latency(results)
        _log("benchmark complete")
    except KeyboardInterrupt:
        _log("KeyboardInterrupt — proceeding to guaranteed teardown")
        exit_code = 130
    except Exception as exc:
        _log(f"ERROR during deploy/bench: {type(exc).__name__}: {exc}")
        exit_code = 1
    finally:
        # GUARANTEED teardown — fires on success, exception, AND KeyboardInterrupt.
        teardown(sm)
        clean = prove_clean(sm)
        done.set()  # disarm watchdog + atexit: endpoint is already gone
        if not clean:
            exit_code = exit_code or 2
    return exit_code


def run_teardown_only() -> int:
    """Delete everything for our fixed names and print proof zero remain. Safe to
    run any time (idempotent) — e.g. if a prior run was killed before its finally:."""
    sm, _ = _clients()
    teardown(sm)
    clean = prove_clean(sm)
    return 0 if clean else 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--teardown-only", action="store_true",
                        help="delete endpoint/config/model for the fixed bench names and prove zero remain")
    parser.add_argument("--reps", type=int, default=DEFAULT_REPS,
                        help=f"timed reps per (model,pool) after warm-up (default {DEFAULT_REPS})")
    parser.add_argument("--watchdog-min", type=int, default=DEFAULT_WATCHDOG_MIN,
                        help=f"hard cap minutes before the watchdog force-deletes (default {DEFAULT_WATCHDOG_MIN})")
    parser.add_argument("--models", nargs="*", default=None,
                        help="only deploy+score these model ids (default: all). scored/latency json MERGE.")
    args = parser.parse_args(argv)

    if args.teardown_only:
        return run_teardown_only()
    return run_full(reps=args.reps, watchdog_min=args.watchdog_min, models_filter=args.models)


if __name__ == "__main__":
    raise SystemExit(main())
