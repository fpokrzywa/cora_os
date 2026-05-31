import logging
import uuid
from datetime import datetime
from typing import Annotated, Literal, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.agents.scribe import (
    load_session_messages,
    search_memory,
    summarize_messages,
)
from app.auth import CurrentUser, get_current_user, require_admin
from app.clients import clients
from app.config import settings
from app.memory import (
    embed_memory_entry,
    embed_missing,
    is_embedding_configured,
    semantic_search,
)
from app.memory.chunking import rebuild_memory_chunks, rebuild_missing_chunks
from app.runtime_traces import write_trace
from app import schema as schema_state

CONTENT_PREVIEW_CHARS = 240

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memory", tags=["memory"])


class MemoryEntryOut(BaseModel):
    id: str
    source_session_id: Optional[str]
    type: str
    title: str
    content: str
    tags: list[str]
    importance: int
    created_at: datetime
    updated_at: datetime


class MemorySearchResultOut(BaseModel):
    id: str
    title: str
    type: str
    content_preview: str
    tags: list[str]
    importance: int
    created_at: datetime


class SemanticSearchResultOut(BaseModel):
    id: str
    title: str
    type: str
    content_preview: str
    tags: list[str]
    importance: int
    scope_type: str
    workspace_id: Optional[str]
    similarity: float
    created_at: datetime


class SemanticSearchResponse(BaseModel):
    status: str
    semantic_unavailable: bool
    reason: Optional[str] = None
    results: list[SemanticSearchResultOut]


class EmbedResponse(BaseModel):
    status: str
    semantic_unavailable: bool
    detail: dict


class EmbeddingsStatusResponse(BaseModel):
    pgvector_available: bool
    embedding_configured: bool
    embedding_model_name: Optional[str]
    embedding_endpoint: Optional[str]
    embedding_dim: int
    total_entries: int
    embedded_entries: int
    missing_count: int
    storage: str


def _preview(content: str, max_chars: int = CONTENT_PREVIEW_CHARS) -> str:
    if len(content) <= max_chars:
        return content
    return content[:max_chars].rstrip() + "…"


def _require_pool():
    if clients.db_pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Postgres pool unavailable",
        )
    return clients.db_pool


