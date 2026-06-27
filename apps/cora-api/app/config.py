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


settings = Settings()
