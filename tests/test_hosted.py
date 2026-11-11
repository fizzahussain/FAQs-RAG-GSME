import os
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import Mock, MagicMock, patch
from uuid import uuid4

from docx import Document
from fastapi.testclient import TestClient

from gsme_rag.hosted.access import AccessStore, Principal, publish_faq
from gsme_rag.hosted.app import create_hosted_app, HostedSettings
from gsme_rag.hosted.identity import google_principal, InvalidIdentity, IdentityUnavailable
from gsme_rag.retrieval.reranking import FakeReranker
from gsme_rag.storage.config import Settings
from gsme_rag.storage.pipeline import process_ingestion, process_index
from gsme_rag.storage.postgres import EvidenceStore
from gsme_rag.storage.providers import FakeEmbeddings
from gsme_rag.storage.vectors import QdrantEvidence


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

        self.hosted = HostedSettings(tenant=self.tenant, chat_global_minute=60)
        self.app = create_hosted_app(self.settings, self.hosted, self.store, verifier, providers)
        self.client = TestClient(self.app, base_url=self.hosted.origin, client=("127.0.0.1", 1234))
        self.addCleanup(self.client.close)

    def cleanup_vectors(self):
        try:
            if self.vectors.client.collection_exists(self.settings.collection):
                self.vectors.client.delete_collection(self.settings.collection)
        finally:
            self.vectors.client.close()

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
        result = self.client.post("/api/chat", json={"question": "Show shared schedules"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["total"], 1)
        self.assertIsNone(result.json()["results"][0]["source_url"])
        self.assertEqual(self.client.get(f"/api/source/{item['version_id']}").status_code, 404)
        self.assertNotIn("shared_schedules.csv", self.client.get("/api/documents").text)

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
