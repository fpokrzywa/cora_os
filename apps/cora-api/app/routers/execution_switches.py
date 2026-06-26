"""Execution kill-switch admin endpoints.

List + toggle the runtime override for the execution master switches. Admin-only.
Only switches marked manageable can be changed (calendar / screen vision); the global
external_execution switch is env-locked and returned read-only. Toggling a switch lifts
ONE gate only — per-provider feature flags + scopes + confirm-before-write still apply.
No /api prefix (codebase convention).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app import runtime_switches as rs
from app.auth import CurrentUser, get_current_user

router = APIRouter(prefix="/admin/execution-switches", tags=["execution-switches"])

_CODE_TO_STATUS = {
    "invalid": status.HTTP_400_BAD_REQUEST,
    "not_found": status.HTTP_404_NOT_FOUND,
    "forbidden": status.HTTP_403_FORBIDDEN,
    "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _err(exc: rs.SwitchError) -> HTTPException:
    return HTTPException(
        status_code=_CODE_TO_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=str(exc),
    )


def _require_admin(current: CurrentUser) -> None:
    if current.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin only")


class SwitchUpdate(BaseModel):
    enabled: bool


@router.get("")
async def list_switches(current: Annotated[CurrentUser, Depends(get_current_user)]):
    _require_admin(current)
    return {"switches": await rs.get_all()}


@router.patch("/{name}")
async def update_switch(
    name: str,
    body: SwitchUpdate,
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    _require_admin(current)
    try:
        return await rs.set_switch(name, body.enabled, admin_id=current.id)
    except rs.SwitchError as exc:
        raise _err(exc)


@router.delete("/{name}")
async def clear_switch(
    name: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    """Remove the override so the switch reverts to its env default."""
    _require_admin(current)
    try:
        return await rs.clear_override(name, admin_id=current.id)
    except rs.SwitchError as exc:
        raise _err(exc)
