# model-bakeoff-harness (`bakeoff/`)

The runnable harness that turns the bake-off question into numbers: many candidate
models answering the same FAQ questions over a **held-constant retrieval
substrate**, scored on the balance of **speed** and **quality**, with a confidence
interval on every reported mean.

This README is the **operations** doc — how to run it, the security posture, the
credential-expiry behavior, and the sourcing caveat. For the **experiment** itself
(the question being answered, the synthetic evaluation set, the scoring strategy,
the competitor list), see [`docs/BAKEOFF.md`](../docs/BAKEOFF.md) — the master
document. The two are complementary and do not overlap: `docs/BAKEOFF.md` is *why
and what*; this file is *how to run*. The spec lives under
[`.kiro/specs/model-bakeoff-harness/`](../.kiro/specs/model-bakeoff-harness/).

---

## The single source of truth

Every trial writes exactly one `TrialEvent` (one JSON line) to an append-only log
(`data/bakeoff/trial_events.jsonl`). The live UI, the aggregation engine, and the
exec visualization are all **derived** from that log. This is what makes runs
**replayable** (re-aggregate without re-running models), **crash-resumable**
(resume by diffing planned trials against the log), and **auditable** (one record
behind every number an exec sees).

On-disk layout (all under `data/bakeoff/`, see `bakeoff/config.py`):

| File | What |
|---|---|
| `sampling_plan.json` | the pilot-sized experiment description (data, not code) |
| `pilot_events.jsonl` | pilot trials only (variance measurement) |
| `trial_events.jsonl` | the full-run append-only event log (source of truth) |
| `cache/` | per-scorer content-hash caches (judge / embeddings / retrieval) |
| `reports/aggregate_<plan_version>.json` | the materialized exec report |

---

## Run steps (pilot → plan → full run → aggregate → reports)

Prerequisite: the **retrieval backend** (the shared constant substrate) must be up
— the harness only ever calls `POST /retrieve` and `GET /healthz`, never
re-implements retrieval. From the repo root:

```bash
# 0. Bring up the shared retrieval substrate (separate process; needs Docker +
#    Bedrock auth). The harness calls it over HTTP; it is held constant for every
#    candidate. See the top-level README.
./run.sh

# 1. Run the whole operator flow end to end (pilot → size plan → full run →
#    aggregate → frontier + report). Crash-resume on re-invoke is automatic.
.venv/bin/python -m bakeoff.main

#    Useful flags (all optional; defaults come from bakeoff/config.py):
#      --data-dir data/synthetic/      dataset directory
#      --pilot-reps 10                 pilot reps per (model,item) on the subsample
#      --temperature 0.2               starting temperature (pilot confirms it)
#      --target-ci 0.05                target CI half-width that sizes reps
#      --max-trials 200000             budget clamp on total primary-pass trials
#      --calibration cal.jsonl         score a human-labeled set for judge↔human agreement
#      --no-pilot                      reuse an existing sampling_plan.json (skip the pilot)
```

What each phase does (design "Example Usage"):

1. **Load + normalize** the dataset (`DatasetLoader`) into uniform cohort-tagged
   items with resolved gold (fails loudly on any dangling `gold_node_id`).
2. **Pilot** — `schedule_run` over a stratified subsample (every non-empty cohort
   cell represented) at `--pilot-reps`, into `pilot_events.jsonl`.
3. **Size the plan** — `SamplingPlanner.required_reps` solves the variance
   equation `Var(Ȳ) ≈ σ²_between/n + σ²_within/(n·R)` for the smallest reps per
   stratum hitting `--target-ci`, floors reps at 2, bumps multi-turn strata to ≥
   their single-turn counterparts, clamps to budget, and flags strata whose target
   is **unreachable** with the available items. Written to `sampling_plan.json`.
   *Reps and temperature are chosen by pilot, not by gut.*
4. **Full run** — `schedule_run` over the plan into `trial_events.jsonl`, maximally
   parallel (per-resource `asyncio` semaphores), with the live SSE broker plugged
   in so the dashboard streams.
5. **Aggregate** — `AggregationEngine` produces the speed/quality Pareto frontier,
   per-model and per-cohort aggregates (each with a cluster-bootstrap CI or marked
   insufficient-data), and materializes `reports/aggregate_<plan_version>.json`.

The Python API mirrors the CLI and is fully injectable (`bakeoff/main.py`):

```python
from bakeoff.main import run_bakeoff
result = await run_bakeoff(
    data_dir="data/synthetic/",         # or items=[...] / loader=DatasetLoader(...)
    models=[...],                        # default: the real Bedrock candidates
    retr=...,                            # default: the live /retrieve client
    scoring=...,                         # default: the real layered scoring pipeline
    broker=...,                          # default: an SSEBroker (live UI)
    pilot_reps=10, temperature=0.2, target_ci_halfwidth=0.05,
    calibration_path="cal.jsonl",        # optional judge↔human agreement (reported)
)
```

Re-running a scorer **without re-running models** (e.g. swap the judge): the
per-scorer caches make this cheap — re-score from the stored answers in the event
log and re-aggregate. Changing the experiment (more multi-turn reps, a different
temperature, a tighter CI) is **editing `sampling_plan.json` or re-running the
pilot — never editing the runner**.

### The live dashboard

The FastAPI app (`bakeoff/app.py`) serves the JSON + SSE API and, when built, the
TypeScript dashboard:

```bash
.venv/bin/python -c "from bakeoff.app import serve; serve()"   # binds 127.0.0.1:8200
```

