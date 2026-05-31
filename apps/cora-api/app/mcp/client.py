"""HTTP JSON-RPC 2.0 client for MCP servers.

v0.1 foundation: speaks plain HTTP JSON-RPC (POST envelope, JSON response).
Real MCP transports — stdio for local processes, streamable-HTTP / SSE for
persistent sessions — are deferred. The client surface (initialize, list_tools,
list_resources, call_tool, ping) is shaped to match the MCP protocol so a
future transport swap is contained to this module.

Tool execution (call_tool) is implemented but intentionally NOT exposed via
any HTTP endpoint in this release — the goal is discovery + governance only.
"""

import itertools
import logging
import time
from typing import Any, Optional

import httpx

from .models import (
    McpCallResult,
    McpCapabilities,
    McpError,
    McpResourceDef,
    McpServerConfig,
    McpToolDef,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0
PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "cora-api", "version": "0.1.0"}


class McpClient:
    def __init__(
        self,
        config: McpServerConfig,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.config = config
        self.timeout = timeout
        self._id_seq = itertools.count(1)

    # ---------- transport ----------

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        auth_type = (self.config.auth_type or "").lower()
        auth = self.config.auth_config or {}
        if auth_type == "bearer":
            token = auth.get("token") or auth.get("access_token")
            if token:
                headers["Authorization"] = f"Bearer {token}"
        elif auth_type == "header":
            for k, v in (auth.get("headers") or {}).items():
                headers[str(k)] = str(v)
        elif auth_type == "basic":
            user = auth.get("username", "")
            pw = auth.get("password", "")
            import base64
            b64 = base64.b64encode(f"{user}:{pw}".encode()).decode()
            headers["Authorization"] = f"Basic {b64}"
        return headers

    async def _rpc(self, method: str, params: Optional[dict] = None) -> Any:
        if self.config.server_type.lower() != "http":
            raise McpError(
                f"transport {self.config.server_type!r} not implemented in v0.1 "
                "(only 'http' is supported)"
            )

        rpc_id = next(self._id_seq)
        envelope: dict = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
        if params is not None:
            envelope["params"] = params

        logger.info(
            "mcp rpc: server=%s endpoint=%s method=%s",
            self.config.name,
            self.config.endpoint,
            method,
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    self.config.endpoint,
                    headers=self._auth_headers(),
                    json=envelope,
                )
        except httpx.HTTPError as exc:
            logger.exception(
                "mcp transport failure: server=%s method=%s",
                self.config.name,
                method,
            )
            raise McpError(f"transport error: {exc}", cause=exc) from exc

        if resp.status_code >= 400:
            snippet = resp.text[:200] if resp.text else ""
            raise McpError(
                f"HTTP {resp.status_code} from MCP server "
                f"(method={method}): {snippet}"
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise McpError(
                f"non-JSON response from MCP server (method={method})",
                cause=exc,
            ) from exc

        if isinstance(data, dict) and "error" in data:
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise McpError(f"JSON-RPC error from server: {msg}")

        return data.get("result") if isinstance(data, dict) else data

    # ---------- protocol methods ----------

    async def initialize(self) -> dict:
        result = await self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "clientInfo": CLIENT_INFO,
                "capabilities": {},
            },
        )
        return result if isinstance(result, dict) else {}

    async def ping(self) -> McpCallResult:
        start = time.perf_counter()
        try:
            await self._rpc("ping", {})
            duration_ms = int((time.perf_counter() - start) * 1000)
            logger.info(
                "mcp ping ok: server=%s duration_ms=%s",
                self.config.name,
                duration_ms,
            )
            return McpCallResult(success=True, duration_ms=duration_ms)
        except McpError as exc:
            # Many servers don't implement ping; fall back to initialize so
            # "test connection" still gives a meaningful signal.
            logger.info(
                "mcp ping unsupported, falling back to initialize: server=%s err=%s",
                self.config.name,
                exc,
            )
            try:
                await self.initialize()
                duration_ms = int((time.perf_counter() - start) * 1000)
                return McpCallResult(success=True, duration_ms=duration_ms)
            except McpError as exc2:
                duration_ms = int((time.perf_counter() - start) * 1000)
                logger.warning(
                    "mcp test failed: server=%s duration_ms=%s error=%s",
                    self.config.name,
                    duration_ms,
                    exc2,
                )
                return McpCallResult(
                    success=False, duration_ms=duration_ms, error=str(exc2)
                )

    async def list_tools(self) -> list[McpToolDef]:
        raw = await self._rpc("tools/list", {})
        tools_raw: list[dict] = []
        if isinstance(raw, dict):
            tools_raw = raw.get("tools", []) or []
        elif isinstance(raw, list):
            tools_raw = raw
        return [
            McpToolDef(
                name=t.get("name", ""),
                description=t.get("description"),
                input_schema=t.get("inputSchema") or t.get("input_schema"),
            )
            for t in tools_raw
            if isinstance(t, dict)
        ]

    async def list_resources(self) -> list[McpResourceDef]:
        raw = await self._rpc("resources/list", {})
        resources_raw: list[dict] = []
        if isinstance(raw, dict):
            resources_raw = raw.get("resources", []) or []
        elif isinstance(raw, list):
            resources_raw = raw
        return [
            McpResourceDef(
                uri=r.get("uri", ""),
                name=r.get("name"),
                description=r.get("description"),
                mime_type=r.get("mimeType") or r.get("mime_type"),
            )
            for r in resources_raw
            if isinstance(r, dict)
        ]

    async def discover_capabilities(self) -> McpCapabilities:
        start = time.perf_counter()
        cap = McpCapabilities()
        try:
            cap.raw_initialize = await self.initialize()
            cap.server_info = (
                cap.raw_initialize.get("serverInfo")
                or cap.raw_initialize.get("server_info")
                or {}
            )
        except McpError as exc:
            logger.warning(
                "mcp discover initialize failed: server=%s error=%s",
                self.config.name,
                exc,
            )
            raise

        try:
            cap.tools = await self.list_tools()
        except McpError as exc:
            logger.info(
                "mcp tools/list failed (may be unsupported): server=%s error=%s",
                self.config.name,
                exc,
            )

        try:
            cap.resources = await self.list_resources()
        except McpError as exc:
            logger.info(
                "mcp resources/list failed (may be unsupported): server=%s error=%s",
                self.config.name,
                exc,
            )

        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "mcp discover ok: server=%s tools=%s resources=%s duration_ms=%s",
            self.config.name,
            len(cap.tools),
            len(cap.resources),
            duration_ms,
        )
        return cap

    async def call_tool(self, tool_name: str, arguments: dict) -> McpCallResult:
        """Execute a tool. NOT exposed via HTTP endpoint in v0.1 — agents must
        not invoke this autonomously yet."""
        start = time.perf_counter()
        try:
            result = await self._rpc(
                "tools/call",
                {"name": tool_name, "arguments": arguments},
            )
            duration_ms = int((time.perf_counter() - start) * 1000)
            logger.info(
                "mcp tool call ok: server=%s tool=%s duration_ms=%s",
                self.config.name,
                tool_name,
                duration_ms,
            )
            return McpCallResult(
                success=True, duration_ms=duration_ms, payload=result
            )
        except McpError as exc:
            duration_ms = int((time.perf_counter() - start) * 1000)
            logger.warning(
                "mcp tool call failed: server=%s tool=%s duration_ms=%s error=%s",
                self.config.name,
                tool_name,
                duration_ms,
                exc,
            )
            return McpCallResult(
                success=False, duration_ms=duration_ms, error=str(exc)
            )
