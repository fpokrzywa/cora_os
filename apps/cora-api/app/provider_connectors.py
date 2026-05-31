"""External provider connector layer (v0.5) — SCAFFOLDING / DRY-RUN ONLY.

Defines the connector contract, capability metadata, and dry-run execution shape
for FUTURE Gmail / Outlook / Google Calendar / Microsoft Calendar integrations.

HARD SAFETY INVARIANT: nothing in this module performs a live external action.
No connector here opens an OAuth flow, sends email, creates a calendar event,
sends an invite, or reads an inbox/calendar. `execute_dry_run` is the ONLY
execution entry point and it provably contacts nothing. Real provider execution
must be added in a future module behind explicit confirmation, governance, and
OAuth — see LIVE_EXECUTION_ENABLED below.
"""

import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional

from app.clients import clients

logger = logging.getLogger(__name__)

# Master kill-switch. v0.5 is dry-run only; this stays False. A future module
# may flip a per-provider live path, but ONLY behind confirmation + OAuth +
# governance — never by toggling this constant alone.
LIVE_EXECUTION_ENABLED = False

PROVIDER_EMAIL = "email"
PROVIDER_CALENDAR = "calendar"
PROVIDER_NOTIFICATION = "notification"

INTERNAL_PREVIEW_PROVIDERS = {"internal_preview_email", "internal_preview_calendar"}

DRY_RUN_MESSAGE = "No external action was performed."

_COLS = (
    "id, provider_name, provider_type, display_name, description, enabled, "
    "dry_run_only, supports_send, supports_draft, supports_calendar_create, "
    "supports_calendar_update, supports_read, requires_oauth, "
    "auth_config_schema, payload_schema, capabilities, metadata, "
    "created_at, updated_at"
)


class ProviderError(Exception):
    """code: not_found (404) | invalid (400) | disabled (409) | unavailable (503)."""

    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProviderCapability:
    provider_name: str
    provider_type: str
    enabled: bool
    dry_run_only: bool
    supports_send: bool
    supports_draft: bool
    supports_calendar_create: bool
    supports_calendar_update: bool
    supports_read: bool
    requires_oauth: bool

    def as_dict(self) -> dict:
        return {
            "provider_name": self.provider_name,
            "provider_type": self.provider_type,
            "enabled": self.enabled,
            "dry_run_only": self.dry_run_only,
            "supports_send": self.supports_send,
            "supports_draft": self.supports_draft,
            "supports_calendar_create": self.supports_calendar_create,
            "supports_calendar_update": self.supports_calendar_update,
            "supports_read": self.supports_read,
            "requires_oauth": self.requires_oauth,
        }


@dataclass
class ProviderValidationResult:
    hard_errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    capability: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return not self.hard_errors

    def as_dict(self) -> dict:
        return {
            "hard_errors": self.hard_errors,
            "warnings": self.warnings,
            "hard_error_count": len(self.hard_errors),
            "warning_count": len(self.warnings),
            "ok": self.ok,
            "capability": self.capability,
        }


# Result of a dry-run execution. external_action_performed is ALWAYS False here.
@dataclass
class PayloadPreviewResult:
    provider_name: str
    provider_type: str
    payload: dict
    dry_run: bool = True
    external_action_performed: bool = False
    message: str = DRY_RUN_MESSAGE

    def as_dict(self) -> dict:
        return {
            "provider_name": self.provider_name,
            "provider_type": self.provider_type,
            "payload": self.payload,
            "dry_run": True,  # invariant
            "external_action_performed": False,  # invariant
            "message": self.message,
        }


def _capability_from_row(row: dict) -> ProviderCapability:
    return ProviderCapability(
        provider_name=row["provider_name"],
        provider_type=row["provider_type"],
        enabled=row["enabled"],
        dry_run_only=row["dry_run_only"],
        supports_send=row["supports_send"],
        supports_draft=row["supports_draft"],
        supports_calendar_create=row["supports_calendar_create"],
        supports_calendar_update=row["supports_calendar_update"],
        supports_read=row["supports_read"],
        requires_oauth=row["requires_oauth"],
    )


