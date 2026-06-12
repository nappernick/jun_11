"""
Prompt Bench — a fixed A/B/C/D prompt leaderboard, fully independent of the optimizers.

Scores a handful of hand-written candidate prompts for ONE Target_Model on a fixed,
deterministic, held-out 24-conversation sample, over the SAME live stack the optimizer
uses (AOSS retrieval + Rerank v4 + the answerability-aware Opus judge + confident-wrong
faithfulness gate) — but with NOTHING shared with the optimizer v3 run so the two can run
concurrently without interfering:

* its OWN Bedrock credential profile (``config.PROMPT_BENCH_PROFILE`` → account
  299635194521) for target generation, the Opus judge, and Embed v4, so it never draws on
  the alpha Bedrock quota a live v3 run uses (retrieval/AOSS stays on alpha, where the
  collection lives — a separate service quota);
* its OWN per-resource concurrency semaphores (``promptbench.scorer``), so it never takes
  judge/model slots from the optimizer's global pool;
* its OWN SSE broker + durable stores (``config.PROMPT_BENCH_*``), archived (never
  destroyed) on reset.

This package contains a near-copy of the optimizer's resilient scorer rather than importing
and mutating it, precisely so the optimizer v3 scoring path is left untouched.
"""
