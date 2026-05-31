"""External Provider Execution Adapter Skeleton v1.6 — defines how future external
execution WILL be handled; executes nothing now.

Each adapter declares its provider/action surface and can VALIDATE + BUILD +
SIMULATE a provider-shaped payload (data only). `execute()` runs the v1.5 final
safety interlock and then ALWAYS returns blocked (blocked_by_governance /
provider_execution_disabled) — no real Gmail / Outlook / Google / Microsoft call
is ever made, and no access/refresh token is ever read or exposed. The registry
resolves by (provider_name, action_type) and FAILS CLOSED when no adapter exists.

This is distinct from the v1.0 `provider_adapters.py` stubs (whose execute()
RAISES and which back the v1.0 execution framework + v1.3 simulation); this module
is the richer forward-looking execution skeleton with build/simulate/blocked-execute
+ audit, used by the Execution Runbook's Adapter Readiness section.
"""

import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional

from app.clients import clients
from app import feature_flags as ff
from app import final_interlock as fi
from app import integration_readiness as ir
from app import provider_adapters as v10  # REQUIRED_FIELDS + action constants
from app import provider_credential_simulation as pcs
from app import provider_execution as pexec  # neutral -> adapter-field mapping
from app.runtime_traces import write_trace

logger = logging.getLogger(__name__)

ACTION_SEND_EMAIL = v10.ACTION_SEND_EMAIL
ACTION_CREATE_CALENDAR_EVENT = v10.ACTION_CREATE_CALENDAR_EVENT

# execute() outcome (spec #3)
BLOCKED_STATUS = "blocked_by_governance"
BLOCKED_REASON = "provider_execution_disabled"

# audit events (spec #10)
EV_RESOLVED = "adapter_resolved"
EV_VALIDATED = "adapter_payload_validated"
EV_SIMULATED = "adapter_simulated"
EV_BLOCKED = "adapter_execution_blocked"

# runtime traces (spec #11)
TRACE_RESOLVED = "provider_adapter_resolved"
TRACE_SIMULATED = "provider_adapter_simulated"
TRACE_BLOCKED = "provider_adapter_execution_blocked"

_TOOL = "provider_adapter"


class AdapterError(Exception):
    """code: not_found (404) | invalid (400) | unavailable (503)."""

    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


