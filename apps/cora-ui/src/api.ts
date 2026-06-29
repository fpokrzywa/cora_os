import { getScreenContext } from "./screenContext";
import type {
  AdminMemoryEntry,
  AdminUser,
  Agent,
  AgentDelegation,
  AgentDetail,
  AgentType,
  AgentVersion,
  AgentVersionCreateRequest,
  RoutingTestRequest,
  RoutingTestResult,
  ResponseTestRequest,
  ResponseTestResult,
  AgentRunResponse,
  AgentAsyncResponse,
  AgentRunSummary,
  AgentRunDetail,
  AgentRuntimeConfig,
  ChatResponse,
  ConversationDetail,
  ConversationSummary,
  BulkKnowledgeResponse,
  ExecutionLog,
  ExecutionPlan,
  ExecutionPlanDetail,
  ExecutionPlanStep,
  KnowledgeEntry,
  KnowledgeScope,
  KnowledgeSource,
  KnowledgeSourceDetail,
  KnowledgeSourceType,
  KnowledgeNewsFeed,
  KnowledgeNewsFeedRegisterRequest,
  KnowledgeNewsFeedUpdateRequest,
  KnowledgeNewsFeedRefreshResponse,
  CommunicationDraft,
  CommunicationDraftCreateRequest,
  CommunicationDraftUpdateRequest,
  DraftReviewEvent,
  ScheduleProposal,
  ScheduleProposalCreateRequest,
  ScheduleProposalUpdateRequest,
  ProposalReviewEvent,
  ExternalIntegrationIntent,
  ExternalIntegrationEvent,
  IntegrationIntentCreateRequest,
  IntegrationIntentListParams,
  ExternalProviderConnector,
  ExternalProviderConnectorUpdateRequest,
  ProviderOAuthConnector,
  ProviderReadiness,
  OAuthReadinessResult,
  OAuthProvidersResponse,
  OAuthProviderStatus,
  OAuthStartResult,
  ExecutionStatus,
  ProviderExecutionResult,
  CredentialUsageSimulation,
  ExecutionApprovalListItem,
  ExecutionApprovalView,
  FinalInterlockResult,
  ExecutionAdapterInfo,
  AdapterSimulationResult,
  AdapterBlockedResult,
  ProviderFeatureFlag,
  ExecutionSwitch,
  GovernanceDashboard,
  ExternalProviderCredential,
  ExternalProviderCredentialCreateRequest,
  ExternalProviderCredentialUpdateRequest,
  ExternalProviderCredentialEvent,
  NewsBriefingParams,
  NewsBriefingResponse,
  NewsIngestResponse,
  SourceRefreshResponse,
  UrlIngestResponse,
  GovernanceStats,
  Job,
  JobStatus,
  QueueStepResponse,
  RuntimeTrace,
  McpCapabilitiesResponse,
  McpServer,
  McpTestResult,
  EmbedResult,
  EmbeddingsStatus,
  MemoryEntry,
  MemorySearchResult,
  SemanticSearchResponse,
  TokenResponse,
  ToolAdminRow,
  Workspace,
  WorkspaceContext,
  WorkspaceDetail,
  ToolPolicy,
  ToolRiskLevel,
  ToolTestResult,
  User,
  VisibilityTestResponse,
} from "./types";

const API_BASE =
  (import.meta.env.VITE_CORA_API_URL as string | undefined)?.replace(/\/$/, "") ||
  "http://api.cora.local.arpa";

const TOKEN_KEY = "cora_auth_token";
const ADMIN_TOKEN_KEY = "cora_admin_token";

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token);
  } catch {
    // localStorage unavailable; token remains in memory only via callers
  }
}

export function clearToken(): void {
  try {
    localStorage.removeItem(TOKEN_KEY);
  } catch {
    // ignore
  }
}

// Impersonation: while admin previews another user, we keep the admin's
// original token in a separate slot so we can restore it without re-login.
export function stashAdminToken(token: string): void {
  try {
    localStorage.setItem(ADMIN_TOKEN_KEY, token);
  } catch {
    // ignore
  }
}

export function popAdminToken(): string | null {
  try {
    const t = localStorage.getItem(ADMIN_TOKEN_KEY);
    localStorage.removeItem(ADMIN_TOKEN_KEY);
    return t;
  } catch {
    return null;
  }
}

export function hasStashedAdminToken(): boolean {
  try {
    return localStorage.getItem(ADMIN_TOKEN_KEY) !== null;
  } catch {
    return false;
  }
}

export const UNAUTHORIZED_EVENT = "cora:unauthorized";

interface RequestOptions extends RequestInit {
  skipAuth?: boolean;
}

async function request<T>(path: string, init?: RequestOptions): Promise<T> {
  const { skipAuth, ...rest } = init ?? {};
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((rest.headers as Record<string, string>) ?? {}),
  };
  const token = getToken();
  if (token && !skipAuth) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  const res = await fetch(`${API_BASE}${path}`, { ...rest, headers });

  if (res.status === 401 && !skipAuth) {
    clearToken();
    window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
  }

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body?.detail) detail = body.detail;
    } catch {
      // ignore parse errors
    }
    throw new Error(`${res.status} ${detail}`);
  }
  return (await res.json()) as T;
}

// ---------- Auth ----------

export function login(email: string, password: string) {
  return request<TokenResponse>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
    skipAuth: true,
  });
}

export function register(
  email: string,
  password: string,
  display_name?: string,
) {
  return request<TokenResponse>("/auth/register", {
    method: "POST",
    body: JSON.stringify({ email, password, display_name }),
  });
}

export function me() {
  return request<User>("/auth/me");
}

// ---------- Chat ----------

export function sendChat(
  message: string,
  sessionId: string | null,
  workspaceId?: string | null,
  screenImage?: string | null,
) {
  return request<ChatResponse>("/chat", {
    method: "POST",
    body: JSON.stringify({
      message,
      session_id: sessionId ?? undefined,
      workspace_id: workspaceId ?? undefined,
      screen_context: getScreenContext(),
      screen_image: screenImage ?? undefined,
    }),
  });
}

