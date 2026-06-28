"""Chat-Native Calendar Assistant (CHRONOS Calendar CRUD v1.0 + confirm-before-write).

Answer + ACT on calendar requests from chat — "What's on my calendar today?",
"Schedule a meeting with Sam tomorrow at 3pm", "Reschedule my … meeting", "Cancel
my … meeting". The calendar analog of the v2.7 inbox assistant, with full CRUD,
so it carries TWO fail-closed gates:

  * READ  (`_read_gate`):  provider connected + valid/refreshable token + calendar
    scope present + provider supports_calendar_read + `calendar_read` flag enabled.
  * WRITE (`_write_gate`): the read gate's connection checks + write scope + provider
    supports_calendar_{create,update,delete} + the `calendar_write` feature flag +
    the dedicated `CALENDAR_EXECUTION_ENABLED` switch. That switch (NOT the global
    external_execution kill switch, which the email/integration governance requires
    to stay false) is the master gate — no real write fires unless an operator
    deliberately opens it.

Writes are **confirm-before-write**: a natural-language request is parsed into
structured event fields by the in-process DGX model (`extract_event_fields`, with a
regex fallback), then Cora shows a confirmation card and STAGES the action in
`calendar_pending_actions` — nothing touches the provider until the user replies
"confirm" (then the gated write fires) or "cancel". With a write gate closed (the
production default), the op is denied without any provider call, and a denied CREATE
falls back to a review-only internal CHRONOS proposal. The token broker
(`_get_access_token`) is only used after a gate passes; tokens are never logged,
traced, or returned. All access is audited (`calendar_access_events`) + traced.
"""

import json
import logging
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx

from app.clients import clients
from app import llm
from app import calendar_adapters
from app import chronos_tools
from app import clock
from app import feature_flags as ff
from app import oauth_flow
from app import provider_defaults
from app import runtime_switches
from app.config import settings
from app.crypto import decrypt_secret
from app.oauth_readiness import _scope_tail
from app.runtime_traces import write_trace

logger = logging.getLogger(__name__)

CHRONOS = "CHRONOS"
TRACE_LIST = "chat_calendar_events_listed"
TRACE_CREATE = "chat_calendar_event_created"
TRACE_UPDATE = "chat_calendar_event_updated"
TRACE_DELETE = "chat_calendar_event_deleted"
TRACE_REQUESTED = "chat_calendar_request"
TRACE_CONFIRM_PENDING = "chat_calendar_confirm_pending"
TRACE_CONFIRMED = "chat_calendar_confirmed"
TRACE_CANCELLED = "chat_calendar_cancelled"
TRACE_CAPABILITY_DENIED = "chat_calendar_capability_denied"
TRACE_WRITE_DENIED = "chat_calendar_write_denied"
TRACE_PROVIDER_FAILED = "chat_calendar_provider_failed"
TRACE_PROPOSAL_FALLBACK = "chat_calendar_proposal_fallback"

TOKEN_REFRESH_MARGIN_SECONDS = 120
LLM_TIMEOUT_SECONDS = 30.0

# Registry / feature-flag canonical name for the connector vault's outlook_calendar.
_REGISTRY_ALIAS = {"outlook_calendar": "microsoft_calendar"}
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?")
# A named target calendar for CREATE: "on/in/to my|the <name> calendar" (the
# non-greedy capture stops at the first " calendar", so a bare "to my calendar"
# matches nothing → primary), or "calendar called|named <name>".
_CAL_HINT_RE = re.compile(r"\b(?:on|in|to|onto|into)\s+(?:my|the)\s+(.+?)\s+calendar\b", re.I)
_CAL_NAMED_RE = re.compile(r"\bcalendar\s+(?:called|named|titled)\s+[\"']?([\w '&./-]+?)[\"']?(?:[.,!?]|$)", re.I)


def _trunc(v, n=80):
    s = "" if v is None else str(v)
    return s if len(s) <= n else s[:n] + "…"


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

_CREATE_VERBS = ("schedule", "book", "set up", "set-up", "add", "create", "put", "make")
_CONFIRM_WORDS = ("confirm", "yes", "yep", "yeah", "go ahead", "do it", "create it",
                  "schedule it", "book it", "send it", "send the invite", "looks good",
                  "confirmed", "proceed", "make it", "sounds good", "correct", "ok", "okay")
_CANCEL_WORDS = ("cancel", "no", "don't", "do not", "nevermind", "never mind", "stop",
                 "scratch that", "discard", "forget it", "nope")


def _starts(m: str, w: str) -> bool:
    return m == w or m.startswith(w + " ") or m.startswith(w + ",")


def detect_confirmation(message: str) -> Optional[str]:
    """Detect a bare confirm/cancel reply. Only meaningful when a pending calendar
    action exists (the caller gates on that), so it stays conservative — word-boundary
    matched so 'now show me…' is NOT read as 'no'."""
    m = (message or "").strip().lower().rstrip("!.? ")
    if not m:
        return None
    if any(_starts(m, w) for w in _CANCEL_WORDS):
        return "cancel"
    if any(_starts(m, w) for w in _CONFIRM_WORDS):
        return "confirm"
    return None


_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
             "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}


def detect_selection(message: str) -> Optional[int]:
    """Detect a 1-based pick from a numbered candidate list ("2", "cancel 2",
    "number 2", "the 2nd one", "second"). Only meaningful when a candidate selection
    is pending (the caller gates on that). Anchored to the whole message so "2 pm
    tomorrow" is NOT read as a pick."""
    m = (message or "").strip().lower().rstrip("!.? ")
    if not m:
        return None
    mt = re.match(r"^(?:option|number|item|no\.?|#|pick|choose|select|cancel|delete|"
                  r"remove|do|the)?\s*#?\s*(\d{1,2})(?:\s+(?:one|please|that one|thanks))?$", m)
    if mt:
        return int(mt.group(1))
    mt2 = re.match(r"^(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)(?:\s+one)?$", m)
    if mt2:
        return int(mt2.group(1))
    for w, n in _ORDINALS.items():
        if re.match(rf"^(?:the\s+)?{w}(?:\s+one)?$", m):
            return n
    return None


_LIST_ACTION_RE = re.compile(
    r"^(?:can you|could you|would you|please|pls|hey|ok|okay)?\s*,?\s*"
    r"(cancel|delete|remove|reschedule|move|change|update|edit)\s+"
    r"(?:the\s+)?(?:event\s+|meeting\s+|number\s+|item\s+|#\s*)?(\d{1,2})(?:st|nd|rd|th)?\b")
_LIST_ACTION_VERB = {"cancel": "delete", "delete": "delete", "remove": "delete",
                     "reschedule": "update", "move": "update", "change": "update",
                     "update": "update", "edit": "update"}


def detect_list_action(message: str) -> Optional[tuple[str, int]]:
    """Detect "cancel 4" / "reschedule 2 to 3pm" / "delete event 3" — a verb + a
    1-based index into the last numbered list Cora showed. Returns (action, index)
    with action in delete|update. Only meaningful when a numbered list is pending
    (the caller gates on that)."""
    mt = _LIST_ACTION_RE.match((message or "").strip().lower())
    if not mt:
        return None
    return (_LIST_ACTION_VERB[mt.group(1)], int(mt.group(2)))


def _extract_event_query(m: str) -> Optional[str]:
    for kw in (" my ", " the ", " that ", " this ", " about ", " for ", " titled ", " called "):
        if kw in m:
            tail = m.split(kw, 1)[1].strip().rstrip("?.! ")
            tail = re.split(r"\b(to|on|at|from|tomorrow|today|next)\b", tail, 1)[0].strip()
            return tail[:120] or None
    return None


def _detect_window(m: str) -> Optional[str]:
    for w in ("today", "tomorrow", "this week", "this morning", "this afternoon",
              "next week", "tonight"):
        if w in m:
            return w
    return None


_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}


def _has_day_ref(m: str) -> bool:
    """True if the message names a specific day/window (used to recognize a day-scoped
    schedule question that omits an explicit calendar anchor)."""
    if any(d in m for d in ("today", "tomorrow", "yesterday", "tonight", "this morning",
                            "this afternoon", "this evening", "this week", "last week",
                            "next week", "the week")):
        return True
    if any(wd in m for wd in _WEEKDAYS):
        return True
    return bool(re.search(r"\d{4}-\d{2}-\d{2}", m))


def resolve_read_window(message: str) -> tuple[str, str, str]:
    """Resolve a read query into a concrete [time_min, time_max] (RFC3339 UTC) +
    a human label. Day/week references resolve against the current date IN THE
    CORA TIMEZONE and CAN BE IN THE PAST ("yesterday", a named weekday, "last
    week") — the old code pinned time_min=now and silently hid past events. The
    default (no day reference) is start-of-today through the next 14 days, so
    earlier-today meetings still show while the view stays near-term."""
    m = (message or "").lower()
    tz = clock.current_tz()
    now = datetime.now(tz)
    today = now.date()

    def day_bounds(d: date) -> tuple[datetime, datetime]:
        start = datetime(d.year, d.month, d.day, tzinfo=tz)
        return start, start + timedelta(days=1)

    def span(a: datetime, b: datetime) -> tuple[str, str]:
        return (a.astimezone(timezone.utc).isoformat(), b.astimezone(timezone.utc).isoformat())

    def week_of(anchor: date) -> tuple[datetime, datetime]:
        monday = anchor - timedelta(days=anchor.weekday())
        return day_bounds(monday)[0], day_bounds(monday + timedelta(days=6))[1]

    if "yesterday" in m:
        s, e = day_bounds(today - timedelta(days=1)); return (*span(s, e), "yesterday")
    if "tomorrow" in m:
        s, e = day_bounds(today + timedelta(days=1)); return (*span(s, e), "tomorrow")
    if any(w in m for w in ("today", "tonight", "this morning", "this afternoon", "this evening")):
        s, e = day_bounds(today); return (*span(s, e), "today")
    if "last week" in m:
        s, e = week_of(today - timedelta(days=7)); return (*span(s, e), "last week")
    if "next week" in m:
        s, e = week_of(today + timedelta(days=7)); return (*span(s, e), "next week")
    if "this week" in m or "the week" in m:
        s, e = week_of(today); return (*span(s, e), "this week")
    for name, idx in _WEEKDAYS.items():
        if name in m:
            if "last" in m or "past" in m:
                # most recent occurrence strictly before today ("this past tuesday")
                target = today - timedelta(days=((today.weekday() - idx) % 7) or 7)
                s, e = day_bounds(target)
                return (*span(s, e), name.capitalize())
            if "next" in m:
                target = today + timedelta(days=((idx - today.weekday()) % 7) or 7)
                s, e = day_bounds(target)
                return (*span(s, e), name.capitalize())
            monday = today - timedelta(days=today.weekday())
            s, e = day_bounds(monday + timedelta(days=idx))
            return (*span(s, e), name.capitalize())
    iso = re.search(r"\d{4}-\d{2}-\d{2}", m)
    if iso:
        try:
            s, e = day_bounds(date.fromisoformat(iso.group(0)))
            return (*span(s, e), iso.group(0))
        except ValueError:
            pass
    # Default: start of today through +14 days (near-term, includes earlier today).
    start_today = day_bounds(today)[0]
    return (*span(start_today, now + timedelta(days=14)), "the next 2 weeks")


