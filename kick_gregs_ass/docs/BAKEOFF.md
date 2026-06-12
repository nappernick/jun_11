# The Model Bake-Off

The master document. This is the experiment the rest of the repo serves: a
head-to-head comparison of candidate models answering internal **Amazon Travel,
Events & Expenses** FAQ questions, over a **shared, held-constant retrieval
substrate**, scored against a **synthetic evaluation set** we generated because no
real user-query data exists yet.

> **Status legend:** ✅ built · 🟡 designed, not yet run · ⛔ **TODO — needs a human decision** (you).

> **This is the experiment doc (why + what).** For how to *run* the harness — run
> steps (pilot → plan → full run → aggregate → reports), launching the FastAPI app
> and where the TypeScript dashboard mounts, the loopback/no-auth posture, the
> credential-expiry behavior, and judge calibration — see
> [`bakeoff/README.md`](../bakeoff/README.md) (the operations doc). The two are
> complementary and do not overlap.

---

## 1. The question

We want a defensible picture of **how candidate models differ in performance** on
this niche but globally-used domain — enough to "fill a normalized curve" of
behavior across the real diversity of who asks and how. Because we have **no
production query logs to normalize against**, we synthesize that diversity on
purpose (see §4).

⛔ **TODO — Competitors.** Name the models/harnesses being compared and their
versions. The design is competitor-agnostic; fill this in:
- Competitor A: `____`
- Competitor B: `____`
- (more as needed)

---

## 2. Why a shared retrieval substrate

From the backend README, verbatim on intent:

> "Shared retrieval substrate for the model bake-off. It is *only* retrieval… Everything downstream — model choice, prompt, generation, TTFT — is each competitor's own harness, so the comparison stays about the models, not the back-end."

So retrieval is a **constant**, identical for every competitor:

```
query → Embed v4 (Bedrock, dense) ┐
      → BM25 (local, sparse)       ├→ Qdrant RRF fusion (top N) → Rerank 3.5 → top K + confidence
        metadata hard filters ─────┘
```

Implications that shape scoring (§5):
- The retriever returns ranked **fragments identified by `nodeId`** (`src/retrieve.py` returns `id = payload[nodeId]`). Our gold labels are keyed on `nodeId` for exactly this reason — they line up with what retrieval returns.
- Because retrieval is constant, **single-turn retrieval quality is the same for everyone.** Differences between competitors show up in (a) how each harness *uses* the retrieved fragments to answer, and (b) **multi-turn**, where each harness must condense a winding conversation into a retrieval query — see §5.3.
- `confidence` (Rerank 3.5 `relevanceScore`) is **relative within a query's candidate set, not a calibrated probability**. Prod reranks with v4; Bedrock only offers 3.5. Treat confidence as a relative signal; calibrate any threshold against gold, don't assume a fixed cutoff.

---

## 3. Entry routes (production surfaces under test)

Two ways a user reaches the system in prod. Every synthetic record is tagged with
its `entry_route` so results can be sliced by surface.

- **Slack bot** — conversational; informal, can be chatty/multi-line.
- **QuickSuite** — user types into QuickSuite, which makes a tool call to the RAG system.

**Design decision (locked):** we generate **raw human text for both routes.**
Rationale: hardening against messy raw input is the conservative target; if
QuickSuite reformulates upstream it only ever makes the input *easier*, so a system
tuned on raw mess is safe either way.

⛔ **TODO — QuickSuite reformulation.** Confirm whether Q passes the user's raw text
to RAG or rewrites it first. If it rewrites, add a "Q-polished" variant of the
eval set (cheap to derive); until confirmed, raw-for-both stands.

---

## 4. The synthetic evaluation set ✅

Built and final. Lives in `data/synthetic/`. See `docs/SYNTHETIC_DATA.md` for the
full pipeline and `docs/DATA_SCHEMA.md` for record formats (both 🟡 to be written).

**Totals:** 1,000 single-turn questions · 300 multi-turn conversations (250×3-turn
+ 50×5-turn endurance) · 50 distinct personas · **0 invalid gold nodeIds** · 50/56
corpus articles exercised as gold · answerability ≈ 60% full / 25% partial / 15% none.

