import type { LayoutAxis } from "plotly.js";

/** Plotly's strict types require axis titles as objects, not strings */
export function ax(title: string, extra?: Partial<LayoutAxis>): Partial<LayoutAxis> {
  return { title: { text: title }, color: "#9aa4b2", ...extra } as Partial<LayoutAxis>;
}
