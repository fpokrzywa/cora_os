# Session Handoff — CHRONOS Calendar CRUD + Live Inbox Read (2026-06-25)

> Continuation handoff for the session that built CHRONOS calendar CRUD and took
> Gmail inbox read live. **Primary source of truth remains `HANDOFF_SESSION.md`
> (§9 changelog + §10 next steps) and `AIOS_CORE_ARCHITECTURE.md`** — both were
> updated this session. This doc only captures what's specific to continuing from
> here. Do not re-summarize shipped modules; read those two files first.

## TL;DR of this session

Built and took **live** two governed external capabilities for Cora, both verified
end-to-end against the real provider accounts:

1. **CHRONOS Calendar CRUD (v1.0 → v1.4)** — chat-native calendar read + create/edit/cancel.
2. **Live Inbox Read GO-LIVE** — Gmail read-only inbox (list/summarize/sender-scoped search).

All 16 `apps/cora-api/scripts/verify_*.py` pass. Stack healthy. Nothing left
half-done. Work is **uncommitted** (see Git state below).

## Current state of the two features

### Calendar (LIVE — reads + writes)
- `google_calendar` connected with `calendar.events` + `calendar.readonly`.
- Flags `calendar_read` + `calendar_write` ON; env `CALENDAR_EXECUTION_ENABLED=true`.
- Reads aggregate ALL calendars (past-aware windows, recurring dedup, local-time render).
- Writes go through confirm-before-write; create→primary, update/delete→event's own calendar; invites sent (`sendUpdates=all`).
- Live-verified: create + cancel a throwaway event; a bogus "cancel" safely refuses (no arbitrary target).

### Inbox (LIVE — read-only)
- `gmail` reconnected with `gmail.readonly` + `gmail.send`; `gmail/inbox_read` flag ON.
- Live-verified: list / summarize / sender-scoped search ("from X" → `from:` operator).
- `supports_send=False` — no send method exists in code.

## Key architecture decision made this session (important)

Calendar writes use a **dedicated `CALENDAR_EXECUTION_ENABLED` switch**, NOT the
global `EXTERNAL_EXECUTION_ENABLED` kill switch. Reason discovered live: the
email/integration approval governance (`execution_approval.approve()` /
`final_interlock`) has a checklist item that REQUIRES `execution_enabled=false`
("cannot approve — … execution_enabled (must be disabled)"). Flipping the global
flag broke 6 governance verify scripts. So calendar got its own master switch;
the global one stays false. **Do not re-couple calendar writes to the global kill
switch.** See `app/config.py` (`calendar_execution_enabled`) and
`app/chat_calendar.py` `_write_gate`.

## Files changed this session

New: `apps/cora-api/app/calendar_adapters.py`, `apps/cora-api/app/chat_calendar.py`,
`apps/cora-api/scripts/verify_chat_calendar.py`, `apps/cora-api/scripts/verify_oauth_redirect.py`,
`AIOS_CORE_ARCHITECTURE.md` (the detailed changelog — see its top entries for full per-module detail).

Modified: `apps/cora-api/app/chat_inbox.py` (sender-scoped `from:` search),
`app/config.py` (calendar switch), `app/oauth_providers.py` (per-provider redirect
derivation + `calendar.readonly` scope), `app/routers/chat.py` (calendar dispatch),
`app/schema.py` (calendar tables/columns/flags), `scripts/verify_chat_inbox.py` +
`scripts/verify_feature_flags.py` (snapshot/restore operator flag state),
`docker-compose.yml` (`CALENDAR_EXECUTION_ENABLED`), `HANDOFF_SESSION.md`.

Per-module detail (v1.0–v1.4, inbox go-live) is in `AIOS_CORE_ARCHITECTURE.md` §9 — do not duplicate here.

## Standing environment state (operator config — already set)

- `.env`: `EXTERNAL_EXECUTION_ENABLED=false` (keep false), `CALENDAR_EXECUTION_ENABLED=true`.
  A timestamped `.env.bak-*` backup was created when flipping switches.
- DB flags: `google_calendar` calendar_read/calendar_write enabled; `gmail/inbox_read` enabled.
- Google Cloud Console (done for this account): Calendar API enabled; redirect
  `http://localhost:8000/oauth/google_calendar/callback` registered. For a
  non-localhost host, set `GOOGLE_OAUTH_REDIRECT_BASE=https://<host>` (Google
  requires https off-localhost). OAuth client creds already in `.env`.

## Verify / build workflow (unchanged project convention)

```
edit → python3 -m py_compile <files>
docker compose up -d --build cora-api cora-worker   # rebuild needed for config.py changes
docker cp apps/cora-api/scripts/verify_X.py cora-api:/tmp/ && docker exec -e PYTHONPATH=/app cora-api python /tmp/verify_X.py
# full regression: loop all scripts/verify_*.py inside the cora-api container
```
Note: verify scripts that touch live operator flags now **snapshot/restore** them
(don't re-introduce assert-disabled or force-disable in cleanup —
`verify_chat_calendar`, `verify_chat_inbox`, `verify_feature_flags` were fixed for this).

## What's next (pick up here)

1. **Commit this work.** It's all uncommitted on `main`. Branch + commit before the
   next module (the user controls when; don't push without asking). Suggested split
   or single "CHRONOS Calendar CRUD v1.0–v1.4 + live inbox read" commit.
2. **n8n `cora-health` webhook** — the last §10 operator action; `n8n_health_check`
   tool 404s until it exists. Needs the n8n editor (http://n8n.local.arpa) — an
   agent can't create it; surface it to the operator.
3. **Next BUILD module candidate: Tier-2 screen vision** — opt-in screenshot capture
   + a vision model on the DGX Spark (qwen2.5-vl via Ollama `images` param), behind a
   feature flag, user-initiated only, audited, no auto-capture. (Builds on Screen
   Context v0.1.)
4. **Optional follow-ups noted but not built:** Outlook inbox/calendar go-live
   (connect `outlook_mail`/`outlook_calendar` OAuth + enable their flags — same
   pattern); calendar CREATE still targets `primary` only (no per-calendar selection
   in NL yet); `DROP TABLE news_sources` (dead since v2.6, destructive — confirm first).

## Working rules (from saved memory — apply these)

- No clarifying / direction-choosing questions; proceed autonomously from context,
  report tersely; no preamble/interim narration. Confirm only before
  destructive/irreversible or outward-facing actions.
- Keep `HANDOFF_SESSION.md` current as each module lands (update §9/§10/§13/§14, don't just append).
- Don't recreate the `cora-stack_postgres_data` volume; don't edit `cora-stack/docker-compose.yml` unless asked.
- Writes to the user's real calendar/inbox are outward-facing — for live tests use
  clearly-labeled throwaway items with no attendees and clean them up.

## Suggested skills for the next session

- **`verify`** — to confirm a change works by running the app and observing behavior
  (the live provider tests this session were done this way).
- **`code-review`** — before committing the large calendar/inbox diff, review for
  correctness (especially the governance gates and the no-arbitrary-target safety logic).
- **`security-review`** — this session added real external-execution paths (calendar
  writes) and live token handling; a security pass on `chat_calendar.py` /
  `calendar_adapters.py` / the gates is warranted before committing.
