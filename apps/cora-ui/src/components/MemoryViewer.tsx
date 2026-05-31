import { useCallback, useEffect, useState } from "react";
import { embedMemoryEntry, getMemory } from "../api";
import type { MemoryEntry } from "../types";

interface Props {
  memoryId: string;
  onClose: () => void;
}

export function MemoryViewer({ memoryId, onClose }: Props) {
  const [entry, setEntry] = useState<MemoryEntry | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [embedding, setEmbedding] = useState(false);
  const [embedMsg, setEmbedMsg] = useState<string | null>(null);

  const runEmbed = useCallback(async () => {
    if (embedding) return;
    setEmbedding(true);
    setEmbedMsg(null);
    try {
      const res = await embedMemoryEntry(memoryId);
      if (res.status === "ok") {
        const dim = (res.detail as Record<string, unknown>).dim ?? "?";
        setEmbedMsg(`Embedded · dim ${dim}`);
      } else if (res.semantic_unavailable) {
        setEmbedMsg("Semantic search unavailable.");
      } else {
        const reason =
          (res.detail as Record<string, unknown>).reason ?? res.status;
        setEmbedMsg(`Skipped: ${reason}`);
      }
    } catch (err) {
      setEmbedMsg(err instanceof Error ? err.message : "Failed");
    } finally {
      setEmbedding(false);
    }
  }, [memoryId, embedding]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setEntry(null);
    getMemory(memoryId)
      .then((e) => {
        if (!cancelled) setEntry(e);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load memory");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [memoryId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const onBackdropClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (e.target === e.currentTarget) onClose();
  };

  return (
    <div className="modal-backdrop" onClick={onBackdropClick}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="Memory entry"
      >
        <header className="modal__header">
          <h2 className="modal__title">
            {entry?.title ?? (loading ? "Loading…" : "Memory")}
          </h2>
          <div className="admin__inline">
            {entry && (
              <button
                className="btn btn--ghost btn--sm"
                onClick={runEmbed}
                disabled={embedding}
                title="(Re)compute the embedding for this memory entry"
              >
                {embedding ? "Embedding…" : "Embed"}
              </button>
            )}
            <button
              className="modal__close"
              onClick={onClose}
              aria-label="Close"
            >
              ×
            </button>
          </div>
        </header>
        {embedMsg && <div className="memory__note">{embedMsg}</div>}

        {error && <div className="modal__error">{error}</div>}

        {entry && (
          <>
            <div className="modal__meta">
              <span className="memory-item__type">{entry.type}</span>
              <span>Importance {entry.importance}/5</span>
              <span>
                {new Date(entry.created_at).toLocaleString(undefined, {
                  dateStyle: "medium",
                  timeStyle: "short",
                })}
              </span>
              {entry.source_session_id && (
                <span title={entry.source_session_id}>
                  session {entry.source_session_id.slice(0, 8)}
                </span>
              )}
            </div>

            {entry.tags.length > 0 && (
              <div className="modal__tags">
                {entry.tags.map((t) => (
                  <span key={t} className="tag-chip">
                    {t}
                  </span>
                ))}
              </div>
            )}

            <div className="modal__body">{entry.content}</div>
          </>
        )}
      </div>
    </div>
  );
}
