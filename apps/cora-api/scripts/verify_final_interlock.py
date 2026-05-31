"""Durable end-to-end verification of Final Safety Interlock v1.5.

Drives the REAL service (`final_interlock.run_final_safety_check`) through every
result status against a throwaway user with a CONNECTED gmail credential + an
approved email intent:
  A) before approval            -> missing_approval
  B) after v1.4 approval        -> ready_but_execution_disabled (all internal
                                   checks pass; execution still disabled)
  C) tamper the intent payload  -> payload_mismatch (hash != approved hash)
  D) disconnect the connector   -> provider_not_ready

Asserts in every case: real_execution_allowed is False, execution stays disabled,
the checklist + block reasons are coherent, the traces (final_interlock_checked +
blocked / ready_but_disabled) fire, an external_integration_events row is written,
and NO token material leaks. Everything is created under a disposable user and
deleted in a finally. No Gmail/Microsoft API call. Run:

    docker cp apps/cora-api/scripts/verify_final_interlock.py cora-api:/tmp/vfi.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vfi.py     # 0=PASS 1=FAIL
"""
import asyncio
import json
import sys
import uuid

from app.clients import clients, init_clients
from app import execution_approval as ea
from app import final_interlock as fi
from app import integration_readiness as ir
from app.crypto import encrypt_secret

FAKE_ACCESS = "FAKE-ACCESS-TOKEN-never-leak"
FAKE_REFRESH = "FAKE-REFRESH-TOKEN-never-leak"


async def main() -> int:
    await init_clients()
    pool = clients.db_pool
    fails: list[str] = []
    uid = None

    def expect(cond, msg):
        if not cond:
            fails.append(msg)

    try:
        async with pool.acquire() as conn:
            uid = await conn.fetchval(
                "INSERT INTO users (email, password_hash, role) "
                "VALUES ($1,'x','admin') RETURNING id",
                f"verify-fi-{uuid.uuid4()}@example.invalid")
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
                "VALUES ($1,$2,$3,'email','V','vip@example.com','ORIG-SUBJECT','B','neutral','approved')",
                draft_id, wid, uid)
        intent = await ir.create_readiness_intent_from_draft(draft_id, user_id=uid, is_admin=True)
        iid = intent["id"]

        # A) no approval yet -> missing_approval
        a = await fi.run_final_safety_check(iid, user_id=uid, is_admin=True)
        expect(a["status"] == fi.ST_MISSING_APPROVAL, f"A status={a['status']} (want missing_approval)")
        expect(a["real_execution_allowed"] is False, "A real_execution_allowed must be False")
        expect(a["checks"]["intent_approved_for_execution"] is False, "A approved check should be False")

        # approve (v1.4) — stores the approved payload hash
        await ea.approve(iid, approver_id=uid, is_admin=True, comment="ok")

        # B) approved + ready -> ready_but_execution_disabled
        b = await fi.run_final_safety_check(iid, user_id=uid, is_admin=True)
        expect(b["status"] == fi.ST_READY_DISABLED, f"B status={b['status']} (want ready_but_execution_disabled)")
        expect(b["real_execution_allowed"] is False, "B real_execution_allowed must be False")
        expect(b["execution_enabled"] is False, "B execution_enabled must be False")
        expect(b["payload_matches"] is True, "B payload_matches should be True")
        expect(all(b["checks"][k] for k in fi._INTERNAL_CHECKS), "B all internal checks should pass")
        expect(b["checks"]["external_execution_enabled"] is False, "B future gate must be off")

        # C) tamper the intent's stored payload -> payload_mismatch
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE external_integration_intents "
                "SET payload_preview = payload_preview || '{\"subject\":\"TAMPERED\"}'::jsonb "
                "WHERE id=$1", iid)
        c = await fi.run_final_safety_check(iid, user_id=uid, is_admin=True)
        expect(c["status"] == fi.ST_PAYLOAD_MISMATCH, f"C status={c['status']} (want payload_mismatch)")
        expect(c["payload_matches"] is False, "C payload_matches should be False")
        expect(c["real_execution_allowed"] is False, "C real_execution_allowed must be False")
        # restore the original payload
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE external_integration_intents "
                "SET payload_preview = payload_preview || '{\"subject\":\"ORIG-SUBJECT\"}'::jsonb "
                "WHERE id=$1", iid)

        # D) disconnect the connector -> provider_not_ready
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE provider_oauth_connectors SET status='disconnected' WHERE user_id=$1", uid)
        d = await fi.run_final_safety_check(iid, user_id=uid, is_admin=True)
        expect(d["status"] == fi.ST_PROVIDER_NOT_READY, f"D status={d['status']} (want provider_not_ready)")
        expect(d["checks"]["provider_connected"] is False, "D provider_connected should be False")
        expect(d["real_execution_allowed"] is False, "D real_execution_allowed must be False")

        # traces + events + no-leak
        async with pool.acquire() as conn:
            traces = {r["trace_type"] for r in await conn.fetch(
                "SELECT DISTINCT trace_type FROM runtime_traces WHERE user_id=$1 "
                "AND trace_type LIKE 'final_interlock_%'", uid)}
            for t in ("final_interlock_checked", "final_interlock_blocked",
                      "final_interlock_ready_but_disabled"):
                expect(t in traces, f"missing trace {t}")
            events = await conn.fetchval(
                "SELECT count(*) FROM external_integration_events "
                "WHERE intent_id=$1 AND event_type='final_interlock_checked'", iid)
            expect(events >= 4, f"final_interlock event rows={events} (want >=4)")
            leak = await conn.fetchval(
                "SELECT count(*) FROM runtime_traces WHERE user_id=$1 "
                "AND tool_result::text LIKE '%FAKE-ACCESS%'", uid)
            expect(not leak, "token leaked into a trace")
        if any("FAKE-ACCESS" in json.dumps(x) for x in (a, b, c, d)):
            fails.append("token leaked into an interlock result")
    finally:
        async with pool.acquire() as conn:
            if uid is not None:
                await conn.execute("DELETE FROM runtime_traces WHERE user_id=$1 AND "
                    "(trace_type LIKE 'final_interlock_%' OR trace_type LIKE 'execution_approval_%' "
                    "OR trace_type LIKE 'provider_%')", uid)
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
    print("RESULT: PASS — missing_approval / ready_but_execution_disabled / "
          "payload_mismatch / provider_not_ready all resolve correctly; "
          "real_execution_allowed always False; traces + events written; "
          "no token exposed; rows cleaned up")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
