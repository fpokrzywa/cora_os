# Next Session — First Message

Continuing the **Cora AI OS** build. This session **closed out the voice-first UI readiness backlog** —
the pre-UI capability phase is now **complete**. It shipped 3 capabilities on top of the prior 6: a
**semantic routing fallback**, a **memory cleanup + spoken disambiguation**, and **barge-in / generation
cancellation**. Everything below is on `main`. Deeper detail lives in code docstrings, the commits, the
`AIOS_CORE_ARCHITECTURE.md` §9 entries ("Voice-readiness close-out" + "Voice-first UI readiness"),
`HANDOFF_SESSION.md`, `VOICE_UI_READINESS.md` (the ranked backlog, now mostly ✅), and the auto-memories
`agent_runtime_build` + `dgx_inference_backends` + `project_voice_ui_readiness` (do NOT re-summarize or
rebuild shipped work).

## Git / deploy state (verify first)
- **Everything is on `main` — local `main` == `origin/main`.** This session's feature HEAD is **`47e4481`**
  (barge-in); a docs-refresh commit sits one ahead of it (this doc). This session's commits, newest first:
  `47e4481` barge-in cancellation · `8c704f0` memory disambiguation · `f5c9676` semantic routing. Prior
  session (also on main): `aebc510` docs · `5386f31` speakable · `548d382` PULSE · `bd029d9` FORGE ·
  `ad0466f` spoken confirm · `8993ff5` calendar read · `a2721d8` SSE streaming. No feature branches remain.
  Quick check: `git log --oneline -12`, `docker compose ps`.
- **The deployed stack runs this code** (each item was `docker compose build` + `up -d`), so **live == `main`**.
  Stack up + healthy: `cora-api`, `cora-worker`, `cora-ui`, `cora-postgres`, MCPs (`mcp-filesystem` real,
  `mcp-postgres`/`mcp-github` placeholders), `cora-searxng`.
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
- **Opt-in request flags (default OFF — text UI unchanged):** `ChatRequest.stream` (SSE) and
  `ChatRequest.speakable` (TTS-friendly reply). The voice/UI layer sets these per turn.
- **NEW flag this session (default OFF):** `SEMANTIC_ROUTING_ENABLED` — the LLM routing fallback (below).
  Wired into compose for cora-api + cora-worker; flip to `true` in `.env` + `up -d cora-api` to enable.

## What shipped this session — DON'T rebuild (all on `main`)
Reference, don't re-derive. Each built → `py_compile` → `compose build`+`up -d` → in-container `verify_*` →
live-confirmed → committed → pushed.
1. **Semantic routing fallback** (`f5c9676`) — when keyword routing (`select_subagent`) scores 0 AND no
   explicit intent override fired, `routing.semantic_route` makes ONE cheap LLM classification that picks a
   specialist (FORGE/PULSE/SIGNAL/CHRONOS) or NONE. Opt-in (`SEMANTIC_ROUTING_ENABLED`, default off),
   FAIL-OPEN (any error/unknown label → stays on Cora), and it can only move OFF Cora — never overrides a
   deterministic match. Runs in `chat.py` after the intent-override chain. **Embeddings were measured and
   rejected** (live nomic-embed-text baseline is flat — chit-chat out-scored real routes; 4/8). The
   classifier routes 6/6 keyword-free phrases live. The gpt-oss reasoning model needs a **256-token budget**
   (a tiny budget returns an empty final channel). `verify_semantic_routing.py`.
2. **Memory cleanup + spoken disambiguation** (`8c704f0` + a live DB fix) — **Data (DONE, operator-confirmed):**
   3 personal facts (wife Dorothy, the dog ×2) re-scoped `global → user` under `freddie@3cpublish.com`;
   ALL 15 test-junk `workspace_knowledge` globals deleted (56 chunks cascaded). 0 personal/junk globals
   remain. **Code:** same-title / different-content recall now appends a one-line instruction so Cora asks
   ONE clarifying question ("which one?") instead of guessing/merging (`app/memory/disambiguation.py` →
   `_format_memory_block`; HIGH-precision — no-op for ordinary recall). `verify_memory_disambiguation.py`.
3. **Barge-in / generation cancellation** (`47e4481`) — on a mid-stream client disconnect, `_event_stream`
   catches `asyncio.CancelledError` → `_finalize_cancelled` (shielded): the httpx teardown already aborts the
   upstream vLLM gen (frees the GPU), and it now writes a `cancelled` `llm_chat` trace + persists the partial
   assistant turn (no draft/proposal hooks). `verify_chat_cancel.py` (ASGITransport + injected CancelledError).

(Prior session's 6 voice-readiness capabilities — SSE streaming, agent calendar READ, spoken confirm-as-interrupt,
FORGE-as-inspector, PULSE web-aware, speakable replies — are on `main` @ `aebc510`; see §9. Don't rebuild.)

## 🛠️ Next phase — the voice-first UI client (the goal everything was prep for)
The pre-UI capability backlog is **exhausted of solo-buildable items**. The backend now gives a voice client
everything it needs: token **streaming**, **speakable** (TTS-clean) output, **spoken yes/no** confirm,
**phrasing-tolerant** routing, and **barge-in** cancellation. The natural next build is the **voice client
itself**: mic capture → STT → `POST /chat` (`stream:true, speakable:true`) → TTS → barge-in (abort the fetch
mid-stream; the backend already cleans up). **Get the operator's STT/TTS choices before writing code.**

## Build backlog (operator picks) — all remaining items need the operator
- **Email-send stance for voice** (P2 #9) — send is hard-disabled by design; whether voice ever sends (and
  behind what gate) is a **policy call**. Don't flip it unprompted (outward capability).
- **n8n deploy** — no compose service exists; the `n8n_health_check` endpoint 404s. Deploying it unblocks
  FORGE's "automation/infra executor" direction (today FORGE is a codebase/infra *inspector* — live
  filesystem reads only).
