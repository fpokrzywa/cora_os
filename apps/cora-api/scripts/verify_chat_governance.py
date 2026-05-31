"""Durable end-to-end verification of Chat-Native Governance Explanation v2.0.

Sets up real governance activity under a throwaway admin (connected gmail; via the
v1.9 chat handlers: create → approve → prepare → safety check), then asks the
governance questions and asserts each explanation/timeline is coherent, the two
traces fire, detection classifies correctly, and NO secret/token material leaks.
Disposable rows cleaned in finally. No provider API call. Run:

    docker cp apps/cora-api/scripts/verify_chat_governance.py cora-api:/tmp/vcg.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vcg.py     # 0=PASS 1=FAIL
"""
import asyncio
import sys
import uuid

from app.clients import clients, init_clients
from app import chat_email_lifecycle as cel
from app import chat_governance as cg
from app.crypto import encrypt_secret

FAKE_ACCESS = "FAKE-ACCESS-never-leak"
_EMAIL = "Subject: Project delay\nTo: Mark\n\nHi Mark, the project is delayed. Thanks."


async def main() -> int:
    await init_clients()
    pool = clients.db_pool
    fails: list[str] = []
    uid = None
    sess = uuid.uuid4()

    def expect(c, m):
        if not c:
            fails.append(m)

    async def _fake_generate(prompt):
        return _EMAIL
    cel._generate_text = _fake_generate

    async def email(msg):
        cmd = cel.detect_email_command(msg)
        return await cel.handle_email_command(
            cmd, message=msg, session_uuid=sess, user_id=uid, workspace_uuid=wid,
            scope_type="user", is_admin=True)

    async def gov(msg):
        kind = cg.detect_governance_question(msg)
        if kind is None:
            return None, None
        _, txt = await cg.handle_governance_question(
            kind, message=msg, session_uuid=sess, user_id=uid, workspace_uuid=wid,
            is_admin=True)
        return kind, txt

    # --- detection unit checks ---
    expect(cg.detect_governance_question("Why is execution disabled?") == "why_execution_disabled", "detect why_execution_disabled")
    expect(cg.detect_governance_question("Who approved this draft?") == "who_approved", "detect who_approved")
    expect(cg.detect_governance_question("What happened to my Gmail intent?") == "what_happened_intent", "detect what_happened_intent")
    expect(cg.detect_governance_question("Show me the governance trail.") == "governance_trail", "detect governance_trail")
    expect(cg.detect_governance_question("Show me the approval history.") == "approval_history", "detect approval_history")
    expect(cg.detect_governance_question("Why was this blocked?") == "why_blocked", "detect why_blocked")
    expect(cg.detect_governance_question("What failed during validation?") == "what_failed", "detect what_failed")
    expect(cg.detect_governance_question("What's the weather today?") is None, "non-governance must be None")

    try:
        async with pool.acquire() as conn:
            uid = await conn.fetchval(
                "INSERT INTO users (email, password_hash, role) VALUES ($1,'x','admin') RETURNING id",
                f"verify-cg-{uuid.uuid4()}@example.invalid")
            wid = await conn.fetchval("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")
            await conn.execute(
                "INSERT INTO provider_oauth_connectors (user_id, workspace_id, provider_name, "
                "provider_type, status, scopes, access_token_encrypted, refresh_token_encrypted, "
                "token_expires_at, metadata) VALUES ($1,$2,'gmail','email','connected',$3,$4,$5,"
                "NOW()+interval '1 hour','{}'::jsonb)",
                uid, wid, ["https://www.googleapis.com/auth/gmail.send"],
                encrypt_secret(FAKE_ACCESS), encrypt_secret("r"))

        # set up activity
        await email("Draft an email to Mark about the project delay and save it as a draft.")
        await email("Approve it.")
        await email("Prepare it for Gmail.")
        await email("Run the final safety check.")

        responses = []

        k, t = await gov("Why is execution disabled?")
        responses.append(t)
        expect(t and "kill switch" in t.lower() and "disabled" in t.lower(), "why_execution_disabled content")

        k, t = await gov("Who approved this draft?")
        responses.append(t)
        expect(t and "approved" in t.lower(), "who_approved content")

        k, t = await gov("What happened to my Gmail intent?")
        responses.append(t)
        expect(t and "intent" in t.lower() and "timeline" in t.lower(), "what_happened_intent content")

        k, t = await gov("Show me the approval history.")
        responses.append(t)
        expect(t and "approval history" in t.lower(), "approval_history content")

        k, t = await gov("Show me the governance trail.")
        responses.append(t)
        expect(t and "governance trail" in t.lower() and "audit view only" in t.lower(),
               "governance_trail content")

        k, t = await gov("Why was this blocked?")
        responses.append(t)
        expect(t and "execution" in t.lower(), "why_blocked content")

        k, t = await gov("What failed during validation?")
        responses.append(t)
        expect(t is not None, "what_failed handled")

        # no token leak anywhere
        for r in responses:
            expect(FAKE_ACCESS not in (r or ""), "token leaked into a governance response")

        # traces written
        async with pool.acquire() as conn:
            traces = {r["trace_type"] for r in await conn.fetch(
                "SELECT DISTINCT trace_type FROM runtime_traces WHERE user_id=$1 "
                "AND trace_type LIKE 'governance_%'", uid)}
        expect("governance_explanation_requested" in traces, "missing governance_explanation_requested")
        expect("governance_timeline_generated" in traces, "missing governance_timeline_generated")
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM chat_email_context WHERE session_id=$1", sess)
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
    print("RESULT: PASS — governance questions detected + answered (why-disabled / "
          "who-approved / intent-status / approval-history / governance-trail / "
          "why-blocked / what-failed); 2 traces written; no token leak; rows cleaned up")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
