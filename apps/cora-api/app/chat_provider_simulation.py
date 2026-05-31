"""Chat-Native Provider Simulation & Payload Inspection v2.1.

Inspect provider payloads in chat — "Show me exactly what Gmail would receive",
"Simulate this email", "Compare Gmail and Outlook payloads", "What would be sent if
execution were enabled?". Reuses the v1.6 adapter framework + v1.3 credential
snapshot + v1.7 feature flags to render a SIMULATION-ONLY provider payload plus a
readiness/safety summary.

NOTHING is sent: no Gmail / Microsoft Graph / Calendar API is ever called, and NO
access/refresh tokens or credential secrets are exposed. Resolves the active draft /
integration intent / selected provider from the v1.9 chat context. Emits
chat_provider_simulation_requested / chat_provider_payload_inspected /
chat_provider_comparison_generated traces and provider_payload_viewed /
provider_simulation_generated integration events.
"""

import logging
import uuid
from typing import Optional

from app.clients import clients
from app import chat_email_lifecycle as cel
from app import execution_adapters as eadapt
from app import execution_guard as guard
from app import feature_flags as ff
from app import integration_readiness as ir
from app import provider_credential_simulation as pcs
from app import provider_execution as pexec
from app.runtime_traces import write_trace

logger = logging.getLogger(__name__)

SIGNAL = "SIGNAL"
ACTION = "send_email"

TRACE_REQUESTED = "chat_provider_simulation_requested"
TRACE_INSPECTED = "chat_provider_payload_inspected"
TRACE_COMPARED = "chat_provider_comparison_generated"
EV_VIEWED = "provider_payload_viewed"
EV_SIMULATED = "provider_simulation_generated"

_PROVIDER_LABEL = {"gmail": "Gmail", "outlook_mail": "Outlook"}


def _trunc(v, n=200):
    if v is None:
        return ""
    s = str(v)
    return s if len(s) <= n else s[:n] + "…"


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def detect_simulation_command(message: str) -> Optional[tuple[str, Optional[str]]]:
    """Return (kind, provider) — kind in {inspect, compare}; provider in
    {gmail, outlook_mail, None} (None = use the intent's selected provider)."""
    m = (message or "").lower().strip()
    if not m:
        return None
    mentions_gmail = "gmail" in m or "google mail" in m
    mentions_outlook = "outlook" in m or "microsoft" in m

    if "compare" in m and (mentions_gmail or mentions_outlook or "payload" in m or "provider" in m):
        return ("compare", None)

    is_sim = (
        ("simulate" in m and any(w in m for w in ("email", "payload", "provider", "this")))
        or ("show" in m and ("provider payload" in m or "simulated email" in m
                             or "what" in m and "receive" in m))
        or ("preview" in m and ("provider" in m or "payload" in m or "email" in m))
        or ("inspect" in m and ("payload" in m or mentions_gmail or mentions_outlook))
        or ("what would" in m and ("sent" in m or "receive" in m))
        or ("exactly what" in m and "receive" in m)
    )
    if not is_sim:
        return None
    if mentions_gmail and not mentions_outlook:
        return ("inspect", "gmail")
    if mentions_outlook and not mentions_gmail:
        return ("inspect", "outlook_mail")
    return ("inspect", None)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

async def _trace(session_id, user_id, workspace_id, *, trace_type, status="ok", result=None):
    await write_trace(
        session_id=session_id, user_id=user_id, trace_type=trace_type, status=status,
        selected_agent=SIGNAL, tool_name="chat_provider_simulation",
        tool_result=result or {}, workspace_id=workspace_id)


async def _event(intent_id, user_id, *, event_type, snapshot, status):
    pool = clients.db_pool
    async with pool.acquire() as conn:
        await ir._insert_event(conn, intent_id, user_id, event_type=event_type,
                               from_status=status, to_status=status, notes=None,
                               payload_snapshot=snapshot)


def _summary(snap: dict, flag: Optional[dict], validation: list) -> dict:
    """Provider simulation summary (spec #7). No secrets — presence/status only."""
    v = snap["validation"]
    return {
        "payload_valid": not validation,
        "provider_ready": v["provider_connected"],
        "oauth_status": snap.get("connector_status"),
        "scope_validation": ("ok" if v["required_scopes_present"]
                             else f"missing: {', '.join(v['missing_scopes']) or 'unknown'}"),
        "feature_flag_status": (
            f"enabled={flag['enabled']}, dry_run_only={flag['dry_run_only']}"
            if flag else "missing (fail-closed)"),
        "final_interlock_status": (
            "real execution NOT allowed — external execution disabled"
            if not guard.external_execution_enabled() else "gated"),
    }


