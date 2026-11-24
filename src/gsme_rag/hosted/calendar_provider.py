"""Google Calendar transport, replaceable independently of search and booking rules."""

import os
from pathlib import Path
from datetime import datetime, timezone
from threading import Lock
from urllib.parse import quote
import time

import httpx

from gsme_rag.observability import emit


class CalendarUnavailable(RuntimeError):
    pass


class CalendarConflict(ValueError):
    pass


def parse_time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise CalendarUnavailable("Calendar returned an invalid time")
    return result.astimezone(timezone.utc)


class GoogleCalendar:
    """One dedicated organizer account; refresh token remains on the server."""

    def __init__(self):
        self.client_id = os.getenv("GSME_CALENDAR_CLIENT_ID", "")
        self.client_secret = os.getenv("GSME_CALENDAR_CLIENT_SECRET", "")
        self.refresh_token = os.getenv("GSME_CALENDAR_REFRESH_TOKEN", "")
        token_file = os.getenv("GSME_CALENDAR_REFRESH_TOKEN_FILE", "")
        if not self.refresh_token and token_file:
            self.refresh_token = Path(token_file).read_text(encoding="utf-8").strip()
        self._token, self._expiry, self._lock = "", 0, Lock()

    @property
    def enabled(self):
        return bool(self.client_id and self.client_secret and self.refresh_token)

    def _access_token(self):
        if not self.enabled:
            raise CalendarUnavailable("Configure the booking calendar credentials")
        with self._lock:
            if time.monotonic() < self._expiry:
                return self._token
            try:
                with httpx.Client(timeout=15, follow_redirects=False) as client:
                    result = client.post("https://oauth2.googleapis.com/token", data={
                        "client_id": self.client_id, "client_secret": self.client_secret,
                        "refresh_token": self.refresh_token, "grant_type": "refresh_token"})
                result.raise_for_status()
                body = result.json()
                self._token = body["access_token"]
                self._expiry = time.monotonic() + max(0, int(body.get("expires_in", 300)) - 60)
                return self._token
            except (httpx.HTTPError, ValueError, KeyError):
                raise CalendarUnavailable("Calendar authorization unavailable; operator must reconnect") from None

    def _request(self, method, path, *, body=None, missing=False):
        started = time.monotonic()
        operation = "availability" if path == "freeBusy" else method.lower() + "_event"
        try:
            with httpx.Client(timeout=20, follow_redirects=False) as client:
                result = client.request(method, "https://www.googleapis.com/calendar/v3/" + path,
                    headers={"Authorization": "Bearer " + self._access_token()}, json=body)
            emit("calendar.request.completed", operation=operation, status=result.status_code,
                 elapsed_ms=round((time.monotonic()-started)*1000, 2))
            if missing and result.status_code in {404, 410}:
                return None
            if result.status_code == 409:
                raise CalendarConflict("Calendar event already exists")
            if result.status_code == 401:
                self._expiry = 0
            result.raise_for_status()
            data = result.json() if result.content else {}
            if not isinstance(data, dict):
                raise CalendarUnavailable("Invalid calendar response")
            return data
        except (httpx.HTTPError, ValueError) as error:
            if isinstance(error, CalendarConflict):
                raise
            emit("calendar.request.failed", operation=operation, error_type=type(error).__name__)
            raise CalendarUnavailable("Calendar request unavailable; retry this booking safely") from None

    def busy(self, calendar_id, start, end):
        data = self._request("POST", "freeBusy", body={"timeMin": start.isoformat(),
            "timeMax": end.isoformat(), "items": [{"id": calendar_id}]})
        entry = data.get("calendars", {}).get(calendar_id)
        if not isinstance(entry, dict) or entry.get("errors") or "busy" not in entry:
            raise CalendarUnavailable("Calendar availability could not be verified")
        try:
            return [(parse_time(p["start"]), parse_time(p["end"])) for p in entry["busy"]]
        except (TypeError, KeyError, ValueError):
            raise CalendarUnavailable("Invalid calendar availability response") from None

    def get(self, calendar_id, event_id):
        return self._request("GET", f"calendars/{quote(calendar_id, safe='')}/events/{event_id}", missing=True)

    def create(self, resource, booking):
        return self._request("POST", f"calendars/{quote(resource['calendar_id'], safe='')}/events", body={
            "id": booking["event_id"], "summary": booking["title"], "visibility": "private",
            "transparency": "opaque", "start": {"dateTime": booking["starts_at"].isoformat(), "timeZone": resource["timezone"]},
            "end": {"dateTime": booking["ends_at"].isoformat(), "timeZone": resource["timezone"]},
            "extendedProperties": {"private": {"gsme_booking_id": str(booking["id"])}}})

    def cancel(self, calendar_id, event_id):
        self._request("DELETE", f"calendars/{quote(calendar_id, safe='')}/events/{event_id}", missing=True)
