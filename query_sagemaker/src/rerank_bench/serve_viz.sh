#!/usr/bin/env bash
# Launch every rerank_bench visualization at once.
#   ./serve_viz.sh start   - start all servers (static HTML + 4 Vite apps)
#   ./serve_viz.sh stop    - stop everything started by this script
#   ./serve_viz.sh status  - show what's listening
#
# Static HTML (one server at repo root, port 8001):
#   http://localhost:8001/index.html               (combo explorer, plotly CDN)
#   http://localhost:8001/combinations.html        (combinations view, plotly CDN)
#   http://localhost:8001/oss_bakeoff/dashboard.html (OSS bake-off dashboard)
# React/Vite apps:
#   dashboard          -> http://localhost:5173  (plotly)
#   bakeoff/dashboard  -> http://localhost:5174  (recharts, sample data)
#   bakeoff/web        -> http://localhost:5175  (recharts)
#   oss_bakeoff/console-> http://localhost:5176  (echarts, live data)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN="$ROOT/.viz_run"
mkdir -p "$RUN"

start_app() { # name dir port
  local name="$1" dir="$2" port="$3"
  ( cd "$ROOT/$dir" && bunx vite --port "$port" --strictPort >"$RUN/$name.log" 2>&1 &
    echo $! >"$RUN/$name.pid" )
  echo "  $name -> http://localhost:$port  (log: .viz_run/$name.log)"
}

case "${1:-start}" in
  start)
    echo "Starting static HTML server (port 8001)..."
    ( cd "$ROOT" && python3 -m http.server 8001 >"$RUN/static.log" 2>&1 &
      echo $! >"$RUN/static.pid" )
    echo "  http://localhost:8001/index.html"
    echo "  http://localhost:8001/combinations.html"
    echo "  http://localhost:8001/oss_bakeoff/dashboard.html"
    echo "Starting Vite apps..."
    start_app dashboard          dashboard           5173
    start_app bakeoff-dashboard  bakeoff/dashboard   5174
    start_app bakeoff-web        bakeoff/web         5175
    start_app oss-console        oss_bakeoff/console 5176
    echo "All launched. Run './serve_viz.sh status' to verify, './serve_viz.sh stop' to halt."
    ;;
  stop)
    for f in "$RUN"/*.pid; do
      [ -e "$f" ] || continue
      pid="$(cat "$f")"; name="$(basename "$f" .pid)"
      if kill "$pid" 2>/dev/null; then echo "stopped $name ($pid)"; fi
      rm -f "$f"
    done
    # vite spawns child esbuild; sweep the ports too
    for p in 8001 5173 5174 5175 5176; do
      lsof -ti tcp:"$p" 2>/dev/null | xargs -r kill 2>/dev/null || true
    done
    ;;
  status)
    for p in 8001 5173 5174 5175 5176; do
      if lsof -ti tcp:"$p" >/dev/null 2>&1; then echo "port $p: UP"; else echo "port $p: down"; fi
    done
    ;;
  *) echo "usage: $0 {start|stop|status}"; exit 1 ;;
esac
