# Quality Signal Diagnosis (Effort A, Task 1)

Evidence-backed root cause for why the v2 optimizer plateaus at ~0.35–0.53 triad
and trends **down** over iterations instead of climbing toward the ~0.85 target.

All strings below were captured by tracing one real turn-1 conversation through
the in-loop scoring path on the live dataset (item `c1-s01`, gold node
`eb558531-...`), not asserted from reading code.

---

## TL;DR

The judge's correctness/completeness reference is a **200-character truncated
snippet** of the gold FAQ, not the full answer body — even though the full body
(median ~1000 chars, up to ~3000) exists on disk in two places. The judge is
being asked "is this answer complete and correct?" against a reference that is
itself cut off mid-sentence. That is the primary reason the signal is mis-targeted
and cannot climb.

A second, smaller defect mis-scores correct abstentions on later turns of
unanswerable conversations. A third (tiny) defect omits the question text from the
in-loop judge call.

---

## The scoring path traced

`JudgeInLoopScorer._judge_turn` (`bakeoff/quality/optimizer/judge_loop.py`)
→ `turn_reference(item, 0)` (`bakeoff/quality/dataset.py:77`)
→ `ideal_response_text(item.gold, item.wants)` (`bakeoff/scoring/semantic.py:176`)
→ `judge_scorer.score_detailed(...)` (`bakeoff/scoring/judge.py:791`).

The gold fragments are resolved by `DatasetLoader.resolve_gold`
(`bakeoff/dataset.py:226`), which constructs `GoldFragment(node_id, title,
snippet)` and **never** sets `markdown=`.

---

## Hypothesis (a) — thin gold reference: **SUPPORTED (primary root cause)**

Captured for `c1-s01`, gold node `eb558531-...`:

```
gold fragment as resolved by the loader:
  title:   "Why is my booking out of policy?"
  markdown is None? -> True
  snippet len: 200
  snippet: '# **Why is my booking out of policy?** The "Out of Policy" flag in the
            [Self-Service Travel Booking Tool](...) helps you identify options
            that comply with the [Global T'      <-- cut off mid-word

what the judge receives:
  ideal_text len:    280   (= item.wants + the 200-char snippet)
  gold_texts[0] len: 200   (the truncated snippet)
  gold_texts[0]: '...comply with the [Global T'   <-- truncated
```

The same gold node's **full body is 1257 chars** and is present, byte-identical,
in both `data/faq_corpus.csv` (`text` column) and `data/results.jsonl`
(`markdown`):

```
results.jsonl markdown len: 1257
faq_corpus.csv  text   len: 1257
identical: True
```

`ideal_response_text` selects `body = frag.markdown or frag.snippet or frag.title`
(`semantic.py:191`). Because `markdown` is `None`, it falls through to the
`snippet`, which `synth/build_index.py` truncates to `SNIPPET_CHARS = 200`
(line 20) when building `corpus_index.tsv`. So the judge's correctness and
completeness reference is the first 200 characters of the FAQ — for `c1-s01`,
cut off at "the [Global T". Faithfulness grounds on the *retrieved* fragments
(passed separately), but correctness/completeness measure against this truncated
ideal. The metric is mis-targeted by construction.

**Conclusion:** the signal the optimizer climbs is anchored to a truncated
reference. No amount of prompt evolution can score well against "complete vs. a
sentence fragment."

## Hypothesis (b) — answerability plumbing: **SUPPORTED for one regime**

- Turn-1 answerable (GOLD) and later turns in answerable conversations: handled
  correctly.
- **BROKEN: later turns in unanswerable conversations.** `turn_reference`
  (`dataset.py:77-86`) hardcodes `WANTS` for `idx > 0` and never inspects
  answerability; `turn.answerability` is `None` for every later turn by
  construction (`dataset.py` `answerability if is_turn1 else None`);
  `_turn_judge_inputs` (`quality/judge.py:186`) then tells the judge
  `answerability="full"` for later turns. So a correct decline on an
  out-of-domain later turn earns zero abstention credit.
- Minor: 6/300 no-gold conversations get a fabricated `answerability="none"`
  (`dataset.py` `_NO_GOLD_ANSWERABILITY="none"`).

## Hypothesis (c) — rubric caps at ~0.5: **REFUTED**

Scale is 1–5 per dimension, normalized `(s-1)/4` clipped to `[0,1]`
(`scoring/judge.py:591`), full range reachable (5 → 1.0). Recorded data shows
full-range scores (1.0/1.0/1.0) and between-conversation SD ≈ 0.21. The mid-scale
centering is the **abstention blend** `w = QUALITY_OPT_ABSTENTION_WEIGHT = 0.5`
(`judge_loop.py`: `overall = (1-w)*triad + w*(1.0 if abstention_correct else 0.0)`),
not the rubric.

Secondary finding (flagged): the in-loop `score_detailed` call
(`judge_loop.py:~338`) does **not** pass `question=`, although the signature
accepts it (`judge.py:791`). The judge prompt therefore renders the question as
unavailable in the loop, which can depress correctness/completeness relative to
the Phase-2 path, which does pass it.

## Hypothesis (d) — retrieval doesn't contain gold: **strong form REFUTED, operative form UNKNOWN**

One shared 56-node corpus: `faq_corpus.csv` == `corpus_index.tsv` ==
`results.jsonl` keys (all 56 `nodeId`s overlap exactly). Gold ids are indexed ids,
so the answer always exists in the corpus. But nothing forces the gold `nodeId`
into the retrieved top-k, and `grounding_fragment_ids` recall@k is not persisted,
so gold-node recall@k is unmeasured. Backend is `QUALITY_OPT_RETRIEVAL_BACKEND =
opensearch` with endpoint/index unset in config (owner-injected at runtime via
`AWS_PROFILE=alpha`). Operative impact remains a live-check item, not a
code-reading conclusion.

---

## Source comparison: `results.jsonl` vs `faq_corpus.csv` vs `corpus_index.tsv`

| source | count | body field | body length (median) | keyed by |
|---|---|---|---|---|
| `data/results.jsonl` | 56 | `markdown` | ~1003 (full) | `nodeId` |
| `data/faq_corpus.csv` | 56 | `text` | ~1003 (full) | `nodeId` |
| `data/synthetic/corpus_index.tsv` | 56 | `snippet` | 200 (truncated) | `nodeId` |

Body content is **byte-identical** between `results.jsonl.markdown` and
`faq_corpus.csv.text` for all 56 nodes. `results.jsonl` carries extra fields the
CSV lacks (`markdown`, `h1`, `loaded_url`, `modelSetId`, `capture_status`,
`raw_html_len`, `n`); `faq_corpus.csv` carries cohort/system metadata
(`system_job-level`, `system_location-type`, etc.) that `results.jsonl` lacks.
Owner instruction is to treat `results.jsonl` as the authoritative full-body
source.

---

## Implied fix (scope decided in Task 2)

1. **(primary)** Resolve `gold_node_ids` to the **full body** from
   `data/results.jsonl` (`markdown`, keyed by `nodeId`) and populate
   `GoldFragment.markdown`, so `ideal_response_text` / `gold_texts` use full
   bodies instead of the 200-char snippet. Keep `corpus_index.tsv` resolution for
   the gold-integrity check.
2. **(secondary)** Thread later-turn answerability so a correct decline on an
   unanswerable later turn scores as abstention-correct.
3. **(tiny)** Pass `question=` into the in-loop `score_detailed` call.
4. **(verify-only)** Add a live gold-node recall@k read before concluding (d).
