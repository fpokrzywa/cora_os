"""Phase 1 agent kernel — the reason→act→observe loop.

The single-agent runtime that lets the MODEL choose tools, instead of the
deterministic regex dispatch in app.routers.chat. It calls the DGX chat
endpoint (Ollama /api/chat) with a tool catalog, executes any tool calls the
model emits through the existing governance + dispatch layer, feeds the
observations back, and loops until the model returns a final answer or the
step budget is exhausted.

Phase 1 scope — deliberately small and fail-closed:
  - READ-ONLY tools only (curated allowlist below). A tool can only be called
    if it is in READ_ONLY_TOOLS, and a defensive re-check at dispatch refuses
    anything requiring confirmation, high-risk, or governance-denied. The
    write / confirm / external paths are structurally out of reach.
  - Gated behind settings.agent_runtime_enabled (default False).
  - Orchestrator-level dispatch (agent_name=None): unrestricted-but-curated.
    Phase 2 delegation will run sub-agents under their real agent_name so the
    tools.allowed_agents scoping applies per domain.

Reused as-is: app.tools.dispatch_tool, app.tools.governance.check_permission,
the tools table, and httpx against the DGX endpoint.
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from app.agents.delegations import (
    DelegationError,
    complete_delegation,
    create_delegation,
    fail_delegation,
)
from app import chronos_tools, signal_tools
from app.clients import clients
from app.config import settings
from app.tools import dispatch_tool
from app.tools.governance import check_permission

logger = logging.getLogger(__name__)

# Curated READ-ONLY catalog. name -> model-facing function schema. The DB row
# (fetched at dispatch) supplies type + governance fields; this map supplies the
# parameters advertised to the model (the seeded rows carry no input_schema).
# Adding a tool here is the ONLY way it becomes callable in Phase 1.
READ_ONLY_TOOLS: dict[str, dict] = {
    "web_search": {
        "description": "Live web search via the internal SearXNG engine. "
        "Returns ranked result snippets to ground an answer.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for."},
                "max_results": {
                    "type": "integer",
                    "description": "Max results to return (default 6).",
                },
            },
            "required": ["query"],
        },
    },
    "filesystem_list_project": {
        "description": "List directory contents of the project via the "
        "filesystem MCP server. Read-only.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path (optional; defaults to project root).",
                },
            },
        },
    },
    "filesystem_read_file": {
        "description": "Read a single file via the filesystem MCP server. Read-only.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read."},
            },
            "required": ["path"],
        },
    },
}

# Curated STAGING catalog (Phase 5). REVIEW-ONLY internal_action tools: they
# create a draft/proposal record and nothing else — no email is sent, no
# calendar is written. Names match the seeded tool rows 1:1 so check_permission
# and the audit log line up. Reachable only when settings.agent_write_enabled.
STAGING_TOOLS: dict[str, dict] = {
    "signal_create_draft": {
        "description": "Stage an email/message DRAFT for the user to review and "
        "send later. Creates a review-only draft — it does NOT send anything.",
        "parameters": {
            "type": "object",
            "properties": {
                "body": {"type": "string", "description": "The draft body."},
                "subject": {"type": "string", "description": "Subject line."},
                "recipient": {"type": "string", "description": "Intended recipient (hint)."},
                "title": {"type": "string", "description": "Short title for the draft."},
            },
            "required": ["body"],
        },
    },
    "chronos_create_schedule_proposal": {
        "description": "Stage a schedule/meeting PROPOSAL for the user to review. "
        "Creates a review-only proposal — it does NOT create any calendar event.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Title of the proposal."},
                "description": {"type": "string", "description": "Details / plan."},
                "proposal_type": {
                    "type": "string",
                    "enum": ["meeting", "timeline", "reminder"],
                    "description": "Kind of proposal (default meeting).",
                },
            },
            "required": ["title"],
        },
    },
}

AGENT_SYSTEM_PROMPT = (
    "You are Cora, an AI assistant and AI operating system. You can call "
    "read-only tools to gather information before answering. Use a tool only "
    "when it materially helps; otherwise answer directly. When you have enough "
    "information, give a concise, direct final answer with no tool call. Never "
    "claim to have taken an action you did not take via a tool result."
)


@dataclass
class AgentStep:
    kind: str  # "tool_call" | "tool_result" | "final" | "error"
    detail: dict = field(default_factory=dict)


@dataclass
class AgentResult:
    answer: str
    steps: list[AgentStep]
    tool_calls: int
    model: str
    stopped: str  # "final" | "budget" | "error"
    run_id: Optional[str] = None


async def _fetch_tool_row(name: str) -> Optional[dict]:
    if clients.db_pool is None:
        return None
    async with clients.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, name, description, type, endpoint, enabled,
                   requires_confirmation, mcp_server_name, mcp_action_name,
                   input_schema, output_schema, risk_level, allowed_agents
            FROM tools WHERE name = $1
            """,
            name,
        )
    return dict(row) if row else None


