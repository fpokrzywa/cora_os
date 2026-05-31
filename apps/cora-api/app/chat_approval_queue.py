"""Chat-Native Approval Queue Management v2.2.

Manage the email approval queue conversationally — "What emails need my approval?",
"Show my pending drafts", "Open item 2", "Approve the first one", "Reject the latest
draft", "Show pending Gmail intents", "Prepare all approved drafts for simulation".

Renders numbered queues, resolves follow-up selection by number/ordinal against the
stored queue context, and reuses the v1.9 lifecycle handlers + v2.1 simulation to
act — all governed, owner/workspace-scoped, and NON-executing. Never sends email,
never calls a provider API, never exposes tokens.
"""

import logging
import re
import uuid
from typing import Optional

from app.clients import clients
from app import chat_email_lifecycle as cel
from app import chat_provider_simulation as cps
from app import integration_readiness as ir
from app import signal_tools
from app.runtime_traces import write_trace

logger = logging.getLogger(__name__)

SIGNAL = "SIGNAL"
TRACE_REQUESTED = "chat_approval_queue_requested"
TRACE_SELECTED = "chat_approval_queue_item_selected"
TRACE_APPROVED = "chat_queue_draft_approved"
TRACE_REJECTED = "chat_queue_draft_rejected"
TRACE_PREPARED = "chat_queue_intent_prepared"

# Drafts that still need a decision (not approved/archived/rejected).
_PENDING_DRAFT = ("draft", "in_review", "changes_requested", "reviewed")
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}


def _trunc(v, n=60):
    s = "" if v is None else str(v)
    return s if len(s) <= n else s[:n] + "…"


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def _selector(m: str):
    mt = re.search(r"(?:item|number|no\.?|#)\s*#?(\d+)", m)
    if mt:
        return int(mt.group(1))
    for w, n in _ORDINALS.items():
        if f"the {w}" in m or f"{w} one" in m or f"{w} draft" in m or f"{w} intent" in m:
            return n
    if "latest" in m or "last one" in m or "the last" in m:
        return "last"
    return None


def _provider(m: str):
    if "gmail" in m or "google" in m:
        return "gmail"
    if "outlook" in m or "microsoft" in m:
        return "outlook_mail"
    return None


def detect_queue_command(message: str) -> Optional[tuple[str, object, Optional[str]]]:
    """Return (kind, selector, provider) or None. kind ∈ {list_drafts, list_intents,
    open, approve, reject, archive, prepare, simulate, prepare_all}."""
    m = (message or "").lower().strip()
    if not m:
        return None

    if "prepare all" in m and "approved" in m and "draft" in m:
        return ("prepare_all", None, _provider(m) or "gmail")

    # List requests.
    if (("pending" in m or "need" in m or "queue" in m or "waiting" in m)
            and "intent" in m) or ("pending" in m and _provider(m) and "intent" in m):
        return ("list_intents", None, _provider(m))
    if (("pending" in m and "draft" in m)
            or ("need" in m and "approval" in m)
            or ("approval queue" in m)
            or ("what emails need" in m)
            or ("show" in m and "pending" in m and "draft" in m)
            or ("my pending drafts" in m)):
        return ("list_drafts", None, None)

    sel = _selector(m)
    if sel is None:
        return None  # no numbered selection → let other chat handlers run
    if "approve" in m:
        return ("approve", sel, None)
    if "reject" in m or "decline" in m:
        return ("reject", sel, None)
    if "archive" in m:
        return ("archive", sel, None)
    if "prepare" in m:
        return ("prepare", sel, _provider(m) or "gmail")
    if "simulate" in m or "inspect" in m or "preview" in m:
        return ("simulate", sel, None)
    if "open" in m or "show" in m or "select" in m:
        return ("open", sel, None)
    return None


# --------------------------------------------------------------------------- #
# Queue context (last_queue JSONB on chat_email_context)
# --------------------------------------------------------------------------- #

async def _set_queue(session_id, items: list[dict]):
    pool = clients.db_pool
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO chat_email_context (session_id, last_queue) VALUES ($1, $2) "
            "ON CONFLICT (session_id) DO UPDATE SET last_queue = $2, updated_at = NOW()",
            session_id, items)


async def _get_queue(session_id) -> list[dict]:
    pool = clients.db_pool
    async with pool.acquire() as conn:
        row = await conn.fetchval(
            "SELECT last_queue FROM chat_email_context WHERE session_id = $1", session_id)
    return list(row) if row else []


