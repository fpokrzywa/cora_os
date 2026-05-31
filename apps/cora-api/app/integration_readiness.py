"""External Integration Readiness service (v0.4) — DRY-RUN ONLY.

Prepares (never performs) a future external send/calendar action from an
APPROVED SIGNAL draft or CHRONOS proposal. There are NO live providers: the only
providers are `internal_preview_*`, which normalize a payload preview. Nothing
here contacts Gmail/Outlook/Google/Microsoft, sends email, writes a calendar,
sends invites, or reads an inbox. "confirmed" means confirmed *internally* only.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Optional

from app.clients import clients
from app import provider_connectors as pc

logger = logging.getLogger(__name__)


def _resolve_provider_name(provider_name: Optional[str], provider_type: str) -> str:
    """Map the generic 'internal_preview' (and empty) to the type-specific
    internal preview connector; otherwise pass the requested provider through."""
    if not provider_name or provider_name == "internal_preview":
        return f"internal_preview_{provider_type}"
    return provider_name

SAFETY_DRY_RUN = "This is a dry-run integration intent. No external action was performed."
SAFETY_CONFIRM = (
    "Confirmation is internal only until a real provider connector is added."
)

SOURCE_DRAFT = "communication_draft"
SOURCE_PROPOSAL = "schedule_proposal"

PROVIDER_EMAIL = "email"
PROVIDER_CALENDAR = "calendar"

# Statuses
STATUS_DRAFT = "draft"
STATUS_READY = "ready_for_confirmation"
STATUS_CONFIRMED = "confirmed"
STATUS_CONFIRMATION_REVOKED = "confirmation_revoked"
STATUS_BLOCKED = "blocked"
STATUS_CANCELLED = "cancelled"
STATUS_EXECUTED_PLACEHOLDER = "executed_placeholder"  # reserved; never set here

# Global hard guard (Execution Approval Gate v0.7). External execution is
# disabled in this phase — even a confirmed intent must NEVER execute. Confirm
# keeps dry_run TRUE and calls no provider; any future executor MUST gate on
# this flag. Single source of truth for the intent layer.
EXECUTION_ENABLED = False


def assert_execution_disabled() -> None:
    """Defensive invariant: raise if anything reaches an execution path while
    execution is globally disabled. Nothing calls a provider in this phase, so
    this should never be hit; it exists to fail loudly if that ever changes."""
    if not EXECUTION_ENABLED:
        raise IntegrationError(
            "external execution is globally disabled (execution_enabled=false)",
            code="forbidden",
        )

EMAIL_ACTIONS = {"send_email", "send_notification"}
CALENDAR_ACTIONS = {"create_calendar_event", "update_calendar_event"}

_COLS = (
    "id, workspace_id, created_by, source_type, source_id, agent_name, "
    "provider_type, provider_name, action_type, status, dry_run, "
    "requires_confirmation, confirmation_required_reason, payload_preview, "
    "validation_result, metadata, confirmed_by, confirmed_at, cancelled_by, "
    "cancelled_at, created_at, updated_at"
)


class IntegrationError(Exception):
    """code: invalid (400) | not_found (404) | forbidden (403) |
    conflict (409) | unavailable (503)."""

    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


def _require_pool():
    if clients.db_pool is None:
        raise IntegrationError("Postgres pool unavailable", code="unavailable")
    return clients.db_pool


# ---------------------------------------------------------------------------
# Provider abstraction (preview-only). NO real provider is wired.
# ---------------------------------------------------------------------------


def render_payload_preview(source_type: str, source_row: dict) -> dict:
    """Normalize an approved source record into a provider-neutral payload
    preview. This produces data only — it sends/creates nothing."""
    if source_type == SOURCE_DRAFT:
        recipients: list[str] = []
        hint = (source_row.get("recipient_hint") or "").strip()
        # recipient_hint is a free-text hint, NOT a real address — never treated
        # as a deliverable recipient in readiness mode.
        return {
            "to": recipients,
            "cc": [],
            "bcc": [],
            "subject": source_row.get("subject") or "",
            "body": source_row.get("body") or "",
            "body_format": "text",
            "recipient_hint": hint or None,
            "source_draft_id": str(source_row["id"]),
        }
    if source_type == SOURCE_PROPOSAL:
        return {
            "title": source_row.get("title") or "",
            "description": source_row.get("description") or "",
            "start_time": (
                source_row["start_time"].isoformat()
                if source_row.get("start_time")
                else None
            ),
            "end_time": (
                source_row["end_time"].isoformat()
                if source_row.get("end_time")
                else None
            ),
            "timezone": source_row.get("timezone"),
            "attendees": list(source_row.get("attendees") or []),
            "agenda": list(source_row.get("agenda") or []),
            "reminders": list(source_row.get("reminders") or []),
            "source_proposal_id": str(source_row["id"]),
        }
    raise IntegrationError(f"unknown source_type {source_type!r}")


# ---------------------------------------------------------------------------
# Validation — produces warnings/hard errors only; performs no external action.
# ---------------------------------------------------------------------------


def validate_payload(provider_type: str, payload: dict) -> dict:
    hard_errors: list[str] = []
    warnings: list[str] = []
    if provider_type == PROVIDER_EMAIL:
        if not (payload.get("subject") or "").strip():
            hard_errors.append("subject is required")
        if not (payload.get("body") or "").strip():
            hard_errors.append("body is required")
        if not payload.get("to"):
            warnings.append(
                "no real recipients resolved (recipient_hint is informational only)"
            )
    elif provider_type == PROVIDER_CALENDAR:
        if not (payload.get("title") or "").strip():
            hard_errors.append("title is required")
        if not payload.get("start_time") or not payload.get("end_time"):
            warnings.append("start/end time incomplete")
        if not payload.get("attendees"):
            warnings.append("no attendees listed")
    else:
        hard_errors.append(f"unknown provider_type {provider_type!r}")
    return {
        "hard_errors": hard_errors,
        "warnings": warnings,
        "hard_error_count": len(hard_errors),
        "warning_count": len(warnings),
        "ok": not hard_errors,
    }


def validate_integration_intent(intent_row: dict) -> dict:
    """Re-run validation against an intent's stored payload preview."""
    return validate_payload(
        intent_row["provider_type"], intent_row.get("payload_preview") or {}
    )


