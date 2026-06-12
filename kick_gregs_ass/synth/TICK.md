# Synthetic-data generation — one tick

Run this once per invocation. It produces ONE batch (one fresh persona) and stops.
State is the append-only ledger, so this is fully resumable: kill it any time, the
next tick continues from the right batch number. Keep your own (orchestrator)
output minimal — the agents write to disk and return tiny summaries; do NOT echo
full batches back into the conversation, or context bloats over 50 ticks.

## 0. Determine the batch number and stop condition
Run: `python3 -m synth.frame`  (prints the NEXT unused batch's persona + a sample
context schedule). The batch number = number of lines in
`data/synthetic/perspectives_ledger.jsonl`.

**STOP CONDITION:** if the ledger already has **50** lines, the run is COMPLETE
(target: 1,000 single-turn + 300 multi-turn). Do NOT generate. Report "complete"
and end the loop. Otherwise let N = that batch number and proceed.

## 1. Generate single-turn (fresh blind agent)
Spawn a `general-purpose` agent with the brief below, substituting N. It must
import `synth.frame` for persona+context and stay blind to everything under
`data/`. It writes `data/synthetic/_pending_batch_N.json` and returns only a count
+ intent tally.

> You generate synthetic search queries for an internal Amazon FAQ. Output is DATA; return only what's asked.
> STAY BLIND: do not read/open/grep anything under data/. You MAY import synth.frame (code, no corpus content).
> Get persona + per-query context:
>     from synth.frame import coordinate_for_batch, context_schedule
>     persona = coordinate_for_batch(N); contexts = context_schedule(N, 20)
> persona is STABLE for the batch (one human): use its origin/native_language/interference, proficiency, disposition, AND region. Combine the country (from origin.label) + the region position to place this person in a SPECIFIC locale (e.g. "northern region" + Spain -> north of Spain: Basque/Galician influence, distinct dialect words and cadence) and render that area's speech texture. The city in origin.label is only a fallback anchor — the region refines it.
> contexts[i] is how query i+1 happens THIS time (channel surface, entry_route tag, momentary_state overlay). Persona = the person (constant); context = the situation (varies per query). Blend: 'desktop-careful' = fuller punctuation but English still at the persona's proficiency; 'frustrated' = sharper; 'mobile-thumb' = lowercase/autocorrect; 'search-box-keywords' = bare tokens.
> Domain = Amazon corporate TRAVEL, EVENTS & EXPENSES. In-domain: flights/hotels/rental cars, fare upgrades & miles, booking tool & travel profile + its tech support, manager approval, out-of-policy bookings, combine work+personal travel, personal travel in tool, medical accommodation, visas, travel insurance, rental-car accident, expenses (reimbursable, submit, deadlines, rejected, lost receipt, meals, mobile phone), corp card personal-use mistake. OUT-of-domain (for the unanswerable ones): payroll, PTO, benefits, stock/RSU, badge/access, IT/VPN, relocation, parental leave, performance review.
> Produce 20 queries; query i in contexts[i-1]; intent quota: ~11 single answerable in-domain (spread across DIFFERENT categories), ~3 multi-intent, ~3 vague (still give each vague query a faint in-domain topic anchor so it's tetherable to a "partial", not contentless), ~3 deliberately unanswerable (out-of-domain). You AIM intent shape; a separate labeler decides final answerability.
> Output -> data/synthetic/_pending_batch_N.json = {"batch":N,"persona_tag":"<origin label> | <region head> | <proficiency head> | <disposition head>","queries":[{"local_id":"bN-q01","query":"...","channel":"<short>","entry_route":"<slack|quicksuite>","momentary_state":"<short>","intent_shape":"single|multi|vague|unanswerable|hyper-specific","intents_aimed":1,"wants":"<clear plain English of the real need>"}]}
> Reply ONLY: count + intent_shape tally.

## 2. Generate multi-turn (fresh blind agent)
Spawn a `general-purpose` agent with the brief below, substituting N.

> You draft SYNTHETIC MULTI-TURN CONVERSATION PLANS to stress-test a retrieval+answer system. Output is DATA; return only what's asked.
> STAY BLIND: do not read anything under data/. You MAY import synth.frame.
>     from synth.frame import coordinate_for_batch, context_for
>     persona = coordinate_for_batch(N)  # STABLE persona; for conversation k (0-based) use context_for(N, 60+k) as the SESSION context
> Use persona.region too: combine country (from origin.label) + region position to place the person in a specific locale (e.g. "northern region" + Spain -> north of Spain) and render that area's dialect/cadence; the city in the label is only a fallback.
> We do NOT script fake transcripts — turn 2 depends on the assistant's real answer, which we can't know. We write a PLAN: persona + arc of intents + how each turn RELATES to earlier ones. Tag response_dependent=true (+depends_on_turn) for turns whose wording only makes sense reacting to the prior answer (still give a plausible seed). Lean into the EDGES: winding, interrelated-but-tangential, callbacks, corrections/contradictions, escalations.
> Same domain as the single-turn brief. Keep every user utterance in the persona voice, colored by each turn's momentary_state.
> Produce 6 sets. Turn-count mix: 5 sets of 3 turns + 1 set of 5 turns (endurance). Give each a distinct edge_profile from: drill_down, tangent, winding, callback, correction, contradiction, escalation, topic_switch.
> Output: append each set as ONE JSON line to data/synthetic/conversations.jsonl:
> {"set_id":"cN-s01","batch":N,"persona_tag":"...","session_channel":"<short>","entry_route":"<slack|quicksuite>","turn_count":3,"edge_profile":[...],"turns":[{"turn":1,"relationship":"opening","momentary_state":"<short>","response_dependent":false,"depends_on_turn":null,"user_utterance":"...","wants":"<clear plain English>"}]}
> Reply ONLY: the 6 set_ids + each turn_count + edge_profile + one-line gist.

## 3. Label (fresh agent, the linchpin)
Spawn a `general-purpose` agent with the brief below, substituting N.

> You assign GOLD LABELS. Output is DATA. Correctness > speed; judge by INTENT, never keyword overlap.
> Inputs: data/synthetic/_pending_batch_N.json (queries with messy text + clear "wants" + context fields); data/synthetic/corpus_index.tsv (nodeId, title, snippet — the ONLY 56 articles; read all first); data/faq_corpus.csv (full bodies — open a row with python csv + csv.field_size_limit(sys.maxsize) ONLY when title+snippet are ambiguous).
> For each query pick the article(s) that genuinely ANSWER the intent -> gold_node_ids: [] if nothing (answerability "none", e.g. payroll/PTO/IT/relocation/stock); 1 for a clean match; 2+ when it truly needs several (multi-intent -> one per intent; or indistinguishable regional variants -> include them, note it). Do NOT force exactly one.
> answerability: "full" = fully answered; "partial" = touched but not resolved (multi-intent with one intent uncovered; only a generic/region-mismatched variant exists — the persona's country may have no variant; only adjacent info); "none" = not covered. For an in-domain but VAGUE query, prefer "partial" with the most plausible article(s) over an empty "none" — reserve "none" + [] for genuinely out-of-domain or truly untether-able asks (keeps vague-handling consistent across batches).
> VALIDATION: every nodeId MUST exist in corpus_index.tsv. Never invent; grep if unsure.
> Write final records -> append to data/synthetic/queries.jsonl, ONE json per line, in order: {"id":"bN-q01","batch":N,"persona":"<persona_tag>","channel":"<copy>","entry_route":"<copy>","momentary_state":"<copy>","query":"<verbatim>","wants":"<verbatim>","gold_node_ids":[...],"answerability":"full|partial|none","intents":<int>,"intent_shape":"<copy>","label_note":"<short why>"}
> Also label multi-turn TURN 1 only (response-independent, so gold is solid): for each set in conversations.jsonl with batch==N, you MAY append {set_id, turn:1, gold_node_ids, answerability} to data/synthetic/conversation_turn1_gold.jsonl. Later turns stay as plan (their gold is unreliable; left for live realization).
> Write ONE ledger line -> append to data/synthetic/perspectives_ledger.jsonl: {"batch":N,"persona":"<tag>","origin":"...","native_language":"...","proficiency":"<head>","disposition":"<head>","query_count":20,"answerability_dist":{"full":x,"partial":y,"none":z}}
> Delete data/synthetic/_pending_batch_N.json after both writes. Reply ONLY: total labeled + answerability distribution.

## 4. Quick integrity check + refresh the readout + report
Run a one-liner to confirm the new batch's gold nodeIds all resolve in
`corpus_index.tsv` (flag any that don't). Then run `python3 -m synth.progress` to
refresh `data/synthetic/PROGRESS.md` (the human-readable snapshot). Then report a
SINGLE line: `batch N done — single=20 (full/partial/none x/y/z), multi=6 sets`.
Nothing more.
