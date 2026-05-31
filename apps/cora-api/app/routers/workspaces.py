import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field

from app.auth import CurrentUser, get_current_user, require_admin
from app.clients import clients
from app.mcp import McpClient, McpError, get_server_by_name
from app.mcp.registry import config_from_row
from app.memory import embed_memory_entry
from app.runtime_traces import write_trace
from app.url_ingest import UrlIngestError, fetch_and_extract, normalize_url
from app.news_ingest import (
    NewsIngestError,
    article_content_hash,
    fetch_article_body,
    fetch_feed,
    ingest_feed_into_knowledge,
    refresh_feed_source,
    update_feed_metadata,
)
from app.news_briefing import gather_briefing, generate_briefing_summary
from app.workspaces import (
    WorkspaceError,
    create_workspace,
    get_workspace,
    list_workspaces,
    update_workspace,
    workspace_counts,
)
from app import schema as schema_state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


class WorkspaceOut(BaseModel):
    id: str
    owner_user_id: Optional[str]
    name: str
    slug: str
    description: Optional[str]
    status: str
    created_at: datetime
    updated_at: datetime


class WorkspaceDetailOut(WorkspaceOut):
    counts: dict[str, int]


class CreateWorkspaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: Optional[str] = Field(default=None, max_length=80)
    description: Optional[str] = None


class UpdateWorkspaceRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None


class ConversationListItem(BaseModel):
    session_id: str
    created_at: datetime
    updated_at: datetime
    message_count: int


class MemoryListItem(BaseModel):
    id: str
    type: str
    title: str
    scope_type: str
    importance: int
    created_at: datetime


class PlanListItem(BaseModel):
    id: str
    title: str
    status: str
    current_step: int
    total_steps: int
    created_at: datetime


class JobListItem(BaseModel):
    id: str
    job_type: str
    status: str
    attempts: int
    created_at: datetime


class TraceListItem(BaseModel):
    id: str
    trace_type: str
    selected_agent: Optional[str]
    status: str
    duration_ms: Optional[int]
    created_at: datetime


# ---------- Workspace context aggregate ----------


class AgentSummary(BaseModel):
    name: str
    display_name: str
    agent_type: str
    enabled: bool
    current_version_number: Optional[int]


class ToolSummary(BaseModel):
    name: str
    type: str
    enabled: bool
    risk_level: str
    allowed_agents: list[str]
    mcp_server_name: Optional[str]
    mcp_action_name: Optional[str]


class McpServerSummary(BaseModel):
    name: str
    server_type: str
    endpoint: str
    enabled: bool
    capabilities_cached: bool


class ProjectFile(BaseModel):
    name: str
    type: str
    size_bytes: Optional[int]


SourceType = Literal[
    "manual_note", "markdown", "text_file", "url", "generated_summary",
    "system_seed", "news_feed", "news_article"
]


class KnowledgeEntryRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    scope_type: Literal["user", "global", "system"] = "user"
    importance: int = Field(default=3, ge=1, le=5)
    auto_embed: bool = True
    type: str = Field(default="workspace_knowledge", min_length=1, max_length=80)
    source_type: SourceType = "manual_note"
    source_url: Optional[str] = None
    source_filename: Optional[str] = None
    source_id: Optional[str] = Field(
        default=None,
        description="Reuse an existing knowledge_source by id instead of creating one.",
    )


class BulkKnowledgeRequest(BaseModel):
    entries: list[KnowledgeEntryRequest] = Field(min_length=1, max_length=200)


class UrlIngestRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    title: Optional[str] = Field(default=None, max_length=300)
    scope_type: Literal["user", "global", "system"] = "user"
    auto_embed: bool = True


class UrlIngestResponse(BaseModel):
    source_id: Optional[str]
    memory_entry_id: str
    title: str
    url: str
    content_length: int
    content_type: Optional[str] = None
    page_count: Optional[int] = None
    duplicate: bool
    embedded: bool


class NewsIngestRequest(BaseModel):
    source_name: Optional[str] = Field(default=None, max_length=200)
    feed_url: str = Field(min_length=1, max_length=2048)
    max_items: int = Field(default=10, ge=1, le=50)
    scope_type: Literal["user", "global", "system"] = "user"
    importance: int = Field(default=3, ge=1, le=5)
    auto_embed: bool = False
    # v0.2: optionally fetch each article link's full readable body (slower).
    fetch_article_body: bool = False


class NewsArticleOut(BaseModel):
    source_id: str
    memory_entry_id: str
    title: str
    url: Optional[str]
    published_at: Optional[str] = None


class NewsIngestResponse(BaseModel):
    status: Literal["ok", "partial", "error"]
    feed_source_id: Optional[str]
    source_name: str
    feed_url: str
    items_seen: int
    articles_created: int
    articles_updated: int = 0
    articles_skipped_duplicate: int
    article_bodies_fetched: int = 0
    article_body_fetch_failures: int = 0
    errors_count: int
    embedded: int
    errors: list[str] = []
    created_articles: list[NewsArticleOut] = []


class RegisterNewsFeedRequest(BaseModel):
    source_name: Optional[str] = Field(default=None, max_length=200)
    feed_url: str = Field(min_length=1, max_length=2048)
    max_items: int = Field(default=20, ge=1, le=50)
    scope_type: Literal["user", "global", "system"] = "user"
    importance: int = Field(default=3, ge=1, le=5)
    auto_embed: bool = False
    fetch_article_body: bool = False
    refresh_enabled: bool = True
    refresh_interval_minutes: Optional[int] = Field(default=360, ge=0, le=43200)
    ingest_now: bool = False


class UpdateNewsFeedRequest(BaseModel):
    source_name: Optional[str] = Field(default=None, max_length=200)
    max_items: Optional[int] = Field(default=None, ge=1, le=50)
    scope_type: Optional[Literal["user", "global", "system"]] = None
    importance: Optional[int] = Field(default=None, ge=1, le=5)
    auto_embed: Optional[bool] = None
    fetch_article_body: Optional[bool] = None
    refresh_enabled: Optional[bool] = None
    refresh_interval_minutes: Optional[int] = Field(default=None, ge=0, le=43200)


class NewsFeedOut(BaseModel):
    id: str
    workspace_id: Optional[str]
    source_name: str
    feed_url: Optional[str]
    scope_type: str
    importance: int
    max_items: int
    auto_embed: bool
    fetch_article_body: bool
    refresh_enabled: bool
    refresh_interval_minutes: Optional[int]
    next_refresh_at: Optional[str]
    last_checked_at: Optional[str]
    last_success_at: Optional[str]
    last_error: Optional[str]
    last_result: Optional[dict]
    created_at: datetime
    updated_at: datetime


class NewsFeedRefreshResponse(NewsIngestResponse):
    next_refresh_at: Optional[str] = None


class NewsBriefingArticleOut(BaseModel):
    source_id: str
    title: str
    source_url: Optional[str]
    source_type: str
    source_name: Optional[str] = None
    feed_url: Optional[str] = None
    published_at: Optional[str] = None
    created_at: datetime
    content_length: int
    article_body_fetched: bool
    article_fetch_status: Optional[str] = None
    chunk_count: int
    embedded_chunk_count: int
    short_preview: str


class NewsBriefingResponse(BaseModel):
    total_articles: int
    feeds_represented: int
    source_names: list[str]
    since_hours: int
    max_articles: int
    article_body_fetch_success_count: int
    article_body_fetch_failure_count: int
    chunked_article_count: int
    include_summary: bool
    summary: Optional[str] = None
    summary_generated: bool = False
    articles: list[NewsBriefingArticleOut] = []


class KnowledgeEntryOut(BaseModel):
    id: str
    workspace_id: Optional[str]
    title: str
    type: str
    scope_type: str
    scope_id: Optional[str]
    tags: list[str]
    importance: int
    embedded: bool
    embedded_at: Optional[datetime]
    source_id: Optional[str]
    duplicate_warning: bool = False
    created_at: datetime
    updated_at: datetime


class BulkKnowledgeResponse(BaseModel):
    created: int
    embedded: int
    skipped: int
    duplicates: int
    entries: list[KnowledgeEntryOut]


class KnowledgeSourceOut(BaseModel):
    id: str
    workspace_id: Optional[str]
    uploaded_by: Optional[str]
    source_type: str
    title: str
    description: Optional[str]
    original_filename: Optional[str]
    source_url: Optional[str]
    content_hash: Optional[str]
    status: str
    linked_memory_count: int
    metadata: Optional[dict] = None
    created_at: datetime
    updated_at: datetime


class KnowledgeSourceDetailOut(KnowledgeSourceOut):
    content: Optional[str]
    linked_memories: list[KnowledgeEntryOut]


class CreateSourceRequest(BaseModel):
    source_type: SourceType
    title: str = Field(min_length=1, max_length=300)
    description: Optional[str] = None
    original_filename: Optional[str] = None
    source_url: Optional[str] = None
    content: Optional[str] = None


class UpdateSourceRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    source_url: Optional[str] = None
    status: Optional[Literal["active", "archived"]] = None


