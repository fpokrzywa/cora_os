import logging
from datetime import datetime
from typing import Annotated, Any, Literal, Optional

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

import time
import uuid as _uuid

from app.auth import CurrentUser, require_admin
from app.clients import clients
from app.tools import dispatch_tool, get_runner
from app.tools.governance import check_permission, log_execution_attempt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/tools", tags=["admin", "tools"])


# ---------- Models ----------


class ToolAdminOut(BaseModel):
    id: str
    name: str
    description: Optional[str]
    type: str
    endpoint: Optional[str]
    enabled: bool
    requires_confirmation: bool
    mcp_server_name: Optional[str]
    mcp_action_name: Optional[str]
    input_schema: Optional[dict]
    output_schema: Optional[dict]
    risk_level: Literal["low", "medium", "high"]
    allowed_agents: list[str]
    created_at: datetime
    updated_at: datetime


class CreateToolRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    description: Optional[str] = None
    type: str = Field(min_length=1, max_length=40)
    endpoint: Optional[str] = None
    enabled: bool = True
    requires_confirmation: bool = False
    mcp_server_name: Optional[str] = None
    mcp_action_name: Optional[str] = None
    input_schema: Optional[dict] = None
    output_schema: Optional[dict] = None
    risk_level: Literal["low", "medium", "high"] = "low"
    allowed_agents: list[str] = Field(default_factory=list)


class UpdateToolRequest(BaseModel):
    description: Optional[str] = None
    endpoint: Optional[str] = None
    enabled: Optional[bool] = None
    requires_confirmation: Optional[bool] = None
    mcp_server_name: Optional[str] = None
    mcp_action_name: Optional[str] = None
    input_schema: Optional[dict] = None
    output_schema: Optional[dict] = None
    risk_level: Optional[Literal["low", "medium", "high"]] = None
    allowed_agents: Optional[list[str]] = None


class TestToolRequest(BaseModel):
    session_id: Optional[str] = None
    user_message: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class TestToolResponse(BaseModel):
    tool_name: str
    type: str
    status: str
    duration_ms: Optional[int] = None
    mcp_server: Optional[str] = None
    mcp_action: Optional[str] = None
    http_status: Optional[int] = None
    response: Any = None
    error: Optional[str] = None


# ---------- Helpers ----------


def _require_pool():
    if clients.db_pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Postgres pool unavailable",
        )
    return clients.db_pool


def _row_to_out(row) -> ToolAdminOut:
    return ToolAdminOut(
        id=str(row["id"]),
        name=row["name"],
        description=row["description"],
        type=row["type"],
        endpoint=row["endpoint"],
        enabled=row["enabled"],
        requires_confirmation=row["requires_confirmation"],
        mcp_server_name=row["mcp_server_name"],
        mcp_action_name=row["mcp_action_name"],
        input_schema=row["input_schema"],
        output_schema=row["output_schema"],
        risk_level=row["risk_level"],
        allowed_agents=list(row["allowed_agents"] or []),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


_SELECT_COLS = """
    id, name, description, type, endpoint, enabled, requires_confirmation,
    mcp_server_name, mcp_action_name, input_schema, output_schema,
    risk_level, allowed_agents, created_at, updated_at
"""


# ---------- Endpoints ----------


@router.get(
    "",
    response_model=list[ToolAdminOut],
    summary="List all tools with admin metadata (admin only).",
)
async def list_tools_admin(
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> list[ToolAdminOut]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_SELECT_COLS} FROM tools ORDER BY name ASC"
        )
    logger.info("admin list tools: admin=%s count=%s", admin.id, len(rows))
    return [_row_to_out(r) for r in rows]


@router.post(
    "",
    response_model=ToolAdminOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new tool (admin only).",
)
async def create_tool_admin(
    req: CreateToolRequest,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> ToolAdminOut:
    pool = _require_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO tools (
                    name, description, type, endpoint, enabled,
                    requires_confirmation, mcp_server_name, mcp_action_name,
                    input_schema, output_schema, risk_level, allowed_agents
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                RETURNING {_SELECT_COLS}
                """,
                req.name,
                req.description,
                req.type,
                req.endpoint,
                req.enabled,
                req.requires_confirmation,
                req.mcp_server_name,
                req.mcp_action_name,
                req.input_schema,
                req.output_schema,
                req.risk_level,
                req.allowed_agents,
            )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"tool {req.name!r} already exists",
        ) from exc
    logger.info(
        "admin created tool: admin=%s name=%s type=%s risk=%s",
        admin.id,
        req.name,
        req.type,
        req.risk_level,
    )
    return _row_to_out(row)


@router.patch(
    "/{tool_name}",
    response_model=ToolAdminOut,
    summary="Update tool fields (admin only). Only supplied fields are written.",
)
async def patch_tool_admin(
    tool_name: str,
    req: UpdateToolRequest,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> ToolAdminOut:
    pool = _require_pool()
    sets: list[str] = []
    args: list[Any] = []

    def add(col: str, val: Any) -> None:
        args.append(val)
        sets.append(f"{col} = ${len(args)}")

    body = req.model_dump(exclude_unset=True)
    for col in (
        "description",
        "endpoint",
        "enabled",
        "requires_confirmation",
        "mcp_server_name",
        "mcp_action_name",
        "input_schema",
        "output_schema",
        "risk_level",
        "allowed_agents",
    ):
        if col in body:
            add(col, body[col])
    if not sets:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_SELECT_COLS} FROM tools WHERE name = $1",
                tool_name,
            )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"tool {tool_name!r} not found",
            )
        return _row_to_out(row)

    sets.append("updated_at = NOW()")
    args.append(tool_name)
    sql = f"""
        UPDATE tools SET {", ".join(sets)}
        WHERE name = ${len(args)}
        RETURNING {_SELECT_COLS}
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *args)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"tool {tool_name!r} not found",
        )
    logger.info(
        "admin updated tool: admin=%s name=%s fields=%s",
        admin.id,
        tool_name,
        list(body.keys()),
    )
    return _row_to_out(row)


