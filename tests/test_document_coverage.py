"""Coverage reports distinguish extraction presence, approval and source review."""

from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
import pymupdf

from gsme_rag.storage.admin import create_admin_app
from gsme_rag.storage.config import Settings
from gsme_rag.storage.coverage import document_coverage


class DocumentCoverageTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / "source.pdf"
        with pymupdf.open() as pdf:
            for _ in range(3):
                pdf.new_page()
            pdf.save(source)
        normalized = self.root / "normalized.md"; normalized.write_bytes(b"Synthetic evidence")
        manifest = self.root / "review_manifest.json"
        self.version = {"source_file": source.name, "original_path": str(source),
            "artifact_path": str(normalized), "source_sha256": sha256(source.read_bytes()).hexdigest(),
            "normalized_sha256": sha256(normalized.read_bytes()).hexdigest(), "is_current": True}
        self.manifest = {"source_sha256": self.version["source_sha256"],
            "normalized_sha256": self.version["normalized_sha256"], "parser_backend": "synthetic",
            "warnings": [{"code": "ocr_needs_review", "location": "page 2"}],
            "evidence_diagnostics": [{"page": 2, "reason": "synthetic"}]}
        manifest.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.store = Mock()
        self.store.inspect_version.return_value = {"version": self.version, "review_manifest_path": str(manifest),
            "status_counts": {"approved": 1, "pending": 2, "excluded": 1}}
        self.store.coverage_rows.return_value = [
            {"page_start": 1, "page_end": 1, "evidence_status": "approved", "chunks": 1, "verified_chunks": 0, "review_required": False},
            {"page_start": 2, "page_end": 2, "evidence_status": "pending", "chunks": 2, "verified_chunks": 0, "review_required": True},
            {"page_start": None, "page_end": None, "evidence_status": "excluded", "chunks": 1, "verified_chunks": 0, "review_required": True}]

    def test_all_pages_include_missing_evidence_and_clean_text_is_not_verified(self):
        report = document_coverage(self.store, "t", "v")
        self.assertEqual(report["source_page_count"], 3)
        first, second, third = report["pages"]
        self.assertEqual((first["approved"], first["verified_chunks"]), (1, 0))
        self.assertEqual((second["pending"], second["diagnostic_regions"]), (2, 1))
        self.assertTrue(second["ocr_indicated"])
        self.assertFalse(third["has_attributed_evidence"])
        self.assertEqual(report["unattributed_chunks"], 1)
        self.assertIn("not complete", report["note"])
        self.store.coverage_rows.assert_called_once_with("t", "v")

    def test_multi_page_attribution_does_not_claim_complete_page_verification(self):
        self.store.coverage_rows.return_value = [{"page_start": 1, "page_end": 2,
            "evidence_status": "approved", "chunks": 1, "verified_chunks": 1, "review_required": True}]
        report = document_coverage(self.store, "t", "v")
        self.assertEqual([p["verified_chunks"] for p in report["pages"]], [1, 1, 0])
        self.assertIn("must not be summed", report["note"])

    def test_api_requires_operator_and_checks_tenant_and_integrity(self):
        token, version = "synthetic-coverage-operator-token", str(uuid4())
        self.store.inspect_version.side_effect = lambda tenant, *args: (
            {"version": self.version, "review_manifest_path": str(self.root / "review_manifest.json"),
             "status_counts": {}} if tenant == "t" else None)
        with patch.dict(os.environ, {"GSME_REVIEW_AUTH_TOKEN": token}):
            with TestClient(create_admin_app(Settings("unused", "unused", self.root), self.store),
                            client=("127.0.0.1", 1234)) as client:
                path = f"/api/coverage/{version}?tenant=t"
                self.assertEqual(client.get(path).status_code, 401)
                headers = {"X-GSME-Token": token}
                self.assertEqual(client.get(path, headers=headers).status_code, 200)
                self.assertEqual(client.get(f"/api/coverage/{version}?tenant=other", headers=headers).status_code, 404)
                Path(self.version["artifact_path"]).write_bytes(b"Changed after ingestion")
                self.assertEqual(client.get(path, headers=headers).status_code, 409)

    def test_source_and_manifest_hash_changes_are_not_reported_as_valid_coverage(self):
        path = self.root / "review_manifest.json"
        self.manifest["source_sha256"] = "0" * 64
        path.write_text(json.dumps(self.manifest), encoding="utf-8")
        with self.assertRaises(ValueError):
            document_coverage(self.store, "t", "v")

    def test_invalid_but_hash_matched_pdf_is_reported_unreadable(self):
        source = Path(self.version["original_path"])
        source.write_bytes(b"not a PDF")
        self.version["source_sha256"] = sha256(source.read_bytes()).hexdigest()
        self.manifest["source_sha256"] = self.version["source_sha256"]
        (self.root / "review_manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        report = document_coverage(self.store, "t", "v")
        self.assertFalse(report["source_readable"])
        self.assertIsNone(report["source_page_count"])
        Path(self.version["original_path"]).write_bytes(b"changed PDF")
        with self.assertRaises(ValueError):
            document_coverage(self.store, "t", "v")
