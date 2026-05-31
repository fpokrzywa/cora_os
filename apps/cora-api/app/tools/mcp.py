"""MCP action runner — bridges the existing Tool Registry to the MCP layer.

A tool row with type='mcp_action' carries:
  - mcp_server_name → which row in mcp_servers to talk to
  - mcp_action_name → which MCP tool to invoke (tools/call name)
  - allowed_agents  → governance hint (enforcement is the caller's job)
  - risk_level      → governance hint (low/medium/high)

The runner does NOT enforce allowed_agents or risk_level today — those are
inspected by /chat or the manual run endpoints when they decide whether to
dispatch. The runner just executes against the MCP server when called.
"""

import logging
import time
from typing import Any

from app.mcp import McpClient, McpError, get_server_by_name
from app.mcp.registry import config_from_row

logger = logging.getLogger(__name__)


async def run_mcp_action(tool: dict, payload: dict) -> dict[str, Any]:
    """Dispatch an mcp_action tool. Returns the standard tool-runner shape:
      {status, http_status, response, duration_ms}
    plus mcp-specific fields (mcp_server, mcp_action).

    Raises:
        ValueError on missing/disabled server or missing config.
    """
    server_name = tool.get("mcp_server_name")
    action_name = tool.get("mcp_action_name")
    tool_name = tool.get("name")

    if not server_name or not action_name:
        raise ValueError(
            f"tool {tool_name!r} is mcp_action but missing "
            "mcp_server_name or mcp_action_name"
        )

    server_row = await get_server_by_name(server_name)
    if server_row is None:
        raise ValueError(
            f"tool {tool_name!r} references unknown MCP server {server_name!r}"
        )
    if not server_row["enabled"]:
        raise ValueError(
            f"tool {tool_name!r} cannot run: MCP server {server_name!r} is disabled"
        )

    # Action arguments come from payload.metadata. Falling back to {} keeps
    # the contract identical to the n8n runner (also reads metadata).
    arguments = payload.get("metadata") or {}
    if not isinstance(arguments, dict):
        raise ValueError(
            f"tool {tool_name!r} arguments must be a JSON object (got "
            f"{type(arguments).__name__})"
        )

    logger.info(
        "mcp_action start: tool=%s mcp_server=%s mcp_action=%s session_id=%s",
        tool_name,
        server_name,
        action_name,
        payload.get("session_id"),
    )

    client = McpClient(config_from_row(server_row))
    started = time.perf_counter()
    try:
        call_result = await client.call_tool(action_name, arguments)
    except McpError as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.warning(
            "mcp_action failed: tool=%s mcp_server=%s mcp_action=%s "
            "duration_ms=%s error=%s",
            tool_name,
            server_name,
            action_name,
            duration_ms,
            exc,
        )
        return {
            "status": "error",
            "http_status": None,
            "mcp_server": server_name,
            "mcp_action": action_name,
            "duration_ms": duration_ms,
            "response": None,
            "error": str(exc),
        }

    log_fn = logger.info if call_result.success else logger.warning
    log_fn(
        "mcp_action complete: tool=%s mcp_server=%s mcp_action=%s "
        "duration_ms=%s status=%s",
        tool_name,
        server_name,
        action_name,
        call_result.duration_ms,
        "ok" if call_result.success else "error",
    )

    return {
        "status": "ok" if call_result.success else "error",
        "http_status": None,
        "mcp_server": server_name,
        "mcp_action": action_name,
        "duration_ms": call_result.duration_ms,
        "response": call_result.payload,
        "error": call_result.error,
    }
