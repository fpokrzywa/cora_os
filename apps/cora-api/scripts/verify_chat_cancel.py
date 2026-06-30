"""Integration check of barge-in cancellation on the streaming /chat route.

Drives the REAL route via ASGITransport. llm.stream_text is monkeypatched to
yield two deltas then raise CancelledError — exactly what Starlette throws into
the response generator when the client disconnects mid-stream (voice barge-in).
Asserts the server-side cleanup ran: a 'cancelled' llm_chat trace + the partial
assistant turn persisted, and NO 'ok' finalize trace. DB assertions are the
source of truth (they hold regardless of how the client surfaces the re-raise).
Cleans up its own test rows.

    docker cp apps/cora-api/scripts/verify_chat_cancel.py cora-api:/tmp/vcc.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vcc.py     # 0=PASS 1=FAIL
"""
import asyncio
import sys
import uuid

import httpx

from app import llm
from app.auth import create_access_token
from app.clients import clients, init_clients

DELTAS = ["Hello", " there"]


async def main() -> int:
    await init_clients()
    fails: list[str] = []

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
    token = create_access_token(u["id"], u["email"], u["role"] or "admin")

    async def fake_stream(prompt, **kwargs):
        for d in DELTAS:
            yield d
        # Simulate the framework cancelling the generator on client disconnect.
        raise asyncio.CancelledError()

    session_id = str(uuid.uuid4())
    sid = uuid.UUID(session_id)
    got_deltas = 0
    saw_done = False

    orig = llm.stream_text
    llm.stream_text = fake_stream
    try:
        from app.main import app

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            try:
                async with client.stream(
                    "POST", "/chat",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"message": "say hi", "session_id": session_id, "stream": True},
                ) as resp:
                    async for line in resp.aiter_lines():
                        if '"type": "delta"' in line:
                            got_deltas += 1
                        if '"type": "done"' in line:
                            saw_done = True
            except (asyncio.CancelledError, Exception):
                # Expected: the server re-raises after cleanup; the client sees the
                # stream cut. This is a RAISED exception (no task.cancel()), so it
                # is safe to swallow here.
                pass
    finally:
        llm.stream_text = orig

    # Let the shielded cleanup settle (it runs inline, but be safe under ASGITransport).
    await asyncio.sleep(0.3)

    async with clients.db_pool.acquire() as conn:
        trace = await conn.fetchrow(
            "SELECT status, error_message FROM runtime_traces "
            "WHERE session_id=$1 AND trace_type='llm_chat' ORDER BY created_at DESC LIMIT 1",
            sid)
        assistant = await conn.fetchval(
            "SELECT content FROM messages WHERE session_id=$1 AND role='assistant' "
            "ORDER BY created_at DESC LIMIT 1", sid)
        ok_traces = await conn.fetchval(
            "SELECT COUNT(*) FROM runtime_traces WHERE session_id=$1 "
            "AND trace_type='llm_chat' AND status='ok'", sid)

    print(f"\n  (streamed {got_deltas} delta frame(s) before cut; saw_done={saw_done})")
    expect(not saw_done, "no 'done' frame emitted on a cancelled stream")
    expect(trace is not None and trace["status"] == "cancelled",
           "a 'cancelled' llm_chat trace was written")
    expect(trace is not None and "barge-in" in (trace["error_message"] or ""),
           "cancelled trace notes the barge-in")
    expect(assistant == "Hello there",
           "partial assistant turn persisted with the server-streamed text")
    expect(ok_traces == 0, "no 'ok' finalize trace for a cancelled stream")

    # ---- cleanup test rows ----
    async with clients.db_pool.acquire() as conn:
        await conn.execute("DELETE FROM runtime_traces WHERE session_id=$1", sid)
        await conn.execute("DELETE FROM conversations WHERE session_id=$1", sid)  # cascades messages

    print()
    if fails:
        print(f"FAIL ({len(fails)}): " + "; ".join(fails))
        return 1
    print("PASS: barge-in cancellation verified (trace + partial persist, no done)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
