"""Closed-loop prompt optimizer package.

A self-contained champion/challenger study that replaces the one-shot quality
prompt selector. It runs a judge-scored optimization loop (retrieval-always, with
abstention as a primary scored behavior) over a held-out tuning slice, then
validates the converged champion on the reserved complement at higher reps.

All optimizer modules live under this package; its configuration, append-only
store paths, and the minimal OVERRIDDEN inline template live in
``bakeoff.config`` (the ``QUALITY_OPT_*`` constants).
"""
