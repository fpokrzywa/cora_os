"""SCRIBE — Cora's memory manager subagent.

SCRIBE reads a conversation transcript and distils durable information
(decisions, preferences, architecture notes, next actions) into a memory
entry that other agents can read later. SCRIBE also serves memory back to
other agents on request (keyword search for v0.1; vector retrieval later).
SCRIBE operates internally; users do not see SCRIBE's voice directly.
"""

import json
import logging
import re
import uuid
from typing import Optional

import httpx

from app import llm
from app.clients import clients
from app.clock import current_datetime_preamble
from app.config import settings

logger = logging.getLogger(__name__)

NAME = "SCRIBE"
OLLAMA_TIMEOUT_SECONDS = 90.0

_KEYWORD_PATTERN = re.compile(r"[A-Za-z0-9_]+")
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "your", "you",
        "are", "was", "were", "but", "not", "have", "has", "had", "can",
        "did", "does", "what", "how", "when", "why", "where", "who", "into",
        "about", "their", "they", "them", "our", "out", "all", "any", "some",
        "more", "than", "such", "also", "would", "could", "should", "may",
        "might", "will", "just", "like", "want", "need", "please", "thanks",
    }
)

SCRIBE_SYSTEM_PROMPT = (
    "You are SCRIBE, the memory manager subagent inside Cora. You operate "
    "internally; the user never sees your output directly. Another agent will "
    "read what you write.\n\n"
    "Your job is to extract DURABLE information from a Cora conversation and "
    "write a concise, structured memory entry. Focus on:\n"
    "- Decisions made (and brief reasoning)\n"
    "- User preferences, constraints, and conventions\n"
    "- Architecture notes / technical facts about the user's systems\n"
    "- Action items or next steps that should outlive the session\n"
    "- Stable facts about the user, their team, or their projects\n\n"
    "Ignore: greetings, small talk, ephemeral debugging back-and-forth, and "
    "repeated questions that were resolved in the same session.\n\n"
    "Output a short structured note in markdown. Use brief section headers "
    "(e.g. 'Decisions', 'Preferences', 'Architecture', 'Next actions') with "
    "bullet points. Omit any section that has nothing worth keeping. Aim for "
    "clarity over completeness — the goal is a memory another agent can read "
    "in seconds."
)


