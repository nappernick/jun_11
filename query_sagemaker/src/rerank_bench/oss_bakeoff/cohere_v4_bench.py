#!/usr/bin/env python3
"""cohere_v4_bench.py — deploy a Cohere Rerank v4 marketplace endpoint, score the
SAME 42 real pools (quality + latency), then ALWAYS tear down. The v4 commercial
baseline for the bakeoff (v4 is NOT on Bedrock — only via this SageMaker package).

Mirrors deploy_bench.py's cost-safety contract: guaranteed finally teardown +
independent watchdog (daemon thread + atexit) + --teardown-only + prove_clean.
One variant per invocation so each has its own atomic lifecycle and only ONE
endpoint bills at a time:

    python cohere_v4_bench.py fast      # deploy fast, score 42 pools, teardown
    python cohere_v4_bench.py pro       # deploy pro,  score 42 pools, teardown
    python cohere_v4_bench.py fast --teardown-only

Marketplace package ARNs + the g5.xlarge / AMI requirements come from the proven
rerank_sandbox.py. Scores merge into scored.json under model id cohere-v4-{variant},
exactly like the OSS + cohere-3.5 slices, so metrics/judge/dashboard treat them alike.
"""
from __future__ import annotations

import argparse
import atexit
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
PROFILE, REGION, ACCOUNT = "nick-caia", "us-east-1", "429134228173"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/executor-sage"
INSTANCE_TYPE = "ml.g5.xlarge"           # v4 package supported type (rerank_sandbox.py)
AMI_VERSION = "al2-ami-sagemaker-inference-gpu-2"  # REQUIRED by Cohere sw >1.0.5

# Marketplace model packages (us-east-1), from rerank_sandbox.py MODELS.
PACKAGES = {
    "pro":  "arn:aws:sagemaker:us-east-1:865070037744:model-package/cohere-rerank-v4-0-pro-v1-0-12-27a435b507143f729689232ecb36c294",
    "fast": "arn:aws:sagemaker:us-east-1:865070037744:model-package/cohere-rerank-v4-0-fast-v1-0-1-7fdc47cb40423c30bd17057a1ba5b1d3",
}
DEFAULT_WATCHDOG_MIN = 40


def _log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _session():
    import boto3
    return boto3.Session(profile_name=PROFILE, region_name=REGION)


def names(variant):
    base = f"cohere-rerank4-{variant}-sandbox"  # matches cohere_adapters endpoint name
    return base, base, base  # model, config, endpoint share the name (as rerank_sandbox.py)


def deploy(sm, variant):
    pkg = PACKAGES[variant]
    model_name, config_name, endpoint_name = names(variant)
    supported = sm.describe_model_package(ModelPackageName=pkg)[
        "InferenceSpecification"]["SupportedRealtimeInferenceInstanceTypes"]
    if INSTANCE_TYPE not in supported:
        raise RuntimeError(f"{INSTANCE_TYPE} unsupported for v4-{variant}; supported: {supported}")
    _log(f"creating Model {model_name} (marketplace package, network-isolated)")
    sm.create_model(ModelName=model_name, ExecutionRoleArn=ROLE_ARN,
                    PrimaryContainer={"ModelPackageName": pkg},
                    EnableNetworkIsolation=True)
    sm.create_endpoint_config(EndpointConfigName=config_name, ProductionVariants=[{
        "VariantName": "AllTraffic", "ModelName": model_name,
        "InitialInstanceCount": 1, "InstanceType": INSTANCE_TYPE,
        "InferenceAmiVersion": AMI_VERSION}])
    _log(f"creating Endpoint {endpoint_name}, waiting InService (~10 min)")
    sm.create_endpoint(EndpointName=endpoint_name, EndpointConfigName=config_name)
    sm.get_waiter("endpoint_in_service").wait(EndpointName=endpoint_name)
    _log(f"endpoint {endpoint_name} InService — BILLING (compute + Cohere marketplace fee)")


