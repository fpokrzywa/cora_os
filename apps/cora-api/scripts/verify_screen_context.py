"""Durable verification of Screen Context Awareness v0.1.

Under throwaway users (one owner, one stranger), asserts:
  - parse_screen_context sanitizes (strips unsafe chars, caps length, drops
    malformed entities/views)
  - known sections render their description; unknown sections still inject
  - owner-scoped entity resolution: owner sees a draft/intent summary;
    a NON-owner gets the screen line but NO entity details (fail-closed)
  - admin can resolve another user's entity
  - last_entity fallback renders with "most recently had" phrasing
  - unknown entity types are ignored (no crash, no injection)
  - block stays within MAX_BLOCK_CHARS
  - no body beyond the preview cap leaks into the block

Run:
    docker cp apps/cora-api/scripts/verify_screen_context.py cora-api:/tmp/vsc.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vsc.py   # 0=PASS 1=FAIL
"""
import asyncio
import sys
import uuid

from app.clients import clients, init_clients
from app import screen_context as sc

SECRET_TAIL = "SECRET-BODY-TAIL-never-inject"


async def main() -> int:
    await init_clients()
    pool = clients.db_pool
    fails = []
    owner = stranger = None

    def expect(c, m):
        if not c:
            fails.append(m)

    # --- parsing / sanitization (no DB needed) ---
    expect(sc.parse_screen_context(None) is None, "None raw -> None")
    expect(sc.parse_screen_context({"view": ""}) is None, "empty view -> None")
    p = sc.parse_screen_context(
        {"view": "admin-console<script>", "section": "tools/tooling",
         "label": "x" * 500,
         "entity": {"type": "communication_draft", "id": "not-a-uuid"}})
    expect(p and "<" not in p["view"], "unsafe chars stripped")
    expect(p and len(p["label"]) <= 120, "label capped")
    expect(p and "entity" not in p, "malformed entity dropped")

    try:
        async with pool.acquire() as conn:
            owner = await conn.fetchval(
                "INSERT INTO users (email, password_hash, role) VALUES ($1,'x','user') RETURNING id",
                f"verify-sc-{uuid.uuid4()}@example.invalid")
            stranger = await conn.fetchval(
                "INSERT INTO users (email, password_hash, role) VALUES ($1,'x','user') RETURNING id",
                f"verify-sc-{uuid.uuid4()}@example.invalid")
            wid = await conn.fetchval("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")
            draft_id = await conn.fetchval(
                "INSERT INTO communication_drafts (workspace_id, created_by, agent_name, "
                "draft_type, title, recipient_hint, subject, body, status) "
                "VALUES ($1,$2,'SIGNAL','email','t','mark@example.com','Quarterly sync',"
                "$3,'draft') RETURNING id",
                wid, owner, ("A" * 600) + SECRET_TAIL)

        # known section description
        built = await sc.build_screen_context_block(
            {"view": "admin-console", "section": "agents/signal-drafts",
             "label": "Agents · SIGNAL Drafts"},
            user_id=owner, is_admin=False)
        expect(built and "SIGNAL communication drafts review queue" in built[0],
               "known section described")

        # unknown section still injects identifier
        built = await sc.build_screen_context_block(
            {"view": "admin-console", "section": "made/up", "label": "Made Up"},
            user_id=owner, is_admin=False)
        expect(built and "Made Up" in built[0], "unknown section injected by label")

        # owner resolves own draft; preview truncated; tail never leaks
        ctx = {"view": "admin-console", "section": "agents/signal-drafts",
               "entity": {"type": "communication_draft", "id": str(draft_id)}}
        built = await sc.build_screen_context_block(ctx, user_id=owner, is_admin=False)
        expect(built and "Quarterly sync" in built[0], "owner entity resolved")
        expect(built and "They have" in built[0], "open entity phrasing")
        expect(built and SECRET_TAIL not in built[0], "body preview truncated (no tail)")
        expect(built and built[1]["entity_resolved"] is True, "meta entity_resolved")
        expect(built and len(built[0]) <= sc.MAX_BLOCK_CHARS, "block within cap")

        # stranger must NOT see the draft details
        built = await sc.build_screen_context_block(ctx, user_id=stranger, is_admin=False)
        expect(built and "Quarterly sync" not in built[0], "stranger sees no entity")
        expect(built and built[1]["entity_resolved"] is False, "stranger meta unresolved")

        # admin resolves another user's draft
        built = await sc.build_screen_context_block(ctx, user_id=stranger, is_admin=True)
        expect(built and "Quarterly sync" in built[0], "admin resolves any owner")

        # last_entity fallback phrasing
        built = await sc.build_screen_context_block(
            {"view": "chat", "section": "chat",
             "last_entity": {"type": "communication_draft", "id": str(draft_id)}},
            user_id=owner, is_admin=False)
        expect(built and "most recently had" in built[0], "last_entity phrasing")

        # unknown entity type ignored
        built = await sc.build_screen_context_block(
            {"view": "chat", "entity": {"type": "oauth_token", "id": str(draft_id)}},
            user_id=owner, is_admin=False)
        expect(built and built[1]["entity_resolved"] is False, "unknown type ignored")
    finally:
        async with pool.acquire() as conn:
            for u in (owner, stranger):
                if u is not None:
                    await conn.execute("DELETE FROM communication_drafts WHERE created_by=$1", u)
                    await conn.execute("DELETE FROM users WHERE id=$1", u)

    if fails:
        print("RESULT: FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print("RESULT: PASS — screen context sanitized; sections described; entity "
          "resolution owner-scoped (stranger blocked, admin allowed); last_entity "
          "fallback works; previews truncated; unknown types ignored; rows cleaned")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
