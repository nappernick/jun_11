/**
 * usePromptBenchStream — live state for the Prompt Bench tab.
 *
 * Consumes the dedicated Prompt Bench SSE stream (``/api/promptbench/stream``) and the
 * status endpoint for durable backfill, accumulating per-prompt scatter points (one per
 * conversation), per-prompt aggregate results, and the crowned winner. Mirrors the
 * discipline of the optimizer hooks: the structural surface reconstructs from the status
 * poll, with live SSE deltas layered on top, so a reload never blanks the plots.
 *
 * Completely separate stream/endpoints from the optimizers — safe to watch while a v3 run
 * is live.
 */
import { useCallback, useEffect, useState } from "react";

export interface PromptBenchPoint {
  readonly conversation_index: number; // X (1..N)
  readonly overall: number; // Y (0..1)
  readonly item_id: string;
  readonly answerability: string;
  readonly turns: number;
}

export interface PromptBenchResult {
  readonly prompt_key: string;
  readonly label: string;
  readonly triad: number;
  readonly ci_half_width: number;
  readonly ci_low: number;
  readonly ci_high: number;
  readonly n_conversations: number;
  readonly per_dimension_mean: Record<string, number>;
  readonly abstention_reward_mean: number;
  readonly answered_when_unsure_rate: number;
  readonly confident_wrong_count: number;
}

export interface PromptBenchWinner {
  readonly prompt_key: string;
  readonly label: string;
  readonly triad: number;
  readonly tie_within_ci: boolean;
}

export interface PromptBenchPromptState {
  readonly key: string;
  readonly label: string;
  readonly text: string;
  readonly points: readonly PromptBenchPoint[];
  readonly result: PromptBenchResult | null;
  readonly failed: string | null;
}

export type PromptBenchStreamStatus = "connecting" | "open" | "closed";

export interface PromptBenchState {
  readonly status: string; // lifecycle: idle | running | completed | failed
  readonly prompts: readonly PromptBenchPromptState[];
  readonly winner: PromptBenchWinner | null;
  readonly model: string;
  readonly streamStatus: PromptBenchStreamStatus;
  readonly received: number;
  refresh: () => void;
  start: () => Promise<void>;
  reset: () => Promise<void>;
}

interface MutableState {
  readonly status: string;
  readonly prompts: Record<string, PromptBenchPromptState>;
  readonly winner: PromptBenchWinner | null;
  readonly model: string;
}

const EMPTY: MutableState = { status: "idle", prompts: {}, winner: null, model: "" };

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}

function ensurePrompt(
  prompts: Record<string, PromptBenchPromptState>,
  key: string,
  label: string,
  text = "",
): PromptBenchPromptState {
  return prompts[key] ?? { key, label, text, points: [], result: null, failed: null };
}

/** Merge a point into a prompt's series, keyed by conversation_index (upsert, sorted). */
function withPoint(
  prompt: PromptBenchPromptState,
  point: PromptBenchPoint,
): PromptBenchPromptState {
  const next = prompt.points.filter((p) => p.conversation_index !== point.conversation_index);
  next.push(point);
  next.sort((a, b) => a.conversation_index - b.conversation_index);
  return { ...prompt, points: next };
}

async function fetchStatus(signal?: AbortSignal): Promise<Record<string, unknown> | null> {
  try {
    const init: RequestInit = {};
    if (signal) init.signal = signal;
    const res = await fetch("/api/promptbench/status", init);
    if (!res.ok) return null;
    return (await res.json()) as Record<string, unknown>;
  } catch {
    return null;
  }
}

