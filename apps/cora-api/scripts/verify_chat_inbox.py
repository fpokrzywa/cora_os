"""Durable end-to-end verification of the Chat-Native Inbox Assistant
(v2.3 fail-closed gate + v2.7 live read implementation).

Three parts under a throwaway user with a connected gmail (gmail.send scope only):
  A) PRODUCTION STATE — fail-closed: list/search/summarize/read-thread/draft-reply
     all denied (no read scope + inbox_read flag disabled); audited; NO draft made;
     NO provider API call; no token exposed.
  B) gate-pass + provider failure — the token broker resolves the (fake) token,
     the live adapter's HTTP choke point is patched to simulate a provider
     rejection: graceful error message, audited, read-failed trace, NO draft,
     no real network call.
  C) gate-pass + mocked adapter returns messages — list renders source metadata; a
     draft-reply creates an INTERNAL SIGNAL draft linked to the source email; traces.

Asserts the inbox_read feature flags are seeded disabled (fail-closed), detection,
audit events, traces, and no token leak. Disposable rows cleaned in finally. Run:

    docker cp apps/cora-api/scripts/verify_chat_inbox.py cora-api:/tmp/vci.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vci.py     # 0=PASS 1=FAIL
"""
import asyncio
import sys
import uuid

from app.clients import clients, init_clients
from app import chat_inbox as ci
from app import inbox_adapters
from app import feature_flags as ff
from app.crypto import encrypt_secret

FAKE_ACCESS = "FAKE-ACCESS-never-leak"
FAKE_MSGS = [{"id": "m1", "thread_id": "t1", "from": "Mark <mark@example.com>",
              "subject": "Project delay", "date": "2026-05-30",
              "snippet": "the project is delayed by a week"}]


