import logging

from app.clients import clients
from app.config import settings

logger = logging.getLogger(__name__)

# Set at startup by init_schema(). Use is_pgvector_available() at call time.
PGVECTOR_AVAILABLE: bool = False


def is_pgvector_available() -> bool:
    return PGVECTOR_AVAILABLE

SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT NOT NULL UNIQUE,
    display_name TEXT,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'admin',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_idx ON users (LOWER(email));

CREATE TABLE IF NOT EXISTS conversations (
    session_id UUID PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES conversations(session_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS messages_session_created_idx
    ON messages (session_id, created_at);

CREATE TABLE IF NOT EXISTS agent_runs (
    id BIGSERIAL PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES conversations(session_id) ON DELETE CASCADE,
    agent TEXT NOT NULL,
    model_name TEXT,
    user_message TEXT NOT NULL,
    assistant_response TEXT,
    placeholder BOOLEAN NOT NULL DEFAULT FALSE,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS agent_runs_session_started_idx
    ON agent_runs (session_id, started_at);

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS tool_name TEXT,
    ADD COLUMN IF NOT EXISTS tool_result JSONB;

CREATE TABLE IF NOT EXISTS tools (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    type TEXT NOT NULL,
    endpoint TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    requires_confirmation BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS memory_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_session_id UUID,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT[] NOT NULL DEFAULT '{}',
    importance INT NOT NULL DEFAULT 3,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS memory_entries_source_session_idx
    ON memory_entries (source_session_id);
CREATE INDEX IF NOT EXISTS memory_entries_type_idx
    ON memory_entries (type);
CREATE INDEX IF NOT EXISTS memory_entries_created_idx
    ON memory_entries (created_at DESC);

-- Scoped data: every row carries (scope_type, scope_id).
-- scope_type:
--   'user'   — scope_id = users.id, owner-private
--   'global' — scope_id NULL, visible to all authenticated users
--   'system' — scope_id NULL, internal/automatic, not surfaced by default
ALTER TABLE conversations
    ADD COLUMN IF NOT EXISTS scope_type TEXT NOT NULL DEFAULT 'user',
    ADD COLUMN IF NOT EXISTS scope_id UUID;

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS scope_type TEXT NOT NULL DEFAULT 'user',
    ADD COLUMN IF NOT EXISTS scope_id UUID;

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS scope_type TEXT NOT NULL DEFAULT 'user',
    ADD COLUMN IF NOT EXISTS scope_id UUID;

ALTER TABLE memory_entries
    ADD COLUMN IF NOT EXISTS scope_type TEXT NOT NULL DEFAULT 'user',
    ADD COLUMN IF NOT EXISTS scope_id UUID;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'conversations_scope_type_chk') THEN
        ALTER TABLE conversations ADD CONSTRAINT conversations_scope_type_chk
            CHECK (scope_type IN ('user','global','system'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'messages_scope_type_chk') THEN
        ALTER TABLE messages ADD CONSTRAINT messages_scope_type_chk
            CHECK (scope_type IN ('user','global','system'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'agent_runs_scope_type_chk') THEN
        ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_scope_type_chk
            CHECK (scope_type IN ('user','global','system'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'memory_entries_scope_type_chk') THEN
        ALTER TABLE memory_entries ADD CONSTRAINT memory_entries_scope_type_chk
            CHECK (scope_type IN ('user','global','system'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS conversations_scope_updated_idx
    ON conversations (scope_type, scope_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS messages_scope_session_created_idx
    ON messages (scope_type, scope_id, session_id, created_at);
CREATE INDEX IF NOT EXISTS memory_entries_scope_updated_idx
    ON memory_entries (scope_type, scope_id, updated_at DESC);

-- Agent registry + version control
CREATE TABLE IF NOT EXISTS agents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    description TEXT,
    agent_type TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    current_version_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS agent_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    version_number INT NOT NULL,
    status TEXT NOT NULL,
    system_prompt TEXT NOT NULL,
    routing_keywords TEXT[] NOT NULL DEFAULT '{}',
    allowed_tools TEXT[] NOT NULL DEFAULT '{}',
    model_name TEXT,
    temperature NUMERIC NOT NULL DEFAULT 0.2,
    max_prompt_chars INT NOT NULL DEFAULT 16000,
    notes TEXT,
    created_by UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    activated_at TIMESTAMPTZ,
    archived_at TIMESTAMPTZ,
    UNIQUE (agent_id, version_number)
);

CREATE UNIQUE INDEX IF NOT EXISTS agent_versions_one_active_per_agent
    ON agent_versions (agent_id) WHERE status = 'active';

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'agents_agent_type_chk') THEN
        ALTER TABLE agents ADD CONSTRAINT agents_agent_type_chk
            CHECK (agent_type IN ('orchestrator','subagent','memory','tool_agent'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'agent_versions_status_chk') THEN
        ALTER TABLE agent_versions ADD CONSTRAINT agent_versions_status_chk
            CHECK (status IN ('draft','active','archived'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'agents_current_version_fk') THEN
        ALTER TABLE agents
            ADD CONSTRAINT agents_current_version_fk
            FOREIGN KEY (current_version_id) REFERENCES agent_versions(id)
            ON DELETE SET NULL;
    END IF;
END $$;

-- Tool registry extensions (MCP-aware governance fields)
ALTER TABLE tools
    ADD COLUMN IF NOT EXISTS mcp_server_name TEXT,
    ADD COLUMN IF NOT EXISTS mcp_action_name TEXT,
    ADD COLUMN IF NOT EXISTS input_schema JSONB,
    ADD COLUMN IF NOT EXISTS output_schema JSONB,
    ADD COLUMN IF NOT EXISTS risk_level TEXT NOT NULL DEFAULT 'low',
    ADD COLUMN IF NOT EXISTS allowed_agents TEXT[] NOT NULL DEFAULT '{}';

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tools_risk_level_chk') THEN
        ALTER TABLE tools ADD CONSTRAINT tools_risk_level_chk
            CHECK (risk_level IN ('low','medium','high'));
    END IF;
END $$;

-- Per-(tool, agent) permission overrides + optional rate limit
CREATE TABLE IF NOT EXISTS tool_execution_policies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tool_name TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    allowed BOOLEAN NOT NULL DEFAULT TRUE,
    requires_confirmation BOOLEAN NOT NULL DEFAULT FALSE,
    max_calls_per_hour INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tool_name, agent_name)
);
CREATE INDEX IF NOT EXISTS tool_execution_policies_lookup_idx
    ON tool_execution_policies (tool_name, agent_name);

-- Audit log: every tool execution attempt (allowed and denied)
CREATE TABLE IF NOT EXISTS tool_execution_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID,
    user_id UUID,
    tool_name TEXT NOT NULL,
    agent_name TEXT,
    scope_type TEXT,
    allowed BOOLEAN NOT NULL,
    duration_ms INT,
    status TEXT NOT NULL,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS tool_execution_logs_tool_created_idx
    ON tool_execution_logs (tool_name, created_at DESC);
CREATE INDEX IF NOT EXISTS tool_execution_logs_user_created_idx
    ON tool_execution_logs (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS tool_execution_logs_agent_created_idx
    ON tool_execution_logs (agent_name, created_at DESC);

-- Workspaces / projects: a top-level grouping over chats/memory/plans/jobs/traces
CREATE TABLE IF NOT EXISTS workspaces (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id UUID,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'workspaces_status_chk') THEN
        ALTER TABLE workspaces ADD CONSTRAINT workspaces_status_chk
            CHECK (status IN ('active','archived'));
    END IF;
END $$;

ALTER TABLE conversations    ADD COLUMN IF NOT EXISTS workspace_id UUID;
ALTER TABLE messages         ADD COLUMN IF NOT EXISTS workspace_id UUID;
ALTER TABLE memory_entries   ADD COLUMN IF NOT EXISTS workspace_id UUID;

-- Chat Management v0.1: friendly titles, pinning, soft-delete.
-- updated_at already exists on conversations (see CREATE TABLE above).
ALTER TABLE conversations
    ADD COLUMN IF NOT EXISTS title TEXT,
    ADD COLUMN IF NOT EXISTS pinned BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS title_source TEXT DEFAULT 'auto';

-- URL Knowledge Refresh v0.2: structured fetch metadata on sources
-- (fetched_at, last_checked_at, last_changed_at, previous_content_hash,
-- status_code, content_type, title_source, last_error, ...).
ALTER TABLE knowledge_sources
    ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Agent Admin v0.2: per-version metadata
-- ({routing_keywords, specializations, change_summary, ...}).
ALTER TABLE agent_versions
    ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Knowledge sources: source-of-truth documents behind memory entries
CREATE TABLE IF NOT EXISTS knowledge_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID,
    uploaded_by UUID,
    source_type TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    original_filename TEXT,
    source_url TEXT,
    content TEXT,
    content_hash TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$ BEGIN
    -- type check: drop + recreate so new source types (e.g. news_feed,
    -- news_article) migrate cleanly on existing databases.
    ALTER TABLE knowledge_sources DROP CONSTRAINT IF EXISTS knowledge_sources_type_chk;
    ALTER TABLE knowledge_sources ADD CONSTRAINT knowledge_sources_type_chk
        CHECK (source_type IN (
            'manual_note','markdown','text_file','url',
            'generated_summary','system_seed','news_feed','news_article'
        ));
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'knowledge_sources_status_chk') THEN
        ALTER TABLE knowledge_sources ADD CONSTRAINT knowledge_sources_status_chk
            CHECK (status IN ('active','archived'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS knowledge_sources_workspace_idx
    ON knowledge_sources (workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS knowledge_sources_hash_idx
    ON knowledge_sources (workspace_id, content_hash);

-- The old `news_sources` registry (deprecated news path) is no longer created;
-- existing deployments may still carry the table — safe to DROP manually.

ALTER TABLE memory_entries ADD COLUMN IF NOT EXISTS source_id UUID;
CREATE INDEX IF NOT EXISTS memory_entries_source_idx
    ON memory_entries (source_id);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'memory_entries_source_fk') THEN
        ALTER TABLE memory_entries
            ADD CONSTRAINT memory_entries_source_fk
            FOREIGN KEY (source_id) REFERENCES knowledge_sources(id)
            ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS conversations_workspace_idx
    ON conversations (workspace_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS messages_workspace_idx
    ON messages (workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS memory_entries_workspace_idx
    ON memory_entries (workspace_id, created_at DESC);

-- Execution plans: ATLAS-authored multi-step plans
CREATE TABLE IF NOT EXISTS execution_plans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID,
    user_id UUID,
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned',
    current_step INT NOT NULL DEFAULT 0,
    total_steps INT NOT NULL DEFAULT 0,
    selected_agent TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS execution_plan_steps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id UUID NOT NULL REFERENCES execution_plans(id) ON DELETE CASCADE,
    step_number INT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    assigned_agent TEXT,
    tool_name TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    result JSONB,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (plan_id, step_number)
);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'execution_plans_status_chk') THEN
        ALTER TABLE execution_plans ADD CONSTRAINT execution_plans_status_chk
            CHECK (status IN ('planned','running','completed','failed','cancelled'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'execution_plan_steps_status_chk') THEN
        ALTER TABLE execution_plan_steps ADD CONSTRAINT execution_plan_steps_status_chk
            CHECK (status IN ('pending','running','completed','failed','skipped'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS execution_plans_user_idx
    ON execution_plans (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS execution_plans_session_idx
    ON execution_plans (session_id, created_at DESC);

ALTER TABLE execution_plans      ADD COLUMN IF NOT EXISTS workspace_id UUID;
ALTER TABLE execution_plan_steps ADD COLUMN IF NOT EXISTS workspace_id UUID;

CREATE INDEX IF NOT EXISTS execution_plans_workspace_idx
    ON execution_plans (workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS execution_plan_steps_workspace_idx
    ON execution_plan_steps (workspace_id);

-- Background jobs (v0.1: queueing only, no worker yet)
CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID,
    session_id UUID,
    plan_id UUID,
    step_id UUID,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    payload JSONB,
    result JSONB,
    error_message TEXT,
    attempts INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 3,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'jobs_status_chk') THEN
        ALTER TABLE jobs ADD CONSTRAINT jobs_status_chk
            CHECK (status IN ('queued','running','completed','failed','cancelled'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS jobs_status_created_idx
    ON jobs (status, created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_plan_idx
    ON jobs (plan_id, created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_user_idx
    ON jobs (user_id, created_at DESC);

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS workspace_id UUID;
CREATE INDEX IF NOT EXISTS jobs_workspace_idx
    ON jobs (workspace_id, created_at DESC);

-- Multi-agent delegations (v0.1)
CREATE TABLE IF NOT EXISTS agent_delegations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID,
    session_id UUID,
    execution_plan_id UUID,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    delegation_reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    input_payload JSONB,
    output_payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'agent_delegations_status_chk') THEN
        ALTER TABLE agent_delegations ADD CONSTRAINT agent_delegations_status_chk
            CHECK (status IN ('pending','running','completed','failed'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS agent_delegations_session_idx
    ON agent_delegations (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS agent_delegations_plan_idx
    ON agent_delegations (execution_plan_id, created_at DESC);
CREATE INDEX IF NOT EXISTS agent_delegations_workspace_idx
    ON agent_delegations (workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS agent_delegations_status_idx
    ON agent_delegations (status, created_at DESC);

CREATE TABLE IF NOT EXISTS worker_heartbeats (
    worker_id TEXT PRIMARY KEY,
    last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_job_id UUID,
    last_job_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Runtime traces: one row per high-level chat-turn / tool-run / mcp-call
CREATE TABLE IF NOT EXISTS runtime_traces (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID,
    user_id UUID,
    trace_type TEXT NOT NULL,
    selected_agent TEXT,
    user_message TEXT,
    memory_count INT NOT NULL DEFAULT 0,
    memory_ids UUID[] NOT NULL DEFAULT '{}',
    tool_name TEXT,
    tool_result JSONB,
    mcp_server_name TEXT,
    mcp_action_name TEXT,
    model_name TEXT,
    model_endpoint TEXT,
    duration_ms INT,
    status TEXT NOT NULL,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS runtime_traces_session_idx
    ON runtime_traces (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS runtime_traces_created_idx
    ON runtime_traces (created_at DESC);
CREATE INDEX IF NOT EXISTS runtime_traces_type_idx
    ON runtime_traces (trace_type, created_at DESC);

ALTER TABLE runtime_traces ADD COLUMN IF NOT EXISTS workspace_id UUID;
CREATE INDEX IF NOT EXISTS runtime_traces_workspace_idx
    ON runtime_traces (workspace_id, created_at DESC);

ALTER TABLE runtime_traces
    ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

-- MCP server registry
CREATE TABLE IF NOT EXISTS mcp_servers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    server_type TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    auth_type TEXT,
    auth_config JSONB,
    capabilities JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- SIGNAL/CHRONOS Governed Tool Planning v0.1: internal-only draft/proposal
-- artifacts. No external send/calendar action exists — these are review-only.
CREATE TABLE IF NOT EXISTS communication_drafts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE SET NULL,
    created_by UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    agent_name TEXT NOT NULL DEFAULT 'SIGNAL',
    draft_type TEXT NOT NULL,
    title TEXT,
    recipient_hint TEXT,
    subject TEXT,
    body TEXT NOT NULL,
    tone TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS communication_drafts_workspace_idx
    ON communication_drafts (workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS communication_drafts_creator_idx
    ON communication_drafts (created_by, created_at DESC);

CREATE TABLE IF NOT EXISTS schedule_proposals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE SET NULL,
    created_by UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    agent_name TEXT NOT NULL DEFAULT 'CHRONOS',
    proposal_type TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    start_time TIMESTAMPTZ,
    end_time TIMESTAMPTZ,
    timezone TEXT,
    attendees JSONB NOT NULL DEFAULT '[]'::jsonb,
    agenda JSONB NOT NULL DEFAULT '[]'::jsonb,
    reminders JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL DEFAULT 'proposed',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS schedule_proposals_workspace_idx
    ON schedule_proposals (workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS schedule_proposals_creator_idx
    ON schedule_proposals (created_by, created_at DESC);

-- Draft/Proposal Review Workflow v0.3: review metadata + event log. Columns are
-- added idempotently to the existing tables; status enums are widened in the
-- service layer (in_review/changes_requested added). Internal-only — no send.
ALTER TABLE communication_drafts
    ADD COLUMN IF NOT EXISTS reviewed_by UUID REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS approved_by UUID REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS review_notes TEXT;

ALTER TABLE schedule_proposals
    ADD COLUMN IF NOT EXISTS reviewed_by UUID REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS approved_by UUID REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS review_notes TEXT;

CREATE TABLE IF NOT EXISTS draft_review_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draft_id UUID NOT NULL REFERENCES communication_drafts(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS draft_review_events_draft_idx
    ON draft_review_events (draft_id, created_at DESC);

CREATE TABLE IF NOT EXISTS proposal_review_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    proposal_id UUID NOT NULL REFERENCES schedule_proposals(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS proposal_review_events_proposal_idx
    ON proposal_review_events (proposal_id, created_at DESC);

-- External Integration Readiness v0.4: dry-run-only intent records that prepare
-- (never perform) a future external send/calendar action from an APPROVED draft
-- or proposal. No live providers; payloads are normalized previews only.
CREATE TABLE IF NOT EXISTS external_integration_intents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE SET NULL,
    created_by UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    source_id UUID NOT NULL,
    agent_name TEXT NOT NULL,
    provider_type TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    action_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    dry_run BOOLEAN NOT NULL DEFAULT TRUE,
    requires_confirmation BOOLEAN NOT NULL DEFAULT TRUE,
    confirmation_required_reason TEXT,
    payload_preview JSONB NOT NULL DEFAULT '{}'::jsonb,
    validation_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    confirmed_by UUID REFERENCES users(id) ON DELETE SET NULL,
    confirmed_at TIMESTAMPTZ,
    cancelled_by UUID REFERENCES users(id) ON DELETE SET NULL,
    cancelled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS external_integration_intents_workspace_idx
    ON external_integration_intents (workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS external_integration_intents_source_idx
    ON external_integration_intents (source_type, source_id);
CREATE INDEX IF NOT EXISTS external_integration_intents_creator_idx
    ON external_integration_intents (created_by, created_at DESC);

CREATE TABLE IF NOT EXISTS external_integration_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    intent_id UUID NOT NULL REFERENCES external_integration_intents(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    notes TEXT,
    payload_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS external_integration_events_intent_idx
    ON external_integration_events (intent_id, created_at ASC);

-- External Provider Connector Design v0.5: registry of (future) outbound
-- providers + their capability metadata. SCAFFOLDING ONLY — no connector here
-- performs a live action. Real send/calendar execution is intentionally absent
-- (supports_send / supports_calendar_* stay FALSE; dry_run_only stays TRUE).
CREATE TABLE IF NOT EXISTS external_provider_connectors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_name TEXT NOT NULL UNIQUE,
    provider_type TEXT NOT NULL,
    display_name TEXT NOT NULL,
    description TEXT,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    dry_run_only BOOLEAN NOT NULL DEFAULT TRUE,
    supports_send BOOLEAN NOT NULL DEFAULT FALSE,
    supports_draft BOOLEAN NOT NULL DEFAULT FALSE,
    supports_calendar_create BOOLEAN NOT NULL DEFAULT FALSE,
    supports_calendar_update BOOLEAN NOT NULL DEFAULT FALSE,
    supports_read BOOLEAN NOT NULL DEFAULT FALSE,
    requires_oauth BOOLEAN NOT NULL DEFAULT TRUE,
    auth_config_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    payload_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    capabilities JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS external_provider_connectors_type_idx
    ON external_provider_connectors (provider_type);
-- CHRONOS Calendar CRUD v1.0: calendar read + delete capability columns (the
-- v0.5 table shipped with create/update only). Idempotent for existing installs.
-- WRITE capability stays gated by the calendar_write flag + the global kill switch.
ALTER TABLE external_provider_connectors
    ADD COLUMN IF NOT EXISTS supports_calendar_read BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE external_provider_connectors
    ADD COLUMN IF NOT EXISTS supports_calendar_delete BOOLEAN NOT NULL DEFAULT FALSE;

-- OAuth Credential Vault Design v0.6: per-workspace/per-user credential records
-- for FUTURE OAuth providers (Gmail/Outlook/Google Calendar/Microsoft Calendar).
-- READINESS / DRY-RUN ONLY — no real OAuth exchange, no token exchange endpoint,
-- no provider API calls. Secret columns hold encrypted blobs only and are NEVER
-- returned by the API; in v0.6 no real secrets are accepted (encryption is a
-- clearly-marked placeholder) so these stay NULL. dry_run_only stays TRUE.
CREATE TABLE IF NOT EXISTS external_provider_credentials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE SET NULL,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    provider_name TEXT NOT NULL,
    provider_type TEXT NOT NULL,
    credential_name TEXT NOT NULL,
    auth_type TEXT NOT NULL DEFAULT 'oauth2',
    status TEXT NOT NULL DEFAULT 'not_configured'
        CHECK (status IN (
            'not_configured', 'configured', 'needs_authorization',
            'authorized_placeholder', 'expired', 'revoked', 'disabled', 'error'
        )),
    scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
    encrypted_access_token TEXT,
    encrypted_refresh_token TEXT,
    encrypted_client_secret TEXT,
    client_id_hint TEXT,
    token_expires_at TIMESTAMPTZ,
    last_authorized_at TIMESTAMPTZ,
    last_validated_at TIMESTAMPTZ,
    last_error TEXT,
    dry_run_only BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS external_provider_credentials_workspace_idx
    ON external_provider_credentials (workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS external_provider_credentials_user_idx
    ON external_provider_credentials (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS external_provider_credentials_provider_idx
    ON external_provider_credentials (provider_name);

CREATE TABLE IF NOT EXISTS external_provider_credential_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    credential_id UUID NOT NULL
        REFERENCES external_provider_credentials(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    notes TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS external_provider_credential_events_cred_idx
    ON external_provider_credential_events (credential_id, created_at ASC);

-- OAuth Credential Vault v0.6: per-user provider OAuth connector records. This
-- is the spec's "provider connector" credential store (distinct from the v0.5
-- capability registry `external_provider_connectors`). READINESS ONLY — no real
-- OAuth flow, no provider API calls. Token columns hold Fernet-encrypted values
-- ONLY (never plaintext) and stay NULL until a future OAuth phase populates them.
CREATE TABLE IF NOT EXISTS provider_oauth_connectors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    workspace_id UUID REFERENCES workspaces(id) ON DELETE SET NULL,
    provider_name TEXT NOT NULL,
    provider_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'not_configured'
        CHECK (status IN (
            'not_configured', 'oauth_required', 'connected',
            'expired', 'disconnected', 'error'
        )),
    scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
    access_token_encrypted TEXT,
    refresh_token_encrypted TEXT,
    token_expires_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    disconnected_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS provider_oauth_connectors_user_idx
    ON provider_oauth_connectors (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS provider_oauth_connectors_provider_idx
    ON provider_oauth_connectors (provider_name);

-- Real OAuth Flow v1.1: short-lived, single-use CSRF state for the OAuth
-- authorization-code flow. The unauthenticated callback recovers the initiating
-- user from this row. No token material is ever stored here.
CREATE TABLE IF NOT EXISTS oauth_states (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    state TEXT NOT NULL UNIQUE,
    provider_name TEXT NOT NULL,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    workspace_id UUID REFERENCES workspaces(id) ON DELETE SET NULL,
    redirect_uri TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    consumed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS oauth_states_state_idx ON oauth_states (state);

-- Credential lifecycle audit for Real OAuth Flow v1.1 connections. One row per
-- connect / token-store / failure / refresh / disconnect. NO token material is
-- ever stored here — only the event kind, status, and a short non-secret detail.
CREATE TABLE IF NOT EXISTS provider_oauth_connector_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    connector_id UUID REFERENCES provider_oauth_connectors(id) ON DELETE SET NULL,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    provider_name TEXT NOT NULL,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS provider_oauth_connector_events_provider_idx
    ON provider_oauth_connector_events (provider_name, created_at DESC);
CREATE INDEX IF NOT EXISTS provider_oauth_connector_events_user_idx
    ON provider_oauth_connector_events (user_id, created_at DESC);

-- Human Approval Execution Console v1.4. One row per approval DECISION on an
-- integration intent (approve-for-future-execution / reject / blocked attempt).
-- Pure audit evidence — approving NEVER executes a provider action. Stores the
-- approver, decision, reason, governance + provider-readiness snapshots, and a
-- payload hash/preview reference. NO token material is stored.
CREATE TABLE IF NOT EXISTS execution_approvals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    intent_id UUID NOT NULL REFERENCES external_integration_intents(id) ON DELETE CASCADE,
    approver_id UUID REFERENCES users(id) ON DELETE SET NULL,
    decision TEXT NOT NULL,
    approval_state TEXT NOT NULL,
    reason TEXT,
    governance_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    provider_readiness_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    payload_hash TEXT,
    payload_preview_ref TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS execution_approvals_intent_idx
    ON execution_approvals (intent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS execution_approvals_approver_idx
    ON execution_approvals (approver_id, created_at DESC);

-- Provider Execution Feature Flag Matrix v1.7. Centralized per-(provider,action)
-- execution control. Default fail-closed: enabled=false / dry_run_only=true. A
-- flag is necessary-but-NOT-sufficient — the global kill switch + final interlock
-- still gate any real execution, which stays disabled this phase. No tokens here.
CREATE TABLE IF NOT EXISTS provider_execution_feature_flags (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_name TEXT NOT NULL,
    provider_type TEXT NOT NULL,
    action_type TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    dry_run_only BOOLEAN NOT NULL DEFAULT TRUE,
    requires_human_approval BOOLEAN NOT NULL DEFAULT TRUE,
    requires_final_interlock BOOLEAN NOT NULL DEFAULT TRUE,
    requires_valid_oauth BOOLEAN NOT NULL DEFAULT TRUE,
    requires_scope_validation BOOLEAN NOT NULL DEFAULT TRUE,
    requires_connected_provider BOOLEAN NOT NULL DEFAULT TRUE,
    requires_payload_hash_match BOOLEAN NOT NULL DEFAULT TRUE,
    requires_kill_switch_clear BOOLEAN NOT NULL DEFAULT TRUE,
    environment TEXT NOT NULL DEFAULT 'production',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (provider_name, action_type, environment)
);
CREATE INDEX IF NOT EXISTS provider_execution_feature_flags_lookup_idx
    ON provider_execution_feature_flags (provider_name, action_type, environment);

-- Chat-Native Email Review & Approval Workflow v1.9. Per-conversation email
-- lifecycle context so ambiguous follow-ups ("approve it") resolve to the active
-- draft. No secrets stored — only ids/provider selection.
CREATE TABLE IF NOT EXISTS chat_email_context (
    session_id UUID PRIMARY KEY,
    current_active_draft_id UUID,
    last_created_draft_id UUID,
    last_reviewed_draft_id UUID,
    selected_provider TEXT,
    last_integration_intent_id UUID,
    last_queue JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- v2.2 numbered approval queue: ordered list of the last rendered queue so
-- "approve item 2" / "the first one" resolve. Idempotent for existing installs.
ALTER TABLE chat_email_context ADD COLUMN IF NOT EXISTS last_queue JSONB NOT NULL DEFAULT '[]'::jsonb;

-- Chat-Native Inbox Assistant v2.3. Audit of read-only inbox access attempts
-- (allowed + fail-closed denials). NO email bodies or tokens stored — only the
-- action, decision, and a message reference.
CREATE TABLE IF NOT EXISTS inbox_access_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    workspace_id UUID,
    provider TEXT NOT NULL,
    action TEXT NOT NULL,
    allowed BOOLEAN NOT NULL,
    reason TEXT,
    message_ref TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS inbox_access_events_user_idx
    ON inbox_access_events (user_id, created_at DESC);

-- CHRONOS Calendar CRUD v1.0. Audit of calendar access attempts (reads + writes,
-- allowed + fail-closed denials). NO event bodies or tokens stored — only the
-- action, decision, and an event reference.
CREATE TABLE IF NOT EXISTS calendar_access_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    workspace_id UUID,
    provider TEXT NOT NULL,
    action TEXT NOT NULL,
    allowed BOOLEAN NOT NULL,
    reason TEXT,
    event_ref TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS calendar_access_events_user_idx
    ON calendar_access_events (user_id, created_at DESC);

-- CHRONOS Calendar CRUD v1.0 confirm-before-write: a per-session staged calendar
-- write awaiting the user's explicit "confirm". One row per session (replaced on a
-- new request, deleted on confirm/cancel). NO tokens — only the parsed event fields
-- + the resolved target event id/summary for update/delete.
CREATE TABLE IF NOT EXISTS calendar_pending_actions (
    session_id UUID PRIMARY KEY,
    user_id UUID,
    workspace_id UUID,
    kind TEXT NOT NULL,
    provider TEXT NOT NULL,
    fields JSONB NOT NULL DEFAULT '{}'::jsonb,
    target_event_id TEXT,
    target_calendar_id TEXT,
    target_summary TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Cross-calendar writes: the resolved target's calendar id (Google update/delete
-- need calendars/<calendarId>/events/<id>). Idempotent for existing installs.
ALTER TABLE calendar_pending_actions ADD COLUMN IF NOT EXISTS target_calendar_id TEXT;

-- Tier-2 Screen Vision (opt-in): audit of screenshot-analysis attempts (allowed +
-- fail-closed denials). PRIVACY: the screenshot bytes are NEVER stored — only the
-- decision, model, byte count, a short question preview, and latency.
CREATE TABLE IF NOT EXISTS screen_vision_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    workspace_id UUID,
    allowed BOOLEAN NOT NULL,
    reason TEXT,
    model TEXT,
    image_bytes INTEGER,
    question_preview TEXT,
    latency_ms INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS screen_vision_events_user_idx
    ON screen_vision_events (user_id, created_at DESC);

-- Per-user default provider for provider-less mail/calendar requests (so "what's on
-- my calendar" with BOTH Google + Outlook connected uses an explicit default instead of
-- "most-recently-connected wins"). One row per (user, provider_type ∈ email|calendar).
CREATE TABLE IF NOT EXISTS user_provider_defaults (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider_type TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, provider_type)
);

-- Per-user default WRITE calendar — the specific calendar (WITHIN a provider) a
-- hint-less CREATE lands on, instead of the provider's `primary`. Complements
-- user_provider_defaults (which picks the provider): "make Work my default calendar"
-- stores the resolved calendar id+name here; an unnamed create then targets it. One
-- row per (user, provider_name); no row => primary. Set/cleared via chat (CHRONOS).
CREATE TABLE IF NOT EXISTS user_calendar_defaults (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider_name TEXT NOT NULL,
    calendar_id TEXT NOT NULL,
    calendar_name TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, provider_name)
);

-- Admin-managed runtime override for the execution kill switches (calendar / screen
-- vision). The env vars in config.py remain the DEFAULT; a row here, when present,
-- OVERRIDES the env value at runtime so an operator can toggle from the app without a
-- redeploy. No row => fall back to env. EXTERNAL_EXECUTION_ENABLED is deliberately NOT
-- managed here (it stays env-locked — turning it on breaks the email/integration final
-- interlock, which requires it false). One row per switch name.
CREATE TABLE IF NOT EXISTS runtime_execution_switches (
    name TEXT PRIMARY KEY,
    enabled BOOLEAN NOT NULL,
    updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed the 4 supported combinations, fail-closed (enabled=false, dry_run_only=true).
-- action_type uses the canonical send_email / create_calendar_event the adapters
-- + intents use (spec's "create_event" maps here via the service alias).
INSERT INTO provider_execution_feature_flags
    (provider_name, provider_type, action_type, environment)
VALUES
    ('gmail', 'email', 'send_email', 'production'),
    ('outlook_mail', 'email', 'send_email', 'production'),
    ('google_calendar', 'calendar', 'create_calendar_event', 'production'),
    ('microsoft_calendar', 'calendar', 'create_calendar_event', 'production'),
    -- v2.3 read-only inbox access flags (fail-closed: disabled by default).
    ('gmail', 'email', 'inbox_read', 'production'),
    ('outlook_mail', 'email', 'inbox_read', 'production'),
    -- CHRONOS Calendar CRUD v1.0: read + write calendar flags (fail-closed). The
    -- calendar_write flag is necessary-but-not-sufficient — the global kill switch
    -- is still the master gate for any real create/update/delete.
    ('google_calendar', 'calendar', 'calendar_read', 'production'),
    ('google_calendar', 'calendar', 'calendar_write', 'production'),
    ('microsoft_calendar', 'calendar', 'calendar_read', 'production'),
    ('microsoft_calendar', 'calendar', 'calendar_write', 'production')
ON CONFLICT (provider_name, action_type, environment) DO NOTHING;

-- Phase 1 agent kernel: durable, resumable run state. One row per run_agent()
-- invocation; the message thread, budget, and result are persisted as the loop
-- advances, so a run survives a restart and is the seam Phase 2 hub-and-spoke
-- delegation resumes through (a spoke run is just another row, with agent_name
-- set to the spoke). status leaves room for the Phase 2 pause states
-- (waiting_user / waiting_tool); Phase 1 only ever sets running / done / failed.
CREATE TABLE IF NOT EXISTS agent_runtime_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID,
    user_id UUID,
    workspace_id UUID,
    agent_name TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    goal TEXT NOT NULL,
    model_name TEXT,
    messages JSONB NOT NULL DEFAULT '[]'::jsonb,
    steps JSONB NOT NULL DEFAULT '[]'::jsonb,
    answer TEXT,
    tool_calls INT NOT NULL DEFAULT 0,
    step_count INT NOT NULL DEFAULT 0,
    max_steps INT NOT NULL DEFAULT 6,
    stopped TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);
-- Phase 6: independent evaluator verdict (pass/concerns/fail + reasons),
-- attached to a finished top-level run. NULL = not evaluated.
ALTER TABLE agent_runtime_runs ADD COLUMN IF NOT EXISTS evaluation JSONB;
CREATE INDEX IF NOT EXISTS agent_runtime_runs_session_idx
    ON agent_runtime_runs (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS agent_runtime_runs_user_idx
    ON agent_runtime_runs (user_id, created_at DESC);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'agent_runtime_runs_status_chk') THEN
        ALTER TABLE agent_runtime_runs ADD CONSTRAINT agent_runtime_runs_status_chk
            CHECK (status IN ('pending','running','waiting_user','waiting_tool','done','failed','cancelled'));
    END IF;
END $$;
"""

# Each entry is one INSERT — asyncpg.Connection.execute() cannot run a
# multi-statement string when parameters are bound, so we dispatch each
# seed statement individually. All seeds are idempotent via ON CONFLICT.
SEED_N8N_HEALTH_TOOL_SQL = """
INSERT INTO tools (name, description, type, endpoint, enabled, requires_confirmation)
VALUES (
    'n8n_health_check',
    'Calls an n8n webhook to verify Cora can trigger workflows.',
    'n8n_webhook',
    $1,
    TRUE,
    FALSE
)
ON CONFLICT (name) DO NOTHING
"""

SEED_FILESYSTEM_LIST_TOOL_SQL = """
INSERT INTO tools (name, description, type, mcp_server_name, mcp_action_name,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'filesystem_list_project',
    'List directory contents via the filesystem MCP server. Read-only.',
    'mcp_action', 'filesystem', 'list_directory',
    TRUE, FALSE, 'low', ARRAY['FORGE']
)
ON CONFLICT (name) DO NOTHING
"""

SEED_DEFAULT_WORKSPACE_SQL = """
INSERT INTO workspaces (name, slug, description)
VALUES (
    'Cora AI OS',
    'cora-ai-os',
    'Primary workspace for building and governing the Cora AI Operating System.'
)
ON CONFLICT (slug) DO NOTHING
"""

SEED_FILESYSTEM_READ_TOOL_SQL = """
INSERT INTO tools (name, description, type, mcp_server_name, mcp_action_name,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'filesystem_read_file',
    'Read a file via the filesystem MCP server. Read-only.',
    'mcp_action', 'filesystem', 'read_file',
    TRUE, FALSE, 'low', ARRAY['FORGE']
)
ON CONFLICT (name) DO NOTHING
"""

SEED_WEB_SEARCH_TOOL_SQL = """
INSERT INTO tools (name, description, type, endpoint,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'web_search',
    'Live web search via the self-hosted SearXNG metasearch engine. Returns ranked result snippets for PULSE to synthesize. Queries stay on the internal network.',
    'web_search', $1,
    TRUE, FALSE, 'medium', ARRAY['PULSE']
)
ON CONFLICT (name) DO NOTHING
"""

# Internal-only governed actions: produce review-only drafts/proposals. They
# never send email or write to a calendar. requires_confirmation=TRUE so any
# future chat-triggered runner must confirm before acting.
SEED_SIGNAL_DRAFT_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'signal_create_draft',
    'Creates an internal communication draft only (email/message/announcement). Does NOT send anything.',
    'internal_action',
    TRUE, TRUE, 'low', ARRAY['SIGNAL']
)
ON CONFLICT (name) DO NOTHING
"""

SEED_SIGNAL_DELETE_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'signal_delete_draft',
    'Permanently deletes an internal communication draft record. Removes the draft only; sends/cancels nothing externally.',
    'internal_action',
    TRUE, TRUE, 'low', ARRAY['SIGNAL']
)
ON CONFLICT (name) DO NOTHING
"""

# Draft review-action tools (Review & Approval Workflow). Each review action is
# governed + audited under its own tool. All are review-only — none send any
# email or perform any external communication.
SEED_SIGNAL_REVIEW_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'signal_review_draft',
    'Marks an internal communication draft as reviewed. Internal review state only; sends nothing.',
    'internal_action',
    TRUE, FALSE, 'low', ARRAY['SIGNAL']
)
ON CONFLICT (name) DO NOTHING
"""

SEED_SIGNAL_APPROVE_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'signal_approve_draft',
    'Approves an internal communication draft (internal sign-off only). Does NOT send any email or message.',
    'internal_action',
    TRUE, TRUE, 'low', ARRAY['SIGNAL']
)
ON CONFLICT (name) DO NOTHING
"""

SEED_SIGNAL_ARCHIVE_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'signal_archive_draft',
    'Archives an internal communication draft (soft close). Removes nothing externally.',
    'internal_action',
    TRUE, FALSE, 'low', ARRAY['SIGNAL']
)
ON CONFLICT (name) DO NOTHING
"""

SEED_CHRONOS_PROPOSAL_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'chronos_create_schedule_proposal',
    'Creates an internal schedule proposal only (meeting/timeline/reminder plan). Does NOT create calendar events.',
    'internal_action',
    TRUE, TRUE, 'low', ARRAY['CHRONOS']
)
ON CONFLICT (name) DO NOTHING
"""

# Proposal review-action tools (Governed Tool Planning v0.5). Each review action
# is governed + audited under its own tool. All are review-only — none create,
# send, or modify any external calendar event.
SEED_CHRONOS_REVIEW_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'chronos_review_proposal',
    'Marks an internal schedule proposal as reviewed. Internal review state only; creates no calendar event.',
    'internal_action',
    TRUE, FALSE, 'low', ARRAY['CHRONOS']
)
ON CONFLICT (name) DO NOTHING
"""

SEED_CHRONOS_APPROVE_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'chronos_approve_proposal',
    'Approves an internal schedule proposal (internal sign-off only). Does NOT create or send any calendar event.',
    'internal_action',
    TRUE, TRUE, 'low', ARRAY['CHRONOS']
)
ON CONFLICT (name) DO NOTHING
"""

SEED_CHRONOS_ARCHIVE_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'chronos_archive_proposal',
    'Archives an internal schedule proposal (soft close). Removes nothing externally.',
    'internal_action',
    TRUE, FALSE, 'low', ARRAY['CHRONOS']
)
ON CONFLICT (name) DO NOTHING
"""

SEED_CHRONOS_DELETE_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'chronos_delete_proposal',
    'Permanently deletes an internal schedule proposal record. Removes the proposal only; creates/cancels nothing externally.',
    'internal_action',
    TRUE, TRUE, 'low', ARRAY['CHRONOS']
)
ON CONFLICT (name) DO NOTHING
"""

# External Integration Readiness v0.4: readiness/dry-run tools. They prepare a
# normalized payload preview + governed intent record; they NEVER send email or
# write to a calendar. risk_level=medium, requires_confirmation=TRUE.
SEED_SIGNAL_EMAIL_INTENT_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'signal_prepare_email_send_intent',
    'Creates a dry-run email send intent from an approved internal draft. Does NOT send.',
    'internal_action',
    TRUE, TRUE, 'medium', ARRAY['SIGNAL']
)
ON CONFLICT (name) DO NOTHING
"""

SEED_CHRONOS_CALENDAR_INTENT_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'chronos_prepare_calendar_event_intent',
    'Creates a dry-run calendar event intent from an approved schedule proposal. Does NOT create calendar events.',
    'internal_action',
    TRUE, TRUE, 'medium', ARRAY['CHRONOS']
)
ON CONFLICT (name) DO NOTHING
"""

# Integration Readiness Queue v0.6: governed internal-intent creation/cancel.
# These create/cancel an internal readiness record only — they perform NO
# external send/calendar action and never touch a provider or OAuth.
SEED_SIGNAL_EMAIL_INTEGRATION_INTENT_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'signal_create_email_integration_intent',
    'Creates an internal email-send integration intent from an approved draft (future provider action). Does NOT send.',
    'internal_action',
    TRUE, TRUE, 'low', ARRAY['SIGNAL']
)
ON CONFLICT (name) DO NOTHING
"""

SEED_CHRONOS_CALENDAR_INTEGRATION_INTENT_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'chronos_create_calendar_integration_intent',
    'Creates an internal calendar-create integration intent from an approved proposal (future provider action). Does NOT create calendar events.',
    'internal_action',
    TRUE, TRUE, 'low', ARRAY['CHRONOS']
)
ON CONFLICT (name) DO NOTHING
"""

SEED_INTEGRATION_INTENT_CANCEL_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'integration_intent_cancelled',
    'Cancels an internal integration readiness intent. Removes nothing externally.',
    'internal_action',
    TRUE, FALSE, 'low', ARRAY['SIGNAL','CHRONOS']
)
ON CONFLICT (name) DO NOTHING
"""

# Execution Approval Gate v0.7: confirm / revoke an integration intent for
# FUTURE execution. Approval only — executes nothing, no provider call.
SEED_INTEGRATION_INTENT_CONFIRM_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'integration_intent_confirmed',
    'Confirms an integration intent for future execution (human approval). Executes nothing; dry_run stays true; no provider call.',
    'internal_action',
    TRUE, FALSE, 'low', ARRAY['SIGNAL','CHRONOS']
)
ON CONFLICT (name) DO NOTHING
"""

SEED_INTEGRATION_INTENT_REVOKE_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'integration_intent_confirmation_revoked',
    'Revokes a prior confirmation on an integration intent. Executes nothing; no provider call.',
    'internal_action',
    TRUE, FALSE, 'low', ARRAY['SIGNAL','CHRONOS']
)
ON CONFLICT (name) DO NOTHING
"""

# Integration Readiness Queue: governed readiness-check tool. Records the
# simulation result into validation_result; analysis only, no provider call.
SEED_INTEGRATION_INTENT_READINESS_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'integration_intent_readiness_checked',
    'Checks readiness of an integration intent and records the result into its validation_result. Analysis only; no provider call.',
    'internal_action',
    TRUE, FALSE, 'low', ARRAY[]::text[]
)
ON CONFLICT (name) DO NOTHING
"""

# Provider Credential Usage Simulation v1.3: governed tool that resolves a
# connected credential + generates a provider-ready payload preview. Simulation
# only — no provider API call, no token exposure, execution stays disabled.
SEED_PROVIDER_CREDENTIAL_SIM_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'provider_credential_usage_simulated',
    'Resolves a connected provider credential and generates a provider-ready payload preview for an approved integration intent. Simulation only; sends/creates nothing; execution stays disabled.',
    'internal_action',
    TRUE, FALSE, 'low', ARRAY[]::text[]
)
ON CONFLICT (name) DO NOTHING
"""

# Human Approval Execution Console v1.4: governed approve/reject decision tools.
# Approving records internal state + audit evidence only — it NEVER executes a
# provider action; external execution stays disabled by the kill switch.
SEED_EXECUTION_APPROVAL_APPROVE_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'execution_approval_approved',
    'Records an admin decision to APPROVE an integration intent for future execution. Internal state + audit only; executes nothing; external execution stays disabled.',
    'internal_action',
    TRUE, TRUE, 'medium', ARRAY[]::text[]
)
ON CONFLICT (name) DO NOTHING
"""

SEED_EXECUTION_APPROVAL_REJECT_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'execution_approval_rejected',
    'Records an admin decision to REJECT an integration intent. Internal state + audit only; executes nothing.',
    'internal_action',
    TRUE, FALSE, 'low', ARRAY[]::text[]
)
ON CONFLICT (name) DO NOTHING
"""

# OAuth Credential Vault v0.6: governed provider-connector readiness tools.
# Manage internal credential records only — NO OAuth flow, NO provider API call.
SEED_PROVIDER_PLACEHOLDER_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'provider_connector_placeholder_created',
    'Creates a placeholder provider OAuth connector record (oauth_required). No real OAuth or provider call.',
    'internal_action',
    TRUE, TRUE, 'low', ARRAY[]::text[]
)
ON CONFLICT (name) DO NOTHING
"""

SEED_PROVIDER_DISCONNECT_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'provider_connector_disconnected',
    'Disconnects a provider OAuth connector (clears any stored token, marks disconnected). No provider call.',
    'internal_action',
    TRUE, FALSE, 'low', ARRAY[]::text[]
)
ON CONFLICT (name) DO NOTHING
"""

SEED_PROVIDER_READINESS_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'provider_readiness_checked',
    'Reports provider OAuth connector readiness (status/token presence/expiry). Read-only; no provider call.',
    'internal_action',
    TRUE, FALSE, 'low', ARRAY[]::text[]
)
ON CONFLICT (name) DO NOTHING
"""

SEED_OAUTH_READINESS_SIM_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'oauth_readiness_simulated',
    'Simulates whether an approved integration intent would be ready for future execution (connector/scopes/token/governance). Analysis only; no provider call.',
    'internal_action',
    TRUE, FALSE, 'low', ARRAY[]::text[]
)
ON CONFLICT (name) DO NOTHING
"""

# External Provider Connector Design v0.5: dry-run provider tools. They exercise
# the connector payload contract; they NEVER call a real provider.
SEED_SIGNAL_PROVIDER_DRY_RUN_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'signal_provider_email_dry_run',
    'Dry-runs an email provider payload. Does not send.',
    'internal_action',
    TRUE, TRUE, 'medium', ARRAY['SIGNAL']
)
ON CONFLICT (name) DO NOTHING
"""

SEED_CHRONOS_PROVIDER_DRY_RUN_TOOL_SQL = """
INSERT INTO tools (name, description, type,
                   enabled, requires_confirmation, risk_level, allowed_agents)
VALUES (
    'chronos_provider_calendar_dry_run',
    'Dry-runs a calendar provider payload. Does not create calendar events.',
    'internal_action',
    TRUE, TRUE, 'medium', ARRAY['CHRONOS']
)
ON CONFLICT (name) DO NOTHING
"""

# Provider connectors. Seeded disabled + dry_run_only (internal_preview_* are
# enabled but still dry-run). supports_send / supports_calendar_* stay FALSE —
# v0.5 has NO live execution path. ON CONFLICT DO NOTHING so admin edits persist.
SEED_PROVIDER_CONNECTORS_SQL = """
INSERT INTO external_provider_connectors
    (provider_name, provider_type, display_name, description, enabled,
     dry_run_only, supports_send, supports_draft, supports_calendar_create,
     supports_calendar_update, supports_read, requires_oauth, capabilities)
VALUES
    ('internal_preview_email', 'email', 'Internal Email Preview',
     'Generates a normalized email payload preview. No email is sent.',
     TRUE, TRUE, FALSE, TRUE, FALSE, FALSE, FALSE, FALSE,
     '{"preview": true}'::jsonb),
    ('internal_preview_calendar', 'calendar', 'Internal Calendar Preview',
     'Generates a normalized calendar payload preview. No event is created.',
     TRUE, TRUE, FALSE, FALSE, FALSE, FALSE, FALSE, FALSE,
     '{"preview": true}'::jsonb),
    -- supports_read=TRUE (read-only inbox capability, v2.3); supports_send +
    -- supports_calendar_* stay FALSE (write disabled); dry_run_only stays TRUE.
    ('gmail', 'email', 'Gmail',
     'Gmail connector — read-only inbox capability available; sending stays disabled.',
     FALSE, TRUE, FALSE, TRUE, FALSE, FALSE, TRUE, TRUE,
     '{"planned": true, "read_only": true}'::jsonb),
    ('outlook_mail', 'email', 'Outlook Mail',
     'Outlook Mail connector — read-only inbox capability available; sending stays disabled.',
     FALSE, TRUE, FALSE, TRUE, FALSE, FALSE, TRUE, TRUE,
     '{"planned": true, "read_only": true}'::jsonb),
    ('google_calendar', 'calendar', 'Google Calendar',
     'Future Google Calendar connector (scaffolding only). No live API calls.',
     FALSE, TRUE, FALSE, FALSE, FALSE, FALSE, FALSE, TRUE,
     '{"planned": true}'::jsonb),
    ('microsoft_calendar', 'calendar', 'Microsoft Calendar',
     'Future Microsoft Calendar connector (scaffolding only). No live API calls.',
     FALSE, TRUE, FALSE, FALSE, FALSE, FALSE, FALSE, TRUE,
     '{"planned": true}'::jsonb)
ON CONFLICT (provider_name) DO NOTHING
"""


async def _init_pgvector(conn) -> bool:
    """Best-effort pgvector enablement. Adds the vector column when the
    extension is present, otherwise adds an embedding_json JSONB fallback.
    Always adds embedding_model + embedded_at columns."""
    available = False
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
        )
        available = bool(exists)
    except Exception as exc:
        logger.warning(
            "pgvector unavailable; semantic search will be disabled. detail=%s",
            exc,
        )
        available = False

    if available:
        try:
            await conn.execute(
                "ALTER TABLE memory_entries ADD COLUMN IF NOT EXISTS "
                f"embedding vector({settings.embedding_dim})"
            )
            try:
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS memory_entries_embedding_idx "
                    "ON memory_entries USING ivfflat "
                    "(embedding vector_cosine_ops) WITH (lists = 100)"
                )
            except Exception:
                logger.exception(
                    "vector index create failed (continuing)"
                )
        except Exception as exc:
            logger.exception(
                "vector column add failed; falling back to JSONB. detail=%s", exc
            )
            available = False

    if not available:
        await conn.execute(
            "ALTER TABLE memory_entries ADD COLUMN IF NOT EXISTS embedding_json JSONB"
        )

    await conn.execute(
        "ALTER TABLE memory_entries ADD COLUMN IF NOT EXISTS embedding_model TEXT"
    )
    await conn.execute(
        "ALTER TABLE memory_entries ADD COLUMN IF NOT EXISTS embedded_at TIMESTAMPTZ"
    )

    # Chunked Embeddings v0.1: per-chunk embeddings for long content. The
    # embedding column mirrors the pgvector/JSONB choice used for memory_entries.
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_entry_chunks (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            memory_entry_id UUID NOT NULL
                REFERENCES memory_entries(id) ON DELETE CASCADE,
            source_id UUID REFERENCES knowledge_sources(id) ON DELETE CASCADE,
            workspace_id UUID,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            token_estimate INTEGER,
            content_hash TEXT,
            embedding_model TEXT,
            embedded_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    if available:
        await conn.execute(
            "ALTER TABLE memory_entry_chunks ADD COLUMN IF NOT EXISTS "
            f"embedding vector({settings.embedding_dim})"
        )
        try:
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS memory_entry_chunks_embedding_idx "
                "ON memory_entry_chunks USING ivfflat "
                "(embedding vector_cosine_ops) WITH (lists = 100)"
            )
        except Exception:
            logger.exception("chunk vector index create failed (continuing)")
    else:
        await conn.execute(
            "ALTER TABLE memory_entry_chunks ADD COLUMN IF NOT EXISTS "
            "embedding_json JSONB"
        )
    for col in ("memory_entry_id", "source_id", "workspace_id", "content_hash"):
        await conn.execute(
            f"CREATE INDEX IF NOT EXISTS memory_entry_chunks_{col}_idx "
            f"ON memory_entry_chunks ({col})"
        )
    return available


async def init_schema() -> None:
    global PGVECTOR_AVAILABLE
    if clients.db_pool is None:
        logger.warning("Skipping schema init: Postgres pool unavailable")
        return
    async with clients.db_pool.acquire() as conn:
        # SCHEMA_SQL is parameterless multi-statement DDL — fine to send as one
        # string. asyncpg only refuses multi-statement strings when parameters
        # are bound.
        await conn.execute(SCHEMA_SQL)
        PGVECTOR_AVAILABLE = await _init_pgvector(conn)
        logger.info(
            "memory embeddings storage: %s",
            "pgvector"
            if PGVECTOR_AVAILABLE
            else "jsonb-fallback (semantic search disabled)",
        )
        # Seeds run as individual statements (one bound parameter on the first;
        # the rest are constant). All ON CONFLICT DO NOTHING, so re-runs are
        # safe even if rows already exist.
        await conn.execute(
            SEED_N8N_HEALTH_TOOL_SQL, settings.n8n_webhook_health_url
        )
        await conn.execute(SEED_FILESYSTEM_LIST_TOOL_SQL)
        await conn.execute(SEED_FILESYSTEM_READ_TOOL_SQL)
        await conn.execute(SEED_WEB_SEARCH_TOOL_SQL, settings.searxng_endpoint)
        await conn.execute(SEED_SIGNAL_DRAFT_TOOL_SQL)
        await conn.execute(SEED_SIGNAL_DELETE_TOOL_SQL)
        await conn.execute(SEED_SIGNAL_REVIEW_TOOL_SQL)
        await conn.execute(SEED_SIGNAL_APPROVE_TOOL_SQL)
        await conn.execute(SEED_SIGNAL_ARCHIVE_TOOL_SQL)
        await conn.execute(SEED_CHRONOS_PROPOSAL_TOOL_SQL)
        await conn.execute(SEED_CHRONOS_REVIEW_TOOL_SQL)
        await conn.execute(SEED_CHRONOS_APPROVE_TOOL_SQL)
        await conn.execute(SEED_CHRONOS_ARCHIVE_TOOL_SQL)
        await conn.execute(SEED_CHRONOS_DELETE_TOOL_SQL)
        await conn.execute(SEED_SIGNAL_EMAIL_INTENT_TOOL_SQL)
        await conn.execute(SEED_CHRONOS_CALENDAR_INTENT_TOOL_SQL)
        await conn.execute(SEED_SIGNAL_EMAIL_INTEGRATION_INTENT_TOOL_SQL)
        await conn.execute(SEED_CHRONOS_CALENDAR_INTEGRATION_INTENT_TOOL_SQL)
        await conn.execute(SEED_INTEGRATION_INTENT_CANCEL_TOOL_SQL)
        await conn.execute(SEED_INTEGRATION_INTENT_CONFIRM_TOOL_SQL)
        await conn.execute(SEED_INTEGRATION_INTENT_REVOKE_TOOL_SQL)
        await conn.execute(SEED_INTEGRATION_INTENT_READINESS_TOOL_SQL)
        await conn.execute(SEED_PROVIDER_CREDENTIAL_SIM_TOOL_SQL)
        await conn.execute(SEED_EXECUTION_APPROVAL_APPROVE_TOOL_SQL)
        await conn.execute(SEED_EXECUTION_APPROVAL_REJECT_TOOL_SQL)
        await conn.execute(SEED_PROVIDER_PLACEHOLDER_TOOL_SQL)
        await conn.execute(SEED_PROVIDER_DISCONNECT_TOOL_SQL)
        await conn.execute(SEED_PROVIDER_READINESS_TOOL_SQL)
        await conn.execute(SEED_OAUTH_READINESS_SIM_TOOL_SQL)
        await conn.execute(SEED_SIGNAL_PROVIDER_DRY_RUN_TOOL_SQL)
        await conn.execute(SEED_CHRONOS_PROVIDER_DRY_RUN_TOOL_SQL)
        await conn.execute(SEED_PROVIDER_CONNECTORS_SQL)
        # v2.3 capability alignment: gmail/outlook gain the read-only inbox
        # capability on existing rows (seed uses ON CONFLICT DO NOTHING). WRITE
        # capabilities are explicitly held FALSE — read-only never implies send.
        await conn.execute(
            "UPDATE external_provider_connectors "
            "SET supports_read = TRUE, supports_send = FALSE, "
            "    supports_calendar_create = FALSE, supports_calendar_update = FALSE, "
            "    dry_run_only = TRUE, updated_at = NOW() "
            "WHERE provider_name IN ('gmail', 'outlook_mail') "
            "  AND supports_read IS DISTINCT FROM TRUE"
        )
        # CHRONOS Calendar CRUD v1.0 capability alignment: the calendar connectors
        # gain read + full CRUD capability on existing rows. The capability being
        # available does NOT enable execution — the calendar_read/calendar_write
        # feature flags + the global kill switch remain the actual gates.
        await conn.execute(
            "UPDATE external_provider_connectors "
            "SET supports_calendar_read = TRUE, supports_calendar_create = TRUE, "
            "    supports_calendar_update = TRUE, supports_calendar_delete = TRUE, "
            "    updated_at = NOW() "
            "WHERE provider_name IN ('google_calendar', 'microsoft_calendar') "
            "  AND supports_calendar_read IS DISTINCT FROM TRUE"
        )
        await conn.execute(SEED_DEFAULT_WORKSPACE_SQL)
    logger.info(
        "Database schema ensured (users, conversations, messages, agent_runs, "
        "tools, memory_entries, agents, agent_versions, mcp_servers, "
        "tool_execution_policies, tool_execution_logs, runtime_traces, "
        "execution_plans, execution_plan_steps, jobs, workspaces, "
        "agent_delegations, knowledge_sources); "
        "built-in tools + default workspace seeded"
    )
