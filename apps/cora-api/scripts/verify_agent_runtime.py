"""Durable verification of the model-driven agent runtime (app.agent_runtime).

Covers the DETERMINISTIC surface — no live DGX/model call is made, so this is
safe to run in CI / offline. The model loop itself (_chat / run_agent end-to-end)
is exercised manually via POST /chat/agent.

Parts:
  A) Catalog scoping — the orchestrator (agent_name=None) sees all read-only
     tools; a spoke sees only tools its tools.allowed_agents permits (domain
     isolation); staging tools appear only when include_staging.
  B) Read-only dispatch governance — an unknown tool and a governance-denied
     tool both come back as error TEXT (never raise); no network is touched.
  C) Staging safety floor — _handle_staging refuses a non-staging tool, and a
     real internal_action tool creates a REVIEW-ONLY draft (then cleaned up).
  D) Durable runs — create_pending_run -> get_run (owner-scoped) -> _finalize_run
     transitions the row to done (then cleaned up).
  E) Delegation helpers — _load_spokes returns the 4 specialists; _render_task
     builds the minimal-context payload; _delegate_tool_schema enumerates spokes.
  F) Hub-and-spoke guards — _handle_delegation blocks depth>=1 and unknown agents
     BEFORE running anything (so no model call).

    docker cp apps/cora-api/scripts/verify_agent_runtime.py cora-api:/tmp/var.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/var.py   # 0=PASS 1=FAIL
"""
import asyncio
import sys
import uuid

from app.clients import clients, init_clients
from app.config import settings
from app import agent_runtime as ar

DRAFT_MARKER = "VERIFY_AGENT_RUNTIME_MARKER"


