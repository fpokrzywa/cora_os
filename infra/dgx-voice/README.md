# DGX voice service (cora-voice) — vendored + brain-swapped

The Pipecat voice pipeline that runs on the DGX (`spark-a84c`, systemd unit
`cora-voice`, code at `~/cora/`): WebRTC transport → Silero VAD → Whisper
large-v3-turbo STT → transcript cleanup (local Ollama) → **LLM** → Kokoro-82M
TTS. Originally from the Cora_2 project (`/home/owner/Cora_2/voice/`); vendored
here because cora-ai-os now owns its evolution.

## The brain swap

Upstream, the LLM stage was the DGX-local `cora-qwen3:4b` (Ollama). The swap
points it at **cora-api's OpenAI-compatible façade**
(`POST /v1/chat/completions`, `apps/cora-api/app/routers/openai_compat.py`) so
every voice turn runs the REAL Cora pipeline: ATLAS routing, memory recall,
governance, traces, `speakable` replies, and barge-in cancellation. Pipecat's
`OpenAILLMService` needs no code changes — the swap is env config plus one
small patch in `phase1_push_to_talk.py`:

- `CORA_LLM_SESSION_HEADERS=1` → sends `X-Cora-Session-Id` (fresh per WebRTC
  connection → one Cora conversation per voice session) and
  `X-Cora-Speakable` headers to the façade. Off (default) = stock behavior.
- `CORA_CLEANUP_BASE_URL` → pins the transcript-cleanup pass to the local
  Ollama when `CORA_LLM_BASE_URL` points at cora-api.

Auth: the façade takes a normal cora-api JWT as the OpenAI `api_key`
(`CORA_LLM_API_KEY`). Sentence-level TTS streaming and interruptions are
native Pipecat behavior — a barge-in aborts the HTTP stream, which cora-api
finalizes as a `cancelled` turn.

## Deploy / cutover

```bash
bash infra/dgx-voice/install-voice-token.sh   # mint 365d JWT + write DGX env (backs up first)
bash infra/dgx-voice/deploy.sh                # backup + copy patched pipeline + restart
```

Verify: the service log's `[llm]` banner shows
`base_url=https://cora.tail343b33.ts.net:8443/v1 … session_headers=on`.

## Backout (env-only)

On the DGX: restore the newest `~/.config/cora/env.bak-*` over
`~/.config/cora/env` (and, if needed, `~/cora/phase1_push_to_talk.py.bak-pre-cora-api`
over the pipeline), then `sudo systemctl restart cora-voice`. The voice
reverts to the local-Qwen brain.

## Known behavior changes vs the old brain

- The voice's UI tool-calls (`open_card`, browser actions over the data
  channel) no longer fire — the façade emits no tool_calls; cora-api's own
  governed tools act server-side instead.
- Screen-context/voice-config fetches still target the retired cora_v2 app
  (`CORA_V2_URL`) and fall back gracefully; harmless, removable later.
