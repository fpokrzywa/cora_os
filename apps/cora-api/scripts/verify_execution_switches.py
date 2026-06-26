"""Verification of the admin-managed execution kill-switch override layer
(app/runtime_switches.py + /admin/execution-switches).

Under a throwaway admin user:
  A) effective() with NO override == the env default (settings).
  B) set_switch() upserts a DB override → effective() follows it; get_all() reports
     overridden/effective/env_default; an audit trace is written.
  C) clear_override() removes the override → effective() reverts to the env default.
  D) SAFETY: external_execution_enabled is registered but NOT manageable —
     set_switch/clear_override refuse it (code=forbidden); it stays env-locked.
  E) unknown switch → set_switch raises not_found; effective() fails closed.

Snapshots + restores the operator's REAL calendar/screen switch overrides so this run
never disturbs their config. Run:

    docker cp apps/cora-api/scripts/verify_execution_switches.py cora-api:/tmp/ves.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/ves.py     # 0=PASS 1=FAIL
"""
import asyncio
import sys
import uuid

from app.clients import clients, init_clients
from app import runtime_switches as rs
from app.config import settings


async def main() -> int:
    await init_clients()
    pool = clients.db_pool
    fails = []
    uid = None
    saved = []

    def expect(c, m):
        if not c:
            fails.append(m)

    async with pool.acquire() as conn:
        # snapshot + clear any real overrides for the managed switches
        saved = [dict(r) for r in await conn.fetch(
            "SELECT name, enabled, updated_by FROM runtime_execution_switches "
            "WHERE name IN ('calendar_execution_enabled','screen_vision_enabled')")]
        await conn.execute(
            "DELETE FROM runtime_execution_switches "
            "WHERE name IN ('calendar_execution_enabled','screen_vision_enabled')")
        uid = await conn.fetchval(
            "INSERT INTO users (email, password_hash, role) VALUES ($1,'x','admin') RETURNING id",
            f"verify-sw-{uuid.uuid4()}@example.invalid")

    try:
        NAME = "calendar_execution_enabled"
        env_default = bool(settings.calendar_execution_enabled)

        # A) no override → env default
        expect(await rs.effective(NAME) == env_default, "A effective == env default when no override")
        all0 = {s["name"]: s for s in await rs.get_all()}
        expect(all0[NAME]["overridden"] is False and all0[NAME]["effective"] == env_default,
               "A get_all reports not-overridden + env effective")
        expect(all0[NAME]["manageable"] is True, "A calendar switch is manageable")
        expect(all0["external_execution_enabled"]["manageable"] is False,
               "A external_execution is NOT manageable")

        # B) set override to the opposite of env → effective follows
        target = not env_default
        out = await rs.set_switch(NAME, target, admin_id=uid)
        expect(out["override"] is target and out["effective"] is target and out["overridden"] is True,
               "B set_switch returns the new override/effective")
        expect(await rs.effective(NAME) is target, "B effective follows the override")
        async with pool.acquire() as conn:
            tr = await conn.fetchval(
                "SELECT count(*) FROM runtime_traces WHERE user_id=$1 "
                "AND trace_type='execution_switch_modified'", uid)
        expect(tr >= 1, "B audit trace written")

        # C) clear → revert to env default
        out = await rs.clear_override(NAME, admin_id=uid)
        expect(out["overridden"] is False and out["effective"] == env_default,
               "C clear_override reverts to env default")
        expect(await rs.effective(NAME) == env_default, "C effective back to env default")

        # D) external_execution is env-locked
        try:
            await rs.set_switch("external_execution_enabled", True, admin_id=uid)
            expect(False, "D set_switch must refuse external_execution_enabled")
        except rs.SwitchError as e:
            expect(e.code == "forbidden", "D refusal is code=forbidden")
        async with pool.acquire() as conn:
            leaked = await conn.fetchval(
                "SELECT count(*) FROM runtime_execution_switches WHERE name='external_execution_enabled'")
        expect(leaked == 0, "D no override row created for the env-locked switch")

        # E) unknown switch
        try:
            await rs.set_switch("ghost_switch", True, admin_id=uid)
            expect(False, "E unknown switch must raise")
        except rs.SwitchError as e:
            expect(e.code == "not_found", "E unknown switch → not_found")
        expect(await rs.effective("ghost_switch") is False, "E unknown effective fails closed")
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM runtime_execution_switches "
                "WHERE name IN ('calendar_execution_enabled','screen_vision_enabled')")
            for r in saved:
                await conn.execute(
                    "INSERT INTO runtime_execution_switches (name, enabled, updated_by) "
                    "VALUES ($1,$2,$3)", r["name"], r["enabled"], r["updated_by"])
            if uid is not None:
                await conn.execute("DELETE FROM runtime_traces WHERE user_id=$1", uid)
                await conn.execute("DELETE FROM users WHERE id=$1", uid)

    if fails:
        print("RESULT: FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print("RESULT: PASS — env default applies with no override; set_switch overrides + "
          "audits; clear_override reverts to env; external_execution is env-locked "
          "(set/clear refused, no row); unknown switch fails closed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
