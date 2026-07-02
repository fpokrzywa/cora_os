"""Workspace registry — top-level grouping over chats/memory/plans/jobs/traces.

A workspace is a thin envelope: every resource that supports workspaces
carries an optional `workspace_id`. NULL means "unassigned / legacy /
visible across workspaces". Chat-driven creation defaults to the seeded
'cora-ai-os' workspace when no explicit workspace is provided.
"""

import logging
import re
import uuid
from typing import Optional

from app.clients import clients

logger = logging.getLogger(__name__)

DEFAULT_WORKSPACE_SLUG = "cora-ai-os"


class WorkspaceError(Exception):
    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


_SELECT_COLS = """
    id, owner_user_id, name, slug, description, status, created_at, updated_at
"""

_SLUG_PATTERN = re.compile(r"[^a-z0-9-]+")


def make_slug(name: str) -> str:
    base = _SLUG_PATTERN.sub("-", name.lower()).strip("-")
    return base or "workspace"


async def get_default_workspace() -> Optional[dict]:
    if clients.db_pool is None:
        return None
    async with clients.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_SELECT_COLS} FROM workspaces WHERE slug = $1",
            DEFAULT_WORKSPACE_SLUG,
        )
    return dict(row) if row else None


async def resolve_workspace_id(
    explicit: Optional[uuid.UUID],
) -> Optional[uuid.UUID]:
    """If explicit ID provided, return it. Else fall back to the default
    workspace's id. Returns None only if neither exists (pool down)."""
    if explicit is not None:
        return explicit
    default = await get_default_workspace()
    return default["id"] if default else None


async def list_workspaces(
    *, include_archived: bool = False
) -> list[dict]:
    if clients.db_pool is None:
        return []
    async with clients.db_pool.acquire() as conn:
        if include_archived:
            rows = await conn.fetch(
                f"SELECT {_SELECT_COLS} FROM workspaces ORDER BY created_at ASC"
            )
        else:
            rows = await conn.fetch(
                f"SELECT {_SELECT_COLS} FROM workspaces "
                "WHERE status = 'active' ORDER BY created_at ASC"
            )
    return [dict(r) for r in rows]


async def get_workspace(workspace_id: uuid.UUID) -> Optional[dict]:
    if clients.db_pool is None:
        return None
    async with clients.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_SELECT_COLS} FROM workspaces WHERE id = $1",
            workspace_id,
        )
    return dict(row) if row else None


async def create_workspace(
    *,
    name: str,
    slug: Optional[str] = None,
    description: Optional[str] = None,
    owner_user_id: Optional[uuid.UUID] = None,
) -> dict:
    if clients.db_pool is None:
        raise WorkspaceError("Postgres pool unavailable", code="unavailable")
    final_slug = (slug or make_slug(name)).strip().lower()
    if not final_slug:
        raise WorkspaceError("slug is empty", code="invalid")
    async with clients.db_pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                f"""
                INSERT INTO workspaces (name, slug, description, owner_user_id)
                VALUES ($1, $2, $3, $4)
                RETURNING {_SELECT_COLS}
                """,
                name,
                final_slug,
                description,
                owner_user_id,
            )
        except Exception as exc:
            # asyncpg.UniqueViolationError surfaces as a subclass; treat
            # uniqueness specifically by code lookup.
            if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
                raise WorkspaceError(
                    f"slug {final_slug!r} already exists",
                    code="conflict",
                ) from exc
            raise
    logger.info("workspace created: id=%s slug=%s", row["id"], final_slug)
    return dict(row)


async def update_workspace(
    workspace_id: uuid.UUID,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    status_value: Optional[str] = None,
) -> Optional[dict]:
    if clients.db_pool is None:
        return None
    sets: list[str] = []
    args: list = []

    def add(col: str, val) -> None:
        args.append(val)
        sets.append(f"{col} = ${len(args)}")

    if name is not None:
        add("name", name)
    if description is not None:
        add("description", description)
    if status_value is not None:
        if status_value not in {"active", "archived"}:
            raise WorkspaceError(
                f"invalid status {status_value!r}", code="invalid"
            )
        add("status", status_value)
    if not sets:
        return await get_workspace(workspace_id)
    sets.append("updated_at = NOW()")
    args.append(workspace_id)
    sql = (
        f"UPDATE workspaces SET {', '.join(sets)} "
        f"WHERE id = ${len(args)} RETURNING {_SELECT_COLS}"
    )
    async with clients.db_pool.acquire() as conn:
        row = await conn.fetchrow(sql, *args)
    return dict(row) if row else None


