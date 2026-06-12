"""
Optimizer V3 — the hardened, LIVE-ONLY rebuild of the v2 island-tournament loop.

Everything V3 lives in this package and writes to its own ``QUALITY_OPT_V3_*`` durable
paths, so v2's code, config, and data are never touched. The algorithm semantics are
v2's (two stance-diverged islands per model on a coverage ladder, periodic tournaments
with migration, Phase B on the reserved complement) — what changes is the failure
envelope and the concurrency, per the v2 post-mortem
(``data/opt_v2_instrumented.log``: both real runs died on an unhandled AOSS 403 at the
~1h credential wall, because one item-level exception had an unbroken path to the top
of the run):

* **Guarded calls** (:mod:`.guards`) — every external call gets a hard timeout +
  classify/backoff/retry on top of the clients' internal auth healing.
* **Contained failures** (:mod:`.scorer`, :mod:`.island`) — a failed turn fails only
  its conversation; an iteration collates the surviving conversations (skipped when
  fewer than ``config.QUALITY_OPT_V3_MIN_SUCCESS_FRACTION`` survive after one batch
  retry); a repeatedly-failing island is marked dead; a model fails only when all its
  islands die; other models keep running.
* **Concurrency** (:mod:`.orchestrator`) — models always run concurrently; the two
  islands of a model step concurrently in waves (so tournament semantics still hold);
  within a scoring pass conversations pipeline generate→judge per item under
  per-resource semaphores instead of v2's two global phase barriers.
* **Resume** — fine-grained durable records (v2's store schema, v3 paths) plus a
  run-state sentinel so a completed Phase A is never re-entered.
"""
from bakeoff.quality.optimizer.v3.guards import GuardedCallError, guarded_call
from bakeoff.quality.optimizer.v3.scorer import ConversationFailure, IterationSkipped, ResilientScorer
from bakeoff.quality.optimizer.v3.island import ResilientIslandLoop
from bakeoff.quality.optimizer.v3.orchestrator import V3Orchestrator
from bakeoff.quality.optimizer.v3.backends import build_v3_backend

__all__ = [
    "GuardedCallError",
    "guarded_call",
    "ConversationFailure",
    "IterationSkipped",
    "ResilientScorer",
    "ResilientIslandLoop",
    "V3Orchestrator",
    "build_v3_backend",
]
