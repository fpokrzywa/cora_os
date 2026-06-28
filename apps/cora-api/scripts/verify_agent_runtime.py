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
  G) Runs view — list_runs is owner-scoped; get_run_delegations rebuilds the
     orchestrator→spoke tree from the _parent_run_id stamp and embeds the spoke
     run's own step trace.
  H) Independent evaluator (Phase 6) — _parse_verdict is robust (clean JSON,
     prose-wrapped JSON, malformed → 'concerns', out-of-range verdict normalized);
     _render_eval_input packs goal/answer/trace; evaluate_run is fail-closed
     (None when the flag is off OR endpoint/model unset — no model call); the
     evaluation column round-trips through _finalize_run → get_run.
  I) Confirm-as-interrupt (Phase 7) — _collect_staged keeps only successful
     staging observations; _pause_run leaves the run at waiting_user (NOT
     completed) with the pending interrupt; resolve_interrupt is owner-scoped,
     rejects an invalid/no-longer-waiting decision, and resolves approve→done /
     reject→cancelled while recording the decision (NO external effect).

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
    spoke_run_id = None
    deleg_id = None
    pause_run_id = None
    reject_run_id = None

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

        # ---- G) Runs view (list + orchestrator→spoke tree) ----
        print("G) runs view")
        listed = await ar.list_runs(user_id=uid, limit=50)
        expect(any(r["id"] == run_id for r in listed),
               "list_runs returns the owner's run")
        other_list = await ar.list_runs(user_id=uuid.uuid4(), limit=50)
        expect(all(r["id"] != run_id for r in other_list),
               "list_runs is owner-scoped (owned run hidden from another user)")

        # A spoke run + a delegation stamped with this run's _parent_run_id.
        spoke_run_id = await ar.create_pending_run(
            goal="spoke subtask", user_id=uid, session_id=sess,
            workspace_id=None, agent_name="PULSE", max_steps=6)
        await ar._finalize_run(
            spoke_run_id, status="done", answer="spoke answer", stopped="final",
            tool_calls=1, step_count=1, messages=[],
            steps=[ar.AgentStep("tool_call", {"name": "web_search", "arguments": {}})],
            error=None)
        deleg = await ar.create_delegation(
            from_agent="ATLAS", to_agent="PULSE",
            delegation_reason="verify tree", session_id=sess,
            input_payload={"goal": "x", "_parent_run_id": str(run_id)},
            user_id=uid, initial_status="running")
        deleg_id = deleg["id"]
        await ar.complete_delegation(
            deleg_id,
            output_payload={"answer": "spoke answer", "run_id": str(spoke_run_id),
                            "stopped": "final"},
            user_id=uid)

        tree = await ar.get_run_delegations(run_id)
        expect(len(tree) == 1 and tree[0]["to_agent"] == "PULSE",
               "get_run_delegations finds the hop via the _parent_run_id stamp")
        node = tree[0] if tree else {}
        expect(bool(node.get("spoke_run"))
               and node["spoke_run"]["id"] == spoke_run_id,
               "the spoke's own run is embedded in the delegation node")
        expect(bool(node.get("spoke_run"))
               and len(node["spoke_run"]["steps"]) == 1,
               "the embedded spoke run carries its step trace")
        empty = await ar.get_run_delegations(uuid.uuid4())
        expect(empty == [], "an unrelated run id yields no delegations")

        # ---- H) independent evaluator (Phase 6) ----
        print("H) evaluator")
        v = ar._parse_verdict('{"verdict":"fail","reasons":["a","b"],"summary":"nope"}')
        expect(v["verdict"] == "fail" and v["reasons"] == ["a", "b"],
               "_parse_verdict reads a clean JSON verdict")
        v2 = ar._parse_verdict(
            'Sure: {"verdict":"pass","reasons":[],"summary":"ok"} -- done')
        expect(v2["verdict"] == "pass", "_parse_verdict extracts JSON embedded in prose")
        v3 = ar._parse_verdict("totally malformed, no json here")
        expect(v3["verdict"] == "concerns" and "malformed" in v3["summary"],
               "a malformed reply falls back to 'concerns' (never a silent pass)")
        v4 = ar._parse_verdict('{"verdict":"perfect","reasons":"x","summary":"y"}')
        expect(v4["verdict"] == "concerns" and v4["reasons"] == ["x"],
               "an out-of-range verdict is normalized to 'concerns'")

        rendered = ar._render_eval_input(
            "do X", "did X",
            [ar.AgentStep("tool_call", {"name": "web_search", "arguments": {"query": "x"}}),
             ar.AgentStep("tool_result", {"name": "web_search", "result": "snippet"})])
        expect(all(k in rendered for k in
                   ("USER GOAL", "do X", "AGENT FINAL ANSWER", "did X", "web_search")),
               "_render_eval_input packs goal + answer + tool trace")

        off = await ar.evaluate_run(goal="g", answer="a", steps=[])
        expect(off is None, "evaluate_run is None when AGENT_EVAL_ENABLED is off")

        prev_enabled = settings.agent_eval_enabled
        prev_endpoint = settings.dgx_model_endpoint
        settings.agent_eval_enabled = True
        settings.dgx_model_endpoint = ""
        try:
            none2 = await ar.evaluate_run(goal="g", answer="a", steps=[])
        finally:
            settings.agent_eval_enabled = prev_enabled
            settings.dgx_model_endpoint = prev_endpoint
        expect(none2 is None,
               "evaluate_run stays None when endpoint/model unset (no model call)")

        verdict = {"verdict": "concerns", "reasons": ["r1"], "summary": "s", "model": "t"}
        await ar._finalize_run(
            run_id, status="done", answer="ok", stopped="final", tool_calls=1,
            step_count=2, messages=[], steps=[], error=None, evaluation=verdict)
        row3 = await ar.get_run(run_id, user_id=uid)
        expect(bool(row3) and (row3.get("evaluation") or {}).get("verdict") == "concerns",
               "_finalize_run persists the evaluation; get_run returns it")

        # ---- I) confirm-as-interrupt (Phase 7) ----
        print("I) confirm-as-interrupt")
        staged = ar._collect_staged([
            ar.AgentStep("tool_result",
                         {"name": "signal_create_draft",
                          "result": "✓ Staged a review-only email draft 'X'"}),
            ar.AgentStep("tool_result",
                         {"name": "web_search", "result": "✓ not a staging tool"}),
            ar.AgentStep("tool_result",
                         {"name": "signal_create_draft",
                          "result": "error: a draft needs a body."}),
        ])
        expect(len(staged) == 1 and staged[0]["tool"] == "signal_create_draft",
               "_collect_staged keeps only successful staging observations")

        pause_run_id = await ar.create_pending_run(
            goal="stage + pause", user_id=uid, session_id=sess, max_steps=6)
        await ar._pause_run(
            pause_run_id, answer="staged a draft", stopped="final", tool_calls=1,
            step_count=1, messages=[], steps=[], evaluation=None,
            interrupt={"staged": staged, "decision": None, "note": None})
        paused = await ar.get_run(pause_run_id, user_id=uid)
        expect(bool(paused) and paused["status"] == "waiting_user"
               and paused["completed_at"] is None,
               "_pause_run leaves the run at waiting_user (not completed)")
        expect(bool((paused.get("interrupt") or {}).get("staged")),
               "the pending interrupt (staged artifacts) is persisted")

        other = await ar.resolve_interrupt(
            pause_run_id, user_id=uuid.uuid4(), decision="approve")
        expect(other is None, "resolve_interrupt is owner-scoped (other user -> None)")
        bad = await ar.resolve_interrupt(pause_run_id, user_id=uid, decision="maybe")
        expect(bad is None, "resolve_interrupt rejects an invalid decision")

        approved = await ar.resolve_interrupt(
            pause_run_id, user_id=uid, decision="approve", note="ok")
        expect(bool(approved) and approved["status"] == "done"
               and approved["completed_at"] is not None
               and (approved.get("interrupt") or {}).get("decision") == "approve",
               "approve resolves the run to done + records the decision")
        again = await ar.resolve_interrupt(
            pause_run_id, user_id=uid, decision="approve")
        expect(again is None, "a run no longer waiting cannot be re-decided")

        reject_run_id = await ar.create_pending_run(
            goal="stage + reject", user_id=uid, session_id=sess, max_steps=6)
        await ar._pause_run(
            reject_run_id, answer="x", stopped="final", tool_calls=0, step_count=0,
            messages=[], steps=[], evaluation=None,
            interrupt={"staged": [], "decision": None, "note": None})
        rejected = await ar.resolve_interrupt(
            reject_run_id, user_id=uid, decision="reject")
        expect(bool(rejected) and rejected["status"] == "cancelled",
               "reject resolves the run to cancelled (no external effect)")

        # ---- config sanity ----
        expect(isinstance(settings.agent_runtime_max_steps, int)
               and isinstance(settings.agent_delegation_max_parallel, int),
               "agent runtime config values are present")

    finally:
        # Clean up the disposable rows we created.
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM communication_drafts WHERE subject=$1", DRAFT_MARKER)
            if deleg_id is not None:
                await conn.execute(
                    "DELETE FROM agent_delegations WHERE id=$1", deleg_id)
                # delegation create/complete also wrote audit traces for this session
                await conn.execute(
                    "DELETE FROM runtime_traces WHERE session_id=$1", sess)
            for rid in (run_id, spoke_run_id, pause_run_id, reject_run_id):
                if rid is not None:
                    await conn.execute(
                        "DELETE FROM agent_runtime_runs WHERE id=$1", rid)

    print()
    if fails:
        print(f"FAIL ({len(fails)}): " + "; ".join(fails))
        return 1
    print("PASS: agent_runtime verified")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
