# Next Session — First Message

Continuing the **Cora AI OS** build. The pre-UI capability phase is **complete** and the **voice-first UI
v1 has shipped** — talk to Cora and hear her answer, with barge-in (browser Web Speech API). This session
landed 5 capabilities: semantic routing fallback, memory cleanup + spoken disambiguation, barge-in
cancellation, whole-plan execution, and the voice UI v1. Everything is on `main`. Deeper detail lives in
code docstrings, the commits, `AIOS_CORE_ARCHITECTURE.md` §9 (entries "Whole-plan execution + voice-first
UI v1" + "Voice-readiness close-out" + "Voice-first UI readiness"), `HANDOFF_SESSION.md`,
`VOICE_UI_READINESS.md`, and the auto-memories `agent_runtime_build` + `dgx_inference_backends` +
`project_voice_ui_readiness` (do NOT re-summarize or rebuild shipped work).

## Git / deploy state (verify first)
- **Everything is on `main` — local `main` == `origin/main`.** This session's HEAD is **`0294803`** (voice
  UI v1); a docs-refresh commit sits one ahead (this doc). This session's commits, newest first: `0294803`
  voice UI v1 · `aaba2a9` whole-plan execution · `a5cefb6` docs · `47e4481` barge-in cancellation · `8c704f0`
  memory disambiguation · `f5c9676` semantic routing. Prior session (also on main): `aebc510` … `a2721d8`
  (the 6 voice-readiness capabilities). No feature branches remain. Quick check: `git log --oneline -12`,
  `docker compose ps`.
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
  `ChatRequest.speakable` (TTS-friendly reply). The voice UI sets these per turn (mic → stream; voice toggle
  → speakable).
- **Feature flags (default OFF; compose passthrough wired for cora-api + cora-worker):**
  `SEMANTIC_ROUTING_ENABLED` (LLM routing fallback) and `PLAN_EXECUTION_ENABLED` (whole-plan run). Flip to
  `true` in `.env` + `up -d cora-api cora-worker` to enable.

## What shipped this session — DON'T rebuild (all on `main`)
Reference, don't re-derive. Each built → compile → `compose build`+`up -d` → in-container `verify_*` (or
tsc for UI) → committed → pushed.
1. **Semantic routing fallback** (`f5c9676`) — keyword routing scores 0 + no intent override → one LLM
   classification (`routing.semantic_route`) picks a specialist. Opt-in `SEMANTIC_ROUTING_ENABLED`, fail-open,
   never overrides a deterministic match. Embeddings measured + rejected (flat nomic-embed baseline). gpt-oss
   needs a 256-token budget. `verify_semantic_routing.py`.
2. **Memory cleanup + spoken disambiguation** (`8c704f0` + live DB fix) — 3 personal facts re-scoped
   `global → user` under `freddie@3cpublish.com`; 15 test-junk globals deleted. Same-title/different-content
   recall asks ONE "which?" (`app/memory/disambiguation.py` → `_format_memory_block`; high-precision).
   `verify_memory_disambiguation.py`.
3. **Barge-in / generation cancellation** (`47e4481`) — a mid-stream disconnect → `_event_stream` catches
   `asyncio.CancelledError` → `_finalize_cancelled` (shielded): aborts upstream gen, writes a `cancelled`
   trace, persists the partial. `verify_chat_cancel.py`.
4. **Whole-plan sequential execution** (`aaba2a9`) — `POST /plans/{id}/execute` (behind `PLAN_EXECUTION_ENABLED`)
   enqueues one `execution_plan` job; the worker `execute_plan` runs steps in order through the EXISTING
   governed `execute_plan_step` (a tool-less template step simulates), `planned → running → completed`,
   halt-on-failure, idempotent/resumable. Distinct from the model-driven `agent_runtime` (runs a defined,
   editable plan). `verify_plan_execution.py`.
