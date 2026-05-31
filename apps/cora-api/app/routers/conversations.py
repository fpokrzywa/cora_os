import logging
import uuid
from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.auth import CurrentUser, get_current_user
from app.clients import clients

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])


MAX_MANUAL_TITLE_LEN = 200


class ConversationSummary(BaseModel):
    session_id: str
    created_at: datetime
    updated_at: datetime
    message_count: int
    last_message_at: Optional[datetime]
    title: Optional[str] = None
    pinned: bool = False
    deleted_at: Optional[datetime] = None
    title_source: Optional[str] = None


class ConversationUpdate(BaseModel):
    title: Optional[str] = None
    pinned: Optional[bool] = None


class ConversationMutationOut(BaseModel):
    session_id: str
    title: Optional[str] = None
    pinned: bool = False
    title_source: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    deleted_at: Optional[datetime] = None


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    created_at: datetime


class AgentRunOut(BaseModel):
    id: int
    agent: str
    model_name: Optional[str]
    user_message: str
    assistant_response: Optional[str]
    placeholder: bool
    started_at: datetime
    completed_at: Optional[datetime]
    tool_name: Optional[str] = None
    tool_result: Optional[dict] = None


class ConversationDetail(BaseModel):
    session_id: str
    created_at: datetime
    updated_at: datetime
    messages: list[MessageOut]
    agent_runs: list[AgentRunOut]


def _require_pool():
    if clients.db_pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Postgres pool unavailable",
        )
    return clients.db_pool


