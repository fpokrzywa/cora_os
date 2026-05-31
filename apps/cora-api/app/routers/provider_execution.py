"""Provider Execution Framework v1.0 — endpoints.

`/provider-execution/{id}/simulate` runs the adapter dry-run (SIMULATED, no real
call). `/provider-execution/{id}/execute` attempts real execution — always
blocked by the v0.8 kill switch this phase (EXECUTION_NOT_ENABLED / BLOCKED).
Both are governed, audited (tool_execution_logs) and traced (runtime_traces).
No /api prefix — consistent with every other router in this codebase.
"""

import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status

from app import provider_adapters as adapters
from app import provider_execution as pexec
from app.auth import CurrentUser, get_current_user

router = APIRouter(prefix="/provider-execution", tags=["provider-execution"])


def _parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="intent_id must be a valid UUID",
        ) from exc


@router.get("/adapters")
async def list_provider_adapters(
    current: Annotated[CurrentUser, Depends(get_current_user)],
):
    """The supported provider adapters + actions. real_execution is always false."""
    return {
        "adapters": adapters.list_adapters(),
        "real_execution_enabled": False,
    }


@router.post("/{intent_id}/simulate")
async def simulate_provider_execution(
    intent_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
    provider: Optional[str] = None,
):
    """Dry-run the provider adapter for an intent. `provider` optionally targets a
    specific adapter (pending-provider intents have no bound provider yet)."""
    result = await pexec.execute_intent(
        _parse_uuid(intent_id),
        user_id=current.id, is_admin=(current.role == "admin"), simulate=True,
        provider_name=provider,
    )
    return result.as_dict()


@router.post("/{intent_id}/execute")
async def execute_provider_execution(
    intent_id: str,
    current: Annotated[CurrentUser, Depends(get_current_user)],
    provider: Optional[str] = None,
):
    """Attempt real execution. Returns a governed result; in this phase the
    status is always execution_not_enabled / blocked (kill switch). Never makes a
    real provider call."""
    result = await pexec.execute_intent(
        _parse_uuid(intent_id),
        user_id=current.id, is_admin=(current.role == "admin"), simulate=False,
        provider_name=provider,
    )
    return result.as_dict()
