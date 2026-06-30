"""Deterministic check that PULSE is web-aware (its prompt no longer claims "no live
web access" while the governed web_search tool is wired). Verifies the rewritten
prompt, the idempotent startup migration on the LIVE version, PULSE's web_search
catalog + governance scope, and migration idempotency. Runs against the live DB.

    docker cp apps/cora-api/scripts/verify_agent_pulse.py cora-api:/tmp/vp.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vp.py     # 0=PASS 1=FAIL
"""
import asyncio
import sys
import uuid

from app.clients import clients, init_clients
from app import agent_runtime as ar
from app.agents import pulse, registry
from app.tools.governance import check_permission


async def main() -> int:
    await init_clients()
    fails: list[str] = []

    def expect(cond: bool, msg: str) -> None:
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # ---- Part A: module prompt is web-aware, contradiction removed ----
    p = pulse.PULSE_SYSTEM_PROMPT
    expect(pulse.PULSE_WEB_AWARE_MARKER in p, "marker phrase present in module prompt")
    expect("no live web access" not in p, "old 'no live web access' claim removed")
    expect("web_search" in p, "prompt references the web_search tool")
    expect(list(pulse.PULSE_ALLOWED_TOOLS) == ["web_search"],
           "PULSE_ALLOWED_TOOLS aligned to web_search")

    # ---- Part B: LIVE version migrated off the 'no web access' seed ----
    av = await registry.get_active_version("PULSE")
    expect(av is not None, "PULSE has an active version")
    if av:
        expect(pulse.PULSE_WEB_AWARE_MARKER in (av["system_prompt"] or ""),
               "LIVE PULSE prompt is web-aware (startup migration ran)")
        expect("no live web access" not in (av["system_prompt"] or ""),
               "LIVE prompt no longer claims no web access")
        expect("web-aware PULSE" in (av["notes"] or ""),
               "live active version is the auto-migrated one (notes marker)")

    # ---- Part C: catalog membership + governance scope ----
    cat = {t["function"]["name"] for t in await ar._build_catalog("PULSE")}
    expect("web_search" in cat, "PULSE spoke catalog includes web_search")
    trow = await ar._fetch_tool_row("web_search")
    expect(trow is not None and "PULSE" in list(trow.get("allowed_agents") or []),
           "web_search seeded with PULSE in allowed_agents")
    if trow:
        d_pulse = await check_permission(trow, agent_name="PULSE", user_id=uuid.uuid4(), is_admin=False)
        d_forge = await check_permission(trow, agent_name="FORGE", user_id=uuid.uuid4(), is_admin=False)
        expect(d_pulse.allowed, "governance allows web_search for PULSE")
        expect(not d_forge.allowed, "governance denies web_search for FORGE (scope isolation)")

    # ---- Part D: migration idempotent (no version churn on re-run) ----
    async def fcount() -> int:
        async with clients.db_pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM agent_versions v JOIN agents a ON a.id = v.agent_id "
                "WHERE a.name = 'PULSE'")

    before = await fcount()
    await registry.ensure_pulse_web_aware_version()
    after = await fcount()
    expect(after == before, "ensure_pulse_web_aware_version is a no-op once migrated (idempotent)")

    print()
    if fails:
        print(f"FAIL ({len(fails)}): " + "; ".join(fails))
        return 1
    print("PASS: PULSE is web-aware")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
