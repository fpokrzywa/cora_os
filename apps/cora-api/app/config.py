from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    cora_env: str = "development"
    database_url: str = "postgresql://cora:cora@postgres:5432/cora"
    redis_url: str = "redis://redis:6379/0"
    dgx_model_endpoint: str = ""
    dgx_model_name: str = ""

    # Phase 1 agent kernel (tool-calling loop). dgx_chat_model_name is the
    # tools-capable chat model served on the DGX (Ollama /api/chat); falls back
    # to dgx_model_name when unset. Fail-closed: the runtime serves only when
    # agent_runtime_enabled is true, and only the curated READ-ONLY catalog is
    # reachable. max_steps bounds the reason→act→observe loop.
    dgx_chat_model_name: str = ""
    # How long the DGX (Ollama) keeps the chat model loaded after a request. The
    # default 5m means sporadic chats reload the model AND lose the prompt cache,
    # paying a full cold prefill (~15s) every time. Keep it warm so warm prefill
    # (~0.3s) is the norm; "-1" pins it loaded indefinitely.
    dgx_keep_alive: str = "30m"
    # Chat-model backend for the user-facing chat + fact-extraction. "ollama" (the
    # DGX native /api/generate, default) or "openai" (an OpenAI-compatible server
    # such as vLLM serving gpt-oss-120b at dgx_openai_endpoint). Flipping to "openai"
    # routes chat + memory/calendar extraction to that model; embeddings, vision, and
    # the agent-runtime tool loop stay on Ollama regardless. See app/llm.py.
    dgx_chat_backend: str = "ollama"
    dgx_openai_endpoint: str = ""   # e.g. http://spark-a84c:8000/v1
    dgx_openai_model: str = ""      # e.g. openai/gpt-oss-120b
    dgx_openai_api_key: str = ""    # optional; vLLM usually needs none
    # Inference backend for the agent-runtime TOOL LOOP (and its evaluator):
    # "ollama" (DGX native /api/chat, default) or "openai" (an OpenAI-compatible
    # server such as vLLM, reusing dgx_openai_endpoint/model). Independent of
    # dgx_chat_backend so the loop migrates/rolls back on its own. Fail-safe default
    # ollama — a fresh .env keeps the proven behavior until flipped. See app/agent_runtime.py.
    dgx_agent_backend: str = "ollama"
    agent_runtime_enabled: bool = False
    agent_runtime_max_steps: int = 6

    # Phase 2 hub-and-spoke delegation. When true (AND agent_runtime_enabled),
    # the orchestrator run gains a delegate_to tool that hands a self-contained
    # subtask to a registry specialist (FORGE/PULSE/SIGNAL/CHRONOS), which runs
    # as its own scoped spoke run and returns its answer. Fail-closed: off ->
    # the orchestrator is a plain single agent (Phase 1 behavior).
    agent_delegation_enabled: bool = False

    # Phase 4: max spokes that run concurrently within one orchestrator turn
    # (asyncio fan-out; spoke model calls are I/O-bound on the DGX so they
    # overlap). Bounds DGX load and concurrent delegations.
    agent_delegation_max_parallel: int = 3

    # Phase 5: let the agent STAGE review-only artifacts (email drafts, schedule
    # proposals) via the internal_action tools. These create draft/proposal
    # records only — they NEVER send email or write a calendar; the actual
    # send/execute stays a separate human-confirmed step gated by the kill
    # switches. Fail-closed: off -> the agent loop is read-only (Phase 1-4).
    agent_write_enabled: bool = False

    # Phase 6: independent evaluator (the generator/evaluator split). When true
    # (AND agent_runtime_enabled), a finished TOP-LEVEL run gets one adversarial
    # review pass: a separate agent identity (assume-broken, no praise, NO tools)
    # judges the answer + any staged artifacts against the goal and returns a
    # verdict (pass/concerns/fail + reasons). REVIEW-ONLY + advisory — it has no
    # tools and no external effects; it never sends, writes, or gates execution,
    # it only attaches a verdict to the run for the human to see. Fail-closed:
    # off -> no evaluation runs. dgx_eval_model_name optionally points the
    # evaluator at a DIFFERENT model (an independent judge catches blind spots a
    # self-review misses); falls back to the chat model when unset.
    agent_eval_enabled: bool = False
    dgx_eval_model_name: str = ""

    # Phase 7 (confirm-as-interrupt, INTERNAL half only). When true (AND
    # agent_runtime_enabled AND agent_write_enabled), a top-level run that STAGED
    # a review-only artifact pauses at status 'waiting_user' instead of finishing,
    # recording what is pending so a human can approve/reject it (POST
    # /chat/agent/runs/{id}/decision). The decision is recorded at the RUN level
    # ONLY — it does NOT send email or write a calendar; the real external firing
    # stays a separate, deliberately-deferred step under the kill switches. The
    # staged drafts/proposals remain in their existing review-only queues either
    # way. Fail-closed: off -> runs finish normally (no pause).
    agent_interrupt_enabled: bool = False

    # Phase 7 (confirm-as-interrupt, OUTWARD half). When true (AND the interrupt
    # half above), approving a paused run FIRES its staged artifacts through the
    # existing gated execution paths: a staged calendar CREATE goes through the
    # SAME _write_gate -> adapter.create_event path the chat-calendar confirm flow
    # uses, so it ALSO requires calendar_execution_enabled + the per-provider
    # calendar_write flag + a connected provider (this flag never bypasses the
    # calendar master gate; both must be on). Email drafts are NEVER sent here —
    # email send stays hard-disabled regardless. Calendar update/delete are not
    # fired by the agent yet (create only). Fail-closed: off -> resolve_interrupt
    # records the decision ONLY and fires nothing (the Phase-7 internal behavior).
    agent_execution_enabled: bool = False

    # Evaluator-gated approval (ties Phase 6 + 7). When true, APPROVING a paused run
    # whose independent-evaluator verdict is 'fail' is BLOCKED — resolve_interrupt
    # refuses (HTTP 409) and fires nothing — UNLESS the human explicitly overrides
    # (decision payload override=true). 'pass'/'concerns'/absent verdicts approve
    # normally; reject is never gated. Independent of agent_execution_enabled (it
    # gates the decision, not the firing). Needs agent_eval_enabled to produce a
    # verdict to gate on. Fail-closed: off -> approval is never blocked (Phase-7
    # behavior unchanged).
    agent_eval_gate_enabled: bool = False

    n8n_webhook_health_url: str = "http://n8n:5678/webhook/cora-health"

    # Self-hosted SearXNG metasearch — backs PULSE's web_search tool. Internal
    # only (cora-internal); never exposed publicly.
    searxng_endpoint: str = "http://searxng:8080"
    web_search_max_results: int = 6

    embedding_endpoint: str = ""
    embedding_model_name: str = ""
    embedding_dim: int = 768

    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expires_minutes: int = 60 * 24 * 7  # 7 days

    log_level: str = "INFO"
    service_name: str = "cora-api"

    # External Execution Kill Switch (v0.8). Master gate for ALL external
    # provider execution (send email / create calendar event / any provider
    # call). MUST default false: nothing external runs unless this is explicitly
    # set true AND every per-intent readiness condition passes. Leaving it false
    # keeps the whole system dry-run / readiness-only.
    external_execution_enabled: bool = False

    # Calendar Execution Switch (CHRONOS Calendar CRUD). DEDICATED master gate for
    # real calendar writes (create/update/delete), independent of the global
    # external_execution kill switch above. This separation is deliberate: the
    # email/integration-intent governance (execution_approval / final_interlock)
    # ENFORCES external_execution_enabled=false as a "still in the no-execution
    # phase" invariant (its approval checklist refuses while it is true), so
    # calendar writes — the first real external-execution path — must NOT reuse
    # that flag or they would break email/integration governance. Calendar writes
    # require this true AND the per-provider calendar_write feature flag AND a
    # connected provider with the write scope/capability. Defaults false.
    calendar_execution_enabled: bool = False

    # Real OAuth Flow (v1.1). Provider OAuth client config. Empty by default →
    # the provider reports not_configured and cannot start an OAuth flow. These
    # connect a provider ACCOUNT only; they never enable execution (still gated
    # by the v0.8 kill switch + v0.7 approval gate). Tokens are encrypted at rest
    # with the existing credential-vault key (CORA_CREDENTIAL_ENC_KEY); no second
    # key is introduced.
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_redirect_uri: str = ""
    microsoft_oauth_client_id: str = ""
    microsoft_oauth_client_secret: str = ""
    microsoft_oauth_redirect_uri: str = ""
    # Per-provider OAuth callback derivation. Each provider's redirect must land
    # on its OWN /oauth/<provider>/callback route (the callback resolves the
    # provider from the URL path + a state check), so one pinned redirect_uri
    # can't serve gmail AND google_calendar. If a *_redirect_base is set the code
    # builds "{base}/oauth/<provider>/callback"; otherwise it rewrites the
    # provider segment of the existing *_redirect_uri per provider (back-compat).
    google_oauth_redirect_base: str = ""
    microsoft_oauth_redirect_base: str = ""

    # Tier-2 Screen Vision (opt-in screenshot analysis). DEDICATED master switch for
    # sending a user-shared screenshot to a LOCAL vision model on the DGX Spark. Like
    # calendar_execution_enabled it is NOT the global external_execution kill switch:
    # this path is local-only (DGX on the internal network; no third party), user-
    # initiated (the browser's screen-share picker grabs one frame), and never auto-
    # captures. Requires this true AND vision_model_name set AND a DGX endpoint.
    # Defaults false → nothing is ever sent to a vision model.
    screen_vision_enabled: bool = False
    # Ollama vision model pulled on the DGX Spark (e.g. qwen2.5-vl). Empty by default →
    # screen vision reports not_configured and stays fail-closed.
    vision_model_name: str = ""

    # IANA timezone used when telling the LLM the current date/time. Defaults to
    # UTC (the server clock); set e.g. CORA_TIMEZONE=America/New_York for local
    # time. Requires the tzdata package for non-UTC names (in requirements.txt).
    cora_timezone: str = "UTC"

    # Semantic routing fallback. When true AND deterministic keyword routing scores
    # 0 (no specialist keyword and no explicit intent override matched), one cheap
    # LLM classification call picks a specialist from the user's intent so phrasing
    # the keyword lists don't anticipate ("dig up what people are saying about X")
    # still reaches the right agent. Fail-open: any failure / unrecognized label
    # leaves routing on the Cora persona — identical to today's behavior. Default
    # false → the deterministic path is the only path until an operator opts in.
    semantic_routing_enabled: bool = False


settings = Settings()
