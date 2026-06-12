/**
 * Static HTML snapshot export (Req 11.7).
 *
 * A leave-behind: serialize the current exec view's key numbers + the provenance
 * footer into a self-contained, downloadable HTML file. The point is that an
 * exported artifact still carries its provenance (plan_version, n_items, trials,
 * judge, judge↔human agreement, CI method, date) — a number that leaves the live
 * tool never loses the context that makes it defensible.
 */
import type { ExecReport, FrontierPoint } from "../api/types";

function esc(s: string): string {
  return s.replace(/[&<>"]/g, (c) =>
    c === "&" ? "&amp;" : c === "<" ? "&lt;" : c === ">" ? "&gt;" : "&quot;",
  );
}

function frontierRows(frontier: readonly FrontierPoint[]): string {
  return frontier
    .map((fp) => {
      const q = fp.quality;
      const quality = q ? `${q.point.toFixed(3)} [${q.low.toFixed(3)}, ${q.high.toFixed(3)}]` : "—";
      return `<tr><td>${esc(fp.model)}</td><td>${fp.on_pareto_front ? "Pareto" : "dominated"}</td>` +
        `<td>${fp.speed_p50_ms.toFixed(0)} ms</td><td>${fp.speed_p90_ms.toFixed(0)} ms</td>` +
        `<td>${quality}</td></tr>`;
    })
    .join("");
}

export function buildSnapshotHtml(report: ExecReport): string {
  const p = report.provenance;
  const judge = Array.isArray(p.judge_model) ? p.judge_model.join(", ") : String(p.judge_model);
  const agree = Object.entries(p.judge_human_agreement)
    .map(([d, r]) => `${esc(d)} ρ=${r.toFixed(2)}`)
    .join(" · ") || "not calibrated";
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>GBBO exec snapshot — ${esc(p.plan_version)}</title>
<style>
body{font:14px/1.5 system-ui,sans-serif;margin:32px;color:#0b0f14}
h1{font-size:18px} table{border-collapse:collapse;margin:12px 0}
th,td{border:1px solid #cdd5dd;padding:6px 10px;text-align:left}
.prov{margin-top:20px;padding:12px;background:#f4f6f8;border-radius:8px;font-size:12px;color:#3a4754}
.prov b{color:#0b0f14}
</style></head><body>
<h1>GBBO Model Bake-Off — executive snapshot</h1>
<h2>Speed / Quality frontier</h2>
<table><thead><tr><th>model</th><th>frontier</th><th>speed p50</th><th>speed p90</th><th>quality (CI)</th></tr></thead>
<tbody>${frontierRows(report.frontier)}</tbody></table>
<div class="prov">
<b>plan</b> ${esc(p.plan_version)} &nbsp; <b>items</b> ${p.n_items} &nbsp; <b>trials</b> ${p.n_trials}<br>
<b>judge</b> ${esc(judge)} &nbsp; <b>CI</b> ${esc(p.ci_method)} @ ${Math.round(p.ci_level * 100)}%<br>
<b>judge↔human agreement</b> ${esc(agree)}<br>
<b>generated</b> ${esc(new Date(p.generated_at).toLocaleString())}
</div>
<p style="font-size:11px;color:#6b7785;margin-top:16px">
Evaluation metrics &amp; judge calibration are general industry practice, pending internal validation
before any number defends a decision upward.</p>
</body></html>`;
}

/** Trigger a client-side download of the static snapshot. */
export function downloadSnapshot(report: ExecReport): void {
  const blob = new Blob([buildSnapshotHtml(report)], { type: "text/html" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `gbbo-exec-${report.provenance.plan_version}.html`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
