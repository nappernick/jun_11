/**
 * RunPanel — lets you launch rerank_bench.py directly from the UI.
 *
 * The dashboard is a static Vite app, so it can't exec Python itself.
 * Instead it:
 *  1. Builds the full CLI command from the form state
 *  2. Shows it as a copyable command string
 *  3. Optionally opens a terminal via a shell:// deep link (macOS only, works in VS Code / iTerm)
 *
 * The "Copy" button is the primary CTA — paste into any terminal.
 * The results/ directory watcher at the bottom auto-reloads the file list
 * so you can pick a fresh result file once the run completes.
 */
import { useCallback, useRef, useState } from "react";

// The 10 corpus-grounded queries derived from query_chunks.jsonl
const CORPUS_QUERIES: string[] = [
  "how do I book a flight and hotel for a business trip",
  "can I upgrade to business class and what does Amazon reimburse",
  "what size rental car am I allowed and what happens if I have an accident",
  "how do I set up my travel profile and book a seat assignment",
  "do I need a visa and how do I get travel approval for international trips",
  "what expense category do I use for airfare hotel and meals in Concur",
  "how do I cancel or change a flight booking I already made",
  "can I extend a business trip for personal travel and will Amazon pay",
  "how do I request a travel accommodation for a medical condition",
  "can I use my personal frequent flyer miles to upgrade and keep the points",
];

type Mode = "pool_sweep" | "combo";

interface RunConfig {
  mode: Mode;
  queries: string[];
  pools: string;          // space-separated, e.g. "5 10 20"
  comboK: string;
  comboBase: string;
  comboCap: string;
  profile: string;
  models: string[];          // CLI tokens: "3.5", "4pro"
  separateRuns: boolean;     // true => one command per model; false => head-to-head
}

const MODEL_OPTIONS: { token: string; label: string; note: string }[] = [
  { token: "3.5", label: "Rerank 3.5 (Bedrock)", note: "alpha acct · us-west-2 · on-demand" },
  { token: "4fast", label: "Rerank 4.0 Fast (SageMaker)", note: "CAIA acct · us-east-1 · DEPLOYED (cohere-rerank4-fast-sandbox)" },
  { token: "4pro", label: "Rerank 4.0 Pro (SageMaker)", note: "CAIA acct · us-east-1 · NOT deployed (torn down) — deploy first" },
];

const DEFAULT_CONFIG: RunConfig = {
  mode: "pool_sweep",
  queries: CORPUS_QUERIES.slice(0, 5),
  pools: "5 10 20",
  comboK: "5",
  comboBase: "12",
  comboCap: "1000",
  profile: "alpha",
  models: ["3.5", "4fast"],
  separateRuns: false,
};

interface Props {
  /** Called when user wants to load a specific results file */
  onLoadFile?: (path: string) => void;
}

