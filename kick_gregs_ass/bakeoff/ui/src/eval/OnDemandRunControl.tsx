/**
 * OnDemandRunControl — the latent, on-demand combinatorial run control
 * (Area F / Req 22).
 *
 * A REACHABLE but NON-DEFAULT control (Req 22.7, 22.8): it is collapsed behind a
 * toggle so the primary surface of the eval feature remains visualization of
 * already-recorded runs. When opened, it lets the user assemble an ARBITRARY pool
 * directly in the browser — one or more agents (not bound to the >= 3 comparison
 * primitive, Req 22.2), an arbitrary subset of enabled ragas + retrieval metrics
 * (Req 22.3), an arbitrary corpus size / sweep series (Req 22.4), and an arbitrary
 * query subset (Req 22.5) — and launch it with NO config-file or source edit
 * (Req 22.1).
 *
 * The run produces one Instance per element of the cartesian combination of
 * agents x corpus sizes x queries (Req 22.6); when that count exceeds the
 * configured threshold the control REQUIRES explicit confirmation before
 * launching (Req 22.12). All request-building / gating logic lives in the pure
 * `onDemandRun.ts` module so it is testable without a DOM; this component is the
 * thin interactive shell over it.
 *
 * Communicates with the backend over loopback only, no auth, inheriting the
 * harness posture (Req 22.13, 22.14) — it issues the same same-origin
 * `POST /api/eval/runs/start` every other run uses, with `on_demand: true`.
 */
import { useMemo, useState } from "react";
import type { JSX } from "react";
import { defaultEnabledNames } from "./catalog";
import { methodologyLabel } from "./methodology";
import {
  ON_DEMAND_DEFAULT_OPEN,
  DEFAULT_ONDEMAND_THRESHOLD,
  buildOnDemandRequest,
  canLaunch,
  combinationCount,
  type OnDemandSelection,
} from "./onDemandRun";
import { startEvalRun, ApiError } from "../api/client";
import type { EvalStreamState } from "../api/useEvalStream";

/** The configured agent pool the dashboard compares (mirrors backend EVAL_AGENTS). */
const AGENT_POOL: readonly string[] = ["agent-a", "agent-b", "agent-c", "agent-d"];
/** The retrieval-metric names selectable alongside ragas metrics (Req 22.3). */
const RETRIEVAL_METRICS: readonly string[] = ["precision_at_k", "recall_at_k", "ndcg_at_k"];
/** A fixed pool of selectable synthetic query ids (the available query subset). */
const QUERY_POOL: readonly string[] = ["q0", "q1", "q2", "q3", "q4", "q5", "q6", "q7"];
/** A small palette of corpus sizes the user can include in a sweep series. */
const CORPUS_SIZE_POOL: readonly number[] = [50, 100, 200, 500];

export interface OnDemandRunControlProps {
  readonly stream: EvalStreamState;
  /** The over-threshold confirmation threshold (Req 22.12). */
  readonly threshold?: number;
}

/** Toggle a value's membership in a list (immutably). */
function toggle<T>(list: readonly T[], value: T): T[] {
  return list.includes(value) ? list.filter((x) => x !== value) : [...list, value];
}

