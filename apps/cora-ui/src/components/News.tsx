// DEPRECATED (News Path Reconciliation v0.1, 2026-05-28): the registration-based
// news_sources path is superseded by the unified knowledge news ingestion
// (Knowledge tab → "Ingest news feed" → POST /workspaces/{id}/knowledge/news →
// knowledge_sources(news_feed/news_article) + memory_entries). This component is
// no longer wired into navigation. Kept for reference only; do not re-add to the
// Admin Console — extend the Knowledge news form instead.
import { useCallback, useEffect, useState } from "react";
import {
  createNewsSource,
  deleteNewsSource,
  fetchNewsSource,
  listNewsArticles,
  listNewsSources,
  updateNewsSource,
} from "../api";
import type { NewsArticle, NewsFetchResult, NewsSource } from "../types";

interface Props {
  workspaceId: string | null;
  isAdmin: boolean;
}

export function News({ workspaceId, isAdmin }: Props) {
  const [sources, setSources] = useState<NewsSource[]>([]);
  const [articles, setArticles] = useState<NewsArticle[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const [name, setName] = useState("");
  const [feedUrl, setFeedUrl] = useState("");
  const [category, setCategory] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const refresh = useCallback(async () => {
    if (!workspaceId) return;
    setLoading(true);
    setError(null);
    try {
      const [s, a] = await Promise.all([
        listNewsSources(workspaceId),
        listNewsArticles(workspaceId),
      ]);
      setSources(s);
      setArticles(a);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load");
    } finally {
      setLoading(false);
    }
  }, [workspaceId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function handleRegister(e: React.FormEvent) {
    e.preventDefault();
    if (!workspaceId || submitting) return;
    setSubmitting(true);
    setError(null);
    setNotice(null);
    try {
      const src = await createNewsSource(workspaceId, {
        name: name.trim(),
        feed_url: feedUrl.trim(),
        category: category.trim() || null,
      });
      setNotice(`Registered "${src.name}".`);
      setName("");
      setFeedUrl("");
      setCategory("");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to register");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleFetch(src: NewsSource) {
    if (!workspaceId) return;
    setBusyId(src.id);
    setError(null);
    setNotice(null);
    try {
      const res: NewsFetchResult = await fetchNewsSource(workspaceId, src.id);
      setNotice(
        `Fetched "${res.name}": ${res.ingested} new, ${res.duplicates} duplicate(s), ` +
          `${res.embedded} embedded (saw ${res.entries_seen}).`,
      );
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Fetch failed");
    } finally {
      setBusyId(null);
    }
  }

  async function handleToggle(src: NewsSource) {
    if (!workspaceId) return;
    setBusyId(src.id);
    setError(null);
    try {
      await updateNewsSource(workspaceId, src.id, !src.enabled);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Update failed");
    } finally {
      setBusyId(null);
    }
  }

  async function handleDelete(src: NewsSource) {
    if (!workspaceId) return;
    if (
      !window.confirm(
        `Remove "${src.name}" from the registry? Already-ingested articles are kept.`,
      )
    )
      return;
    setBusyId(src.id);
    setError(null);
    try {
      await deleteNewsSource(workspaceId, src.id);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed");
    } finally {
      setBusyId(null);
    }
  }

  if (!workspaceId) {
    return (
      <main className="admin">
        <header className="admin__header">
          <h1>News Sources</h1>
        </header>
        <div className="admin__hint">Select a workspace in the sidebar.</div>
      </main>
    );
  }

  return (
    <main className="admin">
      <header className="admin__header">
        <h1>News Sources</h1>
        <p className="admin__subtitle">
          RSS/Atom feeds PULSE draws on. Fetching ingests new articles as{" "}
          <span className="mono">news_article</span> knowledge + embedded memory,
          so PULSE retrieves and cites them.
        </p>
      </header>

      {error && <div className="admin__error">{error}</div>}
      {notice && <div className="admin__hint">{notice}</div>}

      {isAdmin && (
        <section className="admin__section">
          <form className="admin__form" onSubmit={handleRegister}>
            <h3>Register a feed</h3>
            <div className="admin__form-row">
              <label className="admin__field-wide">
                <span>Name</span>
                <input
                  className="cora-input"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="e.g. Ars Technica"
                  disabled={submitting}
                  required
                />
              </label>
              <label>
                <span>Category (optional)</span>
                <input
                  className="cora-input"
                  value={category}
                  onChange={(e) => setCategory(e.target.value)}
                  placeholder="e.g. tech"
                  disabled={submitting}
                />
              </label>
            </div>
            <label className="admin__field-wide">
              <span>Feed URL</span>
              <input
                className="cora-input"
                value={feedUrl}
                onChange={(e) => setFeedUrl(e.target.value)}
                placeholder="https://example.com/rss"
                disabled={submitting}
                required
              />
            </label>
            <div className="admin__form-row">
              <button className="btn btn--primary" disabled={submitting}>
                {submitting ? "Registering…" : "Register feed"}
              </button>
            </div>
          </form>
        </section>
      )}

      <section className="admin__section">
        <div className="admin__section-head">
          <h2>Registered sources ({sources.length})</h2>
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
              <th>Feed</th>
              <th>Last fetch</th>
              <th>Articles</th>
              {isAdmin && <th>Actions</th>}
            </tr>
          </thead>
          <tbody>
            {sources.map((src) => (
              <tr key={src.id}>
                <td>
                  {src.name}{" "}
                  {src.category && (
                    <span className="scope-chip">{src.category}</span>
                  )}
                  {!src.enabled && (
                    <span className="status-chip status-chip--archived">
                      disabled
                    </span>
                  )}
                </td>
                <td className="mono muted ellipsis" title={src.feed_url}>
                  {src.feed_url}
                </td>
                <td>
                  {src.last_status ? (
                    <span
                      className={`status-chip status-chip--${
                        src.last_status === "ok" ? "active" : "error"
                      }`}
                      title={src.last_error ?? ""}
                    >
                      {src.last_status}
                    </span>
                  ) : (
                    <span className="muted">never</span>
                  )}{" "}
                  {src.last_fetched_at && (
                    <span className="muted">
                      {new Date(src.last_fetched_at).toLocaleString()}
                    </span>
                  )}
                </td>
                <td className="mono">
                  {src.last_article_count} / {src.total_article_count}
                </td>
                {isAdmin && (
                  <td className="row-actions">
                    <button
                      className="btn btn--primary btn--sm"
                      disabled={busyId === src.id || !src.enabled}
                      onClick={() => handleFetch(src)}
                    >
                      {busyId === src.id ? "…" : "Fetch"}
                    </button>
                    <button
                      className="btn btn--ghost btn--sm"
                      disabled={busyId === src.id}
                      onClick={() => handleToggle(src)}
                    >
                      {src.enabled ? "Disable" : "Enable"}
                    </button>
                    <button
                      className="btn btn--ghost btn--sm"
                      disabled={busyId === src.id}
                      onClick={() => handleDelete(src)}
                    >
                      Delete
                    </button>
                  </td>
                )}
              </tr>
            ))}
            {!loading && sources.length === 0 && (
              <tr>
                <td colSpan={isAdmin ? 5 : 4} className="muted">
                  No news sources registered yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      <section className="admin__section">
        <div className="admin__section-head">
          <h2>Ingested articles ({articles.length})</h2>
        </div>
        <table className="admin__table">
          <thead>
            <tr>
              <th>Title</th>
              <th>Link</th>
              <th>Ingested</th>
            </tr>
          </thead>
          <tbody>
            {articles.map((a) => (
              <tr key={a.id}>
                <td>{a.title}</td>
                <td className="mono muted ellipsis">
                  {a.source_url ? (
                    <a href={a.source_url} target="_blank" rel="noreferrer">
                      {a.source_url}
                    </a>
                  ) : (
                    "—"
                  )}
                </td>
                <td className="muted">
                  {new Date(a.created_at).toLocaleString()}
                </td>
              </tr>
            ))}
            {articles.length === 0 && (
              <tr>
                <td colSpan={3} className="muted">
                  No articles ingested yet. Register a feed and click Fetch.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>
    </main>
  );
}
