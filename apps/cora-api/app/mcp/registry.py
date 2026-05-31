"""DB-backed registry for MCP servers — list, create, update, cache caps."""

import logging
import uuid
from typing import Any, Optional

from app.clients import clients

from .client import McpClient
from .models import McpCapabilities, McpServerConfig

logger = logging.getLogger(__name__)


# (name, description, server_type, endpoint, auth_type, auth_config, enabled)
_SEED_SERVERS: list[tuple[str, str, str, str, Optional[str], Optional[dict], bool]] = [
    (
        "filesystem",
        "Local filesystem MCP server — exposes file read/list/write tools.",
        "http",
        "http://mcp-filesystem:3000",
        None,
        None,
        True,
    ),
    (
        "postgres",
        "Postgres MCP server — exposes SQL query and schema-introspection tools.",
        "http",
        "http://mcp-postgres:3000",
        None,
        None,
        True,
    ),
    (
        "github",
        "GitHub MCP server — exposes repository, issue, and PR tools.",
        "http",
        "http://mcp-github:3000",
        "bearer",
        {"token": ""},
        True,
    ),
]


async def seed_mcp_servers() -> None:
    """Insert the canonical filesystem/postgres/github rows if absent. Safe to
    re-run; existing rows are left untouched so admin edits persist."""
    if clients.db_pool is None:
        logger.warning("Skipping MCP server seed: Postgres pool unavailable")
        return
    async with clients.db_pool.acquire() as conn:
        for name, description, stype, endpoint, atype, aconfig, enabled in _SEED_SERVERS:
            await conn.execute(
                """
                INSERT INTO mcp_servers
                    (name, description, server_type, endpoint, enabled,
                     auth_type, auth_config)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (name) DO NOTHING
                """,
                name,
                description,
                stype,
                endpoint,
                enabled,
                atype,
                aconfig,
            )
    logger.info("MCP server seed complete (filesystem, postgres, github)")


async def list_servers() -> list[dict]:
    if clients.db_pool is None:
        return []
    async with clients.db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, description, server_type, endpoint, enabled,
                   auth_type, auth_config, capabilities, created_at, updated_at
            FROM mcp_servers
            ORDER BY name ASC
            """
        )
    return [dict(r) for r in rows]


async def get_server_by_name(name: str) -> Optional[dict]:
    if clients.db_pool is None:
        return None
    async with clients.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, name, description, server_type, endpoint, enabled,
                   auth_type, auth_config, capabilities, created_at, updated_at
            FROM mcp_servers
            WHERE name = $1
            """,
            name,
        )
    return dict(row) if row else None


async def create_server(
    *,
    name: str,
    description: Optional[str],
    server_type: str,
    endpoint: str,
    enabled: bool = True,
    auth_type: Optional[str] = None,
    auth_config: Optional[dict] = None,
) -> dict:
    if clients.db_pool is None:
        raise RuntimeError("Postgres pool unavailable")
    async with clients.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO mcp_servers
                (name, description, server_type, endpoint, enabled,
                 auth_type, auth_config)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id, name, description, server_type, endpoint, enabled,
                      auth_type, auth_config, capabilities, created_at, updated_at
            """,
            name,
            description,
            server_type,
            endpoint,
            enabled,
            auth_type,
            auth_config,
        )
    logger.info("mcp server created: name=%s endpoint=%s", name, endpoint)
    return dict(row)


async def update_server(
    name: str,
    *,
    description: Optional[str] = None,
    endpoint: Optional[str] = None,
    enabled: Optional[bool] = None,
    auth_type: Optional[str] = None,
    auth_config: Optional[dict] = None,
    clear_auth: bool = False,
) -> Optional[dict]:
    """Partial update; only fields explicitly passed are written. `clear_auth`
    forces auth_type + auth_config to NULL."""
    if clients.db_pool is None:
        return None
    sets: list[str] = []
    args: list[Any] = []

    def add(col: str, val: Any) -> None:
        args.append(val)
        sets.append(f"{col} = ${len(args)}")

    if description is not None:
        add("description", description)
    if endpoint is not None:
        add("endpoint", endpoint)
    if enabled is not None:
        add("enabled", enabled)
    if clear_auth:
        add("auth_type", None)
        add("auth_config", None)
    else:
        if auth_type is not None:
            add("auth_type", auth_type)
        if auth_config is not None:
            add("auth_config", auth_config)
    if not sets:
        return await get_server_by_name(name)
    sets.append("updated_at = NOW()")
    args.append(name)
    sql = f"""
        UPDATE mcp_servers SET {", ".join(sets)}
        WHERE name = ${len(args)}
        RETURNING id, name, description, server_type, endpoint, enabled,
                  auth_type, auth_config, capabilities, created_at, updated_at
    """
    async with clients.db_pool.acquire() as conn:
        row = await conn.fetchrow(sql, *args)
    if row is None:
        return None
    logger.info("mcp server updated: name=%s fields=%s", name, [s.split(" =")[0] for s in sets])
    return dict(row)


async def store_capabilities(name: str, capabilities: dict) -> None:
    if clients.db_pool is None:
        return
    async with clients.db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE mcp_servers
            SET capabilities = $1, updated_at = NOW()
            WHERE name = $2
            """,
            capabilities,
            name,
        )
    logger.info(
        "mcp capabilities cached: server=%s tools=%s resources=%s",
        name,
        len(capabilities.get("tools", []) or []),
        len(capabilities.get("resources", []) or []),
    )


def config_from_row(row: dict) -> McpServerConfig:
    return McpServerConfig(
        name=row["name"],
        server_type=row["server_type"],
        endpoint=row["endpoint"],
        auth_type=row.get("auth_type"),
        auth_config=row.get("auth_config"),
    )


async def discover_and_cache(name: str) -> Optional[McpCapabilities]:
    """Discover capabilities from the live server and write them to the row.
    Returns the McpCapabilities object, or None if the server isn't found."""
    row = await get_server_by_name(name)
    if row is None:
        return None
    if not row["enabled"]:
        raise RuntimeError(f"server {name!r} is disabled")
    client = McpClient(config_from_row(row))
    cap = await client.discover_capabilities()
    await store_capabilities(name, cap.as_dict())
    return cap
