# Next Session — First Message

Continuing the **Cora AI OS** build. This session ran a **voice-first UI readiness** push — making
each agent/capability solid *before* a voice-first UI sits on top. Shipped **6 capabilities** on a
single branch (`/chat` SSE streaming, agent calendar READ tool, spoken confirm-as-interrupt, FORGE
turned into a real codebase/infra inspector, PULSE made web-aware, and a speakable/TTS reply mode).
This doc is the entry point; deeper detail lives in code docstrings, the commits below,
`AIOS_CORE_ARCHITECTURE.md` §9, `HANDOFF_SESSION.md`, `VOICE_UI_READINESS.md` (the ranked backlog),
and the auto-memories `agent_runtime_build` + `dgx_inference_backends` + `project_voice_ui_readiness`
(do NOT re-summarize or rebuild shipped work).

## Git / deploy state (verify first)
- **Everything is on `main` — local `main` == `origin/main` @ `831cbf8`** (pushed at session end; the
  `feat/voice-readiness` branch was FF-merged + deleted). This session's commits, newest first: `831cbf8`
  docs-state fix · `aebc510` docs · `5386f31` speakable · `548d382` PULSE · `bd029d9` FORGE · `ad0466f`
  spoken confirm · `7beb656` VOICE_UI_READINESS.md · `8993ff5` calendar read · `a2721d8` SSE streaming.
  No feature branches remain. Quick check: `git log --oneline -10`, `docker compose ps`.
- **The deployed stack runs this code** (each item was `docker compose build` + `up -d`), so **live == `main`**.
  Stack up + healthy: `cora-api`, `cora-worker`, `cora-ui`, `cora-postgres`, MCPs (incl. the rebuilt
  `mcp-filesystem`), `cora-searxng`.
- `gh` is NOT installed (no `GH_TOKEN`); use plain `git`. `.env` is gitignored (secrets + flags — never
  commit/echo it); it lives at the repo root `/home/owner/cora-ai-os/.env`.
- Working tree carries pre-existing handoff-doc items to LEAVE: a staged deletion of
  `HANDOFF_CALENDAR_INBOX_SESSION.md` + untracked `HANDOFF_CHAT_VLLM_SESSION.md`.
- **DGX SSH:** the orchestration host reaches the DGX (`spark-a84c`, Tailscale node = 100.114.254.113)
  over **Tailscale SSH** (`ssh -o IdentitiesOnly=yes -o StrictHostKeyChecking=no fpokrzywa@spark-a84c '<cmd>'`,
  key `/home/owner/.ssh/id_dgx_spark`). `docker` on the DGX needs no sudo. (Not used this session.)

## Currently LIVE config (in `.env`, NOT in git)
- **All inference on vLLM/gpt-oss-120b** (unchanged): `DGX_CHAT_BACKEND=openai`, `DGX_AGENT_BACKEND=openai`,
  `DGX_OPENAI_ENDPOINT=http://spark-a84c:8000/v1`, `DGX_OPENAI_MODEL=openai/gpt-oss-120b`. Revert text-gen
  to the 4B Ollama with `DGX_CHAT_BACKEND=ollama` + `DGX_AGENT_BACKEND=ollama` → `up -d cora-api cora-worker`
  (env-only; code default `ollama`). ⚠️ The DGX `vllm-oss` container MUST run with
  `--enable-auto-tool-choice --tool-call-parser openai` or agent tool calls don't parse. See `dgx_inference_backends`.
- **Agent flags ON:** `AGENT_RUNTIME_ENABLED`, `AGENT_DELEGATION_ENABLED`, `AGENT_WRITE_ENABLED`,
  `AGENT_INTERRUPT_ENABLED`, `AGENT_EVAL_ENABLED`, `AGENT_EVAL_GATE_ENABLED`, `AGENT_DELEGATION_MAX_PARALLEL=3`.
- **OFF (the outward kill switch that matters):** `AGENT_EXECUTION_ENABLED` (agent master gate — the agent
  STAGES + pauses but FIRES nothing while off). `EXTERNAL_EXECUTION_ENABLED` off (email). `calendar_execution_enabled`
  is ON via DB runtime override + per-provider `calendar_write` on for google + microsoft — so the ONLY thing
  gating an agent calendar write is `AGENT_EXECUTION_ENABLED`. Email send is hard-disabled regardless.
- **New opt-in request flags (default OFF — text UI unchanged):** `ChatRequest.stream` (SSE) and
  `ChatRequest.speakable` (TTS-friendly reply). The voice/UI layer sets these per turn.

