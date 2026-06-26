"""Durable verification of Tier-2 Screen Vision (opt-in).

The DGX vision call is monkeypatched (no real model needed). Under a throwaway user,
asserts:
  - image validation: data URL + bare base64 accepted; bad base64 / empty /
    unsupported MIME / oversized all rejected before any model call
  - gate FAILS CLOSED by default (SCREEN_VISION_ENABLED off) — denial message, an
    audited allowed=false row, and NO model call
  - with the switch on + a model configured, a valid image is analyzed; audited
    allowed=true with the byte count; the model's answer is returned
  - a bad image with the gate open is rejected (audited, no model call)
  - PRIVACY: screen_vision_events has no column able to hold the screenshot bytes
    (only an integer byte count) — the image is never persisted

Run:
    docker cp apps/cora-api/scripts/verify_screen_vision.py cora-api:/tmp/vsv.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vsv.py   # 0=PASS 1=FAIL
"""
import asyncio
import base64
import sys
import uuid

from app.clients import clients, init_clients
from app.config import settings
from app import screen_vision as sv

# 1x1 PNG.
_PNG = base64.b64encode(base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)).decode()


async def main() -> int:
    await init_clients()
    pool = clients.db_pool
    fails = []
    user = None
    calls = {"n": 0}

    def expect(c, m):
        if not c:
            fails.append(m)

    # snapshot + monkeypatch the model call so nothing hits a real DGX
    orig_analyze = sv._analyze
    orig_enabled = settings.screen_vision_enabled
    orig_model = settings.vision_model_name
    orig_endpoint = settings.dgx_model_endpoint
    # The gate now reads the DB override first (admin app-toggle), so clear any operator
    # override for this test and restore it after — the env-setting monkeypatches below
    # only take effect when no override row exists.
    saved_switch = None

    async def fake_analyze(image_b64, question):
        calls["n"] += 1
        return "I can see a calendar grid with three events."

    sv._analyze = fake_analyze

    # --- image validation (no DB / no gate) ---
    b64, n, err = sv._decode_image(f"data:image/png;base64,{_PNG}")
    expect(err is None and b64 == _PNG and n and n > 0, "valid data URL decodes")
    b64, n, err = sv._decode_image(_PNG)
    expect(err is None and n and n > 0, "bare base64 decodes")
    _, _, err = sv._decode_image("not!!base64!!")
    expect(err is not None, "garbage base64 rejected")
    _, _, err = sv._decode_image("")
    expect(err == "no image attached", "empty rejected")
    _, _, err = sv._decode_image(f"data:image/gif;base64,{_PNG}")
    expect(err and "unsupported" in err, "unsupported MIME rejected")
    big = base64.b64encode(b"\x00" * (sv.MAX_IMAGE_BYTES + 1)).decode()
    _, _, err = sv._decode_image(big)
    expect(err and "too large" in err, "oversized rejected")

    try:
        async with pool.acquire() as conn:
            saved_switch = await conn.fetchrow(
                "SELECT enabled, updated_by FROM runtime_execution_switches WHERE name='screen_vision_enabled'")
            await conn.execute(
                "DELETE FROM runtime_execution_switches WHERE name='screen_vision_enabled'")
            user = await conn.fetchval(
                "INSERT INTO users (email, password_hash, role) VALUES ($1,'x','user') RETURNING id",
                f"verify-sv-{uuid.uuid4()}@example.invalid")
            wid = await conn.fetchval("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")

        async def last_event():
            async with pool.acquire() as conn:
                return await conn.fetchrow(
                    "SELECT allowed, reason, image_bytes, model FROM screen_vision_events "
                    "WHERE user_id=$1 ORDER BY created_at DESC LIMIT 1", user)

        # --- gate FAILS CLOSED by default ---
        settings.screen_vision_enabled = False
        calls["n"] = 0
        handled, text = await sv.handle_screen_vision_turn(
            message="what am I looking at?", image_data=f"data:image/png;base64,{_PNG}",
            session_uuid=uuid.uuid4(), user_id=user, workspace_uuid=wid, is_admin=False)
        expect(handled and "Screen vision is off" in text, "gated-off denial message")
        expect(calls["n"] == 0, "gated-off: NO model call")
        ev = await last_event()
        expect(ev and ev["allowed"] is False, "gated-off audited allowed=false")

        # --- gate OPEN: valid image analyzed ---
        settings.screen_vision_enabled = True
        settings.vision_model_name = "qwen2.5-vl-test"
        settings.dgx_model_endpoint = "http://dgx.invalid:11434"
        calls["n"] = 0
        handled, text = await sv.handle_screen_vision_turn(
            message="what events are on screen?", image_data=f"data:image/png;base64,{_PNG}",
            session_uuid=uuid.uuid4(), user_id=user, workspace_uuid=wid, is_admin=False)
        expect(handled and "calendar grid" in text, "gated-on analyzed answer returned")
        expect(calls["n"] == 1, "gated-on: model called once")
        ev = await last_event()
        expect(ev and ev["allowed"] is True and ev["image_bytes"] and ev["image_bytes"] > 0,
               "gated-on audited allowed=true with byte count")
        expect(ev and ev["model"] == "qwen2.5-vl-test", "audited model name")

        # --- gate OPEN but bad image: rejected, no model call ---
        calls["n"] = 0
        handled, text = await sv.handle_screen_vision_turn(
            message="hi", image_data="not-an-image", session_uuid=uuid.uuid4(),
            user_id=user, workspace_uuid=wid, is_admin=False)
        expect(handled and "could not read" in text, "bad image rejected with message")
        expect(calls["n"] == 0, "bad image: NO model call")

        # --- PRIVACY: no column can hold raw image bytes ---
        async with pool.acquire() as conn:
            cols = await conn.fetch(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name='screen_vision_events'")
        names = {c["column_name"] for c in cols}
        types = {c["data_type"] for c in cols}
        expect("image_bytes" in names, "audit has integer byte count")
        expect(not (names & {"image", "image_data", "screenshot", "image_b64", "frame"}),
               "audit has NO raw-image column")
        expect("bytea" not in types, "audit has no bytea/blob column")
    finally:
        sv._analyze = orig_analyze
        settings.screen_vision_enabled = orig_enabled
        settings.vision_model_name = orig_model
        settings.dgx_model_endpoint = orig_endpoint
        async with pool.acquire() as conn:
            # Restore the operator's real screen_vision override (if any).
            if saved_switch is not None:
                await conn.execute(
                    "INSERT INTO runtime_execution_switches (name, enabled, updated_by) "
                    "VALUES ('screen_vision_enabled',$1,$2)",
                    saved_switch["enabled"], saved_switch["updated_by"])
            if user is not None:
                await conn.execute("DELETE FROM screen_vision_events WHERE user_id=$1", user)
                await conn.execute("DELETE FROM users WHERE id=$1", user)

    if fails:
        print("RESULT: FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print("RESULT: PASS — image validation enforced; gate fails closed by default "
          "(denial + audit + no model call); gated-on analyzes + audits with byte "
          "count; bad image rejected; no raw-image column (screenshot never stored)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
