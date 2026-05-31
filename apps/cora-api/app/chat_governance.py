"""Chat-Native Governance Explanation & Audit Trail v2.0.

Answer governance/audit questions in chat — "Why was this blocked?", "Who approved
this draft?", "What happened to my Gmail intent?", "Show me the governance trail",
"Why is execution disabled?" — by reading the existing audit tables (runtime_traces,
tool_execution_logs, draft_review_events, external_integration_events,
provider_execution_feature_flags, provider_oauth_connectors) and rendering a
human-readable explanation + chronological timeline.

READ-ONLY: no mutation, no provider execution, and NO secrets — OAuth access/refresh
tokens and credential payloads are never selected or returned. Resolves the active
draft/intent from the v1.9 chat context. Emits governance_explanation_requested and
governance_timeline_generated runtime traces.
"""

import logging
import uuid
from typing import Optional

from app.clients import clients
from app import chat_email_lifecycle as cel
from app import execution_guard as guard
from app import feature_flags as ff
from app import signal_tools
from app.runtime_traces import write_trace

logger = logging.getLogger(__name__)

SIGNAL = "SIGNAL"
TRACE_REQUESTED = "governance_explanation_requested"
TRACE_TIMELINE = "governance_timeline_generated"

# Trace types that represent a governance block/denial.
_BLOCK_TRACES = (
    "governance_blocked", "external_execution_blocked", "provider_flag_denied",
    "final_interlock_blocked", "final_interlock_ready_but_disabled",
    "provider_adapter_execution_blocked", "provider_execution_blocked_by_governance",
)
# Trace types worth showing on a governance timeline for an intent.
_TIMELINE_TRACES = _BLOCK_TRACES + (
    "integration_intent_created", "provider_credential_resolved",
    "provider_payload_simulated", "final_interlock_checked",
    "provider_adapter_resolved", "execution_approval_approved",
    "execution_approval_rejected", "chat_email_intent_prepared",
    "chat_email_provider_simulated", "chat_email_safety_check_run",
)


def _fmt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "—"


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def detect_governance_question(message: str) -> Optional[str]:
    """Classify a chat message into a governance/audit question, or None."""
    m = (message or "").lower().strip()
    if not m:
        return None
    if ("execution" in m and any(w in m for w in ("disabled", "off", "blocked"))
            and "why" in m):
        return "why_execution_disabled"
    if any(p in m for p in ("governance trail", "audit trail", "governance log")) \
            or ("show" in m and "governance" in m):
        return "governance_trail"
    if "approval history" in m or ("review history" in m) or \
            ("show" in m and "approval" in m and "history" in m):
        return "approval_history"
    if "who" in m and any(w in m for w in ("approved", "signed off", "reviewed")):
        return "who_approved"
    if ("what happened" in m or "status of" in m or "what's the status" in m) and \
            any(w in m for w in ("intent", "gmail", "outlook", "email", "provider")):
        return "what_happened_intent"
    if ("what" in m and ("failed" in m or "fail" in m or "went wrong" in m)) or \
            "validation" in m and ("fail" in m or "error" in m):
        return "what_failed"
    if "why" in m and any(w in m for w in ("block", "blocked", "can't", "cannot",
                                           "can not", "won't send", "not sent")):
        return "why_blocked"
    return None


# --------------------------------------------------------------------------- #
# Context resolution
# --------------------------------------------------------------------------- #

async def _resolve(session_uuid, user_id, workspace_uuid, is_admin):
    ctx = await cel.get_context(session_uuid)
    draft = None
    did = ctx.get("current_active_draft_id")
    if did is not None:
        draft = cel._visible(await signal_tools.get_draft(did),
                             user_id=user_id, workspace_uuid=workspace_uuid, is_admin=is_admin)
    intent_id = ctx.get("last_integration_intent_id")
    return draft, intent_id


# --------------------------------------------------------------------------- #
# Audit queries (no secrets)
# --------------------------------------------------------------------------- #

async def _intent_row(intent_id) -> Optional[dict]:
    pool = clients.db_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, created_by, provider_type, provider_name, action_type, status, "
            "dry_run, requires_confirmation, source_type, source_id, "
            "metadata->>'selected_provider' AS selected_provider, created_at "
            "FROM external_integration_intents WHERE id = $1", intent_id)
    return dict(row) if row else None


