import logging
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field

from app.agents.scribe import search_memory
from app.auth import (
    CurrentUser,
    create_access_token,
    hash_password,
    require_admin,
)
from app.clients import clients

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

MEMORIES_IN_PROMPT_PREVIEW = 5


# ---------- Models ----------


class AdminUserOut(BaseModel):
    id: str
    email: str
    display_name: Optional[str]
    role: str
    created_at: datetime
    updated_at: datetime


class AdminCreateUserRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    display_name: Optional[str] = Field(default=None, max_length=120)
    role: Literal["admin", "user"] = "user"


class ImpersonationToken(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: AdminUserOut
    impersonated: bool = True


class AdminMemoryOut(BaseModel):
    id: str
    source_session_id: Optional[str]
    type: str
    title: str
    content: str
    tags: list[str]
    importance: int
    scope_type: str
    scope_id: Optional[str]
    created_at: datetime
    updated_at: datetime


class AdminCreateMemoryRequest(BaseModel):
    type: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    importance: int = Field(default=3, ge=1, le=5)
    scope_type: Literal["user", "global", "system"]
    scope_id: Optional[str] = Field(
        default=None,
        description="Required UUID when scope_type='user'; ignored otherwise.",
    )
    source_session_id: Optional[str] = None


class MemoryPreview(BaseModel):
    id: str
    title: str
    type: str
    scope_type: str
    scope_id: Optional[str]
    tags: list[str]
    importance: int
    content_preview: str


class VisibilityTestResponse(BaseModel):
    user_id: str
    user_email: str
    user_role: str
    scope_filter: str
    list_visible_count: int
    list_visible: list[MemoryPreview]
    query: Optional[str]
    search_match_count: int
    search_top_in_prompt: list[MemoryPreview]


# ---------- Helpers ----------


def _require_pool():
    if clients.db_pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Postgres pool unavailable",
        )
    return clients.db_pool


