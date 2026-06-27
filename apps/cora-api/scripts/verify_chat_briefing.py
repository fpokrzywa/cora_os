"""Durable verification of the Daily Briefing composite (chat_briefing).

The briefing is a READ-ONLY digest that composes three GOVERNED, fail-closed reads:
today's schedule (chat_calendar.gather_day_events), inbox highlights
(chat_inbox.gather_inbox_highlights), and a news headline rundown
(news_briefing.gather_briefing). It performs no writes and no sends.

Parts:
  A) Detection — day-digest phrasing routes here; plain calendar/inbox/email phrasing
     does NOT (so the single-domain handlers still own those).
  B) Composite render — with the three governed reads patched to canned data, the
     digest renders all three sections, source tags, and ordering; a generation trace
     is written (counts only).
  C) Fail-soft — with the REAL governed reads and a throwaway user that has NO
     connectors, the schedule + inbox sections degrade to clean "not available" notes
     (gated out per provider) and the briefing still renders. Disposable rows cleaned.

    docker cp apps/cora-api/scripts/verify_chat_briefing.py cora-api:/tmp/vcb.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vcb.py     # 0=PASS 1=FAIL
"""
import asyncio
import sys
import uuid

from app.clients import clients, init_clients
from app import schema as schema_state
from app import chat_briefing as cb
from app import chat_calendar as cc
from app import chat_inbox as ci
from app import news_briefing as nb


async def main() -> int:
    await init_clients()
    pool = clients.db_pool
    # Mirror the live app's startup pgvector detection (init_schema sets this) so the
    # real news read in part C selects the correct embedding column. Read-only.
    async with pool.acquire() as conn:
        schema_state.PGVECTOR_AVAILABLE = bool(
            await conn.fetchval("SELECT 1 FROM pg_extension WHERE extname='vector'"))
    fails = []
    uid = None
    sess = uuid.uuid4()

    def expect(c, m):
        if not c:
            fails.append(m)

    # --- A) detection ---
    for msg in ("Brief me on my day.", "Give me my daily briefing.",
                "What does my day look like?", "Catch me up on my day.",
                "morning brief please", "summarize my day", "start my day"):
        expect(cb.detect_briefing_command(msg), f"A detect briefing: {msg!r}")
    for msg in ("What's on my calendar today?", "Show my latest emails.",
                "Draft an email to Mark.", "Summarize this email thread.",
                "make outlook my default calendar", "hello there"):
        expect(not cb.detect_briefing_command(msg), f"A non-briefing must not detect: {msg!r}")

    orig_cal, orig_inb, orig_news = cc.gather_day_events, ci.gather_inbox_highlights, nb.gather_briefing
    try:
        async with pool.acquire() as conn:
            uid = await conn.fetchval(
                "INSERT INTO users (email, password_hash, role) VALUES ($1,'x','admin') RETURNING id",
                f"verify-cb-{uuid.uuid4()}@example.invalid")
            wid = await conn.fetchval("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")

        # --- B) composite render with the governed reads patched to canned data ---
        async def fake_cal(*, user_id, workspace_uuid, session_uuid):
            return {"events": [
                {"provider": "google_calendar", "title": "Standup",
                 "start": "2026-06-27T09:00:00+00:00", "end": "2026-06-27T09:30:00+00:00"},
                {"provider": "outlook_calendar", "title": "Client review",
                 "start": "2026-06-27T14:00:00+00:00", "end": "2026-06-27T15:00:00+00:00",
                 "location": "Room 4"}],
                "providers_ok": ["google_calendar", "outlook_calendar"], "skipped": [],
                "label": "today"}

        async def fake_inb(*, user_id, workspace_uuid, session_uuid, limit=5):
            return {"messages": [
                {"provider": "gmail", "from": "Alice <alice@gmail.com>", "subject": "Invoice #42"},
                {"provider": "outlook_mail", "from": "Bob <bob@outlook.com>", "subject": "Re: Roadmap"}],
                "providers_ok": ["gmail", "outlook_mail"], "skipped": []}

        async def fake_news(*, workspace_id, since_hours, max_articles, source_name):
            return {"articles": [
                {"title": "Markets rally on rate news", "source_name": "Reuters"},
                {"title": "New AI model released", "source_name": "TechCrunch"}],
                "aggregate": {"total_articles": 2, "feeds_represented": 2}}

        cc.gather_day_events, ci.gather_inbox_highlights, nb.gather_briefing = fake_cal, fake_inb, fake_news

        h, t = await cb.handle_briefing_command(
            message="brief me on my day", session_uuid=sess, user_id=uid, workspace_uuid=wid)
        expect(h and t, "B handled")
        for needle in ("Your Daily Briefing", "Today's schedule", "Inbox highlights",
                       "News rundown", "Standup", "Client review", "Room 4",
                       "Invoice #42", "Re: Roadmap", "Markets rally on rate news",
                       "New AI model released", "[google]", "[outlook]", "[gmail]",
                       "Reuters", "TechCrunch", "2 recent article"):
            expect(needle in (t or ""), f"B briefing missing {needle!r}")
        expect(t.index("Standup") < t.index("Client review"), "B schedule precedes inbox/news ordering")
        expect(t.index("Today's schedule") < t.index("Inbox highlights") < t.index("News rundown"),
               "B sections in order")

        async with pool.acquire() as conn:
            tr = await conn.fetchval(
                "SELECT count(*) FROM runtime_traces WHERE user_id=$1 "
                "AND trace_type='chat_daily_briefing_generated'", uid)
        expect(tr >= 1, "B briefing generation trace written")

        # --- C) fail-soft with the REAL governed reads + a user with NO connectors ---
        cc.gather_day_events, ci.gather_inbox_highlights, nb.gather_briefing = orig_cal, orig_inb, orig_news
        h, t = await cb.handle_briefing_command(
            message="give me my daily briefing", session_uuid=sess, user_id=uid, workspace_uuid=wid)
        expect(h and t, "C handled (no connectors)")
        expect("Calendar not available" in (t or ""), "C schedule fails soft (gated out)")
        expect("Inbox not available" in (t or ""), "C inbox fails soft (gated out)")
        expect("News rundown" in (t or ""), "C news section still present")
        expect("Your Daily Briefing" in (t or ""), "C briefing still renders end-to-end")
    finally:
        cc.gather_day_events, ci.gather_inbox_highlights, nb.gather_briefing = orig_cal, orig_inb, orig_news
        async with pool.acquire() as conn:
            if uid is not None:
                await conn.execute("DELETE FROM runtime_traces WHERE user_id=$1", uid)
                await conn.execute("DELETE FROM calendar_access_events WHERE user_id=$1", uid)
                await conn.execute("DELETE FROM inbox_access_events WHERE user_id=$1", uid)
                await conn.execute("DELETE FROM users WHERE id=$1", uid)

    if fails:
        print("RESULT: FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print("RESULT: PASS — briefing detection routes day-digest phrasing only; composite "
          "renders schedule + inbox + news with source tags + ordering + a counts-only "
          "trace; with no connectors the governed reads fail soft per-section and the "
          "briefing still renders; rows cleaned up")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
