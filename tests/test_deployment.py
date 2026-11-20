"""Managed runtime boundaries and health probes; no external provider calls."""

import base64
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import psycopg

from gsme_rag.hosted.app import HostedSettings, create_hosted_app
from gsme_rag.hosted.deploy import create_app
from gsme_rag.storage.admin import create_admin_app
from gsme_rag.storage.config import Settings
from gsme_rag.storage.vectors import QdrantEvidence


class DeploymentTests(TestCase):
    def test_managed_qdrant_key_uses_https_and_is_never_put_in_url(self):
        with patch.dict(os.environ, {"GSME_QDRANT_API_KEY": "synthetic-managed-secret"}), \
             patch("gsme_rag.storage.vectors.QdrantClient") as client:
            QdrantEvidence("https://vectors.example", "test", 1024)
            self.assertEqual(client.call_args.kwargs["api_key"], "synthetic-managed-secret")
            self.assertEqual(client.call_args.kwargs["url"], "https://vectors.example")
            with self.assertRaisesRegex(ValueError, "require HTTPS"):
                QdrantEvidence("http://vectors.example", "test", 1024)

    def test_render_origin_fallback_and_explicit_override(self):
        with patch.dict(os.environ, {"GSME_HOSTED_ORIGIN": "", "RENDER_EXTERNAL_URL": "https://muse.example"}):
            self.assertEqual(HostedSettings.from_env().origin, "https://muse.example")
        with patch.dict(os.environ, {"GSME_HOSTED_ORIGIN": "https://custom.example", "RENDER_EXTERNAL_URL": "https://muse.example"}):
            self.assertEqual(HostedSettings.from_env().origin, "https://custom.example")

    def test_liveness_is_independent_readiness_checks_schema_vectors_and_files(self):
        with TemporaryDirectory() as temporary:
            settings = Settings("unused", "unused", Path(temporary))
            store, vectors, db = Mock(), Mock(), MagicMock()
            store.connect.return_value = MagicMock()
            store.connect.return_value.__enter__.return_value = db
            migration_path = Path(__file__).parents[1] / "src/gsme_rag/storage/migrations"
            db.execute.return_value.fetchall.return_value = [{"name": p.name} for p in migration_path.glob("*.sql")]
            with patch("gsme_rag.hosted.app.QdrantEvidence", return_value=vectors), \
                 patch("gsme_rag.hosted.app.CohereEmbeddings") as provider:
                with TestClient(create_hosted_app(settings, store=store, auto_worker=False),
                                base_url="http://127.0.0.1:8020") as client:
                    self.assertEqual(client.get("/health/live").status_code, 200)
                    store.connect.assert_not_called()
                    self.assertEqual(client.get("/health/ready").status_code, 200)
                    vectors.client.close.assert_called_once()
                    vectors.client.get_collections.side_effect = RuntimeError("PRIVATE-CONNECTION-DETAILS")
                    result = client.get("/health/ready")
                    self.assertEqual(result.status_code, 503)
                    self.assertFalse(result.json()["checks"]["qdrant"])
                    self.assertNotIn("PRIVATE-CONNECTION", result.text)
                    vectors.client.get_collections.side_effect = None
                    db.execute.return_value.fetchall.return_value = []
                    self.assertFalse(client.get("/health/ready").json()["checks"]["postgres"])
                    store.connect.side_effect = psycopg.OperationalError("PRIVATE-DATABASE-URL")
                    result = client.get("/health/ready")
                    self.assertEqual(result.status_code, 503)
                    self.assertNotIn("PRIVATE-DATABASE", result.text)
                    self.assertEqual(client.get("/health/live").status_code, 200)
                provider.assert_not_called()

    def test_remote_admin_protects_every_path_and_does_not_weaken_local_console(self):
        token = "synthetic-remote-operator-secret-at-least-32"
        settings = Settings("unused", "unused", Path("data"))
        store = Mock(); store.list_versions.return_value = []
        with patch.dict(os.environ, {"GSME_REVIEW_AUTH_TOKEN": token}):
            parent = FastAPI()
            parent.mount("/operator", create_admin_app(settings, store, remote_origin="https://muse.example"))
            with TestClient(parent, base_url="https://muse.example", client=("203.0.113.10", 1234)) as client:
                for path in ("/operator/admin", "/operator/assets/console.js", "/operator/docs", "/operator/openapi.json", "/operator/api/versions?tenant=t"):
                    self.assertEqual(client.get(path).status_code, 401, path)
                    self.assertEqual(client.get(path, headers={"X-GSME-Token": token}).status_code, 401)
                for header in ("Basic invalid-base64", "Bearer " + token, "Basic " + base64.b64encode("öperator:wrong".encode()).decode()):
                    self.assertEqual(client.get("/operator/admin", headers={"Authorization": header}).status_code, 401)
                auth = ("operator", token)
                page = client.get("/operator/admin", auth=auth)
                self.assertEqual(page.status_code, 200)
                self.assertIn('/operator/assets/console.js', page.text)
                self.assertIn('href="/operator/docs"', page.text)
                self.assertNotIn(token, page.text)
                self.assertEqual(page.headers["cache-control"], "no-store")
                self.assertEqual(client.get("/operator/api/versions?tenant=t", auth=auth).status_code, 200)
                self.assertEqual(client.get("/operator/api/versions?tenant=t", auth=auth,
                                           headers={"Origin": "https://evil.example"}).status_code, 403)
            with TestClient(create_admin_app(settings, store), client=("203.0.113.10", 1234)) as client:
                self.assertEqual(client.get("/admin", auth=("operator", token)).status_code, 403)
            with self.assertRaises(ValueError):
                create_admin_app(settings, store, remote_origin="http://muse.example")
        with patch.dict(os.environ, {"GSME_REVIEW_AUTH_TOKEN": "short-token-but-over-20"}):
            with self.assertRaises(ValueError):
                create_admin_app(settings, store, remote_origin="https://muse.example")

    def test_managed_factory_migrates_and_shares_store_before_mounting_admin(self):
        with TemporaryDirectory() as temporary:
            settings = Settings("unused", "unused", Path(temporary))
            store, app = Mock(), FastAPI()
            hosted = HostedSettings(origin="https://muse.example")
            with patch("gsme_rag.hosted.deploy.from_env", return_value=settings), \
                 patch("gsme_rag.hosted.deploy.HostedSettings.from_env", return_value=hosted), \
                 patch("gsme_rag.hosted.deploy.EvidenceStore", return_value=store), \
                 patch("gsme_rag.hosted.deploy.create_hosted_app", return_value=app) as factory, \
                 patch.dict(os.environ, {"GSME_HOSTED_ADMIN_ENABLED": "yes", "GSME_REVIEW_AUTH_TOKEN": "synthetic-operator-credential-more-than-32"}):
                result = create_app()
                store.migrate.assert_called_once()
                factory.assert_called_once_with(settings, hosted, store=store)
                self.assertIs(result, app)
                self.assertTrue(any(getattr(route, "path", None) == "/operator" for route in app.routes))

    def test_combined_runtime_keeps_chat_assets_separate_and_docs_behind_operator_gate(self):
        with TemporaryDirectory() as temporary:
            settings = Settings("unused", "unused", Path(temporary))
            store = Mock()
            store.connect.return_value = MagicMock()
            hosted = HostedSettings(origin="https://muse.example")
            with patch("gsme_rag.hosted.deploy.from_env", return_value=settings), \
                 patch("gsme_rag.hosted.deploy.HostedSettings.from_env", return_value=hosted), \
                 patch("gsme_rag.hosted.deploy.EvidenceStore", return_value=store), \
                 patch("gsme_rag.hosted.deploy.create_hosted_app",
                       side_effect=lambda s, h, store: create_hosted_app(s, h, store=store, auto_worker=False)), \
                 patch.dict(os.environ, {"GSME_HOSTED_ADMIN_ENABLED": "yes",
                                         "GSME_REVIEW_AUTH_TOKEN": "synthetic-operator-credential-more-than-32"}):
                with TestClient(create_app(), base_url=hosted.origin, client=("203.0.113.10", 1234)) as client:
                    self.assertIn("Document chat", client.get("/").text)
                    self.assertEqual(client.get("/assets/chat.js").status_code, 200)
                    self.assertEqual(client.get("/operator/assets/console.js").status_code, 401)
                    self.assertEqual(client.get("/operator/docs").status_code, 401)
                    auth = ("operator", "synthetic-operator-credential-more-than-32")
                    self.assertEqual(client.get("/operator/assets/console.js", auth=auth).status_code, 200)
                    docs = client.get("/operator/docs", auth=auth)
                    self.assertEqual(docs.status_code, 200)
                    self.assertIn("https://cdn.jsdelivr.net", docs.headers["content-security-policy"])
                    self.assertNotIn("unsafe-inline", client.get("/").headers["content-security-policy"])
                    self.assertEqual(client.get("/operator/openapi.json", auth=auth).status_code, 200)