# ---------- Durable run state (best-effort; the loop never fails on a DB error) ----------


def _as_uuid(value: Any) -> Optional[uuid.UUID]:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str) and value:
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


def _steps_to_json(steps: list["AgentStep"]) -> list[dict]:
    return [{"kind": s.kind, **s.detail} for s in steps]


async def _create_run(
    *, session_id, user_id, workspace_id, agent_name, goal, model, max_steps
) -> Optional[uuid.UUID]:
    if clients.db_pool is None:
        return None
    try:
        async with clients.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO agent_runtime_runs
                    (session_id, user_id, workspace_id, agent_name, status,
                     goal, model_name, max_steps)
                VALUES ($1, $2, $3, $4, 'running', $5, $6, $7)
                RETURNING id
                """,
                _as_uuid(session_id), _as_uuid(user_id), _as_uuid(workspace_id),
                agent_name, goal, model, max_steps,
            )
        return row["id"]
    except Exception:
        logger.exception("agent run create failed (continuing without persistence)")
        return None


async def create_pending_run(
    *, goal, user_id, session_id=None, workspace_id=None,
    agent_name: Optional[str] = None, max_steps: Optional[int] = None,
) -> Optional[uuid.UUID]:
    """Insert a 'pending' run row (Phase 3 async submission) and return its id.
    The worker later binds run_agent() to this id to execute it off-request."""
    if clients.db_pool is None:
        return None
    budget = max_steps or settings.agent_runtime_max_steps
    try:
        async with clients.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO agent_runtime_runs
                    (session_id, user_id, workspace_id, agent_name, status,
                     goal, max_steps)
                VALUES ($1, $2, $3, $4, 'pending', $5, $6)
                RETURNING id
                """,
                _as_uuid(session_id), _as_uuid(user_id), _as_uuid(workspace_id),
                agent_name, goal, budget,
            )
        return row["id"]
    except Exception:
        logger.exception("agent pending-run create failed")
        return None


async def _mark_running(run_id: Optional[uuid.UUID], model: str) -> None:
    if run_id is None or clients.db_pool is None:
        return
    try:
        async with clients.db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_runtime_runs SET status = 'running', "
                "model_name = $2, updated_at = NOW() WHERE id = $1",
                run_id, model,
            )
    except Exception:
        logger.exception("agent run mark-running failed (continuing)")


async def _update_run(
    run_id: Optional[uuid.UUID], *, messages, steps, tool_calls, step_count
) -> None:
    if run_id is None or clients.db_pool is None:
        return
    try:
        async with clients.db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE agent_runtime_runs
                SET messages = $2, steps = $3, tool_calls = $4,
                    step_count = $5, updated_at = NOW()
                WHERE id = $1
                """,
                run_id, messages, _steps_to_json(steps), tool_calls, step_count,
            )
    except Exception:
        logger.exception("agent run update failed (continuing)")


async def _finalize_run(
    run_id: Optional[uuid.UUID], *, status, answer, stopped, tool_calls,
    step_count, messages, steps, error,
) -> None:
    if run_id is None or clients.db_pool is None:
        return
    try:
        async with clients.db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE agent_runtime_runs
                SET status = $2, answer = $3, stopped = $4, tool_calls = $5,
                    step_count = $6, messages = $7, steps = $8,
                    error_message = $9, updated_at = NOW(), completed_at = NOW()
                WHERE id = $1
                """,
                run_id, status, answer, stopped, tool_calls, step_count,
                messages, _steps_to_json(steps), error,
            )
    except Exception:
        logger.exception("agent run finalize failed (continuing)")


