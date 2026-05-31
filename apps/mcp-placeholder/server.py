"""Generic MCP placeholder server.

Shared by mcp-postgres and mcp-github. Real implementations replace these.
Exposes the same REST + JSON-RPC surfaces as the filesystem server but with
empty tool/resource lists — useful as a discovery target while building out
the rest of the stack.
"""

import os
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

SERVER_NAME = os.environ.get("MCP_SERVER_NAME", "cora-mcp-placeholder")
SERVER_VERSION = "0.1.0-placeholder"
PROTOCOL_VERSION = "2024-11-05"

app = FastAPI(title=SERVER_NAME, version=SERVER_VERSION)

CAPABILITIES = {
    "server_info": {"name": SERVER_NAME, "version": SERVER_VERSION},
    "tools": [],
    "resources": [],
    "note": "Placeholder MCP server. Tools to be implemented.",
    "transport": ["rest", "jsonrpc-2.0-http"],
}


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "server": SERVER_NAME,
        "version": SERVER_VERSION,
        "placeholder": True,
    }


@app.get("/capabilities")
async def capabilities() -> dict:
    return CAPABILITIES


class ExecuteRequest(BaseModel):
    action: str
    arguments: Optional[dict] = None


@app.post("/execute")
async def execute(req: ExecuteRequest) -> dict:
    raise HTTPException(
        status_code=501,
        detail=f"{SERVER_NAME} is a placeholder; no actions implemented",
    )


def _rpc_ok(rpc_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _rpc_err(rpc_id: Any, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": code, "message": message},
    }


@app.post("/")
async def rpc(envelope: dict) -> dict:
    rpc_id = envelope.get("id")
    method = envelope.get("method")
    if method == "initialize":
        return _rpc_ok(rpc_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "capabilities": {},
        })
    if method == "ping":
        return _rpc_ok(rpc_id, {})
    if method == "tools/list":
        return _rpc_ok(rpc_id, {"tools": []})
    if method == "resources/list":
        return _rpc_ok(rpc_id, {"resources": []})
    if method == "tools/call":
        return _rpc_err(
            rpc_id, -32601, f"{SERVER_NAME} is a placeholder; no tools implemented"
        )
    return _rpc_err(rpc_id, -32601, f"method not found: {method}")
