"""Shared review/approval workflow engine for SIGNAL drafts and CHRONOS
proposals (Draft / Proposal Review Workflow v0.3).

Internal-only and non-autonomous: status moves through a fixed lifecycle and
every transition is recorded in a *_review_events table. "approved" means
approved *internally* — there is NO email send, NO calendar write, NO invite.

The two record types share identical lifecycle logic; only table names and the
initial status ('draft' vs 'proposed') differ, so they're driven by a config.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Optional

from app.clients import clients

logger = logging.getLogger(__name__)

# Full status sets (widened from v0.1's draft/reviewed/approved/archived).
DRAFT_STATUSES = (
    "draft", "in_review", "changes_requested", "reviewed", "approved", "archived",
    "rejected",
)
PROPOSAL_STATUSES = (
    "proposed", "in_review", "changes_requested", "reviewed", "approved", "archived",
)

# Content-editable statuses for non-admins. Admins may edit any non-terminal row.
DRAFT_EDITABLE = {"draft", "changes_requested"}
PROPOSAL_EDITABLE = {"proposed", "changes_requested"}

# Actions exposed as endpoints, mapped to (allowed from-statuses, to-status).
# submit-review accepts changes_requested as a source so a returned item can be
# re-submitted directly; this subsumes the changes_requested -> initial reset.
_DRAFT_TRANSITIONS = {
    "submit_for_review": ({"draft", "changes_requested"}, "in_review"),
    "request_changes": ({"in_review"}, "changes_requested"),
    # mark_reviewed supports the direct draft -> reviewed path AND the
    # submit-first in_review -> reviewed path (changes_requested may also be
    # marked reviewed without re-submitting).
    "mark_reviewed": ({"draft", "in_review", "changes_requested"}, "reviewed"),
    "approve": ({"reviewed"}, "approved"),
    # reject: an explicit decline from any non-terminal state (v1.9 chat flow).
    # Additive — does not alter the existing draft-first lifecycle.
    "reject": (
        {"draft", "in_review", "changes_requested", "reviewed"}, "rejected",
    ),
    "archive": (
        {"draft", "in_review", "changes_requested", "reviewed", "approved", "rejected"},
        "archived",
    ),
}
_PROPOSAL_TRANSITIONS = {
    "submit_for_review": ({"proposed", "changes_requested"}, "in_review"),
    "request_changes": ({"in_review"}, "changes_requested"),
    # mark_reviewed supports the direct proposed -> reviewed path AND the
    # submit-first in_review -> reviewed path.
    "mark_reviewed": ({"proposed", "in_review", "changes_requested"}, "reviewed"),
    "approve": ({"reviewed"}, "approved"),
    "archive": (
        {"proposed", "in_review", "changes_requested", "reviewed", "approved"},
        "archived",
    ),
}


class ReviewError(Exception):
    """code: invalid_transition (409) | forbidden (403) | not_found (404) |
    unavailable (503)."""

    def __init__(self, message: str, *, code: str = "invalid_transition"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ReviewConfig:
    table: str
    event_table: str
    fk_col: str  # 'draft_id' | 'proposal_id'
    select_cols: str
    initial_status: str
    statuses: tuple
    editable: set
    transitions: dict


DRAFT_CONFIG = ReviewConfig(
    table="communication_drafts",
    event_table="draft_review_events",
    fk_col="draft_id",
    select_cols=(
        "id, workspace_id, created_by, agent_name, draft_type, title, "
        "recipient_hint, subject, body, tone, status, metadata, "
        "reviewed_by, reviewed_at, approved_by, approved_at, archived_at, "
        "review_notes, created_at, updated_at"
    ),
    initial_status="draft",
    statuses=DRAFT_STATUSES,
    editable=DRAFT_EDITABLE,
    transitions=_DRAFT_TRANSITIONS,
)

PROPOSAL_CONFIG = ReviewConfig(
    table="schedule_proposals",
    event_table="proposal_review_events",
    fk_col="proposal_id",
    select_cols=(
        "id, workspace_id, created_by, agent_name, proposal_type, title, "
        "description, start_time, end_time, timezone, attendees, agenda, "
        "reminders, status, metadata, reviewed_by, reviewed_at, approved_by, "
        "approved_at, archived_at, review_notes, created_at, updated_at"
    ),
    initial_status="proposed",
    statuses=PROPOSAL_STATUSES,
    editable=PROPOSAL_EDITABLE,
    transitions=_PROPOSAL_TRANSITIONS,
)


def _require_pool():
    if clients.db_pool is None:
        raise ReviewError("Postgres pool unavailable", code="unavailable")
    return clients.db_pool


async def perform_action(
    cfg: ReviewConfig,
    item_id: uuid.UUID,
    *,
    action: str,
    user_id: uuid.UUID,
    is_admin: bool,
    notes: Optional[str] = None,
) -> dict:
    """Validate + apply a review transition, record an event row, and return the
    updated record. Raises ReviewError on invalid transition / authorization.

    Visibility (owner-or-admin) is enforced by the caller; this enforces the
    transition-specific rule that a creator may not approve their own item."""
    if action not in cfg.transitions:
        raise ReviewError(f"unknown action {action!r}", code="invalid_transition")
    allowed_from, to_status = cfg.transitions[action]

    pool = _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"SELECT {cfg.select_cols} FROM {cfg.table} WHERE id = $1 FOR UPDATE",
                item_id,
            )
            if row is None:
                raise ReviewError("record not found", code="not_found")
            from_status = row["status"]
            if from_status not in allowed_from:
                raise ReviewError(
                    f"cannot {action} from status {from_status!r}",
                    code="invalid_transition",
                )
            # A creator may not approve their own item; only an admin can approve.
            if action == "approve" and not is_admin:
                raise ReviewError(
                    "approval requires an admin reviewer",
                    code="forbidden",
                )

            sets = ["status = $1", "updated_at = NOW()"]
            args: list = [to_status]
            if action == "mark_reviewed":
                args.append(user_id)
                sets.append(f"reviewed_by = ${len(args)}")
                sets.append("reviewed_at = NOW()")
            elif action == "approve":
                args.append(user_id)
                sets.append(f"approved_by = ${len(args)}")
                sets.append("approved_at = NOW()")
            elif action == "archive":
                sets.append("archived_at = NOW()")
            if notes is not None and notes.strip():
                args.append(notes.strip())
                sets.append(f"review_notes = ${len(args)}")

            args.append(item_id)
            updated = await conn.fetchrow(
                f"UPDATE {cfg.table} SET {', '.join(sets)} "
                f"WHERE id = ${len(args)} RETURNING {cfg.select_cols}",
                *args,
            )
            await conn.execute(
                f"""
                INSERT INTO {cfg.event_table}
                    ({cfg.fk_col}, user_id, action, from_status, to_status, notes)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                item_id,
                user_id,
                action,
                from_status,
                to_status,
                (notes.strip() if notes and notes.strip() else None),
            )
    result = dict(updated)
    result["_from_status"] = from_status
    result["_to_status"] = to_status
    return result


async def list_events(cfg: ReviewConfig, item_id: uuid.UUID) -> list[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, {cfg.fk_col}, user_id, action, from_status, to_status,
                   notes, created_at
            FROM {cfg.event_table}
            WHERE {cfg.fk_col} = $1
            ORDER BY created_at ASC
            """,
            item_id,
        )
    return [dict(r) for r in rows]
