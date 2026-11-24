"""Optional browser check of company sign-in and explicit booking confirmation.

Set GSME_TEST_BROWSER=yes; uses installed Edge and synthetic intercepted APIs.
No Google, Supabase, email, database or LLM requests leave the browser.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase, skipUnless
from urllib.parse import urlsplit
from uuid import uuid4


@skipUnless(os.getenv("GSME_TEST_BROWSER") == "yes", "Opt-in browser test needs Playwright and Edge")
class CompanyBrowserTests(TestCase):
    def test_signin_proposal_confirmation_and_signout(self):
        from playwright.sync_api import sync_playwright, expect
        web = Path(__file__).parents[1] / "src/gsme_rag/hosted/web"
        resource_id, booking_id = str(uuid4()), str(uuid4())
        proposal = {}
        writes, errors = [], []

        def handler(route):
            path = urlsplit(route.request.url).path
            data = None
            if path == "/":
                return route.fulfill(content_type="text/html", body=(web / "index.html").read_text(encoding="utf-8"))
            if path in {"/assets/chat.css", "/assets/chat.js"}:
                return route.fulfill(content_type="text/css" if path.endswith("css") else "application/javascript",
                                     body=(web / Path(path).name).read_text(encoding="utf-8"))
            if path == "/api/config":
                data = dict(version="test", guest_uploads_enabled=True, uploads_enabled=True,
                            private_uploads_enabled=True, auth_provider="supabase", calendar_enabled=True)
            elif path == "/api/session":
                data = {"status":"ready"}
            elif path == "/api/documents":
                data = {"documents":[], "has_more":False, "signed_in":False, "browser_workspace":True}
            elif path == "/api/chats":
                data = {"chats":[], "history_enabled":True}
            elif path == "/api/auth/code":
                data = {"message":"Synthetic sign-in code sent"}
            elif path == "/api/auth/verify":
                data = {"access_token":"synthetic-access", "expires_in":3600}
            elif path == "/api/access/me":
                data = {"subject":"supabase:"+str(uuid4()), "role":"member"}
            elif path == "/api/calendar/resources":
                data = {"enabled":True, "resources":[{"id":resource_id, "name":"Test team", "timezone":"Asia/Karachi"}]}
            elif path.endswith("/availability"):
                data = {"available":True}
            elif path == "/api/bookings":
                data = {"bookings":list(proposal.values())}
            elif path == "/api/bookings/proposals":
                body = route.request.post_data_json
                data = dict(body, id=booking_id, state="proposed", expires_at="2030-01-01T00:00:00Z")
                proposal[booking_id] = data
            elif path.endswith("/confirm"):
                writes.append(route.request.post_data_json)
                proposal[booking_id]["state"] = "confirmed"
                data = proposal[booking_id]
            else:
                raise AssertionError("Unexpected browser request: " + path)
            route.fulfill(content_type="application/json", body=json.dumps(data))

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                page = browser.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.route("**/*", handler)
                page.goto("http://localhost/")
                expect(page.locator("#send")).to_be_enabled()
                page.locator("#signin").click()
                page.locator("#company-email").fill("synthetic@example.test")
                page.locator("#send-company-code").click()
                expect(page.locator("#company-signin-notice")).to_contain_text("sent")
                page.locator("#company-code").fill("123456")
                page.locator("#company-signin-form button[type=submit]").click()
                expect(page.locator("#signout")).to_be_visible()
                page.locator("#question").fill("Book a meeting")
                page.locator("#question").press("Enter")
                expect(page.locator("#meeting-resource option")).to_have_count(1)
                page.locator("#meeting-title").fill("Test meeting")
                start = datetime.now(timezone.utc)+timedelta(days=2)
                page.locator("#meeting-start").fill(start.strftime("%Y-%m-%dT%H:%M"))
                page.locator("#meeting-end").fill((start+timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"))
                page.locator("#check-meeting").click()
                expect(page.locator("#meetings-notice")).to_contain_text("Available now")
                page.locator("#meeting-form button[type=submit]").click()
                expect(page.locator("#meeting-proposal")).to_be_visible()
                self.assertFalse(writes)
                page.locator("#close-meetings").click()
                page.locator("#question").fill("yes, let's do it")
                page.locator("#question").press("Enter")
                expect(page.locator("#messages")).to_contain_text("Meeting confirmed")
                self.assertEqual(writes, [{"confirmed":True}])
                page.locator("#signout").click()
                expect(page.locator("#my-bookings")).to_be_empty()
                expect(page.locator("#meeting-title")).to_have_value("")
                self.assertFalse(errors, errors)
            finally:
                browser.close()
