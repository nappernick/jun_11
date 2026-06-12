# Implementation Plan: Optimizer Quality Uplift (Effort A → B)

## Overview

Two sequenced efforts to fix why the v2 optimizer plateaus at ~0.35–0.53 triad and
trends **down** over iterations instead of climbing toward the ~0.85 target.

- **Effort A — Fix the evaluation signal (diagnose first, then fix).** No optimizer
  can climb a noisy or mis-targeted metric. A must be done and verified before B.
- **Effort B — Port three GEPA mechanics into our existing asyncio optimizer**
  (feedback-shaped metric → author; Pareto frontier instead of single champion;
  merge of complementary winners). Keeps our Bedrock inline-agent adapter,
  OpenSearch retrieval, SSE dashboard, and live-visibility work — no DSPy migration.

Sequencing is hard: **do not start B until A is verified with real, non-degenerate
scores.** Optimizing on top of a broken metric is wasted effort (and is what burned
the last several runs).

Grounding note (rigor steering): GEPA mechanics are from the DSPy docs +
"GEPA: Reflective Prompt Evolution Can Outperform Reinforcement Learning"
(Agrawal et al., ICLR 2026, arXiv:2507.19457) — an external/industry source, not
Amazon-internal guidance. The coverage-ladder / island-tournament structure is
likewise external. Re-validate any judge-derived number before defending a decision
upward.

Correction carried from analysis: an earlier claim that "gold is empty" was wrong.
`DatasetLoader.resolve_gold()` resolves `gold_node_ids` → `GoldFragment(node_id,
title, snippet)` from `corpus_index.tsv`; `bakeoff/quality/dataset.py::turn_reference`
builds the turn-1 ideal from that. The open question A1 must answer is whether
title+snippet is a *sufficient* reference or whether the judge needs the full FAQ
body text from `data/faq_corpus.csv`. Diagnose before changing.

Python verify: `.venv/bin/python -m pytest bakeoff/tests/ -q`.
Frontend verify: `bun run build` in `bakeoff/ui`.
Live verify: boot dashboard with `AWS_PROFILE=alpha`, run v2, read real scores.

---

## Tasks

### Effort A — Fix the evaluation signal

- [x] 1. (Effort A) Diagnose the scoring signal end-to-end (NO code changes yet)
  - Trace one real turn-1 conversation through the in-loop path:
    `JudgeInLoopScorer._judge_turn` → `turn_reference(item, 0)` →
    `ideal_response_text(item.gold, item.wants)` → `judge_scorer.score_detailed`.
    Capture the EXACT `ideal_text`, `fragments`, and `gold_texts` strings the judge
    receives, plus the per-dimension verdict it returns.
  - Confirm or refute each hypothesis with the captured strings (evidence-before-
    hypothesis): (a) the gold reference is title+snippet only and too thin for the
    judge to score faithfulness/completeness against; (b) `answerability` /
    `ground_truth_kind` is not reaching the judge so abstention turns are mis-scored;
    (c) the judge rubric caps at ~0.5 by construction; (d) the retrieved fragments
    (OpenSearch) don't contain the gold node's content so the model literally cannot
    ground.
  - Cross-check: does the gold node's `nodeId` appear in the OpenSearch fragments
    retrieved for that turn? If gold ≠ retrieved, the model is being asked to answer
    from fragments that don't contain the answer (a retrieval problem, not a prompt
    or judge problem).
  - Write findings to `docs/QUALITY_SIGNAL_DIAGNOSIS.md` with the captured strings as
    evidence. Name which hypothesis the data supports and which it rules out.
  - _Output: a written, evidence-backed root cause. Gate for task 2._

- [x] 2. (Effort A) Confirm scope with the user before fixing
  - Present the task 1 root cause and the specific fix it implies (full-body gold
    references vs. answerability plumbing vs. rubric change vs. retrieval fix). These
    are different-sized changes; per rigor steering, name the lighter and heavier
    paths and the source, then wait for direction.
  - _Gate: explicit user go-ahead on the task-1-implied fix before writing task 3._

- [ ] 3. (Effort A) Implement the evaluation-signal fix (scope set by task 2)
  - If thin-reference: resolve `gold_node_ids` to full body text from
    `data/faq_corpus.csv` (56 nodes, keyed by `nodeId`) and thread it into
    `ideal_response_text` / the judge's `gold_texts`, rather than title+snippet.
    Keep `corpus_index.tsv` resolution for the integrity check; add body resolution
    for the judge reference only.
  - If answerability plumbing: ensure `item.answerability` /
    `GroundTruthKind` flows into `_judge_turn`'s abstention branch for all three
    regimes (full / partial / none), not just turn-1.
  - Keep the change minimal and targeted to what task 1 proved. Do not expand into a
    judge rewrite unless tasks 1/2 explicitly direct it.
  - _Requirements: evaluation signal is non-degenerate and on the 0..1 scale with a
    real target._