async def get_run(run_id: uuid.UUID, *, user_id) -> Optional[dict]:
    """Owner-scoped read of a persisted run (legacy NULL owner allowed)."""
    if clients.db_pool is None:
        return None
    async with clients.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, session_id, user_id, agent_name, status, goal, model_name,
                   answer, tool_calls, step_count, max_steps, stopped,
                   error_message, steps, created_at, updated_at, completed_at
            FROM agent_runtime_runs
            WHERE id = $1 AND (user_id = $2 OR user_id IS NULL)
            """,
            run_id, _as_uuid(user_id),
        )
    return dict(row) if row else None


async def list_runs(*, user_id, limit: int = 50) -> list[dict]:
    """Owner-scoped recent runs (summary columns only — no messages/steps blobs,
    which keeps the list light). Legacy NULL-owner rows are included, matching
    get_run's policy. Orchestrator runs have agent_name NULL; spoke runs carry
    their specialist name."""
    if clients.db_pool is None:
        return []
    async with clients.db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, session_id, agent_name, status, goal, model_name,
                   tool_calls, step_count, max_steps, stopped,
                   created_at, updated_at, completed_at
            FROM agent_runtime_runs
            WHERE user_id = $1 OR user_id IS NULL
            ORDER BY created_at DESC
            LIMIT $2
            """,
            _as_uuid(user_id), max(1, min(limit, 200)),
        )
    return [dict(r) for r in rows]


async def _spoke_run_summary(run_id: uuid.UUID) -> Optional[dict]:
    """Lightweight view of a spoke's own run (its step trace included) for the
    orchestrator→spoke tree."""
    if clients.db_pool is None:
        return None
    async with clients.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, agent_name, status, stopped, answer,
                   tool_calls, step_count, steps
            FROM agent_runtime_runs WHERE id = $1
            """,
            run_id,
        )
    return dict(row) if row else None


async def get_run_delegations(parent_run_id: uuid.UUID) -> list[dict]:
    """The spoke hops this orchestrator run spawned, each with the spoke's own
    run (incl. its step trace) embedded — the orchestrator→spoke tree. Correlated
    via the _parent_run_id stamped into each delegation's input_payload, so it
    works even for sessionless runs (e.g. the Cora Configuration panel). The
    caller is responsible for having owner-checked parent_run_id first."""
    if clients.db_pool is None:
        return []
    async with clients.db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, from_agent, to_agent, delegation_reason, status,
                   output_payload, created_at, completed_at
            FROM agent_delegations
            WHERE input_payload->>'_parent_run_id' = $1
            ORDER BY created_at ASC
            """,
            str(parent_run_id),
        )
    out: list[dict] = []
    for r in rows:
        deleg = dict(r)
        spoke_rid = _as_uuid((deleg.get("output_payload") or {}).get("run_id"))
        deleg["spoke_run"] = await _spoke_run_summary(spoke_rid) if spoke_rid else None
        out.append(deleg)
    return out


async def _build_catalog(
    agent_name: Optional[str], *, include_staging: bool = False
) -> list[dict]:
    """Ollama /api/chat 'tools' array visible to this identity. The orchestrator
    (agent_name=None) sees the whole curated set; a spoke sees only tools whose
    tools.allowed_agents includes it — domain isolation, sourced from the tools
    table. Staging tools (review-only) are included only when include_staging."""
    specs = dict(READ_ONLY_TOOLS)
    if include_staging:
        specs.update(STAGING_TOOLS)
    catalog: list[dict] = []
    for name, spec in specs.items():
        if agent_name is not None:
            row = await _fetch_tool_row(name)
            allowed = list((row or {}).get("allowed_agents") or [])
            if allowed and agent_name not in allowed:
                continue
        catalog.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": spec["description"],
                    "parameters": spec["parameters"],
                },
            }
        )
    return catalog


def _result_to_text(result: Any) -> str:
    if isinstance(result, str):
        return result[:4000]
    try:
        return json.dumps(result, default=str)[:4000]
    except (TypeError, ValueError):
        return str(result)[:4000]


def _parse_args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


