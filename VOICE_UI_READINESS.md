# Voice-First UI Readiness — Capability Backlog

Goal: get every agent/capability solid **before** a voice-first UI sits on top of Cora.
Voice changes the requirements vs a click UI:
- **No pasting** — every "give me the id / paste the event" flow breaks (→ NL discovery).
- **No clicking** — confirm-as-interrupt must work by *spoken* yes/no, not an InterruptCard.
- **Latency is visible** — multi-step agent runs and long replies are felt; replies must be short + speakable.
- **Spoken phrasing varies** — keyword routing is brittle against disfluencies/synonyms.
- **No lists on screen** — ambiguity ("which Dorothy?") must resolve by spoken disambiguation.

Ranking = leverage (for voice) × effort. Effort: **S** ≈ hours, **M** ≈ ~a day, **L** ≈ multi-day.

---

## Already done (de-risks voice)
- ✅ `/chat` **SSE streaming** — token-by-token; first-token latency is the lever voice needs (`a2721d8`).
- ✅ Governed integrations: Calendar R/W (Google + MS, live), Inbox read (Gmail + Outlook, live).
- ✅ Memory: pgvector, chunked, hybrid RRF recall, scope-aware.
- ✅ Agent kernel: tool loop + delegation + evaluator + confirm-as-interrupt + full audit traces.
- ✅ Inference: vLLM/gpt-oss-120b (chat + agent), Ollama (embeddings + vision).

---

## P0 — voice is broken without these

| # | Item | Why voice needs it | Agent | Effort | Done when |
|---|------|--------------------|-------|--------|-----------|
| 1 | **CHRONOS calendar READ tool** (`chronos_list_calendar_events`, governed, read-gated) | "Cancel my 3pm" needs the agent to *discover* the event; voice can't paste an `event_id` | CHRONOS | M | Agent resolves an event from NL time/title and stages an update/delete on it; `verify_chat_calendar` extended |
| 2 | **Spoken confirm-as-interrupt** | The only approval path today is the UI card. Voice needs: (a) a concise *spoken* "About to cancel your 3pm with Dorothy — yes?" generated from the staged action, (b) the next-turn "yes/no" routed to `resolve_interrupt`, not treated as a new chat turn | runtime | M | A pending interrupt is summarized in one speakable line; "yes/approve" fires, "no" cancels; eval-gate still applies |
| 3 | **FORGE capability** (currently persona-only) | If a voice user says "build/run/deploy X", FORGE must *do* something or degrade gracefully. Default direction: **automation/infra executor** via governed n8n trigger + health/status reads (matches n8n = automation layer) | FORGE | M–L | FORGE owns ≥1 real governed read tool (status) + 1 staged/confirmed write (trigger workflow), under the same flags |

## P1 — needed for a *good* first voice cut

| # | Item | Why voice needs it | Agent | Effort | Done when |
|---|------|--------------------|-------|--------|-----------|
| 4 | **Speakable-response discipline** | TTS reads markdown tables/links/`**` literally; long replies feel slow | all/chat | S–M | System prompt + light output normalization yield concise, link/table-free spoken text; streaming breaks on sentence boundaries |
| 5 | **PULSE prompt/capability fix** | Prompt says "no live web access" while `web_search` is wired — confuses the model and any UI copy | PULSE | S | Prompt matches reality; recency questions reliably use the governed tool |
| 6 | ✅ **Semantic routing fallback** (`f5c9676`) | Keyword routing misses spoken synonyms/disfluencies | ATLAS | M | **DONE** — keyword score 0 + no intent override → one LLM classification (`routing.semantic_route`) picks the specialist; opt-in `SEMANTIC_ROUTING_ENABLED`, fail-open, deterministic path unchanged. Embeddings measured + rejected (flat baseline). `verify_semantic_routing.py` |
| 7 | ✅ **Memory cleanup + spoken disambiguation** (`8c704f0`) | Mis-scoped global facts leak across accounts; voice can't show a 5-option list | SCRIBE | S + M | **DONE** — 3 personal facts re-scoped `global → user` under `freddie@3cpublish.com`; all 15 test-junk globals deleted; same-title/different-content recall asks one spoken "which?" (`app/memory/disambiguation.py`). `verify_memory_disambiguation.py` |

## P2 — important, can trail the first voice cut

| # | Item | Why | Effort |
|---|------|-----|--------|
| 8 | ✅ **Generation cancellation / barge-in** (`47e4481`) — mid-stream disconnect aborts the upstream vLLM gen (httpx teardown) + records a `cancelled` trace + persists the partial | Barge-in (user talks over Cora) needs a real stop | S–M — **DONE** (`verify_chat_cancel.py`) |
| 9 | **Decide email-send stance for voice** — send is architecturally absent; "email Bob" will always refuse. Keep blocked + graceful refusal, or open a governed staged path | Product stance | S (decision) |
| 10 | **MCP postgres/github** — currently placeholder images | Only if voice exposes DB/repo Q&A | L (defer) |
| 11 | **Planner step execution** — template-only stub today | Only if voice surfaces runnable "plans" | L (defer) |

---

## Recommended build order
1. **#1 CHRONOS read** — highest leverage, already scoped, contained.
2. **#2 Spoken confirm** — unlocks every outward action by voice; reuses the existing stage/fire machinery.
3. **#4 Speakable responses** + **#5 PULSE prompt** — cheap, broad polish; do together.
4. **#3 FORGE** — give the empty agent a real (governed, minimal) surface.
5. **#6 routing** + **#7 memory cleanup/disambiguation**.
6. P2 as the voice layer firms up.

## Status — pre-UI capability phase COMPLETE (2026-06-30)
P0 (#1–3), P1 (#4–7), and the one buildable P2 item (#8) are all shipped + live on `main` @ `47e4481`.
Remaining P2 is operator/deferred: **#9** email-send stance (policy call — still hard-disabled), **#10**
MCP postgres/github (placeholder images), **#11** Planner step execution (template stub). Plus **n8n is
not deployed**, which keeps FORGE a codebase/infra *inspector* rather than an n8n executor. **Next phase:
the voice-first UI client itself** (mic → STT → `/chat` SSE stream → TTS → barge-in) — the backend now
supports it end-to-end.

## Resolved decisions
- **FORGE direction** → shipped as a **codebase/infra inspector** (live filesystem reads); the n8n-executor
  direction stays deferred until n8n is deployed (operator).
- **Memory re-scoping target account** → **`freddie@3cpublish.com`** (the account holding all 19 memories);
  the 3 personal facts were re-scoped there and #7's cleanup is done.
