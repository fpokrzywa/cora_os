"""External Integration Readiness endpoints (v0.4) — DRY-RUN ONLY.

Shared list/read/validate/confirm/cancel/events over external_integration_intents.
Per-source creation lives in routers/signal.py and routers/chronos.py. Nothing
here performs an external action; confirmation is internal only.
"""

import logging
import uuid
from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app import integration_readiness as ir
from app.auth import CurrentUser, get_current_user
from app.runtime_traces import write_trace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integration", tags=["integration"])

SAFETY_NOTE = ir.SAFETY_DRY_RUN


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class IntentActionRequest(BaseModel):
    notes: Optional[str] = Field(default=None, max_length=2000)


class IntentOut(BaseModel):
    id: str
    workspace_id: Optional[str]
    created_by: Optional[str]
    source_type: str
    source_id: str
    agent_name: str
    provider_type: str
    provider_name: str
    action_type: str
    status: str
    dry_run: bool
    requires_confirmation: bool
    confirmation_required_reason: Optional[str]
    payload_preview: dict
    validation_result: dict
    metadata: dict
    confirmed_by: Optional[str]
    confirmed_at: Optional[datetime]
    cancelled_by: Optional[str]
    cancelled_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    safety_note: str = SAFETY_NOTE


class IntentEventOut(BaseModel):
    id: str
    intent_id: str
    user_id: Optional[str]
    event_type: str
    from_status: Optional[str]
    to_status: Optional[str]
    notes: Optional[str]
    payload_snapshot: dict
    created_at: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_CODE_TO_STATUS = {
    "invalid": status.HTTP_400_BAD_REQUEST,
    "not_found": status.HTTP_404_NOT_FOUND,
    "forbidden": status.HTTP_403_FORBIDDEN,
    "conflict": status.HTTP_409_CONFLICT,
    "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
}


def err(exc: ir.IntegrationError) -> HTTPException:
    return HTTPException(
        status_code=_CODE_TO_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=str(exc),
    )


def parse_uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} must be a valid UUID",
        ) from exc


def intent_to_out(row: dict) -> IntentOut:
    return IntentOut(
        id=str(row["id"]),
        workspace_id=str(row["workspace_id"]) if row.get("workspace_id") else None,
        created_by=str(row["created_by"]) if row.get("created_by") else None,
        source_type=row["source_type"],
        source_id=str(row["source_id"]),
        agent_name=row["agent_name"],
        provider_type=row["provider_type"],
        provider_name=row["provider_name"],
        action_type=row["action_type"],
        status=row["status"],
        dry_run=row["dry_run"],
        requires_confirmation=row["requires_confirmation"],
        confirmation_required_reason=row.get("confirmation_required_reason"),
        payload_preview=row.get("payload_preview") or {},
        validation_result=row.get("validation_result") or {},
        metadata=row.get("metadata") or {},
        confirmed_by=str(row["confirmed_by"]) if row.get("confirmed_by") else None,
        confirmed_at=row.get("confirmed_at"),
        cancelled_by=str(row["cancelled_by"]) if row.get("cancelled_by") else None,
        cancelled_at=row.get("cancelled_at"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


_TOOL_FOR_PROVIDER = {
    "email": "signal_prepare_email_send_intent",
    "calendar": "chronos_prepare_calendar_event_intent",
}


async def write_intent_trace(
    intent: dict,
    *,
    trace_type: str,
    user_id: uuid.UUID,
    trace_status: str = "ok",
) -> None:
    """Emit a runtime trace carrying the full readiness context."""
    validation = intent.get("validation_result") or {}
    await write_trace(
        session_id=None,
        user_id=user_id,
        trace_type=trace_type,
        status=trace_status,
        selected_agent=intent.get("agent_name"),
        tool_name=_TOOL_FOR_PROVIDER.get(intent.get("provider_type")),
        tool_result={
            "intent_id": str(intent["id"]),
            "source_type": intent["source_type"],
            "source_id": str(intent["source_id"]),
            "agent_name": intent["agent_name"],
            "provider_type": intent["provider_type"],
            "provider_name": intent["provider_name"],
            "action_type": intent["action_type"],
            "from_status": intent.get("_from_status"),
            "to_status": intent.get("_to_status", intent.get("status")),
            "dry_run": intent.get("dry_run", True),
            "hard_error_count": validation.get("hard_error_count", 0),
            "warning_count": validation.get("warning_count", 0),
        },
        workspace_id=intent.get("workspace_id"),
    )


async def write_provider_trace(
    intent: dict,
    *,
    trace_type: str,
    user_id: uuid.UUID,
    validation: dict,
) -> None:
    """Provider-layer trace: always external_action_performed=false, dry_run=true."""
    await write_trace(
        session_id=None,
        user_id=user_id,
        trace_type=trace_type,
        status="ok",
        selected_agent=intent.get("agent_name"),
        tool_name=_TOOL_FOR_PROVIDER.get(intent.get("provider_type")),
        tool_result={
            "intent_id": str(intent["id"]),
            "provider_name": intent["provider_name"],
            "provider_type": intent["provider_type"],
            "dry_run": True,
            "external_action_performed": False,
            "hard_error_count": validation.get("hard_error_count", 0),
            "warning_count": validation.get("warning_count", 0),
        },
        workspace_id=intent.get("workspace_id"),
    )


async def _require_visible_intent(intent_id: str, current: CurrentUser) -> dict:
    intent = await ir.get_intent(parse_uuid(intent_id, "intent_id"))
    if intent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="intent not found"
        )
    if current.role != "admin" and intent["created_by"] != current.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="intent not found"
        )
    return intent


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/intents", response_model=list[IntentOut])
async def list_integration_intents(
    current: Annotated[CurrentUser, Depends(get_current_user)],
    workspace_id: Optional[str] = None,
    source_type: Optional[str] = None,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    agent_name: Optional[str] = None,
) -> list[IntentOut]:
    wid = parse_uuid(workspace_id, "workspace_id") if workspace_id else None
    owner_id = None if current.role == "admin" else current.id
    try:
        rows = await ir.list_intents(
            workspace_id=wid,
            owner_id=owner_id,
            source_type=source_type,
            status=status_filter,
            agent_name=agent_name,
        )
    except ir.IntegrationError as exc:
        raise err(exc)
    return [intent_to_out(r) for r in rows]