@dataclass
class ProviderExecutionAdapter:
    provider_name: str
    provider_type: str
    supported_action_types: frozenset
    # provider API method names per action — for the SIMULATED request shape only.
    api_methods: dict = field(default_factory=dict)

    def supports(self, action_type: Optional[str]) -> bool:
        return action_type in self.supported_action_types

    def validate_payload(self, action_type: str, payload: dict) -> list[str]:
        """Required-field validation. Empty list = valid."""
        if not self.supports(action_type):
            return [f"action {action_type!r} not supported by {self.provider_name}"]
        errors: list[str] = []
        for f in v10.REQUIRED_FIELDS.get(action_type, ()):
            val = payload.get(f)
            if val is None or (isinstance(val, (str, list, dict, tuple)) and len(val) == 0):
                errors.append(f"missing required field: {f}")
        return errors

    def build_provider_payload(self, action_type: str, payload: dict) -> dict:
        """Provider-shaped request that a FUTURE executor would send. DATA ONLY —
        this is never transmitted; it carries no token and triggers no call."""
        method = self.api_methods.get(action_type, "(unmapped)")
        if action_type == ACTION_SEND_EMAIL:
            request = {
                "to": list(payload.get("to") or []),
                "cc": list(payload.get("cc") or []),
                "bcc": list(payload.get("bcc") or []),
                "subject": payload.get("subject") or "",
                "body_preview": (payload.get("body") or "")[:280],
            }
        elif action_type == ACTION_CREATE_CALENDAR_EVENT:
            request = {
                "title": payload.get("title") or "",
                "start_time": payload.get("start_time"),
                "end_time": payload.get("end_time"),
                "attendees": list(payload.get("attendees") or []),
                "description_preview": (payload.get("description") or "")[:280],
                "timezone": payload.get("timezone"),
            }
        else:
            request = dict(payload)
        return {
            "provider_name": self.provider_name,
            "provider_type": self.provider_type,
            "action_type": action_type,
            "api_method": method,
            "simulated": True,
            "would_send": False,
            "request": request,
        }

    def simulate(self, action_type: str, payload: dict) -> dict:
        """Simulation-only payload generation (spec #5). Calls nothing external."""
        errors = self.validate_payload(action_type, payload)
        return {
            "simulated": True,
            "external_action_performed": False,
            "provider_name": self.provider_name,
            "action_type": action_type,
            "validation_errors": errors,
            "payload_ready": not errors,
            "provider_request": self.build_provider_payload(action_type, payload),
            "note": (
                f"Simulated {action_type} request shape for {self.provider_name}; "
                "no provider API was called."
            ),
        }

    async def execute(self, intent: dict, *, user_id: uuid.UUID, is_admin: bool) -> dict:
        """ALWAYS blocked in this phase (spec #3). Runs the v1.5 final safety
        interlock first (spec #4), then refuses — no real provider call is ever
        reached. Returns blocked status; never raises to a real execution path."""
        interlock = await fi.run_final_safety_check(
            intent["id"], user_id=user_id, is_admin=is_admin,
        )
        return {
            "status": BLOCKED_STATUS,
            "reason": BLOCKED_REASON,
            "real_execution_performed": False,
            "provider_name": self.provider_name,
            "provider_type": self.provider_type,
            "action_type": intent.get("action_type"),
            "interlock_status": interlock["status"],
            "real_execution_allowed": interlock["real_execution_allowed"],  # False
            "note": (
                "Provider execution is disabled. The adapter ran the final safety "
                "interlock and refused — no Gmail/Outlook/Google/Microsoft API was "
                "called and no token was used."
            ),
        }

    def describe(self) -> dict:
        return {
            "provider_name": self.provider_name,
            "provider_type": self.provider_type,
            "supported_action_types": sorted(self.supported_action_types),
            "api_methods": self.api_methods,
            "real_execution": False,
        }


class GmailEmailAdapter(ProviderExecutionAdapter):
    def __init__(self):
        super().__init__(
            provider_name="gmail", provider_type="email",
            supported_action_types=frozenset({ACTION_SEND_EMAIL}),
            api_methods={ACTION_SEND_EMAIL: "gmail.users.messages.send"},
        )


class OutlookMailAdapter(ProviderExecutionAdapter):
    def __init__(self):
        super().__init__(
            provider_name="outlook_mail", provider_type="email",
            supported_action_types=frozenset({ACTION_SEND_EMAIL}),
            api_methods={ACTION_SEND_EMAIL: "graph.me.sendMail"},
        )


class GoogleCalendarAdapter(ProviderExecutionAdapter):
    def __init__(self):
        super().__init__(
            provider_name="google_calendar", provider_type="calendar",
            supported_action_types=frozenset({ACTION_CREATE_CALENDAR_EVENT}),
            api_methods={ACTION_CREATE_CALENDAR_EVENT: "calendar.events.insert"},
        )


class MicrosoftCalendarAdapter(ProviderExecutionAdapter):
    def __init__(self):
        super().__init__(
            provider_name="microsoft_calendar", provider_type="calendar",
            supported_action_types=frozenset({ACTION_CREATE_CALENDAR_EVENT}),
            api_methods={ACTION_CREATE_CALENDAR_EVENT: "graph.me.events"},
        )


_ADAPTERS: dict[str, ProviderExecutionAdapter] = {
    a.provider_name: a for a in (
        GmailEmailAdapter(), OutlookMailAdapter(),
        GoogleCalendarAdapter(), MicrosoftCalendarAdapter(),
    )
}
# The credential vault / connectors store the Microsoft calendar provider as
# "outlook_calendar"; resolve it to the MicrosoftCalendarAdapter.
_ALIASES = {"outlook_calendar": "microsoft_calendar"}


