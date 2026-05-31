import { useCallback, useEffect, useState } from "react";
import {
  adminCancelJob,
  adminGetJob,
  adminListJobs,
} from "../api";
import type { Job, JobStatus } from "../types";

const JOB_STATUSES: Array<JobStatus | ""> = [
  "",
  "queued",
  "running",
  "completed",
  "failed",
  "cancelled",
];

export function Jobs() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<JobStatus | "">("");
  const [typeFilter, setTypeFilter] = useState("");
  const [selected, setSelected] = useState<Job | null>(null);
  const [busy, setBusy] = useState<Record<string, boolean>>({});

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setJobs(
        await adminListJobs({
          limit: 200,
          status: statusFilter || undefined,
          job_type: typeFilter.trim() || undefined,
        }),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setLoading(false);
    }
  }, [statusFilter, typeFilter]);

  useEffect(() => {
    const handle = window.setTimeout(refresh, 250);
    return () => window.clearTimeout(handle);
  }, [refresh]);

  // Live updates while the page is open.
  useEffect(() => {
    const interval = window.setInterval(() => {
      refresh();
    }, 10_000);
    return () => window.clearInterval(interval);
  }, [refresh]);

  const openDetail = useCallback(async (id: string) => {
    try {
      setSelected(await adminGetJob(id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load job");
    }
  }, []);

  const cancel = useCallback(
    async (id: string) => {
      setBusy((m) => ({ ...m, [id]: true }));
      try {
        await adminCancelJob(id);
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Cancel failed");
      } finally {
        setBusy((m) => ({ ...m, [id]: false }));
      }
    },
    [refresh],
  );

  return (
    <main className="admin">
      <header className="admin__header">
        <h1>Background Jobs</h1>
        <p className="admin__subtitle">
          v0.1 queueing only — no worker yet. Jobs sit in the queue until a
          future executor consumes them.
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
                onChange={(e) =>
                  setStatusFilter(e.target.value as JobStatus | "")
                }
              >
                {JOB_STATUSES.map((s) => (
                  <option key={s || "any"} value={s}>
                    {s || "any"}
                  </option>
                ))}
              </select>
            </label>
            <label className="admin__field-wide">
              <span>Job type</span>
              <input
                className="cora-input"
                type="text"
                value={typeFilter}
                onChange={(e) => setTypeFilter(e.target.value)}
                placeholder="plan_step, …"
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
              <th>Type</th>
              <th>Status</th>
              <th>Plan / Step</th>
              <th>Attempts</th>
              <th>Error</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {jobs.map((j) => (
              <tr
                key={j.id}
                className={`trace-row${selected?.id === j.id ? " admin__row--selected" : ""}`}
                onClick={() => openDetail(j.id)}
              >
                <td className="muted">
                  {new Date(j.created_at).toLocaleString()}
                </td>
                <td className="mono">{j.job_type}</td>
                <td>
                  <span className={`status-chip status-chip--${jobStatusVariant(j.status)}`}>
                    {j.status}
                  </span>
                </td>
                <td className="mono muted">
                  {j.plan_id ? `${j.plan_id.slice(0, 8)}` : "—"}
                  {j.step_id ? ` / ${j.step_id.slice(0, 8)}` : ""}
                </td>
                <td className="muted">
                  {j.attempts}/{j.max_attempts}
                </td>
                <td className="muted">
                  {j.error_message
                    ? j.error_message.slice(0, 60) +
                      (j.error_message.length > 60 ? "…" : "")
                    : "—"}
                </td>
                <td>
                  {j.status === "queued" && (
                    <button
                      className="btn btn--ghost btn--sm"
                      onClick={(e) => {
                        e.stopPropagation();
                        cancel(j.id);
                      }}
                      disabled={!!busy[j.id]}
                    >
                      {busy[j.id] ? "…" : "Cancel"}
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {!loading && jobs.length === 0 && (
              <tr>
                <td colSpan={7} className="muted">
                  No jobs.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      {selected && (
        <JobDetail job={selected} onClose={() => setSelected(null)} />
      )}
    </main>
  );
}

function JobDetail({ job, onClose }: { job: Job; onClose: () => void }) {
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
      <div className="modal" role="dialog" aria-modal="true" aria-label="Job detail">
        <header className="modal__header">
          <h2 className="modal__title">
            {job.job_type}{" "}
            <span className="mono muted">{job.id.slice(0, 8)}</span>
          </h2>
          <button className="modal__close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>
        <div className="modal__meta">
          <span className={`status-chip status-chip--${jobStatusVariant(job.status)}`}>
            {job.status}
          </span>
          <span className="muted">
            attempts {job.attempts}/{job.max_attempts}
          </span>
          {job.plan_id && (
            <span className="mono muted">plan {job.plan_id.slice(0, 8)}</span>
          )}
          {job.step_id && (
            <span className="mono muted">step {job.step_id.slice(0, 8)}</span>
          )}
          <span className="muted">{new Date(job.created_at).toLocaleString()}</span>
        </div>
        <div className="modal__body">
          <pre className="trace-json">{JSON.stringify(job, null, 2)}</pre>
        </div>
      </div>
    </div>
  );
}

function jobStatusVariant(s: string): string {
  if (s === "completed") return "active";
  if (s === "failed" || s === "cancelled") return "archived";
  return "draft";
}