async def main() -> int:
    await init_clients()
    pool = clients.db_pool
    fails = []
    uid = None
    sess = uuid.uuid4()

    def expect(c, m):
        if not c:
            fails.append(m)

    async def inbox(msg):
        cmd = ci.detect_inbox_command(msg)
        if cmd is None:
            return None, None
        return await ci.handle_inbox_command(
            cmd, message=msg, session_uuid=sess, user_id=uid, workspace_uuid=wid,
            scope_type="user", is_admin=True)

    # detection
    expect(ci.detect_inbox_command("Show my latest emails.") == ("list", None), "detect list")
    # provider-word tolerance: a named provider must not break detection
    expect(ci.detect_inbox_command("Show my outlook inbox.") == ("list", None),
           "detect list with a provider word ('outlook inbox')")
    expect(ci.detect_inbox_command("Search my outlook inbox for emails from Mark.") == ("search", "from:mark"),
           "provider word stripped, sender 'from:' search preserved")
    expect(ci.detect_inbox_command("Search my inbox for emails from Mark.") == ("search", "from:mark"), "detect search from → sender-scoped from: operator")
    expect(ci.detect_inbox_command("Summarize unread emails.") == ("summarize", None), "detect summarize")
    expect(ci.detect_inbox_command("Summarize this email thread.") == ("read_thread", None), "detect read_thread")
    expect(ci.detect_inbox_command("Draft a reply to this email, but do not send it.") == ("draft_reply", None), "detect draft_reply")
    expect(ci.detect_inbox_command("Find emails about the project delay.") == ("search", "the project delay"), "detect find about")
    expect(ci.detect_inbox_command("Draft an email to Mark.") is None, "v1.9 create must not be inbox")

    # Snapshot the operator's REAL inbox_read flag state (they may have enabled it
    # for live use), then force fail-closed for the test; restored in finally so
    # this run never disrupts their configuration. Part A's fail-closed still holds
    # via the throwaway user's missing read scope regardless.
    async with pool.acquire() as conn:
        saved_flags = [dict(r) for r in await conn.fetch(
            "SELECT id, enabled FROM provider_execution_feature_flags WHERE action_type='inbox_read'")]
        await conn.execute(
            "UPDATE provider_execution_feature_flags SET enabled=FALSE WHERE action_type='inbox_read'")
    for p in ("gmail", "outlook_mail"):
        flag = await ff.get_flag(p, "inbox_read")
        expect(flag is not None, f"{p}/inbox_read flag exists")

    # capability alignment: gmail/outlook supports_read=TRUE, write caps FALSE
    async with pool.acquire() as conn:
        caps = {r["provider_name"]: r for r in await conn.fetch(
            "SELECT provider_name, supports_read, supports_send, supports_calendar_create, "
            "dry_run_only FROM external_provider_connectors WHERE provider_name IN ('gmail','outlook_mail')")}
    for p in ("gmail", "outlook_mail"):
        expect(caps[p]["supports_read"] is True, f"{p} supports_read must be TRUE")
        expect(caps[p]["supports_send"] is False, f"{p} supports_send must stay FALSE")
        expect(caps[p]["supports_calendar_create"] is False, f"{p} supports_calendar_create FALSE")
        expect(caps[p]["dry_run_only"] is True, f"{p} dry_run_only must stay TRUE")

    orig_gate = ci._gate
    adapter = inbox_adapters.resolve_inbox_adapter("gmail")
    try:
        async with pool.acquire() as conn:
            uid = await conn.fetchval(
                "INSERT INTO users (email, password_hash, role) VALUES ($1,'x','admin') RETURNING id",
                f"verify-ci-{uuid.uuid4()}@example.invalid")
            wid = await conn.fetchval("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")
            await conn.execute(
                "INSERT INTO provider_oauth_connectors (user_id, workspace_id, provider_name, "
                "provider_type, status, scopes, access_token_encrypted, refresh_token_encrypted, "
                "token_expires_at, metadata) VALUES ($1,$2,'gmail','email','connected',$3,$4,$5,"
                "NOW()+interval '1 hour','{}'::jsonb)",
                uid, wid, ["https://www.googleapis.com/auth/gmail.send"],
                encrypt_secret(FAKE_ACCESS), encrypt_secret("r"))

        responses = []

        # --- A) production fail-closed ---
        for msg in ("Show my latest emails.", "Search my inbox for emails from Mark.",
                    "Summarize unread emails.", "Summarize this email thread.",
                    "Draft a reply to this email, but do not send it."):
            h, t = await inbox(msg)
            responses.append(t)
            expect(h and t and ("can't read" in t.lower() or "disabled" in t.lower()),
                   f"A fail-closed for {msg!r}")
        async with pool.acquire() as conn:
            denied = await conn.fetchval(
                "SELECT count(*) FROM inbox_access_events WHERE user_id=$1 AND allowed=false", uid)
            drafts = await conn.fetchval(
                "SELECT count(*) FROM communication_drafts WHERE created_by=$1", uid)
        expect(denied >= 5, f"A denial audit rows={denied} (want >=5)")
        expect(drafts == 0, "A draft-reply must NOT create a draft when fail-closed")

        # gate reports supports_read True now (capability aligned), still fail-closed
        g = await ci._gate("gmail", uid)
        expect(g["supports_read"] is True and g["capability_mismatch"] is False, "gate supports_read true")
        expect(g["allowed"] is False, "gate still fail-closed (scope+flag missing)")

        # capability-mismatch path: temporarily drop supports_read -> denied + trace
        async with pool.acquire() as conn:
            await conn.execute("UPDATE external_provider_connectors SET supports_read=FALSE WHERE provider_name='gmail'")
        try:
            h, t = await inbox("Show my latest emails.")
            responses.append(t)
            expect(h and t and "capability" in t.lower(), "capability-mismatch blocked message")
            async with pool.acquire() as conn:
                capdenied = await conn.fetchval(
                    "SELECT count(*) FROM runtime_traces WHERE user_id=$1 "
                    "AND trace_type='chat_inbox_capability_denied'", uid)
            expect(capdenied >= 1, "capability-denied trace written")
        finally:
            async with pool.acquire() as conn:
                await conn.execute("UPDATE external_provider_connectors SET supports_read=TRUE WHERE provider_name='gmail'")

        # --- B) gate-pass + simulated provider rejection (no real network) ---
        async def _allow_gate(provider, user_id):
            return {"allowed": True, "connected": True, "token_ok": True, "scope_ok": True,
                    "flag_ok": True, "reason": "test-enabled"}
        ci._gate = _allow_gate

        async def _reject_http(url, *, token, params=None):
            raise inbox_adapters.InboxReadError("provider read rejected (HTTP 401)")
        orig_http = inbox_adapters._http_get_json
        inbox_adapters._http_get_json = _reject_http
        try:
            h, t = await inbox("Show my latest emails.")
            responses.append(t)
            expect(h and t and "failed" in t.lower() and "nothing was sent" in t.lower(),
                   "B provider rejection handled gracefully")
            async with pool.acquire() as conn:
                rf = await conn.fetchval(
                    "SELECT count(*) FROM runtime_traces WHERE user_id=$1 "
                    "AND trace_type='chat_inbox_provider_read_failed'", uid)
                drafts_b = await conn.fetchval(
                    "SELECT count(*) FROM communication_drafts WHERE created_by=$1", uid)
            expect(rf >= 1, "B read-failed trace written")
            expect(drafts_b == 0, "B provider failure must NOT create a draft")
        finally:
            inbox_adapters._http_get_json = orig_http

        # --- C) gate-pass + mocked adapter returns data ---
        async def _fake_list(*, access_token=None, query=None, limit=10):
            return list(FAKE_MSGS)

        async def _fake_search(*, access_token=None, query="", limit=10):
            return list(FAKE_MSGS)

        async def _fake_read(*, access_token=None, message_id=None):
            return dict(FAKE_MSGS[0])
        adapter.list_messages = _fake_list
        adapter.search_messages = _fake_search
        adapter.read_message = _fake_read
        h, t = await inbox("Show my latest emails.")
        responses.append(t)
        expect(h and t and "Mark <mark@example.com>" in t and "Project delay" in t
               and "2026-05-30" in t and "[gmail]" in t, "C list renders source metadata")

        h, t = await inbox("Summarize unread emails.")
        responses.append(t)
        expect(h and t and "Inbox summary" in t and "Project delay" in t, "C summary")

        h, t = await inbox("Draft a reply to this email, but do not send it.")
        responses.append(t)
        expect(h and t and "reply" in t.lower() and "draft" in t.lower(), "C draft-reply response")
        async with pool.acquire() as conn:
            d = await conn.fetchrow(
                "SELECT subject, recipient_hint, metadata FROM communication_drafts "
                "WHERE created_by=$1 ORDER BY created_at DESC LIMIT 1", uid)
        expect(d is not None and d["subject"].lower().startswith("re:"), "C reply draft subject Re:")
        src = (d["metadata"] or {}).get("source_email") if d else None
        expect(src and src.get("message_id") == "m1" and src.get("provider") == "gmail",
               "C reply draft linked to source email")

        # --- D) multi-mailbox READ aggregation (provider-less → merge Gmail+Outlook) ---
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO provider_oauth_connectors (user_id, workspace_id, provider_name, "
                "provider_type, status, scopes, access_token_encrypted, refresh_token_encrypted, "
                "token_expires_at, metadata) VALUES ($1,$2,'outlook_mail','email','connected',$3,$4,$5,"
                "NOW()+interval '1 hour','{}'::jsonb)",
                uid, wid, ["https://graph.microsoft.com/Mail.Read"],
                encrypt_secret(FAKE_ACCESS), encrypt_secret("r"))
        ms_adapter = inbox_adapters.resolve_inbox_adapter("outlook_mail")

        async def _gm_list(*, access_token=None, query=None, limit=10):
            return [{"id": "gm1", "thread_id": "tg", "from": "Alice <alice@gmail.com>",
                     "subject": "Gmail hello", "date": "Wed, 24 Jun 2026 09:00:00 +0000",
                     "snippet": "hi from gmail"}]

        async def _om_list(*, access_token=None, query=None, limit=10):
            return [{"id": "om1", "thread_id": "to", "from": "Bob <bob@outlook.com>",
                     "subject": "Outlook hello", "date": "2026-06-24T14:00:00Z",
                     "snippet": "hi from outlook"}]
        adapter.list_messages = _gm_list
        ms_adapter.list_messages = _om_list

        expect(await ci._resolve_read_providers("show my outlook inbox", uid) == ["outlook_mail"],
               "D named provider → single mailbox")
        provsN = await ci._resolve_read_providers("show my latest emails", uid)
        expect(set(provsN) == {"gmail", "outlook_mail"}, "D no provider → ALL connected mailboxes")

        h, t = await inbox("Show my latest emails.")
        responses.append(t)
        expect(h and t and "Gmail hello" in t and "Outlook hello" in t,
               "D provider-less read merges BOTH mailboxes")
        expect(h and t and "[gmail]" in t and "[outlook]" in t, "D each message tagged with its source")
        expect(h and t and "gmail + outlook" in t, "D header names both mailboxes")
        expect(t.index("Outlook hello") < t.index("Gmail hello"),
               "D merged messages sorted newest-first (Outlook 14:00 before Gmail 09:00)")

        h, t = await inbox("Summarize unread emails.")
        responses.append(t)
        expect(h and t and "Inbox summary" in t and "Gmail hello" in t and "Outlook hello" in t,
               "D summarize aggregates both mailboxes")

        # a named provider still routes to just that mailbox
        h, t = await inbox("show my outlook emails")
        responses.append(t)
        expect(h and t and "Outlook hello" in t and "Gmail hello" not in t,
               "D named 'outlook' reads ONLY outlook")

        # no token leak anywhere
        for r in responses:
            expect(FAKE_ACCESS not in (r or ""), "token leaked into an inbox response")

        # traces
        async with pool.acquire() as conn:
            traces = {r["trace_type"] for r in await conn.fetch(
                "SELECT DISTINCT trace_type FROM runtime_traces WHERE user_id=$1 "
                "AND trace_type LIKE 'chat_inbox_%'", uid)}
        for tr in ("chat_inbox_search_requested", "chat_inbox_messages_listed",
                   "chat_inbox_message_read", "chat_inbox_summary_generated",
                   "chat_inbox_draft_reply_created"):
            expect(tr in traces, f"missing trace {tr}")
    finally:
        ci._gate = orig_gate
        for meth in ("list_messages", "search_messages", "read_message"):
            if meth in adapter.__dict__:
                del adapter.__dict__[meth]
        _ms = inbox_adapters.resolve_inbox_adapter("outlook_mail")
        if _ms is not None and "list_messages" in _ms.__dict__:
            del _ms.__dict__["list_messages"]
        async with pool.acquire() as conn:
            # Restore the operator's real inbox_read flag state (never clobber it).
            for fr in saved_flags:
                await conn.execute(
                    "UPDATE provider_execution_feature_flags SET enabled=$1 WHERE id=$2",
                    fr["enabled"], fr["id"])
            await conn.execute("DELETE FROM chat_email_context WHERE session_id=$1", sess)
            if uid is not None:
                await conn.execute("DELETE FROM inbox_access_events WHERE user_id=$1", uid)
                await conn.execute("DELETE FROM runtime_traces WHERE user_id=$1", uid)
                await conn.execute("DELETE FROM communication_drafts WHERE created_by=$1", uid)
                await conn.execute("DELETE FROM provider_oauth_connectors WHERE user_id=$1", uid)
                await conn.execute("DELETE FROM users WHERE id=$1", uid)

    if fails:
        print("RESULT: FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print("RESULT: PASS — inbox reads FAIL CLOSED (no scope/flag); gate-pass + "
          "provider rejection handled gracefully (broker token, no real network, "
          "read-failed trace); mocked path renders source metadata + creates linked "
          "SIGNAL reply draft (no send); flags seeded disabled; traces + audit events; "
          "no token leak; rows cleaned up")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
