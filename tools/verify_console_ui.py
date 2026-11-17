"""Opt-in browser regression using headless Edge and synthetic evidence only.

Needs test-only `playwright` installed; no browser dependency in the application.
Uses an isolated gsme_rag_test* database, local Qdrant and a temporary HTTP port.
"""

from argparse import ArgumentParser
from dataclasses import replace
import json
import os
import re
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from threading import Thread
import time
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from playwright.sync_api import sync_playwright, expect
import uvicorn
import pymupdf

from gsme_rag.storage.config import from_env
from gsme_rag.storage.postgres import EvidenceStore
from gsme_rag.storage.admin import create_admin_app
from qdrant_client import QdrantClient


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("validation/console-ui"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    settings = from_env()
    # Never run browser approval fixtures against the normal development DB.
    parts = urlsplit(settings.database_url)
    test_url = os.getenv("GSME_TEST_DATABASE_URL") or urlunsplit(parts._replace(path="/gsme_rag_test_retrieval_20261103"))
    if not urlsplit(test_url).path.startswith("/gsme_rag_test"):
        raise ValueError("Browser test requires an isolated gsme_rag_test database")
    token = "synthetic-browser-token-at-least-20-characters"
    os.environ["GSME_REVIEW_AUTH_TOKEN"] = token
    os.environ["GSME_AUTHORIZED_REVIEWERS"] = "synthetic-browser-reviewer"
    os.environ["GSME_PDF_BACKEND"] = "native"
    with TemporaryDirectory(prefix="gsme-ui-fixture-") as temporary:
        settings = replace(settings, database_url=test_url, data_dir=Path(temporary) / "store",
                           collection="gsme_ui_" + uuid4().hex[:12])
        store = EvidenceStore(test_url)
        store.migrate()
        source = Path(temporary) / "synthetic_links.docx"
        doc = Document()
        doc.add_heading("Synthetic source links", 1)
        doc.add_heading("Where is the synthetic schedule?", 2)
        paragraph = doc.add_paragraph("The synthetic schedule can be found ")
        link = OxmlElement("w:hyperlink")
        link.set(qn("r:id"), paragraph.part.relate_to("https://example.invalid/shared?lot=A17", RT.HYPERLINK, is_external=True))
        run, text = OxmlElement("w:r"), OxmlElement("w:t")
        text.text = "here"
        run.append(text)
        link.append(run)
        paragraph._p.append(link)
        paragraph.add_run(".")
        doc.add_heading("Where is the missing destination?", 2)
        doc.add_paragraph("The destination can be found here.")
        doc.add_heading("Is source text executable?", 2)
        doc.add_paragraph('<img src=x onerror="window.sourceExecuted=true"> is literal source text.')
        doc.save(source)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_admin_app(settings, store), log_level="error"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        tenant = "ui-synthetic-" + uuid4().hex[:10]
        try:
            deadline = time.monotonic() + 15
            while not server.started:
                if time.monotonic() > deadline:
                    raise RuntimeError("Test HTTP server did not start")
                time.sleep(.05)
            with sync_playwright() as driver:
                browser = driver.chromium.launch(channel="msedge", headless=True)
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                failures = []
                page.on("pageerror", lambda error: failures.append(str(error)))
                base = f"http://127.0.0.1:{port}"
                external_requests = []
                def route(request):
                    if request.request.url.startswith(base + "/"):
                        request.continue_()
                    else:
                        external_requests.append(request.request.url)
                        request.abort()
                page.route("**/*", route)
                page.goto(base + "/admin")
                expect(page.locator("#panel-documents")).to_be_visible()
                expect(page.locator("#panel-search")).to_be_hidden()
                page.locator("#token").fill(token)
                page.locator("#tenant").fill(tenant)
                page.locator("#connect").click()
                expect(page.locator("#connection")).not_to_have_attribute("open", "", timeout=15000)
                page.locator("#newUpload").click()
                page.locator("#file").set_input_files(source)
                page.get_by_text("Replace a document / parsing tools", exact=True).click()
                page.locator("#parseOnly").check()
                page.locator("#submit").click()
                expect(page.locator("#panel-review")).to_be_visible(timeout=30000)
                expect(page.locator("#chunks > details")).to_have_count(3)
                expect(page.locator("#chunks > details").nth(1).locator("p.error")).to_contain_text("without a usable destination")
                card = page.locator("#chunks > details").first
                expect(card.locator("a")).to_have_attribute("href", "https://example.invalid/shared?lot=A17")
                page.locator("#batchReview > summary").click()
                page.locator("#selectPending").click()
                expect(page.locator("#selectionInfo")).to_have_text("2 selected")
                expect(page.locator("#chunks > details").nth(1).locator("input[data-select-chunk]")).not_to_be_checked()
                page.locator("#clearSelection").click()
                card.locator("input[data-select-chunk]").check()
                page.locator("#batchNote").fill("Verified the generated synthetic source and its link target.")
                page.locator("#approveSelected").click()
                expect(page.locator("#message")).to_contain_text("Confirm you checked every selected chunk")
                page.locator("#batchVerified").check()
                page.locator("#approveSelected").click()
                expect(page.locator("#chunks > details")).to_have_count(2)
                page.locator("#reviewStatus").select_option("")
                expect(page.locator("#chunks > details")).to_have_count(3)
                expect(page.locator("#chunks > details").first.locator("summary").first).to_contain_text("approved")
                expect(page.locator("#selectionInfo")).to_have_text("0 selected")
                expect(page.locator("#batchVerified")).not_to_be_checked()
                expect(page.locator("#chunks > details").nth(1).locator("summary").first).to_contain_text("pending")
                expect(page.locator("#chunks > details").nth(2).locator("summary").first).to_contain_text("pending")
                # Source text is rendered with text nodes, never raw HTML.
                page.locator("#chunks > details").nth(2).locator("summary").first.click()
                assert page.evaluate("window.sourceExecuted === undefined")
                expect(page.locator("#chunks img")).to_have_count(0)
                page.locator("#makeSearchable").click()
                expect(page.locator("#indexSummary")).to_contain_text("1 indexed", timeout=30000)
                page.locator("#tab-search").click()
                page.locator("#question").fill("Where is the synthetic schedule?")
                failure_route="**/api/retrieve"
                page.route(failure_route, lambda route: route.fulfill(status=400, content_type="application/json",
                    body=json.dumps({"detail":"Synthetic rerank permission is missing"})))
                page.locator("#retrieve").click()
                expect(page.locator("#searchSummary")).to_contain_text("Synthetic rerank permission is missing")
                expect(page.locator("#retrievalResults .chunk")).to_have_count(0)
                page.unroute(failure_route)
                page.locator("#retrieve").click()
                expect(page.locator("#retrievalResults .chunk")).to_have_count(1, timeout=30000)
                expect(page.locator("#retrievalResults a")).to_have_attribute("href", "https://example.invalid/shared?lot=A17")
                expect(page.locator("#rankingComparison")).to_be_hidden()
                page.screenshot(path=str(args.output / "search-desktop.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                page.screenshot(path=str(args.output / "search-mobile.png"), full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), page.evaluate("Array.from(document.querySelectorAll('body *')).filter(e=>e.getBoundingClientRect().right>innerWidth).map(e=>({tag:e.tagName,id:e.id,width:e.getBoundingClientRect().width})).slice(0,10)")
                # The normal path needs neither reviewer input nor review notes.
                page.locator("#tab-documents").click()
                page.locator("#parseOnly").uncheck()
                normal_source = Path(temporary) / "normal_ingestion.docx"
                normal = Document()
                normal.add_heading("Who coordinates the synthetic service?", 2)
                normal.add_paragraph("Muse coordinates the synthetic service.")
                normal.add_heading("Where is the missing destination?", 2)
                normal.add_paragraph("The destination can be found here.")
                normal.save(normal_source)
                page.locator("#file").set_input_files(normal_source)
                page.locator("#submit").click()
                expect(page.locator("#panel-search")).to_be_visible(timeout=30000)
                expect(page.locator("#message")).to_contain_text("1 accepted for search; 1 extraction issues held aside")
                page.locator("#tab-documents").click()
                selected_document = page.locator("#document").input_value()
                page.locator("#replaceSelected").click()
                expect(page.locator("#existingDoc")).to_have_value(selected_document)
                page.locator("#existingDoc").fill("")
                page.locator("#confirmShare").locator("..").locator("..").locator("summary").click()
                expect(page.locator("#shareVersion")).to_be_disabled()
                page.locator("#confirmShare").check()
                page.locator("#shareVersion").click()
                expect(page.locator("#shareSummary")).to_contain_text("now in the shared library")
                expect(page.locator("#confirmShare")).not_to_be_checked()
                page.locator("#tab-search").click()
                page.locator("#retrieve").click()
                expect(page.locator("#retrievalResults .chunk")).to_have_count(2, timeout=30000)
                expect(page.locator("#retrievalResults img")).to_have_count(0)
                page.screenshot(path=str(args.output / "automatic-ingestion-mobile.png"), full_page=True)
                # Admin review starts with held content, not hundreds of accepted
                # or excluded passages. Filters are server-side before pagination.
                page.locator("#tab-jobs").click()
                expect(page.locator("#jobList .job-card")).to_have_count(2)
                expect(page.locator("#jobList")).to_contain_text("normal_ingestion.docx")
                page.locator("#jobList .job-card").first.get_by_role("button", name="Open document", exact=True).click()
                expect(page.locator("#reviewStatus")).to_have_value("pending")
                expect(page.locator("#chunks > details")).to_have_count(1)
                expect(page.locator("#reviewCounts")).to_contain_text("Needs review")
                page.locator("#source").click()
                expect(page.locator("#previewHint")).to_contain_text("no native browser preview")
                expect(page.locator("#originalImage")).to_be_hidden()
                expect(page.locator("#originalDownload")).to_have_attribute("href", re.compile("^blob:"))
                page.locator("#reviewStatus").select_option("approved")
                expect(page.locator("#chunks > details")).to_have_count(1)
                expect(page.locator("#chunks > details").first.locator("summary").first).to_contain_text("approved")
                page.locator("#reviewPage").fill("999")
                page.locator("#reviewPage").press("Tab")
                expect(page.locator("#chunks > details")).to_have_count(0)
                expect(page.locator("#chunks")).to_contain_text("No passages match")
                page.locator("#clearReviewFilters").click()
                expect(page.locator("#chunks > details")).to_have_count(1)
                page.set_viewport_size({"width":1440,"height":1000})
                page.screenshot(path=str(args.output / "admin-review-desktop.png"), full_page=True)
                page.locator("#tab-documents").click()
                page.locator("#documentSearch").fill("normal_ingestion")
                expect(page.locator("#documentList .document-card")).to_have_count(1)
                page.locator("#documentSearch").fill("")
                page.locator("#cancelReplacement").click()
                page.locator("#uploadPanel > summary").click()
                page.evaluate("window.scrollTo(0,0)")
                page.screenshot(path=str(args.output / "admin-library-desktop.png"), full_page=True)
                page.set_viewport_size({"width":390,"height":844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(args.output / "admin-library-mobile.png"), full_page=True)
                # A real local PDF response can be previewed at its source page;
                # changing version/workspace revokes its blob and clears the frame.
                pdf_source=Path(temporary)/"synthetic_review_pages.pdf"
                with pymupdf.open() as pdf:
                    pdf.new_page().insert_text((72,100),"Synthetic first source page for local review.")
                    pdf.new_page().insert_text((72,100),"Synthetic second source page with different evidence.")
                    pdf.save(pdf_source)
                page.locator("#newUpload").click()
                if not page.locator("#parseOnly").is_visible():
                    page.get_by_text("Replace a document / parsing tools", exact=True).click()
                page.locator("#parseOnly").check()
                page.locator("#file").set_input_files(pdf_source)
                page.locator("#submit").click()
                expect(page.locator("#panel-review")).to_be_visible(timeout=30000)
                page.locator("#reviewPage").fill("2")
                page.locator("#reviewPage").press("Tab")
                expect(page.locator("#chunks > details")).to_have_count(1)
                expect(page.locator("#chunks")).to_contain_text("second source page")
                page.locator("#source").click()
                expect(page.locator("#originalImage")).to_be_visible()
                expect(page.locator("#previewHint")).to_contain_text("page 2 rendered from the original PDF")
                page.wait_for_function("document.getElementById('originalImage').naturalWidth > 0")
                expect(page.locator("#originalImage")).to_have_attribute("src",re.compile(r"^blob:"))
                page.locator("#chunks .page-group button").click()
                expect(page.locator("#selectionInfo")).to_have_text("1 selected")
                page.locator("#reviewStatus").select_option("approved")
                expect(page.locator("#chunks > details")).to_have_count(0)
                expect(page.locator("#selectionInfo")).to_have_text("0 selected")
                expect(page.locator("#batchVerified")).not_to_be_checked()
                page.locator("#reviewStatus").select_option("pending")
                expect(page.locator("#chunks > details")).to_have_count(1)
                page.set_viewport_size({"width":1440,"height":1000})
                page.evaluate("window.scrollTo(0,0)")
                page.screenshot(path=str(args.output / "admin-pdf-review-desktop.png"), full_page=True)
                page.locator("#tab-schedules").click()
                expect(page.locator("#panel-search")).to_be_hidden()
                expect(page.locator("#panel-schedules")).to_be_visible()
                page.locator("#tab-schedules").press("Home")
                expect(page.locator("#panel-documents")).to_be_visible()
                page.locator("#connection > summary").click()
                page.locator("#tenant").fill("different-workspace")
                expect(page.locator("#retrieve")).to_be_disabled()
                expect(page.locator("#retrievalResults")).to_be_empty()
                expect(page.locator("#jobList")).to_be_empty()
                expect(page.locator("#originalDownload")).to_be_hidden()
                expect(page.locator("#originalDownload")).not_to_have_attribute("href", re.compile("^blob:"))
                expect(page.locator("#originalImage")).not_to_have_attribute("src", re.compile("^blob:"))
                # The product home has no chunk-review controls or technical tabs.
                page.goto(base)
                page.set_viewport_size({"width":1280,"height":900})
                expect(page.locator("#login")).to_be_visible()
                expect(page.locator("#batchReview")).to_have_count(0)
                expect(page.locator("input[data-select-chunk]")).to_have_count(0)
                page.locator("#settings > summary").click()
                page.locator("#searchProvider").select_option("fake")
                page.locator("#accessToken").fill(token)
                page.get_by_text("Workspace settings",exact=True).click()
                page.locator("#workspace").fill(tenant)
                page.locator("#loginForm button").click()
                expect(page.locator("#ask")).to_be_visible(timeout=15000)
                page.locator("#documentsToggle").click()
                page.locator("#documentFile").set_input_files(normal_source)
                page.locator("#uploadButton").click()
                expect(page.locator("#notice")).to_contain_text("Document ready to search",timeout=30000)
                expect(page.locator("#documents")).to_be_hidden()
                page.locator("#askQuestion").fill("Who coordinates the synthetic service?")
                page.locator("#askButton").click()
                expect(page.locator("#matches > article.match")).to_have_count(1,timeout=30000)
                expect(page.locator("#matches > article.match")).to_contain_text("Muse coordinates the synthetic service")
                expect(page.locator("#counts")).to_contain_text("2 eligible passages")
                expect(page.locator("#matches img")).to_have_count(0)
                page.screenshot(path=str(args.output / "product-desktop.png"),full_page=True)
                page.set_viewport_size({"width":390,"height":844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(args.output / "product-mobile.png"),full_page=True)
                # Test generated-answer rendering without an external provider:
                # the search remains synthetic and only this browser response is amended.
                def answer_fixture(route):
                    payload = route.fetch().json()
                    part = payload["results"][0]["context_parts"][0]
                    payload["answer"] = {"status": "answered", "text": "Synthetic answer <img src=x onerror=alert(1)> [E1]",
                        "provider": "ollama", "model": "synthetic-ui-fixture", "fallback_used": True,
                        "citations": [{"source_id": "E1", **{key: part.get(key) for key in
                            ("version_id", "chunk_id", "source_file", "page_start")}}]}
                    route.fulfill(json=payload)
                page.route("**/api/retrieve", answer_fixture)
                page.locator("#askButton").click()
                expect(page.locator("#matches > article.match")).to_have_count(2,timeout=30000)
                expect(page.locator("#matches > article.match").first).to_contain_text("local fallback")
                expect(page.locator("#matches > article.match").first.locator("a")).to_have_attribute("href", re.compile(r"#source-"))
                expect(page.locator("#matches img")).to_have_count(0)
                page.unroute("**/api/retrieve", answer_fixture)
                def schedule_fixture(route):
                    route.fulfill(json={"route": "schedules", "status": "results", "has_more": False,
                        "results": [{"technology_nm": 28, "flavor": "Synthetic only", "tapeout_date": "2030-01-01",
                            "final_deadline": "2029-12-20", "estimated_ship_date": "2030-04-01"}]})
                page.route("**/api/retrieve", schedule_fixture)
                page.locator("#askButton").click()
                expect(page.locator("#matches table")).to_have_count(1,timeout=30000)
                expect(page.locator("#searchStatus")).to_contain_text("1 exact schedule rows")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.unroute("**/api/retrieve", schedule_fixture)
                page.locator("#disconnect").click()
                expect(page.locator("#login")).to_be_visible()
                expect(page.locator("#accessToken")).to_have_value("")
                expect(page.locator("#matches")).to_be_empty()
                assert not failures, failures
                assert not external_requests, external_requests
                browser.close()
                print(json.dumps({"browser": "headless Edge", "workflow": "upload, auto-parse, source review, publish/index, mandatory-reranked search",
                    "safe_source_links": "passed", "source_html_inert": "passed", "keyboard_tabs": "passed",
                    "workspace_reset": "passed", "mobile_overflow": "none", "browser_errors": failures,
                    "automatic_ingestion_without_notes": "passed",
                    "admin_jobs_and_filtered_review": "passed",
                    "admin_library_and_update_controls": "passed",
                    "office_preview_is_download_only": "passed",
                    "pdf_preview_and_page_group_review": "passed",
                    "simplified_product_home": "passed",
                    "generated_answer_safe_rendering": "passed (synthetic response fixture)",
                    "schedule_results_mobile": "passed (synthetic response fixture)",
                    "external_requests": external_requests, "screenshots": str(args.output)}, indent=2))
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()
            # Each run owns this random collection; do not accumulate test vectors.
            client = QdrantClient(url=settings.qdrant_url)
            try:
                collection = settings.collection + "_synthetic"
                if client.collection_exists(collection):
                    client.delete_collection(collection)
            finally:
                client.close()


if __name__ == "__main__":
    main()
