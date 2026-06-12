import { useCallback, useEffect, useRef, useState } from "react";
import { parseFile } from "./dataUtils";
import type { Dataset } from "./types";
import ComparisonTab from "./ComparisonTab";
import PoolSweepTab from "./PoolSweepTab";
import RunPanel from "./RunPanel";

type Tab = "compare" | "pool";

export default function App() {
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [activeFile, setActiveFile] = useState<string>("");
  const [available, setAvailable] = useState<{ filename: string }[]>([]);
  const [tab, setTab] = useState<Tab>("compare");
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // Load a result file by name from the served results/ directory (no drag-drop).
  const loadByName = useCallback((filename: string) => {
    setDatasets((prev) => {
      if (prev.some((d) => d.filename === filename)) {
        setActiveFile(filename);
        return prev;
      }
      fetch(`./results/${filename}`)
        .then((r) => r.json())
        .then((raw) => {
          const ds = parseFile(filename, raw);
          setDatasets((cur) => [...cur.filter((d) => d.filename !== filename), ds]);
          setActiveFile(filename);
        })
        .catch(() => {});
      return prev;
    });
  }, []);

  // On mount, auto-discover runs via results/manifest.json and load the newest.
  useEffect(() => {
    fetch("./results/manifest.json")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error("no manifest"))))
      .then((list: { filename: string }[]) => {
        setAvailable(list);
        if (list.length) loadByName(list[0].filename);
      })
      .catch(() => {});
  }, [loadByName]);

  const loadFile = useCallback((file: File) => {
    file.text().then((text) => {
      const raw = JSON.parse(text);
      const ds = parseFile(file.name, raw);
      setDatasets((prev) => {
        const filtered = prev.filter((d) => d.filename !== ds.filename);
        return [...filtered, ds];
      });
      setActiveFile(ds.filename);
      setTab("compare");
    });
  }, []);

  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files[0];
    if (file) loadFile(file);
  }, [loadFile]);

  const onFileInput = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) loadFile(file);
  }, [loadFile]);

  const active = datasets.find((d) => d.filename === activeFile);

  const statusParts: string[] = [];
  if (active) {
    statusParts.push(`${active.rows.length.toLocaleString()} records`);
    statusParts.push(`${new Set(active.rows.map((r) => r.query)).size} queries`);
    if (active.meta?.models && active.meta.models.length) {
      statusParts.push(`models: ${active.meta.models.join(" vs ")}`);
    }
    if (active.meta?.run_mode === "combo") {
      statusParts.push(`combo k=${active.meta.combo_k} base=${active.meta.combo_base}`);
    } else if (active.meta?.pools) {
      statusParts.push(`pools: ${active.meta.pools.join(", ")}`);
    }
  }

  return (
    <div style={{ minHeight: "100vh", background: "#0f1115", color: "#e6e6e6", fontFamily: "system-ui, sans-serif" }}>
      {/* Header */}
      <header style={{ padding: "12px 20px", background: "#161a22", borderBottom: "1px solid #262b36", display: "flex", alignItems: "center", gap: 16 }}>
        <div style={{ flexGrow: 1 }}>
          <h1 style={{ margin: 0, fontSize: 17, fontWeight: 600 }}>Cohere Rerank 3.5 vs 4 Pro — Benchmark Explorer</h1>
          <div style={{ fontSize: 11, color: "#9aa4b2", marginTop: 3 }}>
            {active ? statusParts.join(" · ") : "Load a results JSON file to begin"}
          </div>
        </div>

        {/* File selector */}
        {available.length > 1 && (
          <select
            value={activeFile}
            onChange={(e) => loadByName(e.target.value)}
            style={{ background: "#1c212b", color: "#e6e6e6", border: "1px solid #2c333f", borderRadius: 4, padding: "4px 8px", fontSize: 12 }}
          >
            {available.map((d) => (
              <option key={d.filename} value={d.filename}>{d.filename}</option>
            ))}
          </select>
        )}

        <RunPanel onLoadFile={() => {
          inputRef.current?.click();
        }} />
        <button
          onClick={() => inputRef.current?.click()}
          style={{ background: "#21262d", color: "#cdd3dc", border: "1px solid #30363d", borderRadius: 6, padding: "6px 14px", cursor: "pointer", fontSize: 12 }}
        >
          Load JSON
        </button>
        <input ref={inputRef} type="file" accept=".json" style={{ display: "none" }} onChange={onFileInput} />
      </header>

      {/* Tab bar */}
      {active && (
        <div style={{ display: "flex", gap: 2, padding: "8px 20px", background: "#12151c", borderBottom: "1px solid #1e2330" }}>
          {(["compare", "pool"] as Tab[]).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              style={{
                background: tab === t ? "#1c212b" : "transparent",
                color: tab === t ? "#58a6ff" : "#9aa4b2",
                border: tab === t ? "1px solid #2c333f" : "1px solid transparent",
                borderRadius: 6,
                padding: "5px 14px",
                cursor: "pointer",
                fontSize: 13,
              }}
            >
              {t === "compare" ? "Comparison (3.5 vs 4 Pro)" : "Pool Sweep"}
            </button>
          ))}
        </div>
      )}

      {/* Drop zone (shown when no file loaded) */}
      {!active && (
        <div
          onDrop={onDrop}
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onClick={() => inputRef.current?.click()}
          style={{
            margin: "40px auto",
            maxWidth: 480,
            padding: 48,
            textAlign: "center",
            border: `2px dashed ${dragOver ? "#58a6ff" : "#2c333f"}`,
            borderRadius: 12,
            color: "#9aa4b2",
            cursor: "pointer",
            transition: "border-color 0.15s",
          }}
        >
          <div style={{ fontSize: 32, marginBottom: 12 }}>📂</div>
          <div style={{ fontSize: 14 }}>Drop a <code>results/*.json</code> here</div>
          <div style={{ fontSize: 12, marginTop: 8 }}>or click to browse</div>
          <div style={{ fontSize: 11, marginTop: 16, color: "#6a737d" }}>
            Works with single-model and 3.5-vs-4-Pro runs (and old flat-array files)
          </div>
        </div>
      )}

      {/* Drop zone overlay when dragging onto page with a file already loaded */}
      {active && (
        <div
          onDrop={onDrop}
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          style={{ display: dragOver ? "flex" : "none", position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", zIndex: 100, alignItems: "center", justifyContent: "center", pointerEvents: dragOver ? "all" : "none" }}
        >
          <div style={{ color: "#58a6ff", fontSize: 20, border: "2px dashed #58a6ff", borderRadius: 12, padding: "32px 64px" }}>
            Drop to load
          </div>
        </div>
      )}

      {/* Main content */}
      {active && (
        <div style={{ padding: "12px 16px" }}>
          {tab === "compare" && <ComparisonTab dataset={active} />}
          {tab === "pool" && <PoolSweepTab dataset={active} />}
        </div>
      )}
    </div>
  );
}
