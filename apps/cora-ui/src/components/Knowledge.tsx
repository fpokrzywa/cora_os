import { useCallback, useEffect, useState } from "react";
import {
  deleteKnowledgeSource,
  getKnowledgeSource,
  ingestKnowledge,
  ingestKnowledgeBulk,
  ingestUrlKnowledge,
  listKnowledgeNewsFeeds,
  listKnowledgeSources,
  listWorkspaceKnowledge,
  refreshKnowledgeSource,
  registerKnowledgeNewsFeed,
  refreshKnowledgeNewsFeed,
  updateKnowledgeNewsFeed,
  uploadKnowledgeFile,
} from "../api";
import type {
  KnowledgeEntry,
  KnowledgeScope,
  KnowledgeNewsFeed,
  KnowledgeSource,
  KnowledgeSourceDetail,
  KnowledgeSourceMetadata,
} from "../types";
import { FileDropzone } from "./FileDropzone";
import { NewsBriefing } from "./NewsBriefing";

const REFRESH_LABEL: Record<string, string> = {
  checking: "Checking…",
  updated: "Updated",
  unchanged: "Unchanged",
  failed: "Failed",
};

function relTime(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso).getTime();
  if (Number.isNaN(d)) return "";
  const m = Math.round((Date.now() - d) / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

// Shared scope explainer shown above the ingestion forms (all forms expose a
// Scope selector; admins may pick global/system, others are limited to user).
function ScopeHelp() {
  return (
    <p className="admin__hint scope-help">
      <strong>Scope:</strong> User scope is only visible to you. Global scope is
      visible in the workspace. System scope is reserved for platform-level
      knowledge. Defaults to <span className="mono">user</span>; only admins can
      select global or system.
    </p>
  );
}

function freshnessLine(meta?: KnowledgeSourceMetadata | null): string | null {
  if (!meta) return null;
  const parts: string[] = [];
  if (meta.last_checked_at) parts.push(`checked ${relTime(meta.last_checked_at)}`);
  else if (meta.fetched_at) parts.push(`fetched ${relTime(meta.fetched_at)}`);
  if (meta.last_changed_at) parts.push(`changed ${relTime(meta.last_changed_at)}`);
  return parts.length ? parts.join(" · ") : null;
}

interface Props {
  workspaceId: string | null;
  isAdmin: boolean;
}

const SCOPES: KnowledgeScope[] = ["user", "global", "system"];

export function Knowledge({ workspaceId, isAdmin }: Props) {
  const [rows, setRows] = useState<KnowledgeEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!workspaceId) return;
    setLoading(true);
    setError(null);
    try {
      setRows(await listWorkspaceKnowledge(workspaceId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setLoading(false);
    }
  }, [workspaceId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  if (!workspaceId) {
    return (
      <main className="admin">
        <header className="admin__header">
          <h1>Knowledge</h1>
        </header>
        <div className="admin__hint">Select a workspace in the sidebar.</div>
      </main>
    );
  }

  return (
    <main className="admin">
      <header className="admin__header">
        <h1>Workspace Knowledge</h1>
        <p className="admin__subtitle">
          Structured project knowledge for the selected workspace. Entries are
          stored as memory rows tagged{" "}
          <span className="mono">knowledge_ingested</span>.
        </p>
      </header>

      <ScopeHelp />

      {error && <div className="admin__error">{error}</div>}

      <UrlIngestForm
        workspaceId={workspaceId}
        isAdmin={isAdmin}
        onIngested={refresh}
      />
      <NewsFeedManager
        workspaceId={workspaceId}
        isAdmin={isAdmin}
        onIngested={refresh}
      />
      <NewsBriefing workspaceId={workspaceId} />
      <UploadEntryForm
        workspaceId={workspaceId}
        isAdmin={isAdmin}
        onUploaded={refresh}
      />
      <SingleEntryForm
        workspaceId={workspaceId}
        isAdmin={isAdmin}
        onCreated={refresh}
      />
      <BulkEntryForm
        workspaceId={workspaceId}
        isAdmin={isAdmin}
        onCreated={refresh}
      />

      <KnowledgeSourcesSection workspaceId={workspaceId} />

      <section className="admin__section">
        <div className="admin__section-head">
          <h2>Existing knowledge ({rows.length})</h2>
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
              <th>Title</th>
              <th>Scope</th>
              <th>Tags</th>
              <th>Imp.</th>
              <th>Embedded</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((e) => (
              <tr key={e.id}>
                <td>{e.title}</td>
                <td>
                  <span className={`scope-chip scope-chip--${e.scope_type}`}>
                    {e.scope_type}
                  </span>
                </td>
                <td className="muted">
                  {e.tags.length > 0 ? e.tags.join(", ") : "—"}
                </td>
                <td>{e.importance}</td>
                <td>
                  <span
                    className={`status-chip status-chip--${e.embedded ? "active" : "draft"}`}
                  >
                    {e.embedded ? "yes" : "no"}
                  </span>
                </td>
                <td className="muted">
                  {new Date(e.created_at).toLocaleString()}
                </td>
              </tr>
            ))}
            {!loading && rows.length === 0 && (
              <tr>
                <td colSpan={6} className="muted">
                  No knowledge entries in this workspace yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>
    </main>
  );
}

const REFRESH_INTERVALS: { label: string; value: number }[] = [
  { label: "Manual only", value: 0 },
  { label: "Every 15 minutes", value: 15 },
  { label: "Hourly", value: 60 },
  { label: "Every 6 hours", value: 360 },
  { label: "Every 12 hours", value: 720 },
  { label: "Daily", value: 1440 },
  { label: "Weekly", value: 10080 },
];

function intervalLabel(min: number | null): string {
  if (!min || min <= 0) return "Manual";
  const found = REFRESH_INTERVALS.find((i) => i.value === min);
  return found ? found.label : `${min}m`;
}

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

function lastResultText(r: Record<string, number> | null): string {
  if (!r) return "—";
  return (
    `${r.articles_created ?? 0} new · ${r.articles_updated ?? 0} upd · ` +
    `${r.articles_skipped_duplicate ?? 0} dup`
  );
}

function NewsFeedManager({
  workspaceId,
  isAdmin,
  onIngested,
}: {
  workspaceId: string;
  isAdmin: boolean;
  onIngested: () => void;
}) {
  // --- register/schedule form state ---
  const [sourceName, setSourceName] = useState("");
  const [feedUrl, setFeedUrl] = useState("");
  const [maxItems, setMaxItems] = useState(20);
  const [scope, setScope] = useState<KnowledgeScope>("user");
  const [importance, setImportance] = useState(3);
  const [autoEmbed, setAutoEmbed] = useState(false);
  const [fetchBody, setFetchBody] = useState(false);
  const [interval, setIntervalMin] = useState(0);
  const [enabled, setEnabled] = useState(true);
  const [ingestNow, setIngestNow] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  // --- managed feeds state ---
  const [feeds, setFeeds] = useState<KnowledgeNewsFeed[]>([]);
  const [loadingFeeds, setLoadingFeeds] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [rowMsg, setRowMsg] = useState<Record<string, string>>({});
  const [editingId, setEditingId] = useState<string | null>(null);

  const loadFeeds = useCallback(async () => {
    setLoadingFeeds(true);
    try {
      setFeeds(await listKnowledgeNewsFeeds(workspaceId));
    } catch {
      /* surfaced via register/action errors */
    } finally {
      setLoadingFeeds(false);
    }
  }, [workspaceId]);

  useEffect(() => {
    loadFeeds();
  }, [loadFeeds]);

  const register = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitting || !feedUrl.trim()) return;
    setSubmitting(true);
    setResult(null);
    try {
      const feed = await registerKnowledgeNewsFeed(workspaceId, {
        source_name: sourceName.trim() || undefined,
        feed_url: feedUrl.trim(),
        max_items: maxItems,
        scope_type: scope,
        importance,
        auto_embed: autoEmbed,
        fetch_article_body: fetchBody,
        refresh_enabled: enabled && interval > 0,
        refresh_interval_minutes: interval > 0 ? interval : null,
        ingest_now: ingestNow,
      });
      setResult({
        kind: "ok",
        text:
          `Registered “${feed.source_name}” · ${intervalLabel(feed.refresh_interval_minutes)}` +
          (feed.refresh_enabled ? " · scheduled" : " · manual") +
          (ingestNow ? " · ingested now" : "") +
          (feed.last_error ? ` · ingest error: ${feed.last_error}` : ""),
      });
      setFeedUrl("");
      setSourceName("");
      await loadFeeds();
      if (ingestNow) onIngested();
    } catch (err) {
      setResult({ kind: "error", text: err instanceof Error ? err.message : "Failed" });
    } finally {
      setSubmitting(false);
    }
  };

  const doRefresh = async (f: KnowledgeNewsFeed) => {
    setBusyId(f.id);
    setRowMsg((m) => ({ ...m, [f.id]: "Refreshing…" }));
    try {
      const r = await refreshKnowledgeNewsFeed(f.id);
      setRowMsg((m) => ({
        ...m,
        [f.id]:
          `${r.articles_created} new · ${r.articles_updated} upd · ` +
          `${r.articles_skipped_duplicate} dup · ${r.article_bodies_fetched} bodies`,
      }));
      await loadFeeds();
      onIngested();
    } catch (err) {
      setRowMsg((m) => ({ ...m, [f.id]: err instanceof Error ? err.message : "Failed" }));
    } finally {
      setBusyId(null);
    }
  };

  const toggleEnabled = async (f: KnowledgeNewsFeed) => {
    setBusyId(f.id);
    try {
      await updateKnowledgeNewsFeed(f.id, { refresh_enabled: !f.refresh_enabled });
      await loadFeeds();
    } catch (err) {
      setRowMsg((m) => ({ ...m, [f.id]: err instanceof Error ? err.message : "Failed" }));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <section className="admin__section">
      <form className="admin__form" onSubmit={register}>
        <h3>Register / schedule news feed</h3>
        <p className="admin__hint">
          Register an RSS/Atom feed once; Cora refreshes it in the background on
          the chosen interval (PULSE retrieves the articles). Re-ingest skips
          existing articles. No crawling.
        </p>
        <div className="admin__form-row">
          <label className="admin__field-wide">
            <span>Feed URL (RSS/Atom)</span>
            <input className="cora-input" type="url" inputMode="url"
              placeholder="https://hnrss.org/frontpage" value={feedUrl}
              onChange={(e) => setFeedUrl(e.target.value)} disabled={submitting} />
          </label>
          <label>
            <span>Max items</span>
            <input className="cora-input" type="number" min={1} max={50} value={maxItems}
              onChange={(e) => setMaxItems(Number(e.target.value))} disabled={submitting} />
          </label>
        </div>
        <div className="admin__form-row">
          <label className="admin__field-wide">
            <span>Source name (optional — uses feed title if blank)</span>
            <input className="cora-input" type="text" placeholder="Hacker News Frontpage"
              value={sourceName} onChange={(e) => setSourceName(e.target.value)} disabled={submitting} />
          </label>
          <label>
            <span>Scope</span>
            <select className="cora-input" value={scope}
              onChange={(e) => setScope(e.target.value as KnowledgeScope)} disabled={submitting}>
              <option value="user">user</option>
              <option value="global" disabled={!isAdmin}>global{!isAdmin ? " (admin)" : ""}</option>
              <option value="system" disabled={!isAdmin}>system{!isAdmin ? " (admin)" : ""}</option>
            </select>
          </label>
          <label>
            <span>Importance</span>
            <input className="cora-input" type="number" min={1} max={5} value={importance}
              onChange={(e) => setImportance(Number(e.target.value))} disabled={submitting} />
          </label>
        </div>
        <div className="admin__form-row">
          <label>
            <span>Refresh interval</span>
            <select className="cora-input" value={interval}
              onChange={(e) => setIntervalMin(Number(e.target.value))} disabled={submitting}>
              {REFRESH_INTERVALS.map((i) => (
                <option key={i.value} value={i.value}>{i.label}</option>
              ))}
            </select>
          </label>
          <label style={{ flexDirection: "row", alignItems: "center", gap: "8px" }}>
            <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} disabled={submitting} />
            <span>Scheduling enabled</span>
          </label>
        </div>
        <label style={{ flexDirection: "row", alignItems: "center", gap: "8px" }}>
          <input type="checkbox" checked={fetchBody} onChange={(e) => setFetchBody(e.target.checked)} disabled={submitting} />
          <span>Fetch full article body
            <span className="admin__hint"> — fetches each article link's readable page/PDF body. Slower, better for PULSE.</span>
          </span>
        </label>
        <div className="admin__form-row">
          <label style={{ flexDirection: "row", alignItems: "center", gap: "8px" }}>
            <input type="checkbox" checked={autoEmbed} onChange={(e) => setAutoEmbed(e.target.checked)} disabled={submitting} />
            <span>Auto-embed</span>
          </label>
          <label style={{ flexDirection: "row", alignItems: "center", gap: "8px" }}>
            <input type="checkbox" checked={ingestNow} onChange={(e) => setIngestNow(e.target.checked)} disabled={submitting} />
            <span>Ingest now</span>
          </label>
          <button type="submit" className="btn btn--primary" disabled={submitting || !feedUrl.trim()}>
            {submitting ? "Registering…" : "Register feed"}
          </button>
          {result && (
            <span className={result.kind === "error" ? "admin__error" : "admin__hint"}>{result.text}</span>
          )}
        </div>
      </form>

      <div className="admin__section-head" style={{ marginTop: "8px" }}>
        <h3>Managed news feeds ({feeds.length})</h3>
        <button className="btn btn--ghost btn--sm" onClick={loadFeeds} disabled={loadingFeeds}>↻ Refresh</button>
      </div>
      <table className="admin__table">
        <thead>
          <tr>
            <th>Source</th><th>Scope</th><th>Interval</th><th>Enabled</th>
            <th>Body</th><th>Embed</th><th>Last checked</th><th>Last success</th>
            <th>Next refresh</th><th>Last result</th><th />
          </tr>
        </thead>
        <tbody>
          {feeds.length === 0 && !loadingFeeds && (
            <tr><td colSpan={11} className="muted">No registered feeds.</td></tr>
          )}
          {feeds.map((f) => (
            <tr key={f.id}>
              <td>
                <div>{f.source_name}</div>
                <div className="mono muted" style={{ fontSize: "10.5px" }}>{f.feed_url}</div>
                {rowMsg[f.id] && <div className="admin__hint">{rowMsg[f.id]}</div>}
              </td>
              <td><span className={`scope-chip scope-chip--${f.scope_type}`}>{f.scope_type}</span></td>
              <td className="muted">{intervalLabel(f.refresh_interval_minutes)}</td>
              <td>
                <span className={`status-chip status-chip--${f.refresh_enabled ? "active" : "archived"}`}>
                  {f.refresh_enabled ? "on" : "off"}
                </span>
              </td>
              <td className="muted">{f.fetch_article_body ? "yes" : "no"}</td>
              <td className="muted">{f.auto_embed ? "yes" : "no"}</td>
              <td className="muted">{fmtTime(f.last_checked_at)}</td>
              <td className="muted">{fmtTime(f.last_success_at)}</td>
              <td className="muted">{fmtTime(f.next_refresh_at)}</td>
              <td className="muted">
                {f.last_error ? <span className="admin__error">{f.last_error}</span> : lastResultText(f.last_result)}
              </td>
              <td>
                <div className="admin__row-actions">
                  <button className="btn btn--primary btn--sm" onClick={() => doRefresh(f)} disabled={busyId === f.id}>
                    {busyId === f.id ? "…" : "Refresh"}
                  </button>
                  <button className="btn btn--ghost btn--sm" onClick={() => toggleEnabled(f)} disabled={busyId === f.id}>
                    {f.refresh_enabled ? "Disable" : "Enable"}
                  </button>
                  <button className="btn btn--ghost btn--sm" onClick={() => setEditingId(editingId === f.id ? null : f.id)}>
                    {editingId === f.id ? "Close" : "Edit"}
                  </button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {editingId && (() => {
        const f = feeds.find((x) => x.id === editingId);
        return f ? (
          <NewsFeedEditor key={f.id} feed={f} isAdmin={isAdmin}
            onSaved={() => { setEditingId(null); loadFeeds(); }} />
        ) : null;
      })()}
    </section>
  );
}

function NewsFeedEditor({
  feed,
  isAdmin,
  onSaved,
}: {
  feed: KnowledgeNewsFeed;
  isAdmin: boolean;
  onSaved: () => void;
}) {
  const [sourceName, setSourceName] = useState(feed.source_name);
  const [maxItems, setMaxItems] = useState(feed.max_items);
  const [scope, setScope] = useState<KnowledgeScope>(feed.scope_type);
  const [importance, setImportance] = useState(feed.importance);
  const [autoEmbed, setAutoEmbed] = useState(feed.auto_embed);
  const [fetchBody, setFetchBody] = useState(feed.fetch_article_body);
  const [interval, setIntervalMin] = useState(feed.refresh_interval_minutes ?? 0);
  const [enabled, setEnabled] = useState(feed.refresh_enabled);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const save = async () => {
    setSaving(true);
    setMsg(null);
    try {
      await updateKnowledgeNewsFeed(feed.id, {
        source_name: sourceName.trim() || undefined,
        max_items: maxItems,
        scope_type: scope,
        importance,
        auto_embed: autoEmbed,
        fetch_article_body: fetchBody,
        refresh_enabled: enabled && interval > 0,
        refresh_interval_minutes: interval > 0 ? interval : null,
      });
      onSaved();
    } catch (err) {
      setMsg(err instanceof Error ? err.message : "Failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="admin__form" style={{ marginTop: "8px" }}>
      <h4 className="admin__vt-h3">Edit “{feed.source_name}”</h4>
      <div className="admin__form-row">
        <label className="admin__field-wide">
          <span>Source name</span>
          <input className="cora-input" type="text" value={sourceName}
            onChange={(e) => setSourceName(e.target.value)} disabled={saving} />
        </label>
        <label>
          <span>Max items</span>
          <input className="cora-input" type="number" min={1} max={50} value={maxItems}
            onChange={(e) => setMaxItems(Number(e.target.value))} disabled={saving} />
        </label>
        <label>
          <span>Scope</span>
          <select className="cora-input" value={scope}
            onChange={(e) => setScope(e.target.value as KnowledgeScope)} disabled={saving}>
            <option value="user">user</option>
            <option value="global" disabled={!isAdmin}>global{!isAdmin ? " (admin)" : ""}</option>
            <option value="system" disabled={!isAdmin}>system{!isAdmin ? " (admin)" : ""}</option>
          </select>
        </label>
        <label>
          <span>Importance</span>
          <input className="cora-input" type="number" min={1} max={5} value={importance}
            onChange={(e) => setImportance(Number(e.target.value))} disabled={saving} />
        </label>
      </div>
      <div className="admin__form-row">
        <label>
          <span>Refresh interval</span>
          <select className="cora-input" value={interval}
            onChange={(e) => setIntervalMin(Number(e.target.value))} disabled={saving}>
            {REFRESH_INTERVALS.map((i) => (<option key={i.value} value={i.value}>{i.label}</option>))}
          </select>
        </label>
        <label style={{ flexDirection: "row", alignItems: "center", gap: "8px" }}>
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} disabled={saving} />
          <span>Scheduling enabled</span>
        </label>
        <label style={{ flexDirection: "row", alignItems: "center", gap: "8px" }}>
          <input type="checkbox" checked={fetchBody} onChange={(e) => setFetchBody(e.target.checked)} disabled={saving} />
          <span>Fetch body</span>
        </label>
        <label style={{ flexDirection: "row", alignItems: "center", gap: "8px" }}>
          <input type="checkbox" checked={autoEmbed} onChange={(e) => setAutoEmbed(e.target.checked)} disabled={saving} />
          <span>Auto-embed</span>
        </label>
        <button className="btn btn--primary" onClick={save} disabled={saving}>
          {saving ? "Saving…" : "Save changes"}
        </button>
        {msg && <span className="admin__error">{msg}</span>}
      </div>
    </div>
  );
}

function UrlIngestForm({
  workspaceId,
  isAdmin,
  onIngested,
}: {
  workspaceId: string;
  isAdmin: boolean;
  onIngested: () => void;
}) {
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [scope, setScope] = useState<KnowledgeScope>("user");
  const [autoEmbed, setAutoEmbed] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [msgKind, setMsgKind] = useState<"ok" | "dupe" | "error">("ok");

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitting || !url.trim()) return;
    setSubmitting(true);
    setMsg(null);
    try {
      const res = await ingestUrlKnowledge(workspaceId, {
        url: url.trim(),
        title: title.trim() || undefined,
        scope_type: scope,
        auto_embed: autoEmbed,
      });
      const pages =
        res.page_count != null ? ` · ${res.page_count} pages` : "";
      if (res.duplicate) {
        setMsgKind("dupe");
        setMsg(
          `Already ingested — linked to the existing source. “${res.title}” (${res.content_length} chars${pages}).`,
        );
      } else {
        setMsgKind("ok");
        setMsg(
          `Ingested “${res.title}” · ${res.content_length} chars${pages}` +
            (res.embedded
              ? " · embedded"
              : autoEmbed
                ? " · embed skipped"
                : "") +
            ` → memory ${res.memory_entry_id.slice(0, 8)}`,
        );
      }
      setUrl("");
      setTitle("");
      onIngested();
    } catch (err) {
      setMsgKind("error");
      setMsg(err instanceof Error ? err.message : "Failed to ingest URL");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section className="admin__section">
      <form className="admin__form" onSubmit={submit}>
        <h3>Ingest from URL</h3>
        <p className="admin__hint">
          Fetches a single public URL, including HTML, plain text, and readable
          PDFs (e.g. arxiv.org/pdf links), extracts the readable content, and
          stores it as workspace knowledge. No crawling.
        </p>
        <div className="admin__form-row">
          <label className="admin__field-wide">
            <span>URL</span>
            <input
              className="cora-input"
              type="url"
              inputMode="url"
              placeholder="https://example.com/article"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              disabled={submitting}
            />
          </label>
          <label>
            <span>Scope</span>
            <select
              className="cora-input"
              value={scope}
              onChange={(e) => setScope(e.target.value as KnowledgeScope)}
              disabled={submitting}
            >
              <option value="user">user</option>
              <option value="global" disabled={!isAdmin}>
                global{!isAdmin ? " (admin)" : ""}
              </option>
              <option value="system" disabled={!isAdmin}>
                system{!isAdmin ? " (admin)" : ""}
              </option>
            </select>
          </label>
        </div>
        <label className="admin__field-wide">
          <span>Title (optional — derived from the page if blank)</span>
          <input
            className="cora-input"
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            disabled={submitting}
          />
        </label>
        <div className="admin__form-row">
          <label
            style={{ flexDirection: "row", alignItems: "center", gap: "8px" }}
          >
            <input
              type="checkbox"
              checked={autoEmbed}
              onChange={(e) => setAutoEmbed(e.target.checked)}
              disabled={submitting}
            />
            <span>Auto-embed after ingest</span>
          </label>
          <button
            type="submit"
            className="btn btn--primary"
            disabled={submitting || !url.trim()}
          >
            {submitting ? "Ingesting…" : "Ingest URL"}
          </button>
          {msg && (
            <span
              className={
                msgKind === "error" ? "admin__error" : "admin__hint"
              }
            >
              {msgKind === "dupe" ? "⚠ " : ""}
              {msg}
            </span>
          )}
        </div>
      </form>
    </section>
  );
}

function UploadEntryForm({
  workspaceId,
  isAdmin,
  onUploaded,
}: {
  workspaceId: string;
  isAdmin: boolean;
  onUploaded: () => void;
}) {
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [scope, setScope] = useState<KnowledgeScope>("user");
  const [tags, setTags] = useState("");
  const [importance, setImportance] = useState(3);
  const [autoEmbed, setAutoEmbed] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitting || !selectedFile) return;
    setSubmitting(true);
    setMsg(null);
    try {
      const entry = await uploadKnowledgeFile(workspaceId, {
        file: selectedFile,
        scope_type: scope,
        importance,
        auto_embed: autoEmbed,
        tags,
      });
      setMsg(
        `Uploaded ${selectedFile.name} → memory ${entry.id.slice(0, 8)}` +
          (entry.embedded
            ? " · embedded"
            : autoEmbed
              ? " · embed skipped"
              : "") +
          (entry.duplicate_warning
            ? " · ⚠ duplicate content — linked to existing source"
            : ""),
      );
      setSelectedFile(null);
      onUploaded();
    } catch (err) {
      setMsg(err instanceof Error ? err.message : "Failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section className="admin__section">
      <form className="admin__form" onSubmit={submit}>
        <h3>Upload file</h3>

        <FileDropzone
          value={selectedFile}
          onChange={setSelectedFile}
          accept=".txt,.md,.markdown,.json"
          maxSizeBytes={512 * 1024}
          label="Upload knowledge file"
          helperText=".txt, .md, .markdown, .json · max 512 KiB"
          disabled={submitting}
        />

        <div className="admin__form-row">
          <label>
            <span>Scope</span>
            <select
              className="cora-input"
              value={scope}
              onChange={(e) => setScope(e.target.value as KnowledgeScope)}
              disabled={submitting}
            >
              <option value="user">user</option>
              <option value="global" disabled={!isAdmin}>
                global{!isAdmin ? " (admin)" : ""}
              </option>
              <option value="system" disabled={!isAdmin}>
                system{!isAdmin ? " (admin)" : ""}
              </option>
            </select>
          </label>
          <label>
            <span>Importance</span>
            <input
              className="cora-input"
              type="number"
              min={1}
              max={5}
              value={importance}
              onChange={(e) => setImportance(Number(e.target.value))}
              disabled={submitting}
            />
          </label>
        </div>
        <label className="admin__field-wide">
          <span>Tags (comma-separated)</span>
          <input
            className="cora-input"
            type="text"
            value={tags}
            onChange={(e) => setTags(e.target.value)}
            disabled={submitting}
          />
        </label>
        <div className="admin__form-row">
          <label
            style={{ flexDirection: "row", alignItems: "center", gap: "8px" }}
          >
            <input
              type="checkbox"
              checked={autoEmbed}
              onChange={(e) => setAutoEmbed(e.target.checked)}
              disabled={submitting}
            />
            <span>Auto-embed after upload</span>
          </label>
          <button
            type="submit"
            className="btn btn--primary"
            disabled={submitting || !selectedFile}
          >
            {submitting ? "Uploading…" : "Upload"}
          </button>
          {msg && <span className="admin__hint">{msg}</span>}
        </div>
      </form>
    </section>
  );
}

function SingleEntryForm({
  workspaceId,
  isAdmin,
  onCreated,
}: {
  workspaceId: string;
  isAdmin: boolean;
  onCreated: () => void;
}) {
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [tags, setTags] = useState("");
  const [scope, setScope] = useState<KnowledgeScope>("user");
  const [importance, setImportance] = useState(3);
  const [autoEmbed, setAutoEmbed] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setMsg(null);
    try {
      const entry = await ingestKnowledge(workspaceId, {
        title: title.trim(),
        content,
        tags: tags
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
        scope_type: scope,
        importance,
        auto_embed: autoEmbed,
      });
      setMsg(
        `Added ${entry.id.slice(0, 8)}` +
          (entry.embedded ? " · embedded" : autoEmbed ? " · embed skipped" : "") +
          (entry.duplicate_warning
            ? " · ⚠ duplicate content — linked to existing source"
            : ""),
      );
      setTitle("");
      setContent("");
      setTags("");
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
        <h3>Add knowledge entry</h3>
        <div className="admin__form-row">
          <label className="admin__field-wide">
            <span>Title</span>
            <input
              className="cora-input"
              type="text"
              required
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              disabled={submitting}
            />
          </label>
          <label>
            <span>Scope</span>
            <select
              className="cora-input"
              value={scope}
              onChange={(e) => setScope(e.target.value as KnowledgeScope)}
              disabled={submitting}
            >
              {SCOPES.map((s) => (
                <option
                  key={s}
                  value={s}
                  disabled={s !== "user" && !isAdmin}
                >
                  {s}
                  {s !== "user" && !isAdmin ? " (admin)" : ""}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Importance</span>
            <input
              className="cora-input"
              type="number"
              min={1}
              max={5}
              value={importance}
              onChange={(e) => setImportance(Number(e.target.value))}
              disabled={submitting}
            />
          </label>
        </div>
        <label className="admin__field-wide">
          <span>Content</span>
          <textarea
            className="cora-input"
            rows={5}
            required
            value={content}
            onChange={(e) => setContent(e.target.value)}
            disabled={submitting}
          />
        </label>
        <label className="admin__field-wide">
          <span>Tags (comma-separated)</span>
          <input
            className="cora-input"
            type="text"
            value={tags}
            onChange={(e) => setTags(e.target.value)}
            disabled={submitting}
          />
        </label>
        <div className="admin__form-row">
          <label style={{ flexDirection: "row", alignItems: "center", gap: "8px" }}>
            <input
              type="checkbox"
              checked={autoEmbed}
              onChange={(e) => setAutoEmbed(e.target.checked)}
              disabled={submitting}
            />
            <span>Auto-embed after insert</span>
          </label>
          <button
            type="submit"
            className="btn btn--primary"
            disabled={submitting}
          >
            {submitting ? "…" : "Add"}
          </button>
          {msg && <span className="admin__hint">{msg}</span>}
        </div>
      </form>
    </section>
  );
}

function BulkEntryForm({
  workspaceId,
  isAdmin,
  onCreated,
}: {
  workspaceId: string;
  isAdmin: boolean;
  onCreated: () => void;
}) {
  const [text, setText] = useState("");
  const [scope, setScope] = useState<KnowledgeScope>("user");
  const [autoEmbed, setAutoEmbed] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setMsg(null);
    try {
      const parsed = JSON.parse(text);
      if (!Array.isArray(parsed)) {
        throw new Error("Bulk input must be a JSON array");
      }
      const entries = parsed.map((p: Record<string, unknown>) => ({
        title: String(p.title ?? ""),
        content: String(p.content ?? ""),
        tags: Array.isArray(p.tags) ? (p.tags as string[]) : undefined,
        scope_type: (p.scope_type as KnowledgeScope) ?? scope,
        importance: typeof p.importance === "number" ? (p.importance as number) : 3,
        auto_embed: typeof p.auto_embed === "boolean" ? (p.auto_embed as boolean) : autoEmbed,
        type: typeof p.type === "string" ? (p.type as string) : undefined,
      }));
      if (entries.length === 0) {
        throw new Error("No entries to insert");
      }
      const res = await ingestKnowledgeBulk(workspaceId, entries);
      setMsg(
        `Created ${res.created} · embedded ${res.embedded}` +
          (res.skipped ? ` · skipped ${res.skipped}` : ""),
      );
      setText("");
      onCreated();
    } catch (err) {
      setMsg(err instanceof Error ? err.message : "Failed");
    } finally {
      setSubmitting(false);
    }
  };

  const placeholder = `[
  {"title": "Postgres URL", "content": "postgres://...", "tags": ["infra"]},
  {"title": "n8n webhook", "content": "https://...", "scope_type": "global"}
]`;

  return (
    <section className="admin__section">
      <form className="admin__form" onSubmit={submit}>
        <h3>Bulk paste (JSON array)</h3>
        <p className="admin__hint">
          Paste a JSON array of objects with at least <span className="mono">title</span>{" "}
          and <span className="mono">content</span>. Other fields fall back to
          the defaults below.
        </p>
        <label className="admin__field-wide">
          <span>JSON</span>
          <textarea
            className="cora-input"
            rows={10}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={placeholder}
            disabled={submitting}
          />
        </label>
        <div className="admin__form-row">
          <label>
            <span>Default scope</span>
            <select
              className="cora-input"
              value={scope}
              onChange={(e) => setScope(e.target.value as KnowledgeScope)}
              disabled={submitting}
            >
              {SCOPES.map((s) => (
                <option
                  key={s}
                  value={s}
                  disabled={s !== "user" && !isAdmin}
                >
                  {s}
                  {s !== "user" && !isAdmin ? " (admin)" : ""}
                </option>
              ))}
            </select>
          </label>
          <label style={{ flexDirection: "row", alignItems: "center", gap: "8px" }}>
            <input
              type="checkbox"
              checked={autoEmbed}
              onChange={(e) => setAutoEmbed(e.target.checked)}
              disabled={submitting}
            />
            <span>Auto-embed</span>
          </label>
          <button
            type="submit"
            className="btn btn--primary"
            disabled={submitting || !text.trim()}
          >
            {submitting ? "…" : "Insert bulk"}
          </button>
          {msg && <span className="admin__hint">{msg}</span>}
        </div>
      </form>
    </section>
  );
}

function KnowledgeSourcesSection({
  workspaceId,
}: {
  workspaceId: string;
}) {
  const [sources, setSources] = useState<KnowledgeSource[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [selected, setSelected] = useState<KnowledgeSourceDetail | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  // Per-source refresh state: checking | updated | unchanged | failed.
  const [refreshState, setRefreshState] = useState<
    Record<string, "checking" | "updated" | "unchanged" | "failed">
  >({});

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setSources(await listKnowledgeSources(workspaceId, includeArchived));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setLoading(false);
    }
  }, [workspaceId, includeArchived]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const open = useCallback(async (id: string) => {
    setLoadingDetail(true);
    try {
      setSelected(await getKnowledgeSource(id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setLoadingDetail(false);
    }
  }, []);

  const remove = useCallback(
    async (id: string) => {
      if (!window.confirm("Delete this source? Linked memories keep their content.")) {
        return;
      }
      try {
        await deleteKnowledgeSource(id);
        if (selected?.id === id) setSelected(null);
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed");
      }
    },
    [selected, refresh],
  );

  const doRefreshSource = useCallback(
    async (id: string) => {
      setRefreshState((s) => ({ ...s, [id]: "checking" }));
      try {
        const res = await refreshKnowledgeSource(id);
        setRefreshState((s) => ({ ...s, [id]: res.status }));
        // Reload the list so updated content_hash + metadata appear.
        await refresh();
      } catch (err) {
        setRefreshState((s) => ({ ...s, [id]: "failed" }));
        setError(err instanceof Error ? err.message : "Refresh failed");
      }
    },
    [refresh],
  );

  return (
    <section className="admin__section">
      <div className="admin__section-head">
        <h2>Knowledge sources ({sources.length})</h2>
        <div className="admin__inline">
          <label className="admin__hint">
            <input
              type="checkbox"
              checked={includeArchived}
              onChange={(e) => setIncludeArchived(e.target.checked)}
            />{" "}
            include archived
          </label>
          <button
            className="btn btn--ghost btn--sm"
            onClick={refresh}
            disabled={loading}
          >
            ↻ Refresh
          </button>
        </div>
      </div>

      {error && <div className="admin__error">{error}</div>}

      <table className="admin__table">
        <thead>
          <tr>
            <th>Title</th>
            <th>Type</th>
            <th>Linked</th>
            <th>Filename / URL</th>
            <th>Status</th>
            <th>Created</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {loading && sources.length === 0 && (
            <tr>
              <td colSpan={7} className="muted">
                Loading…
              </td>
            </tr>
          )}
          {sources.map((s) => (
            <tr
              key={s.id}
              className={`trace-row${selected?.id === s.id ? " admin__row--selected" : ""}`}
              onClick={() => open(s.id)}
            >
              <td>
                <div>{s.title}</div>
                {s.source_type === "url" &&
                  freshnessLine(s.metadata) && (
                    <div className="source-freshness">
                      {freshnessLine(s.metadata)}
                    </div>
                  )}
              </td>
              <td>
                <span className="scope-chip scope-chip--subagent">
                  {s.source_type}
                </span>
              </td>
              <td className="muted">{s.linked_memory_count}</td>
              <td className="mono muted">
                {s.original_filename || s.source_url || "—"}
              </td>
              <td>
                <span
                  className={`status-chip status-chip--${s.status === "active" ? "active" : "archived"}`}
                >
                  {s.status}
                </span>
              </td>
              <td className="muted">
                {new Date(s.created_at).toLocaleString()}
              </td>
              <td>
                {s.source_type === "url" && (
                  <>
                    <button
                      className="btn btn--ghost btn--sm"
                      disabled={refreshState[s.id] === "checking"}
                      onClick={(e) => {
                        e.stopPropagation();
                        doRefreshSource(s.id);
                      }}
                    >
                      {refreshState[s.id] === "checking"
                        ? "Checking…"
                        : "Refresh"}
                    </button>
                    {refreshState[s.id] &&
                      refreshState[s.id] !== "checking" && (
                        <span
                          className={`refresh-chip refresh-chip--${refreshState[s.id]}`}
                        >
                          {REFRESH_LABEL[refreshState[s.id]]}
                        </span>
                      )}{" "}
                  </>
                )}
                <button
                  className="btn btn--ghost btn--sm"
                  onClick={(e) => {
                    e.stopPropagation();
                    remove(s.id);
                  }}
                >
                  Delete
                </button>
              </td>
            </tr>
          ))}
          {!loading && sources.length === 0 && (
            <tr>
              <td colSpan={7} className="muted">
                No knowledge sources found for this workspace.
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {selected && (
        <div className="admin__form" style={{ marginTop: "12px" }}>
          <h3>
            {selected.title}{" "}
            <span className="mono muted">{selected.id.slice(0, 8)}</span>
          </h3>
          {loadingDetail && <div className="admin__hint">Loading…</div>}
          <div className="admin__hint">
            <span className="scope-chip scope-chip--subagent">
              {selected.source_type}
            </span>
            {" · "}status: {selected.status}
            {" · "}linked memories: {selected.linked_memory_count}
            {selected.content_hash && (
              <>
                {" · "}hash:{" "}
                <span className="mono muted">
                  {selected.content_hash.slice(0, 12)}…
                </span>
              </>
            )}
          </div>
          {selected.source_url && (
            <div className="admin__hint">
              URL: <span className="mono">{selected.source_url}</span>
            </div>
          )}
          {selected.description && (
            <div className="admin__hint">{selected.description}</div>
          )}
          {selected.content && (
            <>
              <h4 className="admin__vt-h3">Content</h4>
              <pre className="trace-json">{selected.content}</pre>
            </>
          )}
          {selected.linked_memories.length > 0 && (
            <>
              <h4 className="admin__vt-h3">Linked memories</h4>
              <ul className="admin__preview-list">
                {selected.linked_memories.map((m) => (
                  <li key={m.id}>
                    <div className="admin__preview-row">
                      <strong>{m.title}</strong>
                      <span className={`scope-chip scope-chip--${m.scope_type}`}>
                        {m.scope_type}
                      </span>
                      <span
                        className={`status-chip status-chip--${m.embedded ? "active" : "draft"}`}
                      >
                        {m.embedded ? "embedded" : "not embedded"}
                      </span>
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </section>
  );
}
