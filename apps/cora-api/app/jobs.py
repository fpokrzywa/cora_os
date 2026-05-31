"""Background jobs registry — v0.1 queueing layer.

v0.1 only stores job records; there is no worker that picks them up. The
goal is to let plan steps and other long-running actions be expressed as
jobs that an executor can consume later.

A job in v0.1 only transitions queued → cancelled (via admin) or stays
queued. Worker-driven transitions (running, completed, failed) come later.
"""

import logging
import uuid
from typing import Optional

from app.clients import clients

logger = logging.getLogger(__name__)

JOB_TERMINAL_STATUSES: set[str] = {"completed", "failed", "cancelled"}


class JobError(Exception):
    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


_SELECT_COLS = """
    id, user_id, session_id, plan_id, step_id, job_type, status,
    payload, result, error_message, attempts, max_attempts,
    created_at, started_at, completed_at
"""


async def create_job(
    *,
    user_id: Optional[uuid.UUID],
    session_id: Optional[uuid.UUID] = None,
    plan_id: Optional[uuid.UUID] = None,
    step_id: Optional[uuid.UUID] = None,
    job_type: str,
    payload: Optional[dict] = None,
    max_attempts: int = 3,
    workspace_id: Optional[uuid.UUID] = None,
) -> dict:
    if clients.db_pool is None:
        raise JobError("Postgres pool unavailable", code="unavailable")
    async with clients.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO jobs (
                user_id, session_id, plan_id, step_id, job_type,
                payload, max_attempts, workspace_id
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING {_SELECT_COLS}
            """,
            user_id,
            session_id,
            plan_id,
            step_id,
            job_type,
            payload,
            max_attempts,
            workspace_id,
        )
    logger.info(
        "job created: id=%s type=%s user_id=%s plan_id=%s step_id=%s",
        row["id"],
        job_type,
        user_id,
        plan_id,
        step_id,
    )
    return dict(row)


async def list_jobs(
    *,
    limit: int = 100,
    offset: int = 0,
    status_filter: Optional[str] = None,
    job_type: Optional[str] = None,
    plan_id: Optional[uuid.UUID] = None,
) -> list[dict]:
    if clients.db_pool is None:
        return []
    parts = [f"SELECT {_SELECT_COLS} FROM jobs"]
    where: list[str] = []
    args: list = []
    if status_filter:
        args.append(status_filter)
        where.append(f"status = ${len(args)}")
    if job_type:
        args.append(job_type)
        where.append(f"job_type = ${len(args)}")
    if plan_id is not None:
        args.append(plan_id)
        where.append(f"plan_id = ${len(args)}")
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


async def get_job(job_id: uuid.UUID) -> Optional[dict]:
    if clients.db_pool is None:
        return None
    async with clients.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_SELECT_COLS} FROM jobs WHERE id = $1", job_id
        )
    return dict(row) if row else None


async def cancel_job(job_id: uuid.UUID) -> dict:
    if clients.db_pool is None:
        raise JobError("Postgres pool unavailable", code="unavailable")
    async with clients.db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"SELECT {_SELECT_COLS} FROM jobs WHERE id = $1 FOR UPDATE",
                job_id,
            )
            if row is None:
                raise JobError("job not found", code="not_found")
            if row["status"] in JOB_TERMINAL_STATUSES:
                # Idempotent — return the existing terminal state.
                return dict(row)
            if row["status"] != "queued":
                # Running jobs need worker coordination, not in v0.1.
                raise JobError(
                    f"cannot cancel a job in status {row['status']!r}",
                    code="invalid_transition",
                )
            updated = await conn.fetchrow(
                f"""
                UPDATE jobs
                SET status = 'cancelled', completed_at = NOW()
                WHERE id = $1
                RETURNING {_SELECT_COLS}
                """,
                job_id,
            )
    logger.info("job cancelled: id=%s", job_id)
    return dict(updated)
