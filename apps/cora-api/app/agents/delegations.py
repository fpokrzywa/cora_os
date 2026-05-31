"""Multi-agent delegations — v0.1 governance + bookkeeping.

ATLAS is the only orchestrator; specialist subagents (FORGE, SCRIBE, PULSE,
SIGNAL, CHRONOS) are never autonomous in v0.1. A delegation row is just an
audit record showing one agent handed work to another for a specific reason.

Constraints enforced:
  - from_agent != to_agent (no self-delegation)
  - max 3 concurrent (pending/running) delegations per session OR per plan
  - traces written on create / complete / fail
"""

import logging
import uuid
from typing import Optional

from app.clients import clients
from app.runtime_traces import write_trace

logger = logging.getLogger(__name__)

MAX_ACTIVE_DELEGATIONS_PER_SCOPE = 3

_SELECT_COLS = """
    id, workspace_id, session_id, execution_plan_id,
    from_agent, to_agent, delegation_reason,
    status, input_payload, output_payload,
    created_at, completed_at
"""


class DelegationError(Exception):
    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


def _norm(agent: str) -> str:
    return (agent or "").strip()


async def _active_count(
    *,
    session_id: Optional[uuid.UUID],
    execution_plan_id: Optional[uuid.UUID],
) -> int:
    if clients.db_pool is None:
        return 0
    if session_id is None and execution_plan_id is None:
        return 0
    parts = ["SELECT COUNT(*) FROM agent_delegations WHERE status IN ('pending','running')"]
    args: list = []
    if session_id is not None:
        args.append(session_id)
        parts.append(f"AND session_id = ${len(args)}")
    elif execution_plan_id is not None:
        args.append(execution_plan_id)
        parts.append(f"AND execution_plan_id = ${len(args)}")
    sql = " ".join(parts)
    async with clients.db_pool.acquire() as conn:
        return int(await conn.fetchval(sql, *args) or 0)


async def create_delegation(
    *,
    from_agent: str,
    to_agent: str,
    delegation_reason: Optional[str] = None,
    session_id: Optional[uuid.UUID] = None,
    execution_plan_id: Optional[uuid.UUID] = None,
    workspace_id: Optional[uuid.UUID] = None,
    input_payload: Optional[dict] = None,
    user_id: Optional[uuid.UUID] = None,
    initial_status: str = "pending",
) -> dict:
    fa = _norm(from_agent)
    ta = _norm(to_agent)
    if not fa or not ta:
        raise DelegationError("from_agent and to_agent are required", code="invalid")
    if fa.lower() == ta.lower():
        raise DelegationError(
            f"self-delegation rejected ({fa} → {ta})", code="self"
        )
    if initial_status not in {"pending", "running"}:
        raise DelegationError(
            f"initial status must be pending or running, got {initial_status!r}",
            code="invalid",
        )
    if clients.db_pool is None:
        raise DelegationError("Postgres pool unavailable", code="unavailable")

    active = await _active_count(
        session_id=session_id, execution_plan_id=execution_plan_id
    )
    if active >= MAX_ACTIVE_DELEGATIONS_PER_SCOPE:
        raise DelegationError(
            f"delegation depth limit reached ({active} active in scope; "
            f"max {MAX_ACTIVE_DELEGATIONS_PER_SCOPE})",
            code="depth_exceeded",
        )

    async with clients.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO agent_delegations (
                workspace_id, session_id, execution_plan_id,
                from_agent, to_agent, delegation_reason, status,
                input_payload
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING {_SELECT_COLS}
            """,
            workspace_id,
            session_id,
            execution_plan_id,
            fa,
            ta,
            delegation_reason,
            initial_status,
            input_payload,
        )

    logger.info(
        "delegation created: id=%s %s→%s session=%s plan=%s reason=%r status=%s",
        row["id"],
        fa,
        ta,
        session_id,
        execution_plan_id,
        delegation_reason,
        initial_status,
    )
    await write_trace(
        session_id=session_id,
        user_id=user_id,
        trace_type="delegation_created",
        status="ok",
        selected_agent="ATLAS",
        tool_name="delegation",
        tool_result={
            "delegation_id": str(row["id"]),
            "from_agent": fa,
            "to_agent": ta,
            "reason": delegation_reason,
            "execution_plan_id": str(execution_plan_id) if execution_plan_id else None,
        },
        workspace_id=workspace_id,
    )
    return dict(row)


async def update_delegation_status(
    delegation_id: uuid.UUID,
    *,
    status_value: str,
    output_payload: Optional[dict] = None,
    user_id: Optional[uuid.UUID] = None,
    error_message: Optional[str] = None,
) -> Optional[dict]:
    if status_value not in {"pending", "running", "completed", "failed"}:
        raise DelegationError(
            f"invalid status {status_value!r}", code="invalid"
        )
    if clients.db_pool is None:
        return None
    terminal = status_value in {"completed", "failed"}
    async with clients.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE agent_delegations
            SET status = $2,
                output_payload = COALESCE($3, output_payload),
                completed_at = CASE WHEN $4 THEN NOW() ELSE completed_at END
            WHERE id = $1
            RETURNING {_SELECT_COLS}
            """,
            delegation_id,
            status_value,
            output_payload,
            terminal,
        )
    if row is None:
        return None
    if terminal:
        trace_type = (
            "delegation_completed" if status_value == "completed" else "delegation_failed"
        )
        await write_trace(
            session_id=row["session_id"],
            user_id=user_id,
            trace_type=trace_type,
            status="ok" if status_value == "completed" else "error",
            selected_agent="ATLAS",
            tool_name="delegation",
            tool_result={
                "delegation_id": str(row["id"]),
                "from_agent": row["from_agent"],
                "to_agent": row["to_agent"],
                "execution_plan_id": str(row["execution_plan_id"])
                if row["execution_plan_id"]
                else None,
            },
            workspace_id=row["workspace_id"],
            error_message=error_message,
        )
    logger.info(
        "delegation %s: id=%s %s→%s",
        status_value,
        row["id"],
        row["from_agent"],
        row["to_agent"],
    )
    return dict(row)


