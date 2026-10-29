import os
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from gsme_rag.storage.config import Settings
from gsme_rag.storage.providers import FakeEmbeddings, CohereEmbeddings
from gsme_rag.storage.providers import ProviderOperationError
from gsme_rag.storage.files import checksum
from gsme_rag.indexing import prepare_candidate
from gsme_rag.models import MarkdownChunk
from gsme_rag.models import ParseWarning
from gsme_rag.review import ReviewDecision, ReviewLedger


class StorageBoundaryTests(TestCase):
    def test_page_warning_codes_keep_raw_manifest_separate(self):
        from gsme_rag.storage.postgres import _page_warning_codes

        chunk = MarkdownChunk("c", 0, "reviewed text", "section", "manual.pdf", [],
                              source_page_start=30, source_page_end=31)
        parsed = SimpleNamespace(warnings=[
            ParseWarning("formula_unavailable", "raw details", "page 30"),
            ParseWarning("visual_gap", "raw details", "page 31"),
            ParseWarning("other_page", "raw details", "page 29"),
            ParseWarning("global", "raw details"),
        ])
        self.assertEqual(_page_warning_codes(parsed, chunk),
                         ["formula_unavailable", "visual_gap"])

    def test_fake_is_deterministic_but_not_semantic(self):
        one = FakeEmbeddings(16)
        other = FakeEmbeddings(16)
        self.assertEqual(one.embed_documents(["example"]), other.embed_documents(["example"]))
        self.assertEqual(len(one.embed_query("example")), 16)
        self.assertEqual(one.calls, 1)

    def test_external_indexing_requires_explicit_scope(self):
        from gsme_rag.storage.pipeline import process_index

        provider = FakeEmbeddings(16)
        provider.external = True
        with self.assertRaisesRegex(ValueError, "explicit tenant and version"):
            process_index(None, None, provider, None)

    def test_only_sanitized_provider_errors_enter_job_label(self):
        from gsme_rag.storage.pipeline import _index_error_label

        safe = ProviderOperationError("Cohere token count failed (HTTP 429); rate limited")
        self.assertIn("HTTP 429", _index_error_label(safe, "token_budget"))
        private = ValueError("private source text")
        self.assertNotIn("private source text", _index_error_label(private, "token_budget"))

    def test_cohere_cli_requires_scope_before_service_access(self):
        from contextlib import redirect_stderr
        from io import StringIO
        from gsme_rag.storage.cli import main
        import sys

        with patch.object(sys, "argv", ["gsme-rag-storage", "worker-once",
                                        "--provider", "cohere", "--index-only"]):
            with redirect_stderr(StringIO()) as errors:
                with self.assertRaises(SystemExit) as stopped:
                    main()
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("--tenant and --version-id", errors.getvalue())

    def test_cohere_fails_closed_without_permission(self):
        with patch.dict(
            os.environ, {"COHERE_API_KEY": "test", "GSME_EXTERNAL_CONTENT_APPROVED": "no"}
        ):
            with self.assertRaisesRegex(ValueError, "API key and"):
                CohereEmbeddings(1024)

    def test_cohere_provider_request_shapes_and_safe_errors(self):
        class Client:
            def __init__(self):
                self.requests = []

            def tokenize(self, **kwargs):
                self.requests.append(("tokenize", kwargs))
                return SimpleNamespace(tokens=[1, 2, 3])

            def embed(self, **kwargs):
                self.requests.append(("embed", kwargs))
                return SimpleNamespace(embeddings=SimpleNamespace(float=[[1.0] + [0.0] * 1023]
                                       * len(kwargs["texts"])))

        provider = CohereEmbeddings.__new__(CohereEmbeddings)
        provider.dimension = 1024
        provider.client = Client()
        self.assertEqual(len(provider.encode_tokens("approved test")), 3)
        self.assertEqual(len(provider.embed_documents(["approved test"])[0]), 1024)
        self.assertEqual(len(provider.embed_query("query")), 1024)
        self.assertEqual(provider.client.requests[0],
                         ("tokenize", {"text": "approved test", "model": "embed-v5.0-pro",
                                       "offline": False}))
        document = provider.client.requests[1][1]
        query = provider.client.requests[2][1]
        self.assertEqual((document["model"], document["input_type"]),
                         ("embed-v5.0-pro", "search_document"))
        self.assertEqual((query["model"], query["input_type"]),
                         ("embed-v5.0-fast", "search_query"))
        self.assertEqual((document["output_dimension"], document["truncate"]),
                         (1024, "NONE"))

        class PrivateError(Exception):
            status_code = 401

        provider.client.tokenize = lambda **kwargs: (_ for _ in ()).throw(
            PrivateError("secret value"))
        with self.assertRaisesRegex(RuntimeError, "HTTP 401") as raised:
            provider.encode_tokens("approved test")
        self.assertNotIn("secret value", str(raised.exception))

    def test_source_approved_budget_is_enforced_with_real_test_tokenizer(self):
        chunk = MarkdownChunk(
            "chunk-1",
            0,
            "Synthetic sentence",
            "question_answer",
            "example.docx",
            [],
            content_sha256="f" * 64,
        )
        provider = FakeEmbeddings(16)
        ledger = ReviewLedger(
            [
                ReviewDecision(
                    chunk.content_sha256,
                    chunk.chunk_id,
                    "approved",
                    "synthetic-fixture",
                    "source verified",
                    True,
                )
            ]
        )
        with self.assertRaisesRegex(ValueError, "budget"):
            prepare_candidate(
                chunk,
                ledger,
                tenant_id="t",
                model_version=provider.model_version,
                encode_tokens=provider.encode_tokens,
                max_tokens=1,
            )
        with self.assertRaisesRegex(ValueError, "not source-approved"):
            prepare_candidate(
                chunk,
                ReviewLedger(),
                tenant_id="t",
                model_version=provider.model_version,
                encode_tokens=provider.encode_tokens,
                max_tokens=50,
            )

    def test_streaming_checksum_matches_source_metadata(self):
        source = Path(__file__).resolve().parents[1] / "examples" / "synthetic_reviewed_faq.docx"
        from gsme_rag.parsing import parse_to_markdown

        self.assertEqual(checksum(source), parse_to_markdown(source).metadata.content_sha256)

    def test_qdrant_client_local_dense_and_optional_text_fusion(self):
        import warnings
        from qdrant_client import QdrantClient
        from gsme_rag.storage.vectors import QdrantEvidence

        with patch("gsme_rag.storage.vectors.QdrantClient", return_value=QdrantClient(":memory:")):
            index = QdrantEvidence("unused", "local_smoke", 4, hybrid=True)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            index.ensure_collection()
        point_id = "09806434-1111-4111-8111-111111111111"
        index.upsert(
            [
                (
                    point_id,
                    [1.0, 0.0, 0.0, 0.0],
                    {"tenant_id": "tenant-a", "version_id": "v1", "text": "synthetic deadline",
                     "model_version": "model-a"},
                )
            ]
        )
        self.assertEqual(len(index.query("tenant-a", [1.0, 0.0, 0.0, 0.0], 5)), 1)
        self.assertEqual(len(index.query("tenant-b", [1.0, 0.0, 0.0, 0.0], 5)), 0)
        self.assertEqual(len(index.query("tenant-a", [1.0, 0.0, 0.0, 0.0], 5, model_version="model-b")), 0)
        with self.assertRaisesRegex(ValueError, "invalid"):
            index.query("tenant-a", [float("nan"), 0.0, 0.0, 0.0], 5)
        self.assertEqual(len(index.query("tenant-a", [1.0, 0.0, 0.0, 0.0], 5, text="deadline")), 1)
        index.delete([point_id])
        self.assertEqual(index.query("tenant-a", [1.0, 0.0, 0.0, 0.0], 5), [])

    def test_csv_physical_line_of_multiline_record(self):
        from tempfile import TemporaryDirectory
        from gsme_rag.parsing.schedules import parse_schedule_records
        from gsme_rag.parsing import parse_to_markdown

        with TemporaryDirectory() as folder:
            source = Path(folder) / "full_schedules.csv"
            source.write_text(
                "Tech (nm),Flavor,Trial,Final,Tapeout,Est. Ship\n"
                '28,"HPC+\nRF",10/01/2026,10/15/2026,11/01/2026,12/01/2026\n'
                "14,RF,10/01/2026,10/15/2026,11/01/2026,12/01/2026\n",
                encoding="utf-8",
            )
            self.assertEqual(
                [r["source_row_number"] for r in parse_schedule_records(source)], [2, 4]
            )
            self.assertEqual(parse_to_markdown(source).metadata.csv_source_rows, [2, 4])

    def test_schedule_filters_are_parameterized_and_tenant_scoped(self):
        from datetime import date
        from gsme_rag.storage.postgres import EvidenceStore

        class Db:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, query, values):
                self.query, self.values = query, values
                return self

            def fetchall(self):
                return []

        db = Db()
        store = EvidenceStore("unused")
        store.connect = lambda: db
        store.schedules(
            "tenant-a",
            "shared",
            technology_nm=28,
            flavor="HPC+ RF",
            tapeout_on_or_after=date(2026, 10, 1),
        )
        self.assertIn("r.tenant_id=%s", db.query)
        self.assertIn("d.current_version_id=r.version_id", db.query)
        self.assertIn("lower(r.flavor)=lower(%s)", db.query)
        self.assertNotIn("HPC+ RF", db.query)
        self.assertEqual(db.values[:3], ["tenant-a", "tenant-a", "shared"])

    def test_worker_stops_when_it_loses_its_lease(self):
        from gsme_rag.storage.pipeline import _LeaseHeartbeat

        refreshed = Event()

        def rejected_refresh():
            refreshed.set()
            return False

        heartbeat = _LeaseHeartbeat(rejected_refresh, 30)
        heartbeat.interval = 0.01
        with heartbeat:
            self.assertTrue(refreshed.wait(1))
            self.assertTrue(heartbeat.lost.wait(1))
            with self.assertRaisesRegex(ValueError, "lease changed"):
                heartbeat.check()
        self.assertFalse(heartbeat.thread.is_alive())
