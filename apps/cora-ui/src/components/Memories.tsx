import { useCallback, useEffect, useMemo, useState } from "react";
import {
  adminCreateMemory,
  adminListMemory,
  adminListUsers,
  adminVisibilityTest,
} from "../api";
import type {
  AdminMemoryEntry,
  AdminUser,
  MemoryPreview,
  VisibilityTestResponse,
} from "../types";

type ScopeFilter = "all" | "user" | "global" | "system";

const SCOPE_FILTERS: ScopeFilter[] = ["all", "user", "global", "system"];

export function Memories() {
  // ---- Users (owner lookups + scope/visibility dropdowns) ----
  const [users, setUsers] = useState<AdminUser[]>([]);

  const refreshUsers = useCallback(async () => {
    try {
      setUsers(await adminListUsers());
    } catch {
      // Non-fatal: the dropdowns just show no users to pick from.
    }
  }, []);

  // ---- Memory ----
  const [memScope, setMemScope] = useState<ScopeFilter>("all");
  const [memEntries, setMemEntries] = useState<AdminMemoryEntry[]>([]);
  const [memError, setMemError] = useState<string | null>(null);
  const [loadingMem, setLoadingMem] = useState(false);

  const refreshMemory = useCallback(async () => {
    setLoadingMem(true);
    setMemError(null);
    try {
      const rows = await adminListMemory(
        memScope === "all" ? undefined : memScope,
      );
      setMemEntries(rows);
    } catch (err) {
      setMemError(err instanceof Error ? err.message : "Failed");
    } finally {
      setLoadingMem(false);
    }
  }, [memScope]);

  // Create memory form
  const [newMemType, setNewMemType] = useState("note");
  const [newMemTitle, setNewMemTitle] = useState("");
  const [newMemContent, setNewMemContent] = useState("");
  const [newMemTags, setNewMemTags] = useState("");
  const [newMemImportance, setNewMemImportance] = useState(3);
  const [newMemScope, setNewMemScope] = useState<"user" | "global" | "system">(
    "global",
  );
  const [newMemScopeUser, setNewMemScopeUser] = useState("");
  const [newMemSubmitting, setNewMemSubmitting] = useState(false);
  const [newMemMsg, setNewMemMsg] = useState<string | null>(null);

  const submitNewMemory = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (newMemSubmitting) return;
      setNewMemSubmitting(true);
      setNewMemMsg(null);
      try {
        const tags = newMemTags
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean);
        const entry = await adminCreateMemory({
          type: newMemType.trim() || "note",
          title: newMemTitle.trim(),
          content: newMemContent,
          tags,
          importance: newMemImportance,
          scope_type: newMemScope,
          scope_id:
            newMemScope === "user" ? newMemScopeUser.trim() || null : null,
        });
        setNewMemMsg(`Created ${entry.id.slice(0, 8)} (${entry.scope_type})`);
        setNewMemTitle("");
        setNewMemContent("");
        setNewMemTags("");
        await refreshMemory();
      } catch (err) {
        setNewMemMsg(err instanceof Error ? err.message : "Failed");
      } finally {
        setNewMemSubmitting(false);
      }
    },
    [
      newMemType,
      newMemTitle,
      newMemContent,
      newMemTags,
      newMemImportance,
      newMemScope,
      newMemScopeUser,
      newMemSubmitting,
      refreshMemory,
    ],
  );

  // ---- Visibility test ----
  const [vtUserId, setVtUserId] = useState("");
  const [vtQuery, setVtQuery] = useState("");
  const [vtResult, setVtResult] = useState<VisibilityTestResponse | null>(null);
  const [vtError, setVtError] = useState<string | null>(null);
  const [vtLoading, setVtLoading] = useState(false);

  const runVisibilityTest = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (!vtUserId.trim() || vtLoading) return;
      setVtLoading(true);
      setVtError(null);
      try {
        setVtResult(
          await adminVisibilityTest(vtUserId.trim(), vtQuery.trim() || undefined),
        );
      } catch (err) {
        setVtError(err instanceof Error ? err.message : "Failed");
        setVtResult(null);
      } finally {
        setVtLoading(false);
      }
    },
    [vtUserId, vtQuery, vtLoading],
  );

  useEffect(() => {
    refreshUsers();
  }, [refreshUsers]);

  useEffect(() => {
    refreshMemory();
  }, [refreshMemory]);

  const usersById = useMemo(() => {
    const m = new Map<string, AdminUser>();
    users.forEach((u) => m.set(u.id, u));
    return m;
  }, [users]);

  return (
    <main className="admin">
      <header className="admin__header">
        <h1>Memories</h1>
        <p className="admin__subtitle">
          Memory inspection, creation, and visibility preview
        </p>
      </header>

      <section className="admin__section">
        <div className="admin__section-head">
          <h2>Memory by scope</h2>
          <div className="admin__inline">
            <div className="admin__scope-tabs">
              {SCOPE_FILTERS.map((s) => (
                <button
                  key={s}
                  className={`tab${memScope === s ? " tab--active" : ""}`}
                  onClick={() => setMemScope(s)}
                >
                  {s}
                </button>
              ))}
            </div>
            <button
              className="btn btn--ghost btn--sm"
              onClick={refreshMemory}
              disabled={loadingMem}
            >
              ↻
            </button>
          </div>
        </div>

        {memError && <div className="admin__error">{memError}</div>}

        <table className="admin__table">
          <thead>
            <tr>
              <th>Title</th>
              <th>Type</th>
              <th>Scope</th>
              <th>Owner</th>
              <th>Tags</th>
              <th>Imp.</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {memEntries.map((m) => (
              <tr key={m.id}>
                <td>{m.title}</td>
                <td className="mono">{m.type}</td>
                <td>
                  <span className={`scope-chip scope-chip--${m.scope_type}`}>
                    {m.scope_type}
                  </span>
                </td>
                <td className="mono muted">
                  {m.scope_id
                    ? usersById.get(m.scope_id)?.email ?? m.scope_id.slice(0, 8)
                    : "—"}
                </td>
                <td className="muted">{m.tags.join(", ") || "—"}</td>
                <td>{m.importance}</td>
                <td className="muted">
                  {new Date(m.created_at).toLocaleDateString()}
                </td>
              </tr>
            ))}
            {!loadingMem && memEntries.length === 0 && (
              <tr>
                <td colSpan={7} className="muted">
                  No memory entries.
                </td>
              </tr>
            )}
          </tbody>
        </table>

        <form className="admin__form" onSubmit={submitNewMemory}>
          <h3>Create memory entry</h3>
          <div className="admin__form-row">
            <label>
              <span>Scope</span>
              <select
                value={newMemScope}
                onChange={(e) =>
                  setNewMemScope(
                    e.target.value as "user" | "global" | "system",
                  )
                }
                disabled={newMemSubmitting}
              >
                <option value="global">global</option>
                <option value="user">user</option>
                <option value="system">system</option>
              </select>
            </label>
            {newMemScope === "user" && (
              <label>
                <span>User</span>
                <select
                  value={newMemScopeUser}
                  onChange={(e) => setNewMemScopeUser(e.target.value)}
                  required
                  disabled={newMemSubmitting}
                >
                  <option value="">— select —</option>
                  {users.map((u) => (
                    <option key={u.id} value={u.id}>
                      {u.email}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <label>
              <span>Type</span>
              <input
                type="text"
                value={newMemType}
                onChange={(e) => setNewMemType(e.target.value)}
                disabled={newMemSubmitting}
              />
            </label>
            <label>
              <span>Importance</span>
              <input
                type="number"
                min={1}
                max={5}
                value={newMemImportance}
                onChange={(e) => setNewMemImportance(Number(e.target.value))}
                disabled={newMemSubmitting}
              />
            </label>
          </div>
          <label className="admin__field-wide">
            <span>Title</span>
            <input
              type="text"
              required
              value={newMemTitle}
              onChange={(e) => setNewMemTitle(e.target.value)}
              disabled={newMemSubmitting}
            />
          </label>
          <label className="admin__field-wide">
            <span>Content</span>
            <textarea
              required
              rows={4}
              value={newMemContent}
              onChange={(e) => setNewMemContent(e.target.value)}
              disabled={newMemSubmitting}
            />
          </label>
          <label className="admin__field-wide">
            <span>Tags (comma-separated)</span>
            <input
              type="text"
              value={newMemTags}
              onChange={(e) => setNewMemTags(e.target.value)}
              disabled={newMemSubmitting}
            />
          </label>
          <div className="admin__form-row">
            <button
              type="submit"
              className="btn btn--primary"
              disabled={newMemSubmitting}
            >
              {newMemSubmitting ? "…" : "Create memory"}
            </button>
            {newMemMsg && <span className="admin__hint">{newMemMsg}</span>}
          </div>
        </form>
      </section>

      <section className="admin__section">
        <div className="admin__section-head">
          <h2>Visibility test</h2>
        </div>
        <p className="admin__subtitle">
          Preview the memory list + chat-prompt injection for a specific user
          without switching identity.
        </p>

        <form className="admin__form" onSubmit={runVisibilityTest}>
          <div className="admin__form-row">
            <label className="admin__field-wide">
              <span>User</span>
              <select
                value={vtUserId}
                onChange={(e) => setVtUserId(e.target.value)}
                required
                disabled={vtLoading}
              >
                <option value="">— select —</option>
                {users.map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.email} ({u.role})
                  </option>
                ))}
              </select>
            </label>
            <label className="admin__field-wide">
              <span>Chat query (optional)</span>
              <input
                type="text"
                placeholder="e.g. how does ATLAS route requests"
                value={vtQuery}
                onChange={(e) => setVtQuery(e.target.value)}
                disabled={vtLoading}
              />
            </label>
            <button
              type="submit"
              className="btn btn--primary"
              disabled={vtLoading || !vtUserId.trim()}
            >
              {vtLoading ? "…" : "Run test"}
            </button>
          </div>
        </form>

        {vtError && <div className="admin__error">{vtError}</div>}

        {vtResult && (
          <div className="admin__vt-result">
            <div className="admin__vt-meta">
              <strong>{vtResult.user_email}</strong>
              <span className="muted">{vtResult.scope_filter}</span>
            </div>

            <h3>What this user would see in /memory</h3>
            <p className="admin__hint">
              {vtResult.list_visible_count} entr
              {vtResult.list_visible_count === 1 ? "y" : "ies"} (top{" "}
              {vtResult.list_visible.length} shown)
            </p>
            <MemoryPreviewList items={vtResult.list_visible} />

            <h3>
              What would be injected into the chat prompt
              {vtResult.query ? ` for "${vtResult.query}"` : ""}
            </h3>
            {!vtResult.query ? (
              <p className="admin__hint">
                Provide a chat query above to preview prompt injection.
              </p>
            ) : (
              <>
                <p className="admin__hint">
                  Keyword search matched {vtResult.search_match_count}; top{" "}
                  {vtResult.search_top_in_prompt.length} would be embedded under
                  "Relevant Cora Memory".
                </p>
                <MemoryPreviewList items={vtResult.search_top_in_prompt} />
              </>
            )}
          </div>
        )}
      </section>
    </main>
  );
}

function MemoryPreviewList({ items }: { items: MemoryPreview[] }) {
  if (items.length === 0) {
    return <p className="muted">— nothing —</p>;
  }
  return (
    <ul className="admin__preview-list">
      {items.map((m) => (
        <li key={m.id}>
          <div className="admin__preview-row">
            <span className={`scope-chip scope-chip--${m.scope_type}`}>
              {m.scope_type}
            </span>
            <strong>{m.title}</strong>
            <span className="mono muted">{m.type}</span>
            <span className="muted">imp {m.importance}</span>
          </div>
          <div className="admin__preview-content">{m.content_preview}</div>
        </li>
      ))}
    </ul>
  );
}
