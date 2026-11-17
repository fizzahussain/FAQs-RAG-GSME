import os
import time
from dataclasses import replace
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import Mock, MagicMock, patch
from uuid import uuid4

from docx import Document
from fastapi.testclient import TestClient

from gsme_rag.hosted.access import AccessStore, Principal, publish_faq
from gsme_rag.hosted.app import create_hosted_app, HostedSettings, cited_source_groups
from gsme_rag.hosted.identity import google_principal, InvalidIdentity, IdentityUnavailable
from gsme_rag.retrieval.reranking import FakeReranker
from gsme_rag.storage.config import Settings
from gsme_rag.storage.pipeline import process_ingestion, process_index
from gsme_rag.storage.postgres import EvidenceStore
from gsme_rag.storage.providers import FakeEmbeddings, ProviderOperationError, ProviderConfigurationError
from gsme_rag.storage.vectors import QdrantEvidence


class HostedCitationTests(TestCase):
    def test_only_used_evidence_is_grouped_and_duplicate_quotes_keep_labels(self):
        parts = [{'version_id':str(uuid4()), 'chunk_id':f'chunk-{i}', 'source_file':'same.pdf',
                  'source_sha256':'same-original-hash', 'text':text, 'page_start':i+1}
                 for i,text in enumerate(['Shared quote', 'Shared quote', 'Different used quote', 'Unused passage'])]
        answer = {'status':'answered','claims':[{'supports':[
            {'source_id':f'E{i+1}','quote':p['text']} for i,p in enumerate(parts[:3])]}],
            'citations':[{'source_id':f'E{i+1}','version_id':p['version_id'],'chunk_id':p['chunk_id']}
                         for i,p in enumerate(parts[:3])]}
        groups = cited_source_groups(answer,[{'context_parts':parts}])
        self.assertEqual(len(groups),1)
        self.assertEqual(len(groups[0]['passages']),2)
        self.assertEqual([r['source_id'] for r in groups[0]['passages'][0]['references']],['E1','E2'])
        self.assertNotIn('Unused passage',str(groups))
        self.assertEqual(cited_source_groups({**answer,'status':'insufficient_evidence'},[{'context_parts':parts}]),[])
        self.assertEqual(cited_source_groups(answer,[]),[])

    def test_quote_must_belong_to_the_exact_visible_cited_part(self):
        part={'version_id':str(uuid4()),'chunk_id':'visible','source_file':'file.pdf','text':'approved text'}
        answer={'status':'answered','claims':[{'supports':[{'source_id':'E1','quote':'invented quote'}]}],
                'citations':[{'source_id':'E1','version_id':part['version_id'],'chunk_id':'visible'}]}
        self.assertEqual(cited_source_groups(answer,[{'context_parts':[part]}]),[])


try:
    GOOGLE_AUTH_AVAILABLE = importlib.util.find_spec("google.auth") is not None
except ModuleNotFoundError:
    GOOGLE_AUTH_AVAILABLE = False


@skipUnless(GOOGLE_AUTH_AVAILABLE, "Install .[hosted] for Google identity adapter tests")
class HostedIdentityTests(TestCase):
    def test_google_signed_domain_subject_and_audience_checks(self):
        claims = dict(iss="https://accounts.google.com", aud="client", email_verified=True,
                      hd="company.example", sub="stable-id", email="renamable@company.example")
        with patch("google.oauth2.id_token.verify_oauth2_token", return_value=claims) as verify:
            self.assertEqual(google_principal("synthetic", "client", ("company.example",)), Principal("google:stable-id"))
            self.assertEqual(verify.call_args.args[2], "client")
            for key, value in (("aud", "other"), ("iss", "forged"), ("hd", "evil.example"), ("email_verified", False), ("sub", "")):
                with self.subTest(key=key):
                    with patch("google.oauth2.id_token.verify_oauth2_token", return_value={**claims, key: value}):
                        with self.assertRaises(InvalidIdentity):
                            google_principal("synthetic", "client", ("company.example",))
        with self.assertRaises(IdentityUnavailable):
            google_principal("synthetic", "", ())

    def test_google_errors_do_not_leak_credentials_and_origin_requires_https(self):
        with patch("google.oauth2.id_token.verify_oauth2_token", side_effect=ValueError("private token")):
            with self.assertRaisesRegex(InvalidIdentity, "^Google sign-in could not be verified$"):
                google_principal("synthetic", "client", ("company.example",))
        with self.assertRaises(ValueError):
            HostedSettings(origin="http://public.example")
        with self.assertRaises(ValueError):
            HostedSettings(google_client_id="not-a-client")


