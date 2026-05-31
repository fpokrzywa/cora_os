import json
import logging
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Optional

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.agents.registry import load_active_routing_keywords
from app.agents.routing import diagnose_routing
from app.agents.scribe import search_memory
from app.auth import CurrentUser, require_admin
from app.clients import clients
from app.clock import current_datetime_preamble
from app.config import settings
from app.routers.chat import (
    PERSONA_NAME,
    ORCHESTRATOR_NAME,
    resolve_agent_prompt,
)
from app.runtime_traces import write_trace
from app.workspaces import get_chat_context

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/agents", tags=["admin", "agents"])


# ---------- Models ----------


class AgentVersionOut(BaseModel):
    id: str
    agent_id: str
    version_number: int
    status: Literal["draft", "active", "archived"]
    system_prompt: str
    routing_keywords: list[str]
    allowed_tools: list[str]
    model_name: Optional[str]
    temperature: float
    max_prompt_chars: int
    notes: Optional[str]
    metadata: dict = Field(default_factory=dict)
    created_by: Optional[str]
    created_at: datetime
    activated_at: Optional[datetime]
    archived_at: Optional[datetime]


class AgentOut(BaseModel):
    id: str
    name: str
    display_name: str
    description: Optional[str]
    agent_type: Literal["orchestrator", "subagent", "memory", "tool_agent"]
    enabled: bool
    current_version_id: Optional[str]
    current_version_number: Optional[int]
    created_at: datetime
    updated_at: datetime


class AgentDetailOut(AgentOut):
    versions: list[AgentVersionOut]


class CreateAgentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    display_name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = None
    agent_type: Literal["orchestrator", "subagent", "memory", "tool_agent"]
    enabled: bool = True


class CreateVersionRequest(BaseModel):
    system_prompt: str = Field(min_length=1)
    routing_keywords: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    model_name: Optional[str] = None
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_prompt_chars: int = Field(default=16000, ge=500, le=200000)
    notes: Optional[str] = None
    metadata: Optional[dict] = None
    activate: bool = False


class UpdateAgentRequest(BaseModel):
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = None
    enabled: Optional[bool] = None


class TestRoutingRequest(BaseModel):
    message: str = Field(min_length=1)
    workspace_id: Optional[str] = None
    include_prompt_preview: bool = True


class TestRoutingResponse(BaseModel):
    selected_agent: str
    scores: dict[str, int]
    matched_keywords: dict[str, list[str]]
    tie_break_applied: bool
    prompt_source: str
    active_version: Optional[int]
    prompt_preview: Optional[str] = None
    would_delegate: bool
    delegation_from: Optional[str] = None
    delegation_to: Optional[str] = None


class TestResponseRequest(BaseModel):
    message: str = Field(min_length=1)
    workspace_id: Optional[str] = None
    agent_name: Optional[str] = None
    include_memory: bool = False


class TestResponseResponse(BaseModel):
    selected_agent: str
    prompt_source: str
    active_version: Optional[int]
    response: str
    test_run: bool = True


# ---------- Helpers ----------


def _require_pool():
    if clients.db_pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Postgres pool unavailable",
        )
    return clients.db_pool