* live monitoring: `http://127.0.0.1:8200/`  (all-models overview + single-model focus)
* exec visualization: served from the same SPA, reading the materialized reports
* API surface: `GET /api/models`, `GET /api/aggregate`, `GET /api/stream` (SSE),
  `POST /api/control/{pause,resume,abort}`, `GET /exec/aggregate`, `GET /healthz`.

The dashboard is a separate **TypeScript + Vite** single-page app under
`bakeoff/ui/` (the one sanctioned npm/Vite use; the Python backend stays free of
npm/npx in its runtime path). It is developed and built independently and consumes
the documented API. When `bakeoff/ui/dist/` exists the backend serves it at `/`;
when it does not, `/` degrades to a small JSON stub so the backend never hard-fails
because the frontend has not been built yet. *(The TS dashboard build — Tasks 13/14
— is out of scope for the backend and may not be present yet.)*

---

## Security posture: loopback, no auth (a conscious, documented choice)

The web app binds to **localhost (loopback) only** by default
(`config.UI_HOST == 127.0.0.1`) and has **no authentication**. That is acceptable
*only* because it is bound to loopback on the operator's own machine: this is a
throwaway local operator tool, not a network service. The synthetic dataset carries
no real PII, no secrets are written to the event log, and model/judge outputs are
treated as **data, never executed**.

**If the app is ever bound to a non-loopback interface, authentication MUST be
added first** — a hard precondition, not a nice-to-have. `bakeoff.app.serve`
enforces it: it refuses to bind to a non-loopback host unless the caller explicitly
asserts auth was added (`allow_non_loopback=True`), so the no-auth posture cannot
silently leak onto a routable interface.

---

## Credential-expiry behavior (the "200, then 400 five minutes later" case)

A full WIDE+DEEP run across several candidates is hours of wall-clock and can
outlive a short-lived STS/Bedrock session. Rather than crashing a multi-hour run
when credentials roll over mid-run, **every Bedrock-touching call**
(model adapters, the judge, the embedding scorer) and the runner funnel through
`bakeoff.resilience.call_with_resilience`:

* an **expired/invalid-credentials** failure (`ExpiredTokenException`,
  `UnrecognizedClientException`, HTTP 401/403, …) triggers a **credential refresh
  then a retry** of the affected call, up to `config.AUTH_MAX_REFRESH_CYCLES`;
* **throttling / transient** failures (429, 5xx) back off and retry **without** a
  refresh;
* **permanent** errors propagate so the trial is recorded with `error` set and the
  run continues — a later resume retries it.

On top of per-call retry, the runner runs a **run-wide auto-pause**: if the
downstream error rate crosses a threshold mid-run, the run drains in-flight work
and returns (`status == "paused"`) rather than burning every remaining trial as an
error. **Recovery is just re-invoking the run** after the systemic problem is fixed
— the runner diffs the append-only log and executes only the missing trials. This
is reused by `bakeoff.main`, never reimplemented there.

---

## Judge calibration (reported, never gated)

`bakeoff/calibration.py` scores a small **human-labeled calibration set** with the
same judge the harness uses and reports **judge↔human agreement per dimension**.
The agreement metric is **Spearman's rank correlation (ρ)** — graded judge/human
scores call for a *correlation*, and Spearman measures *monotonic* (rank) agreement
so it rewards a judge that **orders** answers the way humans do even if its absolute
scale differs (the property that matters for *ranking* candidates). Cohen's kappa
(categorical) and Pearson (linear, scale-sensitive) are rejected for graded scores;
Pearson and MAE are reported alongside ρ for transparency.

Dimensions whose ρ falls below a threshold — or is **undefined** (fewer than two
paired items, or a constant rater) — are flagged **low-agreement** so the composite
can **soften or exclude** them (`CalibrationReport.adjusted_weights`). This is
**reporting only**: poor agreement never raises and never blocks a run. The defined
per-dimension agreements ride in every exec chart's **provenance footer**
(`judge_human_agreement`). Calibration-set format and a tiny fixture:
`bakeoff/tests/fixtures/calibration_set.jsonl`.

---

## Verification

This is **not** a Brazil workspace, so `brazil-recursive-cmd` does not apply. The
canonical green check is the test suite from the repo root in the project `.venv`:

```bash
.venv/bin/python -m pytest bakeoff/tests/ -q
```

Unit + Hypothesis property tests (Correctness Properties P1–P10) + the end-to-end
integration test all run **offline** — deterministic mock adapters, a network-free
retrieval double, a stub judge — so the suite needs no Bedrock and no live backend.

---

## Sourcing caveat (read before any number defends a decision upward)

The global rigor steering rule asks that non-trivial work (architecture,
statistical methodology, evaluation metrics, judge calibration) be grounded in
current **Amazon-internal** primary sources (BuilderHub, internal code search, AWS
Prescriptive Guidance). **Those internal tools were not available in this execution
environment.** The evaluation-metric and judge-calibration choices in this harness —
RAG-evaluation metrics (faithfulness, grounding precision/recall, nDCG/MRR, semantic
similarity), the LLM-as-judge anchoring/debiasing/calibration methodology, the
item-level cluster-bootstrap CI, and Spearman-ρ for judge↔human agreement — are
therefore grounded in current **external** literature and are **general industry
practice, not Amazon-internal guidance** (cited inline in
[`design.md`](../.kiro/specs/model-bakeoff-harness/design.md)).

This harness is sound infrastructure for **choosing** a model on the balance of
speed and quality. Before any number it produces is used to **defend a decision
upward**, the evaluation-metric and judge-calibration choices should be re-validated
against internal guidance.
