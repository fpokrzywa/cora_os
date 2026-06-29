# Next Session — First Message

Continuing the **Cora AI OS** build. Recent sessions moved Cora's inference onto **vLLM/gpt-oss-120b**
(chat, extraction, AND the agent runtime), completed **confirm-as-interrupt** (internal + outward) plus
the **evaluator gate**, consolidated all text-gen onto one backend, and fixed several user-reported chat
bugs. This doc is the entry point; deeper detail lives in code docstrings, the commits below,
`AIOS_CORE_ARCHITECTURE.md` §9, `HANDOFF_SESSION.md`, and the auto-memories `agent_runtime_build` +
`dgx_inference_backends` (do NOT re-summarize or rebuild shipped work).

## Git / deploy state (verify first)
- **Everything is on `main`** — local `main` == `origin/main` @ **`df63b73`**. No feature branches remain
  (each item this session FF-merged to `main` + pruned its branch). Quick check: `git log --oneline -8`,
  `docker compose ps`.
- Stack up + healthy: `cora-api`, `cora-worker`, `cora-ui`, `cora-postgres`, MCPs, `cora-searxng` — built
  from this code, so **live == `main`**.
- `gh` is NOT installed (no `GH_TOKEN`); use plain `git`. `.env` is gitignored (secrets + flags — never
  commit/echo it); it lives at the repo root `/home/owner/cora-ai-os/.env`.
- Working tree carries TWO pre-existing handoff-doc items (not this work; leave them): a staged deletion of
  `HANDOFF_CALENDAR_INBOX_SESSION.md` + untracked `HANDOFF_CHAT_VLLM_SESSION.md`.
- **DGX SSH:** the orchestration host reaches the DGX (`spark-a84c`, a Tailscale node = 100.114.254.113)
  over **Tailscale SSH** (`ssh -o IdentitiesOnly=yes -o StrictHostKeyChecking=no fpokrzywa@spark-a84c '<cmd>'`,
  key `/home/owner/.ssh/id_dgx_spark`). A Tailscale "check" grant is active (~12h windows); the operator may
  have revoked it. `docker` on the DGX needs no sudo.

## Currently LIVE config (in `.env`, NOT in git)
- **All inference on vLLM/gpt-oss-120b:** `DGX_CHAT_BACKEND=openai`, `DGX_AGENT_BACKEND=openai`,
  `DGX_OPENAI_ENDPOINT=http://spark-a84c:8000/v1`, `DGX_OPENAI_MODEL=openai/gpt-oss-120b`. Revert ALL
  text-gen to the 4B Ollama with `DGX_CHAT_BACKEND=ollama` + `DGX_AGENT_BACKEND=ollama` → `up -d cora-api
  cora-worker` (env-only, no rebuild; code default is `ollama`).
- **Agent flags ON:** `AGENT_RUNTIME_ENABLED`, `AGENT_DELEGATION_ENABLED`, `AGENT_WRITE_ENABLED`,
  `AGENT_INTERRUPT_ENABLED`, `AGENT_EVAL_ENABLED`, `AGENT_EVAL_GATE_ENABLED`, `AGENT_DELEGATION_MAX_PARALLEL=3`.
- **OFF (the outward kill switches):** `AGENT_EXECUTION_ENABLED`, `EXTERNAL_EXECUTION_ENABLED`,
  `CALENDAR_EXECUTION_ENABLED` (DB-override-toggleable in the app). Email send is hard-disabled regardless.
- ⚠️ **DGX vLLM server (`vllm-oss` container) MUST run with `--enable-auto-tool-choice --tool-call-parser
  openai`** for the agent loop's tool calls to parse (else empty `tool_calls` + `stop_reason 200012`). Set up
  this session by recreating the container (a raw `docker run`, NOT compose). See `dgx_inference_backends`.

