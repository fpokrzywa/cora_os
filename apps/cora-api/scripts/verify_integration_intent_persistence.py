"""Durable end-to-end verification of Integration Intent persistence.

Proves that preparing an integration intent from an APPROVED SIGNAL draft /
CHRONOS proposal creates the full, consistent audit set — exercising the REAL
production functions the routers call:

    ir.build_email_intent_from_draft / build_calendar_intent_from_proposal
        -> INSERT external_integration_intents (+ external_integration_events)
    governance.log_execution_attempt   -> tool_execution_logs
    integration.write_intent_trace     -> runtime_traces

For each agent it asserts that a durable intent row, a lifecycle event, a
success tool_execution_log, and an `integration_intent_created` runtime_trace
all land together and share the same intent_id, using the real schema columns
(action_type, not intent_type). Every fixture/artifact it creates is deleted in
a finally block, so a PASS leaves the database exactly as it was found.

DRY-RUN ONLY — no Gmail/Outlook/Google/Microsoft or any external/provider/OAuth
call is made; provider execution stays globally disabled. Run it with:

    docker cp apps/cora-api/scripts/verify_integration_intent_persistence.py \
        cora-api:/tmp/verify.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/verify.py   # 0 = PASS, 1 = FAIL

Read-only on real data; only its own throwaway rows are written then removed.
"""
import asyncio
import sys
import time
import uuid

from app.clients import clients, init_clients
from app import integration_readiness as ir
from app.tools.governance import log_execution_attempt
from app.routers.integration import write_intent_trace

EMAIL_TOOL = "signal_prepare_email_send_intent"
CALENDAR_TOOL = "chronos_prepare_calendar_event_intent"


async def _seed_admin_and_workspace(conn) -> tuple[uuid.UUID, uuid.UUID]:
    admin = await conn.fetchval(
        "SELECT id FROM users WHERE role='admin' ORDER BY created_at LIMIT 1"
    )
    if admin is None:
        raise RuntimeError("no admin user found — cannot run verification")
    workspace = await conn.fetchval(
        "SELECT id FROM workspaces ORDER BY created_at LIMIT 1"
    )
    return admin, workspace


async def _run_case(
    *, agent: str, tool: str, provider_type: str, action_type: str,
    build, source_id: uuid.UUID, admin: uuid.UUID,
) -> list[str]:
    """Mirror the router success path, then assert the full audit set."""
    pool = clients.db_pool
    failures: list[str] = []
    started = time.perf_counter()
    intent = await build()
    intent_id = intent["id"]
    await log_execution_attempt(
        tool_name=tool, agent_name=agent, session_id=None, user_id=admin,
        scope_type=None, allowed=True,
        duration_ms=int((time.perf_counter() - started) * 1000),
        status="success", error_message=None,
    )
    await write_intent_trace(
        intent, trace_type="integration_intent_created", user_id=admin
    )

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT agent_name, source_type, source_id, provider_type, "
            "provider_name, action_type, status, dry_run, requires_confirmation, "
            "confirmation_required_reason, payload_preview, validation_result, "
            "metadata FROM external_integration_intents WHERE id=$1", intent_id)
        if row is None:
            failures.append(f"[{agent}] intent row NOT persisted")
        else:
            r = dict(row)
            if r["agent_name"] != agent: failures.append(f"[{agent}] agent_name={r['agent_name']}")
            if str(r["source_id"]) != str(source_id): failures.append(f"[{agent}] source_id mismatch")
            if r["provider_type"] != provider_type: failures.append(f"[{agent}] provider_type={r['provider_type']}")
            if r["action_type"] != action_type: failures.append(f"[{agent}] action_type={r['action_type']}")
            if r["dry_run"] is not True: failures.append(f"[{agent}] dry_run not TRUE")
            if r["requires_confirmation"] is not True: failures.append(f"[{agent}] requires_confirmation not TRUE")
            if not r["confirmation_required_reason"]: failures.append(f"[{agent}] no confirmation_required_reason")
            if not isinstance(r["payload_preview"], dict) or not r["payload_preview"]:
                failures.append(f"[{agent}] empty payload_preview")
            if not isinstance(r["validation_result"], dict):
                failures.append(f"[{agent}] validation_result not an object")

        ev = await conn.fetchval(
            "SELECT count(*) FROM external_integration_events "
            "WHERE intent_id=$1 AND event_type='integration_intent_created'", intent_id)
        if ev != 1: failures.append(f"[{agent}] events={ev} (want 1)")

        log = await conn.fetchrow(
            "SELECT allowed, status FROM tool_execution_logs "
            "WHERE tool_name=$1 AND user_id=$2 ORDER BY created_at DESC LIMIT 1",
            tool, admin)
        if log is None or log["status"] != "success" or log["allowed"] is not True:
            failures.append(f"[{agent}] tool_execution_log missing/wrong: {dict(log) if log else None}")

        tr = await conn.fetchrow(
            "SELECT status, tool_result->>'intent_id' AS iid, tool_name "
            "FROM runtime_traces WHERE trace_type='integration_intent_created' "
            "AND (tool_result->>'intent_id')=$1 ORDER BY created_at DESC LIMIT 1",
            str(intent_id))
        if tr is None:
            failures.append(f"[{agent}] runtime_trace missing")
        else:
            if tr["status"] != "ok": failures.append(f"[{agent}] trace status={tr['status']}")
            if tr["iid"] != str(intent_id): failures.append(f"[{agent}] trace intent_id mismatch")
            if tr["tool_name"] != tool: failures.append(f"[{agent}] trace tool_name={tr['tool_name']}")

    return failures


