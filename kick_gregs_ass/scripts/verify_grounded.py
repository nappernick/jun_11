"""Phase 4 LIVE verification — does the fragment fix recover the scores, and does
per-role credential routing actually hit the right accounts under load?

Runs the REAL optimizer scorer (ResilientScorer -> the fixed JudgeInLoopScorer
._generate_conversation, which now feeds each turn its own retrieved fragments) on a
small grounded sample with the running seed prompt, and reports the SliceScore the
optimizer would see. Compares to the fragment-STARVED baseline measured earlier
(faith 0.60 / corr 0.40 / comp 0.33, triad ~0.44): if correctness/completeness jump,
the model is now actually grounding on the policy.

Also wraps the credential broker to count which PROFILE (account) each session
resolution hits during the run — proving the judge/execution/embed lanes route to
their dedicated accounts under real load (author isn't exercised by scoring alone).

Touches no optimizer store; does not restart anything. Usage:
    PYTHONPATH=. .venv/bin/python scripts/verify_grounded.py
"""
from __future__ import annotations

import asyncio
import collections
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bakeoff import config
from bakeoff import credentials as creds_mod
from bakeoff.quality.dataset import load_multi_turn_items
from bakeoff.quality.optimizer.backends import build_live_backend
from bakeoff.quality.optimizer.v3.scorer import ResilientScorer
from bakeoff.scoring.judge import JUDGE_DIMENSIONS

MODEL = "sonnet-4.6-thinking-off"
N = {"full": 2, "partial": 1, "none": 1}  # small grounded-heavy sample
STARVED = {"faithfulness": 0.60, "correctness": 0.40, "completeness": 0.33, "triad": 0.44}


def _stratified(items):
    buckets = {k: [] for k in N}
    for it in items:
        a = (getattr(it, "answerability", None) or it.cohort.answerability or "full").lower()
        if a in buckets and len(buckets[a]) < N[a]:
            buckets[a].append(it)
    out = []
    for a in ("full", "partial", "none"):
        out.extend(buckets[a])
    return out


def _instrument_routing():
    """Count which profile each broker.get_session / get_credentials call resolves to."""
    broker = creds_mod.get_broker()
    calls: collections.Counter = collections.Counter()
    _orig_session = broker.get_session
    _orig_creds = broker.get_credentials

    def _session(profile=None, *, region=None):
        calls[broker.resolve_profile(profile)] += 1
        return _orig_session(profile, region=region)

    def _credentials(profile=None):
        calls[f"{broker.resolve_profile(profile)} (aoss-sign)"] += 1
        return _orig_creds(profile)

    broker.get_session = _session
    broker.get_credentials = _credentials
    return calls


async def main() -> int:
    seed = (config.QUALITY_OPT_V3_SEEDS_DIR / f"{MODEL}_i0.txt").read_text(encoding="utf-8").strip()
    sample = _stratified(load_multi_turn_items())
    calls = _instrument_routing()

    backend = build_live_backend()
    scorer = ResilientScorer(backend, reps=1)

    print(f"index={config.QUALITY_OPT_OPENSEARCH_ALPHA_INDEX} model={MODEL} "
          f"items={len(sample)} (per-turn grounded fragments NOW fed)\n")

    score = await scorer.score_prompt(
        model=MODEL, instruction=seed, items=sample, prompt_role="champion",
    )

    dims = score.per_dimension_mean or {}
    print("===== GROUNDED SCORE (real scorer, fragments fed per turn) =====")
    print(f"  triad_score        {score.triad_score:.3f}   (starved baseline ~{STARVED['triad']})")
    for d in JUDGE_DIMENSIONS:
        now = float(dims.get(d, 0.0))
        was = STARVED[d]
        print(f"  {d:<14} {now:.3f}   (was {was:.2f} starved   Δ{now - was:+.2f})")
    print(f"  mean_closeness     {score.mean_closeness:.3f}")
    print(f"  abstention_reward  {score.abstention_reward_mean:.3f}")
    print(f"  answered_when_unsure {score.answered_when_unsure_rate:.3f}")
    print(f"  slice_n_convos     {score.n_conversations}")

    print("\n===== CREDENTIAL ROUTING UNDER LOAD (profile -> #session resolutions) =====")
    for profile, n in sorted(calls.items(), key=lambda kv: -kv[1]):
        acct = config.CREDENTIAL_PROFILES.get(profile.split(" ")[0], {}).get("account", "?")
        print(f"  {profile:<22} {n:>4}   account {acct}")

    out = _ROOT / "data" / "bakeoff" / "verify_grounded.json"
    out.write_text(json.dumps({
        "triad_score": score.triad_score,
        "per_dimension": dims,
        "starved_baseline": STARVED,
        "routing": dict(calls),
    }, indent=2))
    print(f"\n-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
