"""Verify per-provider OAuth redirect-URI derivation (CHRONOS Calendar OAuth fix).

Each provider must get its OWN /oauth/<provider>/callback so one pinned redirect
can't serve gmail AND google_calendar (the callback resolves the provider from the
URL path + a state check). Pure-function test (no DB / no network): monkeypatch
settings, assert `redirect_uri_for` / `provider_config`. Restores settings in
finally. Run:

    docker cp apps/cora-api/scripts/verify_oauth_redirect.py cora-api:/tmp/vor.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vor.py     # 0=PASS 1=FAIL
"""
import sys

from app import oauth_providers as op
from app.config import settings


def main() -> int:
    fails = []

    def expect(c, m):
        if not c:
            fails.append(m)

    saved = {k: getattr(settings, k) for k in (
        "google_oauth_redirect_uri", "google_oauth_redirect_base",
        "microsoft_oauth_redirect_uri", "microsoft_oauth_redirect_base",
        "google_oauth_client_id", "google_oauth_client_secret")}

    gmail = op.get_provider("gmail")
    gcal = op.get_provider("google_calendar")
    omail = op.get_provider("outlook_mail")
    ocal = op.get_provider("outlook_calendar")
    ocal_alias = op.get_provider("microsoft_calendar")  # aliases to outlook_calendar
    try:
        # (1) back-compat rewrite: existing gmail-pinned value adapts per provider.
        settings.google_oauth_redirect_base = ""
        settings.google_oauth_redirect_uri = "http://localhost:8000/oauth/gmail/callback"
        expect(op.redirect_uri_for(gmail) == "http://localhost:8000/oauth/gmail/callback",
               "gmail keeps its existing callback (back-compat)")
        expect(op.redirect_uri_for(gcal) == "http://localhost:8000/oauth/google_calendar/callback",
               "google_calendar gets its OWN callback path from the gmail-pinned value")

        # (2) explicit base wins + works for any host (https domain).
        settings.google_oauth_redirect_base = "https://cora.example.com/"
        expect(op.redirect_uri_for(gmail) == "https://cora.example.com/oauth/gmail/callback",
               "base builds gmail callback")
        expect(op.redirect_uri_for(gcal) == "https://cora.example.com/oauth/google_calendar/callback",
               "base builds google_calendar callback")
        # provider_config surfaces the per-provider redirect (+ client creds).
        settings.google_oauth_client_id = "cid"
        settings.google_oauth_client_secret = "sec"
        cfg = op.provider_config(gcal)
        expect(cfg["redirect_uri"] == "https://cora.example.com/oauth/google_calendar/callback"
               and cfg["client_id"] == "cid", "provider_config carries per-provider redirect")
        expect(op.config_present(gcal) is True, "config_present true with id+secret+redirect")

        # (3) microsoft vendor: outlook_mail vs outlook_calendar diverge; alias maps.
        settings.microsoft_oauth_redirect_base = ""
        settings.microsoft_oauth_redirect_uri = "https://cora.example.com/oauth/outlook_mail/callback"
        expect(op.redirect_uri_for(omail) == "https://cora.example.com/oauth/outlook_mail/callback",
               "outlook_mail keeps its callback")
        expect(op.redirect_uri_for(ocal) == "https://cora.example.com/oauth/outlook_calendar/callback",
               "outlook_calendar gets its own callback")
        expect(ocal_alias is ocal and op.redirect_uri_for(ocal_alias).endswith("/oauth/outlook_calendar/callback"),
               "microsoft_calendar alias resolves to outlook_calendar callback")

        # (4) unconfigured → empty (fail-closed, config_present false).
        settings.google_oauth_redirect_base = ""
        settings.google_oauth_redirect_uri = ""
        expect(op.redirect_uri_for(gcal) == "", "unconfigured redirect → empty")
    finally:
        for k, v in saved.items():
            setattr(settings, k, v)

    if fails:
        print("RESULT: FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print("RESULT: PASS — redirect URIs derive per provider (gmail/google_calendar + "
          "outlook_mail/outlook_calendar land on distinct /oauth/<provider>/callback "
          "routes); base wins over pinned uri; pinned uri rewrites per provider "
          "(back-compat); alias maps; unconfigured fails closed; settings restored")
    return 0


if __name__ == "__main__":
    sys.exit(main())
