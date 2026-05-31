"""CHRONOS internal schedule-proposal service (v0.1).

Persists review-only schedule proposals in `schedule_proposals`. There is NO
calendar-write path — proposals are prepared, reviewed, approved, or archived
only. A future governed `internal_action` runner / chat-trigger may call
create_schedule_proposal.
"""

import logging
import uuid
from typing import Optional

from app.clients import clients
from app.review_workflow import PROPOSAL_STATUSES

logger = logging.getLogger(__name__)

# PROPOSAL_STATUSES (the full review lifecycle) is defined in review_workflow and
# re-exported here for backward compatibility with callers/tests.

_COLS = (
    "id, workspace_id, created_by, agent_name, proposal_type, title, "
    "description, start_time, end_time, timezone, attendees, agenda, "
    "reminders, status, metadata, reviewed_by, reviewed_at, approved_by, "
    "approved_at, archived_at, review_notes, created_at, updated_at"
)


class ChronosToolError(Exception):
    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


def _require_pool():
    if clients.db_pool is None:
        raise ChronosToolError("Postgres pool unavailable", code="unavailable")
    return clients.db_pool


async def create_schedule_proposal(
    *,
    workspace_id: Optional[uuid.UUID],
    user_id: uuid.UUID,
    proposal_type: str,
    title: str,
    description: Optional[str] = None,
    start_time=None,
    end_time=None,
    timezone: Optional[str] = None,
    attendees: Optional[list] = None,
    agenda: Optional[list] = None,
    reminders: Optional[list] = None,
    agent_name: str = "CHRONOS",
    metadata: Optional[dict] = None,
) -> dict:
    if not (proposal_type or "").strip():
        raise ChronosToolError("proposal_type is required")
    if not (title or "").strip():
        raise ChronosToolError("title is required")
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO schedule_proposals
                (workspace_id, created_by, agent_name, proposal_type, title,
                 description, start_time, end_time, timezone,
                 attendees, agenda, reminders, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            RETURNING {_COLS}
            """,
            workspace_id,
            user_id,
            agent_name,
            proposal_type.strip(),
            title,
            description,
            start_time,
            end_time,
            timezone,
            attendees or [],
            agenda or [],
            reminders or [],
            metadata or {},
        )
    return dict(row)


async def list_proposals(
    *,
    workspace_id: Optional[uuid.UUID],
    include_archived: bool = False,
    owner_id: Optional[uuid.UUID] = None,
) -> list[dict]:
    """List proposals in a workspace. Pass owner_id to restrict to one creator
    (non-admin callers); omit it for the admin workspace-level view."""
    pool = _require_pool()
    sql = (
        f"SELECT {_COLS} FROM schedule_proposals "
        "WHERE (workspace_id = $1 OR ($1 IS NULL AND workspace_id IS NULL))"
    )
    args: list = [workspace_id]
    if owner_id is not None:
        args.append(owner_id)
        sql += f" AND created_by = ${len(args)}"
    if not include_archived:
        sql += " AND status <> 'archived'"
    sql += " ORDER BY created_at DESC LIMIT 200"
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [dict(r) for r in rows]


async def get_proposal(proposal_id: uuid.UUID) -> Optional[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_COLS} FROM schedule_proposals WHERE id = $1", proposal_id
        )
    return dict(row) if row else None


async def update_proposal(proposal_id: uuid.UUID, fields: dict) -> Optional[dict]:
    """Update allowed columns. Status, if provided, must be valid."""
    allowed = (
        "proposal_type", "title", "description", "start_time", "end_time",
        "timezone", "attendees", "agenda", "reminders", "status", "metadata",
    )
    sets: list[str] = []
    args: list = []
    for col in allowed:
        if col in fields and fields[col] is not None:
            if col == "status" and fields[col] not in PROPOSAL_STATUSES:
                raise ChronosToolError(
                    f"invalid status {fields[col]!r}", code="invalid_status"
                )
            args.append(fields[col])
            sets.append(f"{col} = ${len(args)}")
    if not sets:
        return await get_proposal(proposal_id)
    sets.append("updated_at = NOW()")
    args.append(proposal_id)
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE schedule_proposals SET {', '.join(sets)} "
            f"WHERE id = ${len(args)} RETURNING {_COLS}",
            *args,
        )
    return dict(row) if row else None


async def archive_proposal(proposal_id: uuid.UUID) -> Optional[dict]:
    return await update_proposal(proposal_id, {"status": "archived"})


async def delete_proposal(proposal_id: uuid.UUID) -> bool:
    pool = _require_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            "DELETE FROM schedule_proposals WHERE id = $1", proposal_id
        )
    return not res.endswith(" 0")