async def load_session_messages(session_uuid: uuid.UUID) -> list[dict]:
    """Return every message for the session, oldest-first."""
    if clients.db_pool is None:
        logger.warning(
            "scribe load skipped session=%s: Postgres pool unavailable",
            session_uuid,
        )
        return []
    async with clients.db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT role, content, created_at
            FROM messages
            WHERE session_id = $1
            ORDER BY created_at ASC, id ASC
            """,
            session_uuid,
        )
    return [
        {"role": r["role"], "content": r["content"], "created_at": r["created_at"]}
        for r in rows
    ]


def _role_label(role: str) -> str:
    if role == "user":
        return "User"
    if role == "assistant":
        return "Cora"
    if role == "system":
        return "System"
    return role.capitalize()


def _format_transcript(messages: list[dict]) -> str:
    return "\n\n".join(
        f"{_role_label(m['role'])}: {m['content']}" for m in messages
    )


def build_scribe_prompt(messages: list[dict]) -> str:
    transcript = _format_transcript(messages)
    return (
        f"System: {current_datetime_preamble()}\n\n{SCRIBE_SYSTEM_PROMPT}\n\n"
        f"Conversation transcript:\n\n{transcript}\n\n"
        f"Write the memory entry now.\n\n"
        f"{NAME}:"
    )


async def summarize_messages(messages: list[dict]) -> str:
    """Summarize a conversation with the SCRIBE prompt via the active chat backend
    (app.llm — Ollama or vLLM, per DGX_CHAT_BACKEND).

    Raises:
        RuntimeError if the chat backend is not configured.
        httpx.HTTPError on transport / non-2xx model failure.
    """
    if not llm.is_chat_configured():
        raise RuntimeError("chat model backend is not configured")
    prompt = build_scribe_prompt(messages)
    logger.info(
        "scribe summarize: messages=%s prompt_chars=%s backend=%s",
        len(messages),
        len(prompt),
        llm.chat_backend(),
    )
    summary = await llm.generate_text(
        prompt, max_tokens=1024, timeout=OLLAMA_TIMEOUT_SECONDS
    )
    logger.info("scribe summarize complete: summary_chars=%s", len(summary))
    return summary


def extract_keywords(text: str, max_keywords: int = 20) -> list[str]:
    """Lowercase token extraction; drops stopwords and tokens < 3 chars."""
    seen: list[str] = []
    for token in _KEYWORD_PATTERN.findall(text.lower()):
        if len(token) < 3 or token in _STOPWORDS:
            continue
        if token not in seen:
            seen.append(token)
        if len(seen) >= max_keywords:
            break
    return seen


async def search_memory(
    query: str,
    limit: int = 10,
    user_id: Optional[uuid.UUID] = None,
    workspace_id: Optional[uuid.UUID] = None,
) -> list[dict]:
    """Keyword search across title, content, and tags, scoped to:
      - all global memories
      - the given user's user-scoped memories (if user_id is provided)
      - legacy user-scoped memories with scope_id IS NULL (pre-scoping data)

    Scoring (binary per field, summed):
      title match   = 3
      tag match     = 2
      content match = 1
    Ties broken by importance DESC, then created_at DESC.
    Returns [] when the pool is unavailable or the query yields no usable
    keywords (e.g. pure stopwords / punctuation).
    """
    if clients.db_pool is None:
        return []
    keywords = extract_keywords(query)
    if not keywords:
        return []

    patterns = [f"%{kw}%" for kw in keywords]

    workspace_filter = (
        "AND (workspace_id = $4 OR workspace_id IS NULL)" if workspace_id else ""
    )
    sql = f"""
        SELECT id, source_session_id, type, title, content, tags, importance,
               created_at, updated_at, scope_type, scope_id, workspace_id,
               (
                   (CASE WHEN title ILIKE ANY($1) THEN 3 ELSE 0 END) +
                   (CASE WHEN array_to_string(tags, ' ') ILIKE ANY($1)
                         THEN 2 ELSE 0 END) +
                   (CASE WHEN content ILIKE ANY($1) THEN 1 ELSE 0 END)
               ) AS score
        FROM memory_entries
        WHERE (
                  scope_type = 'global'
                  OR (
                      scope_type = 'user'
                      AND (scope_id = $3 OR scope_id IS NULL)
                  )
              )
          {workspace_filter}
          AND (
                  title ILIKE ANY($1)
                  OR content ILIKE ANY($1)
                  OR array_to_string(tags, ' ') ILIKE ANY($1)
              )
        ORDER BY score DESC, importance DESC, created_at DESC
        LIMIT $2
    """

    async with clients.db_pool.acquire() as conn:
        if workspace_id is not None:
            rows = await conn.fetch(sql, patterns, limit, user_id, workspace_id)
        else:
            rows = await conn.fetch(sql, patterns, limit, user_id)

    results = [dict(r) for r in rows]
    scoped = sum(1 for r in results if r["scope_type"] == "user" and r["scope_id"] is not None)
    legacy = sum(1 for r in results if r["scope_type"] == "user" and r["scope_id"] is None)
    global_count = sum(1 for r in results if r["scope_type"] == "global")
    logger.info(
        "memory search: user_id=%s keywords=%s returned=%s "
        "scoped_matches=%s legacy_null_matches=%s global_matches=%s",
        user_id,
        len(keywords),
        len(results),
        scoped,
        legacy,
        global_count,
    )
    return results


# =====================================================================
# Chat-driven memory mutation: deterministic intents handled by /chat
# =====================================================================

_INTENT_REMEMBER_GLOBAL = re.compile(
    r"^\s*remember\s+globally\s+that\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_INTENT_REMEMBER_SYSTEM = re.compile(
    r"^\s*remember\s+as\s+system\s+that\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_INTENT_REMEMBER_USER = re.compile(
    r"^\s*remember\s+(?:for\s+me\s+)?that\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_INTENT_SHOW_MEMORIES = re.compile(
    r"^\s*show\s+memor(?:y|ies)\s+about\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)
# Natural "save this to memory" / contextual "remember <this/these/all of it>"
# requests that point at the CONVERSATION rather than carrying an inline fact.
# These route to an LLM extraction over recent turns (kind 'remember_extract')
# instead of a literal text save — so "make sure all of these are stored in
# memory" and "can you remember that" actually persist what was just discussed.
_INTENT_SAVE_TO_MEMORY = re.compile(
    r"\b(?:sav|stor|keep|kept|put|add|commit|record)\w*\b[^.?!]{0,40}?\bmemor(?:y|ies)\b",
    re.IGNORECASE | re.DOTALL,
)
_INTENT_REMEMBER_CONTEXTUAL = re.compile(
    r"^\s*(?:can|could|would|will|please|pls)?\s*(?:you\s+)?(?:please\s+)?"
    r"remember\s+(?:this|that|these|those|it|all\b|everything|us\b|me\b|my\s|our\s)",
    re.IGNORECASE | re.DOTALL,
)
_INTENT_DELETE_MEMORY = re.compile(
    r"^\s*(confirm\s+)?(?:forget|delete)\s+memory\s+([0-9a-fA-F-]{8,36})\s*$",
    re.IGNORECASE,
)
_INTENT_UPDATE_MEMORY = re.compile(
    r"^\s*(confirm\s+)?update\s+memory\s+([0-9a-fA-F-]{8,36})\s+to\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)


def match_chat_memory_intent(message: str) -> Optional[dict]:
    """Detect deterministic memory-management commands in a chat message.

    Returns a dict like {kind: ..., ...} or None. Caller dispatches.
    Order matters: more specific patterns (global/system) come first.
    """
    m = _INTENT_REMEMBER_GLOBAL.match(message)
    if m:
        return {"kind": "remember_global", "text": m.group(1).strip()}
    m = _INTENT_REMEMBER_SYSTEM.match(message)
    if m:
        return {"kind": "remember_system", "text": m.group(1).strip()}
    m = _INTENT_REMEMBER_USER.match(message)
    if m:
        return {"kind": "remember_user", "text": m.group(1).strip()}
    # Broader save-to-memory / contextual-remember requests -> extract from the
    # conversation. Checked AFTER the strict "remember that <inline fact>" forms so
    # those still save their text directly. The save-verb pattern ignores questions
    # ("do you keep things in memory?") so meta-questions don't trigger a save.
    save_cmd = _INTENT_SAVE_TO_MEMORY.search(message) and not message.rstrip().endswith("?")
    if save_cmd or _INTENT_REMEMBER_CONTEXTUAL.match(message):
        return {"kind": "remember_extract"}
    m = _INTENT_SHOW_MEMORIES.match(message)
    if m:
        return {"kind": "show", "query": m.group(1).strip()}
    m = _INTENT_UPDATE_MEMORY.match(message)
    if m:
        return {
            "kind": "update",
            "memory_id": m.group(2).strip(),
            "text": m.group(3).strip(),
            "confirm": m.group(1) is not None,
        }
    m = _INTENT_DELETE_MEMORY.match(message)
    if m:
        return {
            "kind": "delete",
            "memory_id": m.group(2).strip(),
            "confirm": m.group(1) is not None,
        }
    return None


def title_from_text(text: str, max_words: int = 12) -> str:
    words = text.strip().split()
    if not words:
        return "Memory"
    title = " ".join(words[:max_words]).rstrip(".,;:!?")
    return title or "Memory"


async def create_chat_memory(
    *,
    text: str,
    scope_type: str,
    scope_id: Optional[uuid.UUID],
    workspace_id: Optional[uuid.UUID] = None,
) -> Optional[dict]:
    if clients.db_pool is None:
        return None
    title = title_from_text(text)
    async with clients.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO memory_entries (
                type, title, content, tags, importance,
                scope_type, scope_id, workspace_id
            )
            VALUES ('chat_memory', $1, $2, ARRAY['chat_created'], 3, $3, $4, $5)
            RETURNING id, scope_type, scope_id, title
            """,
            title,
            text,
            scope_type,
            scope_id,
            workspace_id,
        )
    logger.info(
        "memory create (chat): scope_type=%s scope_id=%s memory_id=%s title=%r",
        scope_type,
        scope_id,
        row["id"],
        row["title"],
    )
    return dict(row)


