"""Human Approval Execution Console v1.4 — approve/reject decisions, no execution.

A human admin reviews provider-ready integration intents and APPROVES them for
*future* execution or REJECTS them. Approving records internal state + audit
evidence ONLY: it never calls Gmail / Outlook / Google / Microsoft, never sets
dry_run=false, and never lifts the global kill switch. Every decision is gated by
a governance + provider-readiness checklist (reusing the v1.3 credential snapshot)
and recorded in `execution_approvals` with the approver, decision, reason, and
governance/provider-readiness snapshots + a payload hash. No token is ever read.

approval_state vocabulary (a derived dimension over the intent — the intent's own
status machine is left untouched):
  pending_review       — readiness not yet satisfied (missing provider/token/scope
                         or source not approved)
  ready_for_approval   — checklist satisfied, awaiting a human decision
  approved_for_execution — an admin approved it for future execution
  rejected             — an admin rejected it
  blocked_by_governance — a governance invariant fails (dry_run/execution flags)
  cancelled            — the underlying intent was cancelled
"""

import hashlib
import json
import uuid
from typing import Optional

from app.clients import clients
from app import execution_guard as guard
from app import feature_flags as ff
from app import integration_readiness as ir
from app import provider_credential_simulation as pcs
from app.runtime_traces import write_trace
from app.tools.governance import log_execution_attempt

# approval_state values
ST_PENDING_REVIEW = "pending_review"
ST_READY = "ready_for_approval"
ST_APPROVED = "approved_for_execution"
ST_REJECTED = "rejected"
ST_BLOCKED = "blocked_by_governance"
ST_CANCELLED = "cancelled"

# decisions recorded in execution_approvals
DEC_APPROVED = "approved_for_execution"
DEC_REJECTED = "rejected"
DEC_BLOCKED = "blocked"

# runtime traces (spec #5)
TRACE_VIEWED = "execution_approval_viewed"
TRACE_APPROVED = "execution_approval_approved"
TRACE_REJECTED = "execution_approval_rejected"
TRACE_BLOCKED = "execution_approval_blocked"

# governed tools (spec #6)
TOOL_APPROVE = "execution_approval_approved"
TOOL_REJECT = "execution_approval_rejected"

PAYLOAD_PREVIEW_REF = "metadata.credential_usage_simulation"


class ApprovalError(Exception):
    """code: not_found (404) | invalid (400) | forbidden (403) |
    conflict (409) | unavailable (503)."""

    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


def _require_pool():
    if clients.db_pool is None:
        raise ApprovalError("Postgres pool unavailable", code="unavailable")
    return clients.db_pool


async def _visible_intent(intent_id: uuid.UUID, *, user_id: uuid.UUID, is_admin: bool) -> dict:
    intent = await ir.get_intent(intent_id)
    if intent is None or (not is_admin and intent["created_by"] != user_id):
        raise ApprovalError("intent not found", code="not_found")
    return intent


