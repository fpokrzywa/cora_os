"""Daily Briefing (composite) — CHRONOS schedule + SIGNAL inbox + PULSE news.

"Brief me on my day" → one read-only digest combining:
  1. today's schedule across ALL connected calendars,
  2. recent inbox highlights across ALL connected mailboxes,
  3. a short headline rundown from already-ingested news.

Purely a READ/compose view. Each section reuses the existing GOVERNED, fail-closed
cross-provider reads (`chat_calendar.gather_day_events` / `chat_inbox.
gather_inbox_highlights`) and the `news_briefing` DB read, so a gated-out provider
degrades that one section to a clean note — the briefing still renders. No writes,
no sends, no email-send unlock, and no DGX dependency (headlines come straight from
already-ingested news; ask PULSE separately for an analytical summary). Generation
is audited via a runtime trace (counts only — no message/event content).
"""

import logging
import uuid
from typing import Optional

from app import chat_calendar
from app import chat_inbox
from app import news_briefing
from app.runtime_traces import write_trace

logger = logging.getLogger(__name__)

PERSONA = "Cora"
TRACE_BRIEFING = "chat_daily_briefing_generated"

NEWS_SINCE_HOURS = 24
NEWS_MAX_ARTICLES = 5

# Explicit, day-digest phrasing only, so a plain "what's on my calendar" still routes
# to the single-domain calendar handler and isn't swallowed by the composite.
_TRIGGERS = (
    "brief me on my day", "brief me for the day", "brief me on the day",
    "brief me on today", "brief my day", "brief me", "daily briefing", "daily brief",
    "morning briefing", "morning brief", "my briefing", "my daily briefing",
    "give me my briefing", "give me a briefing", "give me the rundown",
    "rundown of my day", "rundown on my day", "summarize my day", "summary of my day",
    "how does my day look", "how's my day look", "hows my day look",
    "what does my day look like", "what's my day look like", "whats my day look like",
    "what's my day looking like", "my day at a glance", "start my day",
    "catch me up on my day", "overview of my day", "plan for my day",
)


def detect_briefing_command(message: str) -> bool:
    m = (message or "").lower().strip()
    if not m:
        return False
    return any(t in m for t in _TRIGGERS)


def _skip_reasons(skipped) -> str:
    if not skipped:
        return "no providers connected"
    return "; ".join(f"{chat_calendar._short(s['provider'])}: {s['reason']}" for s in skipped)


def _render_schedule(s) -> list:
    out = ["## 📅 Today's schedule"]
    oks, evs = s["providers_ok"], s["events"]
    if not oks:
        out.append(f"_Calendar not available — {_skip_reasons(s['skipped'])}._")
        return out
    head = " + ".join(chat_calendar._short(p) for p in oks)
    if not evs:
        out.append(f"Nothing on your calendar today ({head}). 🎉")
    else:
        for e in evs:
            tag = f"[{chat_calendar._short(e.get('provider'))}] " if e.get("provider") else ""
            loc = f" · {chat_calendar._trunc(e.get('location'), 40)}" if e.get("location") else ""
            out.append(f"- {tag}**{chat_calendar._trunc(e.get('title'))}** · "
                       f"{chat_calendar._fmt_event_when(e)}{loc}")
    if s["skipped"]:
        out.append(f"_Skipped: {_skip_reasons(s['skipped'])}._")
    return out


def _render_inbox(ib) -> list:
    out = ["## 📨 Inbox highlights"]
    oks, msgs = ib["providers_ok"], ib["messages"]
    if not oks:
        out.append(f"_Inbox not available — {_skip_reasons(ib['skipped'])}._")
        return out
    head = " + ".join(chat_calendar._short(p) for p in oks)
    if not msgs:
        out.append(f"No recent messages ({head}).")
    else:
        for mm in msgs:
            out.append(f"- [{chat_calendar._short(mm.get('provider'))}] "
                       f"**{chat_calendar._trunc(mm.get('subject'))}** · from {mm.get('from', '—')}")
    if ib["skipped"]:
        out.append(f"_Skipped: {_skip_reasons(ib['skipped'])}._")
    return out


def _render_news(n) -> list:
    out = ["## 📰 News rundown"]
    arts, agg = n["articles"], n["aggregate"]
    if not arts:
        out.append(f"_No news ingested in the last {NEWS_SINCE_HOURS}h._")
        return out
    out.append(f"_{agg['total_articles']} recent article(s) across "
               f"{agg['feeds_represented']} feed(s)._")
    for a in arts:
        src = f" · {a['source_name']}" if a.get("source_name") else ""
        out.append(f"- **{chat_calendar._trunc(a.get('title'))}**{src}")
    out.append("\n_Headlines from already-ingested news — ask PULSE for a full "
               "analytical summary._")
    return out


async def handle_briefing_command(
    *, message: str, session_uuid: uuid.UUID, user_id: uuid.UUID,
    workspace_uuid: Optional[uuid.UUID],
) -> tuple[bool, Optional[str]]:
    """Compose the read-only daily digest. Each section fails soft independently;
    the briefing always renders. Returns (True, text)."""
    schedule = await chat_calendar.gather_day_events(
        user_id=user_id, workspace_uuid=workspace_uuid, session_uuid=session_uuid)
    inbox = await chat_inbox.gather_inbox_highlights(
        user_id=user_id, workspace_uuid=workspace_uuid, session_uuid=session_uuid)
    news = await news_briefing.gather_briefing(
        workspace_id=workspace_uuid, since_hours=NEWS_SINCE_HOURS,
        max_articles=NEWS_MAX_ARTICLES, source_name=None)

    lines = ["# ☀️ Your Daily Briefing", ""]
    lines += _render_schedule(schedule)
    lines.append("")
    lines += _render_inbox(inbox)
    lines.append("")
    lines += _render_news(news)
    text = "\n".join(lines)

    await write_trace(
        session_id=session_uuid, user_id=user_id, trace_type=TRACE_BRIEFING,
        status="ok", selected_agent=PERSONA, tool_name="chat_briefing",
        tool_result={"calendars": schedule["providers_ok"], "events": len(schedule["events"]),
                     "mailboxes": inbox["providers_ok"], "messages": len(inbox["messages"]),
                     "news_articles": news["aggregate"]["total_articles"]},
        workspace_id=workspace_uuid)
    return True, text
