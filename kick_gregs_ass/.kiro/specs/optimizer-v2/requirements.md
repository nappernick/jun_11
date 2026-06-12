# Requirements Document
## **Optimizer v2: Make It Run and Be Visible**

## Introduction

The v2 island-tournament optimizer (coverage-ladder + 2-island coevolution +
tournament/migration, per `docs/OPTIMIZER_V2_DESIGN_NOTES.md`) was built but has
**never once run end-to-end from the dashboard**, and has never displayed live data.
The cause is structural, not algorithmic: v2 was wired *on top of* v1 — sharing the
single `/api/quality/optimize/*` endpoint set and the bake-off SSE stream — so the
Start button never reliably reaches a working launch path and the surface never shows
a live run.

The v2 algorithm core already exists and its offline tests pass (`test_orchestrator_v2`,
`test_tournament`, `test_events_v2`). This is **mostly moving code around**: give v2 its
own start endpoint, its own status endpoint, and **its own dedicated SSE stream**, wire
the Start button to them, and confirm the existing v2 tab shows the run as live. No
rewrite of the optimizer logic.

### This is the first of three separate efforts

1. **(this spec) v2 works.** I can click the button on the v2 tab, it kicks the run
   off, and I can see it running in the front end. That is the whole success bar.
2. **(later, separate spec) v2 front end completely reworked and massively improved.**
3. **(later, separate spec) v1 recreation / extraction work.**

### Scope

