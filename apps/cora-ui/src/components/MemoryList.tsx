import { useCallback, useEffect, useState } from "react";
import {
  embedMissingMemory,
  getEmbeddingsStatus,
  listMemory,
  rebuildMissingChunks,
  searchMemory,
  semanticSearchMemory,
} from "../api";
import type {
  EmbeddingsStatus,
  MemoryEntry,
  MemorySearchResult,
  SemanticSearchResult,
} from "../types";

interface Props {
  onSelect: (id: string) => void;
  selectedId: string | null;
  isAdmin: boolean;
}

type SearchMode = "keyword" | "semantic";

interface ListItem {
  id: string;
  title: string;
  type: string;
  preview: string;
  tags: string[];
  importance: number;
  created_at: string;
  similarity?: number;
}

function formatRelative(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const min = Math.round(diff / 60000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.round(hr / 24);
  return `${day}d ago`;
}

function entryToItem(e: MemoryEntry): ListItem {
  const preview =
    e.content.length > 180 ? e.content.slice(0, 180).trimEnd() + "…" : e.content;
  return {
    id: e.id,
    title: e.title,
    type: e.type,
    preview,
    tags: e.tags,
    importance: e.importance,
    created_at: e.created_at,
  };
}

function searchToItem(r: MemorySearchResult): ListItem {
  return {
    id: r.id,
    title: r.title,
    type: r.type,
    preview: r.content_preview,
    tags: r.tags,
    importance: r.importance,
    created_at: r.created_at,
  };
}

function semanticToItem(r: SemanticSearchResult): ListItem {
  return {
    id: r.id,
    title: r.title,
    type: r.type,
    preview: r.content_preview,
    tags: r.tags,
    importance: r.importance,
    created_at: r.created_at,
    similarity: r.similarity,
  };
}

function ImportanceDots({ value }: { value: number }) {
  const safe = Math.max(0, Math.min(5, value));
  return (
    <span className="importance" title={`Importance ${safe}/5`}>
      {Array.from({ length: 5 }, (_, i) => (
        <span
          key={i}
          className={`importance__dot${i < safe ? " importance__dot--on" : ""}`}
        />
      ))}
    </span>
  );
}

export function MemoryList({ onSelect, selectedId, isAdmin }: Props) {
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<SearchMode>("keyword");
  const [items, setItems] = useState<ListItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [semanticUnavailable, setSemanticUnavailable] = useState<string | null>(
    null,
  );
  const [status, setStatus] = useState<EmbeddingsStatus | null>(null);
  const [embedding, setEmbedding] = useState(false);
  const [embedMsg, setEmbedMsg] = useState<string | null>(null);
  const [chunking, setChunking] = useState(false);

  const refreshStatus = useCallback(async () => {
    try {
      setStatus(await getEmbeddingsStatus());
    } catch {
      setStatus(null);
    }
  }, []);

  useEffect(() => {
    refreshStatus();
  }, [refreshStatus]);

  const runEmbedMissing = useCallback(async () => {
    if (embedding) return;
    setEmbedding(true);
    setEmbedMsg(null);
    try {
      const res = await embedMissingMemory(200);
      const d = res.detail as Record<string, unknown>;
      const embedded = (d.embedded as number) ?? 0;
      const skipped = (d.skipped as number) ?? 0;
      const errors = (d.errors as number) ?? 0;
      if (res.status === "skipped") {
        setEmbedMsg(`Skipped: ${(d.reason as string) ?? "not configured"}`);
      } else if (embedded === 0 && skipped === 0) {
        setEmbedMsg("No embedded memories found to add.");
      } else {
        setEmbedMsg(
          `Embedded ${embedded} memor${embedded === 1 ? "y" : "ies"}` +
            (skipped ? ` · skipped ${skipped}` : "") +
            (errors ? ` · errors ${errors}` : ""),
        );
      }
      await refreshStatus();
    } catch (err) {
      setEmbedMsg(err instanceof Error ? err.message : "Failed");
    } finally {
      setEmbedding(false);
    }
  }, [embedding, refreshStatus]);

  const runRebuildChunks = useCallback(async () => {
    if (chunking) return;
    setChunking(true);
    setEmbedMsg(null);
    try {
      const res = await rebuildMissingChunks(50);
      const d = (res.detail ?? {}) as Record<string, unknown>;
      if (res.status === "skipped") {
        setEmbedMsg(`Chunks skipped: ${(d.reason as string) ?? "not configured"}`);
      } else {
        const rebuilt = (d.rebuilt as number) ?? 0;
        const chunks = (d.chunks_created as number) ?? 0;
        const embedded = (d.embedded_count as number) ?? 0;
        setEmbedMsg(
          rebuilt === 0
            ? "No entries needed chunking."
            : `Chunked ${rebuilt} entr${rebuilt === 1 ? "y" : "ies"} · ` +
              `${chunks} chunks · ${embedded} embedded`,
        );
      }
      await refreshStatus();
    } catch (err) {
      setEmbedMsg(err instanceof Error ? err.message : "Failed");
    } finally {
      setChunking(false);
    }
  }, [chunking, refreshStatus]);

  const load = useCallback(
    async (q: string, m: SearchMode) => {
      setLoading(true);
      setError(null);
      try {
        if (!q.trim()) {
          setSemanticUnavailable(null);
          const rows = await listMemory();
          setItems(rows.map(entryToItem));
          return;
        }
        if (m === "semantic") {
          const res = await semanticSearchMemory(q.trim());
          if (res.semantic_unavailable) {
            setSemanticUnavailable(
              res.reason || "Semantic search is not available; using keyword.",
            );
            const rows = await searchMemory(q.trim());
            setItems(rows.map(searchToItem));
            return;
          }
          setSemanticUnavailable(null);
          setItems(res.results.map(semanticToItem));
          return;
        }
        setSemanticUnavailable(null);
        const rows = await searchMemory(q.trim());
        setItems(rows.map(searchToItem));
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load memory");
        setItems([]);
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    load("", mode);
  }, [load, mode]);

  // Debounce search input
  useEffect(() => {
    const handle = window.setTimeout(() => {
      load(query, mode);
    }, 250);
    return () => window.clearTimeout(handle);
  }, [query, mode, load]);

  const semanticReady =
    status?.pgvector_available && status?.embedding_configured;
  const statusBadge = status ? (
    <span
      className={`semantic-badge semantic-badge--${semanticReady ? "ok" : "off"}`}
      title={
        status
          ? `pgvector: ${status.pgvector_available} · model: ${
              status.embedding_model_name ?? "—"
            } · storage: ${status.storage}`
          : ""
      }
    >
      Semantic: {semanticReady ? "Available" : "Unavailable"}
      {status.missing_count > 0 && semanticReady
        ? ` · ${status.missing_count} missing`
        : ""}
    </span>
  ) : null;

  return (
    <div className="memory">
      <div className="memory__status-row">
        {statusBadge}
        {isAdmin && (
          <button
            className="btn btn--ghost btn--sm"
            onClick={runEmbedMissing}
            disabled={embedding || !semanticReady}
            title={
              !semanticReady
                ? "Embedding model or pgvector not configured"
                : "Embed all memories that don't yet have an embedding"
            }
          >
            {embedding ? "Embedding…" : "Embed Missing"}
          </button>
        )}
        {isAdmin && (
          <button
            className="btn btn--ghost btn--sm"
            onClick={runRebuildChunks}
            disabled={chunking || !semanticReady}
            title={
              !semanticReady
                ? "Embedding model or pgvector not configured"
                : "Build chunk-level embeddings for entries that have none"
            }
          >
            {chunking ? "Chunking…" : "Rebuild Missing Chunks"}
          </button>
        )}
      </div>
      {embedMsg && <div className="memory__note">{embedMsg}</div>}

      <div className="memory__controls">
        <input
          className="memory__search"
          type="search"
          placeholder={
            mode === "semantic" ? "Semantic search…" : "Keyword search…"
          }
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button
          className="btn btn--ghost btn--sm"
          onClick={() => load(query, mode)}
          title="Refresh"
          disabled={loading}
        >
          ↻
        </button>
      </div>
      <div className="memory__mode">
        <button
          className={`tab${mode === "keyword" ? " tab--active" : ""}`}
          onClick={() => setMode("keyword")}
        >
          Keyword
        </button>
        <button
          className={`tab${mode === "semantic" ? " tab--active" : ""}`}
          onClick={() => setMode("semantic")}
          disabled={status ? !semanticReady : false}
          title={
            status && !semanticReady
              ? "Semantic search unavailable (no pgvector or embedding model)"
              : ""
          }
        >
          Semantic
        </button>
      </div>
      {semanticUnavailable && (
        <div className="memory__note">{semanticUnavailable}</div>
      )}
      {mode === "semantic" && status && !semanticReady && (
        <div className="memory__note">Semantic search unavailable.</div>
      )}

      {error && <div className="sidebar__error">{error}</div>}

      <nav className="memory__list">
        {loading && items.length === 0 && (
          <div className="sidebar__empty">Loading…</div>
        )}
        {!loading && items.length === 0 && !error && (
          <div className="sidebar__empty">
            {query.trim()
              ? mode === "semantic"
                ? "No embedded memories found for that query."
                : "No matches."
              : "No memory yet."}
          </div>
        )}
        {items.map((m) => {
          const active = m.id === selectedId;
          return (
            <button
              key={m.id}
              className={`memory-item${active ? " memory-item--active" : ""}`}
              onClick={() => onSelect(m.id)}
            >
              <div className="memory-item__row">
                <span className="memory-item__title">{m.title}</span>
                <ImportanceDots value={m.importance} />
              </div>
              <div className="memory-item__meta">
                <span className="memory-item__type">{m.type}</span>
                {m.similarity != null && (
                  <span
                    className="memory-item__similarity"
                    title={`Cosine similarity ${m.similarity.toFixed(3)}`}
                  >
                    sim {m.similarity.toFixed(2)}
                  </span>
                )}
                <span>{formatRelative(m.created_at)}</span>
              </div>
              {m.preview && (
                <div className="memory-item__preview">{m.preview}</div>
              )}
              {m.tags.length > 0 && (
                <div className="memory-item__tags">
                  {m.tags.map((t) => (
                    <span key={t} className="tag-chip">
                      {t}
                    </span>
                  ))}
                </div>
              )}
            </button>
          );
        })}
      </nav>
    </div>
  );
}
