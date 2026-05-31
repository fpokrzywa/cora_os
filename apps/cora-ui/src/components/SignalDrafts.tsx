import { useCallback, useEffect, useState } from "react";
import {
  listSignalDrafts,
  createSignalDraft,
  updateSignalDraft,
  submitSignalDraftForReview,
  requestSignalDraftChanges,
  markSignalDraftReviewed,
  approveSignalDraft,
  archiveSignalDraft,
  listSignalDraftReviewEvents,
  createSignalDraftIntegrationIntent,
  createEmailIntentFromDraft,
  deleteSignalDraft,
} from "../api";
import type {
  CommunicationDraft,
  CommunicationDraftStatus,
  DraftReviewEvent,
} from "../types";

const EMPTY = {
  draft_type: "email",
  title: "",
  recipient_hint: "",
  subject: "",
  tone: "",
  body: "",
};

const EDITABLE: CommunicationDraftStatus[] = ["draft", "changes_requested"];

const DRAFT_STATUS_FILTERS: { value: string; label: string }[] = [
  { value: "all", label: "All statuses" },
  { value: "draft", label: "Draft" },
  { value: "in_review", label: "In review" },
  { value: "changes_requested", label: "Changes requested" },
  { value: "reviewed", label: "Reviewed" },
  { value: "approved", label: "Approved" },
  { value: "archived", label: "Archived" },
];

function statusChip(s: CommunicationDraftStatus) {
  const cls =
    s === "approved" || s === "reviewed"
      ? "status-chip status-chip--active"
      : s === "archived"
        ? "status-chip status-chip--archived"
        : "status-chip status-chip--draft";
  return <span className={cls}>{s.replace("_", " ")}</span>;
}

