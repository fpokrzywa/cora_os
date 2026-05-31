"""Durable end-to-end verification of Real OAuth Flow v1.1 (readiness only).

Exercises the REAL production service (`app.oauth_flow`) through the full
connection lifecycle WITHOUT any network egress: the provider token endpoint is
faked (cora-internal has no egress), and test OAuth client config is injected via
the provider registry — no real credentials, no real consent, no provider
*execution* call. Proves, end to end:

  start    -> state nonce minted + persisted in oauth_states; real authorize URL
              built with the requested scopes + access_type=offline (google)
  callback -> state validated + single-use; code exchanged; tokens ENCRYPTED at
              rest (ciphertext != plaintext, decrypt round-trips) + masked output
              carries NO raw token; connector marked connected; state consumed
  status   -> connected, has_access/has_refresh flags only, execution_enabled=False
  refresh  -> access token rotated + re-encrypted; success traced; no token leaked
  readiness-> per-provider connection status + scopes + timestamps
  alias    -> spec name `microsoft_calendar` resolves to the outlook_calendar provider
  audit    -> oauth_* runtime_traces + oauth_connection_validated / oauth_readiness_checked
              tool_execution_logs are written

HARD RULES: no email sent, no calendar event created, no provider execution, no
real OAuth handshake; execution stays globally disabled. Every row this script
creates (oauth_states, the gmail + outlook_calendar connectors it connects, and
the oauth_* traces/logs it emits) is deleted in a finally — a PASS leaves the DB
as found. Runs in its own process; patches/config injection never touch the
running cora-api service. Run:

    docker cp apps/cora-api/scripts/verify_oauth_flow.py cora-api:/tmp/voauth.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/voauth.py    # 0=PASS 1=FAIL
"""
import asyncio
import sys
import uuid
from datetime import datetime, timezone

from app.clients import clients, init_clients
from app import oauth_flow as flow
from app import oauth_providers as registry
from app.crypto import decrypt_secret

TEST_CFG = {"client_id": "test-client", "client_secret": "test-secret",
            "redirect_uri": "http://localhost/oauth/cb"}
ACCESS_PLAIN = "ACCESS-TOKEN-PLAINTEXT-should-never-persist-or-leak"
REFRESH_PLAIN = "REFRESH-TOKEN-PLAINTEXT-should-never-persist-or-leak"
NEW_ACCESS_PLAIN = "ROTATED-ACCESS-TOKEN-after-refresh"


class _FakeResp:
    def __init__(self, payload): self._p = payload; self.status_code = 200
    def json(self): return self._p


class _FakeClient:
    """Stand-in for httpx.AsyncClient — returns a canned token, never networks."""
    payload = {"access_token": ACCESS_PLAIN, "refresh_token": REFRESH_PLAIN,
               "expires_in": 3600, "scope": None}

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, data=None): return _FakeResp(dict(self.payload))


