"""MCP integration layer.

Cora's bridge to external Model Context Protocol servers. v0.1 supports
HTTP JSON-RPC transport and exposes admin endpoints for discovery + manual
connection testing. Tool execution is implemented in the client but NOT
exposed to agents or HTTP yet.
"""

from .client import McpClient
from .models import (
    McpCallResult,
    McpCapabilities,
    McpError,
    McpResourceDef,
    McpServerConfig,
    McpToolDef,
)
from .registry import (
    create_server,
    discover_and_cache,
    get_server_by_name,
    list_servers,
    seed_mcp_servers,
    store_capabilities,
    update_server,
)

__all__ = [
    "McpClient",
    "McpCallResult",
    "McpCapabilities",
    "McpError",
    "McpResourceDef",
    "McpServerConfig",
    "McpToolDef",
    "create_server",
    "discover_and_cache",
    "get_server_by_name",
    "list_servers",
    "seed_mcp_servers",
    "store_capabilities",
    "update_server",
]
