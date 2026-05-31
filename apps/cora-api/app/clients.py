import json
import logging
from typing import Optional
from urllib.parse import urlparse

import asyncpg
import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)


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
    try:
        clients.db_pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=1,
            max_size=10,
            command_timeout=10,
            init=_register_json_codecs,
        )
        logger.info("Postgres pool initialized successfully (%s)", dsn_info)
    except Exception as exc:
        logger.exception(
            "Postgres pool initialization failed with exception: %s", exc
        )
        clients.db_pool = None

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
