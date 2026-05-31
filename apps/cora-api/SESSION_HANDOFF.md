# Session Handoff — Cora AI OS (2026-05-29)

> **Canonical source of truth:** [`/home/owner/cora-ai-os/HANDOFF_SESSION.md`](../../HANDOFF_SESSION.md)
> Read it first. It has the full §3 build state, §9 reverse-chronological changelog
> (one entry per shipped module), §10 next steps, §11 "do not repeat", and §12
> assumptions. This file only captures **this session's deltas + working notes**
> so I don't duplicate it.

## What this session shipped (SIGNAL/CHRONOS external-integration track)

Four modules landed, all deployed + verified, each documented in §9 of the
canonical handoff — do **not** re-do these:

1. **SIGNAL / CHRONOS Governed Tool Planning v0.1** — `communication_drafts` /
   `schedule_proposals` tables, review-only CRUD, Admin Console → Agents sub-tabs.
2. **Chat-to-Draft Tool Invocation v0.2** — explicit-intent chat turns create
   drafts/proposals via governance check (`app/routers/chat.py` helpers).
3. **Draft / Proposal Review Workflow v0.3** — `app/review_workflow.py` engine,
   lifecycle (draft/proposed→in_review→changes_requested→reviewed→approved→archived),
   `*_review_events` tables, per-action endpoints, admin-only approve.
4. **External Integration Readiness v0.4** — `external_integration_intents` /
   `external_integration_events`, `app/integration_readiness.py`, dry-run intents.
5. **External Provider Connector Design v0.5** (most recent) —
   `external_provider_connectors` registry, `app/provider_connectors.py` contract,
   `app/routers/integration_providers.py`, `POST /integration/intents/{id}/dry-run`.

**Hard invariant across all of the above:** internal/dry-run only. Nothing sends
email, writes a calendar, sends invites, reads inboxes/calendars, or opens OAuth.
The provider layer has **zero** external-client imports and a
`LIVE_EXECUTION_ENABLED=False` kill-switch.

## Exact next step

**OAuth Credential Vault Design v0.6** — see §10 Step 1 of the canonical handoff
for the full spec. One-line: design + scaffold (NOT live) a per-workspace/
per-provider credential vault for the `requires_oauth=true` connectors
(gmail/outlook_mail/google_calendar/microsoft_calendar). Store secret
*references*/encrypted blobs only (env-based key per CLAUDE.md security rules),
connection-status model, admin register/rotate/revoke endpoints, governance +
audit traces, and a "test connection" that stays a dry-run stub. **No real token
exchange, no provider calls, no secrets in logs/traces/responses.**

## How to work in this repo (project conventions)

- **Per-task contract** (every "Implement X vN" message): do not restart
  architecture, do not repeat completed work, do **not** touch
  `cora-stack/docker-compose.yml`, do **not** recreate/modify the postgres volume.
- **Autonomy:** proceed without clarifying/option-menu questions; report tersely
  (memory `feedback_no_questions.md`). Confirm only genuinely destructive actions
  (memory `cora_deploy_cautions.md`).
- **Keep the canonical `HANDOFF_SESSION.md` current** in-place after each module
  (update §3 state + tables, add a §9 entry, bump §10 Step 1 + §13) — don't append.
- **Schema:** idempotent only — `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ... ADD
  COLUMN IF NOT EXISTS`, seeds `ON CONFLICT DO NOTHING`. All DDL lives in
  `app/schema.py` `SCHEMA_SQL`; seeds execute in `init_schema()`.
- **Governance/audit pattern:** new tool actions go through
  `app/tools/governance.py` `check_permission` + `log_execution_attempt`, and
  `app/runtime_traces.py` `write_trace`. Seed tools in `schema.py`.
- **Tables use `type` column** for tools (not `tool_type`); JSONB codec is
  registered on the pool so pass Python dicts directly as params.

## Validation loop (what "done" means here)

```
# backend syntax
python3 -m py_compile app/<changed>.py   # from apps/cora-api
# frontend types
cd /home/owner/cora-ai-os/apps/cora-ui && \
  docker run --rm -v "$PWD":/app -w /app node:20-alpine \
  sh -c "npm install && npx tsc --noEmit -p tsconfig.json"
# rebuild + health
cd /home/owner/cora-ai-os && docker compose up -d --build cora-api cora-worker cora-ui
docker exec cora-api sh -c "wget -qO- http://localhost:8000/health"
```

Then DB/API tests via an **in-container httpx script** (the established pattern):
mint a token with `app.auth.create_access_token`, run against
`http://localhost:8000`, and run it with
`docker exec -w /app -e PYTHONPATH=/app cora-api python /tmp/<script>.py`
(PYTHONPATH=/app is required or `import app` fails).

Known good test IDs (local dev DB):
- workspace `b53fe79f-5ff7-42a6-a445-653a3cb77e8b` ("Cora AI OS")
- admin user `d4f9c421-3826-4385-8a61-d970f5ee34f3` (freddie@3cpublish.com)
- non-admin user `b87bac82-ccc0-40f5-b7f5-8bca6d14fc0b`

Always **clean up test rows** afterward (dry-run intents:
`DELETE ... WHERE metadata->>'dry_run_only'='true'`; test drafts/proposals by
title). Re-running `init_schema` is safe (idempotent).

## Gotchas learned this session (not obvious from code)

- **In-container test scripts can't see `PGVECTOR_AVAILABLE`** — it's set by
  `init_schema` at server startup, not in a fresh `python` subprocess. Test
  embedding/semantic paths via the HTTP server, not a standalone script.
- **`select_subagent` routing** prefers DB `metadata.routing_keywords` of the
  active agent version over the Python constants — update via Agent Admin
  versioning API, never mutate active prompt rows directly.
- **PATCH on drafts/proposals is content-only** now — status changes go through
  the v0.3 review-workflow endpoints (PATCH rejects `status` with 400).
- **Cross-router import**: `routers/signal.py` and `routers/chronos.py` import
  `IntentOut`/`intent_to_out`/`write_intent_trace` from `routers/integration.py`.
  `integration.py` imports no routers, so no cycle — keep it that way.
- **Provider safety guard**: `provider_connectors.update_connector` force-sets
  `supports_send/calendar_*/read=FALSE` and `dry_run_only=TRUE` on every write,
  and the PATCH endpoint reports `_blocked_live_flags`. v0.6 must not weaken this.

## Suggested skills for the next session

- **`claude-code-guide`** — only if you hit a Claude Code / Agent SDK / Anthropic
  API question; not needed for the v0.6 feature itself.
- No other project-specific skills are registered. The work is standard
  FastAPI + asyncpg + React/Vite/TS following the patterns above; reach for
  `Explore`/`Plan` subagents if you need to map the codebase before editing.

## Environment

- Working dir for backend edits: `/home/owner/cora-ai-os/apps/cora-api`
- Stack: docker compose in `/home/owner/cora-ai-os` (cora-api/-worker/-ui/-postgres/
  -redis/-searxng/mcp-*). Postgres on pgvector image, DB `cora`, role `cora`.
- All four runtime containers currently **healthy**; no open problems.
