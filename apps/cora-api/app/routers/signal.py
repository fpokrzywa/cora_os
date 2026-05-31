"""SIGNAL communication-draft endpoints (Governed Tool Planning v0.1).

Review-only drafts. There is NO Send/Email path — drafts move only through
draft -> reviewed -> approved -> archived. Any external send would be a future,
separately-governed capability. Emits signal_draft_created/updated/archived
runtime traces for auditability.
"""

import logging
import time
import uuid
from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app import signal_tools
from app import review_workflow
from app import integration_readiness as ir
from app.review_workflow import DRAFT_CONFIG, ReviewError
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

router = APIRouter(prefix="/workspaces", tags=["signal"])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class DraftCreate(BaseModel):
    draft_type: str = Field(min_length=1, max_length=50)
    body: str = Field(min_length=1)
    title: Optional[str] = Field(default=None, max_length=300)
    subject: Optional[str] = Field(default=None, max_length=300)
    recipient_hint: Optional[str] = Field(default=None, max_length=300)
    tone: Optional[str] = Field(default=None, max_length=50)
    metadata: Optional[dict] = None


class DraftUpdate(BaseModel):
    draft_type: Optional[str] = Field(default=None, max_length=50)
    title: Optional[str] = Field(default=None, max_length=300)
    subject: Optional[str] = Field(default=None, max_length=300)
    recipient_hint: Optional[str] = Field(default=None, max_length=300)
    body: Optional[str] = None
    tone: Optional[str] = Field(default=None, max_length=50)
    status: Optional[str] = None
    metadata: Optional[dict] = None


class DraftOut(BaseModel):
    id: str
    workspace_id: Optional[str]
    created_by: Optional[str]
    agent_name: str
    draft_type: str
    title: Optional[str]
    recipient_hint: Optional[str]
    subject: Optional[str]
    body: str
    tone: Optional[str]
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


class DraftReviewEventOut(BaseModel):
    id: str
    draft_id: str
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


def _err(exc: signal_tools.SignalToolError) -> HTTPException:
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


# Trace type per workflow action.
_DRAFT_TOOL = "signal_create_draft"
_DELETE_TOOL = "signal_delete_draft"

# Each review action is governed + audited under its own tool, mirroring CHRONOS.
# The two intermediate lifecycle steps (submit_for_review / request_changes) are
# not separately enumerated and fall back to the parent create tool.
_ACTION_TOOL = {
    "mark_reviewed": "signal_review_draft",
    "approve": "signal_approve_draft",
    "archive": "signal_archive_draft",
}

_DRAFT_ACTION_TRACE = {
    "submit_for_review": "signal_draft_submitted_for_review",
    "request_changes": "signal_draft_changes_requested",
    "mark_reviewed": "draft_reviewed",
    "approve": "draft_approved",
    "archive": "draft_archived",
}


