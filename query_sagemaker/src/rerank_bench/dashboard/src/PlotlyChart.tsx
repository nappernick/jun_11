import { useEffect, useRef } from "react";
import Plotly from "plotly.js-dist-min";
import type { Layout, Data, Config } from "plotly.js";

const BASE_LAYOUT: Partial<Layout> = {
  paper_bgcolor: "#161a22",
  plot_bgcolor: "#161a22",
  font: { color: "#cdd3dc", size: 11, family: "system-ui, sans-serif" },
  margin: { t: 44, r: 16, b: 52, l: 60 },
};

interface Props {
  data: Data[];
  layout?: Partial<Layout>;
  style?: React.CSSProperties;
}

export default function PlotlyChart({ data, layout, style }: Props) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    const merged: Partial<Layout> = {
      ...BASE_LAYOUT,
      ...layout,
      legend: { font: { size: 9, color: "#cdd3dc" }, ...(layout?.legend ?? {}) },
    };
    const config: Partial<Config> = { responsive: true, displayModeBar: false };
    Plotly.react(ref.current, data, merged, config);
  }, [data, layout]);

  return (
    <div
      ref={ref}
      style={{ width: "100%", minHeight: 320, ...style }}
    />
  );
}
