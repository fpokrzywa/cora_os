import { useCallback, useEffect, useState } from "react";
import {
  adminCreateMcp,
  adminGetMcpCapabilities,
  adminListMcp,
  adminPatchMcp,
  adminTestMcp,
} from "../api";
import type {
  McpCapabilities,
  McpServer,
  McpTestResult,
} from "../types";

export function McpAdmin() {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, McpTestResult>>(
    {},
  );
  const [testing, setTesting] = useState<Record<string, boolean>>({});
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [capabilities, setCapabilities] = useState<McpCapabilities | null>(null);
  const [capabilitiesCached, setCapabilitiesCached] = useState<boolean>(true);
  const [capabilitiesLoading, setCapabilitiesLoading] = useState(false);
  const [capabilitiesError, setCapabilitiesError] = useState<string | null>(
    null,
  );

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setServers(await adminListMcp());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const test = useCallback(async (name: string) => {
    setTesting((m) => ({ ...m, [name]: true }));
    try {
      const result = await adminTestMcp(name);
      setTestResults((m) => ({ ...m, [name]: result }));
    } catch (err) {
      setTestResults((m) => ({
        ...m,
        [name]: {
          server_name: name,
          success: false,
          duration_ms: 0,
          error: err instanceof Error ? err.message : "Failed",
        },
      }));
    } finally {
      setTesting((m) => ({ ...m, [name]: false }));
    }
  }, []);

  const toggleEnabled = useCallback(
    async (server: McpServer) => {
      try {
        await adminPatchMcp(server.name, { enabled: !server.enabled });
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed");
      }
    },
    [refresh],
  );

  const loadCapabilities = useCallback(
    async (name: string, refreshLive = false) => {
      setSelectedName(name);
      setCapabilitiesLoading(true);
      setCapabilitiesError(null);
      try {
        const result = await adminGetMcpCapabilities(name, refreshLive);
        setCapabilities(result.capabilities);
        setCapabilitiesCached(result.cached);
        if (refreshLive) await refresh(); // pick up updated row
      } catch (err) {
        setCapabilitiesError(err instanceof Error ? err.message : "Failed");
        setCapabilities(null);
      } finally {
        setCapabilitiesLoading(false);
      }
    },
    [refresh],
  );

  return (
    <main className="admin">
      <header className="admin__header">
        <h1>MCP Servers</h1>
        <p className="admin__subtitle">
          External Model Context Protocol servers. v0.1 supports HTTP
          JSON-RPC. Tool execution is intentionally not wired into agents
          yet — discovery and connection testing only.
        </p>
      </header>

      {error && <div className="admin__error">{error}</div>}

      <section className="admin__section">
        <div className="admin__section-head">
          <h2>Registered servers</h2>
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
              <th>Endpoint</th>
              <th>Auth</th>
              <th>Status</th>
              <th>Test</th>
              <th>Capabilities</th>
            </tr>
          </thead>
          <tbody>
            {servers.map((s) => {
              const tr = testResults[s.name];
              const isTesting = !!testing[s.name];
              return (
                <tr
                  key={s.id}
                  className={
                    s.name === selectedName ? "admin__row--selected" : ""
                  }
                >
                  <td className="mono">{s.name}</td>
                  <td className="mono muted">{s.server_type}</td>
                  <td className="mono muted">{s.endpoint}</td>
                  <td className="muted">{s.auth_type || "—"}</td>
                  <td>
                    <button
                      className={`role-chip ${s.enabled ? "role-chip--admin" : "role-chip--user"}`}
                      onClick={() => toggleEnabled(s)}
                      title="Toggle enabled"
                    >
                      {s.enabled ? "enabled" : "disabled"}
                    </button>
                  </td>
                  <td>
                    <button
                      className="btn btn--ghost btn--sm"
                      onClick={() => test(s.name)}
                      disabled={isTesting || !s.enabled}
                    >
                      {isTesting ? "…" : "Test"}
                    </button>
                    {tr && (
                      <div
                        className={`admin__hint ${tr.success ? "mcp-ok" : "mcp-fail"}`}
                      >
                        {tr.success
                          ? `ok (${tr.duration_ms}ms)`
                          : `fail: ${tr.error?.slice(0, 50)}${tr.error && tr.error.length > 50 ? "…" : ""}`}
                      </div>
                    )}
                  </td>
                  <td>
                    <div className="admin__row-actions">
                      <button
                        className="btn btn--ghost btn--sm"
                        onClick={() => loadCapabilities(s.name)}
                      >
                        View
                      </button>
                      <button
                        className="btn btn--ghost btn--sm"
                        onClick={() => loadCapabilities(s.name, true)}
                        disabled={!s.enabled}
                      >
                        Refresh
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
            {!loading && servers.length === 0 && (
              <tr>
                <td colSpan={7} className="muted">
                  No MCP servers registered.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      {selectedName && (
        <McpServerDetail
          server={servers.find((s) => s.name === selectedName) ?? null}
          capabilities={capabilities}
          cached={capabilitiesCached}
          loading={capabilitiesLoading}
          error={capabilitiesError}
          onChanged={refresh}
        />
      )}

      <CreateMcpForm onCreated={refresh} />
    </main>
  );
}

function McpServerDetail({
  server,
  capabilities,
  cached,
  loading,
  error,
  onChanged,
}: {
  server: McpServer | null;
  capabilities: McpCapabilities | null;
  cached: boolean;
  loading: boolean;
  error: string | null;
  onChanged: () => void;
}) {
  const [endpoint, setEndpoint] = useState(server?.endpoint ?? "");
  const [description, setDescription] = useState(server?.description ?? "");
  const [submitting, setSubmitting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    setEndpoint(server?.endpoint ?? "");
    setDescription(server?.description ?? "");
    setMsg(null);
  }, [server?.id]);

  if (!server) return null;

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setMsg(null);
    try {
      await adminPatchMcp(server.name, {
        endpoint: endpoint.trim(),
        description: description.trim(),
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
          {server.name}{" "}
          <span className="mono muted">({server.server_type})</span>
        </h2>
        {loading && <span className="admin__hint">Loading…</span>}
      </div>

      <form className="admin__form" onSubmit={save}>
        <h3>Edit endpoint</h3>
        <label className="admin__field-wide">
          <span>Endpoint URL</span>
          <input
            type="text"
            value={endpoint}
            onChange={(e) => setEndpoint(e.target.value)}
            disabled={submitting}
          />
        </label>
        <label className="admin__field-wide">
          <span>Description</span>
          <input
            type="text"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
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

      <h3 className="admin__vt-h3">
        Capabilities{" "}
        <span className="muted">
          ({cached ? "from cache" : "freshly discovered"})
        </span>
      </h3>

      {error && <div className="admin__error">{error}</div>}

      {!capabilities && !loading && !error && (
        <p className="muted">
          No capabilities cached yet. Click <strong>Refresh</strong> on the row
          above to discover live.
        </p>
      )}

      {capabilities && (
        <>
          {capabilities.server_info &&
            Object.keys(capabilities.server_info).length > 0 && (
              <div className="admin__hint">
                <strong>Server:</strong>{" "}
                {JSON.stringify(capabilities.server_info)}
              </div>
            )}

          <h4 className="admin__vt-h3">
            Tools ({capabilities.tools?.length ?? 0})
          </h4>
          {capabilities.tools && capabilities.tools.length > 0 ? (
            <ul className="admin__preview-list">
              {capabilities.tools.map((t) => (
                <li key={t.name}>
                  <div className="admin__preview-row">
                    <span className="scope-chip scope-chip--tool_agent">
                      tool
                    </span>
                    <strong>{t.name}</strong>
                  </div>
                  {t.description && (
                    <div className="admin__preview-content">
                      {t.description}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">— none —</p>
          )}

          <h4 className="admin__vt-h3">
            Resources ({capabilities.resources?.length ?? 0})
          </h4>
          {capabilities.resources && capabilities.resources.length > 0 ? (
            <ul className="admin__preview-list">
              {capabilities.resources.map((r) => (
                <li key={r.uri}>
                  <div className="admin__preview-row">
                    <span className="scope-chip scope-chip--memory">
                      resource
                    </span>
                    <strong>{r.name || r.uri}</strong>
                    <span className="mono muted">{r.uri}</span>
                  </div>
                  {r.description && (
                    <div className="admin__preview-content">
                      {r.description}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">— none —</p>
          )}
        </>
      )}
    </section>
  );
}

function CreateMcpForm({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [serverType, setServerType] = useState("http");
  const [endpoint, setEndpoint] = useState("");
  const [authType, setAuthType] = useState<string>("");
  const [authToken, setAuthToken] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setMsg(null);
    try {
      const auth_type = authType.trim() || null;
      let auth_config: Record<string, unknown> | null = null;
      if (auth_type === "bearer" && authToken.trim()) {
        auth_config = { token: authToken.trim() };
      }
      await adminCreateMcp({
        name: name.trim(),
        description: description.trim() || null,
        server_type: serverType.trim() || "http",
        endpoint: endpoint.trim(),
        auth_type,
        auth_config,
      });
      setMsg(`Created ${name.trim()}`);
      setName("");
      setDescription("");
      setEndpoint("");
      setAuthType("");
      setAuthToken("");
      onCreated();
    } catch (err) {
      setMsg(err instanceof Error ? err.message : "Failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section className="admin__section">
      <form className="admin__form" onSubmit={submit}>
        <h3>Register new MCP server</h3>
        <div className="admin__form-row">
          <label>
            <span>Name (slug)</span>
            <input
              type="text"
              required
              pattern="^[A-Za-z][A-Za-z0-9_\-]*$"
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={submitting}
            />
          </label>
          <label>
            <span>Type</span>
            <select
              value={serverType}
              onChange={(e) => setServerType(e.target.value)}
              disabled={submitting}
            >
              <option value="http">http</option>
              <option value="sse">sse (future)</option>
              <option value="stdio">stdio (future)</option>
            </select>
          </label>
        </div>
        <label className="admin__field-wide">
          <span>Endpoint URL</span>
          <input
            type="text"
            required
            value={endpoint}
            onChange={(e) => setEndpoint(e.target.value)}
            placeholder="http://mcp-foo:3000"
            disabled={submitting}
          />
        </label>
        <label className="admin__field-wide">
          <span>Description</span>
          <input
            type="text"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            disabled={submitting}
          />
        </label>
        <div className="admin__form-row">
          <label>
            <span>Auth type</span>
            <select
              value={authType}
              onChange={(e) => setAuthType(e.target.value)}
              disabled={submitting}
            >
              <option value="">none</option>
              <option value="bearer">bearer</option>
              <option value="header">header</option>
              <option value="basic">basic</option>
            </select>
          </label>
          {authType === "bearer" && (
            <label className="admin__field-wide">
              <span>Bearer token</span>
              <input
                type="text"
                value={authToken}
                onChange={(e) => setAuthToken(e.target.value)}
                disabled={submitting}
              />
            </label>
          )}
          <button
            type="submit"
            className="btn btn--primary"
            disabled={submitting}
          >
            {submitting ? "…" : "Register"}
          </button>
          {msg && <span className="admin__hint">{msg}</span>}
        </div>
      </form>
    </section>
  );
}
