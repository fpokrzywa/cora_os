"""Execution Governance Dashboard v1.8 — observability ONLY, read-only.

Aggregates the full execution-governance picture (drafts, approvals, integration
intents/events, provider readiness, feature flags, final-interlock + adapter
traces, governance blocks, tool failures) from EXISTING tables. It performs no
mutation, calls no provider API, and NEVER returns secrets: OAuth access/refresh
tokens and full credential payloads are excluded; payload previews are summarized
+ truncated. Writes one `execution_governance_dashboard_viewed` runtime trace per
load; no tool_execution_logs (not a governed tool path).
"""

import uuid
from typing import Optional

from app.clients import clients
from app import feature_flags as ff
from app.runtime_traces import write_trace

DASHBOARD_TRACE = "execution_governance_dashboard_viewed"

_LIST_LIMIT = 12
_CARD_LIMIT = 8

# Trace types treated as governance blocks.
_BLOCK_TRACE_TYPES = (
    "governance_blocked", "external_execution_blocked", "provider_flag_denied",
    "final_interlock_blocked", "provider_adapter_execution_blocked",
    "provider_execution_blocked_by_governance",
)
# tool_execution_logs statuses treated as failures/denials.
_FAIL_STATUSES = ("failed", "denied", "blocked", "error")


class DashboardError(Exception):
    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


def _require_pool():
    if clients.db_pool is None:
        raise DashboardError("Postgres pool unavailable", code="unavailable")
    return clients.db_pool


def _trunc(value, n: int = 100) -> Optional[str]:
    if value is None:
        return None
    s = str(value)
    return s if len(s) <= n else s[:n] + "…"


def _scope(args: list, conds: list, *, owner_col=None, owner=None, ws_col=None,
           workspace_id=None, date_col="created_at", date_from=None, date_to=None):
    if owner_col and owner is not None:
        args.append(owner); conds.append(f"{owner_col} = ${len(args)}")
    if ws_col and workspace_id is not None:
        args.append(workspace_id); conds.append(f"{ws_col} = ${len(args)}")
    if date_from is not None:
        args.append(date_from); conds.append(f"{date_col} >= ${len(args)}::timestamptz")
    if date_to is not None:
        args.append(date_to); conds.append(f"{date_col} <= ${len(args)}::timestamptz")


