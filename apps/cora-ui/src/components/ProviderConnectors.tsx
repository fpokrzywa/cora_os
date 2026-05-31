import { useCallback, useEffect, useState } from "react";
import {
  listProviderConnectors,
  getProviderReadiness,
  registerProviderPlaceholder,
  disconnectProviderConnector,
  getOAuthProviders,
  startOAuth,
  refreshOAuth,
} from "../api";
import type {
  ProviderOAuthConnector,
  ProviderReadiness,
  ProviderReadinessEntry,
  ProviderConnectorStatus,
  OAuthProviderStatus,
  OAuthConnectionStatus,
} from "../types";

const OAUTH_STATUS_LABEL: Record<OAuthConnectionStatus, string> = {
  not_configured: "Not configured",
  ready_to_connect: "Ready to connect",
  connected: "Connected",
  expired: "Expired",
  refresh_failed: "Refresh failed",
};

function oauthChip(s: OAuthConnectionStatus) {
  const cls =
    s === "connected"
      ? "status-chip status-chip--active"
      : s === "not_configured"
        ? "status-chip status-chip--archived"
        : "status-chip status-chip--draft";
  return <span className={cls}>{OAUTH_STATUS_LABEL[s] ?? s}</span>;
}

const PROVIDERS = [
  { name: "gmail", type: "email" },
  { name: "outlook_mail", type: "email" },
  { name: "google_calendar", type: "calendar" },
  { name: "outlook_calendar", type: "calendar" },
];

// Spec status display rules.
const STATUS_LABEL: Record<ProviderConnectorStatus, string> = {
  not_configured: "Not Configured",
  oauth_required: "OAuth Required",
  connected: "Connected",
  expired: "Expired",
  disconnected: "Disconnected",
  error: "Error",
};

function statusChip(s: ProviderConnectorStatus) {
  const cls =
    s === "connected"
      ? "status-chip status-chip--active"
      : s === "disconnected" || s === "not_configured"
        ? "status-chip status-chip--archived"
        : "status-chip status-chip--draft";
  return <span className={cls}>{STATUS_LABEL[s] ?? s.replace(/_/g, " ")}</span>;
}

function fmt(d: string | null | undefined): string {
  return d ? new Date(d).toLocaleString() : "—";
}

function tokenCell(hasAccess: boolean, hasRefresh: boolean): string {
  if (!hasAccess && !hasRefresh) return "—";
  return [hasAccess ? "access" : null, hasRefresh ? "refresh" : null]
    .filter(Boolean)
    .join(" + ");
}

interface DetailView {
  title: string;
  rows: Array<[string, React.ReactNode]>;
  blockers?: string[];
}

