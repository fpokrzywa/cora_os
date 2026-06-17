import logging
from datetime import datetime, timezone

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from app.clients import clients
from app.config import settings

WORKER_HEARTBEAT_STALE_SECONDS = 30
# The news scheduler ticks ~every 60s; allow a wider margin before flagging it.
NEWS_SCHEDULER_STALE_SECONDS = 150
_SCHEDULER_WORKER_SUFFIX = ":news-scheduler"

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", summary="Service liveness")
async def health() -> dict:
    return {
        "status": "ok",
        "service": settings.service_name,
        "env": settings.cora_env,
    }


@router.get("/db", summary="Postgres connectivity")
async def health_db() -> JSONResponse:
    if clients.db_pool is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable", "component": "postgres", "detail": "pool not initialized"},
        )
    try:
        async with clients.db_pool.acquire() as conn:
            value = await conn.fetchval("SELECT 1")
        if value != 1:
            raise RuntimeError(f"unexpected SELECT 1 result: {value!r}")
        return JSONResponse(content={"status": "ok", "component": "postgres"})
    except Exception as exc:
        logger.exception("Postgres health check failed")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "error", "component": "postgres", "detail": str(exc)},
        )


@router.get("/redis", summary="Redis connectivity")
async def health_redis() -> JSONResponse:
    if clients.redis is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable", "component": "redis", "detail": "client not initialized"},
        )
    try:
        pong = await clients.redis.ping()
        if not pong:
            raise RuntimeError("redis PING returned falsy response")
        return JSONResponse(content={"status": "ok", "component": "redis"})
    except Exception as exc:
        logger.exception("Redis health check failed")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "error", "component": "redis", "detail": str(exc)},
        )


# Worker health is read off the worker_heartbeats table. The worker writes
# its own heartbeat on every loop iteration; staleness == worker offline.
worker_router = APIRouter(prefix="/worker", tags=["worker"])


@worker_router.get("/health", summary="Background worker liveness")
async def worker_health() -> JSONResponse:
    if clients.db_pool is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "unavailable",
                "component": "worker",
                "detail": "Postgres pool not initialized",
            },
        )
    try:
        async with clients.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT worker_id, last_heartbeat_at, last_job_id,
                       last_job_at, started_at
                FROM worker_heartbeats
                WHERE worker_id NOT LIKE '%' || $1
                ORDER BY last_heartbeat_at DESC
                LIMIT 1
                """,
                _SCHEDULER_WORKER_SUFFIX,
            )
            sched_row = await conn.fetchrow(
                """
                SELECT worker_id, last_heartbeat_at, last_job_at
                FROM worker_heartbeats
                WHERE worker_id LIKE '%' || $1
                ORDER BY last_heartbeat_at DESC
                LIMIT 1
                """,
                _SCHEDULER_WORKER_SUFFIX,
            )
    except Exception as exc:
        logger.exception("worker health check failed")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "error",
                "component": "worker",
                "detail": str(exc),
            },
        )

    if row is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "no-heartbeat",
                "component": "worker",
                "detail": "no worker has reported a heartbeat yet",
            },
        )

    age = (datetime.now(timezone.utc) - row["last_heartbeat_at"]).total_seconds()
    stale = age > WORKER_HEARTBEAT_STALE_SECONDS
    payload = {
        "status": "stale" if stale else "ok",
        "component": "worker",
        "worker_id": row["worker_id"],
        "heartbeat_age_seconds": round(age, 1),
        "stale_threshold_seconds": WORKER_HEARTBEAT_STALE_SECONDS,
        "last_heartbeat_at": row["last_heartbeat_at"].isoformat(),
        "last_job_at": row["last_job_at"].isoformat() if row["last_job_at"] else None,
        "last_job_id": str(row["last_job_id"]) if row["last_job_id"] else None,
        "started_at": row["started_at"].isoformat(),
    }
    if sched_row is None:
        payload["news_scheduler"] = {"status": "no-tick"}
    else:
        sched_age = (
            datetime.now(timezone.utc) - sched_row["last_heartbeat_at"]
        ).total_seconds()
        payload["news_scheduler"] = {
            "status": "stale" if sched_age > NEWS_SCHEDULER_STALE_SECONDS else "ok",
            "last_tick_age_seconds": round(sched_age, 1),
            "stale_threshold_seconds": NEWS_SCHEDULER_STALE_SECONDS,
            "last_tick_at": sched_row["last_heartbeat_at"].isoformat(),
            "last_enqueued_at": (
                sched_row["last_job_at"].isoformat()
                if sched_row["last_job_at"]
                else None
            ),
        }
    return JSONResponse(
        status_code=(
            status.HTTP_503_SERVICE_UNAVAILABLE if stale else status.HTTP_200_OK
        ),
        content=payload,
    )
