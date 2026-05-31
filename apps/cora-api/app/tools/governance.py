"""Runtime governance for tool execution.

Decides whether a given tool execution attempt is permitted, why, and whether
confirmation is required. Writes an audit row to tool_execution_logs for both
allowed and denied attempts.

Precedence (highest first):
  0. external execution action              → deny (hard block, see below)
  1. tool.enabled = false                   → deny
  2. tool_execution_policies (tool, agent)  → explicit per-pair allow/deny + rate limit
  3. tool.allowed_agents (when non-empty)   → membership check
  4. risk_level == 'high' + not admin       → deny
  5. default                                → allow

agent_name == None means "no agent context" (manual user-triggered or admin
test). In that mode step 2 and step 3 are skipped; only enabled + risk-level
catch-alls apply.

EXTERNAL ACTION BLOCK (step 0): Cora is internal/review-only. Any tool that
would send mail or create/modify a calendar event on an external provider is
HARD-DENIED here regardless of how it is seeded in the `tools` table — this is a
code-level rail that a DB toggle cannot lift. Enabling real external execution
is a future, separately-governed phase (OAuth + confirmation + connectors). The
dry-run *_prepare_*_intent / *_dry_run preview tools are NOT external execution
and remain allowed.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.clients import clients

logger = logging.getLogger(__name__)

# User-facing message returned whenever an external execution action is blocked.
EXTERNAL_ACTION_BLOCK_MESSAGE = (
    "I can prepare this for review, but I cannot send or create external events yet."
)

# Explicitly enumerated external execution tools (exact tool names). These never
# exist as live endpoints today; the block ensures that if one is ever seeded it
# cannot run until the external integration phase is built.
EXTERNAL_EXECUTION_TOOLS = frozenset({
    "send_email",
    "send_outlook_email",
    "send_gmail",
    "create_calendar_event",
    "create_google_calendar_event",
    "create_outlook_calendar_event",
})

# Execution-verb name prefixes that catch "any provider execution action" beyond
# the enumerated set. Dry-run preview / intent-preparation tools are excluded by
# shape below, so allowed internal tools (signal_*, chronos_*, *_prepare_*_intent,
# *_dry_run) never match.
_EXTERNAL_EXEC_PREFIXES = (
    "send_",
    "create_calendar",
    "create_google_calendar",
    "create_outlook_calendar",
    "create_gcal",
    "execute_provider",
    "provider_execute",
)


def is_external_execution_tool(name: Optional[str]) -> bool:
    """True if `name` is a real external send/create action that must be blocked.
    Dry-run preview/intent tools (prepare…/…_intent/…_dry_run) are never external
    execution and return False."""
    n = (name or "").strip().lower()
    if not n:
        return False
    if n in EXTERNAL_EXECUTION_TOOLS:
        return True
    # Allow-by-shape: the dry-run readiness tools contain words like "send"/
    # "calendar" but only ever PREPARE a preview — they are not execution.
    if "prepare" in n or n.endswith("_intent") or "dry_run" in n:
        return False
    return n.startswith(_EXTERNAL_EXEC_PREFIXES)


@dataclass
class PermissionDecision:
    allowed: bool
    reason: str
    requires_confirmation: bool
    policy_source: str  # tool_disabled | policy_table | allowed_agents | risk_level | default
    matched_rule: str


async def fetch_tool(name: str) -> Optional[dict]:
    """Fetch the columns needed to run a permission check + execution log for a
    tool. Returns None if the pool is down or the tool is absent."""
    if clients.db_pool is None:
        return None
    async with clients.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, name, type, enabled, requires_confirmation,
                   risk_level, allowed_agents
            FROM tools
            WHERE name = $1
            """,
            name,
        )
    return dict(row) if row else None


