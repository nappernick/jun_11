"""
Significance statistics and confidence-interval math for the closed-loop prompt optimizer.

This module is the pure, deterministic numerical core behind the optimizer's promotion
decision (design "Significance threshold and CI math"). It contains four side-effect-free
functions and one small helper — no I/O, no network, no global state — so they can be
exercised exhaustively by property-based tests and reused identically by the in-loop
scorer, the promotion decider, and the Phase-B validator.

The decision metric is the **per-conversation triad score** (mean over a conversation's
turns of the judge's per-turn ``overall``, itself the mean of
faithfulness/correctness/completeness). For a slice of ``n`` conversations with
between-conversation standard deviation ``s``, the 95% confidence-interval half-width of
the slice mean is::

    half_width = z * s / sqrt(n)            # z = 1.95996... for the 95% level

and the promotion test keys on the **absolute** triad delta only:

    promote  iff  (challenger - champion) >= threshold

The percentage gain is reported alongside for human readability but never enters the
decision (design Property 8 / Req 5.4, 5.5).

Methodology sourcing caveat (carried verbatim in intent from ``requirements.md``,
``design.md`` and ``bakeoff/README.md``): the noise-floor characterization that motivates
the default threshold — a between-conversation triad SD of ``s ≈ 0.24`` (whence
``half_width ≈ 1.96 * 0.24 / sqrt(60) ≈ 0.0607`` on the default ~60-conversation tuning
slice, so the 0.05 default threshold sits just inside that noise floor) — is grounded in
this repo's own ~900 observed Opus verdicts and in external/industry RAG-evaluation
practice, **not** in Amazon-internal primary sources (BuilderHub Golden Path, internal
code search, AWS Prescriptive Guidance were unavailable when the methodology was set).
Re-validate any judge-derived number against internal guidance before using it to defend
a decision upward. The CI formula itself (a normal-approximation half-width with the exact
z computed via :class:`statistics.NormalDist`) is standard textbook statistics.

The threshold and confidence-level defaults are imported from :mod:`bakeoff.config`
(``QUALITY_OPT_SIGNIFICANCE_THRESHOLD = 0.05`` and ``CONFIDENCE_LEVEL = 0.95``) and are
never redefined here.

Pure standard library only (``math`` + ``statistics``) plus ``bakeoff.config`` for the
default constants — safe to import anywhere with no heavy dependencies.
"""
from __future__ import annotations

import math
import statistics
from typing import Sequence

from bakeoff import config

__all__ = [
    "between_conversation_sd",
    "ci_half_width",
    "is_significant",
    "gain_report",
]


def between_conversation_sd(conv_triads: Sequence[float]) -> float:
    """Return the sample standard deviation (``ddof=1``) across per-conversation triads.

    This is the between-conversation spread ``s`` that drives the CI half-width
    (design "Significance threshold and CI math", Req 5.3). The sample standard deviation
    (Bessel-corrected, ``ddof=1``) is the right estimator because the per-conversation
    triad scores are themselves a sample of the slice's conversations, not the whole
    population.

    A standard deviation is undefined for fewer than two observations, so this returns
    ``0.0`` for ``n < 2`` rather than raising — a degenerate single-conversation (or empty)
    slice has no measurable between-conversation spread, and a ``0.0`` spread flows through
    :func:`ci_half_width` to a ``0.0`` half-width, which is the correct "no resolvable
    noise from this slice" signal.

    Args:
        conv_triads: one triad score per conversation in the slice (each on the 0..1
            scale). Order is irrelevant; only the multiset of values matters.

    Returns:
        The ``ddof=1`` sample standard deviation as a non-negative float, or ``0.0`` when
        fewer than two values are supplied.
    """
    values = list(conv_triads)
    if len(values) < 2:
        return 0.0
    return statistics.stdev(values)


