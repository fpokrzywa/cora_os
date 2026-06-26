"""Chat-Native Inbox Assistant (v2.3 governance gate + v2.7 live read).

Answer read-only mailbox questions — "Show my latest emails", "Search my inbox for
emails from Mark", "Summarize unread emails", "Summarize this email thread", "Draft
a reply to this email (do not send)". Access is GOVERNED and FAILS CLOSED: it
requires the provider connected + a valid/refreshable token + the read scope
(gmail.readonly / Mail.Read) present + an enabled `inbox_read` feature flag. With
the gate passed, the token broker (`_get_access_token`) decrypts — refreshing via
oauth_flow if expiring — the caller's OWN access token and hands it to the live
read-only adapter for that single call; the token is never logged, traced, or
included in any response. With the gate closed (the production default until an
operator grants a read scope + enables the flag), every inbox read is denied
without calling any provider API. No send/reply/forward/delete/archive is ever
performed; "draft a reply" creates an INTERNAL SIGNAL draft only, linked to the
source email. Inbox access is audited (inbox_access_events) + traced.
"""

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.clients import clients
from app import chat_email_lifecycle as cel
from app import feature_flags as ff
from app import inbox_adapters
from app import oauth_flow
from app import signal_tools
from app.crypto import decrypt_secret
from app.oauth_readiness import _scope_tail
from app.runtime_traces import write_trace

logger = logging.getLogger(__name__)

SIGNAL = "SIGNAL"
TRACE_SEARCH = "chat_inbox_search_requested"
TRACE_LISTED = "chat_inbox_messages_listed"
TRACE_READ = "chat_inbox_message_read"
TRACE_SUMMARY = "chat_inbox_summary_generated"
TRACE_REPLY = "chat_inbox_draft_reply_created"
TRACE_CAPABILITY_DENIED = "chat_inbox_capability_denied"
TRACE_READ_FAILED = "chat_inbox_provider_read_failed"

# Refresh the access token when it expires within this window.
TOKEN_REFRESH_MARGIN_SECONDS = 120


def _trunc(v, n=80):
    s = "" if v is None else str(v)
    return s if len(s) <= n else s[:n] + "…"


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def _extract_query(m: str) -> Optional[str]:
    # "from X" → a SENDER-scoped search via the providers' `from:` operator
    # (Gmail `q` and Graph `$search` both understand `from:`), so "emails from
    # Strawberry" matches the sender, not every message containing the word.
    # Other keywords stay full-text.
    if " from " in m:
        val = m.split(" from ", 1)[1].strip().rstrip("?.! ")[:120]
        return f"from:{val}" if val else None
    for kw in (" about ", " for ", " containing ", " mentioning "):
        if kw in m:
            return m.split(kw, 1)[1].strip().rstrip("?.! ")[:120] or None
    return None


def detect_inbox_command(message: str) -> Optional[tuple[str, Optional[str]]]:
    m = (message or "").lower().strip()
    if not m:
        return None
    # Draft a reply to an inbox email (must beat the v1.9 "draft an email" create).
    if "draft a reply" in m or ("reply" in m and ("this email" in m or "this thread" in m
                                                  or "to this" in m or "to that" in m)):
        return ("draft_reply", None)
    if ("summariz" in m or "summaris" in m) and ("thread" in m):
        return ("read_thread", None)
    if ("summariz" in m or "summaris" in m) and ("inbox" in m or "unread" in m
                                                 or "emails" in m or "email" in m):
        return ("summarize", None)
    if ("search" in m and ("inbox" in m or "email" in m)) or "find emails" in m \
            or "find email" in m or "emails from" in m or "emails about" in m:
        return ("search", _extract_query(m))
    if (("show" in m or "list" in m) and ("my emails" in m or "my inbox" in m
                                          or "latest emails" in m or "recent emails" in m
                                          or "my mail" in m)) \
            or "latest emails" in m or "recent emails" in m \
            or "emails need my attention" in m or "what emails need my attention" in m:
        return ("list", None)
    return None


# --------------------------------------------------------------------------- #
# Governance gate (fail-closed) + audit
# --------------------------------------------------------------------------- #

