"""Embedding service + semantic memory search.

v0.1 design:
  - Embeddings are produced by an Ollama-compatible /api/embeddings endpoint.
  - If EMBEDDING_MODEL_NAME / EMBEDDING_ENDPOINT (or DGX_MODEL_ENDPOINT fallback)
    are not set, the service returns None / disables semantic search.
  - Storage is pgvector when available; falls back to embedding_json (JSONB)
    when not. JSONB storage cannot be searched semantically — it's a placeholder
    so we don't lose embeddings if the extension lands later.
"""

import json
import logging
import time
import uuid
from typing import Optional

import httpx

from app import schema as schema_state
from app.clients import clients
from app.config import settings

logger = logging.getLogger(__name__)

EMBEDDING_TIMEOUT_SECONDS = 30.0
# Cap the text sent to the embedding model. nomic-embed-text (and similar)
# 500 / truncate when the input exceeds their context window; long URL/PDF
# ingests blow past it. ~8000 chars (~2k tokens) embeds the document head,
# which is plenty for retrieval matching. Applied at the single call site.
EMBEDDING_MAX_CHARS = 8000


def is_embedding_configured() -> bool:
    endpoint = settings.embedding_endpoint or settings.dgx_model_endpoint
    return bool(endpoint and settings.embedding_model_name)


def vector_text(values: list[float]) -> str:
    """asyncpg has no built-in codec for pgvector; we pass the literal text
    representation and rely on a ::vector cast in SQL."""
    return "[" + ",".join(format(float(v), ".7g") for v in values) + "]"


async def generate_embedding(text: str) -> Optional[list[float]]:
    """Call the embedding endpoint and return the vector, or None on any
    failure / misconfiguration."""
    if not text or not text.strip():
        return None
    if not is_embedding_configured():
        return None
    if len(text) > EMBEDDING_MAX_CHARS:
        text = text[:EMBEDDING_MAX_CHARS]
    endpoint = (
        settings.embedding_endpoint or settings.dgx_model_endpoint
    ).rstrip("/")
    model = settings.embedding_model_name
    payload = {"model": model, "prompt": text}
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=EMBEDDING_TIMEOUT_SECONDS) as client:
            resp = await client.post(f"{endpoint}/api/embeddings", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.exception(
            "embedding call failed: model=%s endpoint=%s err=%s",
            model,
            endpoint,
            exc,
        )
        return None
    vector = data.get("embedding")
    if vector is None:
        # Some servers nest under {"data": [{"embedding": [...]}]}
        try:
            vector = data["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError):
            vector = None
    if not isinstance(vector, list) or not vector:
        logger.warning(
            "embedding response missing vector: keys=%s",
            list(data.keys()) if isinstance(data, dict) else type(data).__name__,
        )
        return None
    duration_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "embedding generated: model=%s dim=%s duration_ms=%s",
        model,
        len(vector),
        duration_ms,
    )
    return vector


async def _persist_embedding(
    memory_id: uuid.UUID, vector: list[float], model: str
) -> bool:
    if clients.db_pool is None:
        return False
    if schema_state.is_pgvector_available():
        sql = (
            "UPDATE memory_entries "
            "SET embedding = $2::vector, embedding_model = $3, embedded_at = NOW() "
            "WHERE id = $1"
        )
        async with clients.db_pool.acquire() as conn:
            await conn.execute(sql, memory_id, vector_text(vector), model)
    else:
        sql = (
            "UPDATE memory_entries "
            "SET embedding_json = $2, embedding_model = $3, embedded_at = NOW() "
            "WHERE id = $1"
        )
        async with clients.db_pool.acquire() as conn:
            await conn.execute(sql, memory_id, {"vector": vector}, model)
    return True


async def embed_memory_entry(memory_id: uuid.UUID) -> dict:
    """Embed a single memory entry by id. Returns a status dict."""
    if clients.db_pool is None:
        return {"status": "error", "reason": "pool unavailable"}
    async with clients.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, title, content FROM memory_entries WHERE id = $1",
            memory_id,
        )
    if row is None:
        return {"status": "not_found"}
    text = f"{row['title']}\n\n{row['content']}"
    vector = await generate_embedding(text)
    if vector is None:
        return {
            "status": "skipped",
            "reason": "embedding model not configured or call failed",
            "semantic_unavailable": not is_embedding_configured(),
        }
    await _persist_embedding(memory_id, vector, settings.embedding_model_name)
    # Also (re)build chunk-level embeddings for finer-grained retrieval.
    chunks_created = 0
    chunks_embedded = 0
    try:
        from app.memory.chunking import rebuild_memory_chunks

        cres = await rebuild_memory_chunks(memory_id, auto_embed=True)
        chunks_created = cres.get("chunks_created", 0)
        chunks_embedded = cres.get("embedded_count", 0)
    except Exception:
        logger.exception("chunk rebuild failed after embed: id=%s", memory_id)
    return {
        "status": "ok",
        "memory_id": str(memory_id),
        "dim": len(vector),
        "model": settings.embedding_model_name,
        "chunks_created": chunks_created,
        "chunks_embedded": chunks_embedded,
        "storage": "pgvector"
        if schema_state.is_pgvector_available()
        else "jsonb-fallback",
    }


