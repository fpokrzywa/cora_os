import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Annotated, AsyncIterator

from app.agents.delegations import (
    DelegationError,
    complete_delegation,
    create_delegation,
    fail_delegation,
)
from app.agents.forge import (
    FORGE_SYSTEM_PROMPT,
    NAME as FORGE_NAME,
)
from app.agents.pulse import (
    PULSE_SYSTEM_PROMPT,
    NAME as PULSE_NAME,
)
from app.agents.signal import (
    SIGNAL_SYSTEM_PROMPT,
    NAME as SIGNAL_NAME,
)
from app.agents.chronos import (
    CHRONOS_SYSTEM_PROMPT,
    NAME as CHRONOS_NAME,
)
from app.agents.routing import select_subagent, semantic_route
from app.agents.planner import create_plan, match_plan_intent
from app.agents.registry import get_active_version, load_active_routing_keywords
from app.memory import (
    detect_ambiguous_recall,
    disambiguation_instruction,
    is_embedding_configured,
    semantic_search,
)
from app import llm
from app.runtime_traces import write_trace
from app.screen_context import build_screen_context_block
from app.speakable import SPEAKABLE_STYLE, to_speakable
from app import schema as schema_state
from app.agents.scribe import (
    create_chat_memory,
    delete_memory_entry,
    extract_memories_from_conversation,
    fetch_memory_for_mutation,
    match_chat_memory_intent,
    resolve_memory_id_prefix,
    search_memory,
    title_from_text,
    update_memory_content,
)
from app.auth import CurrentUser, get_current_user
from app.clients import clients
from app.config import settings
from app.clock import current_datetime_preamble
from app.conversation_titles import generate_title
from app.tools import dispatch_tool
from app.tools.governance import (
    check_permission,
    enforce_external_action_block,
    log_execution_attempt,
)
from app.workspaces import get_chat_context, resolve_workspace_id
from app import signal_tools, chronos_tools
from app import chat_email_lifecycle as chat_email
from app import chat_governance
from app import chat_provider_simulation
from app import chat_approval_queue
from app import chat_inbox
from app import chat_calendar
from app import chat_briefing
from app import chat_scheduling
from app import provider_defaults
from app import screen_vision
from app import agent_runtime
from app.jobs import JobError, create_job

MEMORY_SEARCH_LIMIT = 20
MEMORIES_IN_PROMPT = 5
# Cap each injected memory's content so one long entry (e.g. a whole pasted
# conversation saved as one memory) can't bloat the prompt and slow prefill.
MEMORY_CONTENT_CHARS = 400
# Reciprocal Rank Fusion constant for merging semantic + keyword recall. The
# standard k=60 keeps any single list's top item from dominating, so a strong
# keyword hit (e.g. an exact nickname) co-ranks with the top semantic hits.
RRF_K = 60

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

PERSONA_NAME = "Cora"
ORCHESTRATOR_NAME = "ATLAS"

HISTORY_LIMIT = 10
MAX_PROMPT_CHARS = 16000
CHARS_PER_TOKEN_ESTIMATE = 4

CORA_SYSTEM_PROMPT = (
    "You are Cora, an AI assistant and AI operating system. You speak directly "
    "to the user as Cora. Always address the user as \"you\" and refer to their "
    "life in the second person; NEVER speak in the first person as if you were the "
    "user. Stored facts about the user are written in the third person (\"the "
    "user's wife is Dorothy\") — when you answer, convert them to \"you\"/\"your\" "
    "(say \"Your wife is Dorothy,\" never \"My wife is Dorothy\" or \"We have…\").\n\n"
    "Internally, your orchestration and routing layer is called ATLAS. ATLAS "
    "classifies intent, manages routing, decides whether memory, tools, n8n "
    "workflows, or specialist agents (FORGE for code, SCRIBE for writing, "
    "PULSE for research, SIGNAL for communication, CHRONOS for scheduling) "
    "are needed, constructs prompts, coordinates execution, and returns the "
    "final output through you, Cora.\n\n"
    "ATLAS is internal architecture: never introduce yourself as ATLAS or say "
    "things like \"I am ATLAS.\" Only mention ATLAS when the user explicitly "
    "asks how Cora works under the hood.\n\n"
    "Respond like a person in conversation: answer ONLY what was asked, in about "
    "as many words as a person would naturally use, then stop. Do not restate the "
    "question, pad with unrequested details, or recite stored facts. For a simple "
    "factual question give a one-sentence answer (e.g. \"Who is Sam?\" -> \"Sam is "
    "your brother.\") instead of repeating everything you know. Offer more only if "
    "the user asks. Use prior conversation only for context.\n\n"
    "You cannot save, store, update, or recall long-term memories on your own. "
    "NEVER claim you have remembered, saved, stored, or noted something — the "
    "memory subsystem performs saves and replies with an explicit \"Saved …\" "
    "confirmation, separate from you. If the user asks you to remember something "
    "and you did not just see such a confirmation, tell them to say \"remember "
    "that …\" or \"save this to memory\" so it is actually stored; do not pretend "
    "it was saved.\n\n"
    "When you draft an email on the user's behalf, do NOT sign it with an internal "
    "name (Cora, ATLAS, SIGNAL, or any agent name). If you add a closing, sign with "
    "the user's own name; the system appends the correct signature."
)

# Specialist subagents ATLAS can route to, with their Python-constant fallback
# prompts. At runtime the active DB version (via get_active_version) is preferred
# and these are the fallback if the registry lookup fails.
SUBAGENT_PROMPTS: dict[str, str] = {
    FORGE_NAME: FORGE_SYSTEM_PROMPT,
    PULSE_NAME: PULSE_SYSTEM_PROMPT,
    SIGNAL_NAME: SIGNAL_SYSTEM_PROMPT,
    CHRONOS_NAME: CHRONOS_SYSTEM_PROMPT,
}


async def resolve_agent_prompt(
    agent_name: str,
) -> tuple[str, str, Optional[int]]:
    """Resolve (system_prompt, prompt_source, active_version_number) for an
    agent: prefer the DB active version, fall back to the Python constant
    (SUBAGENT_PROMPTS) or CORA_SYSTEM_PROMPT. Shared by /chat and the admin test
    harness so the two never drift. The DB lookup is skipped for the bare
    persona (Cora), preserving the existing chat hot-path behavior.
    """
    system_prompt = SUBAGENT_PROMPTS.get(agent_name, CORA_SYSTEM_PROMPT)
    prompt_source = "python_constant"
    active_version: Optional[int] = None
    if agent_name and agent_name != PERSONA_NAME:
        try:
            db_version = await get_active_version(agent_name)
        except Exception:
            logger.exception(
                "agent registry lookup raised for %s; using Python fallback",
                agent_name,
            )
            db_version = None
        if db_version and db_version.get("system_prompt"):
            system_prompt = db_version["system_prompt"]
            active_version = db_version["version_number"]
            prompt_source = f"db_agent_version:v{db_version['version_number']}"
    return system_prompt, prompt_source, active_version


async def _load_recent_history(
    session_uuid: uuid.UUID,
    scope_type: str,
    scope_id: Optional[uuid.UUID],
    limit: int = HISTORY_LIMIT,
) -> list[dict]:
    if clients.db_pool is None:
        logger.warning(
            "history load skipped session=%s: Postgres pool unavailable",
            session_uuid,
        )
        return []

    sql = (
        "SELECT role, content, created_at "
        "FROM messages "
        "WHERE session_id = $1 "
        "  AND scope_type = $2 "
        "  AND scope_id IS NOT DISTINCT FROM $3 "
        "ORDER BY created_at ASC "
        "LIMIT $4"
    )

    logger.info(
        "history query: param_session_id=%s scope_type=%s scope_id=%s limit=%s",
        session_uuid,
        scope_type,
        scope_id,
        limit,
    )

    async with clients.db_pool.acquire() as conn:
        rows = await conn.fetch(sql, session_uuid, scope_type, scope_id, limit)

    logger.info(
        "history query result: param_session_id=%s scope_type=%s scope_id=%s "
        "rows_returned=%s",
        session_uuid,
        scope_type,
        scope_id,
        len(rows),
    )

    return [
        {
            "role": row["role"],
            "content": row["content"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


async def _existing_user_memory_texts(user_id: uuid.UUID) -> set[str]:
    """Normalized contents of the user's existing memories, so re-running an extract
    ('store all of this' twice) doesn't create duplicate rows."""
    if clients.db_pool is None:
        return set()
    async with clients.db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT content FROM memory_entries "
            "WHERE scope_type = 'user' AND scope_id = $1",
            user_id,
        )
    return {" ".join((r["content"] or "").split()).lower() for r in rows}


async def _verify_session_ownership(
    session_uuid: uuid.UUID, user_id: uuid.UUID
) -> None:
    """If the conversation row exists, ensure it's owned by this user.
    No-op when the conversation doesn't exist yet (fresh session)."""
    if clients.db_pool is None:
        return
    async with clients.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT scope_type, scope_id FROM conversations WHERE session_id = $1",
            session_uuid,
        )
    if row is None:
        return
    if row["scope_type"] != "user" or row["scope_id"] != user_id:
        logger.warning(
            "session ownership rejected: session=%s requester=%s "
            "owner_scope_type=%s owner_scope_id=%s",
            session_uuid,
            user_id,
            row["scope_type"],
            row["scope_id"],
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="session belongs to another scope",
        )


def _role_label(role: str) -> str:
    if role == "user":
        return "User"
    if role == "assistant":
        return PERSONA_NAME
    if role == "system":
        return "System"
    return role.capitalize()


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN_ESTIMATE)


def _rank_fuse_memories(
    semantic_rows: list[dict], keyword_rows: list[dict], *, limit: int
) -> list[dict]:
    """Merge the two ranked recall lists by Reciprocal Rank Fusion (RRF), dedup by
    id, and return the top `limit`. RRF score = Σ 1/(RRF_K + rank) across the lists a
    row appears in, so (a) a memory BOTH searches surface ranks highest, and (b) a
    strong keyword hit the embedding ranks low still co-ranks with the top semantic
    hits — instead of being appended after every semantic row and cut by the cap
    (the bug where an exact nickname match was never injected). Stable on ties."""
    scores: dict[str, float] = {}
    rows_by_id: dict[str, dict] = {}
    for ranked in (semantic_rows, keyword_rows):
        for rank, row in enumerate(ranked):
            key = str(row.get("id"))
            if not key or key == "None":
                continue
            rows_by_id.setdefault(key, row)
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
    ordered = sorted(scores, key=lambda k: scores[k], reverse=True)
    return [rows_by_id[k] for k in ordered[:limit]]


def _format_memory_block(memories: list[dict]) -> str:
    lines = [
        "Background knowledge about the user (these facts are ABOUT the user — refer "
        "to them as \"you\"/\"your\", never as \"I\"/\"my\"/\"we\"; use ONLY what the "
        "question needs; do NOT recite, list, or quote these entries, and do not "
        "mention that you have memory):"
    ]
    for m in memories:
        # Chunk-sourced rows carry source metadata for citation/grounding.
        cite = ""
        src_title = m.get("source_title")
        src_url = m.get("source_url")
        if m.get("via_chunk"):
            bits = []
            if src_title and src_title != m.get("title"):
                bits.append(f"source: {src_title}")
            if m.get("source_type"):
                bits.append(str(m["source_type"]))
            if src_url:
                bits.append(src_url)
            if m.get("chunk_index") is not None:
                bits.append(f"chunk #{m['chunk_index']}")
            if bits:
                cite = f" ({' · '.join(bits)})"
        content = m["content"]
        if len(content) > MEMORY_CONTENT_CHARS:
            content = content[:MEMORY_CONTENT_CHARS].rstrip() + "…"
        lines.append(f"- [{m['title']}]{cite} {content}")
    # Same-title / different-content recall is genuinely ambiguous (the user has
    # two competing values for one fact). Append a one-line instruction so the
    # assistant asks a single clarifying question instead of guessing — reads as a
    # spoken "which one?" in voice mode. High-precision: fires only on a real
    # collision, so it's a no-op for ordinary recall.
    ambiguous = detect_ambiguous_recall(memories)
    if ambiguous:
        lines.append(disambiguation_instruction(ambiguous))
    return "\n".join(lines)


def _build_prompt(
    history: list[dict],
    user_message: str,
    memories: Optional[list[dict]] = None,
    system_prompt: str = CORA_SYSTEM_PROMPT,
    workspace_context: Optional[str] = None,
    web_results: Optional[str] = None,
    screen_context: Optional[str] = None,
    datetime_line: Optional[str] = None,
) -> tuple[str, dict]:
    """Build the Ollama prompt and return it with size stats.

    History arrives oldest-first. We always keep the system prompt, any
    workspace context, any web-search results, any memory block, and the
    current user turn; if total size would exceed MAX_PROMPT_CHARS, drop the
    OLDEST history lines first.
    """
    separator = "\n\n"
    system_part = f"System: {system_prompt}"
    workspace_part = workspace_context if workspace_context else None
    screen_part = screen_context if screen_context else None
    web_part = web_results if web_results else None
    memory_part = _format_memory_block(memories) if memories else None
    # The live date/time goes right before the user turn — NOT at the front — so it
    # doesn't change the cached prompt prefix every minute (which would force a full
    # cold prefill on the model every request). Stable prefix => prompt-cache hits.
    datetime_part = datetime_line if datetime_line else None
    final_user_part = f"User: {user_message}"
    cue = f"{PERSONA_NAME}:"

    history_lines = [f"{_role_label(m['role'])}: {m['content']}" for m in history]

    fixed_parts = [system_part]
    if workspace_part:
        fixed_parts.append(workspace_part)
    if screen_part:
        fixed_parts.append(screen_part)
    if web_part:
        fixed_parts.append(web_part)
    if memory_part:
        fixed_parts.append(memory_part)
    if datetime_part:
        fixed_parts.append(datetime_part)
    fixed_parts.extend([final_user_part, cue])

    fixed_size = sum(len(p) for p in fixed_parts) + (
        (len(fixed_parts) - 1) * len(separator)
    )
    available = MAX_PROMPT_CHARS - fixed_size

    included: list[str] = []
    running = 0
    for line in reversed(history_lines):
        addition = len(line) + len(separator)
        if running + addition > available:
            break
        included.append(line)
        running += addition
    included.reverse()
    dropped = len(history_lines) - len(included)

    assembled_parts = [system_part]
    if workspace_part:
        assembled_parts.append(workspace_part)
    if screen_part:
        assembled_parts.append(screen_part)
    if web_part:
        assembled_parts.append(web_part)
    if memory_part:
        assembled_parts.append(memory_part)
    assembled_parts.extend(included)
    if datetime_part:
        assembled_parts.append(datetime_part)
    assembled_parts.extend([final_user_part, cue])
    prompt = separator.join(assembled_parts)

    stats = {
        "chars": len(prompt),
        "est_tokens": _estimate_tokens(prompt),
        "history_included": len(included),
        "history_dropped": dropped,
        "truncated": dropped > 0,
        "memories_included": len(memories) if memories else 0,
        "workspace_context_chars": len(workspace_part) if workspace_part else 0,
        "screen_context_chars": len(screen_part) if screen_part else 0,
        "web_results_chars": len(web_part) if web_part else 0,
    }
    return prompt, stats


def _parse_uuid_or_none(value: str) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        return None


def _memory_visible_to(record: dict, user_id: uuid.UUID) -> bool:
    """Mirror /memory visibility: global + own user-scoped + legacy NULL."""
    if record["scope_type"] == "global":
        return True
    if record["scope_type"] == "user":
        return record["scope_id"] == user_id or record["scope_id"] is None
    return False


def _can_mutate_memory(record: dict, *, user_id: uuid.UUID, is_admin: bool) -> bool:
    """Owner of a user-scope row can mutate. Global/system/legacy-null require admin."""
    if is_admin:
        return True
    if record["scope_type"] == "user" and record["scope_id"] == user_id:
        return True
    return False


