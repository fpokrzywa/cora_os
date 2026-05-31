import { useCallback, useEffect, useState } from "react";
import {
  listExecutionApprovals,
  getExecutionApproval,
  approveExecutionIntent,
  rejectExecutionIntent,
  cancelReadinessIntent,
} from "../api";
import type {
  ExecutionApprovalListItem,
  ExecutionApprovalView,
  ExecutionApprovalState,
} from "../types";
import type { AdminTab } from "./AdminConsole";

const STATE_LABEL: Record<ExecutionApprovalState, string> = {
  pending_review: "Pending Review",
  ready_for_approval: "Ready for Approval",
  approved_for_execution: "Approved (future)",
  rejected: "Rejected",
  blocked_by_governance: "Blocked by Governance",
  cancelled: "Cancelled",
};

const STATES: ExecutionApprovalState[] = [
  "pending_review",
  "ready_for_approval",
  "approved_for_execution",
  "rejected",
  "blocked_by_governance",
  "cancelled",
];

function check(ok: boolean) {
  return <span>{ok ? "✓ yes" : "✗ no"}</span>;
}

export function ExecutionApprovalConsole({
  isAdmin,
  onNavigate,
}: {
  isAdmin: boolean;
  onNavigate?: (tab: AdminTab, sub?: string) => void;
}) {
  const [items, setItems] = useState<ExecutionApprovalListItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [providerType, setProviderType] = useState("");
  const [stateFilter, setStateFilter] = useState("");
  const [detail, setDetail] = useState<ExecutionApprovalView | null>(null);
  const [comment, setComment] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const rows = await listExecutionApprovals({
        provider_type: providerType || undefined,
        status: stateFilter || undefined,
      });
      setItems(rows);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load approvals");
    } finally {
      setLoading(false);
    }
  }, [providerType, stateFilter]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 2500);
    return () => clearTimeout(t);
  }, [toast]);

  const openDetail = async (intentId: string) => {
    setBusyId(intentId);
    setError(null);
    try {
      const v = await getExecutionApproval(intentId);
      setDetail(v);
      setComment("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to open intent");
    } finally {
      setBusyId(null);
    }
  };

  const approve = async (intentId: string) => {
    setBusyId(intentId);
    setError(null);
    try {
      const v = await approveExecutionIntent(intentId, comment || undefined);
      setDetail(v);
      setToast("Approved for future execution — nothing was executed.");
      await load();
    } catch (err) {
      setError(
        (err instanceof Error ? err.message : "Failed to approve").replace(
          /^\d{3}:\s*/,
          "",
        ),
      );
    } finally {
      setBusyId(null);
    }
  };

  const reject = async (intentId: string) => {
    setBusyId(intentId);
    setError(null);
    try {
      const v = await rejectExecutionIntent(intentId, comment || undefined);
      setDetail(v);
      setToast("Rejected.");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to reject");
    } finally {
      setBusyId(null);
    }
  };

  const cancel = async (intentId: string) => {
    setBusyId(intentId);
    setError(null);
    try {
      await cancelReadinessIntent(intentId);
      setToast("Intent cancelled.");
      setDetail(null);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to cancel");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <section className="admin__section">
      <div className="admin__section-head">
        <h2>Human Approval Execution Console</h2>
        <button className="btn btn--ghost btn--sm" onClick={load} disabled={loading}>
          ↻ Refresh
        </button>
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
        🔒 <strong>Approve for Future Execution records an internal decision +
        audit evidence only.</strong> It never calls Gmail / Outlook / Google /
        Microsoft, never sends or creates anything, and never lifts the global
        kill switch — external execution stays disabled.
      </div>

      {error && <div className="admin__error">{error}</div>}
      {toast && <div className="admin__toast">{toast}</div>}

      <div className="admin__form-row" style={{ gap: "8px", alignItems: "flex-end" }}>
        <label>
          <span>Provider type</span>
          <select
            className="cora-input"
            value={providerType}
            onChange={(e) => setProviderType(e.target.value)}
          >
            <option value="">All</option>
            <option value="email">email</option>
            <option value="calendar">calendar</option>
          </select>
        </label>
        <label>
          <span>Approval state</span>
          <select
            className="cora-input"
            value={stateFilter}
            onChange={(e) => setStateFilter(e.target.value)}
          >
            <option value="">All</option>
            {STATES.map((s) => (
              <option key={s} value={s}>
                {STATE_LABEL[s]}
              </option>
            ))}
          </select>
        </label>
      </div>

      {items.length === 0 ? (
        <div className="admin__hint">No integration intents awaiting review.</div>
      ) : (
        <table className="admin__table">
          <thead>
            <tr>
              <th>Source</th>
              <th>Provider</th>
              <th>Action</th>
              <th>Approval state</th>
              <th>Approvable</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.map((it) => (
              <tr key={it.intent_id}>
                <td className="muted">{it.source_type}</td>
                <td>
                  {it.provider_type}
                  {it.provider_name ? ` · ${it.provider_name}` : ""}
                </td>
                <td className="muted">{it.action_type}</td>
                <td>{STATE_LABEL[it.approval_state] ?? it.approval_state}</td>
                <td>{it.can_approve ? "yes" : "no"}</td>
                <td>
                  <button
                    className="btn btn--ghost btn--sm"
                    onClick={() => openDetail(it.intent_id)}
                    disabled={busyId === it.intent_id}
                  >
                    Review
                  </button>
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
            style={{ maxWidth: "720px" }}
            role="dialog"
            aria-modal="true"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal__header">
              <h3 className="modal__title">
                Review · {detail.provider_name ?? detail.provider_type} ·{" "}
                {STATE_LABEL[detail.approval_state] ?? detail.approval_state}
              </h3>
            </div>

            <h4 className="admin__vt-h3">Governance checklist</h4>
            <table className="admin__table">
              <tbody>
                <tr><td>Dry-run only</td><td>{check(detail.governance.dry_run_only)}</td></tr>
                <tr><td>Provider execution disabled</td><td>{check(detail.governance.provider_execution_disabled)}</td></tr>
                <tr><td>Kill switch blocks execution</td><td>{check(detail.governance.kill_switch_blocks_execution)}</td></tr>
                <tr><td>Execution enabled</td><td>{detail.execution_enabled ? "yes" : "no (disabled)"}</td></tr>
              </tbody>
            </table>

            {detail.feature_flag && (
              <>
                <h4 className="admin__vt-h3">Feature flag state</h4>
                <table className="admin__table">
                  <tbody>
                    <tr><td>Feature flag present</td><td>{check(detail.feature_flag.present)}</td></tr>
                    <tr><td>Execution enabled (flag)</td><td>{detail.feature_flag.enabled ? "yes" : "no"}</td></tr>
                    <tr><td>Dry-run only</td><td>{detail.feature_flag.dry_run_only ? "yes" : "no"}</td></tr>
                    <tr><td>Execution enabled (global)</td><td>{detail.feature_flag.execution_enabled ? "yes" : "no (disabled)"}</td></tr>
                    <tr><td>Approval required</td><td>{detail.feature_flag.requires_human_approval ? "yes" : "no"}</td></tr>
                    <tr><td>Interlock required</td><td>{detail.feature_flag.requires_final_interlock ? "yes" : "no"}</td></tr>
                  </tbody>
                </table>
              </>
            )}

            <h4 className="admin__vt-h3">Provider readiness checklist</h4>
            <table className="admin__table">
              <tbody>
                <tr><td>Provider connected</td><td>{check(detail.readiness.provider_connected)}</td></tr>
                <tr><td>Token valid or refreshable</td><td>{check(detail.readiness.token_valid_or_refreshable)}</td></tr>
                <tr><td>Required scopes present</td><td>{check(detail.readiness.required_scopes_present)}</td></tr>
                <tr><td>Source draft/proposal approved</td><td>{check(detail.readiness.source_approved)}</td></tr>
                <tr><td>Payload ready</td><td>{check(detail.readiness.payload_ready)}</td></tr>
                {detail.readiness.missing_scopes.length > 0 && (
                  <tr><td>Missing scopes</td><td className="muted">{detail.readiness.missing_scopes.join(", ")}</td></tr>
                )}
              </tbody>
            </table>

            <h4 className="admin__vt-h3">Provider-ready payload preview</h4>
            {detail.payload_errors.length > 0 && (
              <div className="admin__error">{detail.payload_errors.join("; ")}</div>
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
              {JSON.stringify(detail.provider_payload_preview, null, 2)}
            </pre>
            <p className="muted" style={{ fontSize: "12px" }}>
              payload hash: <code>{detail.payload_hash.slice(0, 16)}…</code> · ref:{" "}
              {detail.payload_preview_ref}
            </p>

            <label style={{ display: "block", marginTop: "8px" }}>
              <span>Comment / reason</span>
              <textarea
                className="cora-input"
                rows={2}
                value={comment}
                onChange={(e) => setComment(e.target.value)}
                placeholder="Optional reviewer note (stored with the decision)"
              />
            </label>

            <div className="modal__footer" style={{ gap: "8px", flexWrap: "wrap" }}>
              <button
                className="btn btn--primary btn--sm"
                onClick={() => approve(detail.intent_id)}
                disabled={
                  busyId === detail.intent_id || !detail.can_approve || !isAdmin
                }
                title={
                  detail.can_approve
                    ? "Record approval for future execution (executes nothing)"
                    : "Checklist not satisfied — cannot approve yet"
                }
              >
                Approve for Future Execution
              </button>
              <button
                className="btn btn--ghost btn--sm"
                onClick={() => reject(detail.intent_id)}
                disabled={busyId === detail.intent_id || !isAdmin}
              >
                Reject
              </button>
              <button
                className="btn btn--ghost btn--sm"
                onClick={() => cancel(detail.intent_id)}
                disabled={busyId === detail.intent_id}
              >
                Cancel
              </button>
              {onNavigate && (
                <button
                  className="btn btn--ghost btn--sm"
                  onClick={() => onNavigate("tools", "integration-queue")}
                >
                  Open in Integration Queue
                </button>
              )}
              <button className="btn btn--ghost btn--sm" onClick={() => setDetail(null)}>
                Close
              </button>
            </div>
            <p className="admin__hint">{detail.note}</p>
          </div>
        </div>
      )}
    </section>
  );
}
