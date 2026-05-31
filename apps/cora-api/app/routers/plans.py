import logging
import uuid
from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.agents.planner import (
    PlanError,
    cancel_plan,
    complete_plan,
    complete_step,
    fail_step,
    get_plan,
    list_plans,
    list_plans_for_session,
    update_plan,
    update_step,
)
from app.agents.delegations import list_delegations
from app.auth import CurrentUser, get_current_user
from app.jobs import JobError, create_job
from app.runtime_traces import write_trace
from app.routers.delegations import DelegationOut, _row_to_out as _delegation_row_to_out

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/plans", tags=["plans"])


class PlanStepOut(BaseModel):
    id: str
    plan_id: str
    step_number: int
    title: str
    description: Optional[str]
    assigned_agent: Optional[str]
    tool_name: Optional[str]
    status: str
    result: Optional[dict]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    created_at: datetime


class PlanOut(BaseModel):
    id: str
    session_id: Optional[str]
    user_id: Optional[str]
    title: str
    goal: str
    status: str
    current_step: int
    total_steps: int
    selected_agent: Optional[str]
    created_at: datetime
    updated_at: datetime


class PlanDetailOut(PlanOut):
    steps: list[PlanStepOut]


class UpdatePlanRequest(BaseModel):
    title: Optional[str] = None
    goal: Optional[str] = None
    status: Optional[str] = None


class UpdateStepRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    assigned_agent: Optional[str] = None
    tool_name: Optional[str] = None
    status: Optional[str] = None
    result: Optional[dict] = None


class CompleteStepRequest(BaseModel):
    result: Optional[dict] = None


class FailStepRequest(BaseModel):
    error_message: Optional[str] = None


class QueueStepResponse(BaseModel):
    job_id: str
    plan_id: str
    step_id: str
    status: str
    job_type: str
    created_at: datetime