## What shipped recently — DON'T rebuild (newest first)
Reference, don't re-derive. All on `main`.
- **Unread-inbox query** (`df63b73`) — "what do I have in my outlook that is unread" now detects → routes to
  the inbox handler → filters unread per provider (Gmail `is:unread`, Outlook `$filter=isRead eq false`).
  Was falling through to the general LLM. `verify_chat_inbox.py`. Live-verified (real 10 unread Outlook).
- **All text-gen on one backend** (`ae234b1`) — summarize / news-briefing / email-draft / agent-test-response
  now route through `app.llm.generate_text` like chat. So `DGX_CHAT_BACKEND` governs the WHOLE app's text-gen.
  Only embeddings (`nomic-embed-text`) + screen vision (`qwen2.5vl`) stay Ollama-only. `verify_text_gen_backend.py`.
- **Email drafts never signed with an agent codename** (`2b3e51a`) — drafts used the model reply verbatim, so
  "Best regards, SIGNAL" shipped. Shared `signal_tools.normalize_email_signoff` + `user_signoff_name`
  (display_name, else "Cora - the AI Assistant"); fixed in BOTH paths (`chat_email_lifecycle._h_create/_revise`
  + `routers.chat` SIGNAL draft). `verify_chat_signal_signoff.py`.
- **Evaluator-gated approval** (`8fe9f1b`, Phase 6 + 7) — `AGENT_EVAL_GATE_ENABLED` (default false): approving a
  paused run whose evaluator verdict is `fail` is refused (HTTP 409, fires nothing) unless `override=true`.
  `resolve_interrupt(override=)`; UI InterruptCard shows the verdict + "Override & approve" on 409; Eval-gate
  pill. `verify_agent_runtime.py` Part L → **71 assertions**. Live-exercised on gpt-oss-120b.
- **Agent tool loop on vLLM** (`77451cc` + `bb6f1fd`) — `agent_runtime._chat(backend, …)` is backend-selectable
  (`DGX_AGENT_BACKEND`), returns the canonical Ollama-shaped `{"message":{…}}` either way: openai path translates
  the thread (`_to_openai_messages`, synthesized tool_call ids) → `/chat/completions` (`tool_choice=auto`) →
  `_normalize_openai_response`. Evaluator follows the same backend. Part K. Live end-to-end (web_search → answer).
- **Memory delete/update accepts the short id** (`08ff3cd`) — `show memories` prints an 8-char id;
  `delete/update memory <id>` now resolves a prefix (`scribe.resolve_memory_id_prefix`, visibility-scoped).
- **Chat/vLLM + memory quality session** (`5da4955`→`3957d35`) — natural "remember this" persists; hybrid recall
  via RRF; concise + second-person answers; prompt-cache + keep-warm latency fixes; chat + the two fact-extractions
  moved to gpt-oss-120b. See `HANDOFF_CHAT_VLLM_SESSION.md`.
- **Confirm-as-interrupt OUTWARD half** (`c101eef`) + **non-admin agent panel** (`64891de`).

## Do-not-break (invariants)
- **Fail-closed by flag:** every agent capability is gated by its `AGENT_*` flag; the outward kill switches
  (`AGENT_EXECUTION_ENABLED`, `EXTERNAL_EXECUTION_ENABLED`, `CALENDAR_EXECUTION_ENABLED`) default false; **email
  send is hard-disabled** (no send code path exists).
- **Backends are config-gated + reversible** (default `ollama` in code). `DGX_CHAT_BACKEND` and
  `DGX_AGENT_BACKEND` are INDEPENDENT. `llm.generate_text` raises `httpx.HTTPError` on transport so existing
  handlers keep working. New `DGX_*`/`AGENT_*` flags need a compose passthrough (cora-api AND cora-worker) to reach
  the container.
- **Agent loop:** hub-and-spoke (only the orchestrator gets `delegate_to`; spokes are `allowed_agents`-scoped,
  depth-1); the evaluator is tool-less + advisory; the eval gate blocks the DECISION, not the firing.
