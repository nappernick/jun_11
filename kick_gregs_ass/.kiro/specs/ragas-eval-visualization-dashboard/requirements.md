# Requirements Document

## Sourcing note (read first)

This dashboard selects and displays external RAG-evaluation and statistics methodology:
the **ragas** metric catalog, **NDCG** and the retrieval metrics, a user-tunable
**quality composite**, and the display-layer statistical methods in Area D (confidence
intervals, multiple-comparison-aware significance, quality-floor guardrails, and
holdout-versus-rotating-pool data-split labeling). The global steering rule
(`prefer-rigor-and-internal-best-practices.md`) asks that such choices be grounded in
current Amazon-internal primary sources — BuilderHub (`docs.hub.amazon.dev`), internal
code search (`code.amazon.com`), and AWS Prescriptive Guidance.

**Those internal search tools are not available in this execution environment, so no
Amazon-internal source could be consulted while writing these requirements.** Per the
steering rule's "When Searches Return Nothing Authoritative" clause, every
evaluation-methodology and statistics choice named in this document is **general
industry practice, not Amazon-internal guidance**, and is flagged as such wherever it
appears.

External methodology referenced (all public, none Amazon-internal):
- ragas — automated reference-free RAG evaluation, including its metric catalog and
  Amazon Bedrock adapter.
- NDCG / precision@k / recall@k — standard information-retrieval ranking metrics.
- Weighted-composite scoring — a general aggregation technique.
- The display-layer statistics in Area D are grounded in
  `docs/solo-model-prompt-iteration.md`, which itself cites external/industry
  literature: Bradley-Terry strength estimates with bootstrap confidence intervals (as
  used by LMArena), Benjamini-Hochberg false-discovery-rate control, successive-halving
  / best-arm identification, the reusable holdout (Dwork et al., *Science* 2015), and
  RAGAS guardrail thresholds. All of it is external/industry practice, not
  Amazon-internal guidance.

Before any number from this dashboard is used to defend a decision to an Amazon
audience, the metric, weighting, and statistics choices in this document should be
re-validated against internal guidance.

---

## Introduction

This feature is a multi-tab, interactive, modern web application focused **entirely on
visualization**. Its purpose is to compare **three or more things** (agents, models,
prompts, or configurations) across **two performance dimensions** — speed and quality —
over a **period of instances/sessions**, and to present that comparison both in
**real-time 2D** views and in **real-time 3D** views. Latency of the dashboard matters,
but **correctness of the displayed data matters more**: every value shown must be
traceable to a recorded measurement and must be reproducible.

