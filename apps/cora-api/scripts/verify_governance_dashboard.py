"""Durable end-to-end verification of Execution Governance Dashboard v1.8.

Generates real governance activity under a throwaway admin user (connected gmail,
approved draft -> intent -> approve -> final interlock -> adapter blocked check),
builds the dashboard scoped to that user, and asserts every section is present and
coherent, the view trace is written, and — critically — NO secret/token material
appears anywhere in the payload. Read-only; disposable user cleaned in finally. No
provider API call. Run:

    docker cp apps/cora-api/scripts/verify_governance_dashboard.py cora-api:/tmp/vgd.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vgd.py     # 0=PASS 1=FAIL
"""
import asyncio
import json
import sys
import uuid

from app.clients import clients, init_clients
from app import execution_governance_dashboard as dash
from app import execution_approval as eapp
from app import execution_adapters as eadapt
from app import final_interlock as fi
from app import integration_readiness as ir
from app.crypto import encrypt_secret

FAKE_ACCESS = "FAKE-ACCESS-TOKEN-must-never-appear"
FAKE_REFRESH = "FAKE-REFRESH-TOKEN-must-never-appear"


async def main() -> int:
    await init_clients()
    pool = clients.db_pool
    fails: list[str] = []
    uid = None

    def expect(c, m):
        if not c:
            fails.append(m)

    try:
        async with pool.acquire() as conn:
            uid = await conn.fetchval(
                "INSERT INTO users (email, password_hash, role) VALUES ($1,'x','admin') RETURNING id",
                f"verify-gd-{uuid.uuid4()}@example.invalid")
            wid = await conn.fetchval("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")
            access_ct = encrypt_secret(FAKE_ACCESS)
            await conn.execute(
                "INSERT INTO provider_oauth_connectors (user_id, workspace_id, provider_name, "
                "provider_type, status, scopes, access_token_encrypted, refresh_token_encrypted, "
                "token_expires_at, metadata) VALUES ($1,$2,'gmail','email','connected',$3,$4,$5,"
                "NOW()+interval '1 hour','{}'::jsonb)",
                uid, wid, ["https://www.googleapis.com/auth/gmail.send"],
                access_ct, encrypt_secret(FAKE_REFRESH))
            draft_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO communication_drafts (id, workspace_id, created_by, draft_type, "
                "title, recipient_hint, subject, body, tone, status) "
                "VALUES ($1,$2,$3,'email','V','vip@example.com','Governance subject','B','neutral','approved')",
                draft_id, wid, uid)
        intent = await ir.create_readiness_intent_from_draft(draft_id, user_id=uid, is_admin=True)
        iid = intent["id"]
        # Generate activity across the governance layers.
        await eapp.approve(iid, approver_id=uid, is_admin=True, comment="ok")
        await fi.run_final_safety_check(iid, user_id=uid, is_admin=True)
        await eadapt.run_blocked_execution_check(iid, user_id=uid, is_admin=True)

        # Build the dashboard scoped to this user (admin filtering to target_user_id).
        d = await dash.build_dashboard(user_id=uid, is_admin=True, target_user_id=uid)

        # --- structure + sections ---
        expect(d["safety_banner"] ==
               "Provider execution remains disabled. This dashboard is observability-only.",
               "safety_banner text")
        expect(d["external_execution_enabled"] is False, "external_execution_enabled must be False")
        s = d["summary"]
        expect(s["drafts"] >= 1, "summary.drafts >=1")
        expect(s["integration_intents"] >= 1, "summary.integration_intents >=1")
        expect(s["approval_decisions"] >= 1, "summary.approval_decisions >=1")
        expect(s["approved_for_execution"] >= 1, "summary.approved_for_execution >=1")
        expect(s["providers_connected"] >= 1, "summary.providers_connected >=1")
        expect(s["external_execution_enabled"] is False, "summary execution disabled")

        expect(any(x["id"] == str(draft_id) for x in d["recent_drafts"]), "draft in recent_drafts")
        expect(any(x["intent_id"] == str(iid) and x["decision"] == "approved_for_execution"
                   for x in d["recent_approval_events"]), "approval in recent_approval_events")
        expect(any(x["id"] == str(iid) for x in d["recent_integration_intents"]),
               "intent in recent_integration_intents")
        expect(len(d["recent_integration_events"]) >= 1, "recent_integration_events non-empty")
        expect(len(d["interlock_traces"]) >= 1, "interlock_traces non-empty")
        expect(len(d["adapter_traces"]) >= 1, "adapter_traces non-empty")
        expect(len(d["governance_blocks"]) >= 1, "governance_blocks non-empty")
        expect(isinstance(d["tool_failures"], list), "tool_failures is a list")
        expect(d["feature_flag_summary"]["total"] >= 4, "feature_flag_summary total >=4")

        # provider readiness: no secret columns, has presence flags only
        pr = d["provider_readiness"]
        expect(any(c["provider_name"] == "gmail" and c["status"] == "connected"
                   and c["has_access_token"] is True for c in pr), "gmail readiness present")
        for c in pr:
            expect("access_token_encrypted" not in c and "refresh_token_encrypted" not in c,
                   "readiness must not expose token columns")

        # cards drill-down
        card = next((c for c in d["cards"] if c["intent_id"] == str(iid)), None)
        expect(card is not None, "intent card present")
        if card:
            expect(card["connected_provider"] == "gmail", "card connected_provider")
            expect("feature_flag_state" in card and "provider_readiness_state" in card,
                   "card has flag + readiness state")
            expect(card["latest_trace_status"] is not None, "card latest_trace_status set")

        # view trace written
        async with pool.acquire() as conn:
            t = await conn.fetchval(
                "SELECT count(*) FROM runtime_traces WHERE user_id=$1 "
                "AND trace_type='execution_governance_dashboard_viewed'", uid)
            expect(t >= 1, "execution_governance_dashboard_viewed trace written")

        # CRITICAL: no secret/token material anywhere in the payload (spec #7)
        blob = json.dumps(d, default=str)
        expect(FAKE_ACCESS not in blob, "access token leaked into dashboard")
        expect(FAKE_REFRESH not in blob, "refresh token leaked into dashboard")
        expect(access_ct not in blob, "encrypted token ciphertext leaked into dashboard")
        expect("access_token_encrypted" not in blob, "token column name leaked into dashboard")
    finally:
        async with pool.acquire() as conn:
            if uid is not None:
                await conn.execute("DELETE FROM runtime_traces WHERE user_id=$1", uid)
                await conn.execute("DELETE FROM tool_execution_logs WHERE user_id=$1", uid)
                await conn.execute("DELETE FROM external_integration_intents WHERE created_by=$1", uid)
                await conn.execute("DELETE FROM communication_drafts WHERE created_by=$1", uid)
                await conn.execute("DELETE FROM provider_oauth_connectors WHERE user_id=$1", uid)
                await conn.execute("DELETE FROM users WHERE id=$1", uid)

    if fails:
        print("RESULT: FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print("RESULT: PASS — all dashboard sections present + coherent; cards drill-down; "
          "view trace written; NO token/secret material in payload; "
          "execution disabled; rows cleaned up")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