def _dedupe_series(events: list) -> list:
    """Collapse repeated occurrences of a recurring series to its first instance
    (a yearly birthday or a daily standup shouldn't fill the whole list)."""
    seen, out = set(), []
    for e in events:
        sid = e.get("series_id")
        if sid and sid in seen:
            continue
        if sid:
            seen.add(sid)
        out.append(e)
    return out


def detect_calendar_command(message: str) -> Optional[tuple[str, dict]]:
    """Conservative, explicit-only detection. Requires a calendar/meeting/event
    anchor so generic CHRONOS planning chatter ('plan my week') does NOT route
    here. Returns (kind, payload) with kind in list/create/update/delete."""
    # Strip a provider word used as an adjective ("my outlook calendar" → "my calendar")
    # so a named provider doesn't break phrase matching; provider RESOLUTION still reads
    # the original message in _resolve_provider.
    m = provider_defaults.strip_provider_adjectives((message or "").lower().strip())
    if not m:
        return None
    has_anchor = any(n in m for n in ("calendar", "meeting", "event", "appointment", "agenda"))

    if any(p in m for p in ("cancel my", "cancel the", "cancel that", "cancel this",
                            "cancel our", "delete the event", "delete my event", "delete that",
                            "delete this", "delete the meeting", "delete my meeting",
                            "remove the event", "remove the meeting", "remove my meeting",
                            "remove that", "remove this", "drop the meeting", "drop that meeting",
                            "call off", "get rid of", "clear my calendar")) and has_anchor:
        return ("delete", {"query": _extract_event_query(m)})

    if ("reschedule" in m or "move my meeting" in m or "move the meeting" in m
            or "move my appointment" in m or "change my meeting" in m
            or "change the meeting" in m or "push my meeting" in m):
        return ("update", {"query": _extract_event_query(m)})

    explicit_create = any(p in m for p in (
        "schedule a meeting", "schedule a call", "book a meeting", "set up a meeting",
        "set up a call", "add a meeting", "create a meeting", "create an event",
        "create a calendar event", "add an event", "add an appointment",
        "put a meeting on", "add it to my calendar", "add to my calendar",
        "on my calendar", "to my calendar"))
    if explicit_create or ("calendar" in m and any(v in m for v in _CREATE_VERBS)):
        if any(v in m for v in _CREATE_VERBS) or "schedule a" in m or "book a" in m:
            return ("create", {})

    list_phrases = (
        "what's on my calendar", "whats on my calendar", "what is on my calendar",
        "what's on my schedule", "whats on my schedule", "on my calendar",
        "my calendar today", "my calendar tomorrow", "my calendar this week",
        "my schedule today", "my schedule tomorrow", "my schedule this week",
        "my agenda", "agenda for today", "agenda for the day",
        "do i have any meetings", "do i have meetings", "do i have any events",
        "any meetings today", "what meetings", "my next meeting", "next meeting",
        "upcoming meetings", "upcoming events", "upcoming appointments",
        "list my events", "list my meetings", "show my calendar", "show my meetings",
        "show my schedule", "what's my schedule", "whats my schedule")
    if any(p in m for p in list_phrases):
        return ("list", {"window": _detect_window(m)})

    # Day-scoped schedule questions that omit an explicit calendar anchor, e.g.
    # "what was on this past tuesday", "anything on monday?", "what did I have on the
    # 14th". Requires a clear schedule-query phrase AND a concrete day reference so
    # generic chatter doesn't route here.
    sched_q = ("what was on", "what is on", "what's on", "whats on", "what was scheduled",
               "what's scheduled", "whats scheduled", "what was happening",
               "what did i have", "what do i have", "anything on", "anything scheduled",
               "anything happening", "what was on it", "what was on that")
    if any(q in m for q in sched_q) and _has_day_ref(m):
        return ("list", {"window": _detect_window(m)})
    return None


# --------------------------------------------------------------------------- #
# Field extraction (LLM-backed, regex fallback)
# --------------------------------------------------------------------------- #

def _extract_create_fields(message: str) -> dict:
    """Regex baseline — explicit ISO times + emails only. The LLM extractor builds
    on this and wins when it returns better values."""
    times = _ISO_RE.findall(message or "")
    title = (message or "").strip()
    for lead in ("schedule a ", "schedule ", "book a ", "book ", "set up a ", "set up ",
                 "add a ", "add an ", "add ", "create a ", "create an ", "create ",
                 "put a ", "make a "):
        if title.lower().startswith(lead):
            title = title[len(lead):].strip()
            break
    title = re.split(r"\b(on|at|for|tomorrow|today|next week|this week)\b", title, 1)[0].strip()
    return {
        "title": (title or "New event")[:200],
        "description": (message or "").strip()[:1000],
        "start_time": times[0].replace(" ", "T") if len(times) >= 1 else None,
        "end_time": times[1].replace(" ", "T") if len(times) >= 2 else None,
        "timezone": None,
        "attendees": _EMAIL_RE.findall(message or "") or None,
        "location": None,
    }


def _parse_json_object(text: str) -> Optional[dict]:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


async def extract_event_fields(message: str, kind: str) -> dict:
    """Parse a natural-language calendar request into structured event fields via
    the in-process DGX model, resolving relative dates against the current date.
    Falls back to the regex baseline when the endpoint is unset or the call/parse
    fails — the module still works, just less smart. Never raises."""
    base = _extract_create_fields(message)
    if not llm.is_chat_configured():
        return base
    tz_name = settings.cora_timezone or "UTC"
    prompt = (
        f"{clock.current_datetime_preamble()}\n\n"
        "Extract calendar event details from the user's request into a SINGLE JSON "
        "object and output ONLY that JSON (no prose, no code fence). Keys: "
        "title (string), start_time (ISO 8601 local time like 2026-06-30T15:00:00 or null), "
        "end_time (ISO 8601 or null), attendees (array of email strings, [] if none), "
        "location (string or null), description (string or null). Resolve relative dates "
        "(\"today\", \"tomorrow\", \"next tuesday\") against the current date above. If a "
        "start time is given with no end, assume a 60-minute event. "
        f"Times are in the {tz_name} timezone.\n\nUser request: {message!r}"
    )
    try:
        text = await llm.generate_text(
            prompt, max_tokens=512, temperature=0.2, timeout=LLM_TIMEOUT_SECONDS)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("calendar field extraction failed; using regex fallback: %s", exc)
        return base
    parsed = _parse_json_object(text)
    if not parsed:
        return base
    out = dict(base)
    for k in ("title", "start_time", "end_time", "location", "description"):
        v = parsed.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    if isinstance(parsed.get("attendees"), list):
        emails = [e for e in parsed["attendees"] if isinstance(e, str) and "@" in e]
        if emails:
            out["attendees"] = emails
    if out.get("start_time"):
        out["timezone"] = tz_name
    return out


# --------------------------------------------------------------------------- #
# Provider resolution + governance gates (fail-closed)
# --------------------------------------------------------------------------- #

def _named_provider(message: str) -> Optional[str]:
    """The calendar provider EXPLICITLY named in the message, else None (no silent
    default). Used both to resolve writes and to honor a redirect at confirm time."""
    m = (message or "").lower()
    if "outlook" in m or "microsoft" in m:
        return "outlook_calendar"
    if "google" in m or "gmail" in m:
        return "google_calendar"
    return None


async def _resolve_provider(message: str, user_id: uuid.UUID) -> str:
    named = _named_provider(message)
    if named:
        return named
    # No provider named → the user's connected default, else most-recently connected.
    return await provider_defaults.resolve(message, user_id, "calendar", "google_calendar")


async def _resolve_read_providers(message: str, user_id: uuid.UUID) -> list[str]:
    """Which calendars a READ targets. A named provider → just that one. Otherwise ALL
    connected calendars (so a provider-less 'what's on my calendar' aggregates Google +
    Outlook). Falls back to the default when none are connected (→ a clean gated reply)."""
    m = (message or "").lower()
    if "outlook" in m or "microsoft" in m:
        return ["outlook_calendar"]
    if "google" in m or "gmail" in m:
        return ["google_calendar"]
    pool = clients.db_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT provider_name FROM provider_oauth_connectors "
            "WHERE user_id=$1 AND provider_type='calendar' AND status='connected' "
            "ORDER BY created_at", user_id)
    return [r["provider_name"] for r in rows] or ["google_calendar"]


