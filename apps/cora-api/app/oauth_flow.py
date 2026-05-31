"""Real OAuth Flow v1.1 — secure connection service.

Implements the OAuth authorization-code flow for external providers: build the
authorization URL, exchange the callback code for tokens, encrypt + persist them
in the existing credential vault (provider_oauth_connectors), validate readiness,
and refresh tokens. Tokens are Fernet-encrypted at rest (CORA_CREDENTIAL_ENC_KEY)
and NEVER logged or returned to the UI (only has_* flags + metadata).

HARD SEPARATION: connecting a provider account does NOT enable execution. Real
provider execution (sending mail / creating events) stays blocked by the v0.8
kill switch and the v0.7 approval gate. This module performs NO Gmail / Outlook /
Calendar / Graph *execution* call — only OAuth token endpoints.
"""

import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app import execution_guard as guard
from app import oauth_providers as registry
from app import provider_oauth as vault
from app.clients import clients
from app.crypto import CryptoUnavailable, encrypt_secret, encryption_available
from app.runtime_traces import write_trace
from app.tools.governance import log_execution_attempt

logger = logging.getLogger(__name__)

# UI-facing connection statuses (spec #13).
ST_NOT_CONFIGURED = "not_configured"
ST_READY_TO_CONNECT = "ready_to_connect"
ST_CONNECTED = "connected"
ST_EXPIRED = "expired"
ST_REFRESH_FAILED = "refresh_failed"

_READINESS_TOOL = "oauth_readiness_checked"
_VALIDATE_TOOL = "oauth_connection_validated"
_STATE_TTL_MIN = 10


class OAuthError(Exception):
    """code: not_found (404) | invalid (400) | unavailable (503) |
    provider_error (502) | conflict (409)."""

    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


def _require_pool():
    if clients.db_pool is None:
        raise OAuthError("Postgres pool unavailable", code="unavailable")
    return clients.db_pool


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Authorization URL + state
# --------------------------------------------------------------------------- #

def build_authorization_url(provider: registry.OAuthProvider, *, state: str, cfg: dict) -> str:
    from urllib.parse import urlencode

    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": cfg["redirect_uri"],
        "response_type": "code",
        "scope": " ".join(provider.scopes),
        "state": state,
        **provider.extra_authorize_params,
    }
    return f"{provider.authorize_url}?{urlencode(params)}"


async def start_authorization(
    provider_name: str, *, user_id: uuid.UUID, workspace_id: Optional[uuid.UUID],
    is_admin: bool,
) -> dict:
    provider = registry.get_provider(provider_name)
    if provider is None:
        raise OAuthError(f"unknown provider {provider_name!r}", code="not_found")
    cfg = registry.provider_config(provider)
    if not registry.config_present(provider):
        await _trace(provider, "oauth_start_created", "failed", user_id,
                     workspace_id, error="provider OAuth client not configured")
        raise OAuthError(
            f"{provider_name} OAuth is not configured (missing client id/secret/redirect)",
            code="invalid",
        )
    state = secrets.token_urlsafe(32)
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO oauth_states (state, provider_name, user_id, workspace_id, redirect_uri)
            VALUES ($1, $2, $3, $4, $5)
            """,
            state, provider.name, user_id, workspace_id, cfg["redirect_uri"],
        )
    url = build_authorization_url(provider, state=state, cfg=cfg)
    await _trace(provider, "oauth_start_created", "ok", user_id, workspace_id,
                 result={"state": state, "scopes": provider.scopes})
    return {
        "provider_name": provider.name,
        "provider_type": provider.provider_type,
        "authorization_url": url,
        "state": state,
        "status": ST_READY_TO_CONNECT,
    }


# --------------------------------------------------------------------------- #
# Callback / token exchange
# --------------------------------------------------------------------------- #

async def _exchange_code_for_tokens(provider, cfg, code: str) -> dict:
    """POST to the provider token endpoint. Network call — only reached with real
    config + a real code. Returns the parsed token response; NEVER logged."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "redirect_uri": cfg["redirect_uri"],
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(provider.token_url, data=data)
    except httpx.HTTPError as exc:
        raise OAuthError(f"token endpoint unreachable: {exc!s}", code="provider_error")
    if resp.status_code != 200:
        # Do NOT log the body verbatim (may echo secrets); log status only.
        logger.warning("oauth token exchange failed for %s: HTTP %s",
                       provider.name, resp.status_code)
        raise OAuthError(f"token exchange failed (HTTP {resp.status_code})",
                         code="provider_error")
    return resp.json()


