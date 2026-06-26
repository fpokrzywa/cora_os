"""Durable end-to-end verification of the Chat-Native Calendar Assistant
(CHRONOS Calendar CRUD v1.0 + confirm-before-write).

Under a throwaway user with a connected google_calendar (calendar.events scope),
with field extraction stubbed (no DGX dependency):
  A) PRODUCTION STATE — fail-closed: list/create/update/delete all denied (flags
     disabled + kill switch off); audited; NO provider API call; a blocked CREATE
     falls back to a review-only internal schedule proposal; no token exposed.
  B) KILL SWITCH IS THE MASTER GATE — with calendar_read + calendar_write flags
     enabled the READ gate opens but the WRITE gate STILL denies (kill switch off).
  C) read gate-pass + provider rejection — graceful error + provider-failed trace.
  D) read gate-pass + mocked adapter returns events — list renders event metadata.
  E) write gate-pass (mocked) — CONFIRM-BEFORE-WRITE: a create/update/delete first
     STAGES a pending action + returns a confirmation card (no provider write yet);
     replying "confirm" fires the real adapter write (audited + traced); replying
     "cancel" clears the staged action and writes nothing.

Asserts flags seeded fail-closed, capability alignment, detection (incl. confirm/
cancel), pending lifecycle, audit events, traces, and no token leak. Disposable
rows cleaned in finally. Run:

    docker cp apps/cora-api/scripts/verify_chat_calendar.py cora-api:/tmp/vcc.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vcc.py     # 0=PASS 1=FAIL
"""
import asyncio
import sys
import uuid
from datetime import datetime, timezone

from app.clients import clients, init_clients
from app import chat_calendar as cc
from app import calendar_adapters
from app import feature_flags as ff
from app.crypto import encrypt_secret

FAKE_ACCESS = "FAKE-CAL-ACCESS-never-leak"
FAKE_CAL = "course@group.calendar.google.com"  # a SECONDARY calendar, not primary
FAKE_EVENTS = [{"id": "ev1", "title": "Team sync", "start": "2026-06-26T15:00:00Z",
                "end": "2026-06-26T15:30:00Z", "location": "Room 4", "calendar_id": FAKE_CAL,
                "attendees": ["a@example.com"], "link": "https://cal/ev1"}]
STUB_FIELDS = {"title": "Team meeting", "description": "team meeting",
               "start_time": "2026-06-30T15:00:00", "end_time": "2026-06-30T16:00:00",
               "timezone": "UTC", "attendees": ["sam@example.com"], "location": "Room 4"}
CAL_SCOPE = "https://www.googleapis.com/auth/calendar.events"


