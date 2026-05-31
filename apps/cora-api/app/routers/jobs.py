import logging
import uuid
from datetime import datetime
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.auth import CurrentUser, require_admin
from app.jobs import JobError, cancel_job, create_job, get_job, list_jobs
from app.runtime_traces import write_trace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/jobs", tags=["admin", "jobs"])


class JobOut(BaseModel):
    id: str
    user_id: Optional[str]
    session_id: Optional[str]
    plan_id: Optional[str]
    step_id: Optional[str]
    job_type: str
    status: str
    payload: Optional[Any]
    result: Optional[Any]
    error_message: Optional[str]
    attempts: int
    max_attempts: int
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]


class CreateJobRequest(BaseModel):
    job_type: str = Field(min_length=1, max_length=80)
    payload: Optional[dict] = None
    plan_id: Optional[str] = None
    step_id: Optional[str] = None
    session_id: Optional[str] = None
    max_attempts: int = Field(default=3, ge=1, le=20)


def _row_to_out(row: dict) -> JobOut:
    return JobOut(
        id=str(row["id"]),
        user_id=str(row["user_id"]) if row["user_id"] else None,
        session_id=str(row["session_id"]) if row["session_id"] else None,
        plan_id=str(row["plan_id"]) if row["plan_id"] else None,
        step_id=str(row["step_id"]) if row["step_id"] else None,
        job_type=row["job_type"],
        status=row["status"],
        payload=row["payload"],
        result=row["result"],
        error_message=row["error_message"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


def _job_error_to_http(exc: JobError) -> HTTPException:
    code_map = {
        "not_found": status.HTTP_404_NOT_FOUND,
        "invalid_transition": status.HTTP_409_CONFLICT,
        "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    }
    return HTTPException(
        status_code=code_map.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=str(exc),
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
    response_model=list[JobOut],
    summary="List jobs (admin only). Most recent first.",
)
async def list_jobs_endpoint(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    job_type: Optional[str] = Query(default=None),
    plan_id: Optional[str] = Query(default=None),
) -> list[JobOut]:
    plan_uuid = _parse_uuid(plan_id, "plan_id") if plan_id else None
    rows = await list_jobs(
        limit=limit,
        offset=offset,
        status_filter=status_filter,
        job_type=job_type,
        plan_id=plan_uuid,
    )
    logger.info(
        "admin list jobs: admin=%s status=%s job_type=%s plan_id=%s count=%s",
        admin.id,
        status_filter,
        job_type,
        plan_id,
        len(rows),
    )
    return [_row_to_out(r) for r in rows]


@router.get(
    "/{job_id}",
    response_model=JobOut,
    summary="Get one job by id (admin only).",
)
async def get_job_endpoint(
    job_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> JobOut:
    jid = _parse_uuid(job_id, "job_id")
    row = await get_job(jid)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="job not found"
        )
    return _row_to_out(row)


@router.post(
    "",
    response_model=JobOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an arbitrary job (admin only). Useful for queueing tests.",
)
async def create_job_endpoint(
    req: CreateJobRequest,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> JobOut:
    plan_uuid = _parse_uuid(req.plan_id, "plan_id") if req.plan_id else None
    step_uuid = _parse_uuid(req.step_id, "step_id") if req.step_id else None
    session_uuid = _parse_uuid(req.session_id, "session_id") if req.session_id else None
    try:
        row = await create_job(
            user_id=admin.id,
            session_id=session_uuid,
            plan_id=plan_uuid,
            step_id=step_uuid,
            job_type=req.job_type,
            payload=req.payload,
            max_attempts=req.max_attempts,
        )
    except JobError as exc:
        raise _job_error_to_http(exc) from exc
    await write_trace(
        session_id=session_uuid,
        user_id=admin.id,
        trace_type="job_created",
        status="ok",
        selected_agent="ATLAS",
        tool_name="job_create",
        tool_result={
            "job_id": row["id"] if isinstance(row["id"], str) else str(row["id"]),
            "job_type": req.job_type,
            "plan_id": req.plan_id,
            "step_id": req.step_id,
        },
    )
    return _row_to_out(row)


@router.post(
    "/{job_id}/cancel",
    response_model=JobOut,
    summary="Cancel a queued job (admin only). No-op if already terminal.",
)
async def cancel_job_endpoint(
    job_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> JobOut:
    jid = _parse_uuid(job_id, "job_id")
    try:
        row = await cancel_job(jid)
    except JobError as exc:
        raise _job_error_to_http(exc) from exc
    await write_trace(
        session_id=row["session_id"],
        user_id=admin.id,
        trace_type="job_cancelled",
        status="ok",
        selected_agent="ATLAS",
        tool_name="job_cancel",
        tool_result={"job_id": str(row["id"])},
    )
    return _row_to_out(row)
