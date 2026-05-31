"""Durable end-to-end verification of Human Approval Execution Console v1.4.

Exercises the REAL service (`app.execution_approval`) against a throwaway user:
  - an APPROVED SIGNAL email draft + a CONNECTED gmail credential -> intent that is
    ready_for_approval -> APPROVE for future execution (records audit + traces);
  - an APPROVED CHRONOS calendar proposal with NO calendar connector -> intent that
    cannot be approved -> APPROVE is BLOCKED (audited) -> REJECT.

Asserts: derived approval_state, the governance + provider-readiness checklists,
the execution_approvals audit rows (approver/decision/snapshots/payload_hash), the
4 runtime traces (viewed/approved/rejected/blocked), the governed tool logs, that
execution stays disabled (execution_allowed=False, dry_run_only), and that NO token
material appears in any result/row/trace. Everything is created under a disposable
user with a FAKE encrypted credential and deleted in a finally. No Gmail/Microsoft
API call. Run:

    docker cp apps/cora-api/scripts/verify_execution_approval.py cora-api:/tmp/vea.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vea.py     # 0=PASS 1=FAIL
"""
import asyncio
import json
import sys
import uuid

from app.clients import clients, init_clients
from app import execution_approval as ea
from app import integration_readiness as ir
from app.crypto import encrypt_secret

FAKE_ACCESS = "FAKE-ACCESS-TOKEN-must-never-leak-anywhere"
FAKE_REFRESH = "FAKE-REFRESH-TOKEN-must-never-leak-anywhere"