@router.get(
    "",
    response_model=list[ConversationSummary],
    summary="List conversations (most recently updated first)",
)
async def list_conversations(
    current: Annotated[CurrentUser, Depends(get_current_user)],
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[ConversationSummary]:
    pool = _require_pool()
    logger.info(
        "list conversations: user_id=%s scope_type=user scope_id=%s limit=%s offset=%s",
        current.id,
        current.id,
        limit,
        offset,
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                c.session_id,
                c.created_at,
                c.updated_at,
                c.title,
                c.pinned,
                c.deleted_at,
                c.title_source,
                COUNT(m.id) AS message_count,
                MAX(m.created_at) AS last_message_at
            FROM conversations c
            LEFT JOIN messages m
                   ON m.session_id = c.session_id
                  AND m.scope_type = c.scope_type
                  AND m.scope_id IS NOT DISTINCT FROM c.scope_id
            WHERE c.scope_type = 'user' AND c.scope_id = $3
              AND c.deleted_at IS NULL
            GROUP BY c.session_id, c.created_at, c.updated_at,
                     c.title, c.pinned, c.deleted_at, c.title_source
            ORDER BY c.pinned DESC, c.updated_at DESC, c.created_at DESC
            LIMIT $1 OFFSET $2
            """,
            limit,
            offset,
            current.id,
        )
    return [
        ConversationSummary(
            session_id=str(row["session_id"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            message_count=row["message_count"],
            last_message_at=row["last_message_at"],
            title=row["title"],
            pinned=row["pinned"],
            deleted_at=row["deleted_at"],
            title_source=row["title_source"],
        )
        for row in rows
    ]


@router.get(
    "/{session_id}",
    response_model=ConversationDetail,
    summary="Get a single conversation with messages and agent runs",
)
async def get_conversation(
    session_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> ConversationDetail:
    try:
        session_uuid = uuid.UUID(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="session_id must be a valid UUID",
        ) from exc

    pool = _require_pool()
    logger.info(
        "get conversation: user_id=%s scope_type=user scope_id=%s session=%s",
        current.id,
        current.id,
        session_uuid,
    )
    async with pool.acquire() as conn:
        convo = await conn.fetchrow(
            """
            SELECT session_id, created_at, updated_at, scope_type, scope_id
            FROM conversations
            WHERE session_id = $1
              AND scope_type = 'user'
              AND scope_id = $2
            """,
            session_uuid,
            current.id,
        )
        if convo is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="conversation not found",
            )
        message_rows = await conn.fetch(
            """
            SELECT id, role, content, created_at
            FROM messages
            WHERE session_id = $1
              AND scope_type = 'user'
              AND scope_id = $2
            ORDER BY created_at ASC, id ASC
            """,
            session_uuid,
            current.id,
        )
        run_rows = await conn.fetch(
            """
            SELECT id, agent, model_name, user_message, assistant_response,
                   placeholder, started_at, completed_at, tool_name, tool_result
            FROM agent_runs
            WHERE session_id = $1
              AND scope_type = 'user'
              AND scope_id = $2
            ORDER BY started_at ASC, id ASC
            """,
            session_uuid,
            current.id,
        )

    return ConversationDetail(
        session_id=str(convo["session_id"]),
        created_at=convo["created_at"],
        updated_at=convo["updated_at"],
        messages=[
            MessageOut(
                id=row["id"],
                role=row["role"],
                content=row["content"],
                created_at=row["created_at"],
            )
            for row in message_rows
        ],
        agent_runs=[
            AgentRunOut(
                id=row["id"],
                agent=row["agent"],
                model_name=row["model_name"],
                user_message=row["user_message"],
                assistant_response=row["assistant_response"],
                placeholder=row["placeholder"],
                started_at=row["started_at"],
                completed_at=row["completed_at"],
                tool_name=row["tool_name"],
                tool_result=row["tool_result"],
            )
            for row in run_rows
        ],
    )


def _parse_conversation_id(conversation_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(conversation_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="conversation_id must be a valid UUID",
        ) from exc


@router.patch(
    "/{conversation_id}",
    response_model=ConversationMutationOut,
    summary="Rename and/or pin a conversation (owner only)",
)
async def update_conversation(
    conversation_id: str,
    payload: ConversationUpdate,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> ConversationMutationOut:
    convo_uuid = _parse_conversation_id(conversation_id)
    pool = _require_pool()

    # Build the SET clause dynamically from the provided fields. Owner scoping
    # mirrors the rest of this router (scope_type='user' AND scope_id=current).
    sets: list[str] = []
    args: list = []
    idx = 1

    if payload.title is not None:
        title = payload.title.strip()
        if not title:
            title = "New chat"
        if len(title) > MAX_MANUAL_TITLE_LEN:
            title = title[:MAX_MANUAL_TITLE_LEN].rstrip()
        sets.append(f"title = ${idx}")
        args.append(title)
        idx += 1
        sets.append(f"title_source = ${idx}")
        args.append("manual")
        idx += 1

    if payload.pinned is not None:
        sets.append(f"pinned = ${idx}")
        args.append(payload.pinned)
        idx += 1

    if not sets:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="nothing to update — provide title and/or pinned",
        )

    # Trailing params: session_id, scope_id.
    sid_param = idx
    scope_param = idx + 1
    args.extend([convo_uuid, current.id])

    logger.info(
        "update conversation: user_id=%s session=%s fields=%s",
        current.id,
        convo_uuid,
        [s.split(" =")[0] for s in sets],
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE conversations
            SET {", ".join(sets)}
            WHERE session_id = ${sid_param}
              AND scope_type = 'user'
              AND scope_id = ${scope_param}
              AND deleted_at IS NULL
            RETURNING session_id, title, pinned, title_source,
                      created_at, updated_at, deleted_at
            """,
            *args,
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="conversation not found",
        )
    return ConversationMutationOut(
        session_id=str(row["session_id"]),
        title=row["title"],
        pinned=row["pinned"],
        title_source=row["title_source"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
    )


@router.delete(
    "/{conversation_id}",
    summary="Soft-delete a conversation (owner only; messages are retained)",
)
async def delete_conversation(
    conversation_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    convo_uuid = _parse_conversation_id(conversation_id)
    pool = _require_pool()
    logger.info(
        "delete conversation (soft): user_id=%s session=%s",
        current.id,
        convo_uuid,
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE conversations
            SET deleted_at = NOW()
            WHERE session_id = $1
              AND scope_type = 'user'
              AND scope_id = $2
              AND deleted_at IS NULL
            RETURNING session_id
            """,
            convo_uuid,
            current.id,
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="conversation not found",
        )
    return {"status": "deleted", "session_id": str(row["session_id"])}