def _pick(items: list[dict], selector) -> Optional[dict]:
    if not items:
        return None
    if selector == "last":
        return items[-1]
    if isinstance(selector, int) and 1 <= selector <= len(items):
        return items[selector - 1]
    return None


async def _trace(session_id, user_id, workspace_id, *, trace_type, status="ok", result=None):
    await write_trace(
        session_id=session_id, user_id=user_id, trace_type=trace_type, status=status,
        selected_agent=SIGNAL, tool_name="chat_approval_queue",
        tool_result=result or {}, workspace_id=workspace_id)


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #

async def handle_queue_command(
    cmd: tuple[str, object, Optional[str]], *, message: str, session_uuid: uuid.UUID,
    user_id: uuid.UUID, workspace_uuid: Optional[uuid.UUID], scope_type: str,
    is_admin: bool,
) -> tuple[bool, Optional[str]]:
    kind, selector, provider = cmd
    kw = dict(session_uuid=session_uuid, user_id=user_id, workspace_uuid=workspace_uuid,
              scope_type=scope_type, is_admin=is_admin)

    if kind == "list_drafts":
        return True, await _list_drafts(**kw)
    if kind == "list_intents":
        return True, await _list_intents(provider=provider, **kw)
    if kind == "prepare_all":
        return True, await _prepare_all(provider=provider, **kw)

    # Selection / action — resolve the target from the stored queue, else a fresh list.
    item_type = "intent" if kind == "simulate" else "draft"
    item = await _resolve(session_uuid, selector, item_type, user_id=user_id,
                          workspace_uuid=workspace_uuid, scope_type=scope_type,
                          is_admin=is_admin)
    if item is None:
        return True, ("I couldn't find that item. Try \"Show my pending drafts\" "
                      "(or \"Show pending Gmail intents\") to see the numbered list first.")
    await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_SELECTED,
                 result={"kind": kind, "item_type": item_type, "id": item["id"]})

    if item_type == "draft":
        draft = cel._visible(await signal_tools.get_draft(uuid.UUID(item["id"])),
                             user_id=user_id, workspace_uuid=workspace_uuid, is_admin=is_admin)
        if draft is None:
            return True, "That draft is no longer available."
        await cel.set_context(session_uuid, current_active_draft_id=draft["id"])
        hkw = dict(session_uuid=session_uuid, user_id=user_id, workspace_uuid=workspace_uuid,
                   scope_type=scope_type, is_admin=is_admin)
        if kind == "open":
            await _trace(session_uuid, user_id, workspace_uuid,
                         trace_type="chat_email_draft_shown",
                         result={"draft_id": str(draft["id"])})
            return True, cel.format_draft(draft)
        if kind == "approve":
            txt = await cel._h_approve(draft, **hkw)
            await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_APPROVED,
                         result={"draft_id": str(draft["id"])})
            return True, txt
        if kind == "reject":
            txt = await cel._h_reject(draft, **hkw)
            await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_REJECTED,
                         result={"draft_id": str(draft["id"])})
            return True, txt
        if kind == "archive":
            return True, await cel._h_archive(draft, **hkw)
        if kind == "prepare":
            txt = await cel._h_prepare(draft, provider=provider, **hkw)
            await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_PREPARED,
                         result={"draft_id": str(draft["id"]), "provider": provider})
            return True, txt

    # intent selection → simulate via v2.1
    intent = await ir.get_intent(uuid.UUID(item["id"]))
    if intent is None or (not is_admin and intent["created_by"] != user_id):
        return True, "That intent is no longer available."
    await cel.set_context(session_uuid, last_integration_intent_id=intent["id"])
    return await cps.handle_simulation_command(
        ("inspect", None), message=message, session_uuid=session_uuid, user_id=user_id,
        workspace_uuid=workspace_uuid, is_admin=is_admin)


# --------------------------------------------------------------------------- #
# Listing + resolution
# --------------------------------------------------------------------------- #

async def _fetch_pending_drafts(*, user_id, workspace_uuid, is_admin):
    # The chat queue is a PERSONAL "my queue" — owner-scoped even for admins (the
    # broad cross-user view is the Admin Console → Approval Console). Keeps numbered
    # selection deterministic and matches the "my pending drafts" phrasing.
    rows = await signal_tools.list_drafts(workspace_id=workspace_uuid, owner_id=user_id)
    return [r for r in rows if r["draft_type"] == "email" and r["status"] in _PENDING_DRAFT]


