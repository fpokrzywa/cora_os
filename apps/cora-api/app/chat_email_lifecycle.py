"""Chat-Native Email Review & Approval Workflow v1.9.

Manage the SIGNAL email lifecycle from the Cora chat interface: create / show /
revise / approve / reject / archive a draft, prepare a provider integration intent,
simulate the provider payload, and run the final safety check — all internal,
governed, and NON-executing. NOTHING is ever sent: provider execution stays
disabled and every path reuses the existing governed services + final interlock.

Per-conversation context (chat_email_context) resolves ambiguous follow-ups like
"approve it" to the active draft. No OAuth tokens or credential payloads are ever
read or returned here.

Dispatched from the /chat route AFTER routing; the Agent Test Harness uses a
different endpoint, so this never runs there (spec #19).
"""

import logging
import re
import time
import uuid
from typing import Optional

import httpx

from app.clients import clients
from app.config import settings
from app import execution_adapters as eadapt
from app import final_interlock as fi
from app import integration_readiness as ir
from app import signal_tools
from app import review_workflow as rw
from app.runtime_traces import write_trace
from app.tools.governance import check_permission, fetch_tool, log_execution_attempt

logger = logging.getLogger(__name__)

SIGNAL = "SIGNAL"
_EDITABLE = rw.DRAFT_EDITABLE  # {"draft", "changes_requested"}

_SUBJECT_RE = re.compile(r"^[*_#\s]*subject:[*_\s]*(.+?)[*_\s]*$", re.IGNORECASE | re.MULTILINE)
_RECIPIENT_RE = re.compile(r"^[*_#\s]*to:[*_\s]*(.+?)[*_\s]*$", re.IGNORECASE | re.MULTILINE)

# Commands that always handle (carry an explicit noun) vs ambiguous follow-ups
# that only handle when an active draft exists in context.
_REVISE_PHRASES = ("make it shorter", "make it longer", "make it warmer",
                   "make it friendlier", "change the tone", "shorten it",
                   "reword", "rewrite it", "revise it", "revise the draft",
                   "revise draft", "update the draft", "update draft")


class EmailLifecycleError(Exception):
    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# Command detection
# --------------------------------------------------------------------------- #

def detect_email_command(message: str) -> Optional[tuple[str, bool]]:
    """Classify a chat message into an email-lifecycle command. Returns
    (command, is_ambiguous) or None. Ambiguous (terse follow-up) commands are only
    acted on when an active draft is in context — otherwise the dispatcher lets
    normal chat handle the message."""
    m = (message or "").lower().strip()
    if not m:
        return None

    # Explicit provider / simulation / safety commands.
    if "prepare" in m and any(p in m for p in ("gmail", "google")):
        return ("prepare_gmail", False)
    if "prepare" in m and any(p in m for p in ("outlook", "microsoft")):
        return ("prepare_outlook", False)
    if "prepare" in m and ("provider" in m or "send intent" in m or "integration" in m):
        return ("prepare_gmail", False)  # default provider when unspecified
    if "simulate" in m and ("payload" in m or "provider" in m):
        return ("simulate", False)
    if ("safety check" in m or "final safety" in m or "interlock" in m
            or ("run" in m and "safety" in m)):
        return ("safety_check", False)

    # Explicit draft verbs.
    if ("draft" in m and any(v in m for v in ("write", "compose", "create", "draft an", "draft a"))) \
            or (any(n in m for n in ("email", "e-mail")) and any(v in m for v in ("draft", "compose", "write"))):
        # "draft an email", "write an email and save as draft"
        if "show" not in m and "approve" not in m and "reject" not in m:
            return ("create", False)
    if any(p in m for p in ("show latest draft", "show the latest draft",
                            "show this draft", "show the draft", "view the draft",
                            "show draft", "view draft")):
        return ("show", False)
    if "archive" in m and "draft" in m:
        return ("archive", False)
    if "approve" in m and "draft" in m:
        return ("approve", False)
    if ("reject" in m or "decline" in m) and "draft" in m:
        return ("reject", False)
    if "revise" in m and "draft" in m:
        return ("revise", True)

    # Ambiguous follow-ups (need an active draft).
    if m in ("approve it", "approve this", "approve") or m.startswith("approve it"):
        return ("approve", True)
    if m in ("reject it", "reject this", "reject", "decline it") or m.startswith("reject it"):
        return ("reject", True)
    if m in ("archive it", "archive this"):
        return ("archive", True)
    if m in ("show it", "show this", "show the draft", "show latest"):
        return ("show", True)
    if any(p in m for p in _REVISE_PHRASES):
        return ("revise", True)
    return None


