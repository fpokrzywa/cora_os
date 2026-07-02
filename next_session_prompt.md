# Next Session — First Message

Continuing the **Cora AI OS** build. This session shipped the **Cora_2 voice shell as a second UI
(`cora-ui2`)**, the **voice brain-swap** (her voice now runs the REAL cora-api pipeline via a new
OpenAI-compatible façade — it had been answering from cloud Claude Haiku), a **22-finding latency
overhaul** (session start ~10s→1-2s, local Whisper STT, parallel provider reads), account-backed UI
settings, and conversation-aware voice auto-close. Deep detail lives in the commit messages,
`AIOS_CORE_ARCHITECTURE.md` §9 (entry "Cora_2 voice shell + brain swap + latency overhaul"),
`infra/dgx-voice/README.md`, and the auto-memories `cora_ui2_voice_shell` + `dgx_inference_backends` +
`agent_runtime_build` (do NOT re-summarize or rebuild shipped work).

## Git / deploy state (verify first)
- ⚠️ **13 commits sit on branch `feat/cora-ui2-voice-shell` @ `6d56e24` — NOT pushed, NOT on `main`.**
  `main` @ `7e1cc91` == `origin/main`. **The deployed stack RUNS THE BRANCH** (every item was
  compose-built + `up -d`). On the operator's **"push"**: FF `main`, push, delete the branch.
  Commits newest-first: `6d56e24` barge-in follow-up fix · `bbb564c` prefix-cache-stable workspace line ·
  `ae120f3` latency tranche 1 · `a103ba1` 4B voice rewrite + mailbox detection · `fbcdfba` spoken-prose
  rewrite · `bf91cc3` handler-reply stream fix (all 3 consumers) · `243f63a` script nit · `76f2a48`
  brain-swap kit · `8a0b93b` voice idle guard · `1b80349` ui-prefs settings sync · `0ae1f07`/`058c049`
  HTTPS serving · `5f74c84` cora-ui2 port. Quick check: `git log --oneline -15`, `docker compose ps`.
- Stack healthy: `cora-api`, `cora-worker`, `cora-ui` (classic, untouched default), **`cora-ui2`** (new),
  `cora-postgres`, MCPs, `cora-searxng`. `gh` NOT installed; plain `git`. `.env` gitignored (never echo).
- Pre-existing working-tree items to LEAVE: staged deletion of `HANDOFF_CALENDAR_INBOX_SESSION.md`,
  untracked `HANDOFF_CHAT_VLLM_SESSION.md`.
- **DGX SSH works** (Tailscale SSH re-authed this session; check period may lapse again → the operator
  must approve the printed login.tailscale.com link):
  `ssh -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -i /home/owner/.ssh/id_dgx_spark fpokrzywa@spark-a84c '<cmd>'`.
  `docker` no sudo; `sudo -n systemctl` WORKS passwordless. **Remote writes to the DGX + credential
  installs are permission-gated** — get the operator's explicit go-ahead phrase first (precedent:
  "deploy the voice swap").

## The voice stack as deployed (all LIVE right now)
- **Browser** (`https://cora.tail343b33.ts.net:10000`, or `http://ui2.cora.local.arpa` sans mic) →
  WebRTC → **DGX `cora-voice.service`** (`~/cora`, Pipecat 1.1.0): Silero VAD (`CORA_VAD_STOP_SECS`,
  default 0.4) → **local Whisper large-v3-turbo** (~155ms; `CORA_STT_PROVIDER=whisper`, flip-back
  `=deepgram` one-liner) → `OpenAILLMService` → **cora-api façade** `POST /v1/chat/completions`
  (`https://cora.tail343b33.ts.net:8443/v1`, 365d JWT as api_key, `X-Cora-Session-Id` per connection +
  `X-Cora-Speakable` via env-gated `CORA_LLM_SESSION_HEADERS=1`) → Kokoro TTS (af_heart). Transcript
  cleanup OFF (`CORA_CLEANUP_ENABLED=0`).
- **Façade** (`app/routers/openai_compat.py`): full pipeline per turn (routing/memory/governance/traces);
  handler short-circuits delivered as one-shot chunks; list-like voice replies rewritten to spoken prose
  on the DGX-local 4B (`VOICE_REWRITE_MODEL=cora-qwen3:4b`, `/no_think`, fail-open `to_speakable`).
- **Voice acts as `freddie@3cpublish.com`** (the memories account) — voice sessions appear in that
  account's conversation list; one cora-api session per WebRTC connection.
- **tailscale serve** on this host (`cora`): 443 is BLOCKED by NPM's wildcard bind (don't retry without
  freeing it); `:10000` → 127.0.0.1:8082 (cora-ui2), `:8443` → 127.0.0.1:8000 (cora-api).
- **Backout paths** (all env-only, documented in `infra/dgx-voice/README.md`): DGX env backups
  `~/.config/cora/env.bak-*`, pipeline backups `~/cora/*.bak-pre-cora-api`, restart `cora-voice`.
- Measured server-side TTFB (post-overhaul): general ~2.0-2.3s, calendar ~1.0-1.2s, briefing ~3.4-3.7s,
  inbox ~5s (slowest provider bound). Timing harness: scratchpad `time_turns.py` pattern (docker cp +
  exec in cora-api; self-cleans its conversation).

## Currently LIVE config deltas (this session)
- **cora-api/.env & compose:** unchanged flags from before, plus compose passthrough
  `VOICE_REWRITE_MODEL` (default `cora-qwen3:4b`); `cora-ui2` service env `CORA_API_URL`
  (default `https://cora.tail343b33.ts.net:8443`); cora-api CORS now allows `ui2.cora.local.arpa`,
  `cora.tail343b33.ts.net`(+`:10000`). Embeddings pin `keep_alive=24h`, timeout 8s.