async def build_timeline(*, draft_id=None, intent_id=None, user_id=None) -> list[dict]:
    """Merge draft_review_events + external_integration_events + key runtime_traces
    into one chronological list. No secrets."""
    pool = clients.db_pool
    items: list[dict] = []
    async with pool.acquire() as conn:
        if draft_id is not None:
            for r in await conn.fetch(
                "SELECT created_at, action, from_status, to_status, notes "
                "FROM draft_review_events WHERE draft_id = $1 ORDER BY created_at", draft_id):
                items.append({"when": r["created_at"], "source": "review",
                              "type": r["action"],
                              "detail": f"{r['from_status']} → {r['to_status']}"
                                        + (f" · {r['notes'][:80]}" if r["notes"] else "")})
        if intent_id is not None:
            for r in await conn.fetch(
                "SELECT created_at, event_type, from_status, to_status "
                "FROM external_integration_events WHERE intent_id = $1 ORDER BY created_at", intent_id):
                items.append({"when": r["created_at"], "source": "integration",
                              "type": r["event_type"],
                              "detail": (f"{r['from_status']} → {r['to_status']}"
                                         if r["from_status"] or r["to_status"] else "")})
            for r in await conn.fetch(
                "SELECT created_at, trace_type, status FROM runtime_traces "
                "WHERE (tool_result->>'intent_id') = $1 AND trace_type = ANY($2) "
                "ORDER BY created_at", str(intent_id), list(_TIMELINE_TRACES)):
                items.append({"when": r["created_at"], "source": "trace",
                              "type": r["trace_type"], "detail": r["status"]})
    items.sort(key=lambda x: x["when"])
    return items


def _render_timeline(items: list[dict]) -> str:
    if not items:
        return "_(no recorded events yet)_"
    return "\n".join(
        f"- `{_fmt(i['when'])}` · **{i['type']}** ({i['source']})"
        + (f" — {i['detail']}" if i["detail"] else "")
        for i in items[-20:])


async def _execution_gates(intent: Optional[dict]) -> str:
    """Explain the layered execution gates for an intent (or globally)."""
    kill = guard.external_execution_enabled()
    lines = [
        f"- Global kill switch (external execution): **{'ENABLED' if kill else 'disabled'}**",
    ]
    if intent:
        flag = await ff.get_flag(intent.get("selected_provider") or intent.get("provider_name"),
                                 intent.get("action_type"))
        if flag is None:
            lines.append("- Feature flag: **missing (fail-closed deny)**")
        else:
            lines.append(f"- Feature flag `{flag['provider_name']}/{flag['action_type']}`: "
                         f"enabled=**{flag['enabled']}**, dry_run_only=**{flag['dry_run_only']}**")
        lines.append(f"- Intent dry_run: **{intent.get('dry_run')}** · "
                     f"requires_confirmation: **{intent.get('requires_confirmation')}**")
    lines.append("- Final safety interlock: real execution is **not allowed** in this phase")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #

async def handle_governance_question(
    kind: str, *, message: str, session_uuid: uuid.UUID, user_id: uuid.UUID,
    workspace_uuid: Optional[uuid.UUID], is_admin: bool,
) -> tuple[bool, Optional[str]]:
    draft, intent_id = await _resolve(session_uuid, user_id, workspace_uuid, is_admin)
    intent = await _intent_row(intent_id) if intent_id else None
    await write_trace(
        session_id=session_uuid, user_id=user_id, trace_type=TRACE_REQUESTED, status="ok",
        selected_agent=SIGNAL, tool_name="chat_governance",
        tool_result={"kind": kind, "draft_id": str(draft["id"]) if draft else None,
                     "intent_id": str(intent_id) if intent_id else None},
        workspace_id=workspace_uuid)

    text = await _answer(kind, draft=draft, intent=intent, intent_id=intent_id,
                         session_uuid=session_uuid, user_id=user_id,
                         workspace_uuid=workspace_uuid)
    return True, text


