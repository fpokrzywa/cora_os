"""Runtime trace writer. One row per high-level chat turn or tool/MCP call.

Distinct from `tool_execution_logs` (which is governance audit) — runtime_traces
is the higher-level "what did Cora do this turn" record, designed for the
admin Trace Viewer UI.

Writes are best-effort: a failure to record never breaks the user-facing path.
"""

import logging
import uuid
from typing import Optional

from app.clients import clients

logger = logging.getLogger(__name__)


async def write_trace(
    *,
    session_id: Optional[uuid.UUID],
    user_id: Optional[uuid.UUID],
    trace_type: str,
    status: str,
    selected_agent: Optional[str] = None,
    user_message: Optional[str] = None,
    memory_count: int = 0,
    memory_ids: Optional[list[uuid.UUID]] = None,
    tool_name: Optional[str] = None,
    tool_result: Optional[dict] = None,
    mcp_server_name: Optional[str] = None,
    mcp_action_name: Optional[str] = None,
    model_name: Optional[str] = None,
    model_endpoint: Optional[str] = None,
    duration_ms: Optional[int] = None,
    error_message: Optional[str] = None,
    workspace_id: Optional[uuid.UUID] = None,
    metadata: Optional[dict] = None,
) -> None:
    if clients.db_pool is None:
        logger.warning(
            "runtime trace skipped: pool unavailable trace_type=%s status=%s",
            trace_type,
            status,
        )
        return
    try:
        async with clients.db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO runtime_traces (
                    session_id, user_id, trace_type, selected_agent,
                    user_message, memory_count, memory_ids, tool_name,
                    tool_result, mcp_server_name, mcp_action_name, model_name,
                    model_endpoint, duration_ms, status, error_message,
                    workspace_id, metadata
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                        $13, $14, $15, $16, $17, $18)
                """,
                session_id,
                user_id,
                trace_type,
                selected_agent,
                user_message,
                memory_count,
                memory_ids or [],
                tool_name,
                tool_result,
                mcp_server_name,
                mcp_action_name,
                model_name,
                model_endpoint,
                duration_ms,
                status,
                error_message,
                workspace_id,
                metadata or {},
            )
    except Exception:
        logger.exception(
            "runtime trace write failed: trace_type=%s status=%s",
            trace_type,
            status,
        )
        return

    log_fn = logger.info if status in ("ok", "confirmation_required") else logger.warning
    log_fn(
        "trace: type=%s status=%s session=%s user=%s agent=%s tool=%s "
        "model=%s duration_ms=%s memory_count=%s",
        trace_type,
        status,
        session_id,
        user_id,
        selected_agent,
        tool_name,
        model_name,
        duration_ms,
        memory_count,
    )
