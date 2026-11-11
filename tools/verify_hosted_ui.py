"""Local hosted-chat browser checks with synthetic documents and mocked sign-in.

No company documents or real Google/Cohere calls. Requires the test-only Edge /
Playwright setup and an isolated gsme_rag_test* PostgreSQL database plus Qdrant.
"""

from argparse import ArgumentParser
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import sys
from threading import Thread
import time
from urllib.parse import urlsplit, urlunsplit
from unittest.mock import patch

from playwright.sync_api import sync_playwright, expect
import uvicorn

from gsme_rag.storage.config import from_env
from gsme_rag.hosted.app import create_hosted_app
from gsme_rag.hosted.access import Principal
from gsme_rag.hosted.identity import InvalidIdentity
from gsme_rag.storage.vectors import QdrantEvidence


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("validation/hosted-ui"))
    args = parser.parse_args()
    settings = from_env()
    os.environ["GSME_TEST_DATABASE_URL"] = os.getenv("GSME_TEST_DATABASE_URL") or urlunsplit(urlsplit(settings.database_url)._replace(path="/gsme_rag_test_retrieval_20261103"))
    os.environ["GSME_TEST_QDRANT_URL"] = settings.qdrant_url
    if not urlsplit(os.environ["GSME_TEST_DATABASE_URL"]).path.startswith("/gsme_rag_test"):
        raise ValueError("An isolated test database is required")
    # Reuse the same synthetic privacy fixtures as the API regressions.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from test_hosted import HostedIsolationTests
    fixture = HostedIsolationTests()
    fixture.setUp()
    args.output.mkdir(parents=True, exist_ok=True)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    hosted = replace(fixture.hosted, origin=origin, google_client_id="synthetic.apps.googleusercontent.com",
                     workspace_domains=("company.example",))

    def verifier(token):
        if token != "alice-fixture":
            raise InvalidIdentity()
        return Principal("google:alice")

    def providers():
        return fixture.provider, fixture.reranker, QdrantEvidence(fixture.settings.qdrant_url, fixture.settings.collection, 32, hybrid=True)

    app = create_hosted_app(fixture.settings, hosted, fixture.store, verifier, providers)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    errors, unexpected = [], []
    try:
        with patch("gsme_rag.hosted.app.require_answer_permission"), patch.dict(os.environ, {"GSME_ANSWER_PROVIDER": "cohere"}), patch("cohere.ClientV2", side_effect=AssertionError("External model client forbidden in browser fixture")):
            thread.start()
            deadline = time.monotonic() + 15
            while not server.started and time.monotonic() < deadline:
                time.sleep(.05)
            if not server.started:
                raise RuntimeError("Synthetic hosted server did not start")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                context = browser.new_context(viewport={"width": 1280, "height": 900})

                def network(route):
                    url = route.request.url
                    if url.startswith(origin + "/"):
                        route.continue_()
                    elif url == "https://accounts.google.com/gsi/client":
                        route.fulfill(content_type="application/javascript", body="window.google={accounts:{id:{initialize:function(o){window.fixtureSignin=o.callback},renderButton:function(e){const b=document.createElement('button');b.id='fixtureLogin';b.textContent='Synthetic sign-in';b.onclick=()=>window.fixtureSignin({credential:'alice-fixture'});e.replaceChildren(b)}}}};")
                    else:
                        unexpected.append(url)
                        route.abort()
                context.route("**/*", network)
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(origin)
                expect(page.locator("#scope option")).to_have_count(2)
                assert "alice.docx" not in page.content() and "bob.docx" not in page.content()
                page.locator("#signin").click()
                page.locator("#fixtureLogin").click()
                expect(page.locator("#signout")).to_be_visible()
                expect(page.locator("#scope option")).to_have_count(3)
                assert "bob.docx" not in page.content()
                page.locator("#scope").select_option(fixture.alice["document_id"])
                page.locator("#question").fill('<img src=x onerror="window.injected=true"> Who is the synthetic contact?')
                page.locator("#send").click()
                expect(page.locator("#messages")).to_contain_text("COBALT_914", timeout=30000)
                assert page.locator("#messages img").count() == 0
                assert not page.evaluate("Boolean(window.injected)")
                page.screenshot(path=str(args.output / "private-owner-chat.png"), full_page=True)

                pending = []
                page.route("**/api/chat", lambda route: pending.append(route))
                page.locator("#question").fill("Show my private contact")
                page.locator("#send").click()
                page.wait_for_timeout(100)
                page.locator("#signout").click()
                expect(page.locator("#messages")).to_be_empty()
                assert pending
                pending[0].fulfill(json={"route": "documents", "status": "results",
                    "answer": {"text": "COBALT_914 delayed private response"}, "results": []})
                page.wait_for_timeout(300)
                assert "COBALT_914" not in page.content()
                expect(page.locator("#scope option")).to_have_count(2)
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.screenshot(path=str(args.output / "public-mobile.png"), full_page=True)
                assert not errors and not unexpected, (errors, unexpected)
                browser.close()
        result = {"guest_faq_only": "passed", "owner_only_documents": "passed", "private_source_rendering": "passed",
                  "logout_clears_history": "passed", "late_private_response_after_logout": "discarded",
                  "mobile_overflow": "none", "real_external_calls": 0, "google_signin": "mocked, not live company SSO",
                  "browser_errors": errors, "unexpected_network": unexpected}
        (args.output / "report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))
    finally:
        server.should_exit = True
        if thread.is_alive():
            thread.join(timeout=10)
        listener.close()
        fixture.doCleanups()


if __name__ == "__main__":
    main()