## What shipped this session — DON'T rebuild (all on `main` @ `aebc510`)
Reference, don't re-derive. Each built → `py_compile`/tsc → `compose build`+`up -d` → in-container `verify_*`
→ live-confirmed → committed.
1. **`/chat` SSE streaming** (`a2721d8`) — opt-in `ChatRequest.stream` returns a `StreamingResponse` of
   `meta → delta* → done` (or `error`) SSE frames. New backend-selectable `llm.stream_text` (vLLM SSE +
   Ollama NDJSON). The post-LLM tail (draft/proposal suffix → persist → trace) was factored into a shared
   `_finalize`/`_emit_chat_trace` closure so the JSON + streaming paths can't drift; `done` carries the
   authoritative full reply. Frontend `sendChatStream` fills the bubble token-by-token; typing dots only
   pre-first-token. `X-Accel-Buffering: no` defeats NPM buffering. `verify_chat_streaming.py`. JSON path
   behavior-identical.
2. **Agent calendar READ tool** (`8993ff5`) — `chronos_list_calendar_events` (governed read-only,
   CHRONOS-scoped) lets the agent find an `event_id` from NL ("cancel my 3pm") instead of needing one pasted.
   `chat_calendar.agent_list_calendar_events` reuses `_read_gate`/`resolve_read_window`/`_read_one_calendar`
   (audited); special-cased in `_dispatch_read_only` AFTER governance. `verify_chat_calendar_read.py` (16).
3. **Spoken confirm-as-interrupt** (`ad0466f`) — `agent_runtime.resolve_pending_for_session(session_id,
   user_id, text)` finds the run paused at `waiting_user` and resolves it from a NL yes/no
   (`classify_confirmation`; "do it anyway"/"override" → approve+override). Speakable `confirmation_prompt`
   baked into the interrupt payload at pause; `_speakable_outcome` for the result. `POST /chat/agent/confirm`
   (always 200). Eval-gate + execution-gate still apply inside `resolve_interrupt`. `verify_chat_confirm.py` (33).
4. **FORGE = real codebase/infra inspector** (`bd029d9`) — FORGE already OWNED `filesystem_read_file`/
   `filesystem_list_project` but its frozen seed prompt said "never calls tools", so it didn't. Rewrote the
   prompt tool-aware via an idempotent no-clobber startup migration (`registry._ensure_prompt_revision`, lifts
   the LIVE agent_version off the pristine seed → new active version). Also fixed a latent crash:
   `_dispatch_read_only` now forwards ONLY advertised args (a model-invented `line_start` crashed the MCP read),
   added `read_file` line-range paging (mcp-filesystem + schema), raised the per-tool observation cap 4000→12000.
   `verify_agent_forge.py` (14); a live FORGE run read docker-compose.yml + listed all 8 services. (n8n
   automation-executor direction DEFERRED — no n8n deployed.)
5. **PULSE web-aware** (`548d382`) — prompt no longer claims "no live web access" while the governed
   `web_search` tool IS wired + PULSE-scoped. Same migration helper, generalized to ALSO fire when the active
   prompt still carries the stale phrase (catches operator-edited versions that inherited it — PULSE was on an
   admin-edited v6→v7), preserving routing keywords. `verify_agent_pulse.py`.
6. **Speakable (TTS) reply mode** (`5386f31`) — opt-in `ChatRequest.speakable` appends a "short, spoken,
   no-markdown" style instruction + runs the reply through new `app/speakable.to_speakable` (strips markdown/
   code/links/bullets/tables→prose/emoji; idempotent). Default off. `verify_speakable.py`; live: a "markdown
   table" ask returned clean spoken prose, zero markdown.

## 🛠️ Build backlog (operator picks)
**Remaining from the voice-readiness plan (`VOICE_UI_READINESS.md`):**
- **P1 #6 — Semantic routing fallback** — when keyword routing (`select_subagent`) scores 0/ambiguous, fall
  back to an embedding (or small-LLM) specialist pick; deterministic path unchanged when it matches. Matters
  for spoken phrasing variety.
- **P1 #7 — Memory cleanup + spoken disambiguation** — re-scope the 3 mis-scoped global personal facts (needs
  the operator's target-account pick, below), drop test-junk globals; ambiguous recall asks one spoken "which?".
- **P2** — generation cancellation / barge-in backend support; decide the email-send stance for voice
  (currently hard-disabled); MCP postgres/github real impls (deferred); Planner step execution (deferred).

**Audit findings surfaced this session (operator decisions, NOT done):**
- **n8n is not deployed** (no compose service; the `n8n_health_check` endpoint 404s) — so FORGE's
  "automation/infra executor via n8n" direction is blocked on infra. FORGE today is a codebase/infra *inspector*
  (filesystem reads). Deploying n8n is operator territory.
- **`mcp-postgres` + `mcp-github` are placeholder images** (only `mcp-filesystem` is real).
- **The Planner is a template-only stub** (creates plans, never executes steps).