### 4.1 Design principles (what makes it defensible, not just "LLM-looking text")
- **Coverage-first, structural diversity.** A deterministic sampling frame (`synth/frame.py`) enumerates persona coordinates — `origin × region × proficiency × disposition`, then per-query `channel × entry_route × momentary_state` — by coprime-stride walks on a counter. Breadth is *mechanical*, so it can't collapse to a few modes even across hundreds of batches. ≈ 9,800 distinct persona "heads" before context varies.
- **Two-tier persona vs. interaction context.** Identity/disposition is stable per persona; channel/route/momentary-state are drawn *per interaction* (the same person Slacks on mobile while frustrated one day, uses QuickSuite calmly the next). This was a deliberate fix — freezing a per-session attribute into a person is a grain error.
- **Region granularity.** Personas are placed *within* a country ("northern Spain," "coastal Lagos") because region drives cadence and lexicon.
- **Blind generation, then intent-labeling.** Query generators never see the corpus (so they can't write to the chunks; the unanswerable/partial tail survives). A separate labeler then assigns gold `nodeId`(s) by **intent**, reading the real corpus — never by keyword overlap.
- **Permissive gold.** `gold_node_ids` may be 0, 1, or many. `answerability ∈ {full, partial, none}` captures the messy reality (multi-hop, partial coverage, genuinely out-of-domain). This was an explicit anti-goal of "one query → one perfect chunk."
- **Quality gate.** Every nodeId is validated against the corpus at label time *and* at compile time — hence 0 invalid across all 50 batches.

### 4.2 Multi-turn: plans, not scripts
Multi-turn conversations are stored as **plans** (persona + an arc of intents +
`relationship`/`edge_profile` tags + `response_dependent` flags + seed utterances),
**not** as fixed transcripts. This matches the literature: you cannot script a
user's turn-2 because it depends on the assistant's real turn-1 answer. The artifact
supports two realizations at eval time:
- **Static replay** — feed seed turns to every model. Cheap, deterministic, fair A/B; under-tests difficulty.
- **Live-conditioned** 🟡 — feed turn 1, capture the model's *real* answer, regenerate turn 2 in persona reacting to it. Coherent and genuinely probing; this is where error-cascades surface. `response_dependent` flags mark which turns *must* be regenerated live.

---

## 5. How to score 🟡

Retrieval is constant, so scoring separates cleanly into retrieval quality (a
shared baseline, useful as a sanity check) and per-competitor answer/behavior.

### 5.1 Single-turn retrieval metrics (shared baseline)
Replay each of the 1,000 queries through `/retrieve`; compare returned `nodeId`s to `gold_node_ids`:
- **Recall@K / hit-rate**, **MRR**, over the `answerability == "full"` and `"partial"` subsets.
- Report by slice: region / proficiency / disposition / channel / entry_route / intent_shape.

### 5.2 Answerability & refusal behavior (per competitor)
- `none` items (≈15%, genuinely out-of-domain: payroll, PTO, IT, RSU…) test **abstention**: does the harness correctly decline rather than hallucinate? Score refusal-correctness here.
- `partial` items test whether the harness signals incompleteness vs. over-claiming.

### 5.3 Multi-turn (per competitor) — the high-signal part
Because retrieval is constant, multi-turn gaps are almost entirely about **how each
harness turns a winding conversation into a retrieval query** (reference resolution,
context-stuffing, query rewriting). Hypotheses to test: contradiction/correction
turns break things more than drill-downs; tangents cause topic-bleed on callbacks;
error-cascades only appear under live-conditioned realization.
- Use **instance-level rubric questions**, not holistic judging (rubric judging aligned with human raters ~93% vs ~36% for holistic in MultiChallenge).
- Use **cross-model validation**: don't let one model family be generator, judge, and competitor at once.

### 5.4 Reporting: describe the cohort, never score-by-synthetic-group
⚠️ **Critical methodological guardrail.** We *engineered* the persona proportions, so
"country/region X scores Y" is an artifact of how many queries we minted, **not** a
population signal. Do **not** publish performance broken down by synthetic persona
attribute — it manufactures false security or false alarms. Persona attributes are
for **describing cohort diversity** (the alluvial / evenness-bar / sunburst views),
*separate from* performance, every attribute co-equal.

⛔ **TODO — Metrics & thresholds.** Lock the exact metric set, any pass/fail
thresholds, the judge model(s), and human-calibration plan.

---

## 6. Reproduce / run

```bash
# 1. Retrieval backend (needs Docker + Python 3.10+; Bedrock auth via `ada` line printed at startup)
./run.sh                      # ingests data/faq_corpus.csv into Qdrant, serves POST /retrieve

# 2. Regenerate the synthetic set from scratch (deterministic frame → resumable)
python3 -m synth.build_index  # build the labeler's corpus lookup
#   then run synth/TICK.md per batch (single+multi generators → labeler → compile)
python3 -m synth.progress     # human-readable snapshot of the current dataset
```
The synthetic pipeline is **resumable**: state is the append-only
`perspectives_ledger.jsonl` + the deterministic frame, so any interruption continues
from the right batch with no duplication.

---

## 7. Limitations & deferred work
- **Reranker is 3.5, not prod's v4** (Bedrock limit). Constant across competitors, so it can't move the comparison — but confidence numbers won't match prod.
- **Live-conditioned multi-turn harness** 🟡 — not yet built; needed for error-cascade measurement.
- **Sharded-reveal expansion** 🟡 — split the 151 multi-intent single-turn questions into multi-turn "reveal one shard per turn" cases; cheapest way to add premature-answer/poor-recovery stress.
- **6 missing turn-1 gold labels** — batch 0's conversations predate the turn-1 labeling step (294/300 labeled); ~30s backfill.
- **"Events" coverage** — the corpus is travel-booking + expense heavy; event-specific queries may legitimately return `none`. That's a content gap, not a data bug.
- **Viz** — alluvial/evenness/sunburst previews exist but were built pre-region; re-point at the two-tier model for real-data visuals.

---

## 8. File map
| Path | What |
|---|---|
| `README.md` | the retrieval backend (the shared substrate) |
| `config.py`, `src/` | backend: ingest, retrieve, filters, server, Bedrock client |
| `data/faq_corpus.csv` | the 56-article corpus (gold targets resolve here) |
| `data/synthetic/queries.jsonl` | 1,000 gold-labeled single-turn questions |
| `data/synthetic/conversations.jsonl` | 300 multi-turn conversation plans |
| `data/synthetic/conversation_turn1_gold.jsonl` | turn-1 gold labels |
| `data/synthetic/perspectives_ledger.jsonl` | the 50 personas + per-batch answerability |
| `synth/frame.py` | the deterministic sampling frame (persona + context) |
| `synth/TICK.md` | per-batch generation runbook (the generator/labeler briefs) |
| `synth/build_index.py`, `synth/progress.py`, `synth/preview_*.py` | corpus index, progress readout, diversity viz |
