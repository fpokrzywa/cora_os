import { useCallback, useEffect, useState } from "react";
import { Sidebar, type SidebarTab } from "./components/Sidebar";
import { ChatPanel } from "./components/ChatPanel";
import { MemoryViewer } from "./components/MemoryViewer";
import { Login } from "./components/Login";
import { AdminConsole } from "./components/AdminConsole";
import { CoraConfiguration } from "./components/CoraConfiguration";
import { listWorkspaces } from "./api";
import { setScreenView } from "./screenContext";
import type { Workspace } from "./types";

const WORKSPACE_STORAGE_KEY = "cora_workspace_id";
import {
  UNAUTHORIZED_EVENT,
  adminImpersonate,
  clearToken,
  deleteConversation,
  getConversation,
  getToken,
  hasStashedAdminToken,
  listConversations,
  me,
  popAdminToken,
  sendChat,
  setToken,
  stashAdminToken,
  updateConversation,
} from "./api";
import type {
  ChatMessage,
  ConversationSummary,
  TokenResponse,
  User,
} from "./types";

type ViewMode = "chat" | "admin-console" | "cora-config";

export function App() {
  const [user, setUser] = useState<User | null>(null);
  const [authResolved, setAuthResolved] = useState(false);

  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [sending, setSending] = useState(false);
  const [loadingConvo, setLoadingConvo] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sidebarError, setSidebarError] = useState<string | null>(null);
  const [sidebarTab, setSidebarTab] = useState<SidebarTab>("chats");
  const [selectedMemoryId, setSelectedMemoryId] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<ViewMode>("chat");
  const [impersonating, setImpersonating] = useState<boolean>(false);

  // Report the chat view for screen-context awareness; the Admin Console
  // reports its own tab/sub-tab state.
  useEffect(() => {
    if (viewMode === "chat") setScreenView("chat", "chat", "Chat");
  }, [viewMode]);
  const [selectedAgent, setSelectedAgent] = useState<string | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [currentWorkspaceId, setCurrentWorkspaceIdRaw] = useState<string | null>(
    () => {
      try {
        return localStorage.getItem(WORKSPACE_STORAGE_KEY);
      } catch {
        return null;
      }
    },
  );
  const setCurrentWorkspaceId = useCallback((id: string | null) => {
    setCurrentWorkspaceIdRaw(id);
    try {
      if (id) localStorage.setItem(WORKSPACE_STORAGE_KEY, id);
      else localStorage.removeItem(WORKSPACE_STORAGE_KEY);
    } catch {
      // ignore
    }
  }, []);

  const resetAppState = useCallback(() => {
    setSessionId(null);
    setMessages([]);
    setConversations([]);
    setSidebarError(null);
    setError(null);
    setSelectedMemoryId(null);
    setSidebarTab("chats");
    setSelectedAgent(null);
  }, []);

  // Bootstrap: if we have a token, validate it via /auth/me
  useEffect(() => {
    const token = getToken();
    if (!token) {
      setAuthResolved(true);
      return;
    }
    setImpersonating(hasStashedAdminToken());
    me()
      .then((u) => setUser(u))
      .catch(() => {
        clearToken();
        setUser(null);
      })
      .finally(() => setAuthResolved(true));
  }, []);

  // Global 401 → force logout
  useEffect(() => {
    const onUnauthorized = () => {
      setUser(null);
      resetAppState();
    };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () =>
      window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, [resetAppState]);

  const refreshConversations = useCallback(async () => {
    try {
      const data = await listConversations();
      setConversations(data);
      setSidebarError(null);
    } catch (err) {
      setSidebarError(err instanceof Error ? err.message : "Failed to load");
    }
  }, []);

  useEffect(() => {
    if (user) refreshConversations();
  }, [user, refreshConversations]);

  useEffect(() => {
    if (!user) return;
    listWorkspaces()
      .then((rows) => {
        setWorkspaces(rows);
        if (rows.length === 0) {
          setCurrentWorkspaceId(null);
          return;
        }
        const stored = currentWorkspaceId;
        const valid = stored && rows.some((w) => w.id === stored);
        if (!valid) {
          const def =
            rows.find((w) => w.slug === "cora-ai-os") ?? rows[0];
          setCurrentWorkspaceId(def.id);
        }
      })
      .catch(() => {
        // non-fatal — fall through to no workspace
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user]);

  const handleAuth = useCallback((result: TokenResponse) => {
    setToken(result.access_token);
    setUser(result.user);
  }, []);

  const handleLogout = useCallback(() => {
    // Drop any stashed admin token too — a full logout exits impersonation
    popAdminToken();
    setImpersonating(false);
    clearToken();
    setUser(null);
    resetAppState();
    setViewMode("chat");
  }, [resetAppState]);

  const handleImpersonate = useCallback(
    async (targetUserId: string) => {
      try {
        const adminToken = getToken();
        if (!adminToken) throw new Error("no current token to stash");
        const result = await adminImpersonate(targetUserId);
        stashAdminToken(adminToken);
        setToken(result.access_token);
        setUser(result.user);
        setImpersonating(true);
        resetAppState();
        setViewMode("chat");
      } catch (err) {
        setSidebarError(
          err instanceof Error ? err.message : "Impersonation failed",
        );
      }
    },
    [resetAppState],
  );

  const handleStopImpersonating = useCallback(async () => {
    const adminToken = popAdminToken();
    if (!adminToken) {
      // Defensive: no admin token to restore — fall back to full logout
      handleLogout();
      return;
    }
    setToken(adminToken);
    setImpersonating(false);
    try {
      const adminMe = await me();
      setUser(adminMe);
      resetAppState();
      setViewMode("admin-console");
    } catch (err) {
      // Token failed; force logout
      setSidebarError(
        err instanceof Error ? err.message : "Could not restore admin",
      );
      handleLogout();
    }
  }, [handleLogout, resetAppState]);

  const handleSend = useCallback(
    async (text: string, screenImage?: string | null) => {
      const trimmed = text.trim();
      if ((!trimmed && !screenImage) || sending) return;
      const message = trimmed || "What am I looking at on my screen?";
      setError(null);
      setSending(true);
      const optimisticUser: ChatMessage = {
        role: "user",
        content: screenImage ? `${message}\n\n_(shared a screenshot)_` : message,
        created_at: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, optimisticUser]);
      try {
        const res = await sendChat(message, sessionId, currentWorkspaceId, screenImage);
        setSessionId(res.session_id);
        setSelectedAgent(res.selected_agent);
        setMessages((prev) => [
          ...prev,
          {
            role: "assistant",
            content: res.response,
            created_at: res.created_at,
          },
        ]);
        refreshConversations();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Request failed");
      } finally {
        setSending(false);
      }
    },
    [sessionId, sending, currentWorkspaceId, refreshConversations],
  );

  const handleSelectConversation = useCallback(async (id: string) => {
    setError(null);
    setLoadingConvo(true);
    try {
      const detail = await getConversation(id);
      setSessionId(detail.session_id);
      setMessages(
        detail.messages.map((m) => ({
          id: m.id,
          role: m.role,
          content: m.content,
          created_at: m.created_at,
        })),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load conversation");
    } finally {
      setLoadingConvo(false);
    }
  }, []);

  const handleNewChat = useCallback(() => {
    setSessionId(null);
    setMessages([]);
    setError(null);
    setSidebarTab("chats");
    setViewMode("chat");
  }, []);

  const handleRenameConversation = useCallback(
    async (id: string, title: string) => {
      try {
        await updateConversation(id, { title });
        await refreshConversations();
      } catch (err) {
        setSidebarError(err instanceof Error ? err.message : "Rename failed");
      }
    },
    [refreshConversations],
  );

  const handleTogglePin = useCallback(
    async (id: string, pinned: boolean) => {
      try {
        await updateConversation(id, { pinned });
        await refreshConversations();
      } catch (err) {
        setSidebarError(err instanceof Error ? err.message : "Pin failed");
      }
    },
    [refreshConversations],
  );

  const handleDeleteConversation = useCallback(
    async (id: string) => {
      try {
        await deleteConversation(id);
      } catch (err) {
        setSidebarError(err instanceof Error ? err.message : "Delete failed");
        return;
      }
      // If the deleted chat was active, move to the next available one (most
      // recent of the remaining), or start a fresh chat if none remain.
      if (id === sessionId) {
        const remaining = conversations.filter((c) => c.session_id !== id);
        if (remaining.length > 0) {
          await handleSelectConversation(remaining[0].session_id);
        } else {
          handleNewChat();
        }
      }
      await refreshConversations();
    },
    [
      sessionId,
      conversations,
      handleSelectConversation,
      handleNewChat,
      refreshConversations,
    ],
  );

  const handleSelectConversationFromSidebar = useCallback(
    async (id: string) => {
      setViewMode("chat");
      await handleSelectConversation(id);
    },
    [handleSelectConversation],
  );

  // Switching the sidebar's Chats/Memory tab returns the main panel to chat,
  // so the user leaves any full-page view (Knowledge/News/Admin Console).
  const handleSidebarTabChange = useCallback((next: SidebarTab) => {
    setSidebarTab(next);
    setViewMode("chat");
  }, []);

  if (!authResolved) {
    return <div className="boot-splash">Loading…</div>;
  }
  if (!user) {
    return <Login onAuth={handleAuth} />;
  }

  return (
    <div className="app-shell">
      <Sidebar
        tab={sidebarTab}
        onTabChange={handleSidebarTabChange}
        conversations={conversations}
        activeSessionId={sessionId}
        onSelect={handleSelectConversationFromSidebar}
        onNewChat={handleNewChat}
        onRefresh={refreshConversations}
        onRenameConversation={handleRenameConversation}
        onTogglePin={handleTogglePin}
        onDeleteConversation={handleDeleteConversation}
        error={sidebarError}
        onSelectMemory={setSelectedMemoryId}
        selectedMemoryId={selectedMemoryId}
        isAdmin={user.role === "admin"}
        user={user}
        onLogout={handleLogout}
        canAdmin={user.role === "admin"}
        workspaces={workspaces}
        currentWorkspaceId={currentWorkspaceId}
        onSelectWorkspace={setCurrentWorkspaceId}
        onOpenAdminConsole={() => setViewMode("admin-console")}
        adminConsoleActive={viewMode === "admin-console"}
        onOpenCoraConfig={() => setViewMode("cora-config")}
        coraConfigActive={viewMode === "cora-config"}
        impersonating={impersonating}
        onStopImpersonating={handleStopImpersonating}
      />
      {viewMode === "admin-console" ? (
        <AdminConsole
          onImpersonate={handleImpersonate}
          workspaceId={currentWorkspaceId}
          isAdmin={user.role === "admin"}
          currentUserId={user.id}
          onWorkspacesChanged={() =>
            listWorkspaces().then(setWorkspaces).catch(() => {})
          }
        />
      ) : viewMode === "cora-config" ? (
        <CoraConfiguration standalone />
      ) : (
        <ChatPanel
          messages={messages}
          sessionId={sessionId}
          onSend={handleSend}
          sending={sending}
          loadingConvo={loadingConvo}
          error={error}
          selectedAgent={selectedAgent}
          workspaceName={
            workspaces.find((w) => w.id === currentWorkspaceId)?.name ?? null
          }
        />
      )}
      {selectedMemoryId && (
        <MemoryViewer
          memoryId={selectedMemoryId}
          onClose={() => setSelectedMemoryId(null)}
        />
      )}
    </div>
  );
}