export function ProviderConnectors({
  isAdmin = false,
}: {
  workspaceId?: string | null;
  isAdmin?: boolean;
}) {
  const [connectors, setConnectors] = useState<ProviderOAuthConnector[]>([]);
  const [readiness, setReadiness] = useState<ProviderReadiness | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [newProvider, setNewProvider] = useState(PROVIDERS[0].name);
  const [detail, setDetail] = useState<DetailView | null>(null);
  const [oauth, setOauth] = useState<OAuthProviderStatus[]>([]);
  const [oauthExecEnabled, setOauthExecEnabled] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [c, r, o] = await Promise.all([
        listProviderConnectors(),
        getProviderReadiness(),
        getOAuthProviders().catch(() => null),
      ]);
      setConnectors(c);
      setReadiness(r);
      if (o) {
        setOauth(o.providers);
        setOauthExecEnabled(o.execution_enabled);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load connectors");
    } finally {
      setLoading(false);
    }
  }, []);

  const connectOAuth = async (providerName: string) => {
    setBusy(`oauth-${providerName}`);
    setError(null);
    try {
      const r = await startOAuth(providerName);
      // Open the provider consent screen in a new tab. The provider redirects
      // back to the configured callback; the user returns and refreshes.
      window.open(r.authorization_url, "_blank", "noopener,noreferrer");
      setToast("Opened provider consent. Complete it, then Refresh.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start OAuth");
    } finally {
      setBusy(null);
    }
  };

  const refreshOAuthConn = async (providerName: string) => {
    setBusy(`oauth-${providerName}`);
    setError(null);
    try {
      await refreshOAuth(providerName);
      setToast("Token refresh attempted.");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Token refresh failed");
    } finally {
      setBusy(null);
    }
  };

  const disconnectOAuth = async (p: OAuthProviderStatus) => {
    if (!p.connector_id) return;
    setBusy(`oauth-${p.provider_name}`);
    setError(null);
    try {
      await disconnectProviderConnector(p.connector_id);
      setToast("Provider disconnected.");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to disconnect");
    } finally {
      setBusy(null);
    }
  };

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 2500);
    return () => clearTimeout(t);
  }, [toast]);

  const register = async () => {
    setBusy("register");
    setError(null);
    try {
      await registerProviderPlaceholder({ provider_name: newProvider });
      setToast("Placeholder connector created.");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to register placeholder");
    } finally {
      setBusy(null);
    }
  };

  const disconnect = async (c: ProviderOAuthConnector) => {
    setBusy(c.id);
    setError(null);
    try {
      await disconnectProviderConnector(c.id);
      setToast("Connector disconnected.");
      await load();
    } catch (err) {
      const m = err instanceof Error ? err.message : "Failed to disconnect";
      setError(m.startsWith("404") ? "That connector no longer exists." : m);
    } finally {
      setBusy(null);
    }
  };

  const showReadinessDetail = (p: ProviderReadinessEntry) => {
    setDetail({
      title: `${p.provider_name} (${p.provider_type})`,
      rows: [
        ["Status", STATUS_LABEL[p.status] ?? p.status],
        ["Scopes", (p.scopes || []).length ? (p.scopes as string[]).join(", ") : "—"],
        ["Required scopes", p.required_scopes.length ? p.required_scopes.join(", ") : "—"],
        ["Missing scopes", p.missing_scopes.length ? p.missing_scopes.join(", ") : "none"],
        ["Has access token", p.has_access_token ? "yes" : "no"],
        ["Has refresh token", p.has_refresh_token ? "yes" : "no"],
        ["Token expires at", fmt(p.token_expires_at)],
        ["Disconnected at", fmt(p.disconnected_at)],
        ["Updated at", fmt(p.updated_at)],
        ["Ready for execution", p.ready_for_execution ? "yes" : "no"],
        ["Connector ID", p.connector_id ?? "—"],
      ],
      blockers: p.blockers,
    });
  };

  const showConnectorDetail = (c: ProviderOAuthConnector) => {
    setDetail({
      title: `${c.provider_name} (${c.provider_type})`,
      rows: [
        ["Status", STATUS_LABEL[c.status] ?? c.status],
        ["Scopes", (c.scopes || []).length ? (c.scopes as string[]).join(", ") : "—"],
        ["Has access token", c.has_access_token ? "yes" : "no"],
        ["Has refresh token", c.has_refresh_token ? "yes" : "no"],
        ["Token expires at", fmt(c.token_expires_at)],
        ["Ready for execution", c.ready_for_execution ? "yes" : "no"],
        ["Created at", fmt(c.created_at)],
        ["Updated at", fmt(c.updated_at)],
        ["Disconnected at", fmt(c.disconnected_at)],
        ["Connector ID", c.id],
      ],
    });
  };

  return (
    <section className="admin__section">
      <div className="admin__section-head">
        <h2>Provider Connectors / OAuth Readiness</h2>
        <button
          className="btn btn--ghost btn--sm"
          onClick={load}
          disabled={loading}
          title="Re-check provider readiness"
        >
          ↻ Refresh Readiness
        </button>
      </div>
      <p className="admin__hint">
        OAuth connection is available below; <strong>real provider execution
        stays disabled</strong> (kill switch + approval gate), so a connected
        account still cannot send mail or create events. Tokens are encrypted at
        rest and never displayed.
        {readiness && !readiness.encryption_available && (
          <span style={{ color: "var(--danger)" }}>
            {" "}⚠ Encryption key not configured — providers are unavailable until{" "}
            <code>CORA_CREDENTIAL_ENC_KEY</code> is set.
          </span>
        )}
        {isAdmin ? " Admin view: all users' connectors." : ""}
      </p>

      {error && <div className="admin__error">{error}</div>}
      {toast && <div className="admin__toast">{toast}</div>}

      <div className="admin__form-row" style={{ alignItems: "flex-end", gap: "8px" }}>
        <label>
          <span>Create placeholder</span>
          <select
            className="cora-input"
            value={newProvider}
            onChange={(e) => setNewProvider(e.target.value)}
          >
            {PROVIDERS.map((p) => (
              <option key={p.name} value={p.name}>
                {p.name} ({p.type})
              </option>
            ))}
          </select>
        </label>
        <button className="btn btn--primary" onClick={register} disabled={busy === "register"}>
          {busy === "register" ? "Creating…" : "Create Placeholder"}
        </button>
      </div>

      <h3 className="admin__vt-h3" style={{ marginTop: "16px" }}>
        OAuth Connections
      </h3>
      <div
        className="admin__hint"
        style={{
          border: "1px solid var(--border)",
          borderLeft: "3px solid var(--accent, #7c5cff)",
          borderRadius: 6,
          padding: "8px 12px",
          margin: "8px 0",
        }}
      >
        🔒 <strong>OAuth connection only. Provider execution remains disabled by
        governance.</strong>
      </div>
      <p className="admin__hint" style={{ marginTop: 0 }}>
        Connect a provider account via OAuth. <strong>Connecting does not enable
        execution</strong> — sending mail / creating events stays blocked by the
        global safety guard{oauthExecEnabled ? "" : " (currently disabled)"} and
        the approval gate. Tokens are encrypted at rest and never shown.
      </p>
      {oauth.length === 0 ? (
        <div className="admin__hint">No OAuth providers available.</div>
      ) : (
        <table className="admin__table">
          <thead>
            <tr>
              <th>Provider</th>
              <th>Type</th>
              <th>Connection</th>
              <th>Scopes</th>
              <th>Token expires</th>
              <th>Execution</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {oauth.map((p) => (
              <tr key={p.provider_name}>
                <td>{p.provider_name}</td>
                <td className="muted">{p.provider_type}</td>
                <td>{oauthChip(p.connection_status)}</td>
                <td className="muted">{(p.scopes || []).length || p.required_scopes.length || "—"}</td>
                <td className="muted">{fmt(p.token_expires_at)}</td>
                <td>
                  <span className="status-chip status-chip--archived" title={p.execution_note}>
                    {p.execution_enabled ? "enabled" : "disabled"}
                  </span>
                </td>
                <td>
                  {p.connection_status === "not_configured" ? (
                    <div
                      className="admin__row-actions"
                      style={{ flexDirection: "column", alignItems: "flex-start", gap: 2 }}
                    >
                      <button
                        className="btn btn--ghost btn--sm"
                        disabled
                        title="Set the provider OAuth environment variables to enable connecting"
                      >
                        Connect OAuth
                      </button>
                      <span className="muted" style={{ fontSize: "11px" }}>
                        Missing:{" "}
                        {(p.missing_config && p.missing_config.length
                          ? p.missing_config
                          : ["client_id", "client_secret", "redirect_uri"]
                        ).join(", ")}
                      </span>
                    </div>
                  ) : (
                    <div className="admin__row-actions">
                      {p.connection_status === "connected" ? (
                        <>
                          <button
                            className="btn btn--ghost btn--sm"
                            onClick={() => refreshOAuthConn(p.provider_name)}
                            disabled={busy === `oauth-${p.provider_name}`}
                            title="Refresh the access token (does not execute anything)"
                          >
                            {busy === `oauth-${p.provider_name}` ? "…" : "Refresh Token"}
                          </button>
                          <button
                            className="btn btn--ghost btn--sm"
                            onClick={() => disconnectOAuth(p)}
                            disabled={busy === `oauth-${p.provider_name}` || !p.connector_id}
                            title="Disconnect this provider (clears stored tokens; executes nothing)"
                          >
                            Disconnect
                          </button>
                        </>
                      ) : (
                        <>
                          <button
                            className="btn btn--ghost btn--sm"
                            onClick={() => connectOAuth(p.provider_name)}
                            disabled={busy === `oauth-${p.provider_name}`}
                            title="Open the provider consent screen"
                          >
                            {p.connection_status === "expired" ||
                            p.connection_status === "refresh_failed"
                              ? "Reconnect OAuth"
                              : "Connect OAuth"}
                          </button>
                          {(p.connection_status === "expired" ||
                            p.connection_status === "refresh_failed") && (
                            <button
                              className="btn btn--ghost btn--sm"
                              onClick={() => refreshOAuthConn(p.provider_name)}
                              disabled={busy === `oauth-${p.provider_name}`}
                              title="Refresh the access token (does not execute anything)"
                            >
                              {busy === `oauth-${p.provider_name}` ? "…" : "Refresh Token"}
                            </button>
                          )}
                        </>
                      )}
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3 className="admin__vt-h3" style={{ marginTop: "16px" }}>Readiness</h3>
      {readiness ? (
        <table className="admin__table">
          <thead>
            <tr>
              <th>Provider</th>
              <th>Type</th>
              <th>Status</th>
              <th>Scopes</th>
              <th>Tokens</th>
              <th>Token expires</th>
              <th>Ready</th>
              <th>Blockers</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {readiness.providers.map((p) => (
              <tr key={p.provider_name}>
                <td>{p.provider_name}</td>
                <td className="muted">{p.provider_type}</td>
                <td>{statusChip(p.status)}</td>
                <td className="muted">{(p.scopes || []).length || "—"}</td>
                <td className="muted">
                  {tokenCell(p.has_access_token, p.has_refresh_token)}
                </td>
                <td className="muted">{fmt(p.token_expires_at)}</td>
                <td>{p.ready_for_execution ? "yes" : "no"}</td>
                <td className="muted" style={{ fontSize: "12px" }}>
                  {p.blockers.length ? p.blockers.join("; ") : "none"}
                </td>
                <td>
                  <button
                    className="btn btn--ghost btn--sm"
                    onClick={() => showReadinessDetail(p)}
                  >
                    View Details
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="admin__hint">{loading ? "Loading…" : "No readiness data."}</div>
      )}

      <h3 className="admin__vt-h3" style={{ marginTop: "16px" }}>Connectors</h3>
      {connectors.length === 0 ? (
        <div className="admin__hint">No connector records yet.</div>
      ) : (
        <table className="admin__table">
          <thead>
            <tr>
              <th>Provider</th>
              <th>Type</th>
              <th>Status</th>
              <th>Scopes</th>
              <th>Tokens</th>
              <th>Token expires</th>
              <th>Disconnected</th>
              <th>Updated</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {connectors.map((c) => (
              <tr key={c.id}>
                <td>{c.provider_name}</td>
                <td className="muted">{c.provider_type}</td>
                <td>{statusChip(c.status)}</td>
                <td className="muted">{(c.scopes || []).length || "—"}</td>
                <td className="muted">
                  {tokenCell(c.has_access_token, c.has_refresh_token)}
                </td>
                <td className="muted">{fmt(c.token_expires_at)}</td>
                <td className="muted">{fmt(c.disconnected_at)}</td>
                <td className="muted">{fmt(c.updated_at)}</td>
                <td>
                  <div className="admin__row-actions">
                    <button
                      className="btn btn--ghost btn--sm"
                      onClick={() => showConnectorDetail(c)}
                    >
                      View Details
                    </button>
                    {c.status === "disconnected" ? (
                      <span className="muted" style={{ fontSize: "12px" }}>
                        disconnected
                      </span>
                    ) : (
                      <button
                        className="btn btn--ghost btn--sm btn--danger"
                        onClick={() => disconnect(c)}
                        disabled={busy === c.id}
                        title="Mark this connector disconnected (does not delete it)"
                      >
                        {busy === c.id ? "Disconnecting…" : "Disconnect"}
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {/* No real "Connect" button: real OAuth is intentionally absent in this phase. */}

      {detail && (
        <div className="modal-backdrop" onClick={() => setDetail(null)}>
          <div
            className="modal"
            style={{ maxWidth: "560px" }}
            role="dialog"
            aria-modal="true"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal__header">
              <h3 className="modal__title">{detail.title}</h3>
            </div>
            <div className="modal__body" style={{ whiteSpace: "normal" }}>
              <table className="admin__table" style={{ marginTop: 0 }}>
                <tbody>
                  {detail.rows.map(([k, v]) => (
                    <tr key={k}>
                      <td style={{ fontWeight: 600, width: "40%" }}>{k}</td>
                      <td className="muted">{v}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {detail.blockers && (
                <div style={{ margin: "10px 0 0" }}>
                  <strong>Blockers:</strong>
                  {detail.blockers.length ? (
                    <ul style={{ margin: "4px 0 0", paddingLeft: "20px" }}>
                      {detail.blockers.map((b, i) => (
                        <li key={i}>{b}</li>
                      ))}
                    </ul>
                  ) : (
                    " none"
                  )}
                </div>
              )}
              <div
                className="admin__row-actions"
                style={{ justifyContent: "flex-end", marginTop: "16px" }}
              >
                <button className="btn btn--primary" onClick={() => setDetail(null)}>
                  Close
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
