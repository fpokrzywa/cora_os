"""Chunked Embeddings v0.1 — split long memory content into overlapping chunks,
embed each, and store in memory_entry_chunks for finer-grained semantic recall.

Character-based chunking (no tokenizer) for v0.1. The memory-level embedding on
memory_entries is preserved for backward compatibility; retrieval prefers chunks
when present (see embeddings.semantic_search).
"""

import hashlib
import logging
import re
import uuid
from typing import Optional

from app import schema as schema_state
from app.clients import clients
from app.config import settings
from app.memory.embeddings import (
    generate_embedding,
    is_embedding_configured,
    vector_text,
)

logger = logging.getLogger(__name__)

CHUNK_SIZE_CHARS = 4000
CHUNK_OVERLAP_CHARS = 400

_WS_RUN = re.compile(r"[ \t ]+")


def _normalize(text: str) -> str:
    lines = [_WS_RUN.sub(" ", ln).strip() for ln in (text or "").splitlines()]
    out: list[str] = []
    blanks = 0
    for ln in lines:
        if ln:
            out.append(ln)
            blanks = 0
        else:
            blanks += 1
            if blanks <= 1:
                out.append("")
    return "\n".join(out).strip()


def chunk_text(content: str) -> list[str]:
    """Split content into overlapping character chunks. Short content → one
    chunk. Empty → []. Order is preserved by list position."""
    text = _normalize(content)
    if not text:
        return []
    if len(text) <= CHUNK_SIZE_CHARS:
        return [text]
    chunks: list[str] = []
    start = 0
    n = len(text)
    step = max(1, CHUNK_SIZE_CHARS - CHUNK_OVERLAP_CHARS)
    while start < n:
        piece = text[start : start + CHUNK_SIZE_CHARS].strip()
        if piece:
            chunks.append(piece)
        if start + CHUNK_SIZE_CHARS >= n:
            break
        start += step
    return chunks


def _chunk_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _embed_col() -> str:
    return "embedding" if schema_state.is_pgvector_available() else "embedding_json"


async def _persist_chunk_embedding(conn, chunk_id, vector, model) -> None:
    if schema_state.is_pgvector_available():
        await conn.execute(
            "UPDATE memory_entry_chunks SET embedding = $2::vector, "
            "embedding_model = $3, embedded_at = NOW(), updated_at = NOW() "
            "WHERE id = $1",
            chunk_id,
            vector_text(vector),
            model,
        )
    else:
        await conn.execute(
            "UPDATE memory_entry_chunks SET embedding_json = $2, "
            "embedding_model = $3, embedded_at = NOW(), updated_at = NOW() "
            "WHERE id = $1",
            chunk_id,
            {"vector": vector},
            model,
        )


async def rebuild_memory_chunks(
    memory_entry_id: uuid.UUID, auto_embed: bool = True
) -> dict:
    """Delete + recreate chunks for a memory entry, embedding each chunk when
    auto_embed and an embedding model is configured. Returns a status dict."""
    if clients.db_pool is None:
        return {"status": "error", "reason": "pool unavailable"}
    async with clients.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, title, content, source_id, workspace_id "
            "FROM memory_entries WHERE id = $1",
            memory_entry_id,
        )
    if row is None:
        return {"status": "not_found"}

    title = row["title"] or ""
    pieces = chunk_text(row["content"] or "")
    chunk_rows: list[dict] = []
    async with clients.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM memory_entry_chunks WHERE memory_entry_id = $1",
                memory_entry_id,
            )
            for idx, piece in enumerate(pieces):
                cid = await conn.fetchval(
                    """
                    INSERT INTO memory_entry_chunks
                        (memory_entry_id, source_id, workspace_id, chunk_index,
                         content, token_estimate, content_hash)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    RETURNING id
                    """,
                    memory_entry_id,
                    row["source_id"],
                    row["workspace_id"],
                    idx,
                    piece,
                    max(1, len(piece) // 4),
                    _chunk_hash(piece),
                )
                chunk_rows.append({"id": cid, "content": piece})

    embedded = 0
    if auto_embed and is_embedding_configured():
        for cr in chunk_rows:
            # Embed title + chunk so each chunk's vector carries doc context.
            vector = await generate_embedding(f"{title}\n\n{cr['content']}")
            if vector is None:
                continue
            try:
                async with clients.db_pool.acquire() as conn:
                    await _persist_chunk_embedding(
                        conn, cr["id"], vector, settings.embedding_model_name
                    )
                embedded += 1
            except Exception:
                logger.exception("chunk embed persist failed: chunk=%s", cr["id"])

    return {
        "status": "ok",
        "memory_entry_id": str(memory_entry_id),
        "source_id": str(row["source_id"]) if row["source_id"] else None,
        "workspace_id": str(row["workspace_id"]) if row["workspace_id"] else None,
        "chunks_created": len(chunk_rows),
        "embedded_count": embedded,
    }


async def rebuild_missing_chunks(limit: int = 25) -> dict:
    """Rebuild chunks for memory entries that have none yet. Returns counts."""
    if clients.db_pool is None:
        return {"status": "error", "reason": "pool unavailable"}
    async with clients.db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT m.id FROM memory_entries m
            WHERE NOT EXISTS (
                SELECT 1 FROM memory_entry_chunks c WHERE c.memory_entry_id = m.id
            )
            ORDER BY m.created_at ASC
            LIMIT $1
            """,
            limit,
        )
    scanned = len(rows)
    chunks_created = 0
    embedded = 0
    rebuilt = 0
    for r in rows:
        res = await rebuild_memory_chunks(r["id"], auto_embed=True)
        if res.get("status") == "ok":
            rebuilt += 1
            chunks_created += res.get("chunks_created", 0)
            embedded += res.get("embedded_count", 0)
    return {
        "status": "ok",
        "scanned": scanned,
        "rebuilt": rebuilt,
        "chunks_created": chunks_created,
        "embedded_count": embedded,
    }


async def clear_memory_chunks(memory_entry_id: uuid.UUID) -> None:
    """Delete chunks for a memory entry (used when content goes stale)."""
    if clients.db_pool is None:
        return
    async with clients.db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memory_entry_chunks WHERE memory_entry_id = $1",
            memory_entry_id,
        )
