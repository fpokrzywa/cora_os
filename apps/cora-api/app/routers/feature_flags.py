"""Provider Execution Feature Flag Matrix v1.7 — admin endpoints.

List + manage the per-(provider,action,environment) execution control matrix.
Toggling a flag NEVER enables real execution — the global kill switch + final
interlock still gate everything, which stays disabled this phase. Admin-only
writes. No /api prefix (codebase convention).
"""

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app import feature_flags as ff
from app.auth import CurrentUser, get_current_user
from app.routers.integration import parse_uuid

router = APIRouter(prefix="/provider-feature-flags", tags=["provider-feature-flags"])

_CODE_TO_STATUS = {
    "invalid": status.HTTP_400_BAD_REQUEST,
    "not_found": status.HTTP_404_NOT_FOUND,
    "forbidden": status.HTTP_403_FORBIDDEN,
    "conflict": status.HTTP_409_CONFLICT,
    "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _err(exc: ff.FeatureFlagError) -> HTTPException:
    return HTTPException(
        status_code=_CODE_TO_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=str(exc),
    )


def _require_admin(current: CurrentUser) -> None:
    if current.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin only")


class FlagCreate(BaseModel):
    provider_name: str
    provider_type: str
    action_type: str
    environment: str = ff.DEFAULT_ENV


class FlagUpdate(BaseModel):
    enabled: Optional[bool] = None
    dry_run_only: Optional[bool] = None
    requires_human_approval: Optional[bool] = None
    requires_final_interlock: Optional[bool] = None
    requires_valid_oauth: Optional[bool] = None
    requires_scope_validation: Optional[bool] = None
    requires_connected_provider: Optional[bool] = None
    requires_payload_hash_match: Optional[bool] = None
    requires_kill_switch_clear: Optional[bool] = None


@router.get("")
async def list_feature_flags(
    current: Annotated[CurrentUser, Depends(get_current_user)],
    provider_name: Optional[str] = None,
    action_type: Optional[str] = None,
    environment: Optional[str] = None,
):
    flags = await ff.list_flags(
        provider_name=provider_name, action_type=action_type, environment=environment,
    )
    return {"flags": flags, "external_execution_enabled": False}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_feature_flag(
    body: FlagCreate,
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    _require_admin(current)
    try:
        return await ff.create_flag(
            admin_id=current.id, provider_name=body.provider_name,
            provider_type=body.provider_type, action_type=body.action_type,
            environment=body.environment,
        )
    except ff.FeatureFlagError as exc:
        raise _err(exc)


@router.patch("/{flag_id}")
async def update_feature_flag(
    flag_id: str,
    body: FlagUpdate,
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    _require_admin(current)
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        return await ff.update_flag(
            parse_uuid(flag_id, "flag_id"), admin_id=current.id, changes=changes,
        )
    except ff.FeatureFlagError as exc:
        raise _err(exc)