def _expires_at(token: dict) -> Optional[datetime]:
    secs = token.get("expires_in")
    if not secs:
        return None
    try:
        return _now() + timedelta(seconds=int(secs))
    except (TypeError, ValueError):
        return None


async def _consume_state(conn, state: str, provider_name: str) -> dict:
    row = await conn.fetchrow(
        "SELECT id, provider_name, user_id, workspace_id, created_at, consumed_at "
        "FROM oauth_states WHERE state = $1 FOR UPDATE",
        state,
    )
    if row is None:
        raise OAuthError("invalid or unknown state", code="invalid")
    if row["consumed_at"] is not None:
        raise OAuthError("state already used", code="conflict")
    if row["provider_name"] != provider_name:
        raise OAuthError("state/provider mismatch", code="invalid")
    if row["created_at"] < _now() - timedelta(minutes=_STATE_TTL_MIN):
        raise OAuthError("state expired", code="invalid")
    await conn.execute("UPDATE oauth_states SET consumed_at = NOW() WHERE id = $1", row["id"])
    return dict(row)


async def _upsert_connected(
    conn, *, user_id, workspace_id, provider, token: dict,
) -> dict:
    """Encrypt + persist tokens; mark the connector connected. Returns masked row."""
    if not encryption_available():
        raise OAuthError(
            "credential encryption unavailable (set CORA_CREDENTIAL_ENC_KEY)",
            code="unavailable",
        )
    access_enc = encrypt_secret(token.get("access_token"))
    refresh_enc = encrypt_secret(token.get("refresh_token"))
    expires_at = _expires_at(token)
    granted = token.get("scope")
    scopes = granted.split() if isinstance(granted, str) and granted else list(provider.scopes)
    metadata = {"connected_via": "oauth_v1.1", "vendor": provider.vendor, "refresh_failed": False}
    existing = await conn.fetchrow(
        "SELECT id, refresh_token_encrypted FROM provider_oauth_connectors "
        "WHERE user_id = $1 AND provider_name = $2 ORDER BY created_at DESC LIMIT 1",
        user_id, provider.name,
    )
    if existing is not None:
        # Keep the prior refresh token if the provider didn't return a new one.
        refresh_final = refresh_enc or existing["refresh_token_encrypted"]
        row = await conn.fetchrow(
            f"""
            UPDATE provider_oauth_connectors
            SET status = 'connected', scopes = $1, access_token_encrypted = $2,
                refresh_token_encrypted = $3, token_expires_at = $4,
                metadata = metadata || $5::jsonb, disconnected_at = NULL,
                updated_at = NOW()
            WHERE id = $6 RETURNING {vault._COLS}
            """,
            scopes, access_enc, refresh_final, expires_at, metadata, existing["id"],
        )
    else:
        row = await conn.fetchrow(
            f"""
            INSERT INTO provider_oauth_connectors
                (user_id, workspace_id, provider_name, provider_type, status,
                 scopes, access_token_encrypted, refresh_token_encrypted,
                 token_expires_at, metadata)
            VALUES ($1, $2, $3, $4, 'connected', $5, $6, $7, $8, $9)
            RETURNING {vault._COLS}
            """,
            user_id, workspace_id, provider.name, provider.provider_type,
            scopes, access_enc, refresh_enc, expires_at, metadata,
        )
    return vault.mask(dict(row))