def _as_dict(value) -> dict:
    """asyncpg returns JSONB as dict (codec registered) or str; normalize."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


# Columns selected for every version row (keep in sync across queries).
_VERSION_COLS = (
    "id, agent_id, version_number, status, system_prompt, routing_keywords, "
    "allowed_tools, model_name, temperature, max_prompt_chars, notes, metadata, "
    "created_by, created_at, activated_at, archived_at"
)


def _version_row_to_out(row) -> AgentVersionOut:
    temp = row["temperature"]
    if isinstance(temp, Decimal):
        temp = float(temp)
    return AgentVersionOut(
        id=str(row["id"]),
        agent_id=str(row["agent_id"]),
        version_number=row["version_number"],
        status=row["status"],
        system_prompt=row["system_prompt"],
        routing_keywords=list(row["routing_keywords"] or []),
        allowed_tools=list(row["allowed_tools"] or []),
        model_name=row["model_name"],
        temperature=temp,
        max_prompt_chars=row["max_prompt_chars"],
        notes=row["notes"],
        metadata=_as_dict(row["metadata"]),
        created_by=str(row["created_by"]) if row["created_by"] else None,
        created_at=row["created_at"],
        activated_at=row["activated_at"],
        archived_at=row["archived_at"],
    )


def _agent_row_to_out(row, current_version_number: Optional[int] = None) -> AgentOut:
    return AgentOut(
        id=str(row["id"]),
        name=row["name"],
        display_name=row["display_name"],
        description=row["description"],
        agent_type=row["agent_type"],
        enabled=row["enabled"],
        current_version_id=str(row["current_version_id"])
        if row["current_version_id"]
        else None,
        current_version_number=current_version_number,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def _fetch_agent_by_name(conn, agent_name: str):
    return await conn.fetchrow(
        """
        SELECT a.id, a.name, a.display_name, a.description, a.agent_type,
               a.enabled, a.current_version_id, a.created_at, a.updated_at,
               cv.version_number AS current_version_number
        FROM agents a
        LEFT JOIN agent_versions cv ON cv.id = a.current_version_id
        WHERE a.name = $1
        """,
        agent_name,
    )


# ---------- Endpoints ----------


@router.get(
    "",
    response_model=list[AgentOut],
    summary="List all agents (admin only)",
)
async def list_agents(
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> list[AgentOut]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.id, a.name, a.display_name, a.description, a.agent_type,
                   a.enabled, a.current_version_id, a.created_at, a.updated_at,
                   cv.version_number AS current_version_number
            FROM agents a
            LEFT JOIN agent_versions cv ON cv.id = a.current_version_id
            ORDER BY a.name ASC
            """
        )
    logger.info("admin list agents: admin=%s count=%s", admin.id, len(rows))
    return [
        _agent_row_to_out(r, current_version_number=r["current_version_number"])
        for r in rows
    ]


@router.get(
    "/{agent_name}",
    response_model=AgentDetailOut,
    summary="Get an agent with full version history (admin only)",
)
async def get_agent(
    agent_name: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> AgentDetailOut:
    pool = _require_pool()
    async with pool.acquire() as conn:
        agent_row = await _fetch_agent_by_name(conn, agent_name)
        if agent_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"agent {agent_name!r} not found",
            )
        version_rows = await conn.fetch(
            f"""
            SELECT {_VERSION_COLS}
            FROM agent_versions
            WHERE agent_id = $1
            ORDER BY version_number DESC
            """,
            agent_row["id"],
        )
    agent = _agent_row_to_out(
        agent_row, current_version_number=agent_row["current_version_number"]
    )
    return AgentDetailOut(
        **agent.model_dump(),
        versions=[_version_row_to_out(r) for r in version_rows],
    )


@router.get(
    "/{agent_name}/versions",
    response_model=list[AgentVersionOut],
    summary="List an agent's version history, newest first (admin only)",
)
async def list_agent_versions(
    agent_name: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> list[AgentVersionOut]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        agent_row = await _fetch_agent_by_name(conn, agent_name)
        if agent_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"agent {agent_name!r} not found",
            )
        rows = await conn.fetch(
            f"SELECT {_VERSION_COLS} FROM agent_versions "
            "WHERE agent_id = $1 ORDER BY version_number DESC",
            agent_row["id"],
        )
    return [_version_row_to_out(r) for r in rows]


@router.patch(
    "/{agent_name}",
    response_model=AgentOut,
    summary="Update an agent's display fields / enabled status (admin only)",
)
async def update_agent(
    agent_name: str,
    req: UpdateAgentRequest,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> AgentOut:
    pool = _require_pool()
    sets: list[str] = []
    args: list = []
    body = req.model_dump(exclude_unset=True)
    for col in ("display_name", "description", "enabled"):
        if col in body:
            args.append(body[col])
            sets.append(f"{col} = ${len(args)}")
    if not sets:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="nothing to update",
        )
    sets.append("updated_at = NOW()")
    args.append(agent_name)
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE agents SET {', '.join(sets)} WHERE name = ${len(args)}",
            *args,
        )
        agent_row = await _fetch_agent_by_name(conn, agent_name)
    if agent_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"agent {agent_name!r} not found",
        )
    logger.info(
        "admin updated agent: admin=%s agent=%s fields=%s",
        admin.id, agent_name, list(body.keys()),
    )
    return _agent_row_to_out(
        agent_row, current_version_number=agent_row["current_version_number"]
    )


