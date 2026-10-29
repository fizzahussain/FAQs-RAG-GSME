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
    def setUp(self):
        self.settings = Settings("postgresql://unused", "http://127.0.0.1:6333", Path("data"), dimension=32)

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
            store.migrate(root)
            tenant = "admin-it-" + uuid4().hex
            with patch.dict(os.environ, {"GSME_REVIEW_AUTH_TOKEN": TOKEN,
                                      "GSME_AUTHORIZED_REVIEWERS": "alice"}):
                app = create_admin_app(settings, store)
                with TestClient(app, base_url="http://127.0.0.1:8010",
                                client=("127.0.0.1", 12345)) as client:
                    headers = {"X-GSME-Token": TOKEN}
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
                    self.assertEqual(client.post("/api/search", headers=headers,
                                                 json={**query, "tenant": "wrong"}).json(), [])
                    review.update(decision="rejected", rationale="Revoked", source_verified=False)
                    self.assertEqual(client.post("/api/review", headers=headers, json=review).status_code, 200)
                    self.assertEqual(client.post("/api/search", headers=headers, json=query).json(), [])
                    self.assertEqual(client.get(f"/api/embedding/{chunk['source_chunk_id']}?tenant={tenant}&version_id={version_id}&provider=fake",
                                                headers=headers).status_code, 404)
                    self.assertEqual(client.post("/api/reconcile", headers=headers, json=body).json()["deleted"], 1)
