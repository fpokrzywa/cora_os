"""OAuth credential vault (v0.6) — READINESS / DRY-RUN ONLY.

Stores per-workspace / per-user credential *records* for FUTURE OAuth providers
(Gmail / Outlook / Google Calendar / Microsoft Calendar). It prepares the data
model + lifecycle so a later module can wire a real OAuth flow behind explicit
governance.

HARD SAFETY INVARIANTS (v0.6):
- No real OAuth exchange, no token-exchange endpoint, no provider API calls.
- No plaintext secret is ever stored or returned.
- Because no real encryption utility exists yet (see _encrypt_secret), this
  module REFUSES to accept real tokens/secrets — the encrypted_* columns stay
  NULL. Only non-secret config (scopes, client_id_hint, metadata) is accepted.
- `dry_run_only` is forced TRUE on every write.
- `mask_credential` strips every encrypted_* column before a record leaves the
  service, so the API layer cannot accidentally leak them.
"""

import logging
import uuid
from typing import Optional

from app.clients import clients
from app.provider_connectors import get_connector_row

logger = logging.getLogger(__name__)

# Conceptual status values (mirrors the schema CHECK constraint).
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_CONFIGURED = "configured"
STATUS_NEEDS_AUTHORIZATION = "needs_authorization"
STATUS_AUTHORIZED_PLACEHOLDER = "authorized_placeholder"
STATUS_EXPIRED = "expired"
STATUS_REVOKED = "revoked"
STATUS_DISABLED = "disabled"
STATUS_ERROR = "error"

VALID_STATUSES = {
    STATUS_NOT_CONFIGURED, STATUS_CONFIGURED, STATUS_NEEDS_AUTHORIZATION,
    STATUS_AUTHORIZED_PLACEHOLDER, STATUS_EXPIRED, STATUS_REVOKED,
    STATUS_DISABLED, STATUS_ERROR,
}

# Columns safe to read back (intentionally EXCLUDES every encrypted_* column).
_SAFE_COLS = (
    "id, workspace_id, user_id, provider_name, provider_type, credential_name, "
    "auth_type, status, scopes, client_id_hint, token_expires_at, "
    "last_authorized_at, last_validated_at, last_error, dry_run_only, metadata, "
    "created_by, updated_by, created_at, updated_at"
)

# Secret columns — never selected into a response, never returned by mask.
_SECRET_COLS = (
    "encrypted_access_token", "encrypted_refresh_token", "encrypted_client_secret",
)

SAFETY_NOTE = (
    "Credential Vault is in readiness mode. No OAuth exchange or provider API "
    "calls are performed."
)


class CredentialError(Exception):
    """code: not_found (404) | invalid (400) | forbidden (403) |
    conflict (409) | unavailable (503)."""

    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# Encryption (PLACEHDER ONLY — no real crypto in v0.6).
# --------------------------------------------------------------------------- #
# TODO(v0.7+): replace with a real authenticated-encryption utility keyed by an
# env-provided secret (e.g. Fernet/AES-GCM with CORA_CREDENTIAL_ENC_KEY). Until
# that exists, _encrypt_secret REFUSES any non-empty secret so we never persist
# something that looks like a real token in reversible/plaintext form.
ENCRYPTION_AVAILABLE = False


def _encrypt_secret(value: Optional[str]) -> Optional[str]:
    """Placeholder. Returns None for empty input; raises if a real secret is
    supplied while no encryption backend is configured."""
    if not value:
        return None
    if not ENCRYPTION_AVAILABLE:
        raise CredentialError(
            "storing real secrets is disabled — no encryption backend is "
            "configured in v0.6 (readiness/dry-run only)",
            code="invalid",
        )
    # TODO(v0.7+): return real_encrypt(value)
    raise CredentialError("encryption backend not implemented", code="invalid")


def _require_pool():
    if clients.db_pool is None:
        raise CredentialError("Postgres pool unavailable", code="unavailable")
    return clients.db_pool


def mask_credential(row: dict) -> dict:
    """Return an API-safe view: every encrypted_* column removed, replaced by
    boolean has_* presence flags. Never returns secret material."""
    safe = {k: v for k, v in row.items() if k not in _SECRET_COLS}
    safe["has_access_token"] = bool(row.get("encrypted_access_token"))
    safe["has_refresh_token"] = bool(row.get("encrypted_refresh_token"))
    safe["has_client_secret"] = bool(row.get("encrypted_client_secret"))
    return safe


