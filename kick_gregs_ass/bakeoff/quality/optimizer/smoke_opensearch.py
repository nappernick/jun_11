"""
[LIVE / MANUAL] ALPHA OpenSearch retrieval smoke (Task 4.5, Req 16.1 / 16.6).

OWNER-ASSERTED-ASSUMPTION VALIDATION against an EXTERNAL/VENDOR-SOURCED service. This is an
operator-run smoke, NOT a unit test: it is **excluded from the offline `pytest` suite** and
touches **real AWS** (the deployed ALPHA OpenSearch service in account ``948580600005``)
only when run by hand. Importing this module is network-free and boto3/opensearch-free —
every AWS-touching import happens lazily inside :class:`OpenSearchRetrievalBackend`
(``opensearchpy`` imported on first ``retrieve``) and inside :func:`run_smoke` / :func:`main`,
so it can never break the offline suite even though it is not collected as a test.

WHAT IT VALIDATES. The closed-loop optimizer prefers the **OpenSearch_Backend** as its
held-constant, read-only retrieval substrate (Req 16.1), with the repo's local
``POST /retrieve`` service as a guaranteed-workable fallback (Req 16.2). The ALPHA
OpenSearch **endpoint, index, and auth** for account ``948580600005`` are **owner-provided
operational assumptions to confirm at implementation time** (Req 16.6) — they are NOT
verified against an Amazon-internal primary source in this environment, and they are left
as ``None`` placeholders in :mod:`bakeoff.config`
(``QUALITY_OPT_OPENSEARCH_ENDPOINT`` / ``_INDEX`` / ``_AUTH``), to be INJECTED rather than
hard-coded. This smoke is the manual step that confirms those owner-provided values
actually resolve and that the live service returns fragments in the **same
``{id, text, metadata, ...}`` shape** as the local corpus (Req 16.4), so downstream
grounding/judging are unaffected by which backend served a query.

METHODOLOGY CAVEAT (carried from ``requirements.md`` / ``design.md``). The ALPHA OpenSearch
endpoint specifics are **external/vendor-sourced** and **owner-provided**, NOT an
Amazon-internal primary source. This smoke is exactly the re-validation the caveat calls
for. It does not make the study depend on OpenSearch being the only backend — the local
fallback exists precisely so it never does (Req 16.6).

HOW IT WORKS. Through the **real** :func:`bakeoff.quality.optimizer.retrieval.build_retrieval_backend`
selector (so the smoke exercises the production selection + fallback + memoization path,
not a re-implementation) it:

  1. Resolves the connection facts: CLI ``--endpoint`` / ``--index`` / ``--auth`` override
     the ``QUALITY_OPT_OPENSEARCH_*`` config placeholders. It reports whether each resolved
     and refuses (unless ``--allow-fallback``) to silently smoke the *local* backend when
     OpenSearch is unconfigured — a silent fallback would defeat the purpose of the smoke.
  2. Builds ``build_retrieval_backend("opensearch", ...)`` and confirms the selector landed
     on the OpenSearch backend (``name == "opensearch"``) rather than falling back.
  3. Issues ONE read-only query (a :class:`RetrievalQuery`) against the live index.
  4. Asserts every returned fragment matches the local-corpus shape — at minimum a string
     ``id``, a string ``text``, and a dict ``metadata`` (the common shape all backends
     return, Req 16.4) — and prints a per-fragment shape report + an overall verdict.

SAFETY. Gated behind the **bake-off-active quota guard** (reuses
:func:`bakeoff.quality.main._bakeoff_run_looks_active`): refuses to issue live queries while
a bake-off run looks active unless ``--force`` is given. The query is **read-only** (a
search request, Req 16.5); the smoke never writes to the index.

USAGE::

    # config placeholders filled in (QUALITY_OPT_OPENSEARCH_ENDPOINT/INDEX/AUTH):
    .venv/bin/python -m bakeoff.quality.optimizer.smoke_opensearch --query "how do I reset my password"

    # owner-provided values injected on the command line:
    .venv/bin/python -m bakeoff.quality.optimizer.smoke_opensearch \
        --endpoint https://<domain>.us-west-2.es.amazonaws.com \
        --index faq-corpus --auth sigv4 --query "refund policy" --force
"""
from __future__ import annotations

import argparse
import asyncio
from typing import Optional

__all__ = ["main", "run_smoke", "fragment_shape_ok"]

#: The ALPHA AWS account the OpenSearch_Backend is deployed in (Req 16.1; owner-provided).
ALPHA_OPENSEARCH_ACCOUNT = "948580600005"

# Exit codes (documented so an operator / wrapper script can branch on the verdict):
_EXIT_OK = 0              # endpoint/index/auth resolved AND fragment shape matches
_EXIT_GUARD_REFUSED = 2   # bake-off run looks active and --force not given
_EXIT_UNCONFIGURED = 3    # OpenSearch endpoint/index not configured (and fallback not allowed)
_EXIT_SHAPE_MISMATCH = 4  # query ran but returned fragments do not match the local-corpus shape


