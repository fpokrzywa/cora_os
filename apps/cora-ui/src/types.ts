export type Role = "user" | "assistant" | "system";

export interface ChatMessage {
  id?: number;
  role: Role;
  content: string;
  created_at?: string;
}

export interface ChatResponse {
  session_id: string;
  agent: string;
  selected_agent: string;
  routing_matched_keywords?: string[];
  model_endpoint: string | null;
  response: string;
  placeholder: boolean;
  created_at: string;
}

// ---- Agent runtime (Cora Configuration) ----
export interface AgentRuntimeConfig {
  runtime_enabled: boolean;
  delegation_enabled: boolean;
  write_enabled: boolean;
  eval_enabled: boolean;
  max_steps: number;
  max_parallel: number;
  chat_model: string;
  eval_model: string;
  endpoint_configured: boolean;
}

export interface AgentEvaluation {
  verdict: "pass" | "concerns" | "fail";
  reasons: string[];
  summary: string;
  model?: string;
}

export interface AgentAsyncResponse {
  run_id: string;
  status: string;
}

export interface AgentStagedArtifact {
  tool: string;
  summary: string;
}

export interface AgentInterrupt {
  staged: AgentStagedArtifact[];
  decision: "approve" | "reject" | null;
  note: string | null;
}

export interface AgentRunStep {
  kind: "tool_call" | "tool_result" | "final" | "error";
  name?: string;
  arguments?: Record<string, unknown>;
  result?: string;
  answer?: string;
  error?: string;
}

export interface AgentRunResponse {
  run_id: string | null;
  answer: string;
  model: string;
  tool_calls: number;
  stopped: "final" | "budget" | "error";
  steps: AgentRunStep[];
  evaluation?: AgentEvaluation | null;
  status: string; // done | failed | waiting_user
  interrupt?: AgentInterrupt | null;
}

