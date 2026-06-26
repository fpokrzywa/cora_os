"""Durable end-to-end verification of Provider Execution Feature Flag Matrix v1.7.

Asserts the 4 production seeds (fail-closed: enabled=false, dry_run_only=true),
provider/action alias resolution, fail-closed evaluation (missing flag -> deny +
feature_flag_denied_execution event + provider_flag_denied trace), admin
create/modify audit (feature_flag_created / feature_flag_modified), that enabling a
flag flips flag_allows_execution at the MATRIX level only (global execution stays
disabled), and that the final interlock consults the matrix. Mutations happen in a
throwaway environment so the production seeds are never changed. Disposable user +
flag; cleaned in finally. No provider API call. Run:

    docker cp apps/cora-api/scripts/verify_feature_flags.py cora-api:/tmp/vff.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vff.py     # 0=PASS 1=FAIL
"""
import asyncio
import sys
import uuid

from app.clients import clients, init_clients
from app import execution_approval as eapp
from app import feature_flags as ff
from app import final_interlock as fi
from app import integration_readiness as ir
from app.crypto import encrypt_secret

FAKE_ACCESS = "FAKE-ACCESS-never-leak"
FAKE_REFRESH = "FAKE-REFRESH-never-leak"


async def main() -> int:
    await init_clients()
    pool = clients.db_pool
    fails: list[str] = []
    uid = None
    test_env = f"verify-{uuid.uuid4()}"

    def expect(c, m):
        if not c:
            fails.append(m)

    try:
        # A) production seeds present + fail-closed defaults. Scope the disabled
        # assertion to the EXECUTION seeds this test guarantees (send_email +
        # create_calendar_event); operator-enableable READ flags (inbox_read,
        # calendar_read/write) are intentionally excluded — an operator may turn
        # them on for real use without breaking this invariant.
        EXEC_SEEDS = {("gmail", "send_email"), ("outlook_mail", "send_email"),
                      ("google_calendar", "create_calendar_event"),
                      ("microsoft_calendar", "create_calendar_event")}
        seeds = await ff.list_flags(environment="production")
        combos = {(f["provider_name"], f["action_type"]) for f in seeds}
        for want in EXEC_SEEDS:
            expect(want in combos, f"seed missing {want}")
        for f in seeds:
            if (f["provider_name"], f["action_type"]) in EXEC_SEEDS:
                expect(f["enabled"] is False, f"seed {f['provider_name']} enabled should be False")
                expect(f["dry_run_only"] is True, f"seed {f['provider_name']} dry_run_only should be True")

        # B) provider + action alias resolution
        aliased = await ff.get_flag("outlook_calendar", "create_event")
        expect(aliased is not None and aliased["provider_name"] == "microsoft_calendar"
               and aliased["action_type"] == "create_calendar_event",
               "alias outlook_calendar/create_event -> microsoft_calendar/create_calendar_event")

        async with pool.acquire() as conn:
            uid = await conn.fetchval(
                "INSERT INTO users (email, password_hash, role) VALUES ($1,'x','admin') RETURNING id",
                f"verify-ff-{uuid.uuid4()}@example.invalid")
            wid = await conn.fetchval("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")
            await conn.execute(
                "INSERT INTO provider_oauth_connectors (user_id, workspace_id, provider_name, "
                "provider_type, status, scopes, access_token_encrypted, refresh_token_encrypted, "
                "token_expires_at, metadata) VALUES ($1,$2,'gmail','email','connected',$3,$4,$5,"
                "NOW()+interval '1 hour','{}'::jsonb)",
                uid, wid, ["https://www.googleapis.com/auth/gmail.send"],
                encrypt_secret(FAKE_ACCESS), encrypt_secret(FAKE_REFRESH))
            draft_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO communication_drafts (id, workspace_id, created_by, draft_type, "
                "title, recipient_hint, subject, body, tone, status) "
                "VALUES ($1,$2,$3,'email','V','vip@example.com','S','B','neutral','approved')",
                draft_id, wid, uid)
        intent = await ir.create_readiness_intent_from_draft(draft_id, user_id=uid, is_admin=True)
        iid = intent["id"]

        # C) evaluate seeded-but-disabled -> denied
        d1 = await ff.evaluate("gmail", "send_email", user_id=uid, intent=intent)
        expect(d1["flag_present"] is True and d1["denied"] is True
               and d1["flag_allows_execution"] is False, "C disabled flag must deny")

        # D) fail-closed: missing flag -> denied + audit event
        d2 = await ff.evaluate("ghost_provider", "send_email", user_id=uid, intent=intent)
        expect(d2["flag_present"] is False and d2["denied"] is True, "D missing flag must deny")

        # E) admin create (throwaway env) -> feature_flag_created
        created = await ff.create_flag(admin_id=uid, provider_name="gmail",
                                       provider_type="email", action_type="send_email",
                                       environment=test_env)
        expect(created["enabled"] is False and created["dry_run_only"] is True,
               "E new flag must default fail-closed")

        # F) admin modify: enable + clear dry_run -> matrix-level allow only
        modified = await ff.update_flag(uuid.UUID(str(created["id"])), admin_id=uid,
                                        changes={"enabled": True, "dry_run_only": False})
        expect(ff.flag_allows_execution(modified) is True,
               "F enabled+!dry_run must allow at matrix level")

        # G) evaluate the enabled test-env flag -> provider_flag_allowed
        d3 = await ff.evaluate("gmail", "send_email", user_id=uid, intent=intent,
                               environment=test_env)
        expect(d3["flag_allows_execution"] is True, "G enabled flag must allow (matrix level)")

        # H) interlock consults the matrix (uses production env = disabled seed)
        await eapp.approve(iid, approver_id=uid, is_admin=True, comment="ok")
        il = await fi.run_final_safety_check(iid, user_id=uid, is_admin=True)
        expect("feature_flag_present" in il["checks"], "H interlock missing feature_flag_present check")
        expect("feature_flag_allows_execution" in il["checks"], "H interlock missing feature_flag_allows_execution")
        expect(il["checks"]["feature_flag_present"] is True, "H production gmail flag should be present")
        expect(il["checks"]["feature_flag_allows_execution"] is False, "H production flag must not allow")
        expect(il["real_execution_allowed"] is False, "H real_execution_allowed must stay False")

        # traces + events
        async with pool.acquire() as conn:
            traces = {r["trace_type"] for r in await conn.fetch(
                "SELECT DISTINCT trace_type FROM runtime_traces WHERE user_id=$1 AND "
                "(trace_type LIKE 'provider_flag_%' OR trace_type LIKE 'feature_flag_%')", uid)}
            for t in ("provider_flag_checked", "provider_flag_denied", "provider_flag_allowed",
                      "feature_flag_created", "feature_flag_modified"):
                expect(t in traces, f"missing trace {t}")
            ev = await conn.fetchval(
                "SELECT count(*) FROM external_integration_events WHERE intent_id=$1 "
                "AND event_type='feature_flag_denied_execution'", iid)
            expect(ev >= 1, f"feature_flag_denied_execution events={ev} (want >=1)")

        # req 13: production seeds unchanged
        seeds2 = await ff.list_flags(environment="production")
        expect(all(not f["enabled"] and f["dry_run_only"] for f in seeds2
                   if (f["provider_name"], f["action_type"]) in EXEC_SEEDS),
               "production execution seeds must remain enabled=false/dry_run_only=true")
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM provider_execution_feature_flags WHERE environment=$1", test_env)
            if uid is not None:
                await conn.execute("DELETE FROM runtime_traces WHERE user_id=$1 AND "
                    "(trace_type LIKE 'provider_flag_%' OR trace_type LIKE 'feature_flag_%' "
                    "OR trace_type LIKE 'final_interlock_%' OR trace_type LIKE 'execution_approval_%' "
                    "OR trace_type LIKE 'provider_%')", uid)
                await conn.execute("DELETE FROM tool_execution_logs WHERE user_id=$1 AND "
                    "(tool_name LIKE 'provider_feature_flag%' OR tool_name LIKE 'execution_approval_%')", uid)
                await conn.execute("DELETE FROM external_integration_intents WHERE created_by=$1", uid)
                await conn.execute("DELETE FROM communication_drafts WHERE created_by=$1", uid)
                await conn.execute("DELETE FROM provider_oauth_connectors WHERE user_id=$1", uid)
                await conn.execute("DELETE FROM users WHERE id=$1", uid)

    if fails:
        print("RESULT: FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print("RESULT: PASS — 4 seeds fail-closed; alias resolution; fail-closed deny + "
          "audit event; create/modify audited; matrix-level enable does NOT enable "
          "execution; interlock consults matrix; real_execution_allowed False; "
          "production seeds preserved; rows cleaned up")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
