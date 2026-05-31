"""Chat-Native Inbox Assistant v2.3 — read-only inbox adapter skeleton.

Defines the READ-ONLY mailbox contract (list_messages / search_messages /
read_message / read_thread) for Gmail + Outlook Mail. This phase ships a skeleton:
the read methods are NOT implemented against a live API and raise
`InboxReadDisabled` — defense in depth behind the v2.3 governance gate (which
already fails closed because the read scope + read feature flag are absent). NO
send / reply / forward / delete / archive method exists here, and no token is ever
read or returned by these adapters.

When a real read scope (gmail.readonly / Mail.Read) + an enabled inbox_read feature
flag are added in a future phase, the read methods would be implemented to call the
provider's read-only endpoints and return normalized message dicts (no tokens).
"""

from typing import Optional

# Read scopes a connector must hold before any inbox read is permitted.
READ_SCOPES = {
    "gmail": "https://www.googleapis.com/auth/gmail.readonly",
    "outlook_mail": "https://graph.microsoft.com/Mail.Read",
}
# Read API methods (names only — never invoked in this phase).
READ_METHODS = {
    "gmail": {"list": "gmail.users.messages.list", "get": "gmail.users.messages.get",
              "thread": "gmail.users.threads.get"},
    "outlook_mail": {"list": "graph.me.messages", "get": "graph.me.messages.get",
                     "thread": "graph.me.messages.conversation"},
}


class InboxReadDisabled(Exception):
    """Raised when a read method is called while a live read implementation is not
    enabled in this phase. The governance gate normally fails first."""


class InboxAdapter:
    provider_name: str = ""
    provider_type: str = "email"

    @property
    def read_scope(self) -> str:
        return READ_SCOPES[self.provider_name]

    def _refuse(self, method: str):
        raise InboxReadDisabled(
            f"{self.provider_name}.{method}: live inbox read is not enabled in this phase")

    # Read-only contract — skeleton (no live call). Token is intentionally NOT a
    # parameter: nothing here ever touches credential material.
    def list_messages(self, *, query: Optional[str] = None, limit: int = 10) -> list:
        self._refuse("list_messages")

    def search_messages(self, *, query: str, limit: int = 10) -> list:
        self._refuse("search_messages")

    def read_message(self, *, message_id: str) -> dict:
        self._refuse("read_message")

    def read_thread(self, *, thread_id: str) -> dict:
        self._refuse("read_thread")

    def describe(self) -> dict:
        return {
            "provider_name": self.provider_name,
            "provider_type": self.provider_type,
            "read_scope": self.read_scope,
            "read_methods": READ_METHODS.get(self.provider_name, {}),
            "live_read_enabled": False,
        }


class GmailInboxAdapter(InboxAdapter):
    provider_name = "gmail"


class OutlookInboxAdapter(InboxAdapter):
    provider_name = "outlook_mail"


_ADAPTERS = {a.provider_name: a for a in (GmailInboxAdapter(), OutlookInboxAdapter())}


def resolve_inbox_adapter(provider_name: Optional[str]) -> Optional[InboxAdapter]:
    return _ADAPTERS.get((provider_name or "").strip().lower())


def list_inbox_adapters() -> list[dict]:
    return [a.describe() for a in _ADAPTERS.values()]
