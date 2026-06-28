import { useMemo, useState } from "react";
import { MemoryList } from "./MemoryList";
import type { ConversationSummary, User, Workspace } from "../types";

const RECENT_CHAT_LIMIT = 5;

export type SidebarTab = "chats" | "memory";

interface Props {
  tab: SidebarTab;
  onTabChange: (tab: SidebarTab) => void;
  conversations: ConversationSummary[];
  activeSessionId: string | null;
  onSelect: (sessionId: string) => void;
  onNewChat: () => void;
  onRefresh: () => void;
  onRenameConversation: (id: string, title: string) => void;
  onTogglePin: (id: string, pinned: boolean) => void;
  onDeleteConversation: (id: string) => void;
  error: string | null;
  onSelectMemory: (id: string) => void;
  selectedMemoryId: string | null;
  isAdmin: boolean;
  user: User;
  onLogout: () => void;
  canAdmin: boolean;
  workspaces: Workspace[];
  currentWorkspaceId: string | null;
  onSelectWorkspace: (id: string | null) => void;
  onOpenAdminConsole: () => void;
  adminConsoleActive: boolean;
  onOpenCoraConfig: () => void;
  coraConfigActive: boolean;
  impersonating: boolean;
  onStopImpersonating: () => void;
}

function formatRelative(iso: string | null): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  const diff = Date.now() - then;
  const min = Math.round(diff / 60000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.round(hr / 24);
  return `${day}d ago`;
}

