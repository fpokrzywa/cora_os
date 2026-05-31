"""Provider Execution Framework v1.0 — central execution service.

Accepts an APPROVED external_integration_intent, validates it against the
provider adapter contract, calls the v0.8 external-execution kill switch before
any real action, and returns a governed ExecutionResult.

No real provider call is ever made in this phase. Real execution is globally
disabled, so an `execute` attempt always resolves to EXECUTION_NOT_ENABLED /
BLOCKED (audited + traced); only a dry-run SIMULATION — which calls nothing
external — can succeed. The framework reads intents ONLY from
external_integration_intents (via integration_readiness.get_intent).
"""

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from app import execution_guard as guard
from app import integration_readiness as ir
from app import provider_adapters as adapters
from app.runtime_traces import write_trace
from app.tools.governance import log_execution_attempt

logger = logging.getLogger(__name__)

# Execution statuses (spec #11).
STATUS_BLOCKED = "blocked"
STATUS_SIMULATED = "simulated"
STATUS_VALIDATION_FAILED = "validation_failed"
STATUS_PROVIDER_UNSUPPORTED = "provider_unsupported"
STATUS_ACTION_UNSUPPORTED = "action_unsupported"
STATUS_EXECUTION_NOT_ENABLED = "execution_not_enabled"

# Runtime trace types (spec #12).
TRACE_REQUESTED = "provider_execution_requested"
TRACE_VALIDATION_FAILED = "provider_execution_validation_failed"
TRACE_BLOCKED = "provider_execution_blocked"
TRACE_SIMULATED = "provider_execution_simulated"

_TOOL = "provider_execution"

# Intent statuses considered approved/ready for execution. Existing convention:
# the v0.7 Execution Approval Gate produces 'confirmed' (requires_confirmation
# cleared) as the human-approved, ready-for-future-execution state.
READY_STATUSES = frozenset({ir.STATUS_CONFIRMED})

# Trace status per execution status.
_TRACE_STATUS = {
    STATUS_SIMULATED: "ok",
    STATUS_BLOCKED: "blocked",
    STATUS_EXECUTION_NOT_ENABLED: "blocked",
    STATUS_VALIDATION_FAILED: "failed",
    STATUS_PROVIDER_UNSUPPORTED: "failed",
    STATUS_ACTION_UNSUPPORTED: "failed",
}


@dataclass
class ExecutionResult:
    status: str
    intent_id: str
    provider_name: Optional[str] = None
    provider_type: Optional[str] = None
    action_type: Optional[str] = None
    dry_run: bool = True
    simulate: bool = True
    message: str = ""
    errors: list = field(default_factory=list)
    simulated_result: Optional[dict] = None
    execution_enabled: bool = False

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "intent_id": self.intent_id,
            "provider_name": self.provider_name,
            "provider_type": self.provider_type,
            "action_type": self.action_type,
            "dry_run": self.dry_run,
            "simulate": self.simulate,
            "message": self.message,
            "errors": self.errors,
            "simulated_result": self.simulated_result,
            "execution_enabled": self.execution_enabled,
            "real_execution_performed": False,
        }


def _build_execution_payload(intent: dict) -> dict:
    """Map the stored provider-neutral payload_preview to the adapter's expected
    fields. Used for SIMULATION only; recipient_hint stands in as a placeholder
    recipient (never a real address, never delivered)."""
    pv = dict(intent.get("payload_preview") or {})
    action = intent.get("action_type")
    if action == adapters.ACTION_SEND_EMAIL:
        to = list(pv.get("to") or [])
        if not to and pv.get("recipient_hint"):
            to = [pv["recipient_hint"]]
        return {
            "to": to,
            "cc": list(pv.get("cc") or []),
            "bcc": list(pv.get("bcc") or []),
            "subject": pv.get("subject") or "",
            "body": pv.get("body") or "",
        }
    if action == adapters.ACTION_CREATE_CALENDAR_EVENT:
        return {
            "title": pv.get("title") or "",
            "start_time": pv.get("start_time"),
            "end_time": pv.get("end_time"),
            "attendees": list(pv.get("attendees") or []),
            "description": pv.get("description") or "",
            "location": pv.get("location") or "",
            "timezone": pv.get("timezone"),
        }
    return dict(pv)