async def main() -> int:
    await init_clients()
    pool = clients.db_pool
    fails: list[str] = []
    uid = None
    try:
        async with pool.acquire() as conn:
            uid = await conn.fetchval(
                "INSERT INTO users (email, password_hash, role) "
                "VALUES ($1,'x','admin') RETURNING id",
                f"verify-ea-{uuid.uuid4()}@example.invalid")
            wid = await conn.fetchval("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")
            # Connected gmail credential (email). NO calendar connector.
            await conn.execute(
                """
                INSERT INTO provider_oauth_connectors
                    (user_id, workspace_id, provider_name, provider_type, status,
                     scopes, access_token_encrypted, refresh_token_encrypted,
                     token_expires_at, metadata)
                VALUES ($1,$2,'gmail','email','connected',$3,$4,$5,
                        NOW()+interval '1 hour','{}'::jsonb)
                """,
                uid, wid, ["https://www.googleapis.com/auth/gmail.send"],
                encrypt_secret(FAKE_ACCESS), encrypt_secret(FAKE_REFRESH))
            # Approved email draft + approved calendar proposal.
            draft_id, proposal_id = uuid.uuid4(), uuid.uuid4()
            await conn.execute(
                "INSERT INTO communication_drafts (id, workspace_id, created_by, "
                "draft_type, title, recipient_hint, subject, body, tone, status) "
                "VALUES ($1,$2,$3,'email','V','vip@example.com','S','B','neutral','approved')",
                draft_id, wid, uid)
            await conn.execute(
                "INSERT INTO schedule_proposals (id, workspace_id, created_by, "
                "proposal_type, title, description, start_time, end_time, timezone, "
                "attendees, agenda, reminders, status) VALUES "
                "($1,$2,$3,'meeting','V','d',NOW()+interval '1 day',"
                "NOW()+interval '1 day 1 hour','UTC',$4,$5,$6,'approved')",
                proposal_id, wid, uid, ["a@example.com"], ["x"], ["10m"])

        email_intent = await ir.create_readiness_intent_from_draft(draft_id, user_id=uid, is_admin=True)
        cal_intent = await ir.create_readiness_intent_from_proposal(proposal_id, user_id=uid, is_admin=True)
        eid, cid = email_intent["id"], cal_intent["id"]

        # --- view: email intent should be ready_for_approval ---
        ev = await ea.view_intent(eid, user_id=uid, is_admin=True)
        if ev["approval_state"] != ea.ST_READY:
            fails.append(f"email view approval_state={ev['approval_state']} (want ready_for_approval)")
        if not ev["can_approve"]: fails.append("email can_approve false")
        if not ev["readiness"]["provider_connected"]: fails.append("email provider_connected false")
        if not ev["readiness"]["required_scopes_present"]: fails.append(f"email scopes missing={ev['readiness']['missing_scopes']}")
        if not ev["readiness"]["source_approved"]: fails.append("email source_approved false")
        if not ev["governance"]["dry_run_only"]: fails.append("governance dry_run_only false")
        if ev["governance"]["execution_enabled"]: fails.append("execution_enabled should be False")
        if ev["execution_allowed"]: fails.append("execution_allowed should be False")
        if not (ev["provider_payload_preview"] or {}).get("preview"): fails.append("no payload preview")

        # --- approve the email intent ---
        ap = await ea.approve(eid, approver_id=uid, is_admin=True, comment="LGTM")
        if ap["approval_state"] != ea.ST_APPROVED:
            fails.append(f"approved state={ap['approval_state']}")

        # --- calendar intent: cannot approve (no calendar connector) ---
        cv = await ea.view_intent(cid, user_id=uid, is_admin=True)
        if cv["can_approve"]: fails.append("calendar can_approve should be False")
        if cv["approval_state"] != ea.ST_PENDING_REVIEW:
            fails.append(f"calendar approval_state={cv['approval_state']} (want pending_review)")
        try:
            await ea.approve(cid, approver_id=uid, is_admin=True, comment="try")
            fails.append("calendar approve should have raised (blocked)")
        except ea.ApprovalError:
            pass
        # --- reject the calendar intent ---
        rj = await ea.reject(cid, approver_id=uid, is_admin=True, comment="not ready")
        if rj["approval_state"] != ea.ST_REJECTED:
            fails.append(f"rejected state={rj['approval_state']}")

        # --- audit rows + traces + tool logs + no-leak ---
        async with pool.acquire() as conn:
            arow = await conn.fetchrow(
                "SELECT approver_id, decision, approval_state, reason, "
                "governance_snapshot, provider_readiness_snapshot, payload_hash "
                "FROM execution_approvals WHERE intent_id=$1 AND decision='approved_for_execution'", eid)
            if arow is None: fails.append("no approved execution_approvals row")
            else:
                if str(arow["approver_id"]) != str(uid): fails.append("approval approver_id mismatch")
                if not arow["payload_hash"]: fails.append("approval payload_hash empty")
                if not arow["governance_snapshot"]: fails.append("approval governance_snapshot empty")
                if not arow["provider_readiness_snapshot"]: fails.append("approval readiness_snapshot empty")
            blocked = await conn.fetchval(
                "SELECT count(*) FROM execution_approvals WHERE intent_id=$1 AND decision='blocked'", cid)
            if blocked != 1: fails.append(f"blocked rows={blocked} (want 1)")
            rejected = await conn.fetchval(
                "SELECT count(*) FROM execution_approvals WHERE intent_id=$1 AND decision='rejected'", cid)
            if rejected != 1: fails.append(f"rejected rows={rejected} (want 1)")

            traces = {r["trace_type"] for r in await conn.fetch(
                "SELECT DISTINCT trace_type FROM runtime_traces WHERE user_id=$1 "
                "AND trace_type LIKE 'execution_approval_%'", uid)}
            for t in ("execution_approval_viewed", "execution_approval_approved",
                      "execution_approval_rejected", "execution_approval_blocked"):
                if t not in traces: fails.append(f"missing trace {t}")

            logs = {(r["tool_name"], r["status"]) for r in await conn.fetch(
                "SELECT tool_name, status FROM tool_execution_logs WHERE user_id=$1 "
                "AND tool_name LIKE 'execution_approval_%'", uid)}
            if ("execution_approval_approved", "success") not in logs:
                fails.append("missing approve success tool log")
            if ("execution_approval_rejected", "success") not in logs:
                fails.append("missing reject success tool log")
            if ("execution_approval_approved", "blocked") not in logs:
                fails.append("missing blocked approve tool log")

            for tbl, col in (("execution_approvals", "governance_snapshot::text"),
                             ("runtime_traces", "tool_result::text")):
                leak = await conn.fetchval(
                    f"SELECT count(*) FROM {tbl} WHERE {col} LIKE '%FAKE-ACCESS%'")
                if leak: fails.append(f"token leaked into {tbl}")
            seeded = await conn.fetchval(
                "SELECT count(*) FROM tools WHERE name IN "
                "('execution_approval_approved','execution_approval_rejected')")
            if seeded != 2: fails.append(f"governed tools seeded={seeded} (want 2)")
        if "FAKE-ACCESS" in json.dumps(ap) or "FAKE-ACCESS" in json.dumps(ev):
            fails.append("token leaked into a view/approve result")
    finally:
        async with pool.acquire() as conn:
            if uid is not None:
                await conn.execute("DELETE FROM runtime_traces WHERE user_id=$1 AND "
                    "(trace_type LIKE 'execution_approval_%' OR trace_type LIKE 'provider_%')", uid)
                await conn.execute("DELETE FROM tool_execution_logs WHERE user_id=$1 AND "
                    "tool_name LIKE 'execution_approval_%'", uid)
                # execution_approvals + events cascade on intent delete.
                await conn.execute("DELETE FROM external_integration_intents WHERE created_by=$1", uid)
                await conn.execute("DELETE FROM communication_drafts WHERE created_by=$1", uid)
                await conn.execute("DELETE FROM schedule_proposals WHERE created_by=$1", uid)
                await conn.execute("DELETE FROM provider_oauth_connectors WHERE user_id=$1", uid)
                await conn.execute("DELETE FROM users WHERE id=$1", uid)

    if fails:
        print("RESULT: FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print("RESULT: PASS — ready_for_approval→approved (audit+snapshots+hash), "
          "blocked approve audited, rejected; 4 traces + governed tool logs; "
          "execution stays disabled; no token exposed; rows cleaned up")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
