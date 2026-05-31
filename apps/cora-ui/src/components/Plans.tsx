import { useCallback, useEffect, useState } from "react";
import {
  adminListAgents,
  adminListTools,
  cancelPlan,
  completePlan,
  completePlanStep,
  failPlanStep,
  getPlan,
  listPlanDelegations,
  listPlans,
  patchPlan,
  patchPlanStep,
  queuePlanStep,
} from "../api";
import type {
  Agent,
  AgentDelegation,
  ExecutionPlan,
  ExecutionPlanDetail,
  ExecutionPlanStep,
  PlanStepStatus,
  ToolAdminRow,
} from "../types";
import { delegationStatusVariant } from "./Delegations";

const STEP_STATUSES: PlanStepStatus[] = [
  "pending",
  "running",
  "completed",
  "failed",
  "skipped",
];

const STEP_TERMINAL: PlanStepStatus[] = ["completed", "failed", "skipped"];
const PLAN_TERMINAL = ["completed", "cancelled", "failed"];

export function Plans() {
  const [plans, setPlans] = useState<ExecutionPlan[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [tools, setTools] = useState<ToolAdminRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ExecutionPlanDetail | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [p, a, t] = await Promise.all([
        listPlans(100),
        adminListAgents().catch(() => [] as Agent[]),
        adminListTools().catch(() => [] as ToolAdminRow[]),
      ]);
      setPlans(p);
      setAgents(a);
      setTools(t);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setLoading(false);
    }
  }, []);

  const refreshDetail = useCallback(async (id: string) => {
    setLoadingDetail(true);
    try {
      setDetail(await getPlan(id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load plan");
      setDetail(null);
    } finally {
      setLoadingDetail(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (selectedId) refreshDetail(selectedId);
    else setDetail(null);
  }, [selectedId, refreshDetail]);

  // Live updates: re-pull the open plan + plan list every 10s while visible.
  useEffect(() => {
    const interval = window.setInterval(() => {
      refresh();
      if (selectedId) refreshDetail(selectedId);
    }, 10_000);
    return () => window.clearInterval(interval);
  }, [refresh, refreshDetail, selectedId]);

  return (
    <main className="admin">
      <header className="admin__header">
        <h1>Execution Plans</h1>
        <p className="admin__subtitle">
          ATLAS-authored multi-step plans. Manage steps, mark progress, cancel
          or complete. No auto-execution yet.
        </p>
      </header>

      {error && <div className="admin__error">{error}</div>}

      <section className="admin__section">
        <div className="admin__section-head">
          <h2>Plans</h2>
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
              <th>When</th>
              <th>Title</th>
              <th>Agent</th>
              <th>Status</th>
              <th>Progress</th>
              <th>Session</th>
            </tr>
          </thead>
          <tbody>
            {plans.map((p) => {
              const pct = p.total_steps
                ? Math.round((p.current_step / p.total_steps) * 100)
                : 0;
              return (
                <tr
                  key={p.id}
                  className={`trace-row${selectedId === p.id ? " admin__row--selected" : ""}`}
                  onClick={() => setSelectedId(p.id)}
                >
                  <td className="muted">
                    {new Date(p.created_at).toLocaleString()}
                  </td>
                  <td>{p.title}</td>
                  <td className="mono">{p.selected_agent ?? "—"}</td>
                  <td>
                    <span className={`status-chip status-chip--${planStatusVariant(p.status)}`}>
                      {p.status}
                    </span>
                  </td>
                  <td className="muted">
                    <div className="plan-progress">
                      <div className="plan-progress__bar">
                        <div
                          className="plan-progress__fill"
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                      <span className="plan-progress__label">
                        {p.current_step}/{p.total_steps}
                      </span>
                    </div>
                  </td>
                  <td className="mono muted">
                    {p.session_id ? p.session_id.slice(0, 8) : "—"}
                  </td>
                </tr>
              );
            })}
            {!loading && plans.length === 0 && (
              <tr>
                <td colSpan={6} className="muted">
                  No plans yet. Ask Cora to "plan something" in chat.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      {detail && (
        <PlanDetailSection
          plan={detail}
          loading={loadingDetail}
          agents={agents}
          tools={tools}
          onClose={() => setSelectedId(null)}
          onChanged={() => {
            if (selectedId) refreshDetail(selectedId);
            refresh();
          }}
          setError={setError}
        />
      )}
    </main>
  );
}

function PlanDetailSection({
  plan,
  loading,
  agents,
  tools,
  onClose,
  onChanged,
  setError,
}: {
  plan: ExecutionPlanDetail;
  loading: boolean;
  agents: Agent[];
  tools: ToolAdminRow[];
  onClose: () => void;
  onChanged: () => void;
  setError: (s: string | null) => void;
}) {
  const planIsTerminal = PLAN_TERMINAL.includes(plan.status);
  const [editingMeta, setEditingMeta] = useState(false);
  const [title, setTitle] = useState(plan.title);
  const [goal, setGoal] = useState(plan.goal);
  const [savingMeta, setSavingMeta] = useState(false);
  const [planActing, setPlanActing] = useState(false);

  useEffect(() => {
    setTitle(plan.title);
    setGoal(plan.goal);
    setEditingMeta(false);
  }, [plan.id]);

  const saveMeta = async () => {
    if (savingMeta) return;
    setSavingMeta(true);
    setError(null);
    try {
      await patchPlan(plan.id, { title, goal });
      setEditingMeta(false);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setSavingMeta(false);
    }
  };

  const cancel = async () => {
    if (planActing) return;
    setPlanActing(true);
    setError(null);
    try {
      await cancelPlan(plan.id);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setPlanActing(false);
    }
  };

  const complete = async () => {
    if (planActing) return;
    setPlanActing(true);
    setError(null);
    try {
      await completePlan(plan.id);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setPlanActing(false);
    }
  };

  const pct = plan.total_steps
    ? Math.round((plan.current_step / plan.total_steps) * 100)
    : 0;

  return (
    <section className="admin__section">
      <div className="admin__section-head">
        <h2>
          {plan.title}{" "}
          <span className="mono muted">{plan.id.slice(0, 8)}</span>
        </h2>
        <div className="admin__inline">
          {loading && <span className="admin__hint">Loading…</span>}
          <button className="btn btn--ghost btn--sm" onClick={onClose}>
            Close
          </button>
        </div>
      </div>

      <div className="admin__hint">
        <span className={`status-chip status-chip--${planStatusVariant(plan.status)}`}>
          {plan.status}
        </span>{" "}
        · agent: <span className="mono">{plan.selected_agent ?? "—"}</span> ·{" "}
        created {new Date(plan.created_at).toLocaleString()}
      </div>

      <div className="plan-progress plan-progress--wide">
        <div className="plan-progress__bar">
          <div
            className="plan-progress__fill"
            style={{ width: `${pct}%` }}
          />
        </div>
        <span className="plan-progress__label">
          {plan.current_step} of {plan.total_steps} steps · {pct}%
        </span>
      </div>

      {editingMeta ? (
        <form
          className="admin__form"
          onSubmit={(e) => {
            e.preventDefault();
            saveMeta();
          }}
        >
          <label className="admin__field-wide">
            <span>Title</span>
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              disabled={savingMeta}
            />
          </label>
          <label className="admin__field-wide">
            <span>Goal</span>
            <textarea
              rows={2}
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              disabled={savingMeta}
            />
          </label>
          <div className="admin__form-row">
            <button
              type="submit"
              className="btn btn--primary"
              disabled={savingMeta}
            >
              {savingMeta ? "…" : "Save"}
            </button>
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => {
                setTitle(plan.title);
                setGoal(plan.goal);
                setEditingMeta(false);
              }}
              disabled={savingMeta}
            >
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <div className="admin__hint">
          <strong>Goal:</strong> {plan.goal}{" "}
          {!planIsTerminal && (
            <button
              className="btn btn--ghost btn--sm"
              onClick={() => setEditingMeta(true)}
            >
              Edit
            </button>
          )}
        </div>
      )}

      <div className="admin__form-row" style={{ marginTop: "8px" }}>
        {!planIsTerminal && (
          <>
            <button
              className="btn btn--primary"
              onClick={complete}
              disabled={planActing}
            >
              Complete plan
            </button>
            <button
              className="btn btn--ghost"
              onClick={cancel}
              disabled={planActing}
            >
              Cancel plan
            </button>
          </>
        )}
      </div>

      <h3 className="admin__vt-h3">Steps</h3>
      <ul className="admin__preview-list">
        {plan.steps.map((s) => (
          <StepRow
            key={s.id}
            step={s}
            planId={plan.id}
            planIsTerminal={planIsTerminal}
            agents={agents}
            tools={tools}
            onChanged={onChanged}
            setError={setError}
          />
        ))}
      </ul>

      <DelegationTimeline planId={plan.id} setError={setError} />
    </section>
  );
}

function DelegationTimeline({
  planId,
  setError,
}: {
  planId: string;
  setError: (s: string | null) => void;
}) {
  const [rows, setRows] = useState<AgentDelegation[]>([]);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setRows(await listPlanDelegations(planId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load delegations");
    } finally {
      setLoading(false);
    }
  }, [planId, setError]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Live refresh alongside the rest of the plan detail.
  useEffect(() => {
    const interval = window.setInterval(refresh, 10_000);
    return () => window.clearInterval(interval);
  }, [refresh]);

  return (
    <>
      <h3 className="admin__vt-h3">
        Delegations{" "}
        <span className="muted">
          ({rows.length}
          {loading ? " · loading…" : ""})
        </span>
      </h3>
      {rows.length === 0 && !loading ? (
        <p className="muted">No delegations recorded for this plan.</p>
      ) : (
        <ul className="admin__preview-list">
          {rows
            .slice()
            .sort(
              (a, b) =>
                new Date(a.created_at).getTime() -
                new Date(b.created_at).getTime(),
            )
            .map((d) => (
              <li key={d.id}>
                <div className="admin__preview-row">
                  <span className="mono">
                    {d.from_agent} <span className="muted">→</span> {d.to_agent}
                  </span>
                  <span
                    className={`status-chip status-chip--${delegationStatusVariant(d.status)}`}
                  >
                    {d.status}
                  </span>
                  <span className="muted">
                    {new Date(d.created_at).toLocaleString()}
                  </span>
                </div>
                {d.delegation_reason && (
                  <div className="admin__preview-content">
                    {d.delegation_reason}
                  </div>
                )}
              </li>
            ))}
        </ul>
      )}
    </>
  );
}

function StepRow({
  step,
  planId,
  planIsTerminal,
  agents,
  tools,
  onChanged,
  setError,
}: {
  step: ExecutionPlanStep;
  planId: string;
  planIsTerminal: boolean;
  agents: Agent[];
  tools: ToolAdminRow[];
  onChanged: () => void;
  setError: (s: string | null) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(step.title);
  const [description, setDescription] = useState(step.description ?? "");
  const [agent, setAgent] = useState(step.assigned_agent ?? "");
  const [toolName, setToolName] = useState(step.tool_name ?? "");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setTitle(step.title);
    setDescription(step.description ?? "");
    setAgent(step.assigned_agent ?? "");
    setToolName(step.tool_name ?? "");
    setEditing(false);
  }, [step.id]);

  const stepIsTerminal = STEP_TERMINAL.includes(step.status);
  const canMutate = !planIsTerminal;

  const saveEdits = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await patchPlanStep(planId, step.id, {
        title,
        description,
        assigned_agent: agent.trim() || null,
        tool_name: toolName.trim() || null,
      });
      setEditing(false);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setBusy(false);
    }
  };

  const changeStatus = async (status: PlanStepStatus) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await patchPlanStep(planId, step.id, { status });
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setBusy(false);
    }
  };

  const markComplete = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await completePlanStep(planId, step.id);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setBusy(false);
    }
  };

  const markFailed = async () => {
    if (busy) return;
    const note = window.prompt("Failure note (optional):") ?? undefined;
    setBusy(true);
    setError(null);
    try {
      await failPlanStep(planId, step.id, note?.trim() || undefined);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setBusy(false);
    }
  };

  const queue = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await queuePlanStep(planId, step.id);
      window.alert(`Queued job ${res.job_id.slice(0, 8)} (status ${res.status}).`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <li>
      <div className="admin__preview-row">
        <span className="mono muted">#{step.step_number}</span>
        <strong>{step.title}</strong>
        <span className={`status-chip status-chip--${stepStatusVariant(step.status)}`}>
          {step.status}
        </span>
      </div>

      {editing ? (
        <div className="step-edit">
          <div className="step-edit__row">
            <label className="step-edit__field">
              <span>Title</span>
              <input
                className="cora-input"
                type="text"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                disabled={busy}
              />
            </label>
          </div>
          <div className="step-edit__row">
            <label className="step-edit__field">
              <span>Assigned agent</span>
              <select
                className="cora-input"
                value={agent}
                onChange={(e) => setAgent(e.target.value)}
                disabled={busy}
              >
                <option value="">Unassigned</option>
                {agents.map((a) => (
                  <option key={a.id} value={a.name}>
                    {a.display_name || a.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="step-edit__field">
              <span>Tool</span>
              <select
                className="cora-input"
                value={toolName}
                onChange={(e) => setToolName(e.target.value)}
                disabled={busy}
              >
                <option value="">No tool</option>
                {tools.map((t) => (
                  <option key={t.id} value={t.name}>
                    {t.name}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <label className="step-edit__field step-edit__field--wide">
            <span>Description</span>
            <textarea
              className="cora-input"
              rows={3}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              disabled={busy}
            />
          </label>
          <div className="step-edit__actions">
            <button
              type="button"
              className="btn btn--primary btn--sm"
              onClick={saveEdits}
              disabled={busy}
            >
              {busy ? "…" : "Save"}
            </button>
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              onClick={() => setEditing(false)}
              disabled={busy}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <>
          {step.description && (
            <div className="admin__preview-content">{step.description}</div>
          )}
          <div className="admin__preview-row" style={{ gap: "6px" }}>
            <span className="mono muted">
              agent: {step.assigned_agent ?? "—"}
            </span>
            {step.tool_name && (
              <span className="scope-chip scope-chip--mcp_action">
                {step.tool_name}
              </span>
            )}
          </div>
          {canMutate && (
            <div className="step-actions">
              <label className="step-actions__field">
                <span>Status</span>
                <select
                  className="cora-input cora-input--compact"
                  value={step.status}
                  onChange={(e) =>
                    changeStatus(e.target.value as PlanStepStatus)
                  }
                  disabled={busy || stepIsTerminal}
                >
                  {STEP_STATUSES.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              </label>
              {!stepIsTerminal && (
                <>
                  <button
                    className="btn btn--primary btn--sm"
                    onClick={markComplete}
                    disabled={busy}
                  >
                    Complete
                  </button>
                  <button
                    className="btn btn--ghost btn--sm"
                    onClick={markFailed}
                    disabled={busy}
                  >
                    Fail
                  </button>
                  <button
                    className="btn btn--ghost btn--sm"
                    onClick={queue}
                    disabled={busy}
                    title="Queue this step as a background job"
                  >
                    Queue
                  </button>
                </>
              )}
              <button
                className="btn btn--ghost btn--sm"
                onClick={() => setEditing(true)}
                disabled={busy}
              >
                Edit
              </button>
            </div>
          )}
        </>
      )}
    </li>
  );
}

function planStatusVariant(s: string): string {
  if (s === "completed") return "active";
  if (s === "failed" || s === "cancelled") return "archived";
  return "draft";
}

function stepStatusVariant(s: string): string {
  if (s === "completed") return "active";
  if (s === "failed" || s === "skipped") return "archived";
  return "draft";
}
