"""Provider Execution Feature Flag Matrix v1.7 — centralized execution control.

Every (provider_name, action_type, environment) is independently controlled here.
The matrix is consulted by the adapter execution path (v1.6) and the final safety
interlock (v1.5). It FAILS CLOSED: a missing flag denies execution + records an
audit event + a runtime trace. A flag is necessary-but-NOT-sufficient — the
global kill switch + interlock still gate any real execution, which stays disabled
this phase. No provider API is called and no token is read here.

Runtime traces (spec #8): provider_flag_checked / provider_flag_denied /
provider_flag_allowed. Audit events (spec #9): feature_flag_created /
feature_flag_modified (runtime_traces, admin config) + feature_flag_denied_execution
(external_integration_events, intent-scoped on a consult deny).
"""

import uuid
from typing import Optional

from app.clients import clients
from app import integration_readiness as ir
from app.runtime_traces import write_trace
from app.tools.governance import log_execution_attempt

DEFAULT_ENV = "production"

# Canonicalize the names the rest of the system uses onto the seeded rows.
_PROVIDER_ALIASES = {"outlook_calendar": "microsoft_calendar"}
_ACTION_ALIASES = {"create_event": "create_calendar_event"}

# runtime traces
TRACE_CHECKED = "provider_flag_checked"
TRACE_DENIED = "provider_flag_denied"
TRACE_ALLOWED = "provider_flag_allowed"
# audit events
EV_CREATED = "feature_flag_created"
EV_MODIFIED = "feature_flag_modified"
EV_DENIED_EXECUTION = "feature_flag_denied_execution"

_TOOL = "provider_feature_flag"

_COLS = (
    "id, provider_name, provider_type, action_type, enabled, dry_run_only, "
    "requires_human_approval, requires_final_interlock, requires_valid_oauth, "
    "requires_scope_validation, requires_connected_provider, "
    "requires_payload_hash_match, requires_kill_switch_clear, environment, "
    "metadata, created_at, updated_at"
)

_MUTABLE = frozenset({
    "enabled", "dry_run_only", "requires_human_approval", "requires_final_interlock",
    "requires_valid_oauth", "requires_scope_validation", "requires_connected_provider",
    "requires_payload_hash_match", "requires_kill_switch_clear",
})


class FeatureFlagError(Exception):
    """code: not_found (404) | invalid (400) | forbidden (403) |
    conflict (409) | unavailable (503)."""

    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


def _require_pool():
    if clients.db_pool is None:
        raise FeatureFlagError("Postgres pool unavailable", code="unavailable")
    return clients.db_pool


def _norm_provider(name: Optional[str]) -> str:
    key = (name or "").strip().lower()
    return _PROVIDER_ALIASES.get(key, key)


def _norm_action(action: Optional[str]) -> str:
    key = (action or "").strip().lower()
    return _ACTION_ALIASES.get(key, key)


async def get_flag(provider_name: Optional[str], action_type: Optional[str], *,
                   environment: str = DEFAULT_ENV) -> Optional[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_COLS} FROM provider_execution_feature_flags "
            "WHERE provider_name = $1 AND action_type = $2 AND environment = $3",
            _norm_provider(provider_name), _norm_action(action_type), environment,
        )
    return dict(row) if row else None


def flag_allows_execution(flag: Optional[dict]) -> bool:
    """A flag PERMITS execution only when enabled AND not dry-run-only. Even then,
    the kill switch + interlock must also pass — so this is never sufficient alone."""
    return bool(flag and flag["enabled"] and not flag["dry_run_only"])


def _decision(provider_name, action_type, environment, flag) -> dict:
    present = flag is not None
    allows = flag_allows_execution(flag)
    if not present:
        reason = "no feature flag for this provider/action (fail-closed deny)"
    elif not flag["enabled"]:
        reason = "feature flag disabled"
    elif flag["dry_run_only"]:
        reason = "feature flag is dry_run_only"
    else:
        reason = "feature flag permits (other gates still apply)"
    return {
        "provider_name": _norm_provider(provider_name),
        "action_type": _norm_action(action_type),
        "environment": environment,
        "flag_present": present,
        "enabled": bool(flag["enabled"]) if present else False,
        "dry_run_only": bool(flag["dry_run_only"]) if present else True,
        "flag_allows_execution": allows,            # always False this phase
        "requires_human_approval": bool(flag["requires_human_approval"]) if present else True,
        "requires_final_interlock": bool(flag["requires_final_interlock"]) if present else True,
        "requires_kill_switch_clear": bool(flag["requires_kill_switch_clear"]) if present else True,
        "denied": not allows,
        "reason": reason,
    }


