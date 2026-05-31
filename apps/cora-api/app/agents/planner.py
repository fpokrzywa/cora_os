"""ATLAS execution planner — v0.1 template-based.

Generates a deterministic multi-step plan from a goal string. No LLM is
involved; this is intentional — autonomous LLM-driven planning is out of
scope for v0.1.

The planner only *creates* plans. It does NOT execute steps. Step transitions
will be wired in a later phase.
"""

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.clients import clients

logger = logging.getLogger(__name__)


# ---------- Intent detection ----------

_PLAN_INTENT_PATTERNS: list[re.Pattern] = [
    # "how would you build X" (and synonyms)
    re.compile(
        r"^\s*how\s+would\s+you\s+(?:build|design|implement|approach|tackle)\s+(.+)$",
        re.IGNORECASE | re.DOTALL,
    ),
    # "steps to X" / "steps for X"
    re.compile(
        r"^\s*steps\s+(?:to|for)\s+(.+)$",
        re.IGNORECASE | re.DOTALL,
    ),
    # Main form: optional verb (build/create/make/...), optional article
    # (a/an/the), optional "execution", then "plan [for/to/of/on/about] X".
    # Catches "Build a plan for adding X", "build plan for X",
    # "execution plan for X", "plan for X", "plan X".
    re.compile(
        r"^\s*"
        r"(?:(?:build|create|make|draft|generate|write|propose)\s+)?"
        r"(?:(?:a|an|the)\s+)?"
        r"(?:execution\s+)?"
        r"plan(?:\s+(?:for|to|of|on|about|around))?\s+(.+)$",
        re.IGNORECASE | re.DOTALL,
    ),
    # "plan: X" / "plan - X" (explicit delimiter, no preposition)
    re.compile(
        r"^\s*(?:execution\s+)?plan\s*[:\-]\s*(.+)$",
        re.IGNORECASE | re.DOTALL,
    ),
]


def match_plan_intent(message: str) -> Optional[str]:
    """Return the extracted goal string if the message looks like a plan
    request, else None. Requires a goal — bare 'plan' alone is ignored."""
    for pat in _PLAN_INTENT_PATTERNS:
        m = pat.match(message)
        if m:
            goal = m.group(1).strip().rstrip(".,;:!?")
            if goal:
                return goal
    return None


# ---------- Template plan generation ----------


def _title_from_goal(goal: str) -> str:
    title = goal.strip().splitlines()[0] if goal else "Plan"
    if len(title) > 120:
        title = title[:120].rstrip() + "…"
    return title or "Plan"


def build_template_plan(goal: str) -> tuple[str, list[dict]]:
    """Returns (plan_title, [step, ...]).

    Each step is a dict: {title, description, assigned_agent, tool_name?}.
    The template is intentionally generic; specialization happens later when
    real planner agents are wired in.
    """
    title = _title_from_goal(goal)
    steps: list[dict] = [
        {
            "title": "Define scope and constraints",
            "description": (
                "Clarify the goal, enumerate explicit requirements, and note "
                "constraints (governance, security, scope_type, performance)."
            ),
            "assigned_agent": "ATLAS",
            "tool_name": None,
        },
        {
            "title": "Inventory relevant components",
            "description": (
                "Identify existing Cora services, tools, MCP servers, agents, "
                "and memory entries relevant to the goal."
            ),
            "assigned_agent": "ATLAS",
            "tool_name": None,
        },
        {
            "title": "Propose technical approach",
            "description": (
                "Outline architecture: data flow, agent routing, governance "
                "controls, observability touchpoints."
            ),
            "assigned_agent": "FORGE",
            "tool_name": None,
        },
        {
            "title": "Implementation breakdown",
            "description": (
                "Break the work into ordered tasks with file paths, tools "
                "involved, and verification steps per task."
            ),
            "assigned_agent": "FORGE",
            "tool_name": None,
        },
        {
            "title": "Verification and rollout",
            "description": (
                "Define how to validate (tests, traces, manual probes) and "
                "ship (rebuild, restart, observe)."
            ),
            "assigned_agent": "FORGE",
            "tool_name": None,
        },
    ]
    return title, steps


# ---------- DB helpers ----------


