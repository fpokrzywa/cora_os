import { useCallback, useEffect, useState } from "react";

import { clearScreenEntity, setScreenEntity } from "../screenContext";
import {
  listChronosProposals,
  createChronosProposal,
  updateChronosProposal,
  submitChronosProposalForReview,
  requestChronosProposalChanges,
  markChronosProposalReviewed,
  approveChronosProposal,
  archiveChronosProposal,
  deleteChronosProposal,
  listChronosProposalReviewEvents,
  createChronosProposalIntegrationIntent,
  createCalendarIntentFromProposal,
} from "../api";
import type {
  ScheduleProposal,
  ScheduleProposalStatus,
  ProposalReviewEvent,
} from "../types";
import { ReviewPanel, ApprovalMeta } from "./SignalDrafts";

const EMPTY = {
  proposal_type: "meeting",
  title: "",
  description: "",
  start_time: "",
  end_time: "",
  timezone: "",
  attendees: "",
  agenda: "",
  reminders: "",
};

const EDITABLE: ScheduleProposalStatus[] = ["proposed", "changes_requested"];

const PROPOSAL_STATUS_FILTERS: { value: string; label: string }[] = [
  { value: "all", label: "All statuses" },
  { value: "proposed", label: "Proposed" },
  { value: "in_review", label: "In review" },
  { value: "changes_requested", label: "Changes requested" },
  { value: "reviewed", label: "Reviewed" },
  { value: "approved", label: "Approved" },
  { value: "archived", label: "Archived" },
];

// JSONB list columns are edited as newline-separated text.
function toLines(arr: unknown[]): string {
  return arr.map((x) => (typeof x === "string" ? x : JSON.stringify(x))).join("\n");
}
function fromLines(text: string): string[] {
  return text
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
}
function toLocalInput(iso: string | null): string {
  return iso ? iso.slice(0, 16) : "";
}

function statusChip(s: ScheduleProposalStatus) {
  const cls =
    s === "approved" || s === "reviewed"
      ? "status-chip status-chip--active"
      : s === "archived"
        ? "status-chip status-chip--archived"
        : "status-chip status-chip--draft";
  return <span className={cls}>{s.replace("_", " ")}</span>;
}

