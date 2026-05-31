import { useCallback, useEffect, useMemo, useState } from "react";
import { listProviderFeatureFlags, updateProviderFeatureFlag } from "../api";
import type { ProviderFeatureFlag } from "../types";

export function ProviderFeatureFlags({ isAdmin }: { isAdmin: boolean }) {
  const [flags, setFlags] = useState<ProviderFeatureFlag[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [provider, setProvider] = useState("");
  const [action, setAction] = useState("");
  const [environment, setEnvironment] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await listProviderFeatureFlags({
        provider_name: provider || undefined,
        action_type: action || undefined,
        environment: environment || undefined,
      });
      setFlags(r.flags);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load flags");
    } finally {
      setLoading(false);
    }
  }, [provider, action, environment]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 2500);
    return () => clearTimeout(t);
  }, [toast]);

  const toggle = async (
    f: ProviderFeatureFlag,
    field: "enabled" | "dry_run_only",
  ) => {
    setBusyId(f.id);
    setError(null);
    try {
      await updateProviderFeatureFlag(f.id, { [field]: !f[field] });
      setToast("Flag updated — execution still globally disabled.");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update flag");
    } finally {
      setBusyId(null);
    }
  };

  const environments = useMemo(
    () => Array.from(new Set(flags.map((f) => f.environment))).sort(),
    [flags],
  );
  const providers = useMemo(
    () => Array.from(new Set(flags.map((f) => f.provider_name))).sort(),
    [flags],
  );

  return (
    <section className="admin__section">
      <div className="admin__section-head">
        <h2>Provider Feature Flags</h2>
        <button className="btn btn--ghost btn--sm" onClick={load} disabled={loading}>
          ↻ Refresh
        </button>
      </div>

      <div
        className="admin__error"
        style={{ display: "flex", alignItems: "center", gap: 8 }}
      >
        🔒 <strong>External execution is globally disabled.</strong> A feature flag
        is necessary but NOT sufficient — the kill switch + final interlock still
        block every real action. Toggling a flag here changes the matrix only.
      </div>

      {error && <div className="admin__error">{error}</div>}
      {toast && <div className="admin__toast">{toast}</div>}

      <div className="admin__form-row" style={{ gap: 8, alignItems: "flex-end" }}>
        <label>
          <span>Provider</span>
          <select className="cora-input" value={provider} onChange={(e) => setProvider(e.target.value)}>
            <option value="">All</option>
            {providers.map((p) => (
              <option key={p} value={p}>{p}</option>
            ))}
          </select>
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
          <span>Environment</span>
          <select className="cora-input" value={environment} onChange={(e) => setEnvironment(e.target.value)}>
            <option value="">All</option>
            {environments.map((e) => (
              <option key={e} value={e}>{e}</option>
            ))}
          </select>
        </label>
      </div>

      {flags.length === 0 ? (
        <div className="admin__hint">No feature flags match.</div>
      ) : (
        <table className="admin__table">
          <thead>
            <tr>
              <th>Provider</th>
              <th>Type</th>
              <th>Action</th>
              <th>Environment</th>
              <th>Enabled</th>
              <th>Dry-run only</th>
              <th>Approval</th>
              <th>Interlock</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {flags.map((f) => (
              <tr key={f.id}>
                <td>{f.provider_name}</td>
                <td className="muted">{f.provider_type}</td>
                <td className="muted">{f.action_type}</td>
                <td className="muted">{f.environment}</td>
                <td>{f.enabled ? "enabled" : "disabled"}</td>
                <td>{f.dry_run_only ? "yes" : "no"}</td>
                <td className="muted">{f.requires_human_approval ? "required" : "—"}</td>
                <td className="muted">{f.requires_final_interlock ? "required" : "—"}</td>
                <td>
                  <div className="admin__row-actions" style={{ gap: 4, flexWrap: "wrap" }}>
                    <button
                      className="btn btn--ghost btn--sm"
                      onClick={() => toggle(f, "enabled")}
                      disabled={!isAdmin || busyId === f.id}
                      title="Enable/disable this provider/action in the matrix"
                    >
                      {f.enabled ? "Disable" : "Enable"}
                    </button>
                    <button
                      className="btn btn--ghost btn--sm"
                      onClick={() => toggle(f, "dry_run_only")}
                      disabled={!isAdmin || busyId === f.id}
                      title="Toggle dry-run-only for this flag"
                    >
                      {f.dry_run_only ? "Clear dry-run" : "Set dry-run"}
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
