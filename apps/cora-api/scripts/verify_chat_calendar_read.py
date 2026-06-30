"""Deterministic check of the agent calendar READ tool (chronos_list_calendar_events).
NO live calendar, NO OAuth, NO egress: the per-provider read + provider resolution are
monkeypatched. Covers catalog registration + CHRONOS scoping (against the live seed),
the helper's shaping/title-filter/fail-closed behavior, and the read-only dispatch
intercept (governance runs first, then routes to the helper — never the generic runner).

    docker cp apps/cora-api/scripts/verify_chat_calendar_read.py cora-api:/tmp/vcr.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vcr.py     # 0=PASS 1=FAIL
"""
import asyncio
import sys
import uuid
from types import SimpleNamespace

from app.clients import init_clients
from app import agent_runtime, chat_calendar

UID = uuid.uuid4()


async def main() -> int:
    await init_clients()
    fails: list[str] = []

    def expect(cond: bool, msg: str) -> None:
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # ---- Part A: registration + scope (live seed via _build_catalog) ----
    expect("chronos_list_calendar_events" in agent_runtime.READ_ONLY_TOOLS,
           "tool is in the READ_ONLY_TOOLS catalog")

    cat_chronos = {t["function"]["name"] for t in await agent_runtime._build_catalog("CHRONOS")}
    cat_forge = {t["function"]["name"] for t in await agent_runtime._build_catalog("FORGE")}
    cat_orch = {t["function"]["name"] for t in await agent_runtime._build_catalog(None)}
    expect("chronos_list_calendar_events" in cat_chronos, "CHRONOS spoke sees the read tool")
    expect("chronos_list_calendar_events" not in cat_forge,
           "FORGE spoke does NOT see it (allowed_agents scope isolation)")
    expect("chronos_list_calendar_events" in cat_orch, "orchestrator (None) sees it")

    row = await agent_runtime._fetch_tool_row("chronos_list_calendar_events")
    expect(row is not None, "tool row seeded in the DB")
    if row:
        expect(not row.get("requires_confirmation") and row.get("risk_level") != "high",
               "seed clears the read-only floor (requires_confirmation=F, risk!=high)")
        expect(list(row.get("allowed_agents") or []) == ["CHRONOS"],
               "seed allowed_agents == ['CHRONOS']")

    # ---- Part B: helper shaping / filter / fail-closed (mocked reads) ----
    orig_resolve = chat_calendar._resolve_read_providers
    orig_read_one = chat_calendar._read_one_calendar

    async def fake_resolve(_msg, _uid):
        return ["google_calendar"]

    EVENTS = [
        {"id": "evt-b", "title": "Budget sync", "start": "2026-06-30T19:00:00+00:00",
         "end": "2026-06-30T20:00:00+00:00", "location": "Zoom", "provider": "google_calendar"},
        {"id": "evt-a", "title": "1:1 with Dorothy", "start": "2026-06-30T15:00:00+00:00",
         "end": "2026-06-30T15:30:00+00:00", "location": None, "provider": "google_calendar"},
    ]

    async def fake_read_one(provider, time_min, time_max, *, session_uuid, user_id, workspace_uuid):
        return list(EVENTS), {"provider": provider, "reason": "ok"}

    chat_calendar._resolve_read_providers = fake_resolve
    chat_calendar._read_one_calendar = fake_read_one
    try:
        res = await chat_calendar.agent_list_calendar_events(user_id=UID, window="today")
        expect(res["ok"] and len(res["events"]) == 2, "happy path returns both events")
        expect([e["event_id"] for e in res["events"]] == ["evt-a", "evt-b"],
               "events sorted by start (3pm before 7pm)")
        first = res["events"][0]
        expect(first.get("provider") == "google_calendar" and first.get("event_id") == "evt-a"
               and bool(first.get("when")),
               "event exposes provider + event_id + a friendly 'when'")

        res_q = await chat_calendar.agent_list_calendar_events(
            user_id=UID, window="today", query="dorothy")
        expect(res_q["ok"] and [e["event_id"] for e in res_q["events"]] == ["evt-a"],
               "title query filters case-insensitively")

        async def fake_resolve_none(_msg, _uid):
            return []

        chat_calendar._resolve_read_providers = fake_resolve_none
        res_none = await chat_calendar.agent_list_calendar_events(user_id=UID)
        expect(not res_none["ok"] and "no connected calendar" in res_none["reason"],
               "no connected calendar -> ok=False, fail-closed")

        chat_calendar._resolve_read_providers = fake_resolve

        async def fake_read_gated(provider, *a, **k):
            return None, {"provider": provider, "reason": "calendar_read feature flag disabled"}

        chat_calendar._read_one_calendar = fake_read_gated
        res_gated = await chat_calendar.agent_list_calendar_events(user_id=UID, window="today")
        expect(not res_gated["ok"] and "no calendar readable" in res_gated["reason"],
               "all providers gated out -> ok=False (gate reason surfaced)")
    finally:
        chat_calendar._resolve_read_providers = orig_resolve
        chat_calendar._read_one_calendar = orig_read_one

    # ---- Part C: read-only dispatch intercept (governance first, then helper) ----
    orig_fetch = agent_runtime._fetch_tool_row
    orig_perm = agent_runtime.check_permission
    orig_dispatch = agent_runtime.dispatch_tool
    orig_agent_list = chat_calendar.agent_list_calendar_events

    async def fake_fetch(name):
        return {"name": name, "requires_confirmation": False, "risk_level": "low",
                "enabled": True, "allowed_agents": ["CHRONOS"], "type": "internal_read"}

    async def fake_perm(tool, **k):
        return SimpleNamespace(allowed=True, reason="ok")

    async def boom_dispatch(*a, **k):
        raise AssertionError("generic dispatch_tool must NOT run for the calendar read")

    captured = {}

    async def fake_agent_list(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "events": [], "reason": "SENTINEL-OK", "providers": ["google_calendar"]}

    agent_runtime._fetch_tool_row = fake_fetch
    agent_runtime.check_permission = fake_perm
    agent_runtime.dispatch_tool = boom_dispatch
    chat_calendar.agent_list_calendar_events = fake_agent_list
    try:
        out = await agent_runtime._dispatch_read_only(
            "chronos_list_calendar_events",
            {"window": "today", "query": "x", "provider": "google_calendar"},
            agent_name="CHRONOS", user_id=UID, session_id=str(uuid.uuid4()))
        expect("SENTINEL-OK" in out, "dispatch routes to agent_list_calendar_events (not dispatch_tool)")
        expect(captured.get("window") == "today" and captured.get("provider") == "google_calendar",
               "dispatch forwards window/query/provider args to the helper")

        # Governance still gates: a denial returns the error, never reaching the helper.
        async def deny(tool, **k):
            return SimpleNamespace(allowed=False, reason="agent not in allowed_agents")

        agent_runtime.check_permission = deny
        denied = await agent_runtime._dispatch_read_only(
            "chronos_list_calendar_events", {}, agent_name="FORGE",
            user_id=UID, session_id=None)
        expect("denied by governance" in denied, "governance denial blocks the read (no helper call)")
    finally:
        agent_runtime._fetch_tool_row = orig_fetch
        agent_runtime.check_permission = orig_perm
        agent_runtime.dispatch_tool = orig_dispatch
        chat_calendar.agent_list_calendar_events = orig_agent_list

    print()
    if fails:
        print(f"FAIL ({len(fails)}): " + "; ".join(fails))
        return 1
    print("PASS: chronos_list_calendar_events (agent calendar read) verified")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
