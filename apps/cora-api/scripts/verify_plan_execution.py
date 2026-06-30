"""Integration check of whole-plan sequential execution. Drives the worker
orchestrator `execute_plan` directly against the live DB (no real model), plus a
route smoke for the PLAN_EXECUTION_ENABLED gate. Covers: a template plan (tool-less
steps simulate) runs all steps in order -> plan completed; a step bound to a
non-existent tool HALTS the plan -> that step failed, earlier steps completed,
later steps left pending; the flag exists; and the /execute route is fail-closed
(403) while the flag is off. Cleans up its own plans.

    docker cp apps/cora-api/scripts/verify_plan_execution.py cora-api:/tmp/vpe.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vpe.py     # 0=PASS 1=FAIL
"""
import asyncio
import sys
import uuid

import httpx

from app.agents.planner import create_plan, update_step
from app.auth import create_access_token
from app.clients import clients, init_clients
from app.config import settings
from app.worker import execute_plan


async def _steps(plan_id):
    async with clients.db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT step_number, status, tool_name FROM execution_plan_steps "
            "WHERE plan_id = $1 ORDER BY step_number ASC", plan_id)
    return [dict(r) for r in rows]


async def _plan_status(plan_id):
    async with clients.db_pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT status FROM execution_plans WHERE id = $1", plan_id)


async def main() -> int:
    await init_clients()
    fails: list[str] = []
    created: list = []

    def expect(cond: bool, msg: str) -> None:
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    async with clients.db_pool.acquire() as conn:
        u = await conn.fetchrow(
            "SELECT id, email, role FROM users WHERE email='freddie@3cpublish.com'")
    if u is None:
        print("ABORT: test user not found")
        return 1
    uid = u["id"]

    def job_for(plan_id):
        return {"id": uuid.uuid4(), "plan_id": plan_id, "user_id": uid,
                "session_id": None, "workspace_id": None}

    # ---- flag exists ----
    expect(hasattr(settings, "plan_execution_enabled"),
           "settings.plan_execution_enabled exists")

    # ---- happy path: template plan (tool-less) runs to completion ----
    plan = await create_plan(session_id=None, user_id=uid, goal="verify plan exec happy")
    created.append(plan["id"])
    n = len(plan["steps"])
    res = await execute_plan(job_for(plan["id"]))
    expect(res.get("status") == "completed" and res.get("steps_ran") == n,
           f"orchestrator ran all {n} steps -> completed")
    expect(await _plan_status(plan["id"]) == "completed", "plan row marked completed")
    expect(all(s["status"] == "completed" for s in await _steps(plan["id"])),
           "every step marked completed")

    # ---- halt-on-failure: a step bound to a missing tool stops the plan ----
    plan2 = await create_plan(session_id=None, user_id=uid, goal="verify plan exec fail")
    created.append(plan2["id"])
    step2 = next(s for s in plan2["steps"] if s["step_number"] == 2)
    await update_step(plan2["id"], step2["id"], user_id=uid, is_admin=True,
                      tool_name="__nonexistent_tool__")
    res2 = await execute_plan(job_for(plan2["id"]))
    expect(res2.get("status") == "failed" and res2.get("failed_step") == 2,
           "orchestrator halts on the missing-tool step (failed_step=2)")
    expect(await _plan_status(plan2["id"]) == "failed", "plan row marked failed")
    st = await _steps(plan2["id"])
    by_num = {s["step_number"]: s["status"] for s in st}
    expect(by_num.get(1) == "completed", "step 1 completed before the failure")
    expect(by_num.get(2) == "failed", "step 2 (missing tool) marked failed")
    expect(by_num.get(3) == "pending", "step 3 left pending (execution halted)")

    # ---- route is fail-closed while the flag is off ----
    token = create_access_token(uid, u["email"], u["role"] or "admin")
    from app.main import app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post(f"/plans/{plan['id']}/execute",
                              headers={"Authorization": f"Bearer {token}"})
    expect(r.status_code == 403 and "disabled" in r.text,
           f"/plans/{{id}}/execute is 403 while PLAN_EXECUTION_ENABLED off (got {r.status_code})")

    # ---- cleanup ----
    async with clients.db_pool.acquire() as conn:
        for pid in created:
            await conn.execute("DELETE FROM execution_plans WHERE id = $1", pid)

    print()
    if fails:
        print(f"FAIL ({len(fails)}): " + "; ".join(fails))
        return 1
    print("PASS: whole-plan sequential execution verified")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