async def main() -> int:
    await init_clients()
    pool = clients.db_pool
    fails = []
    uid = None
    wid = None
    sess = uuid.uuid4()
    saved_flags = []

    def expect(c, m):
        if not c:
            fails.append(m)

    async def cal(msg):
        cmd = cc.detect_calendar_command(msg)
        conf = cc.detect_confirmation(msg)
        active = cmd is not None or (conf is not None and await cc.has_pending(sess))
        if not active:
            return None, None
        return await cc.handle_calendar_turn(
            message=msg, command=cmd, confirmation=conf, session_uuid=sess,
            user_id=uid, workspace_uuid=wid, scope_type="user", is_admin=True)

    # detection
    expect(cc.detect_calendar_command("What's on my calendar today?")[0] == "list", "detect list")
    expect(cc.detect_calendar_command("Schedule a meeting with the team tomorrow")[0] == "create", "detect create")
    expect(cc.detect_calendar_command("Reschedule my 1:1 meeting")[0] == "update", "detect update")
    expect(cc.detect_calendar_command("Cancel my standup meeting")[0] == "delete", "detect delete")
    expect(cc.detect_calendar_command("Plan my week") is None, "planning chatter must NOT be a calendar cmd")
    expect(cc.detect_calendar_command("Draft an email to Mark") is None, "email must NOT be a calendar cmd")
    expect(cc.detect_confirmation("confirm") == "confirm" and cc.detect_confirmation("yes") == "confirm", "detect confirm")
    expect(cc.detect_confirmation("cancel") == "cancel" and cc.detect_confirmation("no") == "cancel", "detect cancel")
    expect(cc.detect_confirmation("now show me the agenda") is None, "'now…' must NOT read as 'no'")

    # read-window resolution: past-aware day/week windows (the old code hid the past)
    now = datetime.now(timezone.utc)
    y_min, y_max, y_label = cc.resolve_read_window("what was on my calendar yesterday")
    expect(y_label == "yesterday" and datetime.fromisoformat(y_min) < now,
           "yesterday resolves to a PAST window")
    expect(y_min < y_max, "window min < max")
    expect(cc.resolve_read_window("anything on my calendar tuesday?")[2] == "Tuesday",
           "named weekday resolves to that day")
    expect(cc.resolve_read_window("what's on my calendar")[2] == "the next 2 weeks",
           "no day reference → default near-term window")
    deduped = cc._dedupe_series([
        {"id": "a", "series_id": "S1"}, {"id": "b", "series_id": "S1"},
        {"id": "c", "series_id": None}, {"id": "d", "series_id": None}])
    expect([e["id"] for e in deduped] == ["a", "c", "d"],
           "recurring series collapses to first; non-recurring kept")

    # target-resolution SAFETY: filler-tolerant token match + NO arbitrary fallback
    expect(cc._query_tokens("cancel my Cora Write Test meeting") == ["cora", "write", "test"],
           "query tokens strip filler nouns")
    _evs = [{"id": "a", "title": "Cora Write Test (safe to delete)", "calendar_id": "primary"},
            {"id": "b", "title": "Office Hours Session 1", "calendar_id": "c@x"}]
    expect([e["id"] for e in cc._match_events("cora write test meeting", _evs)] == ["a"],
           "token match finds the right event despite trailing 'meeting'")
    expect(cc._match_events("dentist appointment", _evs) == [],
           "no-match returns empty (never an arbitrary fallback)")

    class _FakeAdapter:
        def __init__(self, evs): self._evs = evs
        async def list_events(self, *, access_token=None, time_min=None, time_max=None, limit=10):
            return list(self._evs)
    r_ok = await cc._resolve_target(_FakeAdapter(_evs), "tok", "cora write test")
    expect(r_ok["status"] == "ok" and r_ok["event"]["id"] == "a", "resolve single match → ok")
    r_nm = await cc._resolve_target(_FakeAdapter(_evs), "tok", "dentist")
    expect(r_nm["status"] == "no_match", "resolve mismatch → no_match (no dangerous fallback)")
    r_amb = await cc._resolve_target(_FakeAdapter(_evs), "tok", None)
    expect(r_amb["status"] == "ambiguous", "resolve no-query + multiple events → ambiguous (no guess)")
    r_one = await cc._resolve_target(_FakeAdapter([_evs[0]]), "tok", None)
    expect(r_one["status"] == "ok", "resolve no-query + single event → ok")

    # Snapshot the LIVE flag state (the operator may have enabled calendar_read/
    # write for real use), then force fail-closed for the test. Restored in finally
    # so this run never disrupts the operator's configuration.
    async with pool.acquire() as conn:
        saved_flags.extend(dict(r) for r in await conn.fetch(
            "SELECT id, enabled, dry_run_only FROM provider_execution_feature_flags "
            "WHERE action_type IN ('calendar_read','calendar_write')"))
        await conn.execute(
            "UPDATE provider_execution_feature_flags SET enabled=FALSE, dry_run_only=TRUE "
            "WHERE action_type IN ('calendar_read','calendar_write')")
    for p in ("google_calendar", "microsoft_calendar"):
        for action in ("calendar_read", "calendar_write"):
            flag = await ff.get_flag(p, action)
            expect(flag is not None, f"{p}/{action} flag exists")

    # capability alignment
    async with pool.acquire() as conn:
        caps = {r["provider_name"]: r for r in await conn.fetch(
            "SELECT provider_name, supports_calendar_read, supports_calendar_create, "
            "supports_calendar_update, supports_calendar_delete FROM external_provider_connectors "
            "WHERE provider_name IN ('google_calendar','microsoft_calendar')")}
    for p in ("google_calendar", "microsoft_calendar"):
        for col in ("supports_calendar_read", "supports_calendar_create",
                    "supports_calendar_update", "supports_calendar_delete"):
            expect(caps[p][col] is True, f"{p}.{col} must be TRUE")

    orig_read_gate = cc._read_gate
    orig_write_gate = cc._write_gate
    orig_token = cc._get_access_token
    orig_extract = cc.extract_event_fields
    adapter = calendar_adapters.resolve_calendar_adapter("google_calendar")

    async def _stub_extract(message, kind):
        return dict(STUB_FIELDS)
    cc.extract_event_fields = _stub_extract  # no DGX dependency

    try:
        async with pool.acquire() as conn:
            uid = await conn.fetchval(
                "INSERT INTO users (email, password_hash, role) VALUES ($1,'x','admin') RETURNING id",
                f"verify-cc-{uuid.uuid4()}@example.invalid")
            wid = await conn.fetchval("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")
            await conn.execute(
                "INSERT INTO provider_oauth_connectors (user_id, workspace_id, provider_name, "
                "provider_type, status, scopes, access_token_encrypted, refresh_token_encrypted, "
                "token_expires_at, metadata) VALUES ($1,$2,'google_calendar','calendar','connected',$3,$4,$5,"
                "NOW()+interval '1 hour','{}'::jsonb)",
                uid, wid, [CAL_SCOPE], encrypt_secret(FAKE_ACCESS), encrypt_secret("r"))

        responses = []

        # --- A) production fail-closed ---
        for msg in ("What's on my calendar today?", "Schedule a meeting with the team tomorrow",
                    "Reschedule my 1:1 meeting", "Cancel my standup meeting"):
            h, t = await cal(msg)
            responses.append(t)
            expect(h and t and "disabled" in t.lower(), f"A fail-closed for {msg!r}")
        async with pool.acquire() as conn:
            denied = await conn.fetchval(
                "SELECT count(*) FROM calendar_access_events WHERE user_id=$1 AND allowed=false", uid)
            proposals = await conn.fetchval(
                "SELECT count(*) FROM schedule_proposals WHERE created_by=$1", uid)
        expect(denied >= 4, f"A denial audit rows={denied} (want >=4)")
        expect(proposals == 1, f"A blocked CREATE must create exactly one fallback proposal (got {proposals})")

        # --- B) the calendar execution switch is the master write gate ---
        # Force CALENDAR_EXECUTION_ENABLED off for this assertion regardless of the
        # operator's real .env setting, so the invariant "flags on + calendar switch
        # off → write still denied" is tested deterministically. (The global
        # external_execution kill switch is owned by email/integration governance and
        # is deliberately NOT what gates calendar writes.)
        orig_kill = cc.settings.calendar_execution_enabled
        cc.settings.calendar_execution_enabled = False
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE provider_execution_feature_flags SET enabled=TRUE, dry_run_only=FALSE "
                "WHERE provider_name='google_calendar' AND action_type IN ('calendar_read','calendar_write')")
        try:
            rg = await cc._read_gate("google_calendar", uid)
            expect(rg["allowed"] is True, "B read gate opens once calendar_read flag enabled")
            wg = await cc._write_gate("google_calendar", uid, "create")
            expect(wg["allowed"] is False and wg["kill_switch_clear"] is False,
                   "B write gate STILL denied — calendar execution switch is the master gate")
        finally:
            cc.settings.calendar_execution_enabled = orig_kill
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE provider_execution_feature_flags SET enabled=FALSE, dry_run_only=TRUE "
                    "WHERE provider_name='google_calendar' AND action_type IN ('calendar_read','calendar_write')")

        # --- C) read gate-pass + simulated provider rejection ---
        async def _allow_read(provider, user_id):
            return {"allowed": True, "supports_read": True, "capability_mismatch": False,
                    "flag_ok": True, "reason": "test-enabled"}
        cc._read_gate = _allow_read

        async def _reject_http(url, *, token, params=None):
            raise calendar_adapters.CalendarError("provider read rejected (HTTP 401)")
        orig_http = calendar_adapters._http_get_json
        calendar_adapters._http_get_json = _reject_http
        try:
            h, t = await cal("What's on my calendar today?")
            responses.append(t)
            expect(h and t and "failed" in t.lower() and "nothing was changed" in t.lower(),
                   "C provider rejection handled gracefully")
            async with pool.acquire() as conn:
                pf = await conn.fetchval(
                    "SELECT count(*) FROM runtime_traces WHERE user_id=$1 "
                    "AND trace_type='chat_calendar_provider_failed'", uid)
            expect(pf >= 1, "C provider-failed trace written")
        finally:
            calendar_adapters._http_get_json = orig_http

        # --- D) read gate-pass + mocked adapter returns events ---
        async def _fake_list(*, access_token=None, time_min=None, time_max=None, limit=10):
            return list(FAKE_EVENTS)
        adapter.list_events = _fake_list
        h, t = await cal("What's on my calendar today?")
        responses.append(t)
        expect(h and t and "Team sync" in t and "Room 4" in t and "for today" in t,
               "D list renders event metadata + window label")

        # --- E) write gate-pass (mocked) + CONFIRM-BEFORE-WRITE ---
        async def _allow_write(provider, user_id, action):
            return {"allowed": True, "supports": True, "capability_mismatch": False,
                    "flag_ok": True, "kill_switch_clear": True, "reason": "test-enabled"}
        cc._write_gate = _allow_write

        async def _fake_token(provider, user_id):
            return FAKE_ACCESS
        cc._get_access_token = _fake_token

        captured = {}

        async def _fake_create(*, access_token=None, fields=None, calendar_id="primary"):
            return {"id": "new1", "title": fields.get("title"), "start": "2026-06-30T15:00:00Z",
                    "end": "—", "location": "", "attendees": [], "link": "https://cal/new1"}

        async def _fake_update(*, access_token=None, event_id=None, fields=None, calendar_id="primary"):
            captured["update_cal"] = calendar_id
            return {"id": event_id, "title": "Team sync (moved)", "start": "2026-06-30T16:00:00Z",
                    "end": "—", "location": "", "attendees": [], "link": ""}

        async def _fake_delete(*, access_token=None, event_id=None, calendar_id="primary"):
            captured["delete_cal"] = calendar_id
            return {"id": event_id, "deleted": True}
        adapter.create_event = _fake_create
        adapter.update_event = _fake_update
        adapter.delete_event = _fake_delete

        # create: prepare (confirm card, no write) → confirm (fires write)
        h, t = await cal("Schedule a meeting with the team tomorrow")
        responses.append(t)
        expect(h and t and "Ready to create" in t and "confirm" in t.lower() and "Team meeting" in t,
               "E create stages a confirmation card")
        expect(await cc.has_pending(sess), "E pending action staged after create")
        async with pool.acquire() as conn:
            mid_writes = await conn.fetchval(
                "SELECT count(*) FROM calendar_access_events WHERE user_id=$1 AND allowed=true "
                "AND action IN ('create','update','delete')", uid)
        expect(mid_writes == 0, "E no write performed before confirm")
        h, t = await cal("confirm")
        responses.append(t)
        expect(h and t and "Created" in t and "✓" in t, "E confirm fires the create")
        expect(not await cc.has_pending(sess), "E pending cleared after confirm")

        # update: prepare → confirm
        h, t = await cal("Reschedule my Team sync meeting")
        responses.append(t)
        expect(h and t and "Ready to reschedule" in t and "Team sync" in t, "E update stages confirmation")
        h, t = await cal("confirm")
        responses.append(t)
        expect(h and t and "Updated" in t, "E confirm fires the update")

        # delete: prepare → confirm
        h, t = await cal("Cancel my Team sync meeting")
        responses.append(t)
        expect(h and t and "Ready to cancel" in t and "Team sync" in t, "E delete stages confirmation")
        h, t = await cal("confirm")
        responses.append(t)
        expect(h and t and "Cancelled" in t, "E confirm fires the delete")
        expect(captured.get("update_cal") == FAKE_CAL and captured.get("delete_cal") == FAKE_CAL,
               "E update/delete target the event's SECONDARY calendar (cross-calendar write)")

        # cancel path: prepare a create then cancel → no write
        h, t = await cal("Schedule a meeting with Sam")
        responses.append(t)
        expect(h and t and "Ready to create" in t, "E cancel-path stages a create")
        h, t = await cal("cancel")
        responses.append(t)
        expect(h and t and "Cancelled" in t and "nothing was changed" in t.lower(), "E cancel discards the staged write")
        expect(not await cc.has_pending(sess), "E pending cleared after cancel")

        async with pool.acquire() as conn:
            allowed_writes = await conn.fetchval(
                "SELECT count(*) FROM calendar_access_events WHERE user_id=$1 AND allowed=true "
                "AND action IN ('create','update','delete')", uid)
        expect(allowed_writes == 3, f"E exactly three real writes via confirm (got {allowed_writes})")

        # traces present
        async with pool.acquire() as conn:
            traces = {r["trace_type"] for r in await conn.fetch(
                "SELECT DISTINCT trace_type FROM runtime_traces WHERE user_id=$1 "
                "AND trace_type LIKE 'chat_calendar_%'", uid)}
        for tr in ("chat_calendar_request", "chat_calendar_events_listed",
                   "chat_calendar_event_created", "chat_calendar_event_updated",
                   "chat_calendar_event_deleted", "chat_calendar_write_denied",
                   "chat_calendar_proposal_fallback", "chat_calendar_confirm_pending",
                   "chat_calendar_confirmed", "chat_calendar_cancelled"):
            expect(tr in traces, f"missing trace {tr}")

        # no token leak anywhere
        for r in responses:
            expect(FAKE_ACCESS not in (r or ""), "token leaked into a calendar response")
    finally:
        cc._read_gate = orig_read_gate
        cc._write_gate = orig_write_gate
        cc._get_access_token = orig_token
        cc.extract_event_fields = orig_extract
        for meth in ("list_events", "create_event", "update_event", "delete_event"):
            if meth in adapter.__dict__:
                del adapter.__dict__[meth]
        async with pool.acquire() as conn:
            # Restore the operator's REAL flag state (never clobber their config).
            for fr in saved_flags:
                await conn.execute(
                    "UPDATE provider_execution_feature_flags SET enabled=$1, dry_run_only=$2 "
                    "WHERE id=$3", fr["enabled"], fr["dry_run_only"], fr["id"])
            await conn.execute("DELETE FROM calendar_pending_actions WHERE session_id=$1", sess)
            if uid is not None:
                await conn.execute("DELETE FROM calendar_access_events WHERE user_id=$1", uid)
                await conn.execute("DELETE FROM runtime_traces WHERE user_id=$1", uid)
                await conn.execute("DELETE FROM schedule_proposals WHERE created_by=$1", uid)
                await conn.execute("DELETE FROM provider_oauth_connectors WHERE user_id=$1", uid)
                await conn.execute("DELETE FROM users WHERE id=$1", uid)

    if fails:
        print("RESULT: FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print("RESULT: PASS — calendar CRUD FAILS CLOSED (flags off + kill switch off); "
          "kill switch is the master write gate; blocked CREATE → internal proposal; "
          "read gate-pass handles provider rejection + renders events; confirm-before-write "
          "stages a card then fires the real adapter write only on 'confirm' (3 writes), "
          "'cancel' discards it; pending lifecycle clean; traces + audit; no token leak")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
