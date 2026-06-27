"""Durable verification of CHRONOS Smart Scheduling (chat_scheduling).

Free/busy is READ-FIRST and computed from the user's own events across all connected
calendars (reusing chat_calendar.gather_events_window → per-provider read gate + broker
+ audit); booking a found slot reuses the calendar confirm-before-write path
(chat_calendar.stage_create), which FAILS CLOSED behind the calendar_write flag +
CALENDAR_EXECUTION_ENABLED.

Parts:
  A) Detection — availability phrasing + (find/booking verb + duration, no clock time)
     route here; an explicit clock time, a plain calendar/inbox phrase, or a verb with
     no duration do NOT.
  B) Free-interval math — busy blocks are subtracted within working hours; all-day
     events don't block; a daypart narrows the window.
  C) Find-only (read) — with the per-provider read patched to canned events, the reply
     lists open ranges and excludes busy time; a freebusy trace is written.
  D) Book, gate CLOSED — a booking request finds the slot but staging FAILS CLOSED
     (no connector → write gate shut): governed write-blocked message, NO pending row.
  E) Book, gate OPEN (patched) — the earliest slot is STAGED in calendar_pending_actions
     (kind=create, explicit start_time + attendee) with a confirm card; no provider
     write happens (only a later "confirm" would). Disposable rows cleaned in finally.

    docker cp apps/cora-api/scripts/verify_chat_scheduling.py cora-api:/tmp/vcs.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vcs.py     # 0=PASS 1=FAIL
"""
import asyncio
import sys
import uuid
from datetime import datetime, timedelta

from app.clients import clients, init_clients
from app import chat_scheduling as cs
from app import chat_calendar as cc
from app import clock


