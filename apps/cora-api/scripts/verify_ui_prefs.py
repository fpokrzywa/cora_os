"""Verify /users/me/ui-prefs (cora-ui2 settings persistence).

Runs IN the cora-api container against the live route:
    docker cp scripts/verify_ui_prefs.py cora-api:/tmp/v.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/v.py

Drives GET/PUT round-trip, the 64KB cap, and the auth gate with a minted
token for a real user; the user's pre-existing prefs row is restored.
"""

import asyncio
import os

import asyncpg
import httpx

from app.auth import create_access_token

BASE = "http://127.0.0.1:8000"
PATH = "/users/me/ui-prefs"


async def main() -> None:
    db = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        row = await db.fetchrow(
            "SELECT id, email, role FROM users ORDER BY created_at LIMIT 1"
        )
        assert row, "no users in DB"
        token = create_access_token(row["id"], row["email"], row["role"])
        pre = await db.fetchrow(
            "SELECT prefs FROM user_ui_prefs WHERE user_id = $1", row["id"]
        )

        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(base_url=BASE, headers=headers, timeout=15) as c:
            r0 = await c.get(PATH)
            assert r0.status_code == 200, f"GET: {r0.status_code} {r0.text}"
            assert isinstance(r0.json()["prefs"], dict)

            want = {"cora-theme": "light", "cora-wake-word-enabled": "true"}
            r1 = await c.put(PATH, json={"prefs": want})
            assert r1.status_code == 200, f"PUT: {r1.status_code} {r1.text}"

            r2 = await c.get(PATH)
            assert r2.json()["prefs"] == want, f"round-trip mismatch: {r2.json()}"

            r3 = await c.put(PATH, json={"prefs": {"k": "x" * (70 * 1024)}})
            assert r3.status_code == 413, f"oversize: {r3.status_code}"

        async with httpx.AsyncClient(base_url=BASE, timeout=15) as anon:
            r4 = await anon.get(PATH)
            assert r4.status_code in (401, 403), f"anon: {r4.status_code}"

        # Restore the user's original state.
        if pre is None:
            await db.execute(
                "DELETE FROM user_ui_prefs WHERE user_id = $1", row["id"]
            )
        else:
            await db.execute(
                "UPDATE user_ui_prefs SET prefs = $2::jsonb WHERE user_id = $1",
                row["id"],
                pre["prefs"],
            )
        print("verify_ui_prefs: ALL PASS (round-trip, 64KB cap, auth gate, restored)")
    finally:
        await db.close()


asyncio.run(main())
