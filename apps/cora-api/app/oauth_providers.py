"""Real OAuth Flow v1.1 — provider registry.

Describes each supported provider's OAuth endpoints, scopes, and which env
credential group (google / microsoft) it uses. The scopes request the
*future* capability (gmail.send, calendar.events, Mail.Send, Calendars.ReadWrite)
so a connected account is ready for a later execution phase — connecting grants
NO ability to send/create here; execution stays blocked by the v0.8 kill switch.
"""

from dataclasses import dataclass, field

from app.config import settings


@dataclass(frozen=True)
class OAuthProvider:
    name: str
    provider_type: str          # email | calendar
    vendor: str                 # google | microsoft
    authorize_url: str
    token_url: str
    scopes: list                # requested OAuth scopes
    requires_refresh_token: bool = True
    extra_authorize_params: dict = field(default_factory=dict)


# Google uses access_type=offline + prompt=consent to obtain a refresh token.
_GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
# Microsoft v2.0 (common tenant). offline_access scope yields a refresh token.
_MS_AUTH = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
_MS_TOKEN = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

PROVIDERS: dict[str, OAuthProvider] = {
    "gmail": OAuthProvider(
        name="gmail",
        provider_type="email",
        vendor="google",
        authorize_url=_GOOGLE_AUTH,
        token_url=_GOOGLE_TOKEN,
        # gmail.readonly enables the governed v2.7 inbox read (still gated by
        # the inbox_read feature flag); granting a scope enables nothing alone.
        scopes=["https://www.googleapis.com/auth/gmail.send",
                "https://www.googleapis.com/auth/gmail.readonly"],
        requires_refresh_token=True,
        extra_authorize_params={"access_type": "offline", "prompt": "consent"},
    ),
    "google_calendar": OAuthProvider(
        name="google_calendar",
        provider_type="calendar",
        vendor="google",
        authorize_url=_GOOGLE_AUTH,
        token_url=_GOOGLE_TOKEN,
        scopes=["https://www.googleapis.com/auth/calendar.events"],
        requires_refresh_token=True,
        extra_authorize_params={"access_type": "offline", "prompt": "consent"},
    ),
    "outlook_mail": OAuthProvider(
        name="outlook_mail",
        provider_type="email",
        vendor="microsoft",
        authorize_url=_MS_AUTH,
        token_url=_MS_TOKEN,
        # Mail.Read enables the governed v2.7 inbox read (flag-gated).
        scopes=["https://graph.microsoft.com/Mail.Send",
                "https://graph.microsoft.com/Mail.Read", "offline_access"],
        requires_refresh_token=True,
    ),
    "outlook_calendar": OAuthProvider(
        name="outlook_calendar",
        provider_type="calendar",
        vendor="microsoft",
        authorize_url=_MS_AUTH,
        token_url=_MS_TOKEN,
        scopes=["https://graph.microsoft.com/Calendars.ReadWrite", "offline_access"],
        requires_refresh_token=True,
    ),
}


# The v0.5 connector registry + spec refer to the Microsoft calendar provider as
# "microsoft_calendar"; the OAuth/vault/adapter layer canonicalizes it as
# "outlook_calendar". Accept the alias so /oauth/microsoft_calendar/* resolves the
# same provider without renaming the verified canonical name elsewhere.
_ALIASES = {"microsoft_calendar": "outlook_calendar"}


def get_provider(name):
    key = (name or "").strip().lower()
    return PROVIDERS.get(_ALIASES.get(key, key))


def provider_config(provider: OAuthProvider) -> dict:
    """Resolve the vendor's OAuth client config from settings (never logged)."""
    if provider.vendor == "google":
        return {
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "redirect_uri": settings.google_oauth_redirect_uri,
        }
    if provider.vendor == "microsoft":
        return {
            "client_id": settings.microsoft_oauth_client_id,
            "client_secret": settings.microsoft_oauth_client_secret,
            "redirect_uri": settings.microsoft_oauth_redirect_uri,
        }
    return {"client_id": "", "client_secret": "", "redirect_uri": ""}


def config_present(provider: OAuthProvider) -> bool:
    cfg = provider_config(provider)
    return bool(cfg["client_id"] and cfg["client_secret"] and cfg["redirect_uri"])
