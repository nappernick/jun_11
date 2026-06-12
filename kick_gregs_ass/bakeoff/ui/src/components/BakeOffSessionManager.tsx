import { useEffect, useMemo, useState } from "react";
import type { JSX } from "react";
import {
  activateBakeOffSession,
  ApiError,
  createBakeOffSession,
  updateBakeOffSession,
} from "../api/client";
import type { BakeOffSession, BakeOffSessionsResponse, RunSnapshot } from "../api/types";

export interface BakeOffSessionManagerProps {
  readonly snapshot: RunSnapshot;
  readonly sessions: BakeOffSessionsResponse | null;
  readonly sessionError: string | null;
  readonly onRefreshSessions: () => Promise<void> | void;
}

function detailMessage(detail: unknown): string | null {
  if (detail && typeof detail === "object" && "detail" in detail) {
    const value = (detail as { detail: unknown }).detail;
    if (typeof value === "string") return value;
  }
  return null;
}

function basename(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  const lastPart = parts.length > 0 ? parts[parts.length - 1] : undefined;
  return lastPart ?? path;
}

function sessionBadge(session: BakeOffSession, activeSessionId: string): string {
  if (session.id === activeSessionId) return session.kind === "legacy" ? "Active legacy" : "Active";
  if (session.archived) return "Archived";
  return session.kind === "legacy" ? "Legacy" : "Session";
}

