"""Deterministic check of spoken confirm-as-interrupt. NO live model, NO DB writes:
the waiting-run lookup + resolve_interrupt are monkeypatched. Covers the yes/no
classifier, the speakable confirmation + outcome builders, and resolve_pending_for_session
(no-pending / unclear / approve / reject / eval-gate blocked / spoken 'override').

    docker cp apps/cora-api/scripts/verify_chat_confirm.py cora-api:/tmp/vcf.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vcf.py     # 0=PASS 1=FAIL
"""
import asyncio
import sys
import uuid

from app import agent_runtime as ar

UID = uuid.uuid4()


async def main() -> int:
    fails: list[str] = []

    def expect(cond: bool, msg: str) -> None:
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # ---- classifier ----
    for t in ("yes", "Yeah", "do it", "go ahead", "approve", "sure, do it", "okay"):
        expect(ar.classify_confirmation(t) == "approve", f"classify approve: {t!r}")
    for t in ("no", "nope", "cancel that", "stop", "never mind", "no, don't"):
        expect(ar.classify_confirmation(t) == "reject", f"classify reject: {t!r}")
    expect(ar.classify_confirmation("do it anyway") == "approve", "override phrase -> approve")
    expect(ar.classify_confirmation("override") == "approve", "'override' -> approve")
    for t in ("maybe", "what time is it", "", "tell me more"):
        expect(ar.classify_confirmation(t) is None, f"classify unclear -> None: {t!r}")

    # ---- speakable confirmation ----
    delete_item = {"type": "calendar_delete", "provider": "google_calendar"}
    c = ar._speakable_confirmation([delete_item])
    expect(c.startswith("I'm about to") and c.endswith("go ahead?")
           and "cancel an event on your Google calendar" in c,
           "delete -> speakable cancel prompt")
    create_item = {"type": "calendar_create", "provider": "outlook_calendar",
                   "fields": {"title": "Budget sync", "start_time": "2026-07-01T15:00:00"}}
    c2 = ar._speakable_confirmation([create_item])
    expect('"Budget sync"' in c2 and "Outlook calendar" in c2 and "2026-07-01T15:00:00" in c2,
           "create -> names title + provider + time")
    c3 = ar._speakable_confirmation([create_item, delete_item])
    expect("; and " in c3, "multiple staged -> joined with '; and'")
    expect(ar._speakable_confirmation([{"tool": "x"}]) == "",
           "no actionable type -> empty confirmation")

    # ---- speakable outcome ----
    expect(ar._speakable_outcome({"interrupt": {}}, "reject") == "Okay — I won't do that.",
           "reject outcome")
    expect("turned off" in ar._speakable_outcome({"interrupt": {}}, "approve"),
           "approve + nothing executed -> execution-off note")
    ok_run = {"interrupt": {"executed": [{"ok": True, "type": "calendar_delete"}]}}
    expect(ar._speakable_outcome(ok_run, "approve").startswith("Done"),
           "approve + executed ok -> Done")
    fail_run = {"interrupt": {"executed": [{"ok": False, "reason": "gate off"}]}}
    expect("couldn't complete" in ar._speakable_outcome(fail_run, "approve"),
           "approve + executed fail -> couldn't complete (reason surfaced)")

    # ---- resolve_pending_for_session (mocked lookup + resolve_interrupt) ----
    orig_find, orig_resolve = ar.find_waiting_run_for_session, ar.resolve_interrupt
    RID = uuid.uuid4()
    calls: list[dict] = []

    async def fake_resolve(run_id, *, user_id, decision, note=None, override=False):
        calls.append({"decision": decision, "override": override})
        if decision == "approve":
            return {"id": run_id, "status": "done", "interrupt": {"decision": "approve"}}
        return {"id": run_id, "status": "cancelled", "interrupt": {"decision": "reject"}}

    async def fake_find_none(_s, _u):
        return None

    async def fake_find(_s, _u):
        return {"id": RID, "interrupt": {"confirmation_prompt": "I'm about to cancel an event. Want me to go ahead?"}}

    ar.resolve_interrupt = fake_resolve
    try:
        ar.find_waiting_run_for_session = fake_find_none
        r0 = await ar.resolve_pending_for_session("sess", UID, "yes")
        expect(r0["pending"] is False and "nothing waiting" in r0["spoken"],
               "no pending run -> pending False")

        ar.find_waiting_run_for_session = fake_find
        r1 = await ar.resolve_pending_for_session("sess", UID, "uhh maybe")
        expect(r1.get("needs_confirmation") and "yes or a no" in r1["spoken"]
               and "Want me to go ahead?" in r1["spoken"],
               "unclear -> re-ask with the confirmation prompt")

        calls.clear()
        r2 = await ar.resolve_pending_for_session("sess", UID, "yes, do it")
        expect(r2.get("resolved") and r2["decision"] == "approve"
               and calls and calls[0]["decision"] == "approve",
               "'yes' -> resolve_interrupt(approve)")

        calls.clear()
        r3 = await ar.resolve_pending_for_session("sess", UID, "no, cancel that")
        expect(r3.get("resolved") and r3["decision"] == "reject"
               and r3["spoken"].startswith("Okay"),
               "'no' -> resolve_interrupt(reject)")

        calls.clear()
        await ar.resolve_pending_for_session("sess", UID, "do it anyway")
        expect(calls and calls[0]["override"] is True,
               "'do it anyway' -> override=True passed to resolve_interrupt")

        async def fake_resolve_blocked(run_id, **k):
            return {"blocked": True, "verdict": "fail", "reason": "blocked"}

        ar.resolve_interrupt = fake_resolve_blocked
        r4 = await ar.resolve_pending_for_session("sess", UID, "yes")
        expect(r4.get("blocked") and "override" in r4["spoken"],
               "eval-gate blocked -> spoken mentions override")
    finally:
        ar.find_waiting_run_for_session = orig_find
        ar.resolve_interrupt = orig_resolve

    print()
    if fails:
        print(f"FAIL ({len(fails)}): " + "; ".join(fails))
        return 1
    print("PASS: spoken confirm-as-interrupt verified")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
