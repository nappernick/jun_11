import { defineConfig } from "vite";

// The Python FastAPI backend (bakeoff/app.py) binds loopback :8200 by default.
// In dev, Vite serves the SPA on :5173 and proxies the API + SSE stream to the
// backend so the browser talks to one origin. In prod, `vite build` emits to
// `dist/`, which the backend serves at `/` (AD-4); no proxy is involved then.
// Allow an env override of the backend target without pulling in @types/node:
// read process.env defensively (it exists in the Node context Vite runs in).
const BACKEND =
  (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env
    ?.BAKEOFF_BACKEND ?? "http://127.0.0.1:8200";

export default defineConfig({
  // Build into bakeoff/ui/dist — exactly where bakeoff/app.py's DEFAULT_DIST_DIR
  // looks for the bundle (Path(__file__).parent / "ui" / "dist").
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: true,
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      // ws:false keeps these plain HTTP proxies; SSE must not be buffered.
      "/api": { target: BACKEND, changeOrigin: true, ws: false },
      "/exec": { target: BACKEND, changeOrigin: true },
      "/healthz": { target: BACKEND, changeOrigin: true },
    },
  },
});
