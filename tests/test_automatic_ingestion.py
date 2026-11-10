"""Ordinary text needs no review forms; extraction uncertainty stays out of search."""

import os
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from docx import Document
from qdrant_client import QdrantClient

from gsme_rag.storage.acceptance import clean_text_eligible, CLEAN_TEXT_POLICY
from gsme_rag.storage.admin import create_admin_app
from gsme_rag.storage.config import Settings
from gsme_rag.storage.postgres import EvidenceStore


class AutomaticAcceptanceTests(TestCase):
    def test_only_clean_text_is_automatic(self):
        self.assertTrue(clean_text_eligible("Normal source prose.", "section", False))
        self.assertTrue(clean_text_eligible("Find [here](<https://example.invalid/?lot=A17>).", "question_answer", False))
        for text, kind, review, status, warnings in [
            ("OCR LOT A17", "section", True, "pending", []),
            ("inferred cells", "table_row", True, "pending", []),
            ("plain", "section", False, "rejected", []),
            ("plain", "section", False, "excluded", []),
            ("here", "question_answer", False, "pending", ["link_target_missing"]),
            ("DPPM = (", "section", False, "pending", []),
            ("unknown \ue001 glyph", "section", False, "pending", []),
            ("broken \ufffd glyph", "section", False, "pending", []),
            ("(cid:34)", "section", False, "pending", []),
            ("Image text (OCR): A17", "section", False, "pending", []),
            ("Visual evidence on page 101", "section", False, "pending", []),
            ("```code```", "section", False, "pending", []),
            ("chart 20%, 80%", "visual", False, "pending", []),
        ]:
            with self.subTest(text=text):
                self.assertFalse(clean_text_eligible(text, kind, review, status, warnings))


DB = os.getenv("GSME_TEST_DATABASE_URL", "")
QDRANT = os.getenv("GSME_TEST_QDRANT_URL", "")


@skipUnless(DB and QDRANT and "/gsme_rag_test" in DB, "Requires isolated PostgreSQL and Qdrant")
class AutomaticIngestionIntegrationTests(TestCase):
    @staticmethod
    def _cleanup_vectors(collection):
        client = QdrantClient(url=QDRANT)
        try:
            if client.collection_exists(collection):
                client.delete_collection(collection)
        finally:
            client.close()

    def test_ingest_and_search_without_reviewer_or_notes_then_respect_revocation(self):
        token = "synthetic-automatic-ingest-token-20-characters"
        with TemporaryDirectory() as temporary:
            settings = Settings(DB, QDRANT, Path(temporary) / "data", collection="gsme_auto_" + uuid4().hex[:10], dimension=32)
            self.addCleanup(self._cleanup_vectors, settings.collection + "_synthetic")
            store = EvidenceStore(DB)
            store.migrate()
            source = Path(temporary) / "mixed.docx"
            doc = Document()
            for question, answer in [("Who coordinates the synthetic service?", "Muse coordinates the synthetic service."),
                                     ("Where is the schedule?", "The schedule can be found here."),
                                     ("What is the damaged formula?", "DPPM = (")]:
                doc.add_heading(question, 2)
                doc.add_paragraph(answer)
            doc.save(source)
            tenant = "auto-" + uuid4().hex
            with patch.dict(os.environ, {"GSME_REVIEW_AUTH_TOKEN": token, "GSME_AUTHORIZED_REVIEWERS": ""}):
                with TestClient(create_admin_app(settings, store), base_url="http://127.0.0.1:8010",
                                client=("127.0.0.1", 12345)) as client:
                    headers = {"X-GSME-Token": token}
                    with source.open("rb") as file:
                        submitted = client.post("/api/submit", headers=headers, data={"tenant": tenant},
                                                files={"file": (source.name, file)}).json()
                    client.post("/api/ingest", headers=headers, json={"tenant": tenant, "job_id": submitted["job_id"]})
                    body = {"tenant": tenant, "version_id": submitted["version_id"],
                            "document_id": submitted["document_id"], "provider": "fake"}
                    self.assertEqual(client.post("/api/ingest-ready", json=body).status_code, 401)
                    self.assertEqual(client.post("/api/ingest-ready", headers=headers,
                                                 json={**body, "tenant": "foreign"}).status_code, 404)
                    self.assertEqual(client.post("/api/ingest-ready", headers=headers,
                                                 json={**body, "provider": "cohere"}).status_code, 400)
                    response = client.post("/api/ingest-ready", headers=headers, json=body)
                    self.assertEqual(response.status_code, 200, response.text)
                    result = response.json()
                    self.assertEqual((result["state"], result["accepted"], result["held"], result["indexed"]),
                                     ("searchable", 1, 2, 1))
                    repeated = client.post("/api/ingest-ready", headers=headers, json=body).json()
                    self.assertEqual((repeated["newly_accepted"], repeated["indexed"]), (0, 0))
                    inspection = store.inspect_version(tenant, submitted["version_id"])
                    self.assertEqual([c["evidence_status"] for c in inspection["chunks"]], ["approved", "pending", "pending"])
                    clean = inspection["chunks"][0]
                    self.assertFalse(clean["source_verified"])
                    self.assertEqual(clean["approval_basis"], CLEAN_TEXT_POLICY)
                    query = {"tenant": tenant, "provider": "fake", "question": "Who coordinates the synthetic service?"}
                    hits = client.post("/api/retrieve", headers=headers, json=query).json()["results"]
                    self.assertEqual(len(hits), 1)
                    self.assertFalse(hits[0]["source_verified"])
                    self.assertEqual(hits[0]["approval_basis"], CLEAN_TEXT_POLICY)
                    # Ordinary ingestion cannot silently undo a later human rejection.
                    store.record_review(tenant, submitted["version_id"], clean["source_chunk_id"],
                                        "rejected", "test-operator", "Revoked synthetic fixture", False)
                    rejected = client.post("/api/ingest-ready", headers=headers, json=body).json()
                    self.assertEqual((rejected["accepted"], rejected["held"], rejected["excluded"]), (0, 2, 1))
                    self.assertEqual(client.post("/api/retrieve", headers=headers, json=query).json()["results"], [])
                    # Neither missing links nor broken equations may be indexed by forged policy decisions.
                    with store.connect() as db:
                        uncertain = db.execute("SELECT * FROM evidence_chunks WHERE version_id=%s AND evidence_status='pending'",
                                               (submitted["version_id"],)).fetchall()
                        for row in uncertain:
                            self.assertFalse(db.execute("SELECT evidence_acceptance_allowed(false,%s,%s,%s::jsonb,%s,%s) AS allowed",
                                (CLEAN_TEXT_POLICY, row["review_required"], json.dumps(row["warning_codes"]),
                                 row["chunk_type"], row["evidence_text"])).fetchone()["allowed"])