- **DGX `~/.config/cora/env`** (NOT in git; token inside — never print): `CORA_LLM_*` → façade,
  `CORA_LLM_SESSION_HEADERS=1`, `CORA_CLEANUP_BASE_URL=http://localhost:11434`,
  `CORA_CLEANUP_MODEL=cora-qwen3:4b`, `CORA_CLEANUP_ENABLED=0`, `CORA_STT_PROVIDER=whisper`.
- **vLLM (`vllm-oss` on DGX):** UNCHANGED this session — prefix caching was already on (V1 default);
  the required `--enable-auto-tool-choice --tool-call-parser openai` flags remain load-bearing.
- **NPM:** proxy host `ui2.cora.local.arpa` → `cora-ui2:80` (operator-created; an NPM nginx reload was
  needed once — `docker exec nginx-proxy-manager nginx -s reload` if a new host serves the default page).

## 🛠️ Open work (operator-steered, in rough value order)
1. **Push decision** — 13 branch commits await "push" (FF main + push + delete branch).
2. **Inbox drill-down** (designed, offered): "tell me more about the one from Sarah" — stash listed
   messages as session context (mirror `chat_calendar._stash_list_context`), add follow-up detection,
   governed single-message/thread read, spoken via the existing rewrite. Read-only, same gates.
3. **History-order bug (correctness, awaiting greenlight)** — `chat.py` `_load_recent_history` returns
   the OLDEST 10 messages (`ORDER BY created_at ASC LIMIT`); long chats feed the model their opening
   turns. Fix = DESC + reverse. Changes behavior on every long session — operator must approve.
4. **Speech-overlap tranche** (the "in the room" move): start memory recall/routing/prefill on interim
   transcripts while the user is still speaking; spoken acknowledgments for slow handler turns need a
   data-channel "turn in flight" signal so auto-close cooperates. Pipecat supports the pattern.
5. **VAD 0.3** — now just `CORA_VAD_STOP_SECS=0.3` on the DGX + restart; watch for utterance-splitting.
6. **Whisper quality watch** — if she mishears more than Deepgram did: re-enable cleanup
   (`CORA_CLEANUP_ENABLED=1`, +~200ms) or flip back (`CORA_STT_PROVIDER=deepgram`).
7. **NPM-443 cleanup** (port-free `https://cora.tail343b33.ts.net`) — requires editing
   `cora-stack/docker-compose.yml` (LAN-restrict/remove NPM's 443 bind) — explicit approval required.
8. Standing operator items: email-send stance (hard-disabled), n8n deploy, real mcp-postgres/github.

## Do-not-break (adds to the standing invariants — see AIOS §9 + prior prompt for the full list)
- **Fail-closed flags unchanged**: `AGENT_EXECUTION_ENABLED`/`EXTERNAL_EXECUTION_ENABLED` off; email
  send hard-disabled; kill switches gate everything outward. Backends env-gated + reversible.
- **cora-ui (classic) stays intact as the default UI**; ui2 is additive. Which is "default" = NPM
  forward-target. The ui2 shell files are VENDORED copies — keep all cora-api mapping in `adapter.js`
  (one edit each in index.html/shell.js are annotated `cora-ui2 addition`).
- **The façade reuses `chat()` — never fork the pipeline.** Handler short-circuits return plain
  `ChatResponse` even with `stream:true`; every stream consumer must handle the JSON body (façade,
  adapter, classic `api.ts` all do — keep it that way).
- **DGX voice service**: `infra/dgx-voice/` is the source of truth; deploy via `deploy.sh` (backs up +
  compile-checks + restarts). The voice JWT lives ONLY in the DGX env file — never in git/logs; re-mint
  via `install-voice-token.sh`. DGX writes need operator authorization (permission-gated).
- **Prefix-cache hygiene**: keep the prompt prefix byte-stable (system → workspace → …); the workspace
  counts are bucketed for this reason — don't reintroduce exact/changing values early in the prompt.
- **`docker exec` heredocs need `-i`**; container `/tmp` is wiped on recreate (re-`docker cp` scripts).
- Don't recreate the postgres volume; don't edit `cora-stack/docker-compose.yml` unless asked.

## Working rules (saved feedback — unchanged)
- **No clarifying/direction-choosing questions**; proceed autonomously, report tersely, end every
  delivery with concrete in-app test steps. Confirm only destructive/irreversible/outward actions
  (DGX writes + credential installs + pushing `main` + NPM/cora-stack edits are in that class).
- **Per-item workflow:** build → `py_compile`/`node --check` → `compose build && up -d` → in-container
  `verify_*.py` (39 scripts now: +`verify_ui_prefs`, +`verify_openai_compat`) → commit on the feature
  branch → report with test steps → on "push", FF `main` + push + delete branch.
- Voice behavioral testing is operator-only (needs a mic); server-side timing via the `time_turns.py`
  pattern; DGX voice log: `~/cora/logs/cora-voice.log` (banner lines `[config] …`).
- Keep `AIOS_CORE_ARCHITECTURE.md` §9, this file, `HANDOFF_SESSION.md`, and the auto-memories current.

## Suggested skills
- `/verify` — confirm changes by real behavior (route smokes, in-container scripts).
- `/run` — launch/drive the app when a change needs eyes on it.
- `/code-review` — review the branch diff before the operator's push (13 commits; `ultra` for depth).
- `/handoff` — regenerate this document as the work continues.