5. **Voice-first UI v1** (`0294803`) — talk + hear, with barge-in, on the browser Web Speech API behind a
   swappable wrapper (`apps/cora-ui/src/voice/speech.ts`). 🎤 mic (tap-to-talk → STT → send), 🔈 voice toggle
   (spoken replies → `speakable:true` + TTS read-aloud), barge-in (a new turn / mic tap aborts the in-flight
   stream via `sendChatStream`'s new `AbortSignal` + cancels speech). Controls render only where supported;
   text UX unchanged when off. tsc-clean; mic/TTS is in-browser (Chromium/Safari).

## 🛠️ Recommended next (operator-steered)
The solo-buildable backlog is exhausted; the voice UI exists. The highest-value next steps:
- **Cloud STT/TTS swap** — the browser Web Speech engines are Chromium/Safari-only and variable quality.
  Pick a provider (e.g. Deepgram / Whisper for STT, ElevenLabs / Azure for TTS) and swap the wrapper
  (`apps/cora-ui/src/voice/speech.ts` — already abstracted; only `createRecognizer`/`speak`/`cancelSpeech`
  change). NEEDS the operator's provider choice (keys, cost).
- **Voice UI v2 polish (buildable solo)** — sentence-chunked TTS that speaks AS the reply streams (not on
  `done`, the current v1 behavior), continuous/wake-word listening, a dedicated full-screen voice mode,
  interim-transcript display while listening.
- **Operator-only:** email-send stance for voice (hard-disabled — policy call, don't flip unprompted); n8n
  deploy (unblocks FORGE-as-executor); real `mcp-postgres`/`mcp-github` (placeholder images).

## Operator-only loose ends (surface, don't do)
- n8n `cora-health` webhook still uncreated; optional `DROP TABLE news_sources` (dead since v2.6, destructive).
- A harmless **duplicate memory** remains after the #7 cleanup: "Family Dog" + "Our family dog" (same content),
  both under `freddie@3cpublish.com` — deletable later (destructive — confirm).
- Test conversations + smoke rows persist under `freddie@3cpublish.com` from live verifies (the barge-in +
  plan-execution verifies self-clean their rows) — harmless; delete from the UI if desired.

## Do-not-break (invariants)
- **Fail-closed by flag:** every agent/execution capability is gated; the outward kill switches
  (`AGENT_EXECUTION_ENABLED`, `EXTERNAL_EXECUTION_ENABLED`, `CALENDAR_EXECUTION_ENABLED`) default false;
  **email send is hard-disabled**. `SEMANTIC_ROUTING_ENABLED` + `PLAN_EXECUTION_ENABLED` are opt-in (default off).
- **Plan execution reuses governance, adds no outward path** — `execute_plan` loops the EXISTING
  `execute_plan_step` (check_permission + dispatch_tool); `PLAN_EXECUTION_ENABLED` gates only the
  auto-sequencing. It is NOT a second agent engine — don't merge it with `agent_runtime`.
- **Semantic routing is opt-in + fail-open**; only fires when keyword routing scores 0 AND no intent override
  matched; never overrides a deterministic match — only moves off the Cora persona.
- **Voice STT/TTS is swappable** — `App`/`ChatPanel` import only from `apps/cora-ui/src/voice/speech.ts`;
  swap providers THERE, don't scatter Web Speech calls. The backend voice contract is `stream` + `speakable`
  + clean mid-stream cancellation; keep those stable.
- **Streaming cancellation:** a mid-stream disconnect is a normal path — `_event_stream` catches
  `asyncio.CancelledError` and finalizes (shielded). Don't add async cleanup under `GeneratorExit` (illegal).
- **Backends config-gated + reversible** (code default `ollama`); `DGX_CHAT_BACKEND`/`DGX_AGENT_BACKEND` are
  INDEPENDENT. New `DGX_*`/`AGENT_*`/feature flags need a compose passthrough (cora-api AND cora-worker).
- **Agent loop:** hub-and-spoke (only the orchestrator gets `delegate_to`; spokes `allowed_agents`-scoped,
  depth-1); evaluator tool-less + advisory; the eval gate blocks the DECISION, not the firing. `resolve_interrupt`
  / `resolve_pending_for_session` fire nothing unless `AGENT_EXECUTION_ENABLED` is on.
- **Agent prompts are runtime-versioned** (`agent_versions`, DB active version preferred via
  `resolve_agent_prompt`/`_load_spokes`). Change a LIVE prompt via a new active version — see the idempotent
  `registry._ensure_prompt_revision` (no-clobber; preserves routing keywords).
- **Read-only tool args are filtered** to the advertised schema before dispatch (`_dispatch_read_only`).
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
  FF `main` + push + delete the branch. Frontend changes are gated by the tsc/vite build (no `verify_*.py`);
  behavioral voice testing is in-browser (Chromium/Safari, needs a mic).
- **37 `scripts/verify_*.py`** cover the backend suite. `/chat` behavioral testing needs an operator JWT
  (browser DevTools → any API call's `Authorization: Bearer …`), OR mint one in-container
  (`app.auth.create_access_token` for a real user) — `/auth/register` is admin-locked. Note: `docker exec`
  heredocs to `python -` need `-i` or read empty stdin — use `docker cp` of a file.
- Keep `HANDOFF_SESSION.md` + `AIOS_CORE_ARCHITECTURE.md` §9 + `VOICE_UI_READINESS.md` + these memories current.

## Suggested skills
- `/run` — launch/drive the app. `/verify` — confirm a change by real behavior. `/code-review` — review the
  diff (`/code-review ultra` for a deep cloud pass). `/handoff` — regenerate this doc as work continues.