async def handle_callback(provider_name: str, *, code: Optional[str], state: Optional[str],
                          error: Optional[str] = None) -> dict:
    provider = registry.get_provider(provider_name)
    if provider is None:
        raise OAuthError(f"unknown provider {provider_name!r}", code="not_found")
    pool = _require_pool()
    # Identify the initiating user from state first (callback is unauthenticated).
    async with pool.acquire() as conn:
        async with conn.transaction():
            if not state:
                raise OAuthError("missing state", code="invalid")
            st = await _consume_state(conn, state, provider.name)
            user_id = st["user_id"]
            workspace_id = st["workspace_id"]
            await _trace(provider, "oauth_callback_received", "ok", user_id, workspace_id,
                         result={"has_code": bool(code), "provider_error": error})
            if error or not code:
                msg = error or "missing authorization code"
                await _validate_log(user_id, provider, ok=False, msg=msg)
                await _trace(provider, "oauth_connection_failed", "failed", user_id,
                             workspace_id, error=msg)
                await _credential_event(provider, user_id=user_id, connector_id=None,
                                        event_type="oauth_connection_failed",
                                        status="failed", detail=msg)
                raise OAuthError(msg, code="invalid")
            cfg = registry.provider_config(provider)
            try:
                token = await _exchange_code_for_tokens(provider, cfg, code)
                connector = await _upsert_connected(
                    conn, user_id=user_id, workspace_id=workspace_id,
                    provider=provider, token=token,
                )
            except (OAuthError, CryptoUnavailable) as exc:
                await _validate_log(user_id, provider, ok=False, msg=str(exc))
                await _trace(provider, "oauth_connection_failed", "failed", user_id,
                             workspace_id, error=str(exc))
                await _credential_event(provider, user_id=user_id, connector_id=None,
                                        event_type="oauth_connection_failed",
                                        status="failed", detail=str(exc))
                raise exc if isinstance(exc, OAuthError) else OAuthError(str(exc), code="unavailable")
    await _validate_log(user_id, provider, ok=True, msg=None)
    await _trace(provider, "oauth_token_stored", "ok", user_id, workspace_id,
                 result={"connector_id": str(connector["id"]), "status": connector["status"],
                         "scopes": connector.get("scopes")})
    await _credential_event(provider, user_id=user_id, connector_id=connector["id"],
                            event_type="oauth_token_stored", status="connected",
                            detail="encrypted access/refresh tokens stored")
    return connector


# --------------------------------------------------------------------------- #
# Token refresh (service method — NOT used for execution this phase)
# --------------------------------------------------------------------------- #

