/** Small, dependency-free formatting helpers shared across views. */

/** Milliseconds -> a compact human string (e.g. 372 ms, 1.84 s). */
export function ms(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  if (value < 1000) return `${value.toFixed(0)} ms`;
  return `${(value / 1000).toFixed(2)} s`;
}

/** A unit-interval score -> fixed-precision string, or em dash for null. */
export function score(value: number | null | undefined, digits = 3): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return value.toFixed(digits);
}

/** Integer with thousands separators. */
export function count(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return value.toLocaleString("en-US");
}

/** Percent (0..1 -> "73%"). */
export function pct(value: number | null | undefined, digits = 0): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

/**
 * Deterministic categorical color for a model id (stable across renders).
 * Hue from a cheap string hash; lightness/chroma tuned for the dark UI. OKLCH so
 * the palette stays perceptually even regardless of how many models there are.
 */
export function modelColor(modelId: string): string {
  let h = 0;
  for (let i = 0; i < modelId.length; i++) {
    h = (h * 31 + modelId.charCodeAt(i)) % 360;
  }
  return `oklch(0.74 0.142 ${h})`;
}