export function Sidebar({
  tab,
  onTabChange,
  conversations,
  activeSessionId,
  onSelect,
  onNewChat,
  onRefresh,
  onRenameConversation,
  onTogglePin,
  onDeleteConversation,
  error,
  onSelectMemory,
  selectedMemoryId,
  isAdmin,
  user,
  onLogout,
  canAdmin,
  workspaces,
  currentWorkspaceId,
  onSelectWorkspace,
  onOpenAdminConsole,
  adminConsoleActive,
  onOpenCoraConfig,
  coraConfigActive,
  impersonating,
  onStopImpersonating,
}: Props) {
  const [showOlderChats, setShowOlderChats] = useState(false);
  const [menuOpenId, setMenuOpenId] = useState<string | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");

  const sortedConversations = useMemo(() => {
    return [...conversations].sort((a, b) => {
      const av = new Date(a.updated_at).getTime();
      const bv = new Date(b.updated_at).getTime();
      return bv - av;
    });
  }, [conversations]);

  // Pinned chats are always shown (under their own header); the recent window +
  // "show older" toggle applies only to the non-pinned chats.
  const pinnedConversations = useMemo(
    () => sortedConversations.filter((c) => c.pinned),
    [sortedConversations],
  );
  const recentConversations = useMemo(
    () => sortedConversations.filter((c) => !c.pinned),
    [sortedConversations],
  );

  const activeRecentIndex = activeSessionId
    ? recentConversations.findIndex((c) => c.session_id === activeSessionId)
    : -1;
  // If the active conversation is outside the recent window, expand by default
  // so it stays visible without forcing the user to click.
  const effectiveShowOlder =
    showOlderChats || activeRecentIndex >= RECENT_CHAT_LIMIT;
  const visibleRecent = effectiveShowOlder
    ? recentConversations
    : recentConversations.slice(0, RECENT_CHAT_LIMIT);
  const olderCount = Math.max(
    0,
    recentConversations.length - RECENT_CHAT_LIMIT,
  );

  const titleFor = (c: ConversationSummary) =>
    c.title || `session ${c.session_id.slice(0, 8)}`;

  const beginRename = (c: ConversationSummary) => {
    setMenuOpenId(null);
    setRenamingId(c.session_id);
    setRenameValue(c.title ?? "");
  };

  const commitRename = (id: string) => {
    const next = renameValue.trim() || "New chat";
    onRenameConversation(id, next);
    setRenamingId(null);
    setRenameValue("");
  };

  const cancelRename = () => {
    setRenamingId(null);
    setRenameValue("");
  };

  const renderConvoItem = (c: ConversationSummary) => {
    const active = c.session_id === activeSessionId;
    const isRenaming = renamingId === c.session_id;
    const menuOpen = menuOpenId === c.session_id;
    return (
      <div
        key={c.session_id}
        className={`convo-item${active ? " convo-item--active" : ""}`}
      >
        {isRenaming ? (
          <input
            className="cora-input cora-input--compact convo-item__rename"
            autoFocus
            value={renameValue}
            onChange={(e) => setRenameValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                commitRename(c.session_id);
              } else if (e.key === "Escape") {
                e.preventDefault();
                cancelRename();
              }
            }}
            onBlur={() => commitRename(c.session_id)}
          />
        ) : (
          <button
            className="convo-item__main"
            onClick={() => onSelect(c.session_id)}
            title={titleFor(c)}
          >
            <div className="convo-item__title">
              {c.pinned && (
                <span className="convo-item__pin" aria-hidden>
                  ★{" "}
                </span>
              )}
              {titleFor(c)}
            </div>
            <div className="convo-item__meta">
              <span>{c.message_count} msg</span>
              <span>{formatRelative(c.last_message_at ?? c.updated_at)}</span>
            </div>
          </button>
        )}

        {!isRenaming && (
          <div className="convo-item__actions">
            <button
              className="convo-item__menu-btn"
              aria-label="Chat actions"
              onClick={(e) => {
                e.stopPropagation();
                setMenuOpenId(menuOpen ? null : c.session_id);
              }}
            >
              ⋯
            </button>
            {menuOpen && (
              <div className="convo-menu" role="menu">
                <button
                  className="convo-menu__item"
                  onClick={() => beginRename(c)}
                >
                  Rename
                </button>
                <button
                  className="convo-menu__item"
                  onClick={() => {
                    setMenuOpenId(null);
                    onTogglePin(c.session_id, !c.pinned);
                  }}
                >
                  {c.pinned ? "Unpin" : "Pin"}
                </button>
                <button
                  className="convo-menu__item convo-menu__item--danger"
                  onClick={() => {
                    setMenuOpenId(null);
                    if (
                      window.confirm(
                        `Delete "${titleFor(c)}"? Messages are kept, but the chat is removed from your list.`,
                      )
                    ) {
                      onDeleteConversation(c.session_id);
                    }
                  }}
                >
                  Delete
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    );
  };

  return (
    <aside className="sidebar">
      <div className="sidebar__header">
        <div className="brand">
          <span className="brand__mark">◆</span>
          <span className="brand__name">Cora</span>
        </div>
        {workspaces.length > 0 && (
          <label className="workspace-picker">
            <span className="workspace-picker__label">Workspace</span>
            <select
              className="cora-input cora-input--compact workspace-picker__select"
              value={currentWorkspaceId ?? ""}
              onChange={(e) =>
                onSelectWorkspace(e.target.value || null)
              }
            >
              {workspaces.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name}
                </option>
              ))}
            </select>
          </label>
        )}
        <button className="btn btn--primary" onClick={onNewChat}>
          + New chat
        </button>
      </div>

      <div className="sidebar__tabs" role="tablist">
        <button
          role="tab"
          aria-selected={tab === "chats"}
          className={`tab${tab === "chats" ? " tab--active" : ""}`}
          onClick={() => onTabChange("chats")}
        >
          Chats
        </button>
        <button
          role="tab"
          aria-selected={tab === "memory"}
          className={`tab${tab === "memory" ? " tab--active" : ""}`}
          onClick={() => onTabChange("memory")}
        >
          Memory
        </button>
      </div>

      {tab === "chats" ? (
        <>
          <div className="sidebar__section-header">
            <span>Conversations</span>
            <button
              className="btn btn--ghost btn--sm"
              onClick={onRefresh}
              title="Refresh"
            >
              ↻
            </button>
          </div>

          {error && <div className="sidebar__error">{error}</div>}

          <nav className="sidebar__list">
            {sortedConversations.length === 0 && !error && (
              <div className="sidebar__empty">No conversations yet.</div>
            )}

            {pinnedConversations.length > 0 && (
              <>
                <div className="sidebar__group-label">Pinned</div>
                {pinnedConversations.map(renderConvoItem)}
              </>
            )}

            {recentConversations.length > 0 && (
              <>
                {pinnedConversations.length > 0 && (
                  <div className="sidebar__group-label">Recent</div>
                )}
                {visibleRecent.map(renderConvoItem)}
                {olderCount > 0 && (
                  <button
                    className="convo-toggle"
                    onClick={() => setShowOlderChats((v) => !v)}
                  >
                    {effectiveShowOlder
                      ? "− Hide older chats"
                      : `+ Show older chats (${olderCount})`}
                  </button>
                )}
              </>
            )}
          </nav>
        </>
      ) : (
        <MemoryList
          onSelect={onSelectMemory}
          selectedId={selectedMemoryId}
          isAdmin={isAdmin}
        />
      )}

      <nav className="sidebar__menu" aria-label="Navigation">
        <button
          className={`menu-item${coraConfigActive ? " menu-item--active" : ""}`}
          onClick={onOpenCoraConfig}
        >
          <span className="menu-item__icon" aria-hidden>
            ✦
          </span>
          <span className="menu-item__label">Cora Configuration</span>
        </button>
        {canAdmin && !impersonating && (
          <button
            className={`menu-item${adminConsoleActive ? " menu-item--active" : ""}`}
            onClick={onOpenAdminConsole}
          >
            <span className="menu-item__icon" aria-hidden>
              ⚙
            </span>
            <span className="menu-item__label">Admin Console</span>
            <span className="menu-item__badge">admin</span>
          </button>
        )}
      </nav>

      {impersonating && (
        <div className="impersonation-banner">
          <div>
            Viewing as <strong>{user.display_name || user.email}</strong>
          </div>
          <button
            className="btn btn--ghost btn--sm"
            onClick={onStopImpersonating}
          >
            Restore admin
          </button>
        </div>
      )}

      <div className="sidebar__user">
        <div className="sidebar__user-info">
          <div className="sidebar__user-name">
            {user.display_name || user.email}
          </div>
          <div className="sidebar__user-role">{user.role}</div>
        </div>
        <button
          className="btn btn--ghost btn--sm"
          onClick={onLogout}
          title="Sign out"
        >
          Sign out
        </button>
      </div>

      <div className="sidebar__footer">
        Cora AI OS · Orchestration: ATLAS · DGX Spark
      </div>
    </aside>
  );
}