# ---------------------------------------------------------------------------
# Connector contract
# ---------------------------------------------------------------------------


class ProviderConnector:
    """Base connector contract. Subclasses MUST NOT call external APIs in v0.5."""

    def __init__(self, row: dict):
        self.row = row
        self.provider_name: str = row["provider_name"]
        self.provider_type: str = row["provider_type"]
        self.dry_run_only: bool = row["dry_run_only"]
        self.capability = _capability_from_row(row)

    def validate_payload(self, intent: dict) -> ProviderValidationResult:  # pragma: no cover - overridden
        raise NotImplementedError

    def build_payload_preview(self, intent: dict) -> dict:  # pragma: no cover - overridden
        raise NotImplementedError

    def execute_dry_run(self, intent: dict) -> dict:
        """The ONLY execution entry point. Provably performs no external action."""
        # Defensive: even if a future bug enabled live execution globally, a
        # dry_run_only connector must never escalate. v0.5 has no live branch.
        if LIVE_EXECUTION_ENABLED and not self.dry_run_only:  # pragma: no cover
            raise ProviderError(
                "live execution is not implemented in this module", code="invalid"
            )
        preview = self.build_payload_preview(intent)
        return PayloadPreviewResult(
            provider_name=self.provider_name,
            provider_type=self.provider_type,
            payload=preview,
        ).as_dict()


class EmailProviderConnector(ProviderConnector):
    def validate_payload(self, intent: dict) -> ProviderValidationResult:
        res = ProviderValidationResult(capability=self.capability.as_dict())
        if self.provider_type != PROVIDER_EMAIL:
            res.hard_errors.append("provider does not support email")
            return res
        payload = intent.get("payload_preview") or {}
        if not (payload.get("subject") or "").strip():
            res.hard_errors.append("subject is required")
        if not (payload.get("body") or "").strip():
            res.hard_errors.append("body is required")
        if not payload.get("to"):
            res.warnings.append(
                "no real recipients resolved (recipient_hint is informational only)"
            )
        return res

    def build_payload_preview(self, intent: dict) -> dict:
        p = intent.get("payload_preview") or {}
        return {
            "to": list(p.get("to") or []),
            "cc": list(p.get("cc") or []),
            "bcc": list(p.get("bcc") or []),
            "subject": p.get("subject") or "",
            "body": p.get("body") or "",
            "body_format": p.get("body_format") or "text",
            "recipient_hint": p.get("recipient_hint"),
            "source_draft_id": p.get("source_draft_id") or str(intent.get("source_id")),
            "_provider": self.provider_name,
            "_dry_run": True,
        }


class CalendarProviderConnector(ProviderConnector):
    def validate_payload(self, intent: dict) -> ProviderValidationResult:
        res = ProviderValidationResult(capability=self.capability.as_dict())
        if self.provider_type != PROVIDER_CALENDAR:
            res.hard_errors.append("provider does not support calendar")
            return res
        payload = intent.get("payload_preview") or {}
        if not (payload.get("title") or "").strip():
            res.hard_errors.append("title is required")
        action = intent.get("action_type")
        if not payload.get("start_time") or not payload.get("end_time"):
            # update needs a concrete time window; create can warn-only.
            if action == "update_calendar_event":
                res.hard_errors.append("start/end time required to update an event")
            else:
                res.warnings.append("start/end time incomplete")
        if not payload.get("attendees"):
            res.warnings.append("no attendees listed")
        return res

    def build_payload_preview(self, intent: dict) -> dict:
        p = intent.get("payload_preview") or {}
        return {
            "title": p.get("title") or "",
            "description": p.get("description") or "",
            "start_time": p.get("start_time"),
            "end_time": p.get("end_time"),
            "timezone": p.get("timezone"),
            "attendees": list(p.get("attendees") or []),
            "agenda": list(p.get("agenda") or []),
            "reminders": list(p.get("reminders") or []),
            "source_proposal_id": p.get("source_proposal_id")
            or str(intent.get("source_id")),
            "_provider": self.provider_name,
            "_dry_run": True,
        }


