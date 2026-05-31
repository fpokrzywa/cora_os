"""Integration Readiness Queue endpoints (v0.6).

Create an internal `external_integration_intents` record from an APPROVED SIGNAL
draft or CHRONOS proposal that represents a FUTURE provider action. This is NOT
external execution: nothing here sends mail, writes a calendar, or touches a
provider/OAuth. A fresh intent is blocked_no_provider / provider_status=
not_configured because no real provider is enabled. Creation + cancellation are
governed (tool_execution_logs) and traced (runtime_traces).
"""

import time
import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app import execution_guard as guard
from app import integration_readiness as ir
from app import oauth_readiness as orr
from app import provider_credential_simulation as pcs
from app.auth import CurrentUser, get_current_user
from app.routers.integration import IntentOut, intent_to_out, err, parse_uuid
from app.runtime_traces import write_trace
from app.tools.governance import check_permission, fetch_tool, log_execution_attempt

router = APIRouter(prefix="/integration-intents", tags=["integration-intents"])

_EMAIL_INTENT_TOOL = "signal_create_email_integration_intent"
_CALENDAR_INTENT_TOOL = "chronos_create_calendar_integration_intent"
_CANCEL_TOOL = "integration_intent_cancelled"
_SIMULATE_TOOL = "oauth_readiness_simulated"
_READINESS_CHECK_TOOL = "integration_intent_readiness_checked"
_SIMULATE_CRED_TOOL = "provider_credential_usage_simulated"
_CONFIRM_TOOL = "integration_intent_confirmed"
_REVOKE_TOOL = "integration_intent_confirmation_revoked"


def _readiness_err(exc: orr.ReadinessError) -> HTTPException:
    return HTTPException(
        status_code=err(ir.IntegrationError(str(exc), code=exc.code)).status_code,
        detail=str(exc),
    )


class IntentActionRequest(BaseModel):
    notes: Optional[str] = Field(default=None, max_length=2000)


async def _govern(tool_name: str, agent_name: str, current: CurrentUser) -> None:
    """Governance-first gate for an internal intent action. Logs a denial and
    raises 403 if the governed tool denies; otherwise returns (the success/fail
    log is written by the caller around the actual work)."""
    tool = await fetch_tool(tool_name)
    if tool is None:
        return
    decision = await check_permission(
        tool, agent_name=agent_name, user_id=current.id,
        is_admin=(current.role == "admin"),
    )
    if not decision.allowed:
        await log_execution_attempt(
            tool_name=tool_name, agent_name=agent_name, session_id=None,
            user_id=current.id, scope_type=None, allowed=False,
            duration_ms=None, status="denied", error_message=decision.reason,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=decision.reason
        )


async def _create_from_source(
    *, tool_name: str, agent_name: str, build, current: CurrentUser,
    notes: Optional[str],
) -> IntentOut:
    await _govern(tool_name, agent_name, current)
    started = time.perf_counter()
    try:
        intent = await build()
    except ir.IntegrationError as exc:
        await log_execution_attempt(
            tool_name=tool_name, agent_name=agent_name, session_id=None,
            user_id=current.id, scope_type=None, allowed=True,
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="error", error_message=str(exc),
        )
        await write_trace(
            session_id=None, user_id=current.id,
            trace_type="integration_intent_created", status="error",
            selected_agent=agent_name, tool_name=tool_name,
            tool_result={"error": str(exc)}, error_message=str(exc),
        )
        raise err(exc)
    duration_ms = int((time.perf_counter() - started) * 1000)
    await log_execution_attempt(
        tool_name=tool_name, agent_name=agent_name, session_id=None,
        user_id=current.id, scope_type=None, allowed=True,
        duration_ms=duration_ms, status="success", error_message=None,
    )
    await write_trace(
        session_id=None, user_id=current.id,
        trace_type="integration_intent_created", status="ok",
        selected_agent=agent_name, tool_name=tool_name,
        tool_result={
            "intent_id": str(intent["id"]),
            "intent_type": (intent.get("metadata") or {}).get("intent_type"),
            "action_type": intent["action_type"],
            "source_type": intent["source_type"],
            "source_id": str(intent["source_id"]),
            "status": intent["status"],
            "provider_status": (intent.get("metadata") or {}).get("provider_status"),
        },
        workspace_id=intent.get("workspace_id"),
    )
    return intent_to_out(intent)


async def _visible_intent(intent_id: str, current: CurrentUser) -> dict:
    intent = await ir.get_intent(parse_uuid(intent_id, "intent_id"))
    if intent is None or (
        current.role != "admin" and intent["created_by"] != current.id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="intent not found"
        )
    return intent