# ---------------------------------------------------------------------------
# Source fetch (approved-only gate)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _BuildSpec:
    source_type: str
    table: str
    agent_name: str
    provider_type: str
    allowed_actions: set
    select_cols: str


_DRAFT_SPEC = _BuildSpec(
    source_type=SOURCE_DRAFT,
    table="communication_drafts",
    agent_name="SIGNAL",
    provider_type=PROVIDER_EMAIL,
    allowed_actions=EMAIL_ACTIONS,
    select_cols="id, workspace_id, created_by, subject, body, recipient_hint, status",
)
_PROPOSAL_SPEC = _BuildSpec(
    source_type=SOURCE_PROPOSAL,
    table="schedule_proposals",
    agent_name="CHRONOS",
    provider_type=PROVIDER_CALENDAR,
    allowed_actions=CALENDAR_ACTIONS,
    select_cols=(
        "id, workspace_id, created_by, title, description, start_time, "
        "end_time, timezone, attendees, agenda, reminders, status"
    ),
)


async def _build_intent(
    spec: _BuildSpec,
    source_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    is_admin: bool,
    provider_name: str,
    action_type: str,
    notes: Optional[str],
) -> dict:
    if action_type not in spec.allowed_actions:
        raise IntegrationError(
            f"action_type {action_type!r} not valid for {spec.provider_type}",
            code="invalid",
        )
    pool = _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            src = await conn.fetchrow(
                f"SELECT {spec.select_cols} FROM {spec.table} WHERE id = $1",
                source_id,
            )
            if src is None:
                raise IntegrationError("source not found", code="not_found")
            src = dict(src)
            # Ownership: non-admins may only act on their own source record.
            if not is_admin and src["created_by"] != user_id:
                raise IntegrationError("source not found", code="not_found")
            # Approved-only gate: readiness intents are post-approval only.
            if src["status"] != "approved":
                raise IntegrationError(
                    "source must be approved before preparing an integration intent",
                    code="conflict",
                )

            # Resolve + validate the provider connector. "internal_preview" maps
            # to the type-specific internal preview connector. Disabled real
            # providers cannot be selected (only internal_preview is always ok).
            resolved_provider = _resolve_provider_name(
                provider_name, spec.provider_type
            )
            connector_row = await conn.fetchrow(
                f"SELECT {pc._COLS} FROM external_provider_connectors "
                "WHERE provider_name = $1",
                resolved_provider,
            )
            if connector_row is None:
                raise IntegrationError(
                    f"unknown provider {resolved_provider!r}", code="invalid"
                )
            connector_row = dict(connector_row)
            if connector_row["provider_name"] not in pc.INTERNAL_PREVIEW_PROVIDERS \
                    and not connector_row["enabled"]:
                raise IntegrationError(
                    f"provider {resolved_provider!r} is disabled", code="conflict"
                )
            connector = pc._connector_for_row(connector_row)

            base_payload = render_payload_preview(spec.source_type, src)
            transient = {
                "payload_preview": base_payload,
                "action_type": action_type,
                "source_id": source_id,
            }
            payload = connector.build_payload_preview(transient)
            validation = connector.validate_payload(transient).as_dict()
            status = STATUS_BLOCKED if not validation["ok"] else STATUS_READY
            reason = (
                "Internal confirmation required before any future external action. "
                + SAFETY_CONFIRM
            )
            row = await conn.fetchrow(
                f"""
                INSERT INTO external_integration_intents
                    (workspace_id, created_by, source_type, source_id, agent_name,
                     provider_type, provider_name, action_type, status, dry_run,
                     requires_confirmation, confirmation_required_reason,
                     payload_preview, validation_result, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, TRUE, TRUE, $10,
                        $11, $12, $13)
                RETURNING {_COLS}
                """,
                src["workspace_id"],
                user_id,
                spec.source_type,
                source_id,
                spec.agent_name,
                spec.provider_type,
                resolved_provider,
                action_type,
                status,
                reason,
                payload,
                validation,
                {"dry_run_only": True, "note": SAFETY_DRY_RUN},
            )
            intent = dict(row)
            await _insert_event(
                conn,
                intent["id"],
                user_id,
                event_type="integration_intent_created",
                from_status=None,
                to_status=status,
                notes=notes,
                payload_snapshot=payload,
            )
    return intent