class WorkspaceContextResponse(BaseModel):
    workspace: WorkspaceOut
    memory: dict
    plans: dict
    jobs: dict
    recent_conversations_count: int
    recent_traces: list[TraceListItem]
    agents: list[AgentSummary]
    tools: list[ToolSummary]
    mcp_servers: list[McpServerSummary]
    project_files: list[ProjectFile]
    project_files_source: str
    project_files_error: Optional[str]


def _row_to_out(row: dict) -> WorkspaceOut:
    return WorkspaceOut(
        id=str(row["id"]),
        owner_user_id=str(row["owner_user_id"]) if row["owner_user_id"] else None,
        name=row["name"],
        slug=row["slug"],
        description=row["description"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _ws_error_to_http(exc: WorkspaceError) -> HTTPException:
    code_map = {
        "conflict": status.HTTP_409_CONFLICT,
        "invalid": status.HTTP_400_BAD_REQUEST,
        "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    }
    return HTTPException(
        status_code=code_map.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=str(exc),
    )


def _parse_uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} must be a valid UUID",
        ) from exc


def _require_pool():
    if clients.db_pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Postgres pool unavailable",
        )
    return clients.db_pool


# ---------- Workspace CRUD ----------


@router.get(
    "",
    response_model=list[WorkspaceOut],
    summary="List active workspaces (any authenticated user).",
)
async def list_workspaces_endpoint(
    current: Annotated[CurrentUser, Depends(get_current_user)],
    include_archived: bool = Query(default=False),
) -> list[WorkspaceOut]:
    rows = await list_workspaces(include_archived=include_archived)
    logger.info(
        "list workspaces: user_id=%s count=%s archived=%s",
        current.id,
        len(rows),
        include_archived,
    )
    return [_row_to_out(r) for r in rows]


