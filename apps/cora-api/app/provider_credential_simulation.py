"""Provider Credential Usage Simulation v1.3 — readiness-only, executes nothing.

For an approved email/calendar integration intent this:
  1. resolves the caller's connected provider credential (provider_oauth_connectors),
  2. validates it (connected / token valid-or-refreshable / required scopes /
     provider execution disabled / dry_run_only / global kill switch blocks),
  3. generates the provider-ready payload preview that *would* be sent, and
  4. confirms the v0.8 kill switch blocks execution — then stores the simulation
     on the intent (metadata.credential_usage_simulation + an
     external_integration_events row).

It calls NO Gmail / Outlook / Google Calendar / Microsoft Graph API, performs no
token exchange, and NEVER reads or exposes access/refresh token values — only
presence flags (has_access_token / has_refresh_token). Execution stays globally
disabled; this only previews and explains.
"""

import uuid
from typing import Optional

from app.clients import clients
from app import execution_guard as guard
from app import integration_readiness as ir
from app import oauth_readiness as orr
from app import provider_adapters as adapters
from app import provider_execution as pexec
from app.runtime_traces import write_trace

# Runtime traces (spec #5).
TRACE_RESOLVED = "provider_credential_resolved"
TRACE_SIMULATED = "provider_payload_simulated"
TRACE_BLOCKED = "provider_execution_blocked_by_governance"

_TOOL = "provider_credential_simulation"

# Provider-neutral external action per provider_type.
_ACTION_FOR_TYPE = {
    "email": adapters.ACTION_SEND_EMAIL,
    "calendar": adapters.ACTION_CREATE_CALENDAR_EVENT,
}
# Intent statuses considered approved/ready for a credential-usage simulation.
_APPROVED_STATUSES = frozenset({
    ir.STATUS_CONFIRMED, ir.STATUS_READY, ir.RQ_READY_FUTURE,
})


class SimulationError(Exception):
    """code: not_found (404) | invalid (400) | unavailable (503)."""

    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


def _require_pool():
    if clients.db_pool is None:
        raise SimulationError("Postgres pool unavailable", code="unavailable")
    return clients.db_pool


async def credential_snapshot(intent: dict, *, user_id: uuid.UUID) -> dict:
    """Resolve the caller's connected credential, validate it, and build the
    provider-ready payload preview + governance evaluation — returning the full
    result dict. PURE: writes no trace, metadata, or event. Used by the v1.3
    endpoint (which then traces + persists) and by the v1.4 approval console
    (which records its own audit). Reads/exposes NO token material."""
    pool = _require_pool()
    provider_type = intent.get("provider_type")
    action_type = intent.get("action_type") or _ACTION_FOR_TYPE.get(provider_type)
    intent_status = intent.get("status")

    async with pool.acquire() as conn:
        connector = await orr._best_connector(conn, intent["created_by"], provider_type)
    ev = orr._evaluate(intent, connector)
    provider_name = ev["required_provider_name"]
    credential_resolved = bool(connector) and ev["connector_status"] == "connected"
    # "token valid or refreshed": a live (unexpired) access token, else a refresh
    # token to renew it. No live refresh is performed (that would be a real
    # token-endpoint call) — this validates refreshability only.
    token_valid_or_refreshable = (
        (ev["has_access_token"] and not ev["token_expired"]) or ev["has_refresh_token"]
    )

    adapter = adapters.get_adapter(provider_name)
    payload_preview = None
    payload_errors: list[str] = []
    if adapter is not None and adapter.provider_type == provider_type:
        payload = pexec._build_execution_payload(intent)
        payload_errors = adapter.validate_payload(action_type, payload)
        payload_preview = adapter.execute(action_type, payload, dry_run=True)
    else:
        payload_errors = [
            f"no connected provider adapter for provider_type {provider_type!r}"
        ]
    payload_ready = bool(payload_preview) and not payload_errors

    gr = guard.evaluate_external_execution(
        action_type, provider_type, user_id=user_id,
        workspace_id=intent.get("workspace_id"), intent=intent,
        provider_connected=credential_resolved, token_ready=token_valid_or_refreshable,
    )

    return {
        "intent_id": str(intent["id"]),
        "intent_status": intent_status,
        "intent_approved": intent_status in _APPROVED_STATUSES,
        "source_type": intent.get("source_type"),
        "source_id": str(intent.get("source_id")),
        "provider_type": provider_type,
        "provider_name": provider_name,
        "action_type": action_type,
        "dry_run_only": True,
        # spec #2 validation checklist
        "validation": {
            "provider_connected": credential_resolved,
            "token_valid_or_refreshable": token_valid_or_refreshable,
            "required_scopes_present": bool(ev["required_scopes"]) and not ev["missing_scopes"],
            "missing_scopes": ev["missing_scopes"],
            "provider_execution_disabled": not guard.external_execution_enabled(),
            "dry_run_only": True,
            "kill_switch_blocks_execution": not gr.allowed,
            "governance_allows_execution": gr.checks["governance_allows"],
        },
        "payload_ready": payload_ready,
        "payload_errors": payload_errors,
        "provider_payload_preview": payload_preview,
        "execution_allowed": gr.allowed,
        "execution_enabled": guard.external_execution_enabled(),
        "blockers": ev["blockers"],
        "guard_blockers": gr.blockers,
        # non-secret enrichments for the tracer + the approval console
        "connector_found": ev["connector_found"],
        "connector_status": ev["connector_status"],
        "credential_resolved": credential_resolved,
        "has_access_token": ev["has_access_token"],
        "has_refresh_token": ev["has_refresh_token"],
        "required_scopes": ev["required_scopes"],
        "guard_checks": gr.checks,
        "note": (
            "Credential usage simulation only — no provider API was called and no "
            "token was exchanged or exposed; execution is disabled by governance."
        ),
    }