# ---------- FORGE deterministic MCP intents ----------
# Read-only filesystem inspection. Only FORGE may dispatch these, enforced by
# governance.check_permission against tools.allowed_agents=['FORGE'].

_FORGE_LIST_RE = re.compile(
    r"^\s*(?:show\s+files|list\s+project\s+files)\s*\.?\s*$",
    re.IGNORECASE,
)
_FORGE_READ_RE = re.compile(
    r"^\s*read\s+file\s+(\S.*?)\s*\.?\s*$",
    re.IGNORECASE,
)
_FORGE_INSPECT_RE = re.compile(
    r"^\s*(?:inspect|open|look\s+at)\s+(\S.*?)\s*\.?\s*$",
    re.IGNORECASE,
)
_PATH_HEURISTIC = re.compile(r"[./\\]")


def _clean_path(raw: str) -> str:
    return raw.strip().strip("'\"`").rstrip(".,;:!?").strip()


def _match_forge_tool_intent(message: str) -> Optional[dict]:
    """Return {'tool_name': ..., 'arguments': {...}} or None."""
    if _FORGE_LIST_RE.match(message):
        return {"tool_name": "filesystem_list_project", "arguments": {}}
    m = _FORGE_READ_RE.match(message)
    if m:
        path = _clean_path(m.group(1))
        if path:
            return {
                "tool_name": "filesystem_read_file",
                "arguments": {"path": path},
            }
    m = _FORGE_INSPECT_RE.match(message)
    if m:
        path = _clean_path(m.group(1))
        # Require path-like to avoid eating phrases like "open the door".
        if path and _PATH_HEURISTIC.search(path):
            return {
                "tool_name": "filesystem_read_file",
                "arguments": {"path": path},
            }
    return None


def _extract_mcp_text(payload) -> str:
    """Best-effort extraction of human-readable text from an MCP tools/call
    result. Standard MCP shape is {content: [{type:'text', text:'...'}, ...]}."""
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        content = payload.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
            if parts:
                return "\n".join(parts)
        if "text" in payload:
            return str(payload["text"])
    try:
        return json.dumps(payload, indent=2, default=str)
    except (TypeError, ValueError):
        return str(payload)


TOOL_TRIGGERS: list[tuple[str, str]] = [
    ("test n8n", "n8n_health_check"),
    ("check n8n", "n8n_health_check"),
    ("run n8n health", "n8n_health_check"),
    ("n8n health check", "n8n_health_check"),
]


def _match_tool_intent(message: str) -> Optional[str]:
    lowered = message.lower()
    for needle, tool_name in TOOL_TRIGGERS:
        if needle in lowered:
            return tool_name
    return None


async def _fetch_tool(name: str) -> Optional[dict]:
    if clients.db_pool is None:
        return None
    async with clients.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, name, description, type, endpoint, enabled,
                   requires_confirmation, mcp_server_name, mcp_action_name,
                   input_schema, output_schema, risk_level, allowed_agents
            FROM tools
            WHERE name = $1
            """,
            name,
        )
    return dict(row) if row else None


def _format_tool_success(tool_name: str, result: dict) -> str:
    duration = result.get("duration_ms", 0)
    http_status = result.get("http_status")
    return (
        f"I ran the {tool_name} tool. The n8n webhook responded with "
        f"HTTP {http_status} in {duration} ms — Cora can reach n8n."
    )


def _format_tool_failure(tool_name: str, result: dict) -> str:
    duration = result.get("duration_ms", 0)
    http_status = result.get("http_status")
    return (
        f"I tried to run the {tool_name} tool, but the n8n webhook returned "
        f"a problem (HTTP {http_status}, {duration} ms). "
        "Check the workflow logs in n8n."
    )


def _format_tool_error(tool_name: str, error_msg: str) -> str:
    return (
        f"I tried to run the {tool_name} tool, but the call to n8n failed: "
        f"{error_msg}"
    )


# ---------- SIGNAL / CHRONOS chat-to-draft intent (v0.2) ----------
# Deterministic, explicit-only: a draft/proposal is created ONLY when the user
# clearly asks to save/create/store one. A routed SIGNAL/CHRONOS turn that just
# asks for drafted content does NOT create a record. These are internal,
# review-only records — never an external send/calendar action. The explicit
# "save" phrase is itself the user confirmation for the requires_confirmation
# tool, so creation proceeds when governance allows it.

SIGNAL_SAVE_PHRASES = (
    "save as draft",
    "save as a draft",
    "create a draft",
    "create draft",
    "draft and save",
    "save this email",
    "save this draft",
    "save communication draft",
    "create stakeholder update draft",
    "prepare draft",
    "save it as a draft",
    "save the draft",
)

CHRONOS_SAVE_PHRASES = (
    "create schedule proposal",
    "save schedule proposal",
    "create a schedule proposal",
    "create proposal",
    "create a proposal",
    "save this plan",
    "save the plan",
    "create meeting proposal",
    "prepare schedule proposal",
    "save timeline proposal",
    "save it as a proposal",
    "save as a proposal",
    "save the proposal",
    "save it",  # qualified below: only when paired with a plan/schedule verb
)


def _match_signal_save_intent(message: str) -> bool:
    m = message.lower()
    return any(p in m for p in SIGNAL_SAVE_PHRASES)


def _match_chronos_save_intent(message: str) -> bool:
    m = message.lower()
    for p in CHRONOS_SAVE_PHRASES:
        if p == "save it":
            # Bare "save it" only counts alongside an explicit planning noun, so
            # generic chatter doesn't create proposals.
            if "save it" in m and any(
                w in m for w in ("proposal", "schedule", "plan", "timeline", "meeting")
            ):
                return True
            continue
        if p in m:
            return True
    return False


# ---------- Draft / proposal GENERATION intent (v0.3) ----------
# A plain "draft an email about X" produces draft content that the user sees in
# the chat thread, so it must also be persisted as a review-only record —
# otherwise the generation and persistence paths diverge (draft shown but no
# communication_drafts row). Detection is a generation verb paired with a
# communication noun, so informational SIGNAL/CHRONOS chatter ("what's a good
# subject line?") still does NOT create a record. This is OR-combined with the
# explicit "save" phrases above; it never sends mail or writes a calendar.
SIGNAL_DRAFT_VERBS = ("draft", "write", "compose", "prepare", "put together")
SIGNAL_DRAFT_NOUNS = (
    "email", "e-mail", "message", "announcement", "memo", "note",
    "update", "reply", "response", "communication", "letter", "newsletter",
)

CHRONOS_PROPOSE_VERBS = ("propose", "plan", "schedule", "draft", "prepare", "set up")
CHRONOS_PROPOSE_NOUNS = (
    "meeting", "schedule", "timeline", "reminder", "agenda", "call", "session",
)


def _match_signal_draft_intent(message: str) -> bool:
    m = message.lower()
    return any(v in m for v in SIGNAL_DRAFT_VERBS) and any(
        n in m for n in SIGNAL_DRAFT_NOUNS
    )


def _match_chronos_propose_intent(message: str) -> bool:
    m = message.lower()
    return any(v in m for v in CHRONOS_PROPOSE_VERBS) and any(
        n in m for n in CHRONOS_PROPOSE_NOUNS
    )


# ---------- EXTERNAL ACTION intent (governance-blocked) ----------
# Asking to SEND an email or CREATE a calendar event is a request to execute an
# external action. Cora cannot do that yet (no provider integration), so these
# are hard-blocked at the governance layer AND a safe internal artifact (a
# SIGNAL draft / CHRONOS proposal) is created instead. Discriminator vs the
# internal draft/propose intents above is the verb: "draft/write/propose/plan"
# stay internal; "send/deliver/dispatch" (email) and a "calendar" reference with
# an action verb (calendar) are external. Conservative on purpose — they require
# an explicit email/calendar keyword so informational queries never trip them.
_EMAIL_SEND_VERBS = (
    "send ", "send,", "send.", "send out", "send off", "deliver ",
    "dispatch ", "fire off", "shoot off", "email out",
)
_CALENDAR_CREATE_VERBS = (
    "create", "add", "put", "schedule", "set up", "book", "make", "send", "invite",
)


def _match_external_email_send_intent(message: str) -> bool:
    m = message.lower().strip()
    if not any(n in m for n in ("email", "e-mail")):
        return False
    if any(v in m for v in _EMAIL_SEND_VERBS):
        return True
    # "email John about X" — email used as an imperative send verb.
    return m.startswith("email ") or m.startswith("e-mail ")


def _match_external_calendar_create_intent(message: str) -> bool:
    m = message.lower()
    if "calendar" in m and any(v in m for v in _CALENDAR_CREATE_VERBS):
        return True
    # "create an event" / "add an invite" without the word "calendar".
    return any(v in m for v in ("create", "add", "schedule", "book", "set up")) and any(
        n in m for n in ("calendar event", "calendar invite", "event on", "invite to")
    )


def _compose_external_block_suffix(
    block_msg: Optional[str], artifact_suffix: Optional[str]
) -> Optional[str]:
    """Combine the governance-block notice with the saved-artifact note into the
    suffix appended to the chat answer."""
    parts: list[str] = []
    if block_msg:
        parts.append(f"\n\n_{block_msg}_")
    if artifact_suffix:
        parts.append(artifact_suffix)
    return "".join(parts) or None


# Tolerate light markdown around the label/value, e.g. "**Subject:** Foo" or
# "*To:* bar" — the model commonly bolds these labels.
_SUBJECT_RE = re.compile(
    r"^[*_#\s]*subject:[*_\s]*(.+?)[*_\s]*$", re.IGNORECASE | re.MULTILINE
)
_RECIPIENT_RE = re.compile(
    r"^[*_#\s]*to:[*_\s]*(.+?)[*_\s]*$", re.IGNORECASE | re.MULTILINE
)


def _strip_md(s: str) -> str:
    return s.strip().lstrip("#*_ ").strip()


def _first_line_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = _strip_md(line)
        # Skip a leading "Subject:"/"To:" label line when deriving a title.
        if not stripped or stripped.lower().startswith(("subject:", "to:")):
            continue
        return stripped[:200]
    return fallback.strip()[:200] or "Untitled"


def _extract_signal_fields(user_message: str, response: str, signoff_name: str) -> dict:
    subj_m = _SUBJECT_RE.search(response)
    subject = subj_m.group(1).strip()[:300] if subj_m else None
    recip_m = _RECIPIENT_RE.search(response)
    recipient_hint = recip_m.group(1).strip()[:300] if recip_m else None
    title = subject or _first_line_title(response, user_message)
    return {
        "draft_type": "email",
        "title": title,
        "subject": subject,
        "recipient_hint": recipient_hint,
        "body": signal_tools.normalize_email_signoff(response, signoff_name),
        "tone": None,
    }


def _extract_chronos_fields(user_message: str, response: str) -> dict:
    m = user_message.lower()
    if "timeline" in m:
        proposal_type = "timeline"
    elif "reminder" in m:
        proposal_type = "reminder"
    else:
        proposal_type = "meeting"
    title = _first_line_title(response, user_message)
    return {
        "proposal_type": proposal_type,
        "title": title,
        "description": response,
    }


async def maybe_create_signal_draft_from_chat(
    *,
    user_message: str,
    response: str,
    session_uuid: uuid.UUID,
    user_id: uuid.UUID,
    workspace_uuid: Optional[uuid.UUID],
    scope_type: str,
    is_admin: bool,
) -> Optional[str]:
    """Create an internal communication draft from an explicit SIGNAL chat
    request. Returns a confirmation/explanation suffix to append to the chat
    response, or None when no record was attempted. Never sends anything."""
    tool_name = "signal_create_draft"
    tool = await _fetch_tool(tool_name)
    if tool is None:
        return None
    decision = await check_permission(
        tool, agent_name=SIGNAL_NAME, user_id=user_id, is_admin=is_admin
    )
    if not decision.allowed:
        await log_execution_attempt(
            tool_name=tool_name,
            agent_name=SIGNAL_NAME,
            session_id=session_uuid,
            user_id=user_id,
            scope_type=scope_type,
            allowed=False,
            duration_ms=None,
            status="denied",
            error_message=decision.reason,
        )
        await write_trace(
            session_id=session_uuid,
            user_id=user_id,
            trace_type="signal_draft_created_from_chat",
            status="denied",
            selected_agent=SIGNAL_NAME,
            tool_name=tool_name,
            tool_result={"reason": decision.reason},
            workspace_id=workspace_uuid,
        )
        return (
            "\n\n_Note: saving this as an internal draft isn't permitted by the "
            f"current tool policy ({decision.reason}). The drafted content is "
            "above for you to copy._"
        )

    signoff_name = await signal_tools.user_signoff_name(user_id)
    fields = _extract_signal_fields(user_message, response, signoff_name)
    logger.info(
        "chat-to-draft SIGNAL: invoking %s session=%s title=%r",
        tool_name, session_uuid, fields["title"],
    )
    started = time.perf_counter()
    try:
        row = await signal_tools.create_communication_draft(
            workspace_id=workspace_uuid,
            user_id=user_id,
            draft_type=fields["draft_type"],
            title=fields["title"],
            subject=fields["subject"],
            body=fields["body"],
            recipient_hint=fields["recipient_hint"],
            tone=fields["tone"],
            metadata={"source": "chat", "session_id": str(session_uuid)},
        )
    except Exception as exc:  # signal_tools.SignalToolError or pool issues
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.exception("chat-to-draft SIGNAL create failed session=%s", session_uuid)
        await log_execution_attempt(
            tool_name=tool_name,
            agent_name=SIGNAL_NAME,
            session_id=session_uuid,
            user_id=user_id,
            scope_type=scope_type,
            allowed=True,
            duration_ms=duration_ms,
            status="failed",
            error_message=str(exc),
        )
        await write_trace(
            session_id=session_uuid,
            user_id=user_id,
            trace_type="signal_draft_created_from_chat",
            status="failed",
            selected_agent=SIGNAL_NAME,
            tool_name=tool_name,
            tool_result={"error": str(exc)},
            workspace_id=workspace_uuid,
        )
        return (
            "\n\n_I prepared the content, but could not save the internal draft._"
        )

    duration_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "chat-to-draft SIGNAL: persisted draft id=%s session=%s duration_ms=%s",
        row["id"], session_uuid, duration_ms,
    )
    await log_execution_attempt(
        tool_name=tool_name,
        agent_name=SIGNAL_NAME,
        session_id=session_uuid,
        user_id=user_id,
        scope_type=scope_type,
        allowed=True,
        duration_ms=duration_ms,
        status="success",
        error_message=None,
    )
    await write_trace(
        session_id=session_uuid,
        user_id=user_id,
        trace_type="signal_draft_created_from_chat",
        status="ok",
        selected_agent=SIGNAL_NAME,
        tool_name=tool_name,
        tool_result={
            "draft_id": str(row["id"]),
            "title": row["title"],
            "status": row["status"],
        },
        workspace_id=workspace_uuid,
    )
    return f"\n\n✓ Saved as an internal communication draft: **{row['title']}**"


async def maybe_create_chronos_proposal_from_chat(
    *,
    user_message: str,
    response: str,
    session_uuid: uuid.UUID,
    user_id: uuid.UUID,
    workspace_uuid: Optional[uuid.UUID],
    scope_type: str,
    is_admin: bool,
) -> Optional[str]:
    """Create an internal schedule proposal from an explicit CHRONOS chat
    request. Returns a suffix to append, or None. Never writes a calendar."""
    tool_name = "chronos_create_schedule_proposal"
    tool = await _fetch_tool(tool_name)
    if tool is None:
        return None
    decision = await check_permission(
        tool, agent_name=CHRONOS_NAME, user_id=user_id, is_admin=is_admin
    )
    if not decision.allowed:
        await log_execution_attempt(
            tool_name=tool_name,
            agent_name=CHRONOS_NAME,
            session_id=session_uuid,
            user_id=user_id,
            scope_type=scope_type,
            allowed=False,
            duration_ms=None,
            status="denied",
            error_message=decision.reason,
        )
        await write_trace(
            session_id=session_uuid,
            user_id=user_id,
            trace_type="chronos_proposal_created_from_chat",
            status="denied",
            selected_agent=CHRONOS_NAME,
            tool_name=tool_name,
            tool_result={"reason": decision.reason},
            workspace_id=workspace_uuid,
        )
        return (
            "\n\n_Note: saving this as an internal proposal isn't permitted by the "
            f"current tool policy ({decision.reason}). The proposed plan is above "
            "for you to copy._"
        )

    fields = _extract_chronos_fields(user_message, response)
    logger.info(
        "chat-to-draft CHRONOS: invoking %s session=%s title=%r",
        tool_name, session_uuid, fields["title"],
    )
    started = time.perf_counter()
    try:
        row = await chronos_tools.create_schedule_proposal(
            workspace_id=workspace_uuid,
            user_id=user_id,
            proposal_type=fields["proposal_type"],
            title=fields["title"],
            description=fields["description"],
            metadata={"source": "chat", "session_id": str(session_uuid)},
        )
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.exception("chat-to-draft CHRONOS create failed session=%s", session_uuid)
        await log_execution_attempt(
            tool_name=tool_name,
            agent_name=CHRONOS_NAME,
            session_id=session_uuid,
            user_id=user_id,
            scope_type=scope_type,
            allowed=True,
            duration_ms=duration_ms,
            status="failed",
            error_message=str(exc),
        )
        await write_trace(
            session_id=session_uuid,
            user_id=user_id,
            trace_type="chronos_proposal_created_from_chat",
            status="failed",
            selected_agent=CHRONOS_NAME,
            tool_name=tool_name,
            tool_result={"error": str(exc)},
            workspace_id=workspace_uuid,
        )
        return (
            "\n\n_I prepared the content, but could not save the internal proposal._"
        )

    duration_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "chat-to-draft CHRONOS: persisted proposal id=%s session=%s duration_ms=%s",
        row["id"], session_uuid, duration_ms,
    )
    await log_execution_attempt(
        tool_name=tool_name,
        agent_name=CHRONOS_NAME,
        session_id=session_uuid,
        user_id=user_id,
        scope_type=scope_type,
        allowed=True,
        duration_ms=duration_ms,
        status="success",
        error_message=None,
    )
    await write_trace(
        session_id=session_uuid,
        user_id=user_id,
        trace_type="chronos_proposal_created_from_chat",
        status="ok",
        selected_agent=CHRONOS_NAME,
        tool_name=tool_name,
        tool_result={
            "proposal_id": str(row["id"]),
            "title": row["title"],
            "status": row["status"],
        },
        workspace_id=workspace_uuid,
    )
    return f"\n\n✓ Saved as an internal schedule proposal: **{row['title']}**"


# ---------- PULSE deterministic web-search intent ----------
# Two ways to trigger a live web search (both force PULSE + dispatch the
# governed web_search tool): an explicit search verb ("search/google/look up
# X"), or a recency cue ("latest/current/recent/news/today …") which signals
# the question is about current info the ingested knowledge base can't hold.
# A bare "research X" with no verb and no recency cue still synthesizes from
# ingested knowledge — Cora doesn't leave the network unprompted.
# NOTE: explicit memory ops ("search my memory …") are siphoned off earlier in
# the pipeline by match_chat_memory_intent, so they never reach here.

_WEB_SEARCH_PATTERNS: list[re.Pattern] = [
    # "search/look (up) (on) (the) web/internet/online [for|:] X"
    re.compile(
        r"\b(?:search|look)\s+(?:up\s+)?(?:on\s+)?(?:the\s+)?"
        r"(?:web|internet|online)(?:\s+for|:)?\s+(.+)$",
        re.IGNORECASE,
    ),
    # "web search [for|:] X"
    re.compile(r"\bweb\s+search(?:\s+for|:)?\s+(.+)$", re.IGNORECASE),
    # "google [for|:] X"
    re.compile(r"\bgoogle(?:\s+for|:)?\s+(.+)$", re.IGNORECASE),
    # "search/find/look up X online | on the web | on the internet"
    re.compile(
        r"\b(?:search|find|look\s+up)\s+(?:for\s+)?(.+?)\s+"
        r"(?:online|on\s+the\s+web|on\s+the\s+internet)\b",
        re.IGNORECASE,
    ),
    # Bare search verbs with an object: "search for X", "search X", "look up X".
    re.compile(r"\b(?:search|google)\s+(?:for\s+)?(.+)$", re.IGNORECASE),
    re.compile(r"\blook\s+up\s+(.+)$", re.IGNORECASE),
]

# Recency / currency markers — a strong signal the ingested KB can't answer, so
# search the live web. Matched against the whole message.
_WEB_RECENCY_RE = re.compile(
    r"\b(?:latest|newest|most\s+recent|recent|currently|current|"
    r"today'?s?|this\s+(?:week|month|year)|right\s+now|"
    r"as\s+of\s+(?:today|now)|breaking|up[\s-]?to[\s-]?date|"
    r"recently|new\s+ai\s+news|what'?s\s+(?:new|happening))\b",
    re.IGNORECASE,
)

# Conversational lead-ins stripped before using a recency question as the query.
_WEB_LEADIN_RE = re.compile(
    r"^\s*(?:hey\s+cora[,\s]+|ok(?:ay)?\s+cora[,\s]+|cora[,\s]+|"
    r"can\s+you\s+|could\s+you\s+|would\s+you\s+|will\s+you\s+|"
    r"please\s+|pls\s+|tell\s+me\s+|do\s+you\s+know\s+|"
    r"i\s+want\s+to\s+know\s+|i'?d\s+like\s+to\s+know\s+)+",
    re.IGNORECASE,
)

# Queries that are just a pronoun/filler — reject so PULSE asks for the topic
# instead of searching garbage (e.g. "can you search for me").
_WEB_QUERY_STOPWORDS = {
    "me", "it", "this", "that", "something", "anything",
    "for me", "for it", "stuff", "things", "online", "the web", "web",
}


def _clean_web_query(raw: str) -> str:
    return raw.strip().strip("'\"`").rstrip(".?!,;:").strip()


def _match_web_search_intent(message: str) -> Optional[str]:
    """Return a web-search query if the message warrants a live search, else
    None. Triggers on an explicit search verb or a recency cue."""
    msg = message.strip()

    for pat in _WEB_SEARCH_PATTERNS:
        m = pat.search(msg)
        if m:
            query = _clean_web_query(m.group(1))
            if query and query.lower() not in _WEB_QUERY_STOPWORDS:
                return query

    # Recency questions: drop the conversational lead-in and search the rest.
    if _WEB_RECENCY_RE.search(msg):
        query = _clean_web_query(_WEB_LEADIN_RE.sub("", msg))
        if query and query.lower() not in _WEB_QUERY_STOPWORDS:
            return query

    return None


def _format_web_results_block(query: str, results: list[dict]) -> str:
    lines = [
        f'Live Web Search Results (SearXNG, query: "{query}"):',
        "These are your primary evidence for this question. Cite each claim by "
        "its result title and URL. Corroborate across results; flag a claim "
        "that rests on a single result. Do not invent results beyond this list.",
    ]
    for i, r in enumerate(results, 1):
        title = r.get("title") or "(untitled)"
        url = r.get("url") or ""
        snippet = r.get("snippet") or ""
        lines.append(f"{i}. {title}\n   URL: {url}\n   {snippet}")
    return "\n".join(lines)


class ChatMessage(BaseModel):
    role: str = Field(..., description="'user', 'assistant', or 'system'")
    content: str


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="User message to Cora")
    session_id: Optional[str] = Field(
        default=None, description="Optional session id for multi-turn context"
    )
    history: Optional[list[ChatMessage]] = Field(
        default=None, description="Optional prior turns for context"
    )
    workspace_id: Optional[str] = Field(
        default=None,
        description="Optional workspace UUID. Falls back to the default "
        "workspace ('cora-ai-os') when omitted.",
    )
    screen_context: Optional[dict] = Field(
        default=None,
        description="Optional UI screen context (view/section/entity). "
        "Sanitized and re-resolved server-side; see app.screen_context.",
    )
    screen_image: Optional[str] = Field(
        default=None,
        description="Optional user-shared screenshot (data URL or base64) for "
        "Tier-2 screen vision. Fail-closed; see app.screen_vision.",
    )
    stream: bool = Field(
        default=False,
        description="When true, the reply is streamed back as Server-Sent Events "
        "(meta/delta/done/error) instead of a single ChatResponse JSON body.",
    )
    speakable: bool = Field(
        default=False,
        description="Voice mode: instruct the model to answer in short, spoken, "
        "markdown-free sentences, and normalize the reply for text-to-speech.",
    )


class ChatResponse(BaseModel):
    session_id: str
    agent: str
    selected_agent: str
    routing_matched_keywords: list[str] = []
    model_endpoint: Optional[str]
    response: str
    placeholder: bool
    created_at: str


def _sse_event(payload: dict) -> bytes:
    """Encode one Server-Sent Events frame: a single JSON `data:` line."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


