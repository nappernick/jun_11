# Building Short-Lived Calibration Judges for a RAG + QA System: A Practitioner-Verified Implementation Guide

## TL;DR
- **Build the judge as a thin layer on top of error analysis, not the other way around.** The practitioner consensus (Hamel Husain, Shreya Shankar, Eugene Yan, Jason Liu, Braintrust, LangChain) is overwhelming and specific: review ~100 traces by open-coding failures, appoint ONE domain expert as a "benevolent dictator," write **binary pass/fail** judges (never 1–5 Likert) with a chain-of-thought critique before the verdict, and **validate the judge against held-out human labels using TPR/TNR — not raw accuracy**. For a 1–2 month calibration tool, skip heavy infra: a notebook, a spreadsheet/custom data viewer, and the `judgy` package get you ~90% of the value.
- **Evaluate RAG component-wise, not end-to-end.** Debug retrieval first with cheap IR metrics (Recall@k, MRR — no LLM needed), then evaluate generation with validated LLM judges along Jason Liu's "6 RAG evals" (the three core ones: Context Relevance C|Q, Faithfulness A|C, Answer Relevance A|Q — the TruLens "RAG triad"). For the user-facing orchestration layer, evaluate multi-turn at the whole-conversation/goal-completion level first, then drop to step-level diagnostics (routing/tool choice) using transition-failure matrices.
- **Trust practitioners over papers, but use papers for the math.** What practitioners insist works: binary scoring, one expert, custom data viewers, error-analysis-first, validating the validator. What papers add (cite only 2024–2026): the quantified biases (position/verbosity/self-preference), the Rogan-Gladen bias-correction math behind `judgy`, and "criteria drift" (Shankar et al.). The single most-trusted source you named — Hamel Husain — is corroborated by an entire network of credible practitioners listed below.

---

## Key Findings

**1. The dominant build methodology is "Critique Shadowing" (Hamel Husain).** It is a 7-step loop and it is the spine of your project:
1. Find THE principal domain expert (one person, the "benevolent dictator").
2. Create a dataset (yours is partly built — synthetic multi-turn + single-question).
3. Have the expert make **binary pass/fail** judgments **with a written critique** explaining the reasoning.
4. Fix obvious errors found along the way.
5. Build the LLM judge iteratively — the critiques become few-shot examples; iterate the prompt until it agrees with the expert.
6. Perform error analysis (classify failures, count them, compute failure rates by dimension).
7. Create specialized judges only if needed.

The punchline Hamel repeats: *"It's Not The Judge That Created Value, After all"* — the real ROI is forcing the team to look at data. In his "Field Guide to Rapidly Improving AI Products" he reports that on the Honeycomb Query Assistant, *"It took three iterations to achieve >90% agreement, but this investment paid off in a system the team could trust."* A revealing side effect, from domain expert Phillip Carter: *"Seeing how the LLM breaks down its reasoning made me realize I wasn't being consistent about how I judged certain edge cases"* — building the judge standardizes the human's own criteria.

**2. Binary beats Likert — this is near-unanimous among practitioners.** Hamel: a dashboard of 1–5 scores across many dimensions "is often a sign of a bad eval process." Reasons given: adjacent points (3 vs 4) are subjective and inconsistent across annotators; Likert needs larger samples to detect differences; annotators cluster on middle values to avoid hard calls. Critique-shadowing instead has the expert answer the yes/no question *"Did the AI achieve the desired outcome?"* with a critique *"detailed enough that a new employee could understand it."* Eugene Yan, Braintrust (Ankur Goyal's hallucination cookbook found classification more reliable than numeric ratings), and the OpenAI cookbook all independently land on binary. To track gradual progress, decompose into multiple binary checks (e.g., "4 of 5 expected facts present") rather than a scale. Always require **chain-of-thought / critique BEFORE the verdict** (G-Eval, Braintrust, and Cameron Wolfe's Netflix synopsis system all confirm reasoning-first improves accuracy; Netflix even adds consensus scoring — running the judge 5× and averaging lifts accuracy ~5% on some criteria).