async def get_chat_context(
    workspace_id: uuid.UUID, *, max_chars: int = 1500
) -> Optional[dict]:
    """Compact workspace snapshot for injection into chat prompts.

    Returns a dict with `text` (the formatted block, ≤ max_chars) and
    `metadata` (the structured fields). Returns None on any failure or if
    the workspace doesn't exist."""
    if clients.db_pool is None:
        return None
    try:
        ws = await get_workspace(workspace_id)
        if ws is None:
            return None
        async with clients.db_pool.acquire() as conn:
            memory_total = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM memory_entries WHERE workspace_id = $1",
                    workspace_id,
                ) or 0
            )
            # Embedded count — handle both pgvector and JSONB fallback columns
            from app import schema as _schema  # local import to avoid cycle
            column = (
                "embedding" if _schema.is_pgvector_available() else "embedding_json"
            )
            memory_embedded = int(
                await conn.fetchval(
                    f"SELECT COUNT(*) FROM memory_entries "
                    f"WHERE workspace_id = $1 AND {column} IS NOT NULL",
                    workspace_id,
                ) or 0
            )
            plans_active = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM execution_plans "
                    "WHERE workspace_id = $1 AND status IN ('planned','running')",
                    workspace_id,
                ) or 0
            )
            jobs_active = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM jobs "
                    "WHERE workspace_id = $1 AND status IN ('queued','running')",
                    workspace_id,
                ) or 0
            )
            agent_rows = await conn.fetch(
                "SELECT name FROM agents WHERE enabled = TRUE ORDER BY name ASC"
            )
            tool_rows = await conn.fetch(
                "SELECT name FROM tools WHERE enabled = TRUE ORDER BY name ASC"
            )
            mcp_rows = await conn.fetch(
                "SELECT name FROM mcp_servers WHERE enabled = TRUE ORDER BY name ASC"
            )
    except Exception:
        logger.exception(
            "workspace chat context fetch failed: workspace_id=%s", workspace_id
        )
        return None

    agents = [r["name"] for r in agent_rows]
    tools = [r["name"] for r in tool_rows]
    mcps = [r["name"] for r in mcp_rows]
    description = (ws["description"] or "").strip()

    lines = [
        "Current Workspace Context:",
        f"Workspace: {ws['name']} ({ws['slug']})",
    ]
    if description:
        # Budget the description so we don't blow past max_chars on long ones.
        if len(description) > 280:
            description = description[:280].rstrip() + "…"
        lines.append(f"Description: {description}")
    # Bucketed, not exact: this block sits near the top of EVERY chat prompt,
    # and vLLM's automatic prefix caching only reuses the KV cache up to the
    # first changed byte — exact counts (jobs_active flips on every scheduled
    # job) invalidated the cached prefix all day. Buckets keep the line
    # byte-stable; the model never needed exact counts here.
    def _approx(n: int) -> str:
        return "<10" if n < 10 else f"~{round(n, -1)}"

    lines.append(
        f"Memory: {_approx(memory_total)} entries ({_approx(memory_embedded)} embedded). "
        f"Active plans: {_approx(plans_active)}. Active jobs: {_approx(jobs_active)}."
    )
    if agents:
        lines.append(f"Agents available: {', '.join(agents)}")
    if tools:
        lines.append(f"Tools available: {', '.join(tools)}")
    if mcps:
        lines.append(f"MCP servers: {', '.join(mcps)}")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return {
        "text": text,
        "metadata": {
            "workspace_id": str(workspace_id),
            "workspace_name": ws["name"],
            "workspace_slug": ws["slug"],
            "memory_total": memory_total,
            "memory_embedded": memory_embedded,
            "plans_active": plans_active,
            "jobs_active": jobs_active,
            "agents": agents,
            "tools": tools,
            "mcp_servers": mcps,
            "chars": len(text),
        },
    }


async def workspace_counts(workspace_id: uuid.UUID) -> dict:
    """Cheap counts for the detail view. All best-effort."""
    if clients.db_pool is None:
        return {}
    queries = {
        "conversations": "SELECT COUNT(*) FROM conversations WHERE workspace_id = $1",
        "messages": "SELECT COUNT(*) FROM messages WHERE workspace_id = $1",
        "memory_entries": "SELECT COUNT(*) FROM memory_entries WHERE workspace_id = $1",
        "execution_plans": "SELECT COUNT(*) FROM execution_plans WHERE workspace_id = $1",
        "jobs": "SELECT COUNT(*) FROM jobs WHERE workspace_id = $1",
        "runtime_traces": "SELECT COUNT(*) FROM runtime_traces WHERE workspace_id = $1",
    }
    results: dict[str, int] = {}
    async with clients.db_pool.acquire() as conn:
        for key, sql in queries.items():
            try:
                results[key] = int(await conn.fetchval(sql, workspace_id) or 0)
            except Exception:
                logger.exception("workspace count failed: key=%s", key)
                results[key] = 0
    return results
