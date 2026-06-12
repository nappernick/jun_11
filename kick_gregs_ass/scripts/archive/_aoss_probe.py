"""Classify the ALPHA aoss 403: stale creds vs permanent permissions/index policy.

Uses the EXACT credential + client path build_live_backend uses (the credential
broker bound to the 'alpha' profile + Urllib3AWSV4SignerAuth, service 'aoss'),
then issues a read-only match_all against candidate index names. Read-only only.
"""
import sys

from bakeoff import config


def main() -> int:
    host = config.QUALITY_OPT_OPENSEARCH_ALPHA_ENDPOINT.replace("https://", "")
    region = config.QUALITY_OPT_OPENSEARCH_ALPHA_REGION
    service = config.QUALITY_OPT_OPENSEARCH_ALPHA_SERVICE
    profile = config.QUALITY_OPT_OPENSEARCH_ALPHA_PROFILE
    print(f"[probe] endpoint={host} region={region} service={service} profile={profile}", flush=True)

    from bakeoff.credentials import get_broker
    creds = get_broker().get_credentials(profile)
    ak = getattr(creds, "access_key", None) or getattr(creds, "access_key_id", None)
    tok = getattr(creds, "token", None) or getattr(creds, "session_token", None)
    print(f"[probe] broker creds: access_key={str(ak)[:10]}… has_session_token={bool(tok)}", flush=True)

    from opensearchpy import OpenSearch, Urllib3AWSV4SignerAuth, Urllib3HttpConnection

    auth = Urllib3AWSV4SignerAuth(creds, region, service)
    client = OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=Urllib3HttpConnection,
        timeout=15,
        max_retries=1,
        retry_on_timeout=False,
    )

    # Try the configured index plus the likely alternates seen elsewhere in this tree.
    for idx in (config.QUALITY_OPT_OPENSEARCH_ALPHA_INDEX, "faq_evidence_b", "faq_evidence", "*"):
        try:
            r = client.search(index=idx, body={"size": 1, "query": {"match_all": {}}})
            total = r.get("hits", {}).get("total")
            n = len(r.get("hits", {}).get("hits", []))
            print(f"[probe] index={idx!r} -> OK total={total} hits_returned={n}", flush=True)
            if n:
                src = r["hits"]["hits"][0].get("_source", {})
                print(f"[probe]   sample _id={r['hits']['hits'][0].get('_id')} keys={list(src)[:8]}", flush=True)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            print(f"[probe] index={idx!r} -> ERR {type(e).__name__}: {msg[:220]}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