async def _run_review_action(
    workspace_id: str,
    draft_id: str,
    action: str,
    notes: Optional[str],
    current: CurrentUser,
) -> DraftOut:
    wid = await _require_workspace(workspace_id)
    await _get_in_workspace(draft_id, wid, current)  # owner-or-admin visibility
    is_admin = current.role == "admin"
    # Governance-first: every review action is checked + logged against its own
    # governed tool (review/approve/archive), falling back to the create tool for
    # intermediate steps. Approval remains admin-only inside perform_action. No
    # external action is ever performed.
    tool_name = _ACTION_TOOL.get(action, _DRAFT_TOOL)
    tool = await fetch_tool(tool_name)
    if tool is not None:
        decision = await check_permission(
            tool, agent_name="SIGNAL", user_id=current.id, is_admin=is_admin
        )
        if not decision.allowed:
            await log_execution_attempt(
                tool_name=tool_name, agent_name="SIGNAL", session_id=None,
                user_id=current.id, scope_type=None, allowed=False,
                duration_ms=None, status="denied", error_message=decision.reason,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=decision.reason
            )
    started = time.perf_counter()
    try:
        result = await review_workflow.perform_action(
            DRAFT_CONFIG,
            _parse_uuid(draft_id, "draft_id"),
            action=action,
            user_id=current.id,
            is_admin=is_admin,
            notes=notes,
        )
    except ReviewError as exc:
        await log_execution_attempt(
            tool_name=tool_name, agent_name="SIGNAL", session_id=None,
            user_id=current.id, scope_type=None, allowed=True,
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="failed", error_message=str(exc),
        )
        raise _review_err(exc)
    await log_execution_attempt(
        tool_name=tool_name, agent_name="SIGNAL", session_id=None,
        user_id=current.id, scope_type=None, allowed=True,
        duration_ms=int((time.perf_counter() - started) * 1000),
        status="success", error_message=None,
    )
    await write_trace(
        session_id=None,
        user_id=current.id,
        trace_type=_DRAFT_ACTION_TRACE[action],
        status="ok",
        selected_agent="SIGNAL",
        tool_name=tool_name,
        tool_result={
            "draft_id": str(result["id"]),
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


def _to_out(row: dict) -> DraftOut:
    return DraftOut(
        id=str(row["id"]),
        workspace_id=str(row["workspace_id"]) if row["workspace_id"] else None,
        created_by=str(row["created_by"]) if row["created_by"] else None,
        agent_name=row["agent_name"],
        draft_type=row["draft_type"],
        title=row["title"],
        recipient_hint=row["recipient_hint"],
        subject=row["subject"],
        body=row["body"],
        tone=row["tone"],
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
    draft_id: str, wid: uuid.UUID, current: CurrentUser
) -> dict:
    """Fetch a draft, enforcing workspace scope and ownership. Non-admins may
    only access drafts they created; admins may access any in the workspace."""
    did = _parse_uuid(draft_id, "draft_id")
    row = await signal_tools.get_draft(did)
    if row is None or (
        row["workspace_id"] is not None and row["workspace_id"] != wid
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="draft not found"
        )
    if current.role != "admin" and row["created_by"] != current.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="draft not found"
        )
    return row


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/{workspace_id}/signal/drafts",
    response_model=list[DraftOut],
    summary="List SIGNAL communication drafts for a workspace.",
)
async def list_signal_drafts(
    workspace_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
    include_archived: bool = False,
) -> list[DraftOut]:
    wid = await _require_workspace(workspace_id)
    owner_id = None if current.role == "admin" else current.id
    try:
        rows = await signal_tools.list_drafts(
            workspace_id=wid,
            include_archived=include_archived,
            owner_id=owner_id,
        )
    except signal_tools.SignalToolError as exc:
        raise _err(exc)
    return [_to_out(r) for r in rows]


@router.post(
    "/{workspace_id}/signal/drafts",
    response_model=DraftOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a SIGNAL communication draft (review-only, never sent).",
)
async def create_signal_draft(
    workspace_id: str,
    req: DraftCreate,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> DraftOut:
    wid = await _require_workspace(workspace_id)
    try:
        row = await signal_tools.create_communication_draft(
            workspace_id=wid,
            user_id=current.id,
            draft_type=req.draft_type,
            title=req.title,
            subject=req.subject,
            body=req.body,
            recipient_hint=req.recipient_hint,
            tone=req.tone,
            metadata=req.metadata,
        )
    except signal_tools.SignalToolError as exc:
        raise _err(exc)
    await write_trace(
        session_id=None,
        user_id=current.id,
        trace_type="signal_draft_created",
        status="ok",
        selected_agent="SIGNAL",
        tool_name="signal_create_draft",
        tool_result={"draft_id": str(row["id"]), "draft_type": row["draft_type"]},
        workspace_id=wid,
    )
    return _to_out(row)


@router.get(
    "/{workspace_id}/signal/drafts/{draft_id}",
    response_model=DraftOut,
    summary="Fetch a single SIGNAL draft.",
)
async def get_signal_draft(
    workspace_id: str,
    draft_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> DraftOut:
    wid = await _require_workspace(workspace_id)
    row = await _get_in_workspace(draft_id, wid, current)
    return _to_out(row)


@router.patch(
    "/{workspace_id}/signal/drafts/{draft_id}",
    response_model=DraftOut,
    summary="Update a SIGNAL draft (edit fields or change review status).",
)
async def update_signal_draft(
    workspace_id: str,
    draft_id: str,
    req: DraftUpdate,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> DraftOut:
    wid = await _require_workspace(workspace_id)
    existing = await _get_in_workspace(draft_id, wid, current)
    fields = req.model_dump(exclude_unset=True)
    # Status transitions go through the dedicated review-workflow endpoints so
    # every change is governed + logged; PATCH is content-only.
    if "status" in fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="change status via the review-workflow endpoints, not PATCH",
        )
    # Edit restrictions (point 7): non-admins may only edit content while the
    # draft is editable (draft / changes_requested). Admins may edit any.
    is_admin = current.role == "admin"
    content_keys = {"draft_type", "title", "recipient_hint", "subject", "body", "tone"}
    editing_content = bool(content_keys & fields.keys())
    if editing_content and not is_admin and existing["status"] not in review_workflow.DRAFT_EDITABLE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"draft cannot be edited in status {existing['status']!r}",
        )
    try:
        row = await signal_tools.update_draft(
            _parse_uuid(draft_id, "draft_id"), fields
        )
    except signal_tools.SignalToolError as exc:
        raise _err(exc)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="draft not found"
        )
    await write_trace(
        session_id=None,
        user_id=current.id,
        trace_type="signal_draft_updated",
        status="ok",
        selected_agent="SIGNAL",
        tool_name="signal_create_draft",
        tool_result={"draft_id": str(row["id"]), "status": row["status"]},
        workspace_id=wid,
    )
    return _to_out(row)