@router.post(
    "",
    response_model=AgentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new agent (admin only). Versions are created separately.",
)
async def create_agent(
    req: CreateAgentRequest,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> AgentOut:
    pool = _require_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO agents (name, display_name, description, agent_type, enabled)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, name, display_name, description, agent_type,
                          enabled, current_version_id, created_at, updated_at
                """,
                req.name,
                req.display_name,
                req.description,
                req.agent_type,
                req.enabled,
            )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"agent name {req.name!r} already exists",
        ) from exc
    logger.info(
        "admin created agent: admin=%s name=%s type=%s",
        admin.id,
        req.name,
        req.agent_type,
    )
    return _agent_row_to_out(row, current_version_number=None)


@router.post(
    "/{agent_name}/versions",
    response_model=AgentVersionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new draft version for an agent (admin only). Auto-bumps version_number.",
)
async def create_version(
    agent_name: str,
    req: CreateVersionRequest,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> AgentVersionOut:
    pool = _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            agent_row = await _fetch_agent_by_name(conn, agent_name)
            if agent_row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"agent {agent_name!r} not found",
                )
            next_version = await conn.fetchval(
                """
                SELECT COALESCE(MAX(version_number), 0) + 1
                FROM agent_versions WHERE agent_id = $1
                """,
                agent_row["id"],
            )
            # Merge metadata; mirror routing_keywords into metadata so the
            # runtime router (which reads metadata.routing_keywords) stays in
            # sync with the column the editor sends.
            metadata = dict(req.metadata or {})
            if "routing_keywords" not in metadata and req.routing_keywords:
                metadata["routing_keywords"] = list(req.routing_keywords)
            new_status = "active" if req.activate else "draft"
            if req.activate:
                # Archive whatever is currently active for this agent.
                await conn.execute(
                    "UPDATE agent_versions SET status='archived', "
                    "archived_at=NOW() WHERE agent_id=$1 AND status='active'",
                    agent_row["id"],
                )
            version_row = await conn.fetchrow(
                f"""
                INSERT INTO agent_versions (
                    agent_id, version_number, status, system_prompt,
                    routing_keywords, allowed_tools, model_name, temperature,
                    max_prompt_chars, notes, metadata, created_by, activated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                        CASE WHEN $3 = 'active' THEN NOW() ELSE NULL END)
                RETURNING {_VERSION_COLS}
                """,
                agent_row["id"],
                next_version,
                new_status,
                req.system_prompt,
                req.routing_keywords,
                req.allowed_tools,
                req.model_name,
                req.temperature,
                req.max_prompt_chars,
                req.notes,
                metadata,
                admin.id,
            )
            if req.activate:
                await conn.execute(
                    "UPDATE agents SET current_version_id=$1, updated_at=NOW() "
                    "WHERE id=$2",
                    version_row["id"],
                    agent_row["id"],
                )
    logger.info(
        "admin created agent version: admin=%s agent=%s version=%s status=%s",
        admin.id,
        agent_name,
        next_version,
        new_status,
    )
    await write_trace(
        session_id=None,
        user_id=admin.id,
        trace_type="agent_version_created",
        status="ok",
        selected_agent="ATLAS",
        tool_name="agent_admin",
        tool_result={
            "agent": agent_name,
            "version": next_version,
            "status": new_status,
            "notes": req.notes,
        },
    )
    if req.activate:
        await write_trace(
            session_id=None,
            user_id=admin.id,
            trace_type="agent_version_activated",
            status="ok",
            selected_agent="ATLAS",
            tool_name="agent_admin",
            tool_result={
                "agent": agent_name,
                "version": next_version,
                "via": "create+activate",
            },
        )
    return _version_row_to_out(version_row)


@router.post(
    "/{agent_name}/versions/{version_id}/activate",
    response_model=AgentVersionOut,
    summary="Activate a draft version. Previous active becomes archived. (admin only)",
)
async def activate_version(
    agent_name: str,
    version_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> AgentVersionOut:
    try:
        version_uuid = uuid.UUID(version_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="version_id must be a valid UUID",
        ) from exc

    pool = _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            agent_row = await _fetch_agent_by_name(conn, agent_name)
            if agent_row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"agent {agent_name!r} not found",
                )
            target = await conn.fetchrow(
                """
                SELECT id, agent_id, status FROM agent_versions
                WHERE id = $1 AND agent_id = $2
                """,
                version_uuid,
                agent_row["id"],
            )
            if target is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="version not found for that agent",
                )
            if target["status"] == "archived":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="cannot activate an archived version",
                )
            if target["status"] == "active":
                # No-op activate — already active
                pass
            else:
                # Archive any currently active version
                await conn.execute(
                    """
                    UPDATE agent_versions
                    SET status = 'archived', archived_at = NOW()
                    WHERE agent_id = $1 AND status = 'active'
                    """,
                    agent_row["id"],
                )
                await conn.execute(
                    """
                    UPDATE agent_versions
                    SET status = 'active', activated_at = NOW(), archived_at = NULL
                    WHERE id = $1
                    """,
                    version_uuid,
                )
            await conn.execute(
                """
                UPDATE agents SET current_version_id = $1, updated_at = NOW()
                WHERE id = $2
                """,
                version_uuid,
                agent_row["id"],
            )
            new_row = await conn.fetchrow(
                f"SELECT {_VERSION_COLS} FROM agent_versions WHERE id = $1",
                version_uuid,
            )
    logger.info(
        "admin activated agent version: admin=%s agent=%s version_id=%s",
        admin.id,
        agent_name,
        version_id,
    )
    await write_trace(
        session_id=None,
        user_id=admin.id,
        trace_type="agent_version_activated",
        status="ok",
        selected_agent="ATLAS",
        tool_name="agent_admin",
        tool_result={
            "agent": agent_name,
            "version": new_row["version_number"],
        },
    )
    return _version_row_to_out(new_row)


@router.post(
    "/{agent_name}/versions/{version_id}/archive",
    response_model=AgentVersionOut,
    summary="Archive an inactive version. The active version cannot be archived. (admin only)",
)
async def archive_version(
    agent_name: str,
    version_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> AgentVersionOut:
    try:
        version_uuid = uuid.UUID(version_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="version_id must be a valid UUID",
        ) from exc

    pool = _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            agent_row = await _fetch_agent_by_name(conn, agent_name)
            if agent_row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"agent {agent_name!r} not found",
                )
            target = await conn.fetchrow(
                """
                SELECT id, status FROM agent_versions
                WHERE id = $1 AND agent_id = $2
                """,
                version_uuid,
                agent_row["id"],
            )
            if target is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="version not found for that agent",
                )
            if target["status"] == "active":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="cannot archive the active version; activate another "
                    "version first",
                )
            if target["status"] == "archived":
                # Already archived — return it as-is (idempotent).
                new_row = await conn.fetchrow(
                    f"SELECT {_VERSION_COLS} FROM agent_versions WHERE id = $1",
                    version_uuid,
                )
                return _version_row_to_out(new_row)
            await conn.execute(
                """
                UPDATE agent_versions
                SET status = 'archived', archived_at = NOW()
                WHERE id = $1
                """,
                version_uuid,
            )
            new_row = await conn.fetchrow(
                f"SELECT {_VERSION_COLS} FROM agent_versions WHERE id = $1",
                version_uuid,
            )
    logger.info(
        "admin archived agent version: admin=%s agent=%s version_id=%s",
        admin.id,
        agent_name,
        version_id,
    )
    await write_trace(
        session_id=None,
        user_id=admin.id,
        trace_type="agent_version_archived",
        status="ok",
        selected_agent="ATLAS",
        tool_name="agent_admin",
        tool_result={
            "agent": agent_name,
            "version": new_row["version_number"],
        },
    )
    return _version_row_to_out(new_row)


# ---------- Test harness (admin only; no live chat side effects) ----------


def _opt_uuid(value: Optional[str], field_name: str) -> Optional[uuid.UUID]:
    if value is None or value == "":
        return None
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must be a valid UUID",
        ) from exc


@router.post(
    "/test-routing",
    response_model=TestRoutingResponse,
    summary="Diagnose how a message would route (read-only; admin only).",
)
async def test_routing(
    req: TestRoutingRequest,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> TestRoutingResponse:
    wid = _opt_uuid(req.workspace_id, "workspace_id")
    overrides = await load_active_routing_keywords()
    diag = diagnose_routing(req.message, overrides)
    selected = diag.selected_agent
    system_prompt, prompt_source, active_version = await resolve_agent_prompt(
        selected
    )
    would_delegate = selected != PERSONA_NAME
    preview = system_prompt[:500] if req.include_prompt_preview else None

    await write_trace(
        session_id=None,
        user_id=admin.id,
        trace_type="agent_test_routing",
        status="ok",
        selected_agent=selected,
        tool_name="agent_test_harness",
        tool_result={
            "message_preview": req.message[:200],
            "selected_agent": selected,
            "scores": diag.scores,
            "matched_keywords": diag.matched_keywords,
            "tie_break_applied": diag.tie_break_applied,
            "prompt_source": prompt_source,
            "active_version": active_version,
        },
        workspace_id=wid,
    )
    return TestRoutingResponse(
        selected_agent=selected,
        scores=diag.scores,
        matched_keywords=diag.matched_keywords,
        tie_break_applied=diag.tie_break_applied,
        prompt_source=prompt_source,
        active_version=active_version,
        prompt_preview=preview,
        would_delegate=would_delegate,
        delegation_from=ORCHESTRATOR_NAME if would_delegate else None,
        delegation_to=selected if would_delegate else None,
    )


@router.post(
    "/test-response",
    response_model=TestResponseResponse,
    summary="Run a one-off agent response (admin only; not saved as a chat).",
)
async def test_response(
    req: TestResponseRequest,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> TestResponseResponse:
    wid = _opt_uuid(req.workspace_id, "workspace_id")
    overrides = await load_active_routing_keywords()

    if req.agent_name:
        selected = req.agent_name
    else:
        selected = diagnose_routing(req.message, overrides).selected_agent

    system_prompt, prompt_source, active_version = await resolve_agent_prompt(
        selected
    )

    # Optional workspace context + memory, mirroring chat's prompt assembly but
    # without history, tools, plans, persistence, or delegations.
    parts: list[str] = [f"System: {current_datetime_preamble()}\n\n{system_prompt}"]
    if wid is not None:
        try:
            ctx = await get_chat_context(wid)
            if ctx and ctx.get("text"):
                parts.append(ctx["text"])
        except Exception:
            logger.exception("test-response workspace context failed wid=%s", wid)
    if req.include_memory:
        try:
            rows = await search_memory(
                req.message, limit=5, user_id=admin.id, workspace_id=wid
            )
            if rows:
                parts.append(
                    "Relevant Cora Memory:\n"
                    + "\n".join(
                        f"- [{r['title']}] {r['content']}" for r in rows
                    )
                )
        except Exception:
            logger.exception("test-response memory search failed")
    parts.append(f"User: {req.message}")
    parts.append(f"{PERSONA_NAME}:")
    prompt = "\n\n".join(parts)

    endpoint = settings.dgx_model_endpoint or ""
    if not endpoint:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DGX_MODEL_ENDPOINT is not configured",
        )
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{endpoint.rstrip('/')}/api/generate",
                json={
                    "model": settings.dgx_model_name,
                    "prompt": prompt,
                    "stream": False,
                },
            )
            resp.raise_for_status()
            text = resp.json().get("response", "")
    except httpx.HTTPError as exc:
        logger.exception("test-response LLM call failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"model request failed: {exc}",
        ) from exc

    await write_trace(
        session_id=None,
        user_id=admin.id,
        trace_type="agent_test_response",
        status="ok",
        selected_agent=selected,
        tool_name="agent_test_harness",
        tool_result={
            "message_preview": req.message[:200],
            "selected_agent": selected,
            "agent_forced": bool(req.agent_name),
            "prompt_source": prompt_source,
            "active_version": active_version,
            "response_chars": len(text),
            "include_memory": req.include_memory,
        },
        workspace_id=wid,
    )
    logger.info(
        "admin test-response: admin=%s selected=%s forced=%s source=%s chars=%s",
        admin.id, selected, bool(req.agent_name), prompt_source, len(text),
    )
    return TestResponseResponse(
        selected_agent=selected,
        prompt_source=prompt_source,
        active_version=active_version,
        response=text,
        test_run=True,
    )