async def refresh_connection(provider_name: str, *, user_id: uuid.UUID, is_admin: bool) -> dict:
    provider = registry.get_provider(provider_name)
    if provider is None:
        raise OAuthError(f"unknown provider {provider_name!r}", code="not_found")
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {vault._COLS} FROM provider_oauth_connectors "
            "WHERE user_id = $1 AND provider_name = $2 ORDER BY created_at DESC LIMIT 1",
            user_id, provider.name,
        )
    if row is None:
        raise OAuthError("no connector to refresh", code="not_found")
    connector = dict(row)
    await _trace(provider, "oauth_token_refresh_attempted", "ok", user_id,
                 connector.get("workspace_id"))
    from app.crypto import decrypt_secret
    try:
        refresh_plain = decrypt_secret(connector.get("refresh_token_encrypted"))
    except CryptoUnavailable as exc:
        await _trace(provider, "oauth_token_refresh_failed", "failed", user_id,
                     connector.get("workspace_id"), error=str(exc))
        raise OAuthError(str(exc), code="unavailable")
    if not refresh_plain:
        await _mark_refresh_failed(user_id, provider.name)
        await _trace(provider, "oauth_token_refresh_failed", "failed", user_id,
                     connector.get("workspace_id"), error="no refresh token stored")
        raise OAuthError("no refresh token stored", code="invalid")
    cfg = registry.provider_config(provider)
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_plain,
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(provider.token_url, data=data)
        if resp.status_code != 200:
            raise OAuthError(f"refresh failed (HTTP {resp.status_code})", code="provider_error")
        token = resp.json()
    except (httpx.HTTPError, OAuthError) as exc:
        await _mark_refresh_failed(user_id, provider.name)
        await _trace(provider, "oauth_token_refresh_failed", "failed", user_id,
                     connector.get("workspace_id"), error=str(exc))
        await _credential_event(provider, user_id=user_id, connector_id=connector.get("id"),
                                event_type="oauth_token_refresh_failed", status="failed",
                                detail=str(exc))
        raise exc if isinstance(exc, OAuthError) else OAuthError(str(exc), code="provider_error")
    # Persist the new access token (refresh token usually unchanged).
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE provider_oauth_connectors
            SET access_token_encrypted = $1, token_expires_at = $2, status = 'connected',
                metadata = metadata || '{"refresh_failed": false}'::jsonb, updated_at = NOW()
            WHERE user_id = $3 AND provider_name = $4
            """,
            encrypt_secret(token.get("access_token")), _expires_at(token),
            user_id, provider.name,
        )
    await _trace(provider, "oauth_token_refresh_succeeded", "ok", user_id,
                 connector.get("workspace_id"))
    await _credential_event(provider, user_id=user_id, connector_id=connector.get("id"),
                            event_type="oauth_token_refreshed", status="ok",
                            detail="access token rotated and re-encrypted")
    return await get_status(provider.name, user_id=user_id, is_admin=is_admin, log=False)


async def _mark_refresh_failed(user_id, provider_name):
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE provider_oauth_connectors "
            "SET metadata = metadata || '{\"refresh_failed\": true}'::jsonb, updated_at = NOW() "
            "WHERE user_id = $1 AND provider_name = $2",
            user_id, provider_name,
        )


# --------------------------------------------------------------------------- #
# Readiness + status
# --------------------------------------------------------------------------- #

def _token_expired(connector: dict) -> bool:
    exp = connector.get("token_expires_at")
    return bool(exp and exp <= _now())


def _compute_status(provider, cfg, connector: Optional[dict]) -> str:
    if not (cfg["client_id"] and cfg["client_secret"] and cfg["redirect_uri"]):
        return ST_NOT_CONFIGURED
    if connector is None or connector.get("status") in (None, "not_configured", "disconnected", "oauth_required"):
        return ST_READY_TO_CONNECT
    if (connector.get("metadata") or {}).get("refresh_failed"):
        return ST_REFRESH_FAILED
    if not connector.get("access_token_encrypted"):
        return ST_READY_TO_CONNECT
    if _token_expired(connector):
        return ST_EXPIRED
    if connector.get("status") == "error":
        return ST_REFRESH_FAILED
    return ST_CONNECTED


def _readiness_checks(provider, cfg, connector: Optional[dict]) -> dict:
    has_access = bool(connector and connector.get("access_token_encrypted"))
    has_refresh = bool(connector and connector.get("refresh_token_encrypted"))
    expired = bool(connector and _token_expired(connector))
    return {
        "client_id_present": bool(cfg["client_id"]),
        "client_secret_present": bool(cfg["client_secret"]),
        "redirect_uri_present": bool(cfg["redirect_uri"]),
        "required_scopes_present": bool(provider.scopes),
        "token_exists": has_access,
        "refresh_token_exists": has_refresh or not provider.requires_refresh_token,
        "token_valid_or_refreshable": (has_access and not expired) or has_refresh,
    }


async def _get_connector_row(provider_name: str, user_id: uuid.UUID) -> Optional[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {vault._COLS} FROM provider_oauth_connectors "
            "WHERE user_id = $1 AND provider_name = $2 ORDER BY created_at DESC LIMIT 1",
            user_id, provider_name,
        )
    return dict(row) if row else None


async def get_status(provider_name: str, *, user_id: uuid.UUID, is_admin: bool,
                     log: bool = True) -> dict:
    provider = registry.get_provider(provider_name)
    if provider is None:
        raise OAuthError(f"unknown provider {provider_name!r}", code="not_found")
    cfg = registry.provider_config(provider)
    connector = await _get_connector_row(provider.name, user_id)
    checks = _readiness_checks(provider, cfg, connector)
    status = _compute_status(provider, cfg, connector)
    masked = vault.mask(connector) if connector else None
    # Exact missing-config labels so the UI can show precisely which env vars are
    # absent (spec #5) instead of a generic "configure OAuth env vars".
    missing_config = [
        label for label, present in (
            ("client_id", bool(cfg["client_id"])),
            ("client_secret", bool(cfg["client_secret"])),
            ("redirect_uri", bool(cfg["redirect_uri"])),
        ) if not present
    ]
    result = {
        "provider_name": provider.name,
        "provider_type": provider.provider_type,
        "vendor": provider.vendor,
        "config_present": registry.config_present(provider),
        "missing_config": missing_config,
        "required_scopes": list(provider.scopes),
        "connection_status": status,
        # connector_id lets the UI target Disconnect for a connected provider.
        "connector_id": str(masked["id"]) if masked and masked.get("id") else None,
        "readiness": checks,
        "ready_to_connect": status == ST_READY_TO_CONNECT,
        "connected": status == ST_CONNECTED,
        "scopes": (masked or {}).get("scopes") or [],
        "token_expires_at": (masked or {}).get("token_expires_at"),
        "has_access_token": bool((masked or {}).get("has_access_token")),
        "has_refresh_token": bool((masked or {}).get("has_refresh_token")),
        "updated_at": (masked or {}).get("updated_at"),
        "encryption_available": encryption_available(),
        # Hard separation: connection NEVER implies execution.
        "execution_enabled": guard.external_execution_enabled(),
        "execution_note": (
            "Execution is disabled by the global safety guard. A connected "
            "provider still cannot send mail or create events."
        ),
    }
    if log:
        await log_execution_attempt(
            tool_name=_READINESS_TOOL, agent_name=None, session_id=None,
            user_id=user_id, scope_type=None, allowed=True, duration_ms=None,
            status="success", error_message=None,
        )
    return result


async def list_provider_status(*, user_id: uuid.UUID, is_admin: bool) -> dict:
    providers = []
    for name in registry.PROVIDERS:
        providers.append(await get_status(name, user_id=user_id, is_admin=is_admin, log=False))
    await log_execution_attempt(
        tool_name=_READINESS_TOOL, agent_name=None, session_id=None,
        user_id=user_id, scope_type=None, allowed=True, duration_ms=None,
        status="success", error_message=None,
    )
    return {
        "execution_enabled": guard.external_execution_enabled(),
        "encryption_available": encryption_available(),
        "providers": providers,
    }


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #

async def _trace(provider, trace_type, status, user_id, workspace_id, *,
                 result: Optional[dict] = None, error: Optional[str] = None):
    await write_trace(
        session_id=None, user_id=user_id, trace_type=trace_type, status=status,
        selected_agent=None, tool_name="oauth_flow",
        tool_result={"provider_name": provider.name, "provider_type": provider.provider_type,
                     **(result or {})},
        error_message=error, workspace_id=workspace_id,
    )


async def _credential_event(provider, *, user_id, connector_id, event_type, status,
                            detail: Optional[str] = None):
    """Record a credential lifecycle event (spec #5). No token material is stored —
    `detail` is a short non-secret note only. Best-effort: a logging failure must
    not abort a real connection, but is surfaced in the app log."""
    pool = clients.db_pool
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO provider_oauth_connector_events
                    (connector_id, user_id, provider_name, event_type, status, detail)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                connector_id, user_id, provider.name, event_type, status,
                (detail[:500] if detail else None),
            )
    except Exception:  # noqa: BLE001 — audit must never break the flow
        logger.exception("failed to write provider_oauth_connector_event %s/%s",
                         provider.name, event_type)


async def _validate_log(user_id, provider, *, ok: bool, msg: Optional[str]):
    await log_execution_attempt(
        tool_name=_VALIDATE_TOOL, agent_name=None, session_id=None,
        user_id=user_id, scope_type=None, allowed=ok, duration_ms=None,
        status="success" if ok else "failed", error_message=msg,
    )