export function BakeOffSessionManager({
  snapshot,
  sessions,
  sessionError,
  onRefreshSessions,
}: BakeOffSessionManagerProps): JSX.Element {
  const [selectedSessionId, setSelectedSessionId] = useState<string>("");
  const [createLabel, setCreateLabel] = useState("Inline run");
  const [createNotes, setCreateNotes] = useState("");
  const [activeLabel, setActiveLabel] = useState("");
  const [activeNotes, setActiveNotes] = useState("");
  const [pendingAction, setPendingAction] = useState<
    "create" | "activate" | "archive" | "save" | null
  >(null);
  const [localError, setLocalError] = useState<string | null>(null);

  const activeSession = useMemo(() => {
    if (!sessions) return null;
    return sessions.sessions.find((session) => session.id === sessions.active_session_id) ?? null;
  }, [sessions]);

  const selectedSession = useMemo(() => {
    if (!sessions) return null;
    return (
      sessions.sessions.find((session) => session.id === selectedSessionId) ??
      activeSession ??
      sessions.sessions[0] ??
      null
    );
  }, [activeSession, selectedSessionId, sessions]);

  const isRunBusy = snapshot.status === "running" || snapshot.status === "paused";
  const isPending = pendingAction !== null;
  const disableSessionControls = isRunBusy || isPending;
  const errorMessage = localError ?? sessionError;

  useEffect(() => {
    if (!sessions?.active_session_id) return;
    setSelectedSessionId(sessions.active_session_id);
  }, [sessions?.active_session_id]);

  useEffect(() => {
    if (!activeSession) return;
    setActiveLabel(activeSession.label);
    setActiveNotes(activeSession.notes);
  }, [activeSession?.id]);

  useEffect(() => {
    if (!sessions) return;
    if (selectedSessionId) return;
    setSelectedSessionId(sessions.active_session_id);
  }, [selectedSessionId, sessions]);

  async function refreshSessions(): Promise<void> {
    await onRefreshSessions();
  }

  async function runAction(
    action: "create" | "activate" | "archive" | "save",
    handler: () => Promise<void>,
  ): Promise<void> {
    setPendingAction(action);
    setLocalError(null);
    try {
      await handler();
      await refreshSessions();
    } catch (error) {
      if (error instanceof ApiError) {
        setLocalError(detailMessage(error.detail) ?? error.message);
      } else {
        setLocalError(error instanceof Error ? error.message : String(error));
      }
    } finally {
      setPendingAction(null);
    }
  }

  async function handleCreate(): Promise<void> {
    await runAction("create", async () => {
      await createBakeOffSession({ label: createLabel, notes: createNotes });
      setCreateLabel("Inline run");
      setCreateNotes("");
    });
  }

  async function handleSaveActive(): Promise<void> {
    if (!activeSession) return;
    await runAction("save", async () => {
      await updateBakeOffSession(activeSession.id, {
        label: activeLabel,
        notes: activeNotes,
      });
    });
  }

  async function handleActivateSelected(): Promise<void> {
    if (!selectedSession || selectedSession.id === sessions?.active_session_id) return;
    await runAction("activate", async () => {
      await activateBakeOffSession(selectedSession.id);
    });
  }

  async function handleToggleArchiveSelected(): Promise<void> {
    if (!selectedSession || selectedSession.id === sessions?.active_session_id) return;
    await runAction("archive", async () => {
      await updateBakeOffSession(selectedSession.id, {
        archived: !selectedSession.archived,
      });
    });
  }

  const activeSessionLabel = activeSession
    ? sessionBadge(activeSession, sessions?.active_session_id ?? "")
    : "No session";

  if (!sessions) {
    return (
      <section className="session-manager panel">
        <div className="panel-head">
          <div>
            <h3>Bake-Off sessions</h3>
            <div className="ph-sub">Loading session registry…</div>
          </div>
        </div>
      </section>
    );
  }

  return (
    <section className="session-manager panel">
      <div className="panel-head">
        <div>
          <h3>Bake-Off sessions</h3>
          <div className="ph-sub">Active session controls and preserved legacy data</div>
        </div>
        <span className="session-badge">{activeSessionLabel}</span>
      </div>

      {errorMessage && <div className="session-error">{errorMessage}</div>}

      <div className="session-manager-grid">
        <section className="session-card">
          <div className="session-row">
            <div>
              <div className="field-label">Active session</div>
              <div className="session-title">{activeSession?.label ?? "No active session"}</div>
            </div>
            <span className="session-badge">
              {activeSession?.kind === "legacy" ? "Legacy" : "Session"}
            </span>
          </div>

          <div className="session-row">
            <label className="field">
              <span className="field-label">Label</span>
              <input
                className="field-input"
                type="text"
                value={activeLabel}
                disabled={disableSessionControls || !activeSession}
                onChange={(event) => setActiveLabel(event.target.value)}
              />
            </label>
            <label className="field">
              <span className="field-label">Notes</span>
              <textarea
                className="field-input session-notes"
                value={activeNotes}
                disabled={disableSessionControls || !activeSession}
                onChange={(event) => setActiveNotes(event.target.value)}
                rows={3}
              />
            </label>
          </div>

          <div className="session-actions">
            <button
              className="btn primary"
              disabled={disableSessionControls || !activeSession}
              onClick={() => void handleSaveActive()}
            >
              {pendingAction === "save" ? "Saving…" : "Save active"}
            </button>
          </div>

          {activeSession && (
            <div className="session-meta">
              <div>
                <span className="field-label">Prompt</span>
                <div>{basename(activeSession.prompt_path)}</div>
                <div className="muted">{activeSession.prompt_path}</div>
              </div>
              <div>
                <span className="field-label">Roster</span>
                <div>{activeSession.roster.join(" · ") || "-"}</div>
              </div>
              <div>
                <span className="field-label">Counts</span>
                <div>{activeSession.total_trials} trials · {activeSession.total_errors} errors · {activeSession.judge_scores_total} judge scores</div>
              </div>
              <div>
                <span className="field-label">Models seen</span>
                <div>{activeSession.models.join(" · ") || "-"}</div>
              </div>
            </div>
          )}
        </section>

        <section className="session-card">
          <div className="session-row">
            <div className="field">
              <span className="field-label">Create session</span>
              <input
                className="field-input"
                type="text"
                value={createLabel}
                disabled={disableSessionControls}
                onChange={(event) => setCreateLabel(event.target.value)}
                aria-label="new session label"
              />
            </div>
            <div className="field">
              <span className="field-label">Notes</span>
              <textarea
                className="field-input session-notes"
                value={createNotes}
                disabled={disableSessionControls}
                onChange={(event) => setCreateNotes(event.target.value)}
                rows={3}
                aria-label="new session notes"
              />
            </div>
          </div>

          <div className="session-actions">
            <button
              className="btn primary"
              disabled={disableSessionControls}
              onClick={() => void handleCreate()}
            >
              {pendingAction === "create" ? "Creating…" : "Create session"}
            </button>
          </div>

          <div className="session-row">
            <div className="field" style={{ flex: 1 }}>
              <span className="field-label">Select session</span>
              <select
                className="field-input"
                value={selectedSessionId || sessions.active_session_id}
                disabled={disableSessionControls}
                onChange={(event) => setSelectedSessionId(event.target.value)}
                aria-label="select bake-off session"
              >
                {sessions.sessions.map((session) => (
                  <option key={session.id} value={session.id}>
                    {session.label}
                    {session.archived ? " (archived)" : ""}
                    {session.id === sessions.active_session_id ? " (active)" : ""}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {selectedSession && (
            <>
              <div className="session-row">
                <div>
                  <div className="session-title">{selectedSession.label}</div>
                  <div className="session-notes">
                    {selectedSession.notes || "No notes"}
                  </div>
                </div>
                <span className="session-badge">{sessionBadge(selectedSession, sessions.active_session_id)}</span>
              </div>
              <div className="session-actions">
                <button
                  className="btn"
                  disabled={
                    disableSessionControls ||
                    selectedSession.id === sessions.active_session_id
                  }
                  onClick={() => void handleActivateSelected()}
                >
                  {pendingAction === "activate" ? "Activating…" : "Activate"}
                </button>
                {selectedSession.id !== sessions.active_session_id && (
                  <button
                    className="btn danger"
                    disabled={disableSessionControls}
                    onClick={() => void handleToggleArchiveSelected()}
                  >
                    {pendingAction === "archive"
                      ? selectedSession.archived
                        ? "Unarchiving…"
                        : "Archiving…"
                      : selectedSession.archived
                        ? "Unarchive"
                        : "Archive"}
                  </button>
                )}
              </div>
              <div className="session-meta">
                <div>
                  <span className="field-label">Created</span>
                  <div>{selectedSession.created_at || "-"}</div>
                </div>
                <div>
                  <span className="field-label">Updated</span>
                  <div>{selectedSession.updated_at || "-"}</div>
                </div>
                <div>
                  <span className="field-label">Root</span>
                  <div className="muted">{selectedSession.root}</div>
                </div>
                <div>
                  <span className="field-label">Prompt path</span>
                  <div className="muted">{selectedSession.prompt_path}</div>
                </div>
              </div>
            </>
          )}
        </section>
      </div>
    </section>
  );
}
