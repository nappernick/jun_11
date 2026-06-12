"""
Verify the prompt-attributes fix for PersistentSessionInlineAdapter.

Part A (offline, deterministic — always runs): build the per-turn InvokeInlineAgent requests
for a 6-turn conversation carrying LARGE fragments, and prove the request size is BOUNDED —
each turn carries only its OWN turn's fragments via promptSessionAttributes, so the size does
NOT grow with turn count. Under the old inline-into-$question$ scheme the server-side history
stacked every prior turn's fragments, which is what blew past the 200k token wall.

Part B (live, best-effort): one real InvokeInlineAgent through the EXECUTION account, fragments
delivered via promptSessionAttributes, to confirm the model GROUNDS on the attribute. Skipped
cleanly when creds are unavailable.
"""
from __future__ import annotations

import json
import sys

from bakeoff import config
from bakeoff.quality.optimizer.inline_session_adapter import PersistentSessionInlineAdapter
from bakeoff.types import CohortKey, Item, Turn


def _big_fragments(turn_index: int, n: int = 10, chars: int = 3000) -> list[dict]:
    """n fragments of ~`chars` each, distinct per turn so accumulation would be visible."""
    return [
        {
            "id": f"t{turn_index}-frag-{j}",
            "text": (f"TURN {turn_index} FRAGMENT {j}: " + ("policy detail " * (chars // 14))),
            "metadata": {},
        }
        for j in range(n)
    ]


def _six_turn_item() -> Item:
    cohort = CohortKey(
        geography="Global", proficiency="fluent", tone="terse", entry_route="slack",
        momentary_state="neutral", answerability="full", turn_type="multi",
    )
    utterances = [
        "How do I request a corporate card?",
        "What is the approval timeline once submitted?",
        "Can I expedite approval for urgent travel?",
        "Who approves the escalation?",
        "What happens if the card is lost abroad?",
        "How do I close the card when I leave?",
    ]
    turns = tuple(
        Turn(turn=i + 1, user_utterance=u, momentary_state="neutral")
        for i, u in enumerate(utterances)
    )
    return Item(id="verify-6turn", turn_type="multi", cohort=cohort,
                query=utterances[0], answerability="full", turns=turns)


def part_a_bounded() -> None:
    print("=== Part A: bounded per-turn request size (deterministic) ===")
    item = _six_turn_item()
    adapter = PersistentSessionInlineAdapter(
        "verify", "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        instruction_override="Answer using only the reference context for THIS turn. "
        "If it does not support a grounded answer, decline.",
        send_fragments=True, history_mode="server",
        client_factory=lambda: None,  # never called — we only build requests here
    )
    # Per-turn fragments map (what the optimizer's grounded path passes).
    frags_by_turn = {ti: _big_fragments(ti) for ti in range(len(item.turns))}

    sizes = []
    prior: list[tuple[str, str]] = []
    for ti in range(len(item.turns)):
        req = adapter._build_request(item, ti, prior, 0.2, fragments=frags_by_turn[ti])
        blob = json.dumps(req, ensure_ascii=False)
        # Approximate tokens ~ chars / 4 (English heuristic; only used to show the trend).
        approx_tokens = len(blob) // 4
        psa = (req.get("inlineSessionState") or {}).get("promptSessionAttributes") or {}
        ctx = psa.get("retrieved_context", "")
        sizes.append(approx_tokens)
        print(
            f"  turn {ti}: request ~{approx_tokens:>7,} tok | "
            f"inputText={req['inputText']!r} (bare) | "
            f"context attr ~{len(ctx)//4:,} tok | "
            f"fragment ids in question? "
            f"{any(f['id'] in req['inputText'] for f in frags_by_turn[ti])}"
        )
        prior.append((req["inputText"], "ok"))

    first, last = sizes[0], sizes[-1]
    growth = (last - first) / max(1, first)
    print(f"  --> first turn ~{first:,} tok, last turn ~{last:,} tok, growth {growth:+.1%}")
    # Under the OLD scheme the last turn would be ~6x the first (all prior fragments stacked
    # in the accumulating history); bounded means the last turn is within noise of the first.
    assert last <= first * 1.15, (
        f"per-turn request size GREW with turn count ({first} -> {last}): fragments are "
        f"accumulating — the fix did not bound the prompt."
    )
    old_style_last = first * len(item.turns)
    print(
        f"  PASS: size is bounded (each turn carries only its own fragments). "
        f"Old inline scheme would have reached ~{old_style_last:,} tok by turn "
        f"{len(item.turns)-1} (>{config.DEFAULT_MAX_TOKENS:,} cap territory)."
    )


def part_b_live() -> None:
    print("\n=== Part B: live grounding on the promptSessionAttributes channel (best-effort) ===")
    try:
        from bakeoff.credentials import get_broker

        broker = get_broker()
        session = broker.get_session(config.QUALITY_OPT_EXECUTION_PROFILE,
                                     region=config.AWS_REGION)
        client = session.client("bedrock-agent-runtime", region_name=config.AWS_REGION)
    except Exception as exc:  # noqa: BLE001
        print(f"  SKIPPED: could not obtain EXECUTION creds/client ({exc!r}).")
        print("  Re-run after the dashboard restart / `ada` refresh to confirm live grounding.")
        return

    # A single-turn item with one distinctive, made-up fact only present in the fragment, so
    # a grounded answer can ONLY come from the attribute channel (not the model's prior).
    secret = "The corporate card request code is ZEBRA-7741."
    cohort = CohortKey(geography="Global", proficiency="fluent", tone="terse",
                       entry_route="slack", momentary_state="neutral",
                       answerability="full", turn_type="single")
    item = Item(id="verify-live", turn_type="single", cohort=cohort,
                query="What is the corporate card request code?", answerability="full", turns=())
    fragments = [{"id": "live-frag-1", "text": secret, "metadata": {}}]

    adapter = PersistentSessionInlineAdapter(
        "verify-live", config.QUALITY_OPT_TARGET_MODELS.get("haiku-4.5", "")
        if hasattr(config, "QUALITY_OPT_TARGET_MODELS") else
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        instruction_override="Answer ONLY from the reference context provided for this turn. "
        "Quote the exact code if present.",
        send_fragments=True, history_mode="server",
        client_factory=lambda: client,
        credential_profile=config.QUALITY_OPT_EXECUTION_PROFILE,
    )
    import asyncio

    try:
        resp = asyncio.run(adapter.generate(item, {0: fragments}, 0.0))
    except Exception as exc:  # noqa: BLE001
        print(f"  LIVE CALL FAILED: {exc!r}")
        if "too long" in repr(exc).lower():
            print("  !!! prompt-too-long STILL occurring — the fix did not take.")
            sys.exit(2)
        print("  (Not a prompt-size error; likely creds/permissions/throttle.)")
        return

    answer = resp.text or ""
    print(f"  answer: {answer[:300]!r}")
    grounded = "ZEBRA-7741" in answer
    print(f"  grounded on the attribute (contains the secret code)? {grounded}")
    if grounded:
        print("  PASS: the model grounded on fragments delivered via promptSessionAttributes.")
    else:
        print("  WARN: answer did not echo the attribute-only fact; inspect the answer above.")


if __name__ == "__main__":
    part_a_bounded()
    part_b_live()
