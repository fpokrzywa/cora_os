"""OAuth readiness SIMULATION (v0.6) — analysis only, never executes.

Given an approved external_integration_intent, simulate whether it *would* be
ready for future external execution based on the caller's provider connector
(status, scopes, token presence/expiry) and governance. This performs NO OAuth,
NO provider call, NO token exchange. execution_enabled is a hard FALSE this
phase, so ready_for_execution is always False — the value of this module is the
explained list of blockers + the recommended next step.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.clients import clients
from app import integration_readiness as ir
from app.tools.governance import is_external_execution_tool

logger = logging.getLogger(__name__)

# Hard invariant for this phase — no external execution is ever enabled.
EXECUTION_ENABLED = False

# intent_type -> required provider_type
INTENT_PROVIDER_TYPE = {
    "email_send_intent": "email",
    "calendar_create_intent": "calendar",
}

# The external action tool each intent would dispatch (all governance-blocked).
INTENT_EXTERNAL_TOOL = {
    "email_send_intent": "send_email",
    "calendar_create_intent": "create_calendar_event",
}

# Required OAuth scopes per provider placeholder.
PROVIDER_REQUIRED_SCOPES = {
    "gmail": ["gmail.send"],
    "outlook_mail": ["Mail.Send"],
    "google_calendar": ["calendar.events"],
    "outlook_calendar": ["Calendars.ReadWrite"],
}


class ReadinessError(Exception):
    """code: not_found (404) | invalid (400) | unavailable (503)."""

    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


def _require_pool():
    if clients.db_pool is None:
        raise ReadinessError("Postgres pool unavailable", code="unavailable")
    return clients.db_pool


def _scope_tail(scope: str) -> str:
    """Normalize a scope to its final segment so the short required form
    (`gmail.send`) matches the full granted URL Google/Microsoft return
    (`https://www.googleapis.com/auth/gmail.send`)."""
    return (scope or "").rstrip("/").rsplit("/", 1)[-1]


def _missing_scopes(required: list, available: list) -> list:
    granted = {_scope_tail(s) for s in (available or [])}
    return [s for s in (required or []) if _scope_tail(s) not in granted]


async def _visible_intent(intent_id: uuid.UUID, *, user_id: uuid.UUID, is_admin: bool) -> dict:
    intent = await ir.get_intent(intent_id)
    if intent is None or (not is_admin and intent["created_by"] != user_id):
        raise ReadinessError("intent not found", code="not_found")
    return intent


async def _best_connector(conn, owner_id, provider_type: str) -> Optional[dict]:
    """Pick the caller's connector for the type: prefer a connected one, then the
    most recent non-disconnected record."""
    row = await conn.fetchrow(
        """
        SELECT provider_name, status, scopes, access_token_encrypted,
               refresh_token_encrypted, token_expires_at
        FROM provider_oauth_connectors
        WHERE user_id = $1 AND provider_type = $2 AND status <> 'disconnected'
        ORDER BY (status = 'connected') DESC, created_at DESC
        LIMIT 1
        """,
        owner_id, provider_type,
    )
    return dict(row) if row else None


def _evaluate(intent: dict, connector: Optional[dict]) -> dict:
    # The semantic intent type lives in metadata; action_type is the external
    # action (send_email / create_calendar_event). provider_type column is the
    # reliable source for required provider + the external tool to governance-check.
    meta = intent.get("metadata") or {}
    intent_type = meta.get("intent_type") or intent["action_type"]
    required_type = intent.get("provider_type") or INTENT_PROVIDER_TYPE.get(intent_type)
    external_tool = "create_calendar_event" if required_type == "calendar" else "send_email"
    # Governance: the external execution tool is hard-blocked, so execution is
    # NOT permitted by governance in this build.
    governance_allowed = not is_external_execution_tool(external_tool)

    connector_found = connector is not None
    connector_status = connector["status"] if connector_found else "not_configured"
    required_provider_name = connector["provider_name"] if connector_found else None
    available_scopes = list(connector.get("scopes") or []) if connector_found else []
    required_scopes = (
        PROVIDER_REQUIRED_SCOPES.get(required_provider_name, []) if connector_found else []
    )
    missing_scopes = _missing_scopes(required_scopes, available_scopes)
    has_access_token = bool(connector.get("access_token_encrypted")) if connector_found else False
    has_refresh_token = bool(connector.get("refresh_token_encrypted")) if connector_found else False
    exp = connector.get("token_expires_at") if connector_found else None
    token_expired = bool(exp and exp <= datetime.now(timezone.utc))

    blockers: list[str] = []
    if not connector_found:
        blockers.append(f"no {required_type} provider connector configured")
    else:
        if connector_status != "connected":
            blockers.append(f"connector status is {connector_status!r} (needs 'connected')")
        if missing_scopes:
            blockers.append("missing required scopes: " + ", ".join(missing_scopes))
        if not has_access_token:
            blockers.append("no access token")
        if not has_refresh_token:
            blockers.append("no refresh token")
        if token_expired:
            blockers.append("access token expired")
    if not governance_allowed:
        blockers.append("governance blocks external execution")
    if not EXECUTION_ENABLED:
        blockers.append("external execution is disabled in this phase")

    ready_for_execution = (
        connector_found
        and connector_status == "connected"
        and not missing_scopes
        and has_access_token
        and has_refresh_token
        and not token_expired
        and governance_allowed
        and EXECUTION_ENABLED
    )

    # Recommended next step from the first actionable blocker.
    if not connector_found:
        step = f"Register a {required_type} provider connector (placeholder) for this intent."
    elif connector_status != "connected":
        step = "Complete OAuth authorization for the connector (future phase) to reach 'connected'."
    elif missing_scopes:
        step = "Grant the missing OAuth scopes: " + ", ".join(missing_scopes) + "."
    elif not (has_access_token and has_refresh_token):
        step = "Obtain access + refresh tokens via the OAuth flow (future phase)."
    elif token_expired:
        step = "Refresh the expired access token (future phase)."
    elif not EXECUTION_ENABLED:
        step = "External execution is intentionally disabled in this phase — no action available yet."
    else:
        step = "Ready pending execution enablement."

    return {
        "intent_id": str(intent["id"]),
        "intent_type": intent_type,
        "source_type": intent["source_type"],
        "source_id": str(intent["source_id"]),
        "required_provider_type": required_type,
        "required_provider_name": required_provider_name,
        "connector_found": connector_found,
        "connector_status": connector_status,
        "required_scopes": required_scopes,
        "available_scopes": available_scopes,
        "missing_scopes": missing_scopes,
        "has_access_token": has_access_token,
        "has_refresh_token": has_refresh_token,
        "token_expired": token_expired,
        "governance_allowed": governance_allowed,
        "execution_enabled": EXECUTION_ENABLED,
        "ready_for_execution": ready_for_execution,
        "blockers": blockers,
        "recommended_next_step": step,
    }


async def simulate_readiness(
    intent_id: uuid.UUID, *, user_id: uuid.UUID, is_admin: bool, persist: bool = True
) -> dict:
    """Run the simulation and (by default) store the result in the intent's
    metadata under `readiness_simulation`. No schema change; no external call."""
    intent = await _visible_intent(intent_id, user_id=user_id, is_admin=is_admin)
    required_type = INTENT_PROVIDER_TYPE.get(intent["action_type"]) or intent["provider_type"]
    pool = _require_pool()
    async with pool.acquire() as conn:
        connector = await _best_connector(conn, intent["created_by"], required_type)
        result = _evaluate(intent, connector)
        if persist:
            meta = dict(intent.get("metadata") or {})
            meta["readiness_simulation"] = result
            await conn.execute(
                "UPDATE external_integration_intents "
                "SET metadata = $1, updated_at = NOW() WHERE id = $2",
                meta, intent_id,
            )
    return result


async def check_intent_readiness(
    intent_id: uuid.UUID, *, user_id: uuid.UUID, is_admin: bool
) -> dict:
    """Readiness check for the Integration Readiness Queue: run the simulation,
    persist a compact summary into the intent's `validation_result` column, and
    return the full result. `dry_run` is never touched (stays true) and execution
    stays disabled — no provider call is made."""
    result = await simulate_readiness(
        intent_id, user_id=user_id, is_admin=is_admin, persist=True
    )
    summary = {
        "ok": result["ready_for_execution"],
        "ready_for_execution": result["ready_for_execution"],
        "execution_enabled": result["execution_enabled"],
        "connector_status": result["connector_status"],
        "required_scopes": result["required_scopes"],
        "missing_scopes": result["missing_scopes"],
        "blockers": result["blockers"],
        "recommended_next_step": result["recommended_next_step"],
        "checked_via": "integration_intent_readiness_checked",
    }
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE external_integration_intents "
            "SET validation_result = $1, updated_at = NOW() WHERE id = $2",
            summary, intent_id,
        )
    return {**result, "validation_result": summary}


async def get_readiness(
    intent_id: uuid.UUID, *, user_id: uuid.UUID, is_admin: bool
) -> dict:
    """Return the last stored simulation; if none, compute a fresh one (read-only,
    not persisted) so the endpoint is always useful."""
    intent = await _visible_intent(intent_id, user_id=user_id, is_admin=is_admin)
    stored = (intent.get("metadata") or {}).get("readiness_simulation")
    if stored:
        return {**stored, "from_cache": True}
    fresh = await simulate_readiness(
        intent_id, user_id=user_id, is_admin=is_admin, persist=False
    )
    return {**fresh, "from_cache": False, "note": "not yet simulated — computed live"}


async def readiness_summary(*, user_id: uuid.UUID, is_admin: bool) -> dict:
    """Aggregate readiness across the caller's readiness-queue intents."""
    rows = await ir.list_intents(
        workspace_id=None, owner_id=None if is_admin else user_id,
    )
    rows = [r for r in rows if (r.get("metadata") or {}).get("workflow") == ir.RQ_WORKFLOW_TAG]
    items = []
    simulated = 0
    for r in rows:
        sim = (r.get("metadata") or {}).get("readiness_simulation")
        if sim:
            simulated += 1
        items.append({
            "intent_id": str(r["id"]),
            "intent_type": (r.get("metadata") or {}).get("intent_type") or r["action_type"],
            "action_type": r["action_type"],
            "intent_status": r["status"],
            "simulated": bool(sim),
            "ready_for_execution": bool(sim and sim.get("ready_for_execution")),
            "blocker_count": len(sim.get("blockers", [])) if sim else None,
        })
    return {
        "total": len(items),
        "simulated": simulated,
        "ready_count": sum(1 for i in items if i["ready_for_execution"]),
        "execution_enabled": EXECUTION_ENABLED,
        "items": items,
    }