def resolve_adapter(provider_name: Optional[str], action_type: Optional[str]
                    ) -> Optional[ProviderExecutionAdapter]:
    """Resolve by (provider_name, action_type). FAILS CLOSED (spec #8): returns
    None when the provider is unknown or the action is unsupported."""
    key = _ALIASES.get((provider_name or "").strip().lower(),
                       (provider_name or "").strip().lower())
    adapter = _ADAPTERS.get(key)
    if adapter is None or not adapter.supports(action_type):
        return None
    return adapter


def list_adapters() -> list[dict]:
    return [a.describe() for a in _ADAPTERS.values()]


# --------------------------------------------------------------------------- #
# Service layer — ties an adapter to an intent + writes audit events + traces.
# --------------------------------------------------------------------------- #

def _require_pool():
    if clients.db_pool is None:
        raise AdapterError("Postgres pool unavailable", code="unavailable")
    return clients.db_pool


async def _visible_intent(intent_id: uuid.UUID, *, user_id: uuid.UUID, is_admin: bool) -> dict:
    intent = await ir.get_intent(intent_id)
    if intent is None or (not is_admin and intent["created_by"] != user_id):
        raise AdapterError("intent not found", code="not_found")
    return intent


async def _event(conn, intent_id, user_id, *, event_type, snapshot, status):
    await ir._insert_event(
        conn, intent_id, user_id, event_type=event_type,
        from_status=status, to_status=status, notes=None, payload_snapshot=snapshot,
    )


async def _trace(intent, user_id, *, trace_type, status, result, error=None):
    await write_trace(
        session_id=None, user_id=user_id, trace_type=trace_type, status=status,
        selected_agent=intent.get("agent_name"), tool_name=_TOOL,
        tool_result=result, error_message=error, workspace_id=intent.get("workspace_id"),
    )


async def _resolve_for_intent(intent: dict, *, user_id: uuid.UUID):
    """Resolve the adapter for an intent via its CONNECTED provider (from the v1.3
    credential snapshot) + the intent's action_type. Writes adapter_resolved
    audit + trace. Returns (adapter | None, provider_name, action_type, payload)."""
    snap = await pcs.credential_snapshot(intent, user_id=user_id)
    provider_name = snap.get("provider_name")
    action_type = intent.get("action_type")
    adapter = resolve_adapter(provider_name, action_type)
    payload = pexec._build_execution_payload(intent)
    resolved = adapter is not None
    await _trace(intent, user_id, trace_type=TRACE_RESOLVED,
                 status="ok" if resolved else "blocked",
                 result={"intent_id": str(intent["id"]), "provider_name": provider_name,
                         "action_type": action_type, "resolved": resolved,
                         "fail_closed": not resolved})
    pool = _require_pool()
    async with pool.acquire() as conn:
        await _event(conn, intent["id"], user_id, event_type=EV_RESOLVED,
                     status=intent.get("status"),
                     snapshot={"provider_name": provider_name, "action_type": action_type,
                               "resolved": resolved})
    return adapter, provider_name, action_type, payload


