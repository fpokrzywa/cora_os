"""Provider Execution Framework v1.0 — adapter contract + STUB adapters.

Each adapter implements the provider contract (name, provider_type,
supported_actions, validate_payload, execute) but makes NO real provider call.
`execute` only ever returns a *simulated* result for dry_run; a real execution
(dry_run=False) raises — real external execution is disabled by the v0.8 kill
switch and unimplemented in this phase. No Gmail / Outlook / Google Calendar /
Microsoft Graph client is imported or called anywhere here.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

ACTION_SEND_EMAIL = "send_email"
ACTION_CREATE_CALENDAR_EVENT = "create_calendar_event"

# Required payload fields per action (spec validation expectations). Optional
# fields (attendees/location/description) are not enforced.
REQUIRED_FIELDS = {
    ACTION_SEND_EMAIL: ("to", "subject", "body"),
    ACTION_CREATE_CALENDAR_EVENT: ("title", "start_time", "end_time"),
}


class ProviderAdapterError(Exception):
    """Raised when a stub adapter is asked to perform a real (non-dry-run) call."""


class ProviderAdapter:
    """Provider adapter contract. Subclasses set name/provider_type/
    supported_actions. validate_payload + execute are generic over the action."""

    name: str = ""
    provider_type: str = ""
    supported_actions: frozenset = frozenset()

    def validate_payload(self, action_type: str, payload: dict) -> list[str]:
        """Return a list of validation error strings (empty list = valid)."""
        if action_type not in self.supported_actions:
            return [f"action {action_type!r} not supported by {self.name}"]
        errors: list[str] = []
        for field_name in REQUIRED_FIELDS.get(action_type, ()):
            val = payload.get(field_name)
            empty = val is None or (
                isinstance(val, (str, list, dict, tuple)) and len(val) == 0
            )
            if empty:
                errors.append(f"missing required field: {field_name}")
        return errors

    def execute(self, action_type: str, payload: dict, dry_run: bool) -> dict:
        """STUB execution. Only a dry_run simulation is permitted; a real call is
        refused. Returns simulated data only — performs nothing external."""
        if not dry_run:
            # Defense in depth: the kill switch blocks before this is reached.
            raise ProviderAdapterError(
                f"{self.name}: real external execution is disabled in this phase"
            )
        fields = REQUIRED_FIELDS.get(action_type, ())
        return {
            "simulated": True,
            "external_action_performed": False,
            "provider": self.name,
            "provider_type": self.provider_type,
            "action_type": action_type,
            "preview": {k: payload.get(k) for k in fields},
            "note": (
                f"Simulated {action_type} via {self.name}; no real provider API "
                "was called."
            ),
        }


class GmailAdapter(ProviderAdapter):
    name = "gmail"
    provider_type = "email"
    supported_actions = frozenset({ACTION_SEND_EMAIL})


class OutlookMailAdapter(ProviderAdapter):
    name = "outlook_mail"
    provider_type = "email"
    supported_actions = frozenset({ACTION_SEND_EMAIL})


class GoogleCalendarAdapter(ProviderAdapter):
    name = "google_calendar"
    provider_type = "calendar"
    supported_actions = frozenset({ACTION_CREATE_CALENDAR_EVENT})


class OutlookCalendarAdapter(ProviderAdapter):
    name = "outlook_calendar"
    provider_type = "calendar"
    supported_actions = frozenset({ACTION_CREATE_CALENDAR_EVENT})


ADAPTERS: dict[str, ProviderAdapter] = {
    a.name: a
    for a in (
        GmailAdapter(),
        OutlookMailAdapter(),
        GoogleCalendarAdapter(),
        OutlookCalendarAdapter(),
    )
}


def get_adapter(provider_name: Optional[str]) -> Optional[ProviderAdapter]:
    return ADAPTERS.get((provider_name or "").strip().lower())


def list_adapters() -> list[dict]:
    return [
        {
            "name": a.name,
            "provider_type": a.provider_type,
            "supported_actions": sorted(a.supported_actions),
            "real_execution": False,
        }
        for a in ADAPTERS.values()
    ]
