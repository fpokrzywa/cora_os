"""Durable end-to-end verification of Chat-Native Provider Simulation v2.1.

Sets up a prepared Gmail intent under a throwaway admin (via the v1.9 handlers),
then exercises the v2.1 inspection/comparison commands and asserts the rendered
payload + summary fields, the comparison, the 3 traces + 2 integration events, and
that NO token material leaks and execution stays disabled. Disposable rows cleaned
in finally. No provider API call. Run:

    docker cp apps/cora-api/scripts/verify_chat_provider_simulation.py cora-api:/tmp/vcp.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vcp.py     # 0=PASS 1=FAIL
"""
import asyncio
import sys
import uuid

from app.clients import clients, init_clients
from app import chat_email_lifecycle as cel
from app import chat_provider_simulation as cps
from app.crypto import encrypt_secret

FAKE_ACCESS = "FAKE-ACCESS-never-leak"
_EMAIL = "Subject: Project delay\nTo: Mark\n\nHi Mark, the project is delayed. Thanks."


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

    async def _fake_generate(prompt):
        return _EMAIL
    cel._generate_text = _fake_generate

    async def email(msg):
        cmd = cel.detect_email_command(msg)
        return await cel.handle_email_command(
            cmd, message=msg, session_uuid=sess, user_id=uid, workspace_uuid=wid,
            scope_type="user", is_admin=True)

    async def sim(msg, session=sess):
        cmd = cps.detect_simulation_command(msg)
        if cmd is None:
            return None, None
        return await cps.handle_simulation_command(
            cmd, message=msg, session_uuid=session, user_id=uid, workspace_uuid=wid,
            is_admin=True)

    # detection unit checks
    expect(cps.detect_simulation_command("Show me exactly what Gmail would receive.") == ("inspect", "gmail"), "detect inspect gmail")
    expect(cps.detect_simulation_command("Simulate this email.") == ("inspect", None), "detect simulate generic")
    expect(cps.detect_simulation_command("Compare Gmail and Outlook payloads.") == ("compare", None), "detect compare")
    expect(cps.detect_simulation_command("What would be sent if execution were enabled?") == ("inspect", None), "detect what-would-be-sent")
    expect(cps.detect_simulation_command("inspect outlook payload") == ("inspect", "outlook_mail"), "detect inspect outlook")
    expect(cps.detect_simulation_command("What's for lunch?") is None, "non-sim must be None")

    try:
        async with pool.acquire() as conn:
            uid = await conn.fetchval(
                "INSERT INTO users (email, password_hash, role) VALUES ($1,'x','admin') RETURNING id",
                f"verify-cp-{uuid.uuid4()}@example.invalid")
            wid = await conn.fetchval("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")
            await conn.execute(
                "INSERT INTO provider_oauth_connectors (user_id, workspace_id, provider_name, "
                "provider_type, status, scopes, access_token_encrypted, refresh_token_encrypted, "
                "token_expires_at, metadata) VALUES ($1,$2,'gmail','email','connected',$3,$4,$5,"
                "NOW()+interval '1 hour','{}'::jsonb)",
                uid, wid, ["https://www.googleapis.com/auth/gmail.send"],
                encrypt_secret(FAKE_ACCESS), encrypt_secret("r"))

        # set up a prepared gmail intent
        await email("Draft an email to Mark about the project delay and save it as a draft.")
        await email("Approve it.")
        await email("Prepare it for Gmail.")

        responses = []

        # 1. inspect gmail
        h, t = await sim("Show me exactly what Gmail would receive.")
        responses.append(t)
        expect(h and t and "Gmail payload" in t and "Recipients:" in t and "Subject:" in t
               and "Body preview:" in t and "Attachments:" in t and "Validation:" in t,
               "inspect gmail render fields")
        expect(t and "OAuth status" in t and "Feature flag" in t and "Final interlock" in t
               and "Provider ready" in t, "inspect summary fields")
        expect(t and "gmail.users.messages.send" in t, "inspect api method")
        expect(t and "would_send: **False**" in t, "inspect would_send false")

        # 2. simulate (resolved provider)
        h, t = await sim("Simulate this email.")
        responses.append(t)
        expect(h and t and "Simulated Gmail payload" in t, "simulate resolved provider")

        # 3. compare
        h, t = await sim("Compare Gmail and Outlook payloads.")
        responses.append(t)
        expect(h and t and "Gmail vs Outlook" in t and "graph.me.sendMail" in t
               and "gmail.users.messages.send" in t, "compare render")
        expect(t and "disabled" in t.lower(), "compare safety note")

        # 4. what-would-be-sent
        h, t = await sim("What would be sent if execution were enabled?")
        responses.append(t)
        expect(h and t and "disabled" in t.lower(), "what-would-be-sent safety")

        # 5. no-intent helpful message
        h, t = await sim("Simulate this email.", session=sess2)
        responses.append(t)
        expect(h and t and "no prepared provider intent" in t.lower(), "no-intent helpful")

        # no token leak
        for r in responses:
            expect(FAKE_ACCESS not in (r or ""), "token leaked into a simulation response")

        # traces + events
        async with pool.acquire() as conn:
            traces = {r["trace_type"] for r in await conn.fetch(
                "SELECT DISTINCT trace_type FROM runtime_traces WHERE user_id=$1 "
                "AND trace_type LIKE 'chat_provider_%'", uid)}
            ev = {r["event_type"] for r in await conn.fetch(
                "SELECT DISTINCT e.event_type FROM external_integration_events e "
                "JOIN external_integration_intents i ON i.id=e.intent_id WHERE i.created_by=$1 "
                "AND e.event_type IN ('provider_payload_viewed','provider_simulation_generated')", uid)}
        for tr in ("chat_provider_simulation_requested", "chat_provider_payload_inspected",
                   "chat_provider_comparison_generated"):
            expect(tr in traces, f"missing trace {tr}")
        for e in ("provider_payload_viewed", "provider_simulation_generated"):
            expect(e in ev, f"missing event {e}")
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
    print("RESULT: PASS — inspect (gmail) renders all payload+summary fields; simulate "
          "resolves provider; Gmail-vs-Outlook comparison; no-intent helpful; safe/no-send; "
          "3 traces + 2 events; no token leak; execution disabled; rows cleaned up")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
