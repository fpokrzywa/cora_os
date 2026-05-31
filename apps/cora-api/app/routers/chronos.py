"""CHRONOS schedule-proposal endpoints (Governed Tool Planning v0.1).

Review-only proposals. There is NO calendar-write / invite path — proposals
move only through proposed -> reviewed -> approved -> archived. Any actual
calendar event creation would be a future, separately-governed capability.
Emits chronos_proposal_created/updated/archived runtime traces for auditability.
"""

import logging
import time
import uuid
from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app import chronos_tools
from app import review_workflow
from app import integration_readiness as ir
from app.review_workflow import PROPOSAL_CONFIG, ReviewError
from app.tools.governance import check_permission, fetch_tool, log_execution_attempt
from app.routers.integration import (
    IntentOut,
    intent_to_out,
    write_intent_trace,
    err as integration_err,
)
from app.auth import CurrentUser, get_current_user
from app.runtime_traces import write_trace
from app.workspaces import get_workspace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["chronos"])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ProposalCreate(BaseModel):
    proposal_type: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=300)
    description: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    timezone: Optional[str] = Field(default=None, max_length=100)
    attendees: Optional[list] = None
    agenda: Optional[list] = None
    reminders: Optional[list] = None
    metadata: Optional[dict] = None


class ProposalUpdate(BaseModel):
    proposal_type: Optional[str] = Field(default=None, max_length=50)
    title: Optional[str] = Field(default=None, max_length=300)
    description: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    timezone: Optional[str] = Field(default=None, max_length=100)
    attendees: Optional[list] = None
    agenda: Optional[list] = None
    reminders: Optional[list] = None
    status: Optional[str] = None
    metadata: Optional[dict] = None


class ProposalOut(BaseModel):
    id: str
    workspace_id: Optional[str]
    created_by: Optional[str]
    agent_name: str
    proposal_type: str
    title: str
    description: Optional[str]
    start_time: Optional[datetime]
    end_time: Optional[datetime]
    timezone: Optional[str]
    attendees: list
    agenda: list
    reminders: list
    status: str
    metadata: dict
    reviewed_by: Optional[str]
    reviewed_at: Optional[datetime]
    approved_by: Optional[str]
    approved_at: Optional[datetime]
    archived_at: Optional[datetime]
    review_notes: Optional[str]
    created_at: datetime
    updated_at: datetime


class ReviewActionRequest(BaseModel):
    notes: Optional[str] = Field(default=None, max_length=2000)


class ProposalDeleteOut(BaseModel):
    deleted: bool
    proposal_id: str


class ProposalReviewEventOut(BaseModel):
    id: str
    proposal_id: str
    user_id: Optional[str]
    action: str
    from_status: Optional[str]
    to_status: Optional[str]
    notes: Optional[str]
    created_at: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} must be a valid UUID",
        ) from exc


