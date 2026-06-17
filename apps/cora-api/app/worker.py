"""Cora background worker (v0.1).

Polls the jobs table, claims queued rows with FOR UPDATE SKIP LOCKED,
dispatches to a handler, and persists results. Currently supports:
    - execution_plan_step  (also matches legacy 'plan_step')

Worker writes a heartbeat row to worker_heartbeats on every loop iteration
so the API's /worker/health can surface freshness.
"""

import asyncio
import json
import logging
import os
import signal
import socket
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app import news_ingest
from app.jobs import create_job
from app.agents.delegations import (
    DelegationError,
    complete_delegation,
    create_delegation,
    fail_delegation,
)
from app.agents.planner import PlanError, update_step
from app.clients import clients, close_clients, init_clients
from app.logging_config import configure_logging
from app.runtime_traces import write_trace
from app.tools import dispatch_tool
from app.tools.governance import check_permission

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = float(
    os.environ.get("WORKER_POLL_INTERVAL_SECONDS", "5")
)
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
# The worker serves no HTTP, so container liveness is tracked via a file the
# loop refreshes each iteration; app.healthcheck reads its mtime.
HEARTBEAT_FILE = os.environ.get(
    "WORKER_HEARTBEAT_FILE", "/tmp/cora-worker.heartbeat"
)
SHUTDOWN = asyncio.Event()


def touch_heartbeat_file() -> None:
    try:
        with open(HEARTBEAT_FILE, "w") as fh:
            fh.write(datetime.now(timezone.utc).isoformat())
    except OSError:
        logger.warning(
            "could not write heartbeat file %s", HEARTBEAT_FILE, exc_info=True
        )


class TransientError(Exception):
    """Retryable failure — job goes back to queued unless attempts exhausted."""


class PermanentError(Exception):
    """Non-retryable failure — job marked failed immediately."""


# ---------- DB helpers ----------


async def heartbeat(job_id: Optional[uuid.UUID] = None) -> None:
    touch_heartbeat_file()
    if clients.db_pool is None:
        return
    async with clients.db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO worker_heartbeats (worker_id, last_heartbeat_at, last_job_id, last_job_at)
            VALUES ($1, NOW(), $2,
                    CASE WHEN $2::uuid IS NULL THEN NULL ELSE NOW() END)
            ON CONFLICT (worker_id) DO UPDATE
            SET last_heartbeat_at = NOW(),
                last_job_id = COALESCE(EXCLUDED.last_job_id, worker_heartbeats.last_job_id),
                last_job_at = COALESCE(EXCLUDED.last_job_at, worker_heartbeats.last_job_at)
            """,
            WORKER_ID,
            job_id,
        )


async def claim_job() -> Optional[dict]:
    if clients.db_pool is None:
        return None
    async with clients.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            WITH claimed AS (
                SELECT id FROM jobs
                WHERE status = 'queued' AND attempts < max_attempts
                ORDER BY created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE jobs
            SET status = 'running', started_at = NOW(),
                attempts = attempts + 1
            WHERE id IN (SELECT id FROM claimed)
            RETURNING id, user_id, session_id, plan_id, step_id, job_type,
                      status, payload, attempts, max_attempts
            """
        )
    return dict(row) if row else None