**3. Validate the judge with TPR/TNR and bias-correction, NOT raw agreement.** This is the most technically important finding and the area most teams get wrong. Hamel/Shreya teach explicitly: *"Measure what matters: TPR/TNR over accuracy."* In their masterclass they put it bluntly: agreement is *"the trap metric… if this failure is only happening, let's say 10% of the time, you can have the dumbest judge in the world have 90% accuracy by just always predicting pass."* The workflow:
   - Split ground-truth labels into **train (10–20%, few-shot examples), dev (40–45%, iterate the prompt), test (40–45%, final unbiased measurement)** (per the Husain/Shankar playbook on Lenny's Newsletter).
   - Measure **TPR** (of things that should pass, % the judge passed) and **TNR** (of things that should fail, % the judge failed) on the held-out test set.
   - Use the `judgy` package (from the `ai-evals-course` GitHub org) to correct the judge's observed pass rate into the true pass rate via the **Rogan-Gladen estimator**: `θ̂ = (p_obs + TNR − 1) / (TPR + TNR − 1)`, with 95% bootstrap confidence intervals (default 20,000 iterations). Valid only when **TPR + TNR > 1** (judge better than chance).
   - Targets practitioners cite: TPR and TNR **>90%** on the dev set (Langfuse); Cohen's kappa **0.4–0.6 = substantial, >0.7 = excellent** (Eugene Yan), used for human-human inter-annotator agreement. Need a **balanced** label set — Yan recommends **at least 50–100 failures out of 200+ samples**; Hamel/Shreya say LLM judges need **100+ labeled examples**.

**4. Known judge biases (papers, 2024–2026) and mitigations.** Position bias (favoring first/one position — in the MT-Bench study gpt-3.5 was biased ~50%, claude-v1 ~70%; pairwise swaps can shift accuracy >10%; bias surveys summarize this as ~10–15 points of winrate swing): randomize/swap order. Verbosity/length bias (longer = higher score; claude-v1/gpt-3.5 preferred verbose responses >90% of the time; summaries put it at ~15–30 points of winrate swing): control for length. Self-preference/self-enhancement bias (gpt-4 +10% to its own outputs, claude-v1 +25%, confirmed across Llama/Claude/GPT pairs at "10 to 25 percent"; Panickssery et al., NeurIPS 2024, "LLM Evaluators Recognize and Favor Their Own Generations" shows self-preference linearly tracks self-recognition, with lower-perplexity/familiar text the likely mechanism): for your scoped binary judge this matters less — Hamel/Shreya say using the same model is usually fine because alignment to human labels is what matters. Sycophancy/agreeableness bias and the documented **low-TNR problem**: Jain, Ahmed, Sahai & Leong, *"Beyond Consensus: Mitigating the Agreeableness Bias in LLM Judge Evaluations"* (arXiv:2510.11822, Oct 2025, UMass Amherst/NUS) found across 14 LLM judges on a code-feedback task (366 high-school Python programs) that *"while LLMs can identify valid outputs with high accuracy (i.e., True Positive Rate >96%), they are remarkably poor at identifying invalid ones (i.e., True Negative Rate <25%)."* This is exactly why you measure TNR separately.

**5. RAG evaluation is two problems, evaluated separately.** Retrieval = a search problem, measured with **IR metrics (Recall@k, Precision@k, MRR) — fast, no LLM**. Generation = validated LLM judges. Jason Liu's framework ("There Are Only 6 RAG Evals"): three core relationships are Context Relevance (C|Q, measures retriever), Faithfulness/Groundedness (A|C, measures hallucination), Answer Relevance (A|Q, end-to-end UX) — these map onto the TruLens "RAG triad." To build a retrieval eval set cheaply: take corpus documents, extract facts, generate the questions those facts answer (reverse-generation gives query-document pairs with no manual annotation). Ragas offers reference-free versions of these metrics; practitioners use it for a quick baseline but warn its off-the-shelf prompts must be validated against your labels (Hamel: "don't just use prompts off the shelf").

**6. The non-RAG orchestration layer: evaluate goal completion first, then steps.** For multi-turn, Hamel's guidance: check whether the **whole conversation met the user's goal** (pass/fail), then find the **first upstream failure** (downstream errors usually cascade from it). Simplify failures to the smallest reproducing test case; use **N-1 testing** (feed real first N-1 turns, test turn N) or LLM user-simulation for test generation. For routing/intent, treat it as classification (accuracy, precision, recall, confusion matrix) against a labeled set — code-based checks beat LLM judges here. For agentic/tool steps, use **transition-failure matrices** (rows = last good state, columns = first failure) to find hotspots — Bryan Bischof and Shreya Shankar both use this for text-to-SQL agents.

**7. Synthetic data: useful for coverage, dangerous as a distribution proxy.** Practitioners (Hamel, and Hex's Bryan Bischof: *"it works, ship it"*) confirm synthetic inputs work surprisingly well — but generate **inputs only**, run them through your real system, and use **structured dimensions** (features × scenarios × personas, or tuples) rather than "give me test queries." Validate synthetic realism against real data; it fails for specialized/domain-heavy content, low-resource languages, and high-stakes domains. Critically for you: synthetic data is a *starting* baseline, not a replacement for real-trace error analysis once you have UAT traffic.

**8. Model choice for the judge.** Start with the most capable model you can afford in your latency/cost budget; optimize cost later. You do NOT need a fine-tuned judge for a 1–2 month tool — Hamel: *"I prefer not to fine-tune LLM judges. I'd rather spend the effort fine-tuning the actual LLM."* Fine-tuned open evaluators (Prometheus 2, JudgeLM, others) exist and can match GPT-4-level correlation in-domain (Prometheus 2 reports Pearson ~0.9 with humans), but a 2024 study ("On the Limitations of Fine-tuned Judge Models") found they behave as narrow task-specific classifiers that collapse out-of-distribution. Same model for generator and judge is fine for scoped binary classification. Panel-of-judges (PoLL — Verga et al., Cohere, arXiv:2404.18796, a panel of Claude Haiku + GPT-3.5 + Command R Plus) *"outperforms a single large judge, exhibits less intra-model bias… and does so while being over seven times less expensive,"* but adds complexity you likely don't need short-term.

---

## Details

### The end-to-end build workflow (concrete, ordered, for your project)

**Phase 0 — Instrument & view (days 1–3).** Build a **custom data viewer**, not an off-the-shelf dashboard. This is, per Hamel, *"the single most impactful investment"* — teams with custom viewers iterate ~10x faster. Render traces domain-specifically (show the conversation, retrieved chunks, tool calls, the source docs the expert needs to judge correctness), add hotkeys and a progress bar, and put everything on one screen. Build it in hours with Cursor/Claude/Lovable. A Jupyter notebook with widgets is a fully acceptable MVP.

**Phase 1 — Error analysis / open coding (days 3–10).** Have your principal domain expert review traces (start with your synthetic multi-turn + single-question data; add real UAT traces the moment they exist). For each, write an open-ended note on the **first failure**. Then axial-code: group notes into a failure taxonomy and **count** each category. Rule of thumb: review **at least 100 traces**; stop when ~20 new traces reveal no new category ("theoretical saturation"). Hamel/Shreya report spending **60–80% of development time on error analysis and evaluation**. Fix obvious bugs immediately — don't build a judge for something a prompt fix or regex assertion solves.

**Phase 2 — Label & build the judge (days 10–20).** For the failure modes worth automating, have the expert produce binary pass/fail labels **with critiques** detailed enough to drop into a few-shot prompt ("detailed enough that a new employee could understand it"). Judge prompt structure (synthesized from Hamel's Honeycomb example + Eugene Yan):
```
[System] You are a {domain} evaluator. Judge whether {specific criterion}.
{domain/context info the judge needs}
{precise pass/fail definitions}

<examples>  ← few-shot, each with input, output, CRITIQUE (reasoning), then outcome
  <example> <input>…</input> <output>…</output>
    <critique>…detailed reasoning…</critique> <outcome>pass|fail</outcome>
  </example>
  …diverse examples covering varied inputs + external context…
</examples>

For the following, FIRST write a detailed critique, THEN give pass/fail.
<input>{{input}}</input> <output>{{output}}</output> <critique>
```
One criterion per judge (Braintrust: "don't measure factuality, conciseness, and tone in one score"). XML or JSON output. Iterate the prompt against the dev set until TPR/TNR converge. Eugene Yan's free **AlignEval** tool and LangChain's **Align Evals** (in LangSmith, explicitly inspired by Yan's article) both operationalize this calibration loop with side-by-side human-vs-judge views and an alignment score.

**Phase 3 — Validate & correct (days 20–25).** Run the frozen judge on the held-out **test** set. Report TPR and TNR (and Cohen's kappa if multiple human annotators). Use `judgy` to get a bias-corrected true pass/fail rate with bootstrap CIs. If TPR+TNR ≤ 1 or alignment is poor, fix the prompt or (rarely) swap models; if it still fails, the task may be over-scoped or you lean more on human review.

**Phase 4 — Deploy lightweight for UAT (days 25+).** Run the judge offline on your synthetic set and on sampled UAT traces. For CI, favor cheap deterministic assertions on a small curated set (~100 examples incl. regression cases); reserve the LLM judge for the subjective/nuanced criteria. Track confidence intervals on production metrics; re-run error analysis when something material changes. Because the tool is short-lived, do NOT over-invest in drift monitoring infra — periodic manual re-labeling of a fresh sample is sufficient.

### Where practitioners DISAGREE (surface, don't paper over)
- **Pairwise vs pointwise.** Eugene Yan and academic work argue pairwise comparisons are more stable/reliable for *subjective* qualities (tone, persuasiveness) and align better with humans. Hamel's critique-shadowing is firmly **pointwise binary** for *objective* product outcomes ("did the AI achieve the goal?"). Resolution: use pointwise binary for faithfulness/goal-completion/objective checks (your main need); consider pairwise only for subjective comparisons or model A/B selection.
- **Scores 0–1 vs strict binary.** Braintrust's platform uses 0–1 scores (averaging many binary sub-checks) and even demonstrates a 0–10 rubric for a Mermaid-diagram quality eval; Hamel/Eugene push strict binary at the labeling layer. These reconcile: label binary, aggregate to rates.
- **Generic/off-the-shelf metrics.** Ragas/TruLens/Arize ship ready-made RAG metrics; Hamel and Eugene are sharply skeptical ("the abuse of generic metrics is endemic… create an illusion of confidence"). Resolution: fine to use them as *exploration signals* to surface interesting traces, not as trusted quality metrics until validated against your labels.
- **Fine-tuned judges.** Papers (Prometheus 2) show strong in-domain correlation; practitioners (Hamel, and the 2024 "Limitations of Fine-tuned Judge Models" study) warn they're brittle task-specific classifiers. For a short-lived calibration tool: don't fine-tune.

### Annotated list of practitioners & sources to trust (with credibility notes)
**Tier 1 — trust deeply, practitioner-verified, directly on-topic:**
- **Hamel Husain (hamel.dev)** — your named source. 20+ yrs ML; led CodeSearchNet at GitHub (precursor to Copilot, used by OpenAI); independent consultant who set up evals for 30+ companies; co-teaches "AI Evals for Engineers & PMs" (700+ engineers/PMs from OpenAI, Anthropic, Google). Essential pieces: "Your AI Product Needs Evals," "Creating a LLM-as-a-Judge That Drives Business Results," the "LLM Evals FAQ," "A Field Guide to Rapidly Improving AI Products."
- **Shreya Shankar (@sh_reya)** — UC Berkeley PhD; first-author of *"Who Validates the Validators?"* (EvalGen, "criteria drift") and SPADE; co-author of the evals-FAQ and course. The academic rigor behind the practitioner methods. Trust for the theory of why human-in-the-loop is irreducible.
- **Eugene Yan (eugeneyan.com)** — Member of Technical Staff at Anthropic; ex-Amazon ML lead. His "Evaluating the Effectiveness of LLM-Evaluators" survey (draws on ~2 dozen papers but written for practitioners), "An LLM-as-Judge Won't Save The Product," "Product Evals in Three Simple Steps," and the **AlignEval** tool. Trust for the metrics decision-tree (objective→direct scoring, subjective→pairwise; binary→classification metrics, Likert→correlation) and concrete kappa thresholds.
- **Jason Liu (jxnl.co)** — built retrieval/recsys at Facebook & Stitch Fix; creator of `instructor`; RAG consultant. "There Are Only 6 RAG Evals" and "Systematically Improving Your RAG" are the cleanest RAG-eval mental models. Trust for RAG specifically.

**Tier 2 — credible, useful, more vendor- or research-flavored:**
- **Cameron Wolfe (cameronrwolfe.substack.com)** — ML Scientist at Netflix, PhD; "Using LLMs for Evaluation" and "Finetuning LLM Judges" are the best literature syntheses; recently published Netflix's production synopsis-judge system (binary per-criterion + reasoning + consensus scoring). Trust for connecting research to production.
- **Bryan Bischof (Hex)** — Head of AI Eng at Hex; transition-failure-matrix for text-to-SQL agents; synthetic-data advocate. Trust for agentic eval.
- **Applied LLMs consortium (applied-llms.org)** — "What We Learned From a Year of Building with LLMs" (Yan, Bischof, Charles Frye, Hamel, Jason Liu, et al.). Trust for the operational/strategic frame.
- **Eval tooling teams (with caveats):** **Braintrust** (Ankur Goyal; the OpenAI hallucination-judge cookbook), **LangChain/LangSmith** (Align Evals), **Arize Phoenix** (Eric Xiao; the EvalGen webinar with Aparna Dhinakaran), **TruLens** (RAG-triad origin, now Snowflake), **Ragas/DeepEval/Confident AI/Patronus/Galileo/Comet-Opik/W&B Weave**. Trust their *engineering* and *frameworks*; be skeptical of their *default metrics*. Hamel has no favorite vendor — Langsmith, Arize, Braintrust are the three he meets most; choice comes down to support, not features.

### Ops / lifecycle for a short-lived (1–2 month) tool
- **CI vs production split:** CI = small curated set (~100), favor deterministic assertions, run frequently. Production/UAT = sample live traces, run judges async, track CIs.
- **Guardrails ≠ evaluators:** guardrails are fast, inline, deterministic (PII, schema, profanity); evaluators (incl. LLM-as-judge) run after the fact for quality. Don't put a slow LLM judge inline.
- **Cost:** use the strong judge for a few hundred dev examples; for the calibration window, batch/async scoring of sampled UAT traffic keeps spend trivial. Don't build dashboards/drift pipelines you'll throw away in 8 weeks.
- **Human-in-the-loop never fully leaves:** the judge must stay aligned to *something* (a human). Periodically re-label a representative sample; trust the judge in prod only within the TPR/TNR bounds you measured.

---

## Recommendations (staged, with thresholds that change them)

**Stage 1 (Week 1): Look at data before building anything.** Stand up a custom notebook/viewer. Have your ONE domain expert open-code ≥100 traces (synthetic now, real UAT ASAP), tagging the first failure. Count failure modes. **Threshold to proceed:** you've hit theoretical saturation (~20 traces, no new categories) and have a ranked failure taxonomy. If the top failures are deterministic (format, missing field, unavailable-option), write code assertions and skip the LLM judge for those.

**Stage 2 (Week 2–3): Build binary judges for the top 1–3 failure modes.** One judge per criterion, critique-before-verdict, few-shot from expert critiques. Calibrate against a dev set in AlignEval or LangSmith Align Evals (or a plain spreadsheet). **Threshold to proceed:** judge–expert agreement is converging on the dev set.

**Stage 3 (Week 3–4): Validate statistically.** Freeze the judge; measure TPR/TNR on the untouched test set; bias-correct with `judgy`. **Go/no-go thresholds:** aim for **TPR and TNR > 0.9** (Langfuse target) and, if using multiple annotators, **Cohen's kappa > 0.7** (Yan's "excellent"); minimum viable is kappa > 0.6 / "substantial." If **TPR + TNR ≤ 1**, the judge is unusable — fix the prompt, then the model. Ensure your label set has **≥50–100 failures in ≥200 samples**; if failures are too rare, stress-test/adversarially generate hard cases to balance it.

**Stage 4 (RAG specifically): Debug retrieval first.** Build a reverse-generated query→doc eval set; compute Recall@k/MRR. **Threshold:** if Context Relevance (C|Q) is low, fix retrieval before touching generation — a doomed retriever can't be fixed by the generator. Only then validate Faithfulness (A|C) and Answer Relevance (A|Q) judges.

**Stage 5 (Orchestration layer): Goal-completion judge + routing classifier.** One conversation-level pass/fail goal judge (validated as above) + a code-based routing/intent accuracy check against labeled examples + transition-failure matrices for tool steps. **Threshold:** if most failures trace to one transition (e.g., GenSQL→ExecSQL), invest there.

**Stage 6 (Pre-launch / post-UAT): Lightweight deployment.** Async-score sampled UAT traces; report bias-corrected pass rates with CIs to stakeholders. **Re-trigger error analysis** on model swap, prompt change, or a metric-CI lower bound crossing your threshold. Given the 1–2 month horizon, deliberately do NOT build drift-monitoring infra — schedule one or two manual re-label checkpoints instead.

---

## Caveats
- **Synthetic-data distribution risk is your biggest threat.** Your judges will be calibrated partly on synthetic data; synthetic inputs systematically miss real-world quirks, edge cases, and emotional/messy phrasing. Treat pre-UAT judge metrics as provisional and re-validate against real UAT traces the moment you have them.
- **Judges inherit LLM weaknesses.** Even validated judges show low TNR on hard/implicit cases (hallucination detection is genuinely hard — gpt-3.5 had only ~58.5% accuracy distinguishing factual vs hallucinated summaries in HaluEval). The bias-correction math assumes your measured TPR/TNR generalize from test set to production — a strong assumption if distributions shift.
- **Raw % agreement and generic metrics will mislead you** under class imbalance — use TPR/TNR/kappa, and treat off-the-shelf metrics as exploration only.
- **Papers vs reality:** I've separated practitioner claims from paper claims throughout. Where they conflict (fine-tuned judges, generic metrics, Likert), I've sided with practitioners for a short-lived production-calibration tool. All papers cited are 2024–2026 per your constraint; older foundational work (e.g., MT-Bench, 2023) is referenced only where practitioners themselves rely on it for the bias taxonomy.
- **One expert is a feature, not a bug — but validate their assumptions.** The benevolent-dictator model assumes the expert truly represents users/business. If they don't, your judge faithfully encodes the wrong target. Spot-check against real user feedback.
- **The tool's value is mostly the process.** Per Hamel, the durable payoff isn't the judge artifact (which you'll retire in 8 weeks) — it's the failure taxonomy, the data literacy, and the calibrated sense of where the system breaks. Capture those findings in writing so they outlive the judge.