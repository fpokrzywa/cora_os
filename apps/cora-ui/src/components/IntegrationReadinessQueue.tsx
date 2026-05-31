import { useCallback, useEffect, useState } from "react";
import {
  listReadinessIntents,
  cancelReadinessIntent,
  confirmReadinessIntent,
  revokeReadinessIntent,
  checkIntentReadiness,
  getIntentReadiness,
  getExecutionStatus,
  executeIntent,
  simulateProviderExecution,
  simulateProviderPayload,
} from "../api";
import type {
  ExternalIntegrationIntent,
  IntegrationIntentStatus,
  OAuthReadinessResult,
  ExecutionStatus,
  CredentialUsageSimulation,
} from "../types";

const INTENT_TYPE_LABEL: Record<string, string> = {
  email_send_intent: "Email send",
  calendar_create_intent: "Calendar create",
  // action_type column values (real schema)
  send_email: "Email send",
  create_calendar_event: "Calendar create",
};

const SOURCE_LABEL: Record<string, string> = {
  communication_draft: "SIGNAL draft",
  schedule_proposal: "CHRONOS proposal",
};

// Spec visual status rules.
const STATUS_LABEL: Record<string, string> = {
  draft: "Pending",
  pending_provider: "Pending",
  blocked_no_provider: "Blocked: No Provider",
  blocked_no_oauth: "Blocked: OAuth Required",
  ready_for_future_execution: "Future Ready",
  confirmed: "Confirmed",
  confirmation_revoked: "Confirmation Revoked",
  cancelled: "Cancelled",
};

function statusChip(s: IntegrationIntentStatus) {
  const cls =
    s === "ready_for_future_execution" || s === "confirmed"
      ? "status-chip status-chip--active"
      : s === "cancelled"
        ? "status-chip status-chip--archived"
        : "status-chip status-chip--draft";
  return <span className={cls}>{STATUS_LABEL[s] ?? s.replace(/_/g, " ")}</span>;
}

// Blockers expected this phase — they do NOT prevent confirmation. Mirrors the
// backend's _NON_CRITICAL_BLOCKER_MARKERS.
const NON_CRITICAL_BLOCKER_MARKERS = [
  "external execution is disabled",
  "governance blocks external execution",
  "execution disabled by governance",
];

function criticalBlockers(v: unknown): string[] {
  if (!v || typeof v !== "object") return [];
  const blockers = (v as { blockers?: unknown }).blockers;
  if (!Array.isArray(blockers)) return [];
  return blockers
    .map((b) => String(b))
    .filter(
      (b) => !NON_CRITICAL_BLOCKER_MARKERS.some((m) => b.toLowerCase().includes(m)),
    );
}

function confirmable(it: ExternalIntegrationIntent): boolean {
  return (
    it.requires_confirmation === true &&
    it.dry_run === true &&
    !!it.validation_result &&
    Object.keys(it.validation_result).length > 0 &&
    criticalBlockers(it.validation_result).length === 0 &&
    it.status !== "cancelled" &&
    it.status !== "confirmed"
  );
}

function md(intent: ExternalIntegrationIntent, key: string): string {
  const v = (intent.metadata || {})[key];
  return typeof v === "string" ? v : "";
}

function sourceTitle(intent: ExternalIntegrationIntent): string {
  return md(intent, "subject") || md(intent, "title") || `${intent.source_id.slice(0, 8)}…`;
}

function intentLabel(it: ExternalIntegrationIntent): string {
  return (
    INTENT_TYPE_LABEL[md(it, "intent_type")] ||
    INTENT_TYPE_LABEL[it.action_type] ||
    it.action_type
  );
}

function fmt(d: string | null | undefined): string {
  return d ? new Date(d).toLocaleString() : "—";
}

function validationSummary(v: unknown): string {
  if (!v || typeof v !== "object") return "—";
  const blockers = Array.isArray((v as { blockers?: unknown }).blockers)
    ? ((v as { blockers: unknown[] }).blockers).length
    : null;
  const ready = (v as { ready_for_execution?: unknown }).ready_for_execution;
  if (ready === true) return "ready";
  if (blockers != null) return `${blockers} blocker(s)`;
  if ((v as { ok?: unknown }).ok === true) return "ok";
  return Object.keys(v).length ? "see details" : "—";
}