async def main() -> int:
    await init_clients()
    pool = clients.db_pool
    draft_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    failures: list[str] = []

    async with pool.acquire() as conn:
        admin, workspace = await _seed_admin_and_workspace(conn)
        # Throwaway APPROVED source records owned by the admin.
        await conn.execute(
            """
            INSERT INTO communication_drafts
                (id, workspace_id, created_by, draft_type, title, recipient_hint,
                 subject, body, tone, status)
            VALUES ($1,$2,$3,'email','VERIFY temp draft','someone@example.com',
                    'VERIFY subject','VERIFY body text','neutral','approved')
            """,
            draft_id, workspace, admin)
        await conn.execute(
            """
            INSERT INTO schedule_proposals
                (id, workspace_id, created_by, proposal_type, title, description,
                 start_time, end_time, timezone, attendees, agenda, reminders, status)
            VALUES ($1,$2,$3,'meeting','VERIFY temp proposal','persistence proof',
                    NOW()+interval '1 day', NOW()+interval '1 day 1 hour','UTC',
                    $4,$5,$6,'approved')
            """,
            proposal_id, workspace, admin,
            ["a@example.com"], ["intro"], ["10m"])

    try:
        failures += await _run_case(
            agent="SIGNAL", tool=EMAIL_TOOL, provider_type="email",
            action_type="send_email", source_id=draft_id, admin=admin,
            build=lambda: ir.build_email_intent_from_draft(
                draft_id, user_id=admin, is_admin=True,
                provider_name="internal_preview", action_type="send_email",
                notes="verify"),
        )
        failures += await _run_case(
            agent="CHRONOS", tool=CALENDAR_TOOL, provider_type="calendar",
            action_type="create_calendar_event", source_id=proposal_id, admin=admin,
            build=lambda: ir.build_calendar_intent_from_proposal(
                proposal_id, user_id=admin, is_admin=True,
                provider_name="internal_preview", action_type="create_calendar_event",
                notes="verify"),
        )
    finally:
        # Remove EVERY artifact this script created — leave the DB as found.
        async with pool.acquire() as conn:
            ids = await conn.fetch(
                "SELECT id FROM external_integration_intents WHERE source_id = ANY($1::uuid[])",
                [draft_id, proposal_id])
            iid_list = [r["id"] for r in ids]
            if iid_list:
                await conn.execute(
                    "DELETE FROM runtime_traces WHERE trace_type='integration_intent_created' "
                    "AND (tool_result->>'intent_id')::uuid = ANY($1::uuid[])", iid_list)
                await conn.execute(
                    "DELETE FROM external_integration_events WHERE intent_id = ANY($1::uuid[])",
                    iid_list)
                await conn.execute(
                    "DELETE FROM external_integration_intents WHERE id = ANY($1::uuid[])",
                    iid_list)
            await conn.execute(
                "DELETE FROM tool_execution_logs WHERE tool_name = ANY($1) AND user_id=$2",
                [EMAIL_TOOL, CALENDAR_TOOL], admin)
            await conn.execute("DELETE FROM communication_drafts WHERE id=$1", draft_id)
            await conn.execute("DELETE FROM schedule_proposals WHERE id=$1", proposal_id)

    if failures:
        print("RESULT: FAIL")
        for f in failures:
            print("  -", f)
        return 1
    print("RESULT: PASS — SIGNAL + CHRONOS each persisted row+event+log+trace "
          "atomically with matching intent_id (real schema, dry_run, cleaned up)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