def _payload_hash(preview: Optional[dict]) -> str:
    return hashlib.sha256(
        json.dumps(preview or {}, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


async def _checklist(intent: dict, *, user_id: uuid.UUID) -> dict:
    """Governance + provider-readiness checklist for an approval decision. Reuses
    the v1.3 credential snapshot (no side effects) + the source-approved gate."""
    snap = await pcs.credential_snapshot(intent, user_id=user_id)
    pool = _require_pool()
    async with pool.acquire() as conn:
        source_approved = await ir._source_is_approved(conn, intent)
    v = snap["validation"]
    governance = {
        "dry_run_only": v["dry_run_only"],
        "provider_execution_disabled": v["provider_execution_disabled"],
        "kill_switch_blocks_execution": v["kill_switch_blocks_execution"],
        "governance_allows_execution": v["governance_allows_execution"],
        "execution_enabled": snap["execution_enabled"],
    }
    readiness = {
        "provider_connected": v["provider_connected"],
        "token_valid_or_refreshable": v["token_valid_or_refreshable"],
        "required_scopes_present": v["required_scopes_present"],
        "missing_scopes": v["missing_scopes"],
        "source_approved": source_approved,
        "payload_ready": snap["payload_ready"],
    }
    # Governance invariant for THIS phase: dry_run on + execution disabled must
    # both hold. (Approval is for FUTURE execution, so the kill switch blocking
    # now is expected and does NOT prevent approval.)
    governance_ok = governance["dry_run_only"] and governance["provider_execution_disabled"]
    can_approve = bool(
        governance_ok
        and readiness["provider_connected"]
        and readiness["token_valid_or_refreshable"]
        and readiness["required_scopes_present"]
        and readiness["source_approved"]
        and readiness["payload_ready"]
    )
    return {
        "snapshot": snap,
        "governance": governance,
        "readiness": readiness,
        "governance_ok": governance_ok,
        "can_approve": can_approve,
        "payload_hash": _payload_hash(snap.get("provider_payload_preview")),
        "payload_preview_ref": PAYLOAD_PREVIEW_REF,
    }


def _derive_state(intent: dict, latest_decision: Optional[str], *,
                  governance_ok: bool, can_approve: bool) -> str:
    if intent.get("status") == ir.STATUS_CANCELLED:
        return ST_CANCELLED
    if latest_decision == DEC_APPROVED:
        return ST_APPROVED
    if latest_decision == DEC_REJECTED:
        return ST_REJECTED
    if not governance_ok:
        return ST_BLOCKED
    return ST_READY if can_approve else ST_PENDING_REVIEW


async def _latest_decision(conn, intent_id: uuid.UUID) -> Optional[str]:
    return await conn.fetchval(
        "SELECT decision FROM execution_approvals "
        "WHERE intent_id = $1 AND decision IN ($2, $3) "
        "ORDER BY created_at DESC LIMIT 1",
        intent_id, DEC_APPROVED, DEC_REJECTED,
    )


def _failed_checks(cl: dict) -> list[str]:
    out: list[str] = []
    for k, val in {**cl["governance"], **cl["readiness"]}.items():
        if isinstance(val, bool) and not val and k not in (
            "kill_switch_blocks_execution", "execution_enabled",
        ):
            # kill_switch_blocks_execution=True / execution_enabled=False are the
            # desired states, so don't report their "falsey-for-approval" inverses.
            out.append(k)
    # execution must stay disabled; surface only the genuinely-missing items.
    if cl["governance"].get("execution_enabled"):
        out.append("execution_enabled (must be disabled)")
    if not cl["governance"].get("kill_switch_blocks_execution"):
        out.append("kill_switch_blocks_execution")
    return out


def _view(intent: dict, cl: dict, *, approval_state: str,
          latest_decision: Optional[str]) -> dict:
    snap = cl["snapshot"]
    return {
        "intent_id": str(intent["id"]),
        "workspace_id": str(intent["workspace_id"]) if intent.get("workspace_id") else None,
        "agent_name": intent.get("agent_name"),
        "source_type": intent.get("source_type"),
        "source_id": str(intent.get("source_id")),
        "provider_type": intent.get("provider_type"),
        "provider_name": snap.get("provider_name"),
        "action_type": intent.get("action_type"),
        "intent_status": intent.get("status"),
        "approval_state": approval_state,
        "latest_decision": latest_decision,
        "can_approve": cl["can_approve"],
        "governance": cl["governance"],
        "readiness": cl["readiness"],
        "payload_ready": snap.get("payload_ready"),
        "payload_errors": snap.get("payload_errors"),
        "provider_payload_preview": snap.get("provider_payload_preview"),
        "payload_hash": cl["payload_hash"],
        "payload_preview_ref": cl["payload_preview_ref"],
        "execution_allowed": snap.get("execution_allowed"),
        "execution_enabled": snap.get("execution_enabled"),
        "blockers": snap.get("blockers"),
        "note": (
            "Approval records internal state + audit evidence only. It never calls "
            "a provider API and never enables execution — the global kill switch "
            "keeps external execution disabled."
        ),
    }


async def _record(conn, *, intent_id, approver_id, decision, approval_state,
                  reason, cl) -> None:
    await conn.execute(
        """
        INSERT INTO execution_approvals
            (intent_id, approver_id, decision, approval_state, reason,
             governance_snapshot, provider_readiness_snapshot, payload_hash,
             payload_preview_ref)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        """,
        intent_id, approver_id, decision, approval_state,
        (reason.strip() if reason and reason.strip() else None),
        cl["governance"], cl["readiness"], cl["payload_hash"], cl["payload_preview_ref"],
    )


async def view_intent(intent_id: uuid.UUID, *, user_id: uuid.UUID, is_admin: bool) -> dict:
    """Console detail view. Computes the checklist + derived approval_state and
    writes an `execution_approval_viewed` trace. Read-only (no decision)."""
    intent = await _visible_intent(intent_id, user_id=user_id, is_admin=is_admin)
    cl = await _checklist(intent, user_id=user_id)
    pool = _require_pool()
    async with pool.acquire() as conn:
        latest = await _latest_decision(conn, intent_id)
    state = _derive_state(intent, latest, governance_ok=cl["governance_ok"],
                          can_approve=cl["can_approve"])
    await write_trace(
        session_id=None, user_id=user_id, trace_type=TRACE_VIEWED, status="ok",
        selected_agent=intent.get("agent_name"), tool_name="execution_approval",
        tool_result={"intent_id": str(intent_id), "approval_state": state,
                     "can_approve": cl["can_approve"]},
        workspace_id=intent.get("workspace_id"),
    )
    view = _view(intent, cl, approval_state=state, latest_decision=latest)
    # Feature Flag State for the console (spec v1.7 #6). Read-only consult.
    flag = await ff.get_flag(cl["snapshot"].get("provider_name"), intent.get("action_type"))
    view["feature_flag"] = {
        "present": flag is not None,
        "enabled": bool(flag["enabled"]) if flag else False,
        "dry_run_only": bool(flag["dry_run_only"]) if flag else True,
        "execution_enabled": guard.external_execution_enabled(),
        "requires_human_approval": bool(flag["requires_human_approval"]) if flag else True,
        "requires_final_interlock": bool(flag["requires_final_interlock"]) if flag else True,
        "flag_allows_execution": ff.flag_allows_execution(flag),
    }
    return view


async def list_for_approval(
    *, user_id: uuid.UUID, is_admin: bool,
    provider_type: Optional[str] = None, source_type: Optional[str] = None,
    approval_state: Optional[str] = None,
) -> list[dict]:
    """List readiness-queue intents with their derived approval_state for the
    console. Read-only; writes no trace (the per-intent view does)."""
    rows = await ir.list_intents(
        workspace_id=None, owner_id=None if is_admin else user_id,
        source_type=source_type,
    )
    rows = [r for r in rows if (r.get("metadata") or {}).get("workflow") == ir.RQ_WORKFLOW_TAG]
    if provider_type:
        rows = [r for r in rows if r.get("provider_type") == provider_type]
    pool = _require_pool()
    items: list[dict] = []
    for r in rows:
        cl = await _checklist(r, user_id=user_id)
        async with pool.acquire() as conn:
            latest = await _latest_decision(conn, r["id"])
        state = _derive_state(r, latest, governance_ok=cl["governance_ok"],
                              can_approve=cl["can_approve"])
        items.append({
            "intent_id": str(r["id"]),
            "source_type": r.get("source_type"),
            "provider_type": r.get("provider_type"),
            "provider_name": cl["snapshot"].get("provider_name"),
            "action_type": r.get("action_type"),
            "intent_status": r.get("status"),
            "approval_state": state,
            "can_approve": cl["can_approve"],
            "latest_decision": latest,
        })
    if approval_state:
        items = [i for i in items if i["approval_state"] == approval_state]
    return items


async def approve(intent_id: uuid.UUID, *, approver_id: uuid.UUID, is_admin: bool,
                  comment: Optional[str] = None) -> dict:
    """Approve an intent for FUTURE execution. Admin-only. Enforces the full
    governance + readiness checklist; on failure records a blocked attempt +
    `execution_approval_blocked` trace and raises. Executes nothing."""
    if not is_admin:
        raise ApprovalError("approval requires an admin reviewer", code="forbidden")
    intent = await _visible_intent(intent_id, user_id=approver_id, is_admin=is_admin)
    if intent.get("status") == ir.STATUS_CANCELLED:
        raise ApprovalError("intent is cancelled", code="conflict")
    cl = await _checklist(intent, user_id=approver_id)
    pool = _require_pool()

    if not cl["can_approve"]:
        failed = _failed_checks(cl)
        state = ST_BLOCKED if not cl["governance_ok"] else ST_PENDING_REVIEW
        async with pool.acquire() as conn:
            await _record(conn, intent_id=intent_id, approver_id=approver_id,
                          decision=DEC_BLOCKED, approval_state=state, reason=comment, cl=cl)
        await log_execution_attempt(
            tool_name=TOOL_APPROVE, agent_name=intent.get("agent_name"), session_id=None,
            user_id=approver_id, scope_type=None, allowed=False, duration_ms=None,
            status="blocked", error_message="approval checklist not satisfied",
        )
        await write_trace(
            session_id=None, user_id=approver_id, trace_type=TRACE_BLOCKED,
            status="blocked", selected_agent=intent.get("agent_name"),
            tool_name=TOOL_APPROVE,
            tool_result={"intent_id": str(intent_id), "approval_state": state,
                         "failed_checks": failed, "payload_hash": cl["payload_hash"]},
            error_message="approval blocked: " + ", ".join(failed),
            workspace_id=intent.get("workspace_id"),
        )
        raise ApprovalError(
            "cannot approve — checklist not satisfied: " + ", ".join(failed),
            code="conflict",
        )

    async with pool.acquire() as conn:
        async with conn.transaction():
            await _record(conn, intent_id=intent_id, approver_id=approver_id,
                          decision=DEC_APPROVED, approval_state=ST_APPROVED,
                          reason=comment, cl=cl)
            meta = dict(intent.get("metadata") or {})
            meta["approval_state"] = ST_APPROVED
            meta["execution_approval"] = {
                "decision": DEC_APPROVED, "approver_id": str(approver_id),
                "payload_hash": cl["payload_hash"], "reason": comment,
            }
            await conn.execute(
                "UPDATE external_integration_intents SET metadata = $1, "
                "updated_at = NOW() WHERE id = $2", meta, intent_id,
            )
    await log_execution_attempt(
        tool_name=TOOL_APPROVE, agent_name=intent.get("agent_name"), session_id=None,
        user_id=approver_id, scope_type=None, allowed=True, duration_ms=None,
        status="success", error_message=None,
    )
    await write_trace(
        session_id=None, user_id=approver_id, trace_type=TRACE_APPROVED, status="ok",
        selected_agent=intent.get("agent_name"), tool_name=TOOL_APPROVE,
        tool_result={"intent_id": str(intent_id), "approval_state": ST_APPROVED,
                     "payload_hash": cl["payload_hash"], "execution_enabled": False,
                     "dry_run_only": True},
        workspace_id=intent.get("workspace_id"),
    )
    return await view_intent(intent_id, user_id=approver_id, is_admin=is_admin)


async def reject(intent_id: uuid.UUID, *, approver_id: uuid.UUID, is_admin: bool,
                 comment: Optional[str] = None) -> dict:
    """Reject an intent. Admin-only. Records the decision + audit snapshot and a
    `execution_approval_rejected` trace. Executes nothing."""
    if not is_admin:
        raise ApprovalError("rejection requires an admin reviewer", code="forbidden")
    intent = await _visible_intent(intent_id, user_id=approver_id, is_admin=is_admin)
    if intent.get("status") == ir.STATUS_CANCELLED:
        raise ApprovalError("intent is cancelled", code="conflict")
    cl = await _checklist(intent, user_id=approver_id)
    pool = _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _record(conn, intent_id=intent_id, approver_id=approver_id,
                          decision=DEC_REJECTED, approval_state=ST_REJECTED,
                          reason=comment, cl=cl)
            meta = dict(intent.get("metadata") or {})
            meta["approval_state"] = ST_REJECTED
            meta["execution_approval"] = {
                "decision": DEC_REJECTED, "approver_id": str(approver_id),
                "reason": comment,
            }
            await conn.execute(
                "UPDATE external_integration_intents SET metadata = $1, "
                "updated_at = NOW() WHERE id = $2", meta, intent_id,
            )
    await log_execution_attempt(
        tool_name=TOOL_REJECT, agent_name=intent.get("agent_name"), session_id=None,
        user_id=approver_id, scope_type=None, allowed=True, duration_ms=None,
        status="success", error_message=None,
    )
    await write_trace(
        session_id=None, user_id=approver_id, trace_type=TRACE_REJECTED, status="ok",
        selected_agent=intent.get("agent_name"), tool_name=TOOL_REJECT,
        tool_result={"intent_id": str(intent_id), "approval_state": ST_REJECTED},
        workspace_id=intent.get("workspace_id"),
    )
    return await view_intent(intent_id, user_id=approver_id, is_admin=is_admin)


async def list_events(intent_id: uuid.UUID) -> list[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, intent_id, approver_id, decision, approval_state, reason, "
            "payload_hash, payload_preview_ref, created_at "
            "FROM execution_approvals WHERE intent_id = $1 ORDER BY created_at DESC",
            intent_id,
        )
    return [dict(r) for r in rows]
