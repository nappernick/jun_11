"""
Deterministic identity helpers for the closed-loop prompt optimizer.

These ids are load-bearing for resume idempotence (design Property 16, Req 10.2/10.3):
the same inputs MUST always hash to the same id (within and across processes/runs), and
any change to any input field MUST change the id with overwhelming probability. That is
what lets a resumed Phase-A run skip iterations whose ``IterationRecord`` is already
durable and pick up from the first incomplete unit of work.

The hashing approach mirrors ``bakeoff/ids.py`` exactly: a stable SHA-256 over a canonical
string built by joining the fields with the ASCII Unit Separator, returning a lowercase
hex-digest prefix of the same length. Each helper carries its own immutable namespace
version prefix (``"optv1"`` / ``"optpromptv1"``) so the optimizer's ids never collide with
the bake-off's ``trial_id`` namespace and so the scheme can be versioned independently.

``gen_trial_id`` does not re-implement the trial-id scheme; it delegates to
``bakeoff.ids.trial_id`` so the optimizer's per-trial ids are produced by the exact same
function the rest of the harness uses, namespaced via ``pass_name``/``plan_version``.

Pure standard library only (``hashlib``) plus the harness's own ``bakeoff.ids`` — safe to
import anywhere with no heavy dependencies.
"""
from __future__ import annotations

import hashlib

from bakeoff import ids as _bakeoff_ids

__all__ = [
    "iteration_id",
    "prompt_version_id",
    "gen_trial_id",
]

# Field separator used to build the canonical pre-hash string. Mirrors
# ``bakeoff.ids._FIELD_SEP``: an ASCII control character (Unit Separator) that cannot
# appear in a model name, phase, role, or item id, so the joined string is unambiguous
# (no delimiter collisions between fields).
_FIELD_SEP: str = "\x1f"

# Length of the returned hex-digest prefix. Mirrors ``bakeoff.ids._TRIAL_ID_HEX_LEN``:
# 16 hex chars = 64 bits of the SHA-256 digest, ample to avoid collisions across the
# low-hundreds-of-thousands of records this harness will ever produce while staying
# compact in the log.
_ID_HEX_LEN: int = 16

# Plan-version tag stamped onto every optimizer trial id, distinguishing optimizer trials
# from bake-off trials within the shared ``bakeoff.ids.trial_id`` namespace.
_OPT_PLAN_VERSION: str = "quality-opt-v1"


def _hash_prefix(namespace: str, *fields: str) -> str:
    """Return a stable lowercase hex-digest prefix over a namespaced field tuple.

    The fields are joined with ``_FIELD_SEP`` into a single canonical string (the
    ``namespace`` first as an immutable scheme tag), SHA-256 hashed, and truncated to
    ``_ID_HEX_LEN`` hex chars — identical hashing discipline to ``bakeoff.ids.trial_id``.
    """
    canonical = _FIELD_SEP.join((namespace, *fields))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest[:_ID_HEX_LEN]


def iteration_id(model: str, phase: str, iteration_index: int) -> str:
    """Return a deterministic, stable id for one optimizer iteration.

    This is the **resume key** (Req 10.2/10.3): an iteration whose ``IterationRecord`` is
    durably present is skipped on resume. Identical inputs always produce the identical id;
    any difference in ``model``, ``phase``, or ``iteration_index`` produces a different id
    with overwhelming probability.

    Args:
        model: the Target_Model whose loop this iteration belongs to.
        phase: the optimizer phase (e.g. ``"A"``).
        iteration_index: the iteration index within the model's loop (``0`` = seed).

    Returns:
        A lowercase hex string (16-char prefix of a SHA-256 digest).
    """
    return _hash_prefix("optv1", str(model), str(phase), str(int(iteration_index)))


def prompt_version_id(model: str, iteration_index: int) -> str:
    """Return a deterministic, stable id for one prompt version.

    Identifies the prompt produced at a given iteration of a model's loop, for the
    audit version-history lookback. Identical inputs always produce the identical id;
    any difference in ``model`` or ``iteration_index`` changes the id.

    Args:
        model: the Target_Model the prompt belongs to.
        iteration_index: the iteration index that produced this prompt version.

    Returns:
        A lowercase hex string (16-char prefix of a SHA-256 digest).
    """
    return _hash_prefix("optpromptv1", str(model), str(int(iteration_index)))


def gen_trial_id(model: str, item_id: str, rep: int, role: str, phase: str) -> str:
    """Return a deterministic id for one optimizer answer-generation trial.

    Delegates to ``bakeoff.ids.trial_id`` so optimizer trials are produced by the exact
    same scheme the rest of the harness uses, namespaced into the optimizer's own
    ``pass_name``/``plan_version`` so they never collide with bake-off trials. Identical
    inputs always produce the identical id; any field change changes the id.

    Args:
        model: the Target_Model generating the answer.
        item_id: the dataset item (conversation) id.
        rep: the repetition index for this ``(model, item)``.
        role: which prompt produced this trial (``"champion"`` | ``"challenger"``).
        phase: the optimizer phase (e.g. ``"A"`` | ``"B"``).

    Returns:
        A lowercase hex string (prefix of a SHA-256 digest), from ``bakeoff.ids.trial_id``.
    """
    return _bakeoff_ids.trial_id(
        model=model,
        item_id=item_id,
        rep=rep,
        pass_name=f"opt-{phase}-{role}",
        plan_version=_OPT_PLAN_VERSION,
    )