def _z_for_level(level: float) -> float:
    """Return the two-sided normal critical value ``z`` for a confidence ``level``.

    For a confidence level ``L`` the two-sided critical value is the
    ``1 - (1 - L) / 2`` quantile of the standard normal distribution. Computed exactly
    via :class:`statistics.NormalDist` (stdlib), so the canonical 95% level yields
    ``z ≈ 1.95996`` (not the rounded ``1.96``) and any other common level
    (e.g. 0.90 → ``≈ 1.64485``, 0.99 → ``≈ 2.57583``) is handled exactly with no lookup
    table to maintain.

    Args:
        level: the confidence level, strictly between 0 and 1 (e.g. ``0.95``).

    Returns:
        The non-negative two-sided ``z`` critical value for ``level``.

    Raises:
        ValueError: if ``level`` is not strictly within ``(0, 1)`` (a confidence level of
            0 or 1, or outside the unit interval, has no finite two-sided critical value).
    """
    if not (0.0 < level < 1.0):
        raise ValueError(f"confidence level must be in the open interval (0, 1); got {level!r}")
    # Upper-tail quantile for a two-sided interval at this level.
    upper_quantile = 1.0 - (1.0 - level) / 2.0
    return statistics.NormalDist().inv_cdf(upper_quantile)


def ci_half_width(
    sd: float,
    n_conversations: int,
    level: float = config.CONFIDENCE_LEVEL,
) -> float:
    """Return the confidence-interval half-width of a slice-mean triad score.

    Implements the normal-approximation half-width ``z * sd / sqrt(n)`` (design
    "Significance threshold and CI math", Req 5.3/5.8), where ``z`` is the exact two-sided
    critical value for ``level`` (``≈ 1.95996`` at the default 0.95). This is what
    quantifies the judge's measurement noise on a given slice size: e.g. with
    ``sd = 0.24`` and ``n = 60`` the half-width is ``≈ 0.0607``, so a gain smaller than
    that is within the noise floor. Raising ``n`` (a bigger slice, Req 5.8) lowers the
    half-width, tightening the interval to resolve smaller gains.

    The half-width is non-increasing in ``n_conversations`` and is exactly ``0.0`` when
    ``sd == 0`` (no spread → no interval), satisfying design Property 7.

    Args:
        sd: the between-conversation standard deviation ``s`` (>= 0), typically from
            :func:`between_conversation_sd`.
        n_conversations: the number of conversations in the slice the mean is over.
        level: the confidence level for the interval; defaults to
            :data:`bakeoff.config.CONFIDENCE_LEVEL` (0.95).

    Returns:
        The CI half-width as a non-negative float. Returns ``float('inf')`` as a
        documented sentinel when ``n_conversations <= 0`` (a half-width is undefined with
        no conversations — the interval is unbounded), and ``0.0`` when ``sd == 0``.
    """
    if n_conversations <= 0:
        return float("inf")
    z = _z_for_level(level)
    return z * sd / math.sqrt(n_conversations)


def is_significant(champion: float, challenger: float, threshold: float) -> bool:
    """Return whether the challenger beats the champion by at least ``threshold``.

    This is the optimizer's **sole promotion predicate** (design Property 1, Req 1.6 /
    5.1 / 5.5 / 5.6): the decision metric is the *absolute* triad delta on the 0..1 scale,
    never closeness and never a percentage. The challenger is promoted **iff**
    ``(challenger - champion) >= threshold``; otherwise the champion is retained.

    Args:
        champion: the current champion's triad score (0..1).
        challenger: the candidate challenger's triad score (0..1).
        threshold: the minimum absolute triad gain that counts as significant, e.g.
            :data:`bakeoff.config.QUALITY_OPT_SIGNIFICANCE_THRESHOLD` (0.05).

    Returns:
        ``True`` iff the challenger should be promoted to champion.
    """
    return (challenger - champion) >= threshold


def gain_report(prev: float, new: float) -> dict:
    """Report an iteration's gain as both an absolute delta and a percentage.

    Every reported gain carries both representations (design Property 8, Req 5.4): the
    absolute triad delta — which the promotion/stop decision keys on — and the percentage
    relative to the previous champion, which is reported only for human readability and
    never decides anything (Req 5.5). Because the decision uses ``absolute_delta`` alone,
    two iterations with the same absolute delta but different percentages produce the same
    decision.

    The percentage is undefined when the previous score is non-positive (division by zero
    / a meaningless ratio), so it is reported as ``float('inf')`` in that case, mirroring
    the design's reference implementation.

    Args:
        prev: the previous champion's triad score.
        new: the new (challenger / promoted) triad score.

    Returns:
        A dict ``{"absolute_delta": new - prev, "percent_delta": ...}`` where
        ``percent_delta`` is ``(new - prev) / prev * 100`` when ``prev > 0`` and
        ``float('inf')`` otherwise.
    """
    delta = new - prev
    percent_delta = (delta / prev * 100.0) if prev > 0 else float("inf")
    return {"absolute_delta": delta, "percent_delta": percent_delta}