async def write_credential_event(
    credential_id: uuid.UUID,
    *,
    event_type: str,
    from_status: Optional[str] = None,
    to_status: Optional[str] = None,
    user_id: Optional[uuid.UUID] = None,
    notes: Optional[str] = None,
    metadata: Optional[dict] = None,
    conn=None,
) -> None:
    """Append an audit row to external_provider_credential_events."""
    async def _run(c):
        await c.execute(
            """
            INSERT INTO external_provider_credential_events
                (credential_id, user_id, event_type, from_status, to_status,
                 notes, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            credential_id, user_id, event_type, from_status, to_status,
            notes, metadata or {},
        )

    if conn is not None:
        await _run(conn)
    else:
        pool = _require_pool()
        async with pool.acquire() as c:
            await _run(c)


async def _fetch_safe(conn, credential_id: uuid.UUID) -> Optional[dict]:
    row = await conn.fetchrow(
        f"SELECT {_SAFE_COLS} FROM external_provider_credentials WHERE id = $1",
        credential_id,
    )
    return dict(row) if row else None


def _assert_can_manage(row: dict, *, user_id: uuid.UUID, is_admin: bool) -> None:
    """Authorization gate. Admins manage everything. Non-admins may only manage
    their own user-scoped records (user_id == self); workspace/global records
    (user_id IS NULL or a different user) are off-limits."""
    if is_admin:
        return
    if row.get("user_id") is None or row["user_id"] != user_id:
        raise CredentialError(
            "not permitted for this credential", code="forbidden"
        )


async def get_credential(
    credential_id: uuid.UUID, *, user_id: uuid.UUID, is_admin: bool
) -> dict:
    """Fetch one (masked) credential, enforcing visibility. 404 if missing or
    not visible to the caller (don't reveal existence across tenants)."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_SAFE_COLS}, {', '.join(_SECRET_COLS)} "
            f"FROM external_provider_credentials WHERE id = $1",
            credential_id,
        )
    if row is None:
        raise CredentialError("credential not found", code="not_found")
    row = dict(row)
    if not is_admin and (row.get("user_id") is None or row["user_id"] != user_id):
        raise CredentialError("credential not found", code="not_found")
    return mask_credential(row)


async def list_credentials(
    *,
    user_id: uuid.UUID,
    is_admin: bool,
    workspace_id: Optional[uuid.UUID] = None,
    provider_name: Optional[str] = None,
) -> list[dict]:
    parts = ["SELECT", _SAFE_COLS, "FROM external_provider_credentials WHERE TRUE"]
    args: list = []
    # Non-admins only ever see their own user-scoped records.
    if not is_admin:
        args.append(user_id)
        parts.append(f"AND user_id = ${len(args)}")
    if workspace_id is not None:
        args.append(workspace_id)
        parts.append(f"AND workspace_id = ${len(args)}")
    if provider_name is not None:
        args.append(provider_name)
        parts.append(f"AND provider_name = ${len(args)}")
    parts.append("ORDER BY created_at DESC")
    sql = " ".join(parts)
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    # _SAFE_COLS already excludes secrets; mask still normalizes has_* flags.
    return [mask_credential(dict(r)) for r in rows]


async def create_credential_record(
    *,
    actor_id: uuid.UUID,
    is_admin: bool,
    provider_name: str,
    provider_type: str,
    credential_name: str,
    auth_type: str = "oauth2",
    scopes: Optional[list] = None,
    client_id_hint: Optional[str] = None,
    workspace_id: Optional[uuid.UUID] = None,
    user_id: Optional[uuid.UUID] = None,
    dry_run_only: bool = True,
    metadata: Optional[dict] = None,
) -> dict:
    """Create a credential placeholder. No secrets are accepted here (v0.6).

    Authorization:
    - Non-admins may only create their OWN user-scoped record (user_id forced to
      self); they cannot create workspace/global records.
    - provider_name must exist in external_provider_connectors.
    """
    if not credential_name or not credential_name.strip():
        raise CredentialError("credential_name is required", code="invalid")

    # Provider linkage: the provider must be a known connector.
    connector = await get_connector_row(provider_name)
    if connector is None:
        raise CredentialError(
            f"unknown provider {provider_name!r}", code="invalid"
        )
    # Trust the connector's type as the source of truth.
    resolved_type = connector["provider_type"]
    if provider_type and provider_type != resolved_type:
        logger.info(
            "credential create: provider_type %r overridden to connector type %r",
            provider_type, resolved_type,
        )
    provider_type = resolved_type

    # Ownership rules.
    if is_admin:
        owner_id = user_id  # admin may make a user- or workspace/global record
    else:
        if user_id is not None and user_id != actor_id:
            raise CredentialError(
                "non-admins may only create their own credentials",
                code="forbidden",
            )
        if workspace_id is not None and user_id is None:
            # A workspace record with no user owner is a workspace/global record.
            raise CredentialError(
                "non-admins cannot create workspace/global credentials",
                code="forbidden",
            )
        owner_id = actor_id

    pool = _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""
                INSERT INTO external_provider_credentials
                    (workspace_id, user_id, provider_name, provider_type,
                     credential_name, auth_type, status, scopes, client_id_hint,
                     dry_run_only, metadata, created_by, updated_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, TRUE, $10, $11, $11)
                RETURNING {_SAFE_COLS}
                """,
                workspace_id,
                owner_id,
                provider_name,
                provider_type,
                credential_name.strip(),
                auth_type or "oauth2",
                STATUS_NOT_CONFIGURED,
                scopes or [],
                (client_id_hint or None),
                metadata or {},
                actor_id,
            )
            row = dict(row)
            await write_credential_event(
                row["id"],
                event_type="created",
                from_status=None,
                to_status=STATUS_NOT_CONFIGURED,
                user_id=actor_id,
                notes="credential placeholder created (dry-run only)",
                conn=conn,
            )
    return mask_credential(row)


_EDITABLE_FIELDS = ("credential_name", "scopes", "client_id_hint", "metadata")


async def update_credential_record(
    credential_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    is_admin: bool,
    fields: dict,
) -> dict:
    """Update non-secret fields only (credential_name, scopes, client_id_hint,
    metadata, dry_run_only). Status is NOT mutable here — use the dedicated
    lifecycle endpoints. dry_run_only is forced TRUE."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                "SELECT id, user_id, status FROM external_provider_credentials "
                "WHERE id = $1 FOR UPDATE",
                credential_id,
            )
            if current is None:
                raise CredentialError("credential not found", code="not_found")
            _assert_can_manage(dict(current), user_id=actor_id, is_admin=is_admin)

            sets: list[str] = []
            args: list = []
            for col in _EDITABLE_FIELDS:
                if col in fields and fields[col] is not None:
                    args.append(fields[col])
                    sets.append(f"{col} = ${len(args)}")
            # dry_run_only may be sent but is always coerced TRUE (no live mode).
            sets.append("dry_run_only = TRUE")
            if not sets or (len(sets) == 1):
                # Only the forced dry_run_only set — nothing real to change.
                row = await _fetch_safe(conn, credential_id)
                return mask_credential(row)
            args.append(actor_id)
            sets.append(f"updated_by = ${len(args)}")
            sets.append("updated_at = NOW()")
            args.append(credential_id)
            row = await conn.fetchrow(
                f"UPDATE external_provider_credentials SET {', '.join(sets)} "
                f"WHERE id = ${len(args)} RETURNING {_SAFE_COLS}",
                *args,
            )
            row = dict(row)
            await write_credential_event(
                credential_id,
                event_type="updated",
                from_status=current["status"],
                to_status=row["status"],
                user_id=actor_id,
                notes="non-secret fields updated",
                conn=conn,
            )
    return mask_credential(row)


