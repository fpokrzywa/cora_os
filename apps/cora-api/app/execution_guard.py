"""External Execution Kill Switch / Safety Guard (v0.8).

A single global gate that blocks ALL external provider execution unless it is
explicitly enabled by configuration. In this phase EXTERNAL_EXECUTION_ENABLED is
false, so every external action is blocked: nothing calls Gmail / Outlook /
Google Calendar / Microsoft Graph or any provider API, dry_run stays true, and a
blocked attempt is audited (tool_execution_logs) + traced (runtime_traces).

This module performs NO provider call and NO token exchange — it only decides
and records. It never sets dry_run=false and never enables execution.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Optional

from app.config import settings
from app.runtime_traces import write_trace
from app.tools.governance import is_external_execution_tool, log_execution_attempt

logger = logging.getLogger(__name__)

BLOCKED_TOOL = "external_execution_blocked"
BLOCKED_TRACE = "external_execution_blocked"

# Audit/trace message (spec #6/#7).
BLOCKED_MESSAGE = "External execution disabled by global safety guard"
# User-facing message when an execute attempt is made on a confirmed intent (#9).
CONFIRMED_BLOCKED_MESSAGE = (
    "This intent is confirmed, but external execution is disabled by the global "
    "safety guard."
)

# Actions that constitute external provider execution (spec #3).
EXECUTABLE_ACTIONS = frozenset({
    "send_email",
    "send_gmail",
    "send_outlook_email",
    "create_calendar_event",
    "create_google_calendar_event",
    "create_outlook_calendar_event",
    "execute_integration_intent",
})


def external_execution_enabled() -> bool:
    """Single source of truth. Default false; only true if explicitly configured
    via EXTERNAL_EXECUTION_ENABLED."""
    return bool(settings.external_execution_enabled)


class ExecutionBlocked(Exception):
    """Raised when an external execution attempt is blocked by the guard."""

    def __init__(self, message: str, *, result: "GuardResult"):
        super().__init__(message)
        self.result = result


@dataclass
class GuardResult:
    allowed: bool
    reason: str
    checks: dict
    blockers: list


def evaluate_external_execution(
    action_type: Optional[str],
    provider_type: Optional[str],
    *,
    user_id: Optional[uuid.UUID] = None,
    workspace_id: Optional[uuid.UUID] = None,
    intent: Optional[dict] = None,
    provider_connected: bool = False,
    token_ready: bool = False,
) -> GuardResult:
    """Decide whether an external provider action may execute (spec #4). Returns
    blocked unless ALL conditions hold. The global flag is the master gate; the
    other conditions are reported so callers/UI can see exactly why a
    future-enabled action would still be gated. No provider call, no token
    exchange — pure evaluation."""
    flag = external_execution_enabled()
    checks = {
        "external_execution_enabled": flag,
        "intent_confirmed": bool(intent and intent.get("status") == "confirmed"),
        # In this phase dry_run is always TRUE, so this is always False → blocked.
        "dry_run_disabled": bool(intent and intent.get("dry_run") is False),
        "provider_connected": bool(provider_connected),
        "token_ready": bool(token_ready),
        # Governance hard-blocks the external send/create tools.
        "governance_allows": not is_external_execution_tool(action_type),
    }
    allowed = all(checks.values())
    blockers = [k for k, v in checks.items() if not v]
    reason = "external execution permitted" if allowed else BLOCKED_MESSAGE
    return GuardResult(allowed=allowed, reason=reason, checks=checks, blockers=blockers)


async def log_blocked(
    action_type: Optional[str],
    provider_type: Optional[str],
    *,
    user_id: Optional[uuid.UUID],
    workspace_id: Optional[uuid.UUID] = None,
    agent_name: Optional[str] = None,
    session_id: Optional[uuid.UUID] = None,
    intent_id: Optional[uuid.UUID] = None,
    result: Optional[GuardResult] = None,
) -> None:
    """Audit + trace a blocked external execution attempt (spec #6/#7)."""
    await log_execution_attempt(
        tool_name=BLOCKED_TOOL, agent_name=agent_name, session_id=session_id,
        user_id=user_id, scope_type=None, allowed=False,
        duration_ms=None, status="blocked", error_message=BLOCKED_MESSAGE,
    )
    await write_trace(
        session_id=session_id, user_id=user_id,
        trace_type=BLOCKED_TRACE, status="blocked",
        selected_agent=agent_name, tool_name=BLOCKED_TOOL,
        tool_result={
            "action_type": action_type,
            "provider_type": provider_type,
            "intent_id": str(intent_id) if intent_id else None,
            "blockers": result.blockers if result else None,
            "external_execution_enabled": external_execution_enabled(),
        },
        error_message=BLOCKED_MESSAGE,
        workspace_id=workspace_id,
    )


async def assert_external_execution_allowed(
    action_type: Optional[str],
    provider_type: Optional[str],
    *,
    user_id: Optional[uuid.UUID] = None,
    workspace_id: Optional[uuid.UUID] = None,
    intent: Optional[dict] = None,
    provider_connected: bool = False,
    token_ready: bool = False,
    agent_name: Optional[str] = None,
    session_id: Optional[uuid.UUID] = None,
    block_message: str = BLOCKED_MESSAGE,
) -> GuardResult:
    """Centralized guard (spec #4). Returns the GuardResult when execution is
    allowed; otherwise audits + traces the blocked attempt and raises
    ExecutionBlocked. In this phase it ALWAYS blocks (the global flag is false)."""
    result = evaluate_external_execution(
        action_type, provider_type, user_id=user_id, workspace_id=workspace_id,
        intent=intent, provider_connected=provider_connected, token_ready=token_ready,
    )
    if not result.allowed:
        await log_blocked(
            action_type, provider_type, user_id=user_id, workspace_id=workspace_id,
            agent_name=agent_name, session_id=session_id,
            intent_id=(intent or {}).get("id"), result=result,
        )
        raise ExecutionBlocked(block_message, result=result)
    return result


def execution_status() -> dict:
    """Status surface for the UI safety banner (spec #8)."""
    enabled = external_execution_enabled()
    return {
        "external_execution_enabled": enabled,
        "dry_run_enforced": not enabled,  # while disabled, dry_run stays true
        "execution_available": enabled,
        "message": (
            "External execution is enabled."
            if enabled
            else "External execution is disabled by the global safety guard."
        ),
    }
