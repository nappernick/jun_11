"""
Deterministic identity helpers for the bakeoff harness.

``trial_id`` is load-bearing for resume idempotence (design Property 3): the same
``(model, item_id, rep, pass_name, plan_version)`` MUST always hash to the same
id so a resumed run can diff planned trials against the durable event log and run
only the missing ones. We use a stable SHA-256 over a canonical joined string,
returning a hex-digest prefix.

Pure standard library only (``hashlib``) — safe to import anywhere with no heavy
dependencies.
"""
from __future__ import annotations

import hashlib

# Event schema version, stamped onto every TrialEvent for forward-compat.
SCHEMA_VERSION: str = "1.0"

# Field separator used to build the canonical pre-hash string. Chosen as a
# control character that cannot appear in a model name, item id, or plan version,
# so the joined string is unambiguous (no delimiter collisions between fields).
_FIELD_SEP: str = "\x1f"  # ASCII Unit Separator

# Length of the returned hex-digest prefix. 16 hex chars = 64 bits of the
# SHA-256 digest, ample to avoid collisions across the low-hundreds-of-thousands
# of trials this harness will ever produce, while staying compact in the log.
_TRIAL_ID_HEX_LEN: int = 16


def trial_id(
    model: str,
    item_id: str,
    rep: int,
    pass_name: str,
    plan_version: str,
) -> str:
    """Return a deterministic, stable id for one trial.

    The id is a function only of its inputs: identical inputs always produce the
    identical id (within and across processes/runs), and any difference in any
    field produces a different id with overwhelming probability.

    Args:
        model: candidate model name (the adapter's ``name``).
        item_id: the dataset item id (e.g. ``"b0-q01"`` or ``"c0-s01"``).
        rep: repetition index for this ``(model, item)``.
        pass_name: which pass produced this trial (``wide``/``deep``/
            ``targeted``/``pilot``).
        plan_version: the ``sampling_plan.json`` version that produced the trial.

    Returns:
        A lowercase hex string (prefix of a SHA-256 digest).
    """
    canonical = _FIELD_SEP.join(
        (
            "v1",  # id-scheme version; bump if the canonical form ever changes
            str(model),
            str(item_id),
            str(int(rep)),
            str(pass_name),
            str(plan_version),
        )
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest[:_TRIAL_ID_HEX_LEN]
