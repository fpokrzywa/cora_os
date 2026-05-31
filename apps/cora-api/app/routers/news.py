"""News-source admin + ingestion endpoints (DEPRECATED).

DEPRECATED (News Path Reconciliation v0.1, 2026-05-28): the canonical news path
is now the unified knowledge ingestion — `POST /workspaces/{id}/knowledge/news`
(see app/news_ingest.py + routers/workspaces.py), which writes
knowledge_sources(news_feed/news_article) + memory_entries that PULSE retrieves
via normal memory retrieval. These /workspaces/{id}/news/* routes are retained
temporarily for back-compat but are no longer referenced by any UI. Do not add
new features here — extend the knowledge path instead.

Reads (list sources / articles) are available to any authenticated user.
Mutations (register / fetch / enable / delete) require an admin.
"""

import logging
import uuid
from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.auth import CurrentUser, get_current_user, require_admin
from app.clients import clients
from app.workspaces import get_workspace
from app import news

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["news"])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class NewsSourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    feed_url: str = Field(min_length=1, max_length=2000)
    category: Optional[str] = Field(default=None, max_length=100)


class NewsSourceUpdate(BaseModel):
    enabled: bool


class NewsSourceOut(BaseModel):
    id: str
    workspace_id: Optional[str]
    name: str
    feed_url: str
    category: Optional[str]
    enabled: bool
    last_fetched_at: Optional[datetime]
    last_status: Optional[str]
    last_error: Optional[str]
    last_article_count: int
    total_article_count: int
    created_at: datetime
    updated_at: datetime


class NewsFetchResult(BaseModel):
    source_id: str
    name: str
    status: str
    entries_seen: int
    ingested: int
    duplicates: int
    embedded: int
    error: Optional[str] = None


class NewsArticleOut(BaseModel):
    id: str
    title: str
    source_url: Optional[str]
    created_at: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} must be a valid UUID",
        ) from exc


_CODE_TO_STATUS = {
    "invalid": status.HTTP_400_BAD_REQUEST,
    "duplicate": status.HTTP_409_CONFLICT,
    "disabled": status.HTTP_409_CONFLICT,
    "not_found": status.HTTP_404_NOT_FOUND,
    "fetch_failed": status.HTTP_502_BAD_GATEWAY,
    "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _news_error_to_http(exc: news.NewsError) -> HTTPException:
    return HTTPException(
        status_code=_CODE_TO_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=str(exc),
    )


async def _require_workspace(workspace_id: str) -> uuid.UUID:
    wid = _parse_uuid(workspace_id, "workspace_id")
    ws = await get_workspace(wid)
    if ws is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found"
        )
    return wid


def _source_to_out(row: dict) -> NewsSourceOut:
    return NewsSourceOut(
        id=str(row["id"]),
        workspace_id=str(row["workspace_id"]) if row["workspace_id"] else None,
        name=row["name"],
        feed_url=row["feed_url"],
        category=row["category"],
        enabled=row["enabled"],
        last_fetched_at=row["last_fetched_at"],
        last_status=row["last_status"],
        last_error=row["last_error"],
        last_article_count=row["last_article_count"],
        total_article_count=row["total_article_count"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def _get_source_in_workspace(
    source_id: str, workspace_id: uuid.UUID
) -> dict:
    sid = _parse_uuid(source_id, "source_id")
    row = await news.get_source(sid)
    if row is None or (
        row["workspace_id"] is not None and row["workspace_id"] != workspace_id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="news source not found"
        )
    return row


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/{workspace_id}/news/sources",
    response_model=list[NewsSourceOut],
    summary="List registered news sources for a workspace.",
)
async def list_news_sources(
    workspace_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> list[NewsSourceOut]:
    wid = await _require_workspace(workspace_id)
    try:
        rows = await news.list_sources(wid)
    except news.NewsError as exc:
        raise _news_error_to_http(exc)
    return [_source_to_out(r) for r in rows]


@router.post(
    "/{workspace_id}/news/sources",
    response_model=NewsSourceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a news feed (admin only).",
)
async def create_news_source(
    workspace_id: str,
    req: NewsSourceCreate,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> NewsSourceOut:
    wid = await _require_workspace(workspace_id)
    try:
        row = await news.register_source(
            workspace_id=wid,
            name=req.name,
            feed_url=req.feed_url,
            category=req.category,
            created_by=admin.id,
        )
    except news.NewsError as exc:
        raise _news_error_to_http(exc)
    return _source_to_out(row)


@router.patch(
    "/{workspace_id}/news/sources/{source_id}",
    response_model=NewsSourceOut,
    summary="Enable or disable a news source (admin only).",
)
async def update_news_source(
    workspace_id: str,
    source_id: str,
    req: NewsSourceUpdate,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> NewsSourceOut:
    wid = await _require_workspace(workspace_id)
    await _get_source_in_workspace(source_id, wid)
    row = await news.set_enabled(_parse_uuid(source_id, "source_id"), req.enabled)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="news source not found"
        )
    return _source_to_out(row)


@router.delete(
    "/{workspace_id}/news/sources/{source_id}",
    status_code=status.HTTP_200_OK,
    summary="Remove a news feed from the registry (admin only). Ingested articles are kept.",
)
async def delete_news_source(
    workspace_id: str,
    source_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> dict:
    wid = await _require_workspace(workspace_id)
    await _get_source_in_workspace(source_id, wid)
    deleted = await news.delete_source(_parse_uuid(source_id, "source_id"))
    return {"deleted": deleted, "source_id": source_id}


@router.post(
    "/{workspace_id}/news/sources/{source_id}/fetch",
    response_model=NewsFetchResult,
    summary="Fetch a news feed now and ingest new articles (admin only).",
)
async def fetch_news_source(
    workspace_id: str,
    source_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> NewsFetchResult:
    wid = await _require_workspace(workspace_id)
    await _get_source_in_workspace(source_id, wid)
    try:
        result = await news.fetch_source(
            _parse_uuid(source_id, "source_id"), user_id=admin.id
        )
    except news.NewsError as exc:
        raise _news_error_to_http(exc)
    return NewsFetchResult(**result)


@router.get(
    "/{workspace_id}/news/articles",
    response_model=list[NewsArticleOut],
    summary="List ingested news articles for a workspace.",
)
async def list_news_articles(
    workspace_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
    limit: int = 50,
    offset: int = 0,
) -> list[NewsArticleOut]:
    wid = await _require_workspace(workspace_id)
    if clients.db_pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        )
    limit = max(1, min(limit, 200))
    async with clients.db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, title, source_url, created_at
            FROM knowledge_sources
            WHERE source_type = 'news_article'
              AND (workspace_id = $1 OR workspace_id IS NULL)
            ORDER BY created_at DESC
            LIMIT $2 OFFSET $3
            """,
            wid,
            limit,
            offset,
        )
    return [
        NewsArticleOut(
            id=str(r["id"]),
            title=r["title"],
            source_url=r["source_url"],
            created_at=r["created_at"],
        )
        for r in rows
    ]