class AgentRunRequest(BaseModel):
    message: str = Field(..., min_length=1, description="User message for the agent kernel")
    session_id: Optional[str] = Field(default=None, description="Optional session id")


class AgentRunResponse(BaseModel):
    run_id: Optional[str]
    answer: str
    model: str
    tool_calls: int
    stopped: str  # final | budget | error
    steps: list[dict]
    evaluation: Optional[dict] = None  # Phase 6 verdict (None unless enabled)
    status: str = "done"  # done | failed | waiting_user
    interrupt: Optional[dict] = None  # Phase 7 pending approval (waiting_user)


@router.post(
    "/chat/agent",
    response_model=AgentRunResponse,
    summary="Phase 1 agent kernel (read-only tool-calling loop)",
)
async def chat_agent(
    request: AgentRunRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> AgentRunResponse:
    """Experimental: route a message through the model-driven reason→act→observe
    loop (read-only tools only). Fail-closed — returns 404 unless
    settings.agent_runtime_enabled is true."""
    if not settings.agent_runtime_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="agent runtime is disabled",
        )
    result = await agent_runtime.run_agent(
        request.message,
        user_id=current.id,
        session_id=request.session_id,
        is_orchestrator=True,
    )
    return AgentRunResponse(
        run_id=result.run_id,
        answer=result.answer,
        model=result.model,
        tool_calls=result.tool_calls,
        stopped=result.stopped,
        steps=[{"kind": s.kind, **s.detail} for s in result.steps],
        evaluation=result.evaluation,
        status=result.status,
        interrupt=result.interrupt,
    )


class AgentAsyncResponse(BaseModel):
    run_id: str
    status: str


@router.post(
    "/chat/agent/async",
    response_model=AgentAsyncResponse,
    summary="Submit a worker-driven agent run (non-blocking); poll the run id",
)
async def chat_agent_async(
    request: AgentRunRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> AgentAsyncResponse:
    """Phase 3: enqueue the orchestrator run on cora-worker and return its id
    immediately. The request never blocks on the model/tools/spokes; poll
    GET /chat/agent/runs/{run_id} until status is terminal."""
    if not settings.agent_runtime_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="agent runtime is disabled",
        )
    session_uuid = (
        _parse_uuid_or_none(request.session_id) if request.session_id else None
    )
    run_id = await agent_runtime.create_pending_run(
        goal=request.message,
        user_id=current.id,
        session_id=session_uuid,
    )
    if run_id is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="run storage unavailable; async runs require persistence",
        )
    try:
        await create_job(
            user_id=current.id,
            session_id=session_uuid,
            job_type="agent_run",
            payload={
                "run_id": str(run_id),
                "message": request.message,
                "is_orchestrator": True,
            },
        )
    except JobError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"could not enqueue run: {exc}",
        ) from exc
    return AgentAsyncResponse(run_id=str(run_id), status="pending")


@router.get(
    "/chat/agent/runs",
    summary="List recent agent runs (owner-scoped, summary only)",
)
async def chat_agent_list_runs(
    current: Annotated[CurrentUser, Depends(get_current_user)],
    limit: int = 50,
) -> list[dict]:
    """Recent runs for the runs / task-manager view. Summary columns only —
    fetch a single run for its full step trace + delegation tree."""
    if not settings.agent_runtime_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="agent runtime is disabled",
        )
    return await agent_runtime.list_runs(user_id=current.id, limit=limit)


@router.get(
    "/chat/agent/runs/{run_id}",
    summary="Fetch a persisted agent run (owner-scoped) + its delegation tree",
)
async def chat_agent_get_run(
    run_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    if not settings.agent_runtime_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="agent runtime is disabled",
        )
    try:
        rid = uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="run_id must be a UUID",
        ) from exc
    run = await agent_runtime.get_run(rid, user_id=current.id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="run not found"
        )
    # Owner verified above; attach the orchestrator→spoke tree (empty for spokes).
    run["delegations"] = await agent_runtime.get_run_delegations(rid)
    return run


class AgentDecisionRequest(BaseModel):
    decision: str  # "approve" | "reject"
    note: Optional[str] = None
    # Override an evaluator 'fail' gate (agent_eval_gate_enabled) on approve.
    override: bool = False


@router.post(
    "/chat/agent/runs/{run_id}/decision",
    summary="Approve/reject a run paused at waiting_user (records the decision ONLY)",
)
async def chat_agent_decide(
    run_id: str,
    request: AgentDecisionRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    """Phase 7 confirm-as-interrupt: resolve a run paused for human approval.
    Records the decision and resumes the run to a terminal state — it does NOT
    send email or write a calendar (the real external firing stays a separate,
    deferred step). Owner-scoped; 404 if no such run is awaiting the caller."""
    if not settings.agent_runtime_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="agent runtime is disabled",
        )
    if request.decision not in ("approve", "reject"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="decision must be 'approve' or 'reject'",
        )
    try:
        rid = uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="run_id must be a UUID",
        ) from exc
    run = await agent_runtime.resolve_interrupt(
        rid, user_id=current.id, decision=request.decision, note=request.note,
        override=request.override,
    )
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no run awaiting your decision",
        )
    if run.get("blocked"):
        # Evaluator-gated approval refused a 'fail' verdict (no override).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=run["reason"]
        )
    return run


class AgentConfirmRequest(BaseModel):
    session_id: str
    message: str = Field(..., min_length=1)
    # Override an evaluator 'fail' gate on approve (also triggered by saying "override").
    override: bool = False


@router.post(
    "/chat/agent/confirm",
    summary="Resolve a session's pending agent confirmation from a natural-language yes/no",
)
async def chat_agent_confirm(
    request: AgentConfirmRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    """Spoken confirm-as-interrupt: when this conversation has a run paused at
    waiting_user, resolve it from the user's own words ('yes' / 'no' / 'override')
    instead of a run_id + an InterruptCard click — the primitive a voice layer needs.
    Always 200: no pending run is just {"pending": false}; an unclear utterance asks
    again; the evaluator gate + execution gate still apply inside resolve_interrupt.
    The `spoken` field is the line to read back to the user."""
    if not settings.agent_runtime_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="agent runtime is disabled",
        )
    return await agent_runtime.resolve_pending_for_session(
        request.session_id, current.id, request.message, override=request.override,
    )


class AgentConfigResponse(BaseModel):
    runtime_enabled: bool
    delegation_enabled: bool
    write_enabled: bool
    eval_enabled: bool
    eval_gate_enabled: bool
    interrupt_enabled: bool
    execution_enabled: bool
    max_steps: int
    max_parallel: int
    chat_model: str
    eval_model: str
    endpoint_configured: bool


