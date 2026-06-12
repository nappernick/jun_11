# Optimizer v2 — coverage-ladder + island-tournament (design notes, NOT YET BUILT)

Captured from owner direction on 2026-06-04. This is the agreed structure for the
next iteration of the closed-loop prompt optimizer. **Nothing here is implemented
yet** — the current single-hill-climb run is still live; build begins only after
the owner reviews the 20-minute observation window and says go.

## Why (the problem with v1)

v1 scores every attempted prompt against the full tuning slice (60 items × 3 reps
= 180 conversations, each multi-turn, each turn judged). At the measured
between-conversation SD ≈ 0.228 that buys a tight CI (±0.033 at n=180) but the
first score takes many minutes, so cycles are far too slow. Owner wants **rapid
iteration that slows over time** as confidence builds.

## The statistical spine (drives everything)

95% CI half-width = 1.96 · SD / √n, SD ≈ 0.228 (measured from the haiku seed):
- n=20  → ±0.100
- n=40  → ±0.071
- n=80  → ±0.051
- n=180 → ±0.033

Significance threshold = 0.05. So **small rungs cannot resolve a 0.05 gap** — they
are *elimination* filters ("not clearly worse"), not *selection* judgments. Only
the upper rungs apply the real 0.05 promotion/winner test. This is the load-bearing
constraint on the escalation gate.

## Structure (FULL)

1. **Coverage ladder (successive-halving / Hyperband-style).** Nested, seeded,
   stratified rungs of the tuning slice: rung0 ⊂ rung1 ⊂ … ⊂ full. Iterate cheap at
   rung0; a prompt only "earns" more coverage by passing the escalation gate. Rung
   sizes + per-rung reps are config-driven and adaptive — exact numbers (20/40/…)
   are NOT important; the *idea* of escalating coverage is.
2. **2 islands per model (coevolution).** Each island runs its own rapid
   author→score→re-author loop on its current rung, evolving its own best prompt
   independently. **Author prompting differs per island on purpose** so the two
   pursue meaningfully different prompt shapes (anti-over-convergence).
3. **Tournament + migration.** When both islands have a champion they're confident
   in, run them head-to-head on a shared, larger rung; the **winner becomes the new
   baseline for BOTH islands** (owner decision). Divergent per-island author
   prompting then re-injects exploration so they don't collapse to one line.
4. **Cadence.** Fast + frequent early (tiny rungs, frequent re-authoring, frequent
   compare); slows as confidence grows (bigger rungs, rarer but more rigorous
   compares); ends with the survivor validated against everything (Phase B = the
   reserved 80% validation set, unchanged).

## Owner decisions (locked)

- **Author model = Sonnet 4.6 with adaptive thinking enabled** (NOT Opus). Rationale:
  the big model's "learning" is already captured in the failure evidence + baked
  Prompting_Guidance handed to the author; authoring is transmission, not discovery.
  (Judge stays Opus; author≠judge invariant still holds, and now author is cheaper.)
- **Islands per model = 2.**
- **Migration = winner replaces BOTH islands' baselines**, with divergent per-island
  author prompting preserving post-migration exploration.
- Escalation gate = **hybrid** (statistical "not significantly worse at this rung" +
  model judgment "worth more coverage"), per owner's "the model says let's bump it up."

## What's reused vs new (impact map)

REUSED UNCHANGED (the expensive correctness core):
- `JudgeInLoopScorer.score_prompt(items, reps, ...)` — already takes an arbitrary
  item list + rep count, so "score a small rung" = pass fewer items. Retrieval-always,
  held-constant memoized fragments, Opus triad, abstention weighting: all untouched.
- `stats.is_significant` / `ci_half_width` — now applied per-rung and between two
  arbitrary prompts (the tournament).
- `AuthorClient` + `select_failures` — re-authoring mechanism unchanged (swap default
  author model to Sonnet-4.6-thinking; add per-island prompt-style variation).
- Memoizing `RetrievalBackend` — MORE valuable (small rungs re-hit the same turns).
- `split_items` — still the train/test boundary; "everything" = the validation set.