@router.post(
    "",
    response_model=WorkspaceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a workspace (admin only).",
)
async def create_workspace_endpoint(
    req: CreateWorkspaceRequest,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> WorkspaceOut:
    try:
        row = await create_workspace(
            name=req.name,
            slug=req.slug,
            description=req.description,
            owner_user_id=admin.id,
        )
    except WorkspaceError as exc:
        raise _ws_error_to_http(exc) from exc
    return _row_to_out(row)


@router.get(
    "/{workspace_id}",
    response_model=WorkspaceDetailOut,
    summary="Get a workspace with cross-resource counts.",
)
async def get_workspace_endpoint(
    workspace_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> WorkspaceDetailOut:
    wid = _parse_uuid(workspace_id, "workspace_id")
    row = await get_workspace(wid)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="workspace not found",
        )
    counts = await workspace_counts(wid)
    base = _row_to_out(row)
    return WorkspaceDetailOut(**base.model_dump(), counts=counts)


@router.patch(
    "/{workspace_id}",
    response_model=WorkspaceOut,
    summary="Update workspace name/description/status (admin only).",
)
async def patch_workspace_endpoint(
    workspace_id: str,
    req: UpdateWorkspaceRequest,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> WorkspaceOut:
    wid = _parse_uuid(workspace_id, "workspace_id")
    try:
        row = await update_workspace(
            wid,
            name=req.name,
            description=req.description,
            status_value=req.status,
        )
    except WorkspaceError as exc:
        raise _ws_error_to_http(exc) from exc
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="workspace not found",
        )
    return _row_to_out(row)


# ---------- Per-workspace resource listings ----------


@router.get(
    "/{workspace_id}/conversations",
    response_model=list[ConversationListItem],
    summary="Conversations in a workspace. Users see their own; admin sees all.",
)
async def workspace_conversations(
    workspace_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
    limit: int = Query(default=50, ge=1, le=500),
) -> list[ConversationListItem]:
    wid = _parse_uuid(workspace_id, "workspace_id")
    pool = _require_pool()
    is_admin = current.role == "admin"
    sql = """
        SELECT c.session_id, c.created_at, c.updated_at,
               COUNT(m.id) AS message_count
        FROM conversations c
        LEFT JOIN messages m ON m.session_id = c.session_id
        WHERE c.workspace_id = $1
    """
    args: list = [wid]
    if not is_admin:
        args.append(current.id)
        sql += f" AND c.scope_type = 'user' AND c.scope_id = ${len(args)}"
    args.append(limit)
    sql += (
        " GROUP BY c.session_id, c.created_at, c.updated_at"
        f" ORDER BY c.updated_at DESC LIMIT ${len(args)}"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [
        ConversationListItem(
            session_id=str(r["session_id"]),
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            message_count=int(r["message_count"] or 0),
        )
        for r in rows
    ]


@router.get(
    "/{workspace_id}/memory",
    response_model=list[MemoryListItem],
    summary="Memory entries visible to the caller within a workspace.",
)
async def workspace_memory(
    workspace_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
    limit: int = Query(default=50, ge=1, le=500),
) -> list[MemoryListItem]:
    wid = _parse_uuid(workspace_id, "workspace_id")
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, type, title, scope_type, importance, created_at
            FROM memory_entries
            WHERE (workspace_id = $1 OR workspace_id IS NULL)
              AND (
                      scope_type = 'global'
                      OR (
                          scope_type = 'user'
                          AND (scope_id = $2 OR scope_id IS NULL)
                      )
                  )
            ORDER BY created_at DESC
            LIMIT $3
            """,
            wid,
            current.id,
            limit,
        )
    return [
        MemoryListItem(
            id=str(r["id"]),
            type=r["type"],
            title=r["title"],
            scope_type=r["scope_type"],
            importance=r["importance"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


@router.get(
    "/{workspace_id}/plans",
    response_model=list[PlanListItem],
    summary="Plans in a workspace. Users see their own; admin sees all.",
)
async def workspace_plans(
    workspace_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
    limit: int = Query(default=50, ge=1, le=500),
) -> list[PlanListItem]:
    wid = _parse_uuid(workspace_id, "workspace_id")
    pool = _require_pool()
    is_admin = current.role == "admin"
    sql = (
        "SELECT id, title, status, current_step, total_steps, created_at "
        "FROM execution_plans WHERE workspace_id = $1"
    )
    args: list = [wid]
    if not is_admin:
        args.append(current.id)
        sql += f" AND user_id = ${len(args)}"
    args.append(limit)
    sql += f" ORDER BY created_at DESC LIMIT ${len(args)}"
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [
        PlanListItem(
            id=str(r["id"]),
            title=r["title"],
            status=r["status"],
            current_step=r["current_step"],
            total_steps=r["total_steps"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


@router.get(
    "/{workspace_id}/jobs",
    response_model=list[JobListItem],
    summary="Jobs in a workspace (admin only).",
)
async def workspace_jobs(
    workspace_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    limit: int = Query(default=50, ge=1, le=500),
) -> list[JobListItem]:
    wid = _parse_uuid(workspace_id, "workspace_id")
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, job_type, status, attempts, created_at
            FROM jobs
            WHERE workspace_id = $1
            ORDER BY created_at DESC LIMIT $2
            """,
            wid,
            limit,
        )
    return [
        JobListItem(
            id=str(r["id"]),
            job_type=r["job_type"],
            status=r["status"],
            attempts=r["attempts"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


@router.get(
    "/{workspace_id}/traces",
    response_model=list[TraceListItem],
    summary="Runtime traces in a workspace (admin only).",
)
async def workspace_traces(
    workspace_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[TraceListItem]:
    wid = _parse_uuid(workspace_id, "workspace_id")
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, trace_type, selected_agent, status, duration_ms, created_at
            FROM runtime_traces
            WHERE workspace_id = $1
            ORDER BY created_at DESC LIMIT $2
            """,
            wid,
            limit,
        )
    return [
        TraceListItem(
            id=str(r["id"]),
            trace_type=r["trace_type"],
            selected_agent=r["selected_agent"],
            status=r["status"],
            duration_ms=r["duration_ms"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


async def _project_files_via_mcp(limit: int = 100) -> tuple[list[ProjectFile], str, Optional[str]]:
    """Best-effort: ask the filesystem MCP server for the workspace root.
    Returns (files, source, error_message).
    source: 'mcp' on success, 'unavailable' otherwise."""
    row = await get_server_by_name("filesystem")
    if row is None:
        return [], "unavailable", "filesystem MCP server not registered"
    if not row["enabled"]:
        return [], "unavailable", "filesystem MCP server disabled"
    client = McpClient(config_from_row(row))
    try:
        result = await client.call_tool("list_directory", {"path": "."})
    except McpError as exc:
        return [], "unavailable", str(exc)
    if not result.success:
        return [], "unavailable", result.error or "list_directory failed"
    payload = result.payload
    # Standard MCP shape: {"content": [{"type": "text", "text": "<json>"}]}
    # Our filesystem server returns the structured result inside that text.
    entries: list[dict] = []
    try:
        if isinstance(payload, dict):
            content = payload.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        import json as _json

                        parsed = _json.loads(item.get("text", "{}"))
                        if isinstance(parsed, dict):
                            entries = parsed.get("entries", []) or []
                            break
            elif "entries" in payload:
                entries = payload.get("entries", []) or []
    except (ValueError, TypeError):
        return [], "mcp", "could not parse filesystem MCP response"
    files = [
        ProjectFile(
            name=str(e.get("name", "")),
            type=str(e.get("type", "file")),
            size_bytes=e.get("size_bytes"),
        )
        for e in entries[:limit]
        if isinstance(e, dict)
    ]
    return files, "mcp", None


@router.get(
    "/{workspace_id}/context",
    response_model=WorkspaceContextResponse,
    summary="Unified context view (workspace + counts + agents + tools + MCP + project files).",
)
async def workspace_context_endpoint(
    workspace_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> WorkspaceContextResponse:
    wid = _parse_uuid(workspace_id, "workspace_id")
    ws_row = await get_workspace(wid)
    if ws_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found"
        )
    pool = _require_pool()
    pgvector = schema_state.is_pgvector_available()
    embedding_column = "embedding" if pgvector else "embedding_json"

    async with pool.acquire() as conn:
        # Memory
        memory_total = int(
            await conn.fetchval(
                "SELECT COUNT(*) FROM memory_entries WHERE workspace_id = $1",
                wid,
            ) or 0
        )
        memory_embedded = int(
            await conn.fetchval(
                f"SELECT COUNT(*) FROM memory_entries "
                f"WHERE workspace_id = $1 AND {embedding_column} IS NOT NULL",
                wid,
            ) or 0
        )

        # Plans
        plans_total = int(
            await conn.fetchval(
                "SELECT COUNT(*) FROM execution_plans WHERE workspace_id = $1",
                wid,
            ) or 0
        )
        plans_active = int(
            await conn.fetchval(
                "SELECT COUNT(*) FROM execution_plans "
                "WHERE workspace_id = $1 AND status IN ('planned','running')",
                wid,
            ) or 0
        )

        # Jobs
        jobs_active = int(
            await conn.fetchval(
                "SELECT COUNT(*) FROM jobs "
                "WHERE workspace_id = $1 AND status IN ('queued','running')",
                wid,
            ) or 0
        )
        jobs_failed = int(
            await conn.fetchval(
                "SELECT COUNT(*) FROM jobs "
                "WHERE workspace_id = $1 AND status = 'failed'",
                wid,
            ) or 0
        )

        # Conversations
        conversations_total = int(
            await conn.fetchval(
                "SELECT COUNT(*) FROM conversations WHERE workspace_id = $1",
                wid,
            ) or 0
        )

        # Recent traces
        trace_rows = await conn.fetch(
            """
            SELECT id, trace_type, selected_agent, status, duration_ms, created_at
            FROM runtime_traces
            WHERE workspace_id = $1
            ORDER BY created_at DESC
            LIMIT 20
            """,
            wid,
        )

        # Agents (workspace-independent)
        agent_rows = await conn.fetch(
            """
            SELECT a.name, a.display_name, a.agent_type, a.enabled,
                   v.version_number AS current_version_number
            FROM agents a
            LEFT JOIN agent_versions v ON v.id = a.current_version_id
            ORDER BY a.name ASC
            """
        )

        # Tools (workspace-independent)
        tool_rows = await conn.fetch(
            """
            SELECT name, type, enabled, risk_level, allowed_agents,
                   mcp_server_name, mcp_action_name
            FROM tools
            ORDER BY name ASC
            """
        )

        # MCP servers (workspace-independent)
        mcp_rows = await conn.fetch(
            """
            SELECT name, server_type, endpoint, enabled,
                   capabilities IS NOT NULL AS capabilities_cached
            FROM mcp_servers
            ORDER BY name ASC
            """
        )

    project_files, files_source, files_error = await _project_files_via_mcp()

    return WorkspaceContextResponse(
        workspace=_row_to_out(ws_row),
        memory={
            "total": memory_total,
            "embedded": memory_embedded,
            "missing": max(0, memory_total - memory_embedded),
            "pgvector_available": pgvector,
        },
        plans={
            "total": plans_total,
            "active": plans_active,
        },
        jobs={
            "active": jobs_active,
            "failed": jobs_failed,
        },
        recent_conversations_count=conversations_total,
        recent_traces=[
            TraceListItem(
                id=str(r["id"]),
                trace_type=r["trace_type"],
                selected_agent=r["selected_agent"],
                status=r["status"],
                duration_ms=r["duration_ms"],
                created_at=r["created_at"],
            )
            for r in trace_rows
        ],
        agents=[
            AgentSummary(
                name=r["name"],
                display_name=r["display_name"],
                agent_type=r["agent_type"],
                enabled=r["enabled"],
                current_version_number=r["current_version_number"],
            )
            for r in agent_rows
        ],
        tools=[
            ToolSummary(
                name=r["name"],
                type=r["type"],
                enabled=r["enabled"],
                risk_level=r["risk_level"],
                allowed_agents=list(r["allowed_agents"] or []),
                mcp_server_name=r["mcp_server_name"],
                mcp_action_name=r["mcp_action_name"],
            )
            for r in tool_rows
        ],
        mcp_servers=[
            McpServerSummary(
                name=r["name"],
                server_type=r["server_type"],
                endpoint=r["endpoint"],
                enabled=r["enabled"],
                capabilities_cached=bool(r["capabilities_cached"]),
            )
            for r in mcp_rows
        ],
        project_files=project_files,
        project_files_source=files_source,
        project_files_error=files_error,
    )


# ---------- Knowledge ingestion ----------


def _knowledge_row_to_out(
    row: dict, *, duplicate_warning: bool = False
) -> KnowledgeEntryOut:
    return KnowledgeEntryOut(
        id=str(row["id"]),
        workspace_id=str(row["workspace_id"]) if row["workspace_id"] else None,
        title=row["title"],
        type=row["type"],
        scope_type=row["scope_type"],
        scope_id=str(row["scope_id"]) if row["scope_id"] else None,
        tags=list(row["tags"]) if row["tags"] is not None else [],
        importance=row["importance"],
        embedded=row.get("embedded_at") is not None,
        embedded_at=row.get("embedded_at"),
        source_id=str(row["source_id"]) if row.get("source_id") else None,
        duplicate_warning=duplicate_warning,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _content_hash(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _source_row_to_out(row: dict, *, linked_count: int = 0) -> KnowledgeSourceOut:
    return KnowledgeSourceOut(
        id=str(row["id"]),
        workspace_id=str(row["workspace_id"]) if row["workspace_id"] else None,
        uploaded_by=str(row["uploaded_by"]) if row["uploaded_by"] else None,
        source_type=row["source_type"],
        title=row["title"],
        description=row["description"],
        original_filename=row["original_filename"],
        source_url=row["source_url"],
        content_hash=row["content_hash"],
        status=row["status"],
        linked_memory_count=linked_count,
        metadata=_as_dict(row.get("metadata")),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _as_dict(value) -> Optional[dict]:
    """asyncpg returns JSONB as dict (codec registered) or str; normalize."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


async def _find_source_by_hash(
    conn, workspace_id: uuid.UUID, content_hash: Optional[str]
) -> Optional[dict]:
    if not content_hash:
        return None
    row = await conn.fetchrow(
        """
        SELECT id, workspace_id, uploaded_by, source_type, title, description,
               original_filename, source_url, content, content_hash, status,
               created_at, updated_at
        FROM knowledge_sources
        WHERE workspace_id = $1 AND content_hash = $2 AND status = 'active'
        ORDER BY created_at ASC
        LIMIT 1
        """,
        workspace_id,
        content_hash,
    )
    return dict(row) if row else None


async def _create_source(
    conn,
    *,
    workspace_id: uuid.UUID,
    uploaded_by: uuid.UUID,
    source_type: str,
    title: str,
    description: Optional[str] = None,
    original_filename: Optional[str] = None,
    source_url: Optional[str] = None,
    content: Optional[str] = None,
) -> dict:
    row = await conn.fetchrow(
        """
        INSERT INTO knowledge_sources (
            workspace_id, uploaded_by, source_type, title, description,
            original_filename, source_url, content, content_hash
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING id, workspace_id, uploaded_by, source_type, title, description,
                  original_filename, source_url, content, content_hash, status,
                  created_at, updated_at
        """,
        workspace_id,
        uploaded_by,
        source_type,
        title,
        description,
        original_filename,
        source_url,
        content,
        _content_hash(content),
    )
    return dict(row)


async def _insert_knowledge(
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    req: KnowledgeEntryRequest,
) -> tuple[dict, bool]:
    """Insert a knowledge entry. Returns (memory_row, duplicate_warning).

    Linkage rules:
      - If `req.source_id` provided, link memory to that existing source.
      - Else if a knowledge_source with matching content_hash already exists
        in this workspace, reuse it and set duplicate_warning=True.
      - Else create a new knowledge_source row.
    """
    pool = _require_pool()
    scope_id = user_id if req.scope_type == "user" else None
    tags = list(req.tags or [])
    if "knowledge_ingested" not in tags:
        tags.append("knowledge_ingested")

    chash = _content_hash(req.content)
    duplicate = False
    source_uuid: Optional[uuid.UUID] = None

    async with pool.acquire() as conn:
        async with conn.transaction():
            if req.source_id:
                try:
                    source_uuid = uuid.UUID(req.source_id)
                except ValueError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="source_id must be a valid UUID",
                    ) from exc
                existing_source = await conn.fetchrow(
                    "SELECT id FROM knowledge_sources WHERE id = $1",
                    source_uuid,
                )
                if existing_source is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="source_id not found",
                    )
            else:
                existing = await _find_source_by_hash(conn, workspace_id, chash)
                if existing:
                    source_uuid = existing["id"]
                    duplicate = True
                else:
                    src = await _create_source(
                        conn,
                        workspace_id=workspace_id,
                        uploaded_by=user_id,
                        source_type=req.source_type,
                        title=req.title,
                        description=None,
                        original_filename=req.source_filename,
                        source_url=req.source_url,
                        content=req.content,
                    )
                    source_uuid = src["id"]

            row = await conn.fetchrow(
                """
                INSERT INTO memory_entries (
                    type, title, content, tags, importance,
                    scope_type, scope_id, workspace_id, source_id
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING id, workspace_id, type, title, content, tags,
                          importance, scope_type, scope_id, embedded_at,
                          source_id, created_at, updated_at
                """,
                req.type,
                req.title,
                req.content,
                tags,
                req.importance,
                req.scope_type,
                scope_id,
                workspace_id,
                source_uuid,
            )
    return dict(row), duplicate


@router.post(
    "/{workspace_id}/knowledge",
    response_model=KnowledgeEntryOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a structured knowledge entry to a workspace.",
)
async def ingest_knowledge_endpoint(
    workspace_id: str,
    req: KnowledgeEntryRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> KnowledgeEntryOut:
    wid = _parse_uuid(workspace_id, "workspace_id")
    ws_row = await get_workspace(wid)
    if ws_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found"
        )
    is_admin = current.role == "admin"
    if req.scope_type in ("global", "system") and not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"admin role required to create {req.scope_type} knowledge",
        )

    row, duplicate = await _insert_knowledge(
        workspace_id=wid, user_id=current.id, req=req
    )
    embedded = False
    if req.auto_embed:
        try:
            embed_res = await embed_memory_entry(row["id"])
            if embed_res.get("status") == "ok":
                # Refresh row to pick up embedded_at
                async with _require_pool().acquire() as conn:
                    row = dict(
                        await conn.fetchrow(
                            """
                            SELECT id, workspace_id, type, title, content, tags,
                                   importance, scope_type, scope_id, embedded_at,
                                   source_id, created_at, updated_at
                            FROM memory_entries WHERE id = $1
                            """,
                            row["id"],
                        )
                    )
                embedded = True
        except Exception:
            logger.exception(
                "knowledge auto-embed failed: workspace=%s entry=%s",
                wid,
                row["id"],
            )

    await write_trace(
        session_id=None,
        user_id=current.id,
        trace_type="knowledge_ingested",
        status="ok",
        selected_agent="ATLAS",
        tool_name="knowledge_ingest",
        tool_result={
            "memory_id": str(row["id"]),
            "scope_type": row["scope_type"],
            "title": row["title"],
            "tags": list(row["tags"] or []),
            "auto_embed_requested": req.auto_embed,
            "embedded": embedded,
        },
        workspace_id=wid,
        metadata={
            "workspace_id": str(wid),
            "workspace_name": ws_row["name"],
            "entry_count": 1,
            "embedded_count": 1 if embedded else 0,
        },
    )
    logger.info(
        "knowledge ingested: user=%s workspace=%s memory_id=%s embedded=%s "
        "duplicate=%s source_id=%s",
        current.id,
        wid,
        row["id"],
        embedded,
        duplicate,
        row.get("source_id"),
    )
    return _knowledge_row_to_out(row, duplicate_warning=duplicate)


@router.post(
    "/{workspace_id}/knowledge/bulk",
    response_model=BulkKnowledgeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Bulk-insert knowledge entries into a workspace.",
)
async def ingest_knowledge_bulk_endpoint(
    workspace_id: str,
    req: BulkKnowledgeRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> BulkKnowledgeResponse:
    wid = _parse_uuid(workspace_id, "workspace_id")
    ws_row = await get_workspace(wid)
    if ws_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found"
        )
    is_admin = current.role == "admin"
    for entry in req.entries:
        if entry.scope_type in ("global", "system") and not is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"admin role required for {entry.scope_type} entries; "
                    "remove them or downgrade to user scope"
                ),
            )

    created_rows: list[tuple[dict, bool]] = []
    embedded_count = 0
    skipped = 0
    duplicate_count = 0
    for entry in req.entries:
        try:
            row, duplicate = await _insert_knowledge(
                workspace_id=wid, user_id=current.id, req=entry
            )
        except Exception:
            logger.exception(
                "bulk knowledge insert failed: workspace=%s title=%r",
                wid,
                entry.title,
            )
            skipped += 1
            continue

        if duplicate:
            duplicate_count += 1

        if entry.auto_embed:
            try:
                embed_res = await embed_memory_entry(row["id"])
                if embed_res.get("status") == "ok":
                    async with _require_pool().acquire() as conn:
                        row = dict(
                            await conn.fetchrow(
                                """
                                SELECT id, workspace_id, type, title, content,
                                       tags, importance, scope_type, scope_id,
                                       embedded_at, source_id, created_at, updated_at
                                FROM memory_entries WHERE id = $1
                                """,
                                row["id"],
                            )
                        )
                    embedded_count += 1
            except Exception:
                logger.exception(
                    "bulk knowledge auto-embed failed: workspace=%s entry=%s",
                    wid,
                    row["id"],
                )
        created_rows.append((row, duplicate))

    await write_trace(
        session_id=None,
        user_id=current.id,
        trace_type="knowledge_ingested",
        status="ok",
        selected_agent="ATLAS",
        tool_name="knowledge_ingest_bulk",
        tool_result={
            "entry_count": len(created_rows),
            "embedded_count": embedded_count,
            "skipped": skipped,
        },
        workspace_id=wid,
        metadata={
            "workspace_id": str(wid),
            "workspace_name": ws_row["name"],
            "entry_count": len(created_rows),
            "embedded_count": embedded_count,
            "skipped": skipped,
            "bulk": True,
        },
    )
    logger.info(
        "knowledge ingested (bulk): user=%s workspace=%s entries=%s "
        "embedded=%s skipped=%s duplicates=%s",
        current.id,
        wid,
        len(created_rows),
        embedded_count,
        skipped,
        duplicate_count,
    )
    return BulkKnowledgeResponse(
        created=len(created_rows),
        embedded=embedded_count,
        skipped=skipped,
        duplicates=duplicate_count,
        entries=[
            _knowledge_row_to_out(r, duplicate_warning=dup)
            for r, dup in created_rows
        ],
    )


