"""Lightweight read-only filesystem MCP server (v0.1 placeholder).

Exposes:
  GET  /health          — liveness
  GET  /capabilities    — REST capabilities discovery
  POST /execute         — REST {action, arguments} dispatch
  POST /                — JSON-RPC 2.0 (initialize, ping, tools/list,
                          resources/list, tools/call) for compatibility with
                          Cora's existing MCP client.

Read-only by design. Only list_directory and read_file are implemented.
Every path is resolved against /workspace and rejected if it escapes the root
(symlinks resolved before the check).
"""

import json
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

WORKSPACE = Path(os.environ.get("MCP_WORKSPACE", "/workspace")).resolve()
SERVER_NAME = os.environ.get("MCP_SERVER_NAME", "cora-mcp-filesystem")
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"
MAX_READ_BYTES = int(os.environ.get("MCP_MAX_READ_BYTES", str(256 * 1024)))

app = FastAPI(title=SERVER_NAME, version=SERVER_VERSION)


def _safe_resolve(rel: str) -> Path:
    rel = (rel or "").lstrip("/").lstrip("\\")
    candidate = (WORKSPACE / rel).resolve()
    try:
        candidate.relative_to(WORKSPACE)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"path escapes workspace: {rel!r}"
        ) from exc
    return candidate


TOOL_DEFS: list[dict] = [
    {
        "name": "list_directory",
        "description": (
            "List entries in a workspace directory. Read-only. "
            "`path` is relative to the workspace root."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to /workspace. Defaults to '.'",
                }
            },
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a file from the workspace. Read-only. Capped at "
            f"{MAX_READ_BYTES} bytes; truncation is reported in the response."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
]

CAPABILITIES = {
    "server_info": {"name": SERVER_NAME, "version": SERVER_VERSION},
    "tools": TOOL_DEFS,
    "resources": [],
    "transport": ["rest", "jsonrpc-2.0-http"],
}


def _list_directory(path: str = ".") -> dict:
    target = _safe_resolve(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"not found: {path}")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail=f"not a directory: {path}")
    entries: list[dict] = []
    for entry in sorted(
        target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
    ):
        try:
            stat = entry.stat()
        except OSError:
            continue
        entries.append(
            {
                "name": entry.name,
                "type": "dir" if entry.is_dir() else "file",
                "size_bytes": stat.st_size if entry.is_file() else None,
            }
        )
    rel = target.relative_to(WORKSPACE).as_posix() or "."
    return {"path": rel, "count": len(entries), "entries": entries}


def _read_file(path: str) -> dict:
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    target = _safe_resolve(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"not found: {path}")
    if not target.is_file():
        raise HTTPException(status_code=400, detail=f"not a regular file: {path}")
    size = target.stat().st_size
    truncated = size > MAX_READ_BYTES
    with target.open("rb") as fh:
        data = fh.read(MAX_READ_BYTES)
    try:
        text = data.decode("utf-8")
        binary = False
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
        binary = True
    rel = target.relative_to(WORKSPACE).as_posix()
    return {
        "path": rel,
        "size_bytes": size,
        "truncated": truncated,
        "binary": binary,
        "content": text,
    }


ACTIONS = {
    "list_directory": _list_directory,
    "read_file": _read_file,
}


# ---------- REST surface ----------

@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "server": SERVER_NAME,
        "version": SERVER_VERSION,
        "workspace": str(WORKSPACE),
    }


@app.get("/capabilities")
async def capabilities() -> dict:
    return CAPABILITIES


class ExecuteRequest(BaseModel):
    action: str
    arguments: Optional[dict] = None


@app.post("/execute")
async def execute(req: ExecuteRequest) -> dict:
    handler = ACTIONS.get(req.action)
    if handler is None:
        raise HTTPException(
            status_code=400, detail=f"unknown action: {req.action!r}"
        )
    args = req.arguments or {}
    try:
        result = handler(**args)
    except HTTPException:
        raise
    except TypeError as exc:
        raise HTTPException(
            status_code=400, detail=f"bad arguments for {req.action!r}: {exc}"
        ) from exc
    return {"action": req.action, "result": result}


# ---------- JSON-RPC 2.0 (for Cora's existing MCP client) ----------


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
    params = envelope.get("params") or {}

    if method == "initialize":
        return _rpc_ok(rpc_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "capabilities": {"tools": {}, "resources": {}},
        })
    if method == "ping":
        return _rpc_ok(rpc_id, {})
    if method == "tools/list":
        return _rpc_ok(rpc_id, {
            "tools": [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "inputSchema": t["input_schema"],
                }
                for t in TOOL_DEFS
            ]
        })
    if method == "resources/list":
        return _rpc_ok(rpc_id, {"resources": []})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = ACTIONS.get(name)
        if handler is None:
            return _rpc_err(rpc_id, -32601, f"unknown tool: {name}")
        try:
            result = handler(**args)
        except HTTPException as exc:
            return _rpc_err(rpc_id, -32000, str(exc.detail))
        except TypeError as exc:
            return _rpc_err(rpc_id, -32602, f"bad arguments: {exc}")
        return _rpc_ok(rpc_id, {
            "content": [
                {"type": "text", "text": json.dumps(result, indent=2)}
            ]
        })
    return _rpc_err(rpc_id, -32601, f"method not found: {method}")
