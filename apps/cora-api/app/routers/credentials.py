"""OAuth credential vault endpoints (v0.6) — READINESS / DRY-RUN ONLY.

CRUD + lifecycle for external_provider_credentials. No OAuth redirect, no token
exchange, no provider API call exists here. Responses never expose any
encrypted_* secret column (the service masks them). dry_run_only stays TRUE.
"""

import logging
import uuid
from datetime import datetime
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app import credential_vault as cv
from app.auth import CurrentUser, get_current_user
from app.runtime_traces import write_trace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integration", tags=["credentials"])

_CODE_TO_STATUS = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "invalid": status.HTTP_400_BAD_REQUEST,
    "forbidden": status.HTTP_403_FORBIDDEN,
    "conflict": status.HTTP_409_CONFLICT,
    "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _err(exc: cv.CredentialError) -> HTTPException:
    return HTTPException(
        status_code=_CODE_TO_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=str(exc),
    )


class CredentialCreateRequest(BaseModel):
    provider_name: str
    provider_type: str
    credential_name: str = Field(min_length=1, max_length=200)
    auth_type: str = "oauth2"
    scopes: list[str] = Field(default_factory=list)
    client_id_hint: Optional[str] = Field(default=None, max_length=300)
    workspace_id: Optional[str] = None
    user_id: Optional[str] = None
    dry_run_only: bool = True  # accepted but always coerced TRUE
    metadata: dict = Field(default_factory=dict)


class CredentialUpdateRequest(BaseModel):
    credential_name: Optional[str] = Field(default=None, max_length=200)
    scopes: Optional[list[str]] = None
    client_id_hint: Optional[str] = Field(default=None, max_length=300)
    dry_run_only: Optional[bool] = None  # accepted but always coerced TRUE
    metadata: Optional[dict] = None


class CredentialOut(BaseModel):
    id: str
    workspace_id: Optional[str]
    user_id: Optional[str]
    provider_name: str
    provider_type: str
    credential_name: str
    auth_type: str
    status: str
    scopes: list[Any]
    client_id_hint: Optional[str]
    token_expires_at: Optional[datetime]
    last_authorized_at: Optional[datetime]
    last_validated_at: Optional[datetime]
    last_error: Optional[str]
    dry_run_only: bool
    metadata: dict
    has_access_token: bool
    has_refresh_token: bool
    has_client_secret: bool
    created_by: Optional[str]
    updated_by: Optional[str]
    created_at: datetime
    updated_at: datetime
    safety_note: str = cv.SAFETY_NOTE
    # populated only by the validate-placeholder endpoint
    validation: Optional[dict] = None


class CredentialEventOut(BaseModel):
    id: str
    credential_id: str
    user_id: Optional[str]
    event_type: str
    from_status: Optional[str]
    to_status: Optional[str]
    notes: Optional[str]
    metadata: dict
    created_at: datetime


def _to_out(row: dict) -> CredentialOut:
    def _s(v: Any) -> Optional[str]:
        return str(v) if v is not None else None

    return CredentialOut(
        id=str(row["id"]),
        workspace_id=_s(row.get("workspace_id")),
        user_id=_s(row.get("user_id")),
        provider_name=row["provider_name"],
        provider_type=row["provider_type"],
        credential_name=row["credential_name"],
        auth_type=row["auth_type"],
        status=row["status"],
        scopes=list(row.get("scopes") or []),
        client_id_hint=row.get("client_id_hint"),
        token_expires_at=row.get("token_expires_at"),
        last_authorized_at=row.get("last_authorized_at"),
        last_validated_at=row.get("last_validated_at"),
        last_error=row.get("last_error"),
        dry_run_only=row["dry_run_only"],
        metadata=row.get("metadata") or {},
        has_access_token=bool(row.get("has_access_token")),
        has_refresh_token=bool(row.get("has_refresh_token")),
        has_client_secret=bool(row.get("has_client_secret")),
        created_by=_s(row.get("created_by")),
        updated_by=_s(row.get("updated_by")),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        validation=row.get("_validation"),
    )


def _event_to_out(row: dict) -> CredentialEventOut:
    return CredentialEventOut(
        id=str(row["id"]),
        credential_id=str(row["credential_id"]),
        user_id=str(row["user_id"]) if row.get("user_id") else None,
        event_type=row["event_type"],
        from_status=row.get("from_status"),
        to_status=row.get("to_status"),
        notes=row.get("notes"),
        metadata=row.get("metadata") or {},
        created_at=row["created_at"],
    )


async def _trace(trace_type: str, current: CurrentUser, row: dict) -> None:
    """Write a credential lifecycle trace with the required identifying fields.
    Never includes secret material (row is already masked)."""
    ws = row.get("workspace_id")
    await write_trace(
        session_id=None,
        user_id=current.id,
        trace_type=trace_type,
        status="ok",
        workspace_id=uuid.UUID(str(ws)) if ws else None,
        tool_result={
            "credential_id": str(row["id"]),
            "provider_name": row["provider_name"],
            "provider_type": row["provider_type"],
            "status": row["status"],
            "dry_run_only": row["dry_run_only"],
            "external_action_performed": False,
        },
    )


