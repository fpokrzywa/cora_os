"""Agent registry: DB-backed governance of agent prompts + routing config.

Seeds the canonical ATLAS / SCRIBE / FORGE rows from the in-code prompts on
first startup, and offers a `get_active_version(name)` helper for runtime
lookup. Runtime callers always treat the DB as best-effort and fall back to
the Python constants if anything goes wrong.
"""

import json
import logging
from typing import Optional

from app.agents.atlas import ATLAS_SYSTEM_PROMPT
from app.agents.forge import (
    FORGE_ALLOWED_TOOLS,
    FORGE_ROUTING_KEYWORDS,
    FORGE_SYSTEM_PROMPT,
    FORGE_TOOL_AWARE_MARKER,
)
from app.agents.pulse import (
    PULSE_ALLOWED_TOOLS,
    PULSE_ROUTING_KEYWORDS,
    PULSE_SYSTEM_PROMPT,
)
from app.agents.signal import (
    SIGNAL_ALLOWED_TOOLS,
    SIGNAL_ROUTING_KEYWORDS,
    SIGNAL_SYSTEM_PROMPT,
)
from app.agents.chronos import (
    CHRONOS_ALLOWED_TOOLS,
    CHRONOS_ROUTING_KEYWORDS,
    CHRONOS_SYSTEM_PROMPT,
)
from app.agents.scribe import SCRIBE_SYSTEM_PROMPT
from app.clients import clients

logger = logging.getLogger(__name__)

# (name, display_name, description, agent_type,
#  system_prompt, routing_keywords, allowed_tools)
_SEED_AGENTS: list[tuple[str, str, str, str, str, list[str], list[str]]] = [
    (
        "ATLAS",
        "ATLAS Orchestrator",
        "Internal routing and orchestration layer. Classifies intent, "
        "selects subagents, builds prompts. Not user-facing.",
        "orchestrator",
        ATLAS_SYSTEM_PROMPT,
        [],
        [],
    ),
    (
        "SCRIBE",
        "SCRIBE Memory Manager",
        "Reads conversations and writes durable memory entries. Also serves "
        "memory back to other agents via keyword search.",
        "memory",
        SCRIBE_SYSTEM_PROMPT,
        [],
        [],
    ),
    (
        "FORGE",
        "FORGE Engineering Specialist",
        "Engineering, build, and devops specialist. Routed to by ATLAS "
        "when the user message is code/infra/error-shaped.",
        "subagent",
        FORGE_SYSTEM_PROMPT,
        list(FORGE_ROUTING_KEYWORDS),
        list(FORGE_ALLOWED_TOOLS),
    ),
    (
        "PULSE",
        "PULSE Research Specialist",
        "Research, information synthesis, and comparative analysis specialist. "
        "Routed to by ATLAS when the user message is research/compare/"
        "summarize-shaped. Grounds answers in injected memory + knowledge.",
        "subagent",
        PULSE_SYSTEM_PROMPT,
        list(PULSE_ROUTING_KEYWORDS),
        list(PULSE_ALLOWED_TOOLS),
    ),
    (
        "SIGNAL",
        "SIGNAL Communication Specialist",
        "Communication and messaging specialist. Routed to by ATLAS when the "
        "user message is communication-shaped (draft/rewrite/summarize emails, "
        "messages, announcements, updates). Drafts content only; sends nothing.",
        "subagent",
        SIGNAL_SYSTEM_PROMPT,
        list(SIGNAL_ROUTING_KEYWORDS),
        list(SIGNAL_ALLOWED_TOOLS),
    ),
    (
        "CHRONOS",
        "CHRONOS Scheduling Specialist",
        "Time, schedule, timeline, and planning specialist. Routed to by ATLAS "
        "when the user message is time-shaped (scheduling, milestones, "
        "deadlines, reminders, meeting prep). Plans only; creates no events.",
        "subagent",
        CHRONOS_SYSTEM_PROMPT,
        list(CHRONOS_ROUTING_KEYWORDS),
        list(CHRONOS_ALLOWED_TOOLS),
    ),
]


async def seed_agents() -> None:
    """Insert canonical agents + v1 versions if not already present.
    Safe to run on every startup."""
    if clients.db_pool is None:
        logger.warning("Skipping agent seed: Postgres pool unavailable")
        return

    async with clients.db_pool.acquire() as conn:
        async with conn.transaction():
            for (
                name,
                display_name,
                description,
                agent_type,
                system_prompt,
                routing_keywords,
                allowed_tools,
            ) in _SEED_AGENTS:
                agent_row = await conn.fetchrow(
                    """
                    INSERT INTO agents (name, display_name, description, agent_type)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (name) DO UPDATE
                        SET display_name = EXCLUDED.display_name,
                            description = EXCLUDED.description,
                            agent_type = EXCLUDED.agent_type,
                            updated_at = NOW()
                    RETURNING id, current_version_id
                    """,
                    name,
                    display_name,
                    description,
                    agent_type,
                )
                agent_id = agent_row["id"]
                existing_version_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM agent_versions WHERE agent_id = $1",
                    agent_id,
                )
                if existing_version_count and existing_version_count > 0:
                    continue
                version_row = await conn.fetchrow(
                    """
                    INSERT INTO agent_versions (
                        agent_id, version_number, status, system_prompt,
                        routing_keywords, allowed_tools, notes,
                        activated_at
                    )
                    VALUES ($1, 1, 'active', $2, $3, $4,
                            'Seeded v1 from Python module', NOW())
                    RETURNING id
                    """,
                    agent_id,
                    system_prompt,
                    routing_keywords,
                    allowed_tools,
                )
                await conn.execute(
                    """
                    UPDATE agents SET current_version_id = $1, updated_at = NOW()
                    WHERE id = $2
                    """,
                    version_row["id"],
                    agent_id,
                )
                logger.info(
                    "agent seeded: name=%s v1 activated", name
                )
    logger.info("Agent registry seed complete")