async def _connection_state(provider: str, user_id: uuid.UUID) -> dict:
    """Read connection status / scopes / token presence + the capability registry
    row for `provider`. Reads NO token columns' values (only their presence)."""
    pool = clients.db_pool
    registry = _REGISTRY_ALIAS.get(provider, provider)
    async with pool.acquire() as conn:
        c = await conn.fetchrow(
            "SELECT status, scopes, (access_token_encrypted IS NOT NULL) AS has_access, "
            "(refresh_token_encrypted IS NOT NULL) AS has_refresh, token_expires_at "
            "FROM provider_oauth_connectors WHERE user_id=$1 AND provider_name=$2 "
            "AND status<>'disconnected' ORDER BY (status='connected') DESC, created_at DESC LIMIT 1",
            user_id, provider)
        cap = await conn.fetchrow(
            "SELECT supports_calendar_read, supports_calendar_create, "
            "supports_calendar_update, supports_calendar_delete "
            "FROM external_provider_connectors WHERE provider_name=$1", registry)
    connected = bool(c and c["status"] == "connected")
    exp = c["token_expires_at"] if c else None
    token_ok = bool(c and (c["has_access"] and not (exp and exp <= datetime.now(timezone.utc))
                           or c["has_refresh"]))
    scope = calendar_adapters.READ_SCOPES.get(provider, "")
    granted = {_scope_tail(s) for s in (c["scopes"] if c else [])}
    scope_ok = _scope_tail(scope) in granted
    return {"connected": connected, "token_ok": token_ok, "scope_ok": scope_ok,
            "cap": dict(cap) if cap else {}}


async def _read_gate(provider: str, user_id: uuid.UUID) -> dict:
    """Fail-closed calendar-read decision: connected + token + scope + provider
    supports_calendar_read + `calendar_read` flag enabled."""
    s = await _connection_state(provider, user_id)
    supports_read = bool(s["cap"].get("supports_calendar_read"))
    flag = await ff.get_flag(provider, "calendar_read")
    flag_ok = bool(flag and flag["enabled"])
    reasons = []
    if not s["connected"]: reasons.append("provider not connected")
    if not s["token_ok"]: reasons.append("no valid/refreshable token")
    if not s["scope_ok"]: reasons.append(f"calendar scope missing ({_scope_tail(calendar_adapters.READ_SCOPES.get(provider, ''))})")
    if not supports_read: reasons.append("provider supports_calendar_read=false (capability mismatch)")
    if not flag_ok: reasons.append("calendar_read feature flag disabled (fail-closed)")
    allowed = s["connected"] and s["token_ok"] and s["scope_ok"] and supports_read and flag_ok
    return {"allowed": allowed, "supports_read": supports_read,
            "capability_mismatch": not supports_read, "flag_ok": flag_ok,
            "reason": "; ".join(reasons) or "all checks pass"}


_WRITE_CAP = {"create": "supports_calendar_create", "update": "supports_calendar_update",
              "delete": "supports_calendar_delete"}


async def _write_gate(provider: str, user_id: uuid.UUID, action: str) -> dict:
    """Fail-closed calendar-WRITE decision. Everything the read gate's connection
    checks need, plus write scope + provider supports_calendar_{action} + the
    `calendar_write` flag + the DEDICATED calendar execution switch
    (CALENDAR_EXECUTION_ENABLED). That switch — not the global external_execution
    kill switch — is the master gate for calendar writes: the global flag is owned
    by the email/integration governance (which requires it false), so calendar uses
    its own. No real write can fire while the calendar switch is off (the default)."""
    s = await _connection_state(provider, user_id)
    supports = bool(s["cap"].get(_WRITE_CAP[action]))
    flag = await ff.get_flag(provider, "calendar_write")
    flag_ok = bool(flag and flag["enabled"] and not flag["dry_run_only"])
    # Master gate: the DB override (admin-toggleable from the app) if set, else the
    # CALENDAR_EXECUTION_ENABLED env default. NOT the global external_execution switch.
    kill_switch_clear = await runtime_switches.effective("calendar_execution_enabled")
    reasons = []
    if not s["connected"]: reasons.append("provider not connected")
    if not s["token_ok"]: reasons.append("no valid/refreshable token")
    if not s["scope_ok"]: reasons.append(f"calendar write scope missing ({_scope_tail(calendar_adapters.WRITE_SCOPES.get(provider, ''))})")
    if not supports: reasons.append(f"provider {_WRITE_CAP[action]}=false (capability mismatch)")
    if not flag_ok: reasons.append("calendar_write feature flag disabled (fail-closed)")
    if not kill_switch_clear: reasons.append("CALENDAR_EXECUTION_ENABLED is OFF (master gate)")
    allowed = (s["connected"] and s["token_ok"] and s["scope_ok"] and supports
               and flag_ok and kill_switch_clear)
    return {"allowed": allowed, "supports": supports, "capability_mismatch": not supports,
            "flag_ok": flag_ok, "kill_switch_clear": kill_switch_clear,
            "reason": "; ".join(reasons) or "all checks pass"}


async def _get_access_token(provider: str, user_id: uuid.UUID) -> Optional[str]:
    """Token broker — only called AFTER a gate passes. Decrypts the caller's own
    connected access token, refreshing via oauth_flow when expiring. Returns None
    on any failure. The plaintext token goes straight to the adapter — never
    logged, traced, stored, or rendered."""
    pool = clients.db_pool

    async def _fetch():
        async with pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT access_token_encrypted, token_expires_at, "
                "(refresh_token_encrypted IS NOT NULL) AS has_refresh "
                "FROM provider_oauth_connectors WHERE user_id=$1 AND provider_name=$2 "
                "AND status='connected' ORDER BY created_at DESC LIMIT 1",
                user_id, provider)

    row = await _fetch()
    if row is None:
        return None
    exp = row["token_expires_at"]
    expiring = bool(exp and exp <= datetime.now(timezone.utc)
                    + timedelta(seconds=TOKEN_REFRESH_MARGIN_SECONDS))
    if expiring and row["has_refresh"]:
        try:
            await oauth_flow.refresh_connection(provider, user_id=user_id, is_admin=False)
            row = await _fetch()
        except oauth_flow.OAuthError as exc:
            logger.warning("calendar token refresh failed: provider=%s err=%s", provider, exc)
            return None
        if row is None:
            return None
    try:
        return decrypt_secret(row["access_token_encrypted"])
    except Exception:
        logger.warning("calendar token decrypt failed: provider=%s", provider)
        return None


# --------------------------------------------------------------------------- #
# Pending action store (confirm-before-write)
# --------------------------------------------------------------------------- #

async def _set_pending(session, *, user_id, workspace_id, kind, provider, fields,
                       target_event_id=None, target_calendar_id=None, target_summary=None) -> None:
    pool = clients.db_pool
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO calendar_pending_actions (session_id, user_id, workspace_id, kind, "
            "provider, fields, target_event_id, target_calendar_id, target_summary) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) "
            "ON CONFLICT (session_id) DO UPDATE SET user_id=$2, workspace_id=$3, kind=$4, "
            "provider=$5, fields=$6, target_event_id=$7, target_calendar_id=$8, "
            "target_summary=$9, created_at=NOW()",
            session, user_id, workspace_id, kind, provider, fields, target_event_id,
            target_calendar_id, target_summary)


async def _get_pending(session) -> Optional[dict]:
    pool = clients.db_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT session_id, user_id, workspace_id, kind, provider, fields, target_event_id, "
            "target_calendar_id, target_summary FROM calendar_pending_actions WHERE session_id=$1",
            session)
    if not row:
        return None
    d = dict(row)
    if isinstance(d.get("fields"), str):
        try:
            d["fields"] = json.loads(d["fields"])
        except ValueError:
            d["fields"] = {}
    return d


async def has_pending(session) -> bool:
    pool = clients.db_pool
    async with pool.acquire() as conn:
        return bool(await conn.fetchval(
            "SELECT 1 FROM calendar_pending_actions WHERE session_id=$1", session))


async def _clear_pending(session) -> None:
    pool = clients.db_pool
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM calendar_pending_actions WHERE session_id=$1", session)


async def _audit(user_id, workspace_id, provider, action, allowed, reason, event_ref=None):
    pool = clients.db_pool
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO calendar_access_events (user_id, workspace_id, provider, action, "
            "allowed, reason, event_ref) VALUES ($1,$2,$3,$4,$5,$6,$7)",
            user_id, workspace_id, provider, action, allowed,
            reason[:300] if reason else None, event_ref)


async def _trace(session_id, user_id, workspace_id, *, trace_type, status="ok", result=None):
    await write_trace(
        session_id=session_id, user_id=user_id, trace_type=trace_type, status=status,
        selected_agent=CHRONOS, tool_name="chat_calendar", tool_result=result or {},
        workspace_id=workspace_id)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def _local_dt(iso_str):
    """Parse a provider ISO timestamp into a CORA-TIMEZONE datetime (the provider
    returns UTC/offset times; raw UTC is confusing — an evening-EDT event shows the
    next calendar day). Returns None if unparseable."""
    if not iso_str or iso_str == "—":
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(clock.current_tz())


def _fmt_event_when(e) -> str:
    """Friendly local-time window, e.g. 'Wed Jun 24, 8:30 – 10:00 PM'."""
    if e.get("all_day"):
        try:
            d = date.fromisoformat((e.get("start") or "")[:10])
            return f"{d.strftime('%a %b %-d')} (all day)"
        except ValueError:
            return f"{e.get('start', '—')} (all day)"
    s, en = _local_dt(e.get("start")), _local_dt(e.get("end"))
    if not s:
        return e.get("start", "—")
    day, st = s.strftime("%a %b %-d"), s.strftime("%-I:%M %p")
    if en and en.date() == s.date():
        return f"{day}, {st} – {en.strftime('%-I:%M %p')}"
    if en:
        return f"{day} {st} → {en.strftime('%a %b %-d %-I:%M %p')}"
    return f"{day}, {st}"


def _act_hint() -> str:
    return ("\n_Reply e.g. **“cancel 2”** or **“reschedule 2 to 3pm”** to act on a "
            "numbered event (I'll confirm before changing anything)._")


def _render_events(provider, events, label) -> str:
    if not events:
        return f"**{provider}** — no events for {label} (read-only)."
    lines = [f"**{provider} — {len(events)} event(s) for {label}** (read-only)"]
    for i, e in enumerate(events, 1):
        loc = f" · {_trunc(e.get('location'), 40)}" if e.get("location") else ""
        lines.append(f"**{i}.** {_trunc(e.get('title'))} · {_fmt_event_when(e)}{loc}")
    return "\n".join(lines) + _act_hint()


def _short(provider) -> str:
    return {"google_calendar": "google", "outlook_calendar": "outlook",
            "gmail": "gmail", "outlook_mail": "outlook"}.get(provider, provider or "")


