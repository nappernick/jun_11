"""
Bedrock calls: Embed v4 (dense vectors) and Rerank 3.5 (confidence scores).
These are the only two functions in the whole service that touch the network.

Credential-expiry resilience (load-bearing for long runs)
---------------------------------------------------------
A bake-off run is many hours of wall-clock; a short-lived STS/Bedrock session
refreshed once at startup expires partway through. When that happens every
``invoke_model`` / ``rerank`` call raises ``ExpiredTokenException`` ("The security
token included in the request is expired"), which surfaced as a flood of
``/retrieve`` 500s that errored every trial.

So the boto3 clients here are NOT created once at import and cached forever.
Instead each call goes through ``_call_with_refresh``: on an expired/invalid-
credentials error it rebuilds the client from a FRESH ``boto3.Session`` (which
re-resolves the on-disk credential chain a sidecar refresher keeps current) and
retries, a bounded number of times with a short backoff. This mirrors the
harness's own adapter resilience (``bakeoff/adapters/bedrock.py``). A client built
from a stale session will keep failing; rebuilding the session is what actually
picks up rolled-over credentials.
"""
import json
import time

import boto3
from botocore.exceptions import ClientError, BotoCoreError

import config

# --- credential-expiry resilience knobs --------------------------------------
# Error codes / message fragments that mean "creds expired/invalid -> rebuild the
# session and retry" (matched case-insensitively against the botocore error code
# and the string form of the error).
_AUTH_EXPIRED_CODES = frozenset({
    "ExpiredTokenException",
    "ExpiredToken",
    "UnrecognizedClientException",
    "InvalidClientTokenId",
    "InvalidSignatureException",
    "AccessDeniedException",
    "CredentialsError",
    "NoCredentialsError",
    "TokenRefreshError",
})
_AUTH_EXPIRED_MESSAGE_FRAGMENTS = (
    "security token",
    "expired",
    "credential",
    "not authorized",
    "unrecognizedclient",
    "invalidsignature",
)
_MAX_REFRESH_CYCLES = 4      # rebuild-session-and-retry attempts before giving up
_REFRESH_BACKOFF_S = 2.0     # short, fixed backoff between rebuild attempts

# Lazily-built clients (rebuilt on a credential refresh). Never assume these are
# valid for the life of the process — always go through the getters.
_runtime = None
_agent_runtime = None


def _build_clients():
    """Rebuild both Bedrock clients from a FRESH session (re-resolves creds)."""
    global _runtime, _agent_runtime
    session = boto3.Session()  # fresh session => re-reads the on-disk cred chain
    _runtime = session.client("bedrock-runtime", region_name=config.AWS_REGION)
    _agent_runtime = session.client("bedrock-agent-runtime", region_name=config.AWS_REGION)


def _ensure_clients():
    if _runtime is None or _agent_runtime is None:
        _build_clients()


def _is_auth_expired(exc) -> bool:
    """True iff ``exc`` looks like an expired/invalid-credentials failure."""
    code = None
    if isinstance(exc, ClientError):
        code = (exc.response or {}).get("Error", {}).get("Code")
    if code and code in _AUTH_EXPIRED_CODES:
        return True
    msg = str(exc).lower()
    return any(frag in msg for frag in _AUTH_EXPIRED_MESSAGE_FRAGMENTS)


def _call_with_refresh(fn):
    """Invoke ``fn(runtime, agent_runtime)`` with rebuild-session-and-retry on auth expiry.

    On an expired/invalid-credentials error the clients are rebuilt from a fresh
    session (picking up rolled-over creds) and the call is retried, up to
    ``_MAX_REFRESH_CYCLES`` with a short backoff. Any other error propagates
    immediately (the caller / FastAPI turns it into a 500, which the harness then
    classifies and retries on its own schedule). The final auth failure also
    propagates once the budget is spent.
    """
    _ensure_clients()
    last_exc = None
    for attempt in range(_MAX_REFRESH_CYCLES + 1):
        try:
            return fn(_runtime, _agent_runtime)
        except (ClientError, BotoCoreError) as exc:
            if not _is_auth_expired(exc):
                raise
            last_exc = exc
            if attempt >= _MAX_REFRESH_CYCLES:
                raise
            time.sleep(_REFRESH_BACKOFF_S)
            _build_clients()  # rebuild from a fresh session, then retry
    # Unreachable (loop either returns or raises), but keep mypy/readers happy.
    raise last_exc  # pragma: no cover


def embed(texts, input_type):
    """
    input_type is "search_document" at ingest, "search_query" at query time.
    Returns list[list[float]] aligned to `texts`.
    """
    body = {
        "texts": texts,
        "input_type": input_type,
        "embedding_types": ["float"],
    }

    def _do(runtime, _agent):
        resp = runtime.invoke_model(
            modelId=config.EMBED_MODEL_ID,
            body=json.dumps(body),
            accept="*/*",
            contentType="application/json",
        )
        result = json.loads(resp["body"].read())
        emb = result["embeddings"]
        # Bedrock returns either {"float": [[...]]} (when embedding_types is set) or
        # a bare [[...]]. Handle both so a Bedrock-side change doesn't silently break us.
        if isinstance(emb, dict):
            emb = emb.get("float") or next(iter(emb.values()))
        return emb

    return _call_with_refresh(_do)


# Bedrock Rerank caps each inline document at 32000 chars. We truncate only the
# COPY sent to the reranker for scoring; the full text stays in Qdrant and is
# returned to the caller untouched.
RERANK_DOC_CHAR_LIMIT = 32000


def rerank(query, documents, top_n):
    """
    documents: list[str] (the candidate fragment texts, in candidate order).
    Returns list of {"index": i, "score": relevanceScore} sorted best-first.
    index refers back into the `documents` list you passed in.
    """

    def _do(_runtime_client, agent_runtime):
        resp = agent_runtime.rerank(
            queries=[{"type": "TEXT", "textQuery": {"text": query}}],
            sources=[
                {
                    "type": "INLINE",
                    "inlineDocumentSource": {
                        "type": "TEXT",
                        "textDocument": {"text": d[:RERANK_DOC_CHAR_LIMIT]},
                    },
                }
                for d in documents
            ],
            rerankingConfiguration={
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "modelConfiguration": {"modelArn": config.RERANK_MODEL_ARN},
                    "numberOfResults": min(top_n, len(documents)),
                },
            },
        )
        return [{"index": r["index"], "score": r["relevanceScore"]} for r in resp["results"]]

    return _call_with_refresh(_do)
