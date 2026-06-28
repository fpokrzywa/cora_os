# Next Session — First Message

Continuing the **Cora AI OS** build. The previous session (2026-06-28) extended the **model-driven agent
runtime** through Phase 7: a Runs viewer, worker concurrency, an independent evaluator, async runs in the
UI, and the **internal half of confirm-as-interrupt**. This doc is the entry point; deeper detail lives in
code docstrings, the commits below, and the auto-memory `agent_runtime_build` (do NOT re-summarize or
rebuild shipped work).

> `HANDOFF_SESSION.md` (What's Completed, §10 DONE breadcrumbs, backlog) and `AIOS_CORE_ARCHITECTURE.md`
> §9 are current as of 2026-06-28. Keep them current as the runtime evolves — update, don't just append.

## Git / deploy state (verify first)
- **Everything is on `main`** — local `main` == `origin/main` @ **`0cd2855`**. Working tree clean. No
  feature branches remain (this session FF-merged each item to `main` and pruned the branch + the two
  older stale branches).
- Stack up + healthy: `cora-api`, `cora-worker`, `cora-ui`, `cora-postgres` — built from this code, so
  **live == `main`**. Quick smoke: `docker compose ps`.
- `gh` is NOT installed (no `GH_TOKEN`); use plain `git`. `.env` is gitignored (secrets — never commit/echo).
  `.env` lives at the repo root: `/home/owner/cora-ai-os/.env`.

## What shipped last session (2026-06-28) — DON'T rebuild
Reference, don't re-derive. Commits: `b801cf8` (Runs view), `d89d875` (worker concurrency), `5368a36`
(evaluator), `ac8a489` (async UI), `0cd2855` (confirm-as-interrupt internal half). All in
`apps/cora-api/app/agent_runtime.py` + `app/worker.py` + `CoraConfiguration.tsx`; memory `agent_runtime_build`.

- **Runs / task-manager view** — a **Runs** sub-tab under Cora Configuration (Cora Config now has
  **Agent** + **Runs** sub-tabs). Owner-scoped run list → detail with the full step trace + the
  orchestrator→spoke **delegation tree** (each hop embeds the spoke's own trace + answer). `GET
  /chat/agent/runs` (list); `GET /chat/agent/runs/{id}` returns `delegations`. Tree correlates via an
  `input_payload._parent_run_id` stamp (no schema change — panel runs are sessionless).
- **Worker concurrency** — `cora-worker` is now a **bounded concurrent pool** (`WORKER_MAX_CONCURRENCY`,
  default 3): `process_one` → `run_claimed` + `_fill_slots`/`_reap`/`_idle_wait`. A long `agent_run` no
  longer blocks news refreshes / other runs; heartbeat + 60s scheduler tick alongside long jobs. Set
  `WORKER_MAX_CONCURRENCY=1` to restore strict serialization.
- **Independent evaluator (Phase 6)** — generator/evaluator split. `evaluate_run` runs ONE adversarial,
  **tool-less** review over a finished top-level run ("assume broken, no praise") → verdict
  `pass`/`concerns`/`fail` + reasons, stored on `agent_runtime_runs.evaluation`. Advisory/review-only —
  no external effects, does NOT gate execution. Optional independent judge model `DGX_EVAL_MODEL_NAME`.
- **Async runs in the UI** — a **"Run in background"** button → `POST /chat/agent/async` (worker-driven)
  → `RunDetail poll` polls `GET /chat/agent/runs/{id}` every 2s until terminal, live-rendering steps +
  delegation tree + verdict.
- **Confirm-as-interrupt — INTERNAL half (Phase 7)** — a top-level run that STAGED something pauses at
  `status='waiting_user'` (`_collect_staged` + `_pause_run`; `completed_at` NULL) with a pending
  `interrupt` payload on `agent_runtime_runs.interrupt`. `POST /chat/agent/runs/{id}/decision` →
  `resolve_interrupt` (owner-scoped, atomic `FOR UPDATE`) records approve→done / reject→cancelled and
  **FIRES NOTHING EXTERNAL**. UI: `InterruptCard` (Approve/Reject) in the Agent result + Runs detail;
  async polling stops at `waiting_user` (`isPollable`).
- **Tests:** `scripts/verify_agent_runtime.py` is up to **47 deterministic assertions** (Parts A–I, no
  live-model call) PASS; `scripts/verify_worker_concurrency.py` (12, DB-free) PASS.

## Currently LIVE + flag status (in `.env` this deploy)
- ON: `AGENT_RUNTIME_ENABLED`, `AGENT_DELEGATION_ENABLED`, `AGENT_WRITE_ENABLED`,
  `AGENT_DELEGATION_MAX_PARALLEL=3`, `DGX_CHAT_MODEL_NAME=cora-qwen3:4b`, `WORKER_MAX_CONCURRENCY=3`.
- OFF (default; operator can enable): `AGENT_EVAL_ENABLED`, `AGENT_INTERRUPT_ENABLED`,
  `DGX_EVAL_MODEL_NAME` (unset → falls back to the chat model). Flipping a flag is env-only —
  `docker compose up -d cora-api cora-worker` (no rebuild). Both services read every `AGENT_*` flag.
- Behavioral testing needs an **operator JWT** (browser DevTools → any API call's `Authorization:
  Bearer …`); `/auth/register` is admin-locked.

## Do-not-break (agent-runtime invariants)
- **Fail-closed by flag**: every capability is gated by its `AGENT_*` flag; all default false.
- **No external effects in the loop / no firing on decision**: staging tools are `internal_action`
  only; the evaluator is tool-less; `resolve_interrupt` records a decision and flips run status ONLY —
  it sends no email, writes no calendar. The agent CANNOT send/write. `check_permission` hard-blocks
  external-execution tools regardless.
- **Hub-and-spoke**: only the orchestrator gets `delegate_to`; spokes can't delegate (depth guard = 1
  hop); spokes run with their own `allowed_agents`-scoped catalog (domain isolation).
- Carry forward the calendar invariants: dedicated `CALENDAR_EXECUTION_ENABLED` switch, all calendar
  writes confirm-before-write, `EXTERNAL_EXECUTION_ENABLED` stays false, email send hard-disabled.
- Don't recreate the postgres volume. Compose edits are OK for `AGENT_*`/worker env passthrough
  (mirroring the existing pattern) — that's how a new flag becomes operable.

## 🛠️ Build backlog (roughly by value/risk — operator picks)
1. **Confirm-as-interrupt — OUTWARD half (the big one, needs the operator present)** — wire `approve`
   to actually FIRE the staged email/calendar write through the existing gated path (`calendar_adapters`
   create/update/delete; email lifecycle) under the kill switches. It's a localized swap inside
   `resolve_interrupt` (on approve → call the execution path per staged artifact). Keep
   `EXTERNAL_EXECUTION_ENABLED` false + email send hard-disabled; calendar gated by
   `CALENDAR_EXECUTION_ENABLED`. Verify on a **throwaway calendar event** with the operator. Highest value,
   highest care. (The internal pause→decide→resume machinery already exists.)
2. **Evaluator-gated approval** — once #1 exists, surface the evaluator verdict on the approval card and
   optionally block auto-approval on a `fail`. Ties Phase 6 + 7 together (the paper's "the verdict is what
   the human acts on before the write fires").
3. **Model reliability** — 4B spokes sometimes answer from memory instead of using web_search; point
   `DGX_CHAT_MODEL_NAME` (and/or `DGX_EVAL_MODEL_NAME` for an independent judge) at a larger Qwen3.
   Config-only, no code — needs a model pulled on the DGX (operator).
4. **Smaller polish (remainder)** — optionally relocate the app-config screens under Cora Configuration
   ("option 2"); open the agent to non-admin users (it's Admin-Console-gated today).

## Operator-only loose ends (I can't click these)
- **Enable + exercise the new flags live:** set `AGENT_EVAL_ENABLED=true` (optionally `DGX_EVAL_MODEL_NAME`)
  and `AGENT_INTERRUPT_ENABLED=true` in `.env`, `up -d cora-api cora-worker`, then in **Cora Configuration
  → Agent** run a staging prompt to see the verdict card + the Approve/Reject interrupt card.
- **Live calendar checklist** on the real Google/Outlook account (code is green — `verify_chat_calendar/
  scheduling/briefing` all PASS; this is just live-account confirmation): (1) `what is on my calendar next
  week`; (2) `cancel 4` → confirm card names the right calendar → `confirm` → event gone; (3) `when am I
  free this week`; (4) `reschedule 2 to <time>` → confirm; (5) `brief me on my day`.

## Working rules (saved feedback)
- **No clarifying/direction-choosing questions** — proceed autonomously from context, report tersely;
  confirm only before destructive/irreversible or outward-facing actions (real calendar/inbox writes,
  pushing to `main`). The session pattern: build an item → `verify_*.py` PASS → commit on a
  `feat/<item>` branch → report → on "push", FF `main` + push + delete the branch.
- **Every delivery ends with concrete in-app testing steps.**
- Per-module workflow: edit → `python3 -m py_compile` (+ `tsc -b` runs inside the cora-ui Docker build) →
  `docker compose build <svc> && docker compose up -d <svc>` (image is baked — no volume mount; a rebuild
  is required to deploy code; env-only flag flips need just `up -d`) → run the relevant
  `scripts/verify_*.py` in-container, e.g.
  `docker cp apps/cora-api/scripts/verify_agent_runtime.py cora-api:/tmp/v.py && docker exec -e PYTHONPATH=/app cora-api python /tmp/v.py`
  → a route smoke (401 not 404; OpenAPI carries new fields) when it touches the chat route.
- Schema changes are idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` run on cora-api start.
- Auto-memory lives at `~/.claude/projects/-home-owner-cora-ai-os/memory/` — keep `agent_runtime_build`
  current as the runtime evolves; update `MEMORY.md` pointers.

## Suggested skills
- `/run` — launch/drive the app to see a change working.
- `/verify` — confirm a change does what it should by observing real behavior.
- `/code-review` — review the working diff before committing (use `/code-review ultra` for a deep pass).
