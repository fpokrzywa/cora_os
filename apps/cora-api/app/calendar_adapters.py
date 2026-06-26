"""Calendar CRUD adapters (CHRONOS Calendar CRUD v1.0).

Defines the full read + create + update + delete calendar contract for Google
Calendar + Microsoft (Outlook) Calendar, implemented against the providers' real
events endpoints. UNLIKE the inbox adapters, this surface can mutate — so the
callers (`chat_calendar`) gate writes behind the fail-closed write gate
(`_write_gate`: provider connected + valid token + write scope + provider write
capability + `calendar_write` feature flag + the global execution kill switch).
Reads use the lighter read gate (the `calendar_read` flag). These methods are
only reachable after the relevant gate passes; the access token is obtained by
the caller's token broker and passed in per call. Tokens are never logged or
returned.

ALL HTTP goes through the two module-level choke points — `_http_get_json`
(reads, GET only) and `_http_write` (writes, POST/PATCH/DELETE) — so tests can
patch them and no other network path exists.
"""

import logging
from typing import Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS = 15.0

# Scope a connector must hold before calendar access is permitted. Google's
# `calendar.events` and Microsoft's `Calendars.ReadWrite` cover both read and
# write of events, so read and write share the same scope here.
READ_SCOPES = {
    "google_calendar": "https://www.googleapis.com/auth/calendar.events",
    "outlook_calendar": "https://graph.microsoft.com/Calendars.ReadWrite",
}
WRITE_SCOPES = dict(READ_SCOPES)

# Calendar API methods (names — informational; the implementations below call them).
API_METHODS = {
    "google_calendar": {"list": "calendar.events.list", "get": "calendar.events.get",
                        "create": "calendar.events.insert", "update": "calendar.events.patch",
                        "delete": "calendar.events.delete"},
    "outlook_calendar": {"list": "graph.me.events", "get": "graph.me.events.get",
                         "create": "graph.me.events.create", "update": "graph.me.events.update",
                         "delete": "graph.me.events.delete"},
}

_GCAL_BASE = "https://www.googleapis.com/calendar/v3/calendars/primary"
_GCAL_CAL = "https://www.googleapis.com/calendar/v3/calendars"  # + /{calendarId}/events
_GRAPH_BASE = "https://graph.microsoft.com/v1.0/me"
_GRAPH_SELECT = "id,subject,start,end,location,webLink,attendees,bodyPreview,seriesMasterId,isAllDay"


class CalendarAccessDisabled(Exception):
    """Raised when a method is called without a usable access token. The
    governance gate + token broker normally fail first (defense in depth)."""


class CalendarError(Exception):
    """A provider calendar API call failed (HTTP error / network). The message is
    sanitized — it never contains token material."""


