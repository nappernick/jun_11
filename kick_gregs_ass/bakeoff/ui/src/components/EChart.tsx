/**
 * A thin, strictly-typed React wrapper around an ECharts instance. Owns the full
 * lifecycle: init on mount, setOption on option change, resize via ResizeObserver,
 * dispose on unmount. Options are typed as EChartsOption, so a malformed chart
 * spec is a compile error. All data shaping happens in the views, never here.
 *
 * STICKY TOOLTIPS: these charts re-receive a fresh `option` on every live data
 * poll (1–4s), and each `setOption(..., {notMerge})` rebuilds the chart, which by
 * default dismisses whatever tooltip the user is hovering — so the hover state
 * "vanished after a few seconds." We keep it pinned two ways: (1) inject
 * `alwaysShowContent`/`enterable`/large `hideDelay` so the tooltip never auto-hides
 * on a timer and the cursor can enter it; (2) remember the last element the user
 * hovered and re-assert `showTip` after every re-render so a live update can't
 * clear it. The tip persists until the user hovers a different element.
 */
import { useEffect, useRef } from "react";
import type { JSX } from "react";
import * as echarts from "echarts";
import "echarts-gl";
import type { EChartsOption } from "echarts";

export interface EChartProps {
  readonly option: EChartsOption;
  readonly height?: number | string;
  readonly notMerge?: boolean;
  readonly className?: string;
  readonly ariaLabel?: string;
}

/** Merge sticky-tooltip defaults into an option's tooltip (only when one exists). */
function withStickyTooltip(option: EChartsOption): EChartsOption {
  const tooltip = (option as { tooltip?: unknown }).tooltip;
  // Leave charts that intentionally have no tooltip — or an (unusual) array of
  // tooltips — untouched.
  if (!tooltip || typeof tooltip !== "object" || Array.isArray(tooltip)) return option;
  const current = tooltip as Record<string, unknown>;
  return {
    ...option,
    tooltip: {
      ...current,
      alwaysShowContent: true,
      enterable: current.enterable ?? true,
      hideDelay: current.hideDelay ?? 100_000_000,
    },
  } as EChartsOption;
}

export function EChart({
  option,
  height = 280,
  notMerge = true,
  className,
  ariaLabel,
}: EChartProps): JSX.Element {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const instRef = useRef<echarts.ECharts | null>(null);
  // The last element the user hovered, re-asserted after each live re-render so
  // the tooltip survives data polls instead of vanishing.
  const lastHoverRef = useRef<{ seriesIndex: number; dataIndex: number } | null>(null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const inst = echarts.init(host, undefined, { renderer: "canvas" });
    instRef.current = inst;

    const onHover = (params: unknown): void => {
      const point = params as { seriesIndex?: number; dataIndex?: number };
      if (typeof point.seriesIndex === "number" && typeof point.dataIndex === "number") {
        lastHoverRef.current = { seriesIndex: point.seriesIndex, dataIndex: point.dataIndex };
      }
    };
    inst.on("mouseover", onHover);

    const ro = new ResizeObserver(() => inst.resize());
    ro.observe(host);

    return () => {
      ro.disconnect();
      inst.off("mouseover", onHover);
      inst.dispose();
      instRef.current = null;
    };
  }, []);

  useEffect(() => {
    const inst = instRef.current;
    if (!inst) return;
    inst.setOption(withStickyTooltip(option), { notMerge });
    // Re-show the last-hovered tooltip so the live re-render above doesn't dismiss
    // it — this is what makes the hover state persist across data polls.
    const last = lastHoverRef.current;
    if (last) {
      inst.dispatchAction({
        type: "showTip",
        seriesIndex: last.seriesIndex,
        dataIndex: last.dataIndex,
      });
    }
  }, [option, notMerge]);

  return (
    <div
      ref={hostRef}
      className={className}
      role="img"
      aria-label={ariaLabel}
      style={{ width: "100%", height }}
    />
  );
}