async def main() -> int:
    await init_clients()
    pool = clients.db_pool
    if pool is None:
        print("FAIL: no Postgres pool")
        return 1

    fails: list[str] = []

    def expect(cond: bool, msg: str) -> None:
        if cond:
            print(f"  ok   {msg}")
        else:
            fails.append(msg)
            print(f"  FAIL {msg}")

    # A real user for the FK-bearing draft create + run owner-scoping.
    async with pool.acquire() as conn:
        uid = await conn.fetchval("SELECT id FROM users ORDER BY created_at LIMIT 1")
    expect(uid is not None, "a user exists to attribute test rows to")
    sess = uuid.uuid4()
    run_id = None

    try:
        # ---- A) Catalog scoping ----
        print("A) catalog scoping")
        orch = {t["function"]["name"] for t in await ar._build_catalog(None)}
        expect("web_search" in orch and "filesystem_read_file" in orch,
               "orchestrator (None) sees all read-only tools")

        pulse = {t["function"]["name"] for t in await ar._build_catalog("PULSE")}
        expect("web_search" in pulse, "PULSE sees web_search")
        expect("filesystem_read_file" not in pulse,
               "PULSE is denied FORGE-only filesystem tools (domain isolation)")

        forge = {t["function"]["name"] for t in await ar._build_catalog("FORGE")}
        expect("filesystem_read_file" in forge, "FORGE sees its filesystem tool")
        expect("web_search" not in forge, "FORGE is denied PULSE-only web_search")

        staged = {
            t["function"]["name"]
            for t in await ar._build_catalog(None, include_staging=True)
        }
        expect("signal_create_draft" in staged,
               "include_staging exposes the staging tools")
        expect("signal_create_draft" not in orch,
               "read-only catalog excludes staging tools (fail-closed)")

        # ---- B) Read-only dispatch governance (no network) ----
        print("B) read-only dispatch governance")
        obs = await ar._dispatch_read_only(
            "not_a_real_tool", {}, agent_name=None, user_id=uid, session_id=None)
        expect(obs.startswith("error"), "unknown tool -> error text, no raise")
        denied = await ar._dispatch_read_only(
            "web_search", {"query": "x"}, agent_name="FORGE",
            user_id=uid, session_id=None)
        expect(denied.startswith("error") and "denied" in denied,
               "FORGE denied web_search by governance (no execution)")

        # ---- C) Staging safety floor ----
        print("C) staging floor + create")
        floor = await ar._handle_staging(
            "web_search", {}, user_id=uid, workspace_id=None,
            session_id=None, agent_name=None)
        expect(floor.startswith("error") and "staging" in floor,
               "staging refuses a non-staging tool name")
        made = await ar._handle_staging(
            "signal_create_draft",
            {"body": "verify body", "subject": DRAFT_MARKER, "title": DRAFT_MARKER},
            user_id=uid, workspace_id=None, session_id=str(sess), agent_name=None)
        expect(made.startswith("✓") and "NOT" in made,
               "signal_create_draft stages a review-only draft (not sent)")
        async with pool.acquire() as conn:
            n = await conn.fetchval(
                "SELECT count(*) FROM communication_drafts WHERE subject=$1",
                DRAFT_MARKER)
        expect(n == 1, "exactly one draft row was created")

        # ---- D) Durable runs ----
        print("D) durable runs")
        run_id = await ar.create_pending_run(
            goal="verify run", user_id=uid, session_id=sess,
            workspace_id=None, agent_name=None, max_steps=6)
        expect(run_id is not None, "create_pending_run returns an id")
        row = await ar.get_run(run_id, user_id=uid)
        expect(bool(row) and row["status"] == "pending" and row["goal"] == "verify run",
               "get_run returns the pending run for its owner")
        other = await ar.get_run(run_id, user_id=uuid.uuid4())
        expect(other is None, "get_run is owner-scoped (other user -> None)")
        await ar._finalize_run(
            run_id, status="done", answer="ok", stopped="final", tool_calls=1,
            step_count=2, messages=[{"role": "user", "content": "x"}],
            steps=[ar.AgentStep("final", {"answer": "ok"})], error=None)
        row2 = await ar.get_run(run_id, user_id=uid)
        expect(bool(row2) and row2["status"] == "done" and row2["answer"] == "ok",
               "_finalize_run transitions the run to done")

        # ---- E) Delegation helpers ----
        print("E) delegation helpers")
        spokes = await ar._load_spokes()
        expect({"PULSE", "FORGE", "SIGNAL", "CHRONOS"}.issubset(spokes.keys()),
               "_load_spokes returns the 4 specialists")
        expect(bool(spokes.get("PULSE", {}).get("system_prompt")),
               "each spoke carries its own system_prompt")
        task = ar._render_task(
            {"goal": "do x", "facts": {"a": 1},
             "constraints": ["c1"], "expected": "y"})
        expect(all(k in task for k in ("Task: do x", "Facts:", "Constraints:", "Expected output:")),
               "_render_task renders the minimal-context payload")
        schema = ar._delegate_tool_schema(["PULSE", "FORGE"])
        enum = schema["function"]["parameters"]["properties"]["agent"]["enum"]
        expect(enum == ["PULSE", "FORGE"], "delegate_to schema enumerates the spokes")

        # ---- F) Hub-and-spoke guards (no model call) ----
        print("F) hub-and-spoke guards")
        deep = await ar._handle_delegation(
            {"agent": "PULSE", "goal": "x"}, spokes=spokes, user_id=uid,
            session_id=sess, workspace_id=None, depth=1)
        expect(deep.startswith("error") and "depth" in deep,
               "depth>=1 is refused (hub-and-spoke = exactly one hop)")
        unknown = await ar._handle_delegation(
            {"agent": "NOPE", "goal": "x"}, spokes=spokes, user_id=uid,
            session_id=sess, workspace_id=None, depth=0)
        expect(unknown.startswith("error") and "unknown agent" in unknown,
               "an unknown delegation target is refused")

        # ---- config sanity ----
        expect(isinstance(settings.agent_runtime_max_steps, int)
               and isinstance(settings.agent_delegation_max_parallel, int),
               "agent runtime config values are present")

    finally:
        # Clean up the disposable rows we created.
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM communication_drafts WHERE subject=$1", DRAFT_MARKER)
            if run_id is not None:
                await conn.execute(
                    "DELETE FROM agent_runtime_runs WHERE id=$1", run_id)

    print()
    if fails:
        print(f"FAIL ({len(fails)}): " + "; ".join(fails))
        return 1
    print("PASS: agent_runtime verified")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
