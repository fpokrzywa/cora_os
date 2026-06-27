"""CHRONOS Smart Scheduling — free/busy + find-a-time (read-first, book-on-confirm).

Answers availability questions and proposes meeting times by reading the user's OWN
events across ALL connected calendars and computing the open gaps within working
hours:
  - "when am I free this week?"  /  "find me 30 minutes tomorrow afternoon"
        → READ-ONLY: lists open slots ≥ the requested duration (default 30 min),
          weekdays, 9:00–17:00 local (morning/afternoon/evening narrows it).
  - "schedule 30 min with sam@x next tuesday" / "book an hour thursday morning"
        → finds the earliest fitting slot, then STAGES it through the existing
          calendar confirm-before-write path (`chat_calendar.stage_create`): a later
          "confirm" books it (invites sent if attendees were named), "cancel" drops it.

Availability is computed from the same GOVERNED, fail-closed per-provider calendar
reads as the rest of CHRONOS (`chat_calendar.gather_events_window` → per-provider read
gate + token broker + audit) — so free/busy works read-only even when calendar WRITES
are disabled, and booking still requires the `calendar_write` flag + the dedicated
CALENDAR_EXECUTION_ENABLED switch. Free/busy is derived from the user's own events
(no new OAuth scope); true multi-attendee availability (Graph getSchedule / Google
freeBusy) is a possible later enhancement. Audited via runtime traces (counts/window
only — no event content).
"""

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from app import chat_calendar as cc
from app import clock
from app import provider_defaults
from app.config import settings
from app.runtime_traces import write_trace

logger = logging.getLogger(__name__)

CHRONOS = "CHRONOS"

WORK_START = 9          # local working-day start hour
WORK_END = 17           # local working-day end hour
EVENING_END = 20        # upper bound when the user asks for an evening slot
DEFAULT_DURATION_MIN = 30
SLOT_ROUND_MIN = 15     # round proposed starts up to a tidy :00/:15/:30/:45
MAX_SLOTS = 6
DEFAULT_LOOKAHEAD_DAYS = 7   # "find me time" with no day reference → next 7 days

_DUR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)\b")
_CLOCK_RE = re.compile(r"\b\d{1,2}:\d{2}\b|\b\d{1,2}\s*(?:am|pm)\b")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_WORDED_HOURS_RE = re.compile(r"\b(an|a|one|two|three|couple)\s+(hours?|hrs?)\b")
_WORD_NUM = {"an": 1, "a": 1, "one": 1, "two": 2, "three": 3, "couple": 2}

# Booking verbs → after finding the slot, stage it for confirmation.
_BOOK_VERBS = ("schedule", "book", "set up", "set-up", "arrange", "block off",
               "block out", "carve out", "set aside")
# Find verbs (a superset incl. booking) — combined with a stated duration they signal
# a find-a-time request (vs a normal create, which names an explicit clock time).
_FIND_VERBS = _BOOK_VERBS + ("find", "fit", "squeeze", "block", "carve", "free up")

# Explicit availability phrasing — scheduling regardless of a stated duration.
_AVAIL_PHRASES = (
    "when am i free", "when am i available", "when i'm free", "when im free",
    "when will i be free", "what's my availability", "whats my availability",
    "my availability", "am i free", "do i have time",
    "do i have any time", "any free time", "free time", "free slot", "free slots",
    "open slot", "open slots", "open time", "any openings", "an opening",
    "available time", "spare time", "block of time", "find time", "find me time",
    "find a time", "find some time", "when can i meet", "when can we meet",
    "when could we meet", "what time am i free",
)


def _parse_duration(m: str) -> Optional[int]:
    if "half an hour" in m or "half hour" in m:
        return 30
    if "hour and a half" in m:
        return 90
    mt = _DUR_RE.search(m)
    if mt:
        val, unit = float(mt.group(1)), mt.group(2)
        return int(val * 60) if unit[0] == "h" else int(val)
    wm = _WORDED_HOURS_RE.search(m)
    if wm:
        return int(_WORD_NUM.get(wm.group(1), 1) * 60)
    return None


def _parse_daypart(m: str) -> Optional[str]:
    if "morning" in m:
        return "morning"
    if "afternoon" in m:
        return "afternoon"
    if "evening" in m or "tonight" in m:
        return "evening"
    return None


def detect_scheduling_command(message: str) -> Optional[tuple[str, dict]]:
    """Conservative free/busy detection. Fires on explicit availability phrasing, OR a
    find/booking verb + a stated duration WITHOUT an explicit clock time (an explicit
    time means a normal create, handled by chat_calendar). Returns ('find', payload)."""
    m = provider_defaults.strip_provider_adjectives((message or "").lower().strip())
    if not m:
        return None
    dur = _parse_duration(m)
    has_clock = bool(_CLOCK_RE.search(m))
    has_find_verb = any(v in m for v in _FIND_VERBS)
    has_avail = any(p in m for p in _AVAIL_PHRASES)
    if not (has_avail or (has_find_verb and dur is not None and not has_clock)):
        return None
    attendees = _EMAIL_RE.findall(message or "")
    book = any(v in m for v in _BOOK_VERBS)
    title = (f"Meeting with {attendees[0].split('@')[0].replace('.', ' ').title()}"
             if attendees else "Focus time")
    return ("find", {"duration_min": dur or DEFAULT_DURATION_MIN, "daypart": _parse_daypart(m),
                     "attendees": attendees, "book": book, "title": title})