@router.post(
    "/{tool_name}/test",
    response_model=TestToolResponse,
    summary="Admin-only test dispatch. Ignores requires_confirmation; still respects enabled.",
)
async def test_tool_admin(
    tool_name: str,
    payload: TestToolRequest,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> TestToolResponse:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_SELECT_COLS} FROM tools WHERE name = $1",
            tool_name,
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"tool {tool_name!r} not found",
        )
    if not row["enabled"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"tool {tool_name!r} is disabled",
        )
    tool_dict = dict(row)
    runner = get_runner(tool_dict["type"])
    if runner is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"no runner registered for tool type {tool_dict['type']!r}",
        )

    logger.info(
        "admin test tool: admin=%s tool=%s type=%s mcp_server=%s mcp_action=%s",
        admin.id,
        tool_name,
        tool_dict["type"],
        tool_dict.get("mcp_server_name"),
        tool_dict.get("mcp_action_name"),
    )

    session_uuid = None
    if payload.session_id:
        try:
            session_uuid = _uuid.UUID(payload.session_id)
        except ValueError:
            session_uuid = None

    decision = await check_permission(
        tool_dict, agent_name=None, user_id=admin.id, is_admin=True
    )
    if not decision.allowed:
        await log_execution_attempt(
            tool_name=tool_name,
            agent_name=None,
            session_id=session_uuid,
            user_id=admin.id,
            scope_type="admin",
            allowed=False,
            duration_ms=None,
            status="denied",
            error_message=decision.reason,
        )
        return TestToolResponse(
            tool_name=tool_name,
            type=tool_dict["type"],
            status="denied",
            error=f"{decision.reason} (source={decision.policy_source}, rule={decision.matched_rule})",
        )

    started = time.perf_counter()
    try:
        result = await dispatch_tool(tool_dict, payload.model_dump())
    except httpx.HTTPError as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.exception("admin test tool network failure: tool=%s", tool_name)
        await log_execution_attempt(
            tool_name=tool_name,
            agent_name=None,
            session_id=session_uuid,
            user_id=admin.id,
            scope_type="admin",
            allowed=True,
            duration_ms=duration_ms,
            status="error",
            error_message=str(exc),
        )
        return TestToolResponse(
            tool_name=tool_name,
            type=tool_dict["type"],
            status="error",
            duration_ms=duration_ms,
            error=str(exc),
        )
    except ValueError as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.exception("admin test tool misconfigured: tool=%s", tool_name)
        await log_execution_attempt(
            tool_name=tool_name,
            agent_name=None,
            session_id=session_uuid,
            user_id=admin.id,
            scope_type="admin",
            allowed=True,
            duration_ms=duration_ms,
            status="error",
            error_message=str(exc),
        )
        return TestToolResponse(
            tool_name=tool_name,
            type=tool_dict["type"],
            status="error",
            duration_ms=duration_ms,
            error=str(exc),
        )

    duration_ms = result.get("duration_ms") or int((time.perf_counter() - started) * 1000)
    status_value = str(result.get("status", "unknown"))
    await log_execution_attempt(
        tool_name=tool_name,
        agent_name=None,
        session_id=session_uuid,
        user_id=admin.id,
        scope_type="admin",
        allowed=True,
        duration_ms=duration_ms,
        status=status_value,
        error_message=result.get("error") if status_value != "ok" else None,
    )
    return TestToolResponse(
        tool_name=tool_name,
        type=tool_dict["type"],
        status=status_value,
        duration_ms=duration_ms,
        mcp_server=result.get("mcp_server"),
        mcp_action=result.get("mcp_action"),
        http_status=result.get("http_status"),
        response=result.get("response"),
        error=result.get("error"),
    )