async def _resolve_provider(message: str, user_id: uuid.UUID) -> str:
    m = (message or "").lower()
    if "outlook" in m or "microsoft" in m:
        return "outlook_mail"
    if "gmail" in m or "google" in m:
        return "gmail"
    pool = clients.db_pool
    async with pool.acquire() as conn:
        row = await conn.fetchval(
            "SELECT provider_name FROM provider_oauth_connectors "
            "WHERE user_id=$1 AND provider_type='email' AND status='connected' "
            "ORDER BY created_at DESC LIMIT 1", user_id)
    return row or "gmail"


async def _gate(provider: str, user_id: uuid.UUID) -> dict:
    """Fail-closed inbox-read decision (spec #9): provider OAuth connected AND
    read scopes present AND provider supports_read=TRUE (capability registry) AND
    inbox_read feature flag enabled. Reads connection status/scopes/token presence
    + the capability flag only — NEVER the token columns. Connection status source
    of truth = provider_oauth_connectors (v1.1 OAuth vault); read capability source
    of truth = external_provider_connectors (v0.5 registry)."""
    pool = clients.db_pool
    async with pool.acquire() as conn:
        c = await conn.fetchrow(
            "SELECT status, scopes, (access_token_encrypted IS NOT NULL) AS has_access, "
            "(refresh_token_encrypted IS NOT NULL) AS has_refresh, token_expires_at "
            "FROM provider_oauth_connectors WHERE user_id=$1 AND provider_name=$2 "
            "AND status<>'disconnected' ORDER BY (status='connected') DESC, created_at DESC LIMIT 1",
            user_id, provider)
        # Read capability from the connector registry (not connection state).
        cap = await conn.fetchrow(
            "SELECT supports_read, supports_send, dry_run_only FROM external_provider_connectors "
            "WHERE provider_name=$1", provider)
    connected = bool(c and c["status"] == "connected")
    exp = c["token_expires_at"] if c else None
    token_ok = bool(c and (c["has_access"] and not (exp and exp <= datetime.now(timezone.utc))
                           or c["has_refresh"]))
    read_scope = inbox_adapters.READ_SCOPES.get(provider, "")
    granted = {_scope_tail(s) for s in (c["scopes"] if c else [])}
    scope_ok = _scope_tail(read_scope) in granted
    supports_read = bool(cap and cap["supports_read"])
    supports_send = bool(cap and cap["supports_send"])
    flag = await ff.get_flag(provider, "inbox_read")
    flag_ok = bool(flag and flag["enabled"])
    reasons = []
    if not connected: reasons.append("provider not connected")
    if not token_ok: reasons.append("no valid/refreshable token")
    if not scope_ok: reasons.append(f"read scope missing ({_scope_tail(read_scope)})")
    if not supports_read: reasons.append("provider supports_read=false (capability mismatch)")
    if not flag_ok: reasons.append("inbox_read feature flag disabled (fail-closed)")
    allowed = connected and token_ok and scope_ok and supports_read and flag_ok
    return {"allowed": allowed, "connected": connected, "token_ok": token_ok,
            "scope_ok": scope_ok, "supports_read": supports_read,
            "supports_send": supports_send, "capability_mismatch": not supports_read,
            "flag_ok": flag_ok, "reason": "; ".join(reasons) or "all checks pass"}


async def _get_access_token(provider: str, user_id: uuid.UUID) -> Optional[str]:
    """Token broker (v2.7) — only called AFTER `_gate` passes. Decrypts the
    caller's own connected access token, refreshing first via oauth_flow when it
    expires inside the margin and a refresh token exists. Returns None on any
    failure (callers deny gracefully). The plaintext token is handed straight to
    the adapter call — never logged, traced, stored, or rendered."""
    pool = clients.db_pool

    async def _fetch():
        async with pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT access_token_encrypted, token_expires_at, "
                "(refresh_token_encrypted IS NOT NULL) AS has_refresh "
                "FROM provider_oauth_connectors WHERE user_id=$1 AND provider_name=$2 "
                "AND status='connected' ORDER BY created_at DESC LIMIT 1",
                user_id, provider)

    row = await _fetch()
    if row is None:
        return None
    exp = row["token_expires_at"]
    expiring = bool(exp and exp <= datetime.now(timezone.utc)
                    + timedelta(seconds=TOKEN_REFRESH_MARGIN_SECONDS))
    if expiring and row["has_refresh"]:
        try:
            await oauth_flow.refresh_connection(provider, user_id=user_id,
                                                is_admin=False)
            row = await _fetch()
        except oauth_flow.OAuthError as exc:
            logger.warning("inbox token refresh failed: provider=%s err=%s",
                           provider, exc)
            return None
        if row is None:
            return None
    try:
        return decrypt_secret(row["access_token_encrypted"])
    except Exception:
        logger.warning("inbox token decrypt failed: provider=%s", provider)
        return None