async def _transition(
    credential_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    is_admin: bool,
    to_status: str,
    event_type: str,
    notes: str,
    stamp: Optional[str] = None,
) -> tuple[dict, str, str]:
    """Shared status-transition primitive: locks the row, enforces management
    permission, sets the new status (+ an optional timestamp column), records an
    event. Returns (masked_row, from_status, to_status)."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                "SELECT id, user_id, status FROM external_provider_credentials "
                "WHERE id = $1 FOR UPDATE",
                credential_id,
            )
            if current is None:
                raise CredentialError("credential not found", code="not_found")
            _assert_can_manage(dict(current), user_id=actor_id, is_admin=is_admin)
            from_status = current["status"]
            stamp_sql = f", {stamp} = NOW()" if stamp else ""
            row = await conn.fetchrow(
                f"""
                UPDATE external_provider_credentials
                SET status = $1, dry_run_only = TRUE, updated_by = $2,
                    updated_at = NOW(){stamp_sql}
                WHERE id = $3
                RETURNING {_SAFE_COLS}
                """,
                to_status, actor_id, credential_id,
            )
            row = dict(row)
            await write_credential_event(
                credential_id,
                event_type=event_type,
                from_status=from_status,
                to_status=to_status,
                user_id=actor_id,
                notes=notes,
                conn=conn,
            )
    return mask_credential(row), from_status, to_status


async def disable_credential(
    credential_id: uuid.UUID, *, actor_id: uuid.UUID, is_admin: bool
) -> dict:
    row, _f, _t = await _transition(
        credential_id, actor_id=actor_id, is_admin=is_admin,
        to_status=STATUS_DISABLED, event_type="disabled",
        notes="credential disabled",
    )
    return row


async def mark_credential_needs_authorization(
    credential_id: uuid.UUID, *, actor_id: uuid.UUID, is_admin: bool
) -> dict:
    row, _f, _t = await _transition(
        credential_id, actor_id=actor_id, is_admin=is_admin,
        to_status=STATUS_NEEDS_AUTHORIZATION,
        event_type="marked_needs_authorization",
        notes="credential marked as needing authorization (no OAuth performed)",
    )
    return row


async def validate_credential_placeholder(
    credential_id: uuid.UUID, *, actor_id: uuid.UUID, is_admin: bool
) -> dict:
    """Placeholder validation. Performs NO provider call and NO token check —
    it only inspects the stored record shape, stamps last_validated_at, and
    records the event. Status is intentionally left unchanged (we cannot truly
    authorize without a real OAuth flow)."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                f"SELECT {_SAFE_COLS} FROM external_provider_credentials "
                "WHERE id = $1 FOR UPDATE",
                credential_id,
            )
            if current is None:
                raise CredentialError("credential not found", code="not_found")
            current = dict(current)
            _assert_can_manage(current, user_id=actor_id, is_admin=is_admin)

            # Shape-only checks; never contacts a provider.
            checks = {
                "has_provider": bool(current["provider_name"]),
                "has_name": bool(current["credential_name"]),
                "has_scopes": bool(current.get("scopes")),
                "has_client_id_hint": bool(current.get("client_id_hint")),
                "dry_run_only": bool(current["dry_run_only"]),
            }
            ok = checks["has_provider"] and checks["has_name"]
            await conn.execute(
                "UPDATE external_provider_credentials "
                "SET last_validated_at = NOW(), updated_by = $1, updated_at = NOW() "
                "WHERE id = $2",
                actor_id, credential_id,
            )
            await write_credential_event(
                credential_id,
                event_type="placeholder_validated",
                from_status=current["status"],
                to_status=current["status"],
                user_id=actor_id,
                notes="placeholder validation (no provider call)",
                metadata={"checks": checks, "ok": ok},
                conn=conn,
            )
            row = await _fetch_safe(conn, credential_id)
    result = mask_credential(row)
    result["_validation"] = {
        "ok": ok,
        "checks": checks,
        "external_action_performed": False,
        "note": "Placeholder validation only — no OAuth or provider API call.",
    }
    return result


