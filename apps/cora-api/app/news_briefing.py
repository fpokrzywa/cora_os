"""PULSE News Briefing v0.1 — read/summarize over already-ingested news.

Queries the canonical unified knowledge path (knowledge_sources
source_type='news_article' + linked memory_entries + memory_entry_chunks) and
optionally generates a PULSE-style briefing with the DGX model. No ingestion,
no web fetch, no tools — purely a read + summarize view.
"""

import logging
import uuid
from typing import Optional

import httpx

from app import llm
from app import schema as schema_state
from app.clients import clients

logger = logging.getLogger(__name__)

MODEL_TIMEOUT_SECONDS = 90.0
# Bounds for the summary prompt so we never send massive full bodies.
SUMMARY_MAX_ARTICLES = 20
SUMMARY_PREVIEW_CHARS = 600
PREVIEW_CHARS = 240


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    try:
        import json

        return json.loads(value)
    except (TypeError, ValueError):
        return {}


async def gather_briefing(
    *,
    workspace_id: Optional[uuid.UUID],
    since_hours: int,
    max_articles: int,
    source_name: Optional[str],
) -> dict:
    """Return {articles, aggregate} over recent news_article knowledge sources.
    Does not call the LLM."""
    if clients.db_pool is None:
        return {"articles": [], "aggregate": _empty_aggregate(since_hours)}

    embed_col = (
        "embedding" if schema_state.is_pgvector_available() else "embedding_json"
    )
    sql = f"""
        SELECT k.id, k.title, k.source_url, k.source_type, k.content,
               k.created_at, k.metadata,
               (SELECT COUNT(*) FROM memory_entry_chunks c
                  WHERE c.source_id = k.id) AS chunk_count,
               (SELECT COUNT(*) FROM memory_entry_chunks c
                  WHERE c.source_id = k.id AND c.{embed_col} IS NOT NULL)
                  AS embedded_chunk_count
        FROM knowledge_sources k
        WHERE k.source_type = 'news_article' AND k.status = 'active'
          AND (k.workspace_id = $1 OR ($1 IS NULL AND k.workspace_id IS NULL)
               OR k.workspace_id IS NULL)
          AND k.created_at >= NOW() - make_interval(hours => $2)
    """
    args: list = [workspace_id, since_hours]
    if source_name:
        args.append(source_name)
        sql += f" AND k.metadata->>'source_name' = ${len(args)}"
    args.append(max_articles)
    sql += f" ORDER BY k.created_at DESC LIMIT ${len(args)}"

    async with clients.db_pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)

    articles: list[dict] = []
    feeds: set[str] = set()
    source_names: set[str] = set()
    body_ok = 0
    body_fail = 0
    chunked = 0
    for r in rows:
        m = _as_dict(r["metadata"])
        content = r["content"] or ""
        fetch_status = m.get("article_fetch_status")
        if fetch_status == "success":
            body_ok += 1
        elif fetch_status in ("fallback", "failed"):
            body_fail += 1
        if (r["chunk_count"] or 0) > 0:
            chunked += 1
        if m.get("feed_url"):
            feeds.add(m["feed_url"])
        if m.get("source_name"):
            source_names.add(m["source_name"])
        articles.append(
            {
                "source_id": str(r["id"]),
                "title": r["title"],
                "source_url": r["source_url"],
                "source_type": r["source_type"],
                "source_name": m.get("source_name"),
                "feed_url": m.get("feed_url"),
                "published_at": m.get("published_at"),
                "created_at": r["created_at"],
                "content_length": len(content),
                "article_body_fetched": bool(m.get("article_body_fetched")),
                "article_fetch_status": fetch_status,
                "chunk_count": int(r["chunk_count"] or 0),
                "embedded_chunk_count": int(r["embedded_chunk_count"] or 0),
                "short_preview": content[:PREVIEW_CHARS].strip(),
            }
        )

    aggregate = {
        "total_articles": len(articles),
        "feeds_represented": len(feeds),
        "source_names": sorted(source_names),
        "since_hours": since_hours,
        "article_body_fetch_success_count": body_ok,
        "article_body_fetch_failure_count": body_fail,
        "chunked_article_count": chunked,
    }
    return {"articles": articles, "aggregate": aggregate}


def _empty_aggregate(since_hours: int) -> dict:
    return {
        "total_articles": 0,
        "feeds_represented": 0,
        "source_names": [],
        "since_hours": since_hours,
        "article_body_fetch_success_count": 0,
        "article_body_fetch_failure_count": 0,
        "chunked_article_count": 0,
    }


async def generate_briefing_summary(articles: list[dict]) -> Optional[str]:
    """Generate a PULSE-style briefing over the given articles using the DGX
    model. Bounded input (no full bodies). Returns text or None if unavailable.
    """
    if not llm.is_chat_configured() or not articles:
        return None

    # Lazy import to avoid any import-order coupling with the chat router.
    from app.clock import current_datetime_preamble
    from app.routers.chat import resolve_agent_prompt

    system_prompt, _src, _ver = await resolve_agent_prompt("PULSE")

    digest_lines: list[str] = []
    for i, a in enumerate(articles[:SUMMARY_MAX_ARTICLES], 1):
        meta = []
        if a.get("source_name"):
            meta.append(a["source_name"])
        if a.get("published_at"):
            meta.append(f"published {a['published_at']}")
        if a.get("source_url"):
            meta.append(a["source_url"])
        head = f"{i}. {a['title']}"
        if meta:
            head += f"  [{' · '.join(meta)}]"
        digest_lines.append(head)
        preview = (a.get("short_preview") or "")[:SUMMARY_PREVIEW_CHARS]
        if preview:
            digest_lines.append(f"   {preview}")
    digest = "\n".join(digest_lines)

    instructions = (
        "You are producing a NEWS BRIEFING strictly from the ingested articles "
        "listed below. You have NOT browsed the live web — reason only over "
        "these ingested sources, and say so. Attribute claims to their source "
        "name/URL. Structure the briefing with these clearly-labeled sections:\n"
        "1. Executive Summary (2-4 sentences)\n"
        "2. Key Themes (bullets)\n"
        "3. Notable Articles (a few, each with its source)\n"
        "4. Risks / Caveats (incl. coverage gaps, single-source or stale items)\n"
        "5. Suggested Follow-Up Questions\n\n"
        f"Ingested articles ({len(articles)} total, showing up to "
        f"{SUMMARY_MAX_ARTICLES}):\n{digest}"
    )
    prompt = (
        f"System: {current_datetime_preamble()}\n\n{system_prompt}\n\n"
        f"{instructions}\n\nCora:"
    )

    try:
        text = await llm.generate_text(
            prompt, max_tokens=1200, timeout=MODEL_TIMEOUT_SECONDS
        )
        return text or None
    except httpx.HTTPError:
        logger.exception("news briefing summary generation failed")
        return None