@router.get(
    "/{workspace_id}/knowledge",
    response_model=list[KnowledgeEntryOut],
    summary="List knowledge entries in a workspace (by 'knowledge_ingested' tag).",
)
async def list_knowledge_endpoint(
    workspace_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
    limit: int = Query(default=100, ge=1, le=500),
) -> list[KnowledgeEntryOut]:
    wid = _parse_uuid(workspace_id, "workspace_id")
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, workspace_id, type, title, content, tags,
                   importance, scope_type, scope_id, embedded_at,
                   source_id, created_at, updated_at
            FROM memory_entries
            WHERE workspace_id = $1
              AND 'knowledge_ingested' = ANY(tags)
              AND (
                      scope_type IN ('global','system')
                      OR (scope_type = 'user' AND scope_id = $2)
                  )
            ORDER BY created_at DESC
            LIMIT $3
            """,
            wid,
            current.id,
            limit,
        )
    return [_knowledge_row_to_out(dict(r)) for r in rows]


MAX_UPLOAD_BYTES = 512 * 1024  # 512 KiB cap for v0.1
_SUPPORTED_EXTENSIONS: dict[str, str] = {
    ".txt": "text_file",
    ".md": "markdown",
    ".markdown": "markdown",
    ".json": "text_file",
}


@router.post(
    "/{workspace_id}/knowledge/upload",
    response_model=KnowledgeEntryOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a text/markdown/JSON file and ingest as a knowledge source + memory entry.",
)
async def upload_knowledge_endpoint(
    workspace_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
    file: UploadFile = File(...),
    scope_type: Literal["user", "global", "system"] = Form("user"),
    importance: int = Form(3, ge=1, le=5),
    auto_embed: bool = Form(True),
    tags: str = Form(""),
) -> KnowledgeEntryOut:
    wid = _parse_uuid(workspace_id, "workspace_id")
    ws_row = await get_workspace(wid)
    if ws_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found"
        )

    is_admin = current.role == "admin"
    if scope_type in ("global", "system") and not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"admin role required to ingest {scope_type} knowledge",
        )

    filename = file.filename or ""
    ext = ""
    if "." in filename:
        ext = "." + filename.rsplit(".", 1)[-1].lower()
    if ext not in _SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"unsupported file extension {ext!r}; "
                "v0.1 accepts .txt, .md, .markdown, .json"
            ),
        )
    source_type = _SUPPORTED_EXTENSIONS[ext]

    # Read with size cap. We read MAX_UPLOAD_BYTES + 1 so we can detect
    # files that exceed the limit without buffering them fully.
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"file exceeds v0.1 upload cap ({MAX_UPLOAD_BYTES} bytes). "
                "Split it into smaller files."
            ),
        )
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="file is not valid UTF-8 text",
        ) from exc
    if not content.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="uploaded file is empty",
        )

    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    if "uploaded_file" not in tag_list:
        tag_list.append("uploaded_file")
    if source_type not in tag_list:
        tag_list.append(source_type)

    # Build the KnowledgeEntryRequest the existing ingestion helper expects,
    # so dedup + source creation + memory linkage all stay in one place.
    req = KnowledgeEntryRequest(
        title=filename or "uploaded file",
        content=content,
        tags=tag_list,
        scope_type=scope_type,
        importance=importance,
        auto_embed=auto_embed,
        type="workspace_knowledge",
        source_type=source_type,
        source_filename=filename,
    )

    row, duplicate = await _insert_knowledge(
        workspace_id=wid, user_id=current.id, req=req
    )
    embedded = False
    if auto_embed:
        try:
            embed_res = await embed_memory_entry(row["id"])
            if embed_res.get("status") == "ok":
                async with _require_pool().acquire() as conn:
                    row = dict(
                        await conn.fetchrow(
                            """
                            SELECT id, workspace_id, type, title, content, tags,
                                   importance, scope_type, scope_id, embedded_at,
                                   source_id, created_at, updated_at
                            FROM memory_entries WHERE id = $1
                            """,
                            row["id"],
                        )
                    )
                embedded = True
        except Exception:
            logger.exception(
                "uploaded knowledge auto-embed failed: workspace=%s entry=%s",
                wid,
                row["id"],
            )

    await write_trace(
        session_id=None,
        user_id=current.id,
        trace_type="knowledge_file_uploaded",
        status="ok",
        selected_agent="ATLAS",
        tool_name="knowledge_upload",
        tool_result={
            "filename": filename,
            "source_type": source_type,
            "memory_id": str(row["id"]),
            "source_id": str(row["source_id"]) if row.get("source_id") else None,
            "size_bytes": len(raw),
            "tags": tag_list,
            "auto_embed_requested": auto_embed,
            "embedded": embedded,
            "duplicate_warning": duplicate,
        },
        workspace_id=wid,
        metadata={
            "workspace_id": str(wid),
            "workspace_name": ws_row["name"],
            "filename": filename,
            "source_type": source_type,
            "memory_id": str(row["id"]),
            "source_id": str(row["source_id"]) if row.get("source_id") else None,
            "auto_embed": auto_embed,
            "embedded": embedded,
            "duplicate_warning": duplicate,
            "size_bytes": len(raw),
        },
    )
    logger.info(
        "knowledge uploaded: user=%s workspace=%s filename=%r size=%s "
        "source_id=%s memory_id=%s embedded=%s duplicate=%s",
        current.id,
        wid,
        filename,
        len(raw),
        row.get("source_id"),
        row["id"],
        embedded,
        duplicate,
    )
    return _knowledge_row_to_out(row, duplicate_warning=duplicate)


_URL_ERROR_STATUS = {
    "invalid_url": status.HTTP_400_BAD_REQUEST,
    "unsupported_content_type": status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    "empty": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "no_content": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "pdf_extract_failed": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "pdf_too_large": status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    "fetch_failed": status.HTTP_502_BAD_GATEWAY,
}


@router.post(
    "/{workspace_id}/knowledge/url",
    response_model=UrlIngestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Fetch a single public URL and ingest its readable content.",
)
async def ingest_url_knowledge_endpoint(
    workspace_id: str,
    req: UrlIngestRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> UrlIngestResponse:
    wid = _parse_uuid(workspace_id, "workspace_id")
    ws_row = await get_workspace(wid)
    if ws_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found"
        )
    is_admin = current.role == "admin"
    if req.scope_type in ("global", "system") and not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"admin role required to ingest {req.scope_type} knowledge",
        )

    # Fetch + extract (no crawling; PDFs/binaries rejected). Trace failures too.
    try:
        norm_url = normalize_url(req.url)
        result = await fetch_and_extract(norm_url)
    except UrlIngestError as exc:
        await write_trace(
            session_id=None,
            user_id=current.id,
            trace_type="knowledge_url_ingested",
            status="error",
            selected_agent="ATLAS",
            tool_name="knowledge_url_ingest",
            tool_result={"url": req.url, "error": str(exc), "code": exc.code},
            workspace_id=wid,
            error_message=str(exc),
        )
        raise HTTPException(
            status_code=_URL_ERROR_STATUS.get(
                exc.code, status.HTTP_400_BAD_REQUEST
            ),
            detail=str(exc),
        ) from exc

    title = (req.title or result["title"] or result["url"]).strip()[:300]
    content = result["content"]
    page_count = result.get("page_count")
    is_pdf = result["content_type"] == "application/pdf"
    meta_note = (
        f"Ingested from {result['url']} · HTTP {result['status_code']} · "
        f"{result['content_type']} · {result['extraction_method']} · "
        f"{len(content)} chars"
        + (f" · {page_count} pages" if page_count is not None else "")
        + (" · truncated" if result["truncated"] else "")
    )

    tags = ["url_ingested", "url"]
    if is_pdf:
        tags.append("pdf")

    kreq = KnowledgeEntryRequest(
        title=title,
        content=content,
        tags=tags,
        scope_type=req.scope_type,
        importance=3,
        auto_embed=req.auto_embed,
        type="workspace_knowledge",
        source_type="url",
        source_url=result["url"],
    )

    row, duplicate = await _insert_knowledge(
        workspace_id=wid, user_id=current.id, req=kreq
    )

    # Persist fetch metadata on the freshly-created source row (description +
    # structured JSONB used by the refresh endpoint). Skip on dedupe — we reuse
    # the existing source and must not clobber it.
    source_id = row.get("source_id")
    if not duplicate and source_id:
        source_metadata = {
            "ingest_method": "single_url",
            "content_type": result["content_type"],
            "extraction_method": result["extraction_method"],
            "page_count": page_count,
            "status_code": result["status_code"],
            "fetched_at": result["fetched_at"],
            "last_checked_at": result["fetched_at"],
            "title_source": "manual" if req.title else "auto",
            "truncated": result["truncated"],
        }
        try:
            async with _require_pool().acquire() as conn:
                await conn.execute(
                    "UPDATE knowledge_sources "
                    "SET description = COALESCE(description, $2), metadata = $3 "
                    "WHERE id = $1",
                    source_id,
                    meta_note,
                    source_metadata,
                )
        except Exception:
            logger.exception(
                "failed to store url metadata on source=%s", source_id
            )

    embedded = False
    if req.auto_embed:
        try:
            embed_res = await embed_memory_entry(row["id"])
            if embed_res.get("status") == "ok":
                async with _require_pool().acquire() as conn:
                    row = dict(
                        await conn.fetchrow(
                            """
                            SELECT id, workspace_id, type, title, content, tags,
                                   importance, scope_type, scope_id, embedded_at,
                                   source_id, created_at, updated_at
                            FROM memory_entries WHERE id = $1
                            """,
                            row["id"],
                        )
                    )
                embedded = True
        except Exception:
            logger.exception(
                "url knowledge auto-embed failed: workspace=%s entry=%s",
                wid,
                row["id"],
            )

    await write_trace(
        session_id=None,
        user_id=current.id,
        trace_type="knowledge_url_ingested",
        status="ok",
        selected_agent="ATLAS",
        tool_name="knowledge_url_ingest",
        tool_result={
            "url": result["url"],
            "title": title,
            "status_code": result["status_code"],
            "content_type": result["content_type"],
            "content_length": len(content),
            "page_count": page_count,
            "truncated": result["truncated"],
            "memory_id": str(row["id"]),
            "source_id": str(source_id) if source_id else None,
            "duplicate": duplicate,
            "auto_embed_requested": req.auto_embed,
            "embedded": embedded,
        },
        workspace_id=wid,
        metadata={
            "workspace_id": str(wid),
            "workspace_name": ws_row["name"],
            "source_type": "url",
            "ingest_method": "single_url",
            "content_type": result["content_type"],
            "extraction_method": result["extraction_method"],
            "page_count": page_count,
            "fetched_at": result["fetched_at"],
            "status_code": result["status_code"],
            "url": result["url"],
            "memory_id": str(row["id"]),
            "source_id": str(source_id) if source_id else None,
            "duplicate": duplicate,
            "embedded": embedded,
        },
    )
    logger.info(
        "knowledge url ingested: user=%s workspace=%s url=%s memory_id=%s "
        "source_id=%s len=%s embedded=%s duplicate=%s",
        current.id,
        wid,
        result["url"],
        row["id"],
        source_id,
        len(content),
        embedded,
        duplicate,
    )
    return UrlIngestResponse(
        source_id=str(source_id) if source_id else None,
        memory_entry_id=str(row["id"]),
        title=title,
        url=result["url"],
        content_length=len(content),
        content_type=result["content_type"],
        page_count=page_count,
        duplicate=duplicate,
        embedded=embedded,
    )


_NEWS_ERROR_STATUS = {
    "invalid_url": status.HTTP_400_BAD_REQUEST,
    "invalid_feed": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "fetch_failed": status.HTTP_502_BAD_GATEWAY,
}


@router.post(
    "/{workspace_id}/knowledge/news",
    response_model=NewsIngestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest articles from an RSS/Atom feed into workspace knowledge.",
)
async def ingest_news_feed_endpoint(
    workspace_id: str,
    req: NewsIngestRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> NewsIngestResponse:
    wid = _parse_uuid(workspace_id, "workspace_id")
    ws_row = await get_workspace(wid)
    if ws_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found"
        )
    if req.scope_type in ("global", "system") and current.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"admin role required to ingest {req.scope_type} knowledge",
        )
    try:
        result = await ingest_feed_into_knowledge(
            workspace_id=wid,
            uploaded_by=current.id,
            source_name=req.source_name,
            feed_url=req.feed_url,
            max_items=req.max_items,
            scope_type=req.scope_type,
            importance=req.importance,
            auto_embed=req.auto_embed,
            fetch_article_body=req.fetch_article_body,
        )
    except NewsIngestError as exc:
        await write_trace(
            session_id=None, user_id=current.id, trace_type="news_feed_ingest",
            status="error", selected_agent="ATLAS", tool_name="news_feed_ingest",
            tool_result={"feed_url": req.feed_url, "error": str(exc), "code": exc.code},
            workspace_id=wid, error_message=str(exc),
        )
        raise HTTPException(
            status_code=_NEWS_ERROR_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
            detail=str(exc),
        ) from exc
    await _trace_news_ingest(current.id, wid, result, req.fetch_article_body, req.auto_embed)
    return _news_result_to_response(result)


def _trace_news_ingest(*args, **kwargs):
    return write_trace(
        session_id=None,
        user_id=args[0],
        trace_type="news_feed_ingest",
        status="error" if args[2]["status"] == "error" else "ok",
        selected_agent="ATLAS",
        tool_name="news_feed_ingest",
        tool_result={
            "source_name": args[2]["source_name"],
            "feed_url": args[2]["feed_url"],
            "feed_source_id": args[2]["feed_source_id"],
            "fetch_article_body": args[3],
            "embedded": args[2]["embedded"],
            "items_seen": args[2]["items_seen"],
            "articles_created": args[2]["articles_created"],
            "articles_updated": args[2]["articles_updated"],
            "articles_skipped_duplicate": args[2]["articles_skipped_duplicate"],
            "article_bodies_fetched": args[2]["article_bodies_fetched"],
            "article_body_fetch_failures": args[2]["article_body_fetch_failures"],
            "errors_count": args[2]["errors_count"],
        },
        workspace_id=args[1],
        metadata={
            "source_name": args[2]["source_name"],
            "feed_url": args[2]["feed_url"],
            "fetch_article_body": args[3],
            "auto_embed": args[4],
            "workspace_id": str(args[1]),
            "items_seen": args[2]["items_seen"],
            "articles_created": args[2]["articles_created"],
            "articles_updated": args[2]["articles_updated"],
            "articles_skipped_duplicate": args[2]["articles_skipped_duplicate"],
            "article_bodies_fetched": args[2]["article_bodies_fetched"],
            "article_body_fetch_failures": args[2]["article_body_fetch_failures"],
            "errors_count": args[2]["errors_count"],
        },
    )


def _news_result_to_response(result: dict) -> NewsIngestResponse:
    return NewsIngestResponse(
        status=result["status"],
        feed_source_id=result["feed_source_id"],
        source_name=result["source_name"],
        feed_url=result["feed_url"],
        items_seen=result["items_seen"],
        articles_created=result["articles_created"],
        articles_updated=result["articles_updated"],
        articles_skipped_duplicate=result["articles_skipped_duplicate"],
        article_bodies_fetched=result["article_bodies_fetched"],
        article_body_fetch_failures=result["article_body_fetch_failures"],
        errors_count=result["errors_count"],
        embedded=result["embedded"],
        errors=result["errors"][:20],
        created_articles=[NewsArticleOut(**a) for a in result["created_articles"]],
    )


def _feed_row_to_out(row) -> NewsFeedOut:
    m = _as_dict(row["metadata"]) or {}
    return NewsFeedOut(
        id=str(row["id"]),
        workspace_id=str(row["workspace_id"]) if row["workspace_id"] else None,
        source_name=m.get("source_name") or row["title"],
        feed_url=m.get("feed_url") or row["source_url"],
        scope_type=m.get("scope_type", "user"),
        importance=int(m.get("importance", 3)),
        max_items=int(m.get("max_items", 20)),
        auto_embed=bool(m.get("auto_embed", False)),
        fetch_article_body=bool(m.get("fetch_article_body", False)),
        refresh_enabled=bool(m.get("refresh_enabled", False)),
        refresh_interval_minutes=m.get("refresh_interval_minutes"),
        next_refresh_at=m.get("next_refresh_at"),
        last_checked_at=m.get("last_checked_at"),
        last_success_at=m.get("last_success_at"),
        last_error=m.get("last_error"),
        last_result=m.get("last_result"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


_FEED_COLS = (
    "id, workspace_id, title, source_url, metadata, created_at, updated_at"
)


@router.get(
    "/{workspace_id}/knowledge/news/feeds",
    response_model=list[NewsFeedOut],
    summary="List registered news feeds (news_feed knowledge sources).",
)
async def list_news_feeds_endpoint(
    workspace_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> list[NewsFeedOut]:
    wid = _parse_uuid(workspace_id, "workspace_id")
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_FEED_COLS} FROM knowledge_sources "
            "WHERE source_type='news_feed' AND status='active' "
            "AND (workspace_id = $1 OR ($1 IS NULL AND workspace_id IS NULL)) "
            "ORDER BY created_at DESC",
            wid,
        )
    return [_feed_row_to_out(r) for r in rows]


@router.post(
    "/{workspace_id}/knowledge/news/feeds",
    response_model=NewsFeedOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register an RSS/Atom feed for scheduled refresh (optionally ingest now).",
)
async def register_news_feed_endpoint(
    workspace_id: str,
    req: RegisterNewsFeedRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> NewsFeedOut:
    wid = _parse_uuid(workspace_id, "workspace_id")
    ws_row = await get_workspace(wid)
    if ws_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found")
    if req.scope_type in ("global", "system") and current.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"admin role required for {req.scope_type} scope",
        )
    try:
        norm_url = normalize_url(req.feed_url)
    except UrlIngestError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    now = datetime.now(timezone.utc)
    interval = req.refresh_interval_minutes
    scheduled = bool(req.refresh_enabled and interval and interval > 0)
    if scheduled:
        next_at = (now if not req.ingest_now else now + timedelta(minutes=interval)).isoformat()
    else:
        next_at = None
    source_name = (req.source_name or norm_url).strip()[:200]
    settings = {
        "ingest_method": "news_feed",
        "source_name": source_name,
        "feed_url": norm_url,
        "max_items": req.max_items,
        "scope_type": req.scope_type,
        "importance": req.importance,
        "auto_embed": req.auto_embed,
        "fetch_article_body": req.fetch_article_body,
        "refresh_enabled": req.refresh_enabled,
        "refresh_interval_minutes": interval,
        "next_refresh_at": next_at,
        "registered_by": str(current.id),
    }
    pool = _require_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id, metadata FROM knowledge_sources WHERE source_type='news_feed' "
            "AND source_url=$2 AND (workspace_id=$1 OR ($1 IS NULL AND workspace_id IS NULL)) "
            "ORDER BY created_at ASC LIMIT 1",
            wid, norm_url,
        )
        if existing:
            merged = {**(_as_dict(existing["metadata"]) or {}), **settings}
            await conn.execute(
                "UPDATE knowledge_sources SET title=$2, metadata=$3, updated_at=NOW() WHERE id=$1",
                existing["id"], source_name, merged,
            )
            feed_id = existing["id"]
        else:
            settings.update({
                "last_checked_at": None, "last_success_at": None,
                "last_error": None, "last_result": None,
            })
            feed_id = await conn.fetchval(
                """
                INSERT INTO knowledge_sources
                    (workspace_id, uploaded_by, source_type, title, description,
                     source_url, content, content_hash, metadata)
                VALUES ($1, $2, 'news_feed', $3, $4, $5, NULL, NULL, $6)
                RETURNING id
                """,
                wid, current.id, source_name,
                "RSS/Atom feed (scheduled)", norm_url, settings,
            )

    await write_trace(
        session_id=None, user_id=current.id, trace_type="news_feed_registered",
        status="ok", selected_agent="ATLAS", tool_name="news_feed_register",
        tool_result={
            "source_id": str(feed_id), "feed_url": norm_url, "source_name": source_name,
            "refresh_enabled": req.refresh_enabled,
            "refresh_interval_minutes": interval, "ingest_now": req.ingest_now,
        },
        workspace_id=wid,
    )

    if req.ingest_now:
        try:
            result = await ingest_feed_into_knowledge(
                workspace_id=wid, uploaded_by=current.id, source_name=source_name,
                feed_url=norm_url, max_items=req.max_items, scope_type=req.scope_type,
                importance=req.importance, auto_embed=req.auto_embed,
                fetch_article_body=req.fetch_article_body,
            )
            await update_feed_metadata(feed_id, {
                "last_checked_at": now.isoformat(),
                "last_success_at": now.isoformat(),
                "last_error": None,
                "last_result": {
                    "items_seen": result["items_seen"],
                    "articles_created": result["articles_created"],
                    "articles_updated": result["articles_updated"],
                    "articles_skipped_duplicate": result["articles_skipped_duplicate"],
                    "article_bodies_fetched": result["article_bodies_fetched"],
                    "article_body_fetch_failures": result["article_body_fetch_failures"],
                    "errors_count": result["errors_count"],
                },
            })
            await _trace_news_ingest(current.id, wid, result, req.fetch_article_body, req.auto_embed)
        except NewsIngestError as exc:
            await update_feed_metadata(feed_id, {
                "last_checked_at": now.isoformat(), "last_error": str(exc),
            })
            logger.warning("register ingest_now failed: feed=%s err=%s", norm_url, exc)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_FEED_COLS} FROM knowledge_sources WHERE id=$1", feed_id
        )
    return _feed_row_to_out(row)


@router.get(
    "/{workspace_id}/knowledge/news/briefing",
    response_model=NewsBriefingResponse,
    summary="PULSE news briefing over recently-ingested news articles.",
)
async def news_briefing_endpoint(
    workspace_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
    since_hours: int = Query(default=24, ge=1, le=8760),
    max_articles: int = Query(default=25, ge=1, le=200),
    source_name: Optional[str] = Query(default=None),
    include_summary: bool = Query(default=False),
) -> NewsBriefingResponse:
    wid = _parse_uuid(workspace_id, "workspace_id")
    data = await gather_briefing(
        workspace_id=wid,
        since_hours=since_hours,
        max_articles=max_articles,
        source_name=source_name,
    )
    agg = data["aggregate"]
    articles = [NewsBriefingArticleOut(**a) for a in data["articles"]]

    summary_text: Optional[str] = None
    summary_generated = False
    if include_summary and data["articles"]:
        summary_text = await generate_briefing_summary(data["articles"])
        summary_generated = summary_text is not None

    await write_trace(
        session_id=None,
        user_id=current.id,
        trace_type=(
            "pulse_news_briefing_generated"
            if include_summary
            else "pulse_news_briefing_viewed"
        ),
        status="ok",
        selected_agent="PULSE" if summary_generated else "ATLAS",
        tool_name="pulse_news_briefing",
        tool_result={
            "since_hours": since_hours,
            "max_articles": max_articles,
            "source_name": source_name,
            "total_articles": agg["total_articles"],
            "include_summary": include_summary,
            "summary_generated": summary_generated,
        },
        workspace_id=wid,
    )
    return NewsBriefingResponse(
        total_articles=agg["total_articles"],
        feeds_represented=agg["feeds_represented"],
        source_names=agg["source_names"],
        since_hours=since_hours,
        max_articles=max_articles,
        article_body_fetch_success_count=agg["article_body_fetch_success_count"],
        article_body_fetch_failure_count=agg["article_body_fetch_failure_count"],
        chunked_article_count=agg["chunked_article_count"],
        include_summary=include_summary,
        summary=summary_text,
        summary_generated=summary_generated,
        articles=articles,
    )


# ---------- Knowledge sources ----------


@router.get(
    "/{workspace_id}/knowledge/sources",
    response_model=list[KnowledgeSourceOut],
    summary="List knowledge sources in a workspace.",
)
async def list_sources_endpoint(
    workspace_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
    include_archived: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[KnowledgeSourceOut]:
    wid = _parse_uuid(workspace_id, "workspace_id")
    pool = _require_pool()
    # Include legacy / unassigned rows where workspace_id IS NULL so sources
    # that pre-date workspace scoping (or were inserted directly) still surface.
    sql_base = """
        SELECT s.id, s.workspace_id, s.uploaded_by, s.source_type, s.title,
               s.description, s.original_filename, s.source_url,
               s.content_hash, s.status, s.metadata, s.created_at, s.updated_at,
               (
                   SELECT COUNT(*) FROM memory_entries m
                   WHERE m.source_id = s.id
               ) AS linked_memory_count
        FROM knowledge_sources s
        WHERE (s.workspace_id = $1 OR s.workspace_id IS NULL)
    """
    args: list = [wid]
    if not include_archived:
        sql_base += " AND s.status = 'active'"
    args.append(limit)
    sql_base += f" ORDER BY s.created_at DESC LIMIT ${len(args)}"
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql_base, *args)
    logger.info(
        "list knowledge sources: user_id=%s workspace=%s include_archived=%s count=%s",
        current.id,
        wid,
        include_archived,
        len(rows),
    )
    return [
        _source_row_to_out(dict(r), linked_count=int(r["linked_memory_count"] or 0))
        for r in rows
    ]


@router.post(
    "/{workspace_id}/knowledge/sources",
    response_model=KnowledgeSourceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a knowledge source (without memory linkage).",
)
async def create_source_endpoint(
    workspace_id: str,
    req: CreateSourceRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> KnowledgeSourceOut:
    wid = _parse_uuid(workspace_id, "workspace_id")
    ws_row = await get_workspace(wid)
    if ws_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found"
        )
    pool = _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await _create_source(
                conn,
                workspace_id=wid,
                uploaded_by=current.id,
                source_type=req.source_type,
                title=req.title,
                description=req.description,
                original_filename=req.original_filename,
                source_url=req.source_url,
                content=req.content,
            )
    logger.info(
        "knowledge source created: user=%s workspace=%s id=%s type=%s",
        current.id,
        wid,
        row["id"],
        req.source_type,
    )
    return _source_row_to_out(row, linked_count=0)


# /knowledge/sources/{id} routes (workspace-agnostic) live on a separate
# router defined below as `sources_router`.


sources_router = APIRouter(prefix="/knowledge/sources", tags=["knowledge"])


@sources_router.get(
    "/{source_id}",
    response_model=KnowledgeSourceDetailOut,
    summary="Get a knowledge source with content + linked memories.",
)
async def get_source_endpoint(
    source_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> KnowledgeSourceDetailOut:
    sid = _parse_uuid(source_id, "source_id")
    pool = _require_pool()
    async with pool.acquire() as conn:
        src = await conn.fetchrow(
            """
            SELECT id, workspace_id, uploaded_by, source_type, title, description,
                   original_filename, source_url, content, content_hash, status,
                   metadata, created_at, updated_at
            FROM knowledge_sources WHERE id = $1
            """,
            sid,
        )
        if src is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="source not found"
            )
        linked = await conn.fetch(
            """
            SELECT id, workspace_id, type, title, content, tags,
                   importance, scope_type, scope_id, embedded_at,
                   source_id, created_at, updated_at
            FROM memory_entries
            WHERE source_id = $1
              AND (
                      scope_type IN ('global','system')
                      OR (scope_type = 'user' AND scope_id = $2)
                  )
            ORDER BY created_at DESC
            """,
            sid,
            current.id,
        )
    src_dict = dict(src)
    base = _source_row_to_out(src_dict, linked_count=len(linked))
    return KnowledgeSourceDetailOut(
        **base.model_dump(),
        content=src_dict["content"],
        linked_memories=[_knowledge_row_to_out(dict(r)) for r in linked],
    )


@sources_router.patch(
    "/{source_id}",
    response_model=KnowledgeSourceOut,
    summary="Update a knowledge source's title/description/url/status.",
)
async def patch_source_endpoint(
    source_id: str,
    req: UpdateSourceRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> KnowledgeSourceOut:
    sid = _parse_uuid(source_id, "source_id")
    pool = _require_pool()
    sets: list[str] = []
    args: list = []

    def add(col: str, val) -> None:
        args.append(val)
        sets.append(f"{col} = ${len(args)}")

    body = req.model_dump(exclude_unset=True)
    for col in ("title", "description", "source_url", "status"):
        if col in body:
            add(col, body[col])
    if not sets:
        # No-op fetch
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, workspace_id, uploaded_by, source_type, title, description,
                       original_filename, source_url, content_hash, status,
                       created_at, updated_at,
                       (SELECT COUNT(*) FROM memory_entries m WHERE m.source_id = $1) AS linked
                FROM knowledge_sources WHERE id = $1
                """,
                sid,
            )
        if row is None:
            raise HTTPException(404, "source not found")
        return _source_row_to_out(dict(row), linked_count=int(row["linked"] or 0))

    sets.append("updated_at = NOW()")
    args.append(sid)
    sql = (
        f"UPDATE knowledge_sources SET {', '.join(sets)} "
        f"WHERE id = ${len(args)} "
        "RETURNING id, workspace_id, uploaded_by, source_type, title, description, "
        "original_filename, source_url, content_hash, status, created_at, updated_at"
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *args)
        if row is None:
            raise HTTPException(404, "source not found")
        linked = int(
            await conn.fetchval(
                "SELECT COUNT(*) FROM memory_entries WHERE source_id = $1", sid
            )
            or 0
        )
    logger.info(
        "knowledge source updated: user=%s id=%s fields=%s",
        current.id,
        sid,
        list(body.keys()),
    )
    return _source_row_to_out(dict(row), linked_count=linked)