async def _chat(endpoint: str, model: str, messages: list[dict], tools: list[dict]) -> dict:
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{endpoint}/api/chat",
            json={
                "model": model,
                "messages": messages,
                "tools": tools,
                "stream": False,
            },
        )
        resp.raise_for_status()
        return resp.json()


async def _dispatch_read_only(
    name: str, args: dict, *, agent_name: Optional[str], user_id, session_id: Optional[str]
) -> str:
    """Resolve, govern, and run one read-only tool call. Returns observation
    text. Never raises — failures come back as text the model can react to.
    Governance runs under agent_name, so a spoke is scoped to its allowed_agents
    (the orchestrator passes None = unrestricted-but-curated)."""
    if name not in READ_ONLY_TOOLS:
        return f"error: tool {name!r} is not available in this read-only session."
    tool = await _fetch_tool_row(name)
    if tool is None:
        return f"error: tool {name!r} not found."

    # Defensive read-only floor — independent of the catalog allowlist.
    if tool.get("requires_confirmation") or tool.get("risk_level") == "high":
        return f"error: tool {name!r} is not permitted in a read-only session."

    decision = await check_permission(
        tool, agent_name=agent_name, user_id=user_id, is_admin=False
    )
    if not decision.allowed:
        return f"error: tool {name!r} denied by governance ({decision.reason})."

    payload = {
        "session_id": session_id,
        "user_message": None,
        "arguments": args,  # web_search runner reads payload['arguments']
        "metadata": args,   # mcp_action runner reads payload['metadata']
    }
    started = time.perf_counter()
    try:
        result = await dispatch_tool(tool, payload)
    except Exception as exc:  # tool/runner failure — report, don't crash the loop
        logger.warning("agent tool %s failed: %s", name, exc)
        return f"error: tool {name!r} failed: {exc}"
    logger.info(
        "agent tool %s ok in %sms", name, int((time.perf_counter() - started) * 1000)
    )
    return _result_to_text(result)


# ---------- Phase 2 hub-and-spoke delegation ----------
# Only the orchestrator run carries delegate_to. Spokes never do (it is absent
# from their catalog), so the topology is always ATLAS -> spoke -> ATLAS. The
# create_delegation governance (no self-delegation, depth cap) and the explicit
# depth guard below bound it; spokes run with their own scoped read-only catalog.

ORCHESTRATOR_NAME = "ATLAS"


async def _load_spokes() -> dict[str, dict]:
    """Delegatable specialists from the agent registry: enabled subagents with
    an active version. name -> {description, system_prompt, model_name}."""
    if clients.db_pool is None:
        return {}
    try:
        async with clients.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT a.name, a.description, v.system_prompt, v.model_name
                FROM agents a
                JOIN agent_versions v ON v.id = a.current_version_id
                WHERE a.enabled = TRUE
                  AND a.agent_type = 'subagent'
                  AND v.status = 'active'
                """
            )
    except Exception:
        logger.exception("load spokes failed (delegation disabled this run)")
        return {}
    return {
        r["name"]: {
            "description": r["description"],
            "system_prompt": r["system_prompt"],
            "model_name": r["model_name"],
        }
        for r in rows
    }


def _delegate_tool_schema(spoke_names: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "delegate_to",
            "description": "Hand a self-contained subtask to a specialist agent "
            "and get its result back. Use when the task is squarely in one "
            "specialist's domain. Pass only the facts the specialist needs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "enum": spoke_names,
                        "description": "Which specialist handles this subtask.",
                    },
                    "goal": {"type": "string", "description": "The subtask to accomplish."},
                    "facts": {
                        "type": "object",
                        "description": "Only the facts the specialist needs (no full transcript).",
                    },
                    "constraints": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Constraints the specialist must respect.",
                    },
                    "expected": {
                        "type": "string",
                        "description": "What output you expect back.",
                    },
                },
                "required": ["agent", "goal"],
            },
        },
    }


def _render_task(args: dict) -> str:
    """Minimal-context task string for a spoke — input_payload only, never the
    orchestrator's thread."""
    parts = [f"Task: {(args.get('goal') or '').strip()}"]
    if args.get("facts"):
        parts.append("Facts: " + json.dumps(args["facts"], default=str))
    if args.get("constraints"):
        parts.append("Constraints: " + "; ".join(str(c) for c in args["constraints"]))
    if args.get("expected"):
        parts.append("Expected output: " + str(args["expected"]))
    return "\n".join(parts)


