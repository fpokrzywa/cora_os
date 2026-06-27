# Next Session — First Message

Continuing the **Cora AI OS** build. Read **`HANDOFF_SESSION.md`** (repo root) as the single
source of truth; full per-module detail is in **`AIOS_CORE_ARCHITECTURE.md`** §9 — do not restart
or re-summarize shipped modules. Stack is up and healthy; **all 21 `scripts/verify_*.py` PASS**.

## First thing to confirm (git / merge state)
Last session's work is **committed as `379c640`** ("Daily Briefing + CHRONOS Smart Scheduling +
actionable calendar lists") on branch **`feat/daily-briefing-scheduling-calendar-ux`**, **pushed to
`origin`**, but **NOT yet on `main`** — deliberately held as a PR pending my live verification on the
real Google + Outlook account. The deployed cora-api/worker already run this branch.

**Ask me first:** did the pre-merge checklist pass? If yes, advance `main`:
`git push origin HEAD:main` (plain `git` — `gh` is NOT installed, no `GH_TOKEN`).
If something misbehaved, fix on the branch first; keep `main` clean.

Pre-merge checklist (operator tests in the app): (1) `what is on my calendar next week` → numbered
list; (2) `cancel 4` → confirm card names the right calendar → `confirm` → event actually gone from
Google; (3) `when am I free this week`; (4) `reschedule 2 to <time>` → confirm; (5) `brief me on my day`.

## What shipped last session (2026-06-27) — don't rebuild
- **Daily Briefing** (`app/chat_briefing.py`) — "brief me on my day" → read-only digest: today's
  schedule (all calendars) + inbox highlights (all mailboxes) + news headlines. Routed Cora.
- **CHRONOS Smart Scheduling** (`app/chat_scheduling.py`) — "when am I free this week?" / "find 30 min
  tomorrow afternoon" (read-only open-slot search from own events across all calendars, weekdays
  9–17 local) + "schedule 30 min with x@y next tue" → finds slot, stages via confirm-before-write.
- **Calendar write-path hardening** (`app/chat_calendar.py`): confirm-time provider redirect
  ("confirm but on google"); broadened delete detection (cancel/delete/remove that|this meeting…);
  **cross-provider** update/delete target resolution (searches ALL calendars, acts on the event's own
  one); **numbered, actionable read lists** — "cancel 4" / "reschedule 2 to 3pm" act on read item #4/#2.
  These fixed two LLM-hallucinated "cancellations" that never ran, and a wrong-calendar delete.

## Do-not-break (key invariants)
- Calendar writes use the **dedicated `CALENDAR_EXECUTION_ENABLED`** switch (app-toggleable via DB
  override), **NOT** the global `EXTERNAL_EXECUTION_ENABLED` (must stay `false` — email/integration
  governance). Email **send** stays hard-disabled.
- **All calendar writes are confirm-before-write.** Provider-less **reads** aggregate all connected
  providers; **named reads + all writes** resolve targets but never fire without an explicit "confirm".
- Never let an unrouted calendar phrase reach the generic LLM and "confirm" an action — that caused the
  hallucination bugs. New chat phrasings that imply a calendar action must route to `chat_calendar`.
- Don't recreate the `cora-stack_postgres_data` volume; don't edit `cora-stack/docker-compose.yml`
  unless asked. `.env` gitignored.

## Open items / candidate next builds (operator picks — ask me, don't assume)
- **Per-user default *calendar* for writes** — "make google my default calendar" currently sets the
  read/email default but writes still use named→most-recent (or the cross-provider search). Small, useful.
- **Governed email SEND go-live** — the one major capability still deliberately hard-disabled; highest
  impact + highest sensitivity. Keep behind explicit per-message approval even when on. (My decision: yours.)
- **Scheduling depth** — true multi-attendee availability (Graph `getSchedule` / Google `freeBusy`);
  "book slot #2" from a listed find; configurable working hours / per-user timezone.
- **Scheduled/delivered Daily Briefing** — reuse the worker `news_feed_refresh` scheduler as an internal artifact.
- **Operator-only:** n8n `cora-health` webhook (tool `n8n_health_check` 404s until created); optional
  `DROP TABLE news_sources` (dead since v2.6, destructive — confirm first).

## Working rules (saved feedback)
- **No clarifying/direction-choosing questions** — proceed autonomously from context, report tersely;
  confirm only before destructive/irreversible or outward-facing actions (real calendar/inbox writes —
  use clearly-labeled throwaway items + clean them up).
- **Every delivery ends with concrete in-app testing steps** so the operator can validate.
- **Keep `HANDOFF_SESSION.md` current** as each module lands (update §9/§10/§13/§14, don't just append).
- Per-module workflow: edit → `python3 -m py_compile` (+ UI `tsc --noEmit` if UI changed) →
  `docker compose up -d --build cora-api cora-worker` → run the relevant `scripts/verify_*.py` inside
  the cora-api container + full-suite regression → live HTTP smoke when it touches the chat route.
- Suggested skills: `/verify`, `/run`, `/code-review`.