async def simulate_credential_usage(
    intent_id: uuid.UUID, *, user_id: uuid.UUID, is_admin: bool,
    session_id: Optional[uuid.UUID] = None,
) -> dict:
    pool = _require_pool()
    intent = await ir.get_intent(intent_id)
    if intent is None or (not is_admin and intent["created_by"] != user_id):
        raise SimulationError("intent not found", code="not_found")

    workspace_id = intent.get("workspace_id")
    agent_name = intent.get("agent_name")
    result = await credential_snapshot(intent, user_id=user_id)
    provider_name = result["provider_name"]
    action_type = result["action_type"]

    # 1. provider_credential_resolved
    await write_trace(
        session_id=session_id, user_id=user_id, trace_type=TRACE_RESOLVED,
        status="ok" if result["credential_resolved"] else "blocked",
        selected_agent=agent_name, tool_name=_TOOL,
        tool_result={
            "intent_id": result["intent_id"],
            "provider_type": result["provider_type"],
            "provider_name": provider_name,
            "connector_found": result["connector_found"],
            "connector_status": result["connector_status"],
            "credential_resolved": result["credential_resolved"],
            "has_access_token": result["has_access_token"],
            "has_refresh_token": result["has_refresh_token"],
            "token_valid_or_refreshable": result["validation"]["token_valid_or_refreshable"],
            "required_scopes": result["required_scopes"],
            "missing_scopes": result["validation"]["missing_scopes"],
        },
        workspace_id=workspace_id,
    )
    # 2. provider_payload_simulated
    await write_trace(
        session_id=session_id, user_id=user_id, trace_type=TRACE_SIMULATED,
        status="ok" if result["payload_ready"] else "failed",
        selected_agent=agent_name, tool_name=_TOOL,
        tool_result={
            "intent_id": result["intent_id"],
            "provider_name": provider_name,
            "action_type": action_type,
            "payload_ready": result["payload_ready"],
            "payload_errors": result["payload_errors"],
            "provider_payload_preview": result["provider_payload_preview"],
        },
        workspace_id=workspace_id,
    )
    # 3. provider_execution_blocked_by_governance
    await write_trace(
        session_id=session_id, user_id=user_id, trace_type=TRACE_BLOCKED,
        status="blocked",
        selected_agent=agent_name, tool_name=_TOOL,
        tool_result={
            "intent_id": result["intent_id"],
            "action_type": action_type,
            "provider_type": result["provider_type"],
            "execution_allowed": result["execution_allowed"],
            "guard_checks": result["guard_checks"],
            "guard_blockers": result["guard_blockers"],
            "execution_enabled": result["execution_enabled"],
        },
        error_message=guard.BLOCKED_MESSAGE,
        workspace_id=workspace_id,
    )

    # 4. Persist on the intent metadata + an external_integration_events row. The
    #    snapshot contains no token material (result carries only presence flags).
    async with pool.acquire() as conn:
        async with conn.transaction():
            meta = dict(intent.get("metadata") or {})
            meta["credential_usage_simulation"] = result
            await conn.execute(
                "UPDATE external_integration_intents "
                "SET metadata = $1, updated_at = NOW() WHERE id = $2",
                meta, intent["id"],
            )
            await ir._insert_event(
                conn, intent["id"], user_id,
                event_type="provider_credential_simulation",
                from_status=result["intent_status"], to_status=result["intent_status"],
                notes=None,
                payload_snapshot={
                    "provider_name": provider_name,
                    "action_type": action_type,
                    "payload_ready": result["payload_ready"],
                    "execution_allowed": result["execution_allowed"],
                    "credential_resolved": result["credential_resolved"],
                },
            )
    return result