# --------------------------------------------------------------------------- #
# Free/busy computation
# --------------------------------------------------------------------------- #

def _round_up(dt: datetime) -> datetime:
    dt = dt.replace(second=0, microsecond=0)
    rem = dt.minute % SLOT_ROUND_MIN
    return dt + timedelta(minutes=SLOT_ROUND_MIN - rem) if rem else dt


def _busy_intervals(events: list, tz) -> list:
    """Timed (non all-day) events → sorted (start, end) local-aware busy intervals."""
    out = []
    for e in events:
        if e.get("all_day"):
            continue
        s, en = cc._local_dt(e.get("start")), cc._local_dt(e.get("end"))
        if s and en and en > s:
            out.append((s, en))
    out.sort()
    return out


def _work_bounds(d, daypart: Optional[str], tz) -> tuple:
    def at(h):
        return datetime(d.year, d.month, d.day, h, tzinfo=tz)
    if daypart == "morning":
        return at(WORK_START), at(12)
    if daypart == "afternoon":
        return at(12), at(WORK_END)
    if daypart == "evening":
        return at(WORK_END), at(EVENING_END)
    return at(WORK_START), at(WORK_END)


def _subtract(ws: datetime, we: datetime, busy: list) -> list:
    """Free sub-intervals of [ws, we] after removing every busy interval."""
    free = [(ws, we)]
    for bs, be in busy:
        nxt = []
        for fs, fe in free:
            if be <= fs or bs >= fe:
                nxt.append((fs, fe))
                continue
            if bs > fs:
                nxt.append((fs, min(bs, fe)))
            if be < fe:
                nxt.append((max(be, fs), fe))
        free = nxt
    return [(s, e) for s, e in free if e > s]


def _free_intervals(events, *, start_date, end_date, duration_min, daypart, tz) -> list:
    """Open intervals ≥ duration_min within working hours, weekdays only, across the
    date range; starts rounded up; today clipped to now. Capped at MAX_SLOTS."""
    busy = _busy_intervals(events, tz)
    now = datetime.now(tz)
    need = timedelta(minutes=duration_min)
    out, d = [], start_date
    while d <= end_date and len(out) < MAX_SLOTS:
        if d.weekday() < 5:  # Mon–Fri
            ws, we = _work_bounds(d, daypart, tz)
            if d == now.date():
                ws = max(ws, _round_up(now + timedelta(minutes=1)))
            if ws < we:
                for fs, fe in _subtract(ws, we, busy):
                    fs2 = _round_up(fs)
                    if fe - fs2 >= need:
                        out.append((fs2, fe))
                        if len(out) >= MAX_SLOTS:
                            break
        d += timedelta(days=1)
    return out


# --------------------------------------------------------------------------- #
# Window resolution (reuses chat_calendar's NL window parser)
# --------------------------------------------------------------------------- #

def _resolve_window(message: str, tz):
    """(start_date, end_date_inclusive, label) in local tz. Reuses
    chat_calendar.resolve_read_window for the NL parsing; with no day reference the
    read default (+14d) is tightened to the next 7 days for a find-a-time request."""
    time_min, time_max, label = cc.resolve_read_window(message)
    start_d = datetime.fromisoformat(time_min).astimezone(tz).date()
    end_d = (datetime.fromisoformat(time_max).astimezone(tz) - timedelta(seconds=1)).date()
    if not cc._has_day_ref((message or "").lower()):
        end_d = min(end_d, start_d + timedelta(days=DEFAULT_LOOKAHEAD_DAYS - 1))
        label = "in the next 7 days"
    return start_d, end_d, label


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def _fmt_slot(dt: datetime) -> str:
    return dt.strftime("%a %b %-d, %-I:%M %p")


def _win_label(daypart: Optional[str]) -> str:
    if daypart == "morning":
        return f"{WORK_START}:00–12:00"
    if daypart == "afternoon":
        return f"12:00–{WORK_END}:00"
    if daypart == "evening":
        return f"{WORK_END}:00–{EVENING_END}:00"
    return f"{WORK_START}:00–{WORK_END}:00"


def _skip_note(read) -> str:
    if not read["skipped"]:
        return ""
    return ("\n\n_Skipped: " + "; ".join(
        f"{cc._short(s['provider'])} ({s['reason']})" for s in read["skipped"]) + "._")