async def _handle_delegation(
    args: dict, *, spokes: dict, user_id, session_id, workspace_id, depth: int,
    parent_run_id: Optional[uuid.UUID] = None,
) -> str:
    """Run one ATLAS -> spoke hop inline and return the spoke's answer as the
    observation. Records an agent_delegations row (create -> complete/fail),
    stamping the orchestrator's run id into input_payload so the runs view can
    rebuild the orchestrator→spoke tree."""
    target = (args.get("agent") or "").strip().upper()
    if target not in spokes:
        return f"error: unknown agent {target!r}. Valid: {', '.join(spokes) or '(none)'}."
    if depth >= 1:
        return "error: delegation depth limit reached (hub-and-spoke allows one hop)."

    spoke = spokes[target]
    task = _render_task(args)
    input_payload = (
        {**args, "_parent_run_id": str(parent_run_id)} if parent_run_id else args
    )
    try:
        deleg = await create_delegation(
            from_agent=ORCHESTRATOR_NAME,
            to_agent=target,
            delegation_reason=(args.get("goal") or "")[:300] or None,
            session_id=_as_uuid(session_id),
            workspace_id=_as_uuid(workspace_id),
            input_payload=input_payload,
            user_id=_as_uuid(user_id),
            initial_status="running",
        )
    except DelegationError as exc:
        return f"error: delegation rejected ({exc})."

    deleg_id = deleg["id"]
    try:
        spoke_result = await run_agent(
            task,
            user_id=user_id,
            session_id=session_id,
            workspace_id=workspace_id,
            agent_name=target,
            system_prompt=spoke["system_prompt"] or AGENT_SYSTEM_PROMPT,
            _depth=depth + 1,
        )
    except Exception as exc:  # never let a spoke crash the orchestrator loop
        logger.exception("spoke %s crashed", target)
        try:
            await fail_delegation(deleg_id, error_message=str(exc), user_id=_as_uuid(user_id))
        except Exception:
            logger.exception("delegation fail-close failed: id=%s", deleg_id)
        return f"error: specialist {target} failed: {exc}"

    try:
        await complete_delegation(
            deleg_id,
            output_payload={
                "answer": spoke_result.answer,
                "run_id": spoke_result.run_id,
                "stopped": spoke_result.stopped,
            },
            user_id=_as_uuid(user_id),
        )
    except Exception:
        logger.exception("delegation complete failed: id=%s", deleg_id)
    return f"[{target} responded] {spoke_result.answer}"


async def _handle_staging(
    name: str, args: dict, *, user_id, workspace_id, session_id, agent_name
) -> str:
    """Stage a REVIEW-ONLY artifact (draft/proposal). Creates a record and
    nothing else — never sends email or writes a calendar. Governed by
    check_permission; a hard floor refuses anything that isn't an
    internal_action tool. Returns observation text; never raises."""
    if name not in STAGING_TOOLS:
        return f"error: tool {name!r} is not a staging action."
    tool = await _fetch_tool_row(name)
    if tool is None:
        return f"error: tool {name!r} not found."
    # Hard safety floor: staging is ONLY ever a review-only internal_action.
    if tool.get("type") != "internal_action":
        return f"error: tool {name!r} is not a review-only staging action."
    decision = await check_permission(
        tool, agent_name=agent_name, user_id=user_id, is_admin=False
    )
    if not decision.allowed:
        return f"error: tool {name!r} denied by governance ({decision.reason})."

    ws = _as_uuid(workspace_id)
    uid = _as_uuid(user_id)
    meta = {"source": "agent", "session_id": str(session_id) if session_id else None}
    try:
        if name == "signal_create_draft":
            body = (args.get("body") or "").strip()
            if not body:
                return "error: a draft needs a body."
            row = await signal_tools.create_communication_draft(
                workspace_id=ws, user_id=uid, draft_type="email",
                title=args.get("title") or args.get("subject") or "Draft",
                subject=args.get("subject"), body=body,
                recipient_hint=args.get("recipient"), metadata=meta,
            )
            return (
                f"✓ Staged a review-only email draft '{row['title']}' "
                f"(id {str(row['id'])[:8]}, status {row['status']}). It was NOT "
                "sent — the user reviews and sends it from their drafts."
            )
        if name == "chronos_create_schedule_proposal":
            title = (args.get("title") or "").strip()
            if not title:
                return "error: a proposal needs a title."
            row = await chronos_tools.create_schedule_proposal(
                workspace_id=ws, user_id=uid,
                proposal_type=args.get("proposal_type") or "meeting",
                title=title, description=args.get("description"), metadata=meta,
            )
            return (
                f"✓ Staged a review-only schedule proposal '{row['title']}' "
                f"(id {str(row['id'])[:8]}, status {row['status']}). No calendar "
                "event was created — the user reviews it to act."
            )
    except Exception as exc:
        logger.exception("staging %s failed", name)
        return f"error: could not stage {name!r}: {exc}"
    return f"error: no staging handler for {name!r}."