async def build_email_intent_from_draft(
    draft_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    is_admin: bool,
    provider_name: str = "internal_preview",
    action_type: str = "send_email",
    notes: Optional[str] = None,
) -> dict:
    return await _build_intent(
        _DRAFT_SPEC, draft_id, user_id=user_id, is_admin=is_admin,
        provider_name=provider_name, action_type=action_type, notes=notes,
    )


async def build_calendar_intent_from_proposal(
    proposal_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    is_admin: bool,
    provider_name: str = "internal_preview",
    action_type: str = "create_calendar_event",
    notes: Optional[str] = None,
) -> dict:
    return await _build_intent(
        _PROPOSAL_SPEC, proposal_id, user_id=user_id, is_admin=is_admin,
        provider_name=provider_name, action_type=action_type, notes=notes,
    )


# ---------------------------------------------------------------------------
# Integration Readiness Queue (v0.6)
#
# A simpler, provider-readiness-centric intent shape on the SAME table. Creates
# an internal record from an APPROVED draft/proposal that represents a FUTURE
# provider action. It performs NO external action: there is no enabled real
# provider and OAuth is never started, so a fresh intent is blocked_no_provider
# with provider_status=not_configured. The richer v0.4 validate/dry-run/confirm
# flow above is left intact.
# ---------------------------------------------------------------------------

INTENT_TYPE_EMAIL = "email_send_intent"
INTENT_TYPE_CALENDAR = "calendar_create_intent"

RQ_PENDING = "pending_provider"
RQ_BLOCKED_NO_PROVIDER = "blocked_no_provider"
RQ_BLOCKED_NO_OAUTH = "blocked_no_oauth"
RQ_READY_FUTURE = "ready_for_future_execution"
RQ_CANCELLED = "cancelled"
RQ_STATUSES = frozenset({
    RQ_PENDING, RQ_BLOCKED_NO_PROVIDER, RQ_BLOCKED_NO_OAUTH,
    RQ_READY_FUTURE, RQ_CANCELLED,
})

PROVIDER_STATUS_NOT_CONFIGURED = "not_configured"
PROVIDER_STATUS_CONFIGURED = "configured"

_RQ_INTENT_TYPE = {SOURCE_DRAFT: INTENT_TYPE_EMAIL, SOURCE_PROPOSAL: INTENT_TYPE_CALENDAR}
RQ_WORKFLOW_TAG = "integration_readiness_queue"