_CODE_TO_STATUS = {
    "invalid": status.HTTP_400_BAD_REQUEST,
    "invalid_status": status.HTTP_400_BAD_REQUEST,
    "not_found": status.HTTP_404_NOT_FOUND,
    "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _err(exc: chronos_tools.ChronosToolError) -> HTTPException:
    return HTTPException(
        status_code=_CODE_TO_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=str(exc),
    )


_REVIEW_CODE_TO_STATUS = {
    "invalid_transition": status.HTTP_409_CONFLICT,
    "forbidden": status.HTTP_403_FORBIDDEN,
    "not_found": status.HTTP_404_NOT_FOUND,
    "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _review_err(exc: ReviewError) -> HTTPException:
    return HTTPException(
        status_code=_REVIEW_CODE_TO_STATUS.get(exc.code, status.HTTP_409_CONFLICT),
        detail=str(exc),
    )


_PROPOSAL_TOOL = "chronos_create_schedule_proposal"
_DELETE_TOOL = "chronos_delete_proposal"

# Each review action is governed + audited under its own tool (v0.5). The two
# intermediate lifecycle steps (submit_for_review / request_changes) are not
# separately enumerated and fall back to the parent create tool.
_ACTION_TOOL = {
    "mark_reviewed": "chronos_review_proposal",
    "approve": "chronos_approve_proposal",
    "archive": "chronos_archive_proposal",
}

_PROPOSAL_ACTION_TRACE = {
    "submit_for_review": "chronos_proposal_submitted_for_review",
    "request_changes": "chronos_proposal_changes_requested",
    "mark_reviewed": "proposal_reviewed",
    "approve": "proposal_approved",
    "archive": "proposal_archived",
}


async def _run_review_action(
    workspace_id: str,
    proposal_id: str,
    action: str,
    notes: Optional[str],
    current: CurrentUser,
) -> ProposalOut:
    wid = await _require_workspace(workspace_id)
    await _get_in_workspace(proposal_id, wid, current)  # owner-or-admin visibility
    is_admin = current.role == "admin"
    # Governance-first: every review action is checked + logged against its own
    # governed tool (mark_reviewed/approve/archive), falling back to the create
    # tool for intermediate steps. Approval remains admin-only inside
    # perform_action. No calendar action is ever performed.
    tool_name = _ACTION_TOOL.get(action, _PROPOSAL_TOOL)
    tool = await fetch_tool(tool_name)
    if tool is not None:
        decision = await check_permission(
            tool, agent_name="CHRONOS", user_id=current.id, is_admin=is_admin
        )
        if not decision.allowed:
            await log_execution_attempt(
                tool_name=tool_name, agent_name="CHRONOS", session_id=None,
                user_id=current.id, scope_type=None, allowed=False,
                duration_ms=None, status="denied", error_message=decision.reason,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=decision.reason
            )
    started = time.perf_counter()
    try:
        result = await review_workflow.perform_action(
            PROPOSAL_CONFIG,
            _parse_uuid(proposal_id, "proposal_id"),
            action=action,
            user_id=current.id,
            is_admin=is_admin,
            notes=notes,
        )
    except ReviewError as exc:
        await log_execution_attempt(
            tool_name=tool_name, agent_name="CHRONOS", session_id=None,
            user_id=current.id, scope_type=None, allowed=True,
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="failed", error_message=str(exc),
        )
        raise _review_err(exc)
    await log_execution_attempt(
        tool_name=tool_name, agent_name="CHRONOS", session_id=None,
        user_id=current.id, scope_type=None, allowed=True,
        duration_ms=int((time.perf_counter() - started) * 1000),
        status="success", error_message=None,
    )
    await write_trace(
        session_id=None,
        user_id=current.id,
        trace_type=_PROPOSAL_ACTION_TRACE[action],
        status="ok",
        selected_agent="CHRONOS",
        tool_name=tool_name,
        tool_result={
            "proposal_id": str(result["id"]),
            "title": result["title"],
            "from_status": result["_from_status"],
            "to_status": result["_to_status"],
            "notes_present": bool(notes and notes.strip()),
        },
        workspace_id=wid,
    )
    return _to_out(result)


async def _require_workspace(workspace_id: str) -> uuid.UUID:
    wid = _parse_uuid(workspace_id, "workspace_id")
    if await get_workspace(wid) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found"
        )
    return wid


def _to_out(row: dict) -> ProposalOut:
    return ProposalOut(
        id=str(row["id"]),
        workspace_id=str(row["workspace_id"]) if row["workspace_id"] else None,
        created_by=str(row["created_by"]) if row["created_by"] else None,
        agent_name=row["agent_name"],
        proposal_type=row["proposal_type"],
        title=row["title"],
        description=row["description"],
        start_time=row["start_time"],
        end_time=row["end_time"],
        timezone=row["timezone"],
        attendees=row["attendees"] or [],
        agenda=row["agenda"] or [],
        reminders=row["reminders"] or [],
        status=row["status"],
        metadata=row["metadata"] or {},
        reviewed_by=str(row["reviewed_by"]) if row.get("reviewed_by") else None,
        reviewed_at=row.get("reviewed_at"),
        approved_by=str(row["approved_by"]) if row.get("approved_by") else None,
        approved_at=row.get("approved_at"),
        archived_at=row.get("archived_at"),
        review_notes=row.get("review_notes"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def _get_in_workspace(
    proposal_id: str, wid: uuid.UUID, current: CurrentUser
) -> dict:
    """Fetch a proposal, enforcing workspace scope and ownership. Non-admins
    may only access proposals they created; admins may access any in the
    workspace."""
    pid = _parse_uuid(proposal_id, "proposal_id")
    row = await chronos_tools.get_proposal(pid)
    if row is None or (
        row["workspace_id"] is not None and row["workspace_id"] != wid
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="proposal not found"
        )
    if current.role != "admin" and row["created_by"] != current.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="proposal not found"
        )
    return row


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/{workspace_id}/chronos/proposals",
    response_model=list[ProposalOut],
    summary="List CHRONOS schedule proposals for a workspace.",
)
async def list_chronos_proposals(
    workspace_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
    include_archived: bool = False,
) -> list[ProposalOut]:
    wid = await _require_workspace(workspace_id)
    owner_id = None if current.role == "admin" else current.id
    try:
        rows = await chronos_tools.list_proposals(
            workspace_id=wid,
            include_archived=include_archived,
            owner_id=owner_id,
        )
    except chronos_tools.ChronosToolError as exc:
        raise _err(exc)
    return [_to_out(r) for r in rows]


@router.post(
    "/{workspace_id}/chronos/proposals",
    response_model=ProposalOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a CHRONOS schedule proposal (review-only, no calendar write).",
)
async def create_chronos_proposal(
    workspace_id: str,
    req: ProposalCreate,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> ProposalOut:
    wid = await _require_workspace(workspace_id)
    try:
        row = await chronos_tools.create_schedule_proposal(
            workspace_id=wid,
            user_id=current.id,
            proposal_type=req.proposal_type,
            title=req.title,
            description=req.description,
            start_time=req.start_time,
            end_time=req.end_time,
            timezone=req.timezone,
            attendees=req.attendees,
            agenda=req.agenda,
            reminders=req.reminders,
            metadata=req.metadata,
        )
    except chronos_tools.ChronosToolError as exc:
        raise _err(exc)
    await write_trace(
        session_id=None,
        user_id=current.id,
        trace_type="chronos_proposal_created",
        status="ok",
        selected_agent="CHRONOS",
        tool_name="chronos_create_schedule_proposal",
        tool_result={
            "proposal_id": str(row["id"]),
            "proposal_type": row["proposal_type"],
        },
        workspace_id=wid,
    )
    return _to_out(row)


@router.get(
    "/{workspace_id}/chronos/proposals/{proposal_id}",
    response_model=ProposalOut,
    summary="Fetch a single CHRONOS schedule proposal.",
)
async def get_chronos_proposal(
    workspace_id: str,
    proposal_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> ProposalOut:
    wid = await _require_workspace(workspace_id)
    row = await _get_in_workspace(proposal_id, wid, current)
    return _to_out(row)


@router.patch(
    "/{workspace_id}/chronos/proposals/{proposal_id}",
    response_model=ProposalOut,
    summary="Update a CHRONOS proposal (edit fields or change review status).",
)
async def update_chronos_proposal(
    workspace_id: str,
    proposal_id: str,
    req: ProposalUpdate,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> ProposalOut:
    wid = await _require_workspace(workspace_id)
    existing = await _get_in_workspace(proposal_id, wid, current)
    fields = req.model_dump(exclude_unset=True)
    # Status transitions go through the review-workflow endpoints; PATCH is
    # content-only.
    if "status" in fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="change status via the review-workflow endpoints, not PATCH",
        )
    is_admin = current.role == "admin"
    content_keys = {
        "proposal_type", "title", "description", "start_time", "end_time",
        "timezone", "attendees", "agenda", "reminders",
    }
    editing_content = bool(content_keys & fields.keys())
    if editing_content and not is_admin and existing["status"] not in review_workflow.PROPOSAL_EDITABLE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"proposal cannot be edited in status {existing['status']!r}",
        )
    try:
        row = await chronos_tools.update_proposal(
            _parse_uuid(proposal_id, "proposal_id"), fields
        )
    except chronos_tools.ChronosToolError as exc:
        raise _err(exc)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="proposal not found"
        )
    await write_trace(
        session_id=None,
        user_id=current.id,
        trace_type="chronos_proposal_updated",
        status="ok",
        selected_agent="CHRONOS",
        tool_name="chronos_create_schedule_proposal",
        tool_result={"proposal_id": str(row["id"]), "status": row["status"]},
        workspace_id=wid,
    )
    return _to_out(row)


# ---------------------------------------------------------------------------
# Review workflow (internal only — approval never sends/schedules anything)
# ---------------------------------------------------------------------------


@router.post("/{workspace_id}/chronos/proposals/{proposal_id}/submit-review", response_model=ProposalOut)
async def submit_chronos_proposal(
    workspace_id: str, proposal_id: str, req: ReviewActionRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> ProposalOut:
    return await _run_review_action(workspace_id, proposal_id, "submit_for_review", req.notes, current)


@router.post("/{workspace_id}/chronos/proposals/{proposal_id}/request-changes", response_model=ProposalOut)
async def request_chronos_proposal_changes(
    workspace_id: str, proposal_id: str, req: ReviewActionRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> ProposalOut:
    return await _run_review_action(workspace_id, proposal_id, "request_changes", req.notes, current)


@router.post("/{workspace_id}/chronos/proposals/{proposal_id}/mark-reviewed", response_model=ProposalOut)
async def mark_chronos_proposal_reviewed(
    workspace_id: str, proposal_id: str, req: ReviewActionRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> ProposalOut:
    return await _run_review_action(workspace_id, proposal_id, "mark_reviewed", req.notes, current)


@router.post("/{workspace_id}/chronos/proposals/{proposal_id}/approve", response_model=ProposalOut)
async def approve_chronos_proposal(
    workspace_id: str, proposal_id: str, req: ReviewActionRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> ProposalOut:
    return await _run_review_action(workspace_id, proposal_id, "approve", req.notes, current)


@router.post("/{workspace_id}/chronos/proposals/{proposal_id}/archive", response_model=ProposalOut)
async def archive_chronos_proposal_action(
    workspace_id: str, proposal_id: str, req: ReviewActionRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> ProposalOut:
    return await _run_review_action(workspace_id, proposal_id, "archive", req.notes, current)


@router.get(
    "/{workspace_id}/chronos/proposals/{proposal_id}/review-events",
    response_model=list[ProposalReviewEventOut],
)
async def list_chronos_proposal_review_events(
    workspace_id: str, proposal_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> list[ProposalReviewEventOut]:
    wid = await _require_workspace(workspace_id)
    await _get_in_workspace(proposal_id, wid, current)
    rows = await review_workflow.list_events(PROPOSAL_CONFIG, _parse_uuid(proposal_id, "proposal_id"))
    return [
        ProposalReviewEventOut(
            id=str(r["id"]),
            proposal_id=str(r["proposal_id"]),
            user_id=str(r["user_id"]) if r["user_id"] else None,
            action=r["action"],
            from_status=r["from_status"],
            to_status=r["to_status"],
            notes=r["notes"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


class IntegrationIntentCreate(BaseModel):
    provider_name: str = Field(default="internal_preview", max_length=100)
    action_type: str = Field(default="create_calendar_event", max_length=50)
    notes: Optional[str] = Field(default=None, max_length=2000)


@router.post(
    "/{workspace_id}/chronos/proposals/{proposal_id}/integration-intent",
    response_model=IntentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Prepare a DRY-RUN calendar event intent from an approved proposal (creates nothing).",
)
async def prepare_chronos_calendar_intent(
    workspace_id: str,
    proposal_id: str,
    req: IntegrationIntentCreate,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> IntentOut:
    wid = await _require_workspace(workspace_id)
    await _get_in_workspace(proposal_id, wid, current)  # owner-or-admin visibility
    tool_name = "chronos_prepare_calendar_event_intent"
    started = time.perf_counter()
    try:
        intent = await ir.build_calendar_intent_from_proposal(
            _parse_uuid(proposal_id, "proposal_id"),
            user_id=current.id,
            is_admin=(current.role == "admin"),
            provider_name=req.provider_name,
            action_type=req.action_type,
            notes=req.notes,
        )
    except ir.IntegrationError as exc:
        # Insert failed → audit + trace as error; persist nothing.
        await log_execution_attempt(
            tool_name=tool_name, agent_name="CHRONOS", session_id=None,
            user_id=current.id, scope_type=None, allowed=True,
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="error", error_message=str(exc),
        )
        await write_trace(
            session_id=None, user_id=current.id,
            trace_type="integration_intent_created", status="error",
            selected_agent="CHRONOS", tool_name=tool_name,
            tool_result={"error": str(exc), "source_id": proposal_id},
            error_message=str(exc), workspace_id=wid,
        )
        raise integration_err(exc)
    # Insert succeeded + committed → audit log THEN runtime trace.
    await log_execution_attempt(
        tool_name=tool_name, agent_name="CHRONOS", session_id=None,
        user_id=current.id, scope_type=None, allowed=True,
        duration_ms=int((time.perf_counter() - started) * 1000),
        status="success", error_message=None,
    )
    await write_intent_trace(
        intent, trace_type="integration_intent_created", user_id=current.id
    )
    if intent["status"] == ir.STATUS_BLOCKED:
        await write_intent_trace(
            intent, trace_type="integration_intent_blocked", user_id=current.id
        )
    return intent_to_out(intent)


@router.delete(
    "/{workspace_id}/chronos/proposals/{proposal_id}",
    response_model=ProposalDeleteOut,
    summary="Permanently delete a CHRONOS proposal record (removes the proposal only; creates/cancels nothing externally).",
)
async def delete_chronos_proposal(
    workspace_id: str,
    proposal_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> ProposalDeleteOut:
    # Permanent delete. Governance-first via chronos_delete_proposal; audited in
    # tool_execution_logs + a proposal_deleted runtime trace. No calendar action.
    # (Archive remains available via the POST .../archive endpoint.)
    wid = await _require_workspace(workspace_id)
    row = await _get_in_workspace(proposal_id, wid, current)  # exists + ws + owner; 404 otherwise
    is_admin = current.role == "admin"
    tool = await fetch_tool(_DELETE_TOOL)
    if tool is not None:
        decision = await check_permission(
            tool, agent_name="CHRONOS", user_id=current.id, is_admin=is_admin
        )
        if not decision.allowed:
            await log_execution_attempt(
                tool_name=_DELETE_TOOL, agent_name="CHRONOS", session_id=None,
                user_id=current.id, scope_type=None, allowed=False,
                duration_ms=None, status="denied", error_message=decision.reason,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=decision.reason
            )
    pid = _parse_uuid(proposal_id, "proposal_id")
    started = time.perf_counter()
    try:
        ok = await chronos_tools.delete_proposal(pid)
    except chronos_tools.ChronosToolError as exc:
        await log_execution_attempt(
            tool_name=_DELETE_TOOL, agent_name="CHRONOS", session_id=None,
            user_id=current.id, scope_type=None, allowed=True,
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="failed", error_message=str(exc),
        )
        raise _err(exc)
    duration_ms = int((time.perf_counter() - started) * 1000)
    if not ok:
        await log_execution_attempt(
            tool_name=_DELETE_TOOL, agent_name="CHRONOS", session_id=None,
            user_id=current.id, scope_type=None, allowed=True,
            duration_ms=duration_ms, status="failed", error_message="proposal not found",
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="proposal not found"
        )
    await log_execution_attempt(
        tool_name=_DELETE_TOOL, agent_name="CHRONOS", session_id=None,
        user_id=current.id, scope_type=None, allowed=True,
        duration_ms=duration_ms, status="success", error_message=None,
    )
    await write_trace(
        session_id=None,
        user_id=current.id,
        trace_type="proposal_deleted",
        status="ok",
        selected_agent="CHRONOS",
        tool_name=_DELETE_TOOL,
        tool_result={
            "proposal_id": str(pid),
            "title": row["title"],
            "prior_status": row["status"],
        },
        workspace_id=wid,
    )
    return ProposalDeleteOut(deleted=True, proposal_id=str(pid))