@router.get(
    "/chat/agent/config",
    response_model=AgentConfigResponse,
    summary="Agent runtime config + flag status (read-only)",
)
async def chat_agent_config(
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> AgentConfigResponse:
    """Read-only view of the agent runtime's effective config. Intentionally NOT
    gated on agent_runtime_enabled — it reports status even when the loop is off."""
    return AgentConfigResponse(
        runtime_enabled=settings.agent_runtime_enabled,
        delegation_enabled=settings.agent_delegation_enabled,
        write_enabled=settings.agent_write_enabled,
        eval_enabled=settings.agent_eval_enabled,
        eval_gate_enabled=settings.agent_eval_gate_enabled,
        interrupt_enabled=settings.agent_interrupt_enabled,
        execution_enabled=settings.agent_execution_enabled,
        max_steps=settings.agent_runtime_max_steps,
        max_parallel=settings.agent_delegation_max_parallel,
        chat_model=settings.dgx_chat_model_name or settings.dgx_model_name or "",
        eval_model=(
            settings.dgx_eval_model_name
            or settings.dgx_chat_model_name
            or settings.dgx_model_name
            or ""
        ),
        endpoint_configured=bool(settings.dgx_model_endpoint),
    )


async def _persist_exchange(
    session_uuid: uuid.UUID,
    scope_type: str,
    scope_id: Optional[uuid.UUID],
    user_message: str,
    assistant_response: str,
    model_name: Optional[str],
    placeholder: bool,
    started_at: datetime,
    completed_at: datetime,
    agent_name: str = PERSONA_NAME,
    tool_name: Optional[str] = None,
    tool_result: Optional[dict] = None,
    workspace_id: Optional[uuid.UUID] = None,
) -> None:
    if clients.db_pool is None:
        logger.warning(
            "Skipping chat persistence for session=%s: Postgres pool unavailable",
            session_uuid,
        )
        return
    # Deterministic friendly title from the first user message. Only applied on
    # insert (or to backfill a NULL title on legacy rows); a manual rename or an
    # existing auto title is preserved by the COALESCE on conflict.
    auto_title = generate_title(user_message)
    async with clients.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO conversations
                    (session_id, scope_type, scope_id, workspace_id,
                     created_at, updated_at, title, title_source)
                VALUES ($1, $2, $3, $4, $5, $5, $6, 'auto')
                ON CONFLICT (session_id) DO UPDATE
                    SET updated_at = EXCLUDED.updated_at,
                        workspace_id = COALESCE(
                            conversations.workspace_id,
                            EXCLUDED.workspace_id
                        ),
                        title = COALESCE(
                            conversations.title,
                            EXCLUDED.title
                        )
                """,
                session_uuid,
                scope_type,
                scope_id,
                workspace_id,
                completed_at,
                auto_title,
            )
            await conn.execute(
                """
                INSERT INTO messages
                    (session_id, scope_type, scope_id, workspace_id,
                     role, content, created_at)
                VALUES ($1, $2, $3, $4, 'user', $5, $6)
                """,
                session_uuid,
                scope_type,
                scope_id,
                workspace_id,
                user_message,
                started_at,
            )
            await conn.execute(
                """
                INSERT INTO messages
                    (session_id, scope_type, scope_id, workspace_id,
                     role, content, created_at)
                VALUES ($1, $2, $3, $4, 'assistant', $5, $6)
                """,
                session_uuid,
                scope_type,
                scope_id,
                workspace_id,
                assistant_response,
                completed_at,
            )
            await conn.execute(
                """
                INSERT INTO agent_runs (
                    session_id, scope_type, scope_id, agent, model_name,
                    user_message, assistant_response, placeholder,
                    started_at, completed_at, tool_name, tool_result
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                """,
                session_uuid,
                scope_type,
                scope_id,
                agent_name,
                model_name,
                user_message,
                assistant_response,
                placeholder,
                started_at,
                completed_at,
                tool_name,
                tool_result,
            )
    logger.info(
        "persisted exchange: session=%s scope_type=%s scope_id=%s "
        "agent=%s user_chars=%s assistant_chars=%s",
        session_uuid,
        scope_type,
        scope_id,
        agent_name,
        len(user_message),
        len(assistant_response),
    )


@router.get(
    "/chat/debug/history/{session_id}",
    summary="Verify what /chat would load as history for the caller's scope",
)
async def chat_debug_history(
    session_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    try:
        session_uuid = uuid.UUID(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="session_id must be a valid UUID",
        ) from exc

    pool_ready = clients.db_pool is not None
    raw_row_count = None
    if pool_ready:
        async with clients.db_pool.acquire() as conn:
            raw_row_count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM messages
                WHERE session_id = $1
                  AND scope_type = 'user'
                  AND scope_id = $2
                """,
                session_uuid,
                current.id,
            )

    history = await _load_recent_history(session_uuid, "user", current.id)
    prompt, stats = _build_prompt(history, "<verification probe>")

    return {
        "incoming_session_id": session_id,
        "resolved_session_uuid": str(session_uuid),
        "pool_ready": pool_ready,
        "user_id": str(current.id),
        "scope_type": "user",
        "scope_id": str(current.id),
        "raw_message_count_for_session": raw_row_count,
        "history_loaded_count": len(history),
        "first_created_at": history[0]["created_at"].isoformat() if history else None,
        "last_created_at": history[-1]["created_at"].isoformat() if history else None,
        "history_preview": [
            {
                "role": m["role"],
                "created_at": m["created_at"].isoformat(),
                "content_preview": (m["content"][:80] + "…")
                if len(m["content"]) > 80
                else m["content"],
            }
            for m in history
        ],
        "prompt_stats": stats,
    }


@router.post("/chat", response_model=ChatResponse, summary="Chat with Cora (orchestrated by ATLAS)")
async def chat(
    request: ChatRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> ChatResponse | StreamingResponse:
    if not request.message.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="message must not be empty",
        )

    session_provided = bool(request.session_id)
    if request.session_id:
        try:
            session_uuid = uuid.UUID(request.session_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="session_id must be a valid UUID",
            ) from exc
    else:
        session_uuid = uuid.uuid4()
    session_id = str(session_uuid)
    endpoint = settings.dgx_model_endpoint or None
    scope_type = "user"
    scope_id: Optional[uuid.UUID] = current.id

    explicit_workspace_uuid: Optional[uuid.UUID] = None
    if request.workspace_id:
        try:
            explicit_workspace_uuid = uuid.UUID(request.workspace_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="workspace_id must be a valid UUID",
            ) from exc
    workspace_uuid = await resolve_workspace_id(explicit_workspace_uuid)
    logger.info(
        "chat workspace: session=%s requested=%s resolved=%s",
        session_id,
        request.workspace_id,
        workspace_uuid,
    )

    logger.info(
        "chat request: user_id=%s scope_type=%s scope_id=%s "
        "incoming_session_id=%r resolved_session_id=%s "
        "session_provided=%s request_history_field_len=%s dgx_configured=%s",
        current.id,
        scope_type,
        scope_id,
        request.session_id,
        session_id,
        session_provided,
        len(request.history) if request.history else 0,
        bool(endpoint),
    )

    if session_provided:
        await _verify_session_ownership(session_uuid, current.id)

    started_at = datetime.now(timezone.utc)

    memory_intent = match_chat_memory_intent(request.message)
    if memory_intent:
        is_admin = current.role == "admin"
        kind = memory_intent["kind"]
        assistant_response: str
        tool_name = f"memory_{kind}"
        tool_result: dict = {"kind": kind}
        logger.info(
            "memory intent: session=%s user_id=%s kind=%s admin=%s",
            session_id,
            current.id,
            kind,
            is_admin,
        )

        if kind in ("remember_global", "remember_system") and not is_admin:
            assistant_response = (
                f"Only admins can create {('global' if kind == 'remember_global' else 'system')} "
                "memories. I haven't saved anything."
            )
            tool_result["status"] = "denied"
            tool_result["reason"] = "admin role required"

        elif kind == "remember_user":
            row = await create_chat_memory(
                text=memory_intent["text"],
                scope_type="user",
                scope_id=current.id,
                workspace_id=workspace_uuid,
            )
            if row is None:
                assistant_response = "I couldn't save that memory — storage was unavailable."
                tool_result["status"] = "error"
            else:
                assistant_response = (
                    f"Saved a user memory: «{row['title']}» "
                    f"(id {str(row['id'])[:8]})."
                )
                tool_result.update({
                    "status": "ok",
                    "memory_id": str(row["id"]),
                    "scope_type": "user",
                })

        elif kind == "remember_extract":
            # The user asked to remember the CONVERSATION (not an inline string), so
            # extract durable facts from recent turns and save EACH as a user memory.
            # We only ever confirm what actually persisted — never a hallucinated save.
            hist = (
                [{"role": m.role, "content": m.content} for m in request.history]
                if request.history
                else await _load_recent_history(session_uuid, scope_type, scope_id)
            )
            lines = [f"{_role_label(h['role'])}: {h['content']}" for h in hist]
            lines.append(f"{_role_label('user')}: {request.message}")
            facts = await extract_memories_from_conversation("\n".join(lines))
            existing = await _existing_user_memory_texts(current.id)
            saved_titles: list[str] = []
            for fact in facts:
                norm = " ".join(fact["text"].split()).lower()
                if norm in existing:
                    continue
                row = await create_chat_memory(
                    text=fact["text"], scope_type="user", scope_id=current.id,
                    workspace_id=workspace_uuid,
                )
                if row is not None:
                    saved_titles.append(row["title"])
                    existing.add(norm)
            if saved_titles:
                bullets = "\n".join(f"• {t}" for t in saved_titles)
                noun = "memory" if len(saved_titles) == 1 else "memories"
                assistant_response = f"Saved {len(saved_titles)} {noun}:\n{bullets}"
                tool_result.update({"status": "ok", "saved": len(saved_titles)})
            else:
                assistant_response = (
                    "I didn't find any new facts to save to memory. Tell me the "
                    "specific details you want kept (for example: \"remember that my "
                    "wife's name is Dorothy\")."
                )
                tool_result.update({"status": "noop", "saved": 0})

        elif kind == "remember_global":
            row = await create_chat_memory(
                text=memory_intent["text"],
                scope_type="global",
                scope_id=None,
                workspace_id=None,
            )
            if row is None:
                assistant_response = "I couldn't save that memory — storage was unavailable."
                tool_result["status"] = "error"
            else:
                assistant_response = (
                    f"Saved a global memory: «{row['title']}» "
                    f"(id {str(row['id'])[:8]})."
                )
                tool_result.update({
                    "status": "ok",
                    "memory_id": str(row["id"]),
                    "scope_type": "global",
                })

        elif kind == "remember_system":
            row = await create_chat_memory(
                text=memory_intent["text"],
                scope_type="system",
                scope_id=None,
                workspace_id=None,
            )
            if row is None:
                assistant_response = "I couldn't save that memory — storage was unavailable."
                tool_result["status"] = "error"
            else:
                assistant_response = (
                    f"Saved a system memory: «{row['title']}» "
                    f"(id {str(row['id'])[:8]})."
                )
                tool_result.update({
                    "status": "ok",
                    "memory_id": str(row["id"]),
                    "scope_type": "system",
                })

        elif kind == "show":
            query = memory_intent["query"]
            matches = await search_memory(
                query,
                limit=10,
                user_id=current.id,
                workspace_id=workspace_uuid,
            )
            tool_result["status"] = "ok"
            tool_result["match_count"] = len(matches)
            if not matches:
                assistant_response = (
                    f"I couldn't find any memories matching «{query}»."
                )
            else:
                lines = [f"I found {len(matches)} memor{'y' if len(matches) == 1 else 'ies'} about «{query}»:"]
                for m in matches:
                    preview = m["content"]
                    if len(preview) > 200:
                        preview = preview[:200].rstrip() + "…"
                    short_id = str(m["id"])[:8]
                    lines.append(
                        f"- [{m['scope_type']}] {m['title']} ({short_id}) — {preview}"
                    )
                assistant_response = "\n".join(lines)

        elif kind in ("delete", "update"):
            # `ref` is what the user typed: a full UUID, or the short id prefix
            # `show memories` prints (e.g. 8 hex chars). Resolve a prefix within the
            # user's visible set so the short id is actionable end-to-end from chat.
            ref = memory_intent["memory_id"]
            mem_uuid = _parse_uuid_or_none(ref)
            resolve_status = "ok"
            if mem_uuid is None:
                resolved = await resolve_memory_id_prefix(
                    ref, user_id=current.id, is_admin=is_admin
                )
                mem_uuid = resolved.get("id")
                resolve_status = resolved["status"]

            if mem_uuid is None:
                if resolve_status == "ambiguous":
                    assistant_response = (
                        f"More than one memory id starts with «{ref}» — add a few "
                        "more characters to pick just one."
                    )
                    tool_result["status"] = "ambiguous"
                else:
                    assistant_response = f"No memory found matching «{ref}»."
                    tool_result["status"] = "not_found"
            else:
                target = await fetch_memory_for_mutation(mem_uuid)
                if target is None:
                    assistant_response = f"No memory found with id {mem_uuid}."
                    tool_result["status"] = "not_found"
                elif not _memory_visible_to(target, current.id) and not is_admin:
                    assistant_response = f"No memory found with id {mem_uuid}."  # don't leak existence
                    tool_result["status"] = "not_found"
                elif not _can_mutate_memory(
                    target, user_id=current.id, is_admin=is_admin
                ):
                    assistant_response = (
                        f"I can't modify that memory — it's scoped to "
                        f"{target['scope_type']} and admin-only to change."
                    )
                    tool_result["status"] = "denied"
                    tool_result["reason"] = "scope requires admin"
                elif kind == "delete":
                    if not memory_intent["confirm"]:
                        assistant_response = (
                            f"Are you sure you want to delete memory "
                            f"{ref} («{target['title']}»)? Reply "
                            f"'confirm delete memory {ref}' to proceed."
                        )
                        tool_result["status"] = "confirmation_required"
                        tool_result["memory_id"] = str(mem_uuid)
                    else:
                        deleted = await delete_memory_entry(mem_uuid)
                        assistant_response = (
                            f"Deleted memory {mem_uuid}."
                            if deleted
                            else f"Could not delete memory {mem_uuid}."
                        )
                        tool_result["status"] = "ok" if deleted else "error"
                        tool_result["memory_id"] = str(mem_uuid)
                else:  # update
                    new_text = memory_intent["text"]
                    if not memory_intent["confirm"]:
                        preview = title_from_text(new_text)
                        assistant_response = (
                            f"Updating memory {ref} («{target['title']}») "
                            f"to «{preview}». Reply "
                            f"'confirm update memory {ref} to {new_text}' "
                            "to proceed."
                        )
                        tool_result["status"] = "confirmation_required"
                        tool_result["memory_id"] = str(mem_uuid)
                    else:
                        updated = await update_memory_content(mem_uuid, new_text)
                        if updated is None:
                            assistant_response = (
                                f"Could not update memory {mem_uuid}."
                            )
                            tool_result["status"] = "error"
                        else:
                            assistant_response = (
                                f"Updated memory {mem_uuid} → «{updated['title']}»."
                            )
                            tool_result["status"] = "ok"
                            tool_result["memory_id"] = str(mem_uuid)
        else:
            # Defensive: unknown kind from matcher
            assistant_response = "I recognized a memory command but couldn't dispatch it."
            tool_result["status"] = "error"

        completed_at = datetime.now(timezone.utc)
        try:
            await _persist_exchange(
                session_uuid=session_uuid,
                scope_type=scope_type,
                scope_id=scope_id,
                user_message=request.message,
                assistant_response=assistant_response,
                model_name=None,
                placeholder=False,
                started_at=started_at,
                completed_at=completed_at,
                tool_name=tool_name,
                tool_result=tool_result,
                workspace_id=workspace_uuid,
            )
        except Exception:
            logger.exception(
                "Failed to persist memory-intent exchange session=%s", session_id
            )

        mem_ids_for_trace: list[uuid.UUID] = []
        mid = tool_result.get("memory_id")
        if mid:
            try:
                mem_ids_for_trace.append(uuid.UUID(mid))
            except (ValueError, TypeError):
                pass
        mem_count_for_trace = (
            tool_result.get("match_count")
            if kind == "show"
            else len(mem_ids_for_trace)
        )
        await write_trace(
            session_id=session_uuid,
            user_id=current.id,
            trace_type="memory_intent",
            status=str(tool_result.get("status", "ok")),
            selected_agent=PERSONA_NAME,
            user_message=request.message,
            memory_count=int(mem_count_for_trace or 0),
            memory_ids=mem_ids_for_trace,
            tool_name=tool_name,
            tool_result=tool_result,
            duration_ms=int((completed_at - started_at).total_seconds() * 1000),
            error_message=tool_result.get("reason"),
            workspace_id=workspace_uuid,
        )

        return ChatResponse(
            session_id=session_id,
            agent=PERSONA_NAME,
            selected_agent=PERSONA_NAME,
            routing_matched_keywords=[],
            model_endpoint=None,
            response=assistant_response,
            placeholder=False,
            created_at=completed_at.isoformat(),
        )

    matched_tool_name = _match_tool_intent(request.message)
    if matched_tool_name:
        logger.info(
            "chat tool router matched: session=%s tool=%s",
            session_id,
            matched_tool_name,
        )
        tool = await _fetch_tool(matched_tool_name)
        tool_result: dict
        assistant_response: str

        # ATLAS is the deterministic router; dispatch is attributed to it.
        gov_agent_name = ORCHESTRATOR_NAME

        if tool is None:
            tool_result = {"status": "not_configured"}
            assistant_response = (
                f"You asked Cora to run {matched_tool_name}, but that tool "
                "isn't registered. An admin needs to seed it in the tools table."
            )
            await log_execution_attempt(
                tool_name=matched_tool_name,
                agent_name=gov_agent_name,
                session_id=session_uuid,
                user_id=current.id,
                scope_type=scope_type,
                allowed=False,
                duration_ms=None,
                status="not_configured",
                error_message="tool row missing",
            )
        else:
            decision = await check_permission(
                tool,
                agent_name=gov_agent_name,
                user_id=current.id,
                is_admin=(current.role == "admin"),
            )
            if not decision.allowed:
                tool_result = {
                    "status": "denied",
                    "reason": decision.reason,
                    "policy_source": decision.policy_source,
                    "matched_rule": decision.matched_rule,
                }
                assistant_response = (
                    f"I can't run {matched_tool_name} right now — "
                    f"{decision.reason}."
                )
                await log_execution_attempt(
                    tool_name=matched_tool_name,
                    agent_name=gov_agent_name,
                    session_id=session_uuid,
                    user_id=current.id,
                    scope_type=scope_type,
                    allowed=False,
                    duration_ms=None,
                    status="denied",
                    error_message=decision.reason,
                )
            elif decision.requires_confirmation:
                tool_result = {"status": "confirmation_required"}
                assistant_response = (
                    f"Running {matched_tool_name} requires explicit confirmation. "
                    "Reply confirming you want Cora to proceed before it runs."
                )
                await log_execution_attempt(
                    tool_name=matched_tool_name,
                    agent_name=gov_agent_name,
                    session_id=session_uuid,
                    user_id=current.id,
                    scope_type=scope_type,
                    allowed=False,
                    duration_ms=None,
                    status="confirmation_required",
                    error_message=None,
                )
            else:
                logger.info(
                    "chat tool allowed: session=%s tool=%s agent=%s "
                    "policy_source=%s matched_rule=%s",
                    session_id,
                    matched_tool_name,
                    gov_agent_name,
                    decision.policy_source,
                    decision.matched_rule,
                )
                import time as _time
                _started = _time.perf_counter()
                try:
                    tool_result = await dispatch_tool(
                        tool,
                        {
                            "session_id": session_id,
                            "user_message": request.message,
                            "metadata": {"source": "chat_router"},
                        },
                    )
                except httpx.HTTPError as exc:
                    duration_ms = int((_time.perf_counter() - _started) * 1000)
                    logger.exception(
                        "chat tool dispatch network failure: tool=%s",
                        matched_tool_name,
                    )
                    tool_result = {"status": "error", "error": str(exc)}
                    assistant_response = _format_tool_error(
                        matched_tool_name, str(exc)
                    )
                    await log_execution_attempt(
                        tool_name=matched_tool_name,
                        agent_name=gov_agent_name,
                        session_id=session_uuid,
                        user_id=current.id,
                        scope_type=scope_type,
                        allowed=True,
                        duration_ms=duration_ms,
                        status="error",
                        error_message=str(exc),
                    )
                except ValueError as exc:
                    duration_ms = int((_time.perf_counter() - _started) * 1000)
                    logger.exception(
                        "chat tool misconfigured: tool=%s", matched_tool_name
                    )
                    tool_result = {"status": "error", "error": str(exc)}
                    assistant_response = _format_tool_error(
                        matched_tool_name, str(exc)
                    )
                    await log_execution_attempt(
                        tool_name=matched_tool_name,
                        agent_name=gov_agent_name,
                        session_id=session_uuid,
                        user_id=current.id,
                        scope_type=scope_type,
                        allowed=True,
                        duration_ms=duration_ms,
                        status="error",
                        error_message=str(exc),
                    )
                else:
                    duration_ms = tool_result.get("duration_ms") or int(
                        (_time.perf_counter() - _started) * 1000
                    )
                    status_val = str(tool_result.get("status", "unknown"))
                    if status_val == "ok":
                        assistant_response = _format_tool_success(
                            matched_tool_name, tool_result
                        )
                    else:
                        assistant_response = _format_tool_failure(
                            matched_tool_name, tool_result
                        )
                    await log_execution_attempt(
                        tool_name=matched_tool_name,
                        agent_name=gov_agent_name,
                        session_id=session_uuid,
                        user_id=current.id,
                        scope_type=scope_type,
                        allowed=True,
                        duration_ms=duration_ms,
                        status=status_val,
                        error_message=tool_result.get("error") if status_val != "ok" else None,
                    )

        completed_at = datetime.now(timezone.utc)
        try:
            await _persist_exchange(
                session_uuid=session_uuid,
                scope_type=scope_type,
                scope_id=scope_id,
                user_message=request.message,
                assistant_response=assistant_response,
                model_name=None,
                placeholder=False,
                started_at=started_at,
                completed_at=completed_at,
                tool_name=matched_tool_name,
                tool_result=tool_result,
                workspace_id=workspace_uuid,
            )
        except Exception:
            logger.exception(
                "Failed to persist tool exchange session=%s", session_id
            )

        await write_trace(
            session_id=session_uuid,
            user_id=current.id,
            trace_type="tool_intent",
            status=str(tool_result.get("status", "unknown")),
            selected_agent=PERSONA_NAME,
            user_message=request.message,
            tool_name=matched_tool_name,
            tool_result=tool_result,
            duration_ms=tool_result.get("duration_ms")
            or int((completed_at - started_at).total_seconds() * 1000),
            error_message=tool_result.get("error") or tool_result.get("reason"),
            workspace_id=workspace_uuid,
        )

        return ChatResponse(
            session_id=session_id,
            agent=PERSONA_NAME,
            selected_agent=PERSONA_NAME,
            routing_matched_keywords=[],
            model_endpoint=None,
            response=assistant_response,
            placeholder=False,
            created_at=completed_at.isoformat(),
        )

    forge_intent = _match_forge_tool_intent(request.message)
    if forge_intent:
        gov_agent_name = FORGE_NAME
        tool_name = forge_intent["tool_name"]
        arguments = forge_intent["arguments"]
        logger.info(
            "forge tool intent: session=%s user_id=%s tool=%s arguments_keys=%s",
            session_id,
            current.id,
            tool_name,
            list(arguments.keys()),
        )

        tool = await _fetch_tool(tool_name)
        assistant_response: str
        tool_result: dict = {"tool": tool_name, "arguments": arguments}

        if tool is None:
            assistant_response = (
                f"You asked Cora to use {tool_name}, but that tool isn't "
                "registered. An admin needs to seed it in the tools table."
            )
            tool_result["status"] = "not_configured"
            await log_execution_attempt(
                tool_name=tool_name,
                agent_name=gov_agent_name,
                session_id=session_uuid,
                user_id=current.id,
                scope_type=scope_type,
                allowed=False,
                duration_ms=None,
                status="not_configured",
                error_message="tool row missing",
            )
        else:
            decision = await check_permission(
                tool,
                agent_name=gov_agent_name,
                user_id=current.id,
                is_admin=(current.role == "admin"),
            )
            if not decision.allowed:
                tool_result.update({
                    "status": "denied",
                    "reason": decision.reason,
                    "policy_source": decision.policy_source,
                    "matched_rule": decision.matched_rule,
                })
                assistant_response = (
                    f"I can't run {tool_name} right now — {decision.reason}."
                )
                await log_execution_attempt(
                    tool_name=tool_name,
                    agent_name=gov_agent_name,
                    session_id=session_uuid,
                    user_id=current.id,
                    scope_type=scope_type,
                    allowed=False,
                    duration_ms=None,
                    status="denied",
                    error_message=decision.reason,
                )
            elif decision.requires_confirmation:
                tool_result["status"] = "confirmation_required"
                assistant_response = (
                    f"Running {tool_name} requires explicit confirmation. "
                    "Reply confirming you want Cora to proceed before it runs."
                )
                await log_execution_attempt(
                    tool_name=tool_name,
                    agent_name=gov_agent_name,
                    session_id=session_uuid,
                    user_id=current.id,
                    scope_type=scope_type,
                    allowed=False,
                    duration_ms=None,
                    status="confirmation_required",
                    error_message=None,
                )
            else:
                logger.info(
                    "forge tool allowed: session=%s tool=%s policy_source=%s "
                    "matched_rule=%s",
                    session_id,
                    tool_name,
                    decision.policy_source,
                    decision.matched_rule,
                )
                started_call = time.perf_counter()
                try:
                    dispatch_result = await dispatch_tool(
                        tool,
                        {
                            "session_id": session_id,
                            "user_message": request.message,
                            "metadata": arguments,
                        },
                    )
                except httpx.HTTPError as exc:
                    duration_ms = int((time.perf_counter() - started_call) * 1000)
                    logger.exception(
                        "forge tool network failure: tool=%s", tool_name
                    )
                    tool_result.update({"status": "error", "error": str(exc)})
                    assistant_response = (
                        f"I tried to use {tool_name} but the call failed: {exc}"
                    )
                    await log_execution_attempt(
                        tool_name=tool_name,
                        agent_name=gov_agent_name,
                        session_id=session_uuid,
                        user_id=current.id,
                        scope_type=scope_type,
                        allowed=True,
                        duration_ms=duration_ms,
                        status="error",
                        error_message=str(exc),
                    )
                except ValueError as exc:
                    duration_ms = int((time.perf_counter() - started_call) * 1000)
                    logger.exception(
                        "forge tool misconfigured: tool=%s", tool_name
                    )
                    tool_result.update({"status": "error", "error": str(exc)})
                    assistant_response = (
                        f"I tried to use {tool_name} but it's misconfigured: {exc}"
                    )
                    await log_execution_attempt(
                        tool_name=tool_name,
                        agent_name=gov_agent_name,
                        session_id=session_uuid,
                        user_id=current.id,
                        scope_type=scope_type,
                        allowed=True,
                        duration_ms=duration_ms,
                        status="error",
                        error_message=str(exc),
                    )
                else:
                    duration_ms = dispatch_result.get("duration_ms") or int(
                        (time.perf_counter() - started_call) * 1000
                    )
                    status_val = str(dispatch_result.get("status", "unknown"))
                    tool_result.update(dispatch_result)
                    if status_val == "ok":
                        body = _extract_mcp_text(dispatch_result.get("response"))
                        if tool_name == "filesystem_list_project":
                            assistant_response = (
                                "Here's what FORGE found via the filesystem "
                                f"MCP server ({duration_ms}ms):\n\n{body}"
                            )
                        else:
                            path = arguments.get("path", "")
                            assistant_response = (
                                f"FORGE read `{path}` via the filesystem MCP "
                                f"server ({duration_ms}ms):\n\n```\n{body}\n```"
                            )
                    else:
                        err = dispatch_result.get("error") or "unknown error"
                        assistant_response = (
                            f"FORGE tried {tool_name} but the MCP server "
                            f"reported a problem ({duration_ms}ms): {err}"
                        )
                    await log_execution_attempt(
                        tool_name=tool_name,
                        agent_name=gov_agent_name,
                        session_id=session_uuid,
                        user_id=current.id,
                        scope_type=scope_type,
                        allowed=True,
                        duration_ms=duration_ms,
                        status=status_val,
                        error_message=dispatch_result.get("error") if status_val != "ok" else None,
                    )

        completed_at = datetime.now(timezone.utc)
        try:
            await _persist_exchange(
                session_uuid=session_uuid,
                scope_type=scope_type,
                scope_id=scope_id,
                user_message=request.message,
                assistant_response=assistant_response,
                model_name=None,
                placeholder=False,
                started_at=started_at,
                completed_at=completed_at,
                agent_name=FORGE_NAME,
                tool_name=tool_name,
                tool_result=tool_result,
                workspace_id=workspace_uuid,
            )
        except Exception:
            logger.exception(
                "Failed to persist forge-tool exchange session=%s", session_id
            )

        await write_trace(
            session_id=session_uuid,
            user_id=current.id,
            trace_type="forge_tool",
            status=str(tool_result.get("status", "unknown")),
            selected_agent=FORGE_NAME,
            user_message=request.message,
            tool_name=tool_name,
            tool_result=tool_result,
            mcp_server_name=tool["mcp_server_name"] if tool else None,
            mcp_action_name=tool["mcp_action_name"] if tool else None,
            duration_ms=tool_result.get("duration_ms")
            or int((completed_at - started_at).total_seconds() * 1000),
            error_message=tool_result.get("error") or tool_result.get("reason"),
            workspace_id=workspace_uuid,
        )

        return ChatResponse(
            session_id=session_id,
            agent=PERSONA_NAME,
            selected_agent=FORGE_NAME,
            routing_matched_keywords=[],
            model_endpoint=None,
            response=assistant_response,
            placeholder=False,
            created_at=completed_at.isoformat(),
        )

    plan_goal = match_plan_intent(request.message)
    if plan_goal:
        logger.info(
            "plan intent: session=%s user_id=%s goal_chars=%s",
            session_id,
            current.id,
            len(plan_goal),
        )
        plan_started = time.perf_counter()
        plan_error: Optional[str] = None
        plan = None
        try:
            plan = await create_plan(
                session_id=session_uuid,
                user_id=current.id,
                goal=plan_goal,
                workspace_id=workspace_uuid,
            )
        except Exception as exc:
            logger.exception("plan create failed session=%s", session_id)
            plan_error = str(exc)
        plan_duration_ms = int((time.perf_counter() - plan_started) * 1000)

        if plan is None:
            assistant_response = (
                "I couldn't build a plan — plan storage is unavailable."
                if plan_error is None
                else f"I couldn't build a plan: {plan_error}"
            )
            tool_result_plan: dict = {
                "status": "error",
                "error": plan_error or "plan storage unavailable",
            }
            trace_status = "error"
            plan_id_str: Optional[str] = None
            memory_ids_for_trace: list[uuid.UUID] = []
        else:
            step_lines = [
                f"{s['step_number']}. {s['title']} "
                f"({s['assigned_agent'] or 'unassigned'}) — {s['description']}"
                for s in plan["steps"]
            ]
            assistant_response = (
                f"ATLAS drafted a plan for «{plan['title']}»:\n\n"
                + "\n".join(step_lines)
                + f"\n\nPlan id: {plan['id']} "
                f"(status: {plan['status']}, {plan['total_steps']} steps). "
                "Inspect it in the Plans admin viewer or via "
                f"GET /plans/{plan['id']}."
            )
            tool_result_plan = {
                "status": "ok",
                "plan_id": str(plan["id"]),
                "total_steps": plan["total_steps"],
                "selected_agent": plan["selected_agent"],
            }
            trace_status = "ok"
            plan_id_str = str(plan["id"])
            memory_ids_for_trace = []

        completed_at = datetime.now(timezone.utc)
        try:
            await _persist_exchange(
                session_uuid=session_uuid,
                scope_type=scope_type,
                scope_id=scope_id,
                user_message=request.message,
                assistant_response=assistant_response,
                model_name=None,
                placeholder=False,
                started_at=started_at,
                completed_at=completed_at,
                agent_name=ORCHESTRATOR_NAME,
                tool_name="plan_create",
                tool_result=tool_result_plan,
                workspace_id=workspace_uuid,
            )
        except Exception:
            logger.exception(
                "Failed to persist plan-intent exchange session=%s", session_id
            )

        await write_trace(
            session_id=session_uuid,
            user_id=current.id,
            trace_type="execution_plan_created",
            status=trace_status,
            selected_agent=ORCHESTRATOR_NAME,
            user_message=request.message,
            tool_name="plan_create",
            tool_result={**tool_result_plan, "plan_id": plan_id_str},
            duration_ms=plan_duration_ms,
            error_message=plan_error,
            workspace_id=workspace_uuid,
        )

        return ChatResponse(
            session_id=session_id,
            agent=PERSONA_NAME,
            selected_agent=ORCHESTRATOR_NAME,
            routing_matched_keywords=[],
            model_endpoint=None,
            response=assistant_response,
            placeholder=False,
            created_at=completed_at.isoformat(),
        )

    # DB-managed routing keywords (active version metadata) override the Python
    # constants per agent; best-effort, falls back to constants on any failure.
    try:
        routing_overrides = await load_active_routing_keywords()
    except Exception:
        logger.exception("routing keyword load failed; using Python constants")
        routing_overrides = {}
    selected_agent, matched_keywords = select_subagent(
        request.message, keyword_overrides=routing_overrides
    )
    # Explicit web-search request → force PULSE so the web_search tool's
    # allowed_agents=['PULSE'] governance check passes and the answer is framed
    # as research.
    web_search_query = _match_web_search_intent(request.message)
    if web_search_query and selected_agent != PULSE_NAME:
        logger.info(
            "web-search intent overrides routing to PULSE: session=%s "
            "prior_agent=%s",
            session_id,
            selected_agent,
        )
        selected_agent = PULSE_NAME
        if not matched_keywords:
            matched_keywords = ["web search"]
    # Explicit "save as a draft" / "save as a proposal" request → route to
    # SIGNAL / CHRONOS so the chat-to-draft hook (which only fires when
    # selected_agent is SIGNAL/CHRONOS) runs. Without this, wording like
    # "research X and draft an email, then save as a draft" routes to PULSE on
    # its research keywords and the explicit save request is silently dropped.
    # A web-search turn keeps precedence (handled above).
    elif _match_signal_save_intent(request.message) and selected_agent != SIGNAL_NAME:
        logger.info(
            "save-draft intent overrides routing to SIGNAL: session=%s prior_agent=%s",
            session_id,
            selected_agent,
        )
        selected_agent = SIGNAL_NAME
        if not matched_keywords:
            matched_keywords = ["save draft"]
    elif (
        _match_chronos_save_intent(request.message)
        and not _match_signal_save_intent(request.message)
        and selected_agent != CHRONOS_NAME
    ):
        logger.info(
            "save-proposal intent overrides routing to CHRONOS: session=%s prior_agent=%s",
            session_id,
            selected_agent,
        )
        selected_agent = CHRONOS_NAME
        if not matched_keywords:
            matched_keywords = ["save proposal"]
    # Semantic routing fallback: keyword routing AND every explicit intent override
    # found nothing (selected_agent is still the Cora persona), so the message used
    # phrasing the keyword lists don't cover. When enabled, one cheap LLM
    # classification picks a specialist; fail-open — it can only move OFF Cora, never
    # override a deterministic match. (Verified vs embeddings, which were unreliable.)
    if selected_agent == PERSONA_NAME and settings.semantic_routing_enabled:
        sem_agent, sem_raw = await semantic_route(request.message)
        if sem_agent != PERSONA_NAME:
            logger.info(
                "semantic routing fallback: session=%s selected_agent=%s raw=%r",
                session_id,
                sem_agent,
                sem_raw,
            )
            selected_agent = sem_agent
            matched_keywords = ["(semantic)"]
    logger.info(
        "agent routing: session=%s user_id=%s selected_agent=%s "
        "matched_keywords=%s web_search=%s",
        session_id,
        current.id,
        selected_agent,
        matched_keywords,
        bool(web_search_query),
    )

    # Chat-Native Email Review & Approval Workflow v1.9. After routing, intercept
    # explicit SIGNAL email-lifecycle commands (create/show/revise/approve/reject/
    # archive/prepare/simulate/safety-check) and handle them deterministically —
    # governed + draft-first preserved, nothing is ever sent. Ambiguous follow-ups
    # with no active draft fall through to normal chat. (The Agent Test Harness
    # uses a different endpoint, so this never runs there — spec #19.)
    # Chat-Native Approval Queue Management v2.2. Numbered pending-draft / pending-
    # intent queues + selection by number/ordinal ("approve item 2", "reject the
    # latest draft"). Checked first so numbered commands route here; non-numbered
    # follow-ups ("approve it") fall through to the v1.9 handlers. Non-executing.
    _queue_cmd = chat_approval_queue.detect_queue_command(request.message)
    if _queue_cmd is not None:
        _q_handled, _q_text = await chat_approval_queue.handle_queue_command(
            _queue_cmd, message=request.message, session_uuid=session_uuid,
            user_id=current.id, workspace_uuid=workspace_uuid, scope_type=scope_type,
            is_admin=(current.role == "admin"),
        )
        if _q_handled and _q_text is not None:
            _completed_at = datetime.now(timezone.utc)
            try:
                await _persist_exchange(
                    session_uuid=session_uuid, scope_type=scope_type, scope_id=scope_id,
                    user_message=request.message, assistant_response=_q_text,
                    model_name=None, placeholder=False, started_at=started_at,
                    completed_at=_completed_at, agent_name=SIGNAL_NAME,
                    workspace_id=workspace_uuid,
                )
            except Exception:
                logger.exception("persist approval-queue exchange failed session=%s", session_id)
            return ChatResponse(
                session_id=session_id, agent=PERSONA_NAME, selected_agent=SIGNAL_NAME,
                routing_matched_keywords=matched_keywords, model_endpoint=endpoint,
                response=_q_text, placeholder=False, created_at=_completed_at.isoformat(),
            )

    # Tier-2 Screen Vision (opt-in). The user deliberately shared a screenshot via the
    # composer's "Share screen" button (browser getDisplayMedia, one frame, no auto-
    # capture) — the attached image IS the intent, so it short-circuits first. FAILS
    # CLOSED: nothing reaches a vision model unless SCREEN_VISION_ENABLED + a configured
    # local model. Image bytes are never stored; only metadata is audited.
    if request.screen_image:
        _sv_handled, _sv_text = await screen_vision.handle_screen_vision_turn(
            message=request.message, image_data=request.screen_image,
            session_uuid=session_uuid, user_id=current.id,
            workspace_uuid=workspace_uuid, is_admin=(current.role == "admin"),
        )
        if _sv_handled and _sv_text is not None:
            _completed_at = datetime.now(timezone.utc)
            try:
                await _persist_exchange(
                    session_uuid=session_uuid, scope_type=scope_type, scope_id=scope_id,
                    user_message=request.message, assistant_response=_sv_text,
                    model_name=None, placeholder=False, started_at=started_at,
                    completed_at=_completed_at, agent_name=PERSONA_NAME,
                    workspace_id=workspace_uuid,
                )
            except Exception:
                logger.exception("persist screen-vision exchange failed session=%s", session_id)
            return ChatResponse(
                session_id=session_id, agent=PERSONA_NAME, selected_agent=PERSONA_NAME,
                routing_matched_keywords=matched_keywords, model_endpoint=endpoint,
                response=_sv_text, placeholder=False, created_at=_completed_at.isoformat(),
            )

    # Per-user default provider ("make outlook my default calendar"). Checked before the
    # inbox/calendar handlers so the phrase isn't swallowed by their detection.
    _pd_cmd = provider_defaults.detect_default_command(request.message)
    if _pd_cmd is not None:
        _pd_handled, _pd_text = await provider_defaults.handle_default_command(
            _pd_cmd, user_id=current.id, session_uuid=session_uuid,
            workspace_uuid=workspace_uuid,
        )
        if _pd_handled and _pd_text is not None:
            _completed_at = datetime.now(timezone.utc)
            try:
                await _persist_exchange(
                    session_uuid=session_uuid, scope_type=scope_type, scope_id=scope_id,
                    user_message=request.message, assistant_response=_pd_text,
                    model_name=None, placeholder=False, started_at=started_at,
                    completed_at=_completed_at, agent_name=PERSONA_NAME,
                    workspace_id=workspace_uuid,
                )
            except Exception:
                logger.exception("persist provider-default exchange failed session=%s", session_id)
            return ChatResponse(
                session_id=session_id, agent=PERSONA_NAME, selected_agent=PERSONA_NAME,
                routing_matched_keywords=matched_keywords, model_endpoint=endpoint,
                response=_pd_text, placeholder=False, created_at=_completed_at.isoformat(),
            )

    # Per-user default WRITE calendar ("make Work my default calendar"). A hint-less
    # create then targets that calendar instead of primary. Checked AFTER the provider
    # default (so "make google my default calendar" stays a provider default — no
    # calendar name) and BEFORE the calendar handler (so the phrase isn't taken as a
    # create). Resolving the calendar is governed by the same read path as a create.
    _cd_cmd = chat_calendar.detect_calendar_default_command(request.message)
    if _cd_cmd is not None:
        _cd_handled, _cd_text = await chat_calendar.handle_calendar_default_command(
            _cd_cmd, message=request.message, session_uuid=session_uuid,
            user_id=current.id, workspace_uuid=workspace_uuid,
        )
        if _cd_handled and _cd_text is not None:
            _completed_at = datetime.now(timezone.utc)
            try:
                await _persist_exchange(
                    session_uuid=session_uuid, scope_type=scope_type, scope_id=scope_id,
                    user_message=request.message, assistant_response=_cd_text,
                    model_name=None, placeholder=False, started_at=started_at,
                    completed_at=_completed_at, agent_name=CHRONOS_NAME,
                    workspace_id=workspace_uuid,
                )
            except Exception:
                logger.exception("persist calendar-default exchange failed session=%s", session_id)
            return ChatResponse(
                session_id=session_id, agent=PERSONA_NAME, selected_agent=CHRONOS_NAME,
                routing_matched_keywords=matched_keywords, model_endpoint=endpoint,
                response=_cd_text, placeholder=False, created_at=_completed_at.isoformat(),
            )

    # Daily Briefing (composite: CHRONOS schedule + SIGNAL inbox + PULSE news). A
    # read-only digest of the user's day — reuses the governed cross-provider calendar
    # and inbox reads (each section fails closed independently) plus the news_briefing
    # DB read. No writes, no sends. Checked before the single-domain inbox/calendar
    # handlers so "brief me on my day" isn't partially swallowed by them.
    if chat_briefing.detect_briefing_command(request.message):
        _br_handled, _br_text = await chat_briefing.handle_briefing_command(
            message=request.message, session_uuid=session_uuid, user_id=current.id,
            workspace_uuid=workspace_uuid,
        )
        if _br_handled and _br_text is not None:
            _completed_at = datetime.now(timezone.utc)
            try:
                await _persist_exchange(
                    session_uuid=session_uuid, scope_type=scope_type, scope_id=scope_id,
                    user_message=request.message, assistant_response=_br_text,
                    model_name=None, placeholder=False, started_at=started_at,
                    completed_at=_completed_at, agent_name=PERSONA_NAME,
                    workspace_id=workspace_uuid,
                )
            except Exception:
                logger.exception("persist daily-briefing exchange failed session=%s", session_id)
            return ChatResponse(
                session_id=session_id, agent=PERSONA_NAME, selected_agent=PERSONA_NAME,
                routing_matched_keywords=matched_keywords, model_endpoint=endpoint,
                response=_br_text, placeholder=False, created_at=_completed_at.isoformat(),
            )

    # CHRONOS Smart Scheduling (free/busy + find-a-time). "When am I free this week?" /
    # "find 30 min tomorrow afternoon" → READ-ONLY open-slot search across all calendars;
    # "schedule 30 min with sam@x next tue" → finds the slot then STAGES it through the
    # calendar confirm-before-write path (a later "confirm" books it). Checked before the
    # calendar handler so a duration-based find isn't taken as a normal create; a bare
    # "confirm" after a staged booking is resolved by the calendar block (has_pending).
    _sched_cmd = chat_scheduling.detect_scheduling_command(request.message)
    if _sched_cmd is not None:
        _sc_handled, _sc_text = await chat_scheduling.handle_scheduling_command(
            message=request.message, payload=_sched_cmd[1], session_uuid=session_uuid,
            user_id=current.id, workspace_uuid=workspace_uuid,
        )
        if _sc_handled and _sc_text is not None:
            _completed_at = datetime.now(timezone.utc)
            try:
                await _persist_exchange(
                    session_uuid=session_uuid, scope_type=scope_type, scope_id=scope_id,
                    user_message=request.message, assistant_response=_sc_text,
                    model_name=None, placeholder=False, started_at=started_at,
                    completed_at=_completed_at, agent_name=CHRONOS_NAME,
                    workspace_id=workspace_uuid,
                )
            except Exception:
                logger.exception("persist scheduling exchange failed session=%s", session_id)
            return ChatResponse(
                session_id=session_id, agent=PERSONA_NAME, selected_agent=CHRONOS_NAME,
                routing_matched_keywords=matched_keywords, model_endpoint=endpoint,
                response=_sc_text, placeholder=False, created_at=_completed_at.isoformat(),
            )

    # Chat-Native Inbox Assistant v2.3. Read-only inbox Q&A (list/search/summarize/
    # thread/draft-reply). Governed + FAILS CLOSED — no provider API call and no
    # token access in this phase. Checked before the email-lifecycle so "draft a
    # reply to this email" routes here (internal SIGNAL draft only).
    _inbox_cmd = chat_inbox.detect_inbox_command(request.message)
    if _inbox_cmd is not None:
        _ib_handled, _ib_text = await chat_inbox.handle_inbox_command(
            _inbox_cmd, message=request.message, session_uuid=session_uuid,
            user_id=current.id, workspace_uuid=workspace_uuid, scope_type=scope_type,
            is_admin=(current.role == "admin"),
        )
        if _ib_handled and _ib_text is not None:
            _completed_at = datetime.now(timezone.utc)
            try:
                await _persist_exchange(
                    session_uuid=session_uuid, scope_type=scope_type, scope_id=scope_id,
                    user_message=request.message, assistant_response=_ib_text,
                    model_name=None, placeholder=False, started_at=started_at,
                    completed_at=_completed_at, agent_name=SIGNAL_NAME,
                    workspace_id=workspace_uuid,
                )
            except Exception:
                logger.exception("persist inbox exchange failed session=%s", session_id)
            return ChatResponse(
                session_id=session_id, agent=PERSONA_NAME, selected_agent=SIGNAL_NAME,
                routing_matched_keywords=matched_keywords, model_endpoint=endpoint,
                response=_ib_text, placeholder=False, created_at=_completed_at.isoformat(),
            )

    # Chat-Native Calendar Assistant (CHRONOS Calendar CRUD v1.0). Full read +
    # create/update/delete calendar ops, confirm-before-write. FAILS CLOSED: reads
    # need the calendar_read flag; writes additionally need the calendar_write flag
    # AND the global kill switch. A blocked CREATE falls back to a review-only
    # internal proposal. A bare "confirm"/"cancel" only routes here when a calendar
    # action is staged for this session (otherwise it falls through untouched).
    _cal_cmd = chat_calendar.detect_calendar_command(request.message)
    _cal_confirm = chat_calendar.detect_confirmation(request.message)
    _cal_select = chat_calendar.detect_selection(request.message)
    _cal_list_action = chat_calendar.detect_list_action(request.message)
    _cal_active = _cal_cmd is not None or (
        (_cal_confirm is not None or _cal_select is not None or _cal_list_action is not None)
        and await chat_calendar.has_pending(session_uuid)
    )
    if _cal_active:
        _cal_handled, _cal_text = await chat_calendar.handle_calendar_turn(
            message=request.message, command=_cal_cmd, confirmation=_cal_confirm,
            selection=_cal_select, list_action=_cal_list_action, session_uuid=session_uuid,
            user_id=current.id, workspace_uuid=workspace_uuid, scope_type=scope_type,
            is_admin=(current.role == "admin"),
        )
        if _cal_handled and _cal_text is not None:
            _completed_at = datetime.now(timezone.utc)
            try:
                await _persist_exchange(
                    session_uuid=session_uuid, scope_type=scope_type, scope_id=scope_id,
                    user_message=request.message, assistant_response=_cal_text,
                    model_name=None, placeholder=False, started_at=started_at,
                    completed_at=_completed_at, agent_name=CHRONOS_NAME,
                    workspace_id=workspace_uuid,
                )
            except Exception:
                logger.exception("persist calendar exchange failed session=%s", session_id)
            return ChatResponse(
                session_id=session_id, agent=PERSONA_NAME, selected_agent=CHRONOS_NAME,
                routing_matched_keywords=matched_keywords, model_endpoint=endpoint,
                response=_cal_text, placeholder=False, created_at=_completed_at.isoformat(),
            )

    # Chat-Native Provider Simulation & Payload Inspection v2.1. Richer payload
    # inspection / Gmail-vs-Outlook comparison (simulation-only, no API call, no
    # secrets); checked before the email-lifecycle so "simulate/inspect/compare"
    # phrases get the detailed renderer.
    _sim_cmd = chat_provider_simulation.detect_simulation_command(request.message)
    if _sim_cmd is not None:
        _sim_handled, _sim_text = await chat_provider_simulation.handle_simulation_command(
            _sim_cmd, message=request.message, session_uuid=session_uuid,
            user_id=current.id, workspace_uuid=workspace_uuid,
            is_admin=(current.role == "admin"),
        )
        if _sim_handled and _sim_text is not None:
            _completed_at = datetime.now(timezone.utc)
            try:
                await _persist_exchange(
                    session_uuid=session_uuid, scope_type=scope_type, scope_id=scope_id,
                    user_message=request.message, assistant_response=_sim_text,
                    model_name=None, placeholder=False, started_at=started_at,
                    completed_at=_completed_at, agent_name=SIGNAL_NAME,
                    workspace_id=workspace_uuid,
                )
            except Exception:
                logger.exception("persist provider-simulation exchange failed session=%s", session_id)
            return ChatResponse(
                session_id=session_id, agent=PERSONA_NAME, selected_agent=SIGNAL_NAME,
                routing_matched_keywords=matched_keywords, model_endpoint=endpoint,
                response=_sim_text, placeholder=False,
                created_at=_completed_at.isoformat(),
            )

    _email_cmd = chat_email.detect_email_command(request.message)
    if _email_cmd is not None:
        _handled, _email_text = await chat_email.handle_email_command(
            _email_cmd, message=request.message, session_uuid=session_uuid,
            user_id=current.id, workspace_uuid=workspace_uuid, scope_type=scope_type,
            is_admin=(current.role == "admin"),
        )
        if _handled and _email_text is not None:
            _completed_at = datetime.now(timezone.utc)
            try:
                await _persist_exchange(
                    session_uuid=session_uuid, scope_type=scope_type, scope_id=scope_id,
                    user_message=request.message, assistant_response=_email_text,
                    model_name=None, placeholder=False, started_at=started_at,
                    completed_at=_completed_at, agent_name=SIGNAL_NAME,
                    workspace_id=workspace_uuid,
                )
            except Exception:
                logger.exception("persist email-lifecycle exchange failed session=%s", session_id)
            return ChatResponse(
                session_id=session_id, agent=PERSONA_NAME, selected_agent=SIGNAL_NAME,
                routing_matched_keywords=matched_keywords, model_endpoint=endpoint,
                response=_email_text, placeholder=False,
                created_at=_completed_at.isoformat(),
            )

    # Chat-Native Governance Explanation & Audit Trail v2.0. Answer governance/
    # audit questions ("Why was this blocked?", "Show me the governance trail")
    # read-only from the audit tables, resolving the active draft/intent from chat
    # context. No execution, no secrets.
    _gov_kind = chat_governance.detect_governance_question(request.message)
    if _gov_kind is not None:
        _gov_handled, _gov_text = await chat_governance.handle_governance_question(
            _gov_kind, message=request.message, session_uuid=session_uuid,
            user_id=current.id, workspace_uuid=workspace_uuid,
            is_admin=(current.role == "admin"),
        )
        if _gov_handled and _gov_text is not None:
            _completed_at = datetime.now(timezone.utc)
            try:
                await _persist_exchange(
                    session_uuid=session_uuid, scope_type=scope_type, scope_id=scope_id,
                    user_message=request.message, assistant_response=_gov_text,
                    model_name=None, placeholder=False, started_at=started_at,
                    completed_at=_completed_at, agent_name=SIGNAL_NAME,
                    workspace_id=workspace_uuid,
                )
            except Exception:
                logger.exception("persist governance-explanation exchange failed session=%s", session_id)
            return ChatResponse(
                session_id=session_id, agent=PERSONA_NAME, selected_agent=SIGNAL_NAME,
                routing_matched_keywords=matched_keywords, model_endpoint=endpoint,
                response=_gov_text, placeholder=False,
                created_at=_completed_at.isoformat(),
            )

    # Resolve system prompt: prefer active DB version of the selected agent,
    # fall back to the Python constant. Always log which path was taken.
    system_prompt, prompt_source, _active_version = await resolve_agent_prompt(
        selected_agent
    )
    # Voice mode: ask the model to answer in short, spoken, markdown-free sentences.
    # The response is also normalized post-hoc in _finalize as a backstop.
    if request.speakable:
        system_prompt = f"{system_prompt}\n\n{SPEAKABLE_STYLE}"
    # The current date/time is injected NEXT TO the user turn (via _build_prompt's
    # datetime_line), not prepended to the system prompt — keeping it out of the
    # cached prompt prefix so per-minute changes don't force a cold prefill every
    # request. The model still sees "today" authoritatively.
    datetime_line = current_datetime_preamble()
    logger.info(
        "agent prompt source: session=%s selected_agent=%s source=%s",
        session_id,
        selected_agent,
        prompt_source,
    )

    if not llm.is_chat_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Chat model backend '{llm.chat_backend()}' is not configured",
        )

    try:
        history = await _load_recent_history(session_uuid, scope_type, scope_id)
    except Exception:
        logger.exception("Failed to load history session=%s; continuing without it", session_id)
        history = []

    first_ts = history[0]["created_at"].isoformat() if history else None
    last_ts = history[-1]["created_at"].isoformat() if history else None
    logger.info(
        "history loaded: session=%s user_id=%s scope_type=%s scope_id=%s "
        "db_message_count=%s first_created_at=%s last_created_at=%s",
        session_id,
        current.id,
        scope_type,
        scope_id,
        len(history),
        first_ts,
        last_ts,
    )

    mem_started = time.perf_counter()
    mem_error: Optional[str] = None
    semantic_enabled = (
        schema_state.is_pgvector_available() and is_embedding_configured()
    )
    semantic_rows: list[dict] = []
    semantic_status = "skipped"
    semantic_used_chunks = False
    if semantic_enabled:
        try:
            sem = await semantic_search(
                request.message,
                limit=MEMORY_SEARCH_LIMIT,
                user_id=current.id,
                workspace_id=workspace_uuid,
            )
            semantic_status = sem["status"]
            semantic_rows = sem.get("rows", [])
            semantic_used_chunks = bool(sem.get("used_chunks"))
        except Exception as exc:
            logger.exception(
                "semantic search failed session=%s; falling back to keyword",
                session_id,
            )
            semantic_status = "error"
            mem_error = str(exc)

    try:
        keyword_rows = await search_memory(
            request.message,
            limit=MEMORY_SEARCH_LIMIT,
            user_id=current.id,
            workspace_id=workspace_uuid,
        )
    except Exception as exc:
        logger.exception(
            "keyword search failed session=%s; continuing without memory",
            session_id,
        )
        keyword_rows = []
        if mem_error is None:
            mem_error = str(exc)

    # Hybrid merge via Reciprocal Rank Fusion (not "all semantic, then keyword"):
    # an exact keyword hit the embedding ranks low — e.g. a nickname — must not be
    # buried past the injection cap. See _rank_fuse_memories.
    memory_candidates = _rank_fuse_memories(
        semantic_rows, keyword_rows, limit=MEMORY_SEARCH_LIMIT
    )

    mem_duration_ms = int((time.perf_counter() - mem_started) * 1000)
    memories_for_prompt = memory_candidates[:MEMORIES_IN_PROMPT]
    memory_ids_in_prompt: list[uuid.UUID] = []
    for m in memories_for_prompt:
        mid = m.get("id")
        if mid is None:
            continue
        try:
            memory_ids_in_prompt.append(
                mid if isinstance(mid, uuid.UUID) else uuid.UUID(str(mid))
            )
        except (ValueError, TypeError):
            pass
    logger.info(
        "memory retrieval: session=%s user_id=%s scope_type=%s scope_id=%s "
        "semantic_search_enabled=%s semantic_status=%s semantic_matches=%s "
        "keyword_matches=%s merged=%s memories_injected=%s duration_ms=%s",
        session_id,
        current.id,
        scope_type,
        scope_id,
        semantic_enabled,
        semantic_status,
        len(semantic_rows),
        len(keyword_rows),
        len(memory_candidates),
        len(memories_for_prompt),
        mem_duration_ms,
    )
    # Classify the retrieval mode for downstream observability.
    semantic_count = len(semantic_rows)
    keyword_count = len(keyword_rows)
    if not semantic_enabled:
        retrieval_mode = "keyword"
    elif semantic_status in ("unavailable", "no_embedding", "error"):
        retrieval_mode = "fallback_keyword" if keyword_count else "unavailable"
    elif semantic_status == "ok":
        if semantic_count > 0 and keyword_count > 0:
            retrieval_mode = "hybrid"
        elif semantic_count > 0:
            retrieval_mode = "semantic"
        elif keyword_count > 0:
            retrieval_mode = "fallback_keyword"
        else:
            # semantic ran cleanly but matched nothing and keyword also empty
            retrieval_mode = "semantic"
        # When the semantic hits came from chunk embeddings, mark the variant.
        if semantic_used_chunks and retrieval_mode in ("semantic", "hybrid"):
            retrieval_mode += "_chunks"
    else:
        retrieval_mode = "keyword"

    # Top semantic similarity scores (only for rows that actually came from
    # semantic search; useful in the trace viewer when debugging recall).
    semantic_scores = [
        round(float(r.get("similarity", 0.0)), 4)
        for r in semantic_rows[:MEMORIES_IN_PROMPT]
        if r.get("similarity") is not None
    ]

    logger.info(
        "memory retrieval mode: session=%s retrieval_mode=%s semantic_status=%s "
        "semantic_matches=%s keyword_matches=%s injected=%s",
        session_id,
        retrieval_mode,
        semantic_status,
        semantic_count,
        keyword_count,
        len(memories_for_prompt),
    )

    await write_trace(
        session_id=session_uuid,
        user_id=current.id,
        trace_type="memory_retrieval",
        status="error" if mem_error else "ok",
        selected_agent=ORCHESTRATOR_NAME,
        user_message=request.message,
        memory_count=len(memories_for_prompt),
        memory_ids=memory_ids_in_prompt,
        duration_ms=mem_duration_ms,
        error_message=mem_error,
        workspace_id=workspace_uuid,
        tool_name="memory_retrieval",
        tool_result={
            "retrieval_mode": retrieval_mode,
            "semantic_enabled": semantic_enabled,
            "semantic_status": semantic_status,
            "semantic_matches": semantic_count,
            "keyword_matches": keyword_count,
            "merged_candidates": len(memory_candidates),
            "memories_injected": len(memories_for_prompt),
            "memory_ids": [str(m) for m in memory_ids_in_prompt],
            "semantic_scores": semantic_scores,
            "pgvector_available": schema_state.is_pgvector_available(),
            "embedding_configured": is_embedding_configured(),
        },
    )

    workspace_context_text: Optional[str] = None
    workspace_context_meta: Optional[dict] = None
    workspace_context_error: Optional[str] = None
    if workspace_uuid is not None:
        try:
            ctx = await get_chat_context(workspace_uuid)
        except Exception as exc:
            logger.exception(
                "workspace context fetch failed session=%s; continuing without it",
                session_id,
            )
            ctx = None
            workspace_context_error = str(exc)
        if ctx:
            workspace_context_text = ctx["text"]
            workspace_context_meta = ctx["metadata"]
            logger.info(
                "workspace context injected: session=%s workspace_id=%s "
                "workspace_name=%r chars=%s",
                session_id,
                workspace_uuid,
                ctx["metadata"]["workspace_name"],
                ctx["metadata"]["chars"],
            )

    # ---------- Screen context (v0.1) ----------
    # The UI reports what screen/entity the user is viewing; sanitize and
    # re-resolve server-side (owner-scoped), then inject a compact block.
    screen_context_text: Optional[str] = None
    screen_context_meta: Optional[dict] = None
    if request.screen_context:
        built = await build_screen_context_block(
            request.screen_context,
            user_id=current.id,
            is_admin=(current.role == "admin"),
        )
        if built:
            screen_context_text, screen_context_meta = built
            logger.info(
                "screen context injected: session=%s section=%s entity=%s chars=%s",
                session_id,
                screen_context_meta.get("section"),
                screen_context_meta.get("entity_type"),
                screen_context_meta.get("chars"),
            )

    # ---------- PULSE live web search (governed) ----------
    # Dispatched only when an explicit web-search cue matched. Results are
    # injected into the prompt as cited evidence; PULSE synthesizes from them.
    web_results_text: Optional[str] = None
    web_search_meta: Optional[dict] = None
    if web_search_query:
        ws_tool = await _fetch_tool("web_search")
        if ws_tool is None:
            logger.warning(
                "web_search intent matched but tool row missing session=%s",
                session_id,
            )
        else:
            ws_decision = await check_permission(
                ws_tool,
                agent_name=PULSE_NAME,
                user_id=current.id,
                is_admin=(current.role == "admin"),
            )
            if not ws_decision.allowed:
                logger.info(
                    "web_search denied by governance: session=%s reason=%s",
                    session_id,
                    ws_decision.reason,
                )
                await log_execution_attempt(
                    tool_name="web_search",
                    agent_name=PULSE_NAME,
                    session_id=session_uuid,
                    user_id=current.id,
                    scope_type=scope_type,
                    allowed=False,
                    duration_ms=None,
                    status="denied",
                    error_message=ws_decision.reason,
                )
                web_search_meta = {
                    "query": web_search_query,
                    "status": "denied",
                    "reason": ws_decision.reason,
                }
            else:
                _ws_started = time.perf_counter()
                try:
                    ws_result = await dispatch_tool(
                        ws_tool,
                        {
                            "session_id": session_id,
                            "user_message": request.message,
                            "arguments": {"query": web_search_query},
                            "metadata": {"source": "pulse_web_search"},
                        },
                    )
                    ws_duration_ms = int(
                        (time.perf_counter() - _ws_started) * 1000
                    )
                    ws_status = ws_result.get("status")
                    ws_rows = ws_result.get("results") or []
                    if ws_status == "ok" and ws_rows:
                        web_results_text = _format_web_results_block(
                            web_search_query, ws_rows
                        )
                    web_search_meta = {
                        "query": web_search_query,
                        "status": ws_status,
                        "count": len(ws_rows),
                        "engine": ws_result.get("engine"),
                    }
                    await log_execution_attempt(
                        tool_name="web_search",
                        agent_name=PULSE_NAME,
                        session_id=session_uuid,
                        user_id=current.id,
                        scope_type=scope_type,
                        allowed=True,
                        duration_ms=ws_duration_ms,
                        status=ws_status or "ok",
                        error_message=ws_result.get("error"),
                    )
                    await write_trace(
                        session_id=session_uuid,
                        user_id=current.id,
                        trace_type="web_search",
                        status="ok" if ws_status == "ok" else "error",
                        selected_agent=PULSE_NAME,
                        user_message=request.message,
                        tool_name="web_search",
                        tool_result=web_search_meta,
                        duration_ms=ws_duration_ms,
                        workspace_id=workspace_uuid,
                        error_message=ws_result.get("error"),
                    )
                    logger.info(
                        "web_search done: session=%s status=%s results=%s "
                        "duration_ms=%s",
                        session_id,
                        ws_status,
                        len(ws_rows),
                        ws_duration_ms,
                    )
                except (httpx.HTTPError, ValueError) as exc:
                    ws_duration_ms = int(
                        (time.perf_counter() - _ws_started) * 1000
                    )
                    logger.exception(
                        "web_search dispatch failed session=%s", session_id
                    )
                    web_search_meta = {
                        "query": web_search_query,
                        "status": "error",
                        "error": str(exc),
                    }
                    await log_execution_attempt(
                        tool_name="web_search",
                        agent_name=PULSE_NAME,
                        session_id=session_uuid,
                        user_id=current.id,
                        scope_type=scope_type,
                        allowed=True,
                        duration_ms=ws_duration_ms,
                        status="error",
                        error_message=str(exc),
                    )
                    await write_trace(
                        session_id=session_uuid,
                        user_id=current.id,
                        trace_type="web_search",
                        status="error",
                        selected_agent=PULSE_NAME,
                        user_message=request.message,
                        tool_name="web_search",
                        tool_result=web_search_meta,
                        duration_ms=ws_duration_ms,
                        workspace_id=workspace_uuid,
                        error_message=str(exc),
                    )

    prompt, prompt_stats = _build_prompt(
        history,
        request.message,
        memories=memories_for_prompt,
        system_prompt=system_prompt,
        workspace_context=workspace_context_text,
        web_results=web_results_text,
        screen_context=screen_context_text,
        datetime_line=datetime_line,
    )

    def _workspace_trace_metadata() -> dict:
        meta_out = _screen_trace_metadata()
        if workspace_context_text and workspace_context_meta:
            m = workspace_context_meta
            return meta_out | {
                "workspace_context_injected": True,
                "workspace_id": str(workspace_uuid) if workspace_uuid else None,
                "workspace_name": m.get("workspace_name"),
                "workspace_context_chars": prompt_stats.get(
                    "workspace_context_chars", m.get("chars", 0)
                ),
                "workspace_context_sources": {
                    "memory_count": m.get("memory_total", 0),
                    "embedded_memory_count": m.get("memory_embedded", 0),
                    "active_plans_count": m.get("plans_active", 0),
                    "queued_jobs_count": m.get("jobs_active", 0),
                    "available_agents_count": len(m.get("agents") or []),
                    "available_tools_count": len(m.get("tools") or []),
                    "healthy_mcp_servers_count": len(
                        m.get("mcp_servers") or []
                    ),
                },
            }
        meta: dict = meta_out | {
            "workspace_context_injected": False,
            "workspace_id": str(workspace_uuid) if workspace_uuid else None,
        }
        if workspace_context_error:
            meta["workspace_context_error"] = workspace_context_error
        return meta

    def _screen_trace_metadata() -> dict:
        if screen_context_text and screen_context_meta:
            return {
                "screen_context_injected": True,
                "screen_section": screen_context_meta.get("section"),
                "screen_entity_type": screen_context_meta.get("entity_type"),
                "screen_entity_resolved": screen_context_meta.get(
                    "entity_resolved", False
                ),
                "screen_context_chars": screen_context_meta.get("chars", 0),
            }
        return {"screen_context_injected": False}

    logger.info(
        "ollama prompt session=%s selected_agent=%s history_loaded=%s "
        "history_included=%s history_dropped=%s truncated=%s "
        "memories_in_prompt=%s prompt_chars=%s est_tokens=%s max_chars=%s",
        session_id,
        selected_agent,
        len(history),
        prompt_stats["history_included"],
        prompt_stats["history_dropped"],
        prompt_stats["truncated"],
        prompt_stats["memories_included"],
        prompt_stats["chars"],
        prompt_stats["est_tokens"],
        MAX_PROMPT_CHARS,
    )

    # Deterministic delegations: created before the LLM call, completed/failed
    # alongside the LLM result. Self-delegations to Cora are skipped (Cora is
    # the persona, not a peer agent). Depth limit (3 concurrent per scope) is
    # enforced inside create_delegation.
    routing_delegation_id: Optional[uuid.UUID] = None
    memory_delegation_id: Optional[uuid.UUID] = None
    if selected_agent and selected_agent != PERSONA_NAME:
        try:
            row = await create_delegation(
                from_agent=ORCHESTRATOR_NAME,
                to_agent=selected_agent,
                delegation_reason=(
                    f"ATLAS routed user message to {selected_agent} "
                    f"(matched keywords: {matched_keywords or 'none'})"
                ),
                session_id=session_uuid,
                workspace_id=workspace_uuid,
                user_id=current.id,
                input_payload={
                    "user_message_preview": request.message[:200],
                    "matched_keywords": matched_keywords,
                },
                initial_status="running",
            )
            routing_delegation_id = row["id"]
        except DelegationError as exc:
            logger.warning(
                "routing delegation skipped: session=%s reason=%s",
                session_id,
                exc,
            )
        if memories_for_prompt:
            try:
                row = await create_delegation(
                    from_agent="SCRIBE",
                    to_agent=selected_agent,
                    delegation_reason=(
                        f"SCRIBE injected {len(memories_for_prompt)} "
                        f"memor{'y' if len(memories_for_prompt) == 1 else 'ies'} "
                        f"into the {selected_agent} prompt"
                    ),
                    session_id=session_uuid,
                    workspace_id=workspace_uuid,
                    user_id=current.id,
                    input_payload={
                        "memory_count": len(memories_for_prompt),
                        "memory_ids": [str(m) for m in memory_ids_in_prompt],
                    },
                    initial_status="completed",
                )
                memory_delegation_id = row["id"]
                # SCRIBE's work was synchronous — already done at retrieval time.
                if memory_delegation_id is not None:
                    await complete_delegation(
                        memory_delegation_id,
                        output_payload={"delivered": True},
                        user_id=current.id,
                    )
            except DelegationError as exc:
                logger.warning(
                    "memory delegation skipped: session=%s reason=%s",
                    session_id,
                    exc,
                )

    # ---- finalization shared by the JSON and streaming reply paths ----
    # write_trace's tool_result + metadata are identical across the ok/error/stream
    # sites; centralize them so the three trace calls cannot drift apart.
    async def _emit_chat_trace(
        status_: str, duration_ms: int, error_message: Optional[str] = None
    ) -> None:
        await write_trace(
            session_id=session_uuid,
            user_id=current.id,
            trace_type="llm_chat",
            status=status_,
            selected_agent=selected_agent,
            user_message=request.message,
            memory_count=len(memories_for_prompt),
            memory_ids=memory_ids_in_prompt,
            model_name=llm.active_chat_model(),
            model_endpoint=llm.active_chat_endpoint(),
            duration_ms=duration_ms,
            error_message=error_message,
            workspace_id=workspace_uuid,
            tool_name="llm_chat",
            tool_result={
                "workspace_context_injected": workspace_context_text is not None,
                "workspace_id": str(workspace_uuid) if workspace_uuid else None,
                "workspace_name": (
                    workspace_context_meta.get("workspace_name")
                    if workspace_context_meta
                    else None
                ),
                "workspace_context_chars": prompt_stats.get(
                    "workspace_context_chars", 0
                ),
            },
            metadata=_workspace_trace_metadata(),
        )

    async def _finalize(
        assistant_response: str, llm_duration_ms: int
    ) -> tuple[str, datetime]:
        """Post-LLM work (draft/proposal suffix, delegation close, persist, trace)
        run once the full reply text is known. Returns the final response (with any
        appended suffix) and its completion timestamp."""
        # Chat-to-draft (v0.2): when a routed SIGNAL/CHRONOS turn explicitly asks to
        # SAVE a draft/proposal, persist it as an internal, review-only record via the
        # governed tool layer. Explicit intent only — plain "draft me an email" does
        # not create a record. Appends a confirmation (or denial/failure note) to the
        # answer; never sends email or writes a calendar.
        draft_suffix: Optional[str] = None
        is_admin = current.role == "admin"
        # External-action requests ("send an email", "create a calendar event") are
        # detected first: the external execution is governance-blocked (logged +
        # traced) and a safe internal artifact is created in its place. Checked
        # before the internal draft/propose gates because their verbs differ.
        if _match_external_email_send_intent(request.message):
            block_msg = await enforce_external_action_block(
                tool_name="send_email",
                agent_name=SIGNAL_NAME,
                session_id=session_uuid,
                user_id=current.id,
                scope_type=scope_type,
                workspace_id=workspace_uuid,
            )
            artifact_suffix = await maybe_create_signal_draft_from_chat(
                user_message=request.message,
                response=assistant_response,
                session_uuid=session_uuid,
                user_id=current.id,
                workspace_uuid=workspace_uuid,
                scope_type=scope_type,
                is_admin=is_admin,
            )
            draft_suffix = _compose_external_block_suffix(block_msg, artifact_suffix)
        elif _match_external_calendar_create_intent(request.message):
            block_msg = await enforce_external_action_block(
                tool_name="create_calendar_event",
                agent_name=CHRONOS_NAME,
                session_id=session_uuid,
                user_id=current.id,
                scope_type=scope_type,
                workspace_id=workspace_uuid,
            )
            artifact_suffix = await maybe_create_chronos_proposal_from_chat(
                user_message=request.message,
                response=assistant_response,
                session_uuid=session_uuid,
                user_id=current.id,
                workspace_uuid=workspace_uuid,
                scope_type=scope_type,
                is_admin=is_admin,
            )
            draft_suffix = _compose_external_block_suffix(block_msg, artifact_suffix)
        elif selected_agent == SIGNAL_NAME and (
            _match_signal_save_intent(request.message)
            or _match_signal_draft_intent(request.message)
        ):
            draft_suffix = await maybe_create_signal_draft_from_chat(
                user_message=request.message,
                response=assistant_response,
                session_uuid=session_uuid,
                user_id=current.id,
                workspace_uuid=workspace_uuid,
                scope_type=scope_type,
                is_admin=is_admin,
            )
        elif selected_agent == CHRONOS_NAME and (
            _match_chronos_save_intent(request.message)
            or _match_chronos_propose_intent(request.message)
        ):
            draft_suffix = await maybe_create_chronos_proposal_from_chat(
                user_message=request.message,
                response=assistant_response,
                session_uuid=session_uuid,
                user_id=current.id,
                workspace_uuid=workspace_uuid,
                scope_type=scope_type,
                is_admin=is_admin,
            )
        if draft_suffix:
            assistant_response = f"{assistant_response}{draft_suffix}"
        # Voice mode: strip any markdown the model still emitted so TTS doesn't read
        # it literally. Applies to the returned + persisted + streamed-done text.
        if request.speakable:
            assistant_response = to_speakable(assistant_response)

        completed_at = datetime.now(timezone.utc)
        if routing_delegation_id is not None:
            try:
                await complete_delegation(
                    routing_delegation_id,
                    output_payload={
                        "response_chars": len(assistant_response),
                        "duration_ms": llm_duration_ms,
                    },
                    user_id=current.id,
                )
            except Exception:
                logger.exception("routing delegation complete-update failed")

        try:
            await _persist_exchange(
                session_uuid=session_uuid,
                scope_type=scope_type,
                scope_id=scope_id,
                user_message=request.message,
                assistant_response=assistant_response,
                model_name=llm.active_chat_model(),
                placeholder=False,
                started_at=started_at,
                completed_at=completed_at,
                agent_name=selected_agent,
                workspace_id=workspace_uuid,
            )
        except Exception:
            logger.exception("Failed to persist chat exchange session=%s", session_id)

        await _emit_chat_trace("ok", llm_duration_ms)
        return assistant_response, completed_at

    # Streaming reply path (opt-in via request.stream): same prompt + finalization,
    # but the model's text is forwarded token-by-token as Server-Sent Events. The
    # `done` event carries the authoritative full response (including any appended
    # draft/proposal suffix, which is NOT part of the streamed deltas).
    if request.stream:

        async def _event_stream() -> AsyncIterator[bytes]:
            yield _sse_event(
                {
                    "type": "meta",
                    "session_id": session_id,
                    "selected_agent": selected_agent,
                }
            )
            stream_started = time.perf_counter()
            parts: list[str] = []
            try:
                async for delta in llm.stream_text(prompt, timeout=120.0):
                    parts.append(delta)
                    yield _sse_event({"type": "delta", "text": delta})
            except httpx.HTTPError as exc:
                duration_ms = int((time.perf_counter() - stream_started) * 1000)
                logger.exception(
                    "chat stream model request failed session=%s", session_id
                )
                if routing_delegation_id is not None:
                    try:
                        await fail_delegation(
                            routing_delegation_id,
                            error_message=str(exc),
                            user_id=current.id,
                        )
                    except Exception:
                        logger.exception("routing delegation fail-update failed")
                await _emit_chat_trace("error", duration_ms, str(exc))
                yield _sse_event(
                    {"type": "error", "detail": f"Chat model request failed: {exc}"}
                )
                return
            duration_ms = int((time.perf_counter() - stream_started) * 1000)
            final_response, completed_at = await _finalize(
                "".join(parts).strip(), duration_ms
            )
            yield _sse_event(
                {
                    "type": "done",
                    "session_id": session_id,
                    "agent": PERSONA_NAME,
                    "selected_agent": selected_agent,
                    "routing_matched_keywords": matched_keywords,
                    "model_endpoint": llm.active_chat_endpoint(),
                    "response": final_response,
                    "placeholder": False,
                    "created_at": completed_at.isoformat(),
                }
            )

        return StreamingResponse(
            _event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    llm_started = time.perf_counter()
    try:
        assistant_response = await llm.generate_text(prompt, timeout=120.0)
    except httpx.HTTPError as exc:
        llm_duration_ms = int((time.perf_counter() - llm_started) * 1000)
        logger.exception("chat model request failed session=%s", session_id)
        if routing_delegation_id is not None:
            try:
                await fail_delegation(
                    routing_delegation_id,
                    error_message=str(exc),
                    user_id=current.id,
                )
            except Exception:
                logger.exception("routing delegation fail-update failed")
        await _emit_chat_trace("error", llm_duration_ms, str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Chat model request failed: {exc}",
        ) from exc

    llm_duration_ms = int((time.perf_counter() - llm_started) * 1000)
    assistant_response, completed_at = await _finalize(
        assistant_response, llm_duration_ms
    )

    return ChatResponse(
        session_id=session_id,
        agent=PERSONA_NAME,
        selected_agent=selected_agent,
        routing_matched_keywords=matched_keywords,
        model_endpoint=llm.active_chat_endpoint(),
        response=assistant_response,
        placeholder=False,
        created_at=completed_at.isoformat(),
    )