def _render_events_multi(providers, events, label, skipped) -> str:
    """Render events merged across MULTIPLE calendars, each line tagged with its source."""
    head = " + ".join(_short(p) for p in providers)
    if not events:
        base = f"**Calendar ({head})** — no events for {label} (read-only)."
    else:
        lines = [f"**Calendar — {len(events)} event(s) for {label}** ({head}, read-only)"]
        for i, e in enumerate(events, 1):
            tag = f"[{_short(e.get('provider'))}] " if e.get("provider") else ""
            loc = f" · {_trunc(e.get('location'), 40)}" if e.get("location") else ""
            lines.append(f"**{i}.** {tag}{_trunc(e.get('title'))} · {_fmt_event_when(e)}{loc}")
        base = "\n".join(lines) + _act_hint()
    if skipped:
        base += "\n\n_Skipped: " + "; ".join(
            f"{_short(s['provider'])} ({s['reason']})" for s in skipped) + "._"
    return base


def _fmt_when(fields) -> str:
    s, e = fields.get("start_time"), fields.get("end_time")
    if s and e:
        return f"{s} → {e}"
    return s or "time not specified"


def _confirm_card_create(provider, fields, calendar_name=None) -> str:
    att = fields.get("attendees") or []
    lines = [f"📅 **Ready to create on {provider}** — reply **confirm** to create "
             "(invites will be sent) or **cancel**:",
             f"- **{fields.get('title') or 'New event'}**",
             f"- When: {_fmt_when(fields)}"]
    if calendar_name:
        lines.append(f"- Calendar: {calendar_name}")
    if fields.get("location"):
        lines.append(f"- Where: {fields['location']}")
    if att:
        lines.append(f"- Attendees: {', '.join(att)}")
    if not fields.get("start_time"):
        lines.append("\n_Note: I couldn't pin down a start time — tell me the day and "
                     "time and I'll restage it._")
    return "\n".join(lines)


def _cal_hint(target) -> str:
    cid = target.get("calendar_id")
    return f" · on calendar `{_trunc(cid, 36)}`" if cid and cid != "primary" else ""


def _confirm_card_update(provider, target, fields) -> str:
    lines = [f"📅 **Ready to reschedule on {provider}** — reply **confirm** or **cancel**:",
             f"- Event: **{target.get('title')}** (currently {_fmt_event_when(target)}){_cal_hint(target)}",
             f"- New time: {_fmt_when(fields)}"]
    if fields.get("title") and fields["title"] != target.get("title"):
        lines.append(f"- New title: {fields['title']}")
    return "\n".join(lines)


def _confirm_card_delete(provider, target) -> str:
    return (f"📅 **Ready to cancel on {provider}** — reply **confirm** or **cancel**:\n"
            f"- Event: **{target.get('title')}** ({_fmt_event_when(target)}){_cal_hint(target)}\n"
            "_Attendees will be notified of the cancellation._")


def _read_blocked_msg(provider, decision) -> str:
    return (f"🔒 I can't read your {provider} — calendar access is governed and "
            f"currently **disabled** ({decision['reason']}). Read access requires the "
            "provider connected with a calendar scope, the provider's read capability, "
            "and an enabled `calendar_read` feature flag. No calendar data was accessed.")


def _write_blocked_msg(provider, action, decision) -> str:
    return (f"🔒 I can't {action} a {provider} event — calendar **writes** are governed "
            f"and currently **disabled** ({decision['reason']}). A real write requires the "
            "provider connected with a write scope, the provider's write capability, the "
            "`calendar_write` feature flag, AND the CALENDAR_EXECUTION_ENABLED switch on. "
            "Nothing was changed on your calendar.")


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #

async def handle_calendar_turn(
    *, message: str, command: Optional[tuple[str, dict]], confirmation: Optional[str],
    session_uuid: uuid.UUID, user_id: uuid.UUID, workspace_uuid: Optional[uuid.UUID],
    scope_type: str, is_admin: bool, selection: Optional[int] = None,
    list_action: Optional[tuple[str, int]] = None,
) -> tuple[bool, Optional[str]]:
    """Single entry point. A "cancel 4"/"reschedule 2" against the last numbered list
    wins when such a list is pending; otherwise a fresh `command` wins, then a bare
    `selection` and a `confirmation`. Returns (False, None) to fall through."""
    # A numbered pick ("cancel 4", "reschedule 2 to 3pm") against the last list takes
    # precedence over re-parsing it as a brand-new command — but ONLY when a numbered
    # list is actually pending (otherwise "reschedule 2 to 3pm" is a normal update).
    if list_action is not None:
        _pend = await _get_pending(session_uuid)
        if _pend is not None and _pend["kind"] == "select_target":
            act, idx = list_action
            ofields = (await extract_event_fields(message, "update")) if act == "update" else None
            return await _handle_selection(idx, _pend, message, override_action=act,
                                           override_fields=ofields, session_uuid=session_uuid,
                                           user_id=user_id, workspace_uuid=workspace_uuid)

    if command is None:
        if confirmation is None and selection is None and list_action is None:
            return False, None
        pending = await _get_pending(session_uuid)
        if pending is None:
            return False, None
        # The last numbered list (a read, or a "which of these?" candidate set) is
        # resolved by "cancel 4" (verb+number) or a bare "4", which stages the real
        # write for confirmation. A "cancel" abandons it.
        if pending["kind"] == "select_target":
            if selection is not None:
                return await _handle_selection(selection, pending, message, session_uuid=session_uuid,
                                               user_id=user_id, workspace_uuid=workspace_uuid)
            if confirmation == "cancel":
                await _clear_pending(session_uuid)
                return True, "✓ Okay — nothing changed."
            return True, "Reply with the number of the event (e.g. **cancel 4**), or name it."
        if confirmation is None:
            return False, None
        # Honor a calendar redirect supplied alongside the confirmation, e.g.
        # "confirm but on google". Only for CREATE: an update/delete acts on an event
        # that already lives on a specific calendar, so a provider word there is ignored
        # (we must never retarget an existing event onto the wrong calendar). The slot
        # was found free across ALL calendars, so re-pointing a create to the named
        # provider's primary is safe.
        override = _named_provider(message)
        if confirmation == "confirm" and override and pending["kind"] == "create" \
                and override != pending["provider"]:
            pending["provider"] = override
            pending["target_calendar_id"] = "primary"
            await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_REQUESTED,
                         result={"kind": "create", "provider": override, "redirect": True})
        return await _handle_confirmation(confirmation, pending, session_uuid=session_uuid,
                                          user_id=user_id, workspace_uuid=workspace_uuid)

    kind, payload = command
    if kind == "list":
        providers = await _resolve_read_providers(message, user_id)
        await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_REQUESTED,
                     result={"providers": providers, "kind": kind})
        return await _handle_read(providers, message, session_uuid=session_uuid,
                                  user_id=user_id, workspace_uuid=workspace_uuid)
    # update/delete with NO named provider + more than one connected calendar → resolve
    # the target across ALL of them (so "cancel that meeting" finds it on whichever
    # calendar holds it). create stays single (a new event lands on one calendar).
    if kind in ("update", "delete") and _named_provider(message) is None:
        providers = await _resolve_read_providers("", user_id)
        if len(providers) > 1:
            await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_REQUESTED,
                         result={"providers": providers, "kind": kind})
            return await _prepare_write_multi(kind, providers, message, payload,
                                              session_uuid=session_uuid, user_id=user_id,
                                              workspace_uuid=workspace_uuid)
    provider = await _resolve_provider(message, user_id)
    await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_REQUESTED,
                 result={"provider": provider, "kind": kind})
    return await _prepare_write(kind, provider, message, payload, session_uuid=session_uuid,
                                user_id=user_id, workspace_uuid=workspace_uuid)


async def _read_one_calendar(provider, time_min, time_max, *, session_uuid, user_id,
                             workspace_uuid):
    """Gate + broker + read ONE calendar (events tagged with their provider). Audits +
    traces per provider. Returns (events|None, {provider, reason}) — events None when
    gated out / no token / read error."""
    decision = await _read_gate(provider, user_id)
    if not decision["allowed"]:
        await _audit(user_id, workspace_uuid, provider, "list", False, decision["reason"])
        if decision["capability_mismatch"]:
            await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_CAPABILITY_DENIED,
                         status="blocked", result={"provider": provider, "reason": "supports_calendar_read=false"})
        return None, {"provider": provider, "reason": decision["reason"]}
    adapter = calendar_adapters.resolve_calendar_adapter(provider)
    if adapter is None:
        await _audit(user_id, workspace_uuid, provider, "list", False, "no calendar adapter")
        return None, {"provider": provider, "reason": "no adapter"}
    token = await _get_access_token(provider, user_id)
    if not token:
        await _audit(user_id, workspace_uuid, provider, "list", False, "no usable token (broker)")
        return None, {"provider": provider, "reason": "no usable token"}
    try:
        events = await adapter.list_events(access_token=token, time_min=time_min,
                                           time_max=time_max, limit=50)
    except calendar_adapters.CalendarAccessDisabled:
        await _audit(user_id, workspace_uuid, provider, "list", False, "adapter disabled")
        return None, {"provider": provider, "reason": "adapter disabled"}
    except calendar_adapters.CalendarError as exc:
        await _audit(user_id, workspace_uuid, provider, "list", False, str(exc))
        await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_PROVIDER_FAILED,
                     status="error", result={"provider": provider, "kind": "list", "error": str(exc)})
        return None, {"provider": provider, "reason": f"read failed ({exc})"}
    for e in events:
        e["provider"] = provider
    await _audit(user_id, workspace_uuid, provider, "list", True, "read ok",
                 event_ref=(events[0].get("id") if events else None))
    return events, {"provider": provider, "reason": "ok"}


