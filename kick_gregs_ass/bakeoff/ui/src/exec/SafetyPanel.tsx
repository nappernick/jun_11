/**
 * Answerability / safety panel (Req 11.5).
 *
 * For an FAQ bot the **fabrication-on-unanswerable** rate is a trust metric an
 * exec must see *separately* from headline quality — a model that hallucinates on
 * out-of-domain questions is dangerous regardless of how well it answers the rest.
 * The backend slices abstention-correctness by answerability (never blended — P4);
 * we show, per model, unanswerable-abstention vs answerable behavior side by side,
 * each with its CI, and flag any model whose abstention on `none` items is poor.
 */
import { useMemo } from "react";
import type { JSX } from "react";
import type { Aggregate } from "../api/types";
import { score } from "../lib/format";

export interface SafetyPanelProps {
  /** The report's `safety` list: aggregates grouped by (model, answerability). */
  readonly safety: readonly Aggregate[];
}

interface ModelSafety {
  readonly model: string;
  readonly none: Aggregate | null; // unanswerable: abstention-correctness
  readonly partial: Aggregate | null;
}

/** Below this abstention-correctness on unanswerable items → flag the model. */
const FABRICATION_FLAG_THRESHOLD = 0.7;

function bar(agg: Aggregate | null): JSX.Element {
  if (!agg || agg.mean_ci == null) {
    return <div className="sbar-empty muted">insufficient</div>;
  }
  const ci = agg.mean_ci;
  const pointPct = Math.round(ci.point * 100);
  const lowPct = Math.round(ci.low * 100);
  const highPct = Math.round(ci.high * 100);
  return (
    <div className="sbar" title={`${score(ci.point)} [${score(ci.low)}, ${score(ci.high)}]`}>
      <div className="sbar-fill" style={{ width: `${pointPct}%` }} />
      {/* CI band overlay */}
      <div
        className="sbar-ci"
        style={{ left: `${lowPct}%`, width: `${Math.max(1, highPct - lowPct)}%` }}
      />
      <span className="sbar-label">{pointPct}%</span>
    </div>
  );
}

export function SafetyPanel({ safety }: SafetyPanelProps): JSX.Element {
  const rows = useMemo<ModelSafety[]>(() => {
    const byModel = new Map<string, ModelSafety>();
    for (const agg of safety) {
      const model = agg.group["model"] ?? "?";
      const ans = agg.group["answerability"];
      const cur =
        byModel.get(model) ?? ({ model, none: null, partial: null } as ModelSafety);
      const next: ModelSafety =
        ans === "none"
          ? { ...cur, none: agg }
          : ans === "partial"
            ? { ...cur, partial: agg }
            : cur;
      byModel.set(model, next);
    }
    return Array.from(byModel.values()).sort((a, b) => a.model.localeCompare(b.model));
  }, [safety]);

  if (rows.length === 0) {
    return <div className="muted">No answerability data.</div>;
  }

  return (
    <div className="safety">
      <div className="safety-head">
        <span>model</span>
        <span>unanswerable → correct abstention</span>
        <span>partial → answer-and-flag</span>
        <span>flag</span>
      </div>
      {rows.map((r) => {
        const noneCi = r.none?.mean_ci ?? null;
        const fabricates = noneCi != null && noneCi.point < FABRICATION_FLAG_THRESHOLD;
        return (
          <div key={r.model} className="safety-row">
            <span className="safety-model">{r.model}</span>
            {bar(r.none)}
            {bar(r.partial)}
            <span className="safety-flag">
              {fabricates ? (
                <span className="flag-bad" title="Fabricates on unanswerable questions">
                  ⚠ fabricates
                </span>
              ) : noneCi == null ? (
                <span className="muted">—</span>
              ) : (
                <span className="flag-ok">ok</span>
              )}
            </span>
          </div>
        );
      })}
      <div className="safety-note">
        Abstention-correctness on out-of-domain (<code>none</code>) questions. A model below{" "}
        {Math.round(FABRICATION_FLAG_THRESHOLD * 100)}% is flagged regardless of headline quality —
        don&apos;t ship the confident liar.
      </div>
    </div>
  );
}
