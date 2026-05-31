import logging
import time
from datetime import datetime
from typing import Annotated, Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.auth import CurrentUser, get_current_user
from app.clients import clients
from app.runtime_traces import write_trace
from app.tools import dispatch_tool, get_runner
from app.tools.governance import check_permission, log_execution_attempt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tools", tags=["tools"])


class ToolOut(BaseModel):
    id: str
    name: str
    description: Optional[str]
    type: str
    endpoint: Optional[str]
    enabled: bool
    requires_confirmation: bool
    created_at: datetime
    updated_at: datetime


class ToolRunRequest(BaseModel):
    session_id: Optional[str] = Field(default=None)
    user_message: Optional[str] = Field(default=None)
    metadata: Optional[dict[str, Any]] = Field(default=None)


class ToolRunResponse(BaseModel):
    tool_name: str
    type: str
    status: str
    http_status: Optional[int]
    response: Any
    duration_ms: int


def _require_pool():
    if clients.db_pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Postgres pool unavailable",
        )
    return clients.db_pool


@router.get("", response_model=list[ToolOut], summary="List all registered tools")
async def list_tools() -> list[ToolOut]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, description, type, endpoint, enabled,
                   requires_confirmation, created_at, updated_at
            FROM tools
            ORDER BY name ASC
            """
        )
    return [
        ToolOut(
            id=str(r["id"]),
            name=r["name"],
            description=r["description"],
            type=r["type"],
            endpoint=r["endpoint"],
            enabled=r["enabled"],
            requires_confirmation=r["requires_confirmation"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


@router.post(
    "/{tool_name}/run",
    response_model=ToolRunResponse,
    summary="Manually execute a registered tool (API-triggered, not LLM-triggered)",
)
async def run_tool(
    tool_name: str,
    payload: ToolRunRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> ToolRunResponse:
    import uuid as _uuid

    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, name, description, type, endpoint, enabled,
                   requires_confirmation, mcp_server_name, mcp_action_name,
                   risk_level, allowed_agents
            FROM tools
            WHERE name = $1
            """,
            tool_name,
        )

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"tool {tool_name!r} not found",
        )
    tool = dict(row)

    if get_runner(tool["type"]) is None:
        logger.error(
            "tool run rejected: tool=%s type=%s reason=no_runner",
            tool_name,
            tool["type"],
        )
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"no runner registered for tool type {tool['type']!r}",
        )

    session_uuid: Optional[_uuid.UUID] = None
    if payload.session_id:
        try:
            session_uuid = _uuid.UUID(payload.session_id)
        except ValueError:
            session_uuid = None

    # Manual user-initiated run — no agent context. Governance still enforces
    # enabled + risk_level catch-alls.
    is_admin = current.role == "admin"
    decision = await check_permission(
        tool, agent_name=None, user_id=current.id, is_admin=is_admin
    )
    if not decision.allowed:
        await log_execution_attempt(
            tool_name=tool_name,
            agent_name=None,
            session_id=session_uuid,
            user_id=current.id,
            scope_type="user",
            allowed=False,
            duration_ms=None,
            status="denied",
            error_message=decision.reason,
        )
        await write_trace(
            session_id=session_uuid,
            user_id=current.id,
            trace_type="manual_tool",
            status="denied",
            tool_name=tool_name,
            mcp_server_name=tool.get("mcp_server_name"),
            mcp_action_name=tool.get("mcp_action_name"),
            error_message=decision.reason,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "denied",
                "reason": decision.reason,
                "policy_source": decision.policy_source,
                "matched_rule": decision.matched_rule,
            },
        )

    logger.info(
        "tool run requested: tool=%s type=%s user_id=%s session_id=%s "
        "policy_source=%s matched_rule=%s requires_confirmation=%s",
        tool_name,
        tool["type"],
        current.id,
        payload.session_id,
        decision.policy_source,
        decision.matched_rule,
        decision.requires_confirmation,
    )

    started = time.perf_counter()
    try:
        result = await dispatch_tool(tool, payload.model_dump())
    except httpx.HTTPError as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.exception("tool run network failure: tool=%s", tool_name)
        await log_execution_attempt(
            tool_name=tool_name,
            agent_name=None,
            session_id=session_uuid,
            user_id=current.id,
            scope_type="user",
            allowed=True,
            duration_ms=duration_ms,
            status="error",
            error_message=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"tool {tool_name!r} webhook call failed: {exc}",
        ) from exc
    except ValueError as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.exception("tool run misconfigured: tool=%s", tool_name)
        await log_execution_attempt(
            tool_name=tool_name,
            agent_name=None,
            session_id=session_uuid,
            user_id=current.id,
            scope_type="user",
            allowed=True,
            duration_ms=duration_ms,
            status="error",
            error_message=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    duration_ms = result.get("duration_ms") or int((time.perf_counter() - started) * 1000)
    status_value = str(result.get("status", "unknown"))
    await log_execution_attempt(
        tool_name=tool_name,
        agent_name=None,
        session_id=session_uuid,
        user_id=current.id,
        scope_type="user",
        allowed=True,
        duration_ms=duration_ms,
        status=status_value,
        error_message=result.get("error") if status_value != "ok" else None,
    )
    await write_trace(
        session_id=session_uuid,
        user_id=current.id,
        trace_type="manual_tool",
        status=status_value,
        tool_name=tool_name,
        tool_result=result,
        mcp_server_name=tool.get("mcp_server_name"),
        mcp_action_name=tool.get("mcp_action_name"),
        duration_ms=duration_ms,
        error_message=result.get("error") if status_value != "ok" else None,
    )
    return ToolRunResponse(
        tool_name=tool_name,
        type=tool["type"],
        status=status_value,
        http_status=result.get("http_status"),
        response=result.get("response"),
        duration_ms=duration_ms,
    )