async def _audit(user_id, workspace_id, provider, action, allowed, reason, message_ref=None):
    pool = clients.db_pool
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO inbox_access_events (user_id, workspace_id, provider, action, "
            "allowed, reason, message_ref) VALUES ($1,$2,$3,$4,$5,$6,$7)",
            user_id, workspace_id, provider, action, allowed, reason[:300] if reason else None,
            message_ref)


async def _trace(session_id, user_id, workspace_id, *, trace_type, status="ok", result=None):
    await write_trace(
        session_id=session_id, user_id=user_id, trace_type=trace_type, status=status,
        selected_agent=SIGNAL, tool_name="chat_inbox", tool_result=result or {},
        workspace_id=workspace_id)


def _status_line(decision) -> str:
    """Spec #8 status logic — read-only availability vs send."""
    read = "available" if decision.get("supports_read") else "unavailable"
    send = "enabled" if decision.get("supports_send") else "disabled"
    return (f"Read-only: **{read}** · Send: **{send}** · Internal drafts: **allowed**")


def _blocked_msg(provider, decision) -> str:
    return (f"🔒 I can't read your {provider} inbox — inbox access is governed and "
            f"currently **disabled** ({decision['reason']}). Read-only inbox access "
            "requires the provider connected with a read scope, the provider's "
            "read-only capability, and an enabled `inbox_read` feature flag "
            "(separate from send_email). No mailbox data was accessed and nothing "
            f"was sent.\n\n{_status_line(decision)}")


# --------------------------------------------------------------------------- #
# Rendering (source metadata — spec #7)
# --------------------------------------------------------------------------- #

def _render_list(provider, msgs) -> str:
    lines = [f"**{provider} — {len(msgs)} message(s)** (read-only)"]
    for i, mm in enumerate(msgs, 1):
        lines.append(f"{i}. [{provider}] `{str(mm.get('id'))[:10]}` · from {mm.get('from','—')} · "
                     f"{_trunc(mm.get('subject'))} · {mm.get('date','—')}")
    lines.append("\n_Safe actions: summarize · draft reply · create follow-up draft._")
    return "\n".join(lines)


def _render_summary(provider, msgs) -> str:
    lines = [f"**Inbox summary — {provider}** ({len(msgs)} message(s), read-only)"]
    for mm in msgs:
        lines.append(f"- `{str(mm.get('id'))[:10]}` · **{_trunc(mm.get('subject'))}** · "
                     f"from {mm.get('from','—')} · {mm.get('date','—')}"
                     + (f"\n  {_trunc(mm.get('snippet'), 140)}" if mm.get('snippet') else ""))
    lines.append("\n_Safe actions: draft reply · create follow-up draft. Nothing was sent._")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #

async def handle_inbox_command(
    cmd: tuple[str, Optional[str]], *, message: str, session_uuid: uuid.UUID,
    user_id: uuid.UUID, workspace_uuid: Optional[uuid.UUID], scope_type: str,
    is_admin: bool,
) -> tuple[bool, Optional[str]]:
    kind, query = cmd
    provider = await _resolve_provider(message, user_id)
    req_trace = {"list": TRACE_LISTED, "search": TRACE_SEARCH, "summarize": TRACE_SUMMARY,
                 "read_thread": TRACE_READ, "draft_reply": TRACE_REPLY}[kind]
    await _trace(session_uuid, user_id, workspace_uuid, trace_type=req_trace, status="ok",
                 result={"provider": provider, "kind": kind, "query": query})

    decision = await _gate(provider, user_id)
    if not decision["allowed"]:
        await _audit(user_id, workspace_uuid, provider, kind, False, decision["reason"])
        # Distinct trace when the denial is specifically a capability mismatch
        # (provider supports_read=false) — vs missing scope/flag (deliverable).
        if decision.get("capability_mismatch"):
            await _trace(session_uuid, user_id, workspace_uuid,
                         trace_type=TRACE_CAPABILITY_DENIED, status="blocked",
                         result={"provider": provider, "kind": kind,
                                 "reason": "supports_read=false",
                                 "supports_read": False})
        return True, _blocked_msg(provider, decision)

    adapter = inbox_adapters.resolve_inbox_adapter(provider)
    if adapter is None:
        await _audit(user_id, workspace_uuid, provider, kind, False, "no inbox adapter")
        return True, f"No read-only inbox adapter is available for {provider}."

    # Gate passed — obtain the caller's own access token (broker; never logged)
    # and perform the read-only operation. Never sends/replies/deletes.
    token = await _get_access_token(provider, user_id)
    if not token:
        await _audit(user_id, workspace_uuid, provider, kind, False,
                     "no usable access token (broker)")
        return True, (f"🔒 Inbox read for {provider} is enabled by policy, but I "
                      "couldn't obtain a usable access token (it may need to be "
                      "reconnected). No mailbox data was accessed and nothing was sent.")
    try:
        if kind in ("list", "summarize"):
            msgs = await adapter.list_messages(access_token=token, limit=10)
        elif kind == "search":
            msgs = await adapter.search_messages(access_token=token,
                                                 query=query or "", limit=10)
        else:  # read_thread / draft_reply both start from the latest message
            msgs = [await adapter.read_message(access_token=token,
                                               message_id="latest")]
    except inbox_adapters.InboxReadDisabled as exc:
        await _audit(user_id, workspace_uuid, provider, kind, False, str(exc))
        return True, ("🔒 Inbox read is enabled by policy but no live read could be "
                      "performed — no mailbox data was accessed and nothing was sent.")
    except inbox_adapters.InboxReadError as exc:
        await _audit(user_id, workspace_uuid, provider, kind, False, str(exc))
        await _trace(session_uuid, user_id, workspace_uuid,
                     trace_type=TRACE_READ_FAILED, status="error",
                     result={"provider": provider, "kind": kind, "error": str(exc)})
        return True, (f"⚠️ The {provider} read-only request failed ({exc}). No "
                      "mailbox changes were made and nothing was sent.")

    # Reached only when a real read returned data (e.g., a future live connector).
    if kind == "draft_reply":
        return True, await _draft_reply(provider, msgs[0], session_uuid=session_uuid,
                                        user_id=user_id, workspace_uuid=workspace_uuid)
    await _audit(user_id, workspace_uuid, provider, kind, True, "read ok",
                 message_ref=(msgs[0].get("id") if msgs else None))
    if kind == "summarize":
        await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_SUMMARY,
                     result={"provider": provider, "count": len(msgs)})
        return True, _render_summary(provider, msgs)
    if kind == "read_thread":
        await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_READ,
                     result={"provider": provider, "count": len(msgs)})
    return True, _render_list(provider, msgs)


async def _draft_reply(provider, src, *, session_uuid, user_id, workspace_uuid) -> str:
    """Create an INTERNAL SIGNAL draft reply linked to the source email. Never
    sends (spec #10)."""
    subject = src.get("subject") or ""
    re_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    body = (f"Hi {src.get('from','there')},\n\n[Your reply here]\n\n"
            "— Draft reply prepared internally (not sent).")
    row = await signal_tools.create_communication_draft(
        workspace_id=workspace_uuid, user_id=user_id, draft_type="email",
        title=re_subject, subject=re_subject, body=body,
        recipient_hint=src.get("from"),
        metadata={"source": "inbox_reply", "session_id": str(session_uuid),
                  "source_email": {"provider": provider, "message_id": src.get("id"),
                                   "from": src.get("from"), "subject": subject,
                                   "date": src.get("date")}})
    await cel.set_context(session_uuid, current_active_draft_id=row["id"],
                          last_created_draft_id=row["id"])
    await _audit(user_id, workspace_uuid, provider, "draft_reply", True,
                 "internal reply draft created", message_ref=src.get("id"))
    await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_REPLY,
                 result={"draft_id": str(row["id"]), "provider": provider,
                         "source_message_id": src.get("id")})
    return ("✓ Created an internal reply **draft** (nothing sent), linked to the source "
            f"email from {src.get('from','—')}.\n\n" + cel.format_draft(row))