export default function RunPanel({ onLoadFile }: Props) {
  const [cfg, setCfg] = useState<RunConfig>(DEFAULT_CONFIG);
  const [copied, setCopied] = useState(false);
  const [open, setOpen] = useState(false);
  const [customQuery, setCustomQuery] = useState("");
  const textRef = useRef<HTMLTextAreaElement>(null);

  const set = useCallback(<K extends keyof RunConfig>(k: K, v: RunConfig[K]) => {
    setCfg((prev) => ({ ...prev, [k]: v }));
  }, []);

  const toggleQuery = useCallback((q: string) => {
    setCfg((prev) => ({
      ...prev,
      queries: prev.queries.includes(q)
        ? prev.queries.filter((x) => x !== q)
        : [...prev.queries, q],
    }));
  }, []);

  const addCustomQuery = useCallback(() => {
    const q = customQuery.trim();
    if (!q || cfg.queries.includes(q)) return;
    set("queries", [...cfg.queries, q]);
    setCustomQuery("");
  }, [customQuery, cfg.queries, set]);

  const toggleModel = useCallback((token: string) => {
    setCfg((prev) => ({
      ...prev,
      models: prev.models.includes(token)
        ? prev.models.filter((x) => x !== token)
        : [...prev.models, token],
    }));
  }, []);

  // Build the CLI command string
  const command = useCallback((): string => {
    // Build one command for a given set of model tokens.
    const buildOne = (models: string[]): string => {
      const parts = ["python rerank_bench.py"];
      if (cfg.queries.length > 0) {
        const qargs = cfg.queries.map((q) => `"${q}"`).join(" ");
        parts.push(`-q ${qargs}`);
      }
      // Always emit --models explicitly so the run is unambiguous.
      if (models.length > 0) {
        parts.push(`--models ${models.join(" ")}`);
      }
      if (cfg.mode === "pool_sweep") {
        const pools = cfg.pools.trim().split(/\s+/).filter(Boolean);
        if (pools.join(" ") !== "5 10 20") {
          parts.push(`--pools ${pools.join(" ")}`);
        }
      } else {
        parts.push(`--combo ${cfg.comboK}`);
        parts.push(`--combo-base ${cfg.comboBase}`);
        parts.push(`--combo-cap ${cfg.comboCap}`);
      }
      return parts.join(" \\\n  ");
    };

    if (cfg.models.length === 0) return "# select at least one model";
    // Head-to-head: one run scores the SAME candidates with every model.
    // Separate: one independent run per model (two output files).
    if (cfg.separateRuns) {
      return cfg.models.map((m) => buildOne([m])).join("\n\n");
    }
    return buildOne(cfg.models);
  }, [cfg]);

  const copy = useCallback(async () => {
    await navigator.clipboard.writeText(command());
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [command]);

  // Estimated combo count for warning
  const estimatedCombos = useCallback((): number | null => {
    if (cfg.mode !== "combo") return null;
    const k = parseInt(cfg.comboK, 10);
    const base = parseInt(cfg.comboBase, 10);
    if (!k || !base || k > base) return null;
    // C(base, k)
    let result = 1;
    for (let i = 0; i < k; i++) result = (result * (base - i)) / (i + 1);
    return Math.round(result);
  }, [cfg]);

  const combos = estimatedCombos();
  const combosPerQuery = combos ?? 0;
  const totalCombos = combosPerQuery * cfg.queries.length;

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        style={{
          background: "#21262d", color: "#cdd3dc",
          border: "1px solid #30363d", borderRadius: 6,
          padding: "6px 14px", cursor: "pointer", fontSize: 12,
        }}
      >
        ▶ Run bench
      </button>
    );
  }

  return (
    <div style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,0.7)", zIndex: 200,
      display: "flex", alignItems: "flex-start", justifyContent: "center",
      paddingTop: 40, overflowY: "auto",
    }}
      onClick={(e) => { if (e.target === e.currentTarget) setOpen(false); }}
    >
      <div style={{
        background: "#161a22", border: "1px solid #30363d", borderRadius: 10,
        width: "min(780px, 96vw)", padding: 24, display: "flex", flexDirection: "column", gap: 16,
      }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <h2 style={{ margin: 0, fontSize: 15, fontWeight: 600 }}>Configure &amp; run benchmark</h2>
          <button onClick={() => setOpen(false)} style={{ background: "none", border: "none", color: "#9aa4b2", fontSize: 18, cursor: "pointer" }}>✕</button>
        </div>

        {/* Model selector */}
        <div>
          <div className="form-label">Models ({cfg.models.length} selected)</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {MODEL_OPTIONS.map((m) => (
              <label key={m.token} style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 13, cursor: "pointer", color: cfg.models.includes(m.token) ? "#e6e6e6" : "#6a737d" }}>
                <input
                  type="checkbox"
                  checked={cfg.models.includes(m.token)}
                  onChange={() => toggleModel(m.token)}
                  style={{ marginTop: 3, accentColor: "#58a6ff" }}
                />
                <span>
                  {m.label}
                  <span style={{ display: "block", fontSize: 11, color: "#6a737d" }}>{m.note}</span>
                </span>
              </label>
            ))}
          </div>
          {cfg.models.length === 0 && (
            <div style={{ fontSize: 11, color: "#f78166", marginTop: 4 }}>Select at least one model.</div>
          )}
        </div>

        {/* Run style — only meaningful with 2+ models */}
        {cfg.models.length > 1 && (
          <div>
            <div className="form-label">Run style</div>
            <div style={{ display: "flex", gap: 8 }}>
              {[
                { val: false, label: "Head-to-head", desc: "one run · same candidates scored by every model" },
                { val: true, label: "Independent runs", desc: "one separate run (and output file) per model" },
              ].map((opt) => (
                <button
                  key={String(opt.val)}
                  onClick={() => set("separateRuns", opt.val)}
                  title={opt.desc}
                  style={{
                    background: cfg.separateRuns === opt.val ? "#1c212b" : "transparent",
                    color: cfg.separateRuns === opt.val ? "#58a6ff" : "#9aa4b2",
                    border: cfg.separateRuns === opt.val ? "1px solid #2c333f" : "1px solid transparent",
                    borderRadius: 6, padding: "5px 14px", cursor: "pointer", fontSize: 13,
                  }}
                >
                  {opt.label}
                </button>
              ))}
            </div>
            <div style={{ fontSize: 11, color: "#6a737d", marginTop: 4 }}>
              {cfg.separateRuns
                ? "Two commands are generated — each model runs alone against its own retrieval."
                : "Head-to-head is apples-to-apples: identical candidates go to both models in one run."}
            </div>
          </div>
        )}

        {/* Mode selector */}
        <div>
          <div className="form-label">Run mode</div>
          <div style={{ display: "flex", gap: 8 }}>
            {(["pool_sweep", "combo"] as Mode[]).map((m) => (
              <button
                key={m}
                onClick={() => set("mode", m)}
                style={{
                  background: cfg.mode === m ? "#1c212b" : "transparent",
                  color: cfg.mode === m ? "#58a6ff" : "#9aa4b2",
                  border: cfg.mode === m ? "1px solid #2c333f" : "1px solid transparent",
                  borderRadius: 6, padding: "5px 14px", cursor: "pointer", fontSize: 13,
                }}
              >
                {m === "pool_sweep" ? "Pool sweep" : "Combinations"}
              </button>
            ))}
          </div>
          <div style={{ fontSize: 11, color: "#6a737d", marginTop: 4 }}>
            {cfg.mode === "pool_sweep"
              ? "Sweep across pool sizes. Good for latency vs quality tradeoff curves."
              : "Every K-combination of the base candidate pool. Good for dominance analysis."}
          </div>
        </div>

        {/* Mode-specific params */}
        {cfg.mode === "pool_sweep" ? (
          <div>
            <div className="form-label">Pool sizes (space-separated)</div>
            <input
              value={cfg.pools}
              onChange={(e) => set("pools", e.target.value)}
              style={inputStyle}
              placeholder="5 10 20 40"
            />
          </div>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12 }}>
            <div>
              <div className="form-label">Combo K (chunk set size)</div>
              <input value={cfg.comboK} onChange={(e) => set("comboK", e.target.value)} style={inputStyle} placeholder="5" />
            </div>
            <div>
              <div className="form-label">Base pool size</div>
              <input value={cfg.comboBase} onChange={(e) => set("comboBase", e.target.value)} style={inputStyle} placeholder="12" />
            </div>
            <div>
              <div className="form-label">Combo cap per query</div>
              <input value={cfg.comboCap} onChange={(e) => set("comboCap", e.target.value)} style={inputStyle} placeholder="1000" />
            </div>
          </div>
        )}

        {/* Combo volume warning */}
        {cfg.mode === "combo" && combos !== null && (
          <div style={{
            fontSize: 12, padding: "8px 12px", borderRadius: 6,
            background: totalCombos > 2000 ? "#3a1414" : "#1a3a24",
            border: `1px solid ${totalCombos > 2000 ? "#6b2d2d" : "#2d6b3a"}`,
            color: totalCombos > 2000 ? "#f78166" : "#3fb950",
          }}>
            C({cfg.comboBase},{cfg.comboK}) = <strong>{combos.toLocaleString()}</strong> combos/query ×{" "}
            {cfg.queries.length} quer{cfg.queries.length === 1 ? "y" : "ies"} ={" "}
            <strong>{totalCombos.toLocaleString()}</strong> total Bedrock calls
            {totalCombos > parseInt(cfg.comboCap, 10) * cfg.queries.length && (
              <> — some queries will be aborted by --combo-cap</>
            )}
          </div>
        )}

        {/* Query selector */}
        <div>
          <div className="form-label">Queries ({cfg.queries.length} selected)</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 4, maxHeight: 240, overflowY: "auto" }}>
            {CORPUS_QUERIES.map((q) => (
              <label key={q} style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 12, cursor: "pointer", color: cfg.queries.includes(q) ? "#e6e6e6" : "#6a737d" }}>
                <input
                  type="checkbox"
                  checked={cfg.queries.includes(q)}
                  onChange={() => toggleQuery(q)}
                  style={{ marginTop: 2, accentColor: "#58a6ff" }}
                />
                {q}
              </label>
            ))}
          </div>
          {/* Custom query */}
          <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
            <input
              value={customQuery}
              onChange={(e) => setCustomQuery(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") addCustomQuery(); }}
              placeholder="Add a custom query…"
              style={{ ...inputStyle, flex: 1 }}
            />
            <button onClick={addCustomQuery} style={btnStyle}>Add</button>
          </div>
          {/* Custom queries (ones not in the preset list) */}
          {cfg.queries.filter((q) => !CORPUS_QUERIES.includes(q)).map((q) => (
            <div key={q} style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 12, color: "#cdd3dc", marginTop: 4 }}>
              <span style={{ flex: 1 }}>{q}</span>
              <button
                onClick={() => set("queries", cfg.queries.filter((x) => x !== q))}
                style={{ background: "none", border: "none", color: "#6a737d", cursor: "pointer", fontSize: 14 }}
              >✕</button>
            </div>
          ))}
        </div>

        {/* Generated command */}
        <div>
          <div className="form-label">Generated command — run from <code>rerank_bench/</code></div>
          <div style={{ position: "relative" }}>
            <textarea
              ref={textRef}
              readOnly
              value={command()}
              rows={Math.min(8, command().split("\n").length + 1)}
              style={{
                width: "100%", background: "#0d1117", color: "#e6e6e6",
                border: "1px solid #30363d", borderRadius: 6,
                padding: "10px 12px", fontFamily: '"SF Mono","Fira Code",monospace',
                fontSize: 12, resize: "vertical", boxSizing: "border-box",
              }}
            />
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
            <button onClick={copy} style={{ ...btnStyle, minWidth: 80 }}>
              {copied ? "✓ Copied" : "Copy command"}
            </button>
            <span style={{ fontSize: 11, color: "#6a737d", alignSelf: "center" }}>
              Output will be saved to <code>results/</code> with a timestamped filename
            </span>
          </div>
        </div>

        {/* After run: load a result file */}
        {onLoadFile && (
          <div>
            <div className="form-label">Load a result file after running</div>
            <div style={{ display: "flex", gap: 8 }}>
              <input
                id="result-file-input"
                type="file"
                accept=".json"
                style={{ display: "none" }}
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) {
                    onLoadFile(file.name);
                    setOpen(false);
                  }
                }}
              />
              <button
                onClick={() => document.getElementById("result-file-input")?.click()}
                style={btnStyle}
              >
                Browse results/
              </button>
              <span style={{ fontSize: 11, color: "#6a737d", alignSelf: "center" }}>
                Pick the <code>.json</code> file that was just generated
              </span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

const inputStyle: React.CSSProperties = {
  background: "#0d1117", color: "#e6e6e6",
  border: "1px solid #30363d", borderRadius: 6,
  padding: "5px 10px", fontSize: 12, width: "100%", boxSizing: "border-box",
};

const btnStyle: React.CSSProperties = {
  background: "#21262d", color: "#cdd3dc",
  border: "1px solid #30363d", borderRadius: 6,
  padding: "5px 12px", cursor: "pointer", fontSize: 12, whiteSpace: "nowrap",
};