async def _handle_read(providers, message, *, session_uuid, user_id, workspace_uuid):
    """Dispatch a calendar read. One provider (named, or only one connected) → the exact
    single-provider path. Multiple → read each (gated) and merge across calendars."""
    if len(providers) <= 1:
        return await _handle_read_single(
            providers[0] if providers else "google_calendar", message,
            session_uuid=session_uuid, user_id=user_id, workspace_uuid=workspace_uuid)
    time_min, time_max, label = resolve_read_window(message)
    merged, oks, skipped = [], [], []
    for p in providers:
        evs, info = await _read_one_calendar(p, time_min, time_max, session_uuid=session_uuid,
                                             user_id=user_id, workspace_uuid=workspace_uuid)
        if evs is not None:
            merged.extend(evs)
            oks.append(p)
        else:
            skipped.append(info)
    if not oks:
        await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_LIST,
                     status="blocked", result={"providers": providers, "skipped": skipped})
        notes = "; ".join(f"{_short(s['provider'])}: {s['reason']}" for s in skipped)
        return True, (f"🔒 I couldn't read any of your calendars right now — {notes}. "
                      "No calendar data was accessed.")
    merged.sort(key=lambda e: e.get("start") or "")
    merged = _dedupe_series(merged)[:12]
    await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_LIST,
                 result={"providers": oks, "count": len(merged), "window": label})
    await _stash_list_context(session_uuid, user_id=user_id, workspace_id=workspace_uuid, events=merged)
    return True, _render_events_multi(oks, merged, label, skipped)


async def _handle_read_single(provider, message, *, session_uuid, user_id, workspace_uuid):
    decision = await _read_gate(provider, user_id)
    if not decision["allowed"]:
        await _audit(user_id, workspace_uuid, provider, "list", False, decision["reason"])
        if decision["capability_mismatch"]:
            await _trace(session_uuid, user_id, workspace_uuid,
                         trace_type=TRACE_CAPABILITY_DENIED, status="blocked",
                         result={"provider": provider, "reason": "supports_calendar_read=false"})
        return True, _read_blocked_msg(provider, decision)

    adapter = calendar_adapters.resolve_calendar_adapter(provider)
    if adapter is None:
        await _audit(user_id, workspace_uuid, provider, "list", False, "no calendar adapter")
        return True, f"No calendar adapter is available for {provider}."
    token = await _get_access_token(provider, user_id)
    if not token:
        await _audit(user_id, workspace_uuid, provider, "list", False, "no usable token (broker)")
        return True, (f"🔒 Calendar read for {provider} is enabled by policy, but I "
                      "couldn't obtain a usable access token (it may need reconnecting). "
                      "No calendar data was accessed.")
    time_min, time_max, label = resolve_read_window(message)
    try:
        events = await adapter.list_events(access_token=token, time_min=time_min,
                                           time_max=time_max, limit=50)
    except calendar_adapters.CalendarAccessDisabled:
        await _audit(user_id, workspace_uuid, provider, "list", False, "adapter disabled")
        return True, "🔒 Calendar read is enabled by policy but no live read could be performed."
    except calendar_adapters.CalendarError as exc:
        await _audit(user_id, workspace_uuid, provider, "list", False, str(exc))
        await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_PROVIDER_FAILED,
                     status="error", result={"provider": provider, "kind": "list", "error": str(exc)})
        return True, f"⚠️ The {provider} calendar read failed ({exc}). Nothing was changed."
    events = _dedupe_series(events)[:10]
    await _audit(user_id, workspace_uuid, provider, "list", True, "read ok",
                 event_ref=(events[0].get("id") if events else None))
    await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_LIST,
                 result={"provider": provider, "count": len(events), "window": label})
    for e in events:
        e.setdefault("provider", provider)
    await _stash_list_context(session_uuid, user_id=user_id, workspace_id=workspace_uuid, events=events)
    return True, _render_events(provider, events, label)


async def gather_events_window(*, user_id, workspace_uuid, session_uuid, time_min, time_max):
    """Read ALL connected calendars for an explicit [time_min, time_max] (RFC3339 UTC)
    window, gated + brokered + audited per provider exactly like a chat calendar read
    (each provider fails closed independently), merged + de-duped. Shared by the Daily
    Briefing (today) and smart scheduling (free/busy). Returns
    {"events", "providers_ok", "skipped"}."""
    providers = await _resolve_read_providers("", user_id)
    merged, oks, skipped = [], [], []
    for p in providers:
        evs, info = await _read_one_calendar(p, time_min, time_max, session_uuid=session_uuid,
                                             user_id=user_id, workspace_uuid=workspace_uuid)
        if evs is not None:
            merged.extend(evs)
            oks.append(p)
        else:
            skipped.append(info)
    merged.sort(key=lambda e: e.get("start") or "")
    return {"events": _dedupe_series(merged), "providers_ok": oks, "skipped": skipped}


async def gather_day_events(*, user_id, workspace_uuid, session_uuid):
    """Composite-friendly read for the Daily Briefing: today's events merged across
    ALL connected calendars. Returns gather_events_window(...) + a 'today' label."""
    time_min, time_max, label = resolve_read_window("today")
    res = await gather_events_window(user_id=user_id, workspace_uuid=workspace_uuid,
                                     session_uuid=session_uuid, time_min=time_min, time_max=time_max)
    res["label"] = label
    return res


# Filler stripped from a target query so "cancel my X meeting" doesn't fail to
# match the event titled "X" (the trailing noun otherwise breaks substring match).
_QUERY_STOPWORDS = {"meeting", "meetings", "event", "events", "appointment",
                    "appointments", "call", "the", "my", "a", "an", "on", "at",
                    "for", "to", "with",
                    # action verbs, in case they leak past _extract_event_query
                    "cancel", "delete", "remove", "reschedule", "move", "change",
                    "push", "update", "edit"}


def _query_tokens(query: Optional[str]) -> list:
    return [t for t in re.findall(r"[a-z0-9]+", (query or "").lower())
            if t not in _QUERY_STOPWORDS]


def _match_events(query: Optional[str], events: list) -> list:
    """Events whose title contains ALL significant query tokens (order-free)."""
    toks = _query_tokens(query)
    if not toks:
        return []
    return [e for e in events
            if all(t in (e.get("title") or "").lower() for t in toks)]


async def _resolve_target(adapter, token, query: Optional[str]) -> dict:
    """Resolve which event a write targets. Returns a status dict and NEVER
    silently falls back to an arbitrary event for a mismatched query — a
    mis-parsed "cancel my X" must not delete an unrelated meeting. status ∈
    none | no_match | ambiguous | ok."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    events = _dedupe_series(await adapter.list_events(
        access_token=token, time_min=now.isoformat(),
        time_max=(now + timedelta(days=30)).isoformat(), limit=50))
    if not events:
        return {"status": "none"}
    if _query_tokens(query):
        matches = _match_events(query, events)
        if not matches:
            return {"status": "no_match", "candidates": events[:5]}
        if len(matches) > 1:
            return {"status": "ambiguous", "candidates": matches[:5]}
        return {"status": "ok", "event": matches[0]}
    # No usable query ("cancel my meeting") → only auto-pick when there's exactly
    # ONE upcoming event; otherwise ask rather than guess.
    if len(events) == 1:
        return {"status": "ok", "event": events[0]}
    return {"status": "ambiguous", "candidates": events[:5]}


def _candidate_lines(events) -> str:
    return "\n".join(f"- **{_trunc(e.get('title'))}** · {_fmt_event_when(e)}{_cal_hint(e)}"
                     for e in events)


def _target_help_msg(provider, kind, status, candidates, query) -> str:
    verb = {"update": "reschedule", "delete": "cancel"}.get(kind, kind)
    if status == "none":
        return f"I don't see any upcoming {provider} events to {verb}."
    if status == "no_match":
        head = (f"I couldn't find an upcoming event matching “{query}”." if query
                else f"I couldn't tell which event to {verb}.")
        return (f"{head} Your upcoming events:\n{_candidate_lines(candidates)}\n\n"
                f"Tell me which one to {verb} (use words from its title) — nothing was changed.")
    return (f"More than one upcoming event could match — I won't {verb} the wrong one. "
            f"Did you mean:\n{_candidate_lines(candidates)}\n\n"
            f"Name the event to {verb} — nothing was changed.")


def _slim_candidate(e: dict) -> dict:
    """Just the fields the confirm card needs — kept small enough to stash in the
    pending row's JSONB and re-render later. Carries `provider` so a cross-calendar
    pick deletes/updates on the event's own calendar."""
    return {"id": e.get("id"), "title": e.get("title"), "calendar_id": e.get("calendar_id"),
            "start": e.get("start"), "end": e.get("end"), "all_day": e.get("all_day"),
            "provider": e.get("provider")}


def _numbered_target_msg(provider, kind, candidates, query, status) -> str:
    verb = {"update": "reschedule", "delete": "cancel"}.get(kind, kind)
    if status == "no_match" and query:
        head = (f"I couldn't find an upcoming event matching “{query}”. Did you mean "
                "one of these")
    else:
        head = f"More than one upcoming event could match — I won't {verb} the wrong one. Did you mean"
    lines = [head + ":", ""]
    for i, e in enumerate(candidates, 1):
        tag = f"[{_short(e.get('provider'))}] " if e.get("provider") else ""
        lines.append(f"**{i}.** {tag}{_trunc(e.get('title'))} · {_fmt_event_when(e)}{_cal_hint(e)}")
    lines.append(f"\n_Reply with a number (1–{len(candidates)}) to pick, or name the event. "
                 "Nothing was changed._")
    return "\n".join(lines)


async def _stage_selection(session, *, user_id, workspace_id, provider, action, candidates,
                           update_fields=None) -> None:
    """Stash a numbered candidate set so a follow-up "2" resolves to a specific event."""
    fields = {"action": action, "candidates": [_slim_candidate(c) for c in candidates]}
    if update_fields is not None:
        fields["update_fields"] = update_fields
    await _set_pending(session, user_id=user_id, workspace_id=workspace_id,
                       kind="select_target", provider=provider, fields=fields)


async def _stash_list_context(session, *, user_id, workspace_id, events) -> None:
    """After a read, remember the numbered events so a follow-up "cancel 4" /
    "reschedule 2 to 3pm" acts on item #4/#2. Stores no action yet (the follow-up
    verb decides). Never clobbers a write that's already awaiting confirmation."""
    if not events:
        return
    existing = await _get_pending(session)
    if existing and existing["kind"] in ("create", "update", "delete"):
        return
    await _set_pending(session, user_id=user_id, workspace_id=workspace_id,
                       kind="select_target", provider=events[0].get("provider") or "multi",
                       fields={"action": None, "candidates": [_slim_candidate(e) for e in events]})


