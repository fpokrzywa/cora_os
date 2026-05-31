"""Provider OAuth connector vault (v0.6) — READINESS ONLY.

Per-user credential *records* for future OAuth providers. NO real OAuth flow, NO
provider API calls, NO email/calendar execution. Token columns hold Fernet-
encrypted values only (never plaintext) and stay NULL in this phase. Secrets are
never returned: records are masked to has_access_token / has_refresh_token flags.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.clients import clients
from app.crypto import encryption_available

logger = logging.getLogger(__name__)

# Canonical providers and their type. register-placeholder validates against this.
PROVIDERS = {
    "gmail": "email",
    "outlook_mail": "email",
    "google_calendar": "calendar",
    "outlook_calendar": "calendar",
}

STATUS_NOT_CONFIGURED = "not_configured"
STATUS_OAUTH_REQUIRED = "oauth_required"
STATUS_CONNECTED = "connected"
STATUS_EXPIRED = "expired"
STATUS_DISCONNECTED = "disconnected"
STATUS_ERROR = "error"
VALID_STATUSES = {
    STATUS_NOT_CONFIGURED, STATUS_OAUTH_REQUIRED, STATUS_CONNECTED,
    STATUS_EXPIRED, STATUS_DISCONNECTED, STATUS_ERROR,
}

# Required OAuth scopes per provider (used only to surface readiness blockers;
# no scope is ever granted in this phase).
REQUIRED_SCOPES = {
    "gmail": ["gmail.send"],
    "outlook_mail": ["Mail.Send"],
    "google_calendar": ["calendar.events"],
    "outlook_calendar": ["Calendars.ReadWrite"],
}

# Governance hard-blocks external execution in this phase, so every provider
# always carries this blocker regardless of connector state.
GOVERNANCE_BLOCKER = "Execution disabled by governance"

SAFETY_NOTE = (
    "Provider connectors are in readiness mode: no OAuth flow runs and no "
    "external provider API is called. Execution stays disabled in this phase."
)

# Full columns (incl. encrypted token columns, used only to compute has_* flags).
_COLS = (
    "id, user_id, workspace_id, provider_name, provider_type, status, scopes, "
    "access_token_encrypted, refresh_token_encrypted, token_expires_at, "
    "metadata, created_at, updated_at, disconnected_at"
)
_SECRET_COLS = ("access_token_encrypted", "refresh_token_encrypted")


class ProviderConnectorError(Exception):
    """code: not_found (404) | invalid (400) | forbidden (403) |
    conflict (409) | unavailable (503)."""

    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


def _require_pool():
    if clients.db_pool is None:
        raise ProviderConnectorError("Postgres pool unavailable", code="unavailable")
    return clients.db_pool


def _token_expired(row: dict) -> bool:
    exp = row.get("token_expires_at")
    if exp is None:
        return False
    return exp <= datetime.now(timezone.utc)


def mask(row: dict) -> dict:
    """API-safe view: drop encrypted token columns, expose presence flags and the
    computed ready_for_execution. Never returns secret material."""
    safe = {k: v for k, v in row.items() if k not in _SECRET_COLS}
    has_access = bool(row.get("access_token_encrypted"))
    has_refresh = bool(row.get("refresh_token_encrypted"))
    safe["has_access_token"] = has_access
    safe["has_refresh_token"] = has_refresh
    # ready_for_execution: connected + token present + not expired. (Always false
    # in this phase because no OAuth ever sets status=connected with a token.)
    safe["ready_for_execution"] = (
        row.get("status") == STATUS_CONNECTED
        and has_access
        and not _token_expired(row)
    )
    safe["encryption_available"] = encryption_available()
    return safe


def _provider_blockers(provider_name: str, row: Optional[dict]) -> tuple[list[str], list[str]]:
    """Human-readable readiness blockers + missing scopes for one provider.
    The governance blocker is always present in this phase."""
    blockers: list[str] = []
    missing: list[str] = []
    if row is None:
        blockers.append("No connector configured")
    else:
        status = row.get("status")
        if status == STATUS_NOT_CONFIGURED:
            blockers.append("No connector configured")
        elif status == STATUS_OAUTH_REQUIRED:
            blockers.append("OAuth required")
        elif status == STATUS_DISCONNECTED:
            blockers.append("Connector disconnected")
        elif status == STATUS_ERROR:
            blockers.append("Connector error")
        available = list(row.get("scopes") or [])
        missing = [s for s in REQUIRED_SCOPES.get(provider_name, []) if s not in available]
        if missing:
            blockers.append("Missing required scopes: " + ", ".join(missing))
        if not row.get("access_token_encrypted"):
            blockers.append("Missing token")
        elif _token_expired(row):
            blockers.append("Token expired")
    blockers.append(GOVERNANCE_BLOCKER)
    return blockers, missing


def _readiness_view(provider_name: str, row: Optional[dict]) -> dict:
    """The spec readiness shape for one provider (row may be None → not_configured)."""
    blockers, missing = _provider_blockers(provider_name, row)
    if row is None:
        return {
            "provider_name": provider_name,
            "provider_type": PROVIDERS[provider_name],
            "status": STATUS_NOT_CONFIGURED,
            "scopes": [],
            "required_scopes": REQUIRED_SCOPES.get(provider_name, []),
            "missing_scopes": missing,
            "has_access_token": False,
            "has_refresh_token": False,
            "token_expires_at": None,
            "disconnected_at": None,
            "updated_at": None,
            "ready_for_execution": False,
            "blockers": blockers,
            "connector_id": None,
            "encryption_available": encryption_available(),
        }
    m = mask(row)
    return {
        "provider_name": m["provider_name"],
        "provider_type": m["provider_type"],
        "status": m["status"],
        "scopes": m.get("scopes") or [],
        "required_scopes": REQUIRED_SCOPES.get(provider_name, []),
        "missing_scopes": missing,
        "has_access_token": m["has_access_token"],
        "has_refresh_token": m["has_refresh_token"],
        "token_expires_at": m.get("token_expires_at"),
        "disconnected_at": m.get("disconnected_at"),
        "updated_at": m.get("updated_at"),
        "ready_for_execution": m["ready_for_execution"],
        "blockers": blockers,
        "connector_id": str(m["id"]),
        "encryption_available": m["encryption_available"],
    }


async def list_connectors(*, user_id: uuid.UUID, is_admin: bool) -> list[dict]:
    parts = [f"SELECT {_COLS} FROM provider_oauth_connectors WHERE TRUE"]
    args: list = []
    if not is_admin:
        args.append(user_id)
        parts.append(f"AND user_id = ${len(args)}")
    parts.append("ORDER BY created_at DESC")
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(" ".join(parts), *args)
    return [mask(dict(r)) for r in rows]


async def _fetch_row(conn, connector_id: uuid.UUID) -> Optional[dict]:
    row = await conn.fetchrow(
        f"SELECT {_COLS} FROM provider_oauth_connectors WHERE id = $1", connector_id
    )
    return dict(row) if row else None


async def get_connector(
    connector_id: uuid.UUID, *, user_id: uuid.UUID, is_admin: bool
) -> dict:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await _fetch_row(conn, connector_id)
    if row is None or (not is_admin and row["user_id"] != user_id):
        raise ProviderConnectorError("connector not found", code="not_found")
    return mask(row)


async def register_placeholder(
    *,
    user_id: uuid.UUID,
    provider_name: str,
    provider_type: Optional[str] = None,
    scopes: Optional[list] = None,
    workspace_id: Optional[uuid.UUID] = None,
) -> dict:
    """Create a placeholder connector (status=oauth_required). No OAuth runs and
    no secret is stored — token columns stay NULL."""
    if provider_name not in PROVIDERS:
        raise ProviderConnectorError(
            f"unknown provider {provider_name!r}; expected one of "
            f"{sorted(PROVIDERS)}",
            code="invalid",
        )
    resolved_type = PROVIDERS[provider_name]
    if provider_type and provider_type != resolved_type:
        logger.info(
            "register_placeholder: provider_type %r overridden to %r",
            provider_type, resolved_type,
        )
    metadata = {
        "placeholder": True,
        "note": SAFETY_NOTE,
        "encryption_available": encryption_available(),
    }
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO provider_oauth_connectors
                (user_id, workspace_id, provider_name, provider_type, status,
                 scopes, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING {_COLS}
            """,
            user_id, workspace_id, provider_name, resolved_type,
            STATUS_OAUTH_REQUIRED, scopes or [], metadata,
        )
    return mask(dict(row))


