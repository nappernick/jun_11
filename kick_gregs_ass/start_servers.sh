#!/usr/bin/env bash

echo "Starting static file server for HTML files on http://localhost:8000..."
python3 -m http.server 8000 &
STATIC_PID=$!

echo "Starting React dashboard via Vite on http://localhost:5173..."
cd bakeoff/ui
bun install
bun run dev &
VITE_PID=$!

echo ""
echo "====================================================="
echo "✅ Servers are running."
echo ""
echo "HTML files available at:"
echo "  - Alluvial Preview:   http://localhost:8000/data/synthetic/cohort_alluvial_preview.html"
echo "  - Sunburst Preview:   http://localhost:8000/data/synthetic/cohort_sunburst.html"
echo "  - Eval Console Insp:  http://localhost:8000/frontend-inspiration/Model%20Eval%20Console.html"
echo ""
echo "React App (Dashboard) available at:"
echo "  - Dashboard:          http://localhost:5173"
echo "====================================================="
echo ""
echo "Press Ctrl+C to stop all servers."

# Wait for user to Ctrl+C, then kill both
trap "echo 'Stopping servers...'; kill $STATIC_PID $VITE_PID; exit" SIGINT SIGTERM
wait