export function listConversations() {
  return request<ConversationSummary[]>("/conversations");
}

// ---------- Agent runtime (Cora Configuration) ----------

export function getAgentConfig() {
  return request<AgentRuntimeConfig>("/chat/agent/config");
}

export function sendAgentChat(message: string, sessionId?: string | null) {
  return request<AgentRunResponse>("/chat/agent", {
    method: "POST",
    body: JSON.stringify({ message, session_id: sessionId ?? undefined }),
  });
}

export function sendAgentChatAsync(message: string, sessionId?: string | null) {
  return request<AgentAsyncResponse>("/chat/agent/async", {
    method: "POST",
    body: JSON.stringify({ message, session_id: sessionId ?? undefined }),
  });
}

export function listAgentRuns(limit = 50) {
  return request<AgentRunSummary[]>(`/chat/agent/runs?limit=${limit}`);
}

export function getAgentRun(runId: string) {
  return request<AgentRunDetail>(`/chat/agent/runs/${encodeURIComponent(runId)}`);
}

export function decideAgentRun(
  runId: string,
  decision: "approve" | "reject",
  note?: string,
  override?: boolean,
) {
  return request<AgentRunDetail>(
    `/chat/agent/runs/${encodeURIComponent(runId)}/decision`,
    {
      method: "POST",
      body: JSON.stringify({ decision, note: note ?? undefined, override: override || undefined }),
    },
  );
}

export function getConversation(sessionId: string) {
  return request<ConversationDetail>(`/conversations/${sessionId}`);
}

