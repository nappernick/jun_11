// Reusable 2D echarts wrapper.
// Mounts one instance from `option`, re-applies on option change (notMerge so re-sorts
// don't leave stale series), resizes via ResizeObserver, disposes on unmount.

import { useEffect, useRef } from 'react';
import * as echarts from 'echarts';
import type { EChartsOption } from 'echarts';

export default function EChart({
  option,
  style,
}: {
  option: EChartsOption;
  style?: React.CSSProperties;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const instanceRef = useRef<echarts.ECharts | null>(null);

  // Mount / unmount the instance and wire the resize observer.
  useEffect(() => {
    const container = containerRef.current;
    if (!container) {
      return;
    }
    const instance = echarts.init(container, undefined, { renderer: 'canvas' });
    instanceRef.current = instance;

    const observer = new ResizeObserver(() => {
      instance.resize();
    });
    observer.observe(container);

    return () => {
      observer.disconnect();
      instance.dispose();
      instanceRef.current = null;
    };
  }, []);

  // Re-apply the option whenever it changes.
  useEffect(() => {
    const instance = instanceRef.current;
    if (!instance) {
      return;
    }
    instance.setOption(option, { notMerge: true });
  }, [option]);

  return <div ref={containerRef} style={{ width: '100%', height: 320, ...style }} />;
}
