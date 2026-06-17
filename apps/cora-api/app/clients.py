import asyncio
import json
import logging
from typing import Optional
from urllib.parse import urlparse

import asyncpg
import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)

# After a host reboot, containers race postgres's crash recovery ("the database
# system is starting up"). Retry pool creation instead of stranding the process
# with db_pool=None for its whole lifetime (observed 2026-06-12).
POOL_INIT_ATTEMPTS = 10
POOL_INIT_RETRY_SECONDS = 3.0


class Clients:
    """Holds shared connection pools for the API lifetime."""

    db_pool: Optional[asyncpg.Pool] = None
    redis: Optional[aioredis.Redis] = None


clients = Clients()


def _safe_dsn_host(dsn: str) -> str:
    try:
        parsed = urlparse(dsn)
        host = parsed.hostname or "<unknown>"
        port = parsed.port
        db = (parsed.path or "").lstrip("/") or "<unknown>"
        user = parsed.username or "<unknown>"
        location = f"{host}:{port}" if port else host
        return f"user={user} host={location} db={db}"
    except Exception:
        return "<unparseable DSN>"


async def _register_json_codecs(conn: asyncpg.Connection) -> None:
    for typename in ("jsonb", "json"):
        await conn.set_type_codec(
            typename,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )


async def init_clients() -> None:
    dsn_info = _safe_dsn_host(settings.database_url)
    logger.info("Initializing Postgres pool (%s)", dsn_info)
    for attempt in range(1, POOL_INIT_ATTEMPTS + 1):
        try:
            clients.db_pool = await asyncpg.create_pool(
                dsn=settings.database_url,
                min_size=1,
                max_size=10,
                command_timeout=10,
                init=_register_json_codecs,
            )
            logger.info("Postgres pool initialized successfully (%s)", dsn_info)
            break
        except Exception as exc:
            clients.db_pool = None
            if attempt < POOL_INIT_ATTEMPTS:
                logger.warning(
                    "Postgres pool init failed (attempt %s/%s, retrying in %ss): %s",
                    attempt, POOL_INIT_ATTEMPTS, POOL_INIT_RETRY_SECONDS, exc,
                )
                await asyncio.sleep(POOL_INIT_RETRY_SECONDS)
            else:
                logger.exception(
                    "Postgres pool initialization failed with exception: %s", exc
                )

    try:
        clients.redis = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        logger.info("Redis client initialized")
    except Exception:
        logger.exception("Failed to initialize Redis client (continuing without it)")
        clients.redis = None


async def close_clients() -> None:
    if clients.db_pool is not None:
        await clients.db_pool.close()
        logger.info("Postgres pool closed")
    if clients.redis is not None:
        await clients.redis.close()
        logger.info("Redis client closed")
