"""Durable end-to-end verification of Chat-Native Email Review & Approval v1.9.

Drives the REAL lifecycle via `chat_email_lifecycle.handle_email_command` (the LLM
call is monkeypatched — no egress) under a throwaway admin user + a connected gmail
credential: create -> revise -> approve -> prepare(Gmail) -> simulate -> safety
check, plus reject and the ambiguity/no-active-draft rules. Asserts draft + review
events + intent + traces are written, context resolves "approve it"/"make it
shorter", responses use SAFE labels only (no Send/Execute/Email Now), execution
stays disabled, and no token leaks. Disposable rows cleaned in finally. No provider
API call. Run:

    docker cp apps/cora-api/scripts/verify_chat_email_lifecycle.py cora-api:/tmp/vce.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vce.py     # 0=PASS 1=FAIL
"""
import asyncio
import sys
import uuid

from app.clients import clients, init_clients
from app import chat_email_lifecycle as cel
from app import signal_tools
from app.crypto import encrypt_secret

FAKE_ACCESS = "FAKE-ACCESS-never-leak"
UNSAFE = ("send email", "email now", "execute", " send ", "schedule", "create event")
_EMAIL = "Subject: Project delay\nTo: Mark\n\nHi Mark, the project is delayed by a week. Thanks."


async def main() -> int:
    await init_clients()
    pool = clients.db_pool
    fails: list[str] = []
    uid = None
    sess = uuid.uuid4()
    sess2 = uuid.uuid4()

    def expect(c, m):
        if not c:
            fails.append(m)

    # Monkeypatch the model call — deterministic, no network.
    async def _fake_generate(prompt):
        return _EMAIL
    cel._generate_text = _fake_generate

    async def run(msg, session=sess):
        cmd = cel.detect_email_command(msg)
        if cmd is None:
            return None, None
        return await cel.handle_email_command(
            cmd, message=msg, session_uuid=session, user_id=uid,
            workspace_uuid=wid, scope_type="user", is_admin=True)

    try:
        async with pool.acquire() as conn:
            uid = await conn.fetchval(
                "INSERT INTO users (email, password_hash, role) VALUES ($1,'x','admin') RETURNING id",
                f"verify-cel-{uuid.uuid4()}@example.invalid")
            wid = await conn.fetchval("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")
            await conn.execute(
                "INSERT INTO provider_oauth_connectors (user_id, workspace_id, provider_name, "
                "provider_type, status, scopes, access_token_encrypted, refresh_token_encrypted, "
                "token_expires_at, metadata) VALUES ($1,$2,'gmail','email','connected',$3,$4,$5,"
                "NOW()+interval '1 hour','{}'::jsonb)",
                uid, wid, ["https://www.googleapis.com/auth/gmail.send"],
                encrypt_secret(FAKE_ACCESS), encrypt_secret("r"))

        responses = []

        # 1. create
        h, txt = await run("Draft an email to Mark about the project delay and save it as a draft.")
        responses.append(txt)
        expect(h and txt and "Draft" in txt and "draft" in txt.lower(), "create response")
        ctx = await cel.get_context(sess)
        did = ctx.get("current_active_draft_id")
        expect(did is not None, "create set current_active_draft_id")

        # 2. revise (ambiguous follow-up resolved via context)
        h, txt = await run("Make it shorter and warmer.")
        responses.append(txt)
        expect(h and txt and "Revised" in txt, "revise response")
        d = await signal_tools.get_draft(did)
        expect((d.get("metadata") or {}).get("revision_history"), "revision_history preserved")

        # 3. approve (ambiguous)
        h, txt = await run("Approve it.")
        responses.append(txt)
        d = await signal_tools.get_draft(did)
        expect(h and d["status"] == "approved", f"approve -> status={d['status']}")

        # 4. prepare for Gmail
        h, txt = await run("Prepare it for Gmail.")
        responses.append(txt)
        ctx = await cel.get_context(sess)
        iid = ctx.get("last_integration_intent_id")
        expect(iid is not None, "prepare set last_integration_intent_id")
        if iid:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT provider_name, action_type, dry_run, requires_confirmation "
                    "FROM external_integration_intents WHERE id=$1", iid)
            expect(row["provider_name"] == "gmail", f"intent provider_name={row['provider_name']}")
            expect(row["action_type"] == "send_email" and row["dry_run"] is True
                   and row["requires_confirmation"] is True, "intent non-executing shape")

        # 5. simulate
        h, txt = await run("Simulate the provider payload.")
        responses.append(txt)
        expect(h and txt and "Simulated" in txt and "no API was called" in txt, "simulate response")

        # 6. final safety check
        h, txt = await run("Run the final safety check.")
        responses.append(txt)
        expect(h and txt and "execution remains disabled" in txt.lower(), "safety-check response")

        # 7. reject a fresh draft
        await run("Draft an email to Sam about lunch and save it as a draft.")
        h, txt = await run("Reject it.")
        responses.append(txt)
        ctx = await cel.get_context(sess)
        d2 = await signal_tools.get_draft(ctx["current_active_draft_id"])
        expect(h and d2["status"] == "rejected", f"reject -> status={d2['status']}")

        # 8. ambiguity: "make it shorter" with NO active draft -> NOT handled
        h, txt = await run("Make it shorter.", session=sess2)
        expect(h is False and txt is None, "ambiguous w/o draft must fall through")

        # 9. explicit-no-draft helpful message (req #6)
        h, txt = await run("approve draft", session=sess2)
        expect(h is True and txt and "don't have an active" in txt, "explicit no-draft helpful msg")

        # 10. SAFE language only (req #16/#17) + no token leak (#18)
        for r in responses:
            low = (r or "").lower()
            for bad in UNSAFE:
                expect(bad not in low, f"unsafe label {bad!r} in a response")
            expect(FAKE_ACCESS not in (r or ""), "token leaked into a chat response")

        # 11. traces written
        async with pool.acquire() as conn:
            traces = {r["trace_type"] for r in await conn.fetch(
                "SELECT DISTINCT trace_type FROM runtime_traces WHERE user_id=$1 "
                "AND trace_type LIKE 'chat_email_%'", uid)}
        for t in ("chat_email_draft_created", "chat_email_draft_updated",
                  "chat_email_draft_approved", "chat_email_draft_rejected",
                  "chat_email_intent_prepared", "chat_email_provider_simulated",
                  "chat_email_safety_check_run"):
            expect(t in traces, f"missing trace {t}")
        # review events from chat
        async with pool.acquire() as conn:
            revs = {r["action"] for r in await conn.fetch(
                "SELECT DISTINCT action FROM draft_review_events dre "
                "JOIN communication_drafts d ON d.id = dre.draft_id WHERE d.created_by=$1", uid)}
        for a in ("revise", "approve", "reject"):
            expect(a in revs, f"missing draft_review_events action {a}")
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM chat_email_context WHERE session_id = ANY($1)", [sess, sess2])
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
    print("RESULT: PASS — chat lifecycle create→revise→approve→prepare→simulate→safety "
          "+ reject; context resolves ambiguous follow-ups; safe labels only; no token "
          "leak; execution disabled; 7 traces + review events; rows cleaned up")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
