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
from app import calendar_adapters
from app import chronos_tools
from app import clock
from app import feature_flags as ff
from app import oauth_flow
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


def _extract_event_query(m: str) -> Optional[str]:
    for kw in (" my ", " the ", " about ", " for ", " titled ", " called "):
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
    m = (message or "").lower().strip()
    if not m:
        return None
    has_anchor = any(n in m for n in ("calendar", "meeting", "event", "appointment", "agenda"))

    if any(p in m for p in ("cancel my", "cancel the", "delete the event", "delete my event",
                            "delete the meeting", "remove the event", "remove the meeting",
                            "remove my meeting", "clear my calendar")) and has_anchor:
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
    endpoint = settings.dgx_model_endpoint or None
    if not endpoint:
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
        async with httpx.AsyncClient(timeout=LLM_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{endpoint.rstrip('/')}/api/generate",
                json={"model": settings.dgx_model_name, "prompt": prompt, "stream": False})
            resp.raise_for_status()
            text = (resp.json() or {}).get("response", "")
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

async def _resolve_provider(message: str, user_id: uuid.UUID) -> str:
    m = (message or "").lower()
    if "outlook" in m or "microsoft" in m:
        return "outlook_calendar"
    if "google" in m or "gmail" in m:
        return "google_calendar"
    pool = clients.db_pool
    async with pool.acquire() as conn:
        row = await conn.fetchval(
            "SELECT provider_name FROM provider_oauth_connectors "
            "WHERE user_id=$1 AND provider_type='calendar' AND status='connected' "
            "ORDER BY created_at DESC LIMIT 1", user_id)
    return row or "google_calendar"


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
    kill_switch_clear = bool(settings.calendar_execution_enabled)
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


def _render_events(provider, events, label) -> str:
    if not events:
        return f"**{provider}** — no events for {label} (read-only)."
    lines = [f"**{provider} — {len(events)} event(s) for {label}** (read-only)"]
    for i, e in enumerate(events, 1):
        loc = f" · {_trunc(e.get('location'), 40)}" if e.get("location") else ""
        lines.append(f"{i}. **{_trunc(e.get('title'))}** · {_fmt_event_when(e)}{loc}")
    return "\n".join(lines)


def _fmt_when(fields) -> str:
    s, e = fields.get("start_time"), fields.get("end_time")
    if s and e:
        return f"{s} → {e}"
    return s or "time not specified"


def _confirm_card_create(provider, fields) -> str:
    att = fields.get("attendees") or []
    lines = [f"📅 **Ready to create on {provider}** — reply **confirm** to create "
             "(invites will be sent) or **cancel**:",
             f"- **{fields.get('title') or 'New event'}**",
             f"- When: {_fmt_when(fields)}"]
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
    scope_type: str, is_admin: bool,
) -> tuple[bool, Optional[str]]:
    """Single entry point. A fresh `command` always wins; a bare `confirmation`
    resolves a staged pending action. Returns (False, None) to fall through when the
    message isn't actually ours (e.g. a 'yes' with nothing pending)."""
    if command is None:
        if confirmation is None:
            return False, None
        pending = await _get_pending(session_uuid)
        if pending is None:
            return False, None
        return await _handle_confirmation(confirmation, pending, session_uuid=session_uuid,
                                          user_id=user_id, workspace_uuid=workspace_uuid)

    kind, payload = command
    provider = await _resolve_provider(message, user_id)
    await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_REQUESTED,
                 result={"provider": provider, "kind": kind})
    if kind == "list":
        return await _handle_read(provider, message, session_uuid=session_uuid,
                                  user_id=user_id, workspace_uuid=workspace_uuid)
    return await _prepare_write(kind, provider, message, payload, session_uuid=session_uuid,
                                user_id=user_id, workspace_uuid=workspace_uuid)


async def _handle_read(provider, message, *, session_uuid, user_id, workspace_uuid):
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
    return True, _render_events(provider, events, label)


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
        await _set_pending(session_uuid, user_id=user_id, workspace_id=workspace_uuid,
                           kind="create", provider=provider, fields=fields)
        await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_CONFIRM_PENDING,
                     result={"provider": provider, "kind": "create",
                             "has_start": bool(fields.get("start_time"))})
        return True, _confirm_card_create(provider, fields)

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
        # No confident single target — do NOT guess a write target. Ask instead.
        await _audit(user_id, workspace_uuid, provider, kind, False, f"target {res['status']}")
        await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_WRITE_DENIED,
                     status="blocked", result={"provider": provider, "kind": kind,
                                               "reason": f"target_{res['status']}"})
        return True, _target_help_msg(provider, kind, res["status"],
                                      res.get("candidates", []), query)
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
            ev = await adapter.create_event(access_token=token, fields=fields)
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
