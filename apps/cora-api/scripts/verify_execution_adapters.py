"""Durable end-to-end verification of External Provider Execution Adapter Skeleton v1.6.

Tests the registry (resolve by provider_name+action_type, fail-closed) and the
REAL service against a throwaway user with a CONNECTED gmail credential + an
approved email intent:
  - resolve_adapter happy paths + fail-closed (unknown provider, unsupported
    action) + outlook_calendar->MicrosoftCalendarAdapter alias;
  - simulate_adapter_payload -> provider-shaped request (api_method), payload_ready,
    external_action_performed False, + adapter_resolved/_payload_validated/_simulated
    events and provider_adapter_simulated trace;
  - run_blocked_execution_check -> status=blocked_by_governance /
    reason=provider_execution_disabled / real_execution_performed False, runs the
    interlock, + adapter_execution_blocked event and provider_adapter_execution_blocked
    trace.
Asserts no token material leaks anywhere. Disposable user; cleaned in finally. No
provider API call. Run:

    docker cp apps/cora-api/scripts/verify_execution_adapters.py cora-api:/tmp/vad.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vad.py     # 0=PASS 1=FAIL
"""
import asyncio
import json
import sys
import uuid

from app.clients import clients, init_clients
from app import execution_adapters as ea
from app import integration_readiness as ir
from app.crypto import encrypt_secret

FAKE_ACCESS = "FAKE-ACCESS-TOKEN-never-leak"
FAKE_REFRESH = "FAKE-REFRESH-TOKEN-never-leak"