NEW / CHANGED (all orchestration ABOVE the scorer):
- Rung-ladder sampler (nested seeded stratified subsets + per-rung reps).
- Island inner loop = `IterationController` parameterized by current rung; stop
  semantics change to "iterate at rung until escalation gate fires → hand up".
- Escalation policy (new, hybrid).
- Tournament scheduler (new) — trigger, shared-rung head-to-head, winner pick, migrate.
- Orchestrator rewrite — per model → 2 islands + tournament loop (currently just
  "run 2 models concurrently").
- Store fields: `rung`, `island_id`, `tournament_round`. SSE: new event types.
- UI: Per_Model_View → multi-island view (bracket + coverage ladder).

## Out of scope / unchanged
- The 300 multi-turn item universe; the Phase B validation-on-80% final number.
- Judge = Opus; author ≠ judge.
- Loopback-only, no-auth posture.

## Sourcing honesty
Hyperband/successive-halving, island-model coevolution, and tournament selection
with migration are **external/industry** techniques, not Amazon-internal guidance —
same posture as the rest of this spec's methodology.


## Front end v2 — a different surface for a different shape of data

The v1 UI (one champion-vs-challenger line chart per model + an author-reasoning
panel) is the WRONG model for v2 and is being replaced, not extended. v2 produces
fundamentally different data: per model there are **2 islands**, each **climbing a
coverage ladder** (rungs of growing size), **re-authoring rapidly**, periodically
fighting **head-to-head tournaments**, then **migrating** a winner and diverging
again. A single line can't represent that.

### What the v2 surface must show (per Target_Model)

1. **Island lanes (2 per model), side by side.** Each lane shows that island's:
   - current **rung** (which step of the coverage ladder it's on) and the rung's
     size/CI — so you can see "this island is iterating cheap at rung 0" vs
     "escalated to rung 2";
   - a compact **score-over-iterations** sparkline for THIS island's champion (not
     a champion-vs-challenger duel — the duel is now between islands, not within
     one);
   - the island's **stance/style** label (concise vs explicit) so the divergence is
     visible;
   - live **author reasoning** for the current re-author, and the current champion
     prompt + last diff;
   - a "stuck / iterating / escalating" state chip.

2. **A coverage-ladder rail.** A vertical/stepped indicator showing the rungs
   (12 → 24 → 40 → 60 → … → full validation) with each island's marker positioned
   at its current rung and the CI it can resolve there. This is the "fast early,
   slower as confidence grows" story made visible — the thing the owner most wants
   to see.

3. **A tournament bracket / timeline.** Each tournament round: the two island
   champions that fought, the shared rung it was decided on, the head-to-head triad
   + CI for each, the **winner**, and the **migration** event (winner becomes both
   islands' new baseline). Successive rounds stack so you can read the lineage of
   how the model's prompt got to where it is.

4. **Per-model summary header.** Current best prompt, its best-resolved triad + CI,
   how many tournament rounds done, and (when it lands) the **Phase B** number on
   the full validation set.

5. **Two models** (sonnet-4.6-thinking-off, haiku-4.5) each get their own such
   panel; both stay subscribed (the viewable→concurrency gate still applies).

### Data/seams this implies (so the backend feeds it)

- New SSE event types beyond v1's champion_scored/author_token/iteration_completed:
  `optimizer_island_step` (island_id, rung_index, champion_score+CI, state),
  `optimizer_rung_escalated` (island_id, from_rung, to_rung),
  `optimizer_tournament` (round, island_a/b champ scores+CI, shared_rung, winner),
  `optimizer_migration` (round, winning prompt_version_id → both islands).
- Status endpoint grows per-island + per-tournament-round progress (durable
  backfill, so a reload never blanks the surface — the v1 lesson).
- Store records gain `island_id`, `rung_index`, `tournament_round`.

### Build posture
The v2 front end is a NEW set of components (island lane, ladder rail, tournament
bracket) and a new top-level Quality-Optimizer-v2 view; the v1 `PerModelView` /
`OptimizerTriadChart` are superseded. Reuse the `EChart` wrapper, the SSE hook
pattern, the typed client, and the durable-backfill discipline (chart/state must
reconstruct from the status poll, never depend solely on the no-replay stream).
Built only after the backend emits the new event types + status shape.
