"""Read-only inbox adapters (v2.3 skeleton → v2.7 live read implementation).

Defines the READ-ONLY mailbox contract (list_messages / search_messages /
read_message / read_thread) for Gmail + Outlook Mail, implemented against the
providers' read-only endpoints. Access remains governed by the v2.3 fail-closed
gate in `chat_inbox._gate` (provider connected + valid token + read scope present
+ `inbox_read` feature flag enabled) — these methods are only reachable after
that gate passes, and the access token is obtained by the caller's token broker
and passed in per call. NO send / reply / forward / delete / archive method
exists here, tokens are never logged or returned, and only metadata + snippets
are read (never full bodies).

All HTTP goes through the single module-level `_http_get_json` choke point so
tests can patch it and no other network path exists.
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS = 15.0

# Read scopes a connector must hold before any inbox read is permitted.
READ_SCOPES = {
    "gmail": "https://www.googleapis.com/auth/gmail.readonly",
    "outlook_mail": "https://graph.microsoft.com/Mail.Read",
}
# Read API methods (names — informational; the implementations below call them).
READ_METHODS = {
    "gmail": {"list": "gmail.users.messages.list", "get": "gmail.users.messages.get",
              "thread": "gmail.users.threads.get"},
    "outlook_mail": {"list": "graph.me.messages", "get": "graph.me.messages.get",
                     "thread": "graph.me.messages.conversation"},
}

_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_GRAPH_BASE = "https://graph.microsoft.com/v1.0/me"
_GRAPH_SELECT = "id,subject,from,receivedDateTime,bodyPreview,conversationId"


class InboxReadDisabled(Exception):
    """Raised when a read method is called without a usable access token.
    The governance gate + token broker normally fail first (defense in depth)."""


class InboxReadError(Exception):
    """A provider read-only API call failed (HTTP error / network). The message
    is sanitized — it never contains token material."""


async def _http_get_json(url: str, *, token: str, params: Optional[dict] = None) -> dict:
    """Single HTTP choke point for ALL inbox reads (GET only — read-only by
    construction). Raises InboxReadError on any failure; never logs the token."""
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                url, params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        raise InboxReadError(f"provider request failed: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise InboxReadError(f"provider read rejected (HTTP {resp.status_code})")
    try:
        return resp.json()
    except ValueError as exc:
        raise InboxReadError("provider returned non-JSON response") from exc


def _trunc(v: Optional[str], n: int = 200) -> str:
    s = (v or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


class InboxAdapter:
    provider_name: str = ""
    provider_type: str = "email"

    @property
    def read_scope(self) -> str:
        return READ_SCOPES[self.provider_name]

    @staticmethod
    def _require_token(access_token: Optional[str]) -> str:
        if not access_token:
            raise InboxReadDisabled("no access token available for inbox read")
        return access_token

    # Read-only contract. The caller (chat_inbox) obtains the token via its
    # broker AFTER the governance gate passes and passes it per call; nothing
    # here stores or logs it.
    async def list_messages(self, *, access_token: Optional[str] = None,
                            query: Optional[str] = None, limit: int = 10) -> list:
        raise NotImplementedError

    async def search_messages(self, *, access_token: Optional[str] = None,
                              query: str, limit: int = 10) -> list:
        return await self.list_messages(access_token=access_token, query=query,
                                        limit=limit)

    async def read_message(self, *, access_token: Optional[str] = None,
                           message_id: str) -> dict:
        raise NotImplementedError

    async def read_thread(self, *, access_token: Optional[str] = None,
                          thread_id: str) -> dict:
        raise NotImplementedError

    def describe(self) -> dict:
        return {
            "provider_name": self.provider_name,
            "provider_type": self.provider_type,
            "read_scope": self.read_scope,
            "read_methods": READ_METHODS.get(self.provider_name, {}),
            "live_read_enabled": True,
        }


class GmailInboxAdapter(InboxAdapter):
    provider_name = "gmail"

    @staticmethod
    def _normalize(msg: dict) -> dict:
        headers = {
            (h.get("name") or "").lower(): h.get("value")
            for h in (msg.get("payload") or {}).get("headers", [])
        }
        return {
            "id": msg.get("id"),
            "thread_id": msg.get("threadId"),
            "from": headers.get("from", "—"),
            "subject": headers.get("subject", "(no subject)"),
            "date": headers.get("date", "—"),
            "snippet": _trunc(msg.get("snippet")),
        }

    async def _get_message(self, token: str, message_id: str) -> dict:
        msg = await _http_get_json(
            f"{_GMAIL_BASE}/messages/{message_id}", token=token,
            params={"format": "metadata",
                    "metadataHeaders": ["From", "Subject", "Date"]},
        )
        return self._normalize(msg)

    async def list_messages(self, *, access_token: Optional[str] = None,
                            query: Optional[str] = None, limit: int = 10) -> list:
        token = self._require_token(access_token)
        params: dict = {"maxResults": max(1, min(limit, 25))}
        if query:
            params["q"] = query
        listing = await _http_get_json(f"{_GMAIL_BASE}/messages", token=token,
                                       params=params)
        out = []
        for ref in (listing.get("messages") or [])[:limit]:
            out.append(await self._get_message(token, ref["id"]))
        return out

    async def read_message(self, *, access_token: Optional[str] = None,
                           message_id: str) -> dict:
        token = self._require_token(access_token)
        if message_id == "latest":
            msgs = await self.list_messages(access_token=token, limit=1)
            if not msgs:
                raise InboxReadError("mailbox returned no messages")
            return msgs[0]
        return await self._get_message(token, message_id)

    async def read_thread(self, *, access_token: Optional[str] = None,
                          thread_id: str) -> dict:
        token = self._require_token(access_token)
        thread = await _http_get_json(
            f"{_GMAIL_BASE}/threads/{thread_id}", token=token,
            params={"format": "metadata",
                    "metadataHeaders": ["From", "Subject", "Date"]},
        )
        return {"thread_id": thread.get("id"),
                "messages": [self._normalize(m) for m in thread.get("messages", [])]}


class OutlookInboxAdapter(InboxAdapter):
    provider_name = "outlook_mail"

    @staticmethod
    def _normalize(msg: dict) -> dict:
        sender = ((msg.get("from") or {}).get("emailAddress") or {})
        frm = sender.get("name") or sender.get("address") or "—"
        if sender.get("name") and sender.get("address"):
            frm = f"{sender['name']} <{sender['address']}>"
        return {
            "id": msg.get("id"),
            "thread_id": msg.get("conversationId"),
            "from": frm,
            "subject": msg.get("subject") or "(no subject)",
            "date": msg.get("receivedDateTime", "—"),
            "snippet": _trunc(msg.get("bodyPreview")),
        }

    async def list_messages(self, *, access_token: Optional[str] = None,
                            query: Optional[str] = None, limit: int = 10) -> list:
        token = self._require_token(access_token)
        params: dict = {"$top": max(1, min(limit, 25)), "$select": _GRAPH_SELECT}
        if query:
            # $search and $orderby are mutually exclusive on Graph.
            params["$search"] = f'"{query}"'
        else:
            params["$orderby"] = "receivedDateTime desc"
        listing = await _http_get_json(f"{_GRAPH_BASE}/messages", token=token,
                                       params=params)
        return [self._normalize(m) for m in (listing.get("value") or [])[:limit]]

    async def read_message(self, *, access_token: Optional[str] = None,
                           message_id: str) -> dict:
        token = self._require_token(access_token)
        if message_id == "latest":
            msgs = await self.list_messages(access_token=token, limit=1)
            if not msgs:
                raise InboxReadError("mailbox returned no messages")
            return msgs[0]
        msg = await _http_get_json(f"{_GRAPH_BASE}/messages/{message_id}",
                                   token=token, params={"$select": _GRAPH_SELECT})
        return self._normalize(msg)

    async def read_thread(self, *, access_token: Optional[str] = None,
                          thread_id: str) -> dict:
        token = self._require_token(access_token)
        listing = await _http_get_json(
            f"{_GRAPH_BASE}/messages", token=token,
            params={"$top": 25, "$select": _GRAPH_SELECT,
                    "$filter": f"conversationId eq '{thread_id}'"},
        )
        return {"thread_id": thread_id,
                "messages": [self._normalize(m) for m in listing.get("value", [])]}


_ADAPTERS = {a.provider_name: a for a in (GmailInboxAdapter(), OutlookInboxAdapter())}


def resolve_inbox_adapter(provider_name: Optional[str]) -> Optional[InboxAdapter]:
    return _ADAPTERS.get((provider_name or "").strip().lower())


def list_inbox_adapters() -> list[dict]:
    return [a.describe() for a in _ADAPTERS.values()]