async def _execute_calls(
    turn: list, *, sem, spokes, agent_name, user_id, session_id, workspace_id, depth,
    parent_run_id: Optional[uuid.UUID] = None,
) -> list[str]:
    """Run one turn's tool/delegate calls concurrently (Phase 4), returning
    observations in call order. Delegations are bounded by sem; every call is
    wrapped so one failure surfaces as observation text instead of cancelling
    its siblings."""
    async def run_one(name: str, args: dict, kind: str) -> str:
        try:
            if kind == "delegate":
                async with sem:
                    return await _handle_delegation(
                        args, spokes=spokes, user_id=user_id, session_id=session_id,
                        workspace_id=workspace_id, depth=depth,
                        parent_run_id=parent_run_id,
                    )
            if kind == "staging":
                return await _handle_staging(
                    name, args, user_id=user_id, workspace_id=workspace_id,
                    session_id=session_id, agent_name=agent_name,
                )
            return await _dispatch_read_only(
                name, args, agent_name=agent_name, user_id=user_id, session_id=session_id,
            )
        except Exception as exc:  # defensive: never let one call abort the gather
            logger.exception("turn call %s failed", name)
            return f"error: tool {name!r} failed: {exc}"

    return await asyncio.gather(*(run_one(n, a, k) for (n, a, k) in turn))