def _parse_memory_facts(text: str) -> list[dict]:
    """Robustly pull a list of {"text": ...} facts from the extractor model's reply
    (a JSON array, possibly wrapped in prose). Dedups within the batch (case-insensitive),
    caps the count, and returns [] on anything malformed — never raises."""
    if not text:
        return []
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        arr = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return []
    if not isinstance(arr, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in arr:
        if isinstance(item, dict):
            raw = item.get("text") or item.get("fact") or ""
        elif isinstance(item, str):
            raw = item
        else:
            raw = ""
        fact = " ".join(str(raw).split()).strip()
        key = fact.lower()
        if fact and key not in seen:
            seen.add(key)
            out.append({"text": fact})
        if len(out) >= 25:
            break
    return out


async def extract_memories_from_conversation(transcript: str) -> list[dict]:
    """LLM pass that pulls discrete, durable USER facts from a conversation transcript
    for permanent memory. Returns a list of {"text": ...} (one concrete fact each), or
    [] when nothing concrete is found or the model/endpoint is unavailable. Never raises.
    Conservative by design: a false-positive trigger just yields [] (nothing saved)."""
    if not llm.is_chat_configured() or not (transcript or "").strip():
        return []
    prompt = (
        "From the conversation below, extract the user's durable personal facts to "
        "store in permanent memory. Include ONLY concrete facts worth remembering "
        "long-term: names, relationships, nicknames, important dates, places, and "
        "stable preferences. Write each as one short self-contained sentence in the "
        "third person about the user, e.g. \"The user's wife is Dorothy, nicknamed "
        "'Meshu'.\" or \"The user's daughter Cailey (nickname 'Goose') was born "
        "2000-02-29.\" Do NOT include questions, smalltalk, transient state, or "
        "assistant messages. Output ONLY a JSON array like [{\"text\": \"...\"}] and "
        "nothing else; use [] if there is nothing concrete to save.\n\n"
        f"Conversation:\n{transcript}"
    )
    try:
        # Lower temperature + more room so the (bigger, on vLLM) model extracts
        # facts faithfully and completely.
        reply = await llm.generate_text(
            prompt, max_tokens=1024, temperature=0.3, timeout=60.0
        )
    except (httpx.HTTPError, ValueError):
        logger.warning("memory extraction model call failed")
        return []
    return _parse_memory_facts(reply)


async def fetch_memory_for_mutation(memory_id: uuid.UUID) -> Optional[dict]:
    if clients.db_pool is None:
        return None
    async with clients.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, scope_type, scope_id, title, content
            FROM memory_entries WHERE id = $1
            """,
            memory_id,
        )
    return dict(row) if row else None


async def resolve_memory_id_prefix(
    ref: str, *, user_id: uuid.UUID, is_admin: bool
) -> dict:
    """Resolve a short hex id prefix (the 8-char id `show memories` prints, or any
    leading slice of a memory UUID) to a single entry the requester may see.

    The prefix is matched only within the requester's VISIBLE set — global + their
    own user-scoped + legacy-NULL rows (admins see all) — so it can neither resolve
    to, nor be rendered ambiguous by, another user's private memory. Visibility here
    mirrors _memory_visible_to in routers.chat.

    Returns one of:
      {"status": "ok", "id": UUID}
      {"status": "not_found"}
      {"status": "ambiguous"}   # >1 visible match — caller asks for more characters
    """
    if clients.db_pool is None:
        return {"status": "not_found"}
    like = ref.lower() + "%"
    if is_admin:
        sql = "SELECT id FROM memory_entries WHERE id::text LIKE $1 ORDER BY id LIMIT 2"
        args = (like,)
    else:
        sql = (
            "SELECT id FROM memory_entries "
            "WHERE id::text LIKE $1 "
            "  AND ( scope_type = 'global' "
            "        OR (scope_type = 'user' AND (scope_id = $2 OR scope_id IS NULL)) ) "
            "ORDER BY id LIMIT 2"
        )
        args = (like, user_id)
    async with clients.db_pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    if not rows:
        return {"status": "not_found"}
    if len(rows) > 1:
        return {"status": "ambiguous"}
    return {"status": "ok", "id": rows[0]["id"]}


async def delete_memory_entry(memory_id: uuid.UUID) -> bool:
    if clients.db_pool is None:
        return False
    async with clients.db_pool.acquire() as conn:
        result = await conn.fetchval(
            "DELETE FROM memory_entries WHERE id = $1 RETURNING id",
            memory_id,
        )
    deleted = result is not None
    logger.info(
        "memory delete (chat): memory_id=%s deleted=%s",
        memory_id,
        deleted,
    )
    return deleted


async def update_memory_content(
    memory_id: uuid.UUID, content: str
) -> Optional[dict]:
    if clients.db_pool is None:
        return None
    title = title_from_text(content)
    async with clients.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE memory_entries
            SET content = $2, title = $3, updated_at = NOW()
            WHERE id = $1
            RETURNING id, scope_type, scope_id, title
            """,
            memory_id,
            content,
            title,
        )
    if row is not None:
        logger.info(
            "memory update (chat): memory_id=%s scope_type=%s title=%r",
            row["id"],
            row["scope_type"],
            row["title"],
        )
    return dict(row) if row else None
