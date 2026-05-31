import { useCallback, useEffect, useState } from "react";
import {
  listIntegrationIntents,
  getIntegrationIntent,
  validateIntegrationIntent,
  confirmIntegrationIntent,
  cancelIntegrationIntent,
  listIntegrationIntentEvents,
  dryRunIntegrationIntent,
} from "../api";
import type {
  ExternalIntegrationIntent,
  ExternalIntegrationEvent,
  IntegrationIntentStatus,
} from "../types";

function statusChip(s: IntegrationIntentStatus) {
  const cls =
    s === "confirmed"
      ? "status-chip status-chip--active"
      : s === "blocked"
        ? "status-chip status-chip--archived"
        : s === "cancelled"
          ? "status-chip status-chip--archived"
          : "status-chip status-chip--draft";
  return <span className={cls}>{s.replace(/_/g, " ")}</span>;
}

export function IntegrationReadiness({
  workspaceId,
  isAdmin = false,
}: {
  workspaceId: string | null;
  isAdmin?: boolean;
}) {
  const [intents, setIntents] = useState<ExternalIntegrationIntent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState("");
  const [sourceFilter, setSourceFilter] = useState("");
  const [agentFilter, setAgentFilter] = useState("");
  const [selected, setSelected] = useState<ExternalIntegrationIntent | null>(null);
  const [events, setEvents] = useState<ExternalIntegrationEvent[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [dryRunMsg, setDryRunMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setIntents(
        await listIntegrationIntents({
          workspace_id: workspaceId || undefined,
          status: statusFilter || undefined,
          source_type: sourceFilter || undefined,
          agent_name: agentFilter || undefined,
        }),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load intents");
    } finally {
      setLoading(false);
    }
  }, [workspaceId, statusFilter, sourceFilter, agentFilter]);

  useEffect(() => {
    load();
  }, [load]);

  const openDetail = async (intent: ExternalIntegrationIntent) => {
    if (selected?.id === intent.id) {
      setSelected(null);
      setEvents(null);
      return;
    }
    setSelected(intent);
    setEvents(null);
    setDryRunMsg(null);
    try {
      const fresh = await getIntegrationIntent(intent.id);
      setSelected(fresh);
    } catch {
      /* keep list version */
    }
  };

  const act = async (
    intent: ExternalIntegrationIntent,
    fn: (id: string) => Promise<ExternalIntegrationIntent>,
  ) => {
    setBusy(true);
    setError(null);
    try {
      const updated = await fn(intent.id);
      setSelected(updated);
      setEvents(null);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Action failed");
    } finally {
      setBusy(false);
    }
  };

  const runDryRun = async (intent: ExternalIntegrationIntent) => {
    setBusy(true);
    setError(null);
    setDryRunMsg(null);
    try {
      const updated = await dryRunIntegrationIntent(intent.id);
      setSelected(updated);
      setEvents(null);
      setDryRunMsg(
        "Dry run complete. dry_run=true · external_action_performed=false — No external action was performed.",
      );
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Dry run failed");
    } finally {
      setBusy(false);
    }
  };

  const loadEvents = async (intent: ExternalIntegrationIntent) => {
    try {
      setEvents(await listIntegrationIntentEvents(intent.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load events");
    }
  };

  return (
    <section className="admin__section">
      <div className="admin__section-head">
        <h2>Integration Readiness</h2>
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
        ⚠ Readiness mode only. Cora will not send email, create calendar events,
        or contact external systems from this screen.
      </div>

      <p className="admin__hint">
        Dry-run integration intents prepared from approved SIGNAL drafts and
        CHRONOS proposals. Confirmation is internal only until a real provider
        connector is added — no external action is ever performed here.
      </p>

      <div className="admin__form-row" style={{ marginTop: "10px" }}>
        <label>
          <span>Status</span>
          <select
            className="cora-input"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
          >
            <option value="">All</option>
            <option value="draft">draft</option>
            <option value="ready_for_confirmation">ready_for_confirmation</option>
            <option value="confirmed">confirmed</option>
            <option value="blocked">blocked</option>
            <option value="cancelled">cancelled</option>
          </select>
        </label>
        <label>
          <span>Source type</span>
          <select
            className="cora-input"
            value={sourceFilter}
            onChange={(e) => setSourceFilter(e.target.value)}
          >
            <option value="">All</option>
            <option value="communication_draft">communication_draft</option>
            <option value="schedule_proposal">schedule_proposal</option>
          </select>
        </label>
        <label>
          <span>Agent</span>
          <select
            className="cora-input"
            value={agentFilter}
            onChange={(e) => setAgentFilter(e.target.value)}
          >
            <option value="">All</option>
            <option value="SIGNAL">SIGNAL</option>
            <option value="CHRONOS">CHRONOS</option>
          </select>
        </label>
      </div>

      {error && <div className="admin__error">{error}</div>}

      {loading && intents.length === 0 ? (
        <div className="admin__hint">Loading intents…</div>
      ) : intents.length === 0 ? (
        <div className="admin__hint">
          No integration intents yet. Approve a SIGNAL draft or CHRONOS proposal,
          then use “Prepare Send Intent” / “Prepare Calendar Intent”.
        </div>
      ) : (
        <table className="admin__table">
          <thead>
            <tr>
              <th>Agent / Provider</th>
              <th>Action</th>
              <th>Status</th>
              <th>Dry run</th>
              <th>Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {intents.map((i) => (
              <tr key={i.id}>
                <td>
                  <div>{i.agent_name}</div>
                  <div className="muted" style={{ fontSize: "11px" }}>
                    {i.provider_type} · {i.provider_name}
                  </div>
                </td>
                <td className="muted">{i.action_type}</td>
                <td>{statusChip(i.status)}</td>
                <td className="muted">{i.dry_run ? "yes" : "no"}</td>
                <td className="muted">
                  {new Date(i.created_at).toLocaleString()}
                </td>
                <td>
                  <button
                    className="btn btn--ghost btn--sm"
                    onClick={() => openDetail(i)}
                  >
                    {selected?.id === i.id ? "Close" : "Details"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {selected && (
        <div
          className="agent-test-result-card"
          style={{ marginTop: "16px" }}
        >
          <h3>
            Intent {selected.provider_type} · {selected.action_type}{" "}
            {statusChip(selected.status)}
          </h3>
          <p className="admin__hint" style={{ marginTop: 0 }}>
            {selected.confirmation_required_reason ||
              "This is a dry-run integration intent. No external action was performed."}
          </p>

          <ValidationBlock result={selected.validation_result} />

          <h4 className="admin__vt-h3">Payload preview (dry-run)</h4>
          <pre className="trace-json" style={{ whiteSpace: "pre-wrap" }}>
            {JSON.stringify(selected.payload_preview, null, 2)}
          </pre>

          {dryRunMsg && (
            <div className="admin__hint" style={{ color: "var(--ok, #6ad19a)" }}>
              {dryRunMsg}
            </div>
          )}

          <div className="admin__row-actions" style={{ flexWrap: "wrap" }}>
            <button
              className="btn btn--ghost btn--sm"
              onClick={() => act(selected, validateIntegrationIntent)}
              disabled={busy}
            >
              Validate
            </button>
            <button
              className="btn btn--ghost btn--sm"
              onClick={() => runDryRun(selected)}
              disabled={busy}
              title="Builds the provider payload preview. No external action is performed."
            >
              Dry Run Provider Payload
            </button>
            {selected.status === "ready_for_confirmation" && (
              <button
                className="btn btn--primary btn--sm"
                onClick={() => act(selected, (id) => confirmIntegrationIntent(id))}
                disabled={busy || !isAdmin}
                title={
                  isAdmin
                    ? "Confirm internally (no external action)"
                    : "Confirmation requires an admin reviewer"
                }
              >
                Confirm Internally
              </button>
            )}
            {selected.status !== "cancelled" &&
              selected.status !== "executed_placeholder" && (
                <button
                  className="btn btn--ghost btn--sm"
                  onClick={() => act(selected, (id) => cancelIntegrationIntent(id))}
                  disabled={busy}
                >
                  Cancel
                </button>
              )}
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

function ValidationBlock({
  result,
}: {
  result: ExternalIntegrationIntent["validation_result"];
}) {
  const hard = result?.hard_errors ?? [];
  const warn = result?.warnings ?? [];
  if (hard.length === 0 && warn.length === 0) {
    return (
      <div className="admin__hint">Validation: no errors or warnings.</div>
    );
  }
  return (
    <div style={{ marginBottom: "8px" }}>
      {hard.length > 0 && (
        <div className="admin__error">
          Hard errors: {hard.join("; ")}
        </div>
      )}
      {warn.length > 0 && (
        <div className="admin__hint" style={{ color: "var(--warn, #d8a23c)" }}>
          Warnings: {warn.join("; ")}
        </div>
      )}
    </div>
  );
}
