/**
 * Provenance footer (Req 11.7) — carried by every exec view and every static
 * export. Reads the report's `provenance` block verbatim: plan_version, n_items,
 * total trials, judge model, judge↔human agreement, CI method, and date. This is
 * the defensibility surface — "show me the data behind this" resolves here.
 */
import type { JSX } from "react";
import type { ExecProvenance } from "../api/types";
import { count, score } from "../lib/format";

export interface ProvenanceFooterProps {
  readonly provenance: ExecProvenance;
}

function judgeModelLabel(judge: string | readonly string[]): string {
  return Array.isArray(judge) ? judge.join(", ") : String(judge);
}

export function ProvenanceFooter({ provenance: p }: ProvenanceFooterProps): JSX.Element {
  const agreement = Object.entries(p.judge_human_agreement);
  return (
    <div className="prov" aria-label="Provenance footer">
      <div className="prov-row">
        <span><b>plan</b> {p.plan_version}</span>
        <span><b>items</b> {count(p.n_items)}</span>
        <span><b>trials</b> {count(p.n_trials)}</span>
        <span><b>judge</b> {judgeModelLabel(p.judge_model)}</span>
        <span><b>CI</b> {p.ci_method} @ {Math.round(p.ci_level * 100)}%</span>
        <span><b>generated</b> {new Date(p.generated_at).toLocaleString()}</span>
      </div>
      <div className="prov-row prov-agree">
        <span className="prov-label">judge↔human agreement:</span>
        {agreement.length === 0 ? (
          <span className="muted">not calibrated</span>
        ) : (
          agreement.map(([dim, rho]) => (
            <span key={dim} className="agree-badge" title={`Spearman ρ for ${dim}`}>
              {dim} ρ={score(rho, 2)}
            </span>
          ))
        )}
      </div>
    </div>
  );
}