def _user_row_to_out(row) -> AdminUserOut:
    return AdminUserOut(
        id=str(row["id"]),
        email=row["email"],
        display_name=row["display_name"],
        role=row["role"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _memory_row_to_out(row) -> AdminMemoryOut:
    src = row["source_session_id"]
    sid = row["scope_id"]
    return AdminMemoryOut(
        id=str(row["id"]),
        source_session_id=str(src) if src is not None else None,
        type=row["type"],
        title=row["title"],
        content=row["content"],
        tags=list(row["tags"]) if row["tags"] is not None else [],
        importance=row["importance"],
        scope_type=row["scope_type"],
        scope_id=str(sid) if sid is not None else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _memory_row_to_preview(row, preview_chars: int = 200) -> MemoryPreview:
    content = row["content"] or ""
    preview = (
        content
        if len(content) <= preview_chars
        else content[:preview_chars].rstrip() + "…"
    )
    sid = row["scope_id"]
    return MemoryPreview(
        id=str(row["id"]),
        title=row["title"],
        type=row["type"],
        scope_type=row["scope_type"],
        scope_id=str(sid) if sid is not None else None,
        tags=list(row["tags"]) if row["tags"] is not None else [],
        importance=row["importance"],
        content_preview=preview,
    )


# ---------- Users ----------


@router.get(
    "/users",
    response_model=list[AdminUserOut],
    summary="List all users (admin only)",
)
async def list_users(
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> list[AdminUserOut]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, email, display_name, role, created_at, updated_at
            FROM users
            ORDER BY created_at ASC
            """
        )
    logger.info("admin list users: admin=%s count=%s", admin.id, len(rows))
    return [_user_row_to_out(r) for r in rows]


@router.post(
    "/users",
    response_model=AdminUserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user (admin only). Useful for seeding test users.",
)
async def create_user(
    req: AdminCreateUserRequest,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> AdminUserOut:
    pool = _require_pool()
    email_lc = req.email.strip().lower()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO users (email, display_name, password_hash, role)
                VALUES ($1, $2, $3, $4)
                RETURNING id, email, display_name, role, created_at, updated_at
                """,
                email_lc,
                req.display_name,
                hash_password(req.password),
                req.role,
            )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="email already registered",
        ) from exc
    logger.info(
        "admin created user: admin=%s new_user_id=%s email=%s role=%s",
        admin.id,
        row["id"],
        email_lc,
        req.role,
    )
    return _user_row_to_out(row)


@router.post(
    "/users/{user_id}/impersonate",
    response_model=ImpersonationToken,
    summary="Issue a JWT for another user (admin only). For local testing scoped data.",
)
async def impersonate_user(
    user_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> ImpersonationToken:
    try:
        target_uuid = uuid.UUID(user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="user_id must be a valid UUID",
        ) from exc

    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, email, display_name, role, created_at, updated_at
            FROM users WHERE id = $1
            """,
            target_uuid,
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="user not found",
        )
    user = _user_row_to_out(row)
    token = create_access_token(target_uuid, user.email, user.role)
    logger.warning(
        "admin impersonation: admin=%s target_user_id=%s target_email=%s",
        admin.id,
        user.id,
        user.email,
    )
    return ImpersonationToken(access_token=token, user=user)


# ---------- Memory ----------


@router.get(
    "/memory",
    response_model=list[AdminMemoryOut],
    summary="List memory entries across all scopes (admin only)",
)
async def list_all_memory(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    scope_type: Optional[Literal["user", "global", "system"]] = Query(
        default=None, description="Optional scope filter"
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[AdminMemoryOut]:
    pool = _require_pool()
    sql_base = """
        SELECT id, source_session_id, type, title, content, tags,
               importance, created_at, updated_at, scope_type, scope_id
        FROM memory_entries
    """
    if scope_type:
        sql = sql_base + " WHERE scope_type = $3 ORDER BY created_at DESC LIMIT $1 OFFSET $2"
        args: tuple[Any, ...] = (limit, offset, scope_type)
    else:
        sql = sql_base + " ORDER BY created_at DESC LIMIT $1 OFFSET $2"
        args = (limit, offset)

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    logger.info(
        "admin list memory: admin=%s scope_filter=%s count=%s",
        admin.id,
        scope_type or "<any>",
        len(rows),
    )
    return [_memory_row_to_out(r) for r in rows]


@router.post(
    "/memory",
    response_model=AdminMemoryOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a memory entry under any scope (admin only)",
)
async def create_memory(
    req: AdminCreateMemoryRequest,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> AdminMemoryOut:
    scope_id_uuid: Optional[uuid.UUID]
    if req.scope_type == "user":
        if not req.scope_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="scope_id is required when scope_type='user'",
            )
        try:
            scope_id_uuid = uuid.UUID(req.scope_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="scope_id must be a valid UUID",
            ) from exc
    else:
        # global and system are owner-less
        scope_id_uuid = None

    source_uuid: Optional[uuid.UUID]
    if req.source_session_id:
        try:
            source_uuid = uuid.UUID(req.source_session_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="source_session_id must be a valid UUID",
            ) from exc
    else:
        source_uuid = None

    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO memory_entries (
                source_session_id, type, title, content, tags, importance,
                scope_type, scope_id
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id, source_session_id, type, title, content, tags,
                      importance, created_at, updated_at, scope_type, scope_id
            """,
            source_uuid,
            req.type,
            req.title,
            req.content,
            req.tags,
            req.importance,
            req.scope_type,
            scope_id_uuid,
        )
    logger.info(
        "admin created memory: admin=%s memory_id=%s scope_type=%s scope_id=%s",
        admin.id,
        row["id"],
        row["scope_type"],
        row["scope_id"],
    )
    return _memory_row_to_out(row)


@router.get(
    "/memory/visibility-test",
    response_model=VisibilityTestResponse,
    summary="Preview what memory a given user would see (list view + chat prompt injection).",
)
async def memory_visibility_test(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    user_id: str = Query(..., description="Target user UUID"),
    q: Optional[str] = Query(default=None, description="Optional query for chat-injection preview"),
    list_limit: int = Query(default=20, ge=1, le=200),
) -> VisibilityTestResponse:
    try:
        target_uuid = uuid.UUID(user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="user_id must be a valid UUID",
        ) from exc

    pool = _require_pool()
    async with pool.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT id, email, role FROM users WHERE id = $1", target_uuid
        )
    if user_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="user not found",
        )

    # Visible-list query mirrors /memory exactly (global + user(target) + legacy NULL)
    async with pool.acquire() as conn:
        list_rows = await conn.fetch(
            """
            SELECT id, source_session_id, type, title, content, tags,
                   importance, created_at, updated_at, scope_type, scope_id
            FROM memory_entries
            WHERE scope_type = 'global'
               OR (
                      scope_type = 'user'
                      AND (scope_id = $1 OR scope_id IS NULL)
                  )
            ORDER BY created_at DESC
            LIMIT $2
            """,
            target_uuid,
            list_limit,
        )

    search_rows: list[dict] = []
    if q:
        # Mirrors /chat retrieval: search returns up to 20, top 5 go to prompt
        search_rows = await search_memory(q, limit=20, user_id=target_uuid)
    top_for_prompt = search_rows[:MEMORIES_IN_PROMPT_PREVIEW]

    logger.info(
        "admin visibility-test: admin=%s target_user=%s q=%r list=%s "
        "search_matches=%s prompt_preview=%s",
        admin.id,
        target_uuid,
        q,
        len(list_rows),
        len(search_rows),
        len(top_for_prompt),
    )

    return VisibilityTestResponse(
        user_id=str(target_uuid),
        user_email=user_row["email"],
        user_role=user_row["role"],
        scope_filter=f"global + user({target_uuid}) + legacy user(NULL)",
        list_visible_count=len(list_rows),
        list_visible=[_memory_row_to_preview(r) for r in list_rows],
        query=q,
        search_match_count=len(search_rows),
        search_top_in_prompt=[_memory_row_to_preview(r) for r in top_for_prompt],
    )
