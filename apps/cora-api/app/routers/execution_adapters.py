"""External Provider Execution Adapter Skeleton v1.6 — endpoints.

List the execution adapters, simulate an adapter's provider-shaped payload for an
intent, and run the blocked execution dry-check (which runs the final interlock and
always returns blocked). No /api prefix (codebase convention). Nothing external is
ever called; no token is exposed.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app import execution_adapters as ea
from app.auth import CurrentUser, get_current_user
from app.routers.integration import parse_uuid

router = APIRouter(prefix="/execution-adapters", tags=["execution-adapters"])

_CODE_TO_STATUS = {
    "invalid": status.HTTP_400_BAD_REQUEST,
    "not_found": status.HTTP_404_NOT_FOUND,
    "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _err(exc: ea.AdapterError) -> HTTPException:
    return HTTPException(
        status_code=_CODE_TO_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=str(exc),
    )


@router.get("")
async def list_execution_adapters(
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    return {"adapters": ea.list_adapters(), "real_execution_enabled": False}


@router.post("/{intent_id}/simulate")
async def simulate_adapter_payload(
    intent_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    try:
        return await ea.simulate_adapter_payload(
            parse_uuid(intent_id, "intent_id"),
            user_id=current.id, is_admin=(current.role == "admin"),
        )
    except ea.AdapterError as exc:
        raise _err(exc)


@router.post("/{intent_id}/blocked-execution-check")
async def run_blocked_execution_check(
    intent_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    try:
        return await ea.run_blocked_execution_check(
            parse_uuid(intent_id, "intent_id"),
            user_id=current.id, is_admin=(current.role == "admin"),
        )
    except ea.AdapterError as exc:
        raise _err(exc)