// ---- Runs / task-manager view (Cora Configuration → Runs) ----
export interface AgentRunSummary {
  id: string;
  session_id: string | null;
  agent_name: string | null; // null = orchestrator (ATLAS); else the spoke
  status: string;
  goal: string;
  model_name: string | null;
  tool_calls: number;
  step_count: number;
  max_steps: number;
  stopped: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface AgentSpokeRun {
  id: string;
  agent_name: string | null;
  status: string;
  stopped: string | null;
  answer: string | null;
  tool_calls: number;
  step_count: number;
  steps: AgentRunStep[];
}

export interface AgentDelegationNode {
  id: string;
  from_agent: string;
  to_agent: string;
  delegation_reason: string | null;
  status: string;
  output_payload: Record<string, unknown> | null;
  created_at: string;
  completed_at: string | null;
  spoke_run: AgentSpokeRun | null;
}

export interface AgentRunDetail extends AgentRunSummary {
  answer: string | null;
  error_message: string | null;
  steps: AgentRunStep[];
  delegations: AgentDelegationNode[];
  evaluation?: AgentEvaluation | null;
  interrupt?: AgentInterrupt | null;
}

export interface ConversationSummary {
  session_id: string;
  created_at: string;
  updated_at: string;
  message_count: number;
  last_message_at: string | null;
  title: string | null;
  pinned: boolean;
  deleted_at: string | null;
  title_source: string | null;
}

export interface AgentRun {
  id: number;
  agent: string;
  model_name: string | null;
  user_message: string;
  assistant_response: string | null;
  placeholder: boolean;
  started_at: string;
  completed_at: string | null;
}

export interface ConversationDetail {
  session_id: string;
  created_at: string;
  updated_at: string;
  messages: ChatMessage[];
  agent_runs: AgentRun[];
}

export interface MemoryEntry {
  id: string;
  source_session_id: string | null;
  type: string;
  title: string;
  content: string;
  tags: string[];
  importance: number;
  created_at: string;
  updated_at: string;
}

export interface MemorySearchResult {
  id: string;
  title: string;
  type: string;
  content_preview: string;
  tags: string[];
  importance: number;
  created_at: string;
}

export interface SemanticSearchResult {
  id: string;
  title: string;
  type: string;
  content_preview: string;
  tags: string[];
  importance: number;
  scope_type: string;
  workspace_id: string | null;
  similarity: number;
  created_at: string;
}

export interface SemanticSearchResponse {
  status: string;
  semantic_unavailable: boolean;
  reason: string | null;
  results: SemanticSearchResult[];
}

export interface EmbeddingsStatus {
  pgvector_available: boolean;
  embedding_configured: boolean;
  embedding_model_name: string | null;
  embedding_endpoint: string | null;
  embedding_dim: number;
  total_entries: number;
  embedded_entries: number;
  missing_count: number;
  storage: string;
}

export interface EmbedResult {
  status: string;
  semantic_unavailable: boolean;
  detail: Record<string, unknown>;
}

export interface User {
  id: string;
  email: string;
  display_name: string | null;
  role: string;
  created_at: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  user: User;
  impersonated?: boolean;
}

export interface AdminUser {
  id: string;
  email: string;
  display_name: string | null;
  role: string;
  created_at: string;
  updated_at: string;
}

export interface AdminMemoryEntry {
  id: string;
  source_session_id: string | null;
  type: string;
  title: string;
  content: string;
  tags: string[];
  importance: number;
  scope_type: "user" | "global" | "system";
  scope_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface MemoryPreview {
  id: string;
  title: string;
  type: string;
  scope_type: "user" | "global" | "system";
  scope_id: string | null;
  tags: string[];
  importance: number;
  content_preview: string;
}

export interface VisibilityTestResponse {
  user_id: string;
  user_email: string;
  user_role: string;
  scope_filter: string;
  list_visible_count: number;
  list_visible: MemoryPreview[];
  query: string | null;
  search_match_count: number;
  search_top_in_prompt: MemoryPreview[];
}

export type AgentType = "orchestrator" | "subagent" | "memory" | "tool_agent";
export type AgentVersionStatus = "draft" | "active" | "archived";

export interface Agent {
  id: string;
  name: string;
  display_name: string;
  description: string | null;
  agent_type: AgentType;
  enabled: boolean;
  current_version_id: string | null;
  current_version_number: number | null;
  created_at: string;
  updated_at: string;
}

export interface AgentVersionMetadata {
  routing_keywords?: string[];
  specializations?: string[];
  change_summary?: string;
  [key: string]: unknown;
}

export interface AgentVersion {
  id: string;
  agent_id: string;
  version_number: number;
  status: AgentVersionStatus;
  system_prompt: string;
  routing_keywords: string[];
  allowed_tools: string[];
  model_name: string | null;
  temperature: number;
  max_prompt_chars: number;
  notes: string | null;
  metadata: AgentVersionMetadata;
  created_by: string | null;
  created_at: string;
  activated_at: string | null;
  archived_at: string | null;
}

export interface AgentVersionCreateRequest {
  system_prompt: string;
  routing_keywords?: string[];
  allowed_tools?: string[];
  model_name?: string | null;
  temperature?: number;
  max_prompt_chars?: number;
  notes?: string | null;
  metadata?: AgentVersionMetadata;
  activate?: boolean;
}

export interface AgentDetail extends Agent {
  versions: AgentVersion[];
}

export interface RoutingTestRequest {
  message: string;
  workspace_id?: string | null;
  include_prompt_preview?: boolean;
}

export interface RoutingTestResult {
  selected_agent: string;
  scores: Record<string, number>;
  matched_keywords: Record<string, string[]>;
  tie_break_applied: boolean;
  prompt_source: string;
  active_version: number | null;
  prompt_preview: string | null;
  would_delegate: boolean;
  delegation_from: string | null;
  delegation_to: string | null;
}

export interface ResponseTestRequest {
  message: string;
  workspace_id?: string | null;
  agent_name?: string | null;
  include_memory?: boolean;
}

export interface ResponseTestResult {
  selected_agent: string;
  prompt_source: string;
  active_version: number | null;
  response: string;
  test_run: boolean;
}

export interface McpServer {
  id: string;
  name: string;
  description: string | null;
  server_type: string;
  endpoint: string;
  enabled: boolean;
  auth_type: string | null;
  auth_config: Record<string, unknown> | null;
  capabilities: McpCapabilities | null;
  created_at: string;
  updated_at: string;
}

export interface McpCapabilities {
  tools?: Array<{
    name: string;
    description: string | null;
    input_schema: unknown;
  }>;
  resources?: Array<{
    uri: string;
    name: string | null;
    description: string | null;
    mime_type: string | null;
  }>;
  server_info?: Record<string, unknown>;
}

export interface McpTestResult {
  server_name: string;
  success: boolean;
  duration_ms: number;
  error: string | null;
}

export interface McpCapabilitiesResponse {
  server_name: string;
  cached: boolean;
  capabilities: McpCapabilities | null;
}

export type ToolRiskLevel = "low" | "medium" | "high";

export interface ToolAdminRow {
  id: string;
  name: string;
  description: string | null;
  type: string;
  endpoint: string | null;
  enabled: boolean;
  requires_confirmation: boolean;
  mcp_server_name: string | null;
  mcp_action_name: string | null;
  input_schema: Record<string, unknown> | null;
  output_schema: Record<string, unknown> | null;
  risk_level: ToolRiskLevel;
  allowed_agents: string[];
  created_at: string;
  updated_at: string;
}

export interface ToolTestResult {
  tool_name: string;
  type: string;
  status: string;
  duration_ms?: number | null;
  mcp_server?: string | null;
  mcp_action?: string | null;
  http_status?: number | null;
  response?: unknown;
  error?: string | null;
}

export interface ToolPolicy {
  id: string;
  tool_name: string;
  agent_name: string;
  allowed: boolean;
  requires_confirmation: boolean;
  max_calls_per_hour: number | null;
  created_at: string;
  updated_at: string;
}

export interface ExecutionLog {
  id: string;
  session_id: string | null;
  user_id: string | null;
  tool_name: string;
  agent_name: string | null;
  scope_type: string | null;
  allowed: boolean;
  duration_ms: number | null;
  status: string;
  error_message: string | null;
  created_at: string;
}

export interface GovernanceStats {
  window_hours: number;
  tools: Array<{
    tool_name: string;
    allowed_count: number;
    denied_count: number;
    error_count: number;
    last_used_at: string | null;
  }>;
}

export type PlanStatus =
  | "planned"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export type PlanStepStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "skipped";

export interface ExecutionPlanStep {
  id: string;
  plan_id: string;
  step_number: number;
  title: string;
  description: string | null;
  assigned_agent: string | null;
  tool_name: string | null;
  status: PlanStepStatus;
  result: Record<string, unknown> | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
}

export interface ExecutionPlan {
  id: string;
  session_id: string | null;
  user_id: string | null;
  title: string;
  goal: string;
  status: PlanStatus;
  current_step: number;
  total_steps: number;
  selected_agent: string | null;
  created_at: string;
  updated_at: string;
}

export interface ExecutionPlanDetail extends ExecutionPlan {
  steps: ExecutionPlanStep[];
}

export type DelegationStatus = "pending" | "running" | "completed" | "failed";

export interface AgentDelegation {
  id: string;
  workspace_id: string | null;
  session_id: string | null;
  execution_plan_id: string | null;
  from_agent: string;
  to_agent: string;
  delegation_reason: string | null;
  status: DelegationStatus;
  input_payload: Record<string, unknown> | null;
  output_payload: Record<string, unknown> | null;
  created_at: string;
  completed_at: string | null;
}

export interface Workspace {
  id: string;
  owner_user_id: string | null;
  name: string;
  slug: string;
  description: string | null;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface WorkspaceDetail extends Workspace {
  counts: Record<string, number>;
}

export interface AgentSummary {
  name: string;
  display_name: string;
  agent_type: string;
  enabled: boolean;
  current_version_number: number | null;
}

export interface ToolSummary {
  name: string;
  type: string;
  enabled: boolean;
  risk_level: string;
  allowed_agents: string[];
  mcp_server_name: string | null;
  mcp_action_name: string | null;
}

export interface McpServerSummary {
  name: string;
  server_type: string;
  endpoint: string;
  enabled: boolean;
  capabilities_cached: boolean;
}

export interface ProjectFile {
  name: string;
  type: string;
  size_bytes: number | null;
}

export interface TraceSummary {
  id: string;
  trace_type: string;
  selected_agent: string | null;
  status: string;
  duration_ms: number | null;
  created_at: string;
}

export type KnowledgeScope = "user" | "global" | "system";

export type KnowledgeSourceType =
  | "manual_note"
  | "markdown"
  | "text_file"
  | "url"
  | "generated_summary"
  | "system_seed";

export interface KnowledgeEntry {
  id: string;
  workspace_id: string | null;
  title: string;
  type: string;
  scope_type: KnowledgeScope;
  scope_id: string | null;
  tags: string[];
  importance: number;
  embedded: boolean;
  embedded_at: string | null;
  source_id: string | null;
  duplicate_warning?: boolean;
  created_at: string;
  updated_at: string;
}

export interface BulkKnowledgeResponse {
  created: number;
  embedded: number;
  skipped: number;
  duplicates: number;
  entries: KnowledgeEntry[];
}

export interface UrlIngestRequest {
  url: string;
  title?: string;
  scope_type?: KnowledgeScope;
  auto_embed?: boolean;
}

export interface UrlIngestResponse {
  source_id: string | null;
  memory_entry_id: string;
  title: string;
  url: string;
  content_length: number;
  content_type?: string | null;
  page_count?: number | null;
  duplicate: boolean;
  embedded: boolean;
}

export interface KnowledgeSourceMetadata {
  fetched_at?: string;
  last_checked_at?: string;
  last_changed_at?: string;
  last_error?: string;
  previous_content_hash?: string;
  status_code?: number;
  content_type?: string;
  extraction_method?: string;
  page_count?: number | null;
  title_source?: string;
  [key: string]: unknown;
}

export interface KnowledgeSource {
  id: string;
  workspace_id: string | null;
  uploaded_by: string | null;
  source_type: KnowledgeSourceType;
  title: string;
  description: string | null;
  original_filename: string | null;
  source_url: string | null;
  content_hash: string | null;
  status: string;
  linked_memory_count: number;
  metadata?: KnowledgeSourceMetadata | null;
  created_at: string;
  updated_at: string;
}

export interface NewsArticleOut {
  source_id: string;
  memory_entry_id: string;
  title: string;
  url: string | null;
  published_at?: string | null;
}

export interface NewsIngestResponse {
  status: "ok" | "partial" | "error";
  feed_source_id: string | null;
  source_name: string;
  feed_url: string;
  items_seen: number;
  articles_created: number;
  articles_updated: number;
  articles_skipped_duplicate: number;
  article_bodies_fetched: number;
  article_body_fetch_failures: number;
  errors_count: number;
  embedded: number;
  errors: string[];
  created_articles: NewsArticleOut[];
}

export interface KnowledgeNewsFeed {
  id: string;
  workspace_id: string | null;
  source_name: string;
  feed_url: string | null;
  scope_type: KnowledgeScope;
  importance: number;
  max_items: number;
  auto_embed: boolean;
  fetch_article_body: boolean;
  refresh_enabled: boolean;
  refresh_interval_minutes: number | null;
  next_refresh_at: string | null;
  last_checked_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
  last_result: Record<string, number> | null;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeNewsFeedRegisterRequest {
  source_name?: string;
  feed_url: string;
  max_items?: number;
  scope_type?: KnowledgeScope;
  importance?: number;
  auto_embed?: boolean;
  fetch_article_body?: boolean;
  refresh_enabled?: boolean;
  refresh_interval_minutes?: number | null;
  ingest_now?: boolean;
}

export interface KnowledgeNewsFeedUpdateRequest {
  source_name?: string;
  max_items?: number;
  scope_type?: KnowledgeScope;
  importance?: number;
  auto_embed?: boolean;
  fetch_article_body?: boolean;
  refresh_enabled?: boolean;
  refresh_interval_minutes?: number | null;
}

export interface KnowledgeNewsFeedRefreshResponse extends NewsIngestResponse {
  next_refresh_at: string | null;
}

export interface NewsBriefingArticle {
  source_id: string;
  title: string;
  source_url: string | null;
  source_type: string;
  source_name: string | null;
  feed_url: string | null;
  published_at: string | null;
  created_at: string;
  content_length: number;
  article_body_fetched: boolean;
  article_fetch_status: string | null;
  chunk_count: number;
  embedded_chunk_count: number;
  short_preview: string;
}

export interface NewsBriefingResponse {
  total_articles: number;
  feeds_represented: number;
  source_names: string[];
  since_hours: number;
  max_articles: number;
  article_body_fetch_success_count: number;
  article_body_fetch_failure_count: number;
  chunked_article_count: number;
  include_summary: boolean;
  summary: string | null;
  summary_generated: boolean;
  articles: NewsBriefingArticle[];
}

export interface NewsBriefingParams {
  since_hours?: number;
  max_articles?: number;
  source_name?: string;
  include_summary?: boolean;
}

export interface SourceRefreshResponse {
  status: "unchanged" | "updated";
  source_id: string;
  url: string;
  content_chars: number;
  old_hash: string | null;
  new_hash: string | null;
  title: string | null;
  linked_updated: number;
  embedded: number;
}

export interface KnowledgeSourceDetail extends KnowledgeSource {
  content: string | null;
  linked_memories: KnowledgeEntry[];
}

export interface WorkspaceContext {
  workspace: Workspace;
  memory: {
    total: number;
    embedded: number;
    missing: number;
    pgvector_available: boolean;
  };
  plans: { total: number; active: number };
  jobs: { active: number; failed: number };
  recent_conversations_count: number;
  recent_traces: TraceSummary[];
  agents: AgentSummary[];
  tools: ToolSummary[];
  mcp_servers: McpServerSummary[];
  project_files: ProjectFile[];
  project_files_source: string;
  project_files_error: string | null;
}

export type JobStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export interface Job {
  id: string;
  user_id: string | null;
  session_id: string | null;
  plan_id: string | null;
  step_id: string | null;
  job_type: string;
  status: JobStatus;
  payload: Record<string, unknown> | null;
  result: Record<string, unknown> | null;
  error_message: string | null;
  attempts: number;
  max_attempts: number;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface QueueStepResponse {
  job_id: string;
  plan_id: string;
  step_id: string;
  status: JobStatus;
  job_type: string;
  created_at: string;
}

export interface RuntimeTrace {
  id: string;
  session_id: string | null;
  user_id: string | null;
  trace_type: string;
  selected_agent: string | null;
  user_message: string | null;
  memory_count: number;
  memory_ids: string[];
  tool_name: string | null;
  tool_result: unknown;
  mcp_server_name: string | null;
  mcp_action_name: string | null;
  model_name: string | null;
  model_endpoint: string | null;
  duration_ms: number | null;
  status: string;
  error_message: string | null;
  metadata: Record<string, unknown> | null;
  created_at: string;
}

// ---------- SIGNAL communication drafts (Governed Tool Planning v0.1) ----------
// Review-only. No send path; status stays within draft/reviewed/approved/archived.

export type CommunicationDraftStatus =
  | "draft"
  | "in_review"
  | "changes_requested"
  | "reviewed"
  | "approved"
  | "archived";

export interface CommunicationDraft {
  id: string;
  workspace_id: string | null;
  created_by: string | null;
  agent_name: string;
  draft_type: string;
  title: string | null;
  recipient_hint: string | null;
  subject: string | null;
  body: string;
  tone: string | null;
  status: CommunicationDraftStatus;
  metadata: Record<string, unknown>;
  reviewed_by: string | null;
  reviewed_at: string | null;
  approved_by: string | null;
  approved_at: string | null;
  archived_at: string | null;
  review_notes: string | null;
  created_at: string;
  updated_at: string;
}

export interface ReviewActionRequest {
  notes?: string;
}

export interface DraftReviewEvent {
  id: string;
  draft_id: string;
  user_id: string | null;
  action: string;
  from_status: string | null;
  to_status: string | null;
  notes: string | null;
  created_at: string;
}

export interface CommunicationDraftCreateRequest {
  draft_type: string;
  body: string;
  title?: string | null;
  subject?: string | null;
  recipient_hint?: string | null;
  tone?: string | null;
  metadata?: Record<string, unknown>;
}

export interface CommunicationDraftUpdateRequest {
  draft_type?: string;
  title?: string | null;
  subject?: string | null;
  recipient_hint?: string | null;
  body?: string;
  tone?: string | null;
  status?: CommunicationDraftStatus;
  metadata?: Record<string, unknown>;
}

// ---------- CHRONOS schedule proposals (Governed Tool Planning v0.1) ----------
// Review-only. No calendar write; status stays within proposed/reviewed/approved/archived.

export type ScheduleProposalStatus =
  | "proposed"
  | "in_review"
  | "changes_requested"
  | "reviewed"
  | "approved"
  | "archived";

export interface ScheduleProposal {
  id: string;
  workspace_id: string | null;
  created_by: string | null;
  agent_name: string;
  proposal_type: string;
  title: string;
  description: string | null;
  start_time: string | null;
  end_time: string | null;
  timezone: string | null;
  attendees: unknown[];
  agenda: unknown[];
  reminders: unknown[];
  status: ScheduleProposalStatus;
  metadata: Record<string, unknown>;
  reviewed_by: string | null;
  reviewed_at: string | null;
  approved_by: string | null;
  approved_at: string | null;
  archived_at: string | null;
  review_notes: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProposalReviewEvent {
  id: string;
  proposal_id: string;
  user_id: string | null;
  action: string;
  from_status: string | null;
  to_status: string | null;
  notes: string | null;
  created_at: string;
}

// ---------- External Integration Readiness v0.4 (dry-run only) ----------

export type IntegrationIntentStatus =
  | "draft"
  | "ready_for_confirmation"
  | "confirmed"
  | "blocked"
  | "cancelled"
  | "executed_placeholder"
  // Integration Readiness Queue (v0.6) statuses
  | "pending_provider"
  | "blocked_no_provider"
  | "blocked_no_oauth"
  | "ready_for_future_execution"
  // Execution Approval Gate (v0.7)
  | "confirmation_revoked";

export type IntegrationPayloadPreview = Record<string, unknown>;

export interface IntegrationValidationResult {
  hard_errors?: string[];
  warnings?: string[];
  hard_error_count?: number;
  warning_count?: number;
  ok?: boolean;
}

export interface ExternalIntegrationIntent {
  id: string;
  workspace_id: string | null;
  created_by: string | null;
  source_type: "communication_draft" | "schedule_proposal";
  source_id: string;
  agent_name: string;
  provider_type: "email" | "calendar";
  provider_name: string;
  action_type: string;
  status: IntegrationIntentStatus;
  dry_run: boolean;
  requires_confirmation: boolean;
  confirmation_required_reason: string | null;
  payload_preview: IntegrationPayloadPreview;
  validation_result: IntegrationValidationResult;
  metadata: Record<string, unknown>;
  confirmed_by: string | null;
  confirmed_at: string | null;
  cancelled_by: string | null;
  cancelled_at: string | null;
  created_at: string;
  updated_at: string;
  safety_note?: string;
}

export interface ExternalIntegrationEvent {
  id: string;
  intent_id: string;
  user_id: string | null;
  event_type: string;
  from_status: string | null;
  to_status: string | null;
  notes: string | null;
  payload_snapshot: Record<string, unknown>;
  created_at: string;
}

export interface IntegrationIntentCreateRequest {
  provider_name?: string;
  action_type?: string;
  notes?: string;
}

export interface IntegrationIntentListParams {
  workspace_id?: string;
  source_type?: string;
  status?: string;
  agent_name?: string;
}

// ---------- External Provider Connectors v0.5 (dry-run scaffolding) ----------

export interface ExternalProviderConnector {
  id: string;
  provider_name: string;
  provider_type: "email" | "calendar" | "notification";
  display_name: string;
  description: string | null;
  enabled: boolean;
  dry_run_only: boolean;
  supports_send: boolean;
  supports_draft: boolean;
  supports_calendar_create: boolean;
  supports_calendar_update: boolean;
  supports_read: boolean;
  requires_oauth: boolean;
  auth_config_schema: Record<string, unknown>;
  payload_schema: Record<string, unknown>;
  capabilities: Record<string, unknown>;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  safety_note?: string;
}

export interface ExternalProviderConnectorUpdateRequest {
  enabled?: boolean;
  dry_run_only?: boolean;
  display_name?: string;
  description?: string;
  metadata?: Record<string, unknown>;
  capabilities?: Record<string, unknown>;
}

export interface ProviderValidationResult {
  hard_errors?: string[];
  warnings?: string[];
  hard_error_count?: number;
  warning_count?: number;
  ok?: boolean;
  capability?: Record<string, unknown> | null;
}

export interface ProviderDryRunResult {
  provider_name: string;
  provider_type: string;
  payload: Record<string, unknown>;
  dry_run: boolean;
  external_action_performed: boolean;
  message: string;
}

// OAuth Credential Vault v0.6 — readiness/dry-run only. Secrets are never
// present client-side; only has_* presence booleans are returned.
export type CredentialStatus =
  | "not_configured"
  | "configured"
  | "needs_authorization"
  | "authorized_placeholder"
  | "expired"
  | "revoked"
  | "disabled"
  | "error";

export interface ExternalProviderCredential {
  id: string;
  workspace_id: string | null;
  user_id: string | null;
  provider_name: string;
  provider_type: string;
  credential_name: string;
  auth_type: string;
  status: CredentialStatus;
  scopes: string[];
  client_id_hint: string | null;
  token_expires_at: string | null;
  last_authorized_at: string | null;
  last_validated_at: string | null;
  last_error: string | null;
  dry_run_only: boolean;
  metadata: Record<string, unknown>;
  has_access_token: boolean;
  has_refresh_token: boolean;
  has_client_secret: boolean;
  created_by: string | null;
  updated_by: string | null;
  created_at: string;
  updated_at: string;
  safety_note?: string;
  validation?: {
    ok: boolean;
    checks: Record<string, boolean>;
    external_action_performed: boolean;
    note: string;
  } | null;
}

export interface ExternalProviderCredentialCreateRequest {
  provider_name: string;
  provider_type: string;
  credential_name: string;
  auth_type?: string;
  scopes?: string[];
  client_id_hint?: string | null;
  workspace_id?: string | null;
  user_id?: string | null;
  dry_run_only?: boolean;
  metadata?: Record<string, unknown>;
}

export interface ExternalProviderCredentialUpdateRequest {
  credential_name?: string;
  scopes?: string[];
  client_id_hint?: string | null;
  dry_run_only?: boolean;
  metadata?: Record<string, unknown>;
}

export interface ExternalProviderCredentialEvent {
  id: string;
  credential_id: string;
  user_id: string | null;
  event_type: string;
  from_status: string | null;
  to_status: string | null;
  notes: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface ScheduleProposalCreateRequest {
  proposal_type: string;
  title: string;
  description?: string | null;
  start_time?: string | null;
  end_time?: string | null;
  timezone?: string | null;
  attendees?: unknown[];
  agenda?: unknown[];
  reminders?: unknown[];
  metadata?: Record<string, unknown>;
}

export interface ScheduleProposalUpdateRequest {
  proposal_type?: string;
  title?: string;
  description?: string | null;
  start_time?: string | null;
  end_time?: string | null;
  timezone?: string | null;
  attendees?: unknown[];
  agenda?: unknown[];
  reminders?: unknown[];
  status?: ScheduleProposalStatus;
  metadata?: Record<string, unknown>;
}

// Real OAuth Flow v1.1 — per-provider connection status (no tokens ever leave
// the server; only presence flags + metadata).
export type OAuthConnectionStatus =
  | "not_configured"
  | "ready_to_connect"
  | "connected"
  | "expired"
  | "refresh_failed";

export interface OAuthProviderStatus {
  provider_name: string;
  provider_type: "email" | "calendar";
  vendor: string;
  config_present: boolean;
  missing_config: string[];
  required_scopes: string[];
  connection_status: OAuthConnectionStatus;
  connector_id: string | null;
  readiness: Record<string, boolean>;
  ready_to_connect: boolean;
  connected: boolean;
  scopes: unknown[];
  token_expires_at: string | null;
  has_access_token: boolean;
  has_refresh_token: boolean;
  updated_at: string | null;
  encryption_available: boolean;
  execution_enabled: boolean;
  execution_note: string;
}

// Provider Credential Usage Simulation v1.3 — preview only, never executes.
export interface CredentialUsageSimulation {
  intent_id: string;
  intent_status: string;
  intent_approved: boolean;
  provider_type: string;
  provider_name: string | null;
  action_type: string;
  dry_run_only: boolean;
  validation: {
    provider_connected: boolean;
    token_valid_or_refreshable: boolean;
    required_scopes_present: boolean;
    missing_scopes: string[];
    provider_execution_disabled: boolean;
    dry_run_only: boolean;
    kill_switch_blocks_execution: boolean;
    governance_allows_execution: boolean;
  };
  payload_ready: boolean;
  payload_errors: string[];
  provider_payload_preview: Record<string, unknown> | null;
  execution_allowed: boolean;
  execution_enabled: boolean;
  blockers: string[];
  guard_blockers: string[];
  note: string;
}

// Human Approval Execution Console v1.4 — approval decisions only, never executes.
export type ExecutionApprovalState =
  | "pending_review"
  | "ready_for_approval"
  | "approved_for_execution"
  | "rejected"
  | "blocked_by_governance"
  | "cancelled";

export interface ExecutionApprovalListItem {
  intent_id: string;
  source_type: string;
  provider_type: string;
  provider_name: string | null;
  action_type: string;
  intent_status: string;
  approval_state: ExecutionApprovalState;
  can_approve: boolean;
  latest_decision: string | null;
}

export interface ExecutionApprovalView {
  intent_id: string;
  workspace_id: string | null;
  agent_name: string | null;
  source_type: string;
  source_id: string;
  provider_type: string;
  provider_name: string | null;
  action_type: string;
  intent_status: string;
  approval_state: ExecutionApprovalState;
  latest_decision: string | null;
  can_approve: boolean;
  governance: Record<string, boolean>;
  readiness: {
    provider_connected: boolean;
    token_valid_or_refreshable: boolean;
    required_scopes_present: boolean;
    missing_scopes: string[];
    source_approved: boolean;
    payload_ready: boolean;
  };
  payload_ready: boolean;
  payload_errors: string[];
  provider_payload_preview: Record<string, unknown> | null;
  payload_hash: string;
  payload_preview_ref: string;
  execution_allowed: boolean;
  execution_enabled: boolean;
  blockers: string[];
  note: string;
  feature_flag?: FeatureFlagState;
}

// Final Safety Interlock v1.5 — diagnostic only; real execution always blocked.
export type FinalInterlockStatus =
  | "blocked_by_final_interlock"
  | "ready_but_execution_disabled"
  | "missing_approval"
  | "provider_not_ready"
  | "payload_mismatch";

export interface FinalInterlockResult {
  intent_id: string;
  status: FinalInterlockStatus;
  real_execution_allowed: boolean;
  execution_enabled: boolean;
  dry_run_only: boolean;
  provider_type: string;
  provider_name: string | null;
  action_type: string;
  checks: Record<string, boolean>;
  block_reasons: string[];
  approval_evidence: {
    approved: boolean;
    approver_id: string | null;
    approved_at: string | null;
    reason: string | null;
    latest_decision: string | null;
  };
  provider_readiness: {
    provider_connected: boolean;
    token_valid_or_refreshable: boolean;
    required_scopes_present: boolean;
    missing_scopes: string[];
    provider_supports_action: boolean;
  };
  payload_hash: string;
  approved_payload_hash: string | null;
  payload_matches: boolean;
  payload_preview_ref: string;
  note: string;
}

// External Provider Execution Adapter Skeleton v1.6 — no real execution.
export interface ExecutionAdapterInfo {
  provider_name: string;
  provider_type: string;
  supported_action_types: string[];
  api_methods: Record<string, string>;
  real_execution: boolean;
}

export interface AdapterSimulationResult {
  intent_id: string;
  resolved: boolean;
  provider_name: string | null;
  provider_type?: string;
  action_type: string;
  supported_action?: boolean;
  validation_errors?: string[];
  payload_ready?: boolean;
  external_action_performed?: boolean;
  status?: string;
  reason?: string;
  simulation?: {
    provider_request: {
      api_method: string;
      would_send: boolean;
      request: Record<string, unknown>;
    };
    note: string;
  };
}

export interface AdapterBlockedResult {
  intent_id: string;
  status: string;
  reason: string;
  real_execution_performed: boolean;
  real_execution_allowed?: boolean;
  resolved?: boolean;
  provider_name: string | null;
  action_type: string;
  interlock_status?: string;
  note?: string;
}

// Provider Execution Feature Flag Matrix v1.7
export interface ProviderFeatureFlag {
  id: string;
  provider_name: string;
  provider_type: string;
  action_type: string;
  enabled: boolean;
  dry_run_only: boolean;
  requires_human_approval: boolean;
  requires_final_interlock: boolean;
  requires_valid_oauth: boolean;
  requires_scope_validation: boolean;
  requires_connected_provider: boolean;
  requires_payload_hash_match: boolean;
  requires_kill_switch_clear: boolean;
  environment: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface ExecutionSwitch {
  name: string;
  label: string;
  description: string;
  manageable: boolean;
  env_default: boolean;
  override: boolean | null;
  overridden: boolean;
  effective: boolean;
  updated_at: string | null;
  updated_by: string | null;
}

export interface FeatureFlagState {
  present: boolean;
  enabled: boolean;
  dry_run_only: boolean;
  execution_enabled: boolean;
  requires_human_approval: boolean;
  requires_final_interlock: boolean;
  flag_allows_execution: boolean;
}

// Execution Governance Dashboard v1.8 — observability only.
export interface GovernanceTraceRow {
  created_at: string;
  trace_type: string;
  status: string;
  tool_name: string | null;
  intent_id: string | null;
  provider_name: string | null;
  action_type: string | null;
  result_status: string | null;
  reason: string | null;
}

export interface GovernanceCard {
  intent_id: string;
  provider_type: string;
  connected_provider: string | null;
  action_type: string;
  status: string;
  source_type: string;
  source_id: string;
  approval_state: string | null;
  latest_trace_status: string | null;
  latest_trace_type: string | null;
  latest_block_reason: string | null;
  feature_flag_state: { present: boolean; enabled: boolean; dry_run_only: boolean };
  provider_readiness_state: string;
}

export interface GovernanceDashboard {
  safety_banner: string;
  external_execution_enabled: boolean;
  summary: Record<string, number | boolean>;
  recent_drafts: {
    id: string; draft_type: string; subject: string | null; status: string; created_at: string;
  }[];
  recent_approval_events: {
    id: string; intent_id: string; approver_id: string | null; decision: string;
    approval_state: string; reason: string | null; payload_hash: string | null; created_at: string;
  }[];
  recent_integration_intents: {
    id: string; agent_name: string | null; source_type: string; source_id: string;
    provider_type: string; provider_name: string; action_type: string; status: string;
    dry_run: boolean; approval_state: string | null;
    payload_summary: { subject: string | null; title: string | null }; created_at: string;
  }[];
  recent_integration_events: {
    id: string; intent_id: string; event_type: string; from_status: string | null;
    to_status: string | null; created_at: string;
  }[];
  provider_readiness: {
    provider_name: string; provider_type: string; status: string; scope_count: number;
    has_access_token: boolean; has_refresh_token: boolean; token_expires_at: string | null;
    updated_at: string | null;
  }[];
  provider_readiness_summary: Record<string, number>;
  feature_flags: {
    provider_name: string; provider_type: string; action_type: string; enabled: boolean;
    dry_run_only: boolean; environment: string;
  }[];
  feature_flag_summary: Record<string, number>;
  interlock_traces: GovernanceTraceRow[];
  adapter_traces: GovernanceTraceRow[];
  governance_blocks: GovernanceTraceRow[];
  tool_failures: {
    created_at: string; tool_name: string; agent_name: string | null; status: string;
    allowed: boolean; error_message: string | null;
  }[];
  cards: GovernanceCard[];
}

export interface OAuthProvidersResponse {
  execution_enabled: boolean;
  encryption_available: boolean;
  providers: OAuthProviderStatus[];
}

export interface OAuthStartResult {
  provider_name: string;
  provider_type: string;
  authorization_url: string;
  state: string;
  status: string;
}

// Provider OAuth connectors (Credential Vault v0.6 — readiness only)
export type ProviderConnectorStatus =
  | "not_configured"
  | "oauth_required"
  | "connected"
  | "expired"
  | "disconnected"
  | "error";

export interface ProviderOAuthConnector {
  id: string;
  user_id: string;
  workspace_id: string | null;
  provider_name: string;
  provider_type: "email" | "calendar";
  status: ProviderConnectorStatus;
  scopes: unknown[];
  token_expires_at: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  disconnected_at: string | null;
  has_access_token: boolean;
  has_refresh_token: boolean;
  ready_for_execution: boolean;
  encryption_available: boolean;
}

export interface ProviderReadinessEntry {
  provider_name: string;
  provider_type: "email" | "calendar";
  status: ProviderConnectorStatus;
  scopes: unknown[];
  required_scopes: string[];
  missing_scopes: string[];
  has_access_token: boolean;
  has_refresh_token: boolean;
  token_expires_at: string | null;
  disconnected_at: string | null;
  updated_at: string | null;
  ready_for_execution: boolean;
  blockers: string[];
  connector_id: string | null;
  encryption_available: boolean;
}

export interface ProviderReadiness {
  encryption_available: boolean;
  execution_enabled: boolean;
  safety_note: string;
  providers: ProviderReadinessEntry[];
}

// Provider Execution Framework (v1.0) — governed execution/simulation result.
export interface ProviderExecutionResult {
  status:
    | "blocked"
    | "simulated"
    | "validation_failed"
    | "provider_unsupported"
    | "action_unsupported"
    | "execution_not_enabled";
  intent_id: string;
  provider_name: string | null;
  provider_type: string | null;
  action_type: string | null;
  dry_run: boolean;
  simulate: boolean;
  message: string;
  errors: string[];
  simulated_result: Record<string, unknown> | null;
  execution_enabled: boolean;
  real_execution_performed: boolean;
}

// External Execution Kill Switch (v0.8) — global safety guard status.
export interface ExecutionStatus {
  external_execution_enabled: boolean;
  dry_run_enforced: boolean;
  execution_available: boolean;
  message: string;
}

// OAuth readiness simulation result (analysis only — never executes)
export interface OAuthReadinessResult {
  intent_id: string;
  intent_type: string;
  source_type: string;
  source_id: string;
  required_provider_type: string;
  required_provider_name: string | null;
  connector_found: boolean;
  connector_status: string;
  required_scopes: string[];
  available_scopes: string[];
  missing_scopes: string[];
  has_access_token: boolean;
  has_refresh_token: boolean;
  token_expired: boolean;
  governance_allowed: boolean;
  execution_enabled: boolean;
  ready_for_execution: boolean;
  blockers: string[];
  recommended_next_step: string;
  from_cache?: boolean;
  note?: string;
  validation_result?: Record<string, unknown>;
}
