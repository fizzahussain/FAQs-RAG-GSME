"""Local evidence console authorization and real-service workflow."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from gsme_rag.api.app import app as public_app
from gsme_rag.storage.admin import create_admin_app
from gsme_rag.storage.config import Settings
from gsme_rag.storage.postgres import EvidenceStore


TOKEN = "local-test-secret-at-least-20-characters"
DB = os.getenv("GSME_TEST_DATABASE_URL", "")
QDRANT = os.getenv("GSME_TEST_QDRANT_URL", "")
ENABLED = bool(DB and QDRANT and "/gsme_rag_test" in DB)


class AdminBoundaryTests(TestCase):
    def test_request_vector_clients_close_on_success_and_failures(self):
        tenant, version, document = "t", str(uuid4()), str(uuid4())
        store = Mock()
        store.publish.return_value = 1
        store.approved_point.return_value = None
        vectors = Mock(collection="synthetic")
        body = dict(tenant=tenant, version_id=version, document_id=document, provider="fake")
        with patch.dict(os.environ, {"GSME_REVIEW_AUTH_TOKEN": TOKEN}), \
             patch("gsme_rag.storage.admin._vectors", return_value=vectors) as make_vectors, \
             patch("gsme_rag.storage.admin.process_index", return_value={"indexed": 0, "failed": 0}) as indexing, \
             patch("gsme_rag.storage.admin.reconcile", return_value=0) as reconcile, \
             patch("gsme_rag.storage.admin.repair_missing_vectors", return_value=0) as repair, \
             patch("gsme_rag.storage.admin.search_evidence", return_value=[]) as search:
            with TestClient(create_admin_app(self.settings, store), base_url="http://127.0.0.1:8010", client=("127.0.0.1", 1234)) as client:
                headers = {"X-GSME-Token": TOKEN}
                self.assertEqual(client.post("/api/publish", headers=headers, json=body).status_code, 200)
                make_vectors.assert_not_called()
                for path, operation in [("index", indexing), ("reconcile", reconcile), ("repair", repair), ("search", search)]:
                    request = dict(tenant=tenant, provider="fake", question="Synthetic") if path == "search" else body
                    for error in (False, True):
                        with self.subTest(path=path, error=error):
                            vectors.client.close.reset_mock()
                            operation.side_effect = ValueError("Synthetic failure") if error else None
                            response = client.post("/api/" + path, headers=headers, json=request)
                            self.assertEqual(response.status_code, 400 if error else 200)
                            vectors.client.close.assert_called_once()
                    operation.side_effect = None
                vectors.client.close.reset_mock()
                response = client.get(f"/api/embedding/missing?tenant={tenant}&version_id={version}&provider=fake", headers=headers)
                self.assertEqual(response.status_code, 404)
                vectors.client.close.assert_called_once()

    def test_product_home_separates_administration_and_library_is_scoped(self):
        store = Mock()
        store.library.return_value = []
        with patch.dict(os.environ, {"GSME_REVIEW_AUTH_TOKEN": TOKEN}):
            with TestClient(create_admin_app(self.settings, store), base_url="http://127.0.0.1:8010",
                            client=("127.0.0.1", 12345)) as client:
                root = client.get("/")
                self.assertIn("What would you like to know?", root.text)
                self.assertNotIn("Include in selected-chunk review", root.text)
                self.assertNotIn('id="batchNote"', root.text)
                self.assertIn('id="batchNote"', client.get("/admin").text)
                self.assertEqual(client.get("/api/library?tenant=workspace").status_code, 401)
                self.assertEqual(client.get("/api/library?tenant=workspace&provider=fake",
                    headers={"X-GSME-Token": TOKEN}).json(), [])
                store.library.assert_called_once_with("workspace", "test-hash-v1-32", "gsme_evidence_v1_synthetic")

    def setUp(self):
        self.settings = Settings("postgresql://unused", "http://127.0.0.1:6333", Path("data"), dimension=32)

    def test_markdown_legacy_newlines_and_actual_tampering(self):
        from hashlib import sha256

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "normalized.md"
            canonical = "# Source\n\nLOT A17; μ = 1\n".encode("utf-8")
            legacy = canonical.replace(b"\n", b"\r\n")
            path.write_bytes(legacy)
            store = Mock()
            store.inspect_version.return_value = {"version": {
                "artifact_path": str(path), "normalized_sha256": sha256(canonical).hexdigest()}}
            with patch.dict(os.environ, {"GSME_REVIEW_AUTH_TOKEN": TOKEN}):
                with TestClient(create_admin_app(self.settings, store), base_url="http://127.0.0.1:8010",
                                client=("127.0.0.1", 12345)) as client:
                    url = f"/api/markdown/{uuid4()}?tenant=t"
                    response = client.get(url, headers={"X-GSME-Token": TOKEN})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.content, canonical)
                    self.assertEqual(path.read_bytes(), legacy)  # Preserve raw legacy evidence.
                    path.write_bytes(legacy.replace(b"A17", b"Al7"))
                    self.assertEqual(client.get(url, headers={"X-GSME-Token": TOKEN}).status_code, 409)

    def test_batch_review_authorization_validation_and_explicit_selection(self):
        store = Mock()
        store.record_reviews.return_value = 2
        with patch.dict(os.environ, {"GSME_REVIEW_AUTH_TOKEN": TOKEN, "GSME_AUTHORIZED_REVIEWERS": "alice"}):
            with TestClient(create_admin_app(self.settings, store), base_url="http://127.0.0.1:8010",
                            client=("127.0.0.1", 12345)) as client:
                body = dict(tenant="t", version_id=str(uuid4()), chunk_ids=["a", "b"], decision="approved",
                            reviewer="alice", rationale="Checked both original table records", source_verified=True)
                self.assertEqual(client.post("/api/review/batch", json=body).status_code, 401)
                headers = {"X-GSME-Token": TOKEN}
                self.assertEqual(client.post("/api/review/batch", headers=headers,
                                             json={**body, "reviewer": "mallory"}).status_code, 403)
                self.assertEqual(client.post("/api/review/batch", headers=headers,
                                             json={**body, "chunk_ids": []}).status_code, 422)
                response = client.post("/api/review/batch", headers=headers, json=body)
                self.assertEqual(response.json(), {"recorded": 2})
                store.record_reviews.assert_called_once_with("t", body["version_id"], ["a", "b"],
                    "approved", "alice", body["rationale"], True)

    def test_requires_strong_local_token_and_public_app_has_no_storage_routes(self):
        with patch.dict(os.environ, {"GSME_REVIEW_AUTH_TOKEN": "short"}):
            with self.assertRaisesRegex(ValueError, "at least 20"):
                create_admin_app(self.settings, Mock())
        public_paths = {getattr(route, "path", "") for route in public_app.routes}
        self.assertFalse(any(path.startswith("/api/") for path in public_paths))

    def test_auth_origin_loopback_and_missing_approval(self):
        store = Mock()
        store.list_versions.return_value = []
        store.inspect_version.return_value = None
        with patch.dict(os.environ, {"GSME_REVIEW_AUTH_TOKEN": TOKEN,
                                      "GSME_AUTHORIZED_REVIEWERS": "alice"}):
            app = create_admin_app(self.settings, store)
            with TestClient(app, base_url="http://127.0.0.1:8010",
                            client=("127.0.0.1", 12345)) as client:
                self.assertEqual(client.get("/api/versions?tenant=t").status_code, 401)
                headers = {"X-GSME-Token": TOKEN}
                self.assertEqual(client.get("/api/versions?tenant=t", headers=headers).json(), [])
                bad_origin = {**headers, "Origin": "https://attacker.invalid"}
                self.assertEqual(client.get("/api/versions?tenant=t", headers=bad_origin).status_code, 403)
                denied = client.post("/api/review", headers=headers, json={
                    "tenant": "t", "version_id": str(uuid4()), "chunk_id": "c",
                    "decision": "approved", "reviewer": "mallory",
                    "rationale": "unverified", "source_verified": True,
                })
                self.assertEqual(denied.status_code, 403)
                self.assertFalse(store.record_review.called)
                denied = client.post("/api/index", headers=headers, json={
                    "tenant": "t", "version_id": str(uuid4()), "provider": "cohere"})
                self.assertEqual(denied.status_code, 400)
            with TestClient(app, base_url="http://127.0.0.1:8010",
                            client=("192.0.2.5", 12345)) as remote:
                self.assertEqual(remote.get("/", headers=headers).status_code, 403)


@skipUnless(ENABLED, "Requires isolated PostgreSQL and Qdrant test services")
class AdminIntegrationTests(TestCase):
    def test_browser_workflow_fake_vectors_and_revocation(self):
        with TemporaryDirectory() as temporary:
            root = Path(__file__).resolve().parents[1]
            settings = Settings(DB, QDRANT, Path(temporary),
                                collection="gsme_admin_it_" + uuid4().hex[:12], dimension=32)
            store = EvidenceStore(DB)
            store.migrate()
            tenant = "admin-it-" + uuid4().hex
            with patch.dict(os.environ, {"GSME_REVIEW_AUTH_TOKEN": TOKEN,
                                      "GSME_AUTHORIZED_REVIEWERS": "alice"}):
                app = create_admin_app(settings, store)
                with TestClient(app, base_url="http://127.0.0.1:8010",
                                client=("127.0.0.1", 12345)) as client:
                    headers = {"X-GSME-Token": TOKEN}
                    self.assertEqual(client.get("/api/health", headers=headers).status_code, 200)
                    source = root / "examples" / "synthetic_reviewed_faq.docx"
                    with source.open("rb") as file:
                        submitted = client.post("/api/submit", headers=headers,
                            data={"tenant": tenant}, files={"file": (source.name, file)}).json()
                    self.assertEqual(submitted["state"], "queued")
                    ingestion = client.post("/api/ingest", headers=headers, json={
                        "tenant": tenant, "job_id": submitted["job_id"]})
                    self.assertEqual(ingestion.json()["state"], "review_needed")
                    version_id = submitted["version_id"]
                    inspection = client.get(f"/api/inspect/{version_id}?tenant={tenant}",
                                            headers=headers).json()
                    chunk = inspection["chunks"][0]
                    self.assertEqual(chunk["evidence_status"], "pending")
                    self.assertEqual(client.get(f"/api/source/{version_id}?tenant={tenant}",
                                                headers=headers).content, source.read_bytes())
                    self.assertIn("warnings", client.get(
                        f"/api/manifest/{version_id}?tenant={tenant}", headers=headers).json())
                    self.assertEqual(client.get(f"/api/embedding/{chunk['source_chunk_id']}?tenant={tenant}&version_id={version_id}&provider=fake",
                                                headers=headers).status_code, 404)
                    body = {"tenant": tenant, "version_id": version_id,
                            "document_id": submitted["document_id"], "provider": "fake"}
                    self.assertEqual(client.post("/api/publish", headers=headers, json=body).status_code, 400)
                    review = {"tenant": tenant, "version_id": version_id,
                              "chunk_id": chunk["source_chunk_id"], "decision": "approved",
                              "reviewer": "alice", "rationale": "Checked synthetic source paragraph",
                              "source_verified": True}
                    self.assertEqual(client.post("/api/review", headers=headers, json=review).status_code, 200)
                    self.assertEqual(client.post("/api/publish", headers=headers, json=body).json()["approved_candidates"], 1)
                    self.assertEqual(client.post("/api/index", headers=headers, json=body).json()["index"]["indexed"], 1)
                    inspection = client.get(f"/api/inspect/{version_id}?tenant={tenant}", headers=headers).json()
                    self.assertTrue(any(row["state"] == "indexed" for row in inspection["index_jobs"]))
                    preview = client.get(f"/api/embedding/{chunk['source_chunk_id']}?tenant={tenant}&version_id={version_id}&provider=fake",
                                         headers=headers).json()
                    self.assertEqual(preview["dimension"], 32)
                    self.assertEqual(len(preview["first_8_values"]), 8)
                    query = {"tenant": tenant, "provider": "fake",
                             "question": "What is the synthetic approval deadline?"}
                    hits = client.post("/api/search", headers=headers, json=query).json()
                    self.assertEqual(hits[0]["chunk_id"], chunk["source_chunk_id"])
                    reranked = client.post("/api/retrieve", headers=headers, json=query)
                    self.assertEqual(reranked.status_code, 200, reranked.text)
                    self.assertEqual(reranked.json()["reranking_status"], "completed")
                    self.assertEqual(reranked.json()["results"][0]["chunk_id"], chunk["source_chunk_id"])
                    self.assertEqual(client.post("/api/search", headers=headers,
                                                 json={**query, "tenant": "wrong"}).json(), [])
                    review.update(decision="rejected", rationale="Revoked", source_verified=False)
                    self.assertEqual(client.post("/api/review", headers=headers, json=review).status_code, 200)
                    self.assertEqual(client.post("/api/search", headers=headers, json=query).json(), [])
                    self.assertEqual(client.post("/api/retrieve", headers=headers, json=query).json()["status"], "no_eligible_evidence")
                    self.assertEqual(client.get(f"/api/embedding/{chunk['source_chunk_id']}?tenant={tenant}&version_id={version_id}&provider=fake",
                                                headers=headers).status_code, 404)
                    self.assertEqual(client.post("/api/reconcile", headers=headers, json=body).json()["deleted"], 1)