async def embed_missing(limit: int = 100) -> dict:
    if clients.db_pool is None:
        return {"status": "error", "reason": "pool unavailable"}
    if not is_embedding_configured():
        return {
            "status": "skipped",
            "reason": "embedding model not configured",
            "semantic_unavailable": True,
        }
    column = (
        "embedding"
        if schema_state.is_pgvector_available()
        else "embedding_json"
    )
    # Process entries that lack a memory-level embedding OR lack chunks.
    async with clients.db_pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, title, content FROM memory_entries m
            WHERE m.{column} IS NULL
               OR NOT EXISTS (
                   SELECT 1 FROM memory_entry_chunks c
                   WHERE c.memory_entry_id = m.id
               )
            ORDER BY created_at ASC
            LIMIT $1
            """,
            limit,
        )
    counts = {"embedded": 0, "skipped": 0, "errors": 0, "chunks_created": 0}
    from app.memory.chunking import rebuild_memory_chunks

    for row in rows:
        text = f"{row['title']}\n\n{row['content']}"
        vector = await generate_embedding(text)
        if vector is None:
            counts["skipped"] += 1
            continue
        try:
            await _persist_embedding(
                row["id"], vector, settings.embedding_model_name
            )
            counts["embedded"] += 1
        except Exception:
            logger.exception("embed_missing persist failed: id=%s", row["id"])
            counts["errors"] += 1
            continue
        try:
            cres = await rebuild_memory_chunks(row["id"], auto_embed=True)
            counts["chunks_created"] += cres.get("chunks_created", 0)
        except Exception:
            logger.exception("embed_missing chunk rebuild failed: id=%s", row["id"])
    return {
        "status": "ok",
        "scanned": len(rows),
        **counts,
        "storage": "pgvector"
        if schema_state.is_pgvector_available()
        else "jsonb-fallback",
    }


async def semantic_search(
    query: str,
    *,
    limit: int = 10,
    user_id: Optional[uuid.UUID] = None,
    workspace_id: Optional[uuid.UUID] = None,
) -> dict:
    """Returns {status, rows}.
      status: 'ok' | 'unavailable' | 'no_embedding' | 'empty_query'
      rows:   list of memory dicts with `similarity` field (cosine; higher = closer)"""
    if not query or not query.strip():
        return {"status": "empty_query", "rows": []}
    if not schema_state.is_pgvector_available():
        return {"status": "unavailable", "rows": [], "reason": "pgvector not enabled"}
    if not is_embedding_configured():
        return {
            "status": "unavailable",
            "rows": [],
            "reason": "embedding model not configured",
        }
    vector = await generate_embedding(query)
    if vector is None:
        return {"status": "no_embedding", "rows": []}
    if clients.db_pool is None:
        return {"status": "unavailable", "rows": [], "reason": "pool unavailable"}

    # Cosine distance via `<=>`, lower is closer. similarity = 1 - distance.
    qvec = vector_text(vector)
    ws_clause = "" if workspace_id is None else (
        " AND (m.workspace_id = $4 OR m.workspace_id IS NULL)"
    )
    scope_clause = (
        " (m.scope_type = 'global' OR (m.scope_type = 'user' "
        "AND (m.scope_id = $2 OR m.scope_id IS NULL)))"
        # Global news_article entries are a news-briefing artifact, not chat recall
        # material — exclude them so they don't drown out real memories. Mirrors
        # scribe.search_memory; a user's OWN saved news memory still recalls.
        " AND NOT (m.scope_type = 'global' AND m.type = 'news_article')"
    )

    # 1) Chunk search: best chunk per parent that HAS chunk embeddings.
    chunk_sql = f"""
        SELECT * FROM (
            SELECT DISTINCT ON (c.memory_entry_id)
                   m.id, m.source_session_id, m.type, m.title, m.tags,
                   m.importance, m.created_at, m.updated_at, m.scope_type,
                   m.scope_id, m.workspace_id,
                   c.content AS chunk_content, c.chunk_index,
                   k.title AS source_title, k.source_type, k.source_url,
                   (1 - (c.embedding <=> $1::vector)) AS similarity
            FROM memory_entry_chunks c
            JOIN memory_entries m ON m.id = c.memory_entry_id
            LEFT JOIN knowledge_sources k ON k.id = m.source_id
            WHERE c.embedding IS NOT NULL AND{scope_clause}{ws_clause}
            ORDER BY c.memory_entry_id, c.embedding <=> $1::vector ASC
        ) t
        ORDER BY t.similarity DESC
        LIMIT $3
    """
    # 2) Memory-level search, EXCLUDING parents that have any chunk (so chunked
    #    entries come only from the chunk search; un-chunked from here).
    mem_sql = f"""
        SELECT m.id, m.source_session_id, m.type, m.title, m.content, m.tags,
               m.importance, m.created_at, m.updated_at, m.scope_type,
               m.scope_id, m.workspace_id,
               (1 - (m.embedding <=> $1::vector)) AS similarity
        FROM memory_entries m
        WHERE m.embedding IS NOT NULL AND{scope_clause}{ws_clause}
          AND NOT EXISTS (
              SELECT 1 FROM memory_entry_chunks c
              WHERE c.memory_entry_id = m.id AND c.embedding IS NOT NULL
          )
        ORDER BY m.embedding <=> $1::vector ASC
        LIMIT $3
    """
    args = [qvec, user_id, limit] + ([] if workspace_id is None else [workspace_id])
    async with clients.db_pool.acquire() as conn:
        chunk_rows = await conn.fetch(chunk_sql, *args)
        mem_rows = await conn.fetch(mem_sql, *args)

    merged: list[dict] = []
    for r in chunk_rows:
        d = dict(r)
        # Inject the matched chunk as the row content; keep parent metadata.
        d["content"] = d.pop("chunk_content")
        d["via_chunk"] = True
        merged.append(d)
    for r in mem_rows:
        d = dict(r)
        d["via_chunk"] = False
        merged.append(d)
    merged.sort(key=lambda x: x.get("similarity") or 0.0, reverse=True)
    merged = merged[:limit]
    return {
        "status": "ok",
        "rows": merged,
        "used_chunks": any(r.get("via_chunk") for r in merged),
    }
