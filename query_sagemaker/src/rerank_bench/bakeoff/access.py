"""bakeoff.access — auth + read-only access to the alpha FAQ AOSS corpus.

This is the AUTH + retrieval substrate the harness FREEZE seam builds on. It is
deliberately read-only: search, count, and full-corpus pull. Nothing here
mutates the collection.

Auth model (proven path, see steering doc corex-access-and-creds / alpha-demo-corpus):
  - A boto3 Session pinned to the `alpha` profile (account 948580600005,
    us-west-2). Refresh creds first:  ada credentials update --account
    948580600005 --provider conduit --role IibsAdminAccess-DO-NOT-DELETE
    --profile alpha --once
  - The live index is resolved from SSM (/skywalker/ingestion/faq_evidence/
    live_index) — the two physical indices faq_evidence_a/_b alternate per
    rebuild, so we never hardcode the name.
  - AOSS requests are SigV4-signed with service "aoss".

Env overrides (so the harness can be pointed elsewhere without code edits):
  SKYWALKER_AWS_PROFILE   default "alpha"
  SKYWALKER_AOSS_ENDPOINT default the alpha collection endpoint
  SKYWALKER_AOSS_REGION   default "us-west-2"
  SKYWALKER_LIVE_INDEX    if set, skips the SSM lookup and uses this index name
"""
from __future__ import annotations

import hashlib
import json
import os

from bakeoff.contract import Candidate

_DEFAULT_ENDPOINT = "https://3z3yxvl1s09ylso0dgh.us-west-2.aoss.amazonaws.com"
_SSM_LIVE_INDEX_PARAM = "/skywalker/ingestion/faq_evidence/live_index"
_SERVICE = "aoss"

# Per-axis "applies to everyone" sentinel token (confirmed live against
# faq_evidence_b, 2026-06-04). A scope filter must match the requester's
# specific value OR this sentinel, so content tagged for all is always returned.
SCOPE_SENTINELS = {
    "country": "Global",
    "level": "All Job Levels",
    "role": "All Employee Classes",
    "line_of_business": "All LOBs",
}


def scope_filter(axis_values: dict[str, str | list[str]]) -> list[dict]:
    """Build sentinel-aware OpenSearch term filters from a {axis: value(s)} map.

    Each axis becomes a `terms` clause over [requested_value(s), axis_sentinel],
    and the clauses are ANDed together. Axes with no known sentinel match the
    requested value(s) exactly.

    Example:
        scope_filter({"country": "India", "level": "L5"})
        -> [{"terms": {"country": ["India", "Global"]}},
            {"terms": {"level": ["L5", "All Job Levels"]}}]
    """
    clauses: list[dict] = []
    for axis, val in axis_values.items():
        vals = [val] if isinstance(val, str) else list(val)
        sentinel = SCOPE_SENTINELS.get(axis)
        if sentinel and sentinel not in vals:
            vals = vals + [sentinel]
        clauses.append({"terms": {axis: vals}})
    return clauses


class AossAccess:
    """Read-only SigV4 client for the alpha FAQ AOSS collection."""

    def __init__(
        self,
        *,
        profile: str | None = None,
        endpoint: str | None = None,
        region: str | None = None,
        index: str | None = None,
    ) -> None:
        import boto3  # lazy

        self.profile = profile or os.environ.get("SKYWALKER_AWS_PROFILE", "alpha")
        self.endpoint = (
            endpoint or os.environ.get("SKYWALKER_AOSS_ENDPOINT", _DEFAULT_ENDPOINT)
        ).rstrip("/")
        self.region = region or os.environ.get("SKYWALKER_AOSS_REGION", "us-west-2")

        self._session = boto3.Session(profile_name=self.profile, region_name=self.region)
        # Frozen creds: fail loudly here if creds are missing/expired rather than
        # at request time with an opaque 403.
        self._creds = self._session.get_credentials()
        if self._creds is None:
            raise RuntimeError(
                f"No AWS credentials for profile '{self.profile}'. Run: ada credentials "
                f"update --account 948580600005 --provider conduit "
                f"--role IibsAdminAccess-DO-NOT-DELETE --profile {self.profile} --once"
            )
        self.index = index or os.environ.get("SKYWALKER_LIVE_INDEX") or self._resolve_index()

    # --- internals ---------------------------------------------------------
    def _resolve_index(self) -> str:
        """Resolve the live physical index name from the SSM pointer."""
        ssm = self._session.client("ssm")
        resp = ssm.get_parameter(Name=_SSM_LIVE_INDEX_PARAM)
        return resp["Parameter"]["Value"]

    def _signed(self, method: str, path: str, body: dict | None = None):
        import requests  # lazy
        from botocore.auth import SigV4Auth  # lazy
        from botocore.awsrequest import AWSRequest  # lazy

        url = self.endpoint + path
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        headers["X-Amz-Content-SHA256"] = hashlib.sha256(data or b"").hexdigest()
        req = AWSRequest(method=method, url=url, data=data, headers=headers)
        # Re-freeze each call so rotating creds are honored within a long run.
        SigV4Auth(self._creds.get_frozen_credentials(), _SERVICE, self.region).add_auth(req)
        resp = requests.request(method, url, headers=dict(req.headers), data=data, timeout=30)
        resp.raise_for_status()
        return resp.json()

    # --- public read-only API ---------------------------------------------
    def count(self) -> int:
        """Total docs in the live index."""
        return int(self._signed("POST", f"/{self.index}/_count", {"query": {"match_all": {}}})["count"])

    def search(self, query: str, size: int, scope_filter: list[dict] | None = None) -> list[Candidate]:
        """BM25 candidate pool for a query, optionally scope-filtered.

        scope_filter is a list of OpenSearch filter clauses (e.g.
        [{"term": {"geography": "Global"}}]) ANDed with the BM25 match.
        Returns contract.Candidate objects ready for a reranker adapter.
        """
        bool_query: dict = {"must": [{"match": {"text": query}}]}
        if scope_filter:
            bool_query["filter"] = scope_filter
        body = {
            "size": size,
            "_source": ["source_id", "text", "source_metadata"],
            "query": {"bool": bool_query},
        }
        hits = self._signed("POST", f"/{self.index}/_search", body)["hits"]["hits"]
        return [self._to_candidate(h["_source"]) for h in hits]

    def fetch_all(self, max_docs: int = 1000) -> list[Candidate]:
        """Pull the entire corpus (small: ~56 docs) as Candidates."""
        body = {"size": max_docs, "_source": ["source_id", "text", "source_metadata"],
                "query": {"match_all": {}}}
        hits = self._signed("POST", f"/{self.index}/_search", body)["hits"]["hits"]
        return [self._to_candidate(h["_source"]) for h in hits]

    @staticmethod
    def _to_candidate(src: dict) -> Candidate:
        md = src.get("source_metadata") or {}
        node_id = md.get("nodeId") or src.get("source_id") or md.get("title") or "unknown"
        title = md.get("title") or ""
        text = src.get("text", "") or ""
        full = f"{title}\n{text}".strip() if title else text
        return Candidate(node_id=str(node_id), text=full, source_metadata=md)
