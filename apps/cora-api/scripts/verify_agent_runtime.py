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
  J) Confirm-as-interrupt OUTWARD half (Phase 7) — _collect_staged builds a
     fireable calendar_create item (provider + event fields) from a proposal that
     named a time + provider, an email_draft item, and NO type for a bare proposal;
     agent_fire_calendar_create is fail-closed + never raises; resolve_interrupt on
     approve fires NOTHING while AGENT_EXECUTION_ENABLED is off (do-not-break), and
     with it on fires ONLY the calendar create (email is never sent) and records the
     per-artifact outcomes. The real gated fire is spied / unknown-provider here, so
     this NEVER performs a live calendar write.
  K) Backend selection + OpenAI/vLLM bridge (tool-loop migration) — _agent_backend
     defaults to ollama (DGX endpoint + chat model) and selects the vLLM endpoint +
     model when dgx_agent_backend='openai'; _to_openai_messages translates the
     canonical thread (synthesized tool_call ids, object args -> JSON string, each
     tool result paired to its call in order); _normalize_openai_response maps a
     chat-completions reply back to the Ollama-shaped {"message": {...}} the loop
     reads (and is null-safe on a malformed body). No network is touched.
  L) Evaluator-gated approval (Phase 6 + 7) — with agent_eval_gate_enabled off a
     'fail' verdict approves normally; on, it BLOCKS approve (returns a blocked
     marker, leaves the run waiting_user, fires nothing) while reject and non-'fail'
     verdicts pass, and override=True forces the approve through (recording it).

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
    fire_run_id = None
    fire_run_id2 = None
    fire_run_id3 = None
    gate_run_id = None
    gate_run_id2 = None
    gate_run_id3 = None
    gate_run_id4 = None

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

        # Force the flag off for this check so it's independent of the live deploy
        # (which may run with AGENT_EVAL_ENABLED on).
        _eval_was = settings.agent_eval_enabled
        settings.agent_eval_enabled = False
        try:
            off = await ar.evaluate_run(goal="g", answer="a", steps=[])
        finally:
            settings.agent_eval_enabled = _eval_was
        expect(off is None, "evaluate_run is None when AGENT_EVAL_ENABLED is off")

        prev_enabled = settings.agent_eval_enabled
        prev_endpoint = settings.dgx_model_endpoint
        prev_backend = settings.dgx_agent_backend
        settings.agent_eval_enabled = True
        # Force the ollama backend so zeroing dgx_model_endpoint unsets the ACTIVE
        # endpoint the (backend-aware) evaluator reads — independent of the deploy.
        settings.dgx_agent_backend = "ollama"
        settings.dgx_model_endpoint = ""
        try:
            none2 = await ar.evaluate_run(goal="g", answer="a", steps=[])
        finally:
            settings.agent_eval_enabled = prev_enabled
            settings.dgx_model_endpoint = prev_endpoint
            settings.dgx_agent_backend = prev_backend
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

        # ---- J) confirm-as-interrupt OUTWARD half (Phase 7) ----
        # NOTE: the real gated calendar fire is NEVER exercised here — it is either
        # spied (J5/J6) or invoked with an unknown provider (J4) — so this script
        # can never perform a live calendar write, even against a DB with a
        # connected calendar. The live path is confirmed by the operator checklist.
        print("J) confirm-as-interrupt OUTWARD half")
        cstaged = ar._collect_staged([
            ar.AgentStep("tool_result", {
                "name": "chronos_create_schedule_proposal",
                "arguments": {"title": "Sync", "start_time": "2026-07-01T15:00:00",
                              "end_time": "2026-07-01T16:00:00",
                              "provider": "google_calendar"},
                "result": "✓ Staged a review-only schedule proposal 'Sync'"}),
        ])
        expect(len(cstaged) == 1 and cstaged[0].get("type") == "calendar_create"
               and cstaged[0].get("provider") == "google_calendar"
               and cstaged[0].get("fields", {}).get("start_time") == "2026-07-01T15:00:00"
               and cstaged[0].get("fields", {}).get("timezone"),
               "_collect_staged builds a fireable calendar_create (+ backfilled timezone)")

        # Backfill: a start with NO end becomes fireable with a defaulted 60-min end.
        noend = ar._collect_staged([
            ar.AgentStep("tool_result", {
                "name": "chronos_create_schedule_proposal",
                "arguments": {"title": "Sync", "start_time": "2026-07-01T15:00:00",
                              "provider": "google_calendar"},
                "result": "✓ Staged a review-only schedule proposal 'Sync'"}),
        ])
        expect(len(noend) == 1 and noend[0].get("type") == "calendar_create"
               and noend[0]["fields"].get("end_time") == "2026-07-01T16:00:00",
               "a start-only proposal is fired with a defaulted 60-minute end")

        # Two proposals in ONE turn must NOT cross-wire (each result carries its
        # own args; regression for the name-keyed last-write-wins bug).
        two = ar._collect_staged([
            ar.AgentStep("tool_result", {
                "name": "chronos_create_schedule_proposal",
                "arguments": {"title": "A", "start_time": "2026-07-01T09:00:00",
                              "end_time": "2026-07-01T10:00:00",
                              "provider": "google_calendar"},
                "result": "✓ Staged a review-only schedule proposal 'A'"}),
            ar.AgentStep("tool_result", {
                "name": "chronos_create_schedule_proposal",
                "arguments": {"title": "B", "start_time": "2026-07-02T09:00:00",
                              "end_time": "2026-07-02T10:00:00",
                              "provider": "outlook_calendar"},
                "result": "✓ Staged a review-only schedule proposal 'B'"}),
        ])
        expect(len(two) == 2 and two[0]["provider"] == "google_calendar"
               and two[1]["provider"] == "outlook_calendar"
               and two[0]["fields"]["title"] == "A" and two[1]["fields"]["title"] == "B",
               "two staging calls in one turn keep their own provider/fields (no cross-wire)")

        plain = ar._collect_staged([
            ar.AgentStep("tool_result", {
                "name": "chronos_create_schedule_proposal",
                "arguments": {"title": "Just an idea"},
                "result": "✓ Staged a review-only schedule proposal 'Just an idea'"}),
        ])
        expect(len(plain) == 1 and plain[0].get("type") is None,
               "a proposal with no time/provider is not fireable (no type)")

        estaged = ar._collect_staged([
            ar.AgentStep("tool_result", {
                "name": "signal_create_draft",
                "result": "✓ Staged a review-only email draft 'X'"}),
        ])
        expect(len(estaged) == 1 and estaged[0].get("type") == "email_draft",
               "an email draft is captured as type email_draft")

        noadapter = await ar.chat_calendar.agent_fire_calendar_create(
            provider="verify_no_such_provider", user_id=uid, workspace_id=None,
            fields={"title": "x", "start_time": "2026-07-01T15:00:00"})
        expect(noadapter["ok"] is False and noadapter.get("event_id") is None,
               "agent_fire_calendar_create is fail-closed + never raises (unknown provider)")

        fire_items = [
            {"tool": "chronos_create_schedule_proposal", "summary": "Sync",
             "type": "calendar_create", "provider": "google_calendar",
             "fields": {"title": "Sync", "start_time": "2026-07-01T15:00:00"}},
            {"tool": "signal_create_draft", "summary": "Draft", "type": "email_draft"},
        ]

        async def _spy(sink):
            async def _fn(**kw):
                sink.append(kw)
                return {"ok": True, "reason": "created", "event_id": "evt_verify",
                        "title": "Sync", "link": None}
            return _fn

        orig_fire = ar.chat_calendar.agent_fire_calendar_create
        prev_exec = settings.agent_execution_enabled
        prev_intr = settings.agent_interrupt_enabled

        # J5: execution OFF (default) -> approve fires NOTHING (do-not-break).
        fire_run_id = await ar.create_pending_run(
            goal="stage+fire off", user_id=uid, session_id=sess, max_steps=6)
        await ar._pause_run(
            fire_run_id, answer="x", stopped="final", tool_calls=2, step_count=2,
            messages=[], steps=[], evaluation=None,
            interrupt={"staged": fire_items, "decision": None, "note": None})
        calls_off: list = []
        ar.chat_calendar.agent_fire_calendar_create = await _spy(calls_off)
        try:
            settings.agent_interrupt_enabled = True
            settings.agent_execution_enabled = False
            r_off = await ar.resolve_interrupt(
                fire_run_id, user_id=uid, decision="approve")
        finally:
            ar.chat_calendar.agent_fire_calendar_create = orig_fire
            settings.agent_execution_enabled = prev_exec
            settings.agent_interrupt_enabled = prev_intr
        expect(bool(r_off) and r_off["status"] == "done" and not calls_off,
               "approve with AGENT_EXECUTION_ENABLED off fires nothing (records decision only)")
        expect((r_off.get("interrupt") or {}).get("executed") is None,
               "no execution outcomes are recorded when execution is off")

        # J5b: execution ON but interrupt OFF -> compound gate still blocks firing.
        fire_run_id3 = await ar.create_pending_run(
            goal="stage+gate", user_id=uid, session_id=sess, max_steps=6)
        await ar._pause_run(
            fire_run_id3, answer="x", stopped="final", tool_calls=2, step_count=2,
            messages=[], steps=[], evaluation=None,
            interrupt={"staged": fire_items, "decision": None, "note": None})
        calls_gate: list = []
        ar.chat_calendar.agent_fire_calendar_create = await _spy(calls_gate)
        try:
            settings.agent_interrupt_enabled = False
            settings.agent_execution_enabled = True
            r_gate = await ar.resolve_interrupt(
                fire_run_id3, user_id=uid, decision="approve")
        finally:
            ar.chat_calendar.agent_fire_calendar_create = orig_fire
            settings.agent_execution_enabled = prev_exec
            settings.agent_interrupt_enabled = prev_intr
        expect(bool(r_gate) and r_gate["status"] == "done" and not calls_gate,
               "compound gate: execution on but interrupt off fires nothing")

        # J6: execution ON -> approve fires ONLY the calendar_create; email never sent.
        fire_run_id2 = await ar.create_pending_run(
            goal="stage+fire on", user_id=uid, session_id=sess, max_steps=6)
        await ar._pause_run(
            fire_run_id2, answer="x", stopped="final", tool_calls=2, step_count=2,
            messages=[], steps=[], evaluation=None,
            interrupt={"staged": fire_items, "decision": None, "note": None})
        calls_on: list = []
        ar.chat_calendar.agent_fire_calendar_create = await _spy(calls_on)
        try:
            settings.agent_interrupt_enabled = True
            settings.agent_execution_enabled = True
            r_on = await ar.resolve_interrupt(
                fire_run_id2, user_id=uid, decision="approve")
        finally:
            ar.chat_calendar.agent_fire_calendar_create = orig_fire
            settings.agent_execution_enabled = prev_exec
            settings.agent_interrupt_enabled = prev_intr
        expect(len(calls_on) == 1 and calls_on[0].get("provider") == "google_calendar",
               "approve with execution on fires ONLY the calendar_create (email never sent)")
        ex = (r_on.get("interrupt") or {}).get("executed") or []
        expect(len(ex) == 2
               and any(e.get("type") == "calendar_create" and e.get("ok") for e in ex)
               and any(e.get("type") == "email_draft" and e.get("ok") is False for e in ex),
               "executed records the fired calendar event + the not-sent email draft")

        # ---- config sanity ----
        expect(isinstance(settings.agent_runtime_max_steps, int)
               and isinstance(settings.agent_delegation_max_parallel, int),
               "agent runtime config values are present")

        # ---- K) backend selection + OpenAI/vLLM bridge (tool loop migration) ----
        print("K) agent backend selection + openai bridge")
        prev = (settings.dgx_agent_backend, settings.dgx_openai_endpoint,
                settings.dgx_openai_model, settings.dgx_chat_model_name,
                settings.dgx_model_name, settings.dgx_model_endpoint)
        try:
            settings.dgx_agent_backend = ""
            settings.dgx_model_endpoint = "http://dgx:11434"
            settings.dgx_chat_model_name = "cora-qwen3:4b"
            expect(ar._agent_backend() == "ollama"
                   and ar._agent_endpoint() == "http://dgx:11434"
                   and ar._agent_model() == "cora-qwen3:4b",
                   "unset backend defaults to ollama (DGX endpoint + chat model)")
            settings.dgx_agent_backend = "openai"
            settings.dgx_openai_endpoint = "http://spark-a84c:8000/v1"
            settings.dgx_openai_model = "openai/gpt-oss-120b"
            expect(ar._agent_backend() == "openai"
                   and ar._agent_endpoint() == "http://spark-a84c:8000/v1"
                   and ar._agent_model() == "openai/gpt-oss-120b",
                   "openai backend selects the vLLM endpoint + model")
        finally:
            (settings.dgx_agent_backend, settings.dgx_openai_endpoint,
             settings.dgx_openai_model, settings.dgx_chat_model_name,
             settings.dgx_model_name, settings.dgx_model_endpoint) = prev

        # The canonical (Ollama-shaped) running thread -> OpenAI messages: ids are
        # synthesized and each tool result is paired to its call IN ORDER, and
        # object arguments become a JSON string.
        thread = [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "U"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "t1", "arguments": {"a": 1}}},
                {"function": {"name": "t2", "arguments": {"b": 2}}},
            ]},
            {"role": "tool", "content": "obs1"},
            {"role": "tool", "content": "obs2"},
            {"role": "assistant", "content": "done"},
        ]
        oai = ar._to_openai_messages(thread)
        a_turn = oai[2]
        expect(a_turn["role"] == "assistant" and len(a_turn["tool_calls"]) == 2
               and a_turn["tool_calls"][0]["id"] == "call_0_0"
               and a_turn["tool_calls"][1]["id"] == "call_0_1"
               and a_turn["tool_calls"][0]["type"] == "function",
               "assistant tool_calls get synthesized ids")
        expect(ar._parse_args(a_turn["tool_calls"][0]["function"]["arguments"]) == {"a": 1},
               "object tool-call arguments are serialized to a JSON string")
        expect(oai[3] == {"role": "tool", "tool_call_id": "call_0_0", "content": "obs1"}
               and oai[4]["tool_call_id"] == "call_0_1",
               "each tool result is paired to its call id in order")
        expect(oai[0] == {"role": "system", "content": "S"}
               and oai[5] == {"role": "assistant", "content": "done"},
               "system/user and a tool-less assistant turn pass through unchanged")

        # An OpenAI chat-completions reply -> canonical {"message": {...}} the loop reads.
        resp = {"choices": [{"message": {"content": "hi", "tool_calls": [
            {"id": "call_x", "type": "function",
             "function": {"name": "web_search", "arguments": "{\"q\": \"x\"}"}},
        ]}}]}
        norm = ar._normalize_openai_response(resp)["message"]
        expect(norm["content"] == "hi"
               and norm["tool_calls"][0]["function"]["name"] == "web_search"
               and ar._parse_args(norm["tool_calls"][0]["function"]["arguments"]) == {"q": "x"},
               "openai response normalizes to the Ollama-shaped message + tool_calls")
        expect(ar._normalize_openai_response({})["message"] == {"content": ""}
               and ar._normalize_openai_response({"choices": []})["message"] == {"content": ""},
               "a malformed/empty openai response normalizes to empty content (no raise)")

        # ---- L) evaluator-gated approval (ties Phase 6 + 7) ----
        # A run paused at waiting_user carrying a 'fail' verdict: approve is blocked
        # only when agent_eval_gate_enabled is on, and override forces it through.
        print("L) evaluator-gated approval")
        prev_gate = settings.agent_eval_gate_enabled
        prev_exec_l = settings.agent_execution_enabled
        try:
            settings.agent_execution_enabled = False  # isolate the gate from firing

            # Gate OFF: a 'fail' verdict approves normally (unchanged behavior).
            gate_run_id = await ar.create_pending_run(
                goal="gate off + fail verdict", user_id=uid, session_id=sess, max_steps=6)
            await ar._pause_run(
                gate_run_id, answer="x", stopped="final", tool_calls=0, step_count=0,
                messages=[], steps=[], evaluation={"verdict": "fail", "reasons": [], "summary": ""},
                interrupt={"staged": [], "decision": None, "note": None})
            settings.agent_eval_gate_enabled = False
            r = await ar.resolve_interrupt(gate_run_id, user_id=uid, decision="approve")
            expect(bool(r) and not r.get("blocked") and r["status"] == "done",
                   "gate OFF: a 'fail' verdict approves normally")

            # Gate ON: a 'fail' verdict blocks approve (no state change, nothing fired).
            settings.agent_eval_gate_enabled = True
            gate_run_id2 = await ar.create_pending_run(
                goal="gate on + fail verdict", user_id=uid, session_id=sess, max_steps=6)
            await ar._pause_run(
                gate_run_id2, answer="x", stopped="final", tool_calls=0, step_count=0,
                messages=[], steps=[], evaluation={"verdict": "fail", "reasons": ["broken"], "summary": ""},
                interrupt={"staged": [], "decision": None, "note": None})
            blocked = await ar.resolve_interrupt(gate_run_id2, user_id=uid, decision="approve")
            expect(isinstance(blocked, dict) and blocked.get("blocked") is True
                   and blocked.get("verdict") == "fail",
                   "gate ON: a 'fail' verdict blocks approve (returns blocked marker)")
            still = await ar.get_run(gate_run_id2, user_id=uid)
            expect(still["status"] == "waiting_user"
                   and (still.get("interrupt") or {}).get("decision") is None,
                   "a blocked approve leaves the run untouched (still waiting_user)")

            # Reject is NEVER gated.
            rej = await ar.resolve_interrupt(gate_run_id2, user_id=uid, decision="reject")
            expect(bool(rej) and rej["status"] == "cancelled",
                   "gate ON: reject is never blocked")

            # Override forces a 'fail' approve through and records the override.
            gate_run_id3 = await ar.create_pending_run(
                goal="gate on + override", user_id=uid, session_id=sess, max_steps=6)
            await ar._pause_run(
                gate_run_id3, answer="x", stopped="final", tool_calls=0, step_count=0,
                messages=[], steps=[], evaluation={"verdict": "fail", "reasons": [], "summary": ""},
                interrupt={"staged": [], "decision": None, "note": None})
            ovr = await ar.resolve_interrupt(
                gate_run_id3, user_id=uid, decision="approve", override=True)
            expect(bool(ovr) and not ovr.get("blocked") and ovr["status"] == "done"
                   and (ovr.get("interrupt") or {}).get("override") is True,
                   "override=True forces a 'fail' approve through + records the override")

            # A non-'fail' verdict is never gated even with the gate on.
            gate_run_id4 = await ar.create_pending_run(
                goal="gate on + concerns verdict", user_id=uid, session_id=sess, max_steps=6)
            await ar._pause_run(
                gate_run_id4, answer="x", stopped="final", tool_calls=0, step_count=0,
                messages=[], steps=[], evaluation={"verdict": "concerns", "reasons": [], "summary": ""},
                interrupt={"staged": [], "decision": None, "note": None})
            okc = await ar.resolve_interrupt(gate_run_id4, user_id=uid, decision="approve")
            expect(bool(okc) and not okc.get("blocked") and okc["status"] == "done",
                   "gate ON: a 'concerns' verdict approves normally (only 'fail' is gated)")
        finally:
            settings.agent_eval_gate_enabled = prev_gate
            settings.agent_execution_enabled = prev_exec_l

        # ---- M) calendar UPDATE / DELETE firing (extends Phase 7 beyond create) ----
        # _collect_staged builds fireable calendar_update / calendar_delete items from
        # the staging tool args; the fire helpers are fail-closed + never raise; and
        # _fire_staged routes each kind to its matching helper. No live write here —
        # the helpers are spied or hit with an unknown provider (same as Part J).
        print("M) calendar update/delete firing")
        upd = ar._collect_staged([
            ar.AgentStep("tool_result", {
                "name": "chronos_update_calendar_event",
                "arguments": {"provider": "google_calendar", "event_id": "evt_abc",
                              "start_time": "2026-07-01T16:00:00"},
                "result": "✓ Staged a review-only request to UPDATE google_calendar event"}),
        ])
        expect(len(upd) == 1 and upd[0].get("type") == "calendar_update"
               and upd[0].get("provider") == "google_calendar"
               and upd[0].get("event_id") == "evt_abc"
               and upd[0]["fields"].get("start_time") == "2026-07-01T16:00:00"
               and upd[0]["fields"].get("end_time") == "2026-07-01T17:00:00",
               "_collect_staged builds a fireable calendar_update (+ backfilled end)")

        dele = ar._collect_staged([
            ar.AgentStep("tool_result", {
                "name": "chronos_cancel_calendar_event",
                "arguments": {"provider": "outlook_calendar", "event_id": "evt_xyz"},
                "result": "✓ Staged a review-only request to CANCEL outlook_calendar event"}),
        ])
        expect(len(dele) == 1 and dele[0].get("type") == "calendar_delete"
               and dele[0].get("provider") == "outlook_calendar"
               and dele[0].get("event_id") == "evt_xyz",
               "_collect_staged builds a fireable calendar_delete")

        # An update with no changeable field, and a cancel missing the event_id, are
        # NOT fireable (no type) — they stay plain review notes.
        bad = ar._collect_staged([
            ar.AgentStep("tool_result", {
                "name": "chronos_update_calendar_event",
                "arguments": {"provider": "google_calendar", "event_id": "evt_abc"},
                "result": "✓ Staged a review-only request to UPDATE google_calendar event"}),
            ar.AgentStep("tool_result", {
                "name": "chronos_cancel_calendar_event",
                "arguments": {"provider": "google_calendar"},
                "result": "✓ Staged a review-only request to CANCEL google_calendar event"}),
        ])
        expect(len(bad) == 2 and bad[0].get("type") is None and bad[1].get("type") is None,
               "update with no fields / cancel with no event_id are not fireable (no type)")

        upd_noadapter = await ar.chat_calendar.agent_fire_calendar_update(
            provider="verify_no_such_provider", user_id=uid, workspace_id=None,
            event_id="evt_abc", fields={"start_time": "2026-07-01T16:00:00"})
        del_noadapter = await ar.chat_calendar.agent_fire_calendar_delete(
            provider="verify_no_such_provider", user_id=uid, workspace_id=None,
            event_id="evt_abc")
        expect(upd_noadapter["ok"] is False and del_noadapter["ok"] is False,
               "agent_fire_calendar_update/delete are fail-closed + never raise (unknown provider)")
        upd_noid = await ar.chat_calendar.agent_fire_calendar_update(
            provider="google_calendar", user_id=uid, workspace_id=None,
            event_id="", fields={"title": "x"})
        expect(upd_noid["ok"] is False and "event_id" in upd_noid["reason"],
               "agent_fire_calendar_update refuses an empty event_id (no write attempted)")

        # _fire_staged routes each kind to the matching helper. Spy all three so no
        # live write can happen, then assert routing + outcome passthrough.
        orig_c = ar.chat_calendar.agent_fire_calendar_create
        orig_u = ar.chat_calendar.agent_fire_calendar_update
        orig_d = ar.chat_calendar.agent_fire_calendar_delete

        async def _spy_kind(sink, kind):
            async def _fn(**kw):
                sink.append({"kind": kind, **kw})
                return {"ok": True, "reason": kind, "event_id": "evt_fired"}
            return _fn

        routed: list = []
        ar.chat_calendar.agent_fire_calendar_create = await _spy_kind(routed, "create")
        ar.chat_calendar.agent_fire_calendar_update = await _spy_kind(routed, "update")
        ar.chat_calendar.agent_fire_calendar_delete = await _spy_kind(routed, "delete")
        try:
            outcomes = await ar._fire_staged([
                {"tool": "chronos_update_calendar_event", "type": "calendar_update",
                 "provider": "google_calendar", "event_id": "evt_abc",
                 "fields": {"start_time": "2026-07-01T16:00:00"}},
                {"tool": "chronos_cancel_calendar_event", "type": "calendar_delete",
                 "provider": "outlook_calendar", "event_id": "evt_xyz"},
            ], user_id=uid, workspace_id=None)
        finally:
            ar.chat_calendar.agent_fire_calendar_create = orig_c
            ar.chat_calendar.agent_fire_calendar_update = orig_u
            ar.chat_calendar.agent_fire_calendar_delete = orig_d
        expect(len(routed) == 2
               and routed[0]["kind"] == "update" and routed[0]["event_id"] == "evt_abc"
               and routed[1]["kind"] == "delete" and routed[1]["event_id"] == "evt_xyz",
               "_fire_staged routes calendar_update→update helper, calendar_delete→delete helper")
        expect(len(outcomes) == 2
               and outcomes[0]["type"] == "calendar_update" and outcomes[0]["ok"] is True
               and outcomes[1]["type"] == "calendar_delete" and outcomes[1]["ok"] is True,
               "_fire_staged returns one outcome per item, tagged with its type")

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
            for rid in (run_id, spoke_run_id, pause_run_id, reject_run_id,
                        fire_run_id, fire_run_id2, fire_run_id3,
                        gate_run_id, gate_run_id2, gate_run_id3, gate_run_id4):
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
