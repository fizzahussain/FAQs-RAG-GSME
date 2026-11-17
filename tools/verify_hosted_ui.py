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
from docx import Document
import pymupdf

from gsme_rag.storage.config import from_env
from gsme_rag.hosted.app import create_hosted_app
from gsme_rag.hosted.access import Principal
from gsme_rag.hosted.identity import InvalidIdentity
from gsme_rag.storage.vectors import QdrantEvidence


def finish_stream(route, result):
    route.fulfill(content_type='text/event-stream', body='event: done\ndata: ' + json.dumps({'result':result}) + '\n\n')


def verify_guest(browser, origin, output, fixture, errors, unexpected):
    """Exercise real upload/storage/retrieval with synthetic, in-process models."""
    contexts = [browser.new_context(viewport={"width": 1280, "height": 900}) for _ in range(2)]
    def network(route):
        if route.request.url.startswith(origin + "/"):
            route.continue_()
        else:
            unexpected.append(route.request.url)
            route.abort()
    for context in contexts:
        context.route("**/*", network)
    page, other = [context.new_page() for context in contexts]
    for item in (page, other):
        item.on("pageerror", lambda error: errors.append(str(error)))
        item.goto(origin)
        try:
            expect(item.locator("#send")).to_be_enabled(timeout=30000)
        except AssertionError:
            raise AssertionError({"notice": item.locator("#notice").inner_text(),
                                  "browser_errors": errors, "unexpected_network": unexpected}) from None
        expect(item.locator("#signin")).to_be_hidden()
    source = Path(fixture.temp.name) / "browser-upload.docx"
    doc = Document()
    doc.add_heading("What is the synthetic upload label?", 2)
    doc.add_paragraph("The synthetic upload label is VIOLET_372.")
    doc.save(source)
    with page.expect_file_chooser() as chooser:
        page.get_by_role("button", name="Upload a document", exact=True).click()
    chooser.value.set_files(str(source))
    expect(page.locator("#notice")).to_contain_text("Ready to chat", timeout=60000)
    expect(page.locator("#scope option")).to_have_count(3)
    expect(page.locator('#scope option').first).to_have_text('All files')
    expect(page.locator("#document-links")).to_contain_text(source.name)
    expect(page.locator("#document-links")).not_to_contain_text("searchable passages")
    expect(page.locator("#notice")).not_to_contain_text("passages indexed")
    assert "All accessible documents" not in page.content()
    version = page.request.get(origin + "/api/documents").json()["documents"]
    uploaded = next(item for item in version if item["source_file"] == source.name)
    assert other.request.get(origin + "/api/source/" + uploaded["version_id"]).status == 404
    assert source.name not in other.content()
    assert page.request.get(origin + "/api/source/" + uploaded["version_id"]).status == 200
    with page.expect_response(lambda response: response.url == origin + "/api/preview/" + uploaded["version_id"]) as opened:
        page.locator("#document-links").get_by_role("button", name=source.name, exact=True).click()
    assert opened.value.status == 200
    expect(page.locator("#document-viewer")).to_be_visible()
    expect(page.locator("#viewer-text")).to_contain_text("VIOLET_372")
    expect(page.locator("#viewer-note")).to_contain_text("not the original Office layout")
    page.locator("#viewer-close").click()
    expect(page.locator("#document-viewer")).not_to_be_visible()
    page.locator("#question").fill("What is the synthetic upload label?")
    page.locator("#question").press("Shift+Enter")
    assert page.locator("#question").input_value().endswith("\n")
    page.locator("#question").press("Enter")
    expect(page.locator(".assistant")).to_contain_text("VIOLET_372", timeout=30000)
    expect(page.locator(".assistant details.cited-source")).to_have_count(1)
    page.get_by_role('button',name='[E1]',exact=True).click()
    assert page.locator('.assistant details.cited-source').evaluate('(element) => element.open')
    expect(page.locator('.assistant details.cited-source')).to_contain_text('VIOLET_372')
    expect(page.locator('#chat-list .chat-history-open')).to_have_count(1)
    saved_chat = page.request.get(origin + '/api/chats').json()['chats'][0]
    assert other.request.get(origin + '/api/chats/' + saved_chat['id']).status == 404
    # Desktop collapse must give the timeline its width back and be reversible.
    page.locator('#close-sidebar').click()
    expect(page.locator('#sidebar')).not_to_be_visible()
    assert page.locator('#open-sidebar').get_attribute('aria-expanded') == 'false'
    page.locator('#open-sidebar').click()
    expect(page.locator('#sidebar')).to_be_visible()
    page.reload()
    expect(page.locator('#messages')).to_contain_text('VIOLET_372', timeout=30000)
    expect(page.locator('#send')).to_be_enabled()
    page.locator('#new-chat').click()
    expect(page.locator('#messages')).to_be_empty()
    page.locator('#question').fill('hello')
    page.locator('#question').press('Enter')
    expect(page.locator('#messages')).to_contain_text("I'm ready to help")
    expect(page.locator('#chat-list .chat-history-open')).to_have_count(2)
    page.locator('#chat-list .chat-history-open').filter(has_text='What is the synthetic upload label?').click()
    expect(page.locator('#messages')).to_contain_text('VIOLET_372')
    page.once('dialog', lambda dialog: dialog.accept('Greeting history'))
    page.get_by_role('button', name='Rename chat hello', exact=True).click()
    expect(page.locator('#chat-list')).to_contain_text('Greeting history')
    page.once('dialog', lambda dialog: dialog.accept())
    page.get_by_role('button', name='Delete chat Greeting history', exact=True).click()
    expect(page.locator('#chat-list .chat-history-open')).to_have_count(1)
    expect(page.locator('#messages')).to_contain_text('VIOLET_372')
    if output:
        page.screenshot(path=str(output / 'cited-answer-desktop.png'), full_page=True)
    # Five retrieved copies must not become five source cards. Exercise aliases
    # and unused evidence explicitly, without asking a real model to invent them.
    duplicated = {"route":"documents","status":"results","request_id":"synthetic-duplicate-test",
        "answer":{"status":"answered","text":"Synthetic label VIOLET_372. [E1] [E2]"},
        "answer_sources":[{"source_file":source.name,"passages":[{"quote":"The synthetic upload label is VIOLET_372.",
            "references":[{"source_id":label,"version_id":uploaded['version_id'],"chunk_id":'synthetic-part','page_start':None}
                          for label in ('E1','E2')]}]}],
        "results":[{"source_file":source.name,"text":"UNUSED_RETRIEVED_TEXT"}]*5,"rerank_input_count":5}
    page.route('**/api/chat/stream',lambda route:finish_stream(route,duplicated))
    page.locator('#question').fill('Synthetic duplicate citation test')
    page.locator('#question').press('Enter')
    last=page.locator('.assistant').last
    expect(last.locator('details.cited-source')).to_have_count(1)
    expect(last.locator('details.cited-source section')).to_have_count(1)
    expect(last).not_to_contain_text('UNUSED_RETRIEVED_TEXT')
    last.get_by_role('button',name='[E2]',exact=True).click()
    assert last.locator('details.cited-source').evaluate('(element) => element.open')
    expect(page.locator('#send')).to_be_enabled()
    page.unroute('**/api/chat/stream')
    # Long answers must stay inside the timeline, with the composer reachable.
    long_reply={**duplicated,'answer':{'status':'answered','text':'\n\n'.join(
        f'Synthetic paragraph {number}: '+('Readable test content. '*12) for number in range(40))+' [E1]'}}
    page.route('**/api/chat/stream',lambda route:finish_stream(route,long_reply))
    page.locator('#question').fill('Synthetic long answer layout test')
    page.locator('#question').press('Enter')
    expect(page.locator('.assistant').last).to_contain_text('Synthetic paragraph 39')
    expect(page.locator('#send')).to_be_enabled()
    assert page.evaluate('document.documentElement.scrollHeight <= window.innerHeight + 1')
    composer=page.locator('#ask').bounding_box()
    assert composer['y'] >= 0 and composer['y']+composer['height'] <= 900
    page.unroute('**/api/chat/stream')
    waiting=[]
    page.route('**/api/chat/stream',lambda route:waiting.append(route))
    page.locator('#question').fill('Synthetic preserve reading position test')
    page.locator('#question').press('Enter')
    page.wait_for_function('() => document.querySelector(".thinking") !== null')
    page.locator('#conversation-scroll').evaluate('(element) => {element.scrollTop=0; element.dispatchEvent(new Event("scroll"));}')
    expect(page.locator('#jump-latest')).to_be_visible()
    assert waiting
    finish_stream(waiting[0],duplicated)
    expect(page.locator('#send')).to_be_enabled()
    assert page.locator('#conversation-scroll').evaluate('(element) => element.scrollTop') < 3
    page.locator('#jump-latest').click()
    page.wait_for_function('() => {const e=document.getElementById("conversation-scroll");return e.scrollHeight-e.clientHeight-e.scrollTop<3}')
    page.unroute('**/api/chat/stream')
    # A controlled browser stream proves provisional text appears before done,
    # split UTF-8/CRLF frames are decoded, and EOF/Stop remove unfinished drafts.
    page.evaluate('''() => {
      window.fixtureRealFetch=window.fetch;
      window.fetch=(url, options) => {
        if (!String(url).endsWith('/api/chat/stream')) return window.fixtureRealFetch(url, options);
        const encoder=new TextEncoder();
        const stream=new ReadableStream({start(controller) {
          window.fixtureStreamController=controller;
          const event='event: claim\\r\\ndata: '+JSON.stringify({index:0,text:'Synthetic µ streamed claim. [E1]'})+'\\r\\n\\r\\n';
          const bytes=encoder.encode(event), split=bytes.indexOf(194)+1;
          controller.enqueue(bytes.slice(0,split));
          setTimeout(()=>controller.enqueue(bytes.slice(split)),30);
        }});
        return Promise.resolve(new Response(stream,{headers:{'Content-Type':'text/event-stream'}}));
      };
    }''')
    page.locator('#question').fill('Synthetic incremental UI check')
    page.locator('#question').press('Enter')
    expect(page.locator('.streaming-draft')).to_contain_text('Synthetic µ streamed claim.')
    expect(page.locator('#stop-reply')).to_be_visible()
    assert page.locator('#send').is_disabled()
    page.evaluate('''result => {
      const encoder=new TextEncoder();
      window.fixtureStreamController.enqueue(encoder.encode('event: done\\ndata: '+JSON.stringify({result})+'\\n\\n'));
      window.fixtureStreamController.close();
      window.fetch=window.fixtureRealFetch;
    }''',duplicated)
    expect(page.locator('#send')).to_be_enabled()
    expect(page.locator('.streaming-draft')).to_have_count(0)
    page.evaluate('''() => {
      window.fetch=(url, options) => {
        if (!String(url).endsWith('/api/chat/stream')) return window.fixtureRealFetch(url, options);
        const stream=new ReadableStream({start(controller) {
          controller.enqueue(new TextEncoder().encode('event: claim\\ndata: '+JSON.stringify({index:0,text:'DISCARDED_UNFINISHED_DRAFT'})+'\\n\\n'));
          controller.close();
        }});
        return Promise.resolve(new Response(stream,{headers:{'Content-Type':'text/event-stream'}}));
      };
    }''')
    page.locator('#question').fill('Synthetic unexpected EOF check')
    page.locator('#question').press('Enter')
    expect(page.locator('#messages')).to_contain_text('connection ended before the reply was finalized')
    expect(page.locator('#messages')).not_to_contain_text('DISCARDED_UNFINISHED_DRAFT')
    page.evaluate('''() => {
      window.fetch=(url, options) => {
        if (!String(url).endsWith('/api/chat/stream')) return window.fixtureRealFetch(url, options);
        const stream=new ReadableStream({start(controller) {
          options.signal.addEventListener('abort',()=>controller.error(new DOMException('Stopped','AbortError')),{once:true});
          controller.enqueue(new TextEncoder().encode('event: claim\\ndata: '+JSON.stringify({index:0,text:'STOPPED_STREAM_DRAFT'})+'\\n\\n'));
        }});
        return Promise.resolve(new Response(stream,{headers:{'Content-Type':'text/event-stream'}}));
      };
    }''')
    page.locator('#question').fill('Synthetic Stop control check')
    page.locator('#question').press('Enter')
    expect(page.locator('.streaming-draft')).to_contain_text('STOPPED_STREAM_DRAFT')
    page.locator('#stop-reply').click()
    expect(page.locator('#messages')).to_contain_text('Reply stopped. No incomplete draft was saved.')
    expect(page.locator('#messages')).not_to_contain_text('STOPPED_STREAM_DRAFT')
    expect(page.locator('#send')).to_be_enabled()
    page.evaluate('window.fetch=window.fixtureRealFetch')
    page.locator('#new-chat').click()
    expect(page.locator('#messages')).to_be_empty()
    expect(page.locator('#welcome')).to_be_visible()
    expect(page.locator('#document-links')).to_contain_text(source.name)
    page.locator('#document-search').fill('browser-upload')
    expect(page.locator('#document-links .document-link:visible')).to_have_count(1)
    page.locator('#document-search').fill('')
    if output:
        page.screenshot(path=str(output / "guest-upload-chat.png"), full_page=True)
    page.reload()
    expect(page.locator("#scope option")).to_have_count(3)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    if output:
        page.screenshot(path=str(output / "guest-mobile.png"), full_page=True)
    # Verify actual PDF bytes are displayed in the in-page frame, not downloaded.
    pdf_path = Path(fixture.temp.name) / "inline-preview.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), 'Synthetic original PDF preview.')
        pdf.save(pdf_path)
    downloads = []
    page.on('download', lambda download: downloads.append(download.suggested_filename))
    with page.expect_file_chooser() as chooser:
        page.get_by_role("button", name="Upload a document", exact=True).click()
    chooser.value.set_files(str(pdf_path))
    expect(page.locator("#scope option")).to_have_count(4)
    expect(page.locator("#notice")).to_contain_text("Ready to chat", timeout=60000)
    page.locator('#open-sidebar').click()
    expect(page.locator('#sidebar')).to_be_visible()
    page.locator("#document-links").get_by_role("button", name=pdf_path.name, exact=True).click()
    expect(page.locator("#viewer-pdf")).to_be_visible()
    assert page.locator("#viewer-pdf").get_attribute("src").startswith('blob:')
    page.locator("#viewer-close").click()
    expect(page.locator('#sidebar')).not_to_be_visible()
    page.set_viewport_size({'width':390,'height':480})
    composer=page.locator('#ask').bounding_box()
    assert composer['y'] >= 0 and composer['y']+composer['height'] <= 480
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    page.locator('#open-sidebar').click()
    page.keyboard.press('Escape')
    expect(page.locator('#sidebar')).not_to_be_visible()
    assert not page.locator("#viewer-pdf").get_attribute("src")
    assert not downloads, downloads
    # Removing an upload revokes both the visible file and the source endpoint.
    pdf_document = next(doc for doc in page.request.get(origin + '/api/documents').json()['documents']
                        if doc['source_file'] == pdf_path.name)
    with fixture.store.connect() as db:
        db.execute('UPDATE document_access SET deletion_protected=true WHERE document_id=%s',
                   (pdf_document['document_id'],))
    page.reload()
    expect(page.locator('#send')).to_be_enabled()
    page.locator('#open-sidebar').click()
    expect(page.get_by_role('button', name='Delete ' + pdf_path.name, exact=True)).to_have_count(0)
    expect(page.locator('#document-links')).to_contain_text(pdf_path.name)
    with fixture.store.connect() as db:
        db.execute('UPDATE document_access SET deletion_protected=false WHERE document_id=%s',
                   (pdf_document['document_id'],))
    page.locator('#refresh').click()
    expect(page.get_by_role('button', name='Delete ' + pdf_path.name, exact=True)).to_be_visible()
    page.once('dialog', lambda dialog: dialog.accept())
    page.get_by_role('button', name='Delete ' + pdf_path.name, exact=True).click()
    expect(page.locator('#document-links')).not_to_contain_text(pdf_path.name)
    expect(page.locator('#scope option')).to_have_count(3)
    assert other.request.get(origin + '/api/source/' + uploaded['version_id']).status == 404
    # Exact schedule mode uses the ACL-protected local endpoint, never chat/LLMs.
    page.set_viewport_size({"width": 1280, "height": 900})
    page.locator('#close-sidebar').click()
    expect(page.get_by_text('Source-linked answers', exact=True)).to_have_count(0)
    page.locator('#mode-schedules').click()
    expect(page.locator('#schedule-workspace')).to_be_visible()
    expect(page.locator('#document-scope')).not_to_be_visible()
    page.locator('#question').fill('Show both schedules')
    page.locator('#question').press('Enter')
    expect(page.locator('#schedule-results')).to_contain_text('No schedule source is available')
    schedule_path = Path(fixture.temp.name) / 'shared_schedules.csv'
    schedule_path.write_text('technology_nm,flavor,trial_deadline,final_deadline,tapeout_date,estimated_ship_date\n28,HPC+ RF,2026-11-01,2026-11-08,2026-11-15,2027-02-15\n', encoding='utf-8')
    with page.expect_file_chooser() as chooser:
        page.get_by_role('button', name='Upload a document', exact=True).click()
    chooser.value.set_files(str(schedule_path))
    expect(page.locator('#notice')).to_contain_text('Ready for schedule search', timeout=60000)
    page.locator('#schedule-kind').select_option('shared')
    page.locator('#question').fill('Show 28nm schedules')
    page.locator('#question').press('Enter')
    expect(page.locator('#schedule-results')).to_contain_text('2026-11-15')
    expect(page.locator('#schedule-results')).to_contain_text('row 2')
    expect(page.locator('#schedule-results')).to_contain_text('Not a live capacity confirmation')
    assert page.locator('#scope option', has_text=schedule_path.name).count() == 0
    expect(page.get_by_role('button', name='Retry ' + schedule_path.name, exact=True)).to_have_count(0)
    assert other.request.post(origin + '/api/schedules/search', headers={'X-GSME-Session':'1'},
                              data={'question':'Show both schedules'}).json()['total'] == 0
    page.get_by_role('button', name='Open source', exact=True).click()
    expect(page.locator('#document-viewer')).to_be_visible()
    page.locator('#viewer-close').click()
    page.locator('#question').fill('Show full schedules')
    page.locator('#question').press('Enter')
    expect(page.locator('#schedule-results')).to_contain_text('Conflicting schedule_kind')
    page.locator('#schedule-kind').select_option('')
    page.locator('#question').fill('Show shared 3nm schedules')
    page.locator('#question').press('Enter')
    expect(page.locator('#schedule-results')).to_contain_text('No matching schedule records')
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    page.locator('#mode-chat').click()
    expect(page.locator('#schedule-workspace')).not_to_be_visible()
    assert not page.locator('#messages').evaluate('(element) => element.hidden')
    expect(page.locator('#welcome')).to_be_visible()
    assert not errors and not unexpected, (errors, unexpected)
    for context in contexts:
        context.close()
    return {"schedule_mode_upload_exact_search_provenance_isolation": "passed",
            "schedule_conflicts_empty_states_mobile_navigation": "passed",
            "no_login_upload_index_chat": "passed", "browser_isolation": "passed",
            "named_document_links": "passed", "filename_click_opens_authorized_source": "passed",
            "plus_upload": "passed", "enter_send_shift_enter_newline": "passed",
            "office_text_preview": "passed", "pdf_inline_preview_no_download": "passed",
            "only_cited_sources_and_clickable_reference": "passed",
            "duplicate_aliases_one_source_unused_passages_hidden": "passed",
            "fixed_composer_long_answers": "passed", "reading_position_preserved": "passed",
            "all_files_short_label_and_owner_delete": "passed",
            "required_document_has_no_delete_control": "passed",
            "new_chat_keeps_documents": "passed", "sidebar_search_and_mobile_drawer": "passed",
            "desktop_sidebar_collapse": "passed", "persistent_multiple_chats": "passed",
            "chat_rename_and_delete": "passed",
            "incremental_claim_stream_and_utf8": "passed", "truncated_draft_discarded": "passed",
            "stop_reply_discards_provisional_output": "passed",
            "source_download": "passed", "cookie_reload": "passed", "mobile_overflow": "none",
            "real_external_calls": 0, "models": "synthetic embeddings/reranker and mocked Command",
            "browser_errors": errors, "unexpected_network": unexpected}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional screenshots/report directory; default writes no artifacts")
    parser.add_argument("--guest", action="store_true", help="Test no-login browser uploads instead of mocked Google sign-in")
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
    # unittest normally owns this instance. Retain an explicit reference for
    # the whole browser run and always execute fixture cleanup on setup failure.
    try:
        fixture.setUp()
    except BaseException:
        fixture.doCleanups()
        raise
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    hosted = replace(fixture.hosted, origin=origin, google_client_id="synthetic.apps.googleusercontent.com",
                     workspace_domains=("company.example",), guest_uploads=args.guest)
    if args.guest:
        # Test-only objects exercise the real answer validator with a mocked
        # Command response. This does not certify real model answer quality.
        fixture.provider.external = fixture.reranker.external = True

    def command_answer(question, evidence):
        item = next(item for item in evidence if "VIOLET_372" in item["text"])
        return json.dumps({"status": "answered", "claims": [{"text": "The synthetic upload label is VIOLET_372.",
            "supports": [{"quote_id": item["quote_id"]}]}]})

    def command_stream(question, evidence):
        raw=command_answer(question,evidence)
        for start in range(0,len(raw),37):
            yield raw[start:start+37]

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
        with patch("gsme_rag.hosted.app.require_answer_permission"), patch.dict(os.environ, {"GSME_ANSWER_PROVIDER": "cohere"}), patch("cohere.ClientV2", side_effect=AssertionError("External model client forbidden in browser fixture")), patch("gsme_rag.generation.answers.CommandAnswers.generate", side_effect=command_answer), patch("gsme_rag.generation.answers.CommandAnswers.generate_stream", side_effect=command_stream):
            thread.start()
            deadline = time.monotonic() + 15
            while not server.started and time.monotonic() < deadline:
                time.sleep(.05)
            if not server.started:
                raise RuntimeError("Synthetic hosted server did not start")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                if args.guest:
                    result = verify_guest(browser, origin, args.output, fixture, errors, unexpected)
                    browser.close()
                    if args.output:
                        (args.output / "report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
                    print(json.dumps(result))
                    return
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
                if args.output:
                    page.screenshot(path=str(args.output / "private-owner-chat.png"), full_page=True)

                pending = []
                page.route("**/api/chat/stream", lambda route: pending.append(route))
                page.locator("#question").fill("Show my private contact")
                page.locator("#send").click()
                page.wait_for_timeout(100)
                page.locator("#signout").click()
                expect(page.locator("#messages")).to_be_empty()
                expect(page.locator("#document-links")).not_to_contain_text("alice.docx")
                assert pending
                finish_stream(pending[0], {"route": "documents", "status": "results",
                    "answer": {"text": "COBALT_914 delayed private response"}, "results": []})
                page.wait_for_timeout(300)
                assert "COBALT_914" not in page.content()
                expect(page.locator("#scope option")).to_have_count(2)
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                if args.output:
                    page.screenshot(path=str(args.output / "public-mobile.png"), full_page=True)
                assert not errors and not unexpected, (errors, unexpected)
                browser.close()
        result = {"guest_faq_only": "passed", "owner_only_documents": "passed", "private_source_rendering": "passed",
                  "logout_clears_history": "passed", "late_private_response_after_logout": "discarded",
                  "mobile_overflow": "none", "real_external_calls": 0, "google_signin": "mocked, not live company SSO",
                  "browser_errors": errors, "unexpected_network": unexpected}
        if args.output:
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
