// Central data loader for the bake-off console.
// Fetches every /data/*.json artifact at runtime. ragas_results.json and
// combo5_results.json are optional (those runs may still be mid-flight) and
// resolve to null on 404.
//
// LIVE auto-refresh: every REFRESH_MS the loader re-fetches all artifacts. It
// compares the raw response *text* per file (cheaper than re-parsing 2MB pools)
// and only calls setData when at least one file actually changed — and even then
// it reuses the previous object reference for every unchanged field so panels
// that memoize on a sub-field do not needlessly re-init their echarts instances.

import { useEffect, useRef, useState } from 'react';
import type { Scored, Metrics, Judge, Latency, Pools, Ragas, Combo5 } from '../types';

// The shared contract consumed by every panel component:
//   export default function Name({ data }: { data: LoadedData })
export interface LoadedData {
  scored: Scored;
  metrics: Metrics;
  judge: Judge;
  latency: Latency;
  pools: Pools;
  ragas: Ragas | null;
  combo5: Combo5 | null;
}

const BASE = `${import.meta.env.BASE_URL}data`;
const REFRESH_MS = 20_000;

// Names of every artifact in fixed order; used for the raw-text change cache.
const REQUIRED_FILES = ['scored.json', 'metrics.json', 'judge.json', 'latency_gpu.json', 'pools.json'] as const;
const OPTIONAL_FILES = ['ragas_results.json', 'combo5_results.json'] as const;

// Raw fetch returning response text (or null for an absent optional file).
async function fetchText(name: string, optional: boolean): Promise<string | null> {
  const response = await fetch(`${BASE}/${name}`, { cache: 'no-store' });
  if (!response.ok) {
    if (optional) {
      return null;
    }
    throw new Error(`Failed to fetch ${name}: ${response.status} ${response.statusText}`);
  }
  return await response.text();
}

async function fetchOptionalText(name: string): Promise<string | null> {
  try {
    return await fetchText(name, true);
  } catch {
    return null;
  }
}

export interface UseDataResult {
  data: LoadedData | null;
  loading: boolean;
  error: string | null;
}

// One snapshot of every artifact's raw text. null = absent optional file.
type TextSnapshot = Record<string, string | null>;

async function fetchSnapshot(): Promise<TextSnapshot> {
  const requiredTexts = await Promise.all(
    REQUIRED_FILES.map((name) => fetchText(name, false)),
  );
  const optionalTexts = await Promise.all(
    OPTIONAL_FILES.map((name) => fetchOptionalText(name)),
  );
  const snapshot: TextSnapshot = {};
  REQUIRED_FILES.forEach((name, index) => {
    snapshot[name] = requiredTexts[index];
  });
  OPTIONAL_FILES.forEach((name, index) => {
    snapshot[name] = optionalTexts[index];
  });
  return snapshot;
}

// Parse a single required field, reusing the previous parsed value when the raw
// text is byte-identical to last cycle (so the LoadedData field keeps its
// reference identity and downstream useMemos do not recompute).
function parseField<T>(
  name: string,
  snapshot: TextSnapshot,
  previousText: TextSnapshot | null,
  previousValue: T | undefined,
): T {
  const text = snapshot[name];
  if (text === null) {
    throw new Error(`Required artifact ${name} resolved to null`);
  }
  if (previousText && previousValue !== undefined && previousText[name] === text) {
    return previousValue;
  }
  return JSON.parse(text) as T;
}

function parseOptionalField<T>(
  name: string,
  snapshot: TextSnapshot,
  previousText: TextSnapshot | null,
  previousValue: T | null | undefined,
): T | null {
  const text = snapshot[name];
  if (text === null) {
    return null;
  }
  if (previousText && previousValue !== undefined && previousValue !== null && previousText[name] === text) {
    return previousValue;
  }
  // Optional files degrade to null on any parse failure. This covers two real
  // cases: (1) a dev server that returns its index.html SPA fallback with HTTP
  // 200 for a still-absent file, and (2) a file caught mid-copy (partial JSON).
  try {
    return JSON.parse(text) as T;
  } catch {
    return null;
  }
}

export function useData(): UseDataResult {
  const [data, setData] = useState<LoadedData | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  // Last raw-text snapshot and last parsed LoadedData, kept in refs so the
  // polling closure can compare without re-subscribing the effect.
  const lastTextRef = useRef<TextSnapshot | null>(null);
  const lastDataRef = useRef<LoadedData | null>(null);

  useEffect(() => {
    let cancelled = false;

    // Build a fresh LoadedData from a snapshot, reusing unchanged references.
    // Returns null when nothing changed (so the caller can skip setData).
    function buildIfChanged(snapshot: TextSnapshot): LoadedData | null {
      const previousText = lastTextRef.current;
      const previousData = lastDataRef.current;

      const changed =
        previousText === null ||
        [...REQUIRED_FILES, ...OPTIONAL_FILES].some((name) => previousText[name] !== snapshot[name]);

      if (!changed && previousData) {
        return null;
      }

      const next: LoadedData = {
        scored: parseField<Scored>('scored.json', snapshot, previousText, previousData?.scored),
        metrics: parseField<Metrics>('metrics.json', snapshot, previousText, previousData?.metrics),
        judge: parseField<Judge>('judge.json', snapshot, previousText, previousData?.judge),
        latency: parseField<Latency>('latency_gpu.json', snapshot, previousText, previousData?.latency),
        pools: parseField<Pools>('pools.json', snapshot, previousText, previousData?.pools),
        ragas: parseOptionalField<Ragas>('ragas_results.json', snapshot, previousText, previousData?.ragas),
        combo5: parseOptionalField<Combo5>('combo5_results.json', snapshot, previousText, previousData?.combo5),
      };

      lastTextRef.current = snapshot;
      lastDataRef.current = next;
      return next;
    }

    // Initial load: sets loading/error. Background polls never touch those.
    async function initialLoad() {
      try {
        const snapshot = await fetchSnapshot();
        if (cancelled) {
          return;
        }
        const next = buildIfChanged(snapshot);
        if (next) {
          setData(next);
        }
        setLoading(false);
      } catch (caught) {
        if (cancelled) {
          return;
        }
        setError(caught instanceof Error ? caught.message : String(caught));
        setLoading(false);
      }
    }

    // Background poll: silent on transient failure (keeps last-good data).
    async function poll() {
      try {
        const snapshot = await fetchSnapshot();
        if (cancelled) {
          return;
        }
        const next = buildIfChanged(snapshot);
        if (next) {
          setData(next);
        }
      } catch {
        // Transient refresh failure — keep showing the last-good snapshot.
      }
    }

    void initialLoad();
    const intervalId = window.setInterval(() => {
      void poll();
    }, REFRESH_MS);

    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, []);

  return { data, loading, error };
}
