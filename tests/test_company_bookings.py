"""Booking lifecycle and security without Google writes or company content."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from threading import RLock
from concurrent.futures import ThreadPoolExecutor
from unittest import TestCase, skipUnless
from unittest.mock import Mock, MagicMock, patch
from uuid import uuid4, UUID
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from gsme_rag.hosted.access import Principal
from gsme_rag.hosted.app import create_hosted_app
from gsme_rag.hosted.bookings import BookingService, BookingProposal, TimeWindow, ResourceInput
from gsme_rag.hosted.calendar_provider import GoogleCalendar, CalendarUnavailable
from gsme_rag.hosted.identity import InvalidIdentity
from gsme_rag.hosted.roles import CompanyRoles
from gsme_rag.hosted.supabase import SupabaseAuth
from gsme_rag.storage.config import Settings


class CalendarFixture:
    enabled = True

    def __init__(self):
        self.events = {}
        self.writes = 0
        self.fail_after_write = False
        self.external_busy = False

    def busy(self, calendar, start, end):
        return [(start, end)] if self.external_busy else []

    def get(self, calendar, event_id):
        return self.events.get(event_id)

    def create(self, resource, row):
        self.writes += 1
        event = {"id": row["event_id"], "status": "confirmed",
            "start": {"dateTime": row["starts_at"].isoformat()},
            "end": {"dateTime": row["ends_at"].isoformat()},
            "extendedProperties": {"private": {"gsme_booking_id": str(row["id"])}}}
        self.events[row["event_id"]] = event
        if self.fail_after_write:
            raise CalendarUnavailable("Synthetic lost response")
        return event

    def cancel(self, calendar, event_id):
        self.events.pop(event_id, None)


class MemoryBookingStore:
    """Transaction fixture only; real SQL/concurrency tests use PostgreSQL below."""
    def __init__(self, tenant, resource):
        self.tenant, self.resource, self.rows, self.lock = tenant, resource, {}, RLock()

    @contextmanager
    def connect(self):
        with self.lock:
            previous = deepcopy(self.rows)
            try:
                yield self
            except BaseException:
                self.rows = previous
                raise

    def execute(self, sql, values=()):
        sql = " ".join(sql.split())
        result = None
        if sql.startswith("SELECT * FROM booking_resources"):
            result = self.resource if values[0] == self.tenant and values[1] == self.resource["id"] else None
        elif sql.startswith("SELECT * FROM calendar_bookings"):
            if "idempotency_key=%s" in sql:
                result = next((r for r in self.rows.values() if (r["tenant_id"], r["owner_subject"], r["idempotency_key"]) == values), None)
            else:
                tenant, key, owner = values
                row = self.rows.get(key)
                result = row if row and row["tenant_id"] == tenant and row["owner_subject"] == owner else None
        elif sql.startswith("SELECT 1 FROM calendar_bookings"):
            tenant, resource, end, start, exclude, _ = values
            result = next((r for r in self.rows.values() if r["tenant_id"] == tenant
                and r["resource_id"] == resource and r["state"] in {"creating", "confirmed", "cancelling"}
                and r["starts_at"] < end and r["ends_at"] > start and r["id"] != exclude), None)
        elif sql.startswith("INSERT INTO calendar_bookings"):
            keys = ("id", "tenant_id", "owner_subject", "resource_id", "title", "starts_at", "ends_at",
                    "expires_at", "idempotency_key", "request_sha256", "event_id")
            result = dict(zip(keys, values), state="proposed")
            self.rows[result["id"]] = result
        elif sql.startswith("UPDATE calendar_bookings"):
            state = sql.split("state='")[1].split("'")[0]
            result = self.rows[values[0]]
            result["state"] = state
        elif sql.startswith("INSERT INTO company_action_audit"):
            pass
        else:
            raise AssertionError("Unsupported test SQL: " + sql)
        return Mock(fetchone=lambda: deepcopy(result))


class LifecycleCases:
    def proposal(self, **changes):
        body = dict(resource_id=self.resource["id"], title="Synthetic meeting",
                    starts_at=self.now+timedelta(days=1), ends_at=self.now+timedelta(days=1, hours=1),
                    idempotency_key=uuid4())
        body.update(changes)
        return BookingProposal(**body)

    def test_confirmation_is_required_and_idempotent(self):
        body = self.proposal()
        result = self.service.propose(self.owner, body)
        self.assertEqual(result["state"], "proposed")
        self.assertEqual(self.provider.writes, 0)
        again = self.service.propose(self.owner, body)
        self.assertEqual(result["id"], again["id"])
        confirmed = self.service.confirm(self.owner, result["id"])
        self.assertEqual(confirmed["state"], "confirmed")
        self.service.confirm(self.owner, result["id"])
        self.assertEqual(self.provider.writes, 1)

    def test_lost_provider_response_recovers_same_event(self):
        result = self.service.propose(self.owner, self.proposal())
        self.provider.fail_after_write = True
        with self.assertRaises(CalendarUnavailable):
            self.service.confirm(self.owner, result["id"])
        self.provider.fail_after_write = False
        self.assertEqual(self.service.confirm(self.owner, result["id"])["state"], "confirmed")
        self.assertEqual(self.provider.writes, 1)

    def test_owner_and_tenant_boundaries(self):
        result = self.service.propose(self.owner, self.proposal())
        for operation in (self.service.confirm, self.service.cancel):
            with self.assertRaises(HTTPException) as error:
                operation(Principal("supabase:"+str(uuid4())), result["id"])
            self.assertEqual(error.exception.status_code, 404)
        another = BookingService(self.store, "another-tenant", self.provider, clock=lambda: self.now)
        with self.assertRaises(HTTPException):
            another.confirm(self.owner, result["id"])
        self.assertEqual(self.provider.writes, 0)

    def test_overlap_blocked_and_adjacent_booking_allowed(self):
        first = self.service.propose(self.owner, self.proposal())
        self.service.confirm(self.owner, first["id"])
        second = self.service.propose(self.owner, self.proposal())
        with self.assertRaises(HTTPException) as error:
            self.service.confirm(self.owner, second["id"])
        self.assertEqual(error.exception.status_code, 409)
        third = self.service.propose(self.owner, self.proposal(starts_at=self.now+timedelta(days=1,hours=1),
                                    ends_at=self.now+timedelta(days=1,hours=2)))
        self.assertEqual(self.service.confirm(self.owner, third["id"])["state"], "confirmed")

    def test_live_busy_expiry_and_changed_idempotency_key(self):
        body = self.proposal()
        result = self.service.propose(self.owner, body)
        with self.assertRaises(HTTPException):
            self.service.propose(self.owner, body.model_copy(update={"title": "Different request"}))
        self.provider.external_busy = True
        with self.assertRaises(HTTPException) as error:
            self.service.confirm(self.owner, result["id"])
        self.assertEqual(error.exception.status_code, 409)
        self.provider.external_busy = False
        self.service.clock = lambda: self.now + timedelta(minutes=11)
        with self.assertRaises(HTTPException):
            self.service.confirm(self.owner, result["id"])
        self.assertEqual(self.provider.writes, 0)

    def test_cancel_is_owner_only_repeatable_and_releases_slot(self):
        result = self.service.propose(self.owner, self.proposal())
        self.service.confirm(self.owner, result["id"])
        self.assertEqual(self.service.cancel(self.owner, result["id"])["state"], "cancelled")
        self.assertEqual(self.service.cancel(self.owner, result["id"])["state"], "cancelled")
        self.assertFalse(self.provider.events)
        replacement = self.service.propose(self.owner, self.proposal())
        self.assertEqual(self.service.confirm(self.owner, replacement["id"])["state"], "confirmed")

    def test_parallel_confirmations_create_only_one_event(self):
        first = self.service.propose(self.owner, self.proposal())
        second = self.service.propose(self.owner, self.proposal())
        def confirm(booking):
            try:
                return self.service.confirm(self.owner, booking["id"])["state"]
            except HTTPException as error:
                return error.status_code
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(confirm, [first, second]))
        self.assertCountEqual(results, ["confirmed", 409])
        self.assertEqual(self.provider.writes, 1)


class BookingUnitTests(LifecycleCases, TestCase):
    def setUp(self):
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.resource = dict(id=uuid4(), calendar_id="synthetic@example.test", timezone="Asia/Karachi")
        self.store = MemoryBookingStore("tenant", self.resource)
        self.provider, self.owner = CalendarFixture(), Principal("supabase:" + str(uuid4()))
        self.service = BookingService(self.store, "tenant", self.provider, clock=lambda: self.now)


TEST_DB = os.getenv("GSME_TEST_DATABASE_URL", "")


@skipUnless(urlsplit(TEST_DB).path.startswith("/gsme_rag_test"), "Requires an isolated gsme_rag_test PostgreSQL database")
class BookingPostgresTests(LifecycleCases, TestCase):
    def setUp(self):
        from gsme_rag.storage.postgres import EvidenceStore
        self.now = datetime.now(timezone.utc)
        self.tenant = "booking-test-" + uuid4().hex
        self.store = EvidenceStore(TEST_DB)
        self.store.migrate()
        self.resource = dict(id=uuid4(), calendar_id="synthetic@example.test", timezone="Asia/Karachi")
        with self.store.connect() as db:
            db.execute("INSERT INTO booking_resources(id,tenant_id,name,calendar_id,timezone) VALUES(%s,%s,'Synthetic',%s,%s)",
                       (self.resource["id"], self.tenant, self.resource["calendar_id"], self.resource["timezone"]))
        self.provider, self.owner = CalendarFixture(), Principal("supabase:" + str(uuid4()))
        self.service = BookingService(self.store, self.tenant, self.provider, clock=lambda: self.now)

    def tearDown(self):
        with self.store.connect() as db:
            for table in ("calendar_bookings", "booking_resources", "company_action_audit"):
                db.execute(f"DELETE FROM {table} WHERE tenant_id=%s", (self.tenant,))


class CompanySecurityTests(TestCase):
    def test_timezones_durations_and_resource_ids_are_validated(self):
        for start, end in (("2030-01-01T12:00", "2030-01-01T13:00"),
                           ("2030-01-01T13:00+05:00", "2030-01-01T12:00+05:00"),
                           ("2030-01-01T12:00Z", "2030-01-02T13:00Z")):
            with self.assertRaises(ValueError):
                TimeWindow(starts_at=start, ends_at=end)
        window = TimeWindow(starts_at="2030-01-01T12:00+05:00", ends_at="2030-01-01T13:00+05:00")
        self.assertEqual(window.starts_at.hour, 7)
        with self.assertRaises(ValueError):
            ResourceInput(name="Calendar", calendar_id="primary", timezone="Asia/Karachi")

    def test_roles_are_server_owned_and_disabled_overrides_bootstrap(self):
        store, db = Mock(), MagicMock()
        store.connect.return_value.__enter__ = Mock(return_value=db)
        store.connect.return_value.__exit__ = Mock(return_value=False)
        roles = CompanyRoles(store, "t")
        principal = Principal("supabase:" + str(uuid4()))
        db.execute.return_value.fetchone.return_value = None
        self.assertEqual(roles.role(principal), "member")
        with patch.dict(os.environ, {"GSME_COMPANY_ADMIN_SUBJECTS": principal.subject}):
            self.assertEqual(roles.role(principal), "admin")
            db.execute.return_value.fetchone.return_value = {"role": "disabled"}
            with self.assertRaises(HTTPException):
                roles.require(principal, "admin")
        with self.assertRaises(HTTPException):
            roles.require(Principal("guest:unverified"), "member")

    def test_supabase_uses_verified_user_not_client_role_claims(self):
        with patch.dict(os.environ, {"GSME_SUPABASE_URL": "https://example.supabase.co", "GSME_SUPABASE_PUBLISHABLE_KEY": "synthetic"}):
            auth = SupabaseAuth()
        uid = str(uuid4())
        with patch.object(auth, "_request", return_value={"id":uid, "email_confirmed_at":"now", "user_metadata":{"role":"admin"}}):
            self.assertEqual(auth.principal("token"), Principal("supabase:" + uid))
        for response in ({"id":uid}, {"id":uid,"email_confirmed_at":"now","is_anonymous":True}):
            with patch.object(auth, "_request", return_value=response):
                with self.assertRaises(InvalidIdentity):
                    auth.principal("token")
        with patch.object(auth, "_request") as request:
            auth.send_code("invited@example.test")
            self.assertFalse(request.call_args.kwargs["body"]["create_user"])

    def test_guest_and_viewer_cannot_execute_booking_commands(self):
        store, db = Mock(), MagicMock()
        store.connect.return_value = MagicMock()
        store.connect.return_value.__enter__.return_value = db
        db.execute.return_value.fetchone.return_value = {"role":"viewer"}
        app = create_hosted_app(Settings("unused", "unused", Path("data")), store=store,
            verifier=lambda _: Principal("supabase:"+str(uuid4())), auto_worker=False,
            calendar_provider=CalendarFixture())
        body = dict(resource_id=str(uuid4()), title="Meeting", starts_at="2030-01-01T12:00Z",
                    ends_at="2030-01-01T13:00Z", idempotency_key=str(uuid4()))
        with TestClient(app, base_url="http://127.0.0.1:8020") as client:
            self.assertEqual(client.post("/api/bookings/proposals", json=body).status_code, 401)
            self.assertEqual(client.post("/api/bookings/proposals", json=body, headers={"Authorization":"Bearer verified"}).status_code, 403)
            self.assertEqual(client.put("/api/access/members", json={"subject":"google:123", "role":"admin"}, headers={"Authorization":"Bearer verified"}).status_code, 403)

    def test_google_availability_errors_do_not_become_free_slots(self):
        provider = GoogleCalendar()
        with patch.object(provider, "_request", return_value={"calendars":{"cal":{"errors":[{"reason":"notFound"}]}}}):
            with self.assertRaises(CalendarUnavailable):
                provider.busy("cal", datetime.now(timezone.utc), datetime.now(timezone.utc)+timedelta(hours=1))

    def test_google_transport_uses_stable_private_event_without_attendee_emails(self):
        provider = GoogleCalendar()
        start = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
        row = dict(id=uuid4(),event_id="b"+uuid4().hex,title="Synthetic",starts_at=start,ends_at=start+timedelta(hours=1))
        with patch.object(provider, "_request", return_value={"id":row["event_id"]}) as request:
            provider.create({"calendar_id":"test@example.test", "timezone":"Asia/Karachi"}, row)
            body = request.call_args.kwargs["body"]
            self.assertEqual(body["id"],row["event_id"])
            self.assertEqual(body["visibility"],"private")
            self.assertNotIn("attendees",body)
            self.assertNotIn("sendUpdates",request.call_args.args[1])

    def test_provider_http_failures_and_expired_identity_fail_closed(self):
        provider = GoogleCalendar()
        response = httpx.Response(503, request=httpx.Request("GET","https://www.googleapis.com/"))
        with patch.object(provider, "_access_token", return_value="synthetic"), patch("httpx.Client") as client:
            client.return_value.__enter__.return_value.request.return_value = response
            with self.assertRaises(CalendarUnavailable):
                provider.get("test@example.test", "b123")
        with patch.dict(os.environ, {"GSME_SUPABASE_URL":"https://test.supabase.co", "GSME_SUPABASE_PUBLISHABLE_KEY":"synthetic"}):
            auth = SupabaseAuth()
        response = httpx.Response(401, request=httpx.Request("GET","https://test.supabase.co/auth/v1/user"))
        with patch("httpx.Client") as client:
            client.return_value.__enter__.return_value.request.return_value = response
            with self.assertRaises(InvalidIdentity):
                auth.principal("expired")
