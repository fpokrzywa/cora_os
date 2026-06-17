"""RSS/Atom feed fetch + parse for News Feed Ingestion.

Fetches a single feed URL, parses metadata + entries with feedparser, and
returns normalized article dicts for the knowledge-ingestion endpoint to store
as knowledge_sources (source_type='news_feed' + 'news_article') + memory rows.

v0.2: the endpoint may optionally fetch each article LINK's full readable body
(HTML/plain-text/PDF) via `fetch_article_body` (which wraps url_ingest). Still
non-crawling — only the feed URL and each item's own link are fetched, never
links discovered inside article bodies.
"""

import hashlib
import html
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import feedparser
import httpx

from app import schema as schema_state
from app.clients import clients
from app.memory import embed_memory_entry
from app.runtime_traces import write_trace
from app.url_ingest import UrlIngestError, fetch_and_extract, normalize_url

logger = logging.getLogger(__name__)

FETCH_TIMEOUT_SECONDS = 20.0
MAX_ARTICLE_CHARS = 8000
_USER_AGENT = "Cora-Knowledge/0.1 (+news ingestion)"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Article-body fetch tuning (shared by ingestion).
MIN_BODY_CHARS = 200
MAX_FULL_BODY_CHARS = 16000
MAX_SUMMARY_CHARS = 8000

# Scheduled-refresh backoff: after N consecutive failures a feed's next refresh
# is pushed out by interval * min(2**(N-1), CAP) so a broken feed stops hammering
# the scheduler at full cadence. Reset to 0 on the next success.
REFRESH_BACKOFF_CAP_MULTIPLIER = 8


class NewsIngestError(Exception):
    """Raised on feed fetch/parse failure. `code` maps to an HTTP status."""

    def __init__(self, message: str, *, code: str = "fetch_failed"):
        super().__init__(message)
        self.code = code


def _strip_html(raw: Optional[str]) -> str:
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _entry_published(entry) -> Optional[str]:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc).isoformat()
            except (TypeError, ValueError):
                continue
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


def _entry_author(entry) -> Optional[str]:
    author = entry.get("author")
    return str(author).strip() if author else None


def _entry_tags(entry) -> list[str]:
    tags = entry.get("tags") or []
    out: list[str] = []
    for t in tags:
        term = t.get("term") if isinstance(t, dict) else None
        if term:
            out.append(str(term).strip())
    return out


def article_content_hash(url: Optional[str], title: str, content: str) -> str:
    """Deterministic per-article hash over canonical URL + title + content."""
    basis = f"{url or ''}\n{title}\n{content}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


async def fetch_article_body(link: str) -> dict:
    """Best-effort full-article fetch via the shared url_ingest extractor
    (HTML / plain-text / PDF). Never raises — returns a status dict so a single
    slow/broken article cannot fail the whole feed ingest.

    Returns: {status: 'success'|'failed', content, content_type,
              extraction_method, page_count, error}.
    """
    try:
        res = await fetch_and_extract(link)
        return {
            "status": "success",
            "content": res.get("content") or "",
            "content_type": res.get("content_type"),
            "extraction_method": res.get("extraction_method"),
            "page_count": res.get("page_count"),
            "error": None,
        }
    except UrlIngestError as exc:
        return {
            "status": "failed",
            "content": "",
            "content_type": None,
            "extraction_method": None,
            "page_count": None,
            "error": f"{exc.code}: {exc}",
        }
    except Exception as exc:  # transport/parse/etc — keep the feed alive
        logger.warning("article body fetch failed: link=%s err=%s", link, exc)
        return {
            "status": "failed",
            "content": "",
            "content_type": None,
            "extraction_method": None,
            "page_count": None,
            "error": str(exc),
        }


