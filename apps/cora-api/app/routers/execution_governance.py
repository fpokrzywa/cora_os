"""Execution Governance Dashboard v1.8 — endpoint.

Read-only observability surface over the execution-governance state. No mutation,
no provider API call, no secrets in the response. No /api prefix (codebase
convention; spec's /api/execution-governance/dashboard maps here).
"""

import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status

from app import execution_governance_dashboard as dash
from app.auth import CurrentUser, get_current_user

router = APIRouter(prefix="/execution-governance", tags=["execution-governance"])


def _opt_uuid(value: Optional[str], field: str) -> Optional[uuid.UUID]:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{field} must be a UUID") from exc


@router.get("/dashboard")
async def execution_governance_dashboard(
    current: Annotated[CurrentUser, Depends(get_current_user)],
    provider_name: Optional[str] = None,
    action_type: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    workspace_id: Optional[str] = None,
    user_id: Optional[str] = None,
):
    is_admin = current.role == "admin"
    try:
        return await dash.build_dashboard(
            user_id=current.id, is_admin=is_admin,
            provider_name=provider_name, action_type=action_type, status=status,
            date_from=date_from, date_to=date_to,
            workspace_id=_opt_uuid(workspace_id, "workspace_id"),
            # user_id filter is admin-only (ignored for non-admins).
            target_user_id=_opt_uuid(user_id, "user_id") if is_admin else None,
        )
    except dash.DashboardError as exc:
        raise HTTPException(
            status_code=503 if exc.code == "unavailable" else 400,
            detail=str(exc),
        )
