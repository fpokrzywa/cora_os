import { useCallback, useEffect, useState } from "react";
import { getNewsBriefing } from "../api";
import type { NewsBriefingResponse } from "../types";

const WINDOWS: { label: string; hours: number }[] = [
  { label: "Last 24 hours", hours: 24 },
  { label: "Last 3 days", hours: 72 },
  { label: "Last 7 days", hours: 168 },
  { label: "Last 30 days", hours: 720 },
];

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function statusChip(s: string | null, bodyFetched: boolean) {
  if (bodyFetched || s === "success")
    return <span className="status-chip status-chip--active">body fetched</span>;
  if (s === "fallback")
    return <span className="status-chip status-chip--draft">fallback</span>;
  if (s === "failed")
    return <span className="status-chip status-chip--archived">failed</span>;
  return <span className="status-chip">summary</span>;
}

export function NewsBriefing({ workspaceId }: { workspaceId: string }) {
  const [hours, setHours] = useState(168);
  const [maxArticles, setMaxArticles] = useState(25);
  const [sourceFilter, setSourceFilter] = useState("");
  const [data, setData] = useState<NewsBriefingResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [summarizing, setSummarizing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (includeSummary: boolean) => {
      if (includeSummary) setSummarizing(true);
      else setLoading(true);
      setError(null);
      try {
        const res = await getNewsBriefing(workspaceId, {
          since_hours: hours,
          max_articles: maxArticles,
          source_name: sourceFilter || undefined,
          include_summary: includeSummary,
        });
        setData(res);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load briefing");
      } finally {
        setLoading(false);
        setSummarizing(false);
      }
    },
    [workspaceId, hours, maxArticles, sourceFilter],
  );

  useEffect(() => {
    load(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId]);

  const sourceNames = data?.source_names ?? [];

  return (
    <section className="admin__section">
      <div className="admin__section-head">
        <h2>News Briefing</h2>
        <button
          className="btn btn--ghost btn--sm"
          onClick={() => load(false)}
          disabled={loading || summarizing}
        >
          ↻ Refresh
        </button>
      </div>
      <p className="admin__hint">
        A read view + PULSE briefing over already-ingested news (unified
        knowledge path). No live web fetch; reasons only over stored articles.
      </p>

      <div className="admin__form-row">
        <label>
          <span>Time window</span>
          <select
            className="cora-input"
            value={hours}
            onChange={(e) => setHours(Number(e.target.value))}
            disabled={loading || summarizing}
          >
            {WINDOWS.map((w) => (
              <option key={w.hours} value={w.hours}>
                {w.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Max articles</span>
          <input
            className="cora-input"
            type="number"
            min={1}
            max={200}
            value={maxArticles}
            onChange={(e) => setMaxArticles(Number(e.target.value))}
            disabled={loading || summarizing}
          />
        </label>
        {sourceNames.length > 0 && (
          <label>
            <span>Source</span>
            <select
              className="cora-input"
              value={sourceFilter}
              onChange={(e) => setSourceFilter(e.target.value)}
              disabled={loading || summarizing}
            >
              <option value="">All sources</option>
              {sourceNames.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
        )}
        <button
          className="btn btn--ghost"
          onClick={() => load(false)}
          disabled={loading || summarizing}
        >
          {loading ? "Loading…" : "Load articles"}
        </button>
        <button
          className="btn btn--primary"
          onClick={() => load(true)}
          disabled={loading || summarizing || (data?.total_articles ?? 0) === 0}
        >
          {summarizing ? "Generating briefing…" : "Generate PULSE Summary"}
        </button>
      </div>

      {error && <div className="admin__error">{error}</div>}
      {loading && !data && (
        <div className="admin__hint">Loading articles…</div>
      )}

      {data && (
        <>
          <div className="admin-console__overview" style={{ marginTop: "12px" }}>
            <div className="admin-console__card">
              <span className="admin-console__card-title">{data.total_articles}</span>
              <span className="admin-console__card-blurb">Total articles</span>
            </div>
            <div className="admin-console__card">
              <span className="admin-console__card-title">{data.feeds_represented}</span>
              <span className="admin-console__card-blurb">Feeds represented</span>
            </div>
            <div className="admin-console__card">
              <span className="admin-console__card-title">
                {data.article_body_fetch_success_count}
              </span>
              <span className="admin-console__card-blurb">Full bodies fetched</span>
            </div>
            <div className="admin-console__card">
              <span className="admin-console__card-title">
                {data.chunked_article_count}
              </span>
              <span className="admin-console__card-blurb">Chunked articles</span>
            </div>
          </div>

          {summarizing && (
            <div className="admin__hint">Generating briefing…</div>
          )}
          {data.summary && (
            <div className="agent-test-result-card" style={{ marginTop: "16px" }}>
              <h3>PULSE Briefing</h3>
              <p className="admin__hint">
                Based only on ingested sources — not live web browsing.
              </p>
              <pre className="trace-json" style={{ whiteSpace: "pre-wrap" }}>
                {data.summary}
              </pre>
            </div>
          )}
          {data.include_summary && !data.summary && !summarizing && (
            <div className="admin__hint">
              Summary unavailable (model not configured or no articles).
            </div>
          )}

          <h3 className="admin__vt-h3" style={{ marginTop: "16px" }}>
            Articles ({data.articles.length})
          </h3>
          {data.articles.length === 0 ? (
            <div className="admin__hint">No articles found for this window.</div>
          ) : (
            <table className="admin__table">
              <thead>
                <tr>
                  <th>Title</th>
                  <th>Source</th>
                  <th>Published / Ingested</th>
                  <th>Body</th>
                  <th>Chunks</th>
                  <th>Link</th>
                </tr>
              </thead>
              <tbody>
                {data.articles.map((a) => (
                  <tr key={a.source_id}>
                    <td>
                      <div>{a.title}</div>
                      <div className="muted" style={{ fontSize: "11px" }}>
                        {a.short_preview}
                      </div>
                    </td>
                    <td className="muted">{a.source_name || "—"}</td>
                    <td className="muted">
                      {a.published_at
                        ? fmtTime(a.published_at)
                        : fmtTime(a.created_at)}
                    </td>
                    <td>{statusChip(a.article_fetch_status, a.article_body_fetched)}</td>
                    <td className="muted">
                      {a.chunk_count > 0 ? (
                        <span className="status-chip status-chip--active">
                          {a.embedded_chunk_count}/{a.chunk_count} chunked
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td>
                      {a.source_url ? (
                        <a
                          href={a.source_url}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          open
                        </a>
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </section>
  );
}