## Operator-only loose ends (surface, don't do)
- **Memory-scoping data cleanup (still awaiting operator decision):** (a) 3 personal facts mis-scoped to
  `global` (visible to ALL accounts): `family` "Dorothy Pokrzywa" (wife), `family` "Family Dog" (Linda/"Bean"),
  `note` "Our family dog". RE-SCOPE to the owner — account ambiguous between `freddie@3cpublish.com`
  (`d4f9c421`, holds the 19 mems) and `fpokrzywa@gmail.com` (`b87bac82`, 0 mems); operator must pick. (b)
  test-junk `workspace_knowledge` globals (Example Domain ×6, Chunk Test Doc, "manual note refresh test"/"hello
  world") — deletable demo noise (destructive — confirm exact list). This is #7's cleanup half.
- n8n `cora-health` webhook still uncreated; optional `DROP TABLE news_sources` (dead since v2.6, destructive).
- Two test conversations ("streaming smoke ok" / "nonstream ok") + a couple of agent smoke runs persisted under
  `freddie@3cpublish.com` from live smokes — harmless internal rows; delete from the UI if desired.

## Do-not-break (invariants)
- **Fail-closed by flag:** every agent capability is gated; the outward kill switches (`AGENT_EXECUTION_ENABLED`,
  `EXTERNAL_EXECUTION_ENABLED`, `CALENDAR_EXECUTION_ENABLED`) default false; **email send is hard-disabled**.
- **Backends config-gated + reversible** (code default `ollama`); `DGX_CHAT_BACKEND` and `DGX_AGENT_BACKEND`
  are INDEPENDENT. New `DGX_*`/`AGENT_*` flags need a compose passthrough (cora-api AND cora-worker).
- **Agent loop:** hub-and-spoke (only the orchestrator gets `delegate_to`; spokes are `allowed_agents`-scoped,
  depth-1); evaluator is tool-less + advisory; the eval gate blocks the DECISION, not the firing.
- **`resolve_interrupt` (and `resolve_pending_for_session`) fire nothing** unless `AGENT_EXECUTION_ENABLED` is
  on (then only staged calendar create/update/delete via `_write_gate`; email never sent).
- **Agent prompts are runtime-versioned** (`agent_versions`, DB active version preferred over the module
  constant via `resolve_agent_prompt`/`_load_spokes`). To change a LIVE agent prompt you add a new active
  version, NOT just edit the module — see the idempotent `registry._ensure_prompt_revision` (no-clobber:
  only the pristine seed OR a version still carrying a named stale phrase; preserves routing keywords).
- **Read-only tool args are filtered** to the tool's advertised schema before dispatch (`_dispatch_read_only`)
  — don't reintroduce raw-arg passthrough (models invent params the MCP server rejects).
- Switches tiered: `calendar_execution_enabled` + `screen_vision_enabled` + per-provider flags are
  admin-toggleable (DB override over env); `external_execution_enabled` is env-locked; `AGENT_*` are env-only.
- Don't recreate the postgres volume. Don't edit `cora-stack/docker-compose.yml` unless asked. Don't
  reintroduce `select_subagent` into `forge.py` (routing lives in `app/agents/routing.py`).

## Working rules (saved feedback)
- **No clarifying/direction-choosing questions** (incl. `AskUserQuestion` option menus) — proceed autonomously
  from context, report tersely, no pre-action plans / interim narration. The ONLY carve-out is confirming
  genuinely destructive/irreversible OR outward-facing actions (real calendar/inbox writes, pushing to `main`).
  ([[feedback_no_questions]], [[feedback_inapp_test_steps]])
- **Per-item workflow:** build → `python3 -m py_compile` (+ `tsc -b` in the cora-ui Docker build) →
  `docker compose build <svc> && up -d <svc>` → run the relevant `scripts/verify_*.py` IN-CONTAINER
  (`docker cp …:/tmp/v.py && docker exec -e PYTHONPATH=/app cora-api python /tmp/v.py`) + a route smoke when it
  touches a route → commit on a `feat/`/`fix/` branch → report with concrete in-app test steps → on **"push"**,
  FF `main` + push + delete the branch.
- **33 `scripts/verify_*.py`** cover the suite (deterministic, in-container). Behavioral `/chat` testing needs
  an operator JWT (browser DevTools → any API call's `Authorization: Bearer …`), OR mint one in-container
  (`app.auth.create_access_token` for a real user) for local smokes — `/auth/register` is admin-locked.
- Keep `HANDOFF_SESSION.md` + `AIOS_CORE_ARCHITECTURE.md` §9 + these memories current as work lands.

## Suggested skills
- `/run` — launch/drive the app. `/verify` — confirm a change by real behavior. `/code-review` — review the
  diff (`/code-review ultra` for a deep cloud pass). `/handoff` — regenerate this doc as work continues.