async def _handle_selection(selection, pending, message, *, override_action=None,
                            override_fields=None, session_uuid, user_id, workspace_uuid):
    """Resolve a numbered pick against a stashed list, then stage the real write
    (delete/update) for confirmation — never writes directly. `override_action` comes
    from a verb+number ("cancel 4"); otherwise the list's own action is used (a read
    list has none → ask which)."""
    data = pending.get("fields") or {}
    candidates = data.get("candidates") or []
    if selection < 1 or selection > len(candidates):
        return True, (f"That's not one of the {len(candidates)} options — reply with a "
                      f"number 1–{len(candidates)}, or name the event.")
    chosen = candidates[selection - 1]
    # The pick may live on a different calendar than the row's default (cross-provider
    # lists) — act on the event's OWN provider.
    provider = chosen.get("provider") or pending["provider"]
    action = override_action or data.get("action")
    if action is None:
        # A numbered read list with no action yet — ask which (never guess a write).
        return True, (f"Did you want to **cancel** or **reschedule** “{_trunc(chosen.get('title'))}” "
                      f"(#{selection})? Say e.g. **cancel {selection}** or "
                      f"**reschedule {selection} to <time>**.")
    decision = await _write_gate(provider, user_id, action)
    if not decision["allowed"]:
        await _audit(user_id, workspace_uuid, provider, action, False, decision["reason"])
        await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_WRITE_DENIED,
                     status="blocked", result={"provider": provider, "kind": action,
                                               "reason": decision["reason"], "from_selection": True})
        return True, _write_blocked_msg(provider, action, decision)
    if action == "update":
        fields = override_fields or data.get("update_fields") or {}
        await _set_pending(session_uuid, user_id=user_id, workspace_id=workspace_uuid,
                           kind="update", provider=provider, fields=fields,
                           target_event_id=chosen.get("id"),
                           target_calendar_id=chosen.get("calendar_id"),
                           target_summary=chosen.get("title"))
        await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_CONFIRM_PENDING,
                     result={"provider": provider, "kind": "update", "from_selection": True,
                             "target": chosen.get("id")})
        return True, _confirm_card_update(provider, chosen, fields)
    await _set_pending(session_uuid, user_id=user_id, workspace_id=workspace_uuid,
                       kind="delete", provider=provider, fields={},
                       target_event_id=chosen.get("id"),
                       target_calendar_id=chosen.get("calendar_id"),
                       target_summary=chosen.get("title"))
    await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_CONFIRM_PENDING,
                 result={"provider": provider, "kind": "delete", "from_selection": True,
                         "target": chosen.get("id")})
    return True, _confirm_card_delete(provider, chosen)


# --------------------------------------------------------------------------- #
# Target-calendar resolution (CREATE) — choose which calendar a new event lands
# on from a natural-language name; default primary; refuse-on-ambiguous.
# --------------------------------------------------------------------------- #

def _extract_calendar_hint(message: str) -> Optional[str]:
    m = message or ""
    mo = _CAL_HINT_RE.search(m) or _CAL_NAMED_RE.search(m)
    if not mo:
        return None
    name = mo.group(1).strip().strip("\"'")
    # Drop a provider word ("on my outlook calendar" → provider, not a calendar named
    # "outlook"; "on my outlook Work calendar" → "Work").
    name = provider_defaults.strip_provider_words(name)
    # "my"/"the" alone (e.g. "to my calendar") carries no real calendar name.
    return name if name and name.lower() not in ("my", "the") else None


def _match_calendars(hint: Optional[str], calendars: list) -> list:
    """Calendars whose name matches the hint — exact (case-insensitive) wins over
    substring, so 'Work' picks 'Work' even when 'Work Projects' also exists."""
    h = (hint or "").strip().lower()
    if not h:
        return []
    exact = [c for c in calendars if (c.get("name") or "").strip().lower() == h]
    if exact:
        return exact
    return [c for c in calendars if h in (c.get("name") or "").lower()]


async def _resolve_calendar(adapter, token, hint: Optional[str]) -> dict:
    """Resolve which calendar a CREATE targets. No hint → primary. Otherwise list
    the user's writable calendars and match — never guessing on 0 or >1 matches.
    status ∈ ok | no_match | ambiguous."""
    if not hint:
        return {"status": "ok", "calendar": {"id": "primary", "name": "primary"}}
    cals = await adapter.list_calendars(access_token=token)
    matches = _match_calendars(hint, cals)
    if not matches:
        return {"status": "no_match", "candidates": cals[:8]}
    if len(matches) > 1:
        return {"status": "ambiguous", "candidates": matches[:8]}
    return {"status": "ok", "calendar": matches[0]}


def _calendar_help_msg(provider, status, candidates, hint) -> str:
    names = "\n".join(f"- {c.get('name')}" for c in candidates) or "- (none found)"
    head = (f"I couldn't find a writable {provider} calendar matching “{hint}”."
            if status == "no_match"
            else f"More than one {provider} calendar matches “{hint}” — I won't guess which.")
    return (f"{head} Your calendars:\n{names}\n\n"
            "Tell me which one (use its exact name) — nothing was created.")


# --------------------------------------------------------------------------- #
# Per-user default WRITE calendar (the specific calendar WITHIN a provider a
# hint-less CREATE targets). Complements the per-user default *provider*
# (provider_defaults): "make Work my default calendar" stores the resolved
# calendar; an unnamed create then lands there instead of `primary`.
# --------------------------------------------------------------------------- #

# Set: "<verb> [my] <name> (as) [my|the] default [calendar]" (name BEFORE) or
# "default calendar (to|=|:) <name>" (name AFTER). Clear reverts to primary.
_CDEF_SET_VERB_RE = re.compile(r"\b(set|use|make|prefer|change)\b", re.I)
_CDEF_CLEAR_RE = re.compile(
    r"\b(clear|remove|unset|reset|forget|delete|drop)\b.*\bdefault\s+calendar\b", re.I)
_CDEF_BEFORE_RE = re.compile(
    r"\b(?:set|use|make|prefer|change)\s+(?:my\s+)?(.+?)\s+(?:as\s+)?(?:my|the)\s+"
    r"default(?:\s+calendar)?\b", re.I)
_CDEF_AFTER_RE = re.compile(
    r"\bdefault\s+calendar\b\s*(?:to(?:\s+be)?|=|:|is)\s+(.+?)(?:[.,!?]|$)", re.I)


def detect_calendar_default_command(message: str) -> Optional[dict]:
    """Detect setting/clearing the per-user default WRITE calendar. Returns
    {"action":"set","name":<str>} | {"action":"clear"} | None. Requires a
    'default'+'calendar' phrase; a provider-only default ("make google my default
    calendar") carries no calendar NAME (handled upstream by provider_defaults) →
    None here. Conservative: a plain read/create with no "default" never matches."""
    low = (message or "").lower()
    if "default" not in low or "calendar" not in low:
        return None
    if _CDEF_CLEAR_RE.search(low):
        return {"action": "clear"}
    if not _CDEF_SET_VERB_RE.search(low):
        return None
    mo = _CDEF_AFTER_RE.search(message) or _CDEF_BEFORE_RE.search(message)
    if not mo:
        return None
    name = re.sub(r"\s+calendar$", "", mo.group(1).strip(), flags=re.I).strip().strip("\"'")
    name = re.sub(r"^(?:my|the)\s+", "", name, flags=re.I).strip()
    name = provider_defaults.strip_provider_words(name).strip()
    if not name or name.lower() in ("my", "the"):
        return None
    if name.lower() == "primary":  # "make primary my default" → revert to primary
        return {"action": "clear"}
    return {"action": "set", "name": name}


async def _set_default_calendar(user_id, provider, calendar_id, calendar_name) -> None:
    pool = clients.db_pool
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_calendar_defaults (user_id, provider_name, calendar_id, "
            "calendar_name, updated_at) VALUES ($1,$2,$3,$4,NOW()) "
            "ON CONFLICT (user_id, provider_name) DO UPDATE "
            "SET calendar_id=$3, calendar_name=$4, updated_at=NOW()",
            user_id, provider, calendar_id, calendar_name)


async def get_default_calendar(user_id, provider) -> Optional[dict]:
    """The user's chosen default calendar for `provider`, as {"id","name"}, else None."""
    pool = clients.db_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT calendar_id, calendar_name FROM user_calendar_defaults "
            "WHERE user_id=$1 AND provider_name=$2", user_id, provider)
    return {"id": row["calendar_id"], "name": row["calendar_name"]} if row else None


async def _clear_default_calendar(user_id, provider) -> None:
    pool = clients.db_pool
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM user_calendar_defaults WHERE user_id=$1 AND provider_name=$2",
            user_id, provider)


async def handle_calendar_default_command(
    cmd: dict, *, message: str, session_uuid: uuid.UUID, user_id: uuid.UUID,
    workspace_uuid: Optional[uuid.UUID],
) -> tuple[bool, str]:
    """Set or clear the per-user default WRITE calendar. The provider is the one named
    in the message, else the user's default/most-recent calendar provider. Setting
    resolves the calendar NAME against the provider's writable calendars (exact-wins,
    refuse-on-ambiguous, same as a named-calendar create) before storing it."""
    provider = await _resolve_provider(message, user_id)
    if cmd["action"] == "clear":
        await _clear_default_calendar(user_id, provider)
        await _trace(session_uuid, user_id, workspace_uuid,
                     trace_type="chat_calendar_default_cleared", result={"provider": provider})
        return True, (f"✓ Cleared your default **{_short(provider)}** calendar — new events "
                      "go to your primary calendar unless you name one.")
    name = cmd["name"]
    adapter = calendar_adapters.resolve_calendar_adapter(provider)
    if adapter is None:
        return True, f"No calendar adapter is available for {provider}."
    token = await _get_access_token(provider, user_id)
    if not token:
        return True, (f"🔒 I couldn't read your {_short(provider)} calendars to set a default "
                      "(no usable access token). Nothing was changed.")
    try:
        res = await _resolve_calendar(adapter, token, name)
    except calendar_adapters.CalendarError as exc:
        return True, f"⚠️ I couldn't look up your {_short(provider)} calendars ({exc}). Nothing was changed."
    if res["status"] != "ok":
        return True, _calendar_help_msg(_short(provider), res["status"], res.get("candidates", []), name)
    cal = res["calendar"]
    cal_name = cal.get("name") or name
    await _set_default_calendar(user_id, provider, cal["id"], cal_name)
    await _trace(session_uuid, user_id, workspace_uuid, trace_type="chat_calendar_default_set",
                 result={"provider": provider, "calendar_id": cal["id"]})
    return True, (f"✓ Set **{cal_name}** as your default {_short(provider)} calendar. New events "
                  "go there unless you name a different one (e.g. **“on my Personal calendar”**). "
                  "Say **“clear my default calendar”** to revert to primary.")


