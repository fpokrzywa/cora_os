import { useCallback, useEffect, useState } from "react";
import {
  listIntegrationProviders,
  updateIntegrationProvider,
} from "../api";
import type { ExternalProviderConnector } from "../types";

function boolChip(v: boolean, label: string) {
  return (
    <span
      className={`status-chip ${v ? "status-chip--active" : "status-chip--archived"}`}
    >
      {label}: {v ? "yes" : "no"}
    </span>
  );
}

export function IntegrationProviders({
  isAdmin = false,
}: {
  isAdmin?: boolean;
}) {
  const [providers, setProviders] = useState<ExternalProviderConnector[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<ExternalProviderConnector | null>(null);
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [metaText, setMetaText] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setProviders(await listIntegrationProviders());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load providers");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const openEdit = (p: ExternalProviderConnector) => {
    if (selected?.provider_name === p.provider_name) {
      setSelected(null);
      return;
    }
    setSelected(p);
    setDisplayName(p.display_name);
    setDescription(p.description ?? "");
    setMetaText(JSON.stringify(p.metadata ?? {}, null, 2));
    setMsg(null);
  };

  const toggleEnabled = async (p: ExternalProviderConnector, enabled: boolean) => {
    setBusy(true);
    setError(null);
    try {
      await updateIntegrationProvider(p.provider_name, { enabled });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Update failed");
    } finally {
      setBusy(false);
    }
  };

  const saveDetails = async (p: ExternalProviderConnector) => {
    setBusy(true);
    setError(null);
    setMsg(null);
    let metadata: Record<string, unknown> | undefined;
    if (metaText.trim()) {
      try {
        metadata = JSON.parse(metaText);
      } catch {
        setError("Metadata must be valid JSON.");
        setBusy(false);
        return;
      }
    }
    try {
      const updated = await updateIntegrationProvider(p.provider_name, {
        display_name: displayName,
        description: description || undefined,
        metadata,
      });
      setSelected(updated);
      setMsg("Saved. Live-execution capabilities remain disabled (dry-run only).");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="admin__section">
      <div className="admin__section-head">
        <h2>Integration Providers</h2>
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
        ⚠ Provider connectors are in dry-run design mode. Cora will not send
        email, create calendar events, or contact external provider APIs.
      </div>

      {error && <div className="admin__error">{error}</div>}
      {msg && <div className="admin__hint">{msg}</div>}

      {loading && providers.length === 0 ? (
        <div className="admin__hint">Loading providers…</div>
      ) : (
        <table className="admin__table">
          <thead>
            <tr>
              <th>Provider</th>
              <th>Type</th>
              <th>Enabled</th>
              <th>Dry run only</th>
              <th>OAuth</th>
              <th>Capabilities</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {providers.map((p) => (
              <tr key={p.provider_name}>
                <td>
                  <div>{p.display_name}</div>
                  <div className="muted" style={{ fontSize: "11px" }}>
                    {p.provider_name}
                  </div>
                </td>
                <td className="muted">{p.provider_type}</td>
                <td>{boolChip(p.enabled, "on")}</td>
                <td>{boolChip(p.dry_run_only, "dry")}</td>
                <td className="muted">{p.requires_oauth ? "required" : "no"}</td>
                <td className="muted" style={{ fontSize: "11px" }}>
                  {[
                    p.supports_draft && "draft",
                    p.supports_send && "send",
                    p.supports_calendar_create && "cal-create",
                    p.supports_calendar_update && "cal-update",
                    p.supports_read && "read",
                  ]
                    .filter(Boolean)
                    .join(", ") || "—"}
                </td>
                <td>
                  {isAdmin && (
                    <div className="admin__row-actions">
                      <button
                        className="btn btn--ghost btn--sm"
                        onClick={() => toggleEnabled(p, !p.enabled)}
                        disabled={busy}
                      >
                        {p.enabled ? "Disable" : "Enable"}
                      </button>
                      <button
                        className="btn btn--ghost btn--sm"
                        onClick={() => openEdit(p)}
                      >
                        {selected?.provider_name === p.provider_name
                          ? "Close"
                          : "Edit"}
                      </button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {selected && isAdmin && (
        <div className="admin__form" style={{ marginTop: "12px" }}>
          <h3 className="admin__vt-h3">Edit {selected.provider_name}</h3>
          <p className="admin__hint" style={{ marginTop: 0 }}>
            Live-execution capabilities (send / calendar create / update / read)
            cannot be enabled here and stay disabled. Dry-run only.
          </p>
          <label>
            <span>Display name</span>
            <input
              className="cora-input"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
            />
          </label>
          <label>
            <span>Description</span>
            <textarea
              className="cora-input"
              rows={2}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
          <label>
            <span>Metadata (JSON)</span>
            <textarea
              className="cora-input"
              rows={4}
              value={metaText}
              onChange={(e) => setMetaText(e.target.value)}
            />
          </label>
          <div className="admin__form-row">
            <span className="muted" style={{ fontSize: "11px" }}>
              Capabilities (read-only):{" "}
              {JSON.stringify(selected.capabilities ?? {})}
            </span>
          </div>
          <div className="admin__form-row">
            <button
              className="btn btn--primary"
              onClick={() => saveDetails(selected)}
              disabled={busy}
            >
              {busy ? "Saving…" : "Save"}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