class HostedResourceTests(TestCase):
    def test_local_failures_have_safe_actionable_json_and_stream_messages(self):
        from gsme_rag.hosted.app import LOCAL_SEARCH_ERRORS
        store = Mock();store.connect.return_value = MagicMock()
        settings = Settings("unused", "unused", Path("data"))
        for code, message in LOCAL_SEARCH_ERRORS.items():
            with self.subTest(code=code), patch("gsme_rag.hosted.app.require_answer_permission",
                    side_effect=ProviderOperationError("PRIVATE_PROVIDER_DETAILS", error_code=code)):
                with TestClient(create_hosted_app(settings, store=store, auto_worker=False),
                                base_url="http://127.0.0.1:8020") as client:
                    response = client.post("/api/chat", json={"question":"Synthetic source question"})
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json()["detail"], message)
                    self.assertEqual(response.json()["code"], code)
                    stream = client.post("/api/chat/stream", json={"question":"Synthetic source question"})
                    self.assertIn("event: error", stream.text)
                    self.assertIn(code, stream.text)
                    self.assertNotIn("PRIVATE_PROVIDER_DETAILS", response.text + stream.text)

    def test_clear_stage_question_returns_cited_scopes_through_real_answer_handler(self):
        import json
        store=Mock();store.connect.return_value=MagicMock()
        settings=Settings('unused','unused',Path('data'))
        parts=[]
        for index,(name,text,page) in enumerate([
            ('operations.pdf','Manufacturing involves three main stages: fabrication, assembly, and testing.',18),
            ('readiness.pdf','In chip manufacturing, there are three main stages before a product is ready for the market: Multi-Project Wafer, Engineering Sample, and Mass Production.',11)]):
            parts.append({'version_id':str(uuid4()),'chunk_id':f'stage-{index}','source_file':name,
                'source_sha256':str(index+1)*64,'page_start':page,'text':text})
        report={'status':'results','reranking_status':'completed','synthetic':False,
                'results':[{'context_complete':True,'context_parts':parts}], 'rerank_input_count':2,'timings_ms':{}}
        primary=Mock();primary.name='cohere';primary.model='synthetic-clarification'
        primary.generate.return_value=json.dumps({'status':'clarification_needed','claims':[]})
        with patch('gsme_rag.hosted.app.require_answer_permission'), \
             patch('gsme_rag.hosted.app.CohereEmbeddings'),patch('gsme_rag.hosted.app.CohereReranker'), \
             patch('gsme_rag.hosted.app.QdrantEvidence'),patch('gsme_rag.hosted.app.retrieve',return_value=report), \
             patch('gsme_rag.hosted.access.AccessStore.is_source_current_approved',return_value=True), \
             patch('gsme_rag.generation.answers.CommandAnswers',return_value=primary):
            with TestClient(create_hosted_app(settings,store=store),base_url='http://127.0.0.1:8020') as client:
                response=client.post('/api/chat',json={'question':'what are the three main semiconductor manufacturing stages?'})
                self.assertEqual(response.status_code,200,response.text)
                body=response.json();self.assertEqual(body['answer']['status'],'answered')
                self.assertEqual(body['answer']['answer_protocol'],'verbatim_stage_extracts_v1')
                self.assertEqual(len(body['answer_sources']),2)
                self.assertIn('fabrication, assembly, and testing',body['answer']['text'])
                self.assertIn('not one combined sequence',body['answer']['text'])

    def test_chat_reports_quota_or_cooldown_without_claiming_reranker_failed(self):
        store=Mock();store.connect.return_value=MagicMock()
        settings=Settings('unused','unused',Path('data'))
        for code in ("quota_exhausted","rate_limit"):
            with self.subTest(code=code), patch('gsme_rag.hosted.app.require_answer_permission',
                    side_effect=ProviderOperationError("private provider response",error_code=code,retry_after_seconds=75)):
                with TestClient(create_hosted_app(settings,store=store,auto_worker=False),base_url='http://127.0.0.1:8020') as client:
                    response=client.post('/api/chat',json={'question':'Synthetic source question'})
                    self.assertEqual(response.status_code,503)
                    self.assertEqual(response.json()['code'],code)
                    self.assertNotIn('private provider response',response.text)
                    self.assertNotIn('unreranked',response.text)
                    if code=="rate_limit":self.assertEqual(response.headers['Retry-After'],'75')
                    else:self.assertIn('quota',response.json()['detail'])

    def test_hosted_stage_logs_share_one_request_id_without_prompt_or_token(self):
        from gsme_rag.observability import REQUEST_ID, emit
        from uuid import UUID
        store=Mock();store.connect.return_value=MagicMock()
        settings=Settings('unused','unused',Path('data'))
        def retrieve_stage(*args,**kwargs):
            emit('retrieval.test.stage')
            return {'status':'no_eligible_evidence','results':[],'rerank_input_count':0,'timings_ms':{}}
        def answer_stage(*args,**kwargs):
            emit('answer.test.stage')
            return {'status':'insufficient_evidence'}
        with patch('gsme_rag.hosted.app.require_answer_permission'), \
             patch('gsme_rag.hosted.app.CohereEmbeddings'),patch('gsme_rag.hosted.app.CohereReranker'), \
             patch('gsme_rag.hosted.app.QdrantEvidence'), \
             patch('gsme_rag.hosted.app.retrieve',side_effect=retrieve_stage), \
             patch('gsme_rag.hosted.app.generate_answer',side_effect=answer_stage):
            with TestClient(create_hosted_app(settings,store=store),base_url='http://127.0.0.1:8020') as client:
                with self.assertLogs('gsme_rag',level='INFO') as logs:
                    response=client.post('/api/chat',json={'question':'private-question-sentinel'})
        request_id=response.headers['X-GSME-Request-ID'];UUID(request_id)
        self.assertEqual(response.json()['request_id'],request_id)
        self.assertIsNone(REQUEST_ID.get())
        self.assertTrue(all(request_id in line for line in logs.output))
        self.assertNotIn('private-question-sentinel','\n'.join(logs.output))
        self.assertIn('hosted.chat.completed','\n'.join(logs.output))
    def test_original_pdf_is_inline_not_attachment(self):
        import pymupdf
        from hashlib import sha256
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / 'original.pdf'
            with pymupdf.open() as pdf:
                pdf.new_page().insert_text((72, 72), 'Synthetic preview original.')
                pdf.save(path)
            scoped = Mock()
            scoped.inspect_version.return_value = {'version': {
                'original_path': str(path), 'source_file': 'original.pdf',
                'source_sha256': sha256(path.read_bytes()).hexdigest()}}
            store = Mock()
            store.connect.return_value = MagicMock()
            settings = Settings('unused', 'unused', Path('data'))
            with patch('gsme_rag.hosted.app.AccessStore', return_value=scoped):
                with TestClient(create_hosted_app(settings, store=store), base_url='http://127.0.0.1:8020') as client:
                    response = client.get('/api/source/' + str(uuid4()))
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.headers['content-disposition'].startswith('inline;'))
                    self.assertEqual(response.headers['content-type'], 'application/pdf')
                    self.assertEqual(response.content, path.read_bytes())

    def test_chat_does_not_route_gds_or_deadline_questions_to_schedules(self):
        store = Mock()
        store.connect.return_value = MagicMock()
        settings = Settings("unused", "unused", Path("data"))
        report = {"status": "no_eligible_evidence", "results": [], "rerank_input_count": 0, "timings_ms": {}}
        with patch("gsme_rag.hosted.app.require_answer_permission"), \
             patch("gsme_rag.hosted.app.CohereEmbeddings"), \
             patch("gsme_rag.hosted.app.CohereReranker"), \
             patch("gsme_rag.hosted.app.QdrantEvidence") as vectors_mock, \
             patch("gsme_rag.hosted.app.retrieve", return_value=report) as retrieve_mock, \
             patch("gsme_rag.hosted.app.generate_answer", return_value={"status":"insufficient_evidence"}), \
             patch("gsme_rag.hosted.app.search_schedules") as schedules:
            with TestClient(create_hosted_app(settings, store=store), base_url="http://127.0.0.1:8020") as client:
                response = client.post("/api/chat", json={"question": "How many days before tapeout are preliminary and final GDS uploads due?"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["route"], "documents")
                self.assertEqual(response.json()["retrieval_strategy"], "hybrid")
                self.assertEqual(response.json()["answer"]["status"], "no_searchable_documents")
                self.assertIn("upload", response.json()["answer"]["text"])
                self.assertIn("Ready to chat", response.json()["answer"]["text"])
                retrieve_mock.assert_called_once()
                # Hosted chat explicitly uses hybrid even when the legacy CLI
                # setting is false; checking .env alone misreports this path.
                self.assertFalse(settings.hybrid_enabled)
                vectors_mock.assert_called_once_with(settings.qdrant_url, settings.collection,
                                                     settings.dimension, hybrid=True)
                self.assertEqual(retrieve_mock.call_args.args[-1].strategy, "hybrid")
                schedules.assert_not_called()

    def test_literal_greeting_needs_neither_source_evidence_nor_model(self):
        store = Mock()
        store.connect.return_value = MagicMock()
        settings = Settings("unused", "unused", Path("data"))
        with patch("gsme_rag.hosted.app.CohereEmbeddings") as embedding:
            with TestClient(create_hosted_app(settings, store=store), base_url="http://127.0.0.1:8020") as client:
                for text in ("hi", "how are you?"):
                    response = client.post("/api/chat", json={"question": text})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["answer"]["status"], "greeting")
                embedding.assert_not_called()

    def test_missing_schema_returns_actionable_setup_error(self):
        import psycopg
        store = Mock()
        store.connect.side_effect = psycopg.errors.UndefinedTable("private SQL details")
        settings = Settings("unused", "unused", Path("data"))
        with TestClient(create_hosted_app(settings, store=store), base_url="http://127.0.0.1:8020") as client:
            response = client.get("/api/documents")
            self.assertEqual(response.status_code, 503)
            self.assertIn("setup is incomplete", response.text)
            self.assertNotIn("private SQL", response.text)

    def test_local_launcher_migrates_before_starting_server(self):
        from gsme_rag.hosted.app import main
        events = []
        settings = Settings("unused", "unused", Path("data"))
        store = Mock()
        store.migrate.side_effect = lambda: events.append("migrate")
        with patch("gsme_rag.hosted.app.from_env", return_value=settings), \
             patch("gsme_rag.hosted.app.EvidenceStore", return_value=store), \
             patch("gsme_rag.observability.configure_console_logging"), \
             patch("gsme_rag.hosted.app.create_hosted_app", return_value="synthetic-app"), \
             patch("uvicorn.run", side_effect=lambda *a, **kw: events.append("serve")):
            main()
        self.assertEqual(events, ["migrate", "serve"])

    def test_later_provider_preflight_failure_closes_earlier_sdk_client(self):
        store = Mock()
        store.connect.return_value = MagicMock()
        provider = Mock()
        provider.client = MagicMock()
        settings = Settings("unused", "unused", Path("data"))
        with patch("gsme_rag.hosted.app.require_answer_permission"), \
             patch("gsme_rag.hosted.app.CohereEmbeddings", return_value=provider), \
             patch("gsme_rag.hosted.app.CohereReranker", side_effect=ValueError("synthetic configuration error")):
            with TestClient(create_hosted_app(settings, store=store), base_url="http://127.0.0.1:8020") as client:
                response = client.post("/api/chat", json={"question": "What does this mean?"})
                self.assertEqual(response.status_code, 400)
                provider.client.__exit__.assert_called_once()