- [ ] 4. (Effort A) Verify the fix produces a real signal (offline + tiny live)
  - Offline: a unit test that scores a known-good answer and a known-bad answer for
    the same turn and asserts good ≫ bad (the metric must separate them). Add to
    `bakeoff/tests/test_quality_optimizer.py`.
  - Live: run ONE rung-0 step against alpha OpenSearch and read the actual triad +
    per-dimension scores. A correct, well-grounded answer should now score
    meaningfully above 0.5; a fabricated answer should score low. If scores are still
    flat, return to task 1 — do not proceed to Effort B.
  - Run `.venv/bin/python -m pytest bakeoff/tests/ -q`; full suite green.
  - _Gate: real separation between good and bad answers, verified on live data._

## Effort B — Port GEPA mechanics into the existing optimizer

> Start only after A4 gate passes. Each B task is independently committable and
> sized under the autonomy ceiling (~500 lines / ~10 files); subdivide if a task
> grows past it.

- [ ] B1. Feedback-shaped metric: thread the judge's critique into the author
  - Extend `JudgeInLoopScorer` to surface the judge's per-turn natural-language
    evidence/critique (already produced by `score_detailed` as `evidence`) up through
    `SliceScore` / `TurnVerdict`, not just the triad float.
  - Change `IslandLoop._author` / `bakeoff/quality/optimizer/author.py` so the author
    rewrite prompt receives that critique text for the selected failures — the GEPA
    insight that the proposer must see *why* a turn failed, not just that it scored N
    (DSPy GEPA: "the metric is the feedback channel; a plain-float metric gives a much
    weaker version of itself").
  - Keep the triad as the promotion decision metric; feedback only conditions the
    proposal. No change to `is_significant`.
  - Tests: assert the author contract receives non-empty critique for a failing turn;
    `bakeoff/tests/test_quality_optimizer.py`.
  - _Requirements: author proposals informed by judge critique (GEPA design 1, 2)._

- [ ] B2. Pareto frontier instead of single champion (fixes the downward drift)
  - In `bakeoff/quality/optimizer/island.py` / the orchestrator, retain every
    candidate that is best on at least one conversation (per-example Pareto frontier),
    not only the best-aggregate champion. A single-champion hill-climb at rung-0 noise
    (CI ±0.10) drifts down on re-measurement; the frontier does not discard a
    candidate that wins the hard cases (GEPA design 4).
  - Selection of the returned/seed champion stays highest-aggregate; the frontier is
    for what survives between iterations and what the next author starts from.
  - Persist frontier membership in the store so it survives reload (extend the
    existing `IterationRecord`/snapshot rather than a new store).
  - Tests: a candidate that wins one example but not the aggregate is retained across
    an iteration.
  - _Requirements: no monotonic-down drift; frontier retained (GEPA design 4)._

- [ ] B3. Merge complementary winners
  - When two frontier candidates each win different conversations, add a merge step
    that proposes a new candidate combining their instructions (GEPA `use_merge`),
    scored on the shared rung like any other challenger. Cap merge attempts
    (mirror GEPA `max_merge_invocations` default 5) via a config constant.
  - Wire into the orchestrator's per-rung tournament point (tournaments fire per-rung
    once both islands have a score at that rung — the agreed model; no waiting beyond
    that).
  - Tests: a merge produces a candidate inheriting from both parents; merge count is
    capped.
  - _Requirements: complementary-lesson combination (GEPA design 7)._

- [ ] B4. Live + dashboard verification of B
  - Run a full live v2 run (the agreed ladder: rung patience 6 @ n=12, 3 @ n=24,
    1 @ n=40, 1 @ n=60) and confirm the trend curve climbs rather than drifts down,
    and that the activity log / island panels reflect critique-driven rewrites.
  - `bun run build` in `bakeoff/ui`; `.venv/bin/python -m pytest bakeoff/tests/ -q`.
  - _Gate: scores trend up across iterations on real data._

---

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["A1"], "rationale": "Diagnosis is the gate for everything; no code until the signal is understood." },
    { "wave": 2, "tasks": ["A2"], "rationale": "User confirms scope of the fix before implementation." },
    { "wave": 3, "tasks": ["A3"], "rationale": "Implement only the A2-approved fix." },
    { "wave": 4, "tasks": ["A4"], "rationale": "Prove the metric separates good from bad on live data. Hard gate before B." },
    { "wave": 5, "tasks": ["B1", "B2"], "rationale": "Feedback metric and Pareto frontier are independent and can land in parallel once the signal is real." },
    { "wave": 6, "tasks": ["B3"], "rationale": "Merge depends on the frontier (B2) existing." },
    { "wave": 7, "tasks": ["B4"], "rationale": "Full live verification of the combined B changes." }
  ]
}
```

## Notes

### Out of scope

- Full DSPy / `dspy.GEPA` migration (effort C) — rewriting the answer path, judge, and
  retrieval as DSPy modules. Deferred; revisit only as a platform decision.
- v2 frontend full redesign (the separately-scoped "effort #2").
- v1 recreation/extraction (the separately-scoped "effort #3").