// Shared review/approval metadata table — satisfies the required display fields
// for both drafts and proposals.
export function ApprovalMeta({
  item,
}: {
  item: {
    status: string;
    reviewed_by: string | null;
    reviewed_at: string | null;
    approved_by: string | null;
    approved_at: string | null;
    review_notes: string | null;
  };
}) {
  const fdate = (d: string | null) => (d ? new Date(d).toLocaleString() : "—");
  const fuser = (u: string | null) => (u ? `${u.slice(0, 8)}…` : "—");
  const rows: Array<[string, string]> = [
    ["Status", item.status.replace(/_/g, " ")],
    ["Reviewed by", fuser(item.reviewed_by)],
    ["Reviewed at", fdate(item.reviewed_at)],
    ["Approved by", fuser(item.approved_by)],
    ["Approved at", fdate(item.approved_at)],
    ["Review notes", item.review_notes || "—"],
  ];
  return (
    <table className="admin__table" style={{ marginTop: 0 }}>
      <tbody>
        {rows.map(([k, v]) => (
          <tr key={k}>
            <td style={{ fontWeight: 600, width: "38%" }}>{k}</td>
            <td className="muted">{v}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function SignalDrafts({
  workspaceId,
  isAdmin = false,
}: {
  workspaceId: string | null;
  isAdmin?: boolean;
}) {
  const [drafts, setDrafts] = useState<CommunicationDraft[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [form, setForm] = useState({ ...EMPTY });
  const [editingId, setEditingId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [manageId, setManageId] = useState<string | null>(null);
  const [notes, setNotes] = useState("");
  const [events, setEvents] = useState<DraftReviewEvent[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [intentMsg, setIntentMsg] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<CommunicationDraft | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [viewTarget, setViewTarget] = useState<CommunicationDraft | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>("all");

  const load = useCallback(async () => {
    if (!workspaceId) return;
    setLoading(true);
    setError(null);
    try {
      setDrafts(await listSignalDrafts(workspaceId, includeArchived));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load drafts");
    } finally {
      setLoading(false);
    }
  }, [workspaceId, includeArchived]);

  useEffect(() => {
    load();
  }, [load]);

  const resetForm = () => {
    setForm({ ...EMPTY });
    setEditingId(null);
  };

  const submit = async () => {
    if (!workspaceId) return;
    if (!form.draft_type.trim() || !form.body.trim()) {
      setError("Draft type and body are required.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const payload = {
        draft_type: form.draft_type.trim(),
        title: form.title || null,
        recipient_hint: form.recipient_hint || null,
        subject: form.subject || null,
        tone: form.tone || null,
        body: form.body,
      };
      if (editingId) {
        await updateSignalDraft(workspaceId, editingId, payload);
      } else {
        await createSignalDraft(workspaceId, payload);
      }
      resetForm();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save draft");
    } finally {
      setSaving(false);
    }
  };

  const startEdit = (d: CommunicationDraft) => {
    setEditingId(d.id);
    setForm({
      draft_type: d.draft_type,
      title: d.title ?? "",
      recipient_hint: d.recipient_hint ?? "",
      subject: d.subject ?? "",
      tone: d.tone ?? "",
      body: d.body,
    });
  };

  const openManage = (d: CommunicationDraft) => {
    if (manageId === d.id) {
      setManageId(null);
      return;
    }
    setManageId(d.id);
    setNotes("");
    setEvents(null);
  };

  const runAction = async (
    d: CommunicationDraft,
    fn: (ws: string, id: string, notes?: string) => Promise<CommunicationDraft>,
  ) => {
    if (!workspaceId) return;
    setBusy(true);
    setError(null);
    try {
      await fn(workspaceId, d.id, notes.trim() || undefined);
      setNotes("");
      setEvents(null);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Action failed");
    } finally {
      setBusy(false);
    }
  };

  const loadEvents = async (d: CommunicationDraft) => {
    if (!workspaceId) return;
    try {
      setEvents(await listSignalDraftReviewEvents(workspaceId, d.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load history");
    }
  };

  const prepareIntent = async (d: CommunicationDraft) => {
    if (!workspaceId) return;
    setBusy(true);
    setError(null);
    setIntentMsg(null);
    try {
      const intent = await createSignalDraftIntegrationIntent(workspaceId, d.id, {
        provider_name: "internal_preview",
        action_type: "send_email",
      });
      setIntentMsg(
        `Dry-run email intent prepared (status: ${intent.status}). No email is sent. See Tools → Integration Readiness.`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to prepare intent");
    } finally {
      setBusy(false);
    }
  };

  const prepareForProvider = async (d: CommunicationDraft) => {
    setBusy(true);
    setError(null);
    setIntentMsg(null);
    try {
      const intent = await createEmailIntentFromDraft(d.id);
      setToast("Integration intent created.");
      setIntentMsg(
        `Integration intent created (status: ${intent.status}). No email is sent — see Tools → Integration Queue.`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create integration intent");
    } finally {
      setBusy(false);
    }
  };

  const copyBody = async (d: CommunicationDraft) => {
    try {
      await navigator.clipboard.writeText(d.body);
      setCopiedId(d.id);
      setTimeout(() => setCopiedId((c) => (c === d.id ? null : c)), 1500);
    } catch {
      setError("Clipboard copy failed");
    }
  };

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
      await deleteSignalDraft(workspaceId, target.id);
      // Remove from the list immediately, then refresh the queue.
      setDrafts((ds) => ds.filter((x) => x.id !== target.id));
      if (manageId === target.id) setManageId(null);
      setDeleteTarget(null);
      setToast("Draft deleted.");
      await load();
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to delete draft";
      // Friendly message when the draft is already gone.
      setError(
        msg.startsWith("404")
          ? "That draft no longer exists — it may have already been deleted."
          : msg,
      );
      setDeleteTarget(null);
    } finally {
      setDeleting(false);
    }
  };

  const visibleDrafts =
    statusFilter === "all"
      ? drafts
      : drafts.filter((d) => d.status === statusFilter);
  const managed = drafts.find((d) => d.id === manageId) || null;

  if (!workspaceId) {
    return (
      <section className="admin__section">
        <h2>SIGNAL Drafts</h2>
        <div className="admin__hint">Select a workspace to manage drafts.</div>
      </section>
    );
  }

  return (
    <section className="admin__section">
      <div className="admin__section-head">
        <h2>SIGNAL Drafts</h2>
        <button
          className="btn btn--ghost btn--sm"
          onClick={load}
          disabled={loading}
        >
          ↻ Refresh
        </button>
      </div>
      <p className="admin__hint">
        Review-only communication drafts. SIGNAL never sends — drafts move
        through draft → in&nbsp;review → reviewed → approved. Approval is
        internal only; no email action is performed.
      </p>

      <div className="admin__form">
        <h3 className="admin__vt-h3">
          {editingId ? "Edit draft" : "New draft"}
        </h3>
        <div className="admin__form-row">
          <label>
            <span>Draft type</span>
            <input
              className="cora-input"
              value={form.draft_type}
              onChange={(e) => setForm({ ...form, draft_type: e.target.value })}
              placeholder="email, message, announcement…"
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
            <span>Tone</span>
            <input
              className="cora-input"
              value={form.tone}
              onChange={(e) => setForm({ ...form, tone: e.target.value })}
              placeholder="professional, friendly…"
            />
          </label>
        </div>
        <div className="admin__form-row">
          <label>
            <span>Recipient hint</span>
            <input
              className="cora-input"
              value={form.recipient_hint}
              onChange={(e) =>
                setForm({ ...form, recipient_hint: e.target.value })
              }
              placeholder="stakeholders, team lead…"
            />
          </label>
          <label>
            <span>Subject</span>
            <input
              className="cora-input"
              value={form.subject}
              onChange={(e) => setForm({ ...form, subject: e.target.value })}
            />
          </label>
        </div>
        <label>
          <span>Body</span>
          <textarea
            className="cora-input"
            rows={6}
            value={form.body}
            onChange={(e) => setForm({ ...form, body: e.target.value })}
          />
        </label>
        <div className="admin__form-row">
          <button
            className="btn btn--primary"
            onClick={submit}
            disabled={saving}
          >
            {saving ? "Saving…" : editingId ? "Save changes" : "Create draft"}
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
            {DRAFT_STATUS_FILTERS.map((o) => (
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

      {loading && drafts.length === 0 ? (
        <div className="admin__hint">Loading drafts…</div>
      ) : drafts.length === 0 ? (
        <div className="admin__hint">No drafts yet.</div>
      ) : visibleDrafts.length === 0 ? (
        <div className="admin__hint">No drafts match this filter.</div>
      ) : (
        <table className="admin__table">
          <thead>
            <tr>
              <th>Title / Type</th>
              <th>Recipient</th>
              <th>Status</th>
              <th>Updated</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {visibleDrafts.map((d) => {
              const editable = EDITABLE.includes(d.status);
              return (
                <tr key={d.id}>
                  <td>
                    <div>{d.title || "(untitled)"}</div>
                    <div className="muted" style={{ fontSize: "11px" }}>
                      {d.draft_type}
                      {d.subject ? ` · ${d.subject}` : ""}
                    </div>
                  </td>
                  <td className="muted">{d.recipient_hint || "—"}</td>
                  <td>{statusChip(d.status)}</td>
                  <td className="muted">
                    {new Date(d.updated_at).toLocaleString()}
                  </td>
                  <td>
                    <div className="admin__row-actions">
                      <button
                        className="btn btn--ghost btn--sm"
                        onClick={() => setViewTarget(d)}
                      >
                        View
                      </button>
                      <button
                        className="btn btn--ghost btn--sm"
                        onClick={() => copyBody(d)}
                      >
                        {copiedId === d.id ? "Copied!" : "Copy Draft"}
                      </button>
                      {editable && (
                        <button
                          className="btn btn--ghost btn--sm"
                          onClick={() => startEdit(d)}
                        >
                          Edit
                        </button>
                      )}
                      {d.status !== "archived" && (
                        <button
                          className="btn btn--ghost btn--sm"
                          onClick={() => openManage(d)}
                        >
                          Review
                        </button>
                      )}
                      {d.status === "approved" && (
                        <button
                          className="btn btn--ghost btn--sm"
                          onClick={() => prepareIntent(d)}
                          disabled={busy}
                          title="Creates a dry-run email payload. No email is sent."
                        >
                          Prepare Send Intent
                        </button>
                      )}
                      {d.status === "approved" && (
                        <button
                          className="btn btn--ghost btn--sm"
                          onClick={() => prepareForProvider(d)}
                          disabled={busy}
                          title="Create an integration intent (future provider action). No email is sent."
                        >
                          Prepare for Provider
                        </button>
                      )}
                      <button
                        className="btn btn--ghost btn--sm btn--danger"
                        onClick={() => setDeleteTarget(d)}
                        disabled={busy || deleting}
                        title="Permanently delete this draft"
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
                {viewTarget.title || "(untitled)"} · {viewTarget.draft_type}
              </h3>
            </div>
            <div className="modal__body" style={{ whiteSpace: "normal" }}>
              <ApprovalMeta item={viewTarget} />
              <table className="admin__table" style={{ marginTop: "8px" }}>
                <tbody>
                  <tr>
                    <td style={{ fontWeight: 600, width: "38%" }}>Recipient</td>
                    <td className="muted">{viewTarget.recipient_hint || "—"}</td>
                  </tr>
                  <tr>
                    <td style={{ fontWeight: 600 }}>Subject</td>
                    <td className="muted">{viewTarget.subject || "—"}</td>
                  </tr>
                  <tr>
                    <td style={{ fontWeight: 600 }}>Tone</td>
                    <td className="muted">{viewTarget.tone || "—"}</td>
                  </tr>
                </tbody>
              </table>
              <h4 style={{ margin: "12px 0 4px" }}>Body</h4>
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
                {viewTarget.body}
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
              <h3 className="modal__title">
                Review draft · {managed.title || "(untitled)"}
              </h3>
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
                onSubmit={() => runAction(managed, submitSignalDraftForReview)}
                onRequestChanges={() => runAction(managed, requestSignalDraftChanges)}
                onMarkReviewed={() => runAction(managed, markSignalDraftReviewed)}
                onApprove={() => runAction(managed, approveSignalDraft)}
                onArchive={() => runAction(managed, archiveSignalDraft)}
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
            aria-labelledby="delete-draft-title"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal__header">
              <h3 id="delete-draft-title" className="modal__title">
                Delete Draft
              </h3>
            </div>
            <div className="modal__body" style={{ whiteSpace: "normal" }}>
              <p style={{ marginTop: 0 }}>
                Are you sure you want to permanently delete this draft?
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
                  {deleting ? "Deleting…" : "Delete Draft"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

// Shared inline review panel — actions are enabled per current status.
export function ReviewPanel({
  status,
  isAdmin,
  notes,
  setNotes,
  busy,
  events,
  onSubmit,
  onRequestChanges,
  onMarkReviewed,
  onApprove,
  onArchive,
  onHistory,
  reviewNotes,
}: {
  status: string;
  isAdmin: boolean;
  notes: string;
  setNotes: (v: string) => void;
  busy: boolean;
  events: { id: string; action: string; from_status: string | null; to_status: string | null; notes: string | null; user_id: string | null; created_at: string }[] | null;
  onSubmit: () => void;
  onRequestChanges: () => void;
  onMarkReviewed: () => void;
  onApprove: () => void;
  onArchive: () => void;
  onHistory: () => void;
  reviewNotes: string | null;
}) {
  const canSubmit = status === "draft" || status === "proposed" || status === "changes_requested";
  const canRequestChanges = status === "in_review";
  // Mark Reviewed supports the direct path (draft/proposed -> reviewed) as well
  // as the submit-first path (in_review -> reviewed).
  const canMarkReviewed =
    status === "in_review" ||
    status === "draft" ||
    status === "proposed" ||
    status === "changes_requested";
  const canApprove = status === "reviewed";
  const canArchive = status !== "archived";
  return (
    <div className="admin__review-panel" style={{ marginTop: "8px", padding: "8px", borderTop: "1px solid var(--border, #333)" }}>
      <p className="admin__hint" style={{ marginTop: 0 }}>
        Approval is internal only. No email or calendar action is performed.
      </p>
      <textarea
        className="cora-input"
        rows={2}
        placeholder="Optional review notes…"
        value={notes}
        onChange={(e) => setNotes(e.target.value)}
      />
      <div className="admin__row-actions" style={{ marginTop: "6px", flexWrap: "wrap" }}>
        {canSubmit && (
          <button className="btn btn--ghost btn--sm" onClick={onSubmit} disabled={busy}>
            Submit for Review
          </button>
        )}
        {canRequestChanges && (
          <button className="btn btn--ghost btn--sm" onClick={onRequestChanges} disabled={busy}>
            Request Changes
          </button>
        )}
        {canMarkReviewed && (
          <button className="btn btn--ghost btn--sm" onClick={onMarkReviewed} disabled={busy}>
            Mark Reviewed
          </button>
        )}
        {canApprove && (
          <button
            className="btn btn--primary btn--sm"
            onClick={onApprove}
            disabled={busy || !isAdmin}
            title={isAdmin ? "Approve internally" : "Approval requires an admin reviewer"}
          >
            Approve
          </button>
        )}
        {canArchive && (
          <button className="btn btn--ghost btn--sm" onClick={onArchive} disabled={busy}>
            Archive
          </button>
        )}
        <button className="btn btn--ghost btn--sm" onClick={onHistory} disabled={busy}>
          View Review History
        </button>
      </div>
      {reviewNotes && (
        <div className="muted" style={{ fontSize: "11px", marginTop: "6px" }}>
          Last note: {reviewNotes}
        </div>
      )}
      {events && (
        <div style={{ marginTop: "8px" }}>
          {events.length === 0 ? (
            <div className="admin__hint">No review history yet.</div>
          ) : (
            <table className="admin__table" style={{ fontSize: "11px" }}>
              <thead>
                <tr>
                  <th>Action</th>
                  <th>From → To</th>
                  <th>Notes</th>
                  <th>When</th>
                </tr>
              </thead>
              <tbody>
                {events.map((ev) => (
                  <tr key={ev.id}>
                    <td>{ev.action.replace(/_/g, " ")}</td>
                    <td className="muted">
                      {ev.from_status} → {ev.to_status}
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
  );
}
