import logging
import uuid
from datetime import datetime
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.agents.delegations import (
    get_delegation,
    list_delegations,
)
from app.auth import CurrentUser, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/delegations", tags=["delegations"])


class DelegationOut(BaseModel):
    id: str
    workspace_id: Optional[str]
    session_id: Optional[str]
    execution_plan_id: Optional[str]
    from_agent: str
    to_agent: str
    delegation_reason: Optional[str]
    status: str
    input_payload: Optional[Any]
    output_payload: Optional[Any]
    created_at: datetime
    completed_at: Optional[datetime]


def _row_to_out(row: dict) -> DelegationOut:
    return DelegationOut(
        id=str(row["id"]),
        workspace_id=str(row["workspace_id"]) if row["workspace_id"] else None,
        session_id=str(row["session_id"]) if row["session_id"] else None,
        execution_plan_id=str(row["execution_plan_id"])
        if row["execution_plan_id"]
        else None,
        from_agent=row["from_agent"],
        to_agent=row["to_agent"],
        delegation_reason=row["delegation_reason"],
        status=row["status"],
        input_payload=row["input_payload"],
        output_payload=row["output_payload"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


def _parse_uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} must be a valid UUID",
        ) from exc


@router.get(
    "",
    response_model=list[DelegationOut],
    summary="List agent delegations. Filterable by session, plan, workspace, status.",
)
async def list_delegations_endpoint(
    current: Annotated[CurrentUser, Depends(get_current_user)],
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session_id: Optional[str] = Query(default=None),
    plan_id: Optional[str] = Query(default=None),
    workspace_id: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
) -> list[DelegationOut]:
    rows = await list_delegations(
        limit=limit,
        offset=offset,
        session_id=_parse_uuid(session_id, "session_id") if session_id else None,
        execution_plan_id=_parse_uuid(plan_id, "plan_id") if plan_id else None,
        workspace_id=_parse_uuid(workspace_id, "workspace_id")
        if workspace_id
        else None,
        status_filter=status_filter,
    )
    logger.info(
        "list delegations: user_id=%s session=%s plan=%s count=%s",
        current.id,
        session_id,
        plan_id,
        len(rows),
    )
    return [_row_to_out(r) for r in rows]


@router.get(
    "/{delegation_id}",
    response_model=DelegationOut,
    summary="Get a single delegation.",
)
async def get_delegation_endpoint(
    delegation_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> DelegationOut:
    did = _parse_uuid(delegation_id, "delegation_id")
    row = await get_delegation(did)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="delegation not found"
        )
    return _row_to_out(row)