# Real-schema mapping (no intent_type column): the spec external action goes in
# the action_type column; the semantic intent_type is kept in metadata.
_RQ_EXTERNAL_ACTION = {PROVIDER_EMAIL: "send_email", PROVIDER_CALENDAR: "create_calendar_event"}
RQ_PROVIDER_PENDING = "pending_provider"
RQ_CONFIRM_REASON = "External provider execution disabled / OAuth not connected"


def _creation_blockers(rq_status: str) -> list[str]:
    """Readiness blockers stored in validation_result at creation time. The full
    connector-aware analysis is produced later by the simulation step."""
    base = [
        "external execution is disabled in this phase",
        "governance blocks external execution",
    ]
    if rq_status == RQ_BLOCKED_NO_PROVIDER:
        return ["no provider connector configured"] + base
    if rq_status == RQ_BLOCKED_NO_OAUTH:
        return ["provider connector not authorized (OAuth required)"] + base
    return base


async def _resolve_provider_readiness(
    conn, provider_type: str, user_id: uuid.UUID
) -> tuple[str, str, str]:
    """Decide the intent status from the caller's OAuth connector
    (provider_oauth_connectors, Credential Vault v0.6):
      - no connector                      → blocked_no_provider / not_configured
      - connector oauth_required/not_cfg  → blocked_no_oauth / not_configured
      - connector expired                 → blocked_no_oauth / expired
      - connector connected               → ready_for_future_execution / configured
        (execution is still disabled in this phase — readiness only)
    Never starts OAuth; performs no provider call."""
    row = await conn.fetchrow(
        "SELECT provider_name, status, token_expires_at "
        "FROM provider_oauth_connectors "
        "WHERE provider_type = $1 AND user_id = $2 AND status <> 'disconnected' "
        "ORDER BY created_at DESC LIMIT 1",
        provider_type, user_id,
    )
    if row is None:
        return RQ_BLOCKED_NO_PROVIDER, PROVIDER_STATUS_NOT_CONFIGURED, "unconfigured"
    row = dict(row)
    name = row["provider_name"]
    st = row["status"]
    if st == "expired":
        return RQ_BLOCKED_NO_OAUTH, "expired", name
    if st == "connected":
        # Connected but this phase performs no external execution.
        return RQ_READY_FUTURE, PROVIDER_STATUS_CONFIGURED, name
    # not_configured / oauth_required / error → still needs OAuth.
    return RQ_BLOCKED_NO_OAUTH, PROVIDER_STATUS_NOT_CONFIGURED, name


def _rq_metadata(spec: _BuildSpec, src: dict, payload: dict,
                 intent_type: str, provider_status: str) -> dict:
    md = {
        "source": "integration_readiness",
        "workflow": RQ_WORKFLOW_TAG,
        "intent_type": intent_type,
        "provider_status": provider_status,
        "dry_run_only": True,
        "note": SAFETY_DRY_RUN,
    }
    if spec.source_type == SOURCE_DRAFT:
        md.update({
            "draft_id": str(src["id"]),
            "subject": src.get("subject") or "",
            "body": src.get("body") or "",
            "recipient_hint": src.get("recipient_hint"),
        })
    else:
        md.update({
            "proposal_id": str(src["id"]),
            "title": src.get("title") or "",
            "description": src.get("description") or "",
            "start_time": payload.get("start_time"),
            "end_time": payload.get("end_time"),
            "attendees": list(src.get("attendees") or []),
        })
    return md