The application is the visualization surface for a quality-metric backbone built on
**ragas** (run through ragas' Amazon Bedrock adapter) plus **retrieval-quality metrics**
computed from the harness's existing gold links. It builds on the established research
harness:

- A FAQ hybrid-retrieval substrate exposed as `POST /retrieve`, returning ranked
  fragments with confidence and per-stage timings, with gold links
  (`gold_node_ids` resolvable to corpus body text) that make retrieval metrics
  (precision@k, recall@k, NDCG) computable.
- A model bake-off harness and a closed-loop prompt optimizer whose existing
  React/TypeScript dashboard (`bakeoff/ui`, built with **bun**) already uses an SSE +
  durable-backfill discipline. This feature reuses that discipline.

The core comparison primitive is deliberately general: **N things (N ≥ 3) across two
dimensions over a period of instances** — never hardcoded to four agents or to two named
models. A new experimental axis, the **corpus-size sweep**, runs the same queries against
varying corpus sizes (small → large) and records speed and quality versus corpus size.

### Scope boundary

A separate, parallel effort owns the **judge**, the **agents / competitors**, and
**prompt optimization** (including any pairwise-versus-pointwise judging,
position-swapping, thinking mode, cross-family audit judges, self-preference mitigation,
and any GEPA/DSPy/optimizer or prompt-authoring/management mechanics). **This dashboard
does not configure, change, or prescribe any judge behavior or any prompt-optimization
behavior.** It only **visualizes the measurements those upstream systems produce** —
including any judge verdicts, rankings, confidence intervals, and significance data
carried on recorded Instances — and it never modifies judging or prompt optimization.
Every requirement in this document constrains the dashboard's display and data-capture
behavior, not the upstream judge or optimizer.

### Retrieval posture (invariant)

Retrieval is performed against the **OpenSearch instance via its API**. For this
dashboard, retrieval is **read-only** and is **held constant for a given comparison**, so
that observed differences are attributable to the agents and conditions being compared
rather than to changes in retrieval.

### Research-environment posture

The dashboard keeps the existing local research-harness posture: **bun** is the only
JavaScript/TypeScript package manager (no npm, npx, or yarn); the app and its backing
endpoints run as a **loopback-only**, **no-authentication** local service; and all data
is the harness's **synthetic, non-PII** data.

Requirements are grouped into five areas:
- **(A) Metric computation layer** — ragas via Bedrock, retrieval metrics from gold
  links, and the configurable quality composite (Requirements 1–4).
- **(B) Experiment / data layer** — multi-agent runs, corpus-size sweep, instance/session
  and latency capture, durable storage feeding the views (Requirements 5–8).
- **(C) Visualization app** — multi-tab shell, the four 3D modes, 2D comparison views,
  controls, ideal-region and watch-for cues, readability, real-time updates with durable
  backfill (Requirements 9–15).
- **(D) Display-layer statistical rigor** — uncertainty on every displayed aggregate,
  overlapping-confidence-interval rankings shown as unresolved, multiple-comparison-aware
  significance, configurable quality-floor guardrails, holdout-versus-rotating-pool
  data-split labeling, and render-only display that never substitutes a judge verdict
  (Requirements 16–20).
- **(E) On-demand evaluation runs (latent capability)** — the user-initiated,
  arbitrary/combinatorial run capability that is reachable on demand but expected to be
  rarely or never exercised; the default surface remains visualization of
  already-recorded runs (Requirement 21).

## Glossary

- **Visualization_App** — the multi-tab React/TypeScript single-page web application that
  renders all 2D and 3D comparison views; built on `bakeoff/ui` using **bun** as the only
  JavaScript/TypeScript package manager.
- **Agent_Under_Test** — one comparable entity: a model, an agent, a prompt variant, or a
  configuration. The comparison primitive supports **three or more** distinct
  Agent_Under_Test entities at once. Referred to in views as an "agent."
- **Instance** — one execution of one Agent_Under_Test against one query/item at one
  corpus size; the atomic unit plotted as a single point. A single-turn query or one
  multi-turn conversation playthrough.
- **Session** — an ordered group of Instances for one Agent_Under_Test, used as the time /
  progression dimension.
- **Instance_Index** — the ordinal position of an Instance within its Session's ordered
  progression; the source of the Z (time/progression) axis.
- **Metric_Engine** — the component that computes all metric values (generation-quality
  via Ragas_Adapter, retrieval-quality via Retrieval_Metric_Computer) and persists them.
- **Ragas_Adapter** — the component that computes ragas generation-quality metrics using
  ragas configured with its Amazon Bedrock LLM and embedding adapter.
- **Retrieval_Metric_Computer** — the component that computes retrieval-quality metrics
  (precision@k, recall@k, NDCG@k) by comparing retrieved fragment ids against the
  resolved gold node ids (`gold_node_ids`) for each query.
- **Ragas_Metric** — a single named generation-quality metric from the ragas catalog
  (for example Faithfulness, Context Precision, Response Relevancy).
- **Retrieval_Metric** — a single named retrieval-quality metric computed from gold links
  (precision@k, recall@k, NDCG@k); distinct from a Ragas_Metric.
- **Quality_Score** — the value plotted on the Y axis: a number in the range 0.0 to 1.0,
  higher is better, produced by the Quality_Composite from selected component metrics.
- **Quality_Composite** — the deterministic weighted aggregation function that combines
  selected component metric values into a single Quality_Score.
- **Composite_Weight_Set** — a named, persisted set of per-metric weights that
  parameterizes the Quality_Composite. The weights are user-configurable.
- **Latency** — the value plotted on the X axis: end-to-end response time of an Instance
  in milliseconds, lower is better, displayed on a logarithmic scale.
- **Corpus_Size_Sweep** — an experiment mode that runs an identical query set against a
  series of corpus sizes (small → large) and records Latency and Quality_Score at each
  corpus size.
- **Experiment_Runner** — the component that orchestrates runs across multiple
  Agent_Under_Test entities, Sessions, and corpus sizes, producing Instances.
- **Event_Store** — the append-only durable store of per-Instance records that is the
  single source of truth feeding every view.
- **Status_Endpoint** — the HTTP endpoint that returns the current durable state
  sufficient to fully reconstruct every view (the durable-backfill source).
- **Stream_Channel** — the Server-Sent-Events channel that pushes incremental updates to
  the Visualization_App as new Instances land.
- **3D_View_Mode** — one of the four selectable three-dimensional renderings: Trajectory,
  Scatter, Surface, or Bubble.
- **2D_View** — a two-dimensional comparison rendering (for example a speed-vs-quality
  scatter or a metric-over-instances line chart).
- **Ideal_Region** — the visual indicator marking the high-quality, low-latency target
  area of the plot (high Y, low X).
- **Watch_For_Cue** — a visual indicator highlighting a concerning pattern: a
  high-latency + low-quality zone, drift over Sessions, or high variance/inconsistency.
- **Control_Panel** — the set of interactive controls for selecting agents, Sessions/time
  range, metrics and weights, filters (prompt/category), and smoothing/window size.
- **Authoritative_Judge** — the upstream, separately-owned judge that produces promotion
  decisions and any judge verdicts carried on recorded Instances. It is owned by a
  parallel effort, is unmodified by this dashboard, and is only visualized here (see the
  Scope boundary in the Introduction).
- **Confidence_Interval** — an interval estimate of the uncertainty around a displayed
  aggregate value (for example a 95% interval), shown instead of or alongside a bare
  point estimate.
- **Quality_Floor** — a configurable minimum threshold on a selected metric (for example a
  faithfulness or context-precision minimum), used purely as a display-side guardrail to
  flag values that fall below it.
- **Holdout_Set** — a sequestered evaluation data split that is not repeatedly queried
  during iteration.
- **Rotating_Dev_Pool** — a repeatedly-queried development / rotating evaluation data
  split, distinct from the Holdout_Set.
- **Data_Split** — the identification of which evaluation data split (Holdout_Set or
  Rotating_Dev_Pool) produced a displayed value.
- **Multiple_Comparison_Correction** — a statistical adjustment (for example
  false-discovery-rate control) applied when many agents or variants are compared at
  once, so that comparison significance is not asserted naively per pair as if the
  comparisons were independent.
- **Gold_Link** — a `gold_node_id` for a query, resolvable to corpus body text, used as
  the ground-truth relevant document for Retrieval_Metric computation.

---

## Requirements

## Area A — Metric Computation Layer

### Requirement 1: Ragas generation-quality metrics via Bedrock

**User Story:** As a researcher comparing agents, I want generation-quality scored by
ragas metrics computed through Amazon Bedrock, so that answer quality is measured with a
recognized, reproducible methodology rather than an ad-hoc heuristic.

#### Acceptance Criteria

1. WHEN the Metric_Engine is asked to score an Instance for an enabled Ragas_Metric, THE Ragas_Adapter SHALL compute that Ragas_Metric using ragas configured with its Amazon Bedrock LLM and embedding adapter.
2. THE Ragas_Adapter SHALL record, for each computed Ragas_Metric value, the metric name, the numeric value, the ragas version, and the Bedrock model identifier used.
3. WHEN a Ragas_Metric produces a numeric value, THE Metric_Engine SHALL store that value on a 0.0 to 1.0 scale where higher indicates better quality.
4. IF computation of a Ragas_Metric for an Instance fails or returns no value, THEN THE Metric_Engine SHALL record that metric as unavailable for that Instance and SHALL retain all successfully computed metric values for the same Instance.
5. WHERE the Ragas_Adapter is run in offline test mode, THE Ragas_Adapter SHALL compute metric values using an injected fake LLM and fake embedding component without making any network call.
6. THE Metric_Engine SHALL compute and store each Ragas_Metric value independently of the Quality_Composite, so that a recorded Ragas_Metric value is never altered by any Composite_Weight_Set.

### Requirement 2: Retrieval-quality metrics from gold links

**User Story:** As a researcher, I want retrieval quality measured from the existing gold
links, so that I can see retrieval-quality and answer-quality as distinct dimensions.

#### Acceptance Criteria

1. WHEN the Retrieval_Metric_Computer scores an Instance whose query has at least one Gold_Link, THE Retrieval_Metric_Computer SHALL compute precision@k, recall@k, and NDCG@k by comparing the retrieved fragment ids against the resolved gold node ids.
2. THE Retrieval_Metric_Computer SHALL record the value of k used for each Retrieval_Metric value.
3. IF a query has no resolvable Gold_Link, THEN THE Retrieval_Metric_Computer SHALL record each Retrieval_Metric as unavailable for that Instance.
4. THE Metric_Engine SHALL store each Retrieval_Metric value as a distinct, separately labeled signal from every Ragas_Metric value, so that retrieval-quality and generation-quality are never conflated in storage.
5. THE Retrieval_Metric_Computer SHALL read retrieval results only and SHALL NOT modify the retrieval substrate or its corpus.
6. WHERE both a precision@k value and a recall@k value exist for an Instance at the same k, THE Retrieval_Metric_Computer SHALL store both values without deriving one from the other.

### Requirement 3: Configurable quality composite

**User Story:** As a researcher who knows "quality" is debated, I want the Y-axis quality
score to be a weighted composite of metrics whose weights I can tune, so that I can change
the definition of quality without changing recorded data or application structure.

#### Acceptance Criteria

1. THE Quality_Composite SHALL compute the Quality_Score as a deterministic weighted aggregation of the selected component metric values and the active Composite_Weight_Set, such that identical component values and identical weights always produce an identical Quality_Score.
2. THE Quality_Composite SHALL accept a user-supplied Composite_Weight_Set that assigns a weight to each selectable component metric.
3. WHEN a user changes the active Composite_Weight_Set, THE Metric_Engine SHALL leave every recorded component metric value unchanged and SHALL recompute the Quality_Score from the unchanged recorded values.
4. THE Quality_Composite SHALL produce a Quality_Score in the range 0.0 to 1.0 where higher indicates better quality.
5. IF a component metric referenced by the active Composite_Weight_Set is unavailable for an Instance, THEN THE Quality_Composite SHALL compute the Quality_Score from the available weighted components and SHALL record which components were missing for that Instance.
6. THE Quality_Composite SHALL record, alongside each Quality_Score, the identifier of the Composite_Weight_Set that produced it.
7. THE Visualization_App SHALL provide a default Composite_Weight_Set whose weights match the documented example (Faithfulness 0.30, Answer Relevancy 0.25, Context Precision 0.15, Context Recall 0.15, Entities Recall 0.10, Others 0.05) and SHALL allow a user to override every weight.

### Requirement 4: Ragas metric catalog as a prioritized candidate menu

**User Story:** As a researcher, I want the full ragas metric catalog available as a
prioritized menu with clearly marked scope, so that I can enable as many relevant metrics
as the harness can support while ignoring families that do not apply to a RAG harness.

#### Acceptance Criteria

1. THE Metric_Engine SHALL expose a metric catalog that includes, as in-scope candidates prioritized first, the RAG family (Context Precision, Context Recall, Context Entities Recall, Noise Sensitivity, Response Relevancy, Faithfulness), the Nvidia family (Answer Accuracy, Context Relevance, Response Groundedness), and the natural-language-comparison family (Factual Correctness, Semantic Similarity).
2. THE Metric_Engine SHALL include in the catalog, as lower-priority in-scope candidates, the traditional non-LLM metrics (BLEU, ROUGE, CHRF, string-presence, exact-match) and the general-purpose metrics (Aspect Critic, Simple Criteria, Rubrics-based, Instance-specific rubrics).
3. THE Metric_Engine SHALL mark the multimodal family (multimodal faithfulness, multimodal relevance), the agent/tool family, and the SQL family as **likely out of scope** for this RAG harness in the catalog.
4. WHERE a catalog metric is marked out of scope, THE Visualization_App SHALL display that metric as out of scope and SHALL exclude it from the default enabled set.
5. THE Metric_Engine SHALL allow each in-scope catalog metric to be individually enabled or disabled for a run via configuration, without code changes.
6. THE Visualization_App SHALL label every metric in the catalog as external/industry methodology, not Amazon-internal guidance.

## Area B — Experiment / Data Layer

### Requirement 5: Multi-agent comparison runs

**User Story:** As a researcher, I want to run three or more agents against the same query
set under identical conditions, so that the comparison is about the agents and not about
differing inputs.

#### Acceptance Criteria

1. WHERE a run configuration lists three or more Agent_Under_Test entities, THE Experiment_Runner SHALL execute every listed Agent_Under_Test against the same query set under the same retrieval conditions.
2. THE Experiment_Runner SHALL produce one Instance record per (Agent_Under_Test, query, corpus size) execution.
3. THE Experiment_Runner SHALL accept the set of Agent_Under_Test entities as configuration, so that adding or removing an agent is a configuration change and not a code change.
4. THE Experiment_Runner SHALL support an arbitrary number N of Agent_Under_Test entities where N is three or more, and SHALL NOT assume a fixed count of agents.
5. IF execution of one Agent_Under_Test for one query fails, THEN THE Experiment_Runner SHALL record an Instance with a failure status for that execution and SHALL continue executing the remaining agents and queries.

### Requirement 6: Corpus-size sweep

**User Story:** As a researcher, I want to run the same queries against varying corpus
sizes, so that I can see the performance curve of speed and quality from small to large
corpus.

#### Acceptance Criteria

1. WHEN a Corpus_Size_Sweep is configured with an ordered series of corpus sizes, THE Experiment_Runner SHALL execute the same query set against each corpus size in the series.
2. THE Experiment_Runner SHALL record, for each (Agent_Under_Test, query, corpus size) execution, the corpus size used, the measured Latency, and the computed Quality_Score.
3. THE Experiment_Runner SHALL label each Instance with the corpus size against which the query was run, so that speed and quality can be plotted against corpus size.
4. THE Corpus_Size_Sweep SHALL hold the query set constant across all corpus sizes in a single sweep, so that observed differences are attributable to corpus size.
5. IF a requested corpus size cannot be prepared, THEN THE Experiment_Runner SHALL record that corpus size as unavailable for the sweep and SHALL continue with the remaining corpus sizes.

### Requirement 7: Instance, session, and latency capture

**User Story:** As a researcher, I want every instance to record its session, its
progression index, and its latency, so that the views can plot evolution over time and
compare speed accurately.

#### Acceptance Criteria

1. WHEN an Instance is executed, THE Experiment_Runner SHALL record the Agent_Under_Test identifier, the Session identifier, the Instance_Index within that Session, and the corpus size.
2. WHEN an Instance is executed, THE Experiment_Runner SHALL record the end-to-end Latency in milliseconds for that Instance.
3. THE Experiment_Runner SHALL record per-stage timings for each Instance separately from the end-to-end Latency, so that retrieval time and generation time are distinguishable.
4. THE Experiment_Runner SHALL assign Instance_Index values within a Session as a strictly increasing ordinal sequence, so that progression order is unambiguous.
5. WHERE a retrieval result for an Instance was served from cache, THE Experiment_Runner SHALL record that the retrieval was cached, so that cold and cached timings are never conflated in speed reporting.

### Requirement 8: Durable event storage feeding the views

**User Story:** As a researcher, I want every recorded instance persisted to a durable
append-only store that is the single source of truth, so that the views are reproducible
and a reload never loses data.

#### Acceptance Criteria

1. WHEN an Instance and its metric values are computed, THE Metric_Engine SHALL append one durable Instance record to the Event_Store.
2. THE Event_Store SHALL retain every appended Instance record so that the complete state of every view can be reconstructed from the Event_Store alone.
3. THE Status_Endpoint SHALL return the current durable state derived from the Event_Store sufficient to fully reconstruct every view without relying on the Stream_Channel.
4. THE Visualization_App SHALL derive every displayed value from the Event_Store or the Status_Endpoint, so that no displayed value originates outside the recorded data.
5. IF the Visualization_App reloads or reconnects, THEN THE Visualization_App SHALL reconstruct every view from the Status_Endpoint before resuming incremental updates from the Stream_Channel.

## Area C — Visualization App

### Requirement 9: Multi-tab application shell

**User Story:** As a user, I want a multi-tab application, so that I can move between
distinct visualization surfaces without losing context.

#### Acceptance Criteria

1. THE Visualization_App SHALL present multiple named tabs, each tab rendering a distinct visualization surface.
2. WHEN a user selects a tab, THE Visualization_App SHALL render that tab's surface without requiring a full-page reload.
3. THE Visualization_App SHALL provide at least one tab dedicated to 3D views and at least one tab dedicated to 2D views.
4. WHILE a run is in progress, THE Visualization_App SHALL keep every tab consistent with the same underlying Event_Store state.
5. THE Visualization_App SHALL be built on the existing `bakeoff/ui` React and TypeScript project using bun, and SHALL NOT introduce npm, npx, or yarn.

### Requirement 10: Four 3D view modes

**User Story:** As a user, I want four selectable 3D view modes mapping latency, quality,
and progression to the X, Y, and Z axes, so that I can analyze trends, distributions,
landscapes, and confidence across agents in three-dimensional space.

#### Acceptance Criteria

1. THE Visualization_App SHALL map the X axis to Latency in milliseconds on a logarithmic scale where lower is better, the Y axis to Quality_Score in the range 0.0 to 1.0 where higher is better, and the Z axis to Instance_Index or time progression.
2. THE Visualization_App SHALL provide a 3D Trajectory view that renders, for each selected Agent_Under_Test, a connected path ordered by Instance_Index, so that evolution, trends, drift, and consistency over Sessions are visible.
3. THE Visualization_App SHALL provide a 3D Scatter view that renders one point per Instance, so that density, distribution, clusters, and outliers are visible.
4. THE Visualization_App SHALL provide a 3D Surface view that renders, for each selected Agent_Under_Test, an interpolated quality landscape, so that sweet spots and overlapping surfaces are visible.
5. THE Visualization_App SHALL provide a 3D Bubble view in which bubble size encodes a selectable measure (confidence, volume, or cost) and the agents are separated along an axis.
6. WHEN a user selects a 3D_View_Mode, THE Visualization_App SHALL render the selected mode using the currently selected agents, Sessions, metrics, and Composite_Weight_Set.
7. THE Visualization_App SHALL render three or more Agent_Under_Test entities simultaneously in every 3D_View_Mode and SHALL distinguish each agent by a consistent visual encoding.

### Requirement 11: 2D comparison views

**User Story:** As a user, I want 2D comparison views, so that I can read precise
speed-versus-quality and metric-over-time comparisons that are hard to read in 3D.

#### Acceptance Criteria

1. THE Visualization_App SHALL provide a 2D speed-versus-quality view plotting Latency on the X axis and Quality_Score on the Y axis for the selected agents.
2. THE Visualization_App SHALL provide a 2D view that plots a selected metric against Instance_Index for each selected Agent_Under_Test.
3. THE Visualization_App SHALL provide a 2D view that plots Latency and Quality_Score against corpus size for the selected agents, so that the Corpus_Size_Sweep performance curve is readable.
4. THE Visualization_App SHALL provide a 2D view that displays Retrieval_Metric values and Ragas_Metric values as distinct, separately labeled series, so that retrieval-quality and answer-quality dimensions are both visible.
5. WHEN a user changes the selected agents, Sessions, metrics, or Composite_Weight_Set, THE Visualization_App SHALL update every visible 2D_View to reflect the selection.

### Requirement 12: Interactive controls

**User Story:** As a user, I want controls to select agents, sessions, metrics, weights,
filters, and smoothing, so that I can shape every view to the comparison I care about.

#### Acceptance Criteria

1. THE Control_Panel SHALL let a user select which Agent_Under_Test entities are displayed, supporting three or more selected at once.
2. THE Control_Panel SHALL let a user select the Sessions or time range displayed.
3. THE Control_Panel SHALL let a user select which component metrics contribute to the Quality_Composite and SHALL let a user set the weight of each selected component metric.
4. THE Control_Panel SHALL let a user filter Instances by prompt and by category.
5. THE Control_Panel SHALL let a user set a smoothing window size applied to trend views.
6. WHEN a user adjusts any control in the Control_Panel, THE Visualization_App SHALL apply the change to the affected views without requiring a full-page reload.
7. WHEN a user adjusts a metric weight, THE Visualization_App SHALL recompute the displayed Quality_Score from the unchanged recorded metric values, so that adjusting weights never changes recorded data.

### Requirement 13: Ideal-region indicator and watch-for cues

**User Story:** As a user, I want an ideal-region indicator and cues for concerning
patterns, so that I can immediately see which agents are good and what to be wary of.

#### Acceptance Criteria

1. THE Visualization_App SHALL display an Ideal_Region indicator marking the high-Quality_Score, low-Latency target area of the plot.
2. THE Visualization_App SHALL display a Watch_For_Cue marking the high-Latency, low-Quality_Score zone.
3. WHILE a selected Agent_Under_Test exhibits a downward Quality_Score trend across consecutive Sessions, THE Visualization_App SHALL display a drift Watch_For_Cue for that agent.
4. WHILE a selected Agent_Under_Test exhibits high variance in Quality_Score across its Instances, THE Visualization_App SHALL display an inconsistency Watch_For_Cue for that agent.
5. THE Visualization_App SHALL display a legend identifying the quality bands and the latency bands used in the current view.

### Requirement 14: Readability and axis-orientation guidance

**User Story:** As a user, I want each 3D view to remain readable with clear axis
orientation, so that I can interpret three-dimensional data without confusion.

#### Acceptance Criteria

1. THE Visualization_App SHALL display, in every 3D_View_Mode, axis labels stating that higher Y is better, lower X is better, and forward or upward Z is later in progression.
2. THE Visualization_App SHALL let a user rotate and zoom each 3D_View_Mode so that occluded points can be brought into view.
3. WHEN a user hovers over or selects a plotted Instance, THE Visualization_App SHALL display that Instance's Agent_Under_Test, Latency, Quality_Score, Session, Instance_Index, and corpus size.
4. THE Visualization_App SHALL render axis scales with labeled tick values, including the logarithmic scale on the Latency axis.

### Requirement 15: Real-time updates with durable backfill

**User Story:** As a user watching a run, I want views to update in real time and to
reconstruct exactly from durable state after any reload, so that I never depend on a
no-replay stream and never see a blank or inconsistent surface.

#### Acceptance Criteria

1. WHEN a new Instance record is appended to the Event_Store during a run, THE Visualization_App SHALL incrementally update the affected views to include the new Instance without requiring a full-page reload.
2. THE Visualization_App SHALL reconstruct every view from the Status_Endpoint, and SHALL NOT depend solely on the Stream_Channel for any view's content.
3. WHEN the Visualization_App reconnects to the Stream_Channel after an interruption, THE Visualization_App SHALL first reconstruct state from the Status_Endpoint and then resume incremental updates.
4. THE Visualization_App SHALL render a view reconstructed from the Status_Endpoint that is equal in displayed content to the same view built by applying the live Stream_Channel updates, for the same underlying set of Instance records.
5. THE Stream_Channel SHALL carry incremental updates only, and THE Status_Endpoint SHALL remain the authoritative source for full reconstruction.

## Area D — Prompt Management & Export

### Requirement 16: View, modify, and manage ragas metric prompts

**User Story:** As a researcher tuning evaluation quality, I want to view and modify the
prompts that power each customizable ragas metric, so that I can adapt a metric to this
domain without altering already-recorded measurements.

#### Acceptance Criteria

1. WHEN a user opens the Prompt_Manager for a customizable Ragas_Metric, THE Prompt_Manager SHALL display the current prompt text (instruction and examples) used by that Ragas_Metric.
2. WHEN a user requests the rendered prompt for a sample input, THE Prompt_Manager SHALL display the fully rendered prompt string that the Ragas_Metric produces for that sample input.
3. THE Prompt_Manager SHALL let a user modify or override the instruction and the few-shot examples of a customizable Ragas_Metric prompt by subclassing the metric, following the ragas "modifying prompts in metrics" mechanism, and SHALL persist the modified prompt as a named, versioned prompt configuration scoped to a run.
4. THE Prompt_Manager SHALL let a user reset a modified Ragas_Metric prompt to its ragas default.
5. WHEN a user changes a Ragas_Metric prompt, THE Metric_Engine SHALL apply the changed prompt only to Instances computed after the change and SHALL leave every previously recorded metric value unchanged.
6. THE Metric_Engine SHALL record, alongside each recorded Ragas_Metric value, the identifier of the prompt configuration that produced it, so that every recorded value is traceable to the exact prompt used.
7. WHERE a Ragas_Metric does not support prompt customization, THE Prompt_Manager SHALL display that metric as not customizable rather than offering an editable prompt.

### Requirement 17: Export of results and configurations

**User Story:** As a researcher, I want to export recorded results and the configurations
that produced them, so that a comparison can be reproduced and shared outside the
dashboard.

#### Acceptance Criteria

1. WHEN a user requests an export, THE Export_Service SHALL export the selected Instance records including, for each Instance, the Agent_Under_Test, Session, Instance_Index, corpus size, Latency, per-stage timings, every recorded Ragas_Metric and Retrieval_Metric value, and the Quality_Score.
2. THE Export_Service SHALL include in the export the active Composite_Weight_Set identifier and weights, and the prompt configuration identifiers, sufficient to reproduce every exported Quality_Score from the exported component values.
3. THE Export_Service SHALL export the recorded component metric values unchanged, so that an exported Quality_Score is recomputable from the exported components and weights.
4. THE Export_Service SHALL include in every export the ragas version and Bedrock model identifier recorded for the exported metric values.
5. THE Export_Service SHALL label every exported metric as external/industry methodology, not Amazon-internal guidance.

## Area E — Cross-Cutting Invariants & Sourcing Honesty

### Requirement 18: Authoritative judge remains the decision signal

**User Story:** As an owner of the optimizer, I want ragas to feed visualization only, so
that the established Opus judge stays authoritative for promotion decisions unless I
explicitly change that.

#### Acceptance Criteria

1. THE Metric_Engine SHALL compute Ragas_Metric and Retrieval_Metric values for visualization and comparison only, and SHALL NOT alter any prompt-promotion decision made by the Authoritative_Judge.
2. THE Visualization_App SHALL present ragas-derived signals and the Authoritative_Judge signal as distinct, separately labeled values, and SHALL NOT present a ragas-derived signal as the promotion decision.
3. WHERE both an Authoritative_Judge value and ragas-derived values exist for an Instance, THE Visualization_App SHALL display them as separate signals without overriding one with the other.

### Requirement 19: Retrieval held constant and read-only

**User Story:** As an owner of the harness, I want the dashboard and its metric
computation to never mutate the retrieval substrate, so that comparisons stay valid and
the substrate is unchanged by being measured.

#### Acceptance Criteria

1. THE Metric_Engine and THE Visualization_App SHALL issue read-only retrieval queries and SHALL NOT modify the retrieval substrate, its index, or its corpus.
2. WHERE a Corpus_Size_Sweep runs queries against differently sized corpora, THE Experiment_Runner SHALL treat each sized corpus as a prepared, read-only experiment input and SHALL NOT mutate the canonical retrieval substrate during scoring.
3. WHEN the same query is scored for two Agent_Under_Test entities at the same corpus size, THE Experiment_Runner SHALL use identical retrieval results for both, so that retrieval is held constant across the agents being compared.

### Requirement 20: External-methodology sourcing honesty as a product obligation

**User Story:** As an owner accountable to an Amazon audience, I want the dashboard itself
to label its evaluation methodology as external, so that no one mistakes a ragas number
for Amazon-internal guidance.

#### Acceptance Criteria

1. WHERE any Ragas_Metric, Retrieval_Metric, NDCG value, or Quality_Composite value is displayed, THE Visualization_App SHALL label that value as external/industry methodology, not Amazon-internal guidance.
2. WHERE results are exported, THE Export_Service SHALL include the same external/industry-methodology labeling.
3. THE Visualization_App SHALL display a notice that the evaluation methodology was not validated against Amazon-internal primary sources and should be re-validated before being used to defend a decision to an Amazon audience.

### Requirement 21: bun, loopback-only, no-auth research posture

**User Story:** As the maintainer, I want the dashboard to keep the existing local
research-harness posture, so that it stays consistent with the throwaway, loopback-only
environment and its tooling conventions.

#### Acceptance Criteria

1. THE Visualization_App SHALL be built and run using bun as the only JavaScript/TypeScript package manager, and SHALL NOT introduce npm, npx, or yarn.
2. THE Visualization_App and its backing endpoints SHALL operate as a local, loopback-only service without authentication, consistent with the existing harness posture.
3. THE Metric_Engine and Experiment_Runner SHALL operate on the harness's synthetic, non-PII data and SHALL NOT introduce handling of personally identifiable information.

## Area F — On-Demand Evaluation Runs (Latent Capability)

*This area documents a latent, on-demand capability requested after the initial
five-area structure (Areas A–E) was reviewed. It is expected to be rarely or never
exercised; the default and primary surface of the feature remains visualization of
already-recorded runs (Areas A–E).*

### Requirement 22: User-initiated arbitrary combinatorial evaluation runs

**User Story:** As a researcher, I want to be able to launch an arbitrary evaluation run
directly from the dashboard — any pool of agents, any subset of metrics, any corpus
sizes, any queries — without editing configuration files or code, so that the capability
exists on demand even though I expect to rely almost entirely on already-recorded runs.

#### Acceptance Criteria

1. WHEN a user initiates an evaluation run from the Visualization_App, THE Visualization_App SHALL launch that run without requiring the user to edit any configuration file or any source code.
2. WHEN a user configures an on-demand run, THE Visualization_App SHALL accept an arbitrary pool of one or more selected Agent_Under_Test entities, not limited to the three-or-more comparison primitive.
3. WHEN a user configures an on-demand run, THE Visualization_App SHALL accept an arbitrary subset of the enabled in-scope metrics, including both Ragas_Metric entries and Retrieval_Metric entries.
4. WHEN a user configures an on-demand run, THE Visualization_App SHALL accept an arbitrary corpus size or an arbitrary Corpus_Size_Sweep series.
5. WHEN a user configures an on-demand run, THE Visualization_App SHALL accept an arbitrary subset of the available queries.
6. WHERE a user requests a combinatorial pool for an on-demand run, THE Experiment_Runner SHALL produce one Instance for each element of the cartesian combination of the selected Agent_Under_Test entities, the selected corpus sizes, and the selected query subset.
7. THE Visualization_App SHALL provide a reachable control for initiating an on-demand run.
8. THE Visualization_App SHALL present visualization of already-recorded runs as the default surface, so that the on-demand run capability remains a non-default latent capability.
9. WHEN an on-demand run produces an Instance, THE Experiment_Runner SHALL append that Instance to the Event_Store and expose it through the Status_Endpoint and the Stream_Channel by the same path used for every other run.
10. THE Experiment_Runner SHALL allow at most one on-demand run to be active at a time.
11. IF a user requests an on-demand run while another on-demand run is active, THEN THE Experiment_Runner SHALL enqueue the request in a bounded queue and SHALL start the enqueued run only after the active run completes.
12. IF the combination count of a requested combinatorial pool exceeds a configurable threshold, THEN THE Visualization_App SHALL require explicit user confirmation before launching the run.
13. WHILE an on-demand run is being initiated or executed, THE Visualization_App SHALL communicate with the run backend over the loopback interface only.
14. WHERE the Visualization_App initiates an on-demand run, THE Visualization_App SHALL permit that run without requiring authentication, consistent with the existing loopback-only harness posture.