async def _mark_completed(job_id: uuid.UUID, result: dict) -> None:
    async with clients.db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE jobs
            SET status = 'completed', result = $2, completed_at = NOW(),
                error_message = NULL
            WHERE id = $1
            """,
            job_id,
            result,
        )


async def _mark_failed(
    job_id: uuid.UUID, error_message: str, *, retry: bool
) -> None:
    async with clients.db_pool.acquire() as conn:
        if retry:
            await conn.execute(
                """
                UPDATE jobs
                SET status = 'queued', error_message = $2, started_at = NULL
                WHERE id = $1
                """,
                job_id,
                error_message,
            )
        else:
            await conn.execute(
                """
                UPDATE jobs
                SET status = 'failed', error_message = $2, completed_at = NOW()
                WHERE id = $1
                """,
                job_id,
                error_message,
            )


# ---------- Handlers ----------


async def execute_plan_step(job: dict) -> dict:
    plan_id = job["plan_id"]
    step_id = job["step_id"]
    if plan_id is None or step_id is None:
        raise PermanentError("execution_plan_step job missing plan_id/step_id")

    async with clients.db_pool.acquire() as conn:
        step_row = await conn.fetchrow(
            """
            SELECT id, plan_id, step_number, title, description,
                   assigned_agent, tool_name, status
            FROM execution_plan_steps
            WHERE id = $1 AND plan_id = $2
            """,
            step_id,
            plan_id,
        )
    if step_row is None:
        raise PermanentError("step no longer exists")
    step = dict(step_row)

    if step["status"] in {"completed", "failed", "skipped"}:
        return {
            "skipped": True,
            "reason": f"step already in terminal state {step['status']!r}",
        }

    tool_name = step["tool_name"]
    agent_name = step["assigned_agent"]

    result_payload: dict[str, Any] = {
        "agent": agent_name,
        "step_number": step["step_number"],
        "worker_id": WORKER_ID,
    }

    # Open an ATLAS→assigned_agent delegation for the step execution window.
    delegation_id: Optional[uuid.UUID] = None
    if agent_name and agent_name.upper() != "ATLAS":
        try:
            delegation_row = await create_delegation(
                from_agent="ATLAS",
                to_agent=agent_name,
                delegation_reason=(
                    f"Worker dispatching plan step #{step['step_number']} "
                    f"to {agent_name}"
                ),
                session_id=job["session_id"],
                execution_plan_id=plan_id,
                workspace_id=job.get("workspace_id"),
                user_id=job["user_id"],
                input_payload={
                    "step_id": str(step_id),
                    "step_title": step["title"],
                    "tool_name": tool_name,
                },
                initial_status="running",
            )
            delegation_id = delegation_row["id"]
        except DelegationError as exc:
            logger.warning(
                "worker delegation skipped: plan=%s step=%s reason=%s",
                plan_id,
                step_id,
                exc,
            )

    async def _close_delegation_fail(message: str) -> None:
        if delegation_id is None:
            return
        try:
            await fail_delegation(
                delegation_id, error_message=message, user_id=job["user_id"]
            )
        except Exception:
            logger.exception(
                "worker delegation fail-close failed: delegation_id=%s",
                delegation_id,
            )

    if tool_name:
        async with clients.db_pool.acquire() as conn:
            tool_row = await conn.fetchrow(
                """
                SELECT id, name, description, type, endpoint, enabled,
                       requires_confirmation, mcp_server_name, mcp_action_name,
                       input_schema, output_schema, risk_level, allowed_agents
                FROM tools WHERE name = $1
                """,
                tool_name,
            )
        if tool_row is None:
            await _close_delegation_fail(f"tool {tool_name!r} not found")
            raise PermanentError(f"tool {tool_name!r} not found")
        tool = dict(tool_row)

        decision = await check_permission(
            tool,
            agent_name=agent_name,
            user_id=job["user_id"],
            is_admin=False,
        )
        if not decision.allowed:
            msg = (
                f"tool denied by governance: {decision.reason} "
                f"(source={decision.policy_source})"
            )
            await _close_delegation_fail(msg)
            raise PermanentError(msg)

        try:
            dispatch_result = await dispatch_tool(
                tool,
                {
                    "session_id": str(job["session_id"])
                    if job["session_id"]
                    else None,
                    "user_message": None,
                    "metadata": {
                        "source": "cora-worker",
                        "job_id": str(job["id"]),
                        "plan_id": str(plan_id),
                        "step_id": str(step_id),
                    },
                },
            )
        except Exception as exc:  # transport / runtime failure → retry
            await _close_delegation_fail(f"tool dispatch failed: {exc}")
            raise TransientError(f"tool dispatch failed: {exc}") from exc

        result_payload["tool_dispatch"] = dispatch_result
        if dispatch_result.get("status") not in ("ok", None):
            # Tool reported a non-ok status — surface but don't retry,
            # the step itself completes with the failure recorded.
            result_payload["tool_status"] = dispatch_result.get("status")
    else:
        result_payload["simulated"] = True
        result_payload["note"] = (
            "v0.1 worker simulated step completion (no tool wired)."
        )

    # Persist worker_execution trace before flipping the step.
    await write_trace(
        session_id=job["session_id"],
        user_id=job["user_id"],
        trace_type="worker_execution",
        status="ok",
        selected_agent=agent_name or "ATLAS",
        tool_name=tool_name,
        tool_result=result_payload,
    )

    # Worker has elevated rights — bypass plan-owner authz check.
    try:
        await update_step(
            plan_id,
            step_id,
            user_id=job["user_id"] or uuid.uuid4(),
            is_admin=True,
            status_value="completed",
            result=result_payload,
        )
    except PlanError as exc:
        await _close_delegation_fail(f"step transition failed: {exc}")
        raise PermanentError(f"step transition failed: {exc}") from exc

    if delegation_id is not None:
        try:
            await complete_delegation(
                delegation_id,
                output_payload={"step_status": "completed"},
                user_id=job["user_id"],
            )
        except Exception:
            logger.exception(
                "worker delegation complete-close failed: delegation_id=%s",
                delegation_id,
            )

    return result_payload


async def refresh_news_feed(job: dict) -> dict:
    """news_feed_refresh handler (unified knowledge path). Payload carries
    source_id; settings come from the feed's metadata. Reuses the same refresh
    logic as the manual endpoint."""
    payload = job.get("payload") or {}
    raw = payload.get("source_id")
    if not raw:
        raise PermanentError("news_feed_refresh job missing payload.source_id")
    try:
        source_id = uuid.UUID(str(raw))
    except ValueError as exc:
        raise PermanentError(f"invalid source_id: {raw!r}") from exc
    try:
        return await news_ingest.refresh_feed_source(
            source_id, user_id=job.get("user_id")
        )
    except news_ingest.NewsIngestError as exc:
        # Network/transient feed failures retry; bad-feed/not-found are permanent.
        if exc.code in ("fetch_failed", "unavailable"):
            raise TransientError(str(exc)) from exc
        raise PermanentError(str(exc)) from exc


HANDLERS = {
    "execution_plan_step": execute_plan_step,
    "plan_step": execute_plan_step,  # backwards-compat
    "news_feed_refresh": refresh_news_feed,
}


# ---------- Scheduled news-feed refresh ----------

NEWS_SCHEDULE_INTERVAL_SECONDS = 60.0
# Scheduler heartbeats share worker_heartbeats with a distinct worker_id so
# /worker/health can prove the news scheduler is ticking (the main loop's
# heartbeat would stay fresh even if only the scheduler stalled).
SCHEDULER_WORKER_ID = f"{WORKER_ID}:news-scheduler"


async def _scheduler_heartbeat(enqueued: int) -> None:
    """Record a scheduler-tick heartbeat (separate row from the main loop)."""
    if clients.db_pool is None:
        return
    async with clients.db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO worker_heartbeats (worker_id, last_heartbeat_at, last_job_at)
            VALUES ($1, NOW(), CASE WHEN $2 > 0 THEN NOW() ELSE NULL END)
            ON CONFLICT (worker_id) DO UPDATE
            SET last_heartbeat_at = NOW(),
                last_job_at = CASE WHEN $2 > 0 THEN NOW()
                                   ELSE worker_heartbeats.last_job_at END
            """,
            SCHEDULER_WORKER_ID,
            enqueued,
        )