async def create_readiness_intent(
    spec: _BuildSpec,
    source_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    is_admin: bool,
    notes: Optional[str] = None,
) -> dict:
    """Create a readiness-queue intent from an APPROVED source. Approved-only +
    ownership gated; performs no external action."""
    pool = _require_pool()
    intent_type = _RQ_INTENT_TYPE[spec.source_type]
    async with pool.acquire() as conn:
        async with conn.transaction():
            src = await conn.fetchrow(
                f"SELECT {spec.select_cols} FROM {spec.table} WHERE id = $1",
                source_id,
            )
            if src is None:
                raise IntegrationError("source not found", code="not_found")
            src = dict(src)
            if not is_admin and src["created_by"] != user_id:
                raise IntegrationError("source not found", code="not_found")
            if src["status"] != "approved":
                raise IntegrationError(
                    "This item must be approved before creating an integration intent.",
                    code="conflict",
                )
            action_type = _RQ_EXTERNAL_ACTION[spec.provider_type]
            # Duplicate protection: at most one ACTIVE readiness intent per
            # (source_type, source_id, action_type). A cancelled prior intent
            # does not block a fresh one.
            dup = await conn.fetchval(
                """
                SELECT id FROM external_integration_intents
                WHERE source_type = $1 AND source_id = $2 AND action_type = $3
                  AND status <> 'cancelled'
                  AND COALESCE(metadata->>'source', '') = 'integration_readiness'
                LIMIT 1
                """,
                spec.source_type, source_id, action_type,
            )
            if dup is not None:
                raise IntegrationError(
                    "an active integration intent already exists for this source; "
                    "cancel it before creating another",
                    code="conflict",
                )
            payload = render_payload_preview(spec.source_type, src)
            # Connector-aware readiness informs the stored blockers + provider
            # status; the stored row uses the fixed provider-pending shape (the
            # detailed analysis is produced by the simulation step).
            rq_status, provider_status, _resolved_name = await _resolve_provider_readiness(
                conn, spec.provider_type, user_id
            )
            metadata = _rq_metadata(spec, src, payload, intent_type, provider_status)
            validation_result = {
                "ready_for_execution": False,
                "execution_enabled": False,
                "blockers": _creation_blockers(rq_status),
            }
            status_ = RQ_PENDING
            row = await conn.fetchrow(
                f"""
                INSERT INTO external_integration_intents
                    (workspace_id, created_by, source_type, source_id, agent_name,
                     provider_type, provider_name, action_type, status, dry_run,
                     requires_confirmation, confirmation_required_reason,
                     payload_preview, validation_result, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, TRUE, TRUE, $10,
                        $11, $12, $13)
                RETURNING {_COLS}
                """,
                src["workspace_id"],
                user_id,
                spec.source_type,
                source_id,
                spec.agent_name,
                spec.provider_type,
                RQ_PROVIDER_PENDING,
                action_type,
                status_,
                RQ_CONFIRM_REASON,
                payload,
                validation_result,
                metadata,
            )
            intent = dict(row)
            intent["_from_status"] = None
            intent["_to_status"] = status_
            await _insert_event(
                conn, intent["id"], user_id,
                event_type="integration_intent_created",
                from_status=None, to_status=status_, notes=notes,
                payload_snapshot=payload,
            )
    return intent


async def create_readiness_intent_from_draft(
    draft_id: uuid.UUID, *, user_id: uuid.UUID, is_admin: bool,
    notes: Optional[str] = None,
) -> dict:
    return await create_readiness_intent(
        _DRAFT_SPEC, draft_id, user_id=user_id, is_admin=is_admin, notes=notes,
    )


async def create_readiness_intent_from_proposal(
    proposal_id: uuid.UUID, *, user_id: uuid.UUID, is_admin: bool,
    notes: Optional[str] = None,
) -> dict:
    return await create_readiness_intent(
        _PROPOSAL_SPEC, proposal_id, user_id=user_id, is_admin=is_admin, notes=notes,
    )


# ---------------------------------------------------------------------------
# Intent lifecycle
# ---------------------------------------------------------------------------


async def _insert_event(
    conn,
    intent_id: uuid.UUID,
    user_id: Optional[uuid.UUID],
    *,
    event_type: str,
    from_status: Optional[str],
    to_status: Optional[str],
    notes: Optional[str],
    payload_snapshot: Optional[dict] = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO external_integration_events
            (intent_id, user_id, event_type, from_status, to_status, notes,
             payload_snapshot)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        intent_id,
        user_id,
        event_type,
        from_status,
        to_status,
        (notes.strip() if notes and notes.strip() else None),
        payload_snapshot or {},
    )


async def get_intent(intent_id: uuid.UUID) -> Optional[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_COLS} FROM external_integration_intents WHERE id = $1",
            intent_id,
        )
    return dict(row) if row else None


async def list_intents(
    *,
    workspace_id: Optional[uuid.UUID],
    owner_id: Optional[uuid.UUID] = None,
    source_type: Optional[str] = None,
    status: Optional[str] = None,
    agent_name: Optional[str] = None,
) -> list[dict]:
    pool = _require_pool()
    parts = [f"SELECT {_COLS} FROM external_integration_intents WHERE 1=1"]
    args: list = []
    if workspace_id is not None:
        args.append(workspace_id)
        parts.append(f"AND workspace_id = ${len(args)}")
    if owner_id is not None:
        args.append(owner_id)
        parts.append(f"AND created_by = ${len(args)}")
    if source_type is not None:
        args.append(source_type)
        parts.append(f"AND source_type = ${len(args)}")
    if status is not None:
        args.append(status)
        parts.append(f"AND status = ${len(args)}")
    if agent_name is not None:
        args.append(agent_name)
        parts.append(f"AND agent_name = ${len(args)}")
    parts.append("ORDER BY created_at DESC LIMIT 200")
    async with pool.acquire() as conn:
        rows = await conn.fetch(" ".join(parts), *args)
    return [dict(r) for r in rows]


