"""Per-user UI preference blob (GET/PUT /users/me/ui-prefs).

Backs cross-origin/cross-device persistence of frontend settings. The
cora-ui2 shell keeps its settings (theme, orb, voice, card layout, panel
state) in browser localStorage, which is per-origin and lost on a
site-data clear — its adapter mirrors those keys here so they follow the
account instead. One JSONB blob per user, replaced wholesale on PUT.
"""

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth import CurrentUser, get_current_user
from app.clients import clients

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users/me/ui-prefs", tags=["ui-prefs"])

# Generous for UI state, small enough to keep the row harmless.
MAX_PREFS_BYTES = 64 * 1024


class UiPrefsOut(BaseModel):
    prefs: dict[str, Any]


class UiPrefsIn(BaseModel):
    prefs: dict[str, Any]


def _require_pool():
    if clients.db_pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database not available",
        )
    return clients.db_pool


@router.get("", response_model=UiPrefsOut, summary="Fetch the caller's UI prefs blob")
async def get_ui_prefs(
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> UiPrefsOut:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT prefs FROM user_ui_prefs WHERE user_id = $1", current.id
        )
    prefs = json.loads(row["prefs"]) if row and row["prefs"] else {}
    return UiPrefsOut(prefs=prefs if isinstance(prefs, dict) else {})


@router.put("", response_model=UiPrefsOut, summary="Replace the caller's UI prefs blob")
async def put_ui_prefs(
    payload: UiPrefsIn,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> UiPrefsOut:
    serialized = json.dumps(payload.prefs)
    if len(serialized.encode("utf-8")) > MAX_PREFS_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"prefs blob exceeds {MAX_PREFS_BYTES} bytes",
        )
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_ui_prefs (user_id, prefs, updated_at)
            VALUES ($1, $2::jsonb, NOW())
            ON CONFLICT (user_id)
            DO UPDATE SET prefs = EXCLUDED.prefs, updated_at = NOW()
            """,
            current.id,
            serialized,
        )
    return UiPrefsOut(prefs=payload.prefs)