def _connector_for_row(row: dict) -> ProviderConnector:
    if row["provider_type"] == PROVIDER_EMAIL:
        return EmailProviderConnector(row)
    if row["provider_type"] == PROVIDER_CALENDAR:
        return CalendarProviderConnector(row)
    # notification / unknown → base connector with a passthrough preview
    return ProviderConnector(row)


# ---------------------------------------------------------------------------
# Registry access
# ---------------------------------------------------------------------------


def _require_pool():
    if clients.db_pool is None:
        raise ProviderError("Postgres pool unavailable", code="unavailable")
    return clients.db_pool


async def get_connector_row(provider_name: str) -> Optional[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_COLS} FROM external_provider_connectors WHERE provider_name = $1",
            provider_name,
        )
    return dict(row) if row else None


async def list_available_connectors() -> list[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_COLS} FROM external_provider_connectors ORDER BY provider_name"
        )
    return [dict(r) for r in rows]


async def get_connector(provider_name: str) -> ProviderConnector:
    row = await get_connector_row(provider_name)
    if row is None:
        raise ProviderError(f"unknown provider {provider_name!r}", code="not_found")
    return _connector_for_row(row)


def validate_payload_for_provider(
    connector: ProviderConnector, intent: dict
) -> ProviderValidationResult:
    return connector.validate_payload(intent)


def render_provider_payload_preview(
    connector: ProviderConnector, intent: dict
) -> dict:
    return connector.build_payload_preview(intent)


def execute_provider_action_dry_run(
    connector: ProviderConnector, intent: dict
) -> dict:
    """Dry-run only. Returns a preview + dry_run/external_action_performed flags.
    Performs NO external call. This is the single execution entry point."""
    return connector.execute_dry_run(intent)


def assert_selectable(row: dict) -> None:
    """A provider may be selected for an intent only if it is enabled, OR it is
    an internal_preview provider (which is always selectable, dry-run)."""
    if row["provider_name"] in INTERNAL_PREVIEW_PROVIDERS:
        return
    if not row["enabled"]:
        raise ProviderError(
            f"provider {row['provider_name']!r} is disabled", code="disabled"
        )


async def update_connector(
    provider_name: str, fields: dict
) -> Optional[dict]:
    """Admin update of a connector. SAFETY: live WRITE-execution capability flags
    can never be turned on here — supports_send / supports_calendar_create /
    supports_calendar_update are forced FALSE and dry_run_only forced TRUE
    regardless of input. supports_read (read-only inbox capability, v2.3) is NOT
    force-reset here — it is a distinct, intentionally-supported read capability set
    at the seed/migration level and is preserved across admin edits. Read-only
    capability does NOT imply send: write actions stay disabled and runtime inbox
    read is still gated by OAuth scope + the inbox_read feature flag."""
    editable = ("enabled", "dry_run_only", "display_name", "description",
                "metadata", "capabilities")
    sets: list[str] = []
    args: list = []
    for col in editable:
        if col in fields and fields[col] is not None:
            value = fields[col]
            if col == "dry_run_only":
                value = True  # forced: no live execution in v0.5
            args.append(value)
            sets.append(f"{col} = ${len(args)}")
    # Hard guard: WRITE capabilities stay FALSE no matter what an admin sends.
    # (supports_read is intentionally NOT forced — it is preserved as set by seed.)
    sets.append("supports_send = FALSE")
    sets.append("supports_calendar_create = FALSE")
    sets.append("supports_calendar_update = FALSE")
    sets.append("dry_run_only = TRUE")
    if not args and len(sets) == 4:
        # nothing editable supplied; still return current row
        return await get_connector_row(provider_name)
    sets.append("updated_at = NOW()")
    args.append(provider_name)
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE external_provider_connectors SET {', '.join(sets)} "
            f"WHERE provider_name = ${len(args)} RETURNING {_COLS}",
            *args,
        )
    return dict(row) if row else None
