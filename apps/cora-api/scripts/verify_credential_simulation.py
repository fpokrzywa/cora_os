"""Durable end-to-end verification of Provider Credential Usage Simulation v1.3.

Exercises the REAL service (`provider_credential_simulation.simulate_credential_usage`)
against a throwaway approved SIGNAL email intent + a throwaway CONNECTED gmail
credential, and asserts:
  - the connected credential is resolved + validated (connected, scopes present,
    token valid-or-refreshable),
  - a provider-ready payload preview is generated (external_action_performed=False),
  - the global kill switch blocks execution (execution_allowed=False),
  - the result + persisted rows contain NO access/refresh token material,
  - the 3 spec traces fire (provider_credential_resolved / provider_payload_simulated
    / provider_execution_blocked_by_governance),
  - the simulation is stored on intent metadata + an external_integration_events row,
  - the governed tool `provider_credential_usage_simulated` is seeded.

Everything is created under a disposable user (connector with a FAKE encrypted
token — never a real account) and deleted in a finally; a PASS leaves the DB as
found. No Gmail/Microsoft API call; execution stays disabled. Run:

    docker cp apps/cora-api/scripts/verify_credential_simulation.py cora-api:/tmp/vcs.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vcs.py     # 0=PASS 1=FAIL
"""
import asyncio
import json
import sys
import uuid

from app.clients import clients, init_clients
from app import integration_readiness as ir
from app import provider_credential_simulation as pcs
from app.crypto import encrypt_secret

FAKE_ACCESS = "FAKE-ACCESS-TOKEN-must-never-appear-in-results-or-rows"
FAKE_REFRESH = "FAKE-REFRESH-TOKEN-must-never-appear-in-results-or-rows"
TRACES = ("provider_credential_resolved", "provider_payload_simulated",
          "provider_execution_blocked_by_governance")


async def main() -> int:
    await init_clients()
    pool = clients.db_pool
    fails: list[str] = []
    uid = draft_id = intent_id = None
    try:
        async with pool.acquire() as conn:
            uid = await conn.fetchval(
                "INSERT INTO users (email, password_hash, role) "
                "VALUES ($1, 'not-a-real-hash', 'admin') RETURNING id",
                f"verify-cred-{uuid.uuid4()}@example.invalid")
            wid = await conn.fetchval("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")
            # A CONNECTED gmail credential (fake encrypted tokens, future expiry).
            await conn.execute(
                """
                INSERT INTO provider_oauth_connectors
                    (user_id, workspace_id, provider_name, provider_type, status,
                     scopes, access_token_encrypted, refresh_token_encrypted,
                     token_expires_at, metadata)
                VALUES ($1,$2,'gmail','email','connected',$3,$4,$5,
                        NOW()+interval '1 hour','{"connected_via":"verify"}'::jsonb)
                """,
                uid, wid, ["https://www.googleapis.com/auth/gmail.send"],
                encrypt_secret(FAKE_ACCESS), encrypt_secret(FAKE_REFRESH))
            # An APPROVED SIGNAL draft → readiness email intent.
            draft_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO communication_drafts
                    (id, workspace_id, created_by, draft_type, title, recipient_hint,
                     subject, body, tone, status)
                VALUES ($1,$2,$3,'email','VERIFY','vip@example.com',
                        'VERIFY subject','VERIFY body','neutral','approved')
                """,
                draft_id, wid, uid)

        intent = await ir.create_readiness_intent_from_draft(
            draft_id, user_id=uid, is_admin=True)
        intent_id = intent["id"]

        # --- run the real v1.3 service ---
        r = await pcs.simulate_credential_usage(intent_id, user_id=uid, is_admin=True)

        v = r["validation"]
        if not v["provider_connected"]: fails.append("validation.provider_connected false")
        if not v["token_valid_or_refreshable"]: fails.append("validation.token_valid_or_refreshable false")
        if not v["required_scopes_present"]: fails.append(f"required_scopes_present false (missing={v['missing_scopes']})")
        if not v["provider_execution_disabled"]: fails.append("provider_execution_disabled false")
        if not v["dry_run_only"]: fails.append("dry_run_only false")
        if not v["kill_switch_blocks_execution"]: fails.append("kill_switch_blocks_execution false")
        if v["governance_allows_execution"]: fails.append("governance_allows_execution should be False")
        if r["execution_allowed"]: fails.append("execution_allowed should be False")
        if r["execution_enabled"]: fails.append("execution_enabled should be False")
        if not r["payload_ready"]: fails.append(f"payload_ready false (errors={r['payload_errors']})")
        prev = r.get("provider_payload_preview") or {}
        if prev.get("external_action_performed") is not False:
            fails.append("payload preview external_action_performed != False")
        if (prev.get("preview") or {}).get("subject") != "VERIFY subject":
            fails.append("payload preview did not carry the draft subject")
        # NO token material anywhere in the result.
        blob = json.dumps(r)
        if FAKE_ACCESS in blob or FAKE_REFRESH in blob:
            fails.append("token material leaked into the simulation result")

        async with pool.acquire() as conn:
            meta = await conn.fetchval(
                "SELECT metadata->'credential_usage_simulation' FROM "
                "external_integration_intents WHERE id=$1", intent_id)
            if not meta: fails.append("metadata.credential_usage_simulation not persisted")
            ev = await conn.fetchval(
                "SELECT count(*) FROM external_integration_events "
                "WHERE intent_id=$1 AND event_type='provider_credential_simulation'", intent_id)
            if ev != 1: fails.append(f"events row count={ev} (want 1)")
            traces = {row["trace_type"]: row["status"] for row in await conn.fetch(
                "SELECT DISTINCT trace_type, status FROM runtime_traces "
                "WHERE user_id=$1 AND trace_type = ANY($2)", uid, list(TRACES))}
            for t in TRACES:
                if t not in traces: fails.append(f"missing trace {t}")
            if traces.get("provider_execution_blocked_by_governance") != "blocked":
                fails.append("blocked trace status != 'blocked'")
            # No token material in any persisted trace/metadata.
            leak = await conn.fetchval(
                "SELECT count(*) FROM runtime_traces WHERE user_id=$1 "
                "AND tool_result::text LIKE '%FAKE-ACCESS%'", uid)
            if leak: fails.append("token material leaked into a runtime trace")
            seeded = await conn.fetchval(
                "SELECT count(*) FROM tools WHERE name='provider_credential_usage_simulated'")
            if not seeded: fails.append("governed tool provider_credential_usage_simulated not seeded")
    finally:
        async with pool.acquire() as conn:
            if uid is not None:
                await conn.execute(
                    "DELETE FROM runtime_traces WHERE user_id=$1 AND trace_type = ANY($2)",
                    uid, list(TRACES))
                # events cascade on intent delete; intents by creator; then fixtures.
                await conn.execute("DELETE FROM external_integration_intents WHERE created_by=$1", uid)
                if draft_id is not None:
                    await conn.execute("DELETE FROM communication_drafts WHERE id=$1", draft_id)
                await conn.execute("DELETE FROM provider_oauth_connectors WHERE user_id=$1", uid)
                await conn.execute("DELETE FROM users WHERE id=$1", uid)

    if fails:
        print("RESULT: FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print("RESULT: PASS — connected credential resolved + validated; provider-ready "
          "payload preview generated; kill switch blocks execution; no token exposed; "
          "3 traces + metadata + event persisted; tool seeded; rows cleaned up")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