# --------------------------------------------------------------------------- #
# Conversation context
# --------------------------------------------------------------------------- #

async def get_context(session_id: uuid.UUID) -> dict:
    pool = clients.db_pool
    if pool is None:
        return {}
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT current_active_draft_id, last_created_draft_id, "
            "last_reviewed_draft_id, selected_provider, last_integration_intent_id "
            "FROM chat_email_context WHERE session_id = $1", session_id)
    return dict(row) if row else {}


async def set_context(session_id: uuid.UUID, **fields) -> None:
    pool = clients.db_pool
    if pool is None:
        return
    cols = ("current_active_draft_id", "last_created_draft_id",
            "last_reviewed_draft_id", "selected_provider", "last_integration_intent_id")
    vals = [fields.get(c) for c in cols]
    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            INSERT INTO chat_email_context
                (session_id, {", ".join(cols)})
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (session_id) DO UPDATE SET
                current_active_draft_id = COALESCE($2, chat_email_context.current_active_draft_id),
                last_created_draft_id = COALESCE($3, chat_email_context.last_created_draft_id),
                last_reviewed_draft_id = COALESCE($4, chat_email_context.last_reviewed_draft_id),
                selected_provider = COALESCE($5, chat_email_context.selected_provider),
                last_integration_intent_id = COALESCE($6, chat_email_context.last_integration_intent_id),
                updated_at = NOW()
            """,
            session_id, *vals)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

async def _generate_text(prompt: str) -> str:
    endpoint = settings.dgx_model_endpoint
    if not endpoint:
        raise EmailLifecycleError("model endpoint not configured", code="unavailable")
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            f"{endpoint.rstrip('/')}/api/generate",
            json={"model": settings.dgx_model_name, "prompt": prompt, "stream": False})
        r.raise_for_status()
        return (r.json().get("response") or "").strip()


def _extract_fields(text: str, fallback_title: str) -> dict:
    subj = _SUBJECT_RE.search(text)
    recip = _RECIPIENT_RE.search(text)
    subject = subj.group(1).strip()[:300] if subj else None
    recipient = recip.group(1).strip()[:300] if recip else None
    return {
        "subject": subject,
        "recipient_hint": recipient,
        "title": subject or fallback_title[:200],
        "body": text,
    }


async def _trace(session_id, user_id, workspace_id, *, trace_type, status="ok",
                 result=None, error=None):
    await write_trace(
        session_id=session_id, user_id=user_id, trace_type=trace_type, status=status,
        selected_agent=SIGNAL, tool_name="chat_email_lifecycle",
        tool_result=result or {}, error_message=error, workspace_id=workspace_id)


def _visible(draft: Optional[dict], *, user_id, workspace_uuid, is_admin) -> Optional[dict]:
    """Ownership + workspace scope (spec #8). Owner-or-admin; workspace must match
    when one is set on the draft."""
    if draft is None:
        return None
    if not is_admin and draft["created_by"] != user_id:
        return None
    if (workspace_uuid is not None and draft.get("workspace_id") is not None
            and draft["workspace_id"] != workspace_uuid):
        return None
    return draft


def format_draft(draft: dict) -> str:
    """Draft display (spec #15) with SAFE action labels only (spec #16/#17)."""
    return (
        f"**Draft** `{str(draft['id'])[:8]}` · Status: **{draft['status']}**\n"
        f"To: {draft.get('recipient_hint') or '—'}\n"
        f"Subject: {draft.get('subject') or '—'}\n\n"
        f"{draft.get('body') or ''}\n\n"
        "_Available actions: revise · approve draft · reject draft · "
        "prepare for Gmail · prepare for Outlook_"
    )


async def _resolve_draft(ctx: dict, *, user_id, workspace_uuid, is_admin,
                         prefer_latest=False) -> Optional[dict]:
    did = ctx.get("current_active_draft_id")
    if did is not None:
        return _visible(await signal_tools.get_draft(did),
                        user_id=user_id, workspace_uuid=workspace_uuid, is_admin=is_admin)
    if prefer_latest:
        rows = await signal_tools.list_drafts(
            workspace_id=workspace_uuid, owner_id=None if is_admin else user_id)
        rows = [r for r in rows if r["draft_type"] == "email"]
        return rows[0] if rows else None
    return None


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #

async def handle_email_command(
    cmd: tuple[str, bool], *, message: str, session_uuid: uuid.UUID,
    user_id: uuid.UUID, workspace_uuid: Optional[uuid.UUID], scope_type: str,
    is_admin: bool,
) -> tuple[bool, Optional[str]]:
    """Returns (handled, response_text). When an ambiguous command has no active
    draft, returns (False, None) so normal chat handles the message (spec #5/#6)."""
    command, ambiguous = cmd
    ctx = await get_context(session_uuid)

    if command == "create":
        return True, await _h_create(message=message, session_uuid=session_uuid,
                                     user_id=user_id, workspace_uuid=workspace_uuid,
                                     scope_type=scope_type, is_admin=is_admin)

    # Commands below operate on a draft / intent.
    if command in ("show", "revise", "approve", "reject", "archive",
                   "prepare_gmail", "prepare_outlook"):
        draft = await _resolve_draft(ctx, user_id=user_id, workspace_uuid=workspace_uuid,
                                     is_admin=is_admin, prefer_latest=(command == "show"))
        if draft is None:
            if ambiguous:
                return False, None  # let normal chat handle it
            return True, ("I don't have an active email draft in this conversation "
                          "yet. Try: \"Draft an email to … and save it as a draft.\"")
        kw = dict(session_uuid=session_uuid, user_id=user_id,
                  workspace_uuid=workspace_uuid, scope_type=scope_type, is_admin=is_admin)
        if command == "show":
            await set_context(session_uuid, current_active_draft_id=draft["id"])
            await _trace(session_uuid, user_id, workspace_uuid,
                         trace_type="chat_email_draft_shown",
                         result={"draft_id": str(draft["id"]), "status": draft["status"]})
            return True, format_draft(draft)
        if command == "revise":
            return True, await _h_revise(draft, message=message, **kw)
        if command == "approve":
            return True, await _h_approve(draft, **kw)
        if command == "reject":
            return True, await _h_reject(draft, **kw)
        if command == "archive":
            return True, await _h_archive(draft, **kw)
        provider = "gmail" if command == "prepare_gmail" else "outlook_mail"
        return True, await _h_prepare(draft, provider=provider, **kw)

    if command in ("simulate", "safety_check"):
        intent_id = ctx.get("last_integration_intent_id")
        if intent_id is None:
            if ambiguous:
                return False, None
            return True, ("There's no prepared provider intent yet. Approve a draft "
                          "and say \"Prepare it for Gmail\" first.")
        if command == "simulate":
            return True, await _h_simulate(intent_id, session_uuid=session_uuid,
                                           user_id=user_id, workspace_uuid=workspace_uuid,
                                           is_admin=is_admin)
        return True, await _h_safety_check(intent_id, session_uuid=session_uuid,
                                           user_id=user_id, workspace_uuid=workspace_uuid,
                                           is_admin=is_admin)
    return False, None


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

async def _h_create(*, message, session_uuid, user_id, workspace_uuid, scope_type, is_admin):
    tool_name = "signal_create_draft"
    tool = await fetch_tool(tool_name)
    if tool is not None:
        decision = await check_permission(tool, agent_name=SIGNAL, user_id=user_id,
                                          is_admin=is_admin)
        if not decision.allowed:
            await log_execution_attempt(
                tool_name=tool_name, agent_name=SIGNAL, session_id=session_uuid,
                user_id=user_id, scope_type=scope_type, allowed=False, duration_ms=None,
                status="denied", error_message=decision.reason)
            return f"_Saving a draft isn't permitted by the current policy ({decision.reason})._"
    prompt = (
        "You are drafting an internal email on the user's behalf for review (it will "
        "NOT be sent). Write a professional email based on this request. Do NOT sign "
        "the email with an internal/agent name (Cora, ATLAS, SIGNAL, etc.); if you add "
        "a closing, sign with the user's own name. Output EXACTLY a line "
        "'Subject: <subject>', then if a recipient is named a line 'To: <name>', then a "
        "blank line, then the email body. Request: " + message)
    started = time.perf_counter()
    try:
        text = await _generate_text(prompt)
    except Exception as exc:
        logger.exception("chat email create generation failed")
        return f"_I couldn't draft the email right now ({exc})._"
    fields = _extract_fields(text, fallback_title=message)
    fields["body"] = signal_tools.normalize_email_signoff(
        fields["body"], await signal_tools.user_signoff_name(user_id))
    row = await signal_tools.create_communication_draft(
        workspace_id=workspace_uuid, user_id=user_id, draft_type="email",
        title=fields["title"], subject=fields["subject"], body=fields["body"],
        recipient_hint=fields["recipient_hint"],
        metadata={"source": "chat", "session_id": str(session_uuid)})
    await set_context(session_uuid, current_active_draft_id=row["id"],
                      last_created_draft_id=row["id"])
    await log_execution_attempt(
        tool_name=tool_name, agent_name=SIGNAL, session_id=session_uuid, user_id=user_id,
        scope_type=scope_type, allowed=True,
        duration_ms=int((time.perf_counter() - started) * 1000), status="success",
        error_message=None)
    await _trace(session_uuid, user_id, workspace_uuid, trace_type="chat_email_draft_created",
                 result={"draft_id": str(row["id"]), "title": row["title"], "status": row["status"]})
    return "✓ Saved as an internal draft (nothing sent).\n\n" + format_draft(row)


async def _h_revise(draft, *, message, session_uuid, user_id, workspace_uuid, scope_type, is_admin):
    if draft["status"] not in _EDITABLE:
        return (f"This draft is **{draft['status']}** and is locked for editing. "
                "Only draft / changes-requested items can be revised.")
    prompt = (
        "Revise the following internal email per the instruction. Keep any Subject:/To: "
        "lines. Output the full revised email only.\n\nInstruction: " + message +
        "\n\nEmail:\n" + (draft.get("body") or ""))
    try:
        revised = await _generate_text(prompt)
    except Exception as exc:
        return f"_I couldn't revise the draft right now ({exc})._"
    fields = _extract_fields(revised, fallback_title=draft.get("title") or "")
    fields["body"] = signal_tools.normalize_email_signoff(
        fields["body"], await signal_tools.user_signoff_name(user_id))
    meta = dict(draft.get("metadata") or {})
    history = list(meta.get("revision_history") or [])
    history.append({"body": draft.get("body"), "subject": draft.get("subject")})
    meta["revision_history"] = history[-10:]  # keep last 10
    updated = await signal_tools.update_draft(draft["id"], {
        "body": fields["body"],
        "subject": fields["subject"] or draft.get("subject"),
        "metadata": meta,
    })
    # Review event with a change summary (spec #11).
    pool = clients.db_pool
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO draft_review_events (draft_id, user_id, action, from_status, "
            "to_status, notes) VALUES ($1,$2,'revise',$3,$3,$4)",
            draft["id"], user_id, draft["status"], f"chat revision: {message[:200]}")
    await _trace(session_uuid, user_id, workspace_uuid, trace_type="chat_email_draft_updated",
                 result={"draft_id": str(draft["id"]), "status": updated["status"]})
    return "✓ Revised the draft.\n\n" + format_draft(updated)


async def _h_approve(draft, *, session_uuid, user_id, workspace_uuid, scope_type, is_admin):
    if draft["status"] == "approved":
        return "This draft is already approved (internal only — nothing is sent)."
    started = time.perf_counter()
    try:
        # Reach 'reviewed' first if needed (preserves draft-first lifecycle).
        if draft["status"] in ("draft", "in_review", "changes_requested"):
            await rw.perform_action(rw.DRAFT_CONFIG, draft["id"], action="mark_reviewed",
                                    user_id=user_id, is_admin=is_admin)
            await set_context(session_uuid, last_reviewed_draft_id=draft["id"])
        result = await rw.perform_action(rw.DRAFT_CONFIG, draft["id"], action="approve",
                                         user_id=user_id, is_admin=is_admin)
    except rw.ReviewError as exc:
        await log_execution_attempt(
            tool_name="signal_approve_draft", agent_name=SIGNAL, session_id=session_uuid,
            user_id=user_id, scope_type=scope_type, allowed=True, duration_ms=None,
            status="failed", error_message=str(exc))
        return f"_Couldn't approve: {exc}_"
    await log_execution_attempt(
        tool_name="signal_approve_draft", agent_name=SIGNAL, session_id=session_uuid,
        user_id=user_id, scope_type=scope_type, allowed=True,
        duration_ms=int((time.perf_counter() - started) * 1000), status="success",
        error_message=None)
    await _trace(session_uuid, user_id, workspace_uuid, trace_type="chat_email_draft_approved",
                 result={"draft_id": str(draft["id"]), "status": result["status"]})
    return ("✓ Approved internally (no email was sent — provider execution is disabled). "
            "Next: \"Prepare it for Gmail\" or \"Prepare it for Outlook\".\n\n"
            + format_draft(result))


async def _h_reject(draft, *, session_uuid, user_id, workspace_uuid, scope_type, is_admin):
    try:
        result = await rw.perform_action(rw.DRAFT_CONFIG, draft["id"], action="reject",
                                         user_id=user_id, is_admin=is_admin)
    except rw.ReviewError as exc:
        return f"_Couldn't reject: {exc}_"
    await log_execution_attempt(
        tool_name="signal_reject_draft", agent_name=SIGNAL, session_id=session_uuid,
        user_id=user_id, scope_type=scope_type, allowed=True, duration_ms=None,
        status="success", error_message=None)
    await _trace(session_uuid, user_id, workspace_uuid, trace_type="chat_email_draft_rejected",
                 result={"draft_id": str(draft["id"]), "status": result["status"]})
    return f"✓ Rejected the draft (status: {result['status']})."


async def _h_archive(draft, *, session_uuid, user_id, workspace_uuid, scope_type, is_admin):
    try:
        result = await rw.perform_action(rw.DRAFT_CONFIG, draft["id"], action="archive",
                                         user_id=user_id, is_admin=is_admin)
    except rw.ReviewError as exc:
        return f"_Couldn't archive: {exc}_"
    await _trace(session_uuid, user_id, workspace_uuid, trace_type="chat_email_draft_archived",
                 result={"draft_id": str(draft["id"]), "status": result["status"]})
    return f"✓ Archived the draft (status: {result['status']})."


async def _h_prepare(draft, *, provider, session_uuid, user_id, workspace_uuid, scope_type, is_admin):
    if draft["status"] != "approved":
        return ("A draft must be **approved** before preparing a provider intent. "
                "Say \"Approve it\" first.")
    tool_name = "signal_prepare_email_send_intent"
    tool = await fetch_tool(tool_name)
    if tool is not None:
        decision = await check_permission(tool, agent_name=SIGNAL, user_id=user_id, is_admin=is_admin)
        if not decision.allowed:
            return f"_Preparing a provider intent isn't permitted by policy ({decision.reason})._"
    started = time.perf_counter()
    try:
        intent = await ir.create_readiness_intent_from_draft(
            draft["id"], user_id=user_id, is_admin=is_admin)
    except ir.IntegrationError as exc:
        return f"_Couldn't prepare the provider intent: {exc}_"
    # Record the chat-selected provider (req #12). dry_run + requires_confirmation
    # stay TRUE — the row remains non-executing.
    pool = clients.db_pool
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE external_integration_intents SET provider_name = $1, "
            "metadata = metadata || jsonb_build_object('selected_provider', $1::text) "
            "WHERE id = $2", provider, intent["id"])
    await set_context(session_uuid, last_integration_intent_id=intent["id"],
                      selected_provider=provider)
    await log_execution_attempt(
        tool_name=tool_name, agent_name=SIGNAL, session_id=session_uuid, user_id=user_id,
        scope_type=scope_type, allowed=True,
        duration_ms=int((time.perf_counter() - started) * 1000), status="success",
        error_message=None)
    await _trace(session_uuid, user_id, workspace_uuid, trace_type="chat_email_intent_prepared",
                 result={"intent_id": str(intent["id"]), "provider_name": provider,
                         "action_type": "send_email", "dry_run": True})
    return (f"✓ Prepared a dry-run **{provider}** integration intent "
            f"`{str(intent['id'])[:8]}` (send_email, dry_run, requires confirmation — "
            "nothing is sent). Next: \"Simulate the provider payload\" or "
            "\"Run the final safety check\".")


async def _h_simulate(intent_id, *, session_uuid, user_id, workspace_uuid, is_admin):
    try:
        result = await eadapt.simulate_adapter_payload(intent_id, user_id=user_id, is_admin=is_admin)
    except Exception as exc:
        return f"_Couldn't simulate the provider payload: {exc}_"
    await _trace(session_uuid, user_id, workspace_uuid, trace_type="chat_email_provider_simulated",
                 result={"intent_id": str(intent_id),
                         "provider_name": result.get("provider_name"),
                         "payload_ready": result.get("payload_ready")})
    sim = (result.get("simulation") or {}).get("provider_request") or {}
    return (f"✓ Simulated the provider payload (no API was called). "
            f"Provider: {result.get('provider_name')} · method: {sim.get('api_method', '—')} · "
            f"would_send: {sim.get('would_send', False)} · payload_ready: {result.get('payload_ready')}.")


async def _h_safety_check(intent_id, *, session_uuid, user_id, workspace_uuid, is_admin):
    try:
        result = await fi.run_final_safety_check(intent_id, user_id=user_id, is_admin=is_admin)
    except Exception as exc:
        return f"_Couldn't run the final safety check: {exc}_"
    await _trace(session_uuid, user_id, workspace_uuid, trace_type="chat_email_safety_check_run",
                 result={"intent_id": str(intent_id), "status": result["status"],
                         "real_execution_allowed": result["real_execution_allowed"]})
    reasons = ", ".join(result.get("block_reasons") or []) or "—"
    return (f"✓ Final safety check: **{result['status']}**. Real execution allowed: "
            f"**{result['real_execution_allowed']}** — provider execution remains disabled. "
            f"Reasons: {reasons}.")
