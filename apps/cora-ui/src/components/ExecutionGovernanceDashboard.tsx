import { useCallback, useEffect, useState } from "react";
import { getExecutionGovernanceDashboard } from "../api";
import type { GovernanceDashboard, GovernanceTraceRow } from "../types";

function fmt(ts: string | null) {
  return ts ? new Date(ts).toLocaleString() : "—";
}

function TraceTable({ rows }: { rows: GovernanceTraceRow[] }) {
  if (rows.length === 0) return <div className="admin__hint">None.</div>;
  return (
    <table className="admin__table">
      <thead>
        <tr>
          <th>When</th>
          <th>Trace</th>
          <th>Status</th>
          <th>Provider</th>
          <th>Action</th>
          <th>Reason</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i}>
            <td className="muted">{fmt(r.created_at)}</td>
            <td>{r.trace_type}</td>
            <td>{r.status}</td>
            <td className="muted">{r.provider_name ?? "—"}</td>
            <td className="muted">{r.action_type ?? "—"}</td>
            <td className="muted" style={{ fontSize: 12 }}>{r.reason ?? "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function ExecutionGovernanceDashboard() {
  const [data, setData] = useState<GovernanceDashboard | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [provider, setProvider] = useState("");
  const [action, setAction] = useState("");
  const [statusFilter, setStatusFilter] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(
        await getExecutionGovernanceDashboard({
          provider_name: provider || undefined,
          action_type: action || undefined,
          status: statusFilter || undefined,
        }),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load dashboard");
    } finally {
      setLoading(false);
    }
  }, [provider, action, statusFilter]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <section className="admin__section">
      <div className="admin__section-head">
        <h2>Execution Governance</h2>
        <button className="btn btn--ghost btn--sm" onClick={load} disabled={loading}>
          ↻ Refresh
        </button>
      </div>

      <div className="admin__error" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        🔒 <strong>Provider execution remains disabled. This dashboard is
        observability-only.</strong>
      </div>

      {error && <div className="admin__error">{error}</div>}

      <div className="admin__form-row" style={{ gap: 8, alignItems: "flex-end" }}>
        <label>
          <span>Provider</span>
          <input className="cora-input" value={provider} placeholder="e.g. gmail"
            onChange={(e) => setProvider(e.target.value)} />
        </label>
        <label>
          <span>Action</span>
          <select className="cora-input" value={action} onChange={(e) => setAction(e.target.value)}>
            <option value="">All</option>
            <option value="send_email">send_email</option>
            <option value="create_calendar_event">create_calendar_event</option>
          </select>
        </label>
        <label>
          <span>Intent status</span>
          <input className="cora-input" value={statusFilter} placeholder="e.g. pending_provider"
            onChange={(e) => setStatusFilter(e.target.value)} />
        </label>
      </div>

      {!data ? (
        <div className="admin__hint">{loading ? "Loading…" : "No data."}</div>
      ) : (
        <>
          <h3 className="admin__vt-h3">Governance Summary</h3>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {Object.entries(data.summary).map(([k, v]) => (
              <div key={k} className="admin__hint"
                style={{ border: "1px solid var(--border)", borderRadius: 6, padding: "6px 12px" }}>
                <div style={{ fontSize: 11, textTransform: "uppercase" }}>{k.replace(/_/g, " ")}</div>
                <div style={{ fontSize: 18 }}><strong>{String(v)}</strong></div>
              </div>
            ))}
          </div>

          <h3 className="admin__vt-h3">Drill-down</h3>
          {data.cards.length === 0 ? (
            <div className="admin__hint">No intents.</div>
          ) : (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              {data.cards.map((c) => (
                <div key={c.intent_id} className="admin__hint"
                  style={{ border: "1px solid var(--border)", borderRadius: 6, padding: 10, minWidth: 280, flex: "1 1 280px" }}>
                  <div><strong>{c.provider_type}</strong> · {c.action_type}</div>
                  <div className="muted" style={{ fontSize: 12 }}>intent {c.intent_id.slice(0, 8)}… · {c.status}</div>
                  <div className="muted" style={{ fontSize: 12 }}>source: {c.source_type} {c.source_id.slice(0, 8)}…</div>
                  <div className="muted" style={{ fontSize: 12 }}>connected: {c.connected_provider ?? "none"} · readiness: {c.provider_readiness_state}</div>
                  <div className="muted" style={{ fontSize: 12 }}>approval: {c.approval_state ?? "—"} · latest trace: {c.latest_trace_status ?? "—"}</div>
                  <div className="muted" style={{ fontSize: 12 }}>
                    flag: {c.feature_flag_state.present ? (c.feature_flag_state.enabled ? "enabled" : "disabled") : "missing"}
                    {c.feature_flag_state.dry_run_only ? " · dry-run" : ""}
                  </div>
                  {c.latest_block_reason && (
                    <div style={{ fontSize: 12, color: "var(--danger)" }}>block: {c.latest_block_reason}</div>
                  )}
                </div>
              ))}
            </div>
          )}

          <h3 className="admin__vt-h3">Draft Approval Activity</h3>
          <table className="admin__table">
            <thead><tr><th>When</th><th>Intent</th><th>Decision</th><th>State</th><th>Reason</th></tr></thead>
            <tbody>
              {data.recent_approval_events.map((r) => (
                <tr key={r.id}>
                  <td className="muted">{fmt(r.created_at)}</td>
                  <td className="muted">{r.intent_id.slice(0, 8)}…</td>
                  <td>{r.decision}</td>
                  <td>{r.approval_state}</td>
                  <td className="muted" style={{ fontSize: 12 }}>{r.reason ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <h3 className="admin__vt-h3">Integration Intent Activity</h3>
          <table className="admin__table">
            <thead><tr><th>When</th><th>Agent</th><th>Provider</th><th>Action</th><th>Status</th><th>Approval</th><th>Subject/Title</th></tr></thead>
            <tbody>
              {data.recent_integration_intents.map((r) => (
                <tr key={r.id}>
                  <td className="muted">{fmt(r.created_at)}</td>
                  <td className="muted">{r.agent_name ?? "—"}</td>
                  <td>{r.provider_type}</td>
                  <td className="muted">{r.action_type}</td>
                  <td>{r.status}</td>
                  <td className="muted">{r.approval_state ?? "—"}</td>
                  <td className="muted" style={{ fontSize: 12 }}>{r.payload_summary.subject ?? r.payload_summary.title ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <h3 className="admin__vt-h3">Provider Readiness</h3>
          <table className="admin__table">
            <thead><tr><th>Provider</th><th>Type</th><th>Status</th><th>Scopes</th><th>Tokens</th><th>Expires</th></tr></thead>
            <tbody>
              {data.provider_readiness.map((c, i) => (
                <tr key={i}>
                  <td>{c.provider_name}</td>
                  <td className="muted">{c.provider_type}</td>
                  <td>{c.status}</td>
                  <td className="muted">{c.scope_count}</td>
                  <td className="muted">{[c.has_access_token ? "access" : null, c.has_refresh_token ? "refresh" : null].filter(Boolean).join("+") || "—"}</td>
                  <td className="muted">{fmt(c.token_expires_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <h3 className="admin__vt-h3">Feature Flag Matrix Summary</h3>
          <table className="admin__table">
            <thead><tr><th>Provider</th><th>Action</th><th>Enabled</th><th>Dry-run only</th><th>Environment</th></tr></thead>
            <tbody>
              {data.feature_flags.map((f, i) => (
                <tr key={i}>
                  <td>{f.provider_name}</td>
                  <td className="muted">{f.action_type}</td>
                  <td>{f.enabled ? "enabled" : "disabled"}</td>
                  <td>{f.dry_run_only ? "yes" : "no"}</td>
                  <td className="muted">{f.environment}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <h3 className="admin__vt-h3">Final Safety Interlock Results</h3>
          <TraceTable rows={data.interlock_traces} />

          <h3 className="admin__vt-h3">Adapter Simulation Activity</h3>
          <TraceTable rows={data.adapter_traces} />

          <h3 className="admin__vt-h3">Governance Blocks</h3>
          <TraceTable rows={data.governance_blocks} />

          <h3 className="admin__vt-h3">Tool Execution Failures</h3>
          {data.tool_failures.length === 0 ? (
            <div className="admin__hint">None.</div>
          ) : (
            <table className="admin__table">
              <thead><tr><th>When</th><th>Tool</th><th>Agent</th><th>Status</th><th>Allowed</th><th>Error</th></tr></thead>
              <tbody>
                {data.tool_failures.map((r, i) => (
                  <tr key={i}>
                    <td className="muted">{fmt(r.created_at)}</td>
                    <td>{r.tool_name}</td>
                    <td className="muted">{r.agent_name ?? "—"}</td>
                    <td>{r.status}</td>
                    <td className="muted">{r.allowed ? "yes" : "no"}</td>
                    <td className="muted" style={{ fontSize: 12 }}>{r.error_message ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </section>
  );
}
