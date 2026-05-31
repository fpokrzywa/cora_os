"""Real OAuth Flow v1.1 — endpoints.

Connecting a provider account is allowed; it NEVER enables execution (still gated
by the v0.8 kill switch + v0.7 approval gate). Tokens are encrypted at rest and
never returned to the client. No /api prefix — consistent with every router here.

The callback is intentionally UNAUTHENTICATED: it is a top-level browser redirect
from the provider and carries no bearer token. The initiating user is recovered
from the single-use, expiring `state` created at /start.
"""

import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status

from app import oauth_flow as flow
from app import oauth_providers as registry
from app.auth import CurrentUser, get_current_user

router = APIRouter(prefix="/oauth", tags=["oauth"])

_CODE_TO_STATUS = {
    "invalid": status.HTTP_400_BAD_REQUEST,
    "not_found": status.HTTP_404_NOT_FOUND,
    "conflict": status.HTTP_409_CONFLICT,
    "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "provider_error": status.HTTP_502_BAD_GATEWAY,
}


def _err(exc: flow.OAuthError) -> HTTPException:
    return HTTPException(
        status_code=_CODE_TO_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=str(exc),
    )


def _parse_ws(value: Optional[str]) -> Optional[uuid.UUID]:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="workspace_id must be a UUID") from exc


@router.get("/providers")
async def oauth_providers(
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    return await flow.list_provider_status(
        user_id=current.id, is_admin=(current.role == "admin")
    )


@router.get("/{provider_name}/start")
async def oauth_start(
    provider_name: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
    workspace_id: Optional[str] = None,
):
    try:
        return await flow.start_authorization(
            provider_name, user_id=current.id, workspace_id=_parse_ws(workspace_id),
            is_admin=(current.role == "admin"),
        )
    except flow.OAuthError as exc:
        raise _err(exc)


@router.get("/{provider_name}/callback")
async def oauth_callback(
    provider_name: str,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    """Unauthenticated provider redirect target. User is recovered from `state`."""
    try:
        connector = await flow.handle_callback(
            provider_name, code=code, state=state, error=error,
        )
    except flow.OAuthError as exc:
        raise _err(exc)
    return {"status": "connected", "connector": connector}


@router.post("/{provider_name}/refresh")
async def oauth_refresh(
    provider_name: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    try:
        return await flow.refresh_connection(
            provider_name, user_id=current.id, is_admin=(current.role == "admin"),
        )
    except flow.OAuthError as exc:
        raise _err(exc)


@router.get("/{provider_name}/status")
async def oauth_status(
    provider_name: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    try:
        return await flow.get_status(
            provider_name, user_id=current.id, is_admin=(current.role == "admin"),
        )
    except flow.OAuthError as exc:
        raise _err(exc)
