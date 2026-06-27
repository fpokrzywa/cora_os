# Next Session — First Message

Continuing the **Cora AI OS** build. Last session shipped a **model-driven agent runtime** and a
**Cora Configuration** UI. This doc is the entry point; deeper detail lives in code docstrings, the
commits below, and the auto-memory `agent_runtime_build` (do not re-summarize or rebuild shipped work).

> `HANDOFF_SESSION.md` (What's Completed, §10, §14) and `AIOS_CORE_ARCHITECTURE.md` §9 now cover the
> agent runtime (updated 2026-06-27). Keep them current as it evolves — update, don't just append.

## Git / deploy state (verify first)
- **Everything is on `main`** — `main` == `feat/agentic-runtime` == `origin` @ **`889ec0e`**. Working tree clean.
- Stack up + healthy: `cora-api`, `cora-worker`, `cora-ui`, `cora-postgres`. Containers were built from this
  code, so **live == `main`**.
- `gh` is NOT installed (no `GH_TOKEN`); use plain `git`. `.env` is gitignored (secrets — never commit/echo).
- Quick smoke on start: `docker compose ps`; the agent runtime is live (see below).

## What shipped last session (2026-06-27) — DON'T rebuild
Reference, don't re-derive: commits `044e307` (runtime), `4004550` (UI), `889ec0e` (verify); memory
`agent_runtime_build`; code in `apps/cora-api/app/agent_runtime.py` (docstrings explain each phase).

- **Model-driven agent runtime** (`app/agent_runtime.py`) — additive to the deterministic regex chat
  router, does NOT replace it. 5 phases: tool-calling kernel → durable runs (`agent_runtime_runs` table)
  → hub-and-spoke delegation (orchestrator-only `delegate_to` → FORGE/PULSE/SIGNAL/CHRONOS spokes, reuses
  `agent_delegations`) → parallel fan-out (`asyncio.gather` + per-run semaphore) → review-only staging
  (stages email drafts / schedule proposals; **never sends or writes a calendar**).
- **Endpoints:** `POST /chat/agent` (sync), `POST /chat/agent/async` (worker, non-blocking), `GET
  /chat/agent/runs/{id}`, `GET /chat/agent/config` (read-only flag status).
- **UI:** "Cora Configuration" is a **tab in Admin Console** (after Workspaces) → `CoraConfiguration.tsx`:
  flag status + a panel to run the agent and see its tool/delegation trace. Operator also added a
  **Memories** tab (`Memories.tsx`) in the same change.
- **Tests:** `scripts/verify_agent_runtime.py` — 24 deterministic assertions (no live-model call), PASS.

## Currently LIVE + verified
- Flags in `.env` (all TRUE this deploy): `AGENT_RUNTIME_ENABLED`, `AGENT_DELEGATION_ENABLED`,
  `AGENT_WRITE_ENABLED`, `AGENT_DELEGATION_MAX_PARALLEL=3`, `DGX_CHAT_MODEL_NAME=cora-qwen3:4b`.
- Verified end-to-end on `qwen3:4b`: read-only web_search turn, ATLAS→PULSE delegation hop, staged
  review-only draft. (4B model tool-calls cleanly but spokes sometimes answer from memory vs. using a tool.)
- Behavioral testing needs an **operator JWT** (grab from browser DevTools → any API call's
  `Authorization: Bearer …`); `/auth/register` is admin-locked so no throwaway accounts.

## Do-not-break (agent-runtime invariants)
- **Fail-closed by flag**: capabilities only exist when their `AGENT_*` flag is on; default false.
- **No external effects in the loop**: staging tools are `internal_action` only; `check_permission`
  hard-blocks external-execution tools regardless. The agent CANNOT send email / write a calendar.
- **Hub-and-spoke**: only the orchestrator gets `delegate_to`; spokes can't delegate (depth guard = 1 hop);
  spokes run with their own `allowed_agents`-scoped tool catalog (domain isolation).
- Carry forward the calendar invariants from the prior handoff (dedicated `CALENDAR_EXECUTION_ENABLED`
  switch, all calendar writes confirm-before-write, `EXTERNAL_EXECUTION_ENABLED` stays false, email send
  hard-disabled). Don't recreate the postgres volume; don't edit compose unless asked.

## 🛠️ Build backlog (roughly by value/risk — operator picks)
1. **Real external execution (the big one)** — the deferred confirm-as-interrupt phase: agent stages →
   run pauses (`waiting_user`, already in the schema) → you approve → the existing calendar/email path
   fires under the kill switches. This is "drafts things" → "does things for real." Highest value, highest care.
2. **Runs / task-manager view** — a sub-tab under Cora Configuration reading `agent_runtime_runs` +
   `agent_delegations`: the orchestrator→spoke tree, step traces, run history. Makes multi-agent behavior
   visible and is low-risk. **My pick for next.**
3. **Worker concurrency** — the worker runs one job at a time, so a long agent run now blocks news
   refreshes / other runs (a side effect of Phase 3). A bounded async pool fixes it.
4. **Model reliability** — spokes sometimes answer from memory instead of using web_search; pointing
   `DGX_CHAT_MODEL_NAME` at a larger Qwen3 helps. Config-only, no code.
5. **Smaller polish** — surface the async endpoint (`/chat/agent/async`) in the UI for long runs;
   optionally relocate the app-config screens under Cora Configuration ("option 2"); open the agent to
   non-admin users if you want it outside Admin Console.

## One human loose end (operator-only — I can't click it)
Original handoff's **live calendar checklist** on the real Google/Outlook account:
(1) `what is on my calendar next week` → numbered list; (2) `cancel 4` → confirm card names the right
calendar → `confirm` → event actually gone; (3) `when am I free this week`; (4) `reschedule 2 to <time>`
→ confirm; (5) `brief me on my day`. The **code is green** (`verify_chat_calendar/scheduling/briefing`
all PASS); this is just live-account confirmation. That work already shipped to `main`.

## Working rules (saved feedback)
- **No clarifying/direction-choosing questions** — proceed autonomously from context, report tersely;
  confirm only before destructive/irreversible or outward-facing actions (real calendar/inbox writes,
  pushing to `main` — use clearly-labeled throwaway items + clean them up).
- **Every delivery ends with concrete in-app testing steps.**
- Per-module workflow: edit → `python3 -m py_compile` (+ `tsc -b` runs inside the cora-ui Docker build) →
  `docker compose build <svc> && docker compose up -d <svc>` (image is baked — no volume mount; a rebuild
  is required to deploy code; env-only flag flips need just `up -d`) → run the relevant
  `scripts/verify_*.py` in the cora-api container, e.g.
  `docker cp apps/cora-api/scripts/verify_agent_runtime.py cora-api:/tmp/v.py && docker exec -e PYTHONPATH=/app cora-api python /tmp/v.py`
  → live HTTP smoke when it touches the chat route (needs an operator JWT).
- Auto-memory lives at `~/.claude/projects/-home-owner-cora-ai-os/memory/` — keep `agent_runtime_build`
  current as the runtime evolves; update `MEMORY.md` pointers.

## Suggested skills
- `/run` — launch/drive the app to see a change working.
- `/verify` — confirm a change does what it should by observing real behavior.
- `/code-review` — review the working diff before committing (use `/code-review ultra` for a deep pass).