export function OnDemandRunControl({
  stream,
  threshold = DEFAULT_ONDEMAND_THRESHOLD,
}: OnDemandRunControlProps): JSX.Element {
  // Collapsed by default so recorded-run visualization stays the default surface.
  const [open, setOpen] = useState<boolean>(ON_DEMAND_DEFAULT_OPEN);

  const [agents, setAgents] = useState<readonly string[]>([]);
  const ragasOptions = useMemo(() => defaultEnabledNames(), []);
  const [metrics, setMetrics] = useState<readonly string[]>(["faithfulness"]);
  const [corpusSizes, setCorpusSizes] = useState<readonly number[]>([]);
  const [queryIds, setQueryIds] = useState<readonly string[]>(["q0"]);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // The pending confirmation latch for an over-threshold run (Req 22.12).
  const [confirmNeeded, setConfirmNeeded] = useState(false);

  const selection: OnDemandSelection = useMemo(
    () => ({ agents, metrics, corpusSizes, queryIds }),
    [agents, metrics, corpusSizes, queryIds],
  );
  const count = combinationCount(selection);
  const decision = canLaunch(selection, { confirmed: confirmNeeded, threshold });

  const launch = async (confirm: boolean): Promise<void> => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const body = buildOnDemandRequest(selection, { confirm });
      const snap = await startEvalRun(body);
      const enqueued = (snap as { enqueued?: boolean }).enqueued === true;
      setNotice(
        enqueued
          ? `Enqueued behind the active run (${count} combination(s)).`
          : `Launched ${count} combination(s).`,
      );
      setConfirmNeeded(false);
    } catch (e) {
      // An over-threshold pool comes back as a structured 409 asking for
      // confirmation (Req 22.12) — surface the confirm affordance rather than a
      // hard error, and latch so the next click resends with confirm=true.
      if (
        e instanceof ApiError &&
        e.status === 409 &&
        (e.detail as { confirmation_required?: boolean })?.confirmation_required
      ) {
        setConfirmNeeded(true);
        const d = e.detail as { combination_count?: number; threshold?: number };
        setError(
          `This run has ${d.combination_count ?? count} combinations, over the ` +
            `threshold of ${d.threshold ?? threshold}. Confirm to launch.`,
        );
      } else if (e instanceof ApiError && e.status === 429) {
        setError("The on-demand run queue is full. Retry after the active run completes.");
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setBusy(false);
    }
  };

  const overThreshold = decision.requiresConfirmation;

  return (
    <div className="panel" style={{ marginBottom: 16 }}>
      <div className="panel-title">
        <button
          className="btn"
          aria-expanded={open}
          aria-controls="on-demand-body"
          onClick={() => setOpen((v) => !v)}
        >
          {open ? "▾" : "▸"} On-demand run (latent)
        </button>{" "}
        <span className="muted">
          — recorded-run visualization is the default surface; this is an on-demand capability.
        </span>
      </div>

      {open && (
        <div id="on-demand-body" className="eval-on-demand">
          <div className="cp-hint muted">
            Assemble an arbitrary pool and launch it — no config or code edit (Req 22.1). One
            Instance is produced per agent × corpus-size × query combination. {methodologyLabel()}.
          </div>

          {/* agents (one or more; Req 22.2) */}
          <div className="cp-field">
            <span>agents (one or more)</span>
            <div className="cp-checks">
              {AGENT_POOL.map((a) => (
                <label key={a} className="cp-check">
                  <input
                    type="checkbox"
                    checked={agents.includes(a)}
                    onChange={() => setAgents((p) => toggle(p, a))}
                  />
                  {a}
                </label>
              ))}
            </div>
          </div>

          {/* metrics (ragas + retrieval subset; Req 22.3) */}
          <div className="cp-field">
            <span>metrics (ragas + retrieval)</span>
            <div className="cp-checks">
              {[...ragasOptions, ...RETRIEVAL_METRICS].map((m) => (
                <label key={m} className="cp-check">
                  <input
                    type="checkbox"
                    checked={metrics.includes(m)}
                    onChange={() => setMetrics((p) => toggle(p, m))}
                  />
                  {m}
                </label>
              ))}
            </div>
          </div>

          {/* corpus sizes (arbitrary sweep series; Req 22.4) */}
          <div className="cp-field">
            <span>corpus size(s) — none = single default size</span>
            <div className="cp-checks">
              {CORPUS_SIZE_POOL.map((s) => (
                <label key={s} className="cp-check">
                  <input
                    type="checkbox"
                    checked={corpusSizes.includes(s)}
                    onChange={() => setCorpusSizes((p) => toggle(p, s))}
                  />
                  {s}
                </label>
              ))}
            </div>
          </div>

          {/* queries (arbitrary subset; Req 22.5) */}
          <div className="cp-field">
            <span>queries</span>
            <div className="cp-checks">
              {QUERY_POOL.map((q) => (
                <label key={q} className="cp-check">
                  <input
                    type="checkbox"
                    checked={queryIds.includes(q)}
                    onChange={() => setQueryIds((p) => toggle(p, q))}
                  />
                  {q}
                </label>
              ))}
            </div>
          </div>

          <div className="cp-hint">
            Combination count: <b>{count}</b>
            {overThreshold && (
              <span className="pill bad" style={{ marginLeft: 8 }}>
                over threshold {threshold} — confirmation required
              </span>
            )}
          </div>

          {error && (
            <div className="banner" style={{ marginBottom: 8 }}>
              {error}
            </div>
          )}
          {notice && (
            <div className="empty" style={{ marginBottom: 8 }}>
              {notice}
            </div>
          )}

          <div className="cp-row">
            {!confirmNeeded ? (
              <button
                className="btn"
                disabled={busy || (!decision.ok && !overThreshold)}
                title={decision.reason}
                onClick={() => void launch(false)}
              >
                Launch run
              </button>
            ) : (
              <button
                className="btn"
                disabled={busy}
                onClick={() => void launch(true)}
              >
                Confirm &amp; launch {count} combination(s)
              </button>
            )}
            {!decision.ok && decision.reason && !overThreshold && (
              <span className="muted" style={{ marginLeft: 8 }}>
                {decision.reason}
              </span>
            )}
            <span className={`pill state ${stream.status}`} style={{ marginLeft: "auto" }}>
              {stream.status}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
