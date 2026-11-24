"""Booking domain: proposals, explicit confirmation, conflict checks and recovery.

No LLM writes events. Database resource locks serialize this application's
bookings. External calendar writers can still race a free/busy check.
"""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .calendar_provider import CalendarConflict, CalendarUnavailable, parse_time
from gsme_rag.observability import emit


class TimeWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    starts_at: datetime
    ends_at: datetime

    @model_validator(mode="after")
    def valid_window(self):
        if self.starts_at.utcoffset() is None or self.ends_at.utcoffset() is None:
            raise ValueError("Include a timezone offset in start and end")
        self.starts_at = self.starts_at.astimezone(timezone.utc)
        self.ends_at = self.ends_at.astimezone(timezone.utc)
        if not timedelta(minutes=5) <= self.ends_at - self.starts_at <= timedelta(hours=8):
            raise ValueError("Choose a duration from 5 minutes to 8 hours")
        return self


class BookingProposal(TimeWindow):
    resource_id: UUID
    title: str = Field(min_length=1, max_length=160, pattern=r"\S")
    idempotency_key: UUID


class ResourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120, pattern=r"\S")
    calendar_id: str = Field(min_length=1, max_length=300, pattern=r"^[^\s/]+$")
    timezone: str = "Asia/Karachi"

    @model_validator(mode="after")
    def valid_zone(self):
        try:
            ZoneInfo(self.timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("Use an IANA timezone") from None
        if self.calendar_id == "primary":
            raise ValueError("Use an explicit dedicated calendar ID")
        return self


def public_booking(row):
    return {k: row[k] for k in ("id", "resource_id", "title", "starts_at", "ends_at", "state", "expires_at")}


class BookingService:
    def __init__(self, store, tenant, provider, clock=None):
        self.store, self.tenant, self.provider = store, tenant, provider
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _resource(self, db, resource_id, lock=False):
        row = db.execute("SELECT * FROM booking_resources WHERE tenant_id=%s AND id=%s AND active" +
                         (" FOR UPDATE" if lock else ""), (self.tenant, resource_id)).fetchone()
        if not row:
            raise HTTPException(404, "Booking resource unavailable")
        return row

    def _owned(self, db, booking_id, principal, lock=False):
        row = db.execute("SELECT * FROM calendar_bookings WHERE tenant_id=%s AND id=%s AND owner_subject=%s" +
                         (" FOR UPDATE" if lock else ""), (self.tenant, booking_id, principal.subject)).fetchone()
        if not row:
            raise HTTPException(404, "Booking unavailable")
        return row

    def _audit(self, db, principal, action, booking_id):
        db.execute("INSERT INTO company_action_audit(tenant_id,actor_subject,action,target_id) VALUES(%s,%s,%s,%s)",
                   (self.tenant, principal.subject, action, str(booking_id)))
        emit("booking.action", action=action, booking_id=str(booking_id))

    def _future(self, window):
        if window.starts_at <= self.clock() + timedelta(minutes=2):
            raise HTTPException(422, "Choose a start at least two minutes in the future")
        if window.starts_at > self.clock() + timedelta(days=366):
            raise HTTPException(422, "Choose a date within the next year")

    def _overlap(self, db, resource_id, start, end, exclude=None):
        return bool(db.execute("""SELECT 1 FROM calendar_bookings
            WHERE tenant_id=%s AND resource_id=%s AND state IN ('creating','confirmed','cancelling')
            AND starts_at<%s AND ends_at>%s AND (%s::uuid IS NULL OR id<>%s) LIMIT 1""",
            (self.tenant, resource_id, end, start, exclude, exclude)).fetchone())

    def availability(self, resource_id, window):
        self._future(window)
        with self.store.connect() as db:
            resource = self._resource(db, resource_id)
            local_busy = self._overlap(db, resource_id, window.starts_at, window.ends_at)
        busy = self.provider.busy(resource["calendar_id"], window.starts_at, window.ends_at)
        return {"available": not local_busy and not busy, "resource_id": resource_id,
                "starts_at": window.starts_at, "ends_at": window.ends_at,
                "checked_at": self.clock(), "timezone": resource["timezone"],
                "message": "Availability is checked again when you confirm."}

    def propose(self, principal, body):
        self._future(body)
        digest = sha256(json.dumps(body.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
        with self.store.connect() as db:
            self._resource(db, body.resource_id)
            row = db.execute("SELECT * FROM calendar_bookings WHERE tenant_id=%s AND owner_subject=%s AND idempotency_key=%s",
                             (self.tenant, principal.subject, body.idempotency_key)).fetchone()
            if row:
                if row["request_sha256"] != digest:
                    raise HTTPException(409, "This request key was already used for different meeting details")
                return public_booking(row)
            booking_id = uuid4()
            row = db.execute("""INSERT INTO calendar_bookings(id,tenant_id,owner_subject,resource_id,title,starts_at,ends_at,
                state,expires_at,idempotency_key,request_sha256,event_id)
                VALUES(%s,%s,%s,%s,%s,%s,%s,'proposed',%s,%s,%s,%s)
                ON CONFLICT(tenant_id,owner_subject,idempotency_key) DO NOTHING RETURNING *""",
                (booking_id, self.tenant, principal.subject, body.resource_id, body.title,
                 body.starts_at, body.ends_at, self.clock()+timedelta(minutes=10), body.idempotency_key,
                 digest, "b" + booking_id.hex)).fetchone()
            if not row:
                raise HTTPException(409, "Request already submitted; retry with the same request key")
            self._audit(db, principal, "booking.proposed", booking_id)
            return public_booking(row)

    def _matches_event(self, event, row):
        try:
            return (event.get("status") != "cancelled"
                and event.get("id") == row["event_id"]
                and event.get("extendedProperties", {}).get("private", {}).get("gsme_booking_id") == str(row["id"])
                and parse_time(event["start"]["dateTime"]) == row["starts_at"]
                and parse_time(event["end"]["dateTime"]) == row["ends_at"])
        except (KeyError, ValueError, TypeError):
            return False

    def confirm(self, principal, booking_id):
        # Reserve in a committed transaction before the external write. A lost
        # response leaves 'creating', so retries reconcile the same Google ID.
        with self.store.connect() as db:
            row = self._owned(db, booking_id, principal, lock=True)
            resource = self._resource(db, row["resource_id"], lock=True)
            if row["state"] == "confirmed":
                event = self.provider.get(resource["calendar_id"], row["event_id"])
                if not event or not self._matches_event(event, row):
                    raise CalendarUnavailable("This meeting changed in Google Calendar; operator reconciliation required")
                return public_booking(row)
            if row["state"] not in {"proposed", "creating"}:
                raise HTTPException(409, "This booking cannot be confirmed")
            if row["state"] == "proposed":
                if row["expires_at"] <= self.clock() or row["starts_at"] <= self.clock():
                    raise HTTPException(409, "Proposal expired; choose a new time")
                if self._overlap(db, row["resource_id"], row["starts_at"], row["ends_at"], row["id"]):
                    raise HTTPException(409, "Time overlaps another booking")
                if self.provider.busy(resource["calendar_id"], row["starts_at"], row["ends_at"]):
                    raise HTTPException(409, "Calendar is busy; choose another time")
                db.execute("UPDATE calendar_bookings SET state='creating',updated_at=now() WHERE id=%s", (booking_id,))
                self._audit(db, principal, "booking.creating", booking_id)
        # Serialize retries for the same resource during reconciliation too.
        with self.store.connect() as db:
            row = self._owned(db, booking_id, principal, lock=True)
            resource = self._resource(db, row["resource_id"], lock=True)
            if row["state"] == "confirmed":
                return public_booking(row)
            if row["state"] != "creating":
                raise HTTPException(409, "Booking state changed; refresh your bookings")
            event = self.provider.get(resource["calendar_id"], row["event_id"])
            if event is None:
                if row["starts_at"] <= self.clock():
                    raise HTTPException(409, "Booking time passed; cancel this request")
                if self.provider.busy(resource["calendar_id"], row["starts_at"], row["ends_at"]):
                    # Keep the durable reservation until the user cancels or an
                    # operator reconciles; never assume a timeout means no write.
                    raise HTTPException(409, "Calendar is busy; refresh or cancel this booking")
                try:
                    event = self.provider.create(resource, row)
                except CalendarConflict:
                    event = self.provider.get(resource["calendar_id"], row["event_id"])
            if not event or not self._matches_event(event, row):
                raise CalendarUnavailable("Calendar event needs reconciliation; retry or contact the operator")
            row = db.execute("UPDATE calendar_bookings SET state='confirmed',updated_at=now() WHERE id=%s RETURNING *",
                             (booking_id,)).fetchone()
            self._audit(db, principal, "booking.confirmed", booking_id)
            return public_booking(row)

    def cancel(self, principal, booking_id):
        with self.store.connect() as db:
            row = self._owned(db, booking_id, principal, lock=True)
            if row["state"] == "cancelled":
                return public_booking(row)
            if row["state"] == "proposed":
                row = db.execute("UPDATE calendar_bookings SET state='cancelled',updated_at=now() WHERE id=%s RETURNING *",
                                 (booking_id,)).fetchone()
                self._audit(db, principal, "booking.cancelled", booking_id)
                return public_booking(row)
            db.execute("UPDATE calendar_bookings SET state='cancelling',updated_at=now() WHERE id=%s", (booking_id,))
        with self.store.connect() as db:
            row = self._owned(db, booking_id, principal, lock=True)
            resource = self._resource(db, row["resource_id"], lock=True)
            if row["state"] == "cancelled":
                return public_booking(row)
            event = self.provider.get(resource["calendar_id"], row["event_id"])
            if event and event.get("status") != "cancelled":
                if not self._matches_event(event, row):
                    raise CalendarUnavailable("Event changed outside Muse; operator review required")
                self.provider.cancel(resource["calendar_id"], row["event_id"])
            row = db.execute("UPDATE calendar_bookings SET state='cancelled',updated_at=now() WHERE id=%s RETURNING *",
                             (booking_id,)).fetchone()
            self._audit(db, principal, "booking.cancelled", booking_id)
            return public_booking(row)