async def complete_delegation(
    delegation_id: uuid.UUID,
    *,
    output_payload: Optional[dict] = None,
    user_id: Optional[uuid.UUID] = None,
) -> Optional[dict]:
    return await update_delegation_status(
        delegation_id,
        status_value="completed",
        output_payload=output_payload,
        user_id=user_id,
    )


async def fail_delegation(
    delegation_id: uuid.UUID,
    *,
    error_message: str,
    user_id: Optional[uuid.UUID] = None,
) -> Optional[dict]:
    return await update_delegation_status(
        delegation_id,
        status_value="failed",
        output_payload={"error": error_message},
        user_id=user_id,
        error_message=error_message,
    )


async def list_delegations(
    *,
    limit: int = 100,
    offset: int = 0,
    session_id: Optional[uuid.UUID] = None,
    execution_plan_id: Optional[uuid.UUID] = None,
    workspace_id: Optional[uuid.UUID] = None,
    status_filter: Optional[str] = None,
) -> list[dict]:
    if clients.db_pool is None:
        return []
    parts = [f"SELECT {_SELECT_COLS} FROM agent_delegations"]
    where: list[str] = []
    args: list = []
    if session_id is not None:
        args.append(session_id)
        where.append(f"session_id = ${len(args)}")
    if execution_plan_id is not None:
        args.append(execution_plan_id)
        where.append(f"execution_plan_id = ${len(args)}")
    if workspace_id is not None:
        args.append(workspace_id)
        where.append(f"workspace_id = ${len(args)}")
    if status_filter:
        args.append(status_filter)
        where.append(f"status = ${len(args)}")
    if where:
        parts.append("WHERE " + " AND ".join(where))
    args.append(limit)
    args.append(offset)
    parts.append(
        f"ORDER BY created_at DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}"
    )
    sql = " ".join(parts)
    async with clients.db_pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [dict(r) for r in rows]


async def get_delegation(delegation_id: uuid.UUID) -> Optional[dict]:
    if clients.db_pool is None:
        return None
    async with clients.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_SELECT_COLS} FROM agent_delegations WHERE id = $1",
            delegation_id,
        )
    return dict(row) if row else None