async def _fetch_pending_intents(*, user_id, workspace_uuid, is_admin, provider=None):
    rows = await ir.list_intents(workspace_id=None, owner_id=user_id)
    rows = [r for r in rows
            if (r.get("metadata") or {}).get("workflow") == ir.RQ_WORKFLOW_TAG
            and r["status"] != "cancelled"]
    if provider:
        rows = [r for r in rows
                if r.get("provider_name") == provider
                or (r.get("metadata") or {}).get("selected_provider") == provider]
    return rows


async def _list_drafts(*, session_uuid, user_id, workspace_uuid, scope_type, is_admin):
    drafts = await _fetch_pending_drafts(user_id=user_id, workspace_uuid=workspace_uuid,
                                         is_admin=is_admin)
    queue = [{"n": i + 1, "type": "draft", "id": str(d["id"])} for i, d in enumerate(drafts)]
    await _set_queue(session_uuid, queue)
    await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_REQUESTED,
                 result={"kind": "drafts", "count": len(drafts)})
    if not drafts:
        return "You have no email drafts awaiting approval."
    lines = [f"**Emails needing approval** ({len(drafts)})"]
    for i, d in enumerate(drafts, 1):
        lines.append(f"{i}. `{str(d['id'])[:8]}` · {d['status']} · "
                     f"{_trunc(d.get('subject') or d.get('title'))}")
    lines.append("\n_Reply: \"open item N\", \"approve item N\", \"reject the latest draft\"._")
    return "\n".join(lines)


async def _list_intents(*, provider, session_uuid, user_id, workspace_uuid, scope_type, is_admin):
    intents = await _fetch_pending_intents(user_id=user_id, workspace_uuid=workspace_uuid,
                                           is_admin=is_admin, provider=provider)
    queue = [{"n": i + 1, "type": "intent", "id": str(it["id"])} for i, it in enumerate(intents)]
    await _set_queue(session_uuid, queue)
    await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_REQUESTED,
                 result={"kind": "intents", "provider": provider, "count": len(intents)})
    label = (provider + " ") if provider else ""
    if not intents:
        return f"You have no pending {label}provider intents."
    lines = [f"**Pending {label}provider intents** ({len(intents)})"]
    for i, it in enumerate(intents, 1):
        prov = (it.get("metadata") or {}).get("selected_provider") or it.get("provider_name")
        lines.append(f"{i}. `{str(it['id'])[:8]}` · {prov} · {it['action_type']} · {it['status']}")
    lines.append("\n_Reply: \"simulate item N\"._")
    return "\n".join(lines)


async def _resolve(session_uuid, selector, item_type, *, user_id, workspace_uuid,
                   scope_type, is_admin):
    queue = [q for q in await _get_queue(session_uuid) if q.get("type") == item_type]
    if not queue:
        if item_type == "draft":
            rows = await _fetch_pending_drafts(user_id=user_id, workspace_uuid=workspace_uuid,
                                               is_admin=is_admin)
        else:
            rows = await _fetch_pending_intents(user_id=user_id, workspace_uuid=workspace_uuid,
                                                is_admin=is_admin)
        queue = [{"n": i + 1, "type": item_type, "id": str(r["id"])} for i, r in enumerate(rows)]
    return _pick(queue, selector)


async def _prepare_all(*, provider, session_uuid, user_id, workspace_uuid, scope_type, is_admin):
    rows = await signal_tools.list_drafts(workspace_id=workspace_uuid, owner_id=user_id)
    approved = [r for r in rows if r["draft_type"] == "email" and r["status"] == "approved"]
    if not approved:
        return "You have no approved drafts to prepare. Approve a draft first."
    prepared = 0
    for d in approved:
        try:
            await cel._h_prepare(d, provider=provider, session_uuid=session_uuid,
                                 user_id=user_id, workspace_uuid=workspace_uuid,
                                 scope_type=scope_type, is_admin=is_admin)
            prepared += 1
        except Exception:
            logger.exception("prepare_all failed for draft %s", d["id"])
    await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_PREPARED,
                 result={"bulk": True, "provider": provider, "prepared": prepared})
    return (f"✓ Prepared {prepared} dry-run **{provider}** intent(s) from your approved "
            "drafts (nothing sent). Say \"Show pending Gmail intents\" then \"simulate item N\".")