async def _resolve_target_across(providers, query, *, session_uuid, user_id, workspace_uuid):
    """Find an update/delete target across MULTIPLE calendars: read each (gated +
    brokered, events tagged with their provider), match the query, and decide. So
    "cancel that meeting" finds the event on whichever calendar holds it. status ∈
    none | no_match (+candidates) | ambiguous (+candidates) | ok (+event w/ provider)."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    tmin, tmax = now.isoformat(), (now + timedelta(days=30)).isoformat()
    all_events = []
    for p in providers:
        evs, _info = await _read_one_calendar(p, tmin, tmax, session_uuid=session_uuid,
                                              user_id=user_id, workspace_uuid=workspace_uuid)
        if evs:
            all_events.extend(_dedupe_series(evs))
    if not all_events:
        return {"status": "none"}
    all_events.sort(key=lambda e: e.get("start") or "")
    if _query_tokens(query):
        matches = _match_events(query, all_events)
        if not matches:
            return {"status": "no_match", "candidates": all_events[:8]}
        if len(matches) > 1:
            return {"status": "ambiguous", "candidates": matches[:8]}
        return {"status": "ok", "event": matches[0]}
    if len(all_events) == 1:
        return {"status": "ok", "event": all_events[0]}
    return {"status": "ambiguous", "candidates": all_events[:8]}


async def _stage_one_write(kind, provider, message, target, *, session_uuid, user_id, workspace_uuid):
    """Write-gate a resolved update/delete target on its own provider, then stage the
    confirm-before-write action. Shared by the cross-provider path and selection."""
    decision = await _write_gate(provider, user_id, kind)
    if not decision["allowed"]:
        await _audit(user_id, workspace_uuid, provider, kind, False, decision["reason"])
        await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_WRITE_DENIED,
                     status="blocked", result={"provider": provider, "kind": kind,
                                               "reason": decision["reason"]})
        return True, _write_blocked_msg(provider, kind, decision)
    if kind == "update":
        fields = await extract_event_fields(message, "update")
        await _set_pending(session_uuid, user_id=user_id, workspace_id=workspace_uuid, kind="update",
                           provider=provider, fields=fields, target_event_id=target.get("id"),
                           target_calendar_id=target.get("calendar_id"), target_summary=target.get("title"))
        await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_CONFIRM_PENDING,
                     result={"provider": provider, "kind": "update", "target": target.get("id")})
        return True, _confirm_card_update(provider, target, fields)
    await _set_pending(session_uuid, user_id=user_id, workspace_id=workspace_uuid, kind="delete",
                       provider=provider, fields={}, target_event_id=target.get("id"),
                       target_calendar_id=target.get("calendar_id"), target_summary=target.get("title"))
    await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_CONFIRM_PENDING,
                 result={"provider": provider, "kind": "delete", "target": target.get("id")})
    return True, _confirm_card_delete(provider, target)


async def _prepare_write_multi(kind, providers, message, payload, *, session_uuid, user_id, workspace_uuid):
    """Update/delete with no named provider + multiple connected calendars: resolve the
    target across all of them. One match → stage it on its own calendar; several (or a
    no-match with suggestions) → a NUMBERED, provider-tagged list to pick from."""
    query = (payload or {}).get("query")
    res = await _resolve_target_across(providers, query, session_uuid=session_uuid,
                                       user_id=user_id, workspace_uuid=workspace_uuid)
    if res["status"] == "ok":
        target = res["event"]
        return await _stage_one_write(kind, target.get("provider") or providers[0], message, target,
                                      session_uuid=session_uuid, user_id=user_id, workspace_uuid=workspace_uuid)
    candidates = res.get("candidates", [])
    await _audit(user_id, workspace_uuid, "multi", kind, False, f"target {res['status']}")
    await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_WRITE_DENIED,
                 status="blocked", result={"providers": providers, "kind": kind,
                                           "reason": f"target_{res['status']}", "candidates": len(candidates)})
    if candidates and res["status"] in ("ambiguous", "no_match"):
        upd = await extract_event_fields(message, "update") if kind == "update" else None
        await _stage_selection(session_uuid, user_id=user_id, workspace_id=workspace_uuid,
                               provider=providers[0], action=kind, candidates=candidates, update_fields=upd)
        return True, _numbered_target_msg(None, kind, candidates, query, res["status"])
    return True, _target_help_msg("your calendars", kind, res["status"], candidates, query)


async def _prepare_write(kind, provider, message, payload, *, session_uuid, user_id, workspace_uuid):
    """Gate the write. If closed → deny (+ create falls back to an internal proposal).
    If open → STAGE the action and ask the user to confirm; nothing touches the
    provider until `_handle_confirmation` fires."""
    decision = await _write_gate(provider, user_id, kind)
    if not decision["allowed"]:
        await _audit(user_id, workspace_uuid, provider, kind, False, decision["reason"])
        await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_WRITE_DENIED,
                     status="blocked", result={"provider": provider, "kind": kind,
                                               "reason": decision["reason"],
                                               "kill_switch_clear": decision["kill_switch_clear"]})
        msg = _write_blocked_msg(provider, kind, decision)
        if kind == "create":
            fields = await extract_event_fields(message, "create")
            fb = await _create_proposal_fallback(fields, message, session_uuid=session_uuid,
                                                 user_id=user_id, workspace_uuid=workspace_uuid)
            if fb:
                msg += f"\n\n{fb}"
        return True, msg

    adapter = calendar_adapters.resolve_calendar_adapter(provider)
    if adapter is None:
        await _audit(user_id, workspace_uuid, provider, kind, False, "no calendar adapter")
        return True, f"No calendar adapter is available for {provider}."

    if kind == "create":
        fields = await extract_event_fields(message, "create")
        # Default to primary. Only when the user names a calendar ("on my Work
        # calendar") do we look it up to resolve a concrete calendar_id; otherwise
        # fall back to the user's saved default WRITE calendar (no provider call —
        # id+name were resolved when it was set), else primary.
        cal_id, cal_name = "primary", None
        hint = _extract_calendar_hint(message)
        if not hint:
            dflt = await get_default_calendar(user_id, provider)
            if dflt:
                cal_id, cal_name = dflt["id"], dflt["name"]
        if hint:
            token = await _get_access_token(provider, user_id)
            if not token:
                await _audit(user_id, workspace_uuid, provider, kind, False, "no usable token (broker)")
                return True, (f"🔒 Calendar write for {provider} is enabled by policy, but I "
                              "couldn't obtain a usable access token. Nothing was changed.")
            try:
                res = await _resolve_calendar(adapter, token, hint)
            except calendar_adapters.CalendarError as exc:
                await _audit(user_id, workspace_uuid, provider, kind, False, str(exc))
                await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_PROVIDER_FAILED,
                             status="error", result={"provider": provider, "kind": kind, "error": str(exc)})
                return True, f"⚠️ I couldn't look up your {provider} calendars ({exc}). Nothing was changed."
            if res["status"] != "ok":
                await _audit(user_id, workspace_uuid, provider, kind, False, f"calendar {res['status']}")
                await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_WRITE_DENIED,
                             status="blocked", result={"provider": provider, "kind": kind,
                                                       "reason": f"calendar_{res['status']}"})
                return True, _calendar_help_msg(provider, res["status"], res.get("candidates", []), hint)
            cal_id, cal_name = res["calendar"]["id"], res["calendar"].get("name")
        await _set_pending(session_uuid, user_id=user_id, workspace_id=workspace_uuid,
                           kind="create", provider=provider, fields=fields, target_calendar_id=cal_id)
        await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_CONFIRM_PENDING,
                     result={"provider": provider, "kind": "create",
                             "has_start": bool(fields.get("start_time")), "calendar_id": cal_id})
        return True, _confirm_card_create(provider, fields,
                                          cal_name if cal_id != "primary" else None)

    # update/delete need a target resolved against the live calendar.
    token = await _get_access_token(provider, user_id)
    if not token:
        await _audit(user_id, workspace_uuid, provider, kind, False, "no usable token (broker)")
        return True, (f"🔒 Calendar write for {provider} is enabled by policy, but I "
                      "couldn't obtain a usable access token. Nothing was changed.")
    query = (payload or {}).get("query")
    try:
        res = await _resolve_target(adapter, token, query)
    except calendar_adapters.CalendarError as exc:
        await _audit(user_id, workspace_uuid, provider, kind, False, str(exc))
        await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_PROVIDER_FAILED,
                     status="error", result={"provider": provider, "kind": kind, "error": str(exc)})
        return True, f"⚠️ The {provider} calendar lookup failed ({exc}). Nothing was changed."
    if res["status"] != "ok":
        # No confident single target — do NOT guess a write target. When there are
        # candidates, present them NUMBERED and stash them so the user can reply "2";
        # otherwise just explain. Either way, nothing is written yet.
        candidates = res.get("candidates", [])
        await _audit(user_id, workspace_uuid, provider, kind, False, f"target {res['status']}")
        await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_WRITE_DENIED,
                     status="blocked", result={"provider": provider, "kind": kind,
                                               "reason": f"target_{res['status']}",
                                               "candidates": len(candidates)})
        if candidates and res["status"] in ("ambiguous", "no_match"):
            upd = await extract_event_fields(message, "update") if kind == "update" else None
            await _stage_selection(session_uuid, user_id=user_id, workspace_id=workspace_uuid,
                                   provider=provider, action=kind, candidates=candidates,
                                   update_fields=upd)
            return True, _numbered_target_msg(provider, kind, candidates, query, res["status"])
        return True, _target_help_msg(provider, kind, res["status"], candidates, query)
    target = res["event"]

    if kind == "update":
        fields = await extract_event_fields(message, "update")
        await _set_pending(session_uuid, user_id=user_id, workspace_id=workspace_uuid,
                           kind="update", provider=provider, fields=fields,
                           target_event_id=target["id"], target_calendar_id=target.get("calendar_id"),
                           target_summary=target["title"])
        await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_CONFIRM_PENDING,
                     result={"provider": provider, "kind": "update", "target": target["id"]})
        return True, _confirm_card_update(provider, target, fields)

    await _set_pending(session_uuid, user_id=user_id, workspace_id=workspace_uuid,
                       kind="delete", provider=provider, fields={},
                       target_event_id=target["id"], target_calendar_id=target.get("calendar_id"),
                       target_summary=target["title"])
    await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_CONFIRM_PENDING,
                 result={"provider": provider, "kind": "delete", "target": target["id"]})
    return True, _confirm_card_delete(provider, target)


async def stage_create(provider, fields, *, session_uuid, user_id, workspace_uuid,
                       calendar_id="primary", calendar_name=None):
    """Stage a confirm-before-write CREATE from EXPLICIT fields (no NL re-extraction),
    reusing the exact same write gate + pending store + confirm card as a chat 'create'.
    Smart scheduling uses this to book a found free slot; a later 'confirm' fires the
    real write through `_handle_confirmation`, unchanged. Returns (handled, text). FAILS
    CLOSED: if the write gate is shut, nothing is staged and the governed write-blocked
    message is returned."""
    decision = await _write_gate(provider, user_id, "create")
    if not decision["allowed"]:
        await _audit(user_id, workspace_uuid, provider, "create", False, decision["reason"])
        await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_WRITE_DENIED,
                     status="blocked", result={"provider": provider, "kind": "create",
                                               "reason": decision["reason"], "source": "scheduling"})
        return True, _write_blocked_msg(provider, "create", decision)
    await _set_pending(session_uuid, user_id=user_id, workspace_id=workspace_uuid,
                       kind="create", provider=provider, fields=fields, target_calendar_id=calendar_id)
    await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_CONFIRM_PENDING,
                 result={"provider": provider, "kind": "create", "source": "scheduling",
                         "has_start": bool(fields.get("start_time")), "calendar_id": calendar_id})
    return True, _confirm_card_create(provider, fields, calendar_name if calendar_id != "primary" else None)


async def _handle_confirmation(confirmation, pending, *, session_uuid, user_id, workspace_uuid):
    """Resolve a staged pending action. cancel → clear, no write. confirm →
    re-check the gate (it may have closed since staging) and fire the real write."""
    kind = pending["kind"]
    provider = pending["provider"]
    if confirmation == "cancel":
        await _clear_pending(session_uuid)
        await _audit(user_id, workspace_uuid, provider, kind, False, "user cancelled before write")
        await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_CANCELLED,
                     result={"provider": provider, "kind": kind})
        return True, f"✓ Cancelled — nothing was changed on your {provider}."

    decision = await _write_gate(provider, user_id, kind)
    if not decision["allowed"]:
        await _clear_pending(session_uuid)
        await _audit(user_id, workspace_uuid, provider, kind, False, decision["reason"])
        await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_WRITE_DENIED,
                     status="blocked", result={"provider": provider, "kind": kind,
                                               "reason": decision["reason"]})
        return True, _write_blocked_msg(provider, kind, decision)

    adapter = calendar_adapters.resolve_calendar_adapter(provider)
    token = await _get_access_token(provider, user_id)
    if adapter is None or not token:
        await _clear_pending(session_uuid)
        await _audit(user_id, workspace_uuid, provider, kind, False, "no adapter/token at confirm")
        return True, (f"🔒 Calendar write for {provider} is enabled by policy, but I "
                      "couldn't obtain a usable access token. Nothing was changed.")
    fields = pending.get("fields") or {}
    target_id = pending.get("target_event_id")
    target_cal = pending.get("target_calendar_id") or "primary"
    await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_CONFIRMED,
                 result={"provider": provider, "kind": kind})
    try:
        if kind == "create":
            ev = await adapter.create_event(access_token=token, fields=fields,
                                            calendar_id=target_cal)
        elif kind == "update":
            ev = await adapter.update_event(access_token=token, event_id=target_id,
                                            fields=fields, calendar_id=target_cal)
        else:
            ev = await adapter.delete_event(access_token=token, event_id=target_id,
                                            calendar_id=target_cal)
    except calendar_adapters.CalendarAccessDisabled:
        await _clear_pending(session_uuid)
        await _audit(user_id, workspace_uuid, provider, kind, False, "adapter disabled")
        return True, "🔒 Calendar write is enabled by policy but no live write could be performed."
    except calendar_adapters.CalendarError as exc:
        await _clear_pending(session_uuid)
        await _audit(user_id, workspace_uuid, provider, kind, False, str(exc))
        await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_PROVIDER_FAILED,
                     status="error", result={"provider": provider, "kind": kind, "error": str(exc)})
        # 403 on a write usually means the event lives on a calendar you can read
        # but don't own (e.g. a subscribed/shared calendar) — make that explicit.
        hint = ("" if "403" not in str(exc) else
                f" That calendar appears to be read-only for you (likely a shared or "
                f"subscribed calendar you don't own), so it can't be modified here.")
        return True, f"⚠️ The {provider} calendar {kind} failed ({exc}).{hint} Nothing was changed."

    await _clear_pending(session_uuid)
    trace_type = {"create": TRACE_CREATE, "update": TRACE_UPDATE, "delete": TRACE_DELETE}[kind]
    await _audit(user_id, workspace_uuid, provider, kind, True, f"{kind} ok", event_ref=ev.get("id"))
    await _trace(session_uuid, user_id, workspace_uuid, trace_type=trace_type,
                 result={"provider": provider, "event_id": ev.get("id")})
    verb = {"create": "Created", "update": "Updated", "delete": "Cancelled"}[kind]
    if kind == "delete":
        return True, f"✓ {verb} the {provider} event `{str(ev.get('id'))[:10]}`."
    return True, (f"✓ {verb} a {provider} event: **{_trunc(ev.get('title'))}** · "
                  f"{ev.get('start','—')} → {ev.get('end','—')}"
                  + (f"\n{ev['link']}" if ev.get("link") else ""))


async def agent_fire_calendar_create(*, provider, user_id, workspace_id, fields,
                                     calendar_id: str = "primary") -> dict:
    """Gated single calendar CREATE for the agent confirm-as-interrupt approve path
    (Phase 7 outward half). Mirrors _handle_confirmation's create branch but WITHOUT
    the session pending store: re-check _write_gate (which enforces the dedicated
    CALENDAR_EXECUTION_ENABLED master gate + the per-provider calendar_write flag +
    connection/scope/token), broker the token, fire adapter.create_event. Returns
    {"ok", "reason", "event_id", "title", "link"} and NEVER raises — the caller
    records the outcome. This helper cannot write while the calendar master gate is
    off: the gate is checked here, fail-closed. It does not consult
    agent_execution_enabled — the caller gates on that BEFORE invoking."""
    provider = (provider or "").strip()
    fields = fields or {}
    try:
        adapter = calendar_adapters.resolve_calendar_adapter(provider)
        if adapter is None:
            return {"ok": False, "reason": f"no calendar adapter for {provider or '(none)'}",
                    "event_id": None}
        try:
            uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
        except (ValueError, TypeError):
            return {"ok": False, "reason": "invalid user id", "event_id": None}
        decision = await _write_gate(provider, uid, "create")
        if not decision["allowed"]:
            await _audit(uid, workspace_id, provider, "create", False,
                         f"agent approve blocked: {decision['reason']}")
            return {"ok": False, "reason": decision["reason"], "event_id": None}
        token = await _get_access_token(provider, uid)
        if not token:
            await _audit(uid, workspace_id, provider, "create", False,
                         "agent approve: no usable token")
            return {"ok": False, "reason": "no usable access token", "event_id": None}
        try:
            ev = await adapter.create_event(access_token=token, fields=fields,
                                            calendar_id=calendar_id or "primary")
        except (calendar_adapters.CalendarAccessDisabled,
                calendar_adapters.CalendarError) as exc:
            await _audit(uid, workspace_id, provider, "create", False, str(exc) or "adapter error")
            return {"ok": False, "reason": str(exc) or "adapter disabled", "event_id": None}
        await _audit(uid, workspace_id, provider, "create", True,
                     "agent approve create ok", event_ref=ev.get("id"))
        return {"ok": True, "reason": "created", "event_id": ev.get("id"),
                "title": ev.get("title"), "link": ev.get("link")}
    except Exception as exc:  # defensive: the approve path must never crash on a fire
        logger.exception("agent_fire_calendar_create failed provider=%s", provider)
        return {"ok": False, "reason": f"unexpected error: {exc}", "event_id": None}


async def _create_proposal_fallback(fields, message, *, session_uuid, user_id, workspace_uuid) -> Optional[str]:
    try:
        row = await chronos_tools.create_schedule_proposal(
            workspace_id=workspace_uuid, user_id=user_id, proposal_type="meeting",
            title=fields.get("title") or "New event",
            description=fields.get("description") or (message or "").strip()[:1000],
            attendees=fields.get("attendees") or [],
            metadata={"source": "chat_calendar_blocked", "session_id": str(session_uuid)})
    except Exception:
        logger.exception("calendar create-fallback proposal failed session=%s", session_uuid)
        return None
    await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_PROPOSAL_FALLBACK,
                 result={"proposal_id": str(row["id"]), "title": row["title"]})
    return (f"📝 Saved as a review-only internal schedule proposal instead: "
            f"**{row['title']}** (nothing was put on your real calendar).")
