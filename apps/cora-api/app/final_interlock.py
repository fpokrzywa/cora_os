"""Execution Runbook & Final Safety Interlock v1.5 — always blocks real execution.

The LAST gate before any future real provider execution. It runs the complete
safety checklist over an integration intent and returns a status — but it NEVER
calls a provider, never sets dry_run=false, and never enables execution. Real
execution requires EVERY internal check to pass AND two future-phase flags
(dry_run cleared + global external execution enabled) that are both OFF in this
phase, so `real_execution_allowed` is always False.

Checks (spec #2):
  - source draft/proposal approved
  - integration intent approved_for_execution (a current v1.4 approval decision)
  - approval audit row exists
  - provider connected
  - token valid or refreshable
  - required scopes present
  - provider supports the action type
  - payload hash matches the approved payload (drift/tamper detection)
  - [future] dry_run cleared for execution        (False this phase)
  - [future] global external execution enabled     (False this phase)
"""

import uuid
from typing import Optional

from app.clients import clients
from app import execution_approval as ea
from app import execution_guard as guard
from app import feature_flags as ff
from app import integration_readiness as ir
from app import provider_adapters as adapters
from app import provider_credential_simulation as pcs
from app.runtime_traces import write_trace

# result statuses (spec #4)
ST_BLOCKED = "blocked_by_final_interlock"
ST_READY_DISABLED = "ready_but_execution_disabled"
ST_MISSING_APPROVAL = "missing_approval"
ST_PROVIDER_NOT_READY = "provider_not_ready"
ST_PAYLOAD_MISMATCH = "payload_mismatch"

# runtime traces (spec #6)
TRACE_CHECKED = "final_interlock_checked"
TRACE_BLOCKED = "final_interlock_blocked"
TRACE_READY_DISABLED = "final_interlock_ready_but_disabled"

_TOOL = "final_interlock"

# Internal checks that must ALL hold before execution would even be considered.
_INTERNAL_CHECKS = (
    "source_approved",
    "intent_approved_for_execution",
    "approval_audit_exists",
    "provider_connected",
    "token_valid_or_refreshable",
    "required_scopes_present",
    "provider_supports_action",
    "payload_hash_matches",
)


class InterlockError(Exception):
    """code: not_found (404) | invalid (400) | unavailable (503)."""

    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


def _require_pool():
    if clients.db_pool is None:
        raise InterlockError("Postgres pool unavailable", code="unavailable")
    return clients.db_pool


async def _visible_intent(intent_id: uuid.UUID, *, user_id: uuid.UUID, is_admin: bool) -> dict:
    intent = await ir.get_intent(intent_id)
    if intent is None or (not is_admin and intent["created_by"] != user_id):
        raise InterlockError("intent not found", code="not_found")
    return intent


def _action_supported(provider_name: Optional[str], provider_type: Optional[str],
                      action_type: Optional[str]) -> bool:
    adapter = adapters.get_adapter(provider_name)
    return bool(
        adapter is not None
        and adapter.provider_type == provider_type
        and action_type in adapter.supported_actions
    )