async def fetch_feed(feed_url: str, max_items: int = 10) -> dict:
    """Fetch + parse an RSS/Atom feed. Returns feed metadata + normalized
    entries. Raises NewsIngestError on any failure."""
    try:
        norm = normalize_url(feed_url)
    except UrlIngestError as exc:
        raise NewsIngestError(str(exc), code="invalid_url") from exc

    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            resp = await client.get(norm, headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
            raw = resp.content
    except httpx.HTTPError as exc:
        raise NewsIngestError(
            f"could not fetch feed: {exc}", code="fetch_failed"
        ) from exc

    parsed = feedparser.parse(raw)
    entries = parsed.entries or []
    if not entries:
        # bozo + no entries → almost certainly not a valid feed.
        raise NewsIngestError(
            "could not parse any entries — is this a valid RSS/Atom feed?",
            code="invalid_feed",
        )

    out_entries: list[dict] = []
    for entry in entries[:max_items]:
        title = _strip_html(entry.get("title")) or "(untitled)"
        link = entry.get("link")
        if link:
            try:
                link = normalize_url(link)
            except UrlIngestError:
                pass  # keep raw link if it can't be normalized
        out_entries.append(
            {
                "title": title,
                "link": link,
                "summary": _entry_summary(entry),
                "published_at": _entry_published(entry),
                "author": _entry_author(entry),
                "tags": _entry_tags(entry),
                "entry_id": entry.get("id") or entry.get("guid"),
            }
        )

    feed = parsed.feed or {}
    return {
        "normalized_feed_url": norm,
        "feed_title": _strip_html(feed.get("title")) or None,
        "feed_link": feed.get("link"),
        "status_code": resp.status_code,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "items_seen": len(out_entries),
        "entries": out_entries,
    }


# ---------------------------------------------------------------------------
# Unified knowledge ingestion service (shared by the manual endpoint, the feed
# register/refresh endpoints, and the worker job). Stores feeds + articles as
# knowledge_sources + linked memory_entries — the canonical news path.
# ---------------------------------------------------------------------------


def _as_dict(value) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def _embed_col() -> str:
    return "embedding" if schema_state.is_pgvector_available() else "embedding_json"


async def update_feed_metadata(source_id: uuid.UUID, updates: dict) -> None:
    """Merge `updates` into a news_feed source's metadata JSONB (best-effort)."""
    if clients.db_pool is None:
        return
    async with clients.db_pool.acquire() as conn:
        cur = await conn.fetchval(
            "SELECT metadata FROM knowledge_sources WHERE id = $1", source_id
        )
        meta = {**_as_dict(cur), **updates}
        await conn.execute(
            "UPDATE knowledge_sources SET metadata = $2, updated_at = NOW() "
            "WHERE id = $1",
            source_id,
            meta,
        )


async def _get_or_create_feed_source(
    conn, *, workspace_id, uploaded_by, source_name, feed_url, basic_meta
) -> uuid.UUID:
    """Dedup feeds by (workspace_id, normalized feed_url). Merges basic feed
    info into existing metadata (preserving refresh_* settings/bookkeeping)."""
    row = await conn.fetchrow(
        "SELECT id, metadata FROM knowledge_sources WHERE source_type='news_feed' "
        "AND source_url = $2 "
        "AND (workspace_id = $1 OR ($1 IS NULL AND workspace_id IS NULL)) "
        "ORDER BY created_at ASC LIMIT 1",
        workspace_id,
        feed_url,
    )
    if row:
        merged = {**_as_dict(row["metadata"]), **basic_meta}
        await conn.execute(
            "UPDATE knowledge_sources SET title=$2, metadata=$3, updated_at=NOW() "
            "WHERE id=$1",
            row["id"],
            source_name,
            merged,
        )
        return row["id"]
    return await conn.fetchval(
        """
        INSERT INTO knowledge_sources
            (workspace_id, uploaded_by, source_type, title, description,
             source_url, content, content_hash, metadata)
        VALUES ($1, $2, 'news_feed', $3, $4, $5, NULL, NULL, $6)
        RETURNING id
        """,
        workspace_id,
        uploaded_by,
        source_name,
        "RSS/Atom feed",
        feed_url,
        basic_meta,
    )


async def ingest_feed_into_knowledge(
    *,
    workspace_id,
    uploaded_by,
    source_name: Optional[str],
    feed_url: str,
    max_items: int,
    scope_type: str,
    importance: int,
    auto_embed: bool,
    fetch_article_body: bool,
) -> dict:
    """Fetch a feed and ingest its items as knowledge. Reuses the v0.2 full-body
    + update-in-place + dedupe logic. Raises NewsIngestError on feed-level
    failure. Returns counts + feed_source_id + created article list."""
    feed = await fetch_feed(feed_url, max_items)
    norm_url = feed["normalized_feed_url"]
    name = (source_name or feed["feed_title"] or norm_url).strip()[:200]
    fetched_at = feed["fetched_at"]
    scope_id = uploaded_by if scope_type == "user" else None
    basic_meta = {
        "ingest_method": "news_feed",
        "source_name": name,
        "feed_title": feed["feed_title"],
        "feed_link": feed["feed_link"],
        "fetched_at": fetched_at,
        "item_count": feed["items_seen"],
        "status_code": feed["status_code"],
    }

    if clients.db_pool is None:
        raise NewsIngestError("Postgres pool unavailable", code="unavailable")

    embed_col = _embed_col()
    articles_created = 0
    articles_updated = 0
    skipped = 0
    bodies_fetched = 0
    body_fetch_failures = 0
    errors: list[str] = []
    created: list[dict] = []
    mem_to_embed: list[uuid.UUID] = []

    async with clients.db_pool.acquire() as conn:
        feed_source_id = await _get_or_create_feed_source(
            conn,
            workspace_id=workspace_id,
            uploaded_by=uploaded_by,
            source_name=name,
            feed_url=norm_url,
            basic_meta=basic_meta,
        )

        for e in feed["entries"]:
            try:
                title = (e["title"] or "(untitled)")[:500]
                link = e["link"]
                summary = e["summary"] or ""
                published = e["published_at"]

                attempted = bool(fetch_article_body and link)
                body_text = ""
                body_status = "skipped"
                body_content_type = None
                body_extraction_method = None
                body_page_count = None
                body_error = None
                if attempted:
                    b = await fetch_article_body(link)
                    if (
                        b["status"] == "success"
                        and len(b["content"].strip()) >= MIN_BODY_CHARS
                    ):
                        body_text = b["content"]
                        body_status = "success"
                        body_content_type = b["content_type"]
                        body_extraction_method = b["extraction_method"]
                        body_page_count = b["page_count"]
                        bodies_fetched += 1
                    else:
                        body_fetch_failures += 1
                        body_error = b.get("error")

                if body_status == "success":
                    content_body = body_text
                    fetch_status = "success"
                    body_fetched = True
                    cap = MAX_FULL_BODY_CHARS
                else:
                    content_body = summary
                    body_fetched = False
                    cap = MAX_SUMMARY_CHARS
                    if attempted:
                        fetch_status = "fallback" if summary.strip() else "failed"
                    else:
                        fetch_status = "skipped"

                header_parts = [title]
                if published:
                    header_parts.append(f"Published: {published}")
                header_parts.append(f"Source: {name}")
                if e["author"]:
                    header_parts.append(f"Author: {e['author']}")
                if link:
                    header_parts.append(f"URL: {link}")
                header = "\n".join(header_parts)
                content = (
                    f"{header}\n\n{content_body}" if content_body.strip() else header
                )
                content = content[:cap]

                if attempted and fetch_status == "failed" and not summary.strip():
                    errors.append(
                        f"{title[:60]}: no body or summary "
                        f"({body_error or 'empty'})"
                    )
                    continue

                chash = article_content_hash(link, title, content)
                article_metadata = {
                    "source_name": name,
                    "feed_url": norm_url,
                    "published_at": published,
                    "author": e["author"],
                    "tags": e["tags"],
                    "fetched_at": fetched_at,
                    "entry_id": e["entry_id"],
                    "article_body_fetched": body_fetched,
                    "article_fetch_status": fetch_status,
                    "article_content_type": body_content_type,
                    "article_extraction_method": body_extraction_method,
                    "article_page_count": body_page_count,
                    "article_fetch_error": body_error,
                }
                tags = ["knowledge_ingested", "news"]
                if name:
                    tags.append(name)

                existing = await conn.fetchrow(
                    "SELECT id, length(content) AS clen FROM knowledge_sources "
                    "WHERE source_type='news_article' AND status='active' "
                    "AND (workspace_id = $1 OR ($1 IS NULL AND workspace_id IS NULL)) "
                    "AND (($2::text IS NOT NULL AND source_url = $2) "
                    "     OR content_hash = $3) "
                    "LIMIT 1",
                    workspace_id,
                    link,
                    chash,
                )
                if existing is not None:
                    if body_fetched and len(content) > int(
                        (existing["clen"] or 0) * 1.1
                    ):
                        async with conn.transaction():
                            await conn.execute(
                                "UPDATE knowledge_sources SET content=$2, "
                                "content_hash=$3, metadata=$4, updated_at=NOW() "
                                "WHERE id=$1",
                                existing["id"],
                                content,
                                chash,
                                article_metadata,
                            )
                            mem_rows = await conn.fetch(
                                f"""
                                UPDATE memory_entries
                                SET content=$2, {embed_col}=NULL,
                                    embedded_at=NULL, updated_at=NOW()
                                WHERE source_id=$1
                                RETURNING id
                                """,
                                existing["id"],
                                content,
                            )
                            # Content changed → drop stale chunks. They are
                            # rebuilt below when auto_embed embeds the entry;
                            # if auto_embed is off they stay cleared (consistent
                            # with the cleared memory-level vector).
                            for m in mem_rows:
                                await conn.execute(
                                    "DELETE FROM memory_entry_chunks "
                                    "WHERE memory_entry_id = $1",
                                    m["id"],
                                )
                        articles_updated += 1
                        for m in mem_rows:
                            mem_to_embed.append(m["id"])
                    else:
                        skipped += 1
                    continue

                async with conn.transaction():
                    art_id = await conn.fetchval(
                        """
                        INSERT INTO knowledge_sources
                            (workspace_id, uploaded_by, source_type, title,
                             description, source_url, content, content_hash,
                             metadata)
                        VALUES ($1, $2, 'news_article', $3, $4, $5, $6, $7, $8)
                        RETURNING id
                        """,
                        workspace_id,
                        uploaded_by,
                        title,
                        f"News article from {name}",
                        link,
                        content,
                        chash,
                        article_metadata,
                    )
                    mem_id = await conn.fetchval(
                        """
                        INSERT INTO memory_entries
                            (type, title, content, tags, importance,
                             scope_type, scope_id, workspace_id, source_id)
                        VALUES ('news_article', $1, $2, $3, $4, $5, $6, $7, $8)
                        RETURNING id
                        """,
                        title,
                        content,
                        tags,
                        importance,
                        scope_type,
                        scope_id,
                        workspace_id,
                        art_id,
                    )
                articles_created += 1
                mem_to_embed.append(mem_id)
                created.append(
                    {
                        "source_id": str(art_id),
                        "memory_entry_id": str(mem_id),
                        "title": title,
                        "url": link,
                        "published_at": published,
                    }
                )
            except Exception as exc:  # per-item failure — record + continue
                logger.exception("news article ingest failed: feed=%s", norm_url)
                errors.append(f"{(e.get('title') or '?')[:60]}: {exc}")

    embedded = 0
    if auto_embed and mem_to_embed:
        for mid in mem_to_embed:
            try:
                res = await embed_memory_entry(mid)
                if res.get("status") == "ok":
                    embedded += 1
            except Exception:
                logger.exception("news article embed failed: memory=%s", mid)

    if errors and (articles_created + articles_updated + skipped) == 0:
        status_val = "error"
    elif errors:
        status_val = "partial"
    else:
        status_val = "ok"

    return {
        "status": status_val,
        "feed_source_id": str(feed_source_id),
        "source_name": name,
        "feed_url": norm_url,
        "items_seen": feed["items_seen"],
        "articles_created": articles_created,
        "articles_updated": articles_updated,
        "articles_skipped_duplicate": skipped,
        "article_bodies_fetched": bodies_fetched,
        "article_body_fetch_failures": body_fetch_failures,
        "errors_count": len(errors),
        "errors": errors,
        "embedded": embedded,
        "created_articles": created,
    }


async def refresh_feed_source(source_id: uuid.UUID, *, user_id) -> dict:
    """Refresh a registered news_feed source using its stored settings, updating
    bookkeeping metadata + writing started/completed/failed traces. Raises
    NewsIngestError on feed-level failure (callers map to HTTP / job status)."""
    if clients.db_pool is None:
        raise NewsIngestError("Postgres pool unavailable", code="unavailable")
    async with clients.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, workspace_id, title, source_url, metadata "
            "FROM knowledge_sources WHERE id = $1 AND source_type='news_feed'",
            source_id,
        )
    if row is None:
        raise NewsIngestError("news feed not found", code="not_found")

    meta = _as_dict(row["metadata"])
    feed_url = meta.get("feed_url") or row["source_url"]
    source_name = meta.get("source_name") or row["title"]
    workspace_id = row["workspace_id"]
    interval = meta.get("refresh_interval_minutes")
    settings = dict(
        max_items=int(meta.get("max_items", 20)),
        scope_type=meta.get("scope_type", "user"),
        importance=int(meta.get("importance", 3)),
        auto_embed=bool(meta.get("auto_embed", False)),
        fetch_article_body=bool(meta.get("fetch_article_body", False)),
    )
    now = datetime.now(timezone.utc)
    next_at = (
        (now + timedelta(minutes=int(interval))).isoformat()
        if interval and int(interval) > 0
        else None
    )

    await update_feed_metadata(source_id, {"last_checked_at": now.isoformat()})
    await write_trace(
        session_id=None,
        user_id=user_id,
        trace_type="news_feed_refresh_started",
        status="ok",
        selected_agent="ATLAS",
        tool_name="news_feed_refresh",
        tool_result={
            "source_id": str(source_id),
            "feed_url": feed_url,
            "source_name": source_name,
        },
        workspace_id=workspace_id,
    )

    try:
        result = await ingest_feed_into_knowledge(
            workspace_id=workspace_id,
            uploaded_by=user_id,
            source_name=source_name,
            feed_url=feed_url,
            **settings,
        )
    except NewsIngestError as exc:
        # Exponential backoff on repeated failures so a broken feed doesn't keep
        # retrying every interval. Capped; consecutive_failures resets on success.
        failures = int(meta.get("consecutive_failures", 0) or 0) + 1
        if interval and int(interval) > 0:
            mult = min(2 ** (failures - 1), REFRESH_BACKOFF_CAP_MULTIPLIER)
            backoff_at = (now + timedelta(minutes=int(interval) * mult)).isoformat()
        else:
            backoff_at = None
        await update_feed_metadata(
            source_id,
            {
                "last_error": str(exc),
                "consecutive_failures": failures,
                "next_refresh_at": backoff_at,
            },
        )
        await write_trace(
            session_id=None,
            user_id=user_id,
            trace_type="news_feed_refresh_failed",
            status="error",
            selected_agent="ATLAS",
            tool_name="news_feed_refresh",
            tool_result={
                "source_id": str(source_id),
                "feed_url": feed_url,
                "source_name": source_name,
                "error": str(exc),
                "code": exc.code,
                "consecutive_failures": failures,
                "next_refresh_at": backoff_at,
            },
            workspace_id=workspace_id,
            error_message=str(exc),
        )
        raise

    last_result = {
        "items_seen": result["items_seen"],
        "articles_created": result["articles_created"],
        "articles_updated": result["articles_updated"],
        "articles_skipped_duplicate": result["articles_skipped_duplicate"],
        "article_bodies_fetched": result["article_bodies_fetched"],
        "article_body_fetch_failures": result["article_body_fetch_failures"],
        "errors_count": result["errors_count"],
    }
    await update_feed_metadata(
        source_id,
        {
            "last_success_at": now.isoformat(),
            "last_error": None,
            "consecutive_failures": 0,
            "last_result": last_result,
            "next_refresh_at": next_at,
        },
    )
    await write_trace(
        session_id=None,
        user_id=user_id,
        trace_type="news_feed_refresh_completed",
        status="ok",
        selected_agent="ATLAS",
        tool_name="news_feed_refresh",
        tool_result={
            "source_id": str(source_id),
            "feed_url": feed_url,
            "source_name": source_name,
            "auto_embed": settings["auto_embed"],
            "fetch_article_body": settings["fetch_article_body"],
            **last_result,
        },
        workspace_id=workspace_id,
    )
    result["next_refresh_at"] = next_at
    return result