@router.post("/from-draft/{draft_id}", response_model=IntentOut, status_code=status.HTTP_201_CREATED)
async def create_email_intent_from_draft(
    draft_id: str,
    req: IntentActionRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> IntentOut:
    return await _create_from_source(
        tool_name=_EMAIL_INTENT_TOOL, agent_name="SIGNAL", current=current,
        notes=req.notes,
        build=lambda: ir.create_readiness_intent_from_draft(
            parse_uuid(draft_id, "draft_id"),
            user_id=current.id, is_admin=(current.role == "admin"), notes=req.notes,
        ),
    )


@router.post("/from-proposal/{proposal_id}", response_model=IntentOut, status_code=status.HTTP_201_CREATED)
async def create_calendar_intent_from_proposal(
    proposal_id: str,
    req: IntentActionRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> IntentOut:
    return await _create_from_source(
        tool_name=_CALENDAR_INTENT_TOOL, agent_name="CHRONOS", current=current,
        notes=req.notes,
        build=lambda: ir.create_readiness_intent_from_proposal(
            parse_uuid(proposal_id, "proposal_id"),
            user_id=current.id, is_admin=(current.role == "admin"), notes=req.notes,
        ),
    )


@router.get("", response_model=list[IntentOut])
async def list_integration_intents(
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> list[IntentOut]:
    is_admin = current.role == "admin"
    rows = await ir.list_intents(
        workspace_id=None,
        owner_id=None if is_admin else current.id,
    )
    # Show only readiness-queue intents (the v0.4 dry-run flow has its own view).
    rows = [r for r in rows if (r.get("metadata") or {}).get("workflow") == ir.RQ_WORKFLOW_TAG]
    return [intent_to_out(r) for r in rows]


# Declared before GET /{intent_id} so "readiness-summary" isn't parsed as an id.
@router.get("/readiness-summary")
async def integration_readiness_summary(
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    return await orr.readiness_summary(
        user_id=current.id, is_admin=(current.role == "admin")
    )


# Declared before GET /{intent_id} so "execution-status" isn't parsed as an id.
@router.get("/execution-status")
async def integration_execution_status(
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    """Global External Execution Kill Switch state (v0.8). Backs the UI safety
    banner. Always reports disabled in this phase."""
    return guard.execution_status()


@router.get("/{intent_id}", response_model=IntentOut)
async def get_integration_intent(
    intent_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> IntentOut:
    return intent_to_out(await _visible_intent(intent_id, current))


@router.get("/{intent_id}/readiness")
async def get_intent_readiness(
    intent_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    try:
        return await orr.get_readiness(
            parse_uuid(intent_id, "intent_id"),
            user_id=current.id, is_admin=(current.role == "admin"),
        )
    except orr.ReadinessError as exc:
        raise _readiness_err(exc)


@router.post("/{intent_id}/simulate-readiness")
async def simulate_intent_readiness(
    intent_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    iid = parse_uuid(intent_id, "intent_id")
    await _govern(_SIMULATE_TOOL, None, current)
    started = time.perf_counter()
    try:
        result = await orr.simulate_readiness(
            iid, user_id=current.id, is_admin=(current.role == "admin"),
        )
    except orr.ReadinessError as exc:
        await log_execution_attempt(
            tool_name=_SIMULATE_TOOL, agent_name=None, session_id=None,
            user_id=current.id, scope_type=None, allowed=True,
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="failed", error_message=str(exc),
        )
        raise _readiness_err(exc)
    await log_execution_attempt(
        tool_name=_SIMULATE_TOOL, agent_name=None, session_id=None,
        user_id=current.id, scope_type=None, allowed=True,
        duration_ms=int((time.perf_counter() - started) * 1000),
        status="success", error_message=None,
    )
    await write_trace(
        session_id=None, user_id=current.id,
        trace_type="oauth_readiness_simulated", status="ok",
        selected_agent=None, tool_name=_SIMULATE_TOOL,
        tool_result={
            "intent_id": result["intent_id"],
            "intent_type": result["intent_type"],
            "ready_for_execution": result["ready_for_execution"],
            "connector_status": result["connector_status"],
            "blocker_count": len(result["blockers"]),
            "missing_scopes": result["missing_scopes"],
        },
    )
    return result


@router.post("/{intent_id}/check-readiness")
async def check_intent_readiness(
    intent_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    """Readiness check for the queue: simulate + record the result into the
    intent's validation_result. Governed/audited/traced as
    `integration_intent_readiness_checked`. No provider call; dry_run stays true."""
    iid = parse_uuid(intent_id, "intent_id")
    await _govern(_READINESS_CHECK_TOOL, None, current)
    started = time.perf_counter()
    try:
        result = await orr.check_intent_readiness(
            iid, user_id=current.id, is_admin=(current.role == "admin"),
        )
    except orr.ReadinessError as exc:
        await log_execution_attempt(
            tool_name=_READINESS_CHECK_TOOL, agent_name=None, session_id=None,
            user_id=current.id, scope_type=None, allowed=True,
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="failed", error_message=str(exc),
        )
        raise _readiness_err(exc)
    await log_execution_attempt(
        tool_name=_READINESS_CHECK_TOOL, agent_name=None, session_id=None,
        user_id=current.id, scope_type=None, allowed=True,
        duration_ms=int((time.perf_counter() - started) * 1000),
        status="success", error_message=None,
    )
    await write_trace(
        session_id=None, user_id=current.id,
        trace_type="integration_intent_readiness_checked", status="ok",
        selected_agent=None, tool_name=_READINESS_CHECK_TOOL,
        tool_result={
            "intent_id": result["intent_id"],
            "intent_type": result["intent_type"],
            "ready_for_execution": result["ready_for_execution"],
            "connector_status": result["connector_status"],
            "blocker_count": len(result["blockers"]),
            "missing_scopes": result["missing_scopes"],
        },
    )
    return result


@router.post("/{intent_id}/simulate-provider-payload")
async def simulate_provider_payload(
    intent_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    """Provider Credential Usage Simulation v1.3: resolve the connected provider
    credential, validate it, and generate the provider-ready payload preview that
    WOULD be sent — then confirm the kill switch blocks execution. Governed +
    audited as `provider_credential_usage_simulated`; the service writes the
    provider_credential_resolved / provider_payload_simulated /
    provider_execution_blocked_by_governance traces. No provider API call; nothing
    is sent or created; tokens are never exposed; dry_run stays true."""
    iid = parse_uuid(intent_id, "intent_id")
    await _govern(_SIMULATE_CRED_TOOL, None, current)
    started = time.perf_counter()
    try:
        result = await pcs.simulate_credential_usage(
            iid, user_id=current.id, is_admin=(current.role == "admin"),
        )
    except pcs.SimulationError as exc:
        await log_execution_attempt(
            tool_name=_SIMULATE_CRED_TOOL, agent_name=None, session_id=None,
            user_id=current.id, scope_type=None, allowed=True,
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="failed", error_message=str(exc),
        )
        raise err(ir.IntegrationError(str(exc), code=exc.code))
    await log_execution_attempt(
        tool_name=_SIMULATE_CRED_TOOL, agent_name=None, session_id=None,
        user_id=current.id, scope_type=None, allowed=True,
        duration_ms=int((time.perf_counter() - started) * 1000),
        status="success", error_message=None,
    )
    return result


@router.post("/{intent_id}/confirm", response_model=IntentOut)
async def confirm_integration_intent(
    intent_id: str,
    req: IntentActionRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> IntentOut:
    """Approval gate: confirm an intent for FUTURE execution. Executes nothing —
    dry_run stays true and execution stays globally disabled."""
    intent = await _visible_intent(intent_id, current)
    agent_name = intent["agent_name"]
    await _govern(_CONFIRM_TOOL, agent_name, current)
    started = time.perf_counter()
    try:
        result = await ir.confirm_readiness_intent(
            parse_uuid(intent_id, "intent_id"),
            user_id=current.id, is_admin=(current.role == "admin"),
            is_owner=(intent["created_by"] == current.id), notes=req.notes,
        )
    except ir.IntegrationError as exc:
        await log_execution_attempt(
            tool_name=_CONFIRM_TOOL, agent_name=agent_name, session_id=None,
            user_id=current.id, scope_type=None, allowed=True,
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="failed", error_message=str(exc),
        )
        raise err(exc)
    await log_execution_attempt(
        tool_name=_CONFIRM_TOOL, agent_name=agent_name, session_id=None,
        user_id=current.id, scope_type=None, allowed=True,
        duration_ms=int((time.perf_counter() - started) * 1000),
        status="success", error_message=None,
    )
    await write_trace(
        session_id=None, user_id=current.id,
        trace_type="integration_intent_confirmed", status="ok",
        selected_agent=agent_name, tool_name=_CONFIRM_TOOL,
        tool_result={
            "intent_id": str(result["id"]),
            "from_status": result.get("_from_status"),
            "to_status": result.get("_to_status", result["status"]),
            "dry_run": result["dry_run"],
            "execution_enabled": ir.EXECUTION_ENABLED,
        },
        workspace_id=result.get("workspace_id"),
    )
    return intent_to_out(result)


@router.post("/{intent_id}/revoke", response_model=IntentOut)
async def revoke_integration_intent(
    intent_id: str,
    req: IntentActionRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> IntentOut:
    """Revoke a prior confirmation → confirmation_revoked. Executes nothing."""
    intent = await _visible_intent(intent_id, current)
    agent_name = intent["agent_name"]
    await _govern(_REVOKE_TOOL, agent_name, current)
    started = time.perf_counter()
    try:
        result = await ir.revoke_readiness_intent(
            parse_uuid(intent_id, "intent_id"),
            user_id=current.id, is_admin=(current.role == "admin"),
            is_owner=(intent["created_by"] == current.id), notes=req.notes,
        )
    except ir.IntegrationError as exc:
        await log_execution_attempt(
            tool_name=_REVOKE_TOOL, agent_name=agent_name, session_id=None,
            user_id=current.id, scope_type=None, allowed=True,
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="failed", error_message=str(exc),
        )
        raise err(exc)
    await log_execution_attempt(
        tool_name=_REVOKE_TOOL, agent_name=agent_name, session_id=None,
        user_id=current.id, scope_type=None, allowed=True,
        duration_ms=int((time.perf_counter() - started) * 1000),
        status="success", error_message=None,
    )
    await write_trace(
        session_id=None, user_id=current.id,
        trace_type="integration_intent_confirmation_revoked", status="ok",
        selected_agent=agent_name, tool_name=_REVOKE_TOOL,
        tool_result={
            "intent_id": str(result["id"]),
            "from_status": result.get("_from_status"),
            "to_status": result.get("_to_status", result["status"]),
        },
        workspace_id=result.get("workspace_id"),
    )
    return intent_to_out(result)


@router.patch("/{intent_id}/cancel", response_model=IntentOut)
async def cancel_integration_intent(
    intent_id: str,
    req: IntentActionRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> IntentOut:
    intent = await _visible_intent(intent_id, current)
    agent_name = intent["agent_name"]
    await _govern(_CANCEL_TOOL, agent_name, current)
    started = time.perf_counter()
    try:
        result = await ir.cancel_integration_intent(
            parse_uuid(intent_id, "intent_id"),
            user_id=current.id, is_admin=(current.role == "admin"),
            is_owner=(intent["created_by"] == current.id), notes=req.notes,
        )
    except ir.IntegrationError as exc:
        await log_execution_attempt(
            tool_name=_CANCEL_TOOL, agent_name=agent_name, session_id=None,
            user_id=current.id, scope_type=None, allowed=True,
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="failed", error_message=str(exc),
        )
        raise err(exc)
    await log_execution_attempt(
        tool_name=_CANCEL_TOOL, agent_name=agent_name, session_id=None,
        user_id=current.id, scope_type=None, allowed=True,
        duration_ms=int((time.perf_counter() - started) * 1000),
        status="success", error_message=None,
    )
    await write_trace(
        session_id=None, user_id=current.id,
        trace_type="integration_intent_cancelled", status="ok",
        selected_agent=agent_name, tool_name=_CANCEL_TOOL,
        tool_result={
            "intent_id": str(result["id"]),
            "from_status": result.get("_from_status"),
            "to_status": result.get("_to_status", result["status"]),
        },
        workspace_id=result.get("workspace_id"),
    )
    return intent_to_out(result)


@router.post("/{intent_id}/execute")
async def execute_integration_intent(
    intent_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    """Attempt to execute an intent's external action. Gated by the global
    External Execution Kill Switch (v0.8) — ALWAYS blocked in this phase: the
    attempt is audited (tool_execution_logs `external_execution_blocked`) +
    traced (runtime_traces `external_execution_blocked`) and a 403 is returned.
    No provider/OAuth call is made; dry_run is never changed."""
    intent = await _visible_intent(intent_id, current)
    block_message = (
        guard.CONFIRMED_BLOCKED_MESSAGE
        if intent["status"] == "confirmed"
        else guard.BLOCKED_MESSAGE
    )
    try:
        await guard.assert_external_execution_allowed(
            intent["action_type"], intent["provider_type"],
            user_id=current.id, workspace_id=intent.get("workspace_id"),
            intent=intent, agent_name=intent.get("agent_name"),
            block_message=block_message,
        )
    except guard.ExecutionBlocked as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        )
    # Unreachable while EXTERNAL_EXECUTION_ENABLED is false. Defensive: never
    # fall through to a real provider call in this phase.
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail=guard.BLOCKED_MESSAGE
    )