async def main() -> int:
    await init_clients()
    pool = clients.db_pool
    fails: list[str] = []
    uid = None

    def expect(c, m):
        if not c:
            fails.append(m)

    # --- registry (no DB needed) ---
    expect(ea.resolve_adapter("gmail", "send_email").__class__.__name__ == "GmailEmailAdapter",
           "resolve gmail/send_email")
    expect(ea.resolve_adapter("gmail", "create_calendar_event") is None,
           "gmail must not support calendar (fail-closed)")
    expect(ea.resolve_adapter("nope", "send_email") is None, "unknown provider must fail closed")
    expect(ea.resolve_adapter("outlook_calendar", "create_calendar_event").__class__.__name__
           == "MicrosoftCalendarAdapter", "outlook_calendar alias -> MicrosoftCalendarAdapter")
    names = {a["provider_name"] for a in ea.list_adapters()}
    expect(names == {"gmail", "outlook_mail", "google_calendar", "microsoft_calendar"},
           f"adapter set = {names}")
    expect(all(a["real_execution"] is False for a in ea.list_adapters()),
           "all adapters real_execution=False")

    try:
        async with pool.acquire() as conn:
            uid = await conn.fetchval(
                "INSERT INTO users (email, password_hash, role) "
                "VALUES ($1,'x','admin') RETURNING id",
                f"verify-ad-{uuid.uuid4()}@example.invalid")
            wid = await conn.fetchval("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")
            await conn.execute(
                "INSERT INTO provider_oauth_connectors (user_id, workspace_id, "
                "provider_name, provider_type, status, scopes, access_token_encrypted, "
                "refresh_token_encrypted, token_expires_at, metadata) VALUES "
                "($1,$2,'gmail','email','connected',$3,$4,$5,NOW()+interval '1 hour','{}'::jsonb)",
                uid, wid, ["https://www.googleapis.com/auth/gmail.send"],
                encrypt_secret(FAKE_ACCESS), encrypt_secret(FAKE_REFRESH))
            draft_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO communication_drafts (id, workspace_id, created_by, "
                "draft_type, title, recipient_hint, subject, body, tone, status) "
                "VALUES ($1,$2,$3,'email','V','vip@example.com','S','Hello body','neutral','approved')",
                draft_id, wid, uid)
        intent = await ir.create_readiness_intent_from_draft(draft_id, user_id=uid, is_admin=True)
        iid = intent["id"]

        # --- simulate adapter payload ---
        sim = await ea.simulate_adapter_payload(iid, user_id=uid, is_admin=True)
        expect(sim["resolved"] is True, "sim resolved")
        expect(sim["provider_name"] == "gmail", f"sim provider={sim.get('provider_name')}")
        expect(sim["payload_ready"] is True, f"sim payload_ready false: {sim.get('validation_errors')}")
        expect(sim["external_action_performed"] is False, "sim external_action_performed must be False")
        req = sim["simulation"]["provider_request"]
        expect(req["api_method"] == "gmail.users.messages.send", f"api_method={req.get('api_method')}")
        expect(req["would_send"] is False, "provider_request would_send must be False")
        expect(req["request"]["subject"] == "S", "provider_request subject")

        # --- blocked execution check ---
        blk = await ea.run_blocked_execution_check(iid, user_id=uid, is_admin=True)
        expect(blk["status"] == ea.BLOCKED_STATUS, f"blocked status={blk['status']}")
        expect(blk["reason"] == ea.BLOCKED_REASON, f"blocked reason={blk['reason']}")
        expect(blk["real_execution_performed"] is False, "real_execution_performed must be False")
        expect(blk.get("real_execution_allowed") is False, "real_execution_allowed must be False")
        expect("interlock_status" in blk, "blocked result missing interlock_status")

        # --- events + traces + no-leak ---
        async with pool.acquire() as conn:
            events = {r["event_type"] for r in await conn.fetch(
                "SELECT DISTINCT event_type FROM external_integration_events "
                "WHERE intent_id=$1 AND event_type LIKE 'adapter_%'", iid)}
            for e in ("adapter_resolved", "adapter_payload_validated",
                      "adapter_simulated", "adapter_execution_blocked"):
                expect(e in events, f"missing audit event {e}")
            traces = {r["trace_type"] for r in await conn.fetch(
                "SELECT DISTINCT trace_type FROM runtime_traces WHERE user_id=$1 "
                "AND trace_type LIKE 'provider_adapter_%'", uid)}
            for t in ("provider_adapter_resolved", "provider_adapter_simulated",
                      "provider_adapter_execution_blocked"):
                expect(t in traces, f"missing trace {t}")
            for tbl, col in (("external_integration_events", "payload_snapshot::text"),
                             ("runtime_traces", "tool_result::text")):
                leak = await conn.fetchval(f"SELECT count(*) FROM {tbl} WHERE {col} LIKE '%FAKE-ACCESS%'")
                expect(not leak, f"token leaked into {tbl}")
        if "FAKE-ACCESS" in json.dumps(sim) or "FAKE-ACCESS" in json.dumps(blk):
            fails.append("token leaked into an adapter result")
    finally:
        async with pool.acquire() as conn:
            if uid is not None:
                await conn.execute("DELETE FROM runtime_traces WHERE user_id=$1 AND "
                    "(trace_type LIKE 'provider_adapter_%' OR trace_type LIKE 'final_interlock_%' "
                    "OR trace_type LIKE 'execution_approval_%' OR trace_type LIKE 'provider_%')", uid)
                await conn.execute("DELETE FROM tool_execution_logs WHERE user_id=$1 AND "
                    "tool_name LIKE 'execution_approval_%'", uid)
                await conn.execute("DELETE FROM external_integration_intents WHERE created_by=$1", uid)
                await conn.execute("DELETE FROM communication_drafts WHERE created_by=$1", uid)
                await conn.execute("DELETE FROM provider_oauth_connectors WHERE user_id=$1", uid)
                await conn.execute("DELETE FROM users WHERE id=$1", uid)

    if fails:
        print("RESULT: FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print("RESULT: PASS — registry resolves + fails closed; adapter simulate builds "
          "provider-shaped payload (no send); execute always blocked_by_governance / "
          "provider_execution_disabled after interlock; 4 events + 3 traces; "
          "no token exposed; rows cleaned up")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
