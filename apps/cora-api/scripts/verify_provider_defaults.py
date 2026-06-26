"""Verification of multi-provider command helpers (app/provider_defaults.py).

  A) strip_provider_adjectives — removes a provider word before a domain noun, keeps a
     provider word used as a search term ("emails from outlook").
  B) strip_provider_words — cleans a calendar-name hint of provider words.
  C) detect_default_command — conservative detection of "make X my default <type>".
  D) get/set_default + resolve — keyword-less resolution prefers the connected default,
     else most-recently connected, else the fallback; an unconnected default is ignored.

Throwaway user + disposable connector rows, cleaned in finally. Run:

    docker cp apps/cora-api/scripts/verify_provider_defaults.py cora-api:/tmp/vpd.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vpd.py     # 0=PASS 1=FAIL
"""
import asyncio
import sys
import uuid

from app.clients import clients, init_clients
from app import provider_defaults as pd


async def main() -> int:
    await init_clients()
    pool = clients.db_pool
    fails = []
    uid = None

    def expect(c, m):
        if not c:
            fails.append(m)

    # A) provider-adjective stripping
    expect(pd.strip_provider_adjectives("whats on my outlook calendar") == "whats on my calendar",
           "A strip 'outlook' before 'calendar'")
    expect(pd.strip_provider_adjectives("show my google inbox") == "show my inbox",
           "A strip 'google' before 'inbox'")
    expect(pd.strip_provider_adjectives("search my inbox for emails from outlook")
           == "search my inbox for emails from outlook",
           "A keep 'outlook' when it is a search term (not before a domain noun)")
    expect(pd.strip_provider_adjectives("what's on my calendar") == "what's on my calendar",
           "A no provider word → unchanged")

    # B) hint cleaning
    expect(pd.strip_provider_words("outlook") == "", "B bare provider word → empty")
    expect(pd.strip_provider_words("outlook Work") == "Work", "B provider + name → name")
    expect(pd.strip_provider_words("Work") == "Work", "B plain name unchanged")

    # C) default-command detection (conservative)
    expect(pd.detect_default_command("make outlook my default calendar") == ("calendar", "outlook_calendar"),
           "C 'make outlook my default calendar'")
    expect(pd.detect_default_command("use gmail as my default inbox") == ("email", "gmail"),
           "C 'use gmail as my default inbox'")
    expect(pd.detect_default_command("set google as the default for email") == ("email", "gmail"),
           "C 'set google as default for email'")
    expect(pd.detect_default_command("what's on my calendar") is None,
           "C a normal calendar query is NOT a default command")
    expect(pd.detect_default_command("show my default emails") is None,
           "C 'default' without a set verb + provider is ignored")

    try:
        async with pool.acquire() as conn:
            uid = await conn.fetchval(
                "INSERT INTO users (email, password_hash, role) VALUES ($1,'x','user') RETURNING id",
                f"verify-pd-{uuid.uuid4()}@example.invalid")
            # two connected calendar providers; outlook_calendar inserted LAST → most recent
            for p in ("google_calendar", "outlook_calendar"):
                await conn.execute(
                    "INSERT INTO provider_oauth_connectors (user_id, provider_name, provider_type, "
                    "status, scopes, metadata) VALUES ($1,$2,'calendar','connected','{}','{}'::jsonb)",
                    uid, p)

        # D) resolution
        expect(await pd.resolve("what's on my calendar", uid, "calendar", "google_calendar")
               == "outlook_calendar", "D no default → most-recently connected (outlook)")
        await pd.set_default(uid, "calendar", "google_calendar")
        expect(await pd.get_default(uid, "calendar") == "google_calendar", "D default persisted")
        expect(await pd.resolve("what's on my calendar", uid, "calendar", "google_calendar")
               == "google_calendar", "D connected default wins over most-recent")
        # a default that isn't connected is ignored → falls back to most-recent
        await pd.set_default(uid, "calendar", "some_unconnected_calendar")
        expect(await pd.resolve("what's on my calendar", uid, "calendar", "google_calendar")
               == "outlook_calendar", "D unconnected default ignored → most-recent")
        # no connections at all → hardcoded fallback
        expect(await pd.resolve("show my mail", uid, "email", "gmail") == "gmail",
               "D no connected providers → fallback")
    finally:
        if uid is not None:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM user_provider_defaults WHERE user_id=$1", uid)
                await conn.execute("DELETE FROM provider_oauth_connectors WHERE user_id=$1", uid)
                await conn.execute("DELETE FROM runtime_traces WHERE user_id=$1", uid)
                await conn.execute("DELETE FROM users WHERE id=$1", uid)

    if fails:
        print("RESULT: FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print("RESULT: PASS — provider adjectives stripped (search terms kept); hints cleaned; "
          "default command detected conservatively; resolution prefers the connected default, "
          "then most-recent, then fallback; an unconnected default is ignored")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
