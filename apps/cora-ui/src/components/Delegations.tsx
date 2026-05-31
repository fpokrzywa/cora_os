import { useCallback, useEffect, useState } from "react";
import { getDelegation, listDelegations } from "../api";
import type { AgentDelegation } from "../types";

const STATUSES = ["", "pending", "running", "completed", "failed"];

export function Delegations() {
  const [rows, setRows] = useState<AgentDelegation[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [sessionFilter, setSessionFilter] = useState("");
  const [planFilter, setPlanFilter] = useState("");
  const [selected, setSelected] = useState<AgentDelegation | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setRows(
        await listDelegations({
          limit: 200,
          status: statusFilter || undefined,
          session_id: sessionFilter.trim() || undefined,
          plan_id: planFilter.trim() || undefined,
        }),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setLoading(false);
    }
  }, [statusFilter, sessionFilter, planFilter]);

  useEffect(() => {
    const handle = window.setTimeout(refresh, 250);
    return () => window.clearTimeout(handle);
  }, [refresh]);

  // Live updates every 10s.
  useEffect(() => {
    const interval = window.setInterval(refresh, 10_000);
    return () => window.clearInterval(interval);
  }, [refresh]);

  const open = useCallback(async (id: string) => {
    try {
      setSelected(await getDelegation(id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load delegation");
    }
  }, []);

  return (
    <main className="admin">
      <header className="admin__header">
        <h1>Agent Delegations</h1>
        <p className="admin__subtitle">
          One row per delegated subtask between agents. ATLAS orchestrates;
          depth capped at 3 active per session/plan. Self-delegation rejected.
        </p>
      </header>

      {error && <div className="admin__error">{error}</div>}

      <section className="admin__section">
        <div className="admin__section-head">
          <h2>Filters</h2>
          <button
            className="btn btn--ghost btn--sm"
            onClick={refresh}
            disabled={loading}
          >
            ↻
          </button>
        </div>
        <div className="admin__form">
          <div className="admin__form-row">
            <label>
              <span>Status</span>
              <select
                className="cora-input"
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value)}
              >
                {STATUSES.map((s) => (
                  <option key={s || "any"} value={s}>
                    {s || "any"}
                  </option>
                ))}
              </select>
            </label>
            <label className="admin__field-wide">
              <span>Session id</span>
              <input
                className="cora-input"
                type="text"
                value={sessionFilter}
                onChange={(e) => setSessionFilter(e.target.value)}
                placeholder="full UUID"
              />
            </label>
            <label className="admin__field-wide">
              <span>Plan id</span>
              <input
                className="cora-input"
                type="text"
                value={planFilter}
                onChange={(e) => setPlanFilter(e.target.value)}
                placeholder="full UUID"
              />
            </label>
          </div>
        </div>
      </section>

      <section className="admin__section">
        <table className="admin__table">
          <thead>
            <tr>
              <th>When</th>
              <th>From → To</th>
              <th>Reason</th>
              <th>Plan / Session</th>
              <th>Status</th>
              <th>Duration</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((d) => (
              <DelegationRow key={d.id} d={d} onClick={() => open(d.id)} />
            ))}
            {!loading && rows.length === 0 && (
              <tr>
                <td colSpan={6} className="muted">
                  No delegations match.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      {selected && (
        <DelegationDetail
          d={selected}
          onClose={() => setSelected(null)}
        />
      )}
    </main>
  );
}

function DelegationRow({
  d,
  onClick,
}: {
  d: AgentDelegation;
  onClick: () => void;
}) {
  const duration =
    d.completed_at
      ? `${Math.max(
          0,
          Math.round(
            (new Date(d.completed_at).getTime() -
              new Date(d.created_at).getTime()) /
              10,
          ) / 100,
        )}s`
      : "—";
  return (
    <tr className="trace-row" onClick={onClick}>
      <td className="muted">{new Date(d.created_at).toLocaleString()}</td>
      <td className="mono">
        {d.from_agent} <span className="muted">→</span> {d.to_agent}
      </td>
      <td className="muted">
        {d.delegation_reason
          ? d.delegation_reason.slice(0, 80) +
            (d.delegation_reason.length > 80 ? "…" : "")
          : "—"}
      </td>
      <td className="mono muted">
        {d.execution_plan_id ? `plan ${d.execution_plan_id.slice(0, 8)}` : ""}
        {d.execution_plan_id && d.session_id ? " · " : ""}
        {d.session_id ? `sess ${d.session_id.slice(0, 8)}` : ""}
        {!d.execution_plan_id && !d.session_id ? "—" : ""}
      </td>
      <td>
        <span
          className={`status-chip status-chip--${delegationStatusVariant(d.status)}`}
        >
          {d.status}
        </span>
      </td>
      <td className="muted">{duration}</td>
    </tr>
  );
}

function DelegationDetail({
  d,
  onClose,
}: {
  d: AgentDelegation;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div
      className="modal-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="modal" role="dialog" aria-modal="true">
        <header className="modal__header">
          <h2 className="modal__title">
            {d.from_agent} → {d.to_agent}{" "}
            <span className="mono muted">{d.id.slice(0, 8)}</span>
          </h2>
          <button className="modal__close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>
        <div className="modal__meta">
          <span
            className={`status-chip status-chip--${delegationStatusVariant(d.status)}`}
          >
            {d.status}
          </span>
          <span className="muted">
            created {new Date(d.created_at).toLocaleString()}
          </span>
          {d.completed_at && (
            <span className="muted">
              done {new Date(d.completed_at).toLocaleString()}
            </span>
          )}
        </div>
        <div className="modal__body">
          <pre className="trace-json">{JSON.stringify(d, null, 2)}</pre>
        </div>
      </div>
    </div>
  );
}

export function delegationStatusVariant(s: string): string {
  if (s === "completed") return "active";
  if (s === "failed") return "archived";
  return "draft";
}