def score_pools(variant):
    """Score the 42 real pools through the v4 endpoint; merge into scored.json."""
    sys.path.insert(0, str(HERE.parent))
    from normalize import squash
    from cohere_adapters import CohereV4Reranker

    pools = json.loads((HERE / "pools.json").read_text())["pools"]
    r = CohereV4Reranker(variant=variant)
    queries_out = {}
    for i, (q, items) in enumerate(pools.items()):
        docs = [it["text"] for it in items]
        ids = [it["node_id"] for it in items]
        t = time.perf_counter()
        scores = r.score_pairs(q, docs)
        lat = (time.perf_counter() - t) * 1000
        norms = [squash(s, "unit") for s in scores]
        order = sorted(range(len(docs)), key=lambda j: (-scores[j], j))
        queries_out[q] = {
            "ranking": [ids[j] for j in order],
            "raw": {ids[j]: round(scores[j], 5) for j in range(len(docs))},
            "norm": {ids[j]: round(norms[j], 5) for j in range(len(docs))},
            "top_id": ids[order[0]],
            "latency_ms": round(lat, 1),
        }
        if (i + 1) % 10 == 0:
            _log(f"  cohere-v4-{variant}: {i + 1}/{len(pools)} scored")
    path = HERE / "scored.json"
    doc = json.loads(path.read_text()) if path.exists() else {"meta": {}, "models": {}}
    doc["models"][f"cohere-v4-{variant}"] = {
        "kind": "unit", "max_context": 4096, "device": "sagemaker-g5.xlarge",
        "queries": queries_out,
    }
    doc["meta"] = {"generated_at": datetime.now().isoformat(),
                   "n_queries": len(pools), "models": sorted(doc["models"].keys())}
    path.write_text(json.dumps(doc, indent=2))
    lats = sorted(v["latency_ms"] for v in queries_out.values())
    _log(f"merged scored.json[cohere-v4-{variant}]  latency p50={lats[len(lats)//2]:.0f}ms "
         f"max={lats[-1]:.0f}ms")


def teardown(sm, variant):
    model_name, config_name, endpoint_name = names(variant)
    _log(f"TEARDOWN cohere-v4-{variant}")
    for label, fn in ((f"endpoint {endpoint_name}", lambda: sm.delete_endpoint(EndpointName=endpoint_name)),
                      (f"config {config_name}", lambda: sm.delete_endpoint_config(EndpointConfigName=config_name)),
                      (f"model {model_name}", lambda: sm.delete_model(ModelName=model_name))):
        try:
            fn(); _log(f"  deleted {label}")
        except Exception as exc:
            msg = str(exc)
            if any(s in msg for s in ("Could not find", "ValidationException", "does not exist")):
                _log(f"  {label} already absent")
            else:
                _log(f"  WARN deleting {label}: {type(exc).__name__}: {msg[:140]}")


def prove_clean(sm, variant):
    _, _, endpoint_name = names(variant)
    all_eps = sm.list_endpoints(NameContains=endpoint_name).get("Endpoints", [])
    live = [e for e in all_eps if e.get("EndpointStatus") != "Deleting"]
    _log(f"PROOF v4-{variant}: live endpoints remaining: {len(live)} "
         f"{[e['EndpointName'] for e in live]}"
         + (f" (+{len(all_eps)-len(live)} Deleting, treated as gone)" if len(all_eps) > len(live) else ""))
    return not live


def _force_delete(variant, why):
    try:
        sm = _session().client("sagemaker")
        _, _, endpoint_name = names(variant)
        sm.delete_endpoint(EndpointName=endpoint_name)
        _log(f"WATCHDOG ({why}): force-deleted endpoint {endpoint_name}")
    except Exception as exc:
        if not any(s in str(exc) for s in ("Could not find", "ValidationException", "does not exist")):
            _log(f"WATCHDOG ({why}) WARN: {type(exc).__name__}: {str(exc)[:140]}")


def start_watchdog(variant, watchdog_min):
    done = threading.Event()

    def _watch():
        if not done.wait(timeout=watchdog_min * 60):
            _force_delete(variant, f"hard cap {watchdog_min}min")
    threading.Thread(target=_watch, name="v4-watchdog", daemon=True).start()
    atexit.register(lambda: (None if done.is_set() else _force_delete(variant, "atexit backstop")))
    _log(f"watchdog armed: force-delete after {watchdog_min} min")
    return done


def run(variant, watchdog_min):
    done = start_watchdog(variant, watchdog_min)
    sm = _session().client("sagemaker")
    code = 0
    try:
        deploy(sm, variant)
        score_pools(variant)
        _log(f"cohere-v4-{variant} scoring complete")
    except KeyboardInterrupt:
        _log("KeyboardInterrupt -> guaranteed teardown"); code = 130
    except Exception as exc:
        _log(f"ERROR: {type(exc).__name__}: {exc}"); code = 1
    finally:
        teardown(sm, variant)
        clean = prove_clean(sm, variant)
        done.set()
        if not clean:
            code = code or 2
    return code


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("variant", choices=["pro", "fast"])
    ap.add_argument("--teardown-only", action="store_true")
    ap.add_argument("--watchdog-min", type=int, default=DEFAULT_WATCHDOG_MIN)
    args = ap.parse_args(argv)
    sm = _session().client("sagemaker")
    if args.teardown_only:
        teardown(sm, args.variant)
        return 0 if prove_clean(sm, args.variant) else 2
    return run(args.variant, args.watchdog_min)


if __name__ == "__main__":
    raise SystemExit(main())
