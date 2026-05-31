import logging
from typing import Any, Awaitable, Callable, Optional

from .mcp import run_mcp_action
from .n8n import run_n8n_webhook
from .web_search import run_web_search

logger = logging.getLogger(__name__)

ToolRunner = Callable[[dict, dict], Awaitable[dict[str, Any]]]

_RUNNERS: dict[str, ToolRunner] = {
    "n8n_webhook": run_n8n_webhook,
    "mcp_action": run_mcp_action,
    "web_search": run_web_search,
    # TODO(SIGNAL/CHRONOS chat-to-draft v0.2): register an "internal_action"
    # runner that dispatches signal_create_draft -> signal_tools.create_communication_draft
    # and chronos_create_schedule_proposal -> chronos_tools.create_schedule_proposal.
    # These tools are seeded (requires_confirmation=TRUE) and exposed via REST
    # CRUD today; chat-triggered invocation is intentionally deferred. Until
    # then dispatch_tool() raises "no runner" for internal_action, which is the
    # safe default (no ungoverned draft creation from the chat path).
}


def get_runner(tool_type: str) -> Optional[ToolRunner]:
    return _RUNNERS.get(tool_type)


async def dispatch_tool(tool: dict, payload: dict) -> dict[str, Any]:
    runner = get_runner(tool["type"])
    if runner is None:
        raise ValueError(f"no runner registered for tool type {tool['type']!r}")
    return await runner(tool, payload)