- **resolve_interrupt fires nothing** unless `AGENT_EXECUTION_ENABLED` is on (then only the staged calendar CREATE,
  via the existing `_write_gate`; email never sent). Calendar gated by `CALENDAR_EXECUTION_ENABLED` + per-provider
  `calendar_write` + confirm-before-write.
- **Switches in the app are tiered:** `calendar_execution_enabled` + `screen_vision_enabled` + per-provider feature
  flags are admin-toggleable (DB override over env, `runtime_switches`); `external_execution_enabled` is env-locked
  (read-only in app); the `AGENT_*` flags are env-only + read-only status pills (no UI toggle).
- Don't recreate the postgres volume. Don't edit `cora-stack/docker-compose.yml` unless asked.

## 🛠️ Build backlog (operator picks)
1. **Live calendar firing (outward — needs the operator + a throwaway event)** — the last unverified-live step:
   enable `AGENT_EXECUTION_ENABLED` + `CALENDAR_EXECUTION_ENABLED` (interrupt + write already on) and confirm a
   staged calendar CREATE fires on approve, on a THROWAWAY event. The machinery exists (`resolve_interrupt` →
   `_fire_staged`); this is the live confirmation. Highest care.
2. **Calendar update/delete firing** — the agent only fires CREATE today; extend to update/delete under the same gates.
3. **Global-memory recall noise** — some demo/global memories rank into a user's personal recall; a scoping/cleanup
   pass would sharpen day-to-day chat. Small, safe, code-only.
4. **`/chat` SSE streaming** — frontend + backend, for snappier perceived latency.
5. **App-config-screen relocation under Cora Configuration** ("option 2") — UI polish.

## Operator-only loose ends (surface, don't do)
- `vllm-oss-prev` was already removed this session. If the DGX vLLM is ever restarted/rebooted, re-confirm it
  still has `--enable-auto-tool-choice --tool-call-parser openai`.
- The HF token used to relaunch `vllm-oss` was pasted in chat earlier; the operator said they rotated it
  (gpt-oss-120b is ungated, so it doesn't affect the running server).
- n8n `cora-health` webhook still uncreated (`n8n_health_check` 404s until it exists); optional `DROP TABLE
  news_sources` (dead since v2.6, destructive — confirm first).

## Working rules (saved feedback)
- **No clarifying/direction-choosing questions** (incl. `AskUserQuestion` option menus) — proceed autonomously
  from context, report tersely, no pre-action plans / interim narration. The ONLY carve-out is confirming
  genuinely destructive/irreversible OR outward-facing actions (real calendar/inbox writes, pushing to `main`).
  ([[feedback_no_questions]], [[feedback_inapp_test_steps]])
- **Per-item workflow:** build → `python3 -m py_compile` (+ `tsc -b` runs in the cora-ui Docker build) →
  `docker compose build <svc> && up -d <svc>` (image is baked — a rebuild deploys code; env-only flag flips need
  just `up -d`) → run the relevant `scripts/verify_*.py` IN-CONTAINER
  (`docker cp …:/tmp/v.py && docker exec -e PYTHONPATH=/app cora-api python /tmp/v.py`) + a route smoke when it
  touches a route → commit on a `feat/`/`fix/` branch → report with concrete in-app test steps → on **"push"**,
  FF `main` + push + delete the branch.
- **26 `scripts/verify_*.py`** cover the suite (deterministic, in-container; `verify_agent_runtime.py` = 71
  assertions Parts A–L). Behavioral `/chat` testing needs an operator JWT (browser DevTools → any API call's
  `Authorization: Bearer …`); `/auth/register` is admin-locked.
- Keep `HANDOFF_SESSION.md` + these memories current as work lands (update, don't just append):
  `agent_runtime_build`, `dgx_inference_backends`.

## Suggested skills
- `/run` — launch/drive the app to see a change working.
- `/verify` — confirm a change does what it should by observing real behavior.
- `/code-review` — review the working diff before committing (`/code-review ultra` for a deep cloud pass).
- `/handoff` — regenerate this doc as work continues.