def _plan_error_to_http(exc: PlanError) -> HTTPException:
    code_map = {
        "not_found": status.HTTP_404_NOT_FOUND,
        "forbidden": status.HTTP_403_FORBIDDEN,
        "terminal": status.HTTP_409_CONFLICT,
        "invalid_transition": status.HTTP_409_CONFLICT,
        "steps_outstanding": status.HTTP_409_CONFLICT,
        "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    }
    return HTTPException(
        status_code=code_map.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=str(exc),
    )


def _parse_plan_id(plan_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(plan_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="plan_id must be a valid UUID",
        ) from exc


def _parse_step_id(step_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(step_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="step_id must be a valid UUID",
        ) from exc


def _plan_row_to_out(row: dict) -> PlanOut:
    return PlanOut(
        id=str(row["id"]),
        session_id=str(row["session_id"]) if row["session_id"] else None,
        user_id=str(row["user_id"]) if row["user_id"] else None,
        title=row["title"],
        goal=row["goal"],
        status=row["status"],
        current_step=row["current_step"],
        total_steps=row["total_steps"],
        selected_agent=row["selected_agent"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _step_row_to_out(row: dict) -> PlanStepOut:
    return PlanStepOut(
        id=str(row["id"]),
        plan_id=str(row["plan_id"]),
        step_number=row["step_number"],
        title=row["title"],
        description=row["description"],
        assigned_agent=row["assigned_agent"],
        tool_name=row["tool_name"],
        status=row["status"],
        result=row["result"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        created_at=row["created_at"],
    )


@router.get(
    "",
    response_model=list[PlanOut],
    summary="List execution plans (admins see all; users see their own).",
)
async def list_plans_endpoint(
    current: Annotated[CurrentUser, Depends(get_current_user)],
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[PlanOut]:
    rows = await list_plans(
        user_id=current.id,
        is_admin=(current.role == "admin"),
        limit=limit,
        offset=offset,
    )
    logger.info(
        "list plans: user_id=%s admin=%s count=%s",
        current.id,
        current.role == "admin",
        len(rows),
    )
    return [_plan_row_to_out(r) for r in rows]


@router.get(
    "/session/{session_id}",
    response_model=list[PlanOut],
    summary="List plans for a session (scoped by ownership; admin sees all).",
)
async def list_plans_for_session_endpoint(
    session_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> list[PlanOut]:
    try:
        session_uuid = uuid.UUID(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="session_id must be a valid UUID",
        ) from exc
    rows = await list_plans_for_session(
        session_uuid,
        user_id=current.id,
        is_admin=(current.role == "admin"),
    )
    return [_plan_row_to_out(r) for r in rows]


@router.get(
    "/{plan_id}",
    response_model=PlanDetailOut,
    summary="Get a plan with its full step list.",
)
async def get_plan_endpoint(
    plan_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> PlanDetailOut:
    try:
        pid = uuid.UUID(plan_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="plan_id must be a valid UUID",
        ) from exc
    plan = await get_plan(
        pid,
        user_id=current.id,
        is_admin=(current.role == "admin"),
    )
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="plan not found"
        )
    base = _plan_row_to_out(plan)
    return PlanDetailOut(
        **base.model_dump(),
        steps=[_step_row_to_out(s) for s in plan["steps"]],
    )


# ---------- Mutations ----------


@router.patch(
    "/{plan_id}",
    response_model=PlanOut,
    summary="Update plan title, goal, and/or status (owner or admin).",
)
async def patch_plan(
    plan_id: str,
    req: UpdatePlanRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> PlanOut:
    pid = _parse_plan_id(plan_id)
    try:
        row = await update_plan(
            pid,
            user_id=current.id,
            is_admin=(current.role == "admin"),
            title=req.title,
            goal=req.goal,
            status_value=req.status,
        )
    except PlanError as exc:
        raise _plan_error_to_http(exc) from exc
    await write_trace(
        session_id=row["session_id"],
        user_id=current.id,
        trace_type="plan_updated",
        status="ok",
        selected_agent="ATLAS",
        tool_name="plan_update",
        tool_result={
            "plan_id": str(pid),
            "fields": [k for k in ("title", "goal", "status")
                       if getattr(req, k) is not None],
            "new_status": row["status"],
        },
    )
    return _plan_row_to_out(row)


@router.patch(
    "/{plan_id}/steps/{step_id}",
    response_model=PlanStepOut,
    summary="Update step fields (title, description, assigned_agent, tool_name, status, result).",
)
async def patch_step(
    plan_id: str,
    step_id: str,
    req: UpdateStepRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> PlanStepOut:
    pid = _parse_plan_id(plan_id)
    sid = _parse_step_id(step_id)
    try:
        row = await update_step(
            pid,
            sid,
            user_id=current.id,
            is_admin=(current.role == "admin"),
            title=req.title,
            description=req.description,
            assigned_agent=req.assigned_agent,
            tool_name=req.tool_name,
            status_value=req.status,
            result=req.result,
        )
    except PlanError as exc:
        raise _plan_error_to_http(exc) from exc
    trace_type = (
        "step_completed" if req.status == "completed" else "step_updated"
    )
    await write_trace(
        session_id=None,
        user_id=current.id,
        trace_type=trace_type,
        status="ok",
        selected_agent="ATLAS",
        tool_name="step_update",
        tool_result={
            "plan_id": str(pid),
            "step_id": str(sid),
            "step_number": row["step_number"],
            "new_status": row["status"],
        },
    )
    return _step_row_to_out(row)


@router.post(
    "/{plan_id}/cancel",
    response_model=PlanOut,
    summary="Cancel a plan (sets status='cancelled'). Owner or admin.",
)
async def cancel_plan_endpoint(
    plan_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> PlanOut:
    pid = _parse_plan_id(plan_id)
    try:
        row = await cancel_plan(
            pid, user_id=current.id, is_admin=(current.role == "admin")
        )
    except PlanError as exc:
        raise _plan_error_to_http(exc) from exc
    await write_trace(
        session_id=row["session_id"],
        user_id=current.id,
        trace_type="plan_cancelled",
        status="ok",
        selected_agent="ATLAS",
        tool_name="plan_cancel",
        tool_result={"plan_id": str(pid)},
    )
    return _plan_row_to_out(row)


@router.post(
    "/{plan_id}/complete",
    response_model=PlanOut,
    summary="Mark a plan completed (only if every step is terminal).",
)
async def complete_plan_endpoint(
    plan_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> PlanOut:
    pid = _parse_plan_id(plan_id)
    try:
        row = await complete_plan(
            pid, user_id=current.id, is_admin=(current.role == "admin")
        )
    except PlanError as exc:
        raise _plan_error_to_http(exc) from exc
    await write_trace(
        session_id=row["session_id"],
        user_id=current.id,
        trace_type="plan_completed",
        status="ok",
        selected_agent="ATLAS",
        tool_name="plan_complete",
        tool_result={"plan_id": str(pid)},
    )
    return _plan_row_to_out(row)


@router.post(
    "/{plan_id}/steps/{step_id}/complete",
    response_model=PlanStepOut,
    summary="Mark a step completed. Owner or admin.",
)
async def complete_step_endpoint(
    plan_id: str,
    step_id: str,
    req: CompleteStepRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> PlanStepOut:
    pid = _parse_plan_id(plan_id)
    sid = _parse_step_id(step_id)
    try:
        row = await complete_step(
            pid,
            sid,
            user_id=current.id,
            is_admin=(current.role == "admin"),
            result=req.result,
        )
    except PlanError as exc:
        raise _plan_error_to_http(exc) from exc
    await write_trace(
        session_id=None,
        user_id=current.id,
        trace_type="step_completed",
        status="ok",
        selected_agent="ATLAS",
        tool_name="step_complete",
        tool_result={
            "plan_id": str(pid),
            "step_id": str(sid),
            "step_number": row["step_number"],
        },
    )
    return _step_row_to_out(row)


@router.post(
    "/{plan_id}/steps/{step_id}/fail",
    response_model=PlanStepOut,
    summary="Mark a step failed. Owner or admin.",
)
async def fail_step_endpoint(
    plan_id: str,
    step_id: str,
    req: FailStepRequest,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> PlanStepOut:
    pid = _parse_plan_id(plan_id)
    sid = _parse_step_id(step_id)
    try:
        row = await fail_step(
            pid,
            sid,
            user_id=current.id,
            is_admin=(current.role == "admin"),
            error_message=req.error_message,
        )
    except PlanError as exc:
        raise _plan_error_to_http(exc) from exc
    await write_trace(
        session_id=None,
        user_id=current.id,
        trace_type="step_updated",
        status="error",
        selected_agent="ATLAS",
        tool_name="step_fail",
        tool_result={
            "plan_id": str(pid),
            "step_id": str(sid),
            "step_number": row["step_number"],
            "new_status": "failed",
        },
        error_message=req.error_message,
    )
    return _step_row_to_out(row)


@router.post(
    "/{plan_id}/steps/{step_id}/queue",
    response_model=QueueStepResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Queue a plan step as a background job. v0.1 stores only; no worker.",
)
async def queue_step_endpoint(
    plan_id: str,
    step_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> QueueStepResponse:
    pid = _parse_plan_id(plan_id)
    sid = _parse_step_id(step_id)
    # Fetch + auth using the existing planner read path (respects ownership).
    plan = await get_plan(
        pid,
        user_id=current.id,
        is_admin=(current.role == "admin"),
    )
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="plan not found"
        )
    step = next((s for s in plan["steps"] if s["id"] == sid), None)
    if step is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="step not found"
        )

    payload = {
        "plan_id": str(pid),
        "step_id": str(sid),
        "step_number": step["step_number"],
        "title": step["title"],
        "description": step["description"],
        "assigned_agent": step["assigned_agent"],
        "tool_name": step["tool_name"],
    }
    try:
        job = await create_job(
            user_id=current.id,
            session_id=plan["session_id"],
            plan_id=pid,
            step_id=sid,
            job_type="execution_plan_step",
            payload=payload,
        )
    except JobError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    await write_trace(
        session_id=plan["session_id"],
        user_id=current.id,
        trace_type="job_created",
        status="ok",
        selected_agent="ATLAS",
        tool_name="job_create",
        tool_result={
            "job_id": str(job["id"]),
            "job_type": "execution_plan_step",
            "plan_id": str(pid),
            "step_id": str(sid),
        },
    )
    logger.info(
        "queued plan step: user=%s plan=%s step=%s job=%s",
        current.id,
        pid,
        sid,
        job["id"],
    )
    return QueueStepResponse(
        job_id=str(job["id"]),
        plan_id=str(pid),
        step_id=str(sid),
        status=job["status"],
        job_type=job["job_type"],
        created_at=job["created_at"],
    )


@router.get(
    "/{plan_id}/delegations",
    response_model=list[DelegationOut],
    summary="Delegation timeline for a plan.",
)
async def list_plan_delegations(
    plan_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> list[DelegationOut]:
    pid = _parse_plan_id(plan_id)
    # Authorize via the standard plan-read path (owner or admin).
    plan = await get_plan(
        pid,
        user_id=current.id,
        is_admin=(current.role == "admin"),
    )
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="plan not found"
        )
    rows = await list_delegations(execution_plan_id=pid, limit=500)
    return [_delegation_row_to_out(r) for r in rows]
