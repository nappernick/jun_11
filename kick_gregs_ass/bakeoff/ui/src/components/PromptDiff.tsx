/**
 * Renders a unified-diff string (the optimizer's `prompt_diff`, produced by
 * Python's `difflib.unified_diff`) as colored add/remove/context lines.
 *
 * Used by the Per_Model_View to show the diff of the current prompt against a
 * prior version (Req 9.5). Pure presentation — the diff text is computed
 * server-side and arrives either on `optimizer_iteration_completed.prompt_diff`
 * (live) or on a `PromptVersion.diff` from the history endpoint (lookback).
 */
import type { JSX } from "react";

export interface PromptDiffProps {
  readonly diff: string;
}

type LineKind = "add" | "del" | "hunk" | "meta" | "ctx";

function classify(line: string): LineKind {
  if (line.startsWith("+++") || line.startsWith("---")) return "meta";
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "del";
  return "ctx";
}

export function PromptDiff({ diff }: PromptDiffProps): JSX.Element {
  const trimmed = diff.replace(/\n$/, "");
  if (!trimmed) {
    return <div className="muted opt-diff-empty">No change from the prior version.</div>;
  }
  const lines = trimmed.split("\n");
  return (
    <pre className="opt-diff" aria-label="Prompt diff against the prior version">
      {lines.map((line, i) => (
        <code key={i} className={`opt-diff-line ${classify(line)}`}>
          {line === "" ? " " : line}
        </code>
      ))}
    </pre>
  );
}
