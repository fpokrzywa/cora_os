import { useCallback, useEffect, useState } from "react";
import { adminGetTrace, adminListTraces } from "../api";
import type { RuntimeTrace } from "../types";

const TRACE_TYPES = [
  "",
  "memory_intent",
  "memory_retrieval",
  "tool_intent",
  "forge_tool",
  "llm_chat",
  "manual_tool",
  "execution_plan_created",
];

const STATUSES = ["", "ok", "error", "denied", "confirmation_required", "not_configured"];

export function Traces() {
  const [rows, setRows] = useState<RuntimeTrace[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [traceType, setTraceType] = useState<string>("");
  const [agent, setAgent] = useState<string>("");
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [sessionFilter, setSessionFilter] = useState<string>("");

  const [selected, setSelected] = useState<RuntimeTrace | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await adminListTraces({
        limit: 200,
        trace_type: traceType || undefined,
        selected_agent: agent || undefined,
        status: statusFilter || undefined,
        session_id: sessionFilter.trim() || undefined,
      });
      setRows(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setLoading(false);
    }
  }, [traceType, agent, statusFilter, sessionFilter]);

  useEffect(() => {
    const handle = window.setTimeout(refresh, 250);
    return () => window.clearTimeout(handle);
  }, [refresh]);

  const openDetail = useCallback(async (id: string) => {
    try {
      const t = await adminGetTrace(id);
      setSelected(t);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load trace");
    }
  }, []);

  return (
    <main className="admin">
      <header className="admin__header">
        <h1>Runtime Traces</h1>
        <p className="admin__subtitle">
          One row per chat turn / tool run / MCP call. Click a row for the
          full JSON payload.
        </p>
      </header>

      {error && <div className="admin__error">{error}</div>}

      <section className="admin__section">
        <div className="admin__section-head">
          <h2>Filters</h2>
          <button
            className="btn btn--ghost btn--sm"
            onClick={refresh}
            disabled={loading}
          >
            ↻
          </button>
        </div>
        <div className="admin__form">
          <div className="admin__form-row">
            <label>
              <span>Trace type</span>
              <select
                value={traceType}
                onChange={(e) => setTraceType(e.target.value)}
              >
                {TRACE_TYPES.map((t) => (
                  <option key={t || "any"} value={t}>
                    {t || "any"}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Agent</span>
              <input
                type="text"
                value={agent}
                onChange={(e) => setAgent(e.target.value)}
                placeholder="Cora, FORGE, …"
              />
            </label>
            <label>
              <span>Status</span>
              <select
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value)}
              >
                {STATUSES.map((s) => (
                  <option key={s || "any"} value={s}>
                    {s || "any"}
                  </option>
                ))}
              </select>
            </label>
            <label className="admin__field-wide">
              <span>Session id</span>
              <input
                type="text"
                value={sessionFilter}
                onChange={(e) => setSessionFilter(e.target.value)}
                placeholder="full UUID"
              />
            </label>
          </div>
        </div>
      </section>

      <section className="admin__section">
        <table className="admin__table">
          <thead>
            <tr>
              <th>When</th>
              <th>Session</th>
              <th>Agent</th>
              <th>Type</th>
              <th>Tool</th>
              <th>Model</th>
              <th>Memory</th>
              <th>Duration</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((t) => (
              <tr
                key={t.id}
                className="trace-row"
                onClick={() => openDetail(t.id)}
              >
                <td className="muted">
                  {new Date(t.created_at).toLocaleString()}
                </td>
                <td className="mono muted">
                  {t.session_id ? t.session_id.slice(0, 8) : "—"}
                </td>
                <td className="mono">{t.selected_agent ?? "—"}</td>
                <td className="mono">{t.trace_type}</td>
                <td className="mono muted">{t.tool_name ?? "—"}</td>
                <td className="mono muted">{t.model_name ?? "—"}</td>
                <td className="muted">
                  {(() => {
                    const md = memoryRetrievalDetail(t);
                    if (md) {
                      const mode = md.retrieval_mode ?? "—";
                      const sem = md.semantic_matches ?? 0;
                      const kw = md.keyword_matches ?? 0;
                      const inj = md.memories_injected ?? t.memory_count;
                      return (
                        <span
                          className={`retrieval-pill retrieval-pill--${retrievalModeVariant(mode)}`}
                          title={`semantic ${sem} · keyword ${kw} · injected ${inj}`}
                        >
                          {mode} · {inj}
                        </span>
                      );
                    }
                    const wm = workspaceMetadata(t);
                    if (wm && t.trace_type === "llm_chat") {
                      const ctxOk = wm.workspace_context_injected === true;
                      return (
                        <span className="trace-ws-summary">
                          {t.memory_count > 0 && (
                            <span className="memory-pill">
                              {t.memory_count}{" "}
                              {t.memory_count === 1 ? "memory" : "memories"}
                            </span>
                          )}
                          {wm.workspace_name && (
                            <span
                              className="agent-badge agent-badge--cora"
                              title={`workspace ${wm.workspace_id ?? ""}`}
                            >
                              ws: {wm.workspace_name}
                            </span>
                          )}
                          <span
                            className={`retrieval-pill retrieval-pill--${ctxOk ? "hybrid" : "off"}`}
                            title={
                              ctxOk
                                ? `${wm.workspace_context_chars ?? 0} chars injected`
                                : wm.workspace_context_error ?? "no context"
                            }
                          >
                            ctx: {ctxOk ? "yes" : "no"}
                          </span>
                        </span>
                      );
                    }
                    return t.memory_count > 0 ? (
                      <span className="memory-pill">
                        {t.memory_count}{" "}
                        {t.memory_count === 1 ? "memory" : "memories"}
                      </span>
                    ) : (
                      "—"
                    );
                  })()}
                </td>
                <td className="muted">
                  {t.duration_ms != null ? `${t.duration_ms}ms` : "—"}
                </td>
                <td>
                  <span className={`status-chip status-chip--${traceStatusVariant(t.status)}`}>
                    {t.status}
                  </span>
                </td>
              </tr>
            ))}
            {!loading && rows.length === 0 && (
              <tr>
                <td colSpan={9} className="muted">
                  No traces match these filters.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      {selected && (
        <TraceDetailModal trace={selected} onClose={() => setSelected(null)} />
      )}
    </main>
  );
}

function traceStatusVariant(s: string): string {
  if (s === "ok") return "active";
  if (s === "error" || s === "denied" || s === "not_configured") return "archived";
  return "draft";
}

interface MemoryRetrievalDetail {
  retrieval_mode?: string;
  semantic_enabled?: boolean;
  semantic_status?: string;
  semantic_matches?: number;
  keyword_matches?: number;
  memories_injected?: number;
  semantic_scores?: number[];
  memory_ids?: string[];
}

function memoryRetrievalDetail(t: RuntimeTrace): MemoryRetrievalDetail | null {
  if (t.trace_type !== "memory_retrieval") return null;
  const tr = t.tool_result;
  if (!tr || typeof tr !== "object") return null;
  return tr as MemoryRetrievalDetail;
}

function retrievalModeVariant(mode?: string): string {
  if (mode === "semantic") return "semantic";
  if (mode === "hybrid") return "hybrid";
  if (mode === "fallback_keyword") return "fallback";
  if (mode === "keyword") return "keyword";
  return "off";
}

interface WorkspaceMetadata {
  workspace_context_injected?: boolean;
  workspace_id?: string | null;
  workspace_name?: string | null;
  workspace_context_chars?: number;
  workspace_context_sources?: {
    memory_count?: number;
    embedded_memory_count?: number;
    active_plans_count?: number;
    queued_jobs_count?: number;
    available_agents_count?: number;
    available_tools_count?: number;
    healthy_mcp_servers_count?: number;
  };
  workspace_context_error?: string;
}

function workspaceMetadata(t: RuntimeTrace): WorkspaceMetadata | null {
  if (!t.metadata || typeof t.metadata !== "object") return null;
  const m = t.metadata as WorkspaceMetadata;
  if (
    m.workspace_context_injected === undefined &&
    m.workspace_name === undefined &&
    m.workspace_id === undefined
  ) {
    return null;
  }
  return m;
}

function TraceDetailModal({
  trace,
  onClose,
}: {
  trace: RuntimeTrace;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const onBackdrop = (e: React.MouseEvent<HTMLDivElement>) => {
    if (e.target === e.currentTarget) onClose();
  };

  return (
    <div className="modal-backdrop" onClick={onBackdrop}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="Trace detail"
      >
        <header className="modal__header">
          <h2 className="modal__title">
            {trace.trace_type}{" "}
            <span className="mono muted">{trace.id.slice(0, 8)}</span>
          </h2>
          <button
            className="modal__close"
            onClick={onClose}
            aria-label="Close"
          >
            ×
          </button>
        </header>
        <div className="modal__meta">
          <span className={`status-chip status-chip--${traceStatusVariant(trace.status)}`}>
            {trace.status}
          </span>
          {trace.selected_agent && (
            <span className="mono">{trace.selected_agent}</span>
          )}
          {trace.duration_ms != null && (
            <span className="muted">{trace.duration_ms}ms</span>
          )}
          {trace.session_id && (
            <span className="mono muted">
              session {trace.session_id.slice(0, 8)}
            </span>
          )}
          <span className="muted">
            {new Date(trace.created_at).toLocaleString()}
          </span>
        </div>
        {(() => {
          const md = memoryRetrievalDetail(trace);
          if (!md) return null;
          return (
            <div className="modal__meta retrieval-summary">
              <span
                className={`retrieval-pill retrieval-pill--${retrievalModeVariant(md.retrieval_mode)}`}
              >
                mode: {md.retrieval_mode ?? "—"}
              </span>
              <span className="mono muted">
                semantic {md.semantic_matches ?? 0}
              </span>
              <span className="mono muted">
                keyword {md.keyword_matches ?? 0}
              </span>
              <span className="mono muted">
                injected {md.memories_injected ?? 0}
              </span>
              {md.semantic_status && (
                <span className="mono muted">
                  status: {md.semantic_status}
                </span>
              )}
              {md.semantic_scores && md.semantic_scores.length > 0 && (
                <span className="mono muted">
                  scores: {md.semantic_scores.map((s) => s.toFixed(2)).join(", ")}
                </span>
              )}
            </div>
          );
        })()}
        {(() => {
          const wm = workspaceMetadata(trace);
          if (!wm) return null;
          const ctxOk = wm.workspace_context_injected === true;
          const src = wm.workspace_context_sources;
          return (
            <div className="modal__meta retrieval-summary">
              <span
                className={`retrieval-pill retrieval-pill--${ctxOk ? "hybrid" : "off"}`}
              >
                workspace ctx: {ctxOk ? "yes" : "no"}
              </span>
              {wm.workspace_name && (
                <span className="mono">{wm.workspace_name}</span>
              )}
              {wm.workspace_id && (
                <span className="mono muted">
                  id {wm.workspace_id.slice(0, 8)}
                </span>
              )}
              {ctxOk && (
                <span className="mono muted">
                  {wm.workspace_context_chars ?? 0} chars
                </span>
              )}
              {!ctxOk && wm.workspace_context_error && (
                <span className="mono muted">
                  err: {wm.workspace_context_error}
                </span>
              )}
              {src && (
                <span className="mono muted">
                  mem {src.memory_count ?? 0}/{src.embedded_memory_count ?? 0}
                  {" · "}plans {src.active_plans_count ?? 0}
                  {" · "}jobs {src.queued_jobs_count ?? 0}
                  {" · "}agents {src.available_agents_count ?? 0}
                  {" · "}tools {src.available_tools_count ?? 0}
                  {" · "}mcp {src.healthy_mcp_servers_count ?? 0}
                </span>
              )}
            </div>
          );
        })()}
        {trace.metadata && Object.keys(trace.metadata).length > 0 && (
          <div className="modal__body">
            <h4 className="admin__vt-h3">Metadata</h4>
            <pre className="trace-json">
              {JSON.stringify(trace.metadata, null, 2)}
            </pre>
          </div>
        )}
        <div className="modal__body">
          <pre className="trace-json">{JSON.stringify(trace, null, 2)}</pre>
        </div>
      </div>
    </div>
  );
}