async def _finish(
    result: ExecutionResult,
    *,
    trace_type: str,
    user_id: Optional[uuid.UUID],
    session_id: Optional[uuid.UUID],
    agent_name: Optional[str],
    workspace_id: Optional[uuid.UUID],
    started: float,
) -> ExecutionResult:
    """Write the per-attempt tool_execution_logs entry (#13) + the outcome
    runtime_trace (#12)."""
    allowed = result.status == STATUS_SIMULATED
    await log_execution_attempt(
        tool_name=_TOOL, agent_name=agent_name, session_id=session_id,
        user_id=user_id, scope_type=None, allowed=allowed,
        duration_ms=int((time.perf_counter() - started) * 1000),
        status=result.status,
        error_message=None if allowed else (result.message or result.status),
    )
    await write_trace(
        session_id=session_id, user_id=user_id, trace_type=trace_type,
        status=_TRACE_STATUS.get(result.status, "failed"),
        selected_agent=agent_name, tool_name=_TOOL,
        tool_result=result.as_dict(),
        error_message=None if allowed else (result.message or None),
        workspace_id=workspace_id,
    )
    return result


async def execute_intent(
    intent_id,
    *,
    user_id: uuid.UUID,
    is_admin: bool,
    simulate: bool,
    provider_name: Optional[str] = None,
    session_id: Optional[uuid.UUID] = None,
) -> ExecutionResult:
    """Validate + route an integration intent through its provider adapter.

    simulate=True  → dry-run only; returns SIMULATED on success (no real call).
    simulate=False → real-execution attempt; the kill switch blocks it
                     (EXECUTION_NOT_ENABLED / BLOCKED). A real adapter call with
                     dry_run=False is never reached this phase.

    provider_name overrides the target adapter. Readiness intents store
    provider_name='pending_provider' (a real provider is only bound in a future
    OAuth phase), so a caller must name a provider to exercise an adapter;
    without one, a pending intent resolves to PROVIDER_UNSUPPORTED.
    """
    started = time.perf_counter()
    iid = intent_id if isinstance(intent_id, uuid.UUID) else uuid.UUID(str(intent_id))
    intent = await ir.get_intent(iid)
    agent_name = (intent or {}).get("agent_name")
    workspace_id = (intent or {}).get("workspace_id")

    # provider_execution_requested (#12) — recorded for every attempt.
    await write_trace(
        session_id=session_id, user_id=user_id, trace_type=TRACE_REQUESTED,
        status="ok", selected_agent=agent_name, tool_name=_TOOL,
        tool_result={"intent_id": str(iid), "simulate": simulate, "found": intent is not None},
        workspace_id=workspace_id,
    )

    def res(status, message, *, errors=None, simulated_result=None) -> ExecutionResult:
        return ExecutionResult(
            status=status, intent_id=str(iid),
            provider_name=(intent or {}).get("provider_name"),
            provider_type=(intent or {}).get("provider_type"),
            action_type=(intent or {}).get("action_type"),
            dry_run=bool((intent or {}).get("dry_run", True)),
            simulate=simulate, message=message, errors=errors or [],
            simulated_result=simulated_result,
            execution_enabled=guard.external_execution_enabled(),
        )

    async def finish(result, trace_type):
        return await _finish(
            result, trace_type=trace_type, user_id=user_id, session_id=session_id,
            agent_name=agent_name, workspace_id=workspace_id, started=started,
        )

    # #5/#6: framework only accepts intents from external_integration_intents.
    if intent is None or (not is_admin and intent.get("created_by") != user_id):
        return await finish(
            res(STATUS_VALIDATION_FAILED, "intent not found", errors=["intent not found"]),
            TRACE_VALIDATION_FAILED,
        )

    # The target provider: an explicit override, else the intent's stored value.
    resolved_provider = provider_name or intent.get("provider_name")
    provider_type = intent.get("provider_type")
    action_type = intent.get("action_type")

    def res(status, message, *, errors=None, simulated_result=None) -> ExecutionResult:  # noqa: F811
        return ExecutionResult(
            status=status, intent_id=str(iid), provider_name=resolved_provider,
            provider_type=provider_type, action_type=action_type,
            dry_run=bool(intent.get("dry_run", True)), simulate=simulate,
            message=message, errors=errors or [], simulated_result=simulated_result,
            execution_enabled=guard.external_execution_enabled(),
        )

    # provider_name supported?
    adapter = adapters.get_adapter(resolved_provider)
    if adapter is None:
        return await finish(
            res(STATUS_PROVIDER_UNSUPPORTED,
                f"provider {resolved_provider!r} is not supported"),
            TRACE_VALIDATION_FAILED,
        )
    # provider_type must match the adapter's declared type.
    if adapter.provider_type != provider_type:
        return await finish(
            res(STATUS_PROVIDER_UNSUPPORTED,
                f"provider_type {provider_type!r} does not match adapter {adapter.name!r}"),
            TRACE_VALIDATION_FAILED,
        )
    # action_type supported?
    if action_type not in adapter.supported_actions:
        return await finish(
            res(STATUS_ACTION_UNSUPPORTED, f"action {action_type!r} not supported by {adapter.name}"),
            TRACE_VALIDATION_FAILED,
        )
    # status approved/ready + confirmation respected (#6).
    if intent.get("status") not in READY_STATUSES:
        return await finish(
            res(STATUS_VALIDATION_FAILED,
                f"intent status {intent.get('status')!r} is not ready (must be 'confirmed')",
                errors=["intent is not in an approved/ready state"]),
            TRACE_VALIDATION_FAILED,
        )
    if intent.get("requires_confirmation"):
        return await finish(
            res(STATUS_VALIDATION_FAILED, "intent still requires confirmation",
                errors=["requires_confirmation is true"]),
            TRACE_VALIDATION_FAILED,
        )
    # required payload fields (#6).
    payload = _build_execution_payload(intent)
    perrors = adapter.validate_payload(action_type, payload)
    if perrors:
        return await finish(
            res(STATUS_VALIDATION_FAILED, "payload validation failed", errors=perrors),
            TRACE_VALIDATION_FAILED,
        )

    # ---- Validated. Split on mode. ----
    if simulate:
        # Dry-run only — adapter performs nothing external.
        sim = adapter.execute(action_type, payload, dry_run=True)
        return await finish(
            res(STATUS_SIMULATED,
                f"Simulated {action_type} via {adapter.name}; no real provider call.",
                simulated_result=sim),
            TRACE_SIMULATED,
        )

    # Real execution requested → consult the v0.8 kill switch FIRST (#7/#8).
    try:
        await guard.assert_external_execution_allowed(
            action_type, provider_type, user_id=user_id, workspace_id=workspace_id,
            intent=intent, agent_name=agent_name, session_id=session_id,
            block_message=guard.CONFIRMED_BLOCKED_MESSAGE,
        )
    except guard.ExecutionBlocked as exc:
        status = (
            STATUS_EXECUTION_NOT_ENABLED
            if not guard.external_execution_enabled()
            else STATUS_BLOCKED
        )
        return await finish(
            res(status, str(exc), errors=list(exc.result.blockers)),
            TRACE_BLOCKED,
        )

    # Unreachable while EXTERNAL_EXECUTION_ENABLED is false. Never call a real
    # provider in this phase — refuse defensively.
    return await finish(
        res(STATUS_BLOCKED, "real execution reached unexpectedly; refusing real provider call"),
        TRACE_BLOCKED,
    )