async def ensure_forge_tool_aware_version() -> None:
    """Lift FORGE off its original tool-suppressing seed prompt onto the tool-aware
    one (it can read the live codebase via its filesystem tools). Idempotent and
    no-clobber: acts ONLY when FORGE's active version is still the pristine seeded v1
    (notes marker) whose prompt predates the rewrite; once the tool-aware version is
    active — or if an operator has edited FORGE via the admin path — it does nothing.
    Adds a NEW active version (preserving history), archiving the old one first to
    respect the one-active-version-per-agent index. Best-effort: never raises."""
    if clients.db_pool is None:
        return
    try:
        async with clients.db_pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT a.id AS agent_id, v.id AS version_id,
                           v.system_prompt, v.notes
                    FROM agents a
                    JOIN agent_versions v ON v.id = a.current_version_id
                    WHERE a.name = 'FORGE'
                    FOR UPDATE OF v
                    """
                )
                if row is None:
                    return
                # Already tool-aware (fresh installs seed the new prompt directly),
                # or operator-customized -> leave untouched.
                if FORGE_TOOL_AWARE_MARKER in (row["system_prompt"] or ""):
                    return
                if (row["notes"] or "") != "Seeded v1 from Python module":
                    return
                next_num = await conn.fetchval(
                    "SELECT COALESCE(MAX(version_number), 0) + 1 "
                    "FROM agent_versions WHERE agent_id = $1",
                    row["agent_id"],
                )
                # Archive the current active version BEFORE inserting the new active
                # one (a partial unique index allows only one active per agent).
                await conn.execute(
                    "UPDATE agent_versions SET status = 'archived', archived_at = NOW() "
                    "WHERE id = $1",
                    row["version_id"],
                )
                new_id = await conn.fetchval(
                    """
                    INSERT INTO agent_versions (
                        agent_id, version_number, status, system_prompt,
                        routing_keywords, allowed_tools, notes, activated_at
                    )
                    VALUES ($1, $2, 'active', $3, $4, $5,
                            'Auto-migrated: tool-aware FORGE (filesystem inspector)', NOW())
                    RETURNING id
                    """,
                    row["agent_id"], next_num, FORGE_SYSTEM_PROMPT,
                    list(FORGE_ROUTING_KEYWORDS), list(FORGE_ALLOWED_TOOLS),
                )
                await conn.execute(
                    "UPDATE agents SET current_version_id = $1, updated_at = NOW() "
                    "WHERE id = $2",
                    new_id, row["agent_id"],
                )
        logger.info("FORGE migrated to tool-aware version v%s", next_num)
    except Exception:
        logger.exception("ensure_forge_tool_aware_version failed (continuing)")


async def load_active_routing_keywords() -> dict[str, list[str]]:
    """Map agent_name -> routing keywords drawn from each enabled agent's active
    version metadata.routing_keywords (non-empty only). Best-effort: returns {}
    on any failure so the router cleanly falls back to Python keyword constants.
    """
    if clients.db_pool is None:
        return {}
    try:
        async with clients.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT a.name, v.metadata
                FROM agents a
                JOIN agent_versions v ON v.id = a.current_version_id
                WHERE a.enabled = TRUE AND v.status = 'active'
                """
            )
    except Exception:
        logger.exception(
            "load_active_routing_keywords failed; routing falls back to "
            "Python keyword constants"
        )
        return {}
    out: dict[str, list[str]] = {}
    for r in rows:
        md = r["metadata"]
        if isinstance(md, str):
            try:
                md = json.loads(md)
            except (TypeError, ValueError):
                md = {}
        if not isinstance(md, dict):
            continue
        kws = md.get("routing_keywords")
        if isinstance(kws, list) and kws:
            cleaned = [str(k).strip() for k in kws if str(k).strip()]
            if cleaned:
                out[r["name"]] = cleaned
    return out


async def get_active_version(agent_name: str) -> Optional[dict]:
    """Return the active version row for an agent (by name), or None.

    Returns None when:
      - the pool is unavailable
      - the agent is missing or disabled
      - no active version exists

    Callers should treat this as best-effort and fall back to Python
    constants if it returns None.
    """
    if clients.db_pool is None:
        return None
    try:
        async with clients.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT v.id, v.agent_id, v.version_number, v.status,
                       v.system_prompt, v.routing_keywords, v.allowed_tools,
                       v.model_name, v.temperature, v.max_prompt_chars,
                       v.notes, v.activated_at, v.archived_at,
                       a.name AS agent_name, a.enabled AS agent_enabled
                FROM agents a
                JOIN agent_versions v ON v.id = a.current_version_id
                WHERE a.name = $1
                  AND a.enabled = TRUE
                  AND v.status = 'active'
                """,
                agent_name,
            )
    except Exception:
        logger.exception(
            "agent registry lookup failed for %s; caller should fall back",
            agent_name,
        )
        return None
    if row is None:
        return None
    return dict(row)
