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

    # IANA timezone used when telling the LLM the current date/time. Defaults to
    # UTC (the server clock); set e.g. CORA_TIMEZONE=America/New_York for local
    # time. Requires the tzdata package for non-UTC names (in requirements.txt).
    cora_timezone: str = "UTC"


settings = Settings()
