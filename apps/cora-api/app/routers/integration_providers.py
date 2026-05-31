"""External provider connector endpoints (v0.5) — registry read + admin update.

Lists/updates the external_provider_connectors registry. SAFETY: this module
cannot enable live execution — supports_send / supports_calendar_* / supports_read
are forced FALSE and dry_run_only forced TRUE on every update (enforced in
provider_connectors.update_connector). No connector performs an external action.
"""

import logging
import uuid
from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app import provider_connectors as pc
from app.auth import CurrentUser, get_current_user, require_admin
from app.runtime_traces import write_trace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integration", tags=["integration-providers"])

SAFETY_NOTE = (
    "Provider connectors are in dry-run design mode. Cora will not send email, "
    "create calendar events, or contact external provider APIs."
)


class ProviderOut(BaseModel):
    id: str
    provider_name: str
    provider_type: str
    display_name: str
    description: Optional[str]
    enabled: bool
    dry_run_only: bool
    supports_send: bool
    supports_draft: bool
    supports_calendar_create: bool
    supports_calendar_update: bool
    supports_read: bool
    requires_oauth: bool
    auth_config_schema: dict
    payload_schema: dict
    capabilities: dict
    metadata: dict
    created_at: datetime
    updated_at: datetime
    safety_note: str = SAFETY_NOTE


class ProviderUpdateRequest(BaseModel):
    enabled: Optional[bool] = None
    dry_run_only: Optional[bool] = None  # accepted but always coerced TRUE
    display_name: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = None
    metadata: Optional[dict] = None
    capabilities: Optional[dict] = None
    # Live-execution flags are intentionally NOT updatable here. If supplied,
    # they are ignored and reported back as blocked.
    supports_send: Optional[bool] = None
    supports_calendar_create: Optional[bool] = None
    supports_calendar_update: Optional[bool] = None
    supports_read: Optional[bool] = None


_CODE_TO_STATUS = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "invalid": status.HTTP_400_BAD_REQUEST,
    "disabled": status.HTTP_409_CONFLICT,
    "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _err(exc: pc.ProviderError) -> HTTPException:
    return HTTPException(
        status_code=_CODE_TO_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=str(exc),
    )


def _to_out(row: dict) -> ProviderOut:
    return ProviderOut(
        id=str(row["id"]),
        provider_name=row["provider_name"],
        provider_type=row["provider_type"],
        display_name=row["display_name"],
        description=row.get("description"),
        enabled=row["enabled"],
        dry_run_only=row["dry_run_only"],
        supports_send=row["supports_send"],
        supports_draft=row["supports_draft"],
        supports_calendar_create=row["supports_calendar_create"],
        supports_calendar_update=row["supports_calendar_update"],
        supports_read=row["supports_read"],
        requires_oauth=row["requires_oauth"],
        auth_config_schema=row.get("auth_config_schema") or {},
        payload_schema=row.get("payload_schema") or {},
        capabilities=row.get("capabilities") or {},
        metadata=row.get("metadata") or {},
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.get("/providers", response_model=list[ProviderOut])
async def list_providers(
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> list[ProviderOut]:
    try:
        rows = await pc.list_available_connectors()
    except pc.ProviderError as exc:
        raise _err(exc)
    await write_trace(
        session_id=None,
        user_id=current.id,
        trace_type="provider_connector_listed",
        status="ok",
        tool_result={"count": len(rows), "external_action_performed": False},
    )
    return [_to_out(r) for r in rows]


@router.get("/providers/{provider_name}", response_model=ProviderOut)
async def get_provider(
    provider_name: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> ProviderOut:
    row = await pc.get_connector_row(provider_name)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="provider not found"
        )
    return _to_out(row)


@router.patch("/providers/{provider_name}", response_model=ProviderOut)
async def update_provider(
    provider_name: str,
    req: ProviderUpdateRequest,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> ProviderOut:
    existing = await pc.get_connector_row(provider_name)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="provider not found"
        )
    # Detect (and refuse) any attempt to turn on live-execution capabilities.
    blocked = [
        f for f in (
            "supports_send", "supports_calendar_create",
            "supports_calendar_update", "supports_read",
        )
        if getattr(req, f) is True
    ]
    fields = req.model_dump(exclude_unset=True)
    try:
        row = await pc.update_connector(provider_name, fields)
    except pc.ProviderError as exc:
        raise _err(exc)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="provider not found"
        )
    await write_trace(
        session_id=None,
        user_id=admin.id,
        trace_type="provider_connector_updated",
        status="ok",
        tool_result={
            "provider_name": provider_name,
            "provider_type": row["provider_type"],
            "enabled": row["enabled"],
            "dry_run_only": row["dry_run_only"],
            "blocked_live_flags": blocked,
            "external_action_performed": False,
        },
    )
    out = _to_out(row)
    if blocked:
        # Make the refusal explicit in the response without failing the request.
        out.metadata = {
            **out.metadata,
            "_blocked_live_flags": blocked,
            "_note": "Live-execution capabilities cannot be enabled in v0.5; "
                     "they remain false.",
        }
    return out