async def revalidate_intent(intent_id: uuid.UUID, user_id: uuid.UUID) -> dict:
    """Re-run validation and (for non-terminal intents) recompute status."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"SELECT {_COLS} FROM external_integration_intents WHERE id = $1 FOR UPDATE",
                intent_id,
            )
            if row is None:
                raise IntegrationError("intent not found", code="not_found")
            intent = dict(row)
            validation = validate_integration_intent(intent)
            from_status = intent["status"]
            to_status = from_status
            if from_status in (STATUS_DRAFT, STATUS_READY, STATUS_BLOCKED):
                to_status = STATUS_READY if validation["ok"] else STATUS_BLOCKED
            updated = await conn.fetchrow(
                f"""
                UPDATE external_integration_intents
                SET validation_result = $1, status = $2, updated_at = NOW()
                WHERE id = $3 RETURNING {_COLS}
                """,
                validation,
                to_status,
                intent_id,
            )
            await _insert_event(
                conn, intent_id, user_id,
                event_type="integration_intent_validated",
                from_status=from_status, to_status=to_status, notes=None,
            )
    result = dict(updated)
    result["_from_status"] = from_status
    result["_to_status"] = to_status
    result["_validation"] = validation
    return result


async def confirm_integration_intent(
    intent_id: uuid.UUID, *, user_id: uuid.UUID, is_admin: bool,
    notes: Optional[str] = None,
) -> dict:
    """Internal confirmation only. dry_run stays TRUE; nothing is executed."""
    if not is_admin:
        raise IntegrationError(
            "confirmation requires an admin reviewer", code="forbidden"
        )
    pool = _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"SELECT {_COLS} FROM external_integration_intents WHERE id = $1 FOR UPDATE",
                intent_id,
            )
            if row is None:
                raise IntegrationError("intent not found", code="not_found")
            from_status = row["status"]
            if from_status != STATUS_READY:
                raise IntegrationError(
                    f"cannot confirm from status {from_status!r}", code="conflict"
                )
            updated = await conn.fetchrow(
                f"""
                UPDATE external_integration_intents
                SET status = $1, confirmed_by = $2, confirmed_at = NOW(),
                    dry_run = TRUE, updated_at = NOW()
                WHERE id = $3 RETURNING {_COLS}
                """,
                STATUS_CONFIRMED,
                user_id,
                intent_id,
            )
            await _insert_event(
                conn, intent_id, user_id,
                event_type="integration_intent_confirmed",
                from_status=from_status, to_status=STATUS_CONFIRMED, notes=notes,
            )
    result = dict(updated)
    result["_from_status"] = from_status
    result["_to_status"] = STATUS_CONFIRMED
    return result


async def cancel_integration_intent(
    intent_id: uuid.UUID, *, user_id: uuid.UUID, is_admin: bool, is_owner: bool,
    notes: Optional[str] = None,
) -> dict:
    pool = _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"SELECT {_COLS} FROM external_integration_intents WHERE id = $1 FOR UPDATE",
                intent_id,
            )
            if row is None:
                raise IntegrationError("intent not found", code="not_found")
            from_status = row["status"]
            if from_status in (STATUS_CANCELLED, STATUS_EXECUTED_PLACEHOLDER):
                raise IntegrationError(
                    f"cannot cancel from status {from_status!r}", code="conflict"
                )
            # confirmed -> cancelled requires admin; draft/ready by owner-or-admin.
            if from_status == STATUS_CONFIRMED and not is_admin:
                raise IntegrationError(
                    "only an admin can cancel a confirmed intent", code="forbidden"
                )
            if not (is_admin or is_owner):
                raise IntegrationError("not permitted", code="forbidden")
            # dry_run is held TRUE explicitly: cancelling must never enable
            # execution (Execution Approval Gate v0.7 safety invariant).
            updated = await conn.fetchrow(
                f"""
                UPDATE external_integration_intents
                SET status = $1, cancelled_by = $2, cancelled_at = NOW(),
                    dry_run = TRUE, updated_at = NOW()
                WHERE id = $3 RETURNING {_COLS}
                """,
                STATUS_CANCELLED,
                user_id,
                intent_id,
            )
            await _insert_event(
                conn, intent_id, user_id,
                event_type="integration_intent_cancelled",
                from_status=from_status, to_status=STATUS_CANCELLED, notes=notes,
            )
    result = dict(updated)
    result["_from_status"] = from_status
    result["_to_status"] = STATUS_CANCELLED
    return result


# ---------------------------------------------------------------------------
# Execution Approval Gate v0.7 — confirm / revoke (readiness-queue intents)
#
# Confirmation is a HUMAN approval that this action may run *once execution is
# enabled in a future phase*. It executes nothing: dry_run stays TRUE, no
# provider is called, and EXECUTION_ENABLED stays False. Confirm requires the
# source to still be approved, dry_run + requires_confirmation true, a stored
# validation_result, and no critical blockers (the always-present
# execution-disabled / governance blockers don't count).
# ---------------------------------------------------------------------------

_SOURCE_TABLE = {SOURCE_DRAFT: "communication_drafts", SOURCE_PROPOSAL: "schedule_proposals"}

# Blockers that are EXPECTED in this phase and must not prevent confirmation.
_NON_CRITICAL_BLOCKER_MARKERS = (
    "external execution is disabled",
    "governance blocks external execution",
    "execution disabled by governance",
)

# Statuses a readiness-queue intent may be confirmed from.
_CONFIRMABLE_FROM = frozenset({
    STATUS_DRAFT, RQ_PENDING, RQ_BLOCKED_NO_PROVIDER, RQ_BLOCKED_NO_OAUTH,
    RQ_READY_FUTURE, STATUS_CONFIRMATION_REVOKED,
})


def _critical_blockers(validation: Optional[dict]) -> list[str]:
    """Blockers that DO prevent confirmation (everything except the expected
    execution-disabled / governance ones)."""
    blockers = (validation or {}).get("blockers") or []
    out: list[str] = []
    for b in blockers:
        low = str(b).lower()
        if any(m in low for m in _NON_CRITICAL_BLOCKER_MARKERS):
            continue
        out.append(str(b))
    return out


async def _source_is_approved(conn, intent: dict) -> bool:
    table = _SOURCE_TABLE.get(intent["source_type"])
    if not table:
        return False
    st = await conn.fetchval(
        f"SELECT status FROM {table} WHERE id = $1", intent["source_id"]
    )
    return st == "approved"


async def confirm_readiness_intent(
    intent_id: uuid.UUID, *, user_id: uuid.UUID, is_admin: bool, is_owner: bool,
    notes: Optional[str] = None,
) -> dict:
    """Confirm a readiness-queue intent. Internal approval only — executes
    nothing, keeps dry_run TRUE, never calls a provider."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"SELECT {_COLS} FROM external_integration_intents WHERE id = $1 FOR UPDATE",
                intent_id,
            )
            if row is None:
                raise IntegrationError("intent not found", code="not_found")
            intent = dict(row)
            if not (is_admin or is_owner):
                raise IntegrationError("not permitted", code="forbidden")
            from_status = intent["status"]
            if from_status not in _CONFIRMABLE_FROM:
                raise IntegrationError(
                    f"cannot confirm from status {from_status!r}", code="conflict"
                )
            if not intent["dry_run"]:
                raise IntegrationError(
                    "intent must be dry_run to confirm", code="conflict"
                )
            if not intent["requires_confirmation"]:
                raise IntegrationError(
                    "intent does not require confirmation", code="conflict"
                )
            if not intent["validation_result"]:
                raise IntegrationError(
                    "run a readiness check before confirming", code="conflict"
                )
            if not await _source_is_approved(conn, intent):
                raise IntegrationError(
                    "source draft/proposal must be approved to confirm",
                    code="conflict",
                )
            crit = _critical_blockers(intent["validation_result"])
            if crit:
                raise IntegrationError(
                    "cannot confirm — unresolved blockers: " + "; ".join(crit),
                    code="conflict",
                )
            # Approval only. dry_run stays TRUE; execution remains disabled.
            updated = await conn.fetchrow(
                f"""
                UPDATE external_integration_intents
                SET status = $1, confirmed_by = $2, confirmed_at = NOW(),
                    requires_confirmation = FALSE, dry_run = TRUE, updated_at = NOW()
                WHERE id = $3 RETURNING {_COLS}
                """,
                STATUS_CONFIRMED, user_id, intent_id,
            )
            await _insert_event(
                conn, intent_id, user_id,
                event_type="integration_intent_confirmed",
                from_status=from_status, to_status=STATUS_CONFIRMED, notes=notes,
                payload_snapshot={"execution_enabled": EXECUTION_ENABLED},
            )
    result = dict(updated)
    result["_from_status"] = from_status
    result["_to_status"] = STATUS_CONFIRMED
    return result