def _render_intervals(intervals, duration_min, label, daypart, read) -> str:
    head = " + ".join(cc._short(p) for p in read["providers_ok"])
    dp = f", {daypart}" if daypart else ""
    lines = [f"📅 **Open time {label}** for a {duration_min}-min meeting "
             f"(across {head}, weekdays {_win_label(daypart)}{dp}):"]
    for s, e in intervals:
        lines.append(f"- **{_fmt_slot(s)} – {e.strftime('%-I:%M %p')}**")
    lines.append("\n_To book one, say e.g. “book "
                 f"{duration_min} min {label}” (add an email to invite someone) and "
                 "I’ll stage it for your confirmation._")
    return "\n".join(lines) + _skip_note(read)


def _no_calendars_msg(read) -> str:
    notes = "; ".join(f"{cc._short(s['provider'])}: {s['reason']}" for s in read["skipped"]) \
        or "no calendars connected"
    return (f"🔒 I couldn't read your calendars to check availability — {notes}. "
            "No calendar data was accessed.")


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #

async def _trace(session_id, user_id, workspace_id, trace_type, result):
    await write_trace(session_id=session_id, user_id=user_id, trace_type=trace_type,
                      status="ok", selected_agent=CHRONOS, tool_name="chat_scheduling",
                      tool_result=result or {}, workspace_id=workspace_id)


async def handle_scheduling_command(
    *, message: str, payload: dict, session_uuid: uuid.UUID, user_id: uuid.UUID,
    workspace_uuid: Optional[uuid.UUID],
) -> tuple[bool, Optional[str]]:
    duration = int(payload["duration_min"])
    daypart = payload.get("daypart")
    tz = clock.current_tz()
    start_d, end_d, label = _resolve_window(message, tz)
    start_local = datetime(start_d.year, start_d.month, start_d.day, tzinfo=tz)
    end_local = datetime(end_d.year, end_d.month, end_d.day, tzinfo=tz) + timedelta(days=1)
    time_min = start_local.astimezone(timezone.utc).isoformat()
    time_max = end_local.astimezone(timezone.utc).isoformat()

    read = await cc.gather_events_window(user_id=user_id, workspace_uuid=workspace_uuid,
                                         session_uuid=session_uuid, time_min=time_min, time_max=time_max)
    await _trace(session_uuid, user_id, workspace_uuid, "chat_scheduling_freebusy_requested",
                 {"window": label, "duration_min": duration, "daypart": daypart,
                  "book": payload.get("book"), "providers": read["providers_ok"]})
    if not read["providers_ok"]:
        return True, _no_calendars_msg(read)

    intervals = _free_intervals(read["events"], start_date=start_d, end_date=end_d,
                                duration_min=duration, daypart=daypart, tz=tz)
    if not intervals:
        dp = f" in the {daypart}" if daypart else ""
        return True, (f"📅 I couldn't find a free {duration}-minute slot {label}{dp} within "
                      f"your working hours ({_win_label(daypart)}, weekdays). Try a wider "
                      "window or a shorter duration." + _skip_note(read))

    if payload.get("book"):
        return await _book_first(intervals[0], duration, payload, message, label, read,
                                 session_uuid=session_uuid, user_id=user_id, workspace_uuid=workspace_uuid)
    await _trace(session_uuid, user_id, workspace_uuid, "chat_scheduling_slots_listed",
                 {"window": label, "count": len(intervals), "duration_min": duration})
    return True, _render_intervals(intervals, duration, label, daypart, read)


async def _book_first(interval, duration, payload, message, label, read, *, session_uuid,
                      user_id, workspace_uuid) -> tuple[bool, Optional[str]]:
    """Stage the earliest fitting slot through the calendar confirm-before-write path."""
    start, _free_end = interval
    end = start + timedelta(minutes=duration)
    provider = await cc._resolve_provider(message, user_id)
    fields = {
        "title": payload.get("title") or "Meeting",
        "description": (message or "").strip()[:1000],
        "start_time": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "end_time": end.strftime("%Y-%m-%dT%H:%M:%S"),
        "timezone": settings.cora_timezone or "UTC",
        "attendees": payload.get("attendees") or None,
        "location": None,
    }
    await _trace(session_uuid, user_id, workspace_uuid, "chat_scheduling_slot_staged",
                 {"provider": provider, "start": fields["start_time"], "duration_min": duration,
                  "attendees": len(payload.get("attendees") or [])})
    handled, text = await cc.stage_create(provider, fields, session_uuid=session_uuid,
                                          user_id=user_id, workspace_uuid=workspace_uuid)
    prefix = (f"📅 Earliest free {duration}-min slot {label}: **{_fmt_slot(start)} – "
              f"{end.strftime('%-I:%M %p')}**.\n\n")
    # When the calendar wasn't named and more than one is connected, make the default
    # target explicit and the redirect discoverable (you can switch at confirm time).
    hint = ""
    others = [p for p in read["providers_ok"] if p != provider]
    if "Ready to create" in (text or "") and cc._named_provider(message) is None and others:
        hint = (f"\n\n_Booking on **{cc._short(provider)}** (your default). To use "
                f"{' or '.join(cc._short(o) for o in others)} instead, reply "
                f"**“confirm on {cc._short(others[0])}”**._")
    return handled, prefix + (text or "") + hint