export function updateConversation(
  conversationId: string,
  payload: { title?: string; pinned?: boolean },
) {
  return request<ConversationSummary>(`/conversations/${conversationId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function deleteConversation(conversationId: string) {
  return request<{ status: string; session_id: string }>(
    `/conversations/${conversationId}`,
    { method: "DELETE" },
  );
}

// ---------- Memory ----------

export function listMemory(limit = 50) {
  return request<MemoryEntry[]>(`/memory?limit=${limit}`);
}

export function searchMemory(q: string, limit = 20) {
  const params = new URLSearchParams({ q, limit: String(limit) });
  return request<MemorySearchResult[]>(`/memory/search?${params.toString()}`);
}

export function getMemory(id: string) {
  return request<MemoryEntry>(`/memory/${id}`);
}

export function semanticSearchMemory(
  q: string,
  limit = 10,
  workspaceId?: string | null,
) {
  const params = new URLSearchParams({ q, limit: String(limit) });
  if (workspaceId) params.set("workspace_id", workspaceId);
  return request<SemanticSearchResponse>(
    `/memory/semantic-search?${params.toString()}`,
  );
}

export function embedMemoryEntry(memoryId: string) {
  return request<EmbedResult>(`/memory/${memoryId}/embed`, { method: "POST" });
}

export function embedMissingMemory(limit = 100) {
  return request<EmbedResult>(`/memory/embed-missing?limit=${limit}`, {
    method: "POST",
  });
}

export function rebuildMissingChunks(limit = 25) {
  return request<EmbedResult>(`/memory/chunks/rebuild-missing?limit=${limit}`, {
    method: "POST",
  });
}

export function getEmbeddingsStatus() {
  return request<EmbeddingsStatus>(`/memory/embeddings/status`);
}

// ---------- Admin ----------

export function adminListUsers() {
  return request<AdminUser[]>("/admin/users");
}

export function adminCreateUser(req: {
  email: string;
  password: string;
  display_name?: string;
  role?: "admin" | "user";
}) {
  return request<AdminUser>("/admin/users", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export function adminImpersonate(userId: string) {
  return request<TokenResponse>(`/admin/users/${userId}/impersonate`, {
    method: "POST",
  });
}

export function adminListMemory(scopeType?: "user" | "global" | "system") {
  const qs = scopeType ? `?scope_type=${encodeURIComponent(scopeType)}` : "";
  return request<AdminMemoryEntry[]>(`/admin/memory${qs}`);
}

export function adminCreateMemory(req: {
  type: string;
  title: string;
  content: string;
  tags: string[];
  importance: number;
  scope_type: "user" | "global" | "system";
  scope_id?: string | null;
  source_session_id?: string | null;
}) {
  return request<AdminMemoryEntry>("/admin/memory", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export function adminVisibilityTest(userId: string, q?: string) {
  const params = new URLSearchParams({ user_id: userId });
  if (q) params.set("q", q);
  return request<VisibilityTestResponse>(
    `/admin/memory/visibility-test?${params.toString()}`,
  );
}

// ---------- Agent registry ----------

export function adminListAgents() {
  return request<Agent[]>("/admin/agents");
}

export function adminGetAgent(name: string) {
  return request<AgentDetail>(`/admin/agents/${encodeURIComponent(name)}`);
}

export function adminCreateAgent(req: {
  name: string;
  display_name: string;
  description?: string | null;
  agent_type: AgentType;
  enabled?: boolean;
}) {
  return request<Agent>("/admin/agents", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export function adminCreateAgentVersion(
  agentName: string,
  req: AgentVersionCreateRequest,
) {
  return request<AgentVersion>(
    `/admin/agents/${encodeURIComponent(agentName)}/versions`,
    { method: "POST", body: JSON.stringify(req) },
  );
}

export function adminUpdateAgent(
  agentName: string,
  req: { display_name?: string; description?: string | null; enabled?: boolean },
) {
  return request<Agent>(`/admin/agents/${encodeURIComponent(agentName)}`, {
    method: "PATCH",
    body: JSON.stringify(req),
  });
}

export function testAgentRouting(req: RoutingTestRequest) {
  return request<RoutingTestResult>("/admin/agents/test-routing", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export function testAgentResponse(req: ResponseTestRequest) {
  return request<ResponseTestResult>("/admin/agents/test-response", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export function adminActivateAgentVersion(agentName: string, versionId: string) {
  return request<AgentVersion>(
    `/admin/agents/${encodeURIComponent(agentName)}/versions/${versionId}/activate`,
    { method: "POST" },
  );
}

export function adminArchiveAgentVersion(agentName: string, versionId: string) {
  return request<AgentVersion>(
    `/admin/agents/${encodeURIComponent(agentName)}/versions/${versionId}/archive`,
    { method: "POST" },
  );
}

// ---------- MCP servers ----------

export function adminListMcp() {
  return request<McpServer[]>("/admin/mcp");
}

export function adminCreateMcp(req: {
  name: string;
  description?: string | null;
  server_type?: string;
  endpoint: string;
  enabled?: boolean;
  auth_type?: string | null;
  auth_config?: Record<string, unknown> | null;
}) {
  return request<McpServer>("/admin/mcp", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export function adminPatchMcp(
  serverName: string,
  req: {
    description?: string | null;
    endpoint?: string;
    enabled?: boolean;
    auth_type?: string | null;
    auth_config?: Record<string, unknown> | null;
    clear_auth?: boolean;
  },
) {
  return request<McpServer>(`/admin/mcp/${encodeURIComponent(serverName)}`, {
    method: "PATCH",
    body: JSON.stringify(req),
  });
}

export function adminTestMcp(serverName: string) {
  return request<McpTestResult>(
    `/admin/mcp/${encodeURIComponent(serverName)}/test`,
    { method: "POST" },
  );
}

export function adminGetMcpCapabilities(serverName: string, refresh = false) {
  const qs = refresh ? "?refresh=true" : "";
  return request<McpCapabilitiesResponse>(
    `/admin/mcp/${encodeURIComponent(serverName)}/capabilities${qs}`,
  );
}

// ---------- Tool registry admin ----------

export function adminListTools() {
  return request<ToolAdminRow[]>("/admin/tools");
}

export function adminCreateTool(req: {
  name: string;
  description?: string | null;
  type: string;
  endpoint?: string | null;
  enabled?: boolean;
  requires_confirmation?: boolean;
  mcp_server_name?: string | null;
  mcp_action_name?: string | null;
  input_schema?: Record<string, unknown> | null;
  output_schema?: Record<string, unknown> | null;
  risk_level?: ToolRiskLevel;
  allowed_agents?: string[];
}) {
  return request<ToolAdminRow>("/admin/tools", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export function adminPatchTool(
  toolName: string,
  req: {
    description?: string | null;
    endpoint?: string | null;
    enabled?: boolean;
    requires_confirmation?: boolean;
    mcp_server_name?: string | null;
    mcp_action_name?: string | null;
    input_schema?: Record<string, unknown> | null;
    output_schema?: Record<string, unknown> | null;
    risk_level?: ToolRiskLevel;
    allowed_agents?: string[];
  },
) {
  return request<ToolAdminRow>(`/admin/tools/${encodeURIComponent(toolName)}`, {
    method: "PATCH",
    body: JSON.stringify(req),
  });
}

export function adminTestTool(
  toolName: string,
  payload: {
    session_id?: string | null;
    user_message?: string | null;
    metadata?: Record<string, unknown> | null;
  } = {},
) {
  return request<ToolTestResult>(
    `/admin/tools/${encodeURIComponent(toolName)}/test`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

// ---------- Governance ----------

export function adminListPolicies(params?: {
  tool_name?: string;
  agent_name?: string;
}) {
  const qs = new URLSearchParams();
  if (params?.tool_name) qs.set("tool_name", params.tool_name);
  if (params?.agent_name) qs.set("agent_name", params.agent_name);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<ToolPolicy[]>(`/admin/governance/policies${suffix}`);
}

export function adminUpsertPolicy(req: {
  tool_name: string;
  agent_name: string;
  allowed: boolean;
  requires_confirmation?: boolean;
  max_calls_per_hour?: number | null;
}) {
  return request<ToolPolicy>("/admin/governance/policies", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export function adminDeletePolicy(policyId: string) {
  return request<void>(`/admin/governance/policies/${policyId}`, {
    method: "DELETE",
  });
}

export function adminListExecutionLogs(params?: {
  limit?: number;
  denied_only?: boolean;
  tool_name?: string;
  agent_name?: string;
}) {
  const qs = new URLSearchParams();
  if (params?.limit) qs.set("limit", String(params.limit));
  if (params?.denied_only) qs.set("denied_only", "true");
  if (params?.tool_name) qs.set("tool_name", params.tool_name);
  if (params?.agent_name) qs.set("agent_name", params.agent_name);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<ExecutionLog[]>(`/admin/governance/logs${suffix}`);
}

export function adminGovernanceStats(windowHours = 24) {
  return request<GovernanceStats>(
    `/admin/governance/stats?window_hours=${windowHours}`,
  );
}

// ---------- Runtime traces ----------

export function adminListTraces(params?: {
  limit?: number;
  offset?: number;
  trace_type?: string;
  selected_agent?: string;
  status?: string;
  session_id?: string;
}) {
  const qs = new URLSearchParams();
  if (params?.limit) qs.set("limit", String(params.limit));
  if (params?.offset) qs.set("offset", String(params.offset));
  if (params?.trace_type) qs.set("trace_type", params.trace_type);
  if (params?.selected_agent) qs.set("selected_agent", params.selected_agent);
  if (params?.status) qs.set("status", params.status);
  if (params?.session_id) qs.set("session_id", params.session_id);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<RuntimeTrace[]>(`/admin/traces${suffix}`);
}

export function adminGetTrace(traceId: string) {
  return request<RuntimeTrace>(`/admin/traces/${traceId}`);
}

export function adminListTracesForSession(sessionId: string) {
  return request<RuntimeTrace[]>(`/admin/traces/session/${sessionId}`);
}

// ---------- Execution plans ----------

export function listPlans(limit = 50, offset = 0) {
  return request<ExecutionPlan[]>(`/plans?limit=${limit}&offset=${offset}`);
}

export function getPlan(planId: string) {
  return request<ExecutionPlanDetail>(`/plans/${planId}`);
}

export function listPlansForSession(sessionId: string) {
  return request<ExecutionPlan[]>(`/plans/session/${sessionId}`);
}

export function patchPlan(
  planId: string,
  body: { title?: string; goal?: string; status?: string },
) {
  return request<ExecutionPlan>(`/plans/${planId}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function patchPlanStep(
  planId: string,
  stepId: string,
  body: {
    title?: string;
    description?: string;
    assigned_agent?: string | null;
    tool_name?: string | null;
    status?: string;
    result?: Record<string, unknown> | null;
  },
) {
  return request<ExecutionPlanStep>(`/plans/${planId}/steps/${stepId}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function cancelPlan(planId: string) {
  return request<ExecutionPlan>(`/plans/${planId}/cancel`, { method: "POST" });
}

export function completePlan(planId: string) {
  return request<ExecutionPlan>(`/plans/${planId}/complete`, {
    method: "POST",
  });
}

export function completePlanStep(
  planId: string,
  stepId: string,
  result?: Record<string, unknown>,
) {
  return request<ExecutionPlanStep>(
    `/plans/${planId}/steps/${stepId}/complete`,
    { method: "POST", body: JSON.stringify({ result: result ?? null }) },
  );
}

export function failPlanStep(
  planId: string,
  stepId: string,
  errorMessage?: string,
) {
  return request<ExecutionPlanStep>(
    `/plans/${planId}/steps/${stepId}/fail`,
    {
      method: "POST",
      body: JSON.stringify({ error_message: errorMessage ?? null }),
    },
  );
}

export function queuePlanStep(planId: string, stepId: string) {
  return request<QueueStepResponse>(
    `/plans/${planId}/steps/${stepId}/queue`,
    { method: "POST" },
  );
}

// ---------- Jobs (admin) ----------

export function adminListJobs(params?: {
  limit?: number;
  status?: JobStatus | "";
  job_type?: string;
  plan_id?: string;
}) {
  const qs = new URLSearchParams();
  if (params?.limit) qs.set("limit", String(params.limit));
  if (params?.status) qs.set("status", params.status);
  if (params?.job_type) qs.set("job_type", params.job_type);
  if (params?.plan_id) qs.set("plan_id", params.plan_id);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<Job[]>(`/admin/jobs${suffix}`);
}

export function adminGetJob(jobId: string) {
  return request<Job>(`/admin/jobs/${jobId}`);
}

export function adminCancelJob(jobId: string) {
  return request<Job>(`/admin/jobs/${jobId}/cancel`, { method: "POST" });
}

export function adminCreateJob(req: {
  job_type: string;
  payload?: Record<string, unknown> | null;
  plan_id?: string | null;
  step_id?: string | null;
  session_id?: string | null;
  max_attempts?: number;
}) {
  return request<Job>(`/admin/jobs`, {
    method: "POST",
    body: JSON.stringify(req),
  });
}

// ---------- Workspaces ----------

export function listWorkspaces(includeArchived = false) {
  const qs = includeArchived ? "?include_archived=true" : "";
  return request<Workspace[]>(`/workspaces${qs}`);
}

export function getWorkspace(workspaceId: string) {
  return request<WorkspaceDetail>(`/workspaces/${workspaceId}`);
}

export function createWorkspace(req: {
  name: string;
  slug?: string;
  description?: string | null;
}) {
  return request<Workspace>(`/workspaces`, {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export function patchWorkspace(
  workspaceId: string,
  req: { name?: string; description?: string | null; status?: string },
) {
  return request<Workspace>(`/workspaces/${workspaceId}`, {
    method: "PATCH",
    body: JSON.stringify(req),
  });
}

export function getWorkspaceContext(workspaceId: string) {
  return request<WorkspaceContext>(`/workspaces/${workspaceId}/context`);
}

export function listWorkspaceKnowledge(workspaceId: string, limit = 100) {
  return request<KnowledgeEntry[]>(
    `/workspaces/${workspaceId}/knowledge?limit=${limit}`,
  );
}

export function ingestKnowledge(
  workspaceId: string,
  req: {
    title: string;
    content: string;
    tags?: string[];
    scope_type?: KnowledgeScope;
    importance?: number;
    auto_embed?: boolean;
    type?: string;
  },
) {
  return request<KnowledgeEntry>(`/workspaces/${workspaceId}/knowledge`, {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export function ingestUrlKnowledge(
  workspaceId: string,
  req: {
    url: string;
    title?: string;
    scope_type?: KnowledgeScope;
    auto_embed?: boolean;
  },
) {
  return request<UrlIngestResponse>(
    `/workspaces/${workspaceId}/knowledge/url`,
    {
      method: "POST",
      body: JSON.stringify(req),
    },
  );
}

export function ingestNewsFeed(
  workspaceId: string,
  req: {
    source_name?: string;
    feed_url: string;
    max_items?: number;
    scope_type?: KnowledgeScope;
    importance?: number;
    auto_embed?: boolean;
    fetch_article_body?: boolean;
  },
) {
  return request<NewsIngestResponse>(
    `/workspaces/${workspaceId}/knowledge/news`,
    {
      method: "POST",
      body: JSON.stringify(req),
    },
  );
}

export function listKnowledgeNewsFeeds(workspaceId: string) {
  return request<KnowledgeNewsFeed[]>(
    `/workspaces/${workspaceId}/knowledge/news/feeds`,
  );
}

export function registerKnowledgeNewsFeed(
  workspaceId: string,
  payload: KnowledgeNewsFeedRegisterRequest,
) {
  return request<KnowledgeNewsFeed>(
    `/workspaces/${workspaceId}/knowledge/news/feeds`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function updateKnowledgeNewsFeed(
  sourceId: string,
  payload: KnowledgeNewsFeedUpdateRequest,
) {
  return request<KnowledgeNewsFeed>(
    `/knowledge/sources/${sourceId}/news-feed`,
    { method: "PATCH", body: JSON.stringify(payload) },
  );
}

export function getNewsBriefing(
  workspaceId: string,
  params: NewsBriefingParams = {},
) {
  const qs = new URLSearchParams();
  if (params.since_hours != null) qs.set("since_hours", String(params.since_hours));
  if (params.max_articles != null)
    qs.set("max_articles", String(params.max_articles));
  if (params.source_name) qs.set("source_name", params.source_name);
  if (params.include_summary != null)
    qs.set("include_summary", String(params.include_summary));
  const q = qs.toString();
  return request<NewsBriefingResponse>(
    `/workspaces/${workspaceId}/knowledge/news/briefing${q ? `?${q}` : ""}`,
  );
}

export function refreshKnowledgeNewsFeed(sourceId: string) {
  return request<KnowledgeNewsFeedRefreshResponse>(
    `/knowledge/sources/${sourceId}/news-refresh`,
    { method: "POST" },
  );
}

export async function uploadKnowledgeFile(
  workspaceId: string,
  opts: {
    file: File;
    scope_type?: KnowledgeScope;
    importance?: number;
    auto_embed?: boolean;
    tags?: string;
  },
): Promise<KnowledgeEntry> {
  const form = new FormData();
  form.append("file", opts.file);
  form.append("scope_type", opts.scope_type ?? "user");
  form.append("importance", String(opts.importance ?? 3));
  form.append("auto_embed", String(opts.auto_embed ?? true));
  form.append("tags", opts.tags ?? "");

  const token = getToken();
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(
    `${API_BASE}/workspaces/${workspaceId}/knowledge/upload`,
    { method: "POST", body: form, headers },
  );
  if (res.status === 401) {
    clearToken();
    window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body?.detail) detail = body.detail;
    } catch {
      // ignore
    }
    throw new Error(`${res.status} ${detail}`);
  }
  return (await res.json()) as KnowledgeEntry;
}

export function ingestKnowledgeBulk(
  workspaceId: string,
  entries: Array<{
    title: string;
    content: string;
    tags?: string[];
    scope_type?: KnowledgeScope;
    importance?: number;
    auto_embed?: boolean;
    type?: string;
  }>,
) {
  return request<BulkKnowledgeResponse>(
    `/workspaces/${workspaceId}/knowledge/bulk`,
    { method: "POST", body: JSON.stringify({ entries }) },
  );
}

// ---------- Knowledge sources ----------

export function listKnowledgeSources(
  workspaceId: string,
  includeArchived = false,
) {
  const qs = includeArchived ? "?include_archived=true" : "";
  return request<KnowledgeSource[]>(
    `/workspaces/${workspaceId}/knowledge/sources${qs}`,
  );
}

export function getKnowledgeSource(sourceId: string) {
  return request<KnowledgeSourceDetail>(`/knowledge/sources/${sourceId}`);
}

export function createKnowledgeSource(
  workspaceId: string,
  req: {
    source_type: KnowledgeSourceType;
    title: string;
    description?: string | null;
    original_filename?: string | null;
    source_url?: string | null;
    content?: string | null;
  },
) {
  return request<KnowledgeSource>(
    `/workspaces/${workspaceId}/knowledge/sources`,
    { method: "POST", body: JSON.stringify(req) },
  );
}

export function patchKnowledgeSource(
  sourceId: string,
  req: {
    title?: string;
    description?: string | null;
    source_url?: string | null;
    status?: "active" | "archived";
  },
) {
  return request<KnowledgeSource>(`/knowledge/sources/${sourceId}`, {
    method: "PATCH",
    body: JSON.stringify(req),
  });
}

export function refreshKnowledgeSource(sourceId: string) {
  return request<SourceRefreshResponse>(
    `/knowledge/sources/${sourceId}/refresh`,
    { method: "POST" },
  );
}

export function deleteKnowledgeSource(sourceId: string) {
  return request<void>(`/knowledge/sources/${sourceId}`, { method: "DELETE" });
}

// ---------- SIGNAL communication drafts (review-only, never sent) ----------

export function listSignalDrafts(workspaceId: string, includeArchived = false) {
  const qs = includeArchived ? "?include_archived=true" : "";
  return request<CommunicationDraft[]>(
    `/workspaces/${workspaceId}/signal/drafts${qs}`,
  );
}

export function createSignalDraft(
  workspaceId: string,
  payload: CommunicationDraftCreateRequest,
) {
  return request<CommunicationDraft>(`/workspaces/${workspaceId}/signal/drafts`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateSignalDraft(
  workspaceId: string,
  draftId: string,
  payload: CommunicationDraftUpdateRequest,
) {
  return request<CommunicationDraft>(
    `/workspaces/${workspaceId}/signal/drafts/${draftId}`,
    { method: "PATCH", body: JSON.stringify(payload) },
  );
}

// SIGNAL draft review workflow (internal only — approval never sends anything).
function signalAction(
  workspaceId: string,
  draftId: string,
  action: string,
  notes?: string,
) {
  return request<CommunicationDraft>(
    `/workspaces/${workspaceId}/signal/drafts/${draftId}/${action}`,
    { method: "POST", body: JSON.stringify({ notes: notes ?? null }) },
  );
}

export function submitSignalDraftForReview(
  workspaceId: string,
  draftId: string,
  notes?: string,
) {
  return signalAction(workspaceId, draftId, "submit-review", notes);
}

export function requestSignalDraftChanges(
  workspaceId: string,
  draftId: string,
  notes?: string,
) {
  return signalAction(workspaceId, draftId, "request-changes", notes);
}

export function markSignalDraftReviewed(
  workspaceId: string,
  draftId: string,
  notes?: string,
) {
  return signalAction(workspaceId, draftId, "mark-reviewed", notes);
}

export function approveSignalDraft(
  workspaceId: string,
  draftId: string,
  notes?: string,
) {
  return signalAction(workspaceId, draftId, "approve", notes);
}

export function archiveSignalDraft(
  workspaceId: string,
  draftId: string,
  notes?: string,
) {
  return signalAction(workspaceId, draftId, "archive", notes);
}

export function listSignalDraftReviewEvents(
  workspaceId: string,
  draftId: string,
) {
  return request<DraftReviewEvent[]>(
    `/workspaces/${workspaceId}/signal/drafts/${draftId}/review-events`,
  );
}

// Permanent delete (removes the draft record only — sends/cancels nothing).
export function deleteSignalDraft(workspaceId: string, draftId: string) {
  return request<{ deleted: boolean; draft_id: string }>(
    `/workspaces/${workspaceId}/signal/drafts/${draftId}`,
    { method: "DELETE" },
  );
}

// ---------- CHRONOS schedule proposals (review-only, no calendar write) ----------

export function listChronosProposals(
  workspaceId: string,
  includeArchived = false,
) {
  const qs = includeArchived ? "?include_archived=true" : "";
  return request<ScheduleProposal[]>(
    `/workspaces/${workspaceId}/chronos/proposals${qs}`,
  );
}

export function createChronosProposal(
  workspaceId: string,
  payload: ScheduleProposalCreateRequest,
) {
  return request<ScheduleProposal>(
    `/workspaces/${workspaceId}/chronos/proposals`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function updateChronosProposal(
  workspaceId: string,
  proposalId: string,
  payload: ScheduleProposalUpdateRequest,
) {
  return request<ScheduleProposal>(
    `/workspaces/${workspaceId}/chronos/proposals/${proposalId}`,
    { method: "PATCH", body: JSON.stringify(payload) },
  );
}

// CHRONOS proposal review workflow (internal only — approval never schedules).
function chronosAction(
  workspaceId: string,
  proposalId: string,
  action: string,
  notes?: string,
) {
  return request<ScheduleProposal>(
    `/workspaces/${workspaceId}/chronos/proposals/${proposalId}/${action}`,
    { method: "POST", body: JSON.stringify({ notes: notes ?? null }) },
  );
}

export function submitChronosProposalForReview(
  workspaceId: string,
  proposalId: string,
  notes?: string,
) {
  return chronosAction(workspaceId, proposalId, "submit-review", notes);
}

export function requestChronosProposalChanges(
  workspaceId: string,
  proposalId: string,
  notes?: string,
) {
  return chronosAction(workspaceId, proposalId, "request-changes", notes);
}

export function markChronosProposalReviewed(
  workspaceId: string,
  proposalId: string,
  notes?: string,
) {
  return chronosAction(workspaceId, proposalId, "mark-reviewed", notes);
}

export function approveChronosProposal(
  workspaceId: string,
  proposalId: string,
  notes?: string,
) {
  return chronosAction(workspaceId, proposalId, "approve", notes);
}

export function archiveChronosProposal(
  workspaceId: string,
  proposalId: string,
  notes?: string,
) {
  return chronosAction(workspaceId, proposalId, "archive", notes);
}

export function deleteChronosProposal(workspaceId: string, proposalId: string) {
  return request<{ deleted: boolean; proposal_id: string }>(
    `/workspaces/${workspaceId}/chronos/proposals/${proposalId}`,
    { method: "DELETE" },
  );
}

export function listChronosProposalReviewEvents(
  workspaceId: string,
  proposalId: string,
) {
  return request<ProposalReviewEvent[]>(
    `/workspaces/${workspaceId}/chronos/proposals/${proposalId}/review-events`,
  );
}

// ---------- External Integration Readiness (dry-run only) ----------

export function createSignalDraftIntegrationIntent(
  workspaceId: string,
  draftId: string,
  payload: IntegrationIntentCreateRequest,
) {
  return request<ExternalIntegrationIntent>(
    `/workspaces/${workspaceId}/signal/drafts/${draftId}/integration-intent`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function createChronosProposalIntegrationIntent(
  workspaceId: string,
  proposalId: string,
  payload: IntegrationIntentCreateRequest,
) {
  return request<ExternalIntegrationIntent>(
    `/workspaces/${workspaceId}/chronos/proposals/${proposalId}/integration-intent`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function listIntegrationIntents(params?: IntegrationIntentListParams) {
  const qs = new URLSearchParams();
  if (params?.workspace_id) qs.set("workspace_id", params.workspace_id);
  if (params?.source_type) qs.set("source_type", params.source_type);
  if (params?.status) qs.set("status", params.status);
  if (params?.agent_name) qs.set("agent_name", params.agent_name);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<ExternalIntegrationIntent[]>(`/integration/intents${suffix}`);
}

export function getIntegrationIntent(intentId: string) {
  return request<ExternalIntegrationIntent>(`/integration/intents/${intentId}`);
}

export function validateIntegrationIntent(intentId: string) {
  return request<ExternalIntegrationIntent>(
    `/integration/intents/${intentId}/validate`,
    { method: "POST" },
  );
}

export function confirmIntegrationIntent(intentId: string, notes?: string) {
  return request<ExternalIntegrationIntent>(
    `/integration/intents/${intentId}/confirm`,
    { method: "POST", body: JSON.stringify({ notes: notes ?? null }) },
  );
}

export function cancelIntegrationIntent(intentId: string, notes?: string) {
  return request<ExternalIntegrationIntent>(
    `/integration/intents/${intentId}/cancel`,
    { method: "POST", body: JSON.stringify({ notes: notes ?? null }) },
  );
}

export function listIntegrationIntentEvents(intentId: string) {
  return request<ExternalIntegrationEvent[]>(
    `/integration/intents/${intentId}/events`,
  );
}

export function dryRunIntegrationIntent(intentId: string) {
  return request<ExternalIntegrationIntent>(
    `/integration/intents/${intentId}/dry-run`,
    { method: "POST" },
  );
}

// ---------- Integration Readiness Queue (v0.6) ----------
// Internal future-action intents from approved drafts/proposals. Never executes.

export function listReadinessIntents() {
  return request<ExternalIntegrationIntent[]>(`/integration-intents`);
}

export function getReadinessIntent(intentId: string) {
  return request<ExternalIntegrationIntent>(`/integration-intents/${intentId}`);
}

export function createEmailIntentFromDraft(draftId: string, notes?: string) {
  return request<ExternalIntegrationIntent>(
    `/integration-intents/from-draft/${draftId}`,
    { method: "POST", body: JSON.stringify({ notes: notes ?? null }) },
  );
}

export function createCalendarIntentFromProposal(proposalId: string, notes?: string) {
  return request<ExternalIntegrationIntent>(
    `/integration-intents/from-proposal/${proposalId}`,
    { method: "POST", body: JSON.stringify({ notes: notes ?? null }) },
  );
}

export function cancelReadinessIntent(intentId: string, notes?: string) {
  return request<ExternalIntegrationIntent>(
    `/integration-intents/${intentId}/cancel`,
    { method: "PATCH", body: JSON.stringify({ notes: notes ?? null }) },
  );
}

export function confirmReadinessIntent(intentId: string, notes?: string) {
  return request<ExternalIntegrationIntent>(
    `/integration-intents/${intentId}/confirm`,
    { method: "POST", body: JSON.stringify({ notes: notes ?? null }) },
  );
}

export function revokeReadinessIntent(intentId: string, notes?: string) {
  return request<ExternalIntegrationIntent>(
    `/integration-intents/${intentId}/revoke`,
    { method: "POST", body: JSON.stringify({ notes: notes ?? null }) },
  );
}

export function simulateIntentReadiness(intentId: string) {
  return request<OAuthReadinessResult>(
    `/integration-intents/${intentId}/simulate-readiness`,
    { method: "POST" },
  );
}

export function getIntentReadiness(intentId: string) {
  return request<OAuthReadinessResult>(
    `/integration-intents/${intentId}/readiness`,
  );
}

export function checkIntentReadiness(intentId: string) {
  return request<OAuthReadinessResult>(
    `/integration-intents/${intentId}/check-readiness`,
    { method: "POST" },
  );
}

export function getExecutionStatus() {
  return request<ExecutionStatus>(`/integration-intents/execution-status`);
}

export function simulateProviderPayload(intentId: string) {
  return request<CredentialUsageSimulation>(
    `/integration-intents/${intentId}/simulate-provider-payload`,
    { method: "POST" },
  );
}

// Human Approval Execution Console v1.4
export function listExecutionApprovals(filters?: {
  provider_type?: string;
  source_type?: string;
  status?: string;
}) {
  const q = new URLSearchParams();
  if (filters?.provider_type) q.set("provider_type", filters.provider_type);
  if (filters?.source_type) q.set("source_type", filters.source_type);
  if (filters?.status) q.set("status", filters.status);
  const qs = q.toString();
  return request<ExecutionApprovalListItem[]>(
    `/execution-approvals${qs ? `?${qs}` : ""}`,
  );
}

export function getExecutionApproval(intentId: string) {
  return request<ExecutionApprovalView>(`/execution-approvals/${intentId}`);
}

export function approveExecutionIntent(intentId: string, comment?: string) {
  return request<ExecutionApprovalView>(
    `/execution-approvals/${intentId}/approve`,
    { method: "POST", body: JSON.stringify({ comment: comment ?? null }) },
  );
}

export function rejectExecutionIntent(intentId: string, comment?: string) {
  return request<ExecutionApprovalView>(
    `/execution-approvals/${intentId}/reject`,
    { method: "POST", body: JSON.stringify({ comment: comment ?? null }) },
  );
}

export function runFinalSafetyCheck(intentId: string) {
  return request<FinalInterlockResult>(
    `/execution-approvals/${intentId}/final-safety-check`,
    { method: "POST" },
  );
}

// External Provider Execution Adapter Skeleton v1.6
export function listExecutionAdapters() {
  return request<{ adapters: ExecutionAdapterInfo[]; real_execution_enabled: boolean }>(
    `/execution-adapters`,
  );
}

export function simulateAdapterPayload(intentId: string) {
  return request<AdapterSimulationResult>(
    `/execution-adapters/${intentId}/simulate`,
    { method: "POST" },
  );
}

export function runBlockedExecutionCheck(intentId: string) {
  return request<AdapterBlockedResult>(
    `/execution-adapters/${intentId}/blocked-execution-check`,
    { method: "POST" },
  );
}

// Execution Governance Dashboard v1.8
export function getExecutionGovernanceDashboard(filters?: {
  provider_name?: string;
  action_type?: string;
  status?: string;
  date_from?: string;
  date_to?: string;
  workspace_id?: string;
  user_id?: string;
}) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(filters ?? {})) {
    if (v) q.set(k, v);
  }
  const qs = q.toString();
  return request<GovernanceDashboard>(
    `/execution-governance/dashboard${qs ? `?${qs}` : ""}`,
  );
}

// Provider Execution Feature Flag Matrix v1.7
export function listProviderFeatureFlags(filters?: {
  provider_name?: string;
  action_type?: string;
  environment?: string;
}) {
  const q = new URLSearchParams();
  if (filters?.provider_name) q.set("provider_name", filters.provider_name);
  if (filters?.action_type) q.set("action_type", filters.action_type);
  if (filters?.environment) q.set("environment", filters.environment);
  const qs = q.toString();
  return request<{ flags: ProviderFeatureFlag[]; external_execution_enabled: boolean }>(
    `/provider-feature-flags${qs ? `?${qs}` : ""}`,
  );
}

export function updateProviderFeatureFlag(
  flagId: string,
  changes: Partial<
    Pick<ProviderFeatureFlag, "enabled" | "dry_run_only" | "requires_human_approval">
  >,
) {
  return request<ProviderFeatureFlag>(`/provider-feature-flags/${flagId}`, {
    method: "PATCH",
    body: JSON.stringify(changes),
  });
}

// Execution kill-switch admin override
export function listExecutionSwitches() {
  return request<{ switches: ExecutionSwitch[] }>(`/admin/execution-switches`);
}

export function updateExecutionSwitch(name: string, enabled: boolean) {
  return request<ExecutionSwitch>(`/admin/execution-switches/${name}`, {
    method: "PATCH",
    body: JSON.stringify({ enabled }),
  });
}

export function clearExecutionSwitch(name: string) {
  return request<ExecutionSwitch>(`/admin/execution-switches/${name}`, {
    method: "DELETE",
  });
}

export function simulateProviderExecution(intentId: string, provider?: string) {
  const q = provider ? `?provider=${encodeURIComponent(provider)}` : "";
  return request<ProviderExecutionResult>(
    `/provider-execution/${intentId}/simulate${q}`,
    { method: "POST" },
  );
}

export function executeProviderExecution(intentId: string, provider?: string) {
  const q = provider ? `?provider=${encodeURIComponent(provider)}` : "";
  return request<ProviderExecutionResult>(
    `/provider-execution/${intentId}/execute${q}`,
    { method: "POST" },
  );
}

export function executeIntent(intentId: string) {
  return request<ExternalIntegrationIntent>(
    `/integration-intents/${intentId}/execute`,
    { method: "POST" },
  );
}

// ---------- Integration providers (dry-run scaffolding) ----------

export function listIntegrationProviders() {
  return request<ExternalProviderConnector[]>(`/integration/providers`);
}

// ---------- Provider OAuth connectors (Credential Vault v0.6) ----------

export function listProviderConnectors() {
  return request<ProviderOAuthConnector[]>(`/provider-connectors`);
}

export function getProviderReadiness() {
  return request<ProviderReadiness>(`/provider-connectors/readiness`);
}

export function registerProviderPlaceholder(payload: {
  provider_name: string;
  scopes?: string[];
}) {
  return request<ProviderOAuthConnector>(
    `/provider-connectors/register-placeholder`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function disconnectProviderConnector(connectorId: string) {
  return request<ProviderOAuthConnector>(
    `/provider-connectors/${connectorId}/disconnect`,
    { method: "PATCH" },
  );
}

// ---------- Real OAuth Flow v1.1 ----------

export function getOAuthProviders() {
  return request<OAuthProvidersResponse>(`/oauth/providers`);
}

export function startOAuth(providerName: string) {
  return request<OAuthStartResult>(`/oauth/${providerName}/start`);
}

export function refreshOAuth(providerName: string) {
  return request<OAuthProviderStatus>(`/oauth/${providerName}/refresh`, {
    method: "POST",
  });
}

export function getOAuthStatus(providerName: string) {
  return request<OAuthProviderStatus>(`/oauth/${providerName}/status`);
}

export function getIntegrationProvider(providerName: string) {
  return request<ExternalProviderConnector>(
    `/integration/providers/${providerName}`,
  );
}

export function updateIntegrationProvider(
  providerName: string,
  payload: ExternalProviderConnectorUpdateRequest,
) {
  return request<ExternalProviderConnector>(
    `/integration/providers/${providerName}`,
    { method: "PATCH", body: JSON.stringify(payload) },
  );
}

// ---------- Credential Vault (v0.6, readiness/dry-run only) ----------

export function listIntegrationCredentials(params?: {
  workspace_id?: string;
  provider_name?: string;
}) {
  const qs = new URLSearchParams();
  if (params?.workspace_id) qs.set("workspace_id", params.workspace_id);
  if (params?.provider_name) qs.set("provider_name", params.provider_name);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<ExternalProviderCredential[]>(
    `/integration/credentials${suffix}`,
  );
}

export function createIntegrationCredential(
  payload: ExternalProviderCredentialCreateRequest,
) {
  return request<ExternalProviderCredential>(`/integration/credentials`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getIntegrationCredential(credentialId: string) {
  return request<ExternalProviderCredential>(
    `/integration/credentials/${credentialId}`,
  );
}

export function updateIntegrationCredential(
  credentialId: string,
  payload: ExternalProviderCredentialUpdateRequest,
) {
  return request<ExternalProviderCredential>(
    `/integration/credentials/${credentialId}`,
    { method: "PATCH", body: JSON.stringify(payload) },
  );
}

export function disableIntegrationCredential(credentialId: string) {
  return request<ExternalProviderCredential>(
    `/integration/credentials/${credentialId}/disable`,
    { method: "POST" },
  );
}

export function markCredentialNeedsAuthorization(credentialId: string) {
  return request<ExternalProviderCredential>(
    `/integration/credentials/${credentialId}/mark-needs-authorization`,
    { method: "POST" },
  );
}

export function validateCredentialPlaceholder(credentialId: string) {
  return request<ExternalProviderCredential>(
    `/integration/credentials/${credentialId}/validate-placeholder`,
    { method: "POST" },
  );
}

export function rotateCredentialPlaceholder(credentialId: string) {
  return request<ExternalProviderCredential>(
    `/integration/credentials/${credentialId}/rotate-placeholder`,
    { method: "POST" },
  );
}

export function listCredentialEvents(credentialId: string) {
  return request<ExternalProviderCredentialEvent[]>(
    `/integration/credentials/${credentialId}/events`,
  );
}

// ---------- Delegations ----------

export function listDelegations(params?: {
  limit?: number;
  session_id?: string;
  plan_id?: string;
  workspace_id?: string;
  status?: string;
}) {
  const qs = new URLSearchParams();
  if (params?.limit) qs.set("limit", String(params.limit));
  if (params?.session_id) qs.set("session_id", params.session_id);
  if (params?.plan_id) qs.set("plan_id", params.plan_id);
  if (params?.workspace_id) qs.set("workspace_id", params.workspace_id);
  if (params?.status) qs.set("status", params.status);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<AgentDelegation[]>(`/delegations${suffix}`);
}

export function getDelegation(id: string) {
  return request<AgentDelegation>(`/delegations/${id}`);
}

export function listPlanDelegations(planId: string) {
  return request<AgentDelegation[]>(`/plans/${planId}/delegations`);
}