async def create_plan(
    *,
    session_id: Optional[uuid.UUID],
    user_id: Optional[uuid.UUID],
    goal: str,
    workspace_id: Optional[uuid.UUID] = None,
) -> Optional[dict]:
    """Insert plan + steps in a single transaction. Returns the plan dict
    with embedded `steps`. None if the pool is unavailable."""
    if clients.db_pool is None:
        logger.warning("plan create skipped: pool unavailable")
        return None

    title, steps = build_template_plan(goal)
    async with clients.db_pool.acquire() as conn:
        async with conn.transaction():
            plan_row = await conn.fetchrow(
                """
                INSERT INTO execution_plans (
                    session_id, user_id, title, goal, status,
                    current_step, total_steps, selected_agent, workspace_id
                )
                VALUES ($1, $2, $3, $4, 'planned', 0, $5, 'ATLAS', $6)
                RETURNING id, session_id, user_id, title, goal, status,
                          current_step, total_steps, selected_agent,
                          created_at, updated_at
                """,
                session_id,
                user_id,
                title,
                goal,
                len(steps),
                workspace_id,
            )
            plan_id = plan_row["id"]
            inserted_steps: list[dict] = []
            for idx, step in enumerate(steps, start=1):
                step_row = await conn.fetchrow(
                    """
                    INSERT INTO execution_plan_steps (
                        plan_id, step_number, title, description,
                        assigned_agent, tool_name, status, workspace_id
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7)
                    RETURNING id, plan_id, step_number, title, description,
                              assigned_agent, tool_name, status, result,
                              started_at, completed_at, created_at
                    """,
                    plan_id,
                    idx,
                    step["title"],
                    step["description"],
                    step.get("assigned_agent"),
                    step.get("tool_name"),
                    workspace_id,
                )
                inserted_steps.append(dict(step_row))
    plan_dict = dict(plan_row)
    plan_dict["steps"] = inserted_steps
    logger.info(
        "plan created: plan_id=%s user_id=%s session_id=%s steps=%s "
        "title=%r",
        plan_dict["id"],
        user_id,
        session_id,
        len(inserted_steps),
        title,
    )
    return plan_dict


