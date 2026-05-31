"""Provider OAuth connector endpoints (Credential Vault v0.6) — READINESS ONLY.

No OAuth flow, no provider API call, no email/calendar execution. Placeholder
create / disconnect / readiness are governed (tool_execution_logs) and traced
(runtime_traces). Secrets are never returned (masked to has_* flags).
"""

import time
import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app import provider_oauth as po
from app.auth import CurrentUser, get_current_user
from app.runtime_traces import write_trace
from app.tools.governance import check_permission, fetch_tool, log_execution_attempt

router = APIRouter(prefix="/provider-connectors", tags=["provider-connectors"])

_PLACEHOLDER_TOOL = "provider_connector_placeholder_created"
_DISCONNECT_TOOL = "provider_connector_disconnected"
_READINESS_TOOL = "provider_readiness_checked"

_CODE_TO_STATUS = {
    "invalid": status.HTTP_400_BAD_REQUEST,
    "not_found": status.HTTP_404_NOT_FOUND,
    "forbidden": status.HTTP_403_FORBIDDEN,
    "conflict": status.HTTP_409_CONFLICT,
    "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _err(exc: po.ProviderConnectorError) -> HTTPException:
    return HTTPException(
        status_code=_CODE_TO_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=str(exc),
    )


def _parse_uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} must be a valid UUID",
        ) from exc


class RegisterPlaceholderRequest(BaseModel):
    provider_name: str = Field(min_length=1, max_length=100)
    provider_type: Optional[str] = Field(default=None, max_length=50)
    scopes: Optional[list] = None
    workspace_id: Optional[str] = None


async def _govern(tool_name: str, current: CurrentUser) -> None:
    tool = await fetch_tool(tool_name)
    if tool is None:
        return
    decision = await check_permission(
        tool, agent_name=None, user_id=current.id,
        is_admin=(current.role == "admin"),
    )
    if not decision.allowed:
        await log_execution_attempt(
            tool_name=tool_name, agent_name=None, session_id=None,
            user_id=current.id, scope_type=None, allowed=False,
            duration_ms=None, status="denied", error_message=decision.reason,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=decision.reason
        )


@router.get("")
async def list_provider_connectors(
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    return await po.list_connectors(
        user_id=current.id, is_admin=(current.role == "admin")
    )


@router.get("/readiness")
async def provider_readiness(
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    await _govern(_READINESS_TOOL, current)
    started = time.perf_counter()
    result = await po.readiness(
        user_id=current.id, is_admin=(current.role == "admin")
    )
    duration_ms = int((time.perf_counter() - started) * 1000)
    await log_execution_attempt(
        tool_name=_READINESS_TOOL, agent_name=None, session_id=None,
        user_id=current.id, scope_type=None, allowed=True,
        duration_ms=duration_ms, status="success", error_message=None,
    )
    await write_trace(
        session_id=None, user_id=current.id,
        trace_type="provider_readiness_checked", status="ok",
        selected_agent=None, tool_name=_READINESS_TOOL,
        tool_result={
            "provider_count": len(result["providers"]),
            "ready_count": sum(1 for p in result["providers"] if p["ready_for_execution"]),
            "encryption_available": result["encryption_available"],
            "execution_enabled": result["execution_enabled"],
        },
    )
    return result


@router.get("/{connector_id}")
async def get_provider_connector(
    connector_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    try:
        return await po.get_connector(
            _parse_uuid(connector_id, "connector_id"),
            user_id=current.id, is_admin=(current.role == "admin"),
        )
    except po.ProviderConnectorError as exc:
        raise _err(exc)


@router.post("/register-placeholder", status_code=status.HTTP_201_CREATED)
async def register_placeholder(
    req: RegisterPlaceholderRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    await _govern(_PLACEHOLDER_TOOL, current)
    started = time.perf_counter()
    try:
        connector = await po.register_placeholder(
            user_id=current.id,
            provider_name=req.provider_name,
            provider_type=req.provider_type,
            scopes=req.scopes,
            workspace_id=_parse_uuid(req.workspace_id, "workspace_id")
            if req.workspace_id else None,
        )
    except po.ProviderConnectorError as exc:
        await log_execution_attempt(
            tool_name=_PLACEHOLDER_TOOL, agent_name=None, session_id=None,
            user_id=current.id, scope_type=None, allowed=True,
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="failed", error_message=str(exc),
        )
        raise _err(exc)
    await log_execution_attempt(
        tool_name=_PLACEHOLDER_TOOL, agent_name=None, session_id=None,
        user_id=current.id, scope_type=None, allowed=True,
        duration_ms=int((time.perf_counter() - started) * 1000),
        status="success", error_message=None,
    )
    await write_trace(
        session_id=None, user_id=current.id,
        trace_type="provider_connector_placeholder_created", status="ok",
        selected_agent=None, tool_name=_PLACEHOLDER_TOOL,
        tool_result={
            "connector_id": str(connector["id"]),
            "provider_name": connector["provider_name"],
            "provider_type": connector["provider_type"],
            "status": connector["status"],
        },
        workspace_id=connector.get("workspace_id"),
    )
    return connector


@router.patch("/{connector_id}/disconnect")
async def disconnect_provider_connector(
    connector_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    cid = _parse_uuid(connector_id, "connector_id")
    await _govern(_DISCONNECT_TOOL, current)
    started = time.perf_counter()
    try:
        connector = await po.disconnect(
            cid, user_id=current.id, is_admin=(current.role == "admin")
        )
    except po.ProviderConnectorError as exc:
        await log_execution_attempt(
            tool_name=_DISCONNECT_TOOL, agent_name=None, session_id=None,
            user_id=current.id, scope_type=None, allowed=True,
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="failed", error_message=str(exc),
        )
        raise _err(exc)
    await log_execution_attempt(
        tool_name=_DISCONNECT_TOOL, agent_name=None, session_id=None,
        user_id=current.id, scope_type=None, allowed=True,
        duration_ms=int((time.perf_counter() - started) * 1000),
        status="success", error_message=None,
    )
    await write_trace(
        session_id=None, user_id=current.id,
        trace_type="provider_connector_disconnected", status="ok",
        selected_agent=None, tool_name=_DISCONNECT_TOOL,
        tool_result={
            "connector_id": str(connector["id"]),
            "provider_name": connector["provider_name"],
            "status": connector["status"],
        },
        workspace_id=connector.get("workspace_id"),
    )
    return connector
