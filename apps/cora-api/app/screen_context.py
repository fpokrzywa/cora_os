"""Screen Context Awareness v0.1 — make chat aware of what the user is viewing.

The UI sends a small `screen_context` object with each /chat request:

    {"view": "admin-console", "section": "tools/integration-readiness",
     "label": "Integration Readiness", "last_section": "knowledge/news",
     "entity": {"type": "communication_draft", "id": "<uuid>"}}

Nothing the client sends is trusted as content: the view/section/label strings
are sanitized + length-capped and only used as identifiers, and the optional
entity is re-resolved SERVER-SIDE (owner-scoped; admins see all) from its id.
The result is a compact "Current screen" block injected into the LLM prompt
between the workspace context and the memory block, so questions like "what am
I looking at?" or "summarize this draft" answer from the same records the
screen renders. Read-only — resolvers SELECT only; no token/secret columns are
ever selected.
"""

import logging
import re
import uuid
from typing import Optional

from app.clients import clients

logger = logging.getLogger(__name__)

MAX_BLOCK_CHARS = 1200
_MAX_FIELD_CHARS = 120
_PREVIEW_CHARS = 280
_SAFE_RE = re.compile(r"[^\w\s\-/:.,()&]")

# Human descriptions for known screens (section keys match the Admin Console
# tab/sub keys the UI reports). Unknown sections still inject the identifier.
SCREEN_DESCRIPTIONS = {
    "chat": "the main Cora chat view",
    "overview": "the Admin Console overview dashboard",
    "users/users": "the Admin Console user management screen",
    "agents/agents": "the agent registry with versioned prompts",
    "agents/signal-drafts": "the SIGNAL communication drafts review queue (internal drafts, never sent)",
    "agents/chronos-proposals": "the CHRONOS schedule proposals review queue (internal proposals, never scheduled)",
    "tools/tooling": "the governed tool registry",
    "tools/governance": "tool execution policies and audit logs",
    "tools/mcp": "the MCP server registry",
    "tools/integrations": "the Integration Readiness screen (dry-run-only provider intents)",
    "tools/integration-queue": "the Integration Readiness queue (dry-run-only provider intents)",
    "tools/approval-console": "the Human Approval Execution Console (approval ≠ execution)",
    "tools/execution-runbook": "the Execution Runbook and final safety interlock",
    "tools/feature-flags": "the provider execution feature-flag matrix (fail-closed)",
    "tools/execution-governance": "the execution governance observability dashboard",
    "tools/providers": "the provider connector registry",
    "tools/provider-connectors": "the provider OAuth connections screen",
    "tools/credentials": "the OAuth credential vault (readiness-only)",
    "knowledge/knowledge": "the knowledge ingestion screen (manual/bulk/upload/URL/news feeds + briefing)",
    "knowledge/context": "the workspace context preview",
    "execution/plans": "execution plans (template-based, no auto-execution)",
    "execution/jobs": "the background job queue",
    "execution/delegations": "the agent delegation timeline",
    "execution/traces": "the runtime trace log",
    "workspaces/workspaces": "workspace management",
}


def _clean(value, limit: int = _MAX_FIELD_CHARS) -> str:
    s = _SAFE_RE.sub("", str(value or "")).strip()
    return s[:limit]


def _trunc(value, limit: int = _PREVIEW_CHARS) -> str:
    s = str(value or "").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def parse_screen_context(raw) -> Optional[dict]:
    """Sanitize the client-supplied context. Returns None when unusable."""
    if not isinstance(raw, dict):
        return None
    view = _clean(raw.get("view"))
    if not view:
        return None
    out: dict = {"view": view}
    for key in ("section", "label", "last_section"):
        v = _clean(raw.get(key))
        if v:
            out[key] = v
    for field in ("entity", "last_entity"):
        entity = raw.get(field)
        if isinstance(entity, dict):
            etype = _clean(entity.get("type"), 60)
            try:
                eid = uuid.UUID(str(entity.get("id")))
            except (ValueError, TypeError):
                eid = None
            if etype and eid:
                out[field] = {"type": etype, "id": eid}
    return out


# --------------------------------------------------------------------------- #
# Entity resolvers — every query is owner-scoped (admin sees all), SELECTs
# summary columns only, and never touches token/secret columns.
# --------------------------------------------------------------------------- #

def _owner(is_admin: bool, column: str) -> str:
    return "TRUE" if is_admin else f"{column} = $2"


async def _resolve_draft(conn, eid, user_id, is_admin):
    row = await conn.fetchrow(
        f"SELECT subject, recipient_hint, status, body, created_at "
        f"FROM communication_drafts WHERE id = $1 AND {_owner(is_admin, 'created_by')}",
        *([eid] if is_admin else [eid, user_id]))
    if not row:
        return None
    return ("a SIGNAL communication draft (internal-only, never sent)", [
        f"subject: {_trunc(row['subject'], 120)}",
        f"recipient hint: {_trunc(row['recipient_hint'], 120)}",
        f"status: {row['status']}",
        f"body preview: {_trunc(row['body'])}",
    ])


