/**
 * Cohort heatmap (Req 11.4) — model × cohort-dimension grid, cells colored by
 * quality with the CI width encoded as opacity: a wide CI (or insufficient_data)
 * renders faded, so thin-data cells never masquerade as strong signal. Clicking a
 * cell surfaces its underlying numbers (the drill-down seam).
 */
import { useMemo, useState } from "react";
import type { JSX } from "react";
import type { Aggregate } from "../api/types";
import { score, count } from "../lib/format";

export interface CohortHeatmapProps {
  readonly dimension: string;
  readonly cells: readonly Aggregate[];
}

interface Cell {
  readonly model: string;
  readonly bucket: string;
  readonly agg: Aggregate;
}

/** Opacity from CI width: tighter CI → more opaque; insufficient → very faded. */
function cellOpacity(agg: Aggregate): number {
  if (agg.insufficient_data || agg.mean_ci == null) return 0.14;
  const width = agg.mean_ci.high - agg.mean_ci.low;
  // width 0 → 1.0, width 0.5+ → ~0.3
  return Math.max(0.3, 1 - width * 1.4);
}

function qufrom(agg: Aggregate): number {
  return agg.mean_ci?.point ?? 0;
}

/** Green-ish for high quality, red-ish for low (hue 0..130 over [0,1]). */
function cellColor(agg: Aggregate): string {
  const q = qufrom(agg);
  const hue = Math.round(q * 130); // 0=red .. 130=green
  return `oklch(0.7 0.13 ${hue} / ${cellOpacity(agg)})`;
}

export function CohortHeatmap({ dimension, cells }: CohortHeatmapProps): JSX.Element {
  const [selected, setSelected] = useState<Cell | null>(null);

  const { models, buckets, byKey } = useMemo(() => {
    const ms = Array.from(new Set(cells.map((c) => c.group["model"] ?? "?"))).sort();
    const bs = Array.from(new Set(cells.map((c) => c.group[dimension] ?? "?"))).sort();
    const map = new Map<string, Aggregate>();
    for (const c of cells) {
      map.set(`${c.group["model"]}\u0000${c.group[dimension]}`, c);
    }
    return { models: ms, buckets: bs, byKey: map };
  }, [cells, dimension]);

  if (cells.length === 0) {
    return <div className="muted">No data for {dimension}.</div>;
  }

  return (
    <div>
      <div className="heatmap" role="table" aria-label={`Quality by model and ${dimension}`}>
        <div className="hm-row hm-head" role="row">
          <div className="hm-corner" role="columnheader">
            model \ {dimension}
          </div>
          {buckets.map((b) => (
            <div key={b} className="hm-cell hm-colhead" role="columnheader">
              {b}
            </div>
          ))}
        </div>
        {models.map((model) => (
          <div key={model} className="hm-row" role="row">
            <div className="hm-rowhead" role="rowheader">
              {model}
            </div>
            {buckets.map((bucket) => {
              const agg = byKey.get(`${model}\u0000${bucket}`);
              if (!agg) {
                return <div key={bucket} className="hm-cell hm-empty" role="cell" />;
              }
              const faded = agg.insufficient_data || agg.mean_ci == null;
              return (
                <button
                  key={bucket}
                  className="hm-cell"
                  role="cell"
                  style={{ background: cellColor(agg) }}
                  title={
                    faded
                      ? `${model} · ${bucket}: insufficient data (n=${agg.n_items})`
                      : `${model} · ${bucket}: ${score(qufrom(agg))} (n=${agg.n_items})`
                  }
                  onClick={() => setSelected({ model, bucket, agg })}
                >
                  {faded ? "·" : score(qufrom(agg), 2)}
                </button>
              );
            })}
          </div>
        ))}
      </div>

      {selected && (
        <div className="drill" role="region" aria-label="Cell detail">
          <b>
            {selected.model} · {dimension}={selected.bucket}
          </b>
          {selected.agg.mean_ci ? (
            <span>
              {" "}
              quality {score(selected.agg.mean_ci.point)} [{score(selected.agg.mean_ci.low)},{" "}
              {score(selected.agg.mean_ci.high)}] · {selected.agg.mean_ci.method}
            </span>
          ) : (
            <span className="muted"> insufficient data — no confident value</span>
          )}
          <span>
            {" "}
            · items {count(selected.agg.n_items)} · trials {count(selected.agg.n_trials)}
          </span>
        </div>
      )}
    </div>
  );
}