DB, QDRANT = os.getenv("GSME_TEST_DATABASE_URL", ""), os.getenv("GSME_TEST_QDRANT_URL", "")


@skipUnless(DB and QDRANT and "/gsme_rag_test" in DB, "Requires isolated PostgreSQL/Qdrant")
class HostedIsolationTests(TestCase):
    def test_quota_recovery_json_and_stream_keep_public_scope_and_skip_cohere(self):
        import json
        from gsme_rag.retrieval import local_fallback
        from gsme_rag.retrieval.reranking import RankedPassage
        from tests.test_answer_streaming import StreamFixture, claim
        self.provider.external = self.reranker.external = True
        self.provider.encode_tokens = Mock(side_effect=ProviderOperationError("quota", error_code="quota_exhausted"))
        local = Mock(model_version="ollama-test")
        def rank(question, documents):
            self.assertTrue(documents)
            self.assertNotIn("COBALT_914", str(documents))
            self.assertNotIn("AMBER_628", str(documents))
            return [RankedPassage(i, .9) for i in range(len(documents))]
        local.rank.side_effect = rank
        draft = {"status":"answered", "claims":[claim("Public synthetic FAQ contact is Example Team.")]}
        fallback = StreamFixture(lambda: iter([json.dumps(draft)]))
        fallback.name = "ollama"
        fallback.generate = Mock(return_value=json.dumps(draft))
        with patch.dict(local_fallback._cooldowns, {}, clear=True), \
             patch("gsme_rag.hosted.app.require_answer_permission"), \
             patch("gsme_rag.retrieval.service.OllamaReranker", return_value=local), \
             patch("gsme_rag.generation.answers.OllamaAnswers", return_value=fallback), \
             patch("gsme_rag.generation.answers.CommandAnswers.generate", side_effect=AssertionError("No external answer allowed")):
            response = self.client.post("/api/chat", json={"question":"Who is the public synthetic FAQ contact?"})
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertTrue(body["retrieval_fallback_used"])
            self.assertEqual(body["retrieval_strategy"], "bm25_local_outage")
            self.assertEqual(body["answer"]["status"], "answered")
            self.assertEqual(body["answer"]["provider"], "ollama")
            self.assertEqual(len(body["answer_sources"]), 1)
            stream = self.client.post("/api/chat/stream", json={"question":"Who is the public synthetic FAQ contact?"})
            self.assertIn("local_retrieval", stream.text)
            self.assertIn("event: claim", stream.text)
            frames = [frame for frame in stream.text.split("\n\n") if frame.startswith("event: done")]
            final = json.loads(frames[-1].split("data: ", 1)[1])["result"]
            self.assertEqual(final["answer"]["status"], "answered")
            self.assertEqual(final["answer"]["citations"], body["answer"]["citations"])
            self.assertNotIn("COBALT_914", stream.text)
            self.assertNotIn("AMBER_628", stream.text)
            self.assertEqual(self.provider.encode_tokens.call_count, 1)
            self.reranker.rank.assert_not_called()

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tenant = "hosted-" + uuid4().hex
        self.settings = Settings(DB, QDRANT, Path(self.temp.name) / "store", dimension=32,
                                 collection="hosted_it_" + uuid4().hex[:12])
        self.store = EvidenceStore(DB)
        self.store.migrate()
        self.provider, self.reranker = FakeEmbeddings(32), FakeReranker()
        self.reranker.rank = Mock(wraps=self.reranker.rank)
        self.vectors = QdrantEvidence(QDRANT, self.settings.collection, 32, hybrid=True)
        self.addCleanup(self.cleanup_vectors)
        self.faq = self.ingest("public.docx", "Public synthetic FAQ contact is Example Team.")
        self.alice = self.ingest("alice.docx", "Alice private canary COBALT_914, never disclose to Bob or guests.", "google:alice")
        self.bob = self.ingest("bob.docx", "Bob private canary AMBER_628, never disclose to Alice or guests.", "google:bob")
        publish_faq(self.store, self.tenant, self.faq["document_id"], self.faq["version_id"])

        def verifier(value):
            if value not in {"alice", "bob"}:
                raise InvalidIdentity()
            return Principal("google:" + value)

        def providers():
            return self.provider, self.reranker, QdrantEvidence(QDRANT, self.settings.collection, 32, hybrid=True)

        self.hosted = HostedSettings(tenant=self.tenant, chat_global_minute=60, guest_uploads=False)
        self.app = create_hosted_app(self.settings, self.hosted, self.store, verifier, providers)
        self.client = TestClient(self.app, base_url=self.hosted.origin, client=("127.0.0.1", 1234))
        self.addCleanup(self.client.close)

    def cleanup_vectors(self):
        try:
            if self.vectors.client.collection_exists(self.settings.collection):
                self.vectors.client.delete_collection(self.settings.collection)
        finally:
            self.vectors.client.close()

    def guest_client(self, auto_worker=None, **options):
        def providers():
            return self.provider, self.reranker, QdrantEvidence(QDRANT, self.settings.collection, 32, hybrid=True)
        hosted = replace(self.hosted, guest_uploads=True, **options)
        client = TestClient(create_hosted_app(self.settings, hosted, self.store, providers=providers, auto_worker=auto_worker),
                            base_url=hosted.origin, client=("127.0.0.1", 1234))
        self.addCleanup(client.close)
        return client

    def automatic_upload(self):
        client = self.guest_client()
        self.addCleanup(client.app.state.upload_worker_pool.shutdown, wait=True)
        headers={"X-GSME-Session":"1"}
        client.post("/api/session",headers=headers)
        source=Path(self.temp.name)/"automatic.docx"
        doc=Document();doc.add_paragraph("Synthetic automatic processing evidence.");doc.save(source)
        response=client.post("/api/upload",headers=headers,files={"file":(source.name,source.read_bytes())})
        self.assertEqual(response.status_code,200,response.text)
        return client,response.json()["version_id"]

    def make_upload_due(self, version):
        with self.store.connect() as db:
            db.execute("UPDATE ingestion_jobs SET hosted_next_attempt_at=now()-interval '1 second' WHERE version_id=%s",(version,))
            db.execute("UPDATE index_jobs SET next_attempt_at=now()-interval '1 second' WHERE chunk_id IN (SELECT id FROM evidence_chunks WHERE version_id=%s)",(version,))

    def test_rate_limited_upload_resumes_after_restart_without_browser_retry(self):
        publish=Mock(wraps=self.store.publish)
        with patch.object(self.store,"publish",publish):
            with patch.object(self.provider,"encode_tokens",side_effect=ProviderOperationError(
                    "Cohere token count HTTP 429",retry_after_seconds=60,error_code="rate_limit")):
                client,version=self.automatic_upload()
            status=client.get(f"/api/documents/{version}/status").json()
            self.assertTrue(status["automatic_processing"])
            self.assertGreaterEqual(status["retry_after_seconds"],59)
            self.assertEqual(client.app.state.run_due_uploads(),[])
            restarted=self.guest_client(auto_worker=True)
            self.addCleanup(restarted.app.state.upload_worker_pool.shutdown,wait=True)
            restarted.cookies.update(client.cookies)
            self.make_upload_due(version)
            with restarted:
                deadline=time.monotonic()+25
                while time.monotonic()<deadline:
                    final=restarted.get(f"/api/documents/{version}/status").json()
                    if final.get("state")=="indexed" and not final.get("automatic_processing"):break
                    time.sleep(.25)
            self.assertEqual(final["state"],"indexed")
            self.assertFalse(final["automatic_processing"])
            self.assertEqual(publish.call_count,1)  # Automatic retry never resets publication/backoff.
            with self.store.connect() as db:
                row=db.execute("SELECT attempt_count FROM index_jobs WHERE chunk_id IN (SELECT id FROM evidence_chunks WHERE version_id=%s)",(version,)).fetchone()
                self.assertEqual(row["attempt_count"],2)

    def test_upload_continues_past_twenty_batch_slice_without_manual_resume(self):
        with patch("gsme_rag.hosted.app.process_index",return_value={"indexed":1,"failed":0}) as index:
            client,version=self.automatic_upload()
            self.assertEqual(index.call_count,20)
        self.assertTrue(client.get(f"/api/documents/{version}/status").json()["automatic_processing"])
        self.make_upload_due(version)
        for future in client.app.state.run_due_uploads():future.result(timeout=30)
        self.assertEqual(client.get(f"/api/documents/{version}/status").json()["state"],"indexed")

    def test_explicit_exhausted_quota_stops_without_automatic_retry(self):
        with patch.object(self.provider,"encode_tokens",side_effect=ProviderOperationError(
                "Provider quota exhausted",retryable=False,error_code="quota_exhausted")) as tokens:
            client,version=self.automatic_upload()
            self.make_upload_due(version)
            self.assertEqual(client.app.state.run_due_uploads(),[])
            self.assertEqual(tokens.call_count,1)
        status=client.get(f"/api/documents/{version}/status").json()
        self.assertFalse(status["automatic_processing"])
        self.assertEqual(status["processing_error"],"quota_exhausted")

    def test_repeated_rate_limits_stop_after_five_attempts(self):
        with patch.object(self.provider,"encode_tokens",side_effect=ProviderOperationError(
                "Cohere HTTP 429",retry_after_seconds=60,error_code="rate_limit")) as tokens:
            client,version=self.automatic_upload()
            for _ in range(4):
                self.make_upload_due(version)
                for future in client.app.state.run_due_uploads():future.result(timeout=30)
            status=client.get(f"/api/documents/{version}/status").json()
            self.assertFalse(status["automatic_processing"])
            self.assertEqual(status["processing_error"],"retry_exhausted")
            self.assertEqual(tokens.call_count,5)
            self.make_upload_due(version)
            self.assertEqual(client.app.state.run_due_uploads(),[])

    def test_deleting_waiting_upload_cancels_automatic_work(self):
        with patch.object(self.provider,"encode_tokens",side_effect=ProviderOperationError(
                "Cohere HTTP 429",retry_after_seconds=60,error_code="rate_limit")):
            client,version=self.automatic_upload()
        response=client.delete(f"/api/documents/{version}",headers={"X-GSME-Session":"1"})
        self.assertEqual(response.status_code,200,response.text)
        self.make_upload_due(version)
        self.assertEqual(client.app.state.run_due_uploads(),[])
        with self.store.connect() as db:
            self.assertFalse(db.execute("SELECT hosted_auto_process FROM ingestion_jobs WHERE version_id=%s",(version,)).fetchone()["hosted_auto_process"])

    def test_upload_configuration_failure_exposes_actionable_safe_message(self):
        with patch.object(self.provider,"encode_tokens",side_effect=ProviderConfigurationError(
                "safe setup error",error_code="embedding_content_not_approved")):
            client,version=self.automatic_upload()
        status=client.get(f"/api/documents/{version}/status").json()
        self.assertFalse(status["automatic_processing"])
        self.assertEqual(status["processing_error"],"embedding_content_not_approved")
        self.assertIn("GSME_EXTERNAL_CONTENT_APPROVED=yes",status["processing_message"])
        self.assertIn("restart",status["processing_message"])
        row=next(r for r in client.get("/api/documents").json()["documents"] if r["version_id"]==version)
        self.assertEqual(row["processing_message"],status["processing_message"])
        self.make_upload_due(version)
        self.assertEqual(client.app.state.run_due_uploads(),[])

    def test_resume_staged_version_with_existing_jobs_publishes_before_indexing(self):
        client,version=self.automatic_upload()
        # A previous bounded/offline run can leave jobs behind without making
        # this version current. Job existence is not proof of publication.
        with self.store.connect() as db:
            db.execute("UPDATE documents SET current_version_id=NULL WHERE current_version_id=%s",(version,))
            db.execute("UPDATE document_versions SET state='review_needed' WHERE id=%s",(version,))
            db.execute("UPDATE index_jobs SET state='queued' WHERE chunk_id IN (SELECT id FROM evidence_chunks WHERE version_id=%s)",(version,))
        with patch.object(self.store,"publish",wraps=self.store.publish) as publish:
            response=client.post(f"/api/documents/{version}/resume",headers={"X-GSME-Session":"1"})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(publish.call_count,1)
        final=client.get(f"/api/documents/{version}/status").json()
        self.assertEqual(final["state"],"indexed")
        self.assertFalse(final["automatic_processing"])
        self.assertTrue(self.store.inspect_version(self.tenant,version,1)["version"]["is_current"])

    def test_owner_delete_revokes_sources_retry_and_stale_vector_hits(self):
        version = self.alice["version_id"]
        url = f"/api/documents/{version}"
        headers = {"Authorization": "Bearer alice", "X-GSME-Session": "1"}
        with self.store.connect() as db:
            db.execute("UPDATE document_access SET deletion_protected=true WHERE document_id=%s", (self.alice["document_id"],))
        self.assertEqual(self.client.delete(url, headers=headers).status_code, 404)
        self.assertEqual(self.client.get(f"/api/source/{version}", headers=headers).status_code, 200)
        # Even a stale server or direct removal cannot bypass the required flag.
        import psycopg
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.store.connect() as db:
                db.execute("DELETE FROM document_access WHERE document_id=%s", (self.alice["document_id"],))
        with self.store.connect() as db:
            db.execute("UPDATE document_access SET deletion_protected=false WHERE document_id=%s", (self.alice["document_id"],))
        self.assertEqual(self.client.delete(url, headers={**headers, "Authorization": "Bearer bob"}).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/documents/{self.faq['version_id']}", headers=headers).status_code, 404)
        self.assertEqual(self.client.delete(url, headers=headers).status_code, 200)
        self.assertNotIn("alice.docx", self.client.get("/api/documents", headers=headers).text)
        for endpoint in (f"/api/source/{version}", f"/api/preview/{version}", url + "/status"):
            self.assertEqual(self.client.get(endpoint, headers=headers).status_code, 404)
        self.assertEqual(self.client.post(url + "/resume", headers=headers).status_code, 404)
        scope = AccessStore(self.store, self.tenant, Principal("google:alice"), True)
        self.assertEqual(scope.retrieval_scope(self.tenant, self.provider.model_version,
            self.settings.collection, document=self.alice["document_id"]), [])
        with self.store.connect() as db:
            row = db.execute("SELECT state,original_path FROM document_versions WHERE id=%s", (version,)).fetchone()
            self.assertEqual(row["state"], "superseded")
            self.assertTrue(Path(row["original_path"]).is_file())
            job = db.execute("SELECT id FROM ingestion_jobs WHERE version_id=%s", (version,)).fetchone()
        self.assertFalse(self.store.retry_job(self.tenant, str(job["id"])))

    def test_reprocessed_document_appears_once_as_latest_owner_version(self):
        source = Path(self.temp.name) / "revised.docx"
        doc = Document(); doc.add_paragraph("Revised synthetic content."); doc.save(source)
        revised = self.store.submit(self.tenant, source, self.settings.data_dir, self.alice["document_id"])
        scoped = AccessStore(self.store, self.tenant, Principal("google:alice"))
        visible = [row for row in scoped.visible_documents() if str(row["document_id"]) == self.alice["document_id"]]
        self.assertEqual(len(visible), 1)
        self.assertEqual(str(visible[0]["version_id"]), revised["version_id"])

    def test_retry_failed_parse_requeues_then_indexes_clean_text(self):
        path = Path(self.temp.name) / "retry.docx"
        doc = Document(); doc.add_paragraph("Synthetic retry evidence is available."); doc.save(path)
        submitted = self.store.submit(self.tenant, path, self.settings.data_dir)
        AccessStore(self.store, self.tenant, Principal("google:alice")).claim_new_document(submitted["document_id"])
        with self.store.connect() as db:
            db.execute("UPDATE ingestion_jobs SET state='failed' WHERE id=%s", (submitted["job_id"],))
            db.execute("UPDATE document_versions SET state='failed' WHERE id=%s", (submitted["version_id"],))
        headers = {"Authorization": "Bearer alice", "X-GSME-Session": "1"}
        response = self.client.post(f"/api/documents/{submitted['version_id']}/resume", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        status = self.client.get(f"/api/documents/{submitted['version_id']}/status", headers=headers).json()
        self.assertEqual(status["state"], "indexed")
        self.assertGreater(status["accepted"], 0)
        self.assertIn("held_reasons", status)
        # Repeating Retry reuses completed jobs and must remain ready.
        again = self.client.post(f"/api/documents/{submitted['version_id']}/resume", headers=headers)
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(self.client.get(f"/api/documents/{submitted['version_id']}/status", headers=headers).json()["state"], "indexed")
        # Manual Retry must not discard a provider's HTTP 429 cooldown.
        with self.store.connect() as db:
            db.execute("""UPDATE index_jobs SET state='failed',last_error='Cohere token count failed (HTTP 429)',
                next_attempt_at=now()+interval '60 seconds' WHERE chunk_id IN
                (SELECT id FROM evidence_chunks WHERE version_id=%s)""", (submitted["version_id"],))
            db.execute("UPDATE document_versions SET state='indexing' WHERE id=%s", (submitted["version_id"],))
        calls = self.provider.calls
        limited = self.client.post(f"/api/documents/{submitted['version_id']}/resume", headers=headers)
        self.assertEqual(limited.status_code, 429, limited.text)
        self.assertGreater(int(limited.headers["Retry-After"]), 0)
        self.assertEqual(self.provider.calls, calls)
        status = self.client.get(f"/api/documents/{submitted['version_id']}/status", headers=headers).json()
        self.assertGreater(status["retry_after_seconds"], 0)
        self.assertIn("delete_own_upload", self.client.get("/api/config").json()["document_actions"])

    def test_browser_upload_to_index_and_chat_is_isolated_without_signin(self):
        alice, bob = self.guest_client(), self.guest_client()
        headers = {"X-GSME-Session": "1"}
        for client in (alice, bob):
            response = client.post("/api/session", headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertIn("HttpOnly", response.headers["set-cookie"])
            self.assertIn("SameSite=strict", response.headers["set-cookie"])
        token = alice.cookies.get("gsme_guest")
        self.assertNotEqual(token, bob.cookies.get("gsme_guest"))
        self.assertEqual(alice.post("/api/session", headers=headers).status_code, 200)
        self.assertEqual(token, alice.cookies.get("gsme_guest"))
        path = Path(self.temp.name) / "guest-upload.docx"
        doc = Document()
        doc.add_heading("What is the synthetic upload label?", 2)
        doc.add_paragraph("The synthetic upload label is VIOLET_372.")
        doc.save(path)
        uploaded = alice.post("/api/upload", headers=headers,
            files={"file": (path.name, path.read_bytes())},
            data={"owner": "bob", "tenant": "evil", "visibility": "company_faq"})
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        version = uploaded.json()["version_id"]
        status = alice.get(f"/api/documents/{version}/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertGreater(status.json()["index_jobs"].get("indexed", 0), 0)
        self.assertFalse(status.json()["processing"])
        document = status.json()["document_id"]
        result = alice.post("/api/chat", headers=headers, json={"question": "What is the synthetic upload label?",
                            "document_id": document, "generate_answer": False})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn("VIOLET_372", result.text)
        for url in (f"/api/source/{version}", f"/api/preview/{version}", f"/api/documents/{version}/status"):
            self.assertEqual(alice.get(url).status_code, 200)
            self.assertEqual(bob.get(url).status_code, 404)
        self.assertNotIn(path.name, bob.get("/api/documents").text)
        self.assertIn("VIOLET_372", alice.get(f"/api/preview/{version}").json()["text"])
        self.assertNotIn("alice.docx", alice.get("/api/documents").text)
        forbidden = bob.post("/api/chat", headers=headers, json={"question": "What is the synthetic upload label?",
                             "document_id": document, "generate_answer": False})
        self.assertNotIn("VIOLET_372", forbidden.text)
        self.assertEqual(bob.post(f"/api/documents/{version}/resume", headers=headers).status_code, 404)
        self.assertEqual(bob.delete(f"/api/documents/{version}", headers=headers).status_code, 404)
        self.assertEqual(bob.post("/api/upload", headers=headers, files={"file": (path.name, path.read_bytes())},
                                 data={"document_id": document}).status_code, 404)
        # Only a hash is persisted. A session cannot turn itself into a public publisher.
        with self.store.connect() as db:
            from hashlib import sha256
            row = db.execute("SELECT subject,token_sha256 FROM hosted_guest_sessions WHERE token_sha256=%s",
                             (sha256(token.encode()).hexdigest(),)).fetchone()
            self.assertTrue(row["subject"].startswith("guest:"))
            self.assertNotEqual(row["token_sha256"], token)
            policy = db.execute("SELECT owner_subject,visibility FROM document_access WHERE document_id=%s", (document,)).fetchone()
            self.assertEqual(policy["owner_subject"], row["subject"])
            self.assertEqual(policy["visibility"], "private")

    def test_browser_session_csrf_expiry_and_shared_faq_policy(self):
        client = self.guest_client(public_faq_guests=False)
        headers = {"X-GSME-Session": "1"}
        self.assertEqual(client.post("/api/session").status_code, 403)
        self.assertEqual(client.post("/api/session", headers={**headers, "Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(client.post("/api/session", headers=headers).status_code, 200)
        self.assertNotIn("public.docx", client.get("/api/documents").text)
        self.assertEqual(client.get(f"/api/source/{self.faq['version_id']}").status_code, 404)
        self.assertEqual(client.post("/api/chat", json={"question": "Hi"}).status_code, 403)
        token = client.cookies.get("gsme_guest")
        from hashlib import sha256
        with self.store.connect() as db:
            db.execute("UPDATE hosted_guest_sessions SET expires_at=now()-interval '1 second' WHERE token_sha256=%s",
                       (sha256(token.encode()).hexdigest(),))
        self.assertEqual(client.get("/api/documents").status_code, 401)
        self.assertEqual(client.post("/api/session", headers=headers).status_code, 200)
        self.assertNotEqual(client.cookies.get("gsme_guest"), token)
        client.cookies.set("gsme_guest", "forged", domain="127.0.0.1", path="/")
        self.assertEqual(client.get("/api/documents").status_code, 401)

    def test_guest_disabled_and_https_cookie(self):
        self.assertEqual(self.client.post("/api/session", headers={"X-GSME-Session": "1"}).status_code, 403)
        client = self.guest_client(origin="https://chat.company.example")
        response = client.post("/api/session", headers={"X-GSME-Session": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("__Host-gsme_guest=", response.headers["set-cookie"])
        self.assertIn("; Secure", response.headers["set-cookie"])

    def test_office_preview_is_labeled_hash_checked_and_owner_only(self):
        url = f"/api/preview/{self.alice['version_id']}"
        self.assertEqual(self.client.get(url).status_code, 404)
        response = self.client.get(url, headers={"Authorization": "Bearer alice"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["kind"], "text")
        self.assertIn("not the original Office layout", response.json()["notice"])
        self.assertIn("COBALT_914", response.json()["text"])
        item = self.store.inspect_version(self.tenant, self.alice['version_id'])
        with Path(item['version']['artifact_path']).open('ab') as artifact:
            artifact.write(b'changed preview text')
        response = self.client.get(url, headers={"Authorization": "Bearer alice"})
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("changed preview text", response.text)

    def ingest(self, filename, text, owner=None):
        path = Path(self.temp.name) / filename
        doc = Document()
        doc.add_heading("Who is the synthetic contact?", 2)
        doc.add_paragraph(text)
        doc.save(path)
        item = self.store.submit(self.tenant, path, self.settings.data_dir)
        if owner:
            AccessStore(self.store, self.tenant, Principal(owner)).claim_new_document(item["document_id"])
        process_ingestion(self.store, self.settings, item["job_id"])
        self.store.accept_clean_text(self.tenant, item["version_id"])
        self.store.publish(self.tenant, item["document_id"], item["version_id"], self.provider.model_version, self.vectors.collection)
        process_index(self.store, self.vectors, self.provider, self.settings, self.tenant, item["version_id"])
        return item

    def test_owner_isolation_applies_to_list_source_scope_and_rerank_input(self):
        for user, allowed, forbidden in [(None, {"public.docx"}, {"COBALT_914", "AMBER_628"}),
                                         ("alice", {"public.docx", "alice.docx"}, {"AMBER_628"}),
                                         ("bob", {"public.docx", "bob.docx"}, {"COBALT_914"})]:
            headers = {"Authorization": "Bearer " + user} if user else {}
            docs = self.client.get("/api/documents", headers=headers).json()["documents"]
            self.assertEqual({r["source_file"] for r in docs}, allowed)
            result = self.client.post("/api/chat", headers=headers,
                json={"question": "Who is the synthetic contact?", "generate_answer": False})
            self.assertEqual(result.status_code, 200)
            self.assertEqual({r["source_file"] for r in result.json()["results"]}, allowed)
            sent = " ".join(self.reranker.rank.call_args.args[1])
            self.assertTrue(all(word not in sent and word not in result.text for word in forbidden))
        source = f"/api/source/{self.alice['version_id']}"
        self.assertEqual(self.client.get(source).status_code, 404)
        self.assertEqual(self.client.get(source, headers={"Authorization": "Bearer bob"}).status_code, 404)
        self.assertEqual(self.client.get(source, headers={"Authorization": "Bearer alice"}).status_code, 200)
        self.assertEqual(self.client.get(f"/api/source/{self.faq['version_id']}").status_code, 200)

    def test_private_upload_cannot_select_owner_visibility_or_tenant(self):
        path = Path(self.temp.name) / "upload.docx"
        doc = Document()
        doc.add_heading("What is the synthetic upload label?", 2)
        doc.add_paragraph("The label is synthetic violet.")
        doc.save(path)
        self.assertEqual(self.client.post("/api/upload", files={"file": (path.name, path.read_bytes())}).status_code, 401)
        result = self.client.post("/api/upload", headers={"Authorization": "Bearer alice"},
            files={"file": (path.name, path.read_bytes())}, data={"visibility": "company_faq", "owner": "bob", "tenant": "evil"})
        self.assertEqual(result.status_code, 200)
        version = result.json()["version_id"]
        self.assertEqual(self.client.get(f"/api/source/{version}", headers={"Authorization": "Bearer bob"}).status_code, 404)
        self.assertEqual(self.client.get(f"/api/source/{version}", headers={"Authorization": "Bearer alice"}).status_code, 200)
        self.assertEqual(self.client.post("/api/chat", json={"question": "Hi", "tenant": "evil"}).status_code, 422)
        self.assertEqual(self.client.get("/admin").status_code, 404)
        self.assertEqual(self.client.get("/api/logs").status_code, 404)
        self.assertEqual(self.client.post("/api/review", json={}).status_code, 404)
        self.assertEqual(self.client.post("/api/chat", headers={"Origin": "https://evil.example"}, json={"question": "Hi"}).status_code, 403)
        self.assertEqual(self.client.get("/api/documents", headers={"Authorization": "Bearer forged"}).status_code, 401)
        response = self.client.post("/api/upload", headers={"Authorization": "Bearer bob"},
            files={"file": (path.name, path.read_bytes())}, data={"document_id": self.alice["document_id"]})
        self.assertEqual(response.status_code, 404)

    def test_revocation_during_rerank_removes_private_result_and_source(self):
        def revoke(question, documents):
            with self.store.connect() as db:
                db.execute("DELETE FROM document_access WHERE document_id=%s", (self.alice["document_id"],))
            return FakeReranker().rank(question, documents)
        self.reranker.rank.side_effect = revoke
        response = self.client.post("/api/chat", headers={"Authorization": "Bearer alice"},
            json={"question": "Private canary", "generate_answer": False, "document_id": self.alice["document_id"]})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("COBALT_914", response.text)
        self.assertEqual(response.json()["results"], [])
        self.assertEqual(self.client.get(f"/api/source/{self.alice['version_id']}", headers={"Authorization": "Bearer alice"}).status_code, 404)

    def test_public_schedule_rows_do_not_allow_original_download(self):
        source = Path(self.temp.name) / "shared_schedules.csv"
        source.write_text("Tech (nm),Flavor,Trial,Final,Tapeout,Est. Ship\n28,LP,2026-11-01,2026-11-02,2026-11-03,2026-12-01\n", encoding="utf-8")
        item = self.store.submit(self.tenant, source, self.settings.data_dir)
        process_ingestion(self.store, self.settings, item["job_id"])
        publish_faq(self.store, self.tenant, item["document_id"], item["version_id"], schedule=True)
        result = self.client.post("/api/schedules/search", json={"question": "Show shared schedules"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["total"], 1)
        self.assertIsNone(result.json()["results"][0]["source_url"])
        self.assertEqual(self.client.get(f"/api/source/{item['version_id']}").status_code, 404)
        self.assertNotIn("shared_schedules.csv", self.client.get("/api/documents").text)

    def test_uploaded_schedule_owner_search_needs_no_embedding_or_answer_provider(self):
        source = Path(self.temp.name) / "shared_schedules.csv"
        source.write_text("technology_nm,flavor,trial_deadline,final_deadline,tapeout_date,estimated_ship_date\n28,HPC+ RF,2026-11-01,2026-11-08,2026-11-15,2027-02-15\n", encoding="utf-8")
        headers = {"Authorization": "Bearer alice", "X-GSME-Session": "1"}
        with patch("gsme_rag.hosted.app.CohereEmbeddings", side_effect=AssertionError("No embedding call expected")), \
             patch("gsme_rag.hosted.app.generate_answer", side_effect=AssertionError("No generation expected")):
            response = self.client.post("/api/upload", headers=headers,
                files={"file": (source.name, source.read_bytes())})
            self.assertEqual(response.status_code, 200, response.text)
            version = response.json()["version_id"]
            result = self.client.post("/api/schedules/search", headers=headers,
                json={"question": 'Show shared 28nm schedules flavor "HPC+ RF"'})
        self.assertEqual(result.status_code, 200, result.text)
        row = result.json()["results"][0]
        self.assertEqual((row["tapeout_date"], row["source_row_number"]), ("2026-11-15", 2))
        self.assertEqual(row["version_id"], version)
        self.assertIsNotNone(row["source_url"])
        self.assertEqual(result.json()["reranking_status"], "not_applicable_exact_sql")
        for auth in ({}, {"Authorization": "Bearer bob"}):
            other = self.client.post("/api/schedules/search", headers=auth,
                json={"question": "Show both schedules"})
            self.assertEqual(other.json()["total"], 0)

    def test_shared_schedule_replacement_requires_explicit_republication(self):
        source = Path(self.temp.name) / "shared_schedules.csv"
        source.write_text("technology_nm,flavor,trial_deadline,final_deadline,tapeout_date,estimated_ship_date\n28,LP,2026-11-01,2026-11-08,2026-11-15,2027-02-15\n", encoding="utf-8")
        first = self.store.submit(self.tenant, source, self.settings.data_dir)
        process_ingestion(self.store, self.settings, first["job_id"])
        publish_faq(self.store, self.tenant, first["document_id"], first["version_id"], schedule=True)
        query = {"question": "Show shared schedules"}
        self.assertEqual(self.client.post("/api/schedules/search", json=query).json()["total"], 1)
        unchanged = self.store.submit(self.tenant, source, self.settings.data_dir, first["document_id"])
        self.assertTrue(unchanged["no_op"])
        source.write_text("technology_nm,flavor,trial_deadline,final_deadline,tapeout_date,estimated_ship_date\n65,LP,2026-12-01,2026-12-08,2026-12-15,2027-03-15\n", encoding="utf-8")
        updated = self.store.submit(self.tenant, source, self.settings.data_dir, first["document_id"])
        process_ingestion(self.store, self.settings, updated["job_id"])
        # Publication is pinned to an exact version, not inherited by new bytes.
        self.assertEqual(self.client.post("/api/schedules/search", json=query).json()["total"], 0)
        publish_faq(self.store, self.tenant, updated["document_id"], updated["version_id"], schedule=True)
        current = self.client.post("/api/schedules/search", json=query).json()
        self.assertEqual(current["total"], 1)
        self.assertEqual(current["results"][0]["tapeout_date"], "2026-12-15")
        with self.store.connect() as db:
            old = db.execute("SELECT state,original_path FROM document_versions WHERE id=%s", (first["version_id"],)).fetchone()
        self.assertEqual(old["state"], "superseded")
        self.assertTrue(Path(old["original_path"]).is_file())

    def test_revocation_during_generation_removes_answer_and_old_source_extracts(self):
        def revoke(*args, **kwargs):
            with self.store.connect() as db:
                db.execute("DELETE FROM document_access WHERE document_id=%s", (self.alice["document_id"],))
            return {"status": "answered", "text": "COBALT_914", "claims": [], "citations": []}
        with patch("gsme_rag.hosted.app.require_answer_permission"), patch("gsme_rag.hosted.app.generate_answer", side_effect=revoke):
            response = self.client.post("/api/chat", headers={"Authorization": "Bearer alice"},
                json={"question": "Private contact", "document_id": self.alice["document_id"]})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("COBALT_914", response.text)
        self.assertEqual(response.json()["status"], "evidence_changed")