async def main() -> int:
    await init_clients()
    pool = clients.db_pool
    fails = []
    uid = None
    sess = uuid.uuid4()
    tz = clock.current_tz()

    def expect(c, m):
        if not c:
            fails.append(m)

    # --- A) detection ---
    def det(msg):
        return cs.detect_scheduling_command(msg)

    expect(det("When am I free this week?") is not None, "A avail phrase → scheduling")
    fm = det("find me 30 minutes tomorrow afternoon")
    expect(fm is not None and fm[1]["duration_min"] == 30 and fm[1]["daypart"] == "afternoon"
           and fm[1]["book"] is False, "A find+duration+daypart (read-only)")
    bk = det("schedule 30 min with sam@example.com next tuesday")
    expect(bk is not None and bk[1]["book"] is True and bk[1]["attendees"] == ["sam@example.com"],
           "A booking verb + email → book intent + attendee")
    hr = det("book an hour thursday morning")
    expect(hr is not None and hr[1]["duration_min"] == 60 and hr[1]["book"] is True
           and hr[1]["daypart"] == "morning", "A 'an hour' → 60 min, morning, book")
    expect(det("do I have any free time on friday") is not None, "A 'free time' phrase")
    # negatives — must fall through (None)
    expect(det("schedule a meeting at 3pm tomorrow") is None, "A explicit clock time → NOT scheduling")
    expect(det("what's on my calendar today") is None, "A plain calendar list → NOT scheduling")
    expect(det("find emails from sam") is None, "A find w/o duration → NOT scheduling")
    expect(det("set up a meeting") is None, "A booking verb w/o duration → NOT scheduling (normal create)")
    expect(det("create an event called Launch") is None, "A create → NOT scheduling")

    # --- B) free-interval math (deterministic on a fixed weekday) ---
    sd, ed, _label = cs._resolve_window("find 30 min next monday", tz)  # sd = next Monday (weekday)

    def iso(d, h, mi=0):
        return datetime(d.year, d.month, d.day, h, mi, tzinfo=tz).isoformat()

    events = [
        {"title": "Busy AM", "start": iso(sd, 10), "end": iso(sd, 11), "all_day": False},
        {"title": "Busy PM", "start": iso(sd, 14), "end": iso(sd, 15), "all_day": False},
        {"title": "Holiday", "start": sd.isoformat(), "end": (sd + timedelta(days=1)).isoformat(),
         "all_day": True},
    ]
    ints = cs._free_intervals(events, start_date=sd, end_date=sd, duration_min=30, daypart=None, tz=tz)
    expect(len(ints) == 3, f"B three free intervals around two busy blocks (got {len(ints)})")
    if len(ints) == 3:
        expect(ints[0][0].hour == 9 and ints[0][1].hour == 10, "B first free 9:00–10:00 (before busy)")
        expect(ints[1][0].hour == 11 and ints[1][1].hour == 14, "B middle free 11:00–14:00 (between busy)")
        expect(ints[2][0].hour == 15 and ints[2][1].hour == 17, "B last free 15:00–17:00 (after busy)")
    starts = {s.hour for s, _ in ints}
    expect(10 not in starts and 14 not in starts, "B busy starts are not offered as free")
    aft = cs._free_intervals(events, start_date=sd, end_date=sd, duration_min=30,
                             daypart="afternoon", tz=tz)
    expect(all(s.hour >= 12 for s, _ in aft), "B daypart=afternoon narrows to >=12:00")

    orig_gather, orig_gate = cc.gather_events_window, cc._write_gate
    try:
        async with pool.acquire() as conn:
            uid = await conn.fetchval(
                "INSERT INTO users (email, password_hash, role) VALUES ($1,'x','admin') RETURNING id",
                f"verify-cs-{uuid.uuid4()}@example.invalid")
            wid = await conn.fetchval("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")

        async def fake_gather(*, user_id, workspace_uuid, session_uuid, time_min, time_max):
            return {"events": events, "providers_ok": ["google_calendar", "outlook_calendar"],
                    "skipped": []}
        cc.gather_events_window = fake_gather

        # --- C) find-only render ---
        msg_find = "find me 30 minutes next monday"
        h, t = await cs.handle_scheduling_command(
            message=msg_find, payload=det(msg_find)[1], session_uuid=sess, user_id=uid,
            workspace_uuid=wid)
        expect(h and t and "Open time" in t, "C find-only renders open ranges")
        expect(h and t and "9:00 AM" in t, "C earliest free range starts 9:00 AM")
        expect(h and t and "10:00 AM – 11:00 AM" not in t, "C busy block not shown as free")
        expect(h and t and "google" in t, "C provider header present")
        async with pool.acquire() as conn:
            fb = await conn.fetchval(
                "SELECT count(*) FROM runtime_traces WHERE user_id=$1 "
                "AND trace_type='chat_scheduling_freebusy_requested'", uid)
        expect(fb >= 1, "C freebusy trace written")

        # --- D) book with the write gate CLOSED (real fail-closed; no connector) ---
        msg_book = "schedule 30 min with sam@example.com next monday"
        h, t = await cs.handle_scheduling_command(
            message=msg_book, payload=det(msg_book)[1], session_uuid=sess, user_id=uid,
            workspace_uuid=wid)
        expect(h and t and "Earliest free 30-min slot" in t, "D slot found before gate check")
        expect(h and t and "writes" in t.lower() and "disabled" in t.lower(),
               "D booking FAILS CLOSED with governed write-blocked message")
        expect(not await cc.has_pending(sess), "D nothing staged when the write gate is shut")

        # --- E) book with the write gate OPEN (patched) → stages a pending create ---
        async def allow_gate(provider, user_id, action):
            return {"allowed": True, "supports": True, "capability_mismatch": False,
                    "flag_ok": True, "kill_switch_clear": True, "reason": "test-enabled"}
        cc._write_gate = allow_gate
        h, t = await cs.handle_scheduling_command(
            message=msg_book, payload=det(msg_book)[1], session_uuid=sess, user_id=uid,
            workspace_uuid=wid)
        expect(h and t and "Earliest free 30-min slot" in t and "confirm" in t.lower(),
               "E open gate → slot found + confirm card")
        # default-calendar is made explicit + redirectable (two calendars connected, none named)
        expect(h and t and "your default" in t and "confirm on" in t.lower(),
               "E booking names the default calendar and offers a redirect")
        pend = await cc._get_pending(sess)
        expect(pend is not None and pend["kind"] == "create", "E pending CREATE staged")
        if pend:
            f = pend["fields"] if isinstance(pend["fields"], dict) else {}
            expect(bool(f.get("start_time")) and f.get("attendees") == ["sam@example.com"],
                   "E staged fields carry the explicit slot start + attendee")
        # staging only — no provider write fired (no TRACE for create/confirmed)
        async with pool.acquire() as conn:
            wrote = await conn.fetchval(
                "SELECT count(*) FROM runtime_traces WHERE user_id=$1 "
                "AND trace_type IN ('chat_calendar_event_created','chat_calendar_confirmed')", uid)
            staged = await conn.fetchval(
                "SELECT count(*) FROM runtime_traces WHERE user_id=$1 "
                "AND trace_type='chat_scheduling_slot_staged'", uid)
        expect(wrote == 0, "E no real create/confirm happened (staging only)")
        expect(staged >= 1, "E slot-staged trace written")
    finally:
        cc.gather_events_window, cc._write_gate = orig_gather, orig_gate
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM calendar_pending_actions WHERE session_id=$1", sess)
            if uid is not None:
                await conn.execute("DELETE FROM runtime_traces WHERE user_id=$1", uid)
                await conn.execute("DELETE FROM calendar_access_events WHERE user_id=$1", uid)
                await conn.execute("DELETE FROM users WHERE id=$1", uid)

    if fails:
        print("RESULT: FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print("RESULT: PASS — detection routes availability/find-a-time (not explicit-time "
          "creates or plain calendar/inbox); free-interval math subtracts busy within "
          "working hours (all-day ignored, daypart narrows); find-only lists open ranges "
          "read-only with a trace; booking finds the slot but FAILS CLOSED when the write "
          "gate is shut (no pending); with the gate open it stages a confirm-before-write "
          "CREATE carrying the explicit slot + attendee (no provider write); rows cleaned")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