# ---------------------------------------------------------------------------
# Review workflow (internal only — approval never sends/schedules anything)
# ---------------------------------------------------------------------------


@router.post("/{workspace_id}/signal/drafts/{draft_id}/submit-review", response_model=DraftOut)
async def submit_signal_draft(
    workspace_id: str, draft_id: str, req: ReviewActionRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> DraftOut:
    return await _run_review_action(workspace_id, draft_id, "submit_for_review", req.notes, current)


@router.post("/{workspace_id}/signal/drafts/{draft_id}/request-changes", response_model=DraftOut)
async def request_signal_draft_changes(
    workspace_id: str, draft_id: str, req: ReviewActionRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> DraftOut:
    return await _run_review_action(workspace_id, draft_id, "request_changes", req.notes, current)


@router.post("/{workspace_id}/signal/drafts/{draft_id}/mark-reviewed", response_model=DraftOut)
async def mark_signal_draft_reviewed(
    workspace_id: str, draft_id: str, req: ReviewActionRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> DraftOut:
    return await _run_review_action(workspace_id, draft_id, "mark_reviewed", req.notes, current)


@router.post("/{workspace_id}/signal/drafts/{draft_id}/approve", response_model=DraftOut)
async def approve_signal_draft(
    workspace_id: str, draft_id: str, req: ReviewActionRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> DraftOut:
    return await _run_review_action(workspace_id, draft_id, "approve", req.notes, current)


@router.post("/{workspace_id}/signal/drafts/{draft_id}/archive", response_model=DraftOut)
async def archive_signal_draft_action(
    workspace_id: str, draft_id: str, req: ReviewActionRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> DraftOut:
    return await _run_review_action(workspace_id, draft_id, "archive", req.notes, current)


@router.get(
    "/{workspace_id}/signal/drafts/{draft_id}/review-events",
    response_model=list[DraftReviewEventOut],
)
async def list_signal_draft_review_events(
    workspace_id: str, draft_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> list[DraftReviewEventOut]:
    wid = await _require_workspace(workspace_id)
    await _get_in_workspace(draft_id, wid, current)
    rows = await review_workflow.list_events(DRAFT_CONFIG, _parse_uuid(draft_id, "draft_id"))
    return [
        DraftReviewEventOut(
            id=str(r["id"]),
            draft_id=str(r["draft_id"]),
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
    action_type: str = Field(default="send_email", max_length=50)
    notes: Optional[str] = Field(default=None, max_length=2000)


@router.post(
    "/{workspace_id}/signal/drafts/{draft_id}/integration-intent",
    response_model=IntentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Prepare a DRY-RUN email send intent from an approved draft (sends nothing).",
)
async def prepare_signal_email_intent(
    workspace_id: str,
    draft_id: str,
    req: IntegrationIntentCreate,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> IntentOut:
    wid = await _require_workspace(workspace_id)
    await _get_in_workspace(draft_id, wid, current)  # owner-or-admin visibility
    tool_name = "signal_prepare_email_send_intent"
    started = time.perf_counter()
    try:
        intent = await ir.build_email_intent_from_draft(
            _parse_uuid(draft_id, "draft_id"),
            user_id=current.id,
            is_admin=(current.role == "admin"),
            provider_name=req.provider_name,
            action_type=req.action_type,
            notes=req.notes,
        )
    except ir.IntegrationError as exc:
        # Insert failed → audit + trace as error; persist nothing.
        await log_execution_attempt(
            tool_name=tool_name, agent_name="SIGNAL", session_id=None,
            user_id=current.id, scope_type=None, allowed=True,
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="error", error_message=str(exc),
        )
        await write_trace(
            session_id=None, user_id=current.id,
            trace_type="integration_intent_created", status="error",
            selected_agent="SIGNAL", tool_name=tool_name,
            tool_result={"error": str(exc), "source_id": draft_id},
            error_message=str(exc), workspace_id=wid,
        )
        raise integration_err(exc)
    # Insert succeeded + committed → audit log THEN runtime trace.
    await log_execution_attempt(
        tool_name=tool_name, agent_name="SIGNAL", session_id=None,
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


class DraftDeleteOut(BaseModel):
    deleted: bool
    draft_id: str


@router.delete(
    "/{workspace_id}/signal/drafts/{draft_id}",
    response_model=DraftDeleteOut,
    summary="Permanently delete a SIGNAL draft record (removes the draft only; sends nothing).",
)
async def delete_signal_draft(
    workspace_id: str,
    draft_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> DraftDeleteOut:
    # Permanent delete. _get_in_workspace enforces the draft exists AND the caller
    # has workspace access (non-admins: own drafts only) — 404 otherwise. Governed
    # by the signal_delete_draft tool and audited (tool_execution_logs +
    # draft_deleted runtime trace). No external action is ever performed.
    wid = await _require_workspace(workspace_id)
    row = await _get_in_workspace(draft_id, wid, current)
    is_admin = current.role == "admin"
    tool = await fetch_tool(_DELETE_TOOL)
    if tool is not None:
        decision = await check_permission(
            tool, agent_name="SIGNAL", user_id=current.id, is_admin=is_admin
        )
        if not decision.allowed:
            await log_execution_attempt(
                tool_name=_DELETE_TOOL, agent_name="SIGNAL", session_id=None,
                user_id=current.id, scope_type=None, allowed=False,
                duration_ms=None, status="denied", error_message=decision.reason,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=decision.reason
            )
    did = _parse_uuid(draft_id, "draft_id")
    started = time.perf_counter()
    try:
        ok = await signal_tools.delete_draft(did)
    except signal_tools.SignalToolError as exc:
        await log_execution_attempt(
            tool_name=_DELETE_TOOL, agent_name="SIGNAL", session_id=None,
            user_id=current.id, scope_type=None, allowed=True,
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="failed", error_message=str(exc),
        )
        raise _err(exc)
    duration_ms = int((time.perf_counter() - started) * 1000)
    if not ok:
        # Lost a race — the row vanished between visibility check and delete.
        await log_execution_attempt(
            tool_name=_DELETE_TOOL, agent_name="SIGNAL", session_id=None,
            user_id=current.id, scope_type=None, allowed=True,
            duration_ms=duration_ms, status="failed", error_message="draft not found",
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="draft not found"
        )
    await log_execution_attempt(
        tool_name=_DELETE_TOOL, agent_name="SIGNAL", session_id=None,
        user_id=current.id, scope_type=None, allowed=True,
        duration_ms=duration_ms, status="success", error_message=None,
    )
    await write_trace(
        session_id=None,
        user_id=current.id,
        trace_type="draft_deleted",
        status="ok",
        selected_agent="SIGNAL",
        tool_name=_DELETE_TOOL,
        tool_result={
            "draft_id": str(did),
            "title": row["title"],
            "prior_status": row["status"],
        },
        workspace_id=wid,
    )
    return DraftDeleteOut(deleted=True, draft_id=str(did))