- **`mcp-postgres` + `mcp-github` real impls** (P2 #10) — placeholder images today (only `mcp-filesystem` is real).
- **Planner step execution** (P2 #11) — the Planner creates template plans but never executes steps; the one
  remaining substantial backend build I *could* take on solo if the operator wants one more capability first.

## Operator-only loose ends (surface, don't do)
- n8n `cora-health` webhook still uncreated; optional `DROP TABLE news_sources` (dead since v2.6, destructive).
- A harmless **duplicate memory** remains after the #7 cleanup: "Family Dog" + "Our family dog" carry the same
  content, both now under `freddie@3cpublish.com` — deletable later if desired (destructive — confirm).
- A few test conversations + smoke runs persist under `freddie@3cpublish.com` from live verifies (the
  barge-in verify self-cleans its row) — harmless internal rows; delete from the UI if desired.

## Do-not-break (invariants)
- **Fail-closed by flag:** every agent capability is gated; the outward kill switches (`AGENT_EXECUTION_ENABLED`,
  `EXTERNAL_EXECUTION_ENABLED`, `CALENDAR_EXECUTION_ENABLED`) default false; **email send is hard-disabled**.
- **Semantic routing is opt-in + fail-open** (`SEMANTIC_ROUTING_ENABLED`, default off). It only fires when
  keyword routing scores 0 AND no intent override matched; any failure/unknown label stays on Cora. It must
  NEVER override a deterministic keyword/intent match — only move off the persona.
- **Backends config-gated + reversible** (code default `ollama`); `DGX_CHAT_BACKEND` and `DGX_AGENT_BACKEND`
  are INDEPENDENT. New `DGX_*`/`AGENT_*`/feature flags need a compose passthrough (cora-api AND cora-worker).
- **Agent loop:** hub-and-spoke (only the orchestrator gets `delegate_to`; spokes are `allowed_agents`-scoped,
  depth-1); evaluator is tool-less + advisory; the eval gate blocks the DECISION, not the firing.
- **`resolve_interrupt` (and `resolve_pending_for_session`) fire nothing** unless `AGENT_EXECUTION_ENABLED` is
  on (then only staged calendar create/update/delete via `_write_gate`; email never sent).
- **Agent prompts are runtime-versioned** (`agent_versions`, DB active version preferred over the module
  constant via `resolve_agent_prompt`/`_load_spokes`). To change a LIVE agent prompt you add a new active
  version — see the idempotent `registry._ensure_prompt_revision` (no-clobber; preserves routing keywords).
- **Read-only tool args are filtered** to the tool's advertised schema before dispatch (`_dispatch_read_only`)
  — don't reintroduce raw-arg passthrough (models invent params the MCP server rejects).
- **Streaming cancellation:** a mid-stream disconnect is a normal path — `_event_stream` catches
  `asyncio.CancelledError` and finalizes (shielded). Don't add async cleanup under `GeneratorExit` (illegal).
- Don't recreate the postgres volume. Don't edit `cora-stack/docker-compose.yml` unless asked. Don't
  reintroduce `select_subagent` into `forge.py` (routing lives in `app/agents/routing.py`).

## Working rules (saved feedback)
- **No clarifying/direction-choosing questions** (incl. `AskUserQuestion` option menus) — proceed autonomously
  from context, report tersely, no pre-action plans / interim narration. The ONLY carve-out is confirming
  genuinely destructive/irreversible OR outward-facing actions (real calendar/inbox writes, pushing to `main`,
  destructive DB mutations). ([[feedback_no_questions]], [[feedback_inapp_test_steps]])
- **Per-item workflow:** build → `python3 -m py_compile` (+ `tsc -b` in the cora-ui Docker build) →
  `docker compose build <svc> && up -d <svc>` → run the relevant `scripts/verify_*.py` IN-CONTAINER
  (`docker cp …:/tmp/v.py && docker exec -e PYTHONPATH=/app cora-api python /tmp/v.py`) + a route smoke when it
  touches a route → commit on a `feat/`/`fix/` branch → report with concrete in-app test steps → on **"push"**,
  FF `main` + push + delete the branch.
- **36 `scripts/verify_*.py`** cover the suite (deterministic, in-container). Behavioral `/chat` testing needs
  an operator JWT (browser DevTools → any API call's `Authorization: Bearer …`), OR mint one in-container
  (`app.auth.create_access_token` for a real user) for local smokes — `/auth/register` is admin-locked.
  Note: `docker exec` heredocs to `python -` need `-i` or they read empty stdin — use `docker cp` of a file.
- Keep `HANDOFF_SESSION.md` + `AIOS_CORE_ARCHITECTURE.md` §9 + `VOICE_UI_READINESS.md` + these memories current
  as work lands.

## Suggested skills
- `/run` — launch/drive the app. `/verify` — confirm a change by real behavior. `/code-review` — review the
  diff (`/code-review ultra` for a deep cloud pass). `/handoff` — regenerate this doc as work continues.