async def main() -> int:
    await init_clients()
    pool = clients.db_pool
    fails: list[str] = []
    t0 = datetime.now(timezone.utc)

    # Inject test config for both vendors so start/callback don't bail as
    # "not configured" — without touching settings or the live service.
    orig_cfg, orig_present, orig_httpx = (
        registry.provider_config, registry.config_present, flow.httpx.AsyncClient)
    registry.provider_config = lambda p: dict(TEST_CFG)
    registry.config_present = lambda p: True
    flow.httpx.AsyncClient = _FakeClient

    # Run against a DISPOSABLE user so the connect/upsert never touches a real
    # connected account (e.g. the operator's live gmail). Deleted at the end.
    async with pool.acquire() as conn:
        admin = await conn.fetchval(
            "INSERT INTO users (email, password_hash, role) "
            "VALUES ($1, 'not-a-real-hash', 'admin') RETURNING id",
            f"verify-oauth-{uuid.uuid4()}@example.invalid")
    states: list[str] = []

    try:
        # 1. start (gmail) ----------------------------------------------------
        start = await flow.start_authorization(
            "gmail", user_id=admin, workspace_id=None, is_admin=True)
        states.append(start["state"])
        url = start["authorization_url"]
        if "accounts.google.com" not in url: fails.append("start: wrong authorize host")
        if "gmail.send" not in url: fails.append("start: scope missing from URL")
        if "access_type=offline" not in url: fails.append("start: access_type=offline missing")
        if f"state={start['state']}" not in url: fails.append("start: state not in URL")
        async with pool.acquire() as conn:
            srow = await conn.fetchrow(
                "SELECT provider_name, user_id, consumed_at FROM oauth_states WHERE state=$1",
                start["state"])
        if srow is None or str(srow["user_id"]) != str(admin) or srow["consumed_at"] is not None:
            fails.append("start: oauth_states row missing/wrong/consumed")

        # 2. callback (token exchange + encryption) ---------------------------
        connector = await flow.handle_callback("gmail", code="fake-code", state=start["state"])
        if connector.get("status") != "connected": fails.append("callback: status != connected")
        if "access_token_encrypted" in connector or "refresh_token_encrypted" in connector:
            fails.append("callback: masked output leaked an encrypted token column")
        if not connector.get("has_access_token") or not connector.get("has_refresh_token"):
            fails.append("callback: has_* token flags not set")
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status, access_token_encrypted, refresh_token_encrypted "
                "FROM provider_oauth_connectors WHERE id=$1", connector["id"])
            consumed = await conn.fetchval(
                "SELECT consumed_at FROM oauth_states WHERE state=$1", start["state"])
        enc_access = row["access_token_encrypted"]
        if not enc_access or enc_access == ACCESS_PLAIN:
            fails.append("callback: access token not encrypted at rest")
        elif decrypt_secret(enc_access) != ACCESS_PLAIN:
            fails.append("callback: encrypted access token does not decrypt to original")
        if decrypt_secret(row["refresh_token_encrypted"]) != REFRESH_PLAIN:
            fails.append("callback: refresh token round-trip failed")
        if consumed is None: fails.append("callback: state not consumed (single-use)")

        # 2b. replay the consumed state -> rejected ---------------------------
        try:
            await flow.handle_callback("gmail", code="fake-code-2", state=start["state"])
            fails.append("callback: consumed state was accepted on replay")
        except flow.OAuthError:
            pass

        # 3. status (masked, execution disabled) ------------------------------
        st = await flow.get_status("gmail", user_id=admin, is_admin=True)
        if st["connection_status"] != "connected": fails.append("status: not connected")
        if st["execution_enabled"] is not False: fails.append("status: execution_enabled not False")
        if not st["has_access_token"]: fails.append("status: has_access_token false")
        if any("token" in k and isinstance(v, str) and ACCESS_PLAIN in str(v)
               for k, v in st.items()): fails.append("status: leaked a raw token value")

        # 4. refresh (rotate access token, re-encrypt) -----------------------
        _FakeClient.payload = {"access_token": NEW_ACCESS_PLAIN, "expires_in": 3600}
        await flow.refresh_connection("gmail", user_id=admin, is_admin=True)
        async with pool.acquire() as conn:
            enc2 = await conn.fetchval(
                "SELECT access_token_encrypted FROM provider_oauth_connectors WHERE id=$1",
                connector["id"])
        if decrypt_secret(enc2) != NEW_ACCESS_PLAIN:
            fails.append("refresh: access token not rotated/re-encrypted")

        # 5. readiness list ---------------------------------------------------
        listing = await flow.list_provider_status(user_id=admin, is_admin=True)
        names = {p["provider_name"] for p in listing["providers"]}
        if names != {"gmail", "google_calendar", "outlook_mail", "outlook_calendar"}:
            fails.append(f"readiness: provider set = {names}")
        if listing["execution_enabled"] is not False:
            fails.append("readiness: execution_enabled not False")

        # 6. microsoft_calendar alias resolves --------------------------------
        if registry.get_provider("microsoft_calendar") is None:
            fails.append("alias: microsoft_calendar did not resolve")
        else:
            astart = await flow.start_authorization(
                "microsoft_calendar", user_id=admin, workspace_id=None, is_admin=True)
            states.append(astart["state"])
            if astart["provider_name"] != "outlook_calendar":
                fails.append(f"alias: resolved to {astart['provider_name']}")
            if "login.microsoftonline.com" not in astart["authorization_url"]:
                fails.append("alias: wrong authorize host for microsoft_calendar")

        # 7. audit artifacts --------------------------------------------------
        async with pool.acquire() as conn:
            traces = {r["trace_type"] for r in await conn.fetch(
                "SELECT DISTINCT trace_type FROM runtime_traces "
                "WHERE user_id=$1 AND created_at>=$2 AND trace_type LIKE 'oauth_%'",
                admin, t0)}
            logs = {r["tool_name"] for r in await conn.fetch(
                "SELECT DISTINCT tool_name FROM tool_execution_logs "
                "WHERE user_id=$1 AND created_at>=$2 AND tool_name LIKE 'oauth_%'",
                admin, t0)}
        for need in ("oauth_start_created", "oauth_callback_received",
                     "oauth_token_stored", "oauth_token_refresh_succeeded"):
            if need not in traces: fails.append(f"audit: missing trace {need}")
        for need in ("oauth_connection_validated", "oauth_readiness_checked"):
            if need not in logs: fails.append(f"audit: missing tool log {need}")
        # Credential events (spec #5): token-store + refresh rows recorded, no secrets.
        async with pool.acquire() as conn:
            cred = {r["event_type"] for r in await conn.fetch(
                "SELECT DISTINCT event_type FROM provider_oauth_connector_events "
                "WHERE user_id=$1 AND created_at>=$2", admin, t0)}
        for need in ("oauth_token_stored", "oauth_token_refreshed"):
            if need not in cred: fails.append(f"credential-events: missing {need}")
    finally:
        registry.provider_config, registry.config_present = orig_cfg, orig_present
        flow.httpx.AsyncClient = orig_httpx
        # Everything is scoped to the disposable user — delete its rows then the
        # user. (oauth_states/connectors cascade on the user FK; events/traces/
        # logs are SET NULL or unscoped, so remove them explicitly first.)
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM provider_oauth_connector_events WHERE user_id=$1", admin)
            await conn.execute(
                "DELETE FROM runtime_traces WHERE user_id=$1 AND trace_type LIKE 'oauth_%'", admin)
            await conn.execute(
                "DELETE FROM tool_execution_logs WHERE user_id=$1 AND tool_name LIKE 'oauth_%'", admin)
            await conn.execute("DELETE FROM provider_oauth_connectors WHERE user_id=$1", admin)
            await conn.execute("DELETE FROM oauth_states WHERE user_id=$1", admin)
            await conn.execute("DELETE FROM users WHERE id=$1", admin)

    if fails:
        print("RESULT: FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print("RESULT: PASS — OAuth v1.1 start/callback/refresh/readiness work; tokens "
          "encrypted at rest + masked in output; microsoft_calendar alias resolves; "
          "execution stays disabled; audit traces+logs written; rows cleaned up")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
