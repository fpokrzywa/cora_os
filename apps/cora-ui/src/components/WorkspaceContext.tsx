import { useCallback, useEffect, useState } from "react";
import { getWorkspaceContext } from "../api";
import type { WorkspaceContext as Ctx } from "../types";

interface Props {
  workspaceId: string | null;
}

export function WorkspaceContext({ workspaceId }: Props) {
  const [ctx, setCtx] = useState<Ctx | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!workspaceId) return;
    setLoading(true);
    setError(null);
    try {
      setCtx(await getWorkspaceContext(workspaceId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setLoading(false);
    }
  }, [workspaceId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  if (!workspaceId) {
    return (
      <main className="admin">
        <header className="admin__header">
          <h1>Workspace Context</h1>
        </header>
        <div className="admin__hint">Select a workspace in the sidebar.</div>
      </main>
    );
  }

  return (
    <main className="admin">
      <header className="admin__header">
        <h1>
          Workspace Context
          {ctx && (
            <>
              {" — "}
              <span className="mono muted">{ctx.workspace.name}</span>
            </>
          )}
        </h1>
        <p className="admin__subtitle">
          Read-only snapshot of the selected workspace: counts, agents, tools,
          MCP servers, project files, and recent runtime traces.
        </p>
      </header>

      {error && <div className="admin__error">{error}</div>}

      <section className="admin__section">
        <div className="admin__section-head">
          <h2>Snapshot</h2>
          <button
            className="btn btn--ghost btn--sm"
            onClick={refresh}
            disabled={loading}
          >
            ↻ Refresh
          </button>
        </div>

        {ctx && (
          <div className="ctx-grid">
            <Card title="Workspace">
              <Row label="Name" value={ctx.workspace.name} />
              <Row label="Slug" value={ctx.workspace.slug} mono />
              <Row label="Status" value={ctx.workspace.status} />
              {ctx.workspace.description && (
                <Row label="Description" value={ctx.workspace.description} />
              )}
            </Card>

            <Card title="Memory">
              <Row
                label="Total"
                value={String(ctx.memory.total)}
              />
              <Row
                label="Embedded"
                value={String(ctx.memory.embedded)}
              />
              <Row
                label="Missing embeddings"
                value={String(ctx.memory.missing)}
              />
              <Row
                label="Semantic"
                value={
                  ctx.memory.pgvector_available ? "available" : "unavailable"
                }
                pill={ctx.memory.pgvector_available ? "ok" : "off"}
              />
            </Card>

            <Card title="Plans">
              <Row label="Total" value={String(ctx.plans.total)} />
              <Row label="Active" value={String(ctx.plans.active)} />
            </Card>

            <Card title="Jobs">
              <Row label="Active" value={String(ctx.jobs.active)} />
              <Row
                label="Failed"
                value={String(ctx.jobs.failed)}
                pill={ctx.jobs.failed > 0 ? "warn" : undefined}
              />
            </Card>

            <Card title="Conversations">
              <Row label="Total" value={String(ctx.recent_conversations_count)} />
            </Card>
          </div>
        )}
      </section>

      {ctx && (
        <>
          <section className="admin__section">
            <div className="admin__section-head">
              <h2>Agents</h2>
            </div>
            <table className="admin__table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Type</th>
                  <th>Version</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {ctx.agents.map((a) => (
                  <tr key={a.name}>
                    <td className="mono">{a.name}</td>
                    <td className="mono muted">{a.agent_type}</td>
                    <td className="muted">
                      {a.current_version_number != null
                        ? `v${a.current_version_number}`
                        : "—"}
                    </td>
                    <td>
                      <span
                        className={`role-chip ${a.enabled ? "role-chip--admin" : "role-chip--user"}`}
                      >
                        {a.enabled ? "enabled" : "disabled"}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <section className="admin__section">
            <div className="admin__section-head">
              <h2>Tools</h2>
            </div>
            <table className="admin__table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Type</th>
                  <th>Target</th>
                  <th>Risk</th>
                  <th>Allowed</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {ctx.tools.map((t) => (
                  <tr key={t.name}>
                    <td className="mono">{t.name}</td>
                    <td className="mono muted">{t.type}</td>
                    <td className="mono muted">
                      {t.mcp_server_name
                        ? `${t.mcp_server_name} / ${t.mcp_action_name ?? ""}`
                        : "—"}
                    </td>
                    <td>
                      <span className={`risk-chip risk-chip--${t.risk_level}`}>
                        {t.risk_level}
                      </span>
                    </td>
                    <td className="muted mono">
                      {t.allowed_agents.length > 0
                        ? t.allowed_agents.join(", ")
                        : "—"}
                    </td>
                    <td>
                      <span
                        className={`role-chip ${t.enabled ? "role-chip--admin" : "role-chip--user"}`}
                      >
                        {t.enabled ? "enabled" : "disabled"}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <section className="admin__section">
            <div className="admin__section-head">
              <h2>MCP servers</h2>
            </div>
            <table className="admin__table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Type</th>
                  <th>Endpoint</th>
                  <th>Capabilities</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {ctx.mcp_servers.map((m) => (
                  <tr key={m.name}>
                    <td className="mono">{m.name}</td>
                    <td className="mono muted">{m.server_type}</td>
                    <td className="mono muted">{m.endpoint}</td>
                    <td className="muted">
                      {m.capabilities_cached ? "cached" : "—"}
                    </td>
                    <td>
                      <span
                        className={`role-chip ${m.enabled ? "role-chip--admin" : "role-chip--user"}`}
                      >
                        {m.enabled ? "enabled" : "disabled"}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <section className="admin__section">
            <div className="admin__section-head">
              <h2>
                Project files{" "}
                <span className="muted">
                  (source: {ctx.project_files_source})
                </span>
              </h2>
            </div>
            {ctx.project_files_error && (
              <div className="admin__hint">
                {ctx.project_files_error}
              </div>
            )}
            {ctx.project_files.length === 0 ? (
              <p className="muted">No project files returned.</p>
            ) : (
              <ul className="ctx-files">
                {ctx.project_files.map((f) => (
                  <li key={f.name}>
                    <span
                      className={`scope-chip scope-chip--${f.type === "dir" ? "subagent" : "tool_agent"}`}
                    >
                      {f.type}
                    </span>
                    <span className="mono">{f.name}</span>
                    {f.size_bytes != null && (
                      <span className="muted">{f.size_bytes} B</span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="admin__section">
            <div className="admin__section-head">
              <h2>Recent traces</h2>
            </div>
            <table className="admin__table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Type</th>
                  <th>Agent</th>
                  <th>Status</th>
                  <th>Duration</th>
                </tr>
              </thead>
              <tbody>
                {ctx.recent_traces.map((t) => (
                  <tr key={t.id}>
                    <td className="muted">
                      {new Date(t.created_at).toLocaleString()}
                    </td>
                    <td className="mono">{t.trace_type}</td>
                    <td className="mono muted">{t.selected_agent ?? "—"}</td>
                    <td className="mono muted">{t.status}</td>
                    <td className="muted">
                      {t.duration_ms != null ? `${t.duration_ms}ms` : "—"}
                    </td>
                  </tr>
                ))}
                {ctx.recent_traces.length === 0 && (
                  <tr>
                    <td colSpan={5} className="muted">
                      No traces yet for this workspace.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </section>
        </>
      )}
    </main>
  );
}

function Card({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="ctx-card">
      <h3 className="ctx-card__title">{title}</h3>
      <div className="ctx-card__body">{children}</div>
    </div>
  );
}

function Row({
  label,
  value,
  mono,
  pill,
}: {
  label: string;
  value: string;
  mono?: boolean;
  pill?: "ok" | "warn" | "off";
}) {
  return (
    <div className="ctx-row">
      <span className="ctx-row__label">{label}</span>
      {pill ? (
        <span
          className={`semantic-badge ${pill === "ok" ? "semantic-badge--ok" : pill === "warn" ? "" : "semantic-badge--off"}`}
          style={
            pill === "warn"
              ? {
                  background: "rgba(239,68,68,0.12)",
                  color: "#fecaca",
                  borderColor: "rgba(239,68,68,0.35)",
                }
              : undefined
          }
        >
          {value}
        </span>
      ) : (
        <span className={`ctx-row__value${mono ? " mono" : ""}`}>{value}</span>
      )}
    </div>
  );
}
