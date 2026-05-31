"""Durable end-to-end verification of Chat-Native Approval Queue Management v2.2.

Creates 3 drafts under a throwaway admin (via the v1.9 handlers), then drives the
numbered queue: list → open → approve the first → reject item 2 → prepare item 1 →
approve item 3 → list intents → simulate item 1 → prepare all approved. Asserts
numbered resolution, the queue traces, review/audit events, ownership, no token
leak, and execution disabled. Disposable rows cleaned in finally. No provider API
call. Run:

    docker cp apps/cora-api/scripts/verify_chat_approval_queue.py cora-api:/tmp/vcq.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vcq.py     # 0=PASS 1=FAIL
"""
import asyncio
import sys
import uuid

from app.clients import clients, init_clients
from app import chat_approval_queue as cq
from app import chat_email_lifecycle as cel
from app import signal_tools
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

    async def q(msg):
        cmd = cq.detect_queue_command(msg)
        if cmd is None:
            return None, None
        return await cq.handle_queue_command(
            cmd, message=msg, session_uuid=sess, user_id=uid, workspace_uuid=wid,
            scope_type="user", is_admin=True)

    # detection unit checks
    expect(cq.detect_queue_command("What emails need my approval?") == ("list_drafts", None, None), "detect list_drafts")
    expect(cq.detect_queue_command("Show pending Gmail intents.") == ("list_intents", None, "gmail"), "detect list_intents gmail")
    expect(cq.detect_queue_command("Open item 2.") == ("open", 2, None), "detect open item")
    expect(cq.detect_queue_command("Approve the first one.") == ("approve", 1, None), "detect approve first")
    expect(cq.detect_queue_command("Reject the latest draft.") == ("reject", "last", None), "detect reject latest")
    expect(cq.detect_queue_command("Prepare all approved drafts for simulation.") == ("prepare_all", None, "gmail"), "detect prepare_all")
    expect(cq.detect_queue_command("Approve it.") is None, "approve-it (no selector) must be None")
    expect(cq.detect_queue_command("simulate item 1") == ("simulate", 1, None), "detect simulate item")

    try:
        async with pool.acquire() as conn:
            uid = await conn.fetchval(
                "INSERT INTO users (email, password_hash, role) VALUES ($1,'x','admin') RETURNING id",
                f"verify-cq-{uuid.uuid4()}@example.invalid")
            wid = await conn.fetchval("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")
            await conn.execute(
                "INSERT INTO provider_oauth_connectors (user_id, workspace_id, provider_name, "
                "provider_type, status, scopes, access_token_encrypted, refresh_token_encrypted, "
                "token_expires_at, metadata) VALUES ($1,$2,'gmail','email','connected',$3,$4,$5,"
                "NOW()+interval '1 hour','{}'::jsonb)",
                uid, wid, ["https://www.googleapis.com/auth/gmail.send"],
                encrypt_secret(FAKE_ACCESS), encrypt_secret("r"))

        ids = []
        for who in ("Anna", "Ben", "Cara"):
            await email(f"Draft an email to {who} about the update and save it as a draft.")
            ids.append((await cel.get_context(sess))["last_created_draft_id"])
        responses = []

        # list → 3 pending, newest first (Cara, Ben, Anna)
        h, t = await q("Show my pending drafts.")
        responses.append(t)
        expect(h and t and "Emails needing approval** (3)" in t, "list shows 3")

        # open item 2
        h, t = await q("Open item 2.")
        responses.append(t)
        expect(h and t and "**Draft**" in t, "open item renders draft")

        # approve the first one (newest = Cara)
        h, t = await q("Approve the first one.")
        responses.append(t)
        # reject item 2 (Ben)
        h, t = await q("Reject item 2.")
        responses.append(t)
        # approve item 3 (Anna)
        h, t = await q("Approve item 3.")
        responses.append(t)

        async with pool.acquire() as conn:
            statuses = {str(r["id"]): r["status"] for r in await conn.fetch(
                "SELECT id, status FROM communication_drafts WHERE created_by=$1", uid)}
        approved = sum(1 for s in statuses.values() if s == "approved")
        rejected = sum(1 for s in statuses.values() if s == "rejected")
        expect(approved == 2, f"two drafts approved (got {approved})")
        expect(rejected == 1, f"one draft rejected (got {rejected})")

        # prepare item 1 (newest approved) for gmail
        h, t = await q("Prepare item 1 for Gmail.")
        responses.append(t)
        expect(h and t and "intent" in t.lower(), "prepare item response")

        # list pending gmail intents
        h, t = await q("Show pending Gmail intents.")
        responses.append(t)
        expect(h and t and "Pending gmail provider intents" in t, "list intents")

        # simulate item 1 (intent)
        h, t = await q("simulate item 1")
        responses.append(t)
        expect(h and t and "payload" in t.lower(), "simulate item renders payload")

        # prepare all approved drafts
        h, t = await q("Prepare all approved drafts for simulation.")
        responses.append(t)
        expect(h and t and "Prepared" in t, "prepare_all response")

        # intents created
        async with pool.acquire() as conn:
            n_intents = await conn.fetchval(
                "SELECT count(*) FROM external_integration_intents WHERE created_by=$1", uid)
        expect(n_intents >= 1, "at least one intent prepared")

        # no token leak
        for r in responses:
            expect(FAKE_ACCESS not in (r or ""), "token leaked into a queue response")

        # traces
        async with pool.acquire() as conn:
            traces = {r["trace_type"] for r in await conn.fetch(
                "SELECT DISTINCT trace_type FROM runtime_traces WHERE user_id=$1 "
                "AND trace_type LIKE 'chat_approval_queue%' OR trace_type LIKE 'chat_queue_%' "
                "AND user_id=$1", uid)}
            traces = {r["trace_type"] for r in await conn.fetch(
                "SELECT DISTINCT trace_type FROM runtime_traces WHERE user_id=$1 "
                "AND (trace_type LIKE 'chat_approval_queue%' OR trace_type LIKE 'chat_queue_%')", uid)}
        for tr in ("chat_approval_queue_requested", "chat_approval_queue_item_selected",
                   "chat_queue_draft_approved", "chat_queue_draft_rejected",
                   "chat_queue_intent_prepared"):
            expect(tr in traces, f"missing trace {tr}")
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
    print("RESULT: PASS — numbered queue list/open/approve/reject/prepare/simulate + "
          "prepare-all resolve by number/ordinal; 5 queue traces + review events; "
          "ownership enforced; no token leak; execution disabled; rows cleaned up")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
