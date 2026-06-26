"""Multi-provider command helpers (both mail + calendar now connect two providers).

Two concerns:
  1. PROVIDER-WORD TOLERANCE — a provider word used as an adjective before a domain
     noun ("what's on my **outlook** calendar", "show my **google** inbox") splits the
     "my calendar" / "my inbox" detection phrases. `strip_provider_adjectives` removes
     just that adjective so the underlying phrase matches; a provider word NOT before a
     domain noun ("emails from outlook") is preserved (it's a search term, not a
     selector). Provider RESOLUTION still reads the original message.
  2. PER-USER DEFAULT PROVIDER — when a request names no provider, prefer the user's
     explicit default (settable via chat: "make outlook my default calendar") over the
     old "most-recently-connected wins".
"""

import re
import uuid
from typing import Optional

from app.clients import clients
from app.runtime_traces import write_trace

PROVIDER_WORDS = ("outlook", "microsoft", "google", "gmail")

_DOMAIN_NOUNS = ("calendars", "calendar", "schedule", "inbox", "mailbox", "mail",
                 "emails", "email", "events", "event", "meetings", "meeting",
                 "agenda", "appointments", "appointment")
# A provider word immediately before a domain noun → strip just the provider word.
_PROVIDER_ADJ_RE = re.compile(
    r"\b(?:%s)\s+(?=(?:%s)\b)" % ("|".join(PROVIDER_WORDS), "|".join(_DOMAIN_NOUNS)),
    re.I)


def strip_provider_adjectives(m: str) -> str:
    """'whats on my outlook calendar' -> 'whats on my calendar'. Whitespace collapsed."""
    return re.sub(r"\s{2,}", " ", _PROVIDER_ADJ_RE.sub("", m or "")).strip()


def strip_provider_words(text: str) -> str:
    """Drop standalone provider words from a fragment, so a calendar-name hint like
    'outlook' or 'outlook Work' doesn't read as a calendar literally named that."""
    toks = [t for t in re.split(r"\s+", (text or "").strip())
            if t and t.lower() not in PROVIDER_WORDS]
    return " ".join(toks)


# --------------------------------------------------------------------------- #
# Per-user default provider
# --------------------------------------------------------------------------- #

def _canonical(word: str, provider_type: str) -> Optional[str]:
    w = (word or "").lower()
    if provider_type == "calendar":
        if w in ("outlook", "microsoft"):
            return "outlook_calendar"
        if w in ("google", "gmail"):
            return "google_calendar"
    elif provider_type == "email":
        if w in ("outlook", "microsoft"):
            return "outlook_mail"
        if w in ("google", "gmail"):
            return "gmail"
    return None


async def get_default(user_id: uuid.UUID, provider_type: str) -> Optional[str]:
    pool = clients.db_pool
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT provider_name FROM user_provider_defaults "
            "WHERE user_id=$1 AND provider_type=$2", user_id, provider_type)


async def set_default(user_id: uuid.UUID, provider_type: str, provider_name: str) -> None:
    pool = clients.db_pool
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_provider_defaults (user_id, provider_type, provider_name, updated_at) "
            "VALUES ($1,$2,$3,NOW()) ON CONFLICT (user_id, provider_type) DO UPDATE "
            "SET provider_name=$3, updated_at=NOW()", user_id, provider_type, provider_name)


def _phrase_type(m: str) -> Optional[str]:
    if any(w in m for w in ("calendar", "schedule", "meeting", "event", "agenda",
                            "appointment")):
        return "calendar"
    if any(w in m for w in ("inbox", "mailbox", "mail", "email")):
        return "email"
    return None


# "default" must read as a SETTING (next to a type noun / "my"/"as the default"), so a
# stray "default" in another sense ("the default team") doesn't trigger a preference set.
_DEFAULT_POS_RE = re.compile(
    r"\b(my default|as (?:the )?default|default (?:for|to)\b|"
    r"default (?:calendar|schedule|inbox|mailbox|mail|email))\b", re.I)


def detect_default_command(message: str) -> Optional[tuple[str, str]]:
    """Detect 'set/use/make/prefer <provider> (as) (my) default <calendar|inbox>'.
    Conservative: requires a set verb + 'default' in a settings position + a provider
    word + a domain noun. Returns (provider_type, canonical_provider_name) or None."""
    m = (message or "").lower().strip()
    if not re.search(r"\b(set|use|make|prefer)\b", m) or not _DEFAULT_POS_RE.search(m):
        return None
    ptype = _phrase_type(m)
    if not ptype:
        return None
    for w in PROVIDER_WORDS:
        if re.search(rf"\b{w}\b", m):
            name = _canonical(w, ptype)
            if name:
                return (ptype, name)
    return None


async def handle_default_command(
    cmd: tuple[str, str], *, user_id: uuid.UUID, session_uuid: uuid.UUID,
    workspace_uuid: Optional[uuid.UUID],
) -> tuple[bool, str]:
    ptype, name = cmd
    await set_default(user_id, ptype, name)
    await write_trace(
        session_id=session_uuid, user_id=user_id, trace_type="provider_default_set",
        status="ok", selected_agent=None, tool_name="provider_defaults",
        tool_result={"provider_type": ptype, "provider_name": name},
        workspace_id=workspace_uuid)
    label = "calendar" if ptype == "calendar" else "inbox/email"
    return True, (f"✓ Set **{name}** as your default {label} provider. I'll use it when you "
                  "don't name a provider — say e.g. 'google' or 'outlook' in a request to "
                  "override it for that one.")


async def resolve(message: str, user_id: uuid.UUID, provider_type: str,
                  fallback: str) -> str:
    """Resolve which provider a provider-less request targets. Order: explicit
    provider word in the message → the user's connected default → most-recently
    connected → the hardcoded fallback. Keyword resolution is handled by the caller
    (it knows the canonical names); this covers the no-keyword case."""
    pool = clients.db_pool
    async with pool.acquire() as conn:
        connected = [r["provider_name"] for r in await conn.fetch(
            "SELECT provider_name FROM provider_oauth_connectors "
            "WHERE user_id=$1 AND provider_type=$2 AND status='connected' "
            "ORDER BY created_at DESC", user_id, provider_type)]
    default = await get_default(user_id, provider_type)
    if default and default in connected:
        return default
    return connected[0] if connected else fallback