async def build_dashboard(
    *, user_id: uuid.UUID, is_admin: bool,
    provider_name: Optional[str] = None, action_type: Optional[str] = None,
    status: Optional[str] = None, date_from: Optional[str] = None,
    date_to: Optional[str] = None, workspace_id: Optional[uuid.UUID] = None,
    target_user_id: Optional[uuid.UUID] = None,
) -> dict:
    pool = _require_pool()
    # Owner scope: non-admins see only their own; admins see all (or a filtered user).
    owner: Optional[uuid.UUID] = None if is_admin else user_id
    if is_admin and target_user_id is not None:
        owner = target_user_id

    def base(owner_col, *, ws_col=None):
        args: list = []
        conds: list = []
        _scope(args, conds, owner_col=owner_col, owner=owner, ws_col=ws_col,
               workspace_id=workspace_id, date_from=date_from, date_to=date_to)
        where = (" AND " + " AND ".join(conds)) if conds else ""
        return where, args

    async with pool.acquire() as conn:
        # --- recent drafts (communication_drafts) ---
        w, a = base("created_by", ws_col="workspace_id")
        drafts = [dict(r) for r in await conn.fetch(
            f"SELECT id, draft_type, subject, status, created_at FROM communication_drafts "
            f"WHERE 1=1{w} ORDER BY created_at DESC LIMIT {_LIST_LIMIT}", *a)]
        recent_drafts = [{
            "id": str(d["id"]), "draft_type": d["draft_type"],
            "subject": _trunc(d["subject"], 80), "status": d["status"],
            "created_at": d["created_at"],
        } for d in drafts]

        # --- recent approval events (execution_approvals) ---
        w, a = base("approver_id")
        approvals = [dict(r) for r in await conn.fetch(
            f"SELECT id, intent_id, approver_id, decision, approval_state, reason, "
            f"payload_hash, created_at FROM execution_approvals WHERE 1=1{w} "
            f"ORDER BY created_at DESC LIMIT {_LIST_LIMIT}", *a)]
        recent_approvals = [{
            "id": str(r["id"]), "intent_id": str(r["intent_id"]),
            "approver_id": str(r["approver_id"]) if r["approver_id"] else None,
            "decision": r["decision"], "approval_state": r["approval_state"],
            "reason": _trunc(r["reason"], 120),
            "payload_hash": (r["payload_hash"] or "")[:16] or None,
            "created_at": r["created_at"],
        } for r in approvals]

        # --- recent integration intents (filterable) ---
        args: list = []
        conds: list = []
        _scope(args, conds, owner_col="created_by", owner=owner, ws_col="workspace_id",
               workspace_id=workspace_id, date_from=date_from, date_to=date_to)
        if provider_name:
            args.append(provider_name); conds.append(f"provider_name = ${len(args)}")
        if action_type:
            args.append(action_type); conds.append(f"action_type = ${len(args)}")
        if status:
            args.append(status); conds.append(f"status = ${len(args)}")
        iw = (" AND " + " AND ".join(conds)) if conds else ""
        intents = [dict(r) for r in await conn.fetch(
            f"SELECT id, created_by, workspace_id, agent_name, source_type, source_id, "
            f"provider_type, provider_name, action_type, status, dry_run, payload_preview, "
            f"metadata, created_at FROM external_integration_intents WHERE 1=1{iw} "
            f"ORDER BY created_at DESC LIMIT {_LIST_LIMIT}", *args)]

        def intent_brief(r):
            pp = r.get("payload_preview") or {}
            meta = r.get("metadata") or {}
            return {
                "id": str(r["id"]), "agent_name": r["agent_name"],
                "source_type": r["source_type"], "source_id": str(r["source_id"]),
                "provider_type": r["provider_type"], "provider_name": r["provider_name"],
                "action_type": r["action_type"], "status": r["status"],
                "dry_run": r["dry_run"],
                "approval_state": meta.get("approval_state"),
                "payload_summary": {
                    "subject": _trunc(pp.get("subject"), 80),
                    "title": _trunc(pp.get("title"), 80),
                },
                "created_at": r["created_at"],
            }
        recent_intents = [intent_brief(r) for r in intents]

        # --- recent integration events ---
        w, a = base(None, ws_col=None)  # events have no owner/workspace columns
        events = [dict(r) for r in await conn.fetch(
            f"SELECT id, intent_id, event_type, from_status, to_status, created_at "
            f"FROM external_integration_events WHERE 1=1{w} "
            f"ORDER BY created_at DESC LIMIT {_LIST_LIMIT}", *a)]
        recent_events = [{
            "id": str(e["id"]), "intent_id": str(e["intent_id"]),
            "event_type": e["event_type"], "from_status": e["from_status"],
            "to_status": e["to_status"], "created_at": e["created_at"],
        } for e in events]

        # --- provider readiness (NO secret columns) ---
        ra: list = []
        rc: list = []
        if owner is not None:
            ra.append(owner); rc.append(f"user_id = ${len(ra)}")
        rwhere = (" AND " + " AND ".join(rc)) if rc else ""
        connectors = [dict(r) for r in await conn.fetch(
            f"SELECT user_id, provider_name, provider_type, status, scopes, "
            f"token_expires_at, updated_at, "
            f"(access_token_encrypted IS NOT NULL) AS has_access_token, "
            f"(refresh_token_encrypted IS NOT NULL) AS has_refresh_token "
            f"FROM provider_oauth_connectors WHERE 1=1{rwhere} "
            f"ORDER BY updated_at DESC LIMIT 50", *ra)]
        provider_readiness = [{
            "provider_name": c["provider_name"], "provider_type": c["provider_type"],
            "status": c["status"], "scope_count": len(c["scopes"] or []),
            "has_access_token": c["has_access_token"],
            "has_refresh_token": c["has_refresh_token"],
            "token_expires_at": c["token_expires_at"], "updated_at": c["updated_at"],
        } for c in connectors]
        readiness_summary = {
            "total": len(connectors),
            "connected": sum(1 for c in connectors if c["status"] == "connected"),
            "expired": sum(1 for c in connectors if c["status"] == "expired"),
            "disconnected": sum(1 for c in connectors if c["status"] == "disconnected"),
        }

        # --- feature flag summary ---
        flags = [dict(r) for r in await conn.fetch(
            "SELECT provider_name, provider_type, action_type, enabled, dry_run_only, "
            "environment FROM provider_execution_feature_flags "
            "ORDER BY provider_name, action_type")]
        feature_flags = [dict(f) for f in flags]
        feature_flag_summary = {
            "total": len(flags),
            "enabled": sum(1 for f in flags if f["enabled"]),
            "dry_run_only": sum(1 for f in flags if f["dry_run_only"]),
        }

        # --- trace helpers ---
        async def traces_like(pattern=None, types=None, *, status_eq=None, limit=_LIST_LIMIT):
            ta: list = []
            tc: list = []
            _scope(ta, tc, owner_col="user_id", owner=owner, ws_col="workspace_id",
                   workspace_id=workspace_id, date_from=date_from, date_to=date_to)
            if pattern:
                ta.append(pattern); tc.append(f"trace_type LIKE ${len(ta)}")
            if types:
                ta.append(list(types)); tc.append(f"trace_type = ANY(${len(ta)})")
            if status_eq:
                ta.append(status_eq); tc.append(f"status = ${len(ta)}")
            tw = (" AND " + " AND ".join(tc)) if tc else ""
            rows = await conn.fetch(
                f"SELECT created_at, trace_type, status, tool_name, error_message, "
                f"tool_result->>'intent_id' AS intent_id, "
                f"tool_result->>'provider_name' AS provider_name, "
                f"tool_result->>'action_type' AS action_type, "
                f"tool_result->>'status' AS result_status, "
                f"tool_result->>'reason' AS reason "
                f"FROM runtime_traces WHERE 1=1{tw} ORDER BY created_at DESC LIMIT {limit}",
                *ta)
            return [{
                "created_at": r["created_at"], "trace_type": r["trace_type"],
                "status": r["status"], "tool_name": r["tool_name"],
                "intent_id": r["intent_id"], "provider_name": r["provider_name"],
                "action_type": r["action_type"], "result_status": r["result_status"],
                "reason": _trunc(r["reason"] or r["error_message"], 140),
            } for r in rows]

        interlock_traces = await traces_like(pattern="final_interlock_%")
        adapter_traces = await traces_like(pattern="provider_adapter_%")
        governance_blocks = await traces_like(types=_BLOCK_TRACE_TYPES)

        # --- tool execution failures ---
        fa: list = []
        fc: list = ["status = ANY($1)"]
        fa.append(list(_FAIL_STATUSES))
        _scope(fa, fc, owner_col="user_id", owner=owner, date_from=date_from, date_to=date_to)
        fw = " AND ".join(fc)
        failrows = await conn.fetch(
            f"SELECT created_at, tool_name, agent_name, status, allowed, error_message "
            f"FROM tool_execution_logs WHERE {fw} ORDER BY created_at DESC LIMIT {_LIST_LIMIT}",
            *fa)
        tool_failures = [{
            "created_at": r["created_at"], "tool_name": r["tool_name"],
            "agent_name": r["agent_name"], "status": r["status"],
            "allowed": r["allowed"], "error_message": _trunc(r["error_message"], 140),
        } for r in failrows]

        # --- summary counts ---
        async def count(sql, *args):
            return await conn.fetchval(sql, *args) or 0

        dw, da = base("created_by", ws_col="workspace_id")
        drafts_total = await count(
            f"SELECT count(*) FROM communication_drafts WHERE 1=1{dw}", *da)
        intents_total = await count(
            f"SELECT count(*) FROM external_integration_intents WHERE 1=1{iw}", *args)
        aw, aa = base("approver_id")
        approvals_total = await count(
            f"SELECT count(*) FROM execution_approvals WHERE 1=1{aw}", *aa)
        approved_total = await count(
            f"SELECT count(*) FROM execution_approvals WHERE decision='approved_for_execution'{aw}", *aa)

        summary = {
            "drafts": drafts_total,
            "integration_intents": intents_total,
            "approval_decisions": approvals_total,
            "approved_for_execution": approved_total,
            "providers_connected": readiness_summary["connected"],
            "feature_flags_enabled": feature_flag_summary["enabled"],
            "governance_blocks_recent": len(governance_blocks),
            "tool_failures_recent": len(tool_failures),
            "external_execution_enabled": False,
        }

        # --- drill-down cards (per recent intent) ---
        cards = []
        for r in intents[:_CARD_LIMIT]:
            iid = r["id"]
            # connected provider for this intent's type (no secrets).
            conn_row = await conn.fetchrow(
                "SELECT provider_name, status FROM provider_oauth_connectors "
                "WHERE user_id = $1 AND provider_type = $2 AND status <> 'disconnected' "
                "ORDER BY (status='connected') DESC, created_at DESC LIMIT 1",
                r["created_by"], r["provider_type"])
            connected_provider = conn_row["provider_name"] if conn_row else None
            readiness_state = conn_row["status"] if conn_row else "not_configured"
            flag = await ff.get_flag(connected_provider or r["provider_name"], r["action_type"])
            # latest trace for this intent + latest block reason.
            latest = await conn.fetchrow(
                "SELECT status, trace_type FROM runtime_traces "
                "WHERE tool_result->>'intent_id' = $1 ORDER BY created_at DESC LIMIT 1",
                str(iid))
            block = await conn.fetchrow(
                "SELECT trace_type, error_message FROM runtime_traces "
                "WHERE tool_result->>'intent_id' = $1 AND status = 'blocked' "
                "ORDER BY created_at DESC LIMIT 1", str(iid))
            meta = r.get("metadata") or {}
            cards.append({
                "intent_id": str(iid), "provider_type": r["provider_type"],
                "connected_provider": connected_provider,
                "action_type": r["action_type"], "status": r["status"],
                "source_type": r["source_type"], "source_id": str(r["source_id"]),
                "approval_state": meta.get("approval_state"),
                "latest_trace_status": latest["status"] if latest else None,
                "latest_trace_type": latest["trace_type"] if latest else None,
                "latest_block_reason": _trunc(
                    (block["error_message"] or block["trace_type"]) if block else None, 140),
                "feature_flag_state": {
                    "present": flag is not None,
                    "enabled": bool(flag["enabled"]) if flag else False,
                    "dry_run_only": bool(flag["dry_run_only"]) if flag else True,
                },
                "provider_readiness_state": readiness_state,
            })

    # Observability view trace (spec #16). No tool_execution_logs (#17).
    await write_trace(
        session_id=None, user_id=user_id, trace_type=DASHBOARD_TRACE, status="ok",
        selected_agent=None, tool_name="execution_governance_dashboard",
        tool_result={"is_admin": is_admin,
                     "filters": {"provider_name": provider_name, "action_type": action_type,
                                 "status": status, "workspace_id": str(workspace_id) if workspace_id else None},
                     "summary": summary},
        workspace_id=workspace_id,
    )

    return {
        "safety_banner": ("Provider execution remains disabled. This dashboard is "
                          "observability-only."),
        "external_execution_enabled": False,
        "summary": summary,
        "recent_drafts": recent_drafts,
        "recent_approval_events": recent_approvals,
        "recent_integration_intents": recent_intents,
        "recent_integration_events": recent_events,
        "provider_readiness": provider_readiness,
        "provider_readiness_summary": readiness_summary,
        "feature_flags": feature_flags,
        "feature_flag_summary": feature_flag_summary,
        "interlock_traces": interlock_traces,
        "adapter_traces": adapter_traces,
        "governance_blocks": governance_blocks,
        "tool_failures": tool_failures,
        "cards": cards,
    }