async def _http_get_json(url: str, *, token: str, params: Optional[dict] = None) -> dict:
    """Single HTTP choke point for ALL calendar reads (GET only)."""
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                url, params=params, headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        raise CalendarError(f"provider request failed: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise CalendarError(f"provider read rejected (HTTP {resp.status_code})")
    try:
        return resp.json()
    except ValueError as exc:
        raise CalendarError("provider returned non-JSON response") from exc


async def _http_write(method: str, url: str, *, token: str,
                      json_body: Optional[dict] = None,
                      params: Optional[dict] = None) -> dict:
    """Single HTTP choke point for ALL calendar writes (POST/PATCH/DELETE). Only
    reached after the write gate + kill switch pass. Returns the parsed JSON body
    (or {} for an empty 204). Never logs the token."""
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.request(
                method, url, headers={"Authorization": f"Bearer {token}"},
                json=json_body, params=params,
            )
    except httpx.HTTPError as exc:
        raise CalendarError(f"provider request failed: {type(exc).__name__}") from exc
    if resp.status_code not in (200, 201, 204):
        raise CalendarError(f"provider write rejected (HTTP {resp.status_code})")
    if resp.status_code == 204 or not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {}


def _trunc(v: Optional[str], n: int = 200) -> str:
    s = (v or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


class CalendarAdapter:
    provider_name: str = ""
    provider_type: str = "calendar"

    @property
    def read_scope(self) -> str:
        return READ_SCOPES[self.provider_name]

    @property
    def write_scope(self) -> str:
        return WRITE_SCOPES[self.provider_name]

    @staticmethod
    def _require_token(access_token: Optional[str]) -> str:
        if not access_token:
            raise CalendarAccessDisabled("no access token available for calendar access")
        return access_token

    async def list_events(self, *, access_token: Optional[str] = None,
                          time_min: Optional[str] = None, time_max: Optional[str] = None,
                          limit: int = 10) -> list:
        raise NotImplementedError

    async def get_event(self, *, access_token: Optional[str] = None,
                        event_id: str, calendar_id: str = "primary") -> dict:
        raise NotImplementedError

    async def create_event(self, *, access_token: Optional[str] = None,
                           fields: dict, calendar_id: str = "primary") -> dict:
        raise NotImplementedError

    async def update_event(self, *, access_token: Optional[str] = None,
                           event_id: str, fields: dict, calendar_id: str = "primary") -> dict:
        raise NotImplementedError

    async def delete_event(self, *, access_token: Optional[str] = None,
                           event_id: str, calendar_id: str = "primary") -> dict:
        raise NotImplementedError

    async def list_calendars(self, *, access_token: Optional[str] = None) -> list:
        """Writable calendars the user owns: [{id, name, primary}]. Used to resolve
        a natural-language calendar name on CREATE. Fails closed to primary."""
        raise NotImplementedError

    def describe(self) -> dict:
        return {
            "provider_name": self.provider_name,
            "provider_type": self.provider_type,
            "read_scope": self.read_scope,
            "write_scope": self.write_scope,
            "api_methods": API_METHODS.get(self.provider_name, {}),
            "live_crud_enabled": True,
        }


class GoogleCalendarAdapter(CalendarAdapter):
    provider_name = "google_calendar"

    @staticmethod
    def _normalize(ev: dict, calendar_id: str = "primary") -> dict:
        start = (ev.get("start") or {})
        end = (ev.get("end") or {})
        return {
            "id": ev.get("id"),
            "calendar_id": calendar_id,
            "title": ev.get("summary") or "(no title)",
            "start": start.get("dateTime") or start.get("date") or "—",
            "end": end.get("dateTime") or end.get("date") or "—",
            "all_day": "date" in start and "dateTime" not in start,
            "series_id": ev.get("recurringEventId"),
            "location": ev.get("location") or "",
            "attendees": [a.get("email") for a in (ev.get("attendees") or []) if a.get("email")],
            "link": ev.get("htmlLink") or "",
        }

    @staticmethod
    def _body(fields: dict) -> dict:
        """Build a Google event resource from neutral fields (only set keys)."""
        body: dict = {}
        if fields.get("title") is not None:
            body["summary"] = fields["title"]
        if fields.get("description") is not None:
            body["description"] = fields["description"]
        if fields.get("location") is not None:
            body["location"] = fields["location"]
        tz = fields.get("timezone")
        if fields.get("start_time"):
            body["start"] = {"dateTime": fields["start_time"], **({"timeZone": tz} if tz else {})}
        if fields.get("end_time"):
            body["end"] = {"dateTime": fields["end_time"], **({"timeZone": tz} if tz else {})}
        if fields.get("attendees"):
            body["attendees"] = [{"email": e} for e in fields["attendees"]]
        return body

    @staticmethod
    async def _calendar_ids(token: str) -> list:
        """All calendar ids the user can read. Needs the calendar.readonly scope;
        if it isn't granted (403) the request fails closed to ['primary'] so a
        narrower connection still reads the primary calendar."""
        try:
            data = await _http_get_json(
                "https://www.googleapis.com/calendar/v3/users/me/calendarList",
                token=token, params={"fields": "items(id,selected)", "minAccessRole": "reader"})
        except CalendarError:
            return ["primary"]
        ids = [c["id"] for c in (data.get("items") or [])
               if c.get("id") and c.get("selected", True)]
        return ids or ["primary"]

    async def list_events(self, *, access_token: Optional[str] = None,
                          time_min: Optional[str] = None, time_max: Optional[str] = None,
                          limit: int = 10) -> list:
        token = self._require_token(access_token)
        params: dict = {"maxResults": max(1, min(limit, 50)),
                        "singleEvents": "true", "orderBy": "startTime"}
        if time_min:
            params["timeMin"] = time_min
        if time_max:
            params["timeMax"] = time_max
        # Aggregate across ALL the user's calendars (not just primary), so work /
        # secondary calendars show up. A single unreadable calendar is skipped, but
        # if EVERY calendar read fails (e.g. auth/network) the error is surfaced —
        # never silently reported as "no events".
        cal_ids = await self._calendar_ids(token)
        events: list = []
        errors = 0
        last_exc: Optional[CalendarError] = None
        for cal_id in cal_ids:
            try:
                listing = await _http_get_json(
                    f"https://www.googleapis.com/calendar/v3/calendars/{quote(cal_id)}/events",
                    token=token, params=params)
            except CalendarError as exc:
                errors += 1
                last_exc = exc
                continue
            events.extend(self._normalize(e, cal_id) for e in (listing.get("items") or []))
        if errors and errors == len(cal_ids):
            raise last_exc
        events.sort(key=lambda e: e.get("start") or "")
        return events[:limit]

    async def list_calendars(self, *, access_token: Optional[str] = None) -> list:
        token = self._require_token(access_token)
        # minAccessRole=writer → only calendars the user can create events on, so a
        # subscribed/read-only calendar can't be picked as a write target.
        try:
            data = await _http_get_json(
                "https://www.googleapis.com/calendar/v3/users/me/calendarList",
                token=token, params={"fields": "items(id,summary,summaryOverride,primary)",
                                     "minAccessRole": "writer"})
        except CalendarError:
            return [{"id": "primary", "name": "primary", "primary": True}]
        out = [{"id": c["id"],
                "name": c.get("summaryOverride") or c.get("summary") or c["id"],
                "primary": bool(c.get("primary"))}
               for c in (data.get("items") or []) if c.get("id")]
        return out or [{"id": "primary", "name": "primary", "primary": True}]

    async def get_event(self, *, access_token: Optional[str] = None, event_id: str,
                        calendar_id: str = "primary") -> dict:
        token = self._require_token(access_token)
        return self._normalize(await _http_get_json(
            f"{_GCAL_CAL}/{quote(calendar_id)}/events/{event_id}", token=token), calendar_id)

    async def create_event(self, *, access_token: Optional[str] = None, fields: dict,
                           calendar_id: str = "primary") -> dict:
        token = self._require_token(access_token)
        # sendUpdates=all so attendees actually receive the invite email.
        return self._normalize(await _http_write(
            "POST", f"{_GCAL_CAL}/{quote(calendar_id)}/events", token=token,
            json_body=self._body(fields), params={"sendUpdates": "all"}), calendar_id)

    async def update_event(self, *, access_token: Optional[str] = None,
                           event_id: str, fields: dict, calendar_id: str = "primary") -> dict:
        token = self._require_token(access_token)
        return self._normalize(await _http_write(
            "PATCH", f"{_GCAL_CAL}/{quote(calendar_id)}/events/{event_id}", token=token,
            json_body=self._body(fields), params={"sendUpdates": "all"}), calendar_id)

    async def delete_event(self, *, access_token: Optional[str] = None, event_id: str,
                           calendar_id: str = "primary") -> dict:
        token = self._require_token(access_token)
        # sendUpdates=all so attendees are notified of the cancellation.
        await _http_write("DELETE", f"{_GCAL_CAL}/{quote(calendar_id)}/events/{event_id}",
                          token=token, params={"sendUpdates": "all"})
        return {"id": event_id, "deleted": True, "calendar_id": calendar_id}


class MicrosoftCalendarAdapter(CalendarAdapter):
    provider_name = "outlook_calendar"

    @staticmethod
    def _normalize(ev: dict) -> dict:
        return {
            "id": ev.get("id"),
            "title": ev.get("subject") or "(no title)",
            "start": (ev.get("start") or {}).get("dateTime") or "—",
            "end": (ev.get("end") or {}).get("dateTime") or "—",
            "all_day": bool(ev.get("isAllDay")),
            "series_id": ev.get("seriesMasterId"),
            "location": (ev.get("location") or {}).get("displayName") or "",
            "attendees": [(a.get("emailAddress") or {}).get("address")
                          for a in (ev.get("attendees") or [])
                          if (a.get("emailAddress") or {}).get("address")],
            "link": ev.get("webLink") or "",
        }

    @staticmethod
    def _body(fields: dict) -> dict:
        """Build a Graph event resource from neutral fields (only set keys)."""
        body: dict = {}
        if fields.get("title") is not None:
            body["subject"] = fields["title"]
        if fields.get("description") is not None:
            body["body"] = {"contentType": "text", "content": fields["description"]}
        if fields.get("location") is not None:
            body["location"] = {"displayName": fields["location"]}
        tz = fields.get("timezone") or "UTC"
        if fields.get("start_time"):
            body["start"] = {"dateTime": fields["start_time"], "timeZone": tz}
        if fields.get("end_time"):
            body["end"] = {"dateTime": fields["end_time"], "timeZone": tz}
        if fields.get("attendees"):
            body["attendees"] = [{"emailAddress": {"address": e}, "type": "required"}
                                 for e in fields["attendees"]]
        return body

    async def list_events(self, *, access_token: Optional[str] = None,
                          time_min: Optional[str] = None, time_max: Optional[str] = None,
                          limit: int = 10) -> list:
        token = self._require_token(access_token)
        top = max(1, min(limit, 50))
        if time_min and time_max:
            # calendarView expands recurrences within [start,end] and includes past.
            url = f"{_GRAPH_BASE}/calendarView"
            params = {"startDateTime": time_min, "endDateTime": time_max, "$top": top,
                      "$select": _GRAPH_SELECT, "$orderby": "start/dateTime"}
        else:
            url = f"{_GRAPH_BASE}/events"
            params = {"$top": top, "$select": _GRAPH_SELECT, "$orderby": "start/dateTime"}
        listing = await _http_get_json(url, token=token, params=params)
        return [self._normalize(e) for e in (listing.get("value") or [])[:limit]]

    async def list_calendars(self, *, access_token: Optional[str] = None) -> list:
        token = self._require_token(access_token)
        try:
            data = await _http_get_json(
                f"{_GRAPH_BASE}/calendars", token=token,
                params={"$select": "id,name,isDefaultCalendar,canEdit", "$top": 50})
        except CalendarError:
            return [{"id": "primary", "name": "primary", "primary": True}]
        out = [{"id": c["id"], "name": c.get("name") or c["id"],
                "primary": bool(c.get("isDefaultCalendar"))}
               for c in (data.get("value") or [])
               if c.get("id") and c.get("canEdit") is not False]
        return out or [{"id": "primary", "name": "primary", "primary": True}]

    async def get_event(self, *, access_token: Optional[str] = None, event_id: str,
                        calendar_id: str = "primary") -> dict:
        token = self._require_token(access_token)
        return self._normalize(await _http_get_json(
            f"{_GRAPH_BASE}/events/{event_id}", token=token,
            params={"$select": _GRAPH_SELECT}))

    async def create_event(self, *, access_token: Optional[str] = None, fields: dict,
                           calendar_id: str = "primary") -> dict:
        token = self._require_token(access_token)
        # Default calendar → /me/events; a named calendar → /me/calendars/{id}/events.
        base = (_GRAPH_BASE if calendar_id in (None, "primary")
                else f"{_GRAPH_BASE}/calendars/{calendar_id}")
        return self._normalize(await _http_write(
            "POST", f"{base}/events", token=token, json_body=self._body(fields)))

    async def update_event(self, *, access_token: Optional[str] = None,
                           event_id: str, fields: dict, calendar_id: str = "primary") -> dict:
        token = self._require_token(access_token)
        return self._normalize(await _http_write(
            "PATCH", f"{_GRAPH_BASE}/events/{event_id}", token=token,
            json_body=self._body(fields)))

    async def delete_event(self, *, access_token: Optional[str] = None, event_id: str,
                           calendar_id: str = "primary") -> dict:
        token = self._require_token(access_token)
        await _http_write("DELETE", f"{_GRAPH_BASE}/events/{event_id}", token=token)
        return {"id": event_id, "deleted": True}


_ADAPTERS = {a.provider_name: a for a in (GoogleCalendarAdapter(), MicrosoftCalendarAdapter())}
# The capability registry + feature flags call the Microsoft calendar provider
# "microsoft_calendar"; the OAuth vault/adapter layer uses "outlook_calendar".
_ALIASES = {"microsoft_calendar": "outlook_calendar"}


def resolve_calendar_adapter(provider_name: Optional[str]) -> Optional[CalendarAdapter]:
    key = (provider_name or "").strip().lower()
    return _ADAPTERS.get(_ALIASES.get(key, key))


def list_calendar_adapters() -> list[dict]:
    return [a.describe() for a in _ADAPTERS.values()]