**In scope (effort #1 only)**
- A dedicated v2 endpoint group: `POST /api/quality/optimize/v2/start`,
  `GET /api/quality/optimize/v2/status`, and a **dedicated v2 SSE stream** endpoint.
- A dedicated v2 launch path that calls `run_v2(...)` correctly and builds the live
  backend off the event loop (so the dashboard never freezes).
- The existing v2 tab's Start button wired to the v2 start endpoint, and the tab
  reading v2 status + the v2 stream so a launched run is **visibly running**.
- Live v2 runs using the OpenSearch ALPHA retrieval substrate (config values already
  present), with the existing local fallback.
- End-to-end verification that the button launches a run that reaches `running` and
  shows live activity.

**Out of scope (deferred to the later efforts named above)**
- The complete v2 front-end redesign / visual overhaul — effort #2. Effort #1 only
  needs the run to be *visibly running*, not beautiful.
- Any v1 recreation, extraction, endpoint, or tab work — effort #3. v1 already has a
  tab; this spec does not touch v1.
- **SSE stream sharing.** v2 must NOT reuse the bake-off `/api/stream`; it gets its own
  stream. (This is a hard constraint, not a preference.)
- Changing the v2 optimizer algorithm, the statistical spine, the judge, or the data
  universe.
- Adding error handling, configurability, or abstractions beyond what these
  requirements name; internal callers are trusted.

---

## Glossary

- **v2 / island-tournament optimizer**: the coverage-ladder + 2-island coevolution +
  tournament/migration optimizer described in `docs/OPTIMIZER_V2_DESIGN_NOTES.md`,
  entered via `PerModelOrchestrator.run_v2`.
- **v1 optimizer**: the original per-model champion/challenger hill-climb
  (`IterationController`), which already has its own dashboard tab and is out of scope.
- **Island**: one of the two per-model loops (id 0 and 1) that evolve a prompt
  independently with divergent authoring stances.
- **Rung / coverage ladder**: nested, stratified subsets of the tuning slice of growing
  size; a prompt escalates coverage only when it earns it.
- **Tournament / migration**: a head-to-head between the two islands' champions on a
  shared rung; the winner becomes both islands' new baseline.
- **v2 stream**: the dedicated SSE endpoint carrying v2 island/tournament/migration
  events, separate from the bake-off `/api/stream`.
- **ALPHA OpenSearch**: the live `aoss` serverless retrieval collection (account
  948580600005) defined in `docs/OPENSEARCH_ALPHA.md` and `bakeoff/config.py`.

---

## Requirements

### Requirement 1: v2 has its own endpoints, separate from v1 and the bake-off stream

**User Story:** As an operator, I want v2 to run on its own API routes and its own
event stream so it never collides with v1 or the bake-off.

#### Acceptance Criteria
1. WHEN the backend is running THEN it SHALL expose `POST /api/quality/optimize/v2/start`
   and `GET /api/quality/optimize/v2/status`, distinct from the v1 optimizer routes.
2. WHEN the backend is running THEN it SHALL expose a **dedicated v2 SSE stream**
   endpoint that carries the v2 island/tournament/migration events, separate from the
   bake-off `/api/stream`.
3. WHEN `POST /api/quality/optimize/v2/start` is called THEN the backend SHALL launch the
   v2 island-tournament run (`PerModelOrchestrator.run_v2`) and nothing else.
4. WHEN v2 routes/stream are added THEN the existing v1 optimizer routes and the bake-off
   stream SHALL remain present and unchanged in path and shape.

### Requirement 2: the v2 Start button runs end-to-end without freezing

**User Story:** As an operator, I want to press Start on the v2 tab and have a run
actually begin, with the dashboard staying responsive.

#### Acceptance Criteria
1. WHEN I press Start on the v2 tab THEN the browser SHALL POST to the v2 start endpoint
   and the backend SHALL record the request reaching it (observable in the log).
2. WHEN a v2 run is launched with the live backend THEN the backend SHALL build that
   backend off the event loop so status polls, the v2 stream, and page reloads stay
   responsive while the run starts.
3. WHEN the Author and Judge would resolve to the same model on a live v2 run THEN the
   start endpoint SHALL refuse with a clean client error rather than a server error.
4. WHEN a v2 run is already active and Start is pressed again THEN the endpoint SHALL
   refuse with a clean "already running" response and start nothing new.

### Requirement 3: a launched v2 run is visibly running in the front end

**User Story:** As an operator, I want to see that the run started and is doing work —
not a frozen "connecting" — so I know it actually kicked off.

#### Acceptance Criteria
1. WHEN a v2 run is active THEN the v2 tab SHALL show a running lifecycle state (not
   idle, not stuck "connecting").
2. WHEN island/tournament/migration events are emitted on the v2 stream THEN the v2 tab
   SHALL reflect live activity from them (e.g. islands advancing), confirming the run is
   progressing.
3. WHEN the page is reloaded mid-run THEN the v2 tab SHALL reconstruct the running state
   from the v2 status endpoint rather than blanking.
4. WHEN no v2 run has ever been recorded THEN the v2 tab SHALL show an idle/empty state
   and SHALL NOT present residual v1 records as v2 islands.

> Note: effort #1 requires only that the run is *legibly running*. The polished
> presentation of islands, ladders, brackets, and lineage is effort #2.

### Requirement 4: live v2 uses the OpenSearch ALPHA retrieval substrate

**User Story:** As an operator, I want live v2 runs to retrieve from the real ALPHA
OpenSearch collection, since the connection details are in config.

#### Acceptance Criteria
1. WHEN a live v2 run starts with the OpenSearch retrieval backend selected THEN the
   backend SHALL build a SigV4-`aoss`-signed client from the configured ALPHA endpoint
   and credentials and use it for retrieval.
2. WHEN the ALPHA OpenSearch endpoint is unreachable or unconfigured THEN retrieval SHALL
   fall back to the guaranteed-workable local substrate rather than failing the run.
3. WHEN a live v2 run is executing THEN retrieval SHALL be invoked against the selected
   substrate (observable as retrieval activity), confirming end-to-end wiring.

### Requirement 5: verified end-to-end before hand-off

**User Story:** As the owner, I want proof v2 runs the whole way through from the button,
since it has never once worked from the controls available to me.

#### Acceptance Criteria
1. WHEN the work is presented as complete THEN the offline v2 test suites
   (`test_orchestrator_v2`, `test_tournament`, `test_events_v2`, and any new v2 route
   tests) SHALL pass.
2. WHEN the work is presented as complete THEN a v2 run SHALL have been started through
   the v2 start endpoint and observed to reach `running`, emit island events on the v2
   stream, and surface them on the v2 status endpoint — captured as evidence.
3. WHEN v2 routes or launch behavior change THEN the full Python test suite SHALL pass
   (no regressions elsewhere in the harness).