export function ChronosProposals({
  workspaceId,
  isAdmin = false,
}: {
  workspaceId: string | null;
  isAdmin?: boolean;
}) {
  const [proposals, setProposals] = useState<ScheduleProposal[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [form, setForm] = useState({ ...EMPTY });
  const [editingId, setEditingId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [manageId, setManageId] = useState<string | null>(null);
  const [notes, setNotes] = useState("");
  const [events, setEvents] = useState<ProposalReviewEvent[] | null>(null);

  // Screen-context awareness: report the proposal being managed.
  useEffect(() => {
    if (manageId) {
      setScreenEntity({ type: "schedule_proposal", id: manageId });
    } else {
      clearScreenEntity();
    }
  }, [manageId]);
  const [busy, setBusy] = useState(false);
  const [intentMsg, setIntentMsg] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ScheduleProposal | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [viewTarget, setViewTarget] = useState<ScheduleProposal | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>("all");

  const load = useCallback(async () => {
    if (!workspaceId) return;
    setLoading(true);
    setError(null);
    try {
      setProposals(await listChronosProposals(workspaceId, includeArchived));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load proposals");
    } finally {
      setLoading(false);
    }
  }, [workspaceId, includeArchived]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 2500);
    return () => clearTimeout(t);
  }, [toast]);

  const confirmDelete = async () => {
    if (!workspaceId || !deleteTarget) return;
    const target = deleteTarget;
    setDeleting(true);
    setError(null);
    try {
      await deleteChronosProposal(workspaceId, target.id);
      // Remove from the list immediately, then refresh the queue.
      setProposals((ps) => ps.filter((x) => x.id !== target.id));
      if (manageId === target.id) setManageId(null);
      setDeleteTarget(null);
      setToast("Proposal deleted.");
      await load();
    } catch (err) {
      const msg =
        err instanceof Error ? err.message : "Failed to delete proposal";
      // Friendly message when the proposal is already gone.
      setError(
        msg.startsWith("404")
          ? "That proposal no longer exists — it may have already been deleted."
          : msg,
      );
      setDeleteTarget(null);
    } finally {
      setDeleting(false);
    }
  };

  const resetForm = () => {
    setForm({ ...EMPTY });
    setEditingId(null);
  };

  const submit = async () => {
    if (!workspaceId) return;
    if (!form.proposal_type.trim() || !form.title.trim()) {
      setError("Proposal type and title are required.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const payload = {
        proposal_type: form.proposal_type.trim(),
        title: form.title.trim(),
        description: form.description || null,
        start_time: form.start_time || null,
        end_time: form.end_time || null,
        timezone: form.timezone || null,
        attendees: fromLines(form.attendees),
        agenda: fromLines(form.agenda),
        reminders: fromLines(form.reminders),
      };
      if (editingId) {
        await updateChronosProposal(workspaceId, editingId, payload);
      } else {
        await createChronosProposal(workspaceId, payload);
      }
      resetForm();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save proposal");
    } finally {
      setSaving(false);
    }
  };

  const startEdit = (p: ScheduleProposal) => {
    setEditingId(p.id);
    setForm({
      proposal_type: p.proposal_type,
      title: p.title,
      description: p.description ?? "",
      start_time: toLocalInput(p.start_time),
      end_time: toLocalInput(p.end_time),
      timezone: p.timezone ?? "",
      attendees: toLines(p.attendees),
      agenda: toLines(p.agenda),
      reminders: toLines(p.reminders),
    });
  };

  const openManage = (p: ScheduleProposal) => {
    if (manageId === p.id) {
      setManageId(null);
      return;
    }
    setManageId(p.id);
    setNotes("");
    setEvents(null);
  };

  const runAction = async (
    p: ScheduleProposal,
    fn: (ws: string, id: string, notes?: string) => Promise<ScheduleProposal>,
  ) => {
    if (!workspaceId) return;
    setBusy(true);
    setError(null);
    try {
      await fn(workspaceId, p.id, notes.trim() || undefined);
      setNotes("");
      setEvents(null);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Action failed");
    } finally {
      setBusy(false);
    }
  };

  const loadEvents = async (p: ScheduleProposal) => {
    if (!workspaceId) return;
    try {
      setEvents(await listChronosProposalReviewEvents(workspaceId, p.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load history");
    }
  };

  const prepareIntent = async (p: ScheduleProposal) => {
    if (!workspaceId) return;
    setBusy(true);
    setError(null);
    setIntentMsg(null);
    try {
      const intent = await createChronosProposalIntegrationIntent(
        workspaceId,
        p.id,
        { provider_name: "internal_preview", action_type: "create_calendar_event" },
      );
      setIntentMsg(
        `Dry-run calendar intent prepared (status: ${intent.status}). No event is created. See Tools → Integration Readiness.`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to prepare intent");
    } finally {
      setBusy(false);
    }
  };

  const prepareForProvider = async (p: ScheduleProposal) => {
    setBusy(true);
    setError(null);
    setIntentMsg(null);
    try {
      const intent = await createCalendarIntentFromProposal(p.id);
      setToast("Integration intent created.");
      setIntentMsg(
        `Integration intent created (status: ${intent.status}). No calendar event is created — see Tools → Integration Queue.`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create integration intent");
    } finally {
      setBusy(false);
    }
  };

  const visibleProposals =
    statusFilter === "all"
      ? proposals
      : proposals.filter((p) => p.status === statusFilter);
  const managed = proposals.find((p) => p.id === manageId) || null;

  if (!workspaceId) {
    return (
      <section className="admin__section">
        <h2>CHRONOS Proposals</h2>
        <div className="admin__hint">
          Select a workspace to manage proposals.
        </div>
      </section>
    );
  }

  return (
    <section className="admin__section">
      <div className="admin__section-head">
        <h2>CHRONOS Proposals</h2>
        <button
          className="btn btn--ghost btn--sm"
          onClick={load}
          disabled={loading}
        >
          ↻ Refresh
        </button>
      </div>
      <p className="admin__hint">
        Review-only schedule proposals. CHRONOS never writes to a calendar —
        proposals move through proposed → in&nbsp;review → reviewed → approved.
        Approval is internal only; no calendar action is performed.
      </p>

      <div className="admin__form">
        <h3 className="admin__vt-h3">
          {editingId ? "Edit proposal" : "New proposal"}
        </h3>
        <div className="admin__form-row">
          <label>
            <span>Proposal type</span>
            <input
              className="cora-input"
              value={form.proposal_type}
              onChange={(e) =>
                setForm({ ...form, proposal_type: e.target.value })
              }
              placeholder="meeting, timeline, reminder…"
            />
          </label>
          <label>
            <span>Title</span>
            <input
              className="cora-input"
              value={form.title}
              onChange={(e) => setForm({ ...form, title: e.target.value })}
            />
          </label>
          <label>
            <span>Timezone</span>
            <input
              className="cora-input"
              value={form.timezone}
              onChange={(e) => setForm({ ...form, timezone: e.target.value })}
              placeholder="America/New_York"
            />
          </label>
        </div>
        <div className="admin__form-row">
          <label>
            <span>Start time</span>
            <input
              className="cora-input"
              type="datetime-local"
              value={form.start_time}
              onChange={(e) => setForm({ ...form, start_time: e.target.value })}
            />
          </label>
          <label>
            <span>End time</span>
            <input
              className="cora-input"
              type="datetime-local"
              value={form.end_time}
              onChange={(e) => setForm({ ...form, end_time: e.target.value })}
            />
          </label>
        </div>
        <label>
          <span>Description</span>
          <textarea
            className="cora-input"
            rows={3}
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
          />
        </label>
        <div className="admin__form-row">
          <label>
            <span>Attendees (one per line)</span>
            <textarea
              className="cora-input"
              rows={3}
              value={form.attendees}
              onChange={(e) => setForm({ ...form, attendees: e.target.value })}
            />
          </label>
          <label>
            <span>Agenda (one per line)</span>
            <textarea
              className="cora-input"
              rows={3}
              value={form.agenda}
              onChange={(e) => setForm({ ...form, agenda: e.target.value })}
            />
          </label>
          <label>
            <span>Reminders (one per line)</span>
            <textarea
              className="cora-input"
              rows={3}
              value={form.reminders}
              onChange={(e) => setForm({ ...form, reminders: e.target.value })}
            />
          </label>
        </div>
        <div className="admin__form-row">
          <button
            className="btn btn--primary"
            onClick={submit}
            disabled={saving}
          >
            {saving
              ? "Saving…"
              : editingId
                ? "Save changes"
                : "Create proposal"}
          </button>
          {editingId && (
            <button
              className="btn btn--ghost"
              onClick={resetForm}
              disabled={saving}
            >
              Cancel
            </button>
          )}
        </div>
      </div>

      {error && <div className="admin__error">{error}</div>}
      {intentMsg && <div className="admin__hint">{intentMsg}</div>}
      {toast && <div className="admin__toast">{toast}</div>}

      <div
        className="admin__form-row"
        style={{ marginTop: "12px", alignItems: "flex-end", gap: "12px" }}
      >
        <label>
          <span>Filter by status</span>
          <select
            className="cora-input"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
          >
            {PROPOSAL_STATUS_FILTERS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        <label className="admin__checkbox">
          <input
            type="checkbox"
            checked={includeArchived}
            onChange={(e) => setIncludeArchived(e.target.checked)}
          />
          <span>Show archived</span>
        </label>
      </div>

      {loading && proposals.length === 0 ? (
        <div className="admin__hint">Loading proposals…</div>
      ) : proposals.length === 0 ? (
        <div className="admin__hint">No proposals yet.</div>
      ) : visibleProposals.length === 0 ? (
        <div className="admin__hint">No proposals match this filter.</div>
      ) : (
        <table className="admin__table">
          <thead>
            <tr>
              <th>Title / Type</th>
              <th>When</th>
              <th>Status</th>
              <th>Updated</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {visibleProposals.map((p) => {
              const editable = EDITABLE.includes(p.status);
              return (
                <tr key={p.id}>
                  <td>
                    <div>{p.title}</div>
                    <div className="muted" style={{ fontSize: "11px" }}>
                      {p.proposal_type}
                    </div>
                  </td>
                  <td className="muted">
                    {p.start_time
                      ? new Date(p.start_time).toLocaleString()
                      : "—"}
                    {p.timezone ? ` (${p.timezone})` : ""}
                  </td>
                  <td>{statusChip(p.status)}</td>
                  <td className="muted">
                    {new Date(p.updated_at).toLocaleString()}
                  </td>
                  <td>
                    <div className="admin__row-actions">
                      <button
                        className="btn btn--ghost btn--sm"
                        onClick={() => setViewTarget(p)}
                      >
                        View
                      </button>
                      {editable && (
                        <button
                          className="btn btn--ghost btn--sm"
                          onClick={() => startEdit(p)}
                        >
                          Edit
                        </button>
                      )}
                      {p.status !== "archived" && (
                        <button
                          className="btn btn--ghost btn--sm"
                          onClick={() => openManage(p)}
                        >
                          Review
                        </button>
                      )}
                      {p.status === "approved" && (
                        <button
                          className="btn btn--ghost btn--sm"
                          onClick={() => prepareIntent(p)}
                          disabled={busy}
                          title="Creates a dry-run calendar payload. No event is created."
                        >
                          Prepare Calendar Intent
                        </button>
                      )}
                      {p.status === "approved" && (
                        <button
                          className="btn btn--ghost btn--sm"
                          onClick={() => prepareForProvider(p)}
                          disabled={busy}
                          title="Create an integration intent (future provider action). No event is created."
                        >
                          Prepare for Provider
                        </button>
                      )}
                      <button
                        className="btn btn--ghost btn--sm btn--danger"
                        onClick={() => setDeleteTarget(p)}
                        disabled={busy || deleting}
                        title="Permanently delete this proposal"
                      >
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {viewTarget && (
        <div className="modal-backdrop" onClick={() => setViewTarget(null)}>
          <div
            className="modal"
            style={{ maxWidth: "620px" }}
            role="dialog"
            aria-modal="true"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal__header">
              <h3 className="modal__title">
                {viewTarget.title} · {viewTarget.proposal_type}
              </h3>
            </div>
            <div className="modal__body" style={{ whiteSpace: "normal" }}>
              <ApprovalMeta item={viewTarget} />
              <table className="admin__table" style={{ marginTop: "8px" }}>
                <tbody>
                  <tr>
                    <td style={{ fontWeight: 600, width: "38%" }}>When</td>
                    <td className="muted">
                      {viewTarget.start_time
                        ? new Date(viewTarget.start_time).toLocaleString()
                        : "—"}
                      {viewTarget.end_time
                        ? ` → ${new Date(viewTarget.end_time).toLocaleString()}`
                        : ""}
                      {viewTarget.timezone ? ` (${viewTarget.timezone})` : ""}
                    </td>
                  </tr>
                  <tr>
                    <td style={{ fontWeight: 600 }}>Attendees</td>
                    <td className="muted">
                      {viewTarget.attendees.length
                        ? viewTarget.attendees
                            .map((a) => (typeof a === "string" ? a : JSON.stringify(a)))
                            .join(", ")
                        : "—"}
                    </td>
                  </tr>
                  <tr>
                    <td style={{ fontWeight: 600 }}>Agenda</td>
                    <td className="muted">
                      {viewTarget.agenda.length
                        ? viewTarget.agenda
                            .map((a) => (typeof a === "string" ? a : JSON.stringify(a)))
                            .join(", ")
                        : "—"}
                    </td>
                  </tr>
                </tbody>
              </table>
              <h4 style={{ margin: "12px 0 4px" }}>Description</h4>
              <pre
                style={{
                  background: "var(--surface-2, #1b1b27)",
                  padding: "8px",
                  borderRadius: "6px",
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                  margin: 0,
                }}
              >
                {viewTarget.description || "—"}
              </pre>
              <div
                className="admin__row-actions"
                style={{ justifyContent: "flex-end", marginTop: "16px" }}
              >
                <button className="btn btn--primary" onClick={() => setViewTarget(null)}>
                  Close
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {managed && (
        <div className="modal-backdrop" onClick={() => setManageId(null)}>
          <div
            className="modal"
            style={{ maxWidth: "620px" }}
            role="dialog"
            aria-modal="true"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal__header">
              <h3 className="modal__title">Review proposal · {managed.title}</h3>
            </div>
            <div className="modal__body" style={{ whiteSpace: "normal" }}>
              <ApprovalMeta item={managed} />
              <ReviewPanel
                status={managed.status}
                isAdmin={isAdmin}
                notes={notes}
                setNotes={setNotes}
                busy={busy}
                events={events}
                onSubmit={() => runAction(managed, submitChronosProposalForReview)}
                onRequestChanges={() => runAction(managed, requestChronosProposalChanges)}
                onMarkReviewed={() => runAction(managed, markChronosProposalReviewed)}
                onApprove={() => runAction(managed, approveChronosProposal)}
                onArchive={() => runAction(managed, archiveChronosProposal)}
                onHistory={() => loadEvents(managed)}
                reviewNotes={managed.review_notes}
              />
              <div
                className="admin__row-actions"
                style={{ justifyContent: "flex-end", marginTop: "12px" }}
              >
                <button className="btn btn--ghost" onClick={() => setManageId(null)}>
                  Close
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {deleteTarget && (
        <div
          className="modal-backdrop"
          onClick={() => !deleting && setDeleteTarget(null)}
        >
          <div
            className="modal"
            style={{ maxWidth: "440px" }}
            role="dialog"
            aria-modal="true"
            aria-labelledby="delete-proposal-title"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal__header">
              <h3 id="delete-proposal-title" className="modal__title">
                Delete Proposal
              </h3>
            </div>
            <div className="modal__body" style={{ whiteSpace: "normal" }}>
              <p style={{ marginTop: 0 }}>
                Are you sure you want to permanently delete this proposal?
              </p>
              <div
                className="admin__row-actions"
                style={{ justifyContent: "flex-end", marginTop: "16px" }}
              >
                <button
                  className="btn btn--ghost"
                  onClick={() => setDeleteTarget(null)}
                  disabled={deleting}
                >
                  Cancel
                </button>
                <button
                  className="btn btn--primary btn--danger"
                  onClick={confirmDelete}
                  disabled={deleting}
                >
                  {deleting ? "Deleting…" : "Delete Proposal"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