async def enqueue_due_news_feeds() -> int:
    """Find due news_feed knowledge_sources and enqueue news_feed_refresh jobs,
    skipping feeds that already have a queued/running refresh job. Returns the
    number of jobs enqueued and records a scheduler-tick heartbeat."""
    if clients.db_pool is None:
        return 0
    enqueued = 0
    async with clients.db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, workspace_id, uploaded_by, title, source_url, metadata
            FROM knowledge_sources
            WHERE source_type='news_feed' AND status='active'
              AND (metadata->>'refresh_enabled')::boolean IS TRUE
              AND COALESCE((metadata->>'refresh_interval_minutes')::int, 0) > 0
              AND metadata->>'next_refresh_at' IS NOT NULL
              AND (metadata->>'next_refresh_at')::timestamptz <= NOW()
            """
        )
    for r in rows:
        sid = r["id"]
        try:
            async with clients.db_pool.acquire() as conn:
                dup = await conn.fetchval(
                    "SELECT 1 FROM jobs WHERE job_type='news_feed_refresh' "
                    "AND status IN ('queued','running') "
                    "AND payload->>'source_id' = $1 LIMIT 1",
                    str(sid),
                )
            if dup:
                continue
            meta = r["metadata"]
            if not isinstance(meta, dict):
                meta = json.loads(meta or "{}")
            payload = {
                "source_id": str(sid),
                "workspace_id": str(r["workspace_id"]) if r["workspace_id"] else None,
                "feed_url": meta.get("feed_url") or r["source_url"],
                "source_name": meta.get("source_name") or r["title"],
                "max_items": meta.get("max_items", 20),
                "scope_type": meta.get("scope_type", "user"),
                "importance": meta.get("importance", 3),
                "auto_embed": meta.get("auto_embed", False),
                "fetch_article_body": meta.get("fetch_article_body", False),
            }
            uid = r["uploaded_by"]
            job = await create_job(
                user_id=uid,
                job_type="news_feed_refresh",
                payload=payload,
                workspace_id=r["workspace_id"],
            )
            await write_trace(
                session_id=None,
                user_id=uid,
                trace_type="news_feed_refresh_job_enqueued",
                status="ok",
                selected_agent="ATLAS",
                tool_name="news_feed_refresh",
                tool_result={
                    "source_id": str(sid),
                    "job_id": str(job["id"]),
                    "feed_url": payload["feed_url"],
                    "source_name": payload["source_name"],
                },
                workspace_id=r["workspace_id"],
            )
            enqueued += 1
            logger.info(
                "enqueued news_feed_refresh: source=%s job=%s", sid, job["id"]
            )
        except Exception:
            logger.exception("failed to enqueue news_feed_refresh: source=%s", sid)
    await _scheduler_heartbeat(enqueued)
    return enqueued


# ---------- Main loop ----------


async def process_one() -> bool:
    try:
        job = await claim_job()
    except Exception:
        logger.exception("claim_job failed")
        return False
    if job is None:
        return False

    job_id = job["id"]
    logger.info(
        "worker claimed job: id=%s type=%s attempt=%s/%s",
        job_id,
        job["job_type"],
        job["attempts"],
        job["max_attempts"],
    )
    await write_trace(
        session_id=job["session_id"],
        user_id=job["user_id"],
        trace_type="job_started",
        status="ok",
        selected_agent="ATLAS",
        tool_name="worker",
        tool_result={
            "job_id": str(job_id),
            "job_type": job["job_type"],
            "attempt": job["attempts"],
        },
    )

    handler = HANDLERS.get(job["job_type"])
    if handler is None:
        msg = f"no handler for job_type {job['job_type']!r}"
        logger.error("%s job_id=%s", msg, job_id)
        await _mark_failed(job_id, msg, retry=False)
        await write_trace(
            session_id=job["session_id"],
            user_id=job["user_id"],
            trace_type="job_failed",
            status="error",
            error_message=msg,
            selected_agent="ATLAS",
            tool_name="worker",
            tool_result={"job_id": str(job_id)},
        )
        await heartbeat(job_id=job_id)
        return True

    started = datetime.now(timezone.utc)
    try:
        result = await handler(job)
    except TransientError as exc:
        should_retry = job["attempts"] < job["max_attempts"]
        logger.warning(
            "worker handler transient error: job_id=%s retry=%s err=%s",
            job_id,
            should_retry,
            exc,
        )
        await _mark_failed(job_id, str(exc), retry=should_retry)
        await write_trace(
            session_id=job["session_id"],
            user_id=job["user_id"],
            trace_type="job_failed" if not should_retry else "worker_execution",
            status="error",
            error_message=str(exc),
            selected_agent="ATLAS",
            tool_name="worker",
            tool_result={
                "job_id": str(job_id),
                "will_retry": should_retry,
                "attempts": job["attempts"],
                "max_attempts": job["max_attempts"],
            },
        )
        await heartbeat(job_id=job_id)
        return True
    except PermanentError as exc:
        logger.warning(
            "worker handler permanent error: job_id=%s err=%s", job_id, exc
        )
        await _mark_failed(job_id, str(exc), retry=False)
        await write_trace(
            session_id=job["session_id"],
            user_id=job["user_id"],
            trace_type="job_failed",
            status="error",
            error_message=str(exc),
            selected_agent="ATLAS",
            tool_name="worker",
            tool_result={"job_id": str(job_id)},
        )
        await heartbeat(job_id=job_id)
        return True
    except Exception as exc:
        should_retry = job["attempts"] < job["max_attempts"]
        logger.exception("worker handler crashed: job_id=%s", job_id)
        await _mark_failed(job_id, repr(exc), retry=should_retry)
        await write_trace(
            session_id=job["session_id"],
            user_id=job["user_id"],
            trace_type="job_failed" if not should_retry else "worker_execution",
            status="error",
            error_message=repr(exc),
            selected_agent="ATLAS",
            tool_name="worker",
            tool_result={
                "job_id": str(job_id),
                "will_retry": should_retry,
                "attempts": job["attempts"],
            },
        )
        await heartbeat(job_id=job_id)
        return True

    duration_ms = int(
        (datetime.now(timezone.utc) - started).total_seconds() * 1000
    )
    final_result = result if isinstance(result, dict) else {"value": result}
    await _mark_completed(job_id, final_result)
    await write_trace(
        session_id=job["session_id"],
        user_id=job["user_id"],
        trace_type="job_completed",
        status="ok",
        selected_agent="ATLAS",
        tool_name="worker",
        tool_result={"job_id": str(job_id), "result": final_result},
        duration_ms=duration_ms,
    )
    logger.info(
        "worker completed job: id=%s duration_ms=%s", job_id, duration_ms
    )
    await heartbeat(job_id=job_id)
    return True


async def main() -> None:
    configure_logging()
    logger.info(
        "cora-worker starting: worker_id=%s poll_interval=%ss",
        WORKER_ID,
        POLL_INTERVAL_SECONDS,
    )
    touch_heartbeat_file()
    await init_clients()
    if clients.db_pool is None:
        logger.error("worker cannot start: Postgres pool unavailable")
        return

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, SHUTDOWN.set)
        except NotImplementedError:
            # e.g. on Windows; fall back to plain signal handlers
            signal.signal(sig, lambda *_: SHUTDOWN.set())

    last_schedule = 0.0
    try:
        while not SHUTDOWN.is_set():
            try:
                processed = await process_one()
            except Exception:
                logger.exception("worker loop error")
                processed = False
            try:
                await heartbeat()
            except Exception:
                logger.exception("heartbeat write failed")
            # Periodic news-feed scheduler (gated so it doesn't run every poll).
            if time.monotonic() - last_schedule >= NEWS_SCHEDULE_INTERVAL_SECONDS:
                last_schedule = time.monotonic()
                try:
                    await enqueue_due_news_feeds()
                except Exception:
                    logger.exception("news feed scheduler tick failed")
            if not processed:
                try:
                    await asyncio.wait_for(
                        SHUTDOWN.wait(), timeout=POLL_INTERVAL_SECONDS
                    )
                except asyncio.TimeoutError:
                    pass
    finally:
        logger.info("cora-worker shutting down: worker_id=%s", WORKER_ID)
        await close_clients()


if __name__ == "__main__":
    asyncio.run(main())