def fragment_shape_ok(fragment) -> tuple[bool, list[str]]:
    """Check one fragment against the common local-corpus ``{id, text, metadata, ...}`` shape.

    Returns ``(ok, problems)`` where ``problems`` lists each field that is missing or of the
    wrong type. The contract every backend honors (Req 16.4): a string ``id``, a string
    ``text``, and a dict ``metadata`` (extra keys such as ``confidence`` are allowed and not
    required). This mirrors what :meth:`OpenSearchRetrievalBackend._map_hit` produces and
    what the local ``/retrieve`` service returns, so a match here means downstream grounding
    /judging cannot tell which backend served the query.
    """
    problems: list[str] = []
    if not isinstance(fragment, dict):
        return False, [f"fragment is not a dict (got {type(fragment).__name__})"]
    if not isinstance(fragment.get("id"), str):
        problems.append("missing/!str 'id'")
    if not isinstance(fragment.get("text"), str):
        problems.append("missing/!str 'text'")
    if not isinstance(fragment.get("metadata"), dict):
        problems.append("missing/!dict 'metadata'")
    return (not problems), problems


async def run_smoke(
    *,
    endpoint: Optional[str],
    index: Optional[str],
    auth: Optional[str],
    query: str,
    top_k: Optional[int],
    allow_fallback: bool,
):
    """Build the real selector, issue ONE read-only query, and return the observations.

    Returns a dict with: ``resolved`` (per-field endpoint/index/auth resolution), the
    selected backend ``name``, whether the selector fell back off OpenSearch, the returned
    ``fragments``, and the per-fragment shape report. All AWS access is lazy (the OpenSearch
    client is built inside the backend's ``retrieve`` on first use).
    """
    # Lazy imports (keep module import network-free and boto3/opensearch-free).
    from bakeoff.quality.optimizer.retrieval import RetrievalQuery, build_retrieval_backend

    resolved = {
        "endpoint": endpoint,
        "index": index,
        "auth": auth,
    }

    # Build via the REAL selector so we exercise selection + fallback + memoization. Passing
    # the owner-provided values explicitly (when given) overrides the config placeholders.
    backend = build_retrieval_backend(
        "opensearch",
        opensearch_endpoint=endpoint,
        opensearch_index=index,
        opensearch_auth=auth,
    )
    selected_name = backend.name
    fell_back = selected_name != "opensearch"

    if fell_back and not allow_fallback:
        # The selector fell back to local (OpenSearch unconfigured/unusable). A silent local
        # smoke would not validate OpenSearch, so surface this rather than masking it.
        return {
            "resolved": resolved,
            "selected_name": selected_name,
            "fell_back": True,
            "fragments": [],
            "shape_report": [],
        }

    q = RetrievalQuery(item_id="smoke-opensearch", turn=1, query=query, top_k=top_k)
    fragments = list(await backend.retrieve(q))

    shape_report = []
    for i, frag in enumerate(fragments, start=1):
        ok, problems = fragment_shape_ok(frag)
        shape_report.append((i, ok, problems, frag))

    return {
        "resolved": resolved,
        "selected_name": selected_name,
        "fell_back": fell_back,
        "fragments": fragments,
        "shape_report": shape_report,
    }