async def _fetch_policy(tool_name: str, agent_name: str) -> Optional[dict]:
    if clients.db_pool is None:
        return None
    async with clients.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, tool_name, agent_name, allowed, requires_confirmation,
                   max_calls_per_hour, created_at, updated_at
            FROM tool_execution_policies
            WHERE tool_name = $1 AND agent_name = $2
            """,
            tool_name,
            agent_name,
        )
    return dict(row) if row else None


async def _count_recent_executions(
    tool_name: str,
    *,
    since: datetime,
    user_id: Optional[uuid.UUID] = None,
    agent_name: Optional[str] = None,
) -> int:
    if clients.db_pool is None:
        return 0
    parts = [
        "SELECT COUNT(*) FROM tool_execution_logs",
        "WHERE tool_name = $1 AND allowed = TRUE AND created_at >= $2",
    ]
    args: list = [tool_name, since]
    if user_id is not None:
        args.append(user_id)
        parts.append(f"AND user_id = ${len(args)}")
    if agent_name is not None:
        args.append(agent_name)
        parts.append(f"AND agent_name = ${len(args)}")
    async with clients.db_pool.acquire() as conn:
        return int(await conn.fetchval(" ".join(parts), *args) or 0)


async def check_permission(
    tool: dict,
    *,
    agent_name: Optional[str],
    user_id: Optional[uuid.UUID],
    is_admin: bool = False,
) -> PermissionDecision:
    tool_name = tool.get("name", "<unknown>")

    # Step 0: hard external-action block. Highest precedence — applies even if
    # the tool is enabled/allow-listed. No external send or calendar write may
    # run in this build.
    if is_external_execution_tool(tool_name):
        return PermissionDecision(
            allowed=False,
            reason=EXTERNAL_ACTION_BLOCK_MESSAGE,
            requires_confirmation=False,
            policy_source="external_action_block",
            matched_rule="external_execution_tool",
        )

    if not tool.get("enabled"):
        return PermissionDecision(
            allowed=False,
            reason="tool is disabled",
            requires_confirmation=False,
            policy_source="tool_disabled",
            matched_rule="tool.enabled=false",
        )

    requires_confirmation = bool(tool.get("requires_confirmation"))
    policy_source = "default"
    matched_rule = "default allow"

    # Steps 2 + 3 apply only when an agent is doing the dispatch.
    # Admin overrides skip the agent allowlist but still respect explicit denies.
    if agent_name is not None:
        policy = await _fetch_policy(tool_name, agent_name)
        if policy is not None:
            policy_source = "policy_table"
            matched_rule = f"policy({tool_name},{agent_name})"
            if not policy["allowed"]:
                return PermissionDecision(
                    allowed=False,
                    reason="explicitly denied by execution policy",
                    requires_confirmation=False,
                    policy_source=policy_source,
                    matched_rule=matched_rule + " allowed=false",
                )
            if policy["requires_confirmation"]:
                requires_confirmation = True
            mcph = policy.get("max_calls_per_hour")
            if mcph is not None:
                since = datetime.now(timezone.utc) - timedelta(hours=1)
                cnt = await _count_recent_executions(
                    tool_name,
                    since=since,
                    agent_name=agent_name,
                )
                if cnt >= mcph:
                    return PermissionDecision(
                        allowed=False,
                        reason=f"rate limit exceeded ({cnt}/{mcph} per hour for agent {agent_name})",
                        requires_confirmation=False,
                        policy_source=policy_source,
                        matched_rule=matched_rule + ".max_calls_per_hour",
                    )
        elif not is_admin:
            allowed_agents = list(tool.get("allowed_agents") or [])
            if allowed_agents:
                policy_source = "allowed_agents"
                matched_rule = f"tool.allowed_agents={allowed_agents}"
                if agent_name not in allowed_agents:
                    return PermissionDecision(
                        allowed=False,
                        reason=f"agent {agent_name!r} not in tool.allowed_agents",
                        requires_confirmation=False,
                        policy_source=policy_source,
                        matched_rule=matched_rule,
                    )
            else:
                matched_rule = "tool.allowed_agents empty (unrestricted)"

    # Step 4: risk-level catch-all. high-risk tools require admin invocation.
    if tool.get("risk_level") == "high" and not is_admin:
        return PermissionDecision(
            allowed=False,
            reason="high-risk tools require admin invocation",
            requires_confirmation=False,
            policy_source="risk_level",
            matched_rule="risk_level=high",
        )

    return PermissionDecision(
        allowed=True,
        reason="allowed",
        requires_confirmation=requires_confirmation,
        policy_source=policy_source,
        matched_rule=matched_rule,
    )


async def log_execution_attempt(
    *,
    tool_name: str,
    agent_name: Optional[str],
    session_id: Optional[uuid.UUID],
    user_id: Optional[uuid.UUID],
    scope_type: Optional[str],
    allowed: bool,
    duration_ms: Optional[int],
    status: str,
    error_message: Optional[str] = None,
) -> None:
    if clients.db_pool is None:
        logger.warning(
            "tool execution log skipped: pool unavailable tool=%s agent=%s",
            tool_name,
            agent_name,
        )
        return
    try:
        async with clients.db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tool_execution_logs (
                    session_id, user_id, tool_name, agent_name, scope_type,
                    allowed, duration_ms, status, error_message
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                session_id,
                user_id,
                tool_name,
                agent_name,
                scope_type,
                allowed,
                duration_ms,
                status,
                error_message,
            )
    except Exception:
        logger.exception(
            "tool execution log write failed: tool=%s agent=%s status=%s",
            tool_name,
            agent_name,
            status,
        )

    log_fn = logger.info if allowed else logger.warning
    log_fn(
        "tool execution %s: tool=%s agent=%s user_id=%s session_id=%s "
        "status=%s duration_ms=%s error=%s",
        "allowed" if allowed else "denied",
        tool_name,
        agent_name,
        user_id,
        session_id,
        status,
        duration_ms,
        error_message,
    )


async def enforce_external_action_block(
    *,
    tool_name: str,
    agent_name: Optional[str],
    session_id: Optional[uuid.UUID],
    user_id: Optional[uuid.UUID],
    scope_type: Optional[str] = None,
    workspace_id: Optional[uuid.UUID] = None,
) -> Optional[str]:
    """Single choke point for external execution actions.

    If `tool_name` is an external send/calendar-write action, record the block
    (tool_execution_logs allowed=false/status=blocked + a governance_blocked
    runtime trace) and return the user-facing message. Returns None if the tool
    is NOT an external execution action, in which case the caller proceeds
    normally. The future external-integration phase MUST call this before any
    real provider dispatch.
    """
    if not is_external_execution_tool(tool_name):
        return None
    await log_execution_attempt(
        tool_name=tool_name,
        agent_name=agent_name,
        session_id=session_id,
        user_id=user_id,
        scope_type=scope_type,
        allowed=False,
        duration_ms=None,
        status="blocked",
        error_message=EXTERNAL_ACTION_BLOCK_MESSAGE,
    )
    # Lazy import keeps governance free of a runtime_traces import cycle.
    from app.runtime_traces import write_trace

    await write_trace(
        session_id=session_id,
        user_id=user_id,
        trace_type="governance_blocked",
        status="blocked",
        selected_agent=agent_name,
        tool_name=tool_name,
        tool_result={"block": "external_action", "reason": EXTERNAL_ACTION_BLOCK_MESSAGE},
        error_message=EXTERNAL_ACTION_BLOCK_MESSAGE,
        workspace_id=workspace_id,
    )
    return EXTERNAL_ACTION_BLOCK_MESSAGE
