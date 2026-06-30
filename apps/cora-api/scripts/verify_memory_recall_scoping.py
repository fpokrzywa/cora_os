"""Verify global news_article entries are kept OUT of chat memory recall.

The news-briefing pipeline ingests every article as a GLOBAL memory_entry
(type='news_article'); ~96% of the global pool is news, which drowned real
memories out of personal recall. search_memory (keyword) and semantic_search
(vector) now both exclude `scope_type='global' AND type='news_article'` — while
keeping global curated knowledge AND a user's OWN saved news memory.

DB-backed + deterministic for the keyword path; the semantic path is exercised
only when embeddings + pgvector are live (else SKIPPED). Fixtures carry a unique
marker and are deleted in a finally.
"""

import asyncio
import sys
import uuid

from app.clients import clients, init_clients
from app.agents.scribe import search_memory
from app.memory.embeddings import (
    semantic_search, generate_embedding, vector_text, is_embedding_configured,
)
from app import schema as schema_state

MARK = "zqxrecallmarker"  # distinctive token, present in every fixture
TITLE_TAG = "VERIFY_RECALL_SCOPING"


async def main() -> int:
    await init_clients()
    pool = clients.db_pool
    if pool is None:
        print("FAIL: no Postgres pool")
        return 1

    fails: list[str] = []

    def expect(cond: bool, msg: str) -> None:
        print(f"  {'ok  ' if cond else 'FAIL'} {msg}")
        if not cond:
            fails.append(msg)

    uid = uuid.uuid4()  # disposable "user" that owns the user-scoped fixtures
    ids: dict[str, uuid.UUID] = {}
    try:
        async with pool.acquire() as conn:
            async def ins(key, scope_type, scope_id, mtype):
                row = await conn.fetchrow(
                    "INSERT INTO memory_entries (type, title, content, scope_type, "
                    "scope_id, importance) VALUES ($1,$2,$3,$4,$5,3) RETURNING id",
                    mtype, f"{TITLE_TAG} {key}", f"a fact about {MARK} thing",
                    scope_type, scope_id)
                ids[key] = row["id"]
            await ins("g_news", "global", None, "news_article")    # excluded
            await ins("g_arch", "global", None, "global_architecture_doc")  # kept
            await ins("u_mine", "user", uid, "note")               # kept
            await ins("u_news", "user", uid, "news_article")       # kept (user's own)

        # ---- keyword recall (deterministic) ----
        kw = await search_memory(MARK, limit=50, user_id=uid)
        kw_ids = {r["id"] for r in kw}
        expect(ids["g_news"] not in kw_ids,
               "keyword: a GLOBAL news_article is excluded from recall")
        expect(ids["g_arch"] in kw_ids,
               "keyword: a GLOBAL non-news (architecture) memory still recalls")
        expect(ids["u_mine"] in kw_ids,
               "keyword: the user's own memory still recalls")
        expect(ids["u_news"] in kw_ids,
               "keyword: the user's OWN news_article still recalls (only globals excluded)")

        # ---- semantic recall (best-effort; needs live embeddings + pgvector) ----
        # The pgvector-available flag is set at app startup (init_schema), not in a
        # bare script — set it from a read-only extension probe so semantic runs.
        async with pool.acquire() as conn:
            if await conn.fetchval("SELECT 1 FROM pg_extension WHERE extname='vector'"):
                schema_state.PGVECTOR_AVAILABLE = True
        if schema_state.is_pgvector_available() and is_embedding_configured():
            vec = await generate_embedding(f"a fact about {MARK} thing")
            if vec is not None:
                async with pool.acquire() as conn:
                    for key in ("g_news", "g_arch", "u_mine", "u_news"):
                        await conn.execute(
                            "UPDATE memory_entries SET embedding = $1::vector "
                            "WHERE id = $2", vector_text(vec), ids[key])
                sem = await semantic_search(f"{MARK} fact", limit=50, user_id=uid)
                sem_ids = {r["id"] for r in sem.get("rows", [])}
                expect(sem["status"] == "ok", "semantic: search ran (status ok)")
                expect(ids["g_news"] not in sem_ids,
                       "semantic: a GLOBAL news_article is excluded from recall")
                expect(ids["g_arch"] in sem_ids and ids["u_mine"] in sem_ids
                       and ids["u_news"] in sem_ids,
                       "semantic: global-arch + user-own + user-news all still recall")
            else:
                print("  SKIP semantic: embedding model returned no vector")
        else:
            print("  SKIP semantic: pgvector/embedding model not live")
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM memory_entries WHERE title LIKE $1", f"{TITLE_TAG}%")

    print()
    if fails:
        print(f"FAIL ({len(fails)}): " + "; ".join(fails))
        return 1
    print("PASS: global news_article excluded from recall; curated + user memories kept")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
