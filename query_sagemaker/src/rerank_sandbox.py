#!/usr/bin/env python3
"""Personal-sandbox deploy / test / teardown for Cohere Rerank v4.0 Pro on SageMaker.

    python rerank_sandbox.py deploy   # ~10 min, then bills per hour while InService (see Marketplace Pricing)
    python rerank_sandbox.py test     # send a rerank query, print scores + latency
    python rerank_sandbox.py delete   # tear down -- DO THIS when you're done

Needs: pip install boto3 cohere
"""
import os, sys, time, boto3, cohere

# --- Credentials come from the environment; never hardcode them. ---
#   export AWS_ACCESS_KEY_ID=...  AWS_SECRET_ACCESS_KEY=...  (or use an AWS profile)
ACCOUNT_ID        = os.environ.get("AWS_ACCOUNT_ID", "429134228173")
AWS_ACCESS_KEY    = os.environ.get("AWS_ACCESS_KEY_ID")
AWS_SECRET_KEY    = os.environ.get("AWS_SECRET_ACCESS_KEY")
REGION            = os.environ.get("AWS_REGION", "us-east-1")
ROLE_ARN          = f"arn:aws:iam::{ACCOUNT_ID}:role/executor-sage"  # SageMaker exec role (S3/ECR/CW)
VPC_ID            = "vpc-0cbaa131eec9d28e5"     # default VPC  (not wired -- see VpcConfig note)
SUBNET_ID         = "subnet-0111e46e9ee98fe80"  # us-east-1b default subnet
IGW_ID            = "igw-07129f0e458526124"     # internet gateway (informational)
# Cohere Rerank v4.0 PRO model package (us-east-1; seller publishes a per-region ARN map):
MODEL_PACKAGE_ARN = "arn:aws:sagemaker:us-east-1:865070037744:model-package/cohere-rerank-v4-0-pro-v1-0-12-27a435b507143f729689232ecb36c294"
# Variant registry (us-east-1): which Marketplace package + endpoint name to use.
# Select on the CLI: `python rerank_sandbox.py deploy fast` (default: pro).
MODELS = {
    "pro":  ("arn:aws:sagemaker:us-east-1:865070037744:model-package/cohere-rerank-v4-0-pro-v1-0-12-27a435b507143f729689232ecb36c294",  "cohere-rerank4-pro-sandbox"),
    "fast": ("arn:aws:sagemaker:us-east-1:865070037744:model-package/cohere-rerank-v4-0-fast-v1-0-1-7fdc47cb40423c30bd17057a1ba5b1d3", "cohere-rerank4-fast-sandbox"),
}
# -------------------------------------------------------------------------------

NAME          = "cohere-rerank4-pro-sandbox"    # reused for model/config/endpoint
INSTANCE_TYPE = "ml.g5.xlarge"  # deploy() asserts this is in the package's supported list first
AMI_VERSION   = "al2-ami-sagemaker-inference-gpu-2"  # REQUIRED (Cohere sw >1.0.5) or deploy fails

_session = boto3.Session(aws_access_key_id=AWS_ACCESS_KEY,
                         aws_secret_access_key=AWS_SECRET_KEY,
                         region_name=REGION)
sm = _session.client("sagemaker")


def _create():
    assert MODEL_PACKAGE_ARN.startswith("arn:"), "Set MODEL_PACKAGE_ARN from the Marketplace Configuration step."
    supported = sm.describe_model_package(ModelPackageName=MODEL_PACKAGE_ARN) \
        ["InferenceSpecification"]["SupportedRealtimeInferenceInstanceTypes"]
    assert INSTANCE_TYPE in supported, f"{INSTANCE_TYPE} unsupported for this package; choose one of: {supported}"
    sm.create_model(
        ModelName=NAME,
        ExecutionRoleArn=ROLE_ARN,
        PrimaryContainer={"ModelPackageName": MODEL_PACKAGE_ARN},
        EnableNetworkIsolation=True,  # Marketplace model packages run isolated
        # Optional sandbox VPC pinning (off by default; SageMaker-managed network is simpler):
        # VpcConfig={"Subnets": [SUBNET_ID], "SecurityGroupIds": ["<sg-id>"]},
    )
    sm.create_endpoint_config(
        EndpointConfigName=NAME,
        ProductionVariants=[{
            "VariantName": "AllTraffic",
            "ModelName": NAME,
            "InitialInstanceCount": 1,
            "InstanceType": INSTANCE_TYPE,
            "InferenceAmiVersion": AMI_VERSION,
        }],
    )
    sm.create_endpoint(EndpointName=NAME, EndpointConfigName=NAME)
    print(f"Creating {NAME} (Creating; not waiting). Poll with: python rerank_sandbox.py status")


def status():
    try:
        d = sm.describe_endpoint(EndpointName=NAME)
        print(d["EndpointStatus"], "|", d.get("FailureReason", ""))
    except Exception as e:
        print("no endpoint:", e)


def deploy():
    _create()
    print(f"waiting for InService (~10 min)...")
    sm.get_waiter("endpoint_in_service").wait(EndpointName=NAME)
    print(f"InService -- BILLING NOW (compute + Cohere fee; see Marketplace Pricing). Run 'delete' when finished.")


def test():
    co = cohere.SagemakerClient(aws_region=REGION,
                                aws_access_key=AWS_ACCESS_KEY,
                                aws_secret_key=AWS_SECRET_KEY)
    query = "How do I get reimbursed for travel?"
    docs = [
        "Submit travel expenses in Emburse within 30 days of your trip.",
        "Parental leave is available to all regular full-time employees.",
        "Book flights through the corporate travel portal to be reimbursed.",
    ]
    t0 = time.time()
    r = co.rerank(model=NAME, query=query, documents=docs, top_n=len(docs))
    dt = (time.time() - t0) * 1000
    for res in r.results:
        print(f"  doc[{res.index}] score={res.relevance_score:.4f}")
    print(f"latency: {dt:.0f} ms (laptop RTT included; subtract ~30-80 ms for in-AWS callers)")


def delete():
    for fn, kw in ((sm.delete_endpoint,        {"EndpointName": NAME}),
                   (sm.delete_endpoint_config, {"EndpointConfigName": NAME}),
                   (sm.delete_model,           {"ModelName": NAME})):
        try:
            fn(**kw); print("deleted", kw)
        except Exception as e:
            print("skip", kw, e)


if __name__ == "__main__":
    cmd = sys.argv[1]
    variant = sys.argv[2] if len(sys.argv) > 2 else "pro"
    MODEL_PACKAGE_ARN, NAME = MODELS[variant]  # override globals for the chosen variant
    print(f"[variant={variant}] endpoint={NAME}")
    {"deploy": deploy, "create": _create, "status": status, "test": test, "delete": delete}[cmd]()