def _row_to_memory(row) -> MemoryEntryOut:
    src = row["source_session_id"]
    return MemoryEntryOut(
        id=str(row["id"]),
        source_session_id=str(src) if src is not None else None,
        type=row["type"],
        title=row["title"],
        content=row["content"],
        tags=list(row["tags"]) if row["tags"] is not None else [],
        importance=row["importance"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.get(
    "",
    response_model=list[MemoryEntryOut],
    summary="List memory entries (most recent first)",
)
async def list_memory(
    current: Annotated[CurrentUser, Depends(get_current_user)],
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[MemoryEntryOut]:
    pool = _require_pool()
    logger.info(
        "list memory: user_id=%s scope_filter=global+user(%s)+legacy_null "
        "limit=%s offset=%s",
        current.id,
        current.id,
        limit,
        offset,
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, source_session_id, type, title, content, tags,
                   importance, created_at, updated_at, scope_type, scope_id
            FROM memory_entries
            WHERE scope_type = 'global'
               OR (
                      scope_type = 'user'
                      AND (scope_id = $3 OR scope_id IS NULL)
                  )
            ORDER BY created_at DESC
            LIMIT $1 OFFSET $2
            """,
            limit,
            offset,
            current.id,
        )
    scoped = sum(1 for r in rows if r["scope_type"] == "user" and r["scope_id"] is not None)
    legacy = sum(1 for r in rows if r["scope_type"] == "user" and r["scope_id"] is None)
    global_count = sum(1 for r in rows if r["scope_type"] == "global")
    logger.info(
        "list memory result: user_id=%s returned=%s scoped=%s legacy_null=%s global=%s",
        current.id,
        len(rows),
        scoped,
        legacy,
        global_count,
    )
    return [_row_to_memory(r) for r in rows]


@router.get(
    "/search",
    response_model=list[MemorySearchResultOut],
    summary="Keyword search across memory titles, content, and tags",
)
async def search_memory_endpoint(
    current: Annotated[CurrentUser, Depends(get_current_user)],
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(default=10, ge=1, le=50),
) -> list[MemorySearchResultOut]:
    rows = await search_memory(q, limit=limit, user_id=current.id)
    logger.info(
        "memory search: user_id=%s scope_filter=global+user(%s) q=%r limit=%s matches=%s",
        current.id,
        current.id,
        q,
        limit,
        len(rows),
    )
    return [
        MemorySearchResultOut(
            id=str(r["id"]),
            title=r["title"],
            type=r["type"],
            content_preview=_preview(r["content"]),
            tags=list(r["tags"]) if r["tags"] is not None else [],
            importance=r["importance"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


@router.get(
    "/semantic-search",
    response_model=SemanticSearchResponse,
    summary="Vector / embedding nearest-neighbor search over memory.",
)
async def semantic_search_endpoint(
    current: Annotated[CurrentUser, Depends(get_current_user)],
    q: str = Query(..., min_length=1),
    limit: int = Query(default=10, ge=1, le=50),
    workspace_id: Optional[str] = Query(default=None),
) -> SemanticSearchResponse:
    ws_uuid: Optional[uuid.UUID] = None
    if workspace_id:
        try:
            ws_uuid = uuid.UUID(workspace_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="workspace_id must be a valid UUID",
            ) from exc

    result = await semantic_search(
        q, limit=limit, user_id=current.id, workspace_id=ws_uuid
    )
    semantic_unavailable = result["status"] in (
        "unavailable",
        "no_embedding",
    )
    rows = result.get("rows", [])
    logger.info(
        "memory semantic-search: user_id=%s status=%s rows=%s pgvector=%s",
        current.id,
        result["status"],
        len(rows),
        schema_state.is_pgvector_available(),
    )
    return SemanticSearchResponse(
        status=result["status"],
        semantic_unavailable=semantic_unavailable,
        reason=result.get("reason"),
        results=[
            SemanticSearchResultOut(
                id=str(r["id"]),
                title=r["title"],
                type=r["type"],
                content_preview=_preview(r["content"]),
                tags=list(r["tags"]) if r["tags"] is not None else [],
                importance=r["importance"],
                scope_type=r["scope_type"],
                workspace_id=str(r["workspace_id"]) if r.get("workspace_id") else None,
                similarity=float(r["similarity"]),
                created_at=r["created_at"],
            )
            for r in rows
        ],
    )


@router.post(
    "/embed-missing",
    response_model=EmbedResponse,
    summary="Embed every memory entry that doesn't yet have an embedding. Admin only.",
)
async def embed_missing_endpoint(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    limit: int = Query(default=100, ge=1, le=1000),
) -> EmbedResponse:
    detail = await embed_missing(limit=limit)
    semantic_unavailable = (
        not is_embedding_configured()
        or not schema_state.is_pgvector_available()
    )
    logger.info(
        "memory embed-missing: admin=%s detail=%s", admin.id, detail
    )
    return EmbedResponse(
        status=detail.get("status", "unknown"),
        semantic_unavailable=semantic_unavailable,
        detail=detail,
    )


@router.get(
    "/embeddings/status",
    response_model=EmbeddingsStatusResponse,
    summary="Embedding subsystem health + counts.",
)
async def embeddings_status_endpoint(
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> EmbeddingsStatusResponse:
    pool = _require_pool()
    pgvector = schema_state.is_pgvector_available()
    column = "embedding" if pgvector else "embedding_json"
    async with pool.acquire() as conn:
        total = int(await conn.fetchval("SELECT COUNT(*) FROM memory_entries") or 0)
        embedded = int(
            await conn.fetchval(
                f"SELECT COUNT(*) FROM memory_entries WHERE {column} IS NOT NULL"
            )
            or 0
        )
    return EmbeddingsStatusResponse(
        pgvector_available=pgvector,
        embedding_configured=is_embedding_configured(),
        embedding_model_name=settings.embedding_model_name or None,
        embedding_endpoint=(
            settings.embedding_endpoint or settings.dgx_model_endpoint or None
        ),
        embedding_dim=settings.embedding_dim,
        total_entries=total,
        embedded_entries=embedded,
        missing_count=max(0, total - embedded),
        storage="pgvector" if pgvector else "jsonb-fallback",
    )


@router.post(
    "/{memory_id}/embed",
    response_model=EmbedResponse,
    summary="(Re)compute the embedding for one memory entry.",
)
async def embed_one_endpoint(
    memory_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> EmbedResponse:
    try:
        mid = uuid.UUID(memory_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="memory_id must be a valid UUID",
        ) from exc
    detail = await embed_memory_entry(mid)
    semantic_unavailable = (
        not is_embedding_configured()
        or not schema_state.is_pgvector_available()
    )
    logger.info(
        "memory embed: user_id=%s memory_id=%s status=%s",
        current.id,
        mid,
        detail.get("status"),
    )
    if detail.get("status") == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="memory entry not found"
        )
    return EmbedResponse(
        status=detail.get("status", "unknown"),
        semantic_unavailable=semantic_unavailable,
        detail=detail,
    )


@router.post(
    "/{memory_id}/chunks/rebuild",
    response_model=EmbedResponse,
    summary="(Re)build + embed chunk-level embeddings for one memory entry.",
)
async def rebuild_chunks_endpoint(
    memory_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> EmbedResponse:
    try:
        mid = uuid.UUID(memory_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="memory_id must be a valid UUID",
        ) from exc
    detail = await rebuild_memory_chunks(mid, auto_embed=True)
    if detail.get("status") == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="memory entry not found"
        )
    await write_trace(
        session_id=None,
        user_id=current.id,
        trace_type="memory_chunks_rebuilt",
        status="ok" if detail.get("status") == "ok" else "error",
        selected_agent="SCRIBE",
        tool_name="memory_chunks",
        tool_result={
            "memory_entry_id": str(mid),
            "source_id": detail.get("source_id"),
            "chunks_created": detail.get("chunks_created", 0),
            "embedded_count": detail.get("embedded_count", 0),
        },
        workspace_id=(
            uuid.UUID(detail["workspace_id"]) if detail.get("workspace_id") else None
        ),
    )
    return EmbedResponse(
        status=detail.get("status", "unknown"),
        semantic_unavailable=not is_embedding_configured()
        or not schema_state.is_pgvector_available(),
        detail=detail,
    )


@router.post(
    "/chunks/rebuild-missing",
    response_model=EmbedResponse,
    summary="Build chunks for memory entries that have none yet. Admin only.",
)
async def rebuild_missing_chunks_endpoint(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    limit: int = Query(default=25, ge=1, le=500),
) -> EmbedResponse:
    detail = await rebuild_missing_chunks(limit=limit)
    logger.info("memory chunks rebuild-missing: admin=%s detail=%s", admin.id, detail)
    await write_trace(
        session_id=None,
        user_id=admin.id,
        trace_type="memory_chunks_rebuild_missing",
        status="ok" if detail.get("status") == "ok" else "error",
        selected_agent="SCRIBE",
        tool_name="memory_chunks",
        tool_result={
            "scanned": detail.get("scanned", 0),
            "rebuilt": detail.get("rebuilt", 0),
            "chunks_created": detail.get("chunks_created", 0),
            "embedded_count": detail.get("embedded_count", 0),
        },
    )
    return EmbedResponse(
        status=detail.get("status", "unknown"),
        semantic_unavailable=not is_embedding_configured()
        or not schema_state.is_pgvector_available(),
        detail=detail,
    )


@router.get(
    "/{memory_id}",
    response_model=MemoryEntryOut,
    summary="Get a single memory entry",
)
async def get_memory(
    memory_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> MemoryEntryOut:
    try:
        mid = uuid.UUID(memory_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="memory_id must be a valid UUID",
        ) from exc

    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, source_session_id, type, title, content, tags,
                   importance, created_at, updated_at, scope_type, scope_id
            FROM memory_entries
            WHERE id = $1
              AND (
                      scope_type = 'global'
                      OR (
                          scope_type = 'user'
                          AND (scope_id = $2 OR scope_id IS NULL)
                      )
                  )
            """,
            mid,
            current.id,
        )
    if row is None:
        # 404 (not 403) — don't reveal whether the id exists in another scope
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="memory entry not found",
        )
    logger.info(
        "get memory: user_id=%s memory_id=%s scope_type=%s scope_id=%s%s",
        current.id,
        mid,
        row["scope_type"],
        row["scope_id"],
        " (legacy null)" if row["scope_type"] == "user" and row["scope_id"] is None else "",
    )
    return _row_to_memory(row)


@router.post(
    "/summarize/{session_id}",
    response_model=MemoryEntryOut,
    status_code=status.HTTP_201_CREATED,
    summary="SCRIBE: summarize a session into a durable memory entry",
)
async def summarize_session_into_memory(
    session_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
    scope: Literal["user", "global"] = Query(
        default="user",
        description="Scope to write the memory under. 'global' requires admin role.",
    ),
) -> MemoryEntryOut:
    try:
        session_uuid = uuid.UUID(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="session_id must be a valid UUID",
        ) from exc

    if scope == "global" and current.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin role required to create global memory entries",
        )

    pool = _require_pool()

    # Source session must be owned by the caller (no cross-user summarization)
    async with pool.acquire() as conn:
        owner = await conn.fetchrow(
            """
            SELECT scope_type, scope_id FROM conversations
            WHERE session_id = $1
            """,
            session_uuid,
        )
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"session {session_id} not found",
        )
    if owner["scope_type"] != "user" or owner["scope_id"] != current.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="session belongs to another scope",
        )

    messages = await load_session_messages(session_uuid)
    target_scope_id = None if scope == "global" else current.id
    logger.info(
        "scribe summarize requested: user_id=%s session=%s message_count=%s "
        "target_scope_type=%s target_scope_id=%s",
        current.id,
        session_id,
        len(messages),
        scope,
        target_scope_id,
    )
    if not messages:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"session {session_id} has no messages to summarize",
        )

    if not settings.dgx_model_endpoint:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DGX_MODEL_ENDPOINT is not configured",
        )

    try:
        summary = await summarize_messages(messages)
    except httpx.HTTPError as exc:
        logger.exception(
            "scribe summarize: model call failed session=%s", session_id
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"summarization model call failed: {exc}",
        ) from exc
    except RuntimeError as exc:
        # endpoint missing was already checked, but defend against scribe-side
        logger.exception(
            "scribe summarize: runtime error session=%s", session_id
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    title = f"Summary for session {session_id[:8]}"
    tags = ["scribe", "conversation_summary"]
    if scope == "global":
        tags.append("global")

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO memory_entries (
                    source_session_id, type, title, content, tags, importance,
                    scope_type, scope_id
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id, source_session_id, type, title, content, tags,
                          importance, created_at, updated_at,
                          scope_type, scope_id
                """,
                session_uuid,
                "conversation_summary",
                title,
                summary,
                tags,
                3,
                scope,
                target_scope_id,
            )
    except Exception as exc:
        logger.exception(
            "scribe summarize: persistence failed session=%s", session_id
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to persist memory entry: {exc}",
        ) from exc

    logger.info(
        "scribe summarize complete: user_id=%s session=%s memory_id=%s "
        "scope_type=%s scope_id=%s content_chars=%s",
        current.id,
        session_id,
        row["id"],
        row["scope_type"],
        row["scope_id"],
        len(summary),
    )
    return _row_to_memory(row)