async def disconnect(
    connector_id: uuid.UUID, *, user_id: uuid.UUID, is_admin: bool
) -> dict:
    """Mark a connector disconnected and clear any stored (encrypted) tokens.
    Performs no provider/OAuth revocation call."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, user_id, status FROM provider_oauth_connectors "
                "WHERE id = $1 FOR UPDATE",
                connector_id,
            )
            if row is None or (not is_admin and row["user_id"] != user_id):
                raise ProviderConnectorError("connector not found", code="not_found")
            if row["status"] == STATUS_DISCONNECTED:
                raise ProviderConnectorError(
                    "connector is already disconnected", code="conflict"
                )
            updated = await conn.fetchrow(
                f"""
                UPDATE provider_oauth_connectors
                SET status = $1, access_token_encrypted = NULL,
                    refresh_token_encrypted = NULL, token_expires_at = NULL,
                    disconnected_at = NOW(), updated_at = NOW()
                WHERE id = $2 RETURNING {_COLS}
                """,
                STATUS_DISCONNECTED, connector_id,
            )
    return mask(dict(updated))


async def readiness(*, user_id: uuid.UUID, is_admin: bool) -> dict:
    """Readiness for each canonical provider (latest connector for the caller).
    ready_for_execution is always false in this phase."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_COLS} FROM provider_oauth_connectors "
            "WHERE user_id = $1 ORDER BY created_at DESC",
            user_id,
        )
    latest: dict[str, dict] = {}
    for r in rows:
        r = dict(r)
        latest.setdefault(r["provider_name"], r)  # rows are DESC → first = latest
    providers = [_readiness_view(name, latest.get(name)) for name in PROVIDERS]
    return {
        "encryption_available": encryption_available(),
        "execution_enabled": False,  # hard invariant for this phase
        "safety_note": SAFETY_NOTE,
        "providers": providers,
    }