async def _resolve_proposal(conn, eid, user_id, is_admin):
    row = await conn.fetchrow(
        f"SELECT title, start_time, end_time, status, description "
        f"FROM schedule_proposals WHERE id = $1 AND {_owner(is_admin, 'created_by')}",
        *([eid] if is_admin else [eid, user_id]))
    if not row:
        return None
    return ("a CHRONOS schedule proposal (internal-only, never scheduled)", [
        f"title: {_trunc(row['title'], 120)}",
        f"start: {row['start_time']} · end: {row['end_time']}",
        f"status: {row['status']}",
        f"description preview: {_trunc(row['description'])}",
    ])


async def _resolve_intent(conn, eid, user_id, is_admin):
    row = await conn.fetchrow(
        f"SELECT provider_type, action_type, status, provider_name, dry_run, "
        f"validation_result FROM external_integration_intents "
        f"WHERE id = $1 AND {_owner(is_admin, 'created_by')}",
        *([eid] if is_admin else [eid, user_id]))
    if not row:
        return None
    vr = row["validation_result"]
    validation = vr.get("status") if isinstance(vr, dict) else None
    return ("an external integration intent (dry-run-only; execution disabled)", [
        f"provider: {row['provider_name'] or row['provider_type']}",
        f"action: {row['action_type']}",
        f"status: {row['status']}",
        f"validation: {validation or 'n/a'}",
        f"dry run: {row['dry_run']}",
    ])


async def _resolve_knowledge_source(conn, eid, user_id, is_admin):
    row = await conn.fetchrow(
        f"SELECT title, source_type, source_url, status "
        f"FROM knowledge_sources WHERE id = $1 AND ({_owner(is_admin, 'uploaded_by')} "
        f"OR uploaded_by IS NULL)",
        *([eid] if is_admin else [eid, user_id]))
    if not row:
        return None
    return ("a knowledge source", [
        f"title: {_trunc(row['title'], 120)}",
        f"type: {row['source_type']}",
        f"url: {_trunc(row['source_url'], 120)}",
        f"status: {row['status']}",
    ])


async def _resolve_job(conn, eid, user_id, is_admin):
    row = await conn.fetchrow(
        f"SELECT job_type, status, attempts, max_attempts, error_message "
        f"FROM jobs WHERE id = $1 AND {_owner(is_admin, 'user_id')}",
        *([eid] if is_admin else [eid, user_id]))
    if not row:
        return None
    return ("a background job", [
        f"type: {row['job_type']}",
        f"status: {row['status']}",
        f"attempts: {row['attempts']}/{row['max_attempts']}",
        f"error: {_trunc(row['error_message'], 160) or 'none'}",
    ])


async def _resolve_plan(conn, eid, user_id, is_admin):
    row = await conn.fetchrow(
        f"SELECT title, status, current_step, total_steps "
        f"FROM execution_plans WHERE id = $1 AND {_owner(is_admin, 'user_id')}",
        *([eid] if is_admin else [eid, user_id]))
    if not row:
        return None
    return ("an execution plan (template-based, no auto-execution)", [
        f"title: {_trunc(row['title'], 120)}",
        f"status: {row['status']}",
        f"step: {row['current_step']}/{row['total_steps']}",
    ])


_RESOLVERS = {
    "communication_draft": _resolve_draft,
    "schedule_proposal": _resolve_proposal,
    "integration_intent": _resolve_intent,
    "knowledge_source": _resolve_knowledge_source,
    "job": _resolve_job,
    "execution_plan": _resolve_plan,
}


async def build_screen_context_block(
    raw, *, user_id: uuid.UUID, is_admin: bool,
) -> Optional[tuple[str, dict]]:
    """Build the prompt block + metadata from a raw client screen context.
    Returns None when there is nothing usable. Never raises."""
    try:
        ctx = parse_screen_context(raw)
        if ctx is None:
            return None
        section = ctx.get("section") or ctx.get("view")
        desc = SCREEN_DESCRIPTIONS.get(section)
        label = ctx.get("label") or section
        lines = ["## Current screen"]
        if desc:
            lines.append(f"The user is currently on {desc} ({label}).")
        else:
            lines.append(f"The user is currently on the '{label}' screen.")
        if ctx.get("last_section") and ctx["last_section"] != section:
            last_desc = SCREEN_DESCRIPTIONS.get(ctx["last_section"])
            lines.append(
                "Before this they were viewing "
                + (last_desc or f"the '{ctx['last_section']}' screen") + "."
            )

        meta: dict = {"view": ctx["view"], "section": section,
                      "entity_resolved": False}
        # Prefer the entity currently open; fall back to the one the user most
        # recently had open (they must leave the panel to reach the chat box).
        entity = ctx.get("entity") or ctx.get("last_entity")
        currently_open = ctx.get("entity") is not None
        if entity and clients.db_pool is not None:
            resolver = _RESOLVERS.get(entity["type"])
            if resolver:
                async with clients.db_pool.acquire() as conn:
                    resolved = await resolver(conn, entity["id"], user_id, is_admin)
                if resolved:
                    what, fields = resolved
                    lead = "They have" if currently_open else "They most recently had"
                    lines.append(f"{lead} {what} open:")
                    lines.extend(f"- {f}" for f in fields)
                    meta["entity_resolved"] = True
                    meta["entity_type"] = entity["type"]
                    meta["entity_id"] = str(entity["id"])

        block = "\n".join(lines)
        if len(block) > MAX_BLOCK_CHARS:
            block = block[: MAX_BLOCK_CHARS - 1] + "…"
        meta["chars"] = len(block)
        return block, meta
    except Exception:
        logger.exception("screen context build failed (ignored)")
        return None