async def simulate_adapter_payload(intent_id: uuid.UUID, *, user_id: uuid.UUID,
                                   is_admin: bool) -> dict:
    """Validate + simulate the adapter's provider-shaped payload for an intent.
    Calls nothing external. Writes adapter_payload_validated + adapter_simulated
    events and a provider_adapter_simulated trace."""
    intent = await _visible_intent(intent_id, user_id=user_id, is_admin=is_admin)
    adapter, provider_name, action_type, payload = await _resolve_for_intent(intent, user_id=user_id)
    pool = _require_pool()
    if adapter is None:
        # Fail closed — no adapter resolved.
        result = {
            "intent_id": str(intent_id), "resolved": False,
            "provider_name": provider_name, "action_type": action_type,
            "status": "no_adapter",
            "reason": "no execution adapter for this provider/action (fail-closed)",
        }
        await _trace(intent, user_id, trace_type=TRACE_SIMULATED, status="failed",
                     result=result, error="no adapter resolved")
        return result

    errors = adapter.validate_payload(action_type, payload)
    sim = adapter.simulate(action_type, payload)
    async with pool.acquire() as conn:
        await _event(conn, intent_id, user_id, event_type=EV_VALIDATED,
                     status=intent.get("status"),
                     snapshot={"provider_name": provider_name, "action_type": action_type,
                               "valid": not errors, "validation_errors": errors})
        await _event(conn, intent_id, user_id, event_type=EV_SIMULATED,
                     status=intent.get("status"),
                     snapshot={"provider_name": provider_name, "action_type": action_type,
                               "payload_ready": sim["payload_ready"],
                               "api_method": sim["provider_request"]["api_method"]})
    result = {
        "intent_id": str(intent_id), "resolved": True,
        "provider_name": provider_name, "provider_type": adapter.provider_type,
        "action_type": action_type, "supported_action": adapter.supports(action_type),
        "validation_errors": errors, "payload_ready": sim["payload_ready"],
        "simulation": sim, "external_action_performed": False,
    }
    await _trace(intent, user_id, trace_type=TRACE_SIMULATED,
                 status="ok" if sim["payload_ready"] else "failed", result={
                     "intent_id": str(intent_id), "provider_name": provider_name,
                     "action_type": action_type, "payload_ready": sim["payload_ready"],
                     "api_method": sim["provider_request"]["api_method"]})
    return result


async def run_blocked_execution_check(intent_id: uuid.UUID, *, user_id: uuid.UUID,
                                      is_admin: bool) -> dict:
    """Run the adapter execute() path — which runs the final interlock and ALWAYS
    returns blocked. Writes adapter_execution_blocked event + a
    provider_adapter_execution_blocked trace. Nothing external is called."""
    intent = await _visible_intent(intent_id, user_id=user_id, is_admin=is_admin)
    adapter, provider_name, action_type, _ = await _resolve_for_intent(intent, user_id=user_id)
    # Consult the v1.7 feature flag matrix BEFORE the execution path (spec #4).
    # Fail-closed: a missing/disabled flag denies (and audits) the execution.
    flag_decision = await ff.evaluate(
        provider_name, action_type, user_id=user_id, intent=intent,
    )
    pool = _require_pool()
    if adapter is None:
        result = {
            "intent_id": str(intent_id), "status": BLOCKED_STATUS,
            "reason": "no execution adapter for this provider/action (fail-closed)",
            "real_execution_performed": False, "resolved": False,
            "provider_name": provider_name, "action_type": action_type,
            "feature_flag": flag_decision,
        }
        async with pool.acquire() as conn:
            await _event(conn, intent_id, user_id, event_type=EV_BLOCKED,
                         status=intent.get("status"),
                         snapshot={"resolved": False, "status": BLOCKED_STATUS})
        await _trace(intent, user_id, trace_type=TRACE_BLOCKED, status="blocked",
                     result=result, error=BLOCKED_REASON)
        return result

    result = await adapter.execute(intent, user_id=user_id, is_admin=is_admin)
    result["intent_id"] = str(intent_id)
    result["feature_flag"] = flag_decision
    async with pool.acquire() as conn:
        await _event(conn, intent_id, user_id, event_type=EV_BLOCKED,
                     status=intent.get("status"),
                     snapshot={"provider_name": provider_name, "action_type": action_type,
                               "status": result["status"], "reason": result["reason"],
                               "real_execution_performed": False,
                               "interlock_status": result.get("interlock_status")})
    await _trace(intent, user_id, trace_type=TRACE_BLOCKED, status="blocked", result={
        "intent_id": str(intent_id), "provider_name": provider_name,
        "action_type": action_type, "status": result["status"],
        "reason": result["reason"], "real_execution_allowed": result.get("real_execution_allowed"),
    }, error=BLOCKED_REASON)
    return result
