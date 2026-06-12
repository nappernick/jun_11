"""
Operator-flow orchestration entrypoint for the model-bakeoff-harness (Task 15).

This module is the **thin seam** that wires the already-built pieces into the
end-to-end operator flow from the design's "Example Usage": load the dataset →
register candidates → run a PILOT on the stratified subsample → SIZE the plan
from the pilot's measured variance → run the FULL run (live UI streaming) →
AGGREGATE into a frontier + a materialized exec report. Crash-resume on
re-invoke is automatic — the runner diffs the append-only log and runs only the
missing trials (design Property 3), so calling :func:`run_bakeoff` twice over the
same paths runs zero new trials the second time.

It deliberately holds **no statistics and no scoring logic of its own** — every
load-bearing computation lives in the modules it composes:

* :class:`bakeoff.dataset.DatasetLoader` — normalize the synthetic corpus into
  uniform :class:`~bakeoff.types.Item`s with cohort vectors and resolved gold.
* :class:`bakeoff.planner.SamplingPlanner` — the stratified subsample, the pilot
  plan, the variance-driven :meth:`~bakeoff.planner.SamplingPlanner.required_reps`
  sizing, serialized to ``sampling_plan.json`` (the experiment is data, AD-6).
* :func:`bakeoff.runner.schedule_run` — maximally-parallel, resumable, resilient
  execution (credential-refresh + auto-pause are reused, not reimplemented).
* :class:`bakeoff.aggregate.AggregationEngine` — the frontier, the per-model and
  per-cohort aggregates, and the materialized ``reports/aggregate_<plan>.json``.
* :mod:`bakeoff.calibration` — judge↔human agreement, surfaced in the provenance
  footer (reported, never gated — Req 14.4).

**Everything is injectable** (Req 12/15): the dataset dir or pre-built items, the
list of model adapters (tests pass :class:`~bakeoff.adapters.mock.MockAdapter`s),
the retrieval client, the scoring pipeline (tests pass
:meth:`ScoringPipeline.offline`), the broker/controller, every on-disk path, the
pilot reps, temperature, target CI, and budget. The defaults wire the *real*
components (the Bedrock candidates, the live ``/retrieve`` client, the real
scoring pipeline, an :class:`~bakeoff.app.SSEBroker`) so a bare ``run_bakeoff()``
runs the real bakeoff, while the integration test drives the identical flow
fully offline with zero Bedrock calls.

**Health gate at start (Req 2.4 / 13.3).** The run is gated on the retrieval
substrate's ``/healthz`` before any trial is scheduled and fails fast with a
clear operator message if the backend is unhealthy.

**Credential-expiry resilience (the "200 then 400, refresh creds and redo"
requirement).** This module does not reimplement it: :func:`schedule_run` already
funnels every downstream call through
:func:`bakeoff.resilience.call_with_resilience` (refresh-and-retry on an
``AUTH_EXPIRED`` burst) and runs the run-wide auto-pause. ``run_bakeoff`` only
forwards the injectable ``refresh_credentials`` callback through.

**Sourcing caveat.** The evaluation metrics and judge-calibration methodology
this flow composes are **general industry practice, not Amazon-internal
guidance** (the internal primary sources the global rigor rule prefers were not
reachable in this environment) — see ``bakeoff/README.md`` and the design's
sourcing note. Re-validate internally before any number defends a decision
upward.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, Union

import bakeoff.config as config
from bakeoff.aggregate import AggregationEngine
from bakeoff.calibration import (
    CalibrationReport,
    load_calibration_set,
    score_calibration_set,
)
from bakeoff.dataset import DatasetLoader
from bakeoff.eventlog import read_events
from bakeoff.planner import DEFAULT_PLAN_VERSION, SamplingPlanner, write_plan
from bakeoff.runner import (
    CompletionBroker,
    RunController,
    RunHealthError,
    schedule_run,
)
from bakeoff.stats import Budget
from bakeoff.types import (
    Aggregate,
    FrontierPoint,
    Item,
    SamplingPlan,
)

__all__ = ["BakeoffResult", "run_bakeoff", "build_argument_parser", "main"]

PathLike = Union[str, "os.PathLike[str]"]


# ---------------------------------------------------------------------------
# Result bundle
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BakeoffResult:
    """Everything the operator flow produced, for inspection / the CLI summary.

    A pure data bundle (no behavior) so callers and tests can assert on the whole
    flow's outcome from one object: the sized plan and where it was written, the
    pilot/full :class:`~bakeoff.runner.RunController`s (their snapshots carry
    status + per-model counts), the speed/quality frontier, the per-model
    composite aggregates, the materialized exec-report path, and the (optional)
    judge↔human calibration report.
    """

    items_count: int
    subsample_size: int
    plan: SamplingPlan
    plan_path: Path
    events_path: Path
    pilot_controller: Optional[RunController]
    full_controller: RunController
    frontier: list[FrontierPoint]
    by_model: list[Aggregate]
    report_path: Path
    calibration: Optional[CalibrationReport] = None
    new_trials: int = 0
    model_names: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Defaults wiring (the real components) — kept lazy so importing this module
# pulls in no boto3/httpx and never touches the network.
# ---------------------------------------------------------------------------
def _default_models() -> list:
    """Build the real Bedrock candidate adapters from ``config.CANDIDATE_MODELS``."""
    from bakeoff.adapters.bedrock import build_candidate_adapters

    return build_candidate_adapters()


def _default_retr():
    """Build the real async ``/retrieve`` client (the held-constant substrate)."""
    from bakeoff.retrieval_client import RetrievalClient

    return RetrievalClient()


def _default_scoring():
    """Build the real layered scoring pipeline (Bedrock judge + Embed v4)."""
    from bakeoff.scoring.pipeline import ScoringPipeline

    return ScoringPipeline()


def _default_broker() -> CompletionBroker:
    """Build the live SSE broker so the dashboard streams during the full run."""
    from bakeoff.app import SSEBroker

    return SSEBroker()


def _coerce_budget(budget: Union[Budget, Mapping[str, int], None]) -> Budget:
    """Coerce ``budget`` to a :class:`Budget`; ``None`` → no clamp (pure stat need)."""
    if budget is None:
        # A very large cap so the sizing reflects the pure statistical need; pass
        # a real Budget to clamp to a trial ceiling (design Example Usage).
        return Budget(max_trials=10**12)
    return Budget.coerce(budget)


# ---------------------------------------------------------------------------
# The orchestration entrypoint
# ---------------------------------------------------------------------------
async def run_bakeoff(
    *,
    # -- dataset (provide exactly one of items / loader / data_dir) --------
    items: Optional[Sequence[Item]] = None,
    loader: Optional[DatasetLoader] = None,
    data_dir: Optional[PathLike] = None,
    # -- candidates + downstream clients -----------------------------------
    models: Optional[Sequence] = None,
    retr=None,
    scoring=None,
    broker: Optional[CompletionBroker] = None,
    # -- planning ----------------------------------------------------------
    planner: Optional[SamplingPlanner] = None,
    plan: Optional[SamplingPlan] = None,
    run_pilot: bool = True,
    pilot_reps: int = config.PILOT_REPS,
    temperature: float = config.DEFAULT_TEMPERATURE,
    target_ci_halfwidth: float = config.TARGET_CI_HALFWIDTH,
    confidence_level: float = config.CONFIDENCE_LEVEL,
    budget: Union[Budget, Mapping[str, int], None] = None,
    plan_version: str = DEFAULT_PLAN_VERSION,
    metric: str = "composite",
    # -- on-disk layout (all injectable; tests pass tmp paths) -------------
    events_path: PathLike = config.TRIAL_EVENTS_PATH,
    pilot_events_path: PathLike = config.PILOT_EVENTS_PATH,
    plan_path: PathLike = config.SAMPLING_PLAN_PATH,
    reports_dir: PathLike = config.REPORTS_DIR,
    # -- report ------------------------------------------------------------
    cohort_dimensions: Sequence[str] = ("momentary_state", "geography"),
    # -- calibration (Req 14.4 reported / 13.2 surfaced; never a gate) -----
    calibration_records: Optional[Sequence] = None,
    calibration_path: Optional[PathLike] = None,
    calibration_judge=None,
    calibration_threshold: Optional[float] = None,
    # -- resilience / control passthrough ----------------------------------
    refresh_credentials: Optional[Callable[[], object]] = None,
    resilience_sleep: Optional[Callable[[float], object]] = None,
    gate_healthz: bool = True,
    **schedule_kwargs,
) -> BakeoffResult:
    """Run the full operator flow end to end and return a :class:`BakeoffResult`.

    Steps (design "Example Usage"); each is delegated to an already-built module:

    1. **Load + normalize** the dataset into uniform :class:`Item`s (skipped if
       ``items`` is supplied directly, as the integration test does).
    2. **Register candidates** (``models``; default = the real Bedrock adapters).
    3. **Health gate** — fail fast with a clear message if ``/healthz`` is not ok.
    4. **PILOT** — :func:`schedule_run` of :meth:`SamplingPlanner.pilot_plan` over
       the stratified subsample at ``pilot_reps`` into ``pilot_events_path``.
       (Skipped when an explicit ``plan`` is provided or ``run_pilot=False``.)
    5. **SIZE the plan** — :meth:`SamplingPlanner.required_reps` from the pilot's
       measured variance → a :class:`SamplingPlan` written to ``plan_path`` (the
       experiment is data, not code — AD-6).
    6. **FULL run** — :func:`schedule_run` of the plan into ``events_path`` with
       the live ``broker`` plugged in so the dashboard streams. Crash-resume is
       automatic: a re-invocation runs only the missing trials (Property 3).
    7. **AGGREGATE** — :class:`AggregationEngine` → the speed/quality frontier,
       per-model aggregates, and a materialized ``aggregate_<plan>.json``.
    8. **CALIBRATION** (optional) — judge↔human agreement is computed and surfaced
       in the report's provenance footer (reported, not gated — Req 14.4).

    Everything is injectable; the defaults wire the real components (including the
    dataset source: when none of ``items`` / ``loader`` / ``data_dir`` is given it
    defaults to a :class:`DatasetLoader` over ``config.DATASET_DIR``, exactly the
    design's Example-Usage default). Returns a :class:`BakeoffResult` capturing the
    plan, controllers, frontier, aggregates, the report path, and the calibration
    report.

    Raises:
        ValueError: if ``run_pilot=False`` without an explicit ``plan`` (no pilot
            means no measured variance to size from), or if the resolved dataset
            produces zero items.
        RunHealthError: if the retrieval substrate is unhealthy at start.
    """
    # --- resolve injectable collaborators (defaults = the real components) ---
    if models is None:
        models = _default_models()
    models = list(models)
    if not models:
        raise ValueError("run_bakeoff: no candidate models to run")
    model_names = [getattr(m, "name", str(m)) for m in models]

    retr = retr if retr is not None else _default_retr()
    scoring = scoring if scoring is not None else _default_scoring()
    broker = broker if broker is not None else _default_broker()
    planner = planner if planner is not None else SamplingPlanner()

    events_path = Path(events_path)
    pilot_events_path = Path(pilot_events_path)
    plan_path = Path(plan_path)
    reports_dir = Path(reports_dir)

    # --- 1. dataset → uniform items -------------------------------------
    items = _resolve_items(items, loader, data_dir)
    if not items:
        raise ValueError("run_bakeoff: dataset produced zero items")

    # --- 3. health gate at start (fail fast with a clear message) -------
    # Done once here so the operator gets a single clear signal before any work;
    # the inner schedule_run calls then skip the redundant probe (gate_healthz
    # is threaded through as False below).
    if gate_healthz:
        await _gate_health(retr)

    # --- 2/4/5. subsample → pilot → size the plan -----------------------
    subsample = planner.build_subsample(items)
    pilot_controller: Optional[RunController] = None

    if plan is None:
        if not run_pilot:
            raise ValueError(
                "run_bakeoff: run_pilot=False requires an explicit plan= (no pilot "
                "means no variance to size from)"
            )
        pilot_plan = planner.pilot_plan(
            temperature=temperature,
            reps=pilot_reps,
            subsample=subsample,
            plan_version=f"{plan_version}-pilot",
            confidence_level=confidence_level,
            target_ci_halfwidth=target_ci_halfwidth,
        )
        pilot_controller = await schedule_run(
            pilot_plan,
            models,
            pilot_events_path,
            broker,
            items=items,
            retr=retr,
            scoring=scoring,
            refresh_credentials=refresh_credentials,
            resilience_sleep=resilience_sleep,
            gate_healthz=False,  # already gated above
            **schedule_kwargs,
        )
        pilot_events = read_events(pilot_events_path)
        plan = planner.required_reps(
            pilot_events,
            target_ci_halfwidth,
            _coerce_budget(budget),
            subsample=subsample,
            temperature=temperature,
            plan_version=plan_version,
            confidence_level=confidence_level,
            metric=metric,
        )

    # The plan is data, not code (AD-6): persist it so the run is replayable and
    # the experiment can be re-shaped by editing the file, not the runner.
    write_plan(plan, plan_path)

    # --- 6. FULL run (resumable; live UI streams via the broker) --------
    full_controller = await schedule_run(
        plan,
        models,
        events_path,
        broker,
        items=items,
        retr=retr,
        scoring=scoring,
        refresh_credentials=refresh_credentials,
        resilience_sleep=resilience_sleep,
        gate_healthz=False,  # already gated above
        **schedule_kwargs,
    )

    # --- 8. CALIBRATION (optional; reported, never gated — Req 14.4) ----
    calibration = _maybe_calibrate(
        calibration_records, calibration_path, calibration_judge, calibration_threshold
    )
    judge_human_agreement = (
        calibration.agreement_for_footer() if calibration is not None else None
    )

    # --- 7. AGGREGATE + FRONTIER + materialized report ------------------
    engine = AggregationEngine(level=confidence_level)
    events = read_events(events_path)
    frontier = engine.frontier(events, quality_metric=metric)
    by_model = engine.aggregate(events, ["model"], metric)
    report_path = engine.materialize(
        events,
        plan.plan_version,
        reports_dir=reports_dir,
        cohort_dimensions=cohort_dimensions,
        judge_human_agreement=judge_human_agreement,
    )

    return BakeoffResult(
        items_count=len(items),
        subsample_size=len(subsample.subsample_item_ids),
        plan=plan,
        plan_path=plan_path,
        events_path=events_path,
        pilot_controller=pilot_controller,
        full_controller=full_controller,
        frontier=frontier,
        by_model=by_model,
        report_path=report_path,
        calibration=calibration,
        new_trials=full_controller.total_done + full_controller.total_errored,
        model_names=model_names,
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _resolve_items(
    items: Optional[Sequence[Item]],
    loader: Optional[DatasetLoader],
    data_dir: Optional[PathLike],
) -> list[Item]:
    """Resolve the item set from exactly one of items / loader / data_dir."""
    if items is not None:
        return list(items)
    if loader is None:
        loader = DatasetLoader(Path(data_dir) if data_dir is not None else None)
    return list(loader.load_items())


async def _gate_health(retr) -> None:
    """Gate the run on the substrate's health, raising a clear message if not ok.

    Reuses the retrieval client's own ``healthz()`` (Req 2.4); the runner gates
    again per-run, but surfacing it here gives the operator one clear up-front
    signal (design Error Scenario 2).
    """
    healthz = getattr(retr, "healthz", None)
    if healthz is None:
        return
    healthy = await healthz()
    if not healthy:
        raise RunHealthError(
            "retrieval substrate is unhealthy at start (GET /healthz did not report "
            "ok). Bring the backend up (e.g. ./run.sh) and re-invoke — crash-resume "
            "will run only the missing trials. No trials were run."
        )


def _maybe_calibrate(
    calibration_records: Optional[Sequence],
    calibration_path: Optional[PathLike],
    calibration_judge,
    calibration_threshold: Optional[float],
) -> Optional[CalibrationReport]:
    """Score the calibration set with the judge if one was supplied (else None).

    Reported-only: agreement is computed and surfaced; poor agreement never raises
    and never blocks the run (Req 14.4). Requires a judge to be injected — the
    integration test passes a stub judge, never real Bedrock.
    """
    records = list(calibration_records) if calibration_records is not None else None
    if records is None and calibration_path is not None:
        records = load_calibration_set(calibration_path)
    if not records:
        return None
    if calibration_judge is None:
        # No judge to score with → skip rather than reach for real Bedrock.
        return None
    kwargs = {}
    if calibration_threshold is not None:
        kwargs["threshold"] = calibration_threshold
    return score_calibration_set(records, calibration_judge, **kwargs)


# ---------------------------------------------------------------------------
# CLI — defaults wire the real components (loopback live UI, Bedrock candidates)
# ---------------------------------------------------------------------------
def build_argument_parser() -> argparse.ArgumentParser:
    """Build the ``run_bakeoff`` CLI parser (the operator's command line)."""
    p = argparse.ArgumentParser(
        prog="python -m bakeoff.main",
        description=(
            "Run the model bake-off end to end: load dataset → pilot → size plan "
            "→ full run (live UI) → aggregate + frontier + report. Crash-resume on "
            "re-invoke is automatic."
        ),
    )
    p.add_argument(
        "--data-dir", default=None,
        help="dataset directory (default: config.DATASET_DIR = data/synthetic/)",
    )
    p.add_argument(
        "--pilot-reps", type=int, default=config.PILOT_REPS,
        help=f"pilot reps per (model,item) on the subsample (default {config.PILOT_REPS})",
    )
    p.add_argument(
        "--temperature", type=float, default=config.DEFAULT_TEMPERATURE,
        help=f"starting temperature, pilot-confirmed (default {config.DEFAULT_TEMPERATURE})",
    )
    p.add_argument(
        "--target-ci", type=float, default=config.TARGET_CI_HALFWIDTH,
        help=f"target CI half-width that sizes reps (default {config.TARGET_CI_HALFWIDTH})",
    )
    p.add_argument(
        "--confidence", type=float, default=config.CONFIDENCE_LEVEL,
        help=f"confidence level for every CI (default {config.CONFIDENCE_LEVEL})",
    )
    p.add_argument(
        "--max-trials", type=int, default=None,
        help="budget clamp on total primary-pass trials (default: no clamp)",
    )
    p.add_argument(
        "--plan-version", default=DEFAULT_PLAN_VERSION,
        help=f"plan version stamped on trials + the report (default {DEFAULT_PLAN_VERSION})",
    )
    p.add_argument(
        "--calibration", default=None,
        help="optional calibration-set JSONL to score for judge↔human agreement",
    )
    p.add_argument(
        "--no-pilot", action="store_true",
        help="skip the pilot and reuse an existing sampling_plan.json (must exist)",
    )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entrypoint: parse args, run the real bakeoff, print a short summary.

    Defaults wire the real components — the live ``/retrieve`` client, the Bedrock
    candidates, the real scoring pipeline, and an :class:`~bakeoff.app.SSEBroker`
    (so the live dashboard at ``http://127.0.0.1:8200/`` streams while the run
    proceeds). Re-invoking resumes from the append-only log.
    """
    args = build_argument_parser().parse_args(argv)
    config.ensure_dirs()

    plan = None
    run_pilot = not args.no_pilot
    if args.no_pilot:
        from bakeoff.planner import read_plan

        plan = read_plan(config.SAMPLING_PLAN_PATH)

    budget = Budget(max_trials=args.max_trials) if args.max_trials else None

    calibration_judge = None
    if args.calibration:
        # Real calibration uses the default (Bedrock) judge; built lazily here so
        # the CLI only reaches for boto3 when a calibration set is actually given.
        from bakeoff.scoring.judge import JudgeScorer

        calibration_judge = JudgeScorer()

    result = asyncio.run(
        run_bakeoff(
            data_dir=args.data_dir,
            plan=plan,
            run_pilot=run_pilot,
            pilot_reps=args.pilot_reps,
            temperature=args.temperature,
            target_ci_halfwidth=args.target_ci,
            confidence_level=args.confidence,
            budget=budget,
            plan_version=args.plan_version,
            calibration_path=args.calibration,
            calibration_judge=calibration_judge,
        )
    )

    _print_summary(result)
    return 0


def _print_summary(result: BakeoffResult) -> None:
    """Print a terse operator summary of the completed flow."""
    print(f"items:            {result.items_count}")
    print(f"subsample:        {result.subsample_size}")
    print(f"plan:             {result.plan.plan_version} -> {result.plan_path}")
    print(f"full run status:  {result.full_controller.status}")
    snap = result.full_controller.snapshot()
    print(f"totals:           {snap['totals']}")
    print(f"report:           {result.report_path}")
    print("frontier (speed p50 ms / quality):")
    for fp in result.frontier:
        flag = "PARETO" if fp.on_pareto_front else "      "
        print(
            f"  [{flag}] {fp.model:<24} "
            f"speed_p50={fp.speed_p50_ms:8.1f}ms  "
            f"quality={fp.quality.point:.3f} "
            f"[{fp.quality.low:.3f}, {fp.quality.high:.3f}]"
        )
    if result.calibration is not None:
        cal = result.calibration
        print(
            f"calibration:      metric={cal.agreement_metric} "
            f"n={cal.n_items} low-agreement dims={cal.low_agreement_dimensions or 'none'}"
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
