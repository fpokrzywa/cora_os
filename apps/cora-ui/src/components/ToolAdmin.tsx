import { useCallback, useEffect, useState } from "react";
import {
  adminListTools,
  adminPatchTool,
  adminTestTool,
} from "../api";
import type { ToolAdminRow, ToolRiskLevel, ToolTestResult } from "../types";

const RISK_LEVELS: ToolRiskLevel[] = ["low", "medium", "high"];

export function ToolAdmin() {
  const [tools, setTools] = useState<ToolAdminRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, ToolTestResult>>(
    {},
  );
  const [testing, setTesting] = useState<Record<string, boolean>>({});
  const [selectedName, setSelectedName] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setTools(await adminListTools());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const runTest = useCallback(async (name: string) => {
    setTesting((m) => ({ ...m, [name]: true }));
    try {
      const result = await adminTestTool(name, {});
      setTestResults((m) => ({ ...m, [name]: result }));
    } catch (err) {
      setTestResults((m) => ({
        ...m,
        [name]: {
          tool_name: name,
          type: "?",
          status: "error",
          error: err instanceof Error ? err.message : "Failed",
        },
      }));
    } finally {
      setTesting((m) => ({ ...m, [name]: false }));
    }
  }, []);

  const patch = useCallback(
    async (
      name: string,
      changes: Partial<{
        enabled: boolean;
        requires_confirmation: boolean;
        risk_level: ToolRiskLevel;
        allowed_agents: string[];
      }>,
    ) => {
      try {
        await adminPatchTool(name, changes);
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed");
      }
    },
    [refresh],
  );

  return (
    <main className="admin">
      <header className="admin__header">
        <h1>Tool Registry</h1>
        <p className="admin__subtitle">
          Governed tool catalog: n8n webhooks and MCP actions. MCP tools are
          dispatched through the MCP layer but are NOT exposed to the LLM yet —
          manual admin testing only.
        </p>
      </header>

      {error && <div className="admin__error">{error}</div>}

      <section className="admin__section">
        <div className="admin__section-head">
          <h2>Registered tools</h2>
          <button
            className="btn btn--ghost btn--sm"
            onClick={refresh}
            disabled={loading}
          >
            ↻ Refresh
          </button>
        </div>

        <table className="admin__table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Type</th>
              <th>Target</th>
              <th>Risk</th>
              <th>Allowed agents</th>
              <th>Status</th>
              <th>Confirm</th>
              <th>Test</th>
            </tr>
          </thead>
          <tbody>
            {tools.map((t) => {
              const tr = testResults[t.name];
              const isTesting = !!testing[t.name];
              const isSelected = selectedName === t.name;
              return (
                <tr
                  key={t.id}
                  className={isSelected ? "admin__row--selected" : ""}
                  onClick={() => setSelectedName(t.name)}
                >
                  <td className="mono">{t.name}</td>
                  <td>
                    <span className={`scope-chip scope-chip--${t.type.replace(/[^a-z_]/gi, "_")}`}>
                      {t.type}
                    </span>
                  </td>
                  <td className="mono muted">
                    {t.type === "mcp_action"
                      ? `${t.mcp_server_name ?? "?"} / ${t.mcp_action_name ?? "?"}`
                      : t.endpoint ?? "—"}
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
                    <button
                      className={`role-chip ${t.enabled ? "role-chip--admin" : "role-chip--user"}`}
                      onClick={(e) => {
                        e.stopPropagation();
                        patch(t.name, { enabled: !t.enabled });
                      }}
                    >
                      {t.enabled ? "enabled" : "disabled"}
                    </button>
                  </td>
                  <td>
                    <input
                      type="checkbox"
                      checked={t.requires_confirmation}
                      onChange={(e) =>
                        patch(t.name, {
                          requires_confirmation: e.target.checked,
                        })
                      }
                      onClick={(e) => e.stopPropagation()}
                    />
                  </td>
                  <td>
                    <button
                      className="btn btn--ghost btn--sm"
                      onClick={(e) => {
                        e.stopPropagation();
                        runTest(t.name);
                      }}
                      disabled={isTesting || !t.enabled}
                    >
                      {isTesting ? "…" : "Test"}
                    </button>
                    {tr && (
                      <div
                        className={`admin__hint ${tr.status === "ok" ? "mcp-ok" : "mcp-fail"}`}
                      >
                        {tr.status === "ok"
                          ? `ok (${tr.duration_ms ?? "?"}ms)`
                          : `${tr.status}: ${(tr.error ?? "").slice(0, 60)}`}
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
            {!loading && tools.length === 0 && (
              <tr>
                <td colSpan={8} className="muted">
                  No tools registered.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      {selectedName && (
        <ToolEditor
          tool={tools.find((t) => t.name === selectedName) ?? null}
          onChanged={refresh}
        />
      )}
    </main>
  );
}

function ToolEditor({
  tool,
  onChanged,
}: {
  tool: ToolAdminRow | null;
  onChanged: () => void;
}) {
  const [enabled, setEnabled] = useState(false);
  const [requiresConf, setRequiresConf] = useState(false);
  const [riskLevel, setRiskLevel] = useState<ToolRiskLevel>("low");
  const [allowedAgents, setAllowedAgents] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    if (!tool) return;
    setEnabled(tool.enabled);
    setRequiresConf(tool.requires_confirmation);
    setRiskLevel(tool.risk_level);
    setAllowedAgents(tool.allowed_agents.join(", "));
    setMsg(null);
  }, [tool?.id]);

  if (!tool) return null;

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setMsg(null);
    try {
      await adminPatchTool(tool.name, {
        enabled,
        requires_confirmation: requiresConf,
        risk_level: riskLevel,
        allowed_agents: allowedAgents
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
      });
      setMsg("Saved");
      onChanged();
    } catch (err) {
      setMsg(err instanceof Error ? err.message : "Failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section className="admin__section">
      <div className="admin__section-head">
        <h2>
          {tool.name} <span className="mono muted">({tool.type})</span>
        </h2>
      </div>

      {tool.type === "mcp_action" && (
        <p className="admin__hint">
          MCP target: <span className="mono">{tool.mcp_server_name}</span>{" "}
          / <span className="mono">{tool.mcp_action_name}</span>
        </p>
      )}
      {tool.description && (
        <p className="admin__hint">{tool.description}</p>
      )}

      <form className="admin__form" onSubmit={save}>
        <h3>Governance</h3>
        <div className="admin__form-row">
          <label>
            <span>Enabled</span>
            <select
              value={enabled ? "yes" : "no"}
              onChange={(e) => setEnabled(e.target.value === "yes")}
              disabled={submitting}
            >
              <option value="yes">yes</option>
              <option value="no">no</option>
            </select>
          </label>
          <label>
            <span>Requires confirmation</span>
            <select
              value={requiresConf ? "yes" : "no"}
              onChange={(e) => setRequiresConf(e.target.value === "yes")}
              disabled={submitting}
            >
              <option value="no">no</option>
              <option value="yes">yes</option>
            </select>
          </label>
          <label>
            <span>Risk level</span>
            <select
              value={riskLevel}
              onChange={(e) => setRiskLevel(e.target.value as ToolRiskLevel)}
              disabled={submitting}
            >
              {RISK_LEVELS.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </label>
        </div>
        <label className="admin__field-wide">
          <span>Allowed agents (comma-separated, empty = unrestricted)</span>
          <input
            type="text"
            value={allowedAgents}
            onChange={(e) => setAllowedAgents(e.target.value)}
            placeholder="e.g. FORGE, SCRIBE"
            disabled={submitting}
          />
        </label>
        <div className="admin__form-row">
          <button
            type="submit"
            className="btn btn--primary"
            disabled={submitting}
          >
            {submitting ? "…" : "Save"}
          </button>
          {msg && <span className="admin__hint">{msg}</span>}
        </div>
      </form>
    </section>
  );
}