export function usePromptBenchStream(): PromptBenchState {
  const [streamStatus, setStreamStatus] = useState<PromptBenchStreamStatus>("connecting");
  const [received, setReceived] = useState(0);
  const [state, setState] = useState<MutableState>(EMPTY);

  // Durable backfill from the status endpoint (points + results + winner + lifecycle).
  const applyBackfill = useCallback((s: Record<string, unknown>) => {
    setState((prev) => {
      const prompts: Record<string, PromptBenchPromptState> = {};
      const points = isObj(s.points) ? (s.points as Record<string, PromptBenchPoint[]>) : {};
      const results = isObj(s.results) ? (s.results as Record<string, PromptBenchResult>) : {};
      const meta = isObj(s.prompts_meta)
        ? (s.prompts_meta as Record<string, { label: string; text: string }>)
        : {};
      const keys = new Set<string>([
        ...Object.keys(points),
        ...Object.keys(results),
        ...Object.keys(meta),
      ]);
      for (const key of keys) {
        const result = results[key] ?? null;
        const label = meta[key]?.label ?? result?.label ?? key.toUpperCase();
        const text = meta[key]?.text ?? prev.prompts[key]?.text ?? "";
        const merged = (points[key] ?? [])
          .map((p) => ({ ...p }))
          .sort((a, b) => a.conversation_index - b.conversation_index);
        prompts[key] = {
          key,
          label,
          text,
          points: merged,
          result,
          failed: prev.prompts[key]?.failed ?? null,
        };
      }
      return {
        status: typeof s.status === "string" ? s.status : prev.status,
        prompts,
        winner: (s.winner as PromptBenchWinner | null) ?? null,
        model: typeof s.model === "string" ? s.model : prev.model,
      };
    });
  }, []);

  const refresh = useCallback(() => {
    void fetchStatus().then((s) => {
      if (s) applyBackfill(s);
    });
  }, [applyBackfill]);

  const start = useCallback(async () => {
    await fetch("/api/promptbench/start", { method: "POST" });
    refresh();
  }, [refresh]);

  const reset = useCallback(async () => {
    await fetch("/api/promptbench/reset", { method: "POST" });
    setState(EMPTY);
    refresh();
  }, [refresh]);

  // Poll backfill every 3s (and once on mount).
  useEffect(() => {
    const ctrl = new AbortController();
    void fetchStatus(ctrl.signal).then((s) => {
      if (s) applyBackfill(s);
    });
    const id = window.setInterval(refresh, 3000);
    return () => {
      ctrl.abort();
      window.clearInterval(id);
    };
  }, [applyBackfill, refresh]);

  // Live SSE deltas.
  useEffect(() => {
    setStreamStatus("connecting");
    const es = new EventSource("/api/promptbench/stream");
    es.onopen = () => setStreamStatus("open");
    es.onerror = () => {
      setStreamStatus(es.readyState === EventSource.CLOSED ? "closed" : "connecting");
    };

    const bump = () => setReceived((n) => n + 1);

    function parse(msg: MessageEvent<string>): Record<string, unknown> | null {
      try {
        const d = JSON.parse(msg.data);
        return isObj(d) ? d : null;
      } catch {
        return null;
      }
    }

    const onPoint = (msg: MessageEvent<string>) => {
      const d = parse(msg);
      if (!d || typeof d["prompt_key"] !== "string") return;
      bump();
      const key = d["prompt_key"] as string;
      const label = (d["label"] as string) ?? key.toUpperCase();
      const point: PromptBenchPoint = {
        conversation_index: Number(d["conversation_index"] ?? 0),
        overall: Number(d["overall"] ?? 0),
        item_id: String(d["item_id"] ?? ""),
        answerability: String(d["answerability"] ?? ""),
        turns: Number(d["turns"] ?? 0),
      };
      setState((s) => ({
        ...s,
        status: "running",
        prompts: { ...s.prompts, [key]: withPoint(ensurePrompt(s.prompts, key, label), point) },
      }));
    };

    const onCompleted = (msg: MessageEvent<string>) => {
      const d = parse(msg);
      if (!d || typeof d["prompt_key"] !== "string") return;
      bump();
      const key = d["prompt_key"] as string;
      const result = d as unknown as PromptBenchResult;
      setState((s) => ({
        ...s,
        prompts: {
          ...s.prompts,
          [key]: { ...ensurePrompt(s.prompts, key, result.label ?? key.toUpperCase()), result },
        },
      }));
    };

    const onFailed = (msg: MessageEvent<string>) => {
      const d = parse(msg);
      if (!d || typeof d["prompt_key"] !== "string") return;
      bump();
      const key = d["prompt_key"] as string;
      const label = (d["label"] as string) ?? key.toUpperCase();
      setState((s) => ({
        ...s,
        prompts: {
          ...s.prompts,
          [key]: { ...ensurePrompt(s.prompts, key, label), failed: String(d["reason"] ?? "failed") },
        },
      }));
    };

    const onStatus = (msg: MessageEvent<string>) => {
      const d = parse(msg);
      if (!d) return;
      bump();
      applyBackfill(d);
    };

    const onStarted = (msg: MessageEvent<string>) => {
      const d = parse(msg);
      if (!d || typeof d["prompt_key"] !== "string") return;
      bump();
      const key = d["prompt_key"] as string;
      const label = (d["label"] as string) ?? key.toUpperCase();
      const text = (d["text"] as string) ?? "";
      setState((s) => {
        const cur = ensurePrompt(s.prompts, key, label, text);
        return {
          ...s,
          status: "running",
          prompts: { ...s.prompts, [key]: { ...cur, label, text: text || cur.text } },
        };
      });
    };

    es.addEventListener("promptbench_prompt_started", onStarted as EventListener);
    es.addEventListener("promptbench_point", onPoint as EventListener);
    es.addEventListener("promptbench_prompt_completed", onCompleted as EventListener);
    es.addEventListener("promptbench_prompt_failed", onFailed as EventListener);
    es.addEventListener("promptbench_status", onStatus as EventListener);

    return () => {
      es.removeEventListener("promptbench_prompt_started", onStarted as EventListener);
      es.removeEventListener("promptbench_point", onPoint as EventListener);
      es.removeEventListener("promptbench_prompt_completed", onCompleted as EventListener);
      es.removeEventListener("promptbench_prompt_failed", onFailed as EventListener);
      es.removeEventListener("promptbench_status", onStatus as EventListener);
      es.close();
      setStreamStatus("closed");
    };
  }, [applyBackfill]);

  const prompts = Object.values(state.prompts).sort((a, b) => a.key.localeCompare(b.key));
  return {
    status: state.status,
    prompts,
    winner: state.winner,
    model: state.model,
    streamStatus,
    received,
    refresh,
    start,
    reset,
  };
}
