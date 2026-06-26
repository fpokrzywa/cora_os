from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    cora_env: str = "development"
    database_url: str = "postgresql://cora:cora@postgres:5432/cora"
    redis_url: str = "redis://redis:6379/0"
    dgx_model_endpoint: str = ""
    dgx_model_name: str = ""

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

    # IANA timezone used when telling the LLM the current date/time. Defaults to
    # UTC (the server clock); set e.g. CORA_TIMEZONE=America/New_York for local
    # time. Requires the tzdata package for non-UTC names (in requirements.txt).
    cora_timezone: str = "UTC"


settings = Settings()
