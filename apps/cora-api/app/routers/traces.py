import logging
import uuid
from datetime import datetime
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.auth import CurrentUser, require_admin
from app.clients import clients

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/traces", tags=["admin", "traces"])


class TraceOut(BaseModel):
    id: str
    session_id: Optional[str]
    user_id: Optional[str]
    trace_type: str
    selected_agent: Optional[str]
    user_message: Optional[str]
    memory_count: int
    memory_ids: list[str]
    tool_name: Optional[str]
    tool_result: Optional[Any]
    mcp_server_name: Optional[str]
    mcp_action_name: Optional[str]
    model_name: Optional[str]
    model_endpoint: Optional[str]
    duration_ms: Optional[int]
    status: str
    error_message: Optional[str]
    metadata: Optional[dict]
    created_at: datetime


_SELECT_COLS = """
    id, session_id, user_id, trace_type, selected_agent, user_message,
    memory_count, memory_ids, tool_name, tool_result, mcp_server_name,
    mcp_action_name, model_name, model_endpoint, duration_ms, status,
    error_message, metadata, created_at
"""


def _require_pool():
    if clients.db_pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Postgres pool unavailable",
        )
    return clients.db_pool


def _row_to_out(row) -> TraceOut:
    return TraceOut(
        id=str(row["id"]),
        session_id=str(row["session_id"]) if row["session_id"] else None,
        user_id=str(row["user_id"]) if row["user_id"] else None,
        trace_type=row["trace_type"],
        selected_agent=row["selected_agent"],
        user_message=row["user_message"],
        memory_count=row["memory_count"],
        memory_ids=[str(mid) for mid in (row["memory_ids"] or [])],
        tool_name=row["tool_name"],
        tool_result=row["tool_result"],
        mcp_server_name=row["mcp_server_name"],
        mcp_action_name=row["mcp_action_name"],
        model_name=row["model_name"],
        model_endpoint=row["model_endpoint"],
        duration_ms=row["duration_ms"],
        status=row["status"],
        error_message=row["error_message"],
        metadata=row["metadata"] or {},
        created_at=row["created_at"],
    )


@router.get(
    "",
    response_model=list[TraceOut],
    summary="List runtime traces (admin only). Filterable; most recent first.",
)
async def list_traces(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    trace_type: Optional[str] = Query(default=None),
    selected_agent: Optional[str] = Query(default=None),
    trace_status: Optional[str] = Query(
        default=None,
        alias="status",
        description="Filter by trace status (ok, error, denied, ...).",
    ),
    session_id: Optional[str] = Query(default=None),
) -> list[TraceOut]:
    pool = _require_pool()
    where: list[str] = []
    args: list = []

    def add(col: str, val) -> None:
        args.append(val)
        where.append(f"{col} = ${len(args)}")

    if trace_type:
        add("trace_type", trace_type)
    if selected_agent:
        add("selected_agent", selected_agent)
    if trace_status:
        add("status", trace_status)
    if session_id:
        try:
            session_uuid = uuid.UUID(session_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="session_id must be a valid UUID",
            ) from exc
        add("session_id", session_uuid)

    sql = f"SELECT {_SELECT_COLS} FROM runtime_traces"
    if where:
        sql += " WHERE " + " AND ".join(where)
    args.append(limit)
    args.append(offset)
    sql += f" ORDER BY created_at DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}"

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    logger.info(
        "admin list traces: admin=%s filters=%s count=%s",
        admin.id,
        {
            "trace_type": trace_type,
            "selected_agent": selected_agent,
            "status": trace_status,
            "session_id": session_id,
        },
        len(rows),
    )
    return [_row_to_out(r) for r in rows]


@router.get(
    "/session/{session_id}",
    response_model=list[TraceOut],
    summary="All traces for a session (admin only). Chronological order.",
)
async def list_traces_for_session(
    session_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> list[TraceOut]:
    try:
        session_uuid = uuid.UUID(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="session_id must be a valid UUID",
        ) from exc
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_SELECT_COLS}
            FROM runtime_traces
            WHERE session_id = $1
            ORDER BY created_at ASC, id ASC
            """,
            session_uuid,
        )
    return [_row_to_out(r) for r in rows]


@router.get(
    "/{trace_id}",
    response_model=TraceOut,
    summary="Get one runtime trace by id (admin only).",
)
async def get_trace(
    trace_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> TraceOut:
    try:
        tid = uuid.UUID(trace_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="trace_id must be a valid UUID",
        ) from exc
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_SELECT_COLS} FROM runtime_traces WHERE id = $1", tid
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="trace not found",
        )
    return _row_to_out(row)
