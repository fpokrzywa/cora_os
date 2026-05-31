import { useCallback, useEffect, useState } from "react";
import {
  listIntegrationCredentials,
  createIntegrationCredential,
  getIntegrationCredential,
  disableIntegrationCredential,
  markCredentialNeedsAuthorization,
  validateCredentialPlaceholder,
  rotateCredentialPlaceholder,
  listCredentialEvents,
  listIntegrationProviders,
} from "../api";
import type {
  ExternalProviderCredential,
  ExternalProviderCredentialEvent,
  ExternalProviderConnector,
  CredentialStatus,
} from "../types";

function statusChip(s: CredentialStatus) {
  const cls =
    s === "authorized_placeholder" || s === "configured"
      ? "status-chip status-chip--active"
      : s === "disabled" || s === "revoked" || s === "expired"
        ? "status-chip status-chip--archived"
        : s === "error"
          ? "status-chip status-chip--error"
          : "status-chip status-chip--draft";
  return <span className={cls}>{s.replace(/_/g, " ")}</span>;
}

function scopeLabel(c: ExternalProviderCredential): string {
  if (c.user_id) return "user";
  if (c.workspace_id) return "workspace";
  return "global";
}

export function CredentialVault({
  workspaceId,
  isAdmin = false,
  currentUserId,
}: {
  workspaceId: string | null;
  isAdmin?: boolean;
  currentUserId?: string;
}) {
  const [creds, setCreds] = useState<ExternalProviderCredential[]>([]);
  const [providers, setProviders] = useState<ExternalProviderConnector[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // create form
  const [provider, setProvider] = useState("");
  const [name, setName] = useState("");
  const [scope, setScope] = useState<"user" | "workspace">("user");
  const [scopes, setScopes] = useState("");
  const [clientIdHint, setClientIdHint] = useState("");
  const [metaText, setMetaText] = useState("");

  // detail panel
  const [selected, setSelected] = useState<ExternalProviderCredential | null>(null);
  const [events, setEvents] = useState<ExternalProviderCredentialEvent[] | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setCreds(await listIntegrationCredentials());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load credentials");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    listIntegrationProviders()
      .then((p) => {
        setProviders(p);
        if (!provider && p.length) setProvider(p[0].provider_name);
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load]);

  const resetForm = () => {
    setName("");
    setScopes("");
    setClientIdHint("");
    setMetaText("");
  };

  const create = async () => {
    setBusy(true);
    setError(null);
    setMsg(null);
    let metadata: Record<string, unknown> = {};
    if (metaText.trim()) {
      try {
        metadata = JSON.parse(metaText);
      } catch {
        setError("Metadata must be valid JSON.");
        setBusy(false);
        return;
      }
    }
    const prov = providers.find((p) => p.provider_name === provider);
    const payload = {
      provider_name: provider,
      provider_type: prov?.provider_type ?? "email",
      credential_name: name,
      scopes: scopes
        .split(/[,\n]/)
        .map((s) => s.trim())
        .filter(Boolean),
      client_id_hint: clientIdHint || null,
      // Non-admins always create personal records (backend forces owner=self);
      // admins can target the current workspace.
      workspace_id:
        isAdmin && scope === "workspace" ? workspaceId : undefined,
      user_id:
        scope === "user" ? currentUserId : undefined,
      metadata,
    };
    try {
      await createIntegrationCredential(payload);
      setMsg("Credential placeholder created (dry-run only, no secrets stored).");
      resetForm();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Create failed");
    } finally {
      setBusy(false);
    }
  };

  const openDetail = async (c: ExternalProviderCredential) => {
    if (selected?.id === c.id) {
      setSelected(null);
      setEvents(null);
      return;
    }
    setSelected(c);
    setEvents(null);
    try {
      setSelected(await getIntegrationCredential(c.id));
    } catch {
      /* keep list version */
    }
  };

  const act = async (
    c: ExternalProviderCredential,
    fn: (id: string) => Promise<ExternalProviderCredential>,
    okMsg: string,
  ) => {
    setBusy(true);
    setError(null);
    setMsg(null);
    try {
      const updated = await fn(c.id);
      setSelected(updated);
      setEvents(null);
      setMsg(okMsg);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Action failed");
    } finally {
      setBusy(false);
    }
  };

  const loadEvents = async (c: ExternalProviderCredential) => {
    try {
      setEvents(await listCredentialEvents(c.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load events");
    }
  };

  return (
    <section className="admin__section">
      <div className="admin__section-head">
        <h2>Credential Vault</h2>
        <button className="btn btn--ghost btn--sm" onClick={load} disabled={loading}>
          ↻ Refresh
        </button>
      </div>

      <div
        className="admin__error"
        style={{
          background: "rgba(180,120,40,0.15)",
          borderColor: "rgba(220,160,60,0.5)",
          color: "var(--text, #e8e3f5)",
        }}
      >
        ⚠ Credential Vault is in readiness mode. No OAuth exchange or provider
        API calls are performed. No secrets are accepted or stored in this
        version.
      </div>

      {error && <div className="admin__error">{error}</div>}
      {msg && <div className="admin__hint">{msg}</div>}

      {/* Create form */}
      <div className="admin__form" style={{ marginTop: "12px" }}>
        <h3 className="admin__vt-h3">New credential placeholder</h3>
        <div className="admin__form-row">
          <label>
            <span>Provider</span>
            <select
              className="cora-input"
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
            >
              {providers.map((p) => (
                <option key={p.provider_name} value={p.provider_name}>
                  {p.display_name} ({p.provider_type})
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Credential name</span>
            <input
              className="cora-input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Freddie Gmail"
            />
          </label>
          <label>
            <span>Scope</span>
            <select
              className="cora-input"
              value={scope}
              onChange={(e) => setScope(e.target.value as "user" | "workspace")}
            >
              <option value="user">User (me)</option>
              {isAdmin && <option value="workspace">Workspace</option>}
            </select>
          </label>
        </div>
        <label>
          <span>Scopes (comma or newline separated)</span>
          <textarea
            className="cora-input"
            rows={2}
            value={scopes}
            onChange={(e) => setScopes(e.target.value)}
            placeholder="https://www.googleapis.com/auth/gmail.send"
          />
        </label>
        <label>
          <span>Client ID hint (non-secret label or public client id)</span>
          <input
            className="cora-input"
            value={clientIdHint}
            onChange={(e) => setClientIdHint(e.target.value)}
          />
        </label>
        <label>
          <span>Metadata (JSON)</span>
          <textarea
            className="cora-input"
            rows={3}
            value={metaText}
            onChange={(e) => setMetaText(e.target.value)}
            placeholder="{}"
          />
        </label>
        <p className="admin__hint" style={{ marginTop: 0 }}>
          No client secret or token fields exist — secrets cannot be entered or
          stored until encryption is implemented in a later module.
        </p>
        <div className="admin__form-row">
          <button
            className="btn btn--primary"
            onClick={create}
            disabled={busy || !provider || !name.trim()}
          >
            {busy ? "Saving…" : "Create placeholder"}
          </button>
        </div>
      </div>

      {/* List */}
      {loading && creds.length === 0 ? (
        <div className="admin__hint">Loading credentials…</div>
      ) : creds.length === 0 ? (
        <div className="admin__hint">No credential placeholders yet.</div>
      ) : (
        <table className="admin__table" style={{ marginTop: "12px" }}>
          <thead>
            <tr>
              <th>Name</th>
              <th>Provider</th>
              <th>Type</th>
              <th>Scope</th>
              <th>Status</th>
              <th>Dry run</th>
              <th>Last validated</th>
              <th>Expires</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {creds.map((c) => (
              <tr key={c.id}>
                <td>{c.credential_name}</td>
                <td className="muted">{c.provider_name}</td>
                <td className="muted">{c.provider_type}</td>
                <td className="muted">{scopeLabel(c)}</td>
                <td>{statusChip(c.status)}</td>
                <td className="muted">{c.dry_run_only ? "yes" : "no"}</td>
                <td className="muted">
                  {c.last_validated_at
                    ? new Date(c.last_validated_at).toLocaleString()
                    : "—"}
                </td>
                <td className="muted">
                  {c.token_expires_at
                    ? new Date(c.token_expires_at).toLocaleString()
                    : "—"}
                </td>
                <td>
                  <button
                    className="btn btn--ghost btn--sm"
                    onClick={() => openDetail(c)}
                  >
                    {selected?.id === c.id ? "Close" : "Details"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {/* Detail panel */}
      {selected && (
        <div className="agent-test-result-card" style={{ marginTop: "16px" }}>
          <h3>
            {selected.credential_name} {statusChip(selected.status)}
          </h3>
          <p className="admin__hint" style={{ marginTop: 0 }}>
            {selected.provider_name} · {selected.provider_type} · scope{" "}
            {scopeLabel(selected)} · dry-run only. Secrets present:{" "}
            access={String(selected.has_access_token)} · refresh=
            {String(selected.has_refresh_token)} · client_secret=
            {String(selected.has_client_secret)} (always masked).
          </p>

          {selected.scopes.length > 0 && (
            <p className="muted" style={{ fontSize: "11px" }}>
              Scopes: {selected.scopes.join(", ")}
            </p>
          )}

          {selected.validation && (
            <div
              className="admin__hint"
              style={{
                color: selected.validation.ok
                  ? "var(--ok, #6ad19a)"
                  : "var(--warn, #d8a23c)",
              }}
            >
              Placeholder validation {selected.validation.ok ? "passed" : "incomplete"} —{" "}
              {selected.validation.note}
            </div>
          )}

          <div className="admin__row-actions" style={{ flexWrap: "wrap" }}>
            <button
              className="btn btn--ghost btn--sm"
              onClick={() =>
                act(
                  selected,
                  validateCredentialPlaceholder,
                  "Placeholder validated (no provider call).",
                )
              }
              disabled={busy}
              title="Shape-only check. No OAuth or provider API call."
            >
              Validate Placeholder
            </button>
            <button
              className="btn btn--ghost btn--sm"
              onClick={() =>
                act(
                  selected,
                  markCredentialNeedsAuthorization,
                  "Marked as needing authorization.",
                )
              }
              disabled={busy}
            >
              Mark Needs Authorization
            </button>
            <button
              className="btn btn--ghost btn--sm"
              onClick={() =>
                act(
                  selected,
                  rotateCredentialPlaceholder,
                  "Rotated (placeholder); re-authorization required.",
                )
              }
              disabled={busy}
              title="Simulated rotation. No real secret exists to rotate."
            >
              Rotate Placeholder
            </button>
            <button
              className="btn btn--ghost btn--sm"
              onClick={() =>
                act(selected, disableIntegrationCredential, "Credential disabled.")
              }
              disabled={busy || selected.status === "disabled"}
            >
              Disable
            </button>
            <button
              className="btn btn--ghost btn--sm"
              onClick={() => loadEvents(selected)}
              disabled={busy}
            >
              View History
            </button>
          </div>

          {events && (
            <div style={{ marginTop: "10px" }}>
              {events.length === 0 ? (
                <div className="admin__hint">No events yet.</div>
              ) : (
                <table className="admin__table" style={{ fontSize: "11px" }}>
                  <thead>
                    <tr>
                      <th>Event</th>
                      <th>From → To</th>
                      <th>Notes</th>
                      <th>When</th>
                    </tr>
                  </thead>
                  <tbody>
                    {events.map((ev) => (
                      <tr key={ev.id}>
                        <td>{ev.event_type.replace(/_/g, " ")}</td>
                        <td className="muted">
                          {ev.from_status ?? "—"} → {ev.to_status ?? "—"}
                        </td>
                        <td className="muted">{ev.notes || "—"}</td>
                        <td className="muted">
                          {new Date(ev.created_at).toLocaleString()}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