async def _answer(kind, *, draft, intent, intent_id, session_uuid, user_id, workspace_uuid):
    pool = clients.db_pool

    if kind == "why_execution_disabled":
        return ("**Why execution is disabled**\n\n"
                "External provider execution is blocked by a layered, governance-first "
                "design — every gate must pass, and the master gate is off:\n\n"
                + await _execution_gates(intent) +
                "\n\nNothing can be sent or created externally in this phase.")

    if kind == "why_blocked":
        gates = await _execution_gates(intent)
        recent = ""
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT created_at, trace_type, error_message FROM runtime_traces "
                "WHERE user_id = $1 AND trace_type = ANY($2) ORDER BY created_at DESC LIMIT 3",
                user_id, list(_BLOCK_TRACES))
        if rows:
            recent = "\n\nMost recent block events:\n" + "\n".join(
                f"- `{_fmt(r['created_at'])}` · **{r['trace_type']}**"
                + (f" — {r['error_message'][:100]}" if r["error_message"] else "")
                for r in rows)
        return ("**Why this is blocked**\n\nThis email cannot be sent because provider "
                "execution is governed and currently disabled:\n\n" + gates + recent)

    if kind == "who_approved":
        if draft is None:
            return "I don't have an active draft in this conversation to check."
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT u.email, u.display_name, d.approved_at, d.status "
                "FROM communication_drafts d LEFT JOIN users u ON u.id = d.approved_by "
                "WHERE d.id = $1", draft["id"])
        if row and row["approved_at"]:
            who = row["display_name"] or row["email"] or "an admin"
            return (f"Draft `{str(draft['id'])[:8]}` was **approved** by **{who}** on "
                    f"{_fmt(row['approved_at'])} (internal approval only — no email was sent).")
        return (f"Draft `{str(draft['id'])[:8]}` is **{draft['status']}** and has not been "
                "approved yet.")

    if kind == "approval_history":
        if draft is None:
            return "I don't have an active draft in this conversation to show history for."
        items = await build_timeline(draft_id=draft["id"])
        await write_trace(session_id=session_uuid, user_id=user_id, trace_type=TRACE_TIMELINE,
                          status="ok", selected_agent=SIGNAL, tool_name="chat_governance",
                          tool_result={"draft_id": str(draft["id"]), "events": len(items)},
                          workspace_id=workspace_uuid)
        return (f"**Approval history — draft `{str(draft['id'])[:8]}`**\n\n"
                + _render_timeline(items))

    if kind == "what_happened_intent":
        if intent is None:
            return ("There's no prepared provider intent in this conversation yet. Approve "
                    "a draft and say \"Prepare it for Gmail\" first.")
        items = await build_timeline(intent_id=intent_id)
        await write_trace(session_id=session_uuid, user_id=user_id, trace_type=TRACE_TIMELINE,
                          status="ok", selected_agent=SIGNAL, tool_name="chat_governance",
                          tool_result={"intent_id": str(intent_id), "events": len(items)},
                          workspace_id=workspace_uuid)
        prov = intent.get("selected_provider") or intent.get("provider_name")
        return (f"**Intent `{str(intent_id)[:8]}`** ({prov} · {intent['action_type']}) — "
                f"status **{intent['status']}**, dry_run **{intent['dry_run']}**. "
                "It was prepared for review only; nothing was sent.\n\n"
                "Timeline:\n" + _render_timeline(items))

    if kind == "what_failed":
        async with pool.acquire() as conn:
            logs = await conn.fetch(
                "SELECT created_at, tool_name, status, error_message FROM tool_execution_logs "
                "WHERE user_id = $1 AND status IN ('failed','denied','blocked','error') "
                "ORDER BY created_at DESC LIMIT 8", user_id)
            traces = await conn.fetch(
                "SELECT created_at, trace_type, error_message FROM runtime_traces "
                "WHERE user_id = $1 AND status IN ('failed','error','blocked') "
                "ORDER BY created_at DESC LIMIT 8", user_id)
        if not logs and not traces:
            return "No validation failures or blocked attempts are recorded for you."
        out = ["**Recent validation failures / blocked attempts**\n"]
        for r in logs:
            out.append(f"- `{_fmt(r['created_at'])}` · tool **{r['tool_name']}** ({r['status']})"
                       + (f" — {r['error_message'][:100]}" if r["error_message"] else ""))
        for r in traces:
            out.append(f"- `{_fmt(r['created_at'])}` · trace **{r['trace_type']}**"
                       + (f" — {r['error_message'][:100]}" if r["error_message"] else ""))
        return "\n".join(out)

    if kind == "governance_trail":
        if draft is None and intent_id is None:
            return ("I don't have an active draft or intent in this conversation to build a "
                    "governance trail. Create or show a draft first.")
        items = await build_timeline(
            draft_id=draft["id"] if draft else None, intent_id=intent_id, user_id=user_id)
        await write_trace(session_id=session_uuid, user_id=user_id, trace_type=TRACE_TIMELINE,
                          status="ok", selected_agent=SIGNAL, tool_name="chat_governance",
                          tool_result={"draft_id": str(draft["id"]) if draft else None,
                                       "intent_id": str(intent_id) if intent_id else None,
                                       "events": len(items)},
                          workspace_id=workspace_uuid)
        header = "**Governance trail**"
        if draft:
            header += f" · draft `{str(draft['id'])[:8]}` ({draft['status']})"
        if intent:
            header += f" · intent `{str(intent_id)[:8]}` ({intent['status']})"
        return (header + "\n\n" + _render_timeline(items)
                + "\n\n_Provider execution remains disabled; this is an audit view only._")

    return "I can explain governance state — try \"Why is execution disabled?\" or " \
           "\"Show me the governance trail.\""
