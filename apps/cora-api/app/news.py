"""News-source ingestion — PULSE's external-evidence pipeline.

DEPRECATED (News Path Reconciliation v0.1, 2026-05-28): superseded by the
unified knowledge news path — `app/news_ingest.py` +
`POST /workspaces/{id}/knowledge/news`, which stores feeds/articles as
`knowledge_sources` (source_type='news_feed'/'news_article') + linked
`memory_entries`. No active UI uses this `news_sources` registry path anymore.
Routes remain temporarily (non-destructive) but should not be extended; build
news features on the knowledge path. Do NOT drop the news_sources table here.

A `news_sources` row registers an RSS/Atom feed. Fetching a source pulls recent
entries, stores each new article as a `knowledge_sources` row
(source_type='news_article') with a linked `memory_entries` row, and embeds it —
so PULSE retrieves and cites news exactly like any other ingested knowledge.

Non-autonomous by design: fetching is triggered manually by an admin (or via a
queued `news_fetch` job). Nothing polls feeds on its own.
"""

import hashlib
import html
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import asyncpg
import feedparser
import httpx

from app.clients import clients
from app.memory import embed_memory_entry
from app.runtime_traces import write_trace

logger = logging.getLogger(__name__)

FETCH_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_ARTICLES = 20
MAX_ARTICLE_CHARS = 8000
_USER_AGENT = "Cora-PULSE/0.1 (+news ingestion)"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_SOURCE_COLS = """
    id, workspace_id, name, feed_url, category, enabled,
    last_fetched_at, last_status, last_error, last_article_count,
    total_article_count, created_by, created_at, updated_at
"""


class NewsError(Exception):
    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


def _require_pool():
    if clients.db_pool is None:
        raise NewsError("Postgres pool unavailable", code="unavailable")
    return clients.db_pool


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _strip_html(raw: Optional[str]) -> str:
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _entry_published(entry) -> Optional[str]:
    """Return an ISO-8601 string for the entry's publish/update time, or None."""
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc).isoformat()
            except (TypeError, ValueError):
                continue
    # Fall back to the raw string fields if present.
    for key in ("published", "updated"):
        val = entry.get(key)
        if val:
            return str(val)
    return None


def _entry_summary(entry) -> str:
    content_list = entry.get("content")
    if content_list:
        try:
            return _strip_html(content_list[0].get("value"))
        except (AttributeError, IndexError, KeyError):
            pass
    return _strip_html(entry.get("summary") or entry.get("description"))


# ---------------------------------------------------------------------------
# Source registry CRUD
# ---------------------------------------------------------------------------


async def register_source(
    *,
    workspace_id: Optional[uuid.UUID],
    name: str,
    feed_url: str,
    category: Optional[str],
    created_by: Optional[uuid.UUID],
) -> dict:
    name = (name or "").strip()
    feed_url = (feed_url or "").strip()
    if not name or not feed_url:
        raise NewsError("name and feed_url are required", code="invalid")
    if not feed_url.lower().startswith(("http://", "https://")):
        raise NewsError("feed_url must be an http(s) URL", code="invalid")
    pool = _require_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO news_sources
                    (workspace_id, name, feed_url, category, created_by)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING {_SOURCE_COLS}
                """,
                workspace_id,
                name,
                feed_url,
                (category or "").strip() or None,
                created_by,
            )
    except asyncpg.UniqueViolationError as exc:
        raise NewsError(
            "this feed is already registered in the workspace", code="duplicate"
        ) from exc
    logger.info(
        "news source registered: id=%s name=%r feed=%s workspace=%s",
        row["id"], name, feed_url, workspace_id,
    )
    return dict(row)


async def list_sources(workspace_id: Optional[uuid.UUID] = None) -> list[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        if workspace_id is not None:
            rows = await conn.fetch(
                f"SELECT {_SOURCE_COLS} FROM news_sources "
                "WHERE workspace_id = $1 OR workspace_id IS NULL "
                "ORDER BY created_at DESC",
                workspace_id,
            )
        else:
            rows = await conn.fetch(
                f"SELECT {_SOURCE_COLS} FROM news_sources ORDER BY created_at DESC"
            )
    return [dict(r) for r in rows]


async def get_source(source_id: uuid.UUID) -> Optional[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_SOURCE_COLS} FROM news_sources WHERE id = $1", source_id
        )
    return dict(row) if row else None


async def set_enabled(source_id: uuid.UUID, enabled: bool) -> Optional[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE news_sources SET enabled = $2, updated_at = NOW()
            WHERE id = $1 RETURNING {_SOURCE_COLS}
            """,
            source_id,
            enabled,
        )
    return dict(row) if row else None


