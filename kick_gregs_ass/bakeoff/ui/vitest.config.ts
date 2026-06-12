import { defineConfig } from "vitest/config";

/**
 * Minimal Vitest config for the eval dashboard.
 *
 * The chart-option builders, composite-quality logic, axis mapping, and selectors
 * are all pure functions tested as plain option objects / data structures, so the
 * default `node` environment is sufficient (no jsdom). Property tests use fast-check.
 */
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.{test,spec}.ts", "src/**/__tests__/**/*.ts"],
  },
});