def _print_resolution(resolved: dict) -> None:
    """Print whether each owner-provided connection fact resolved to a value."""
    print("[smoke] connection-fact resolution (owner-provided assumptions, Req 16.6):")
    for field in ("endpoint", "index", "auth"):
        value = resolved.get(field)
        status = "RESOLVED" if value else "MISSING "
        # Never echo a full auth descriptor verbatim; report presence, not the secret value.
        shown = "<set>" if (field == "auth" and value) else (value if value else "(none)")
        print(f"    {field:9s}: {status} {shown}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bakeoff.quality.optimizer.smoke_opensearch",
        description=(
            f"[LIVE / MANUAL] Smoke the ALPHA OpenSearch retrieval backend (AWS account "
            f"{ALPHA_OPENSEARCH_ACCOUNT}): confirm the owner-provided endpoint/index/auth "
            f"resolve and that returned fragments match the local-corpus {{id,text,metadata,...}} "
            f"shape. Owner-asserted-assumption / external-vendor validation; not part of the "
            f"offline pytest suite."
        ),
    )
    parser.add_argument(
        "--endpoint", default=None,
        help="OpenSearch endpoint (owner-provided). Overrides config.QUALITY_OPT_OPENSEARCH_ENDPOINT.",
    )
    parser.add_argument(
        "--index", default=None,
        help="OpenSearch index (owner-provided). Overrides config.QUALITY_OPT_OPENSEARCH_INDEX.",
    )
    parser.add_argument(
        "--auth", default=None,
        help="auth descriptor (owner-provided). Overrides config.QUALITY_OPT_OPENSEARCH_AUTH.",
    )
    parser.add_argument(
        "--query", default="how do I reset my password",
        help="the read-only query text to issue against the live index.",
    )
    parser.add_argument(
        "--top-k", type=int, default=5,
        help="number of fragments to request (read-only search size). Default 5.",
    )
    parser.add_argument(
        "--allow-fallback", action="store_true",
        help="permit smoking the LOCAL backend if OpenSearch is unconfigured/unusable "
             "(default: refuse, so a missing OpenSearch config is reported, not masked).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="run the live smoke even if a bake-off run looks active (shared Bedrock/AWS quota).",
    )
    args = parser.parse_args(argv)

    # --- bake-off-active quota guard (reuse main.py's heuristic) ---------------------
    from bakeoff.quality.main import _bakeoff_run_looks_active

    if _bakeoff_run_looks_active() and not args.force:
        print(
            "[guard] A bake-off run looks active (outcomes.jsonl written in the last 2 min). "
            "Refusing to issue live OpenSearch queries to avoid contending for the shared "
            "Bedrock/AWS rate limit. Re-run with --force once the bake-off run is done."
        )
        return _EXIT_GUARD_REFUSED

    from bakeoff import config

    # CLI overrides win; otherwise fall back to the owner-provided config placeholders.
    endpoint = args.endpoint if args.endpoint is not None else config.QUALITY_OPT_OPENSEARCH_ENDPOINT
    index = args.index if args.index is not None else config.QUALITY_OPT_OPENSEARCH_INDEX
    auth = args.auth if args.auth is not None else config.QUALITY_OPT_OPENSEARCH_AUTH

    print(f"[smoke] ALPHA OpenSearch account {ALPHA_OPENSEARCH_ACCOUNT} — LIVE/MANUAL retrieval smoke.")
    print("[smoke] Methodology caveat: endpoint/index/auth are OWNER-PROVIDED, external/vendor-")
    print("[smoke]   sourced assumptions — NOT an Amazon-internal primary source (Req 16.6).")
    _print_resolution({"endpoint": endpoint, "index": index, "auth": auth})

    if not (endpoint and index):
        if not args.allow_fallback:
            print(
                "\n[verdict] OpenSearch endpoint/index NOT configured. Fill in "
                "QUALITY_OPT_OPENSEARCH_ENDPOINT/INDEX (and AUTH) or pass --endpoint/--index, "
                "or re-run with --allow-fallback to smoke the local backend instead. Refusing "
                "to silently smoke the local fallback (it would not validate OpenSearch)."
            )
            return _EXIT_UNCONFIGURED

    result = asyncio.run(
        run_smoke(
            endpoint=endpoint,
            index=index,
            auth=auth,
            query=args.query,
            top_k=args.top_k,
            allow_fallback=args.allow_fallback,
        )
    )

    if result["fell_back"] and not args.allow_fallback:
        print(
            f"\n[verdict] selector fell back to '{result['selected_name']}' (OpenSearch not "
            "usable with the resolved config). Re-run with --allow-fallback to smoke the local "
            "backend, or fix the OpenSearch endpoint/index/auth."
        )
        return _EXIT_UNCONFIGURED

    print(f"\n[smoke] selected backend: '{result['selected_name']}'"
          + (" (FELL BACK off OpenSearch)" if result["fell_back"] else " (OpenSearch resolved)"))
    print(f"[smoke] query: {args.query!r}")
    print(f"[smoke] fragments returned: {len(result['fragments'])}")

    all_ok = True
    for i, ok, problems, frag in result["shape_report"]:
        frag_id = frag.get("id") if isinstance(frag, dict) else None
        text = frag.get("text", "") if isinstance(frag, dict) else ""
        snippet = str(text).strip().replace("\n", " ")
        if len(snippet) > 120:
            snippet = snippet[:120] + "…"
        status = "OK  " if ok else "BAD "
        detail = "" if ok else f"  problems={problems}"
        print(f"    [{status}] frag {i}: id={frag_id!r} text={snippet!r}{detail}")
        all_ok = all_ok and ok

    if not result["fragments"]:
        print(
            "\n[verdict] endpoint/index/auth resolved and the read-only query ran, but the "
            "index returned ZERO fragments for this query. Try a --query known to match the "
            "corpus before concluding shape parity."
        )
        # Resolution succeeded even though no rows came back; treat as OK-resolution but
        # surface the empty result for the operator to judge.
        return _EXIT_OK if not result["fell_back"] else _EXIT_UNCONFIGURED

    if all_ok:
        print(
            "\n[verdict] OpenSearch endpoint/index/auth RESOLVED and every returned fragment "
            "matches the local-corpus {id, text, metadata, ...} shape (Req 16.4). The "
            "owner-provided ALPHA connection facts are confirmed for this query."
        )
        return _EXIT_OK

    print(
        "\n[verdict] endpoint/index/auth resolved and the query ran, but one or more "
        "fragments do NOT match the local-corpus {id, text, metadata, ...} shape. The "
        "OpenSearch hit→fragment mapping needs attention before this backend is used."
    )
    return _EXIT_SHAPE_MISMATCH


if __name__ == "__main__":  # pragma: no cover - manual/live entrypoint
    raise SystemExit(main())
