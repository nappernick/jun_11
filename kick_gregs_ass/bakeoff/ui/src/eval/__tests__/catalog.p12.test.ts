/**
 * Property 12: Out-of-scope metrics are excluded from the default enabled set and
 * labeled as out-of-scope.
 *
 * For the metric catalog: every `scope: "out"` entry is present in the catalog
 * (so it can be rendered + labeled out-of-scope) but is NEVER in the default
 * enabled set, and the default enabled set is exactly the in-scope entries. Every
 * entry is external methodology (Req 4.6 / P13).
 *
 * Validates: Requirements 4.3, 4.4
 */
import { describe, it, expect } from "vitest";
import fc from "fast-check";
import {
  EVAL_CATALOG,
  catalogByPriority,
  defaultEnabled,
  defaultEnabledNames,
  inScope,
  outOfScope,
  isEnabledByDefault,
} from "../catalog";

describe("Property 12: out-of-scope excluded from default enabled set + labeled (Req 4.3, 4.4)", () => {
  it("no out-of-scope metric is in the default enabled set", () => {
    const enabled = new Set(defaultEnabledNames());
    for (const entry of EVAL_CATALOG) {
      if (entry.scope === "out") {
        expect(enabled.has(entry.name)).toBe(false);
        expect(isEnabledByDefault(entry.name)).toBe(false);
      }
    }
  });

  it("the default enabled set is EXACTLY the in-scope entries", () => {
    const defaultNames = [...defaultEnabledNames()].sort();
    const inScopeNames = inScope()
      .map((e) => e.name)
      .sort();
    expect(defaultNames).toEqual(inScopeNames);
    // and every default-enabled entry is itself scope === "in".
    for (const e of defaultEnabled()) expect(e.scope).toBe("in");
  });

  it("out-of-scope metrics are still present in the catalog so they can be labeled", () => {
    const out = outOfScope();
    expect(out.length).toBeGreaterThan(0);
    for (const e of out) {
      expect(e.scope).toBe("out");
      // present in the full catalog (renderable + labelable as out-of-scope).
      expect(EVAL_CATALOG.some((c) => c.name === e.name)).toBe(true);
    }
    // the families the design marks out of scope are all represented.
    const families = new Set(out.map((e) => e.family));
    expect(families.has("multimodal")).toBe(true);
    expect(families.has("agentic")).toBe(true);
    expect(families.has("sql")).toBe(true);
  });

  it("every catalog entry is external methodology (Req 4.6 / P13)", () => {
    for (const e of EVAL_CATALOG) expect(e.external).toBe(true);
  });

  it("catalogByPriority sorts in-scope families ahead of out-of-scope ones", () => {
    const ordered = catalogByPriority();
    const lastInScopeIdx = ordered.reduce(
      (acc, e, i) => (e.scope === "in" ? i : acc),
      -1,
    );
    const firstOutIdx = ordered.findIndex((e) => e.scope === "out");
    expect(firstOutIdx).toBeGreaterThan(-1);
    expect(lastInScopeIdx).toBeLessThan(firstOutIdx);
  });

  it("property: for any catalog entry, scope===out ⟹ not default-enabled", () => {
    const names = EVAL_CATALOG.map((e) => e.name);
    fc.assert(
      fc.property(fc.constantFrom(...names), (name) => {
        const entry = EVAL_CATALOG.find((e) => e.name === name)!;
        if (entry.scope === "out") {
          expect(isEnabledByDefault(name)).toBe(false);
        } else {
          expect(isEnabledByDefault(name)).toBe(true);
        }
      }),
    );
  });
});
