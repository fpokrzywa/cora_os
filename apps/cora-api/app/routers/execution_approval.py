"""Human Approval Execution Console v1.4 — endpoints.

Review provider-ready integration intents and APPROVE them for future execution
or REJECT them. Approving records internal state + audit evidence ONLY — it never
calls a provider API, never sets dry_run=false, and never lifts the kill switch.
No /api prefix (codebase convention). Cancel reuses the existing
PATCH /integration-intents/{id}/cancel endpoint.
"""

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app import execution_approval as ea
from app import final_interlock as fi
from app.auth import CurrentUser, get_current_user
from app.routers.integration import parse_uuid

router = APIRouter(prefix="/execution-approvals", tags=["execution-approvals"])

_CODE_TO_STATUS = {
    "invalid": status.HTTP_400_BAD_REQUEST,
    "not_found": status.HTTP_404_NOT_FOUND,
    "forbidden": status.HTTP_403_FORBIDDEN,
    "conflict": status.HTTP_409_CONFLICT,
    "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _err(exc: ea.ApprovalError) -> HTTPException:
    return HTTPException(
        status_code=_CODE_TO_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=str(exc),
    )


class DecisionRequest(BaseModel):
    comment: Optional[str] = Field(default=None, max_length=2000)


@router.get("")
async def list_execution_approvals(
    current: Annotated[CurrentUser, Depends(get_current_user)],
    provider_type: Optional[str] = None,
    source_type: Optional[str] = None,
    approval_state: Optional[str] = Query(default=None, alias="status"),
):
    return await ea.list_for_approval(
        user_id=current.id, is_admin=(current.role == "admin"),
        provider_type=provider_type, source_type=source_type,
        approval_state=approval_state,
    )


@router.get("/{intent_id}")
async def get_execution_approval(
    intent_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    try:
        return await ea.view_intent(
            parse_uuid(intent_id, "intent_id"),
            user_id=current.id, is_admin=(current.role == "admin"),
        )
    except ea.ApprovalError as exc:
        raise _err(exc)


@router.get("/{intent_id}/events")
async def get_execution_approval_events(
    intent_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    # Visibility check via view (also confirms the intent exists + is visible).
    await get_execution_approval(intent_id, current)
    return await ea.list_events(parse_uuid(intent_id, "intent_id"))


@router.post("/{intent_id}/approve")
async def approve_execution_intent(
    intent_id: str,
    req: DecisionRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    try:
        return await ea.approve(
            parse_uuid(intent_id, "intent_id"),
            approver_id=current.id, is_admin=(current.role == "admin"),
            comment=req.comment,
        )
    except ea.ApprovalError as exc:
        raise _err(exc)


@router.post("/{intent_id}/reject")
async def reject_execution_intent(
    intent_id: str,
    req: DecisionRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    try:
        return await ea.reject(
            parse_uuid(intent_id, "intent_id"),
            approver_id=current.id, is_admin=(current.role == "admin"),
            comment=req.comment,
        )
    except ea.ApprovalError as exc:
        raise _err(exc)


@router.post("/{intent_id}/final-safety-check")
async def run_final_safety_check(
    intent_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    """Execution Runbook & Final Safety Interlock v1.5. Runs the complete safety
    checklist and returns a status — it calls NO provider API, never clears
    dry_run, and never enables execution; real_execution_allowed is always False.
    Writes final_interlock_checked + a blocked/ready-but-disabled trace and an
    external_integration_events audit row."""
    try:
        return await fi.run_final_safety_check(
            parse_uuid(intent_id, "intent_id"),
            user_id=current.id, is_admin=(current.role == "admin"),
        )
    except fi.InterlockError as exc:
        raise HTTPException(
            status_code=_CODE_TO_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
            detail=str(exc),
        )
