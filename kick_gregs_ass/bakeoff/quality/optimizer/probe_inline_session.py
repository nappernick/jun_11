"""
[LIVE / MANUAL] Inline persistent-session history probe (Task 11.6, Req 12.4 / 10.7).

OWNER-ASSERTED-ASSUMPTION VALIDATION. This is an operator-run probe, NOT a unit test:
it is **excluded from the offline `pytest` suite** and makes **real Bedrock Agent
Runtime** ``InvokeInlineAgent`` calls when (and only when) it is run by hand. Importing
this module is network-free and boto3-free — every AWS-touching import (boto3 via the
adapter's default client factory) happens lazily inside :func:`run_probe` / :func:`main`,
so it can never break the offline suite even though it is not collected as a test.

WHAT IT VALIDATES. The closed-loop optimizer's quality answer path
(:class:`bakeoff.quality.optimizer.inline_session_adapter.PersistentSessionInlineAdapter`)
issues **one ``invoke_inline_agent`` call per turn** under **one stable ``sessionId``** and
relies — by default (``history_mode="server"``) — on Bedrock retaining turn history
*server-side*, keyed to that ``sessionId``, under a fully **OVERRIDDEN minimal
orchestration template** whose user turn references only ``$question$`` (no history
placeholder). The project owner asserts, from production knowledge, that the inline agent
replays prior turns implicitly under a persistent ``sessionId``. The AWS **public** API
docs only *partially* confirm this (sessions persist state within the TTL) and leave
**unambiguously unconfirmed** whether prior turns are auto-injected into the model's
context under an OVERRIDDEN minimal template. The design therefore records the
implicit-history behavior as an **owner-asserted assumption to validate against this live
probe**, with an AWS-doc-grounded fallback (``history_mode="explicit"``, which replays
prior turns via ``inlineSessionState.conversationHistory``) if the probe refutes it. See
``design.md`` ("The inline-agent invocation path (persistent session)",
"Error Handling: live-probe failure for the inline session assumption") and the live-probe
discipline already documented in ``bakeoff/adapters/inline_agent.py``.

METHODOLOGY CAVEAT (carried from ``requirements.md`` / ``design.md``). The persistent-
session implicit-history behavior is grounded in the owner's production knowledge and AWS
**public** API docs — **not** an Amazon-internal primary source (none was available when
the design was set). This probe is exactly the re-validation step the caveat calls for.

HOW IT WORKS. Through the **real** ``PersistentSessionInlineAdapter`` (so the probe
exercises the production code path, not a re-implementation) it answers a two-turn
conversation under one stable ``sessionId``:

  * **Turn 1** hands the model a distinctive secret pass-phrase the model could not
    otherwise know (generated fresh each run) and asks it to acknowledge.
  * **Turn 2** asks the model to repeat that pass-phrase, sending ONLY the turn-2
    utterance as ``inputText`` (no ``conversationHistory`` under ``history_mode="server"``).

If turn 2's answer contains the pass-phrase, the model saw turn 1 → **server-side history
is CONFIRMED** and the owner's assumption holds. If it does not, the model did not see
turn 1 under OVERRIDDEN + stable ``sessionId`` → **server-side history NOT observed → use
``history_mode="explicit"``** (the documented fallback). With ``--check-explicit-fallback``
the probe then re-runs the same conversation in ``history_mode="explicit"`` and reports
whether the documented fallback recovers the prior-turn visibility.

SAFETY. Gated behind the **bake-off-active quota guard** (reuses
:func:`bakeoff.quality.main._bakeoff_run_looks_active`): the probe refuses to issue live
calls while a bake-off run looks active, unless ``--force`` is given, so it never contends
for the shared Bedrock rate limit. Temperature is pinned to ``0.0`` for a deterministic
read. Only the two fixed Target_Models are offered (Req 12.3).

USAGE::

    # default model (sonnet-4.6-thinking-off), server-history probe only:
    .venv/bin/python -m bakeoff.quality.optimizer.probe_inline_session

    # haiku, and also confirm the explicit-history fallback recovers history:
    .venv/bin/python -m bakeoff.quality.optimizer.probe_inline_session \
        --model haiku-4.5 --check-explicit-fallback --force
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from typing import Optional

__all__ = ["main", "run_probe", "build_probe_item"]


# Exit codes (documented so an operator / wrapper script can branch on the verdict):
_EXIT_CONFIRMED = 0      # server-side history CONFIRMED (owner assumption holds)
_EXIT_GUARD_REFUSED = 2  # bake-off run looks active and --force not given
_EXIT_NOT_OBSERVED = 3   # server-side history NOT observed -> use history_mode="explicit"


def build_probe_item(token: str):
    """Build the two-turn probe conversation carrying ``token`` as a secret pass-phrase.

    Turn 1 plants the pass-phrase and asks for a bare acknowledgement; turn 2 asks the
    model to repeat it. The pass-phrase is the load-bearing signal: only a model that saw
    turn 1 (via server-side session history) can reproduce it in turn 2, since it is never
    resent. Built with the real :class:`bakeoff.types.Item` / :class:`bakeoff.types.Turn`
    so the adapter's ``_turn_utterances`` path is exercised exactly as in production.
    """
    # Local import (kept out of module import so importing this module is dependency-free).
    from bakeoff.types import CohortKey, Item, Turn

    cohort = CohortKey(
        geography="probe",
        proficiency="fluent",
        tone="neutral",
        entry_route="probe",
        momentary_state="neutral",
        answerability="full",
        turn_type="multi",
    )
    turn1 = Turn(
        turn=1,
        user_utterance=(
            f"I am going to give you a secret pass-phrase to remember for later in this "
            f"conversation. The secret pass-phrase is '{token}'. Reply with only the word OK."
        ),
        momentary_state="neutral",
    )
    turn2 = Turn(
        turn=2,
        user_utterance=(
            "What was the secret pass-phrase I gave you a moment ago? "
            "Reply with only the pass-phrase, nothing else."
        ),
        momentary_state="neutral",
    )
    return Item(
        id=f"probe-inline-session-{token}",
        turn_type="multi",
        cohort=cohort,
        turns=(turn1, turn2),
    )


async def run_probe(
    *,
    bedrock_model_id: str,
    name: str,
    history_mode: str,
    region: Optional[str] = None,
):
    """Issue the two-turn probe through the real adapter and return its observations.

    Builds a :class:`PersistentSessionInlineAdapter` in ``history_mode`` (fragment-free —
    the probe is about session history, not retrieval), answers the two-turn item under one
    stable ``sessionId``, and returns ``(token, session_id, per_turn_answers, observed)``
    where ``observed`` is ``True`` iff turn 2's answer contains the planted pass-phrase
    (case-insensitive). All AWS access is lazy: the adapter builds its
    ``bedrock-agent-runtime`` client on first invoke via its default client factory.
    """
    # Local imports (lazy on purpose: keep module import network-free and boto3-free).
    from bakeoff.quality.optimizer.inline_session_adapter import PersistentSessionInlineAdapter

    token = "ZEPHYR-" + uuid.uuid4().hex[:6].upper()
    item = build_probe_item(token)

    adapter = PersistentSessionInlineAdapter(
        name=name,
        bedrock_model_id=bedrock_model_id,
        # The only system instruction the model sees; deliberately tells it to rely on the
        # conversation so a miss reflects missing history, not an unhelpful instruction.
        instruction_override=(
            "You are a concise assistant. Answer using only what the user has told you "
            "earlier in this same conversation. If you do not have the information, say so."
        ),
        send_fragments=False,   # the probe tests session history, not retrieval
        history_mode=history_mode,
        region=region,
    )

    # Deterministic read: temperature 0.0. The inline path accepts temperature even on the
    # 4.x models (see adapter docs), and the adapter always passes it through.
    response = await adapter.generate(item, fragments=[], temperature=0.0)
    per_turn = list(response.per_turn_answers)
    turn2_answer = per_turn[-1] if per_turn else ""
    observed = token.lower() in turn2_answer.lower()
    session_id = str(response.raw.get("sessionId", "")) if isinstance(response.raw, dict) else ""
    return token, session_id, per_turn, observed


def _print_run(label: str, token: str, session_id: str, per_turn, observed: bool) -> None:
    """Print the per-turn transcript and the per-run observation for one history mode."""
    print(f"\n=== {label} ===")
    print(f"  sessionId      : {session_id}")
    print(f"  planted token  : {token}")
    for i, ans in enumerate(per_turn, start=1):
        snippet = ans.strip().replace("\n", " ")
        if len(snippet) > 200:
            snippet = snippet[:200] + "…"
        print(f"  turn-{i} answer : {snippet!r}")
    print(f"  token echoed in turn-2 : {observed}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bakeoff.quality.optimizer.probe_inline_session",
        description=(
            "[LIVE / MANUAL] Probe whether Bedrock inline-agent server-side session history "
            "is visible to the model under an OVERRIDDEN minimal template + stable sessionId "
            "(validates history_mode='server' vs the need for history_mode='explicit'). "
            "Owner-asserted-assumption validation; not part of the offline pytest suite."
        ),
    )
    parser.add_argument(
        "--model", choices=["sonnet-4.6-thinking-off", "haiku-4.5"],
        default="sonnet-4.6-thinking-off",
        help="which fixed Target_Model to probe (resolves to its Bedrock id). Default sonnet-4.6-thinking-off.",
    )
    parser.add_argument(
        "--bedrock-model-id", default=None,
        help="explicit Bedrock foundation-model id override (bypasses --model resolution).",
    )
    parser.add_argument(
        "--region", default=None,
        help="AWS region for the bedrock-agent-runtime client (default: config.AWS_REGION).",
    )
    parser.add_argument(
        "--check-explicit-fallback", action="store_true",
        help="after the server-history probe, also run history_mode='explicit' and report "
             "whether the documented conversationHistory fallback recovers prior-turn visibility.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="run the live probe even if a bake-off run looks active (shared Bedrock quota).",
    )
    args = parser.parse_args(argv)

    # --- bake-off-active quota guard (reuse main.py's heuristic) ---------------------
    # Lazy import: boto3-free and pure, but kept local so the module import surface stays
    # minimal and the guard is evaluated only when the CLI actually runs.
    from bakeoff.quality.main import _bakeoff_run_looks_active

    if _bakeoff_run_looks_active() and not args.force:
        print(
            "[guard] A bake-off run looks active (outcomes.jsonl written in the last 2 min). "
            "Refusing to issue live InvokeInlineAgent calls to avoid contending for the "
            "shared Bedrock rate limit. Re-run with --force once the bake-off run is done."
        )
        return _EXIT_GUARD_REFUSED

    from bakeoff import config

    bedrock_model_id = args.bedrock_model_id or str(
        config.QUALITY_MODELS[args.model]["bedrock_model_id"]
    )

    print("[probe] OWNER-ASSERTED-ASSUMPTION live probe — Bedrock InvokeInlineAgent.")
    print("[probe] Methodology caveat: owner production knowledge + AWS PUBLIC API docs,")
    print("[probe]   NOT an Amazon-internal primary source. This probe is the re-validation.")
    print(f"[probe] model={args.model} bedrock_model_id={bedrock_model_id}")

    # --- primary probe: history_mode="server" (the owner-asserted behavior) ----------
    token, session_id, per_turn, observed = asyncio.run(
        run_probe(
            bedrock_model_id=bedrock_model_id,
            name=f"probe-{args.model}",
            history_mode="server",
            region=args.region,
        )
    )
    _print_run("history_mode='server' (owner-asserted behavior under test)",
               token, session_id, per_turn, observed)

    if observed:
        print(
            "\n[verdict] server-side history CONFIRMED: turn-2 saw the turn-1 pass-phrase "
            "under an OVERRIDDEN minimal template + stable sessionId. The owner-asserted "
            "assumption holds; history_mode='server' is valid for this model."
        )
        exit_code = _EXIT_CONFIRMED
    else:
        print(
            "\n[verdict] server-side history NOT observed: turn-2 did not reproduce the "
            "turn-1 pass-phrase, so prior turns are NOT auto-injected under OVERRIDDEN + "
            "stable sessionId for this model. Use history_mode='explicit' (replay prior "
            "turns via inlineSessionState.conversationHistory) — the documented fallback."
        )
        exit_code = _EXIT_NOT_OBSERVED

    # --- optional: confirm the explicit-history fallback recovers prior-turn visibility
    if args.check_explicit_fallback:
        token_e, session_e, per_turn_e, observed_e = asyncio.run(
            run_probe(
                bedrock_model_id=bedrock_model_id,
                name=f"probe-{args.model}-explicit",
                history_mode="explicit",
                region=args.region,
            )
        )
        _print_run("history_mode='explicit' (documented conversationHistory fallback)",
                   token_e, session_e, per_turn_e, observed_e)
        if observed_e:
            print(
                "\n[fallback] history_mode='explicit' RECOVERS prior-turn visibility: the "
                "documented inlineSessionState.conversationHistory replay works for this model."
            )
        else:
            print(
                "\n[fallback] history_mode='explicit' did NOT recover prior-turn visibility — "
                "investigate the conversationHistory payload / template before relying on it."
            )

    return exit_code


if __name__ == "__main__":  # pragma: no cover - manual/live entrypoint
    raise SystemExit(main())
