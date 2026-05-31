import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.auth import CurrentUser, require_admin
from app.clients import clients

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/governance", tags=["admin", "governance"])


# ---------- Models ----------


class PolicyOut(BaseModel):
    id: str
    tool_name: str
    agent_name: str
    allowed: bool
    requires_confirmation: bool
    max_calls_per_hour: Optional[int]
    created_at: datetime
    updated_at: datetime


class UpsertPolicyRequest(BaseModel):
    tool_name: str = Field(min_length=1, max_length=80)
    agent_name: str = Field(min_length=1, max_length=80)
    allowed: bool = True
    requires_confirmation: bool = False
    max_calls_per_hour: Optional[int] = Field(default=None, ge=1, le=100000)


class ExecutionLogOut(BaseModel):
    id: str
    session_id: Optional[str]
    user_id: Optional[str]
    tool_name: str
    agent_name: Optional[str]
    scope_type: Optional[str]
    allowed: bool
    duration_ms: Optional[int]
    status: str
    error_message: Optional[str]
    created_at: datetime


class ToolStat(BaseModel):
    tool_name: str
    allowed_count: int
    denied_count: int
    error_count: int
    last_used_at: Optional[datetime]


class GovernanceStats(BaseModel):
    window_hours: int
    tools: list[ToolStat]


# ---------- Helpers ----------


def _require_pool():
    if clients.db_pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Postgres pool unavailable",
        )
    return clients.db_pool


def _policy_row_to_out(row) -> PolicyOut:
    return PolicyOut(
        id=str(row["id"]),
        tool_name=row["tool_name"],
        agent_name=row["agent_name"],
        allowed=row["allowed"],
        requires_confirmation=row["requires_confirmation"],
        max_calls_per_hour=row["max_calls_per_hour"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _log_row_to_out(row) -> ExecutionLogOut:
    return ExecutionLogOut(
        id=str(row["id"]),
        session_id=str(row["session_id"]) if row["session_id"] else None,
        user_id=str(row["user_id"]) if row["user_id"] else None,
        tool_name=row["tool_name"],
        agent_name=row["agent_name"],
        scope_type=row["scope_type"],
        allowed=row["allowed"],
        duration_ms=row["duration_ms"],
        status=row["status"],
        error_message=row["error_message"],
        created_at=row["created_at"],
    )


# ---------- Endpoints ----------


@router.get(
    "/policies",
    response_model=list[PolicyOut],
    summary="List all tool execution policies (admin only).",
)
async def list_policies(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    tool_name: Optional[str] = Query(default=None),
    agent_name: Optional[str] = Query(default=None),
) -> list[PolicyOut]:
    pool = _require_pool()
    sql = """
        SELECT id, tool_name, agent_name, allowed, requires_confirmation,
               max_calls_per_hour, created_at, updated_at
        FROM tool_execution_policies
    """
    where: list[str] = []
    args: list = []
    if tool_name:
        args.append(tool_name)
        where.append(f"tool_name = ${len(args)}")
    if agent_name:
        args.append(agent_name)
        where.append(f"agent_name = ${len(args)}")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY tool_name ASC, agent_name ASC"
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    logger.info(
        "admin list policies: admin=%s count=%s", admin.id, len(rows)
    )
    return [_policy_row_to_out(r) for r in rows]


@router.post(
    "/policies",
    response_model=PolicyOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upsert a (tool, agent) execution policy (admin only).",
)
async def upsert_policy(
    req: UpsertPolicyRequest,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> PolicyOut:
    pool = _require_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO tool_execution_policies
                    (tool_name, agent_name, allowed, requires_confirmation,
                     max_calls_per_hour)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (tool_name, agent_name) DO UPDATE
                    SET allowed = EXCLUDED.allowed,
                        requires_confirmation = EXCLUDED.requires_confirmation,
                        max_calls_per_hour = EXCLUDED.max_calls_per_hour,
                        updated_at = NOW()
                RETURNING id, tool_name, agent_name, allowed,
                          requires_confirmation, max_calls_per_hour,
                          created_at, updated_at
                """,
                req.tool_name,
                req.agent_name,
                req.allowed,
                req.requires_confirmation,
                req.max_calls_per_hour,
            )
    except asyncpg.PostgresError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"failed to upsert policy: {exc}",
        ) from exc
    logger.info(
        "admin upsert policy: admin=%s tool=%s agent=%s allowed=%s "
        "max_per_hour=%s",
        admin.id,
        req.tool_name,
        req.agent_name,
        req.allowed,
        req.max_calls_per_hour,
    )
    return _policy_row_to_out(row)


@router.delete(
    "/policies/{policy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove an execution policy override (admin only).",
)
async def delete_policy(
    policy_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> None:
    try:
        pid = uuid.UUID(policy_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="policy_id must be a valid UUID",
        ) from exc
    pool = _require_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM tool_execution_policies WHERE id = $1", pid
        )
    if result.endswith(" 0"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="policy not found",
        )
    logger.info("admin delete policy: admin=%s policy_id=%s", admin.id, pid)


@router.get(
    "/logs",
    response_model=list[ExecutionLogOut],
    summary="Recent tool execution audit log (admin only). Most recent first.",
)
async def list_logs(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    limit: int = Query(default=100, ge=1, le=1000),
    denied_only: bool = Query(default=False),
    tool_name: Optional[str] = Query(default=None),
    agent_name: Optional[str] = Query(default=None),
) -> list[ExecutionLogOut]:
    pool = _require_pool()
    sql = """
        SELECT id, session_id, user_id, tool_name, agent_name, scope_type,
               allowed, duration_ms, status, error_message, created_at
        FROM tool_execution_logs
    """
    where: list[str] = []
    args: list = []
    if denied_only:
        where.append("allowed = FALSE")
    if tool_name:
        args.append(tool_name)
        where.append(f"tool_name = ${len(args)}")
    if agent_name:
        args.append(agent_name)
        where.append(f"agent_name = ${len(args)}")
    if where:
        sql += " WHERE " + " AND ".join(where)
    args.append(limit)
    sql += f" ORDER BY created_at DESC LIMIT ${len(args)}"
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [_log_row_to_out(r) for r in rows]


@router.get(
    "/stats",
    response_model=GovernanceStats,
    summary="Per-tool execution counts over the last N hours (admin only).",
)
async def stats(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    window_hours: int = Query(default=24, ge=1, le=24 * 30),
) -> GovernanceStats:
    pool = _require_pool()
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT tool_name,
                   COUNT(*) FILTER (WHERE allowed = TRUE AND status = 'ok') AS allowed_count,
                   COUNT(*) FILTER (WHERE allowed = FALSE) AS denied_count,
                   COUNT(*) FILTER (WHERE allowed = TRUE AND status <> 'ok') AS error_count,
                   MAX(created_at) AS last_used_at
            FROM tool_execution_logs
            WHERE created_at >= $1
            GROUP BY tool_name
            ORDER BY tool_name ASC
            """,
            since,
        )
    return GovernanceStats(
        window_hours=window_hours,
        tools=[
            ToolStat(
                tool_name=r["tool_name"],
                allowed_count=int(r["allowed_count"] or 0),
                denied_count=int(r["denied_count"] or 0),
                error_count=int(r["error_count"] or 0),
                last_used_at=r["last_used_at"],
            )
            for r in rows
        ],
    )