async def run_final_safety_check(
    intent_id: uuid.UUID, *, user_id: uuid.UUID, is_admin: bool,
) -> dict:
    """Run the full interlock checklist and record the result. NEVER executes —
    real_execution_allowed is always False in this phase."""
    intent = await _visible_intent(intent_id, user_id=user_id, is_admin=is_admin)
    snap = await pcs.credential_snapshot(intent, user_id=user_id)
    v = snap["validation"]
    provider_name = snap.get("provider_name")
    provider_type = intent.get("provider_type")
    action_type = intent.get("action_type")

    pool = _require_pool()
    async with pool.acquire() as conn:
        source_approved = await ir._source_is_approved(conn, intent)
        latest_decision = await ea._latest_decision(conn, intent_id)
        approval_row = await conn.fetchrow(
            "SELECT approver_id, payload_hash, reason, created_at "
            "FROM execution_approvals WHERE intent_id = $1 AND decision = $2 "
            "ORDER BY created_at DESC LIMIT 1",
            intent_id, ea.DEC_APPROVED,
        )

    approval_audit_exists = approval_row is not None
    approved = approval_audit_exists and latest_decision == ea.DEC_APPROVED
    approved_payload_hash = approval_row["payload_hash"] if approval_row else None
    current_payload_hash = ea._payload_hash(snap.get("provider_payload_preview"))
    payload_matches = bool(approved and approved_payload_hash
                           and approved_payload_hash == current_payload_hash)
    action_supported = _action_supported(provider_name, provider_type, action_type)

    # Consult the v1.7 feature flag matrix (read-only). A flag permits execution
    # only when enabled AND not dry_run_only — currently never, and a missing flag
    # fails closed.
    flag = await ff.get_flag(provider_name, action_type)
    flag_present = flag is not None
    flag_allows = ff.flag_allows_execution(flag)

    checks = {
        "source_approved": source_approved,
        "intent_approved_for_execution": approved,
        "approval_audit_exists": approval_audit_exists,
        "provider_connected": v["provider_connected"],
        "token_valid_or_refreshable": v["token_valid_or_refreshable"],
        "required_scopes_present": v["required_scopes_present"],
        "provider_supports_action": action_supported,
        "payload_hash_matches": payload_matches,
        "feature_flag_present": flag_present,
        # Future-phase gates — all OFF now, so execution can never proceed.
        "dry_run_cleared_for_execution": not v["dry_run_only"],
        "external_execution_enabled": guard.external_execution_enabled(),
        "feature_flag_allows_execution": flag_allows,
    }
    internal_ready = all(checks[k] for k in _INTERNAL_CHECKS)
    future_gates_open = (
        checks["dry_run_cleared_for_execution"]
        and checks["external_execution_enabled"]
        and checks["feature_flag_allows_execution"]
    )
    # HARD INVARIANT (spec #3): this phase always blocks. Even if everything were
    # internally ready, the future gates are off and we still never execute.
    real_execution_allowed = internal_ready and future_gates_open

    block_reasons = [k for k in _INTERNAL_CHECKS if not checks[k]]
    if not flag_present:
        block_reasons.append("feature_flag_missing (fail-closed)")
    if not future_gates_open:
        block_reasons.append("external_execution_disabled (dry_run + kill switch + feature flag)")

    if not (approval_audit_exists and approved):
        result_status = ST_MISSING_APPROVAL
    elif not (source_approved and v["provider_connected"]
              and v["token_valid_or_refreshable"] and v["required_scopes_present"]
              and action_supported):
        result_status = ST_PROVIDER_NOT_READY
    elif not payload_matches:
        result_status = ST_PAYLOAD_MISMATCH
    elif internal_ready and not future_gates_open:
        result_status = ST_READY_DISABLED
    else:
        result_status = ST_BLOCKED

    result = {
        "intent_id": str(intent["id"]),
        "status": result_status,
        "real_execution_allowed": real_execution_allowed,  # always False this phase
        "execution_enabled": guard.external_execution_enabled(),
        "dry_run_only": v["dry_run_only"],
        "provider_type": provider_type,
        "provider_name": provider_name,
        "action_type": action_type,
        "checks": checks,
        "block_reasons": block_reasons,
        "approval_evidence": {
            "approved": approved,
            "approver_id": str(approval_row["approver_id"]) if approval_row and approval_row["approver_id"] else None,
            "approved_at": approval_row["created_at"].isoformat() if approval_row else None,
            "reason": approval_row["reason"] if approval_row else None,
            "latest_decision": latest_decision,
        },
        "provider_readiness": {
            "provider_connected": v["provider_connected"],
            "token_valid_or_refreshable": v["token_valid_or_refreshable"],
            "required_scopes_present": v["required_scopes_present"],
            "missing_scopes": v["missing_scopes"],
            "provider_supports_action": action_supported,
        },
        "payload_hash": current_payload_hash,
        "approved_payload_hash": approved_payload_hash,
        "payload_matches": payload_matches,
        "payload_preview_ref": "metadata.credential_usage_simulation",
        "note": (
            "Final safety interlock — diagnostic only. It calls NO provider API, "
            "never clears dry_run, and never enables execution. Real execution "
            "stays blocked by the global kill switch."
        ),
    }

    workspace_id = intent.get("workspace_id")
    agent_name = intent.get("agent_name")
    # Trace #1 — always.
    await write_trace(
        session_id=None, user_id=user_id, trace_type=TRACE_CHECKED, status="ok",
        selected_agent=agent_name, tool_name=_TOOL,
        tool_result={"intent_id": str(intent["id"]), "status": result_status,
                     "real_execution_allowed": real_execution_allowed,
                     "checks": checks, "payload_matches": payload_matches},
        workspace_id=workspace_id,
    )
    # Trace #2 — outcome.
    if result_status == ST_READY_DISABLED:
        await write_trace(
            session_id=None, user_id=user_id, trace_type=TRACE_READY_DISABLED,
            status="blocked", selected_agent=agent_name, tool_name=_TOOL,
            tool_result={"intent_id": str(intent["id"]),
                         "reason": "all internal checks pass; execution disabled by governance",
                         "execution_enabled": False},
            error_message=guard.BLOCKED_MESSAGE, workspace_id=workspace_id,
        )
    else:
        await write_trace(
            session_id=None, user_id=user_id, trace_type=TRACE_BLOCKED,
            status="blocked", selected_agent=agent_name, tool_name=_TOOL,
            tool_result={"intent_id": str(intent["id"]), "status": result_status,
                         "block_reasons": block_reasons},
            error_message="final interlock blocked: " + ", ".join(block_reasons),
            workspace_id=workspace_id,
        )

    # Audit event (spec #7). No token material in the snapshot.
    async with pool.acquire() as conn:
        await ir._insert_event(
            conn, intent["id"], user_id,
            event_type=TRACE_CHECKED,
            from_status=intent.get("status"), to_status=intent.get("status"),
            notes=None,
            payload_snapshot={
                "status": result_status,
                "real_execution_allowed": real_execution_allowed,
                "payload_hash": current_payload_hash,
                "payload_matches": payload_matches,
                "checks": checks,
            },
        )
    return result