async def revoke_readiness_intent(
    intent_id: uuid.UUID, *, user_id: uuid.UUID, is_admin: bool, is_owner: bool,
    notes: Optional[str] = None,
) -> dict:
    """Revoke a previously confirmed intent → confirmation_revoked. Clears
    confirmation, restores requires_confirmation. Executes nothing."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"SELECT {_COLS} FROM external_integration_intents WHERE id = $1 FOR UPDATE",
                intent_id,
            )
            if row is None:
                raise IntegrationError("intent not found", code="not_found")
            if not (is_admin or is_owner):
                raise IntegrationError("not permitted", code="forbidden")
            from_status = row["status"]
            if from_status != STATUS_CONFIRMED:
                raise IntegrationError(
                    f"cannot revoke from status {from_status!r}", code="conflict"
                )
            updated = await conn.fetchrow(
                f"""
                UPDATE external_integration_intents
                SET status = $1, confirmed_by = NULL, confirmed_at = NULL,
                    requires_confirmation = TRUE, dry_run = TRUE, updated_at = NOW()
                WHERE id = $2 RETURNING {_COLS}
                """,
                STATUS_CONFIRMATION_REVOKED, intent_id,
            )
            await _insert_event(
                conn, intent_id, user_id,
                event_type="integration_intent_confirmation_revoked",
                from_status=from_status, to_status=STATUS_CONFIRMATION_REVOKED,
                notes=notes,
            )
    result = dict(updated)
    result["_from_status"] = from_status
    result["_to_status"] = STATUS_CONFIRMATION_REVOKED
    return result


async def list_events(intent_id: uuid.UUID) -> list[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, intent_id, user_id, event_type, from_status, to_status,
                   notes, payload_snapshot, created_at
            FROM external_integration_events
            WHERE intent_id = $1 ORDER BY created_at ASC
            """,
            intent_id,
        )
    return [dict(r) for r in rows]


