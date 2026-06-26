"""Admin-managed runtime override for the execution kill switches.

The env vars (config.Settings: calendar_execution_enabled / screen_vision_enabled /
external_execution_enabled) are the DEFAULT. A row in `runtime_execution_switches`,
when present, OVERRIDES the env value at runtime so an admin can toggle a switch from
the app without a redeploy. No row => the env default applies.

SAFETY: `external_execution_enabled` is registered here for VISIBILITY but is NOT
manageable — it stays env-locked because turning it on breaks the email/integration
approval final interlock (which requires it false). `set_switch` refuses any
non-manageable switch. Nothing here calls a provider; flipping a switch lifts ONE
gate only — the per-provider feature flags + OAuth scopes + (for calendar)
confirm-before-write all still apply.
"""

import uuid
from typing import Optional

from app.clients import clients
from app.config import settings
from app.runtime_traces import write_trace

TRACE_MODIFIED = "execution_switch_modified"
_TOOL = "execution_switch"


class SwitchError(Exception):
    """code: not_found (404) | forbidden (403) | unavailable (503) | invalid (400)."""

    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


# name -> metadata. env_attr is the config.Settings field that supplies the default.
_SWITCHES: dict[str, dict] = {
    "calendar_execution_enabled": {
        "env_attr": "calendar_execution_enabled",
        "label": "Calendar execution (CALENDAR_EXECUTION_ENABLED)",
        "manageable": True,
        "description": "Master gate for real calendar writes (create/edit/cancel). The "
                       "per-provider calendar_write flag + confirm-before-write still apply.",
    },
    "screen_vision_enabled": {
        "env_attr": "screen_vision_enabled",
        "label": "Screen vision (SCREEN_VISION_ENABLED)",
        "manageable": True,
        "description": "Master gate for the opt-in screen-vision path. Also requires "
                       "VISION_MODEL_NAME + a DGX endpoint, else it still fails closed.",
    },
    "external_execution_enabled": {
        "env_attr": "external_execution_enabled",
        "label": "External execution (EXTERNAL_EXECUTION_ENABLED)",
        "manageable": False,
        "description": "Global email/integration kill switch. ENV-LOCKED: turning it on "
                       "breaks the approval final interlock (which requires it false). "
                       "Change via env + restart only.",
    },
}


def _require_pool():
    if clients.db_pool is None:
        raise SwitchError("Postgres pool unavailable", code="unavailable")
    return clients.db_pool


def is_managed(name: str) -> bool:
    meta = _SWITCHES.get(name)
    return bool(meta and meta["manageable"])


def _env_default(name: str) -> bool:
    return bool(getattr(settings, _SWITCHES[name]["env_attr"]))


async def _override(name: str) -> Optional[bool]:
    """The DB override value for a switch, or None if no row (=> use env default)."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT enabled FROM runtime_execution_switches WHERE name = $1", name)
    return None if row is None else bool(row["enabled"])


async def effective(name: str) -> bool:
    """The live value of a switch: DB override if set, else the env default. Unknown
    names fall back to the matching settings attr if present, else False (fail-closed)."""
    if name not in _SWITCHES:
        return bool(getattr(settings, name, False))
    ov = await _override(name)
    return _env_default(name) if ov is None else ov


async def get_all() -> list[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = {r["name"]: r for r in await conn.fetch(
            "SELECT name, enabled, updated_by, updated_at FROM runtime_execution_switches")}
    out = []
    for name, meta in _SWITCHES.items():
        r = rows.get(name)
        override = bool(r["enabled"]) if r else None
        env_default = _env_default(name)
        out.append({
            "name": name,
            "label": meta["label"],
            "description": meta["description"],
            "manageable": meta["manageable"],
            "env_default": env_default,
            "override": override,
            "overridden": override is not None,
            "effective": env_default if override is None else override,
            "updated_at": r["updated_at"] if r else None,
            "updated_by": str(r["updated_by"]) if r and r["updated_by"] else None,
        })
    return out


async def set_switch(name: str, enabled: bool, *, admin_id: uuid.UUID) -> dict:
    meta = _SWITCHES.get(name)
    if meta is None:
        raise SwitchError(f"unknown switch {name!r}", code="not_found")
    if not meta["manageable"]:
        raise SwitchError(f"{name} is env-locked and cannot be changed from the app",
                          code="forbidden")
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO runtime_execution_switches (name, enabled, updated_by, updated_at) "
            "VALUES ($1,$2,$3,NOW()) ON CONFLICT (name) DO UPDATE "
            "SET enabled=$2, updated_by=$3, updated_at=NOW()",
            name, enabled, admin_id)
    await write_trace(
        session_id=None, user_id=admin_id, trace_type=TRACE_MODIFIED, status="ok",
        selected_agent=None, tool_name=_TOOL,
        tool_result={"name": name, "enabled": enabled, "env_default": _env_default(name)})
    for s in await get_all():
        if s["name"] == name:
            return s
    raise SwitchError("switch missing after update", code="not_found")  # unreachable


async def clear_override(name: str, *, admin_id: uuid.UUID) -> dict:
    """Remove the DB override so the switch reverts to its env default."""
    if name not in _SWITCHES:
        raise SwitchError(f"unknown switch {name!r}", code="not_found")
    if not _SWITCHES[name]["manageable"]:
        raise SwitchError(f"{name} is env-locked", code="forbidden")
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM runtime_execution_switches WHERE name = $1", name)
    await write_trace(
        session_id=None, user_id=admin_id, trace_type=TRACE_MODIFIED, status="ok",
        selected_agent=None, tool_name=_TOOL,
        tool_result={"name": name, "cleared": True, "env_default": _env_default(name)})
    for s in await get_all():
        if s["name"] == name:
            return s
    raise SwitchError("switch missing after clear", code="not_found")  # unreachable
