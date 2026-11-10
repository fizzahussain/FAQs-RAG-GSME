"""Diagnostics identify the failing stage without recording source text or secrets."""

import json
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import UUID

from fastapi.testclient import TestClient

from gsme_rag.observability import LOGGER, REQUEST_ID, configure_console_logging, emit, operation
from gsme_rag.retrieval.reranking import CohereReranker, missing_rerank_settings
from gsme_rag.storage.admin import create_admin_app
from gsme_rag.storage.config import Settings


class OperatorLoggingTests(TestCase):
    def test_stage_failure_records_type_and_id_but_not_payload(self):
        token = REQUEST_ID.set("synthetic-request")
        try:
            with self.assertLogs("gsme_rag", level="INFO") as captured:
                emit("synthetic", question="private question", api_key="private API key", chunks=2)
                with self.assertRaises(ValueError):
                    with operation("rerank"):
                        raise ValueError("private technical source and credentials")
            output = "\n".join(captured.output)
            self.assertIn("rerank.failed", output)
            self.assertIn("ValueError", output)
            self.assertIn("synthetic-request", output)
            self.assertNotIn("private", output)
            self.assertNotIn("api_key", output)
        finally:
            REQUEST_ID.reset(token)

    def test_file_logging_rotates_and_reconfiguration_does_not_duplicate_handlers(self):
        previous_handlers, previous_level, previous_propagate = list(LOGGER.handlers), LOGGER.level, LOGGER.propagate
        try:
            with TemporaryDirectory() as temporary:
                root = Path(temporary)
                configure_console_logging(root)
                configure_console_logging(root)
                self.assertEqual(sum(getattr(h, "_gsme_operator_handler", False) for h in LOGGER.handlers), 2)
                emit("synthetic.file", chunks=1)
                contents = (root / "logs" / "console.log").read_text(encoding="utf-8")
                self.assertEqual(contents.count("synthetic.file"), 1)
                rotating = next(h for h in LOGGER.handlers if hasattr(h, "maxBytes"))
                self.assertEqual((rotating.maxBytes, rotating.backupCount), (2_000_000, 3))
                for handler in list(LOGGER.handlers):
                    if getattr(handler, "_gsme_operator_handler", False):
                        LOGGER.removeHandler(handler)
                        handler.close()
        finally:
            LOGGER.handlers[:] = previous_handlers
            LOGGER.setLevel(previous_level)
            LOGGER.propagate = previous_propagate

    def test_preflight_explains_missing_permission_and_links_http_request_to_logs(self):
        access_token = "synthetic-local-token-20-characters"
        env = {"GSME_REVIEW_AUTH_TOKEN": access_token, "COHERE_API_KEY": "private-key-sentinel",
               "GSME_EXTERNAL_CONTENT_APPROVED": "yes", "GSME_RERANK_CONTENT_APPROVED": ""}
        with patch.dict(os.environ, env):
            self.assertEqual(missing_rerank_settings(), ["GSME_RERANK_CONTENT_APPROVED=yes"])
            settings = Settings("postgresql://unused", "http://127.0.0.1:6333", Path("data"), dimension=32)
            with TestClient(create_admin_app(settings, Mock()), base_url="http://127.0.0.1:8010",
                            client=("127.0.0.1", 12345)) as client:
                with patch("gsme_rag.storage.admin._provider"), patch("cohere.ClientV2") as external:
                    with self.assertLogs("gsme_rag", level="INFO") as logs:
                        response = client.post("/api/retrieve", headers={"X-GSME-Token": access_token}, json={
                            "tenant": "local", "question": "private-question-sentinel", "provider": "cohere", "confirm_external": True})
                    external.assert_not_called()
                self.assertEqual(response.status_code, 400)
                self.assertIn("GSME_RERANK_CONTENT_APPROVED=yes", response.json()["detail"])
                self.assertIn("restart", response.json()["detail"])
                request_id = response.headers["X-GSME-Request-ID"]
                UUID(request_id)
                self.assertIsNone(REQUEST_ID.get())
                output = "\n".join(logs.output)
                self.assertIn(request_id, output)
                self.assertIn("rerank.preflight.blocked", output)
                self.assertNotIn("private-key-sentinel", output)
                self.assertNotIn("private-question-sentinel", output)
                self.assertNotIn(access_token, output)

    def test_recent_logs_are_authenticated_and_bounded(self):
        token = "synthetic-local-token-20-characters"
        with TemporaryDirectory() as temporary, patch.dict(os.environ, {"GSME_REVIEW_AUTH_TOKEN": token}):
            root = Path(temporary)
            (root / "logs").mkdir()
            (root / "logs" / "console.log").write_bytes(b"stage 1\nstage 2\nstage 3\n")
            settings = Settings("postgresql://unused", "http://127.0.0.1:6333", root, dimension=32)
            with TestClient(create_admin_app(settings, Mock()), base_url="http://127.0.0.1:8010",
                            client=("127.0.0.1", 12345)) as client:
                self.assertEqual(client.get("/api/logs").status_code, 401)
                headers = {"X-GSME-Token": token}
                response = client.get("/api/logs?limit=2", headers=headers)
                self.assertEqual(response.json()["entries"], ["stage 2", "stage 3"])
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(client.get("/api/logs?limit=201", headers=headers).status_code, 422)