@router.get("/intents/{intent_id}", response_model=IntentOut)
async def get_integration_intent(
    intent_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> IntentOut:
    return intent_to_out(await _require_visible_intent(intent_id, current))


@router.post("/intents/{intent_id}/validate", response_model=IntentOut)
async def validate_integration_intent_endpoint(
    intent_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> IntentOut:
    await _require_visible_intent(intent_id, current)
    try:
        result = await ir.revalidate_intent(
            parse_uuid(intent_id, "intent_id"), current.id
        )
    except ir.IntegrationError as exc:
        raise err(exc)
    await write_intent_trace(
        result, trace_type="integration_intent_validated", user_id=current.id
    )
    if result["status"] == ir.STATUS_BLOCKED:
        await write_intent_trace(
            result, trace_type="integration_intent_blocked", user_id=current.id
        )
    return intent_to_out(result)


@router.post("/intents/{intent_id}/dry-run", response_model=IntentOut)
async def dry_run_integration_intent_endpoint(
    intent_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> IntentOut:
    """Run the provider connector dry-run. No external action is performed; the
    intent is NOT moved to an executed state."""
    await _require_visible_intent(intent_id, current)
    try:
        result = await ir.dry_run_intent(parse_uuid(intent_id, "intent_id"), current.id)
    except ir.IntegrationError as exc:
        raise err(exc)
    validation = result.get("_validation") or {}
    # provider_payload_validated + provider_dry_run_executed traces.
    await write_provider_trace(
        result, trace_type="provider_payload_validated", user_id=current.id,
        validation=validation,
    )
    await write_provider_trace(
        result, trace_type="provider_dry_run_executed", user_id=current.id,
        validation=validation,
    )
    return intent_to_out(result)


@router.post("/intents/{intent_id}/confirm", response_model=IntentOut)
async def confirm_integration_intent_endpoint(
    intent_id: str,
    req: IntentActionRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> IntentOut:
    await _require_visible_intent(intent_id, current)
    try:
        result = await ir.confirm_integration_intent(
            parse_uuid(intent_id, "intent_id"),
            user_id=current.id,
            is_admin=(current.role == "admin"),
            notes=req.notes,
        )
    except ir.IntegrationError as exc:
        raise err(exc)
    await write_intent_trace(
        result, trace_type="integration_intent_confirmed", user_id=current.id
    )
    return intent_to_out(result)


@router.post("/intents/{intent_id}/cancel", response_model=IntentOut)
async def cancel_integration_intent_endpoint(
    intent_id: str,
    req: IntentActionRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> IntentOut:
    intent = await _require_visible_intent(intent_id, current)
    try:
        result = await ir.cancel_integration_intent(
            parse_uuid(intent_id, "intent_id"),
            user_id=current.id,
            is_admin=(current.role == "admin"),
            is_owner=(intent["created_by"] == current.id),
            notes=req.notes,
        )
    except ir.IntegrationError as exc:
        raise err(exc)
    await write_intent_trace(
        result, trace_type="integration_intent_cancelled", user_id=current.id
    )
    return intent_to_out(result)


@router.get("/intents/{intent_id}/events", response_model=list[IntentEventOut])
async def list_integration_intent_events(
    intent_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> list[IntentEventOut]:
    await _require_visible_intent(intent_id, current)
    rows = await ir.list_events(parse_uuid(intent_id, "intent_id"))
    return [
        IntentEventOut(
            id=str(r["id"]),
            intent_id=str(r["intent_id"]),
            user_id=str(r["user_id"]) if r["user_id"] else None,
            event_type=r["event_type"],
            from_status=r["from_status"],
            to_status=r["to_status"],
            notes=r["notes"],
            payload_snapshot=r["payload_snapshot"] or {},
            created_at=r["created_at"],
        )
        for r in rows
    ]
