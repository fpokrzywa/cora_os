import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

WEBHOOK_TIMEOUT_SECONDS = 30.0


async def run_n8n_webhook(tool: dict, payload: dict) -> dict[str, Any]:
    """Trigger an n8n webhook for a tool row.

    Raises httpx.HTTPError on transport failure (the router translates to 502).
    Raises ValueError on missing/invalid configuration.
    """
    endpoint = tool.get("endpoint")
    if not endpoint:
        raise ValueError(f"tool {tool.get('name')!r} has no endpoint configured")

    body = {
        "tool_name": tool["name"],
        "session_id": payload.get("session_id"),
        "user_message": payload.get("user_message"),
        "metadata": payload.get("metadata") or {},
        "triggered_at": datetime.now(timezone.utc).isoformat(),
    }

    logger.info(
        "n8n webhook start: tool=%s endpoint=%s session_id=%s",
        tool["name"],
        endpoint,
        body["session_id"],
    )

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS) as client:
            resp = await client.post(endpoint, json=body)
    except httpx.HTTPError as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.exception(
            "n8n webhook failed: tool=%s endpoint=%s duration_ms=%s error=%s",
            tool["name"],
            endpoint,
            duration_ms,
            exc,
        )
        raise

    duration_ms = int((time.perf_counter() - started) * 1000)

    try:
        response_body: Any = resp.json()
    except ValueError:
        response_body = resp.text

    success = resp.is_success
    log_fn = logger.info if success else logger.warning
    log_fn(
        "n8n webhook complete: tool=%s endpoint=%s http_status=%s "
        "duration_ms=%s success=%s",
        tool["name"],
        endpoint,
        resp.status_code,
        duration_ms,
        success,
    )

    return {
        "status": "ok" if success else "error",
        "http_status": resp.status_code,
        "response": response_body,
        "duration_ms": duration_ms,
    }
