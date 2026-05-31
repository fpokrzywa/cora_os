"""web_search tool runner — live web search via self-hosted SearXNG.

PULSE's external-evidence path for on-demand queries (vs. the news pipeline,
which ingests pre-registered feeds). The chat router dispatches this tool when a
message carries an explicit web-search cue, then injects the returned snippets
into PULSE's prompt so it synthesizes a grounded, cited answer. Queries stay on
cora-internal — SearXNG runs locally and no key or third party is involved.

Non-autonomous: a result is fetched only in direct response to a user message;
nothing here loops or schedules follow-up searches.
"""

import logging
import time
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

SEARCH_TIMEOUT_SECONDS = 20.0
_SNIPPET_MAX_CHARS = 600


async def run_web_search(tool: dict, payload: dict) -> dict[str, Any]:
    """Query SearXNG and return normalized results.

    Query resolution: payload['arguments']['query'] first, else the raw
    user_message. Raises httpx.HTTPError on transport failure (the chat router
    catches it); raises ValueError on missing query/endpoint.
    """
    arguments = payload.get("arguments") or {}
    query = (arguments.get("query") or payload.get("user_message") or "").strip()
    if not query:
        raise ValueError("web_search requires a non-empty query")

    endpoint = (tool.get("endpoint") or settings.searxng_endpoint or "").rstrip("/")
    if not endpoint:
        raise ValueError("web_search has no SearXNG endpoint configured")

    max_results = int(arguments.get("max_results") or settings.web_search_max_results)

    params = {
        "q": query,
        "format": "json",
        "categories": "general",
        "language": "en",
        "safesearch": "1",
    }

    logger.info(
        "web_search start: endpoint=%s query=%r max_results=%s",
        endpoint,
        query,
        max_results,
    )

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                f"{endpoint}/search",
                params=params,
                headers={"User-Agent": "Cora-PULSE/0.1 (+web search)"},
            )
            resp.raise_for_status()
    except httpx.HTTPError:
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.exception(
            "web_search failed: endpoint=%s query=%r duration_ms=%s",
            endpoint,
            query,
            duration_ms,
        )
        raise

    duration_ms = int((time.perf_counter() - started) * 1000)

    try:
        body = resp.json()
    except ValueError:
        logger.warning("web_search: SearXNG returned non-JSON (format=json off?)")
        return {
            "status": "error",
            "query": query,
            "error": "SearXNG did not return JSON; enable the json format in settings.yml",
            "results": [],
            "count": 0,
            "duration_ms": duration_ms,
            "engine": "searxng",
        }

    raw_results = body.get("results") or []
    results: list[dict[str, Any]] = []
    for item in raw_results[:max_results]:
        if not isinstance(item, dict):
            continue
        snippet = (item.get("content") or "").strip()
        results.append(
            {
                "title": (item.get("title") or "(untitled)").strip(),
                "url": item.get("url"),
                "snippet": snippet[:_SNIPPET_MAX_CHARS],
                "engine": item.get("engine"),
                "published": item.get("publishedDate"),
            }
        )

    logger.info(
        "web_search complete: query=%r raw=%s returned=%s duration_ms=%s",
        query,
        len(raw_results),
        len(results),
        duration_ms,
    )

    return {
        "status": "ok",
        "query": query,
        "results": results,
        "count": len(results),
        "duration_ms": duration_ms,
        "engine": "searxng",
    }
