"""LIVE retrieval proof through the production build_live_backend path (Req 4.1/4.3).

Builds the real live OptimizerBackend (clients are lazy, so construction does no
Bedrock I/O), then issues ONE read-only retrieval against the held-constant substrate
and prints the real fragments. Proves the live v2 run's retrieval uses the ALPHA
OpenSearch substrate end-to-end, exercising the new client_factory/refresh heal wiring.
Read-only; no Bedrock author/judge calls.
"""
import asyncio


async def main() -> int:
    from bakeoff.quality.optimizer.backends import build_live_backend
    from bakeoff.quality.optimizer.retrieval import RetrievalQuery

    backend = build_live_backend()  # lazy clients; retrieval client built eagerly via factory
    sub = getattr(backend.retrieval, "name", "?")
    print(f"[live-retr] backend={backend.name} retrieval_substrate={sub}", flush=True)

    q = RetrievalQuery(item_id="probe", turn=1, query="How do I submit an expense report?", top_k=3)
    frags = await backend.retrieval.retrieve(q)
    print(f"[live-retr] substrate={sub} fragments_returned={len(frags)}", flush=True)
    for f in frags[:3]:
        md = f.get("metadata", {}) or {}
        print(
            f"[live-retr]  id={str(f.get('id'))[:40]} conf={f.get('confidence')} "
            f"country={md.get('country')} level={md.get('level')} "
            f"text={str(f.get('text'))[:70]!r}",
            flush=True,
        )

    ok = sub == "opensearch" and len(frags) >= 1
    print(
        f"[live-retr] RESULT={'PASS (live aoss)' if ok else 'PARTIAL/' + str(sub)} "
        f"substrate={sub} frags={len(frags)}",
        flush=True,
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