async def evaluate(provider_name: Optional[str], action_type: Optional[str], *,
                   user_id: Optional[uuid.UUID], intent: Optional[dict] = None,
                   environment: str = DEFAULT_ENV) -> dict:
    """Consult the matrix for a (provider, action). Writes provider_flag_checked +
    provider_flag_allowed/denied traces, and (on deny, when intent-scoped) a
    feature_flag_denied_execution audit event. FAILS CLOSED."""
    flag = await get_flag(provider_name, action_type, environment=environment)
    decision = _decision(provider_name, action_type, environment, flag)
    workspace_id = (intent or {}).get("workspace_id")
    agent_name = (intent or {}).get("agent_name")

    await write_trace(
        session_id=None, user_id=user_id, trace_type=TRACE_CHECKED, status="ok",
        selected_agent=agent_name, tool_name=_TOOL,
        tool_result={**decision, "intent_id": str(intent["id"]) if intent else None},
        workspace_id=workspace_id,
    )
    if decision["flag_allows_execution"]:
        await write_trace(
            session_id=None, user_id=user_id, trace_type=TRACE_ALLOWED, status="ok",
            selected_agent=agent_name, tool_name=_TOOL,
            tool_result={**decision, "intent_id": str(intent["id"]) if intent else None},
            workspace_id=workspace_id,
        )
    else:
        await write_trace(
            session_id=None, user_id=user_id, trace_type=TRACE_DENIED, status="blocked",
            selected_agent=agent_name, tool_name=_TOOL,
            tool_result={**decision, "intent_id": str(intent["id"]) if intent else None},
            error_message=decision["reason"], workspace_id=workspace_id,
        )
        if intent is not None:
            pool = _require_pool()
            async with pool.acquire() as conn:
                await ir._insert_event(
                    conn, intent["id"], user_id, event_type=EV_DENIED_EXECUTION,
                    from_status=intent.get("status"), to_status=intent.get("status"),
                    notes=None, payload_snapshot={
                        "provider_name": decision["provider_name"],
                        "action_type": decision["action_type"],
                        "flag_present": decision["flag_present"],
                        "reason": decision["reason"],
                    },
                )
    return decision


# --------------------------------------------------------------------------- #
# Admin CRUD (matrix management)
# --------------------------------------------------------------------------- #

async def list_flags(*, provider_name: Optional[str] = None,
                     action_type: Optional[str] = None,
                     environment: Optional[str] = None) -> list[dict]:
    pool = _require_pool()
    parts = [f"SELECT {_COLS} FROM provider_execution_feature_flags WHERE 1=1"]
    args: list = []
    if provider_name:
        args.append(_norm_provider(provider_name)); parts.append(f"AND provider_name = ${len(args)}")
    if action_type:
        args.append(_norm_action(action_type)); parts.append(f"AND action_type = ${len(args)}")
    if environment:
        args.append(environment); parts.append(f"AND environment = ${len(args)}")
    parts.append("ORDER BY provider_name, action_type, environment")
    async with pool.acquire() as conn:
        rows = await conn.fetch(" ".join(parts), *args)
    return [dict(r) for r in rows]


async def create_flag(*, admin_id: uuid.UUID, provider_name: str, provider_type: str,
                      action_type: str, environment: str = DEFAULT_ENV) -> dict:
    pool = _require_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO provider_execution_feature_flags
                    (provider_name, provider_type, action_type, environment)
                VALUES ($1,$2,$3,$4) RETURNING {_COLS}
                """,
                _norm_provider(provider_name), provider_type,
                _norm_action(action_type), environment,
            )
    except Exception as exc:  # unique violation etc.
        raise FeatureFlagError(f"could not create flag: {exc!s}", code="conflict")
    flag = dict(row)
    await log_execution_attempt(
        tool_name=_TOOL, agent_name=None, session_id=None, user_id=admin_id,
        scope_type=None, allowed=True, duration_ms=None, status="success",
        error_message=None,
    )
    await write_trace(
        session_id=None, user_id=admin_id, trace_type=EV_CREATED, status="ok",
        selected_agent=None, tool_name=_TOOL,
        tool_result={"flag_id": str(flag["id"]), "provider_name": flag["provider_name"],
                     "action_type": flag["action_type"], "environment": flag["environment"],
                     "enabled": flag["enabled"], "dry_run_only": flag["dry_run_only"]},
    )
    return flag


async def update_flag(flag_id: uuid.UUID, *, admin_id: uuid.UUID, changes: dict) -> dict:
    fields = {k: bool(v) for k, v in changes.items() if k in _MUTABLE}
    if not fields:
        raise FeatureFlagError("no valid fields to update", code="invalid")
    pool = _require_pool()
    sets = ", ".join(f"{k} = ${i}" for i, k in enumerate(fields, start=1))
    args = list(fields.values())
    args.append(flag_id)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE provider_execution_feature_flags SET {sets}, updated_at = NOW() "
            f"WHERE id = ${len(args)} RETURNING {_COLS}",
            *args,
        )
    if row is None:
        raise FeatureFlagError("flag not found", code="not_found")
    flag = dict(row)
    await log_execution_attempt(
        tool_name=_TOOL, agent_name=None, session_id=None, user_id=admin_id,
        scope_type=None, allowed=True, duration_ms=None, status="success",
        error_message=None,
    )
    await write_trace(
        session_id=None, user_id=admin_id, trace_type=EV_MODIFIED, status="ok",
        selected_agent=None, tool_name=_TOOL,
        tool_result={"flag_id": str(flag["id"]), "provider_name": flag["provider_name"],
                     "action_type": flag["action_type"], "changed": list(fields.keys()),
                     "enabled": flag["enabled"], "dry_run_only": flag["dry_run_only"]},
    )
    return flag