async def list_plans(
    *,
    user_id: Optional[uuid.UUID],
    is_admin: bool,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    if clients.db_pool is None:
        return []
    async with clients.db_pool.acquire() as conn:
        if is_admin:
            rows = await conn.fetch(
                """
                SELECT id, session_id, user_id, title, goal, status,
                       current_step, total_steps, selected_agent,
                       created_at, updated_at
                FROM execution_plans
                ORDER BY created_at DESC
                LIMIT $1 OFFSET $2
                """,
                limit,
                offset,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, session_id, user_id, title, goal, status,
                       current_step, total_steps, selected_agent,
                       created_at, updated_at
                FROM execution_plans
                WHERE user_id = $1
                ORDER BY created_at DESC
                LIMIT $2 OFFSET $3
                """,
                user_id,
                limit,
                offset,
            )
    return [dict(r) for r in rows]


async def get_plan(
    plan_id: uuid.UUID,
    *,
    user_id: Optional[uuid.UUID],
    is_admin: bool,
) -> Optional[dict]:
    if clients.db_pool is None:
        return None
    async with clients.db_pool.acquire() as conn:
        if is_admin:
            plan_row = await conn.fetchrow(
                """
                SELECT id, session_id, user_id, title, goal, status,
                       current_step, total_steps, selected_agent,
                       created_at, updated_at
                FROM execution_plans WHERE id = $1
                """,
                plan_id,
            )
        else:
            plan_row = await conn.fetchrow(
                """
                SELECT id, session_id, user_id, title, goal, status,
                       current_step, total_steps, selected_agent,
                       created_at, updated_at
                FROM execution_plans WHERE id = $1 AND user_id = $2
                """,
                plan_id,
                user_id,
            )
        if plan_row is None:
            return None
        step_rows = await conn.fetch(
            """
            SELECT id, plan_id, step_number, title, description,
                   assigned_agent, tool_name, status, result,
                   started_at, completed_at, created_at
            FROM execution_plan_steps
            WHERE plan_id = $1
            ORDER BY step_number ASC
            """,
            plan_id,
        )
    plan_dict = dict(plan_row)
    plan_dict["steps"] = [dict(r) for r in step_rows]
    return plan_dict


# ---------- Status / transition validation ----------

PLAN_VALID_TRANSITIONS: dict[str, set[str]] = {
    "planned": {"planned", "running", "completed", "cancelled", "failed"},
    "running": {"running", "completed", "cancelled", "failed"},
}
PLAN_TERMINAL_STATUSES: set[str] = {"completed", "cancelled", "failed"}

STEP_VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"pending", "running", "completed", "failed", "skipped"},
    "running": {"running", "completed", "failed", "skipped"},
}
STEP_TERMINAL_STATUSES: set[str] = {"completed", "failed", "skipped"}


class PlanError(Exception):
    """Raised for plan-management constraint failures (validation, auth)."""

    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


def _check_plan_transition(current: str, target: str) -> None:
    if current == target:
        return
    if current in PLAN_TERMINAL_STATUSES:
        raise PlanError(
            f"plan is {current} (terminal); cannot transition to {target}",
            code="terminal",
        )
    allowed = PLAN_VALID_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise PlanError(
            f"invalid plan transition {current!r} → {target!r}",
            code="invalid_transition",
        )


def _check_step_transition(current: str, target: str) -> None:
    if current == target:
        return
    if current in STEP_TERMINAL_STATUSES:
        raise PlanError(
            f"step is {current} (terminal); cannot transition to {target}",
            code="terminal",
        )
    allowed = STEP_VALID_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise PlanError(
            f"invalid step transition {current!r} → {target!r}",
            code="invalid_transition",
        )


def _ensure_authorized(plan_row: dict, user_id: uuid.UUID, is_admin: bool) -> None:
    if is_admin:
        return
    if plan_row["user_id"] == user_id:
        return
    raise PlanError("not authorized to modify this plan", code="forbidden")


async def _recompute_current_step(conn, plan_id: uuid.UUID) -> int:
    """current_step = count of steps in a terminal state. Returns the value."""
    count = await conn.fetchval(
        """
        SELECT COUNT(*) FROM execution_plan_steps
        WHERE plan_id = $1 AND status IN ('completed','failed','skipped')
        """,
        plan_id,
    )
    count = int(count or 0)
    await conn.execute(
        """
        UPDATE execution_plans
        SET current_step = $1, updated_at = NOW()
        WHERE id = $2
        """,
        count,
        plan_id,
    )
    return count


async def update_plan(
    plan_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    is_admin: bool,
    title: Optional[str] = None,
    goal: Optional[str] = None,
    status_value: Optional[str] = None,
) -> dict:
    if clients.db_pool is None:
        raise PlanError("Postgres pool unavailable", code="unavailable")
    async with clients.db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM execution_plans WHERE id = $1 FOR UPDATE",
                plan_id,
            )
            if row is None:
                raise PlanError("plan not found", code="not_found")
            _ensure_authorized(dict(row), user_id, is_admin)
            new_title = title if title is not None else row["title"]
            new_goal = goal if goal is not None else row["goal"]
            new_status = status_value if status_value is not None else row["status"]
            if status_value is not None:
                _check_plan_transition(row["status"], status_value)
            updated = await conn.fetchrow(
                """
                UPDATE execution_plans
                SET title = $2, goal = $3, status = $4, updated_at = NOW()
                WHERE id = $1
                RETURNING id, session_id, user_id, title, goal, status,
                          current_step, total_steps, selected_agent,
                          created_at, updated_at
                """,
                plan_id,
                new_title,
                new_goal,
                new_status,
            )
    return dict(updated)


async def update_step(
    plan_id: uuid.UUID,
    step_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    is_admin: bool,
    title: Optional[str] = None,
    description: Optional[str] = None,
    assigned_agent: Optional[str] = None,
    tool_name: Optional[str] = None,
    status_value: Optional[str] = None,
    result: Optional[dict] = None,
) -> dict:
    if clients.db_pool is None:
        raise PlanError("Postgres pool unavailable", code="unavailable")
    async with clients.db_pool.acquire() as conn:
        async with conn.transaction():
            plan_row = await conn.fetchrow(
                "SELECT * FROM execution_plans WHERE id = $1 FOR UPDATE",
                plan_id,
            )
            if plan_row is None:
                raise PlanError("plan not found", code="not_found")
            _ensure_authorized(dict(plan_row), user_id, is_admin)
            step_row = await conn.fetchrow(
                "SELECT * FROM execution_plan_steps WHERE id = $1 AND plan_id = $2",
                step_id,
                plan_id,
            )
            if step_row is None:
                raise PlanError("step not found", code="not_found")
            new_status = status_value if status_value is not None else step_row["status"]
            if status_value is not None:
                _check_step_transition(step_row["status"], status_value)

            # Timestamp transitions
            started_at = step_row["started_at"]
            completed_at = step_row["completed_at"]
            if status_value == "running" and started_at is None:
                started_at = datetime.now(timezone.utc)
            if status_value in STEP_TERMINAL_STATUSES and completed_at is None:
                completed_at = datetime.now(timezone.utc)
                if started_at is None:
                    started_at = completed_at

            updated = await conn.fetchrow(
                """
                UPDATE execution_plan_steps
                SET title = $3, description = $4, assigned_agent = $5,
                    tool_name = $6, status = $7, result = $8,
                    started_at = $9, completed_at = $10
                WHERE id = $1 AND plan_id = $2
                RETURNING id, plan_id, step_number, title, description,
                          assigned_agent, tool_name, status, result,
                          started_at, completed_at, created_at
                """,
                step_id,
                plan_id,
                title if title is not None else step_row["title"],
                description if description is not None else step_row["description"],
                assigned_agent if assigned_agent is not None else step_row["assigned_agent"],
                tool_name if tool_name is not None else step_row["tool_name"],
                new_status,
                result if result is not None else step_row["result"],
                started_at,
                completed_at,
            )
            if status_value is not None:
                await _recompute_current_step(conn, plan_id)
    return dict(updated)


async def complete_step(
    plan_id: uuid.UUID,
    step_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    is_admin: bool,
    result: Optional[dict] = None,
) -> dict:
    return await update_step(
        plan_id,
        step_id,
        user_id=user_id,
        is_admin=is_admin,
        status_value="completed",
        result=result,
    )


async def fail_step(
    plan_id: uuid.UUID,
    step_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    is_admin: bool,
    error_message: Optional[str] = None,
) -> dict:
    payload = {"error": error_message} if error_message else None
    return await update_step(
        plan_id,
        step_id,
        user_id=user_id,
        is_admin=is_admin,
        status_value="failed",
        result=payload,
    )


async def cancel_plan(
    plan_id: uuid.UUID, *, user_id: uuid.UUID, is_admin: bool
) -> dict:
    return await update_plan(
        plan_id,
        user_id=user_id,
        is_admin=is_admin,
        status_value="cancelled",
    )


async def complete_plan(
    plan_id: uuid.UUID, *, user_id: uuid.UUID, is_admin: bool
) -> dict:
    if clients.db_pool is None:
        raise PlanError("Postgres pool unavailable", code="unavailable")
    async with clients.db_pool.acquire() as conn:
        async with conn.transaction():
            plan_row = await conn.fetchrow(
                "SELECT * FROM execution_plans WHERE id = $1 FOR UPDATE",
                plan_id,
            )
            if plan_row is None:
                raise PlanError("plan not found", code="not_found")
            _ensure_authorized(dict(plan_row), user_id, is_admin)
            _check_plan_transition(plan_row["status"], "completed")
            # Validate every step is in a terminal state.
            outstanding = await conn.fetchval(
                """
                SELECT COUNT(*) FROM execution_plan_steps
                WHERE plan_id = $1 AND status NOT IN
                    ('completed','failed','skipped')
                """,
                plan_id,
            )
            if outstanding and int(outstanding) > 0:
                raise PlanError(
                    f"cannot complete plan: {outstanding} step(s) still "
                    "pending/running",
                    code="steps_outstanding",
                )
            updated = await conn.fetchrow(
                """
                UPDATE execution_plans
                SET status = 'completed', updated_at = NOW()
                WHERE id = $1
                RETURNING id, session_id, user_id, title, goal, status,
                          current_step, total_steps, selected_agent,
                          created_at, updated_at
                """,
                plan_id,
            )
    return dict(updated)


async def list_plans_for_session(
    session_id: uuid.UUID,
    *,
    user_id: Optional[uuid.UUID],
    is_admin: bool,
) -> list[dict]:
    if clients.db_pool is None:
        return []
    async with clients.db_pool.acquire() as conn:
        if is_admin:
            rows = await conn.fetch(
                """
                SELECT id, session_id, user_id, title, goal, status,
                       current_step, total_steps, selected_agent,
                       created_at, updated_at
                FROM execution_plans
                WHERE session_id = $1
                ORDER BY created_at ASC
                """,
                session_id,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, session_id, user_id, title, goal, status,
                       current_step, total_steps, selected_agent,
                       created_at, updated_at
                FROM execution_plans
                WHERE session_id = $1 AND user_id = $2
                ORDER BY created_at ASC
                """,
                session_id,
                user_id,
            )
    return [dict(r) for r in rows]