def _render_payload(provider: str, pp: dict, validation: list, summary: dict) -> str:
    req = pp.get("request") or {}
    to = req.get("to") or []
    label = _PROVIDER_LABEL.get(provider, provider)
    return (
        f"**Simulated {label} payload** (preview only — nothing is sent)\n"
        f"- Provider: **{provider}** · API method: `{pp.get('api_method', '—')}` · "
        f"would_send: **{pp.get('would_send', False)}**\n"
        f"- Action type: **{ACTION}**\n"
        f"- Recipients: **{len(to)}**" + (f" ({', '.join(map(str, to))})" if to else "") + "\n"
        f"- Subject: {req.get('subject') or '—'}\n"
        f"- Body preview: {_trunc(req.get('body_preview'), 200) or '—'}\n"
        f"- Attachments: **{len(req.get('attachments') or [])}**\n"
        f"- Validation: **{'valid' if not validation else 'invalid — ' + '; '.join(validation)}**\n"
        f"- Safety: external execution **disabled** (real_execution_allowed=false)\n\n"
        "**Summary**\n"
        f"- Payload valid: **{summary['payload_valid']}**\n"
        f"- Provider ready: **{summary['provider_ready']}**\n"
        f"- OAuth status: **{summary['oauth_status']}**\n"
        f"- Scope validation: **{summary['scope_validation']}**\n"
        f"- Feature flag: **{summary['feature_flag_status']}**\n"
        f"- Final interlock: **{summary['final_interlock_status']}**"
    )


def _render_comparison(snap, gmail_pp, outlook_pp, validation) -> str:
    def row(pp):
        req = pp.get("request") or {}
        return (f"  - API method: `{pp.get('api_method', '—')}`\n"
                f"  - Recipients: {len(req.get('to') or [])}\n"
                f"  - Subject: {req.get('subject') or '—'}\n"
                f"  - Body preview: {_trunc(req.get('body_preview'), 120) or '—'}\n"
                f"  - would_send: {pp.get('would_send', False)}")
    return (
        "**Gmail vs Outlook payload comparison** (simulation only — nothing is sent)\n\n"
        f"**Gmail**\n{row(gmail_pp)}\n\n"
        f"**Outlook**\n{row(outlook_pp)}\n\n"
        f"Shared content validation: **{'valid' if not validation else 'invalid'}**. "
        "Both are previews; external execution is **disabled**, so neither would be "
        "transmitted in this phase."
    )


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #

async def handle_simulation_command(
    cmd: tuple[str, Optional[str]], *, message: str, session_uuid: uuid.UUID,
    user_id: uuid.UUID, workspace_uuid: Optional[uuid.UUID], is_admin: bool,
) -> tuple[bool, Optional[str]]:
    kind, provider = cmd
    ctx = await cel.get_context(session_uuid)
    intent_id = ctx.get("last_integration_intent_id")
    await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_REQUESTED,
                 result={"kind": kind, "provider": provider,
                         "intent_id": str(intent_id) if intent_id else None})
    if intent_id is None:
        return True, ("There's no prepared provider intent in this conversation yet. "
                      "Approve a draft and say \"Prepare it for Gmail\" first, then I can "
                      "simulate or inspect the payload.")
    intent = await ir.get_intent(intent_id)
    if intent is None or (not is_admin and intent["created_by"] != user_id):
        return True, "I couldn't find that integration intent."

    snap = await pcs.credential_snapshot(intent, user_id=user_id)
    payload = pexec._build_execution_payload(intent)

    if kind == "compare":
        g = eadapt.resolve_adapter("gmail", ACTION)
        o = eadapt.resolve_adapter("outlook_mail", ACTION)
        gmail_pp = g.build_provider_payload(ACTION, payload)
        outlook_pp = o.build_provider_payload(ACTION, payload)
        validation = g.validate_payload(ACTION, payload)
        await _event(intent_id, user_id, event_type=EV_SIMULATED, status=intent.get("status"),
                     snapshot={"mode": "compare", "providers": ["gmail", "outlook_mail"],
                               "payload_valid": not validation})
        await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_COMPARED,
                     result={"intent_id": str(intent_id), "payload_valid": not validation})
        return True, _render_comparison(snap, gmail_pp, outlook_pp, validation)

    # inspect — resolve the provider (explicit, else the intent's selected provider).
    resolved = provider or intent.get("provider_name") or snap.get("provider_name") or "gmail"
    if resolved not in ("gmail", "outlook_mail"):
        resolved = "gmail"
    adapter = eadapt.resolve_adapter(resolved, ACTION)
    if adapter is None:
        return True, f"No execution adapter is available for {resolved}."
    pp = adapter.build_provider_payload(ACTION, payload)
    validation = adapter.validate_payload(ACTION, payload)
    flag = await ff.get_flag(resolved, ACTION)
    summary = _summary(snap, flag, validation)

    await _event(intent_id, user_id, event_type=EV_VIEWED, status=intent.get("status"),
                 snapshot={"provider": resolved, "recipient_count": len(pp["request"].get("to") or []),
                           "payload_valid": not validation})
    await _event(intent_id, user_id, event_type=EV_SIMULATED, status=intent.get("status"),
                 snapshot={"provider": resolved, "payload_ready": not validation,
                           "api_method": pp.get("api_method")})
    await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_INSPECTED,
                 result={"intent_id": str(intent_id), "provider": resolved,
                         "payload_valid": not validation,
                         "provider_ready": summary["provider_ready"]})
    return True, _render_payload(resolved, pp, validation, summary)