async def rotate_credential_placeholder(
    credential_id: uuid.UUID, *, actor_id: uuid.UUID, is_admin: bool
) -> dict:
    """Simulate a credential rotation. No real secret exists to rotate; this
    clears any (placeholder) token state, bumps a rotation counter in metadata,
    and moves the record to needs_authorization (a real rotation would require
    re-auth). No provider call is made."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                "SELECT id, user_id, status, metadata "
                "FROM external_provider_credentials WHERE id = $1 FOR UPDATE",
                credential_id,
            )
            if current is None:
                raise CredentialError("credential not found", code="not_found")
            current = dict(current)
            _assert_can_manage(current, user_id=actor_id, is_admin=is_admin)
            from_status = current["status"]
            meta = dict(current.get("metadata") or {})
            meta["rotation_count"] = int(meta.get("rotation_count", 0)) + 1
            row = await conn.fetchrow(
                f"""
                UPDATE external_provider_credentials
                SET status = $1,
                    encrypted_access_token = NULL,
                    encrypted_refresh_token = NULL,
                    token_expires_at = NULL,
                    last_authorized_at = NULL,
                    metadata = $2,
                    dry_run_only = TRUE,
                    updated_by = $3,
                    updated_at = NOW()
                WHERE id = $4
                RETURNING {_SAFE_COLS}
                """,
                STATUS_NEEDS_AUTHORIZATION, meta, actor_id, credential_id,
            )
            row = dict(row)
            await write_credential_event(
                credential_id,
                event_type="placeholder_rotated",
                from_status=from_status,
                to_status=STATUS_NEEDS_AUTHORIZATION,
                user_id=actor_id,
                notes="placeholder rotation (no provider call); re-auth required",
                metadata={"rotation_count": meta["rotation_count"]},
                conn=conn,
            )
    return mask_credential(row)


async def list_credential_events(
    credential_id: uuid.UUID, *, actor_id: uuid.UUID, is_admin: bool
) -> list[dict]:
    """List a credential's events, enforcing the same visibility as get."""
    # get_credential raises not_found if the caller can't see it.
    await get_credential(credential_id, user_id=actor_id, is_admin=is_admin)
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, credential_id, user_id, event_type, from_status, "
            "to_status, notes, metadata, created_at "
            "FROM external_provider_credential_events "
            "WHERE credential_id = $1 ORDER BY created_at ASC",
            credential_id,
        )
    return [dict(r) for r in rows]
