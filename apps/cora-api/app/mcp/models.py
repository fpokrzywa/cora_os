"""MCP data models — request/response shapes shared by the client + registry."""

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class McpServerConfig:
    """Subset of the mcp_servers row needed to talk to a server."""

    name: str
    server_type: str
    endpoint: str
    auth_type: Optional[str] = None
    auth_config: Optional[dict] = None


@dataclass
class McpToolDef:
    name: str
    description: Optional[str] = None
    input_schema: Optional[dict] = None


@dataclass
class McpResourceDef:
    uri: str
    name: Optional[str] = None
    description: Optional[str] = None
    mime_type: Optional[str] = None


@dataclass
class McpCapabilities:
    tools: list[McpToolDef] = field(default_factory=list)
    resources: list[McpResourceDef] = field(default_factory=list)
    server_info: dict = field(default_factory=dict)
    raw_initialize: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                }
                for t in self.tools
            ],
            "resources": [
                {
                    "uri": r.uri,
                    "name": r.name,
                    "description": r.description,
                    "mime_type": r.mime_type,
                }
                for r in self.resources
            ],
            "server_info": self.server_info,
        }


class McpError(Exception):
    """Wraps any failure to talk to an MCP server."""

    def __init__(self, message: str, *, cause: Optional[BaseException] = None):
        super().__init__(message)
        self.cause = cause


@dataclass
class McpCallResult:
    success: bool
    duration_ms: int
    payload: Any = None
    error: Optional[str] = None