async def delete_source(source_id: uuid.UUID) -> bool:
    """Remove the feed from the registry. Already-ingested articles
    (knowledge_sources / memory_entries) are intentionally kept."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        deleted = await conn.fetchval(
            "DELETE FROM news_sources WHERE id = $1 RETURNING id", source_id
        )
    return deleted is not None


# ---------------------------------------------------------------------------
# Fetch + ingest
# ---------------------------------------------------------------------------


async def _ingest_entry(
    conn,
    *,
    workspace_id: Optional[uuid.UUID],
    source: dict,
    title: str,
    link: Optional[str],
    summary: str,
    published: Optional[str],
) -> Optional[uuid.UUID]:
    """Create a news_article knowledge_source + linked memory entry if not a
    duplicate. Returns the new memory_entry id, or None if it already existed."""
    dedup_key = (link or f"{title}\n{summary}").strip()
    chash = _content_hash(dedup_key)

    existing = await conn.fetchval(
        "SELECT id FROM knowledge_sources "
        "WHERE content_hash = $1 AND status = 'active' "
        "AND (workspace_id = $2 OR ($2 IS NULL AND workspace_id IS NULL)) "
        "LIMIT 1",
        chash,
        workspace_id,
    )
    if existing is not None:
        return None

    parts = [title]
    if published:
        parts.append(f"Published: {published}")
    parts.append(f"Source: {source['name']}")
    if link:
        parts.append(f"URL: {link}")
    if summary:
        parts.append("")
        parts.append(summary)
    content = "\n".join(parts)[:MAX_ARTICLE_CHARS]

    src_row = await conn.fetchrow(
        """
        INSERT INTO knowledge_sources
            (workspace_id, uploaded_by, source_type, title, description,
             source_url, content, content_hash)
        VALUES ($1, $2, 'news_article', $3, $4, $5, $6, $7)
        RETURNING id
        """,
        workspace_id,
        source.get("created_by"),
        title[:500],
        f"News article from {source['name']}",
        link,
        content,
        chash,
    )

    tags = ["news", source["name"]]
    if source.get("category"):
        tags.append(source["category"])

    mem_row = await conn.fetchrow(
        """
        INSERT INTO memory_entries
            (type, title, content, tags, importance,
             scope_type, scope_id, workspace_id, source_id)
        VALUES ('news_article', $1, $2, $3, 3, 'global', NULL, $4, $5)
        RETURNING id
        """,
        title[:500],
        content,
        tags,
        workspace_id,
        src_row["id"],
    )
    return mem_row["id"]


async def fetch_source(
    source_id: uuid.UUID,
    *,
    user_id: Optional[uuid.UUID] = None,
    max_articles: int = DEFAULT_MAX_ARTICLES,
    auto_embed: bool = True,
) -> dict:
    """Fetch a registered feed and ingest new articles. Updates the source's
    fetch state and writes a runtime trace. Returns a summary dict."""
    source = await get_source(source_id)
    if source is None:
        raise NewsError("news source not found", code="not_found")
    if not source["enabled"]:
        raise NewsError("news source is disabled", code="disabled")

    workspace_id = source["workspace_id"]
    feed_url = source["feed_url"]
    seen = 0
    ingested_ids: list[uuid.UUID] = []
    duplicates = 0
    fetch_error: Optional[str] = None

    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            resp = await client.get(feed_url, headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
            raw = resp.content
        parsed = feedparser.parse(raw)
        entries = parsed.entries or []
        pool = _require_pool()
        async with pool.acquire() as conn:
            for entry in entries[:max_articles]:
                seen += 1
                title = _strip_html(entry.get("title")) or "(untitled)"
                link = entry.get("link")
                summary = _entry_summary(entry)
                published = _entry_published(entry)
                async with conn.transaction():
                    mem_id = await _ingest_entry(
                        conn,
                        workspace_id=workspace_id,
                        source=source,
                        title=title,
                        link=link,
                        summary=summary,
                        published=published,
                    )
                if mem_id is None:
                    duplicates += 1
                else:
                    ingested_ids.append(mem_id)
    except httpx.HTTPError as exc:
        fetch_error = f"feed fetch failed: {exc}"
        logger.warning("news fetch failed: source=%s %s", source_id, exc)
    except Exception as exc:  # parsing / db errors shouldn't crash the worker
        fetch_error = f"news ingest failed: {exc}"
        logger.exception("news ingest error: source=%s", source_id)

    embedded = 0
    if auto_embed and ingested_ids:
        for mem_id in ingested_ids:
            try:
                res = await embed_memory_entry(mem_id)
                if res.get("status") == "ok":
                    embedded += 1
            except Exception:
                logger.exception("news article embed failed: memory=%s", mem_id)

    status_value = "error" if fetch_error else "ok"
    new_count = len(ingested_ids)
    await _update_fetch_state(
        source_id,
        status_value=status_value,
        error=fetch_error,
        new_count=new_count,
    )

    summary = {
        "source_id": str(source_id),
        "name": source["name"],
        "status": status_value,
        "entries_seen": seen,
        "ingested": new_count,
        "duplicates": duplicates,
        "embedded": embedded,
        "error": fetch_error,
    }
    await write_trace(
        session_id=None,
        user_id=user_id,
        trace_type="news_fetch",
        status=status_value,
        selected_agent="PULSE",
        tool_name="news_fetch",
        tool_result=summary,
        workspace_id=workspace_id,
        error_message=fetch_error,
    )
    logger.info(
        "news fetch complete: source=%s seen=%s ingested=%s dupes=%s embedded=%s status=%s",
        source_id, seen, new_count, duplicates, embedded, status_value,
    )
    if fetch_error and new_count == 0:
        raise NewsError(fetch_error, code="fetch_failed")
    return summary


async def _update_fetch_state(
    source_id: uuid.UUID,
    *,
    status_value: str,
    error: Optional[str],
    new_count: int,
) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE news_sources
            SET last_fetched_at = NOW(),
                last_status = $2,
                last_error = $3,
                last_article_count = $4,
                total_article_count = total_article_count + $4,
                updated_at = NOW()
            WHERE id = $1
            """,
            source_id,
            status_value,
            error,
            new_count,
        )