def _parse_uuid(value: Optional[str], label: str) -> Optional[uuid.UUID]:
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid {label}",
        )


@router.get("/credentials", response_model=list[CredentialOut])
async def list_credentials(
    current: Annotated[CurrentUser, Depends(get_current_user)],
    workspace_id: Optional[str] = Query(default=None),
    provider_name: Optional[str] = Query(default=None),
) -> list[CredentialOut]:
    try:
        rows = await cv.list_credentials(
            user_id=current.id,
            is_admin=(current.role == "admin"),
            workspace_id=_parse_uuid(workspace_id, "workspace_id"),
            provider_name=provider_name,
        )
    except cv.CredentialError as exc:
        raise _err(exc)
    return [_to_out(r) for r in rows]


@router.post("/credentials", response_model=CredentialOut, status_code=201)
async def create_credential(
    req: CredentialCreateRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> CredentialOut:
    try:
        row = await cv.create_credential_record(
            actor_id=current.id,
            is_admin=(current.role == "admin"),
            provider_name=req.provider_name,
            provider_type=req.provider_type,
            credential_name=req.credential_name,
            auth_type=req.auth_type,
            scopes=req.scopes,
            client_id_hint=req.client_id_hint,
            workspace_id=_parse_uuid(req.workspace_id, "workspace_id"),
            user_id=_parse_uuid(req.user_id, "user_id"),
            dry_run_only=True,
            metadata=req.metadata,
        )
    except cv.CredentialError as exc:
        raise _err(exc)
    await _trace("credential_record_created", current, row)
    return _to_out(row)


@router.get("/credentials/{credential_id}", response_model=CredentialOut)
async def get_credential(
    credential_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> CredentialOut:
    cid = _parse_uuid(credential_id, "credential_id")
    try:
        row = await cv.get_credential(
            cid, user_id=current.id, is_admin=(current.role == "admin")
        )
    except cv.CredentialError as exc:
        raise _err(exc)
    return _to_out(row)


@router.patch("/credentials/{credential_id}", response_model=CredentialOut)
async def update_credential(
    credential_id: str,
    req: CredentialUpdateRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> CredentialOut:
    cid = _parse_uuid(credential_id, "credential_id")
    fields = req.model_dump(exclude_unset=True)
    try:
        row = await cv.update_credential_record(
            cid,
            actor_id=current.id,
            is_admin=(current.role == "admin"),
            fields=fields,
        )
    except cv.CredentialError as exc:
        raise _err(exc)
    await _trace("credential_record_updated", current, row)
    return _to_out(row)


@router.post("/credentials/{credential_id}/disable", response_model=CredentialOut)
async def disable_credential(
    credential_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> CredentialOut:
    cid = _parse_uuid(credential_id, "credential_id")
    try:
        row = await cv.disable_credential(
            cid, actor_id=current.id, is_admin=(current.role == "admin")
        )
    except cv.CredentialError as exc:
        raise _err(exc)
    await _trace("credential_record_disabled", current, row)
    return _to_out(row)


@router.post(
    "/credentials/{credential_id}/mark-needs-authorization",
    response_model=CredentialOut,
)
async def mark_needs_authorization(
    credential_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> CredentialOut:
    cid = _parse_uuid(credential_id, "credential_id")
    try:
        row = await cv.mark_credential_needs_authorization(
            cid, actor_id=current.id, is_admin=(current.role == "admin")
        )
    except cv.CredentialError as exc:
        raise _err(exc)
    await _trace("credential_marked_needs_authorization", current, row)
    return _to_out(row)


@router.post(
    "/credentials/{credential_id}/validate-placeholder",
    response_model=CredentialOut,
)
async def validate_placeholder(
    credential_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> CredentialOut:
    cid = _parse_uuid(credential_id, "credential_id")
    try:
        row = await cv.validate_credential_placeholder(
            cid, actor_id=current.id, is_admin=(current.role == "admin")
        )
    except cv.CredentialError as exc:
        raise _err(exc)
    await _trace("credential_placeholder_validated", current, row)
    return _to_out(row)


@router.post(
    "/credentials/{credential_id}/rotate-placeholder",
    response_model=CredentialOut,
)
async def rotate_placeholder(
    credential_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> CredentialOut:
    cid = _parse_uuid(credential_id, "credential_id")
    try:
        row = await cv.rotate_credential_placeholder(
            cid, actor_id=current.id, is_admin=(current.role == "admin")
        )
    except cv.CredentialError as exc:
        raise _err(exc)
    await _trace("credential_placeholder_rotated", current, row)
    return _to_out(row)


@router.get(
    "/credentials/{credential_id}/events",
    response_model=list[CredentialEventOut],
)
async def list_events(
    credential_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> list[CredentialEventOut]:
    cid = _parse_uuid(credential_id, "credential_id")
    try:
        rows = await cv.list_credential_events(
            cid, actor_id=current.id, is_admin=(current.role == "admin")
        )
    except cv.CredentialError as exc:
        raise _err(exc)
    return [_event_to_out(r) for r in rows]
