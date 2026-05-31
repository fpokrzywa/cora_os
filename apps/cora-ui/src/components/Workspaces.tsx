import { useCallback, useEffect, useState } from "react";
import {
  createWorkspace,
  getWorkspace,
  listWorkspaces,
  patchWorkspace,
} from "../api";
import type { Workspace, WorkspaceDetail } from "../types";

interface Props {
  onWorkspacesChanged?: () => void;
}

export function Workspaces({ onWorkspacesChanged }: Props) {
  const [rows, setRows] = useState<Workspace[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<WorkspaceDetail | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setRows(await listWorkspaces(true));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setLoading(false);
    }
  }, []);

  const refreshDetail = useCallback(async (id: string) => {
    setLoadingDetail(true);
    try {
      setDetail(await getWorkspace(id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
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

  return (
    <main className="admin">
      <header className="admin__header">
        <h1>Workspaces</h1>
        <p className="admin__subtitle">
          Top-level grouping for chats, memory, plans, jobs and traces. Chat
          attaches new conversations to the workspace selected in the sidebar.
        </p>
      </header>

      {error && <div className="admin__error">{error}</div>}

      <section className="admin__section">
        <div className="admin__section-head">
          <h2>Workspaces</h2>
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
              <th>Name</th>
              <th>Slug</th>
              <th>Status</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((w) => (
              <tr
                key={w.id}
                className={`trace-row${selectedId === w.id ? " admin__row--selected" : ""}`}
                onClick={() => setSelectedId(w.id)}
              >
                <td>{w.name}</td>
                <td className="mono muted">{w.slug}</td>
                <td>
                  <span
                    className={`status-chip status-chip--${w.status === "active" ? "active" : "archived"}`}
                  >
                    {w.status}
                  </span>
                </td>
                <td className="muted">
                  {new Date(w.created_at).toLocaleDateString()}
                </td>
              </tr>
            ))}
            {!loading && rows.length === 0 && (
              <tr>
                <td colSpan={4} className="muted">
                  No workspaces yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      <CreateWorkspaceForm
        onCreated={() => {
          refresh();
          onWorkspacesChanged?.();
        }}
      />

      {detail && (
        <WorkspaceDetailSection
          detail={detail}
          loading={loadingDetail}
          onChanged={() => {
            refresh();
            if (selectedId) refreshDetail(selectedId);
            onWorkspacesChanged?.();
          }}
        />
      )}
    </main>
  );
}

function CreateWorkspaceForm({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setMsg(null);
    try {
      await createWorkspace({
        name: name.trim(),
        slug: slug.trim() || undefined,
        description: description.trim() || null,
      });
      setName("");
      setSlug("");
      setDescription("");
      setMsg("Created");
      onCreated();
    } catch (err) {
      setMsg(err instanceof Error ? err.message : "Failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section className="admin__section">
      <form className="admin__form" onSubmit={submit}>
        <h3>Create workspace</h3>
        <div className="admin__form-row">
          <label>
            <span>Name</span>
            <input
              className="cora-input"
              type="text"
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={submitting}
            />
          </label>
          <label>
            <span>Slug (optional)</span>
            <input
              className="cora-input"
              type="text"
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              disabled={submitting}
              placeholder="auto from name"
            />
          </label>
        </div>
        <label className="admin__field-wide">
          <span>Description</span>
          <textarea
            className="cora-input"
            rows={2}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            disabled={submitting}
          />
        </label>
        <div className="admin__form-row">
          <button
            type="submit"
            className="btn btn--primary"
            disabled={submitting}
          >
            {submitting ? "…" : "Create"}
          </button>
          {msg && <span className="admin__hint">{msg}</span>}
        </div>
      </form>
    </section>
  );
}

function WorkspaceDetailSection({
  detail,
  loading,
  onChanged,
}: {
  detail: WorkspaceDetail;
  loading: boolean;
  onChanged: () => void;
}) {
  const [name, setName] = useState(detail.name);
  const [description, setDescription] = useState(detail.description ?? "");
  const [statusVal, setStatusVal] = useState(detail.status);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    setName(detail.name);
    setDescription(detail.description ?? "");
    setStatusVal(detail.status);
    setMsg(null);
  }, [detail.id]);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setMsg(null);
    try {
      await patchWorkspace(detail.id, {
        name,
        description: description || null,
        status: statusVal,
      });
      setMsg("Saved");
      onChanged();
    } catch (err) {
      setMsg(err instanceof Error ? err.message : "Failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="admin__section">
      <div className="admin__section-head">
        <h2>
          {detail.name}{" "}
          <span className="mono muted">{detail.slug}</span>
        </h2>
        {loading && <span className="admin__hint">Loading…</span>}
      </div>

      <div className="admin__form-row" style={{ flexWrap: "wrap", gap: "12px" }}>
        {Object.entries(detail.counts).map(([k, v]) => (
          <span key={k} className="memory-pill">
            {v} {k.replace(/_/g, " ")}
          </span>
        ))}
      </div>

      <form className="admin__form" onSubmit={save}>
        <h3>Edit</h3>
        <div className="admin__form-row">
          <label>
            <span>Name</span>
            <input
              className="cora-input"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={busy}
            />
          </label>
          <label>
            <span>Status</span>
            <select
              className="cora-input"
              value={statusVal}
              onChange={(e) => setStatusVal(e.target.value)}
              disabled={busy}
            >
              <option value="active">active</option>
              <option value="archived">archived</option>
            </select>
          </label>
        </div>
        <label className="admin__field-wide">
          <span>Description</span>
          <textarea
            className="cora-input"
            rows={2}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            disabled={busy}
          />
        </label>
        <div className="admin__form-row">
          <button
            type="submit"
            className="btn btn--primary"
            disabled={busy}
          >
            {busy ? "…" : "Save"}
          </button>
          {msg && <span className="admin__hint">{msg}</span>}
        </div>
      </form>
    </section>
  );
}
