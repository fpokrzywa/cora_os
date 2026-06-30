"""Deterministic check that FORGE is a real tool-aware codebase inspector (no longer
persona-only). Verifies the rewritten prompt, the idempotent startup migration that
lifts the LIVE version off the tool-suppressing seed, FORGE's filesystem-tool catalog
+ governance scope, and migration idempotency. Runs against the live DB after deploy.

    docker cp apps/cora-api/scripts/verify_agent_forge.py cora-api:/tmp/vf.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vf.py     # 0=PASS 1=FAIL
"""
import asyncio
import sys
import uuid

from app.clients import clients, init_clients
from app import agent_runtime as ar
from app.agents import forge, registry
from app.tools.governance import check_permission

FS_TOOLS = {"filesystem_list_project", "filesystem_read_file"}


async def main() -> int:
    await init_clients()
    fails: list[str] = []

    def expect(cond: bool, msg: str) -> None:
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # ---- Part A: module prompt is tool-aware ----
    p = forge.FORGE_SYSTEM_PROMPT
    expect(forge.FORGE_TOOL_AWARE_MARKER in p, "marker phrase present in module prompt")
    expect("filesystem_read_file" in p and "filesystem_list_project" in p,
           "prompt names its read tools")
    expect("READ-ONLY" in p, "prompt states the read-only stance")
    expect(list(forge.FORGE_ALLOWED_TOOLS) == ["filesystem_list_project", "filesystem_read_file"],
           "FORGE_ALLOWED_TOOLS aligned to the real filesystem tools")

    # ---- Part B: LIVE version migrated off the suppressing seed ----
    av = await registry.get_active_version("FORGE")
    expect(av is not None, "FORGE has an active version")
    if av:
        expect(forge.FORGE_TOOL_AWARE_MARKER in (av["system_prompt"] or ""),
               "LIVE FORGE prompt is tool-aware (startup migration ran)")
        expect("tool-aware FORGE" in (av["notes"] or ""),
               "live active version is the auto-migrated one (notes marker)")
        expect(set(av["allowed_tools"] or []) == FS_TOOLS,
               "live allowed_tools == the filesystem tools")

    # ---- Part C: catalog membership + governance scope ----
    cat = {t["function"]["name"] for t in await ar._build_catalog("FORGE")}
    expect(FS_TOOLS <= cat, "FORGE spoke catalog includes both filesystem tools")
    trow = await ar._fetch_tool_row("filesystem_read_file")
    expect(trow is not None and list(trow.get("allowed_agents") or []) == ["FORGE"],
           "filesystem_read_file is seeded allowed_agents=['FORGE']")
    if trow:
        d_forge = await check_permission(trow, agent_name="FORGE", user_id=uuid.uuid4(), is_admin=False)
        d_chronos = await check_permission(trow, agent_name="CHRONOS", user_id=uuid.uuid4(), is_admin=False)
        expect(d_forge.allowed, "governance allows filesystem_read_file for FORGE")
        expect(not d_chronos.allowed, "governance denies it for CHRONOS (scope isolation)")

    # ---- Part D: migration is idempotent (no version churn on re-run) ----
    async def fcount() -> int:
        async with clients.db_pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM agent_versions v JOIN agents a ON a.id = v.agent_id "
                "WHERE a.name = 'FORGE'")

    before = await fcount()
    await registry.ensure_forge_tool_aware_version()
    after = await fcount()
    expect(after == before, "ensure_forge_tool_aware_version is a no-op once tool-aware (idempotent)")

    # ---- Part E: dispatch drops model-invented args (no MCP crash on extra params) ----
    from types import SimpleNamespace
    orig_fetch, orig_perm, orig_disp = ar._fetch_tool_row, ar.check_permission, ar.dispatch_tool
    captured: dict = {}

    async def fake_fetch(_n):
        return {"name": "filesystem_read_file", "requires_confirmation": False,
                "risk_level": "low", "allowed_agents": ["FORGE"], "type": "mcp_action"}

    async def fake_perm(_t, **k):
        return SimpleNamespace(allowed=True, reason="ok")

    async def fake_disp(tool, payload):
        captured["metadata"] = payload.get("metadata")
        return {"status": "ok", "response": "x"}

    ar._fetch_tool_row, ar.check_permission, ar.dispatch_tool = fake_fetch, fake_perm, fake_disp
    try:
        await ar._dispatch_read_only(
            "filesystem_read_file",
            {"path": "docker-compose.yml", "line_start": 5, "bogus": 1, "encoding": "x"},
            agent_name="FORGE", user_id=uuid.uuid4(), session_id=None)
        expect(captured.get("metadata") == {"path": "docker-compose.yml", "line_start": 5},
               "dispatch keeps advertised args (path, line_start) and drops invented ones (bogus, encoding)")
    finally:
        ar._fetch_tool_row, ar.check_permission, ar.dispatch_tool = orig_fetch, orig_perm, orig_disp

    print()
    if fails:
        print(f"FAIL ({len(fails)}): " + "; ".join(fails))
        return 1
    print("PASS: FORGE is a tool-aware codebase inspector")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
