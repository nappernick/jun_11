"""Per-Model_Channel SSE emission for the closed-loop prompt optimizer.

This module is design **Component 11 (OptimizerEventEmitter)**. It is the single
seam through which the Optimizer streams its live iteration view into the
existing Quality_Tab, and it does so by riding the **existing**
:class:`bakeoff.app.SSEBroker` *unchanged* (Req 9.7): it neither subclasses,
patches, nor reconfigures the broker, and it never touches the bake-off's own
``trial_completed`` / ``judge_*`` streaming behavior. It only adds **new event
types** (the ``optimizer_*`` constants below) over the same fan-out broker.

Channel isolation (the load-bearing invariant — Req 9.10 / 9.11, design
**Property 19**)
------------------------------------------------------------------------------
The two Target_Models' loops may run concurrently and publish onto the *same*
broker, so their events are physically interleaved on the wire. To let each
``Per_Model_View`` recover *only* its own model's events, **every** payload this
emitter publishes is stamped with ``payload[MODEL_CHANNEL]`` equal to the model
the event describes. A consumer that filters the stream by ``model_channel``
therefore partitions it cleanly into each model's events with none
misattributed — which is exactly design Property 19.

Because the emitter is the *only* writer of these event types and it stamps
``model_channel`` on every single emission (there is no code path that publishes
an ``optimizer_*`` event without going through :meth:`OptimizerEventEmitter.emit`),
the "every emitted event carries a ``model_channel`` equal to the model it
describes" half of Property 19 holds by construction.

Broker contract (duck-typed)
----------------------------
The emitter takes the existing broker as an injected, **duck-typed** object and
calls its synchronous, non-blocking ``publish(event_type: str, payload: dict)``
method — the exact signature exposed by :class:`bakeoff.app.SSEBroker.publish`
(verified against ``bakeoff/app.py``). It does not import the broker class, so a
test double exposing the same ``publish`` method works without modification, and
the real broker is used without being altered in any way.

Non-mutation
------------
:meth:`OptimizerEventEmitter.emit` stamps ``model_channel`` onto a **shallow
copy** of the caller's payload, never the caller's own dict. A caller can build a
payload, hand it to :meth:`emit`, and keep using its dict without discovering a
surprise ``model_channel`` key (or a clobbered one) after the fact.

The convenience methods (:meth:`champion_scored`, :meth:`author_token`,
:meth:`iteration_completed`, :meth:`converged`, :meth:`phase_b`) build the exact
payload shapes documented in the design's "Per-iteration SSE event shape"
section and forward them through :meth:`emit`, so the JSON keys on the wire match
the design's examples 1:1.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

__all__ = [
    "MODEL_CHANNEL",
    "EVENT_CHAMPION_SCORED",
    "EVENT_AUTHOR_TOKEN",
    "EVENT_ITERATION_COMPLETED",
    "EVENT_CONVERGED",
    "EVENT_PHASE_B",
    "EVENT_ISLAND_STEP",
    "EVENT_RUNG_ESCALATED",
    "EVENT_TOURNAMENT",
    "EVENT_MIGRATION",
    "EVENT_AUDIT_FLAG",
    "OPTIMIZER_EVENT_TYPES",
    "OptimizerEventEmitter",
]

#: The payload key every optimizer event is stamped with so a ``Per_Model_View``
#: can filter the shared stream down to its own model (Req 9.10 / 9.11).
MODEL_CHANNEL = "model_channel"

# -- New SSE event *types* (names only; they ride the existing broker) --------
#: Champion (or challenger) was scored on the slice — triad + CI + per-dimension.
EVENT_CHAMPION_SCORED = "optimizer_champion_scored"
#: A streamed chunk of the Author's reasoning/rationale (Req 9.3).
EVENT_AUTHOR_TOKEN = "optimizer_author_token"
#: One iteration finished — accept/reject, gain, new champion state + diff.
EVENT_ITERATION_COMPLETED = "optimizer_iteration_completed"
#: Phase A converged for a model — the iteration index and stop reason.
EVENT_CONVERGED = "optimizer_converged"
#: Phase B validation result for the converged champion (the final number).
EVENT_PHASE_B = "optimizer_phase_b"

# -- v2 island-tournament event types -----------------------------------------
#: One island completed a scoring step at its current rung.
EVENT_ISLAND_STEP = "optimizer_island_step"
#: An island escalated to a higher rung on the coverage ladder.
EVENT_RUNG_ESCALATED = "optimizer_rung_escalated"
#: A head-to-head tournament round between two islands.
EVENT_TOURNAMENT = "optimizer_tournament"
#: Post-tournament migration — winner prompt becomes both islands' baseline.
EVENT_MIGRATION = "optimizer_migration"

# -- cross-family audit event type (optimizer-cross-family-eval, Req 3.5) -----
#: A periodic cross-family Audit_Judge re-scored the winner and the proxy-vs-audit ranking
#: divergence exceeded the configured threshold — a potential self-preference condition.
EVENT_AUDIT_FLAG = "optimizer_audit_flag"

#: All optimizer event types, for consumers/tests that want to allow-list them.
OPTIMIZER_EVENT_TYPES: frozenset[str] = frozenset(
    {
        EVENT_CHAMPION_SCORED,
        EVENT_AUTHOR_TOKEN,
        EVENT_ITERATION_COMPLETED,
        EVENT_CONVERGED,
        EVENT_PHASE_B,
        EVENT_ISLAND_STEP,
        EVENT_RUNG_ESCALATED,
        EVENT_TOURNAMENT,
        EVENT_MIGRATION,
        EVENT_AUDIT_FLAG,
    }
)


class OptimizerEventEmitter:
    """Stamp every optimizer SSE payload with its ``model_channel`` and publish it.

    The emitter wraps the **existing** broker (duck-typed on a synchronous
    ``publish(event_type, payload)`` method, as exposed by
    :class:`bakeoff.app.SSEBroker`) and is the sole writer of the ``optimizer_*``
    event types. It does not modify the broker or the bake-off's streaming
    behavior (Req 9.7); it only fans new event *types* through the same seam.

    Every publish goes through :meth:`emit`, which stamps
    ``payload[MODEL_CHANNEL] = model`` on a shallow copy of the payload. That is
    what makes design Property 19 hold by construction: there is no path to emit
    an optimizer event without a ``model_channel`` equal to the model it
    describes, so filtering the shared stream by ``model_channel`` partitions it
    cleanly per model.
    """

    __slots__ = ("_broker",)

    def __init__(self, broker: Any) -> None:
        """Wrap the existing broker.

        :param broker: The existing SSE broker, duck-typed: it only needs a
            synchronous ``publish(event_type: str, payload: dict) -> None`` method
            (the contract of :class:`bakeoff.app.SSEBroker`). The broker is used
            as-is and never modified.
        """
        self._broker = broker

    # -- the one publish path (stamps model_channel; never mutates caller) ----
    def emit(self, event_type: str, model: str, payload: Mapping[str, Any]) -> None:
        """Stamp ``model_channel`` and publish one event over the existing broker.

        Stamps ``payload[MODEL_CHANNEL] = model`` so each ``Per_Model_View`` can
        filter to its own model and the two models' streams never interleave
        ambiguously (Req 9.10 / 9.11; design Property 19). The stamp is applied to
        a **shallow copy** of ``payload`` so the caller's own dict is never
        mutated — the caller can keep using its dict unchanged after this call.

        The copied-and-stamped payload is then handed to the existing broker's
        synchronous ``publish(event_type, payload)`` method unchanged; the broker
        and the bake-off's streaming behavior are untouched (Req 9.7).

        :param event_type: One of the ``optimizer_*`` event-type constants.
        :param model: The Target_Model this event describes; becomes the
            ``model_channel`` value the event is stamped with.
        :param payload: The event body. Copied before stamping; not mutated.
        """
        stamped: dict[str, Any] = dict(payload)
        stamped[MODEL_CHANNEL] = model
        self._broker.publish(event_type, stamped)

    # -- convenience builders for the documented payload shapes ---------------
    # Each builds the exact JSON shape from design's "Per-iteration SSE event
    # shape" section and forwards it through emit (which adds model_channel).

    def champion_scored(
        self,
        *,
        model: str,
        phase: str,
        iteration_index: int,
        role: str,
        triad: float,
        ci_half_width: float,
        ci_low: float,
        ci_high: float,
        per_dimension: Mapping[str, float],
        abstention_reward_mean: float,
        answered_when_unsure_rate: float,
        retrieval_backend: str,
        mean_closeness: float,
        n_conversations: int,
        island_id: int | None = None,
    ) -> None:
        """Emit ``optimizer_champion_scored`` (a scored champion or challenger).

        Matches the design payload shape: it carries an explicit ``model`` key in
        addition to the ``model_channel`` stamp emit adds, plus the triad score,
        its 95% CI, the per-dimension breakdown (Req 2.6), the abstention-behavior
        summary (``abstention_reward_mean`` / ``answered_when_unsure_rate`` —
        Req 14.2), the held-constant ``retrieval_backend`` name (Req 16.1), the
        secondary ``mean_closeness`` cross-check (Req 2.3), and the slice size.

        ``role`` is ``"champion"`` or ``"challenger"`` so a view can label which
        prompt the score belongs to.

        ``island_id`` (v2 only) attributes the score to a specific island so a
        Per_Model_View can route it to the right island lane even though both
        islands of a model share one Model_Channel. It is ``None`` for the v1
        single-loop controller (which has no islands); v1 consumers ignore it.
        """
        self.emit(
            EVENT_CHAMPION_SCORED,
            model,
            {
                "model": model,
                "phase": phase,
                "iteration_index": iteration_index,
                "role": role,
                "triad": triad,
                "ci_half_width": ci_half_width,
                "ci_low": ci_low,
                "ci_high": ci_high,
                "per_dimension": dict(per_dimension),
                "abstention_reward_mean": abstention_reward_mean,
                "answered_when_unsure_rate": answered_when_unsure_rate,
                "retrieval_backend": retrieval_backend,
                "mean_closeness": mean_closeness,
                "n_conversations": n_conversations,
                "island_id": island_id,
            },
        )

    def author_token(
        self,
        *,
        model: str,
        iteration_index: int,
        delta: str,
        island_id: int | None = None,
    ) -> None:
        """Emit ``optimizer_author_token`` (one streamed chunk of Author reasoning).

        ``delta`` is the partial rationale text as it streams from the Author
        (Req 9.3); the Quality_Tab appends these to render the live reasoning.

        ``island_id`` (v2 only) attributes the streamed reasoning to a specific
        island lane; ``None`` for the v1 single-loop controller.
        """
        self.emit(
            EVENT_AUTHOR_TOKEN,
            model,
            {
                "iteration_index": iteration_index,
                "delta": delta,
                "island_id": island_id,
            },
        )

    def iteration_completed(
        self,
        *,
        model: str,
        iteration_index: int,
        challenger_triad: float | None,
        challenger_ci_half_width: float | None,
        gain_absolute: float | None,
        gain_percent: float | None,
        accepted: bool,
        consecutive_non_improving: int,
        champion_instruction: str,
        prompt_diff: str,
        lookback_version_ids: Sequence[str],
        island_id: int | None = None,
    ) -> None:
        """Emit ``optimizer_iteration_completed`` (accept/reject + new champion state).

        Carries the challenger triad + CI, the gain reported both ways
        (absolute delta and percentage — Req 5.4), the accept/reject decision and
        the resulting convergence counter, the full current champion prompt text,
        the unified diff against the prior version, and the lookback version ids
        (Req 8.5; ≥ several versions). ``challenger_*`` / ``gain_*`` are ``None``
        for an iteration that produced no usable challenger.

        ``island_id`` (v2 only) attributes the completed iteration — and the new
        champion prompt text it carries — to a specific island lane; ``None`` for
        the v1 single-loop controller.
        """
        self.emit(
            EVENT_ITERATION_COMPLETED,
            model,
            {
                "iteration_index": iteration_index,
                "challenger_triad": challenger_triad,
                "challenger_ci_half_width": challenger_ci_half_width,
                "gain_absolute": gain_absolute,
                "gain_percent": gain_percent,
                "accepted": accepted,
                "consecutive_non_improving": consecutive_non_improving,
                "champion_instruction": champion_instruction,
                "prompt_diff": prompt_diff,
                "lookback_version_ids": list(lookback_version_ids),
                "island_id": island_id,
            },
        )

    def converged(
        self, *, model: str, converged_iteration: int, stop_reason: str
    ) -> None:
        """Emit ``optimizer_converged`` (Phase A stopped for this model).

        Reports the iteration at which convergence was reached and why Phase A
        stopped (Req 6.6).
        """
        self.emit(
            EVENT_CONVERGED,
            model,
            {
                "converged_iteration": converged_iteration,
                "stop_reason": stop_reason,
            },
        )

    def phase_b(
        self,
        *,
        model: str,
        triad: float,
        ci_half_width: float,
        n_conversations: int,
    ) -> None:
        """Emit ``optimizer_phase_b`` (the final validation number on the held-out set).

        This is the converged champion's triad + CI on the Validation_Set, which
        is always the final reported performance for a model (Req 7.5).
        """
        self.emit(
            EVENT_PHASE_B,
            model,
            {
                "triad": triad,
                "ci_half_width": ci_half_width,
                "n_conversations": n_conversations,
            },
        )

    # -- v2 island-tournament convenience methods -----------------------------

    def island_step(
        self, model: str, island_id: int, rung_index: int,
        champion_score: float, ci_half_width: float, state: str,
    ) -> None:
        """Emit ``optimizer_island_step`` (one island scored at its current rung)."""
        self.emit(
            EVENT_ISLAND_STEP,
            model,
            {
                "island_id": island_id,
                "rung_index": rung_index,
                "champion_score": champion_score,
                "ci_half_width": ci_half_width,
                "state": state,
            },
        )

    def rung_escalated(
        self, model: str, island_id: int, from_rung: int, to_rung: int,
    ) -> None:
        """Emit ``optimizer_rung_escalated`` (island promoted to a higher rung)."""
        self.emit(
            EVENT_RUNG_ESCALATED,
            model,
            {"island_id": island_id, "from_rung": from_rung, "to_rung": to_rung},
        )

    def tournament(
        self, model: str, round: int, island_a: dict, island_b: dict,
        shared_rung: int, winner: int,
    ) -> None:
        """Emit ``optimizer_tournament`` (head-to-head between two islands)."""
        self.emit(
            EVENT_TOURNAMENT,
            model,
            {
                "round": round,
                "island_a": island_a,
                "island_b": island_b,
                "shared_rung": shared_rung,
                "winner": winner,
            },
        )

    def migration(
        self, model: str, round: int, winning_prompt_version_id: str,
    ) -> None:
        """Emit ``optimizer_migration`` (winner prompt migrated to both islands)."""
        self.emit(
            EVENT_MIGRATION,
            model,
            {"round": round, "winning_prompt_version_id": winning_prompt_version_id},
        )

    def audit_flag(
        self, *, model: str, round: int, report: Mapping[str, Any],
    ) -> None:
        """Emit ``optimizer_audit_flag`` (a potential self-preference condition, Req 3.5).

        Carries the audit ``round`` and the :meth:`bakeoff.quality.optimizer.audit.\
DivergenceReport.to_dict` payload (the proxy/audit scores, the computed divergence, the
        threshold, and the ``flagged`` outcome) so a view can surface that the cross-family
        Audit_Judge disagreed with the Opus proxy ranking more than the configured threshold.
        """
        self.emit(
            EVENT_AUDIT_FLAG,
            model,
            {"round": round, "report": dict(report)},
        )