@sources_router.delete(
    "/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a knowledge source. Linked memory entries lose their source_id.",
)
async def delete_source_endpoint(
    source_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> None:
    sid = _parse_uuid(source_id, "source_id")
    pool = _require_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM knowledge_sources WHERE id = $1", sid
        )
    if result.endswith(" 0"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="source not found"
        )
    logger.info("knowledge source deleted: user=%s id=%s", current.id, sid)


class SourceRefreshResponse(BaseModel):
    status: Literal["unchanged", "updated"]
    source_id: str
    url: str
    content_chars: int
    old_hash: Optional[str] = None
    new_hash: Optional[str] = None
    title: Optional[str] = None
    linked_updated: int = 0
    embedded: int = 0


@sources_router.post(
    "/{source_id}/refresh",
    response_model=SourceRefreshResponse,
    summary="Re-fetch a url source; update content + linked memory if it changed.",
)
async def refresh_source_endpoint(
    source_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> SourceRefreshResponse:
    sid = _parse_uuid(source_id, "source_id")
    pool = _require_pool()
    async with pool.acquire() as conn:
        src = await conn.fetchrow(
            """
            SELECT id, workspace_id, source_type, source_url, title,
                   content_hash, metadata
            FROM knowledge_sources WHERE id = $1
            """,
            sid,
        )
    if src is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="source not found"
        )
    if src["source_type"] != "url" or not src["source_url"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="only url sources with a source_url can be refreshed",
        )

    metadata = _as_dict(src["metadata"]) or {}
    now_iso = datetime.now(timezone.utc).isoformat()
    src_url = src["source_url"]

    # Fetch + extract. On failure: record last_error/last_checked_at, keep
    # existing content, return a clean error.
    try:
        norm_url = normalize_url(src_url)
        result = await fetch_and_extract(norm_url)
    except UrlIngestError as exc:
        metadata["last_checked_at"] = now_iso
        metadata["last_error"] = str(exc)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE knowledge_sources SET metadata = $2, updated_at = NOW() "
                "WHERE id = $1",
                sid,
                metadata,
            )
        await write_trace(
            session_id=None,
            user_id=current.id,
            trace_type="url_refresh",
            status="error",
            selected_agent="ATLAS",
            tool_name="url_refresh",
            tool_result={
                "source_id": str(sid),
                "url": src_url,
                "status": "failed",
                "error": str(exc),
                "code": exc.code,
            },
            workspace_id=src["workspace_id"],
            error_message=str(exc),
        )
        raise HTTPException(
            status_code=_URL_ERROR_STATUS.get(
                exc.code, status.HTTP_400_BAD_REQUEST
            ),
            detail=str(exc),
        ) from exc

    new_content = result["content"]
    new_hash = _content_hash(new_content)
    old_hash = src["content_hash"]

    metadata["last_checked_at"] = now_iso
    metadata["status_code"] = result["status_code"]
    metadata["content_type"] = result["content_type"]
    metadata["extraction_method"] = result["extraction_method"]
    metadata["page_count"] = result["page_count"]
    metadata.pop("last_error", None)

    # Unchanged — only bump last_checked_at; no new source/memory rows.
    if new_hash == old_hash:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE knowledge_sources SET metadata = $2, updated_at = NOW() "
                "WHERE id = $1",
                sid,
                metadata,
            )
        await write_trace(
            session_id=None,
            user_id=current.id,
            trace_type="url_refresh",
            status="ok",
            selected_agent="ATLAS",
            tool_name="url_refresh",
            tool_result={
                "source_id": str(sid),
                "url": src_url,
                "status": "unchanged",
                "old_hash": old_hash,
                "new_hash": new_hash,
                "content_chars": len(new_content),
            },
            workspace_id=src["workspace_id"],
        )
        logger.info("url source refresh: id=%s status=unchanged", sid)
        return SourceRefreshResponse(
            status="unchanged",
            source_id=str(sid),
            url=src_url,
            content_chars=len(new_content),
            old_hash=old_hash,
            new_hash=new_hash,
            title=src["title"],
        )

    # Changed — update source + linked memory; clear stale vectors then re-embed.
    metadata["previous_content_hash"] = old_hash
    metadata["last_changed_at"] = now_iso

    new_title = (result["title"] or "").strip()
    title_source = metadata.get("title_source", "auto")
    if title_source == "auto" and new_title and new_title != src["title"]:
        final_title = new_title[:300]
    else:
        final_title = src["title"]

    embed_col = (
        "embedding" if schema_state.is_pgvector_available() else "embedding_json"
    )
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE knowledge_sources
                SET content = $2, content_hash = $3, title = $4,
                    metadata = $5, updated_at = NOW()
                WHERE id = $1
                """,
                sid,
                new_content,
                new_hash,
                final_title,
                metadata,
            )
            mem_rows = await conn.fetch(
                f"""
                UPDATE memory_entries
                SET content = $2, title = $3,
                    {embed_col} = NULL, embedded_at = NULL, updated_at = NOW()
                WHERE source_id = $1
                RETURNING id
                """,
                sid,
                new_content,
                final_title,
            )
            # Drop stale chunks for changed content; re-embed below rebuilds them.
            for m in mem_rows:
                await conn.execute(
                    "DELETE FROM memory_entry_chunks WHERE memory_entry_id = $1",
                    m["id"],
                )

    # Immediate re-embed (matches the ingest auto-embed pattern). If embedding
    # fails the vector simply stays NULL — never stale; chunks rebuilt on embed.
    embedded = 0
    for m in mem_rows:
        try:
            res = await embed_memory_entry(m["id"])
            if res.get("status") == "ok":
                embedded += 1
        except Exception:
            logger.exception("refresh re-embed failed: memory=%s", m["id"])

    await write_trace(
        session_id=None,
        user_id=current.id,
        trace_type="url_refresh",
        status="ok",
        selected_agent="ATLAS",
        tool_name="url_refresh",
        tool_result={
            "source_id": str(sid),
            "url": src_url,
            "status": "updated",
            "old_hash": old_hash,
            "new_hash": new_hash,
            "content_chars": len(new_content),
            "linked_updated": len(mem_rows),
            "embedded": embedded,
            "title": final_title,
        },
        workspace_id=src["workspace_id"],
    )
    logger.info(
        "url source refresh: id=%s status=updated linked=%s embedded=%s",
        sid,
        len(mem_rows),
        embedded,
    )
    return SourceRefreshResponse(
        status="updated",
        source_id=str(sid),
        url=src_url,
        content_chars=len(new_content),
        old_hash=old_hash,
        new_hash=new_hash,
        title=final_title,
        linked_updated=len(mem_rows),
        embedded=embedded,
    )


# ---------- Managed news feed settings + manual refresh (sources_router) ----------


@sources_router.patch(
    "/{source_id}/news-feed",
    response_model=NewsFeedOut,
    summary="Update a registered news feed's settings (owner/admin).",
)
async def update_news_feed_endpoint(
    source_id: str,
    req: UpdateNewsFeedRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> NewsFeedOut:
    sid = _parse_uuid(source_id, "source_id")
    if req.scope_type in ("global", "system") and current.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"admin role required for {req.scope_type} scope",
        )
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_FEED_COLS} FROM knowledge_sources "
            "WHERE id=$1 AND source_type='news_feed'",
            sid,
        )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="news feed not found"
            )
        meta = _as_dict(row["metadata"]) or {}
        body = req.model_dump(exclude_unset=True)
        # Map source_name → title later; merge the rest into metadata.
        for key in (
            "source_name", "max_items", "scope_type", "importance",
            "auto_embed", "fetch_article_body", "refresh_enabled",
            "refresh_interval_minutes",
        ):
            if key in body and body[key] is not None:
                meta[key] = body[key]
        # Recompute next_refresh_at when scheduling settings change.
        interval = meta.get("refresh_interval_minutes")
        if meta.get("refresh_enabled") and interval and int(interval) > 0:
            if not meta.get("next_refresh_at"):
                meta["next_refresh_at"] = datetime.now(timezone.utc).isoformat()
        else:
            meta["next_refresh_at"] = None
        new_title = body.get("source_name") or row["title"]
        await conn.execute(
            "UPDATE knowledge_sources SET title=$2, metadata=$3, updated_at=NOW() "
            "WHERE id=$1",
            sid, new_title, meta,
        )
        updated = await conn.fetchrow(
            f"SELECT {_FEED_COLS} FROM knowledge_sources WHERE id=$1", sid
        )
    await write_trace(
        session_id=None, user_id=current.id, trace_type="news_feed_updated",
        status="ok", selected_agent="ATLAS", tool_name="news_feed_update",
        tool_result={"source_id": str(sid), "fields": list(body.keys())},
        workspace_id=updated["workspace_id"],
    )
    return _feed_row_to_out(updated)


@sources_router.post(
    "/{source_id}/news-refresh",
    response_model=NewsFeedRefreshResponse,
    summary="Manually refresh a registered news feed now (owner/admin).",
)
async def manual_news_refresh_endpoint(
    source_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> NewsFeedRefreshResponse:
    sid = _parse_uuid(source_id, "source_id")
    await write_trace(
        session_id=None, user_id=current.id,
        trace_type="news_feed_refresh_requested", status="ok",
        selected_agent="ATLAS", tool_name="news_feed_refresh",
        tool_result={"source_id": str(sid), "trigger": "manual"},
    )
    try:
        result = await refresh_feed_source(sid, user_id=current.id)
    except NewsIngestError as exc:
        raise HTTPException(
            status_code=_NEWS_ERROR_STATUS.get(
                exc.code,
                status.HTTP_404_NOT_FOUND
                if exc.code == "not_found"
                else status.HTTP_400_BAD_REQUEST,
            ),
            detail=str(exc),
        ) from exc
    base = _news_result_to_response(result)
    return NewsFeedRefreshResponse(
        **base.model_dump(), next_refresh_at=result.get("next_refresh_at")
    )