async def dry_run_intent(intent_id: uuid.UUID, user_id: uuid.UUID) -> dict:
    """Run the provider connector's DRY-RUN against an intent: re-derive the
    provider payload preview + validation, persist them, log an event. Performs
    NO external action and does NOT change status to executed. Allowed in
    draft/ready_for_confirmation/confirmed/blocked."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"SELECT {_COLS} FROM external_integration_intents WHERE id = $1 FOR UPDATE",
                intent_id,
            )
            if row is None:
                raise IntegrationError("intent not found", code="not_found")
            intent = dict(row)
            connector_row = await conn.fetchrow(
                f"SELECT {pc._COLS} FROM external_provider_connectors "
                "WHERE provider_name = $1",
                intent["provider_name"],
            )
            if connector_row is None:
                raise IntegrationError(
                    f"provider {intent['provider_name']!r} not found", code="invalid"
                )
            connector = pc._connector_for_row(dict(connector_row))
            # Dry-run execution — provably contacts nothing external.
            run = pc.execute_provider_action_dry_run(connector, intent)
            validation = connector.validate_payload(intent).as_dict()
            preview = run["payload"]
            updated = await conn.fetchrow(
                f"""
                UPDATE external_integration_intents
                SET payload_preview = $1, validation_result = $2, updated_at = NOW()
                WHERE id = $3 RETURNING {_COLS}
                """,
                preview,
                validation,
                intent_id,
            )
            await _insert_event(
                conn, intent_id, user_id,
                event_type="provider_dry_run_executed",
                from_status=intent["status"], to_status=intent["status"],
                notes=None, payload_snapshot=preview,
            )
    result = dict(updated)
    result["_from_status"] = intent["status"]
    result["_to_status"] = intent["status"]
    result["_dry_run_result"] = run
    result["_validation"] = validation
    return result
