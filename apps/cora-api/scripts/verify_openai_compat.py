"""Verify the OpenAI-compatible façade (POST /v1/chat/completions).

Runs IN the cora-api container against the live route (real model turns —
keep prompts short):
    docker cp scripts/verify_openai_compat.py cora-api:/tmp/v.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/v.py

Asserts: OpenAI chunk framing on the streamed path (role first, content,
finish_reason stop, [DONE]), session affinity via X-Cora-Session-Id (two
calls land in ONE conversation), the non-stream aggregate, and the auth
gate. Self-cleans the conversations it creates.
"""

import asyncio
import json
import os
import uuid

import asyncpg
import httpx

from app.auth import create_access_token

BASE = "http://127.0.0.1:8000"
PATH = "/v1/chat/completions"


def _payload(text: str, stream: bool = True) -> dict:
    return {
        "model": "cora",
        "stream": stream,
        "messages": [
            {"role": "system", "content": "ignored client-side prompt"},
            {"role": "user", "content": text},
        ],
    }


async def _stream_chunks(client: httpx.AsyncClient, payload: dict, headers: dict) -> list[dict]:
    chunks: list[dict] = []
    async with client.stream("POST", PATH, json=payload, headers=headers) as res:
        assert res.status_code == 200, f"stream status {res.status_code}"
        buf = ""
        async for raw in res.aiter_text():
            buf += raw
            while "\n\n" in buf:
                frame, buf = buf.split("\n\n", 1)
                for line in frame.split("\n"):
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        chunks.append({"done_marker": True})
                    elif data:
                        chunks.append(json.loads(data))
    return chunks


async def main() -> None:
    db = await asyncpg.connect(os.environ["DATABASE_URL"])
    sessions_to_clean: list[uuid.UUID] = []
    try:
        row = await db.fetchrow(
            "SELECT id, email, role FROM users ORDER BY created_at LIMIT 1"
        )
        assert row, "no users in DB"
        token = create_access_token(row["id"], row["email"], row["role"])
        auth = {"Authorization": f"Bearer {token}"}
        voice_session = str(uuid.uuid4())

        async with httpx.AsyncClient(base_url=BASE, timeout=120) as c:
            # A) streamed turn — OpenAI chunk framing
            headers = dict(auth)
            headers["X-Cora-Session-Id"] = voice_session
            headers["X-Cora-Speakable"] = "true"
            chunks = await _stream_chunks(
                c, _payload("Reply with just the word pong."), headers
            )
            assert chunks and chunks[-1].get("done_marker"), "missing [DONE]"
            body = [ch for ch in chunks if not ch.get("done_marker")]
            assert all(ch.get("object") == "chat.completion.chunk" for ch in body)
            deltas = [ch["choices"][0]["delta"] for ch in body]
            assert deltas and deltas[0].get("role") == "assistant", "role chunk must be first"
            text = "".join(d.get("content") or "" for d in deltas)
            assert text.strip(), "no content streamed"
            finishes = [ch["choices"][0].get("finish_reason") for ch in body]
            assert finishes[-1] == "stop", f"last finish_reason={finishes[-1]}"
            print(f"A) stream OK — {len(body)} chunks, {len(text)} chars")

            # B) session affinity — same X-Cora-Session-Id → same conversation
            conv = await db.fetchrow(
                """
                SELECT c.session_id, COUNT(m.id) AS n
                FROM conversations c JOIN messages m ON m.session_id = c.session_id
                WHERE c.scope_type = 'user' AND c.scope_id = $1
                GROUP BY c.session_id
                ORDER BY MAX(m.created_at) DESC LIMIT 1
                """,
                row["id"],
            )
            assert conv and conv["n"] == 2, f"expected 2 messages, got {conv}"
            sessions_to_clean.append(conv["session_id"])
            await _stream_chunks(c, _payload("And now just the word ping."), headers)
            n2 = await db.fetchval(
                "SELECT COUNT(*) FROM messages WHERE session_id = $1",
                conv["session_id"],
            )
            assert n2 == 4, f"second turn did not join the session (messages={n2})"
            print(f"B) session affinity OK — one conversation, {n2} messages")

            # C) non-stream aggregate
            r = await c.post(
                PATH, json=_payload("Reply with just the word pong.", stream=False),
                headers=auth,
            )
            assert r.status_code == 200, f"non-stream: {r.status_code} {r.text[:200]}"
            out = r.json()
            assert out["object"] == "chat.completion"
            assert out["choices"][0]["message"]["content"].strip()
            conv2 = await db.fetchval(
                """
                SELECT c.session_id FROM conversations c
                JOIN messages m ON m.session_id = c.session_id
                WHERE c.scope_type = 'user' AND c.scope_id = $1
                GROUP BY c.session_id
                ORDER BY MAX(m.created_at) DESC LIMIT 1
                """,
                row["id"],
            )
            if conv2 and conv2 not in sessions_to_clean:
                sessions_to_clean.append(conv2)
            print("C) non-stream OK")

            # D) auth gate
            r = await c.post(PATH, json=_payload("hi"))
            assert r.status_code in (401, 403), f"anon: {r.status_code}"
            print("D) auth gate OK")

            # E) deterministic short-circuit (briefing handler returns a plain
            # ChatResponse even with stream=true) → must still arrive as
            # OpenAI chunks. This was the live voice failure mode.
            chunks = await _stream_chunks(
                c, _payload("brief me on my day"), headers
            )
            assert chunks and chunks[-1].get("done_marker"), "E: missing [DONE]"
            body = [ch for ch in chunks if not ch.get("done_marker")]
            deltas = [ch["choices"][0]["delta"] for ch in body]
            text = "".join(d.get("content") or "" for d in deltas)
            assert deltas[0].get("role") == "assistant" and text.strip(), "E: empty"
            assert body[-1]["choices"][0].get("finish_reason") == "stop"
            print(f"E) deterministic-handler one-shot OK — {len(text)} chars")
    finally:
        for sid in sessions_to_clean:
            await db.execute("DELETE FROM messages WHERE session_id = $1", sid)
            await db.execute("DELETE FROM conversations WHERE session_id = $1", sid)
        await db.close()
    print("verify_openai_compat: ALL PASS (self-cleaned)")


asyncio.run(main())
