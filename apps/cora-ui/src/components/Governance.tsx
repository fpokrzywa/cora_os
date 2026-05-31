import { useCallback, useEffect, useMemo, useState } from "react";
import {
  adminDeletePolicy,
  adminGovernanceStats,
  adminListAgents,
  adminListExecutionLogs,
  adminListPolicies,
  adminListTools,
  adminUpsertPolicy,
} from "../api";
import type {
  Agent,
  ExecutionLog,
  GovernanceStats,
  ToolAdminRow,
  ToolPolicy,
} from "../types";

type Cell =
  | { kind: "policy"; allowed: boolean; rateLimit: number | null; id: string }
  | { kind: "allowlist"; allowed: boolean }
  | { kind: "unrestricted" };

export function Governance() {
  const [tools, setTools] = useState<ToolAdminRow[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [policies, setPolicies] = useState<ToolPolicy[]>([]);
  const [stats, setStats] = useState<GovernanceStats | null>(null);
  const [logs, setLogs] = useState<ExecutionLog[]>([]);
  const [deniedOnly, setDeniedOnly] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [editing, setEditing] = useState<{
    tool_name: string;
    agent_name: string;
    allowed: boolean;
    requires_confirmation: boolean;
    max_calls_per_hour: number | null;
    existingPolicyId: string | null;
  } | null>(null);
  const [savingPolicy, setSavingPolicy] = useState(false);
  const [policyMsg, setPolicyMsg] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [t, a, p, s, l] = await Promise.all([
        adminListTools(),
        adminListAgents(),
        adminListPolicies(),
        adminGovernanceStats(24),
        adminListExecutionLogs({ limit: 100, denied_only: deniedOnly }),
      ]);
      setTools(t);
      setAgents(a);
      setPolicies(p);
      setStats(s);
      setLogs(l);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setLoading(false);
    }
  }, [deniedOnly]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const policyByPair = useMemo(() => {
    const m = new Map<string, ToolPolicy>();
    policies.forEach((p) => m.set(`${p.tool_name}::${p.agent_name}`, p));
    return m;
  }, [policies]);

  const statsByTool = useMemo(() => {
    const m = new Map<string, GovernanceStats["tools"][number]>();
    stats?.tools.forEach((s) => m.set(s.tool_name, s));
    return m;
  }, [stats]);

  const cellFor = useCallback(
    (tool: ToolAdminRow, agentName: string): Cell => {
      const policy = policyByPair.get(`${tool.name}::${agentName}`);
      if (policy) {
        return {
          kind: "policy",
          allowed: policy.allowed,
          rateLimit: policy.max_calls_per_hour,
          id: policy.id,
        };
      }
      if (tool.allowed_agents.length > 0) {
        return {
          kind: "allowlist",
          allowed: tool.allowed_agents.includes(agentName),
        };
      }
      return { kind: "unrestricted" };
    },
    [policyByPair],
  );

  const openEditor = (tool: ToolAdminRow, agentName: string) => {
    const existing = policyByPair.get(`${tool.name}::${agentName}`);
    setEditing({
      tool_name: tool.name,
      agent_name: agentName,
      allowed: existing ? existing.allowed : true,
      requires_confirmation: existing ? existing.requires_confirmation : false,
      max_calls_per_hour: existing ? existing.max_calls_per_hour : null,
      existingPolicyId: existing ? existing.id : null,
    });
    setPolicyMsg(null);
  };

  const savePolicy = async () => {
    if (!editing || savingPolicy) return;
    setSavingPolicy(true);
    setPolicyMsg(null);
    try {
      await adminUpsertPolicy({
        tool_name: editing.tool_name,
        agent_name: editing.agent_name,
        allowed: editing.allowed,
        requires_confirmation: editing.requires_confirmation,
        max_calls_per_hour: editing.max_calls_per_hour,
      });
      setPolicyMsg("Saved");
      await refresh();
    } catch (err) {
      setPolicyMsg(err instanceof Error ? err.message : "Failed");
    } finally {
      setSavingPolicy(false);
    }
  };

  const deletePolicy = async () => {
    if (!editing || !editing.existingPolicyId || savingPolicy) return;
    setSavingPolicy(true);
    setPolicyMsg(null);
    try {
      await adminDeletePolicy(editing.existingPolicyId);
      setPolicyMsg("Removed override (falls back to tool.allowed_agents)");
      setEditing(null);
      await refresh();
    } catch (err) {
      setPolicyMsg(err instanceof Error ? err.message : "Failed");
    } finally {
      setSavingPolicy(false);
    }
  };

  return (
    <main className="admin">
      <header className="admin__header">
        <h1>Tool Governance</h1>
        <p className="admin__subtitle">
          Permission matrix, runtime policies, and execution audit log. Cells
          show the effective rule per (tool, agent). Click a cell to set or
          remove a policy override.
        </p>
      </header>

      {error && <div className="admin__error">{error}</div>}

      <section className="admin__section">
        <div className="admin__section-head">
          <h2>Permissions matrix</h2>
          <button
            className="btn btn--ghost btn--sm"
            onClick={refresh}
            disabled={loading}
          >
            ↻ Refresh
          </button>
        </div>

        <div className="gov-matrix-wrap">
          <table className="admin__table gov-matrix">
            <thead>
              <tr>
                <th>Tool</th>
                <th>Risk</th>
                <th>Last 24h</th>
                {agents.map((a) => (
                  <th key={a.id}>{a.name}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {tools.map((t) => {
                const s = statsByTool.get(t.name);
                return (
                  <tr key={t.id}>
                    <td className="mono">{t.name}</td>
                    <td>
                      <span className={`risk-chip risk-chip--${t.risk_level}`}>
                        {t.risk_level}
                      </span>
                    </td>
                    <td className="muted gov-stat">
                      {s ? (
                        <>
                          <span className="mcp-ok">ok {s.allowed_count}</span>
                          {" · "}
                          <span className="mcp-fail">
                            denied {s.denied_count}
                          </span>
                          {s.error_count ? ` · err ${s.error_count}` : ""}
                        </>
                      ) : (
                        "—"
                      )}
                    </td>
                    {agents.map((a) => {
                      const cell = cellFor(t, a.name);
                      return (
                        <td
                          key={a.id}
                          className="gov-cell"
                          onClick={() => openEditor(t, a.name)}
                          title="Click to edit policy"
                        >
                          {renderCell(cell)}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
              {!loading && tools.length === 0 && (
                <tr>
                  <td colSpan={3 + agents.length} className="muted">
                    No tools registered.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        <p className="admin__hint">
          Legend: <span className="gov-mark gov-mark--policy-allow">✓ P</span>{" "}
          policy allow · <span className="gov-mark gov-mark--policy-deny">✗ P</span>{" "}
          policy deny ·{" "}
          <span className="gov-mark gov-mark--allowlist-allow">✓</span>{" "}
          allowlist allow · <span className="gov-mark gov-mark--allowlist-deny">✗</span>{" "}
          not in allowlist · <span className="gov-mark gov-mark--unrestricted">◯</span>{" "}
          unrestricted
        </p>
      </section>

      {editing && (
        <section className="admin__section">
          <div className="admin__section-head">
            <h2>
              Policy:{" "}
              <span className="mono">{editing.tool_name}</span> ×{" "}
              <span className="mono">{editing.agent_name}</span>
            </h2>
            <button
              className="btn btn--ghost btn--sm"
              onClick={() => setEditing(null)}
            >
              Close
            </button>
          </div>
          <div className="admin__form">
            <div className="admin__form-row">
              <label>
                <span>Allowed</span>
                <select
                  value={editing.allowed ? "yes" : "no"}
                  onChange={(e) =>
                    setEditing({
                      ...editing,
                      allowed: e.target.value === "yes",
                    })
                  }
                  disabled={savingPolicy}
                >
                  <option value="yes">yes</option>
                  <option value="no">no</option>
                </select>
              </label>
              <label>
                <span>Requires confirmation</span>
                <select
                  value={editing.requires_confirmation ? "yes" : "no"}
                  onChange={(e) =>
                    setEditing({
                      ...editing,
                      requires_confirmation: e.target.value === "yes",
                    })
                  }
                  disabled={savingPolicy}
                >
                  <option value="no">no</option>
                  <option value="yes">yes</option>
                </select>
              </label>
              <label>
                <span>Max calls / hour (blank = unlimited)</span>
                <input
                  type="number"
                  min={1}
                  max={100000}
                  value={editing.max_calls_per_hour ?? ""}
                  onChange={(e) =>
                    setEditing({
                      ...editing,
                      max_calls_per_hour:
                        e.target.value === ""
                          ? null
                          : Math.max(1, Number(e.target.value)),
                    })
                  }
                  disabled={savingPolicy}
                />
              </label>
            </div>
            <div className="admin__form-row">
              <button
                type="button"
                className="btn btn--primary"
                onClick={savePolicy}
                disabled={savingPolicy}
              >
                {savingPolicy ? "…" : "Save policy"}
              </button>
              {editing.existingPolicyId && (
                <button
                  type="button"
                  className="btn btn--ghost"
                  onClick={deletePolicy}
                  disabled={savingPolicy}
                >
                  Remove override
                </button>
              )}
              {policyMsg && (
                <span className="admin__hint">{policyMsg}</span>
              )}
            </div>
          </div>
        </section>
      )}

      <section className="admin__section">
        <div className="admin__section-head">
          <h2>Execution audit log</h2>
          <label className="admin__hint">
            <input
              type="checkbox"
              checked={deniedOnly}
              onChange={(e) => setDeniedOnly(e.target.checked)}
            />{" "}
            denied only
          </label>
        </div>
        <table className="admin__table">
          <thead>
            <tr>
              <th>When</th>
              <th>Tool</th>
              <th>Agent</th>
              <th>Allowed</th>
              <th>Status</th>
              <th>Duration</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {logs.map((l) => (
              <tr key={l.id}>
                <td className="muted">
                  {new Date(l.created_at).toLocaleString()}
                </td>
                <td className="mono">{l.tool_name}</td>
                <td className="mono muted">{l.agent_name ?? "(manual)"}</td>
                <td>
                  <span
                    className={`role-chip ${l.allowed ? "role-chip--admin" : "role-chip--user"}`}
                  >
                    {l.allowed ? "allowed" : "denied"}
                  </span>
                </td>
                <td className="mono muted">{l.status}</td>
                <td className="muted">
                  {l.duration_ms != null ? `${l.duration_ms}ms` : "—"}
                </td>
                <td className="muted">{l.error_message ?? ""}</td>
              </tr>
            ))}
            {!loading && logs.length === 0 && (
              <tr>
                <td colSpan={7} className="muted">
                  No execution attempts yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>
    </main>
  );
}

function renderCell(cell: Cell) {
  if (cell.kind === "policy") {
    return (
      <span
        className={`gov-mark ${cell.allowed ? "gov-mark--policy-allow" : "gov-mark--policy-deny"}`}
      >
        {cell.allowed ? "✓ P" : "✗ P"}
        {cell.rateLimit ? ` (${cell.rateLimit}/h)` : ""}
      </span>
    );
  }
  if (cell.kind === "allowlist") {
    return (
      <span
        className={`gov-mark ${cell.allowed ? "gov-mark--allowlist-allow" : "gov-mark--allowlist-deny"}`}
      >
        {cell.allowed ? "✓" : "✗"}
      </span>
    );
  }
  return <span className="gov-mark gov-mark--unrestricted">◯</span>;
}
