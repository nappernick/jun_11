/**
 * Property 5: Agent-to-color mapping is stable and injective.
 *
 * Same agent ⟹ same color regardless of the order ids arrive in (stability), and
 * the mapping is injective for N <= palette size (no two agents share a color).
 *
 * Validates: Requirements 10.7
 */
import { describe, it, expect } from "vitest";
import fc from "fast-check";
import { buildAgentColorMap, AGENT_PALETTE } from "../agentColor";

const agentIdArb = fc.string({ minLength: 1, maxLength: 8 });

describe("Property 5: agent→color mapping is stable + injective (Req 10.7)", () => {
  it("is stable across arrival order: same agent ⟹ same color regardless of permutation", () => {
    fc.assert(
      fc.property(
        fc.uniqueArray(agentIdArb, { minLength: 1, maxLength: 12 }),
        fc.array(fc.nat(), { minLength: 0, maxLength: 12 }),
        (ids, shuffleSeed) => {
          // Build a shuffled permutation of the same id set.
          const shuffled = [...ids];
          for (let i = shuffled.length - 1; i > 0; i--) {
            const j = (shuffleSeed[i % shuffleSeed.length] ?? i) % (i + 1);
            [shuffled[i], shuffled[j]] = [shuffled[j]!, shuffled[i]!];
          }
          const a = buildAgentColorMap(ids);
          const b = buildAgentColorMap(shuffled);
          for (const id of ids) {
            expect(b.get(id)).toBe(a.get(id));
          }
        },
      ),
    );
  });

  it("is injective for N <= palette size: distinct agents get distinct colors", () => {
    fc.assert(
      fc.property(
        fc.uniqueArray(agentIdArb, { minLength: 1, maxLength: AGENT_PALETTE.length }),
        (ids) => {
          const map = buildAgentColorMap(ids);
          const colors = ids.map((id) => map.get(id)!);
          expect(new Set(colors).size).toBe(ids.length);
        },
      ),
    );
  });

  it("duplicate ids collapse to a single stable entry", () => {
    const map = buildAgentColorMap(["A", "A", "B", "B", "B"]);
    expect(map.size).toBe(2);
    expect(map.get("A")).toBeDefined();
    expect(map.get("B")).toBeDefined();
    expect(map.get("A")).not.toBe(map.get("B"));
  });

  it("the four-agent headline case maps A/B/C/D to the first four palette colors", () => {
    const map = buildAgentColorMap(["D", "C", "B", "A"]); // arrival order irrelevant
    expect(map.get("A")).toBe(AGENT_PALETTE[0]);
    expect(map.get("B")).toBe(AGENT_PALETTE[1]);
    expect(map.get("C")).toBe(AGENT_PALETTE[2]);
    expect(map.get("D")).toBe(AGENT_PALETTE[3]);
  });
});