function Json({ value }: { value: unknown }) {
  return (
    <pre
      style={{
        background: "var(--surface-2, #1b1b27)",
        padding: "8px",
        borderRadius: "6px",
        fontSize: "12px",
        overflowX: "auto",
        margin: "4px 0 0",
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
      }}
    >
      {JSON.stringify(value ?? null, null, 2)}
    </pre>
  );
}

export function IntegrationReadinessQueue({
  isAdmin = false,
  onNavigate,
}: {
  workspaceId?: string | null;
  isAdmin?: boolean;
  onNavigate?: (tab: string, sub?: string) => void;
}) {
  const [intents, setIntents] = useState<ExternalIntegrationIntent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [detail, setDetail] = useState<ExternalIntegrationIntent | null>(null);
  const [credSim, setCredSim] = useState<CredentialUsageSimulation | null>(null);
  const [readiness, setReadiness] = useState<OAuthReadinessResult | null>(null);
  const [readinessLoading, setReadinessLoading] = useState(false);
  const [execStatus, setExecStatus] = useState<ExecutionStatus | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [list, status] = await Promise.all([
        listReadinessIntents(),
        getExecutionStatus().catch(() => null),
      ]);
      setIntents(list);
      if (status) setExecStatus(status);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load intents");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 2500);
    return () => clearTimeout(t);
  }, [toast]);

  const openDetail = async (intent: ExternalIntegrationIntent) => {
    setDetail(intent);
    setReadiness(null);
    setReadinessLoading(true);
    try {
      setReadiness(await getIntentReadiness(intent.id));
    } catch {
      setReadiness(null);
    } finally {
      setReadinessLoading(false);
    }
  };

  const simulate = async (intent: ExternalIntegrationIntent) => {
    setBusyId(intent.id);
    setError(null);
    try {
      const r = await checkIntentReadiness(intent.id);
      setToast(
        r.ready_for_execution
          ? "Readiness checked — ready."
          : `Readiness checked — ${r.blockers.length} blocker(s).`,
      );
      if (detail && detail.id === intent.id) setReadiness(r);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to check readiness");
    } finally {
      setBusyId(null);
    }
  };

  const cancel = async (intent: ExternalIntegrationIntent) => {
    setBusyId(intent.id);
    setError(null);
    try {
      await cancelReadinessIntent(intent.id);
      setToast("Intent cancelled.");
      if (detail && detail.id === intent.id) setDetail(null);
      await load();
    } catch (err) {
      const m = err instanceof Error ? err.message : "Failed to cancel intent";
      setError(
        m.startsWith("404")
          ? "That intent no longer exists — it may have already been removed."
          : m,
      );
    } finally {
      setBusyId(null);
    }
  };

  const confirm = async (intent: ExternalIntegrationIntent) => {
    setBusyId(intent.id);
    setError(null);
    try {
      const updated = await confirmReadinessIntent(intent.id);
      setToast("Intent confirmed (no execution — still disabled).");
      if (detail && detail.id === intent.id) setDetail(updated);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to confirm intent");
    } finally {
      setBusyId(null);
    }
  };

  const revoke = async (intent: ExternalIntegrationIntent) => {
    setBusyId(intent.id);
    setError(null);
    try {
      const updated = await revokeReadinessIntent(intent.id);
      setToast("Confirmation revoked.");
      if (detail && detail.id === intent.id) setDetail(updated);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to revoke confirmation");
    } finally {
      setBusyId(null);
    }
  };

  const simulateExecution = async (intent: ExternalIntegrationIntent) => {
    setBusyId(intent.id);
    setError(null);
    try {
      // Pending-provider intents have no bound provider yet; target a
      // representative stub adapter for the dry-run by provider_type.
      const provider =
        intent.provider_type === "calendar" ? "google_calendar" : "gmail";
      const r = await simulateProviderExecution(intent.id, provider);
      if (r.status === "simulated") {
        setToast(r.message);
      } else {
        setError(
          `Simulation ${r.status}: ${r.message}` +
            (r.errors.length ? ` (${r.errors.join("; ")})` : ""),
        );
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to simulate execution");
    } finally {
      setBusyId(null);
    }
  };

  const simulateProviderPayloadAction = async (
    intent: ExternalIntegrationIntent,
  ) => {
    setBusyId(intent.id);
    setError(null);
    try {
      // Resolve the connected credential + build the provider-ready payload
      // preview. Sends/creates NOTHING — execution stays disabled by governance.
      const r = await simulateProviderPayload(intent.id);
      setCredSim(r);
      setToast(
        r.payload_ready
          ? "Provider payload simulated — execution remains disabled."
          : `Simulated — payload not ready (${r.payload_errors.length} issue(s)).`,
      );
      await load();
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to simulate provider payload",
      );
    } finally {
      setBusyId(null);
    }
  };

  const attemptExecute = async (intent: ExternalIntegrationIntent) => {
    setBusyId(intent.id);
    setError(null);
    try {
      await executeIntent(intent.id);
      // Unreachable while the kill switch is engaged; the call returns 403.
      setToast("Execution returned unexpectedly.");
      await load();
    } catch (err) {
      const m = err instanceof Error ? err.message : "Execution blocked";
      // Surface the guard's message (strip any leading "403:" prefix).
      setError(m.replace(/^\d{3}:\s*/, ""));
    } finally {
      setBusyId(null);
    }
  };

  const viewSource = (intent: ExternalIntegrationIntent) => {
    if (!onNavigate) return;
    if (intent.source_type === "communication_draft")
      onNavigate("agents", "signal-drafts");
    else if (intent.source_type === "schedule_proposal")
      onNavigate("agents", "chronos-proposals");
  };

  const viewProviderConnector = () => {
    if (onNavigate) onNavigate("tools", "provider-connectors");
  };

  return (
    <section className="admin__section">
      <div className="admin__section-head">
        <h2>Integration Readiness Queue</h2>
        <button className="btn btn--ghost btn--sm" onClick={load} disabled={loading}>
          ↻ Refresh
        </button>
      </div>

      {execStatus && !execStatus.execution_available && (
        <div
          className="admin__error"
          style={{
            background: "var(--surface-2, #1b1b27)",
            color: "var(--text)",
            borderLeft: "3px solid var(--danger, #ef4444)",
          }}
        >
          🔒 {execStatus.message}
          <div className="muted" style={{ fontSize: "12px", marginTop: "4px" }}>
            <code>EXTERNAL_EXECUTION_ENABLED=false</code> · <code>dry_run=true</code>{" "}
            · execution unavailable
          </div>
        </div>
      )}

      <div
        className="admin__error"
        style={{
          background: "var(--surface-2, #1b1b27)",
          color: "var(--text)",
          borderLeft: "3px solid var(--accent, #8b5cf6)",
        }}
      >
        External execution is disabled. These intents are readiness records only.
      </div>
      <div
        className="admin__error"
        style={{
          background: "var(--surface-2, #1b1b27)",
          color: "var(--text)",
          borderLeft: "3px solid var(--warning, #f59e0b)",
        }}
      >
        Confirmation does not execute this action. External execution is still disabled.
      </div>

      <p className="admin__hint">
        Internal records representing a <strong>future</strong> provider action
        from an approved draft/proposal. Cora performs no external action — no
        email is sent and no calendar event is created. Simulating readiness
        keeps the intent dry-run and never calls a provider.
        {isAdmin ? " Admin view: all users' intents." : ""}
      </p>

      {error && <div className="admin__error">{error}</div>}
      {toast && <div className="admin__toast">{toast}</div>}

      {loading && intents.length === 0 ? (
        <div className="admin__hint">Loading intents…</div>
      ) : intents.length === 0 ? (
        <div className="admin__hint">No integration intents yet.</div>
      ) : (
        <table className="admin__table">
          <thead>
            <tr>
              <th>Intent</th>
              <th>Source record</th>
              <th>Provider</th>
              <th>Status</th>
              <th>Dry-run</th>
              <th>Confirmation</th>
              <th>Validation</th>
              <th>Updated</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {intents.map((it) => (
              <tr key={it.id}>
                <td>{intentLabel(it)}</td>
                <td>
                  <div>{sourceTitle(it)}</div>
                  <div className="muted" style={{ fontSize: "11px" }}>
                    {SOURCE_LABEL[it.source_type] || it.source_type}
                  </div>
                </td>
                <td className="muted">
                  {it.provider_type}
                  <div style={{ fontSize: "11px" }}>{it.provider_name}</div>
                </td>
                <td>{statusChip(it.status)}</td>
                <td className="muted">{it.dry_run ? "yes" : "no"}</td>
                <td className="muted" style={{ fontSize: "12px" }}>
                  {it.status === "confirmed"
                    ? `confirmed ${fmt(it.confirmed_at)}`
                    : it.status === "confirmation_revoked"
                      ? "revoked"
                      : it.requires_confirmation
                        ? "required"
                        : "—"}
                </td>
                <td className="muted" style={{ fontSize: "12px" }}>
                  {validationSummary(it.validation_result)}
                </td>
                <td className="muted">{fmt(it.updated_at)}</td>
                <td>
                  <div className="admin__row-actions">
                    <button
                      className="btn btn--ghost btn--sm"
                      onClick={() => simulate(it)}
                      disabled={busyId === it.id || it.status === "cancelled"}
                      title="Simulate readiness and record the result (no provider call)"
                    >
                      {busyId === it.id ? "…" : "Simulate Readiness"}
                    </button>
                    <button
                      className="btn btn--ghost btn--sm"
                      onClick={() => openDetail(it)}
                      title="View full intent details"
                    >
                      Details
                    </button>
                    {it.status === "confirmed" ? (
                      <button
                        className="btn btn--ghost btn--sm"
                        onClick={() => revoke(it)}
                        disabled={busyId === it.id}
                        title="Revoke this confirmation"
                      >
                        {busyId === it.id ? "…" : "Revoke Confirmation"}
                      </button>
                    ) : (
                      <button
                        className="btn btn--ghost btn--sm"
                        onClick={() => confirm(it)}
                        disabled={busyId === it.id || !confirmable(it)}
                        title={
                          confirmable(it)
                            ? "Confirm for future execution (does not execute)"
                            : "Resolve readiness blockers before confirming"
                        }
                      >
                        {busyId === it.id ? "…" : "Confirm Intent"}
                      </button>
                    )}
                    {it.status === "confirmed" && (
                      <button
                        className="btn btn--ghost btn--sm"
                        onClick={() => simulateExecution(it)}
                        disabled={busyId === it.id}
                        title="Dry-run the provider adapter — no real provider call"
                      >
                        {busyId === it.id ? "…" : "Simulate Execution"}
                      </button>
                    )}
                    {it.status === "confirmed" && (
                      <button
                        className="btn btn--ghost btn--sm"
                        onClick={() => attemptExecute(it)}
                        disabled={busyId === it.id}
                        title="Execution is blocked by the global safety guard"
                      >
                        {busyId === it.id ? "…" : "Execute"}
                      </button>
                    )}
                    {it.status !== "cancelled" && (
                      <button
                        className="btn btn--ghost btn--sm"
                        onClick={() => simulateProviderPayloadAction(it)}
                        disabled={busyId === it.id}
                        title="Resolve the connected credential + preview the provider-ready payload. Sends/creates nothing; execution stays disabled."
                      >
                        {busyId === it.id ? "…" : "Simulate Provider Payload"}
                      </button>
                    )}
                    {it.status === "cancelled" ? (
                      <span className="muted" style={{ fontSize: "12px" }}>
                        cancelled
                      </span>
                    ) : (
                      <button
                        className="btn btn--ghost btn--sm btn--danger"
                        onClick={() => cancel(it)}
                        disabled={busyId === it.id}
                        title="Cancel this integration intent (does not delete it)"
                      >
                        {busyId === it.id ? "Cancelling…" : "Cancel"}
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {detail && (
        <div className="modal-backdrop" onClick={() => setDetail(null)}>
          <div
            className="modal"
            style={{ maxWidth: "680px" }}
            role="dialog"
            aria-modal="true"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal__header">
              <h3 className="modal__title">
                {intentLabel(detail)} · {STATUS_LABEL[detail.status] ?? detail.status}
              </h3>
            </div>
            <div className="modal__body" style={{ whiteSpace: "normal" }}>
              <table className="admin__table" style={{ marginTop: 0 }}>
                <tbody>
                  {(
                    [
                      ["ID", detail.id],
                      ["Source type", SOURCE_LABEL[detail.source_type] || detail.source_type],
                      ["Source ID", detail.source_id],
                      ["Provider type", detail.provider_type],
                      ["Provider name", detail.provider_name],
                      ["Action type", detail.action_type],
                      ["Status", STATUS_LABEL[detail.status] ?? detail.status],
                      ["Dry-run", detail.dry_run ? "yes" : "no"],
                      ["Requires confirmation", detail.requires_confirmation ? "yes" : "no"],
                      ["Confirmation reason", detail.confirmation_required_reason || "—"],
                      ["Confirmed by", detail.confirmed_by || "—"],
                      ["Confirmed at", fmt(detail.confirmed_at)],
                      ["Cancelled by", detail.cancelled_by || "—"],
                      ["Cancelled at", fmt(detail.cancelled_at)],
                      ["Created at", fmt(detail.created_at)],
                      ["Updated at", fmt(detail.updated_at)],
                    ] as Array<[string, string]>
                  ).map(([k, v]) => (
                    <tr key={k}>
                      <td style={{ fontWeight: 600, width: "38%" }}>{k}</td>
                      <td className="muted">{v}</td>
                    </tr>
                  ))}
                </tbody>
              </table>

              <h4 style={{ margin: "14px 0 0" }}>Payload preview</h4>
              <Json value={detail.payload_preview} />

              <h4 style={{ margin: "14px 0 0" }}>Validation result</h4>
              <Json value={detail.validation_result} />

              <h4 style={{ margin: "14px 0 0" }}>Metadata</h4>
              <Json value={detail.metadata} />

              <h4 style={{ margin: "14px 0 0" }}>Provider readiness</h4>
              {readinessLoading ? (
                <p className="muted" style={{ margin: "4px 0 0" }}>Loading readiness…</p>
              ) : readiness ? (
                <>
                  <p style={{ margin: "4px 0" }}>
                    <strong>Ready:</strong>{" "}
                    {readiness.ready_for_execution ? "yes" : "no"} ·{" "}
                    <strong>Provider:</strong>{" "}
                    {readiness.required_provider_name || "—"} (
                    {readiness.required_provider_type}) ·{" "}
                    <strong>connector</strong> {readiness.connector_status}
                  </p>
                  <p style={{ margin: "4px 0" }}>
                    <strong>Missing scopes:</strong>{" "}
                    {readiness.missing_scopes.length
                      ? readiness.missing_scopes.join(", ")
                      : "none"}
                  </p>
                  <div style={{ margin: "4px 0" }}>
                    <strong>Blockers:</strong>
                    {readiness.blockers.length ? (
                      <ul style={{ margin: "4px 0 0", paddingLeft: "20px" }}>
                        {readiness.blockers.map((b, i) => (
                          <li key={i}>{b}</li>
                        ))}
                      </ul>
                    ) : (
                      " none"
                    )}
                  </div>
                  <p style={{ margin: "4px 0" }}>
                    <strong>Recommended next step:</strong>{" "}
                    {readiness.recommended_next_step}
                  </p>
                </>
              ) : (
                <p className="muted" style={{ margin: "4px 0 0" }}>
                  No readiness data — run Simulate Readiness.
                </p>
              )}

              <div
                className="admin__row-actions"
                style={{ justifyContent: "flex-end", marginTop: "16px", flexWrap: "wrap" }}
              >
                <button
                  className="btn btn--ghost btn--sm"
                  onClick={() => simulate(detail)}
                  disabled={busyId === detail.id || detail.status === "cancelled"}
                >
                  Simulate Readiness
                </button>
                {detail.status === "confirmed" ? (
                  <button
                    className="btn btn--ghost btn--sm"
                    onClick={() => revoke(detail)}
                    disabled={busyId === detail.id}
                  >
                    Revoke Confirmation
                  </button>
                ) : (
                  <button
                    className="btn btn--ghost btn--sm"
                    onClick={() => confirm(detail)}
                    disabled={busyId === detail.id || !confirmable(detail)}
                    title={
                      confirmable(detail)
                        ? "Confirm for future execution (does not execute)"
                        : "Resolve readiness blockers before confirming"
                    }
                  >
                    Confirm Intent
                  </button>
                )}
                {onNavigate && (
                  <>
                    <button className="btn btn--ghost btn--sm" onClick={() => viewSource(detail)}>
                      View Source{" "}
                      {detail.source_type === "schedule_proposal" ? "Proposal" : "Draft"}
                    </button>
                    <button className="btn btn--ghost btn--sm" onClick={viewProviderConnector}>
                      View Provider Connector
                    </button>
                  </>
                )}
                {detail.status !== "cancelled" && (
                  <button
                    className="btn btn--ghost btn--sm btn--danger"
                    onClick={() => cancel(detail)}
                    disabled={busyId === detail.id}
                  >
                    Cancel Intent
                  </button>
                )}
                <button className="btn btn--primary btn--sm" onClick={() => setDetail(null)}>
                  Close
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {credSim && (
        <div className="modal-backdrop" onClick={() => setCredSim(null)}>
          <div
            className="modal"
            style={{ maxWidth: "680px" }}
            role="dialog"
            aria-modal="true"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal__header">
              <h3 className="modal__title">
                Provider Payload Simulation · {credSim.provider_name ?? credSim.provider_type}
              </h3>
            </div>
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
              🔒 <strong>Preview only. Provider execution remains disabled by
              governance — nothing was sent or created.</strong>
            </div>
            <table className="admin__table">
              <tbody>
                {(
                  [
                    ["Provider connected", credSim.validation.provider_connected],
                    ["Token valid or refreshable", credSim.validation.token_valid_or_refreshable],
                    ["Required scopes present", credSim.validation.required_scopes_present],
                    ["Provider execution disabled", credSim.validation.provider_execution_disabled],
                    ["Dry-run only", credSim.validation.dry_run_only],
                    ["Kill switch blocks execution", credSim.validation.kill_switch_blocks_execution],
                  ] as [string, boolean][]
                ).map(([label, ok]) => (
                  <tr key={label}>
                    <td>{label}</td>
                    <td>{ok ? "✓ yes" : "✗ no"}</td>
                  </tr>
                ))}
                {credSim.validation.missing_scopes.length > 0 && (
                  <tr>
                    <td>Missing scopes</td>
                    <td className="muted">{credSim.validation.missing_scopes.join(", ")}</td>
                  </tr>
                )}
                <tr>
                  <td>Execution allowed</td>
                  <td>{credSim.execution_allowed ? "yes" : "no (blocked)"}</td>
                </tr>
              </tbody>
            </table>
            <h4 className="admin__vt-h3">Provider-ready payload preview</h4>
            {credSim.payload_errors.length > 0 && (
              <div className="admin__error">{credSim.payload_errors.join("; ")}</div>
            )}
            <pre
              style={{
                background: "var(--surface-2, #16161e)",
                padding: "10px",
                borderRadius: 6,
                overflowX: "auto",
                fontSize: "12px",
              }}
            >
              {JSON.stringify(credSim.provider_payload_preview, null, 2)}
            </pre>
            <p className="admin__hint">{credSim.note}</p>
            <div className="modal__footer">
              <button className="btn btn--ghost btn--sm" onClick={() => setCredSim(null)}>
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