async def run_agent(
    message: str,
    *,
    user_id,
    session_id: Optional[str] = None,
    workspace_id=None,
    agent_name: Optional[str] = None,
    is_orchestrator: bool = False,
    system_prompt: str = AGENT_SYSTEM_PROMPT,
    max_steps: Optional[int] = None,
    run_id: Optional[uuid.UUID] = None,
    _depth: int = 0,
) -> AgentResult:
    """Run the read→act→observe loop for one user message. Read-only tools
    only. Persists a durable run row (best-effort) that the loop updates as it
    advances and finalizes on exit. Returns the final answer + step trace.

    When run_id is given, the loop binds to that pre-created 'pending' row
    (Phase 3 worker-driven run) instead of creating a fresh one."""
    endpoint = (settings.dgx_model_endpoint or "").rstrip("/")
    model = settings.dgx_chat_model_name or settings.dgx_model_name
    if not endpoint or not model:
        if run_id is not None:  # don't strand a bound 'pending' row
            await _finalize_run(
                run_id, status="failed", answer="", stopped="error",
                tool_calls=0, step_count=0, messages=[], steps=[],
                error="DGX endpoint/model unset",
            )
        return AgentResult(
            answer="Agent runtime is not configured (DGX endpoint/model unset).",
            steps=[], tool_calls=0, model=model or "<unset>", stopped="error",
            run_id=str(run_id) if run_id else None,
        )

    budget = max_steps or settings.agent_runtime_max_steps
    if run_id is not None:
        await _mark_running(run_id, model)
    else:
        run_id = await _create_run(
            session_id=session_id, user_id=user_id, workspace_id=workspace_id,
            agent_name=agent_name, goal=message, model=model, max_steps=budget,
        )
    # Orchestrator-only: load the spoke roster and expose delegate_to.
    spokes: dict[str, dict] = {}
    delegation_on = (
        is_orchestrator
        and settings.agent_runtime_enabled
        and settings.agent_delegation_enabled
        and _depth == 0
    )
    effective_prompt = system_prompt
    if delegation_on:
        spokes = await _load_spokes()
        if spokes:
            roster = "\n".join(f"- {n}: {s['description']}" for n, s in spokes.items())
            effective_prompt = system_prompt + (
                "\n\nYou can delegate a self-contained subtask to a specialist via "
                "the delegate_to tool, then synthesize its result into your final "
                "answer. Delegate only when the task is squarely in a specialist's "
                "domain, and give it just the facts it needs. Specialists:\n" + roster
            )
        else:
            delegation_on = False

    write_on = settings.agent_runtime_enabled and settings.agent_write_enabled
    catalog = await _build_catalog(agent_name, include_staging=write_on)
    if delegation_on:
        catalog.append(_delegate_tool_schema(list(spokes)))
    delegation_sem = asyncio.Semaphore(max(1, settings.agent_delegation_max_parallel))
    messages: list[dict] = [
        {"role": "system", "content": effective_prompt},
        {"role": "user", "content": message},
    ]
    steps: list[AgentStep] = []
    tool_calls = 0
    step_count = 0
    answer = ""
    stopped = "final"
    error: Optional[str] = None

    try:
        for _ in range(budget):
            step_count += 1
            data = await _chat(endpoint, model, messages, catalog)
            msg = data.get("message") or {}
            raw_calls = msg.get("tool_calls") or []
            # Keep the assistant turn (with any tool_calls) in the running thread.
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.get("content") or "",
                    **({"tool_calls": raw_calls} if raw_calls else {}),
                }
            )

            if not raw_calls:
                answer = (msg.get("content") or "").strip()
                steps.append(AgentStep("final", {"answer": answer}))
                stopped = "final"
                break

            # Classify this turn's calls and record each as a step.
            turn: list[tuple[str, dict, str]] = []
            for call in raw_calls:
                fn = call.get("function") or {}
                name = fn.get("name") or ""
                args = _parse_args(fn.get("arguments"))
                tool_calls += 1
                steps.append(AgentStep("tool_call", {"name": name, "arguments": args}))
                if name == "delegate_to" and delegation_on:
                    kind = "delegate"
                elif name in STAGING_TOOLS and write_on:
                    kind = "staging"
                else:
                    kind = "tool"
                turn.append((name, args, kind))

            # Phase 4: independent read-only tools and multiple spokes run
            # concurrently; observations are stitched back in call order so the
            # model sees a stable tool-result sequence.
            observations = await _execute_calls(
                turn, sem=delegation_sem, spokes=spokes, agent_name=agent_name,
                user_id=user_id, session_id=session_id, workspace_id=workspace_id,
                depth=_depth, parent_run_id=run_id,
            )
            for (name, _args, _kind), observation in zip(turn, observations):
                steps.append(
                    AgentStep("tool_result", {"name": name, "result": observation[:500]})
                )
                messages.append({"role": "tool", "content": observation})

            await _update_run(
                run_id, messages=messages, steps=steps,
                tool_calls=tool_calls, step_count=step_count,
            )
        else:
            # Budget exhausted: one final no-tools pass so gathered work isn't lost.
            data = await _chat(
                endpoint, model,
                messages + [{
                    "role": "user",
                    "content": "Give your best final answer now from what you've "
                    "gathered. Do not call any tools.",
                }],
                [],
            )
            answer = ((data.get("message") or {}).get("content") or "").strip()
            steps.append(AgentStep("final", {"answer": answer, "forced": True}))
            stopped = "budget"
            if not answer:
                answer = "I reached the step limit before finishing — try narrowing the request."
    except httpx.HTTPError as exc:
        logger.exception("agent chat call failed")
        steps.append(AgentStep("error", {"error": str(exc)}))
        answer = "The model call failed; please retry."
        stopped = "error"
        error = str(exc)

    await _finalize_run(
        run_id,
        status="failed" if stopped == "error" else "done",
        answer=answer, stopped=stopped, tool_calls=tool_calls,
        step_count=step_count, messages=messages, steps=steps, error=error,
    )
    return AgentResult(
        answer=answer or "(no answer)",
        steps=steps, tool_calls=tool_calls, model=model, stopped=stopped,
        run_id=str(run_id) if run_id else None,
    )
