"""SIGNAL internal communication-draft service (v0.1).

Persists review-only drafts in `communication_drafts`. There is NO external
send path — drafts are prepared, reviewed, approved, or archived only. A future
governed `internal_action` runner / chat-trigger may call create_communication_draft.
"""

import logging
import re
import uuid
from typing import Optional

from app.clients import clients
from app.review_workflow import DRAFT_STATUSES

logger = logging.getLogger(__name__)

# DRAFT_STATUSES (the full review lifecycle) is defined in review_workflow and
# re-exported here for backward compatibility with callers/tests.

_COLS = (
    "id, workspace_id, created_by, agent_name, draft_type, title, "
    "recipient_hint, subject, body, tone, status, metadata, "
    "reviewed_by, reviewed_at, approved_by, approved_at, archived_at, "
    "review_notes, created_at, updated_at"
)


class SignalToolError(Exception):
    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


def _require_pool():
    if clients.db_pool is None:
        raise SignalToolError("Postgres pool unavailable", code="unavailable")
    return clients.db_pool


# Email sign-off normalization. A draft written for the user must close with the
# user's name (or the Cora fallback), NEVER an internal agent codename like SIGNAL —
# the draft body is the model reply verbatim, so a stray "Best regards, SIGNAL" would
# otherwise ship. Shared by the chat-route SIGNAL path AND the email lifecycle.
SIGNOFF_FALLBACK = "Cora - the AI Assistant"

_EMAIL_CLOSINGS = sorted(
    ("best regards", "kind regards", "warm regards", "warmest regards", "best wishes",
     "many thanks", "thank you", "thanks again", "talk soon", "respectfully",
     "sincerely", "regards", "cheers", "warmly", "thanks", "best"),
    key=len, reverse=True,  # longest-first so 'best regards' wins over 'best'
)
_SIGNOFF_RE = re.compile(
    r"(?is)\n[ \t]*(" + "|".join(re.escape(c) for c in _EMAIL_CLOSINGS)
    + r")[ \t]*,?[ \t]*\n+.*\Z"
)


def normalize_email_signoff(body: str, signoff_name: str) -> str:
    """Force an email body to close with `signoff_name`, never an internal agent
    codename. Replaces the model's signatory block under any common closing, or
    appends a clean closing when none is present. Idempotent."""
    text = (body or "").rstrip()
    if not text:
        return text
    m = _SIGNOFF_RE.search(text)
    if m:
        closing = m.group(1).strip()
        closing = closing[:1].upper() + closing[1:]
        return text[: m.start()].rstrip() + f"\n\n{closing},\n{signoff_name}"
    return text + f"\n\nBest regards,\n{signoff_name}"


async def user_signoff_name(user_id: uuid.UUID) -> str:
    """The user's display name for email sign-offs, or the Cora fallback. Never
    raises — a lookup failure falls back too."""
    if clients.db_pool is not None:
        try:
            async with clients.db_pool.acquire() as conn:
                name = await conn.fetchval(
                    "SELECT display_name FROM users WHERE id = $1", user_id)
            if name and name.strip():
                return name.strip()
        except Exception:
            logger.exception("user signoff-name lookup failed; using fallback")
    return SIGNOFF_FALLBACK


async def create_communication_draft(
    *,
    workspace_id: Optional[uuid.UUID],
    user_id: uuid.UUID,
    draft_type: str,
    title: Optional[str],
    subject: Optional[str],
    body: str,
    recipient_hint: Optional[str] = None,
    tone: Optional[str] = None,
    agent_name: str = "SIGNAL",
    metadata: Optional[dict] = None,
) -> dict:
    if not (draft_type or "").strip():
        raise SignalToolError("draft_type is required")
    if not (body or "").strip():
        raise SignalToolError("body is required")
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO communication_drafts
                (workspace_id, created_by, agent_name, draft_type, title,
                 recipient_hint, subject, body, tone, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            RETURNING {_COLS}
            """,
            workspace_id,
            user_id,
            agent_name,
            draft_type.strip(),
            title,
            recipient_hint,
            subject,
            body,
            tone,
            metadata or {},
        )
    return dict(row)


async def list_drafts(
    *,
    workspace_id: Optional[uuid.UUID],
    include_archived: bool = False,
    owner_id: Optional[uuid.UUID] = None,
) -> list[dict]:
    """List drafts in a workspace. Pass owner_id to restrict to one creator
    (non-admin callers); omit it for the admin workspace-level view."""
    pool = _require_pool()
    sql = (
        f"SELECT {_COLS} FROM communication_drafts "
        "WHERE (workspace_id = $1 OR ($1 IS NULL AND workspace_id IS NULL))"
    )
    args: list = [workspace_id]
    if owner_id is not None:
        args.append(owner_id)
        sql += f" AND created_by = ${len(args)}"
    if not include_archived:
        sql += " AND status <> 'archived'"
    sql += " ORDER BY created_at DESC LIMIT 200"
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [dict(r) for r in rows]


async def get_draft(draft_id: uuid.UUID) -> Optional[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_COLS} FROM communication_drafts WHERE id = $1", draft_id
        )
    return dict(row) if row else None


async def update_draft(draft_id: uuid.UUID, fields: dict) -> Optional[dict]:
    """Update allowed columns. Status, if provided, must be valid."""
    allowed = (
        "draft_type", "title", "recipient_hint", "subject", "body",
        "tone", "status", "metadata",
    )
    sets: list[str] = []
    args: list = []
    for col in allowed:
        if col in fields and fields[col] is not None:
            if col == "status" and fields[col] not in DRAFT_STATUSES:
                raise SignalToolError(
                    f"invalid status {fields[col]!r}", code="invalid_status"
                )
            args.append(fields[col])
            sets.append(f"{col} = ${len(args)}")
    if not sets:
        return await get_draft(draft_id)
    sets.append("updated_at = NOW()")
    args.append(draft_id)
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE communication_drafts SET {', '.join(sets)} "
            f"WHERE id = ${len(args)} RETURNING {_COLS}",
            *args,
        )
    return dict(row) if row else None


async def archive_draft(draft_id: uuid.UUID) -> Optional[dict]:
    return await update_draft(draft_id, {"status": "archived"})


async def delete_draft(draft_id: uuid.UUID) -> bool:
    pool = _require_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            "DELETE FROM communication_drafts WHERE id = $1", draft_id
        )
    return not res.endswith(" 0")
